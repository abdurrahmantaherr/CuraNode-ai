# Doctor Verification v1 (PMDC) — design

**Feature:** Clinic-admin doctor verification, version 1
**Owner:** Zarwa · **Branch:** TBD, from `dev`
**Status:** awaiting review

## 1. Goal

Today a new doctor is stuck as "pending" forever — nobody in the app can
verify them, so the team has been editing the database by hand (see D4
session notes). This feature gives a clinic administrator a real screen to
approve, reject or revoke a doctor, checking PMDC's public Doctor Search
first, with every decision recorded.

It also fixes a bug found while designing this: the login page offers only
Patient/Doctor, so **no admin account can sign in today** — `role != "admin"`
always fails the role-mismatch check in `web/router.py`.

**Out of scope for v1:**
- Any automatic call to PMDC's website (future addition 2 in
  `future_addition.docx`). The admin checks PMDC by hand, via a link.
- Licence document upload and expiry tracking (future addition 1).
- A platform super-admin role or screen. Clinic admin accounts exist only via
  self-sign-up with an invite code, or a one-off seed script.
- Email notifications to the doctor.

## 2. Decisions already made (brainstorm log)

| Question | Decision |
|---|---|
| Who creates the doctor account? | The doctor signs up themself (existing flow, unchanged). The admin only approves/rejects. |
| What happens on reject? | Reject with a reason; admin can approve later from a "Rejected" list. |
| Can an approved doctor be un-verified later? | Yes — **revoke** with a reason, same mechanics as reject. |
| What must the admin confirm before approving? | A 3-item checklist, all required: number exists on PMDC, name matches, licence valid/not suspended. Plus an optional note. |
| Where is each decision stored? | A new history table `doctor_verification`, one row per decision, never edited or deleted. `doctor.verification_status` stays the current-state column, as today. |
| How does an admin account get created? | Self-sign-up with a per-clinic invite code (new table `clinic_invite_code`, codes hashed). No super-admin screen yet — codes are set/rotated by a one-off script. |

## 3. Design

### 3.1 Data model

**New table `doctor_verification`** (Alembic migration, additive only):

| Column | Type | Notes |
|---|---|---|
| `id` | uuid, PK | |
| `doctor_id` | uuid, FK → `doctor.doctor_id` | |
| `decided_by` | uuid, FK → `user_profile.user_id` | the admin |
| `decision` | enum: `approve` / `reject` / `revoke` | |
| `reason` | text, nullable | required for reject/revoke; one of a fixed set (see 3.3) or `other` |
| `checked_number`, `checked_name`, `checked_licence` | boolean, not null, default false | the 3 ticks; required true for `approve` |
| `note` | text, nullable, max 500 chars | optional; required when `reason = other` |
| `decided_at` | timestamptz, not null, default now() | |

No update/delete path — exactly like `audit_log` (BL-15 pattern). An index on
`(doctor_id, decided_at)` supports "latest decision for this doctor".

**New table `clinic_invite_code`**:

| Column | Type | Notes |
|---|---|---|
| `clinic_id` | uuid, FK → `clinic.clinic_id`, unique | one active code per clinic |
| `code_hash` | text, not null | same hashing approach as passwords — never store plain text |
| `is_active` | boolean, not null, default true | rotate by setting the old row inactive and inserting a new one |

**`doctor.verification_status`** (existing column) is still the fast,
current-state read D4 and the consent module already use. It is written by
the verification service in the same transaction as the
`doctor_verification` row:
- `approve` → `verified`, `verified_by`/`verified_at` set (existing CHECK
  constraint already requires this pair).
- `reject` / `revoke` → `rejected`, `verified_by`/`verified_at` cleared.

### 3.2 Touch points

