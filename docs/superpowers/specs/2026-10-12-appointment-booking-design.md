# Appointment Booking v1 (P5, FR15 + FR16) — design

**Feature:** Patient appointment booking, version 1
**Owner:** Zarwa · **Branch:** `feature/appointment-booking`, from `dev`
**Status:** awaiting review

## 1. Goal

A patient finds a **verified** doctor, sees the doctor's real open times, books
one, and can cancel or reschedule it. The doctor sees their day's appointments.
Success means:

- Two patients can never hold the same time with a doctor.
- A session never goes over its cap (`max_tokens`).
- Time off and extra hours (D7) are respected.
- Booking never grants the doctor access to a record (consent stays separate, FR4).
- Everything works in English and Urdu.

**Out of scope for v1:** the live queue, check-in and walk-ins (FR17, FR19, a
later feature); notifications (FR20); fees, ratings, years of experience (not in
the schema); token numbers; sharing the record at booking time; waitlists; a
clinic-admin booking screen.

## 2. Decisions already made

| Question | Decision |
|---|---|
| What does the doctor see about a booked patient? | **Name, time and status only.** No passport number, nothing clinical. Opening the record still needs the patient's own grant (FR4). |
| Time limits | Book from **30 minutes from now** up to **14 days ahead**. Cancel or reschedule **any time before the appointment starts**. |
| What does the confirmation show? | Doctor, hospital, date and time. **No token number** and no "slot N of M". The real token is assigned at check-in by the live queue, later. |
| How is double booking prevented? | **Lock in the app plus one database safety index.** The booking code locks the doctor and re-checks; a partial unique index makes the database itself refuse two active bookings at the same time. |
| Which doctors are visible to patients? | **Verified doctors only**, with at least one live hospital. This settles the question D7 left to this feature (D7 decision 6). |
| Reschedule | **One step**: book the new time and cancel the old one in the same transaction. A failed rebook leaves the old booking untouched. |

## 3. Design

### 3.1 Building blocks

Reused unchanged:

- `schedule.service.get_effective_availability` for a doctor's session windows
  (time off applied, extra hours added, each window with its effective cap).
