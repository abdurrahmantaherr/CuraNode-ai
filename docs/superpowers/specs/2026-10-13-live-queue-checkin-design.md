# Live Queue, Check-in and Walk-ins v1 (FR17, FR19, FR24) — design

**Feature:** Clinic front-desk check-in, the live queue, and walk-ins, version 1
**Owner:** Zarwa · **Branch:** `feature/live-queue`, stacked on `feature/appointment-booking`
**Status:** awaiting review

## 1. Goal

A booked patient arrives and the clinic admin checks them in; a patient with no
booking is added as a walk-in. Everyone joins one queue per doctor session, in
arrival order. The admin sends patients into the room, marks them seen, and can
move an urgent case forward. The patient sees their live position and an
estimated wait; the doctor sees their queue. Success means:

- A patient's position is always right to within one place (PRD Objective 3.2).
- Two patients never get the same place in a queue, even if two staff click at once.
- A session never goes over its cap (`max_tokens`), counting booked **and**
  walk-in patients together (D7 R14).
- Walk-ins never take bookable times away from online patients.
- The clinic admin never sees medical records, only names, times and queue state.
- Everything works in English and Urdu, and every action works without JavaScript.

**This is the basic v1. It must be made scalable and efficient later** (see section 7).

**Requirements covered:** FR17 (patient sees live position and estimated wait),
FR19 (admin checks in, marks seen, reorders), FR24 (doctor sees their queue).

**Out of scope for v1:** notifications (FR20); walk-ins for people with **no
account** (a required follow-up, section 7); doctor-side queue buttons; Redis, a
position cache and instant push; admin page auto-refresh; undo for "seen" and for
"check in"; queue statistics or reports; fees.

## 2. Decisions already made

| Question | Decision |
|---|---|
| Scope | **Booked patients and walk-ins** in v1 (FR17, FR19, FR24). |
| Who can be a walk-in | A patient who **already has an account**. Walk-ins with no account are a required follow-up. |
| How the desk finds a walk-in | Search by **passport number or name**. Passport numbers are **masked** in results (last 4 characters visible); an exact full number matches. |
| Queue order | **Arrival order**: first checked in, first seen. The admin can move someone forward. |
| Who moves patients | The **clinic admin only**. The doctor and the patient only view. |
| Estimated wait | **(people ahead, plus the one in the room) × the session's minutes per patient.** |
| How live is the patient page | It **refreshes itself every ~15 seconds**; a Refresh button covers no-JavaScript. |
| Where the data lives | The **existing shared `queue_entry` table**, no migration (Approach 1). |
| Redis | **Not in v1.** The database is the source of truth; a cache is a later speed-up behind the existing `Cache` interface. |

## 3. Design

### 3.1 Building blocks

Reused unchanged: `schedule.service.get_effective_availability` and `lock_doctor`
(sessions and the per-doctor lock); `booking.slots` (window bounds and Pakistan
time); the booking `appointment` rows and their `uq_appointment_active_slot`
index; the existing admin page guard and the admin clinic scoping
(`actor.clinic_ids`); the audit writer and the rate-limit helpers.

New:

| File | Responsibility |
|---|---|
| `backend/app/queueing/order.py` | **Pure logic, no database.** Position, estimated wait, next token, move up, make next. |
| `backend/app/queueing/schemas.py` | Constants (queue states, the `walk_in` channel, limits) and small result types. |
| `backend/app/queueing/service.py` | Every action and view: check in, send in, mark seen, put back, move, add a walk-in, find a patient, the admin's, the patient's and the doctor's views. |
| `backend/app/web/queue.py` | The admin, patient and doctor pages, wired in `main.py` like `web/booking.py`. |
| `frontend/templates/queue/*.html` | The admin queue page, the patient queue card, the doctor's queue list. |

Touched, additively: `backend/app/db/models.py` (the `QueueEntry` model; no
migration); `backend/app/booking/service.py` (`_doctor_held` ignores walk-ins);
`backend/app/web/router.py` (admin nav "Queue", the doctor dashboard panel, the
patient appointment page hook); `backend/app/errors.py` (`QueueRefused`);
`backend/app/settings.py` and `backend/app/deps.py` (two rate limits);
`backend/app/audit/writer.py` (audit constants); the i18n files.

### 3.2 Rules

**A queue** is one doctor's **session**: one effective window on one date at one
clinic, the same unit as the cap. Only **verified** doctors with an active
affiliation at the admin's clinic have queues.

**Check in** (a booked patient):
- Only an appointment with status `booked` whose Pakistan date is **today**, for a
  doctor at one of the admin's own clinics, while the session has not ended.
  Arriving early is fine.
- Refused if the doctor is **no longer verified** (the page says the doctor is
  not available), since an unverified doctor cannot see patients.
- It creates a `queue_entry` (next token, state `waiting`) and sets the
  appointment to `checked_in`. Checking in twice does nothing.