| File | Change |
|---|---|
| `alembic/versions/<rev>_doctor_verification_and_invite_codes.py` (new) | The two tables above. Additive only, hand-written, matching `CLAUDE.md`'s rule. |
| `backend/app/verification/schemas.py` (new) | `Decision` enum, `REASONS` tuple, `VerificationDecisionOut`. |
| `backend/app/verification/service.py` (new) | `list_doctors_for_clinic(session, actor, tab)`, `approve(...)`, `reject(...)`, `revoke(...)`. Clinic-scoped via `ClinicAdminDep`'s `clinic_ids`; a doctor outside the admin's clinic(s) raises `NotFound` — same "doesn't reveal existence" pattern as `consent/service.py`. |
| `backend/app/admin/schemas.py`, `service.py` (new) | `AdminRegisterRequest`; `register_admin(session, body)` verifies the invite code hash, creates the `clinic_admin` `Profile` + `ClinicStaff` row, same shape as `identity/service.py::register`. |
| `backend/ops/scripts/set_clinic_invite_code.py` (new) | One-off script: pick a clinic, print a generated code once, store only its hash. Never queryable back out. |
| `backend/app/web/router.py` | `admin_area` (`_guarded`'s admin branch) loads the 3 tabs via `verification.service`. New `admin_register_page`/`admin_register_submit` routes at `/{locale}/admin/register`. `login_submit`'s role check already handles any string in `("patient","doctor")`; add `"admin"`. |
| `frontend/templates/auth/login.html` | Third `role-tile` for `admin`, alongside the existing `patient`/`doctor` loop. |
| `frontend/templates/auth/admin_register.html` (new) | Name, email, password, clinic dropdown, invite code. Not linked from the public register page. |
| `frontend/templates/admin/_pending_list.html`, `_verified_list.html`, `_rejected_list.html` (new) | Tab content, included from `shell.html` the same way `doctor/_patient_list.html` is for D4. |
| `frontend/templates/doctor/shell.html` banner block (in `shell.html`, shared) | New red banner for `rejected`/`revoked` doctors, alongside the existing yellow pending banner. |
| `backend/app/i18n/messages/en.json`, `ur.json` | New keys: tab labels, column headings, reason options, checklist labels, banners, admin sign-up form, errors. |
| `tests/test_verification.py`, `tests/test_admin_register.py` (new) | See §5. |

### 3.3 Behaviour

**Reasons** (fixed set, shown as a dropdown): `number_not_found`,
`name_mismatch`, `licence_invalid`, `not_at_clinic`, `other`. `other`
requires a non-empty `note`.

**Approve** requires `checked_number`, `checked_name`, `checked_licence` all
`true`. The server re-checks this regardless of client-side disabling (the
D4 plan's pattern: client is a convenience, server is the rule).

**Clinic scoping.** `ClinicAdminDep` already resolves `actor.clinic_ids` from
`ClinicStaff` (see `deps.py`). Every verification-service call filters
`DoctorAffiliation` by `doctor_id` + `clinic_id IN actor.clinic_ids` +
`status = 'active'`. A doctor with no active affiliation to the admin's
clinic is `NotFound`, identical to how `consent/service.py` treats a
foreign/nonexistent patient — nothing distinguishes "wrong clinic" from
"doesn't exist".

**Doctor-side banners** (`shell.html`, extending the existing
`show_unverified_banner` block):
- `pending` → existing yellow banner, unchanged copy.
- `rejected` → new red banner: "Your verification was not approved: {reason}.
  Please contact {clinic}." The admin's private `note` is never shown to the
  doctor; only the fixed `reason` label is.
- `verified` → no banner (today's behaviour, unchanged).

**Audit.** Each decision writes one `audit_log` row
(`verification.approve` / `.reject` / `.revoke`) with `detail = {"reason":
...}` only — never the note, matching the audit writer's no-free-text rule
for anything that might carry PII.

**Admin sign-up.** `/{locale}/admin/register` is a plain URL, not linked from
the public register page's role tiles — the invite code is the gate, not
discoverability. A wrong code returns the same generic "doesn't match" shape
regardless of whether the clinic exists, so the form can't be used to probe
which clinics are real.

**Login fix.** Adding `"admin"` to the role-tile loop and to
`login_submit`'s accepted `role` values is the entire fix; the role-mismatch
logic downstream already handles a third value correctly, it was only ever
fed two.

### 3.4 Security

- Verification decisions, like consent, are decided in exactly one module
  (`verification/service.py`); the admin page never queries `Doctor` or
  `DoctorAffiliation` directly.
- Invite codes are hashed, never logged, never re-displayed after creation.
- No clinical or patient data appears anywhere in this feature.
- `doctor_verification.note` can contain free text written by a human about
  another human (e.g. "looks like a fake PMDC number") — stored, shown only
  to clinic admins of that clinic, never to the doctor, never in audit
  `detail`.

## 4. Dependencies and risks

- **Builds on D4 and P4**, both already in `dev`. No conflict expected —
  this feature adds new modules and only extends `shell.html`'s existing
  banner block.
- **Database change on the shared schema.** Two new tables, additive only,
  matching the pattern used for `audit_log`/`patient_medication`. Must be
  announced to the team before running on the shared Supabase project, and
  applied by whoever runs migrations there (Zarwa or AbdurRahman).
- **No automated PMDC check** means this still depends on the admin actually
  looking. Documented as the known limit in `future_addition.docx`.
- **Evaluation-week timing:** the existing demo doctor
  (`d4.doctor@example.com`) is already `verified` via the seed script, so
  this feature does not need to touch that data to keep the D4 demo working.

## 5. Tests

In-memory SQLite + `FakeSupabaseAuth`, no network, same harness as
`test_doctor_dashboard.py`.

**`tests/test_verification.py`**
1. An admin lists only doctors affiliated with their own clinic.
2. A doctor at another clinic is invisible (not merely blocked) — `NotFound`.
3. Approve requires all 3 checklist ticks; missing any one is refused server-side.
4. Approve sets `verified_status`, `verified_by`, `verified_at`, and writes one `doctor_verification` row with `decision=approve`.
5. Reject with a fixed reason sets `rejected` and records the reason.
6. Reject with `reason=other` and no note is refused.
7. A rejected doctor later approved moves to the Verified tab; the Rejected-tab history entry is retained (no row ever updated/deleted).
8. Revoke on a previously verified doctor sets `rejected`, clears `verified_by`/`verified_at`.
9. A revoked doctor loses the D4 patient-dashboard list on their very next request (reuses D4's live re-check pattern).
10. Each decision writes exactly one `audit_log` row with `detail={"reason": ...}` and no note text.
11. Two decisions on the same doctor over time both appear in history, in order.
12. A non-admin actor (patient/doctor) gets `NotFound` from every verification-service call.

**`tests/test_admin_register.py`**
13. A correct invite code creates a `clinic_admin` account tied to the right clinic.
14. A wrong invite code is refused, with the same error shape whether the clinic exists or not.
15. Login as `admin` with the right role now succeeds (fixes the pre-existing bug).
16. Login as `admin` selecting `patient`/`doctor` on the tile is a role mismatch, as today.
17. English and Urdu both render every new page; `missing_keys()` is empty.
18. PMDC numbers and invite codes render `dir="ltr"` on the Urdu pages.

## 6. Done means

- All new tests pass; the existing suite (242+ on `dev`) still passes.
- `ruff check` / `ruff format --check` clean.
- Manual check: sign up a doctor, sign up a clinic admin via invite code,
  approve the doctor, confirm the D4 dashboard now works for them; reject
  another doctor and confirm the red banner; revoke a verified doctor and
  confirm the D4 list drops them on next load.
- Migration applied to the shared Supabase project only after telling the
  team, and only the two new tables are touched.
- PR opened into `dev`, reviewed by at least one teammate (Osama or
  AbdurRahman, since this touches `doctor`/`clinic_staff` adjacent code and
  the shared schema).