- `schedule.service.lock_doctor` / `lock_statement`, the per-doctor
  `SELECT ... FOR UPDATE` lock (SQLite ignores it; tests assert the statement, as D7's AC-12 does).
- The existing `appointment` table (see 3.4 for the one change).
- The page-guard and redirect-after-post patterns used by the consent pages.

New:

| File | Responsibility |
|---|---|
| `backend/app/booking/__init__.py` | Package marker. |
| `backend/app/booking/slots.py` | **Pure logic, no database.** From a session window, the doctor's `slot_minutes`, the already-taken times, a "now" and a horizon, it returns the bookable times. Owns the grid, the last-slot-fits rule and the cap. |
| `backend/app/booking/schemas.py` | Constants (statuses that count, channel value, limits) and small result types. |
| `backend/app/booking/service.py` | Search doctors, list bookable times, book, cancel, reschedule, the patient's appointments, the doctor's day. Owns every rule in 3.2. |
| `backend/app/web/booking.py` | The patient and doctor pages, wired in `main.py` like `web/documents.py`. |
| `frontend/templates/booking/*.html` | Find a doctor, doctor page, confirmation, my appointments, doctor's day list. |

Touched, additively: `backend/app/main.py` (include the router), `backend/app/web/router.py`
(nav entries only), `backend/app/deps.py` and `backend/app/settings.py` (a
`booking_write_rate_limit_per_minute` setting and `check_booking_write_rate`, same shape as
the consent and document ones), `backend/app/audit/writer.py` (three constants),
`backend/app/db/models.py` (the index on `Appointment`), the i18n files.

### 3.2 Rules

**Bookable times.**

- A time is `session start + n × slot_minutes`, and the whole slot must fit
  inside the session. (09:00–13:00 at 15 minutes gives 16 times; leftover
  minutes are not a whole turn.)
- Sessions come from `get_effective_availability`: time off hides times, extra
  hours add them, each window keeps its own effective cap.
- Only times from now + 30 minutes to today + 14 days. All wall-clock times are
  Pakistan time (`dbtypes.PKT`), as in D7; `slot_start` is stored as an aware
  timestamp.

**Blocked times.**

- **Taken:** an active appointment with the same `schedule_id` and `slot_start`.
- **Full:** the session already holds `max_tokens` active appointments. A
  session is one effective window on one date; an appointment belongs to it when
  it has the window's `schedule_id` and its local start falls in `[start, end)`
  on that date. Windows of one doctor never overlap (D7 R1/R1b/R4), so
  membership is unambiguous. Walk-ins will share the same cap later (D7 R14).
- "Active" means `booked`, `checked_in` or `done`. `cancelled` frees both the
  time and the cap.

**Patient rules.**

- A patient cannot hold two active appointments that overlap in time, with any doctor.
- A patient cannot hold two active appointments with the same doctor on the same day.
- Only a patient with a completed profile can book, and only for themselves.

**Writes.**

- Every appointment sets `schedule_id` (D7 R14.3) and `booking_channel = "online"`.
- Cancel: the patient's own appointment, status `booked` only. It becomes
  `cancelled` and is never deleted.
- Reschedule: lock the doctor, validate the new time with the same rules while
  ignoring the old appointment, insert the new one, mark the old one `cancelled`,
  commit once. A rescheduled booking may move to a different session or hospital
  of the same doctor.
- Each write: one `audit_log` row (`appointment.book`, `.cancel`, `.reschedule`)
  with ids only (appointment, doctor, clinic), never names, and a per-patient
  write rate limit.

**Concurrency.** Every booking and reschedule runs: lock the doctor, re-read the
taken times and the session's count, then write. The database index (3.4) is the
backstop: if a race still slips past, the second insert fails and the patient
sees "someone just booked that time" with fresh times.

**Probing.** A foreign or nonexistent appointment id is one
identical `NotFound`.

### 3.3 Screens

Follow the prototype (Find a Doctor, then pick a time, then Confirmed, then
Appointments), minus fee, ratings and experience.

Patient:

- `/{locale}/patient/doctors`: search by doctor name or hospital, plus specialty
  chips built from the verified doctors that exist. Cards show name, specialty, a
  "Verified" badge and their live hospitals. Only verified doctors with a live hospital.
- `/{locale}/patient/doctors/{doctor_id}`: choose a hospital when there are
  several, then the next 14 days. Only days with free times appear; each time is
  a button; taken and full times are absent. Choosing a time opens a confirm step
  (doctor, hospital, date, time).
- Confirmation: doctor, hospital, date, time, status "Confirmed", link to my appointments.
- `/{locale}/patient/appointments`: **Upcoming** (not cancelled, not done, starting now or later) and **Past** (everything else, including cancelled). Upcoming rows have
  **Cancel** (two-step confirm like revoking access) and **Reschedule**, which
  reopens the doctor page with the old booking marked "you're moving this one".
- Patient nav gains "Find a doctor" and "Appointments".

Doctor:

- A "Today's appointments" panel on the doctor home page, above "My patients":
  time, patient name, status.
- `/{locale}/doctor/appointments`: the same list with a date picker (default
  today). Cancelled ones show as muted "Cancelled" rows. Name, time and status
  only. A pending doctor sees nothing.

Everywhere: English and Urdu (right-to-left in Urdu), and every action works
without JavaScript (post, then redirect).

### 3.4 Data and migration

No new table. One hand-written, additive Alembic revision after the current head
(check `alembic heads` when planning): a partial unique index

`uq_appointment_active_slot` on `appointment(schedule_id, slot_start) WHERE status <> 'cancelled'`.

It is declared on the model for both Postgres and SQLite so tests exercise it.
The table is empty on the shared project today (re-check before applying). The
migration is applied to the shared Supabase project **only after Zarwa's go-ahead
and a heads-up to the team**, and `tests/test_privacy_access.py`'s migration-chain
guard is updated to the new head.

### 3.5 Errors the patient sees

The page re-renders with fresh times, never a blank error:

- "Someone just booked that time. Please pick another."
- "That session is full."
- "That time is too soon" / "too far ahead."
- "You already have an appointment at that time" / "with this doctor that day."
- "This appointment can't be cancelled."
- "Not found" for anything that isn't theirs.

## 4. Dependencies and risks

- **Builds on D7** (schedules, in `dev`) and the doctor verification just merged. Any
  change to D7's availability function would affect this feature; it is consumed
  read-only.
- **Shared schema:** one additive index on a table this feature owns. It still
  needs the team's heads-up before it runs on the shared project.
- **Conflicts:** teammates are changing `web/router.py`, `models.py` and the i18n
  files. Keep edits there small and additive, and put the pages in their own module.
- **Evaluation timing:** v1 is sized to be finished and demoable in a few days.

## 5. Tests

In-memory SQLite and the existing fake Supabase. No network.

1. Pure slot logic: the grid, the last full slot fitting, the cap, time off
   removing times, an extra window adding times, the 30-minute and 14-day edges.
2. Double booking is refused; a duplicate inserted straight into the database is
   refused by the index.
3. A full session refuses another booking; cancelling frees the time and the cap.
4. Overlap with the patient's other appointment, and two with the same doctor on
   the same day, are refused.
5. Reschedule books the new time and cancels the old in one step; if the new time
   is invalid, the old booking is untouched.
6. Cancel works only on the patient's own `booked` appointment; a foreign or fake
   id is `NotFound`.
7. Unverified, pending and rejected doctors are not searchable or bookable; a
   hospital that is not live is not offered.
8. The booking lock statement is asserted (as D7's AC-12); the write rate limit holds.
9. Role gating: a doctor, admin or logged-out visitor is redirected from the
   patient pages; a patient from the doctor pages.
10. Pages: search by name, hospital and specialty; the doctor page shows only free
    times; confirm, cancel and the lists; escaping of names.
11. The doctor's list shows name, time and status only (no passport number, no
    clinical data) and only that doctor's patients.
12. Audit rows hold ids only.
13. Edge cases: a time that became time off after the page loaded; a time just
    after midnight in Pakistan (a different date in UTC); cancel then rebook the
    same time; an extra session with an inherited cap.
14. English and Urdu both render; `missing_keys()` is empty.
15. The migration-chain guard test matches the new head.

## 6. Done means

- All new tests pass and the existing suite still passes; `ruff check` and
  `ruff format --check` are clean.
- Manual check in the browser with a verified doctor who has a schedule and a
  patient: search, book, see it in both lists, cancel, reschedule, and a full session.
- The migration applied to the shared project only after Zarwa's go-ahead.
- PR opened into `dev`, reviewed by at least one teammate.