**Order, position and wait:**
- Waiting patients are ordered by token number. A new check-in or walk-in gets the
  next unused token of the session (the highest token ever used there, plus 1).
- A patient's **position** is the number of waiting patients with a smaller token,
  plus one.
- The **estimated wait** in minutes is (waiting patients ahead + 1 if someone is in
  the room) × the session's `slot_minutes`. Position 1 with nobody in the room
  means "you're next".
- A patient's token number is internal. Screens show position, never the token.

**Send in:** any waiting patient (the first may have stepped out). Refused if
someone is already in the room of that session.

**Mark seen:** only from "in the room". The entry becomes `served` and the
appointment `done`. Final in v1.

**Put back:** an in-room patient goes back to waiting, **keeping their place**
(their token is unchanged), and the appointment stays `checked_in`.

**Move forward:** two actions on a **waiting** patient, both inside the doctor lock:
- **Move up** swaps them with the waiting patient directly ahead.
- **Make next** puts them first among the waiting patients.
Both **permute the existing token numbers among the waiting entries**, so tokens
stay unique and in-room or served tokens are untouched.

**Walk-in:**
- The desk picks a verified doctor at its clinic and one of today's sessions that
  has not ended, then a registered patient from the search.
- Refused if the session is **full** (the count includes booked and walk-in
  patients, all active appointments of the session), if the patient already has
  an active appointment with that doctor today ("check them in instead"), or if
  the patient is already in a queue.
- It creates an `appointment` (channel `walk_in`, status `checked_in`, the
  session's `schedule_id`) and a `queue_entry` together. Its
  `slot_start` is the arrival time, but never before the session's start; if that
  exact instant is already held, the next free microsecond. It is off the booking
  grid on purpose.
- A walk-in **counts toward the cap but never blocks a bookable time**: booking's
  "one patient at a time" check (`_doctor_held`) ignores `walk_in` appointments.

**Walk-in search:** passport number or name; at least 3 characters; at most 8
results; each result shows the name and the **masked** passport number (last 4
characters). An exact full passport number matches that patient. The search is
rate-limited and audited (only the result count is logged, never the text typed).

**Who sees what:**
- **Admin:** names, times, walk-in or booked, and queue state only. No medical
  records, ever.
- **Doctor:** names, times and state of their own queue only.
- **Patient:** only their own position, state and the number waiting. Never another
  patient's name.

**Writes:** every action takes the doctor lock first, re-reads the queue, then
writes. Each action writes one `audit_log` row with ids only (appointment, queue
entry, doctor, clinic, session), never names or search text. Desk actions and
lookups have their own per-user rate limits. A foreign or unknown id is one
identical `NotFound` (the page goes back to the queue).

### 3.3 Screens

All screens reuse existing classes (no new CSS), post-then-redirect actions, EN/UR
with right-to-left, and times forced left-to-right.

**Clinic admin, `/{locale}/admin/queue`** (a fourth item in the admin menu). One
panel per session today at the admin's clinics: doctor, hospital, hours, "places
used X of Y". Inside each panel:
- **In the room:** the patient, with *Mark seen* and *Put back*.
- **Waiting:** numbered by position, each with name, arrival time, a Walk-in badge
  where it applies, and *Send in*, *Move up*, *Make next*.
- **Expected:** booked patients not yet arrived, with *Check in*.
- **Seen today:** a collapsed list.
- **Add a walk-in:** a search box and, below it, the results with *Add to queue*.
Each action reloads the page with a success banner; refusals show an error
banner. The page does not auto-refresh.

**Patient, the queue card on `/{locale}/patient/appointments/{id}`** (today's
appointments only):
- Booked, not arrived: "Not checked in yet. Tell the front desk when you arrive."
- Waiting: "You are #N in line, about M minutes."
- Next: "You're next. Please be ready."
- In the room: "It's your turn. Please go in."
- Seen: "Seen. Thank you."
- Shows "last updated HH:MM", refreshes itself every ~15 seconds, and has a Refresh
  button for no-JavaScript. In the appointments list, today's rows get a small
  badge such as "In queue #N".

**Doctor, "Today's queue" (view only)** on the home page beside "Appointments
today", and at the top of `/{locale}/doctor/appointments` when the chosen day is
today: who is in the room, the waiting patients in order, and the patients still
expected. Names and times only.

### 3.4 Data

**No new table and no migration.** A `QueueEntry` model maps the existing shared
`queue_entry` table.

What is already known about it (D7 spec, checked 2026-10-10): `appointment_id`
NOT NULL and UNIQUE; `token_number` int NOT NULL, with **no uniqueness and no
upper bound**; `state` CHECK IN (`waiting`, `in_room`, `served`). Nothing else
about its columns is recorded in the repo, so the model is finished only after the
schema owner's read-only check (section 4).

Written to `appointment`: status `booked → checked_in → done`; walk-in rows as
above. A session's queue entries are found through their appointment's
`schedule_id` and `slot_start` falling inside the session window, exactly as
booking counts a session.

Because the database enforces neither unique tokens nor the cap, the service does,
under the doctor lock; `uq_appointment_active_slot` remains the backstop for
appointment times.

### 3.5 Errors the user sees

The page redraws with an error banner, never a blank error. New messages (EN and
UR): the appointment is not for today; the session has ended; that doctor is not
available; already checked in;
someone is already in the room; the session is full; this patient already has an
appointment with this doctor today; this patient is already in a queue; type at
least 3 characters; that patient is no longer waiting (two staff acted at once);
not found. They are raised as `QueueRefused(reason)`, a 409, like `BookingRefused`.

## 4. Dependencies and risks

- **Schema owner's read-only check of `queue_entry` (blocks the build, not the
  plan).** The queries to send them, which return structure and counts only:
  ```sql
  SELECT column_name, data_type, is_nullable, column_default
  FROM information_schema.columns WHERE table_name = 'queue_entry' ORDER BY ordinal_position;
  SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'queue_entry'::regclass;
  SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'queue_entry';
  SELECT count(*) FROM queue_entry;
  SELECT booking_channel, count(*) FROM appointment GROUP BY 1;
  ```
  Also ask whether any teammate's code already writes `queue_entry`. If it has
  extra required columns, they are filled from the appointment. If something we
  need is missing, the fallback is a migration (Approach 2) with the schema
  owner's approval. The last query shows the `booking_channel` names in use so
  `walk_in` does not clash.
