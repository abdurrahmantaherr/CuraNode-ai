# D4 v1 — Doctor Patient Dashboard (design)

**Feature:** D4 Patient Management Dashboard, version 1
**Owner:** Zarwa · **Branch:** `feature/doctor-dashboard` (based on `feature/P4-medical-passport` until it merges into `dev`)
**Status:** awaiting review

## 1. Goal

A verified doctor opens their home page (`/{locale}/doctor`) and sees the
patients who have granted them access. Success means the doctor sees exactly
the patients with an active consent grant, and nobody else.

**Out of scope for v1:** risk triage, risk scoring, "today's queue" (needs P5
appointments), search, pagination, clinical data on the list, charts (D5), the
"what changed" summary (D3), and the real patient record view (FR21/D2).

## 2. Decisions already made

| Question | Decision |
|---|---|
| Which patients are listed? | Only patients with an active consent grant for this doctor (Osama's P4 consent). |
| What does a row show? | Name, age, gender, passport number. No grant date. |
| Pending (unverified) doctor? | Friendly notice, no list. The existing FR3 banner on `/{locale}/doctor` already does this. |
| New table or migration? | None. |

## 3. Design

### 3.1 Touch points

| File | Change |
|---|---|
| `backend/app/consent/service.py` | Add `list_patients_for_doctor(session, actor)`. Reuses `_active()` so "active grant" has one definition. Owned by Osama, so tell him and keep the diff to one function. |
| `backend/app/web/router.py` | In the doctor branch of `_guarded`, load the list for verified doctors and pass `patients` to `shell.html`. Unverified doctors keep the banner and get no list. |
| `frontend/templates/doctor/_patient_list.html` (new) | Table partial plus empty state, included from `shell.html`. |
| `backend/app/i18n/messages/en.json`, `ur.json` | New keys: title, column headings, empty state. |
| `tests/test_doctor_dashboard.py` (new) | See section 5. |

### 3.2 Behaviour

- `list_patients_for_doctor` raises `NotFound` for a missing or unverified doctor,
  like the other consent helpers, and otherwise returns patients joined to
  active doctor-type grants. The check runs on every request and is never cached.
- Rows are sorted by name. Age is computed from `date_of_birth` and shows "—"
  when it is missing. Gender shows "—" when missing.
- The passport number is displayed `dir="ltr"` on the Urdu page.
- Each row links to `/{locale}/doctor/patient/{patient_id}`. The patient id is
  used and not the passport number, because passport numbers must never reach
  access logs (P4 AC-12). That page re-checks consent on every view, so a
  revoked patient disappears from the list and the link 404s.
- Empty state: "No patients have shared their record with you yet."
- Names are rendered through Jinja2 autoescaping, with no `|safe`.
- One audit entry per dashboard load through the existing audit writer, holding
  the doctor and the patient count only, with no patient details.

### 3.3 Security

- Page access: existing doctor role guard and onboarding redirects, unchanged.
- Consent is decided only in `consent/service.py`. The dashboard never queries
  `Patient` directly.
- No clinical data (conditions, allergies, medications) appears on the list.

## 4. Dependencies and risks

- **Depends on** Osama's P4 PR merging into `dev`. Until then this branch sits on
  top of his branch. After his PR merges, rebase onto `dev`.
- **Conflict risk:** his PR changes `web/router.py` heavily and both PRs edit
  `en.json` and `ur.json`. Keep this diff small and merge after his.
- **Open point for Osama:** his grants are all-or-nothing, while the prototype and
  user stories show per-section scopes. Not needed for D4 v1.

## 5. Tests (`tests/test_doctor_dashboard.py`)

In-memory SQLite and the existing `FakeSupabaseAuth`. No network.

1. A verified doctor sees only the patients who granted them access.
2. A revoked grant and an expired grant do not appear.
3. Another doctor's patient does not appear.
4. An unverified doctor sees the banner and no list.
5. A patient or logged-out visitor is redirected like other protected areas.
6. Empty state renders when there are no grants.
7. A patient name containing HTML is escaped.
8. Missing date of birth or gender renders "—".
9. Rows are ordered by name and link to `/doctor/patient/{id}`.
10. The page contains no passport-number URL and no clinical data.
11. An audit entry is written with a count and no patient details.
12. English and Urdu both render, and the passport number is left-to-right.

## 6. Done means

- All new tests pass and the existing suite still passes.
- `ruff check` and `ruff format --check` are clean.
- Manual check in the browser, as a verified doctor with one granted and one
  ungranted patient.
- PR opened into `dev`, reviewed by at least one teammate.