- **Branch stacking.** The queue needs booking's code, which is not in `dev` yet.
  `feature/live-queue` starts from `feature/appointment-booking`, so its pull
  request goes in after booking's.
- **The estimate is simple**, not a prediction of real consultation length.
- **The in-memory rate limiter** works for one server process only (existing
  limitation, listed in section 7).
- **The real database wins over the TDD:** its states are `waiting`, `in_room`,
  `served`, not `expected`, `called` and so on, and it has no `position` column.
- **Shared-table writes:** `queue_entry` is shared with the wider product; only
  rows for appointments created or checked in through this feature are written.

## 5. Tests

In-memory SQLite and the existing fake Supabase. No network.

1. Pure rules: position, estimated wait, next token, move up, make next.
2. Check-in: today only; booked only; only the admin's own clinic; twice does
   nothing; refused after the session ends; tokens always unique and increasing.
3. State flow: send in; one in the room; mark seen; put back keeps its place; the
   appointment status stays in step.
4. Move forward: move up and make next permute tokens, tokens stay unique, only
   waiting entries move.
5. Walk-in: registered only; counts toward the cap; refused when full; never blocks
   a bookable time; refused with a booking today; refused if already queued;
   `slot_start` never before the session start and never collides.
6. Walk-in search: minimum length; at most 8 results; masked passport numbers; an
   exact number matches; admin only; rate limit; audit logs only the count.
7. Locking: the lock statement is asserted; two check-ins at once get distinct
   tokens; the cap holds with walk-ins.
8. Screens: admin page (role gating, clinic scoping, escaping, Urdu, no hidden
   data); the patient card in every state with no other names and a foreign id
   going to not-found; the doctor's list view-only and own-queue only; a pending
   doctor sees nothing.
9. Edge cases: two admins acting at the same moment; a session ending mid-day;
   Pakistan midnight (00:10 local is the previous day in UTC); a booked patient
   who never arrives never enters the queue.
10. Audit rows hold ids only.
11. Regression: every existing booking test still passes with walk-ins present.
12. English and Urdu both render; `missing_keys()` is empty; placeholders match.

## 6. Done means

- All new tests pass and the existing suite still passes; `ruff check` and
  `ruff format --check` are clean on the changed files.
- A manual run in the browser on the local demo: book, check in, send in, mark
  seen, a walk-in, a reorder, the patient card updating, the doctor's view, Urdu,
  and phone width.
- An independent review of the whole branch.
- The schema owner's `queue_entry` check confirmed before anything touches the
  shared database. No migration is needed unless that check forces the fallback.
- Zarwa opens the pull request (after booking's).

## 7. Required follow-ups and "make it scalable" list

**Required follow-up (not optional): walk-ins for people with no account.** The
database requires a login account behind every patient. Guest patients (name and
phone only, linkable to an account if they sign up later) need a change to the
shared patient tables and the schema owner's approval. It is its own feature.

**Later, to make the queue scalable and efficient:**
- A Redis position cache behind the existing `Cache` interface, once many patients
  poll at once.
- Instant push (server-sent events) instead of the 15-second refresh.
- Multi-process-safe rate limiting (the in-memory limiter is single-process).
- Admin page auto-refresh for several desk staff.
- Doctor-side buttons (call next, mark seen).
- Undo for "seen" and "check in"; a queue-position cache; a better wait estimate
  from real consultation times; notifications (FR20).
