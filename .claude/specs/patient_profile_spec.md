# SPEC — Patient Profile

| | |
|---|---|
| **Feature** | Patient Profile (view and edit own personal and health information) |
| **PRD requirement** | **FR2** (M, PAT). Also touches FR1 (passport shown read-only), FR6 (role gates), FR28/NFR13 (Urdu/RTL), NFR14 (accessibility), NFR17/FR5 (audit), NFR19 (synthetic data), D1 (no silent loss of history) |
| **TDD references** | §3.1 (UUIDv7, UTC), §4.1 (API conventions), §4.4 (`PATCH /api/v1/me` → realised here as `/api/v1/me/profile`, see §6.0), §7.2 (authorisation), §7.8 (logging), §8.2/§8.3 (error envelope) |
| **Branch** | `feature/patient-profile` |
| **Status** | Ready for implementation |
| **Decisions already taken (do not revisit)** | (1) Current medications live in a **new repo-owned table `patient_medication`**. (2) Entries are **edited in place and soft-removed**: allergy and medication get `removed_at`, and a chronic condition is set to `status='resolved'`. Nothing is hard-deleted. Every change is audit-logged, with before/after values for clinical fields. |

> **Read before writing code.** The database is the **shared CuraNode-AI Supabase project**, not the TDD §3 schema. Every table and column named below was verified against the live `information_schema` on 2026-09-22. Where this spec and `docs/TDD.md` disagree about table or column names, **this spec wins**. Where this spec is silent, stop and ask; do not invent a column, endpoint, or behaviour.

---

## 1. Feature Overview

### 1.1 What it is
An authenticated **patient** gets a "My profile" page (`/{locale}/patient/profile`) and a matching JSON API (`/api/v1/me/...`). On it they can view and maintain:

| Group | Contents | Stored in (shared Supabase schema) |
|---|---|---|
| **Identity (read-only)** | Medical Passport number, sign-in email, age (derived) | `patient.passport_uid`, `user_profile.email`, derived from `patient.date_of_birth` |
| **Basic details (editable)** | Full name, date of birth, gender, blood group, phone, emergency contact | `user_profile.full_name` + `patient.full_name` (kept in sync), `patient.date_of_birth`, `patient.gender`, `patient.blood_group`, `user_profile.phone`, `patient.emergency_contact` |
| **Allergies (list)** | Substance, reaction, severity | `allergy` (pre-existing, shared) |
| **Chronic conditions (list)** | Condition name, since-date | `chronic_condition` (pre-existing, shared) |
| **Current medications (list)** | Medicine name, strength, how often, taking-since, notes | `patient_medication` (**new, owned by this repo**) |

### 1.2 Why
PRD FR2: *"A patient can record and edit their own profile: name, age, gender, contact details, blood group, known allergies, chronic conditions, and current medications."* The profile is the first real content on the patient dashboard. It is also the data a consented doctor will later see at the point of care (FR21), which is outside this feature's scope.

### 1.3 Where it sits in the codebase
- New package `backend/app/profile/` (`__init__.py`, `schemas.py`, `service.py`, `router.py`) owns all logic. The JSON API router and the web pages both call **the same service functions**; no business rule is implemented twice.
- Web pages live in `backend/app/web/router.py`, following the existing convention that all server-rendered pages live there.
- One new, hand-written, **additive** Alembic migration.

### 1.4 Key constraints inherited from the codebase (non-negotiable)
1. **Role and verification come from the DB on every request.** Use `PatientDep` from `deps.py` and never perform an ad-hoc role check (CLAUDE.md, `deps.py` docstring).
2. **Migrations are additive and hand-written.** Never use `--autogenerate`, because it would try to recreate the shared tables.
3. **The app connects to Postgres as `postgres` with `BYPASSRLS`.** The Supabase RLS policies on `allergy`, `chronic_condition`, and `patient` therefore **do not protect this app's queries**. Every query must filter by the caller's own `patient_id` in application code. RLS exists only to protect the Supabase Data API.
4. **Every user-facing string goes through `translate()`** and exists in both `en.json` and `ur.json`. The existing test `test_t18_catalogue_is_complete` enforces this.
5. **CSS uses tokens only, logical properties only, and no raw hex.** Enforced by existing tests `test_no_raw_hex_in_app_css` and `test_t18_no_physical_direction_css`.
6. **Errors are `AppError` subclasses with an i18n `message_key`**, rendered through the envelope in `errors.py`. Never raise `HTTPException`.

---

## 2. User Story

**Primary.** *As a registered patient, I want to see and update my personal details, blood group, allergies, long-term conditions, and the medicines I currently take, so that my Medical Passport is accurate and any doctor I choose to share it with has the facts they need.*

**Supporting stories**
- *As a patient,* I want to correct a mistake (for example a misspelled medicine) without it looking as if the original never existed, so my record stays trustworthy (D1).
- *As a patient,* I want to remove an allergy or medicine I no longer have or take, so my current list is accurate.
- *As a patient who reads Urdu,* I want the whole page in Urdu, laid out right-to-left, and able to switch language without losing my place (FR28, NFR13).
- *As an older patient on a phone,* I want large controls, clear errors, and a page that works even if JavaScript fails (NFR12, NFR14).
- *As a patient,* I must **not** be able to change entries a clinician recorded about me, or anything that identifies my account (passport number, sign-in email, role).

**Not the actor here:** doctors and clinic admins. They cannot reach any part of this feature (AC-03).

---

## 3. Acceptance Criteria

Each AC has a matching test `Tn` in §11 with the same number.

| ID | Criterion |
|---|---|
| **AC-01** | **View (web).** An active patient with a `patient` row who opens `GET /{locale}/patient/profile` gets `200` with a page showing: passport number (read-only), sign-in email (read-only), derived age (or "Not set"), the Basic-details form prefilled from the DB, and the Allergies, Chronic conditions, and Current medications sections. Each list shows only **active** entries, or its empty-state message when there are none. |
| **AC-02** | **View (API).** `GET /api/v1/me/profile` returns `200` with a `PatientProfileOut` body (§6). `age_years` is derived from `date_of_birth` against today's date in Pakistan time (UTC+5), and is `null` when there is no DOB. Lists exclude removed allergies and medications and conditions with `status='resolved'`, and are ordered per BL-22. |
| **AC-03** | **Access control.** Page and API behave as follows. *No/invalid/expired session:* API `401 UNAUTHENTICATED`; web `303` to `/{locale}/login?next=/{locale}/patient/profile`. *Doctor or admin:* API `403 FORBIDDEN`; web `303` to the caller's own area. *Suspended account:* treated as no session. *Patient with no `patient` row (OAuth onboarding incomplete):* API `404 NOT_FOUND`; web `303` to `/{locale}/onboarding`. |
| **AC-04** | **Update basic details.** Via `PATCH /api/v1/me/profile` or the Basic-details form, the patient can set full name, date of birth, gender, blood group, phone, and emergency contact. **Only fields present in the request change.** `full_name` is written to both `user_profile.full_name` and `patient.full_name` in the same transaction. The API returns the updated `PatientProfileOut`. The web form answers `303` to `/{locale}/patient/profile?saved=basics.updated#basics`, which renders a success banner. |
| **AC-05** | **Clearing optional details.** Sending `null` (API) or an empty value (web) clears `date_of_birth`, `gender`, `blood_group`, `phone_e164`, or `emergency_contact`. `full_name` can never be cleared: `null` or blank is rejected with `errors.name_required`. |
| **AC-06** | **Basic-details validation.** Invalid input is rejected with `422 VALIDATION_FAILED` and field-level `message_key`s, and **nothing is written**. Rules are in BL-08…BL-13. On the web, the page re-renders with status `422`, the submitted values preserved, and each error shown under its field with `aria-invalid="true"` and `role="alert"`. A stored legacy value the patient did not touch (e.g. a `gender` outside the allowed set) never blocks a web save (BL-14). |
| **AC-07** | **Protected fields are immutable.** The API rejects any body containing a field outside §6's request models (e.g. `email`, `passport_no`, `role`, `status`, `patient_id`, `cnic_hash`) with `422`. The web handler ignores unknown form fields. `user_profile.email`, `user_profile.role`, `user_profile.status`, `patient.passport_uid`, and `patient.cnic_hash` are never modified by this feature, and **no Supabase Auth API is called**. |
| **AC-08** | **Add allergy.** `POST /api/v1/me/allergies` (or the Add-allergy form) creates an `allergy` row with `recorded_by = caller's user_id`, returning `201` with `AllergyOut` (`editable: true`). The web answers `303` to `?saved=allergy.added#allergies`. |
| **AC-09** | **Edit allergy.** `PATCH /api/v1/me/allergies/{allergy_id}` changes only the provided fields of an **own, active, editable** allergy, sets `updated_at`, and returns `200` with the updated `AllergyOut`. On the web, `?edit=allergy&id={uuid}` renders that row as an inline form; saving answers `303` to `?saved=allergy.updated#allergies`. |
| **AC-10** | **Remove allergy (soft).** `DELETE /api/v1/me/allergies/{allergy_id}` sets `removed_at` (and `updated_at`) and returns `204`. **The row is not deleted.** It no longer appears in any view. A repeat `DELETE` on an already-removed own allergy returns `204` and writes no second audit row. The web Remove control uses a two-step, no-JS confirmation and answers `303` to `?saved=allergy.removed#allergies`. |
| **AC-11** | **Chronic conditions.** Add (`POST /api/v1/me/conditions`, `201`, status `'active'`), edit name and since-date (`PATCH .../{condition_id}`, `200`), and remove (`DELETE .../{condition_id}`, `204`, sets `status='resolved'` and `updated_at`; the row is kept; idempotent) all work, with the same web behaviour as AC-08…AC-10 (`#conditions`, `condition.*` saved keys). `icd10_code` and `risk_level` are never modified. |
| **AC-12** | **Current medications.** Add (`POST /api/v1/me/medications`, `201`), edit (`PATCH .../{medication_id}`, `200`), and remove (`DELETE .../{medication_id}`, `204`, sets `removed_at`; the row is kept; idempotent) all work against the new `patient_medication` table, with the same web behaviour (`#medications`, `medication.*` saved keys). |
| **AC-13** | **Entry validation.** Required names, length limits, allowed severities, dates (not in the future, not before 1900-01-01, and for since/taking-since dates not before the patient's stored DOB), and control-character rejection are enforced per BL-15…BL-18. A failure returns `422` with field-level keys and writes nothing. |
| **AC-14** | **No duplicates.** Adding an entry whose normalised name (BL-19) matches an **active** entry of the same kind for the same patient, or renaming an entry to one, is rejected with `409 DUPLICATE_ENTRY`. The web shows the error under the name field. Removed allergies and medications, and resolved conditions, do not count. |
| **AC-15** | **List cap.** When a patient already has `profile_max_entries_per_list` (default **50**) active entries of a kind, adding another returns `422 LIST_FULL`, and the web shows the error in the section's banner. |
| **AC-16** | **Clinician and legacy entries are read-only.** An entry whose `recorded_by` is `NULL` or another user's id is shown with the label "Recorded by a clinician" and has no Edit or Remove controls. `PATCH`/`DELETE` on it returns `403 NOT_EDITABLE` and changes nothing. |
| **AC-17** | **Ownership isolation.** A patient can never read, edit, or remove another patient's entries. `PATCH`/`DELETE` with an id that belongs to another patient, is removed/resolved (for `PATCH`), or does not exist returns **`404 NOT_FOUND` with an identical body, apart from `request_id`**. A malformed UUID returns `422`. |
| **AC-18** | **Audit trail.** Every successful change writes **exactly one** `audit_log` row **in the same transaction**, with the action names, `resource_type`, and `detail` shape defined in BL-24…BL-27. A rejected request, a no-op update (no field actually changed), and a repeat removal write **no** audit row. |
| **AC-19** | **No health data or PII in application logs.** No field value from this feature (name, DOB, phone, emergency contact, substance, reaction, condition, medicine, strength, frequency, notes) appears in any structlog output at any level. The redaction key set is extended per BL-28. |
| **AC-20** | **Write rate limit.** More than `profile_write_rate_limit_per_minute` (default **30**) successful-or-failed write requests by one patient in 60 s returns `429 RATE_LIMITED` with a `Retry-After` header on the API, and the web re-renders with the `errors.rate_limited` banner. Reads are not rate-limited. |
| **AC-21** | **Urdu, RTL, and language switch.** `/ur/patient/profile` renders with `<html lang="ur" dir="rtl">`, and every visible string comes from the `ur` catalogue. The language-switch link on `/en/patient/profile` points to `/ur/patient/profile`, and vice versa. The catalogue-completeness test still passes. |
| **AC-22** | **Accessibility, design system, and no-JS operation.** Every form works with JavaScript disabled, including add, edit, remove, and confirm. Every input has a visible `<label>`. Errors follow the `partials/form_field.html` contract. Submit buttons disable themselves on submit (`aria-busy`). Touch targets are ≥ 44 px on coarse pointers, and the page is usable at 360 px width with no horizontal scroll. All new CSS uses tokens and logical properties only. |
| **AC-23** | **Output escaping.** Text such as `<script>alert(1)</script>` saved in any free-text field is stored verbatim and rendered HTML-escaped. It is never executed and never breaks the page structure. |
| **AC-24** | **Entry point.** The signed-in patient shell (`/{locale}/patient`) shows a "My profile" link to `/{locale}/patient/profile`, and the profile page shows a link back to the dashboard. Doctor and admin shells show no profile link. |
| **AC-25** | **Migration is additive and protected.** The new Alembic revision (`down_revision = "6feacefae2d0"`) only adds columns and a table. It never drops, renames, or alters an existing column or table in `upgrade()`. It enables RLS on `patient_medication` and creates its policies (Postgres only). `downgrade()` reverses exactly what `upgrade()` added. |
| **AC-26** | **No regression.** All 78 pre-existing tests still pass, and `uv run ruff check backend tests` and `uv run ruff format --check backend tests` are clean. |

---

## 4. Functional Specifications

### 4.1 Resolving the patient (used by every operation)
`service._patient_for(session, actor) -> Patient`
1. Select `Patient` where `Patient.user_id == actor.user_id`.
2. If there is none, raise `NotFound()`. This is the OAuth-onboarding-incomplete case: the web layer redirects to onboarding before this is reached, and the API surfaces `404`.
3. Every entry lookup below uses **this** `patient.id` in its `WHERE` clause. An entry id is never trusted on its own.

### 4.2 Get profile
`service.get_profile(session, actor) -> PatientProfileOut`
1. `patient = _patient_for(...)`; `user = session.get(Profile, actor.user_id)`.
2. Build basic fields:
   - `full_name`: `user.full_name` if non-empty, else `patient.full_name`.
   - `email`: `user.email`, which may be `None` in the shared schema.
   - `phone_e164`: `user.phone_e164`, returned **unmasked** because the owner is viewing their own data.
   - `date_of_birth`, `gender`, `blood_group`, `emergency_contact`: from `patient`, returned as **raw stored values** even if outside the allowed sets.
   - `age_years`: `compute_age(date_of_birth, today_pk())`, or `None`.
3. Load active allergies (`removed_at IS NULL`), non-resolved conditions (`status IS DISTINCT FROM 'resolved'`, i.e. `status != 'resolved'` or `NULL`), and active medications (`removed_at IS NULL`) for `patient.id`.
4. Set `editable` on each entry: `recorded_by == actor.user_id` (BL-20).
5. Order per BL-22. Return.
6. **Reads write no audit row** (BL-27).

### 4.3 Update basic details
`service.update_profile(session, actor, body: ProfileUpdateRequest, *, user_agent) -> PatientProfileOut`
1. Resolve `patient` and `user`.
2. For each field in `body.model_fields_set`, compare the new value to the stored value. Build `changed: list[str]` with only the fields whose value actually differs.
3. If `changed` is empty, return `get_profile(...)` without writing or auditing (no-op, AC-18).
4. Apply the changes:
   - `full_name` → set **both** `user.full_name` and `patient.full_name`.
   - `phone_e164` → `user.phone_e164` (column `user_profile.phone`) only. **Never** call Supabase Auth (BL-06).
   - `date_of_birth`, `gender`, `blood_group`, `emergency_contact` → the corresponding `patient` attribute.
5. `audit.write(action=PROFILE_UPDATE, resource_type="patient", subject_patient_id=patient.id, detail={"resource_id": str(patient.id), "fields_changed": sorted(changed)})`. **No values** go into the detail for basic fields (BL-25).
6. `session.commit()`, then return `get_profile(...)`.

### 4.4 Add entry (allergy / condition / medication)
`service.add_allergy | add_condition | add_medication(session, actor, body, *, user_agent) -> <Kind>Out`
1. Resolve `patient`.
2. **Date-vs-DOB check** (conditions and medications only): if a since-date is given and `patient.date_of_birth` is set and `since < date_of_birth`, raise `ValidationFailed({"fields": {"onset_date" | "started_on": "errors.date_before_birth"}})`.
3. **Cap check:** count active entries of this kind. If the count is `>= settings.profile_max_entries_per_list`, raise `ListFull({"max": N})`.
4. **Duplicate check:** if any active entry of this kind (editable or not) has `normalise_name(existing) == normalise_name(new)`, raise `DuplicateEntry({"field": "substance" | "name"})`.
5. Insert with `id=uuid7()`, `patient_id=patient.id`, `recorded_by=actor.user_id`, and `recorded_at=utcnow()` (allergy, condition, medication) or the column default.
   - Conditions: `status='active'`; `icd10_code` and `risk_level` stay `NULL`.
6. `audit.write(action=PROFILE_<KIND>_ADD, resource_type=<table>, subject_patient_id=patient.id, detail={"resource_id": ..., "after": {<clinical fields>}})` (BL-26).
7. Commit, then return `<Kind>Out` with `editable=True`.

### 4.5 Edit entry
`service.update_allergy | update_condition | update_medication(session, actor, entry_id, body, *, user_agent) -> <Kind>Out`
1. Resolve `patient`.
2. Load the entry `WHERE id = entry_id AND patient_id = patient.id AND <active predicate>`. If there is none, raise `NotFound()`, whether the id is foreign, nonexistent, or removed/resolved (AC-17).
3. If `entry.recorded_by != actor.user_id` (including `NULL`), raise `NotEditable()` (AC-16).
4. Compute `changed` as in §4.3 step 2. If empty, return the current `<Kind>Out` (no write, no audit).
5. Apply the date-vs-DOB check (§4.4 step 2) if a since-date changed.
6. If the name changed, run the duplicate check **excluding this entry's own id**.
7. Capture `before = {f: old for f in changed if f in CLINICAL_FIELDS}` and `after = {f: new ...}`.
8. Apply the changes and set `updated_at = utcnow()`.
9. `audit.write(action=PROFILE_<KIND>_UPDATE, ..., detail={"resource_id", "fields_changed": sorted(changed), "before": before, "after": after})`.
10. Commit, then return.

### 4.6 Remove entry (soft)
`service.remove_allergy | remove_condition | remove_medication(session, actor, entry_id, *, user_agent) -> None`
1. Resolve `patient`.
2. Load the entry `WHERE id = entry_id AND patient_id = patient.id`, **without** the active predicate. If there is none, raise `NotFound()`.
3. If `entry.recorded_by != actor.user_id`, raise `NotEditable()`.
4. If the entry is already removed (allergy/medication: `removed_at IS NOT NULL`; condition: `status == 'resolved'`), **return without writing or auditing** (idempotent, AC-10).
5. Allergy and medication: set `removed_at = utcnow()` and `updated_at = utcnow()`. Condition: set `status = 'resolved'` and `updated_at = utcnow()`.
6. `audit.write(action=PROFILE_<KIND>_REMOVE, ..., detail={"resource_id", "before": {<clinical fields snapshot>}})`.
7. Commit.

### 4.7 Web page flow
All web handlers use `OptionalActorDep` and a helper `_patient_page_guard(request, locale, actor) -> Response | None`. The helper returns the redirect Response for AC-03 (login with `next`, own area, onboarding) or `None` when the caller may proceed. It mirrors `_guarded()` and **raises nothing**.

| Route | Handler | Behaviour |
|---|---|---|
| `GET /{locale}/patient/profile` | `patient_profile_page` | Guard. Load `service.get_profile`. Read the optional query params `saved` (allow-list, BL-31) and `edit` + `id` (BL-32). Render `patient/profile.html` with `200`. |
| `POST /{locale}/patient/profile` | `patient_profile_basics_submit` | Guard. Rate limit. Build the dict of submitted basic fields and **drop fields whose submitted value equals the stored value** (BL-14). Convert empty strings to `None`. Validate as `ProfileUpdateRequest`. Call `service.update_profile`. On success, `303` to `?saved=basics.updated#basics`. On failure, re-render with section `basics` errors (status per §4.8). |
| `POST /{locale}/patient/profile/allergies` | `patient_allergy_add_submit` | Guard, rate limit, validate `AllergyCreate`, call `service.add_allergy`, then `303` to `?saved=allergy.added#allergies`. |
| `POST /{locale}/patient/profile/allergies/{entry_id}` | `patient_allergy_edit_submit` | Guard, rate limit, validate `AllergyUpdate` (all three fields submitted; unchanged ones dropped per BL-14), call `service.update_allergy`, then `303` to `?saved=allergy.updated#allergies`. |
| `POST /{locale}/patient/profile/allergies/{entry_id}/remove` | `patient_allergy_remove_submit` | Guard, rate limit, call `service.remove_allergy`, then `303` to `?saved=allergy.removed#allergies`. |
| `POST /{locale}/patient/profile/conditions` (+`/{entry_id}`, +`/{entry_id}/remove`) | `patient_condition_*_submit` | Same pattern; section `conditions`; saved keys `condition.added / condition.updated / condition.removed`. |
| `POST /{locale}/patient/profile/medications` (+`/{entry_id}`, +`/{entry_id}/remove`) | `patient_medication_*_submit` | Same pattern; section `medications`; saved keys `medication.added / medication.updated / medication.removed`. |

A web `{entry_id}` that is not a valid UUID is treated exactly like `NotFound` (§4.8), not as a raw `422`.

### 4.8 Web error mapping
Every failure **re-renders the full profile page** (never a raw JSON envelope). Sections that did not fail render from the DB as usual. The failing section receives `section_values` (what the user typed) and `section_errors` (translated per field) or `section_error` (a banner).

| Service outcome | HTTP status | Where the message appears |
|---|---|---|
| Pydantic / `ValidationFailed` field errors | `422` | Under each field of the failing form |
| `DuplicateEntry` | `409` | Under `substance` / `name` of the failing form |
| `ListFull` | `422` | The section's banner (`errors.list_full` with `{max}`) |
| `NotEditable` | `403` | The section's banner |
| `NotFound` (incl. malformed id) | `404` | The page-level banner (`errors.not_found`) |
| `RateLimited` | `429` | The page-level banner (`errors.rate_limited`) |
| Any other `AppError` / unexpected exception | `500` | The page-level banner (`errors.internal`), with the session rolled back |

When an **edit** form fails validation, the page re-renders with that row still in edit mode, showing the submitted values.

---

## 5. UI/UX Requirements

### 5.1 Page structure (`frontend/templates/patient/profile.html`, extends `base.html`)
Top to bottom, inside the existing shell layout (`.shell` → topbar → `main.content`):

1. **Topbar**, the shared partial (see 5.5):
   - crumb: `auth.role.patient`
   - title: `profile.title`
   - actions: nav links (Dashboard, My profile with `aria-current="page"`), language switch, sign out.
2. **Identity panel** (`.panel`):
   - `.avatar` with initials, the full name, and a mono line `{passport_no}`, plus the `patient.dashboard.passport` chip.
   - a read-only definition list (`<dl>`) of:
     - **Email**, with help text `field.email_readonly_help`
     - **Age**: `profile.age_years` with `{years}`, or `option.not_set`
   - the lead paragraph `profile.subtitle`.
3. **Basic details** (`<section id="basics" aria-labelledby="basics-h">`, `.panel`):
   - `<h2 id="basics-h" class="panel__label">` = `profile.section.basics`.
   - One `<form method="post" action="/{locale}/patient/profile" novalidate>` with the fields below, laid out in a `.grid-2`, then a submit button `profile.action.save`.
4. **Allergies** (`<section id="allergies">`), **Chronic conditions** (`<section id="conditions">`), and **Current medications** (`<section id="medications">`). Each is a `.panel` containing:
   - The section heading.
   - The section banner slot (error or success; see 5.4).
   - The list of entries (`<ul class="entry-list">`), or the empty-state paragraph (`.empty-state`) with the section's `profile.empty.*` key.
   - The **add form** at the bottom of the panel (compact `.grid-2`, submit button `profile.action.add_*`).
5. **Footer notes** (`.mono-note`-style paragraph, muted): `profile.self_reported_note` and `profile.no_advice_note`.

### 5.2 Basic-details form fields
Use `partials/form_field.html` macros (`f.field`, `f.select`) for every control. Add the macro parameters `min`, `max`, `maxlength`, and `dir` to `f.field` as optional keyword arguments; they default to omitted, so existing callers are unaffected.

| Field (`name`) | Control | Label key | Constraints shown to the user |
|---|---|---|---|
| `full_name` | `f.field` text, `required`, `autocomplete="name"`, `maxlength=120` | `field.full_name` | — |
| `date_of_birth` | `f.field` `type="date"`, `min="1900-01-01"`, `max={today_pk}`, `dir="ltr"` | `field.date_of_birth` | — |
| `gender` | `f.select`, placeholder `option.not_set` (value `""`), options `female/male/other/prefer_not_to_say` → `gender.*` | `field.gender` | — |
| `blood_group` | `f.select`, placeholder `option.not_set`, options `A+ A- B+ B- AB+ AB- O+ O-` (labels are the literal values, not translated) | `field.blood_group` | — |
| `phone_e164` | `f.field` `type="tel"`, `autocomplete="tel"`, placeholder `+923001234567` | `field.phone` | — |
| `emergency_contact` | `f.field` text, `maxlength=255` | `field.emergency_contact` | help `field.emergency_contact_help` |

**Legacy values** (BL-14): if the stored `gender` or `blood_group` is not in the allowed set, the `<select>` renders one extra `<option>` whose value **and** label are the raw stored value, and it is selected. Submitting unchanged therefore sends the same value, which BL-14 drops before validation.

### 5.3 Entry rows and forms
Each `<li class="entry-row">` contains:
- **`.entry-row__main`:**
  - primary text (substance, condition name, or medicine name), weight 600.
  - a secondary line in `.mono`:
    - allergy: reaction · severity label
    - condition: `field.onset_date` + date
    - medication: strength · frequency · `field.started_on` + date
  - Omit empty parts, and never render a dangling separator.
- **Severity badge** (allergies only), a `.badge` pill with semantic colour (BR-07; this colour carries meaning):
  - `severe` → `.badge--err`
  - `moderate` → `.badge--warn`
  - `mild` → `.badge--ok`
  - `unknown` → `.badge--neutral`
  - a legacy/unrecognised value → `.badge--neutral` showing the **raw** value.
- **Condition status**: when the stored status is anything other than `active` (a legacy value), show it raw in a `.badge--neutral`.
- **`.entry-row__actions`**, only when `editable`:
  - **Edit**: a link (`<a class="btn btn--secondary btn--sm">`) to `?edit=<kind>&id=<uuid>#<section>`.
  - **Remove**: a two-step confirmation with **no JavaScript**:
    ```html
    <details class="confirm">
      <summary class="btn btn--secondary btn--sm">{{ t('profile.action.remove') }}</summary>
      <form method="post" action=".../{id}/remove">
        <p class="field-help">{{ t('profile.remove_hint') }}</p>
        <button class="btn btn--danger btn--sm" type="submit">{{ t('profile.action.confirm_remove') }}</button>
      </form>
    </details>
    ```
- **Non-editable entries** show a `.chip` with `profile.recorded_by_clinician` in place of the actions.
- **Edit mode**: the row renders as a `<form method="post" action=".../{id}">` with the same fields as the add form, prefilled, plus Save (`profile.action.save_entry`) and a Cancel **link** (`profile.action.cancel`) to `/{locale}/patient/profile#<section>`.

**Add/edit form fields**

| Kind | Field (`name`) | Control | Label / placeholder keys |
|---|---|---|---|
| Allergy | `substance` | text, `required`, `maxlength=120` | `field.substance` / `field.substance_placeholder` |
| | `reaction` | text, `maxlength=255` | `field.reaction` / `field.reaction_placeholder` |
| | `severity` | select, placeholder `option.not_set`; options `mild moderate severe unknown` → `severity.*` | `field.severity` |
| Condition | `name` | text, `required`, `maxlength=120` | `field.condition_name` / `field.condition_placeholder` |
| | `onset_date` | `type="date"`, `min=1900-01-01`, `max={today_pk}`, `dir="ltr"` | `field.onset_date` |
| Medication | `name` | text, `required`, `maxlength=120` | `field.medication_name` / `field.medication_placeholder` |
| | `strength` | text, `maxlength=50` | `field.strength` / `field.strength_placeholder` |
| | `frequency` | text, `maxlength=50` | `field.frequency` / `field.frequency_placeholder` |
| | `started_on` | `type="date"`, `min=1900-01-01`, `max={today_pk}`, `dir="ltr"` | `field.started_on` |
| | `notes` | `<textarea class="input" rows="2" maxlength="500">`, label and error wiring identical to `f.field` (add an `f.textarea` macro) | `field.notes` |

HTML `min`/`max`/`maxlength` attributes are **conveniences only**; the server is authoritative (the forms use `novalidate`).

Because the patient page has several `<form>` elements, **every field id must be unique on the page**. Extend the `f.field`/`f.select`/`f.textarea` macros with an optional `id_prefix` argument (default `""`) that is prepended to `f-{name}` / `e-{name}`. Each form passes its own prefix: `basics-`, `allergy-new-`, `allergy-{id}-`, `condition-new-`, and so on. Existing callers keep their current ids.

### 5.4 Feedback
- **Success**: when `?saved=<key>` is in the allow-list (BL-31), render `<div class="banner banner--ok" role="status">` with the mapped `profile.saved.*` message **inside the relevant section** (`basics.updated` → Basic details, `allergy.*` → Allergies, and so on).
- **Errors**: field errors under fields (`role="alert"`, `aria-invalid`). Section banners use `.banner--error` with `role="alert"`. Page-level banners (not found, rate limited, internal) render above the identity panel.
- **No toasts or modals.** DESIGN.md has no such pattern (§10).

### 5.5 Shell and navigation changes
- Extract the topbar from `shell.html` into `frontend/templates/partials/topbar.html`. It takes `crumb`, `page_title`, `nav_items` (a list of `{href, label, current}`), and the existing language-switch and sign-out behaviour. `shell.html` and `patient/profile.html` both include it. **The rendered output of `shell.html` for doctors and admins must not change**: `nav_items` is empty for them.
- For the patient role, `nav_items = [{Dashboard → /{locale}/patient}, {My profile → /{locale}/patient/profile}]`, labels `nav.dashboard` / `nav.profile`. The current page gets `aria-current="page"` and the active style: filled background, weight 600 (DESIGN.md nav rule adapted to the topbar).
- The patient dashboard placeholder panel additionally shows a `.btn btn--secondary` link `profile.open_link` to the profile page (AC-24).

### 5.6 New CSS (append to `frontend/static/css/app.css`)
The existing CSS tests must still pass: tokens only, no raw hex, no physical properties (`margin-left/right`, `padding-left/right`, `left:`, `right:`, `text-align:left/right`).

| Class | Purpose / required declarations |
|---|---|
| `.topnav`, `.topnav__link`, `.topnav__link[aria-current="page"]` | Inline nav in the topbar. The active state uses `background: var(--brand-soft); color: var(--brand); font-weight: 600`. |
| `.entry-list` | `list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--sp-2)` |
| `.entry-row` | Design-system list row: `border-radius: 11px; background: var(--surface-2); border: 1px solid var(--line); padding: var(--sp-4); display: flex; flex-wrap: wrap; gap: var(--sp-4); align-items: center; border-inline-start: 3px solid var(--line-2)`. Allergy rows with severity `severe` use `border-inline-start-color: var(--err)`. |
| `.entry-row__main` | `flex: 1; min-width: 0` (long text wraps; `overflow-wrap: anywhere`) |
| `.entry-row__actions` | `display: flex; gap: var(--sp-2); flex-wrap: wrap` |
| `.badge`, `.badge--ok/warn/err/neutral` | Pill, `-soft` fill plus solid matching text (`--ok-soft`/`--ok`, …; neutral = `--surface-2` fill, `--muted` text, `1px solid var(--line-2)`) |
| `.btn--sm` | `padding: 7px 12px; font-size: 12.5px` (still ≥ 44 px min-height under `pointer: coarse`: add `.btn--sm` to the existing coarse-pointer rule) |
| `.btn--danger` | `background: var(--err); color: #fff` (`#fff` on a fill is already allow-listed by the hex test) |
| `.empty-state` | `color: var(--muted); font-size: 13px; padding-block: var(--sp-4)` |
| `.confirm > summary` | `list-style: none; display: inline-flex; cursor: pointer` plus `.confirm > summary::-webkit-details-marker { display: none }`. The open state reveals the form beneath with `margin-block-start: var(--sp-2)` |
| `.readonly-list` (`<dl>`) | Two-column grid of term/value, collapsing to one column under 520 px |
| `.input[type="date"]` | Add to the existing `direction: ltr; text-align: start` rule for email/tel (dates never mirror) |

Layout must remain intrinsic (`auto-fit`/`flex-wrap`/`min-width: 0`). No new `@media` breakpoints beyond the existing `1024px`/`520px`.

### 5.7 JavaScript
- New file `frontend/static/js/profile.js`, loaded with `defer` on the profile page only. It contains **only** the submitting-state guard, the same contract as `auth.js`: on submit, disable **that form's** submit button, set `aria-busy="true"`, and show `.spinner` plus `data-label`.
- The page must be fully functional with this file absent (AC-22). `auth.js` is **not** modified; the existing `test_t19_submitting_state_present` depends on it.

### 5.8 Copy: i18n keys to add
Add every key to **both** `backend/app/i18n/messages/en.json` and `ur.json`. Urdu strings must be reviewed by a native speaker before pilot; the values below are the implementation baseline. Placeholders in `{braces}` are preserved verbatim.

| Key | English | Urdu |
|---|---|---|
| `nav.profile` | My profile | میری پروفائل |
| `profile.title` | My profile | میری پروفائل |
| `profile.subtitle` | Keep your details up to date. Only doctors you give access to can see them. | اپنی معلومات تازہ رکھیں۔ صرف وہی ڈاکٹر انہیں دیکھ سکتے ہیں جنہیں آپ رسائی دیں۔ |
| `profile.open_link` | View and edit my profile | اپنی پروفائل دیکھیں اور ترمیم کریں |
| `profile.self_reported_note` | Details you add here are self-reported and are labelled that way for your doctors. | یہاں درج کی گئی معلومات آپ کی اپنی فراہم کردہ ہیں اور ڈاکٹروں کو اسی طرح دکھائی جائیں گی۔ |
| `profile.no_advice_note` | CuraNode does not check medicines or give medical advice. Ask your doctor before changing any treatment. | کیورا نوڈ ادویات کی جانچ یا طبی مشورہ نہیں دیتا۔ علاج میں کوئی تبدیلی کرنے سے پہلے اپنے ڈاکٹر سے پوچھیں۔ |
| `profile.section.basics` | Basic details | بنیادی معلومات |
| `profile.section.allergies` | Allergies | الرجی |
| `profile.section.conditions` | Chronic conditions | دائمی بیماریاں |
| `profile.section.medications` | Current medications | موجودہ ادویات |
| `profile.age_years` | {years} years | {years} سال |
| `profile.empty.allergies` | No allergies recorded. | کوئی الرجی درج نہیں۔ |
| `profile.empty.conditions` | No chronic conditions recorded. | کوئی دائمی بیماری درج نہیں۔ |
| `profile.empty.medications` | No current medicines recorded. | کوئی موجودہ دوا درج نہیں۔ |
| `profile.recorded_by_clinician` | Recorded by a clinician | معالج کی جانب سے درج |
| `profile.remove_hint` | Removed entries stay in your record history but are no longer shown. | ہٹائے گئے اندراجات ریکارڈ کی تاریخ میں محفوظ رہتے ہیں مگر دکھائی نہیں دیتے۔ |
| `profile.action.save` | Save details | معلومات محفوظ کریں |
| `profile.action.add_allergy` | Add allergy | الرجی شامل کریں |
| `profile.action.add_condition` | Add condition | بیماری شامل کریں |
| `profile.action.add_medication` | Add medicine | دوا شامل کریں |
| `profile.action.edit` | Edit | ترمیم |
| `profile.action.save_entry` | Save | محفوظ کریں |
| `profile.action.cancel` | Cancel | منسوخ |
| `profile.action.remove` | Remove | ہٹائیں |
| `profile.action.confirm_remove` | Yes, remove | جی ہاں، ہٹائیں |
| `profile.saved.basics` | Your details were saved. | آپ کی معلومات محفوظ کر لی گئیں۔ |
| `profile.saved.added` | Added to your profile. | آپ کی پروفائل میں شامل کر دیا گیا۔ |
| `profile.saved.updated` | Changes saved. | تبدیلیاں محفوظ کر لی گئیں۔ |
| `profile.saved.removed` | Removed from your profile. | آپ کی پروفائل سے ہٹا دیا گیا۔ |
| `field.date_of_birth` | Date of birth | تاریخِ پیدائش |
| `field.age` | Age | عمر |
| `field.gender` | Gender | جنس |
| `field.blood_group` | Blood group | بلڈ گروپ |
| `field.emergency_contact` | Emergency contact | ہنگامی رابطہ |
| `field.emergency_contact_help` | Name and phone number of a family member or friend | کسی رشتہ دار یا دوست کا نام اور فون نمبر |
| `field.email_readonly_help` | Your sign-in email can't be changed here. | سائن اِن ای میل یہاں تبدیل نہیں کی جا سکتی۔ |
| `field.substance` | Allergic to | کس چیز سے الرجی ہے |
| `field.substance_placeholder` | e.g. Penicillin | مثلاً پینسلین |
| `field.reaction` | Reaction | ردِعمل |
| `field.reaction_placeholder` | e.g. Rash, swelling | مثلاً خارش، سوجن |
| `field.severity` | Severity | شدت |
| `field.condition_name` | Condition | بیماری |
| `field.condition_placeholder` | e.g. Type 2 diabetes | مثلاً ذیابیطس (ٹائپ 2) |
| `field.onset_date` | Since | کب سے |
| `field.medication_name` | Medicine | دوا |
| `field.medication_placeholder` | e.g. Metformin | مثلاً میٹفارمن |
| `field.strength` | Strength | طاقت |
| `field.strength_placeholder` | e.g. 500 mg | مثلاً 500 ملی گرام |
| `field.frequency` | How often | کتنی بار |
| `field.frequency_placeholder` | e.g. Twice a day | مثلاً دن میں دو بار |
| `field.started_on` | Taking since | کب سے لے رہے ہیں |
| `field.notes` | Notes | نوٹس |
| `option.not_set` | Not set | درج نہیں |
| `gender.female` | Female | خاتون |
| `gender.male` | Male | مرد |
| `gender.other` | Other | دیگر |
| `gender.prefer_not_to_say` | Prefer not to say | بتانا نہیں چاہتے |
| `severity.mild` | Mild | معمولی |
| `severity.moderate` | Moderate | درمیانی |
| `severity.severe` | Severe | شدید |
| `severity.unknown` | Not sure | معلوم نہیں |
| `errors.not_found` | We couldn't find that. | یہ نہیں ملا۔ |
| `errors.not_editable` | This entry was recorded by a clinician and can't be changed here. | یہ اندراج کسی معالج نے کیا ہے اور یہاں تبدیل نہیں ہو سکتا۔ |
| `errors.duplicate_entry` | This is already on your list. | یہ پہلے سے آپ کی فہرست میں موجود ہے۔ |
| `errors.list_full` | You can list up to {max} entries here. | یہاں زیادہ سے زیادہ {max} اندراجات ہو سکتے ہیں۔ |
| `errors.text_required` | This field is required. | یہ خانہ لازمی ہے۔ |
| `errors.text_too_long` | Keep this under {max} characters. | اسے {max} حروف سے کم رکھیں۔ |
| `errors.text_invalid` | Remove unsupported characters. | غیر معاون حروف ہٹا دیں۔ |
| `errors.date_invalid` | Enter a valid date. | درست تاریخ درج کریں۔ |
| `errors.date_in_future` | Date can't be in the future. | تاریخ مستقبل کی نہیں ہو سکتی۔ |
| `errors.date_too_early` | Enter a date after 1 January 1900. | یکم جنوری 1900 کے بعد کی تاریخ درج کریں۔ |
| `errors.date_before_birth` | Date can't be before your date of birth. | تاریخ آپ کی تاریخِ پیدائش سے پہلے کی نہیں ہو سکتی۔ |
| `errors.phone_invalid` | Enter a Pakistani mobile number like +923001234567. | ‎+923001234567 جیسا پاکستانی موبائل نمبر درج کریں۔ |
| `errors.choice_invalid` | Choose one of the options. | دیے گئے اختیارات میں سے ایک منتخب کریں۔ |

Existing keys are reused as-is: `auth.role.patient`, `patient.dashboard.passport`, `nav.dashboard`, `nav.language`, `nav.sign_out`, `field.full_name`, `field.email`, `field.phone`, `errors.name_required`, `errors.rate_limited`, `errors.internal`, `errors.validation_failed`.

---

## 6. API Contract (exact signature)

### 6.0 Deviation from TDD, recorded
TDD §4.4 lists `PATCH /api/v1/me | bearer | FR2`. `GET /api/v1/me` is role-agnostic and is used by every role, while FR2 is patient-only. This feature therefore realises FR2 as **`/api/v1/me/profile`** (plus `/api/v1/me/{allergies|conditions|medications}`) behind `PatientDep`. **`GET /api/v1/me` and `MeOut` are unchanged.**

### 6.1 Conventions (unchanged from the codebase)
- Auth is the `cn_access` HttpOnly cookie, resolved by `deps.current_actor`. There is no `Authorization` header handling in this feature.
- Every error uses the envelope `{"error": {"code", "message_key", "message", "details", "request_id", "retryable"}}` (`errors.envelope`).
- Pydantic field errors map to `422 VALIDATION_FAILED` with `details.fields = {"<field>": "<message>"}`. **Custom validators must raise `pydantic_core.PydanticCustomError("<type>", "errors.<key>")`, so that `msg` is exactly the i18n key.** A plain `ValueError` would prefix it with `"Value error, "`.
- Request models use `ConfigDict(extra="forbid")` (AC-07).
- Dates are ISO `YYYY-MM-DD`. Timestamps are ISO-8601 UTC.

### 6.2 New error classes (`backend/app/errors.py`)
```python
class NotFound(AppError):
    """Nonexistent, foreign, or no-longer-active resource. Deliberately one
    shape for all three so ids cannot be probed (AC-17)."""
    code = "NOT_FOUND"
    http_status = status.HTTP_404_NOT_FOUND
    message_key = "errors.not_found"
    retryable = False


class NotEditable(AppError):
    """The caller owns the patient record but not this entry — it was recorded
    by someone else (a clinician, or a legacy row). Kept separate from
    `Forbidden`, whose contract is role mismatch / unverified doctor only."""
    code = "NOT_EDITABLE"
    http_status = status.HTTP_403_FORBIDDEN
    message_key = "errors.not_editable"
    retryable = False


class DuplicateEntry(AppError):
    code = "DUPLICATE_ENTRY"
    http_status = status.HTTP_409_CONFLICT
    message_key = "errors.duplicate_entry"
    retryable = False


class ListFull(AppError):
    code = "LIST_FULL"
    http_status = status.HTTP_422_UNPROCESSABLE_CONTENT
    message_key = "errors.list_full"
    retryable = False
```
The envelope's `message` for `ListFull` must interpolate `{max}`. Extend `envelope()` so it passes `exc.details` to `translate()` as params **only for keys present in the message template**. Alternatively, `ListFull.__init__(max_entries)` stores `details={"max": n}` and `envelope` calls `translate(key, locale, **{k: v for k, v in details.items() if isinstance(v, (int, str))})`. Either way, existing envelopes must render identically.

### 6.3 Schemas (`backend/app/profile/schemas.py`)
```python
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Gender = Literal["female", "male", "other", "prefer_not_to_say"]
BloodGroup = Literal["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"]
Severity = Literal["mild", "moderate", "severe", "unknown"]

PHONE_PATTERN = r"^\+92[0-9]{10}$"
EARLIEST_DATE = date(1900, 1, 1)
NAME_MIN, NAME_MAX = 2, 120

# Helpers (module-level, unit-tested):
def clean_text(value: str | None) -> str | None: ...
    # strip; collapse internal whitespace runs to one space; "" -> None;
    # any char with unicodedata.category(c) == "Cc" -> PydanticCustomError("text_invalid", "errors.text_invalid")
def normalise_name(value: str) -> str: ...
    # clean_text(value).casefold()  — used for duplicate detection (BL-19)
def check_past_date(value: date | None) -> date | None: ...
    # None passes; < EARLIEST_DATE -> errors.date_too_early; > dbtypes.today_pk() -> errors.date_in_future


class ProfileUpdateRequest(BaseModel):
    """PATCH semantics: only fields in `model_fields_set` are applied.
    An explicit null clears an optional field; `full_name` cannot be null."""
    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, max_length=NAME_MAX)
    date_of_birth: date | None = None
    gender: Gender | None = None
    blood_group: BloodGroup | None = None
    phone_e164: str | None = Field(default=None, pattern=PHONE_PATTERN)
    emergency_contact: str | None = Field(default=None, max_length=255)
    # validators: clean_text on full_name/emergency_contact (before length checks);
    # full_name in fields_set and (None or len < NAME_MIN) -> errors.name_required;
    # check_past_date on date_of_birth; phone pattern failure -> errors.phone_invalid
    # (use a field_validator, not only Field(pattern=), so the key is ours).


class AllergyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    substance: str = Field(max_length=NAME_MAX)          # required; min NAME_MIN after clean_text
    reaction: str | None = Field(default=None, max_length=255)
    severity: Severity | None = None


class AllergyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    substance: str | None = Field(default=None, max_length=NAME_MAX)   # may not be set to null
    reaction: str | None = Field(default=None, max_length=255)
    severity: Severity | None = None


class ConditionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=NAME_MAX)
    onset_date: date | None = None                        # check_past_date


class ConditionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=NAME_MAX)        # may not be set to null
    onset_date: date | None = None


class MedicationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=NAME_MAX)
    strength: str | None = Field(default=None, max_length=50)
    frequency: str | None = Field(default=None, max_length=50)
    started_on: date | None = None                        # check_past_date
    notes: str | None = Field(default=None, max_length=500)


class MedicationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=NAME_MAX)        # may not be set to null
    strength: str | None = Field(default=None, max_length=50)
    frequency: str | None = Field(default=None, max_length=50)
    started_on: date | None = None
    notes: str | None = Field(default=None, max_length=500)


class AllergyOut(BaseModel):
    id: uuid.UUID
    substance: str
    reaction: str | None
    severity: str | None          # raw stored value — may be a legacy value outside `Severity`
    recorded_at: datetime
    updated_at: datetime | None
    editable: bool


class ConditionOut(BaseModel):
    id: uuid.UUID
    name: str
    onset_date: date | None
    status: str                   # raw stored value; never "resolved" in responses
    icd10_code: str | None        # read-only, never written by this feature
    recorded_at: datetime | None
    updated_at: datetime | None
    editable: bool


class MedicationOut(BaseModel):
    id: uuid.UUID
    name: str
    strength: str | None
    frequency: str | None
    started_on: date | None
    notes: str | None
    recorded_at: datetime
    updated_at: datetime | None
    editable: bool


class PatientProfileOut(BaseModel):
    passport_no: str
    full_name: str
    email: str | None             # user_profile.email is nullable in the shared schema
    phone_e164: str | None        # unmasked — owner-only endpoint
    date_of_birth: date | None
    age_years: int | None
    gender: str | None            # raw stored value
    blood_group: str | None       # raw stored value
    emergency_contact: str | None
    allergies: list[AllergyOut]
    conditions: list[ConditionOut]
    medications: list[MedicationOut]
```
Validation error keys per field must be exactly those listed in BL-08…BL-18. Length violations map to `errors.text_too_long`, and a missing required name maps to `errors.text_required` (entries) or `errors.name_required` (`full_name`).

### 6.4 JSON router (`backend/app/profile/router.py`, included in `main.py` after `identity_router`)
```python
router = APIRouter(prefix="/api/v1/me", tags=["profile"])

@router.get("/profile", response_model=PatientProfileOut)
async def get_my_profile(actor: PatientDep, session: SessionDep) -> PatientProfileOut: ...

@router.patch("/profile", response_model=PatientProfileOut, dependencies=[ProfileWriteRateLimit])
async def update_my_profile(
    request: Request, actor: PatientDep, session: SessionDep, body: ProfileUpdateRequest
) -> PatientProfileOut: ...

# ── Allergies ─────────────────────────────────────────────────────────────
@router.post("/allergies", status_code=status.HTTP_201_CREATED,
             response_model=AllergyOut, dependencies=[ProfileWriteRateLimit])
async def add_allergy(
    request: Request, actor: PatientDep, session: SessionDep, body: AllergyCreate
) -> AllergyOut: ...

@router.patch("/allergies/{allergy_id}", response_model=AllergyOut,
              dependencies=[ProfileWriteRateLimit])
async def update_allergy(
    allergy_id: uuid.UUID, request: Request, actor: PatientDep, session: SessionDep,
    body: AllergyUpdate,
) -> AllergyOut: ...

@router.delete("/allergies/{allergy_id}", status_code=status.HTTP_204_NO_CONTENT,
               dependencies=[ProfileWriteRateLimit])
async def remove_allergy(
    allergy_id: uuid.UUID, request: Request, actor: PatientDep, session: SessionDep
) -> Response: ...

# ── Chronic conditions ────────────────────────────────────────────────────
@router.post("/conditions", status_code=201, response_model=ConditionOut,
             dependencies=[ProfileWriteRateLimit])
async def add_condition(request: Request, actor: PatientDep, session: SessionDep,
                        body: ConditionCreate) -> ConditionOut: ...

@router.patch("/conditions/{condition_id}", response_model=ConditionOut,
              dependencies=[ProfileWriteRateLimit])
async def update_condition(condition_id: uuid.UUID, request: Request, actor: PatientDep,
                           session: SessionDep, body: ConditionUpdate) -> ConditionOut: ...

@router.delete("/conditions/{condition_id}", status_code=204,
               dependencies=[ProfileWriteRateLimit])
async def remove_condition(condition_id: uuid.UUID, request: Request, actor: PatientDep,
                           session: SessionDep) -> Response: ...

# ── Current medications ───────────────────────────────────────────────────
@router.post("/medications", status_code=201, response_model=MedicationOut,
             dependencies=[ProfileWriteRateLimit])
async def add_medication(request: Request, actor: PatientDep, session: SessionDep,
                         body: MedicationCreate) -> MedicationOut: ...

@router.patch("/medications/{medication_id}", response_model=MedicationOut,
              dependencies=[ProfileWriteRateLimit])
async def update_medication(medication_id: uuid.UUID, request: Request, actor: PatientDep,
                            session: SessionDep, body: MedicationUpdate) -> MedicationOut: ...

@router.delete("/medications/{medication_id}", status_code=204,
               dependencies=[ProfileWriteRateLimit])
async def remove_medication(medication_id: uuid.UUID, request: Request, actor: PatientDep,
                            session: SessionDep) -> Response: ...
```
Every write handler passes `user_agent=request.headers.get("user-agent")` to the service. The IP comes from `actor.ip_address`. `DELETE` handlers return `Response(status_code=204)` with an empty body.

**Module-scope imports:** `PatientDep`, `SessionDep`, and `ProfileWriteRateLimit` must be imported at module scope. With `from __future__ import annotations`, a function-local import degrades the dependency into a query parameter (see the comment in `tests/test_auth.py`).

### 6.5 Rate-limit dependency (`backend/app/deps.py`)
```python
async def enforce_profile_write_rate_limit(actor: PatientDep) -> None:
    """Per-patient write budget. Depends on PatientDep so 401/403 surface before 429."""
    count = await cache.incr(ratelimit_key("profile_write", str(actor.user_id)), 60)
    if count > settings.profile_write_rate_limit_per_minute:
        raise RateLimited(retry_after_s=60)

ProfileWriteRateLimit = Depends(enforce_profile_write_rate_limit)
```
To keep FastAPI's per-request dependency cache effective, hoist the patient gate to a module-level callable, `_require_patient = require_role("patient")`, and define `PatientDep = Annotated[Actor, Depends(_require_patient)]`. Behaviour is unchanged. The web handlers call a plain helper, `await check_profile_write_rate(actor)`, that shares the same counter key, and catch `RateLimited`.

### 6.6 Service signatures (`backend/app/profile/service.py`)
```python
async def get_profile(session: AsyncSession, actor: Actor) -> PatientProfileOut: ...
async def update_profile(session: AsyncSession, actor: Actor, body: ProfileUpdateRequest,
                         *, user_agent: str | None = None) -> PatientProfileOut: ...

async def add_allergy(session, actor, body: AllergyCreate, *, user_agent=None) -> AllergyOut: ...
async def update_allergy(session, actor, allergy_id: uuid.UUID, body: AllergyUpdate,
                         *, user_agent=None) -> AllergyOut: ...
async def remove_allergy(session, actor, allergy_id: uuid.UUID, *, user_agent=None) -> None: ...

async def add_condition(session, actor, body: ConditionCreate, *, user_agent=None) -> ConditionOut: ...
async def update_condition(session, actor, condition_id: uuid.UUID, body: ConditionUpdate,
                           *, user_agent=None) -> ConditionOut: ...
async def remove_condition(session, actor, condition_id: uuid.UUID, *, user_agent=None) -> None: ...

async def add_medication(session, actor, body: MedicationCreate, *, user_agent=None) -> MedicationOut: ...
async def update_medication(session, actor, medication_id: uuid.UUID, body: MedicationUpdate,
                            *, user_agent=None) -> MedicationOut: ...
async def remove_medication(session, actor, medication_id: uuid.UUID, *, user_agent=None) -> None: ...

def compute_age(dob: date | None, today: date) -> int | None: ...
    # full years; birthday not yet reached this year -> subtract 1; Feb-29 DOB counts as Mar-1 in non-leap years
```
Raises: `NotFound`, `NotEditable`, `DuplicateEntry`, `ListFull`, and `ValidationFailed` (date-before-birth). The service **commits** on success. On any exception it lets the caller's session roll back; it never commits a partial change.

### 6.7 Status-code summary
| Endpoint | Success | Errors |
|---|---|---|
| `GET /api/v1/me/profile` | 200 | 401, 403, 404 |
| `PATCH /api/v1/me/profile` | 200 | 401, 403, 404, 422, 429 |
| `POST /api/v1/me/{kind}` | 201 | 401, 403, 404, 409, 422 (`VALIDATION_FAILED` / `LIST_FULL`), 429 |
| `PATCH /api/v1/me/{kind}/{id}` | 200 | 401, 403 (`FORBIDDEN` role / `NOT_EDITABLE`), 404, 409, 422, 429 |
| `DELETE /api/v1/me/{kind}/{id}` | 204 | 401, 403, 404, 422 (malformed id), 429 |

---

## 7. Data Requirements

### 7.1 Tables used (verified in the live shared schema, 2026-09-22)

**`user_profile`** (shared, pre-existing): this feature **reads** `email` and **writes** only `full_name` and `phone`.
| Column | Type | Notes |
|---|---|---|
| `user_id` | uuid PK → `auth.users.id` | |
| `email` | varchar(255) NULL | read-only here; kept in sync from `auth.users` by the `on_auth_user_updated` trigger |
| `phone` | varchar(32) NULL | mapped as `Profile.phone_e164`; **written by this feature** |
| `full_name` | varchar(120) NULL | **written by this feature** |
| `role`, `status` | varchar(20) | **never written**; the `trg_prevent_role_escalation` trigger guards them too |

**`patient`** (shared, pre-existing)
| Column | Type | This feature |
|---|---|---|
| `patient_id` | uuid PK | read (scoping key; audit `subject_patient_id`) |
| `user_id` | uuid UNIQUE → user_profile | read (resolve caller) |
| `passport_uid` | varchar(64) UNIQUE NOT NULL | read-only (displayed) |
| `full_name` | varchar(255) NOT NULL | written (synced with `user_profile.full_name`) |
| `date_of_birth` | date NULL | written |
| `gender` | varchar(20) NULL | written (allowed values BL-10; legacy values tolerated on read) |
| `blood_group` | varchar(5) NULL | written (BL-11) |
| `emergency_contact` | varchar(255) NULL | written; **must be added to the `Patient` model** |
| `cnic_hash` | varchar(255) NULL | **not mapped, never touched** |
| `created_at` | timestamptz | untouched (no `updated_at` exists; do not add one) |

**`allergy`** (shared, pre-existing, 2 synthetic rows at time of writing, including a legacy `severity='very much'`)
| Column | Type | Notes |
|---|---|---|
| `allergy_id` | uuid PK (default `gen_random_uuid()`) | the app supplies `uuid7()` |
| `patient_id` | uuid NOT NULL → patient ON DELETE CASCADE | |
| `substance` | varchar(255) NOT NULL | app limit 120 |
| `reaction` | varchar(255) NULL | |
| `severity` | varchar(20) NULL, **no CHECK** | map as a plain `String(20)`, **not** `_enum(...)`: `validate_strings=True` would raise on the legacy value when loading |
| `recorded_at` | timestamptz NOT NULL default now() | |
| `recorded_by` | **NEW** uuid NULL → user_profile(user_id) ON DELETE SET NULL | |
| `updated_at` | **NEW** timestamptz NULL | |
| `removed_at` | **NEW** timestamptz NULL | soft-remove marker |

**`chronic_condition`** (shared, pre-existing, 0 rows)
| Column | Type | Notes |
|---|---|---|
| `condition_id` | uuid PK | the app supplies `uuid7()` |
| `patient_id` | uuid NOT NULL → patient ON DELETE CASCADE | |
| `name` | varchar(255) NOT NULL | app limit 120 |
| `icd10_code` | varchar(10) NULL | read-only here |
| `onset_date` | date NULL | |
| `status` | varchar(20) NOT NULL default `'active'`, **no CHECK** | this app writes only `'active'` / `'resolved'`; map as `String(20)` |
| `risk_level` | varchar(20) NULL | never read or written by this feature (not exposed) |
| `recorded_by` | **NEW** uuid NULL → user_profile ON DELETE SET NULL | |
| `recorded_at` | **NEW** timestamptz NOT NULL server_default now() | backfills existing rows automatically |
| `updated_at` | **NEW** timestamptz NULL | |

**`patient_medication`** (**NEW**, owned by this repo). The name avoids confusion with the shared `medicine` catalogue and the doctor-owned `prescription_item`.
| Column | Type | Constraints |
|---|---|---|
| `medication_id` | uuid | PK; server_default `gen_random_uuid()`; the app supplies `uuid7()` |
| `patient_id` | uuid | NOT NULL, FK → `patient(patient_id)` ON DELETE CASCADE |
| `name` | varchar(255) | NOT NULL |
| `strength` | varchar(50) | NULL |
| `frequency` | varchar(50) | NULL |
| `started_on` | date | NULL |
| `notes` | text | NULL (app limit 500) |
| `recorded_by` | uuid | NULL, FK → `user_profile(user_id)` ON DELETE SET NULL |
| `recorded_at` | timestamptz | NOT NULL, server_default `now()` |
| `updated_at` | timestamptz | NULL |
| `removed_at` | timestamptz | NULL |
| index | `idx_patient_medication_patient_id` on `(patient_id)` | |

**`audit_log`** (repo-owned, existing): append only, via `audit.writer.write()`. It has **no `resource_id` column**, so the resource id goes in `detail` (BL-24).

**Not used, do not touch:** `medicine`, `prescription`, `prescription_item` (doctor-owned, trigger-bound to an encounter), `consent_grant`, `access_log`, `diagnosis`, `vital_sign`.

### 7.2 ORM models (`backend/app/db/models.py`)
- `Patient`: add `emergency_contact: Mapped[str | None] = mapped_column(String(255), nullable=True)`.
- Add `Allergy`, `ChronicCondition`, and `PatientMedication`. Map column names exactly as in §7.1 (e.g. `id` ↔ `"allergy_id"`, following the existing `mapped_column("<db_name>", ...)` style).
- Python-side defaults: `recorded_at`/`created` defaults use `default=utcnow` (needed for the SQLite test DB); `status` default `"active"`.
- Keep models **portable to SQLite** (tests use `Base.metadata.create_all`): no Postgres-only types, and no server-side-only defaults the app relies on.
- Each model's docstring states its ownership: shared vs repo-owned, and which columns this repo added.

### 7.3 Migration (hand-written, additive)
Create it with `uv run alembic revision -m "patient profile: entry provenance, soft-remove, patient_medication"`, then **replace the body by hand**. `down_revision = "6feacefae2d0"`.

`upgrade()` must, in order:
1. `allergy`: `add_column recorded_by` (Uuid, FK `user_profile.user_id` ondelete SET NULL, nullable), `add_column updated_at` (DateTime tz, nullable), `add_column removed_at` (DateTime tz, nullable).
2. `chronic_condition`: `add_column recorded_by` (as above), `add_column recorded_at` (DateTime tz, `nullable=False`, `server_default=sa.func.now()`), `add_column updated_at` (nullable).
3. `create_table("patient_medication", ...)` exactly per §7.1, plus `create_index("idx_patient_medication_patient_id", ...)`.
4. **Postgres only** (`if op.get_bind().dialect.name == "postgresql":`), enable RLS and create policies mirroring the shared tables. Without this, the new table would be readable through Supabase's public Data API.
   ```sql
   ALTER TABLE public.patient_medication ENABLE ROW LEVEL SECURITY;
   CREATE POLICY patient_medication_select ON public.patient_medication FOR SELECT
     USING (patient_id = my_patient_id() OR doctor_has_patient_access(patient_id) OR is_admin());
   CREATE POLICY patient_medication_write ON public.patient_medication FOR ALL
     USING (patient_id = my_patient_id() OR is_admin())
     WITH CHECK (patient_id = my_patient_id() OR is_admin());
   ```
   (`my_patient_id()`, `doctor_has_patient_access(uuid)`, and `is_admin()` already exist in `public`.)

`upgrade()` must contain **no** `drop_*`, `alter_column`, or `rename` of anything pre-existing. `downgrade()` reverses the steps in reverse order: drop policies, index, and table, then drop the added columns.

Apply with `uv run alembic upgrade head` against the dev Supabase project. **Do not** run it against any non-synthetic environment without sign-off.

### 7.4 Derived and computed data
- `age_years = compute_age(date_of_birth, dbtypes.today_pk())`.
- `today_pk()` goes in `backend/app/db/types.py`: `datetime.now(PKT).date()` with `PKT = timezone(timedelta(hours=5), "PKT")`. Asia/Karachi has had no DST since 2009. **Do not** use `zoneinfo.ZoneInfo("Asia/Karachi")`: Windows dev machines have no IANA database without adding the `tzdata` package.
- Callers use `from ..db import types as dbtypes; dbtypes.today_pk()` (module attribute access), so tests can monkeypatch `app.db.types.today_pk` in one place.

### 7.5 Settings (`backend/app/settings.py`)
```python
# ── Patient profile (FR2) ────────────────────────────────────────────────
profile_write_rate_limit_per_minute: int = 30
profile_max_entries_per_list: int = 50
```
Add both to `.env.example` (commented, with their defaults).

### 7.6 Data classification
Everything this feature stores is **personal health information**. Dev/test data must be synthetic (NFR19); the existing startup guard already enforces this for `user_profile`. Nothing from this feature may be sent to any third-party service.

---

## 8. Business Logic (numbered rules)

**Access**
- **BL-01** Only an active `patient`-role user with a `patient` row may use this feature. Enforce it with `PatientDep` (API) and `_patient_page_guard` (web). A doctor or admin cannot read or write another person's profile through this feature under any circumstances. Doctor access to patient data is FR21 and goes through consent, not here.
- **BL-02** Every query touching `allergy`, `chronic_condition`, or `patient_medication` includes `patient_id = <caller's patient.id>`. RLS is **not** relied on, because the app role bypasses it.
- **BL-03** Foreign, nonexistent, and inactive-for-`PATCH` ids all raise the same `NotFound`, with an identical response body apart from `request_id`. Nothing may reveal that another patient's id exists.

**Immutable data**
- **BL-04** Never written by this feature: `user_profile.email`, `.role`, `.status`, `.locale`, `.is_synthetic`, `.failed_logins`, `.locked_until`, `.last_login_at`; `patient.passport_uid`, `.cnic_hash`, `.user_id`, `.patient_id`; `chronic_condition.icd10_code`, `.risk_level`; any entry's `recorded_by`/`recorded_at` after creation.
- **BL-05** API request models forbid extra fields (`422`). The web handlers read only the named form fields and ignore everything else.
- **BL-06** Phone is written **only** to `user_profile.phone`. No Supabase Auth call (admin or user) is made by this feature. *Known hazard, documented for future features:* the `on_auth_user_updated` trigger (`AFTER UPDATE OF email, phone ON auth.users`) copies `auth.users.phone` into `user_profile.phone`. Any future feature that changes a user's **email** in Supabase Auth will overwrite the profile phone with `auth.users.phone`, which is normally `NULL`. That feature must re-apply the profile phone.
- **BL-07** `full_name` is written to both `user_profile.full_name` and `patient.full_name` in one transaction. They must never diverge because of this feature.

**Basic-details validation** (after `clean_text`)
- **BL-08** `full_name`: required when present in the request; 2–120 characters; control characters rejected. Keys: `errors.name_required`, `errors.text_too_long`, `errors.text_invalid`.
- **BL-09** `date_of_birth`: optional; valid ISO date (`errors.date_invalid`); `>= 1900-01-01` (`errors.date_too_early`); `<= today_pk()` (`errors.date_in_future`).
- **BL-10** `gender` ∈ {`female`, `male`, `other`, `prefer_not_to_say`} or null (`errors.choice_invalid`).
- **BL-11** `blood_group` ∈ {`A+`, `A-`, `B+`, `B-`, `AB+`, `AB-`, `O+`, `O-`} or null. It is case-sensitive, with no normalisation (`errors.choice_invalid`). "Unknown" is represented by null.
- **BL-12** `phone_e164` matches `^\+92[0-9]{10}$` or is null; empty web input means null (`errors.phone_invalid`). This matches the registration schema.
- **BL-13** `emergency_contact`: optional free text; ≤ 255 characters; control characters rejected. It is stored as typed after `clean_text`, with no parsing.
- **BL-14** **Web only:** before validation, drop each submitted field whose value (after empty→null) equals the currently stored value. This means (a) legacy stored values the patient did not touch never fail validation, and (b) `fields_changed` contains only real changes. The API validates every field it is sent.

**Entry validation** (after `clean_text`)
- **BL-15** Names (`substance`, condition `name`, medication `name`): required on create and cannot be set to null on update (`errors.text_required`); 2–120 characters (`errors.text_required` below 2, `errors.text_too_long` above 120).
- **BL-16** Optional texts: `reaction` ≤ 255, `strength` ≤ 50, `frequency` ≤ 50, `notes` ≤ 500 (`errors.text_too_long`). Empty after cleaning becomes null.
- **BL-17** `severity` ∈ {`mild`, `moderate`, `severe`, `unknown`} or null (`errors.choice_invalid`).
- **BL-18** Since-dates (`onset_date`, `started_on`): optional; same bounds as BL-09; and, when the patient has a stored `date_of_birth`, not earlier than it (`errors.date_before_birth`, checked in the service). Changing the DOB later does **not** re-validate existing entries.

**List integrity**
- **BL-19** Duplicate rule: two entries of the same kind are duplicates when `clean_text(a).casefold() == clean_text(b).casefold()`. Only **active** entries count (not removed, not resolved), whether editable or not. This check also gives natural idempotency for retried adds (NFR8).
- **BL-20** Editability: an entry is editable **if and only if** `recorded_by == caller's user_id`. `recorded_by IS NULL` (legacy rows and rows written by the wider product without provenance) and any other user's id are **read-only**. This errs toward patient safety: a patient must never be able to erase a clinician-recorded allergy.
- **BL-21** Active definition: allergy and medication are active when `removed_at IS NULL`; a condition is active when `status IS DISTINCT FROM 'resolved'`. Inactive entries never appear in any response or page, and there is no restore action in this feature.
- **BL-22** Ordering (deterministic):
  - allergies by severity rank (`severe`=0, `moderate`=1, `mild`=2, `unknown`=3, null or legacy=4), then `substance.casefold()`, then `recorded_at`.
  - conditions by `name.casefold()`, then `recorded_at` (NULLs last).
  - medications by `name.casefold()`, then `recorded_at`.
- **BL-23** At most `profile_max_entries_per_list` active entries per kind per patient (`ListFull`). Inactive entries do not count.

**Audit**
- **BL-24** Every successful mutation writes exactly one row through `audit.writer.write()` in the mutation's own transaction, with these fields:
  - `actor_user_id` = caller, `actor_role="patient"`
  - `subject_patient_id = patient.patient_id`
  - `resource_type` ∈ {`patient`, `allergy`, `chronic_condition`, `patient_medication`}
  - `ip_address = actor.ip_address`, `user_agent` from the request
  - `detail` JSON always contains `"resource_id"`.

  Add these constants to `audit/writer.py`:

  | Constant | Action string |
  |---|---|
  | `PROFILE_UPDATE` | `profile.update` |
  | `PROFILE_ALLERGY_ADD` / `_UPDATE` / `_REMOVE` | `profile.allergy.add` / `.update` / `.remove` |
  | `PROFILE_CONDITION_ADD` / `_UPDATE` / `_REMOVE` | `profile.condition.add` / `.update` / `.remove` |
  | `PROFILE_MEDICATION_ADD` / `_UPDATE` / `_REMOVE` | `profile.medication.add` / `.update` / `.remove` |
- **BL-25** For basic details (`profile.update`), `detail = {"resource_id", "fields_changed": [...]}` only. **No values**: name, DOB, phone, and emergency contact are identity PII and stay out of the audit blob, in line with `writer.py`'s existing "no PII in detail" rule.
- **BL-26** For entries, `detail` also carries clinical values, so D1's "nothing silently lost" holds even for in-place edits:
  - add: `"after"`
  - update: `"fields_changed"`, `"before"`, `"after"`
  - remove: `"before"` (a snapshot)

  `CLINICAL_FIELDS` = allergy {`substance`, `reaction`, `severity`}, condition {`name`, `onset_date`, `status`}, medication {`name`, `strength`, `frequency`, `started_on`}. **`notes` is never copied into audit values** because it is free text that may contain PII; it appears only in `fields_changed`. Update the `writer.py` module docstring to state this exception precisely.
- **BL-27** No audit row for: reads, rejected requests, no-op updates, and repeat removals.

**Logging**
- **BL-28** Never pass field values to the logger. As defence in depth, extend `log_config.REDACTED_KEYS` with `date_of_birth`, `emergency_contact`, `blood_group`, `gender`, `substance`, `reaction`, `severity`, `strength`, `frequency`, `started_on`, `onset_date`, `notes`, `before`, and `after`.

**Web behaviour**
- **BL-29** Post/Redirect/Get: every successful web mutation answers `303` to the profile page with `saved` and an anchor, so a refresh never re-submits.
- **BL-30** Failed web mutations re-render per §4.8. Only the failing form shows the submitted values; other sections show DB state.
- **BL-31** The `saved` query value is honoured only if it is in {`basics.updated`, `allergy.added`, `allergy.updated`, `allergy.removed`, `condition.added`, `condition.updated`, `condition.removed`, `medication.added`, `medication.updated`, `medication.removed`}. Anything else is ignored, and user input is never reflected.
- **BL-32** `?edit=<allergy|condition|medication>&id=<uuid>` puts that row in edit mode only if the id is an active, **editable**, own entry of that kind. Otherwise the parameter is silently ignored: no error, and no existence oracle.
- **BL-33** CSRF: web forms rely on the session cookies being `SameSite=Strict` (existing posture), so no token is added. Do not weaken the cookie settings.
- **BL-34** Rate limit (AC-20) counts every write attempt, API and web, per patient in a shared 60-second window.

**Clinical safety**
- **BL-35** The profile is **informational only**. It performs no drug-interaction checks, dosage checks, allergy-vs-medication cross-checks, or suggestions of any kind (PRD §6.2, NFR21, BR-04). It shows no AI-generated content, so no FR31 disclaimer component is needed. It does carry the static `profile.no_advice_note`.
- **BL-36** Self-reported entries are the patient's claims, and are labelled as such for future doctor views (`profile.self_reported_note`). This feature never marks them as clinically confirmed.

---

## 9. Dependencies

### 9.1 Internal code (read before changing)
| File | Use |
|---|---|
| `backend/app/deps.py` | `PatientDep`, `SessionDep`, `OptionalActorDep`, `Actor`; add `ProfileWriteRateLimit` and `check_profile_write_rate` (§6.5) |
| `backend/app/db/models.py` | `Profile`, `Patient` (+`emergency_contact`); add `Allergy`, `ChronicCondition`, `PatientMedication` |
| `backend/app/db/types.py` | `uuid7`, `utcnow`; add `today_pk` and `PKT` |
| `backend/app/errors.py` | Add `NotFound`, `NotEditable`, `DuplicateEntry`, `ListFull`; `envelope` param interpolation (§6.2) |
| `backend/app/audit/writer.py` | Add `PROFILE_*` constants; docstring update (BL-26) |
| `backend/app/log_config.py` | Extend `REDACTED_KEYS` (BL-28) |
| `backend/app/cache.py` | `cache.incr`, `ratelimit_key` |
| `backend/app/i18n/catalogue.py` + `messages/{en,ur}.json` | New keys (§5.8) |
| `backend/app/settings.py`, `.env.example` | Two new settings (§7.5) |
| `backend/app/main.py` | `app.include_router(profile_router)` after `identity_router` and **before** `web_router` |
| `backend/app/web/router.py` | New page handlers (§4.7). Extend `_guarded` to pass `nav_items` and the profile link for patients |
| `frontend/templates/shell.html`, new `partials/topbar.html`, new `patient/profile.html`, `partials/form_field.html` (new optional macro args + `textarea` macro) | UI |
| `frontend/static/css/app.css`, new `frontend/static/js/profile.js` | Styling, submit guard |
| `alembic/versions/<new>_patient_profile_*.py` | Migration (§7.3) |
| `tests/conftest.py` | Reuse `client`, `db`, `clinic`, `make_user`; add helpers (§11.1) |

### 9.2 Database objects relied on (shared Supabase project)
- Tables: `user_profile`, `patient`, `allergy`, `chronic_condition`, `audit_log`.
- Functions (for the new RLS policies only): `public.my_patient_id()`, `public.doctor_has_patient_access(uuid)`, `public.is_admin()`.
- Triggers whose behaviour this feature must not disturb:
  - `on_auth_user_updated`: only fires on `auth.users` email/phone updates, which this feature never makes (BL-06).
  - `trg_prevent_role_escalation`: this feature never changes `role`/`status`.
- The migration head currently applied to the dev project is `6feacefae2d0`.

### 9.3 Packages
**No new runtime or dev dependencies.** Everything needed (`fastapi`, `pydantic`, `sqlalchemy`, `alembic`, `jinja2`, `structlog`; `pytest`, `httpx`, `aiosqlite`) is already in `pyproject.toml`. `tzdata` is deliberately avoided (§7.4).

### 9.4 Prerequisites and sequencing
1. Models, migration, and settings.
2. `errors.py` additions and the `deps.py` rate limit.
3. `profile/schemas.py`, then `profile/service.py`, then `profile/router.py`, wired into `main.py`.
4. i18n keys.
5. Templates and CSS/JS, then the web handlers.
6. Tests (§11).
7. `uv run alembic upgrade head` on the dev Supabase project. Then manually smoke-test with the seeded patient account (`seed_synthetic.py`).

### 9.5 Downstream features that will depend on this one
FR21 (doctor views a consented patient's profile through the consent gateway) and FR22 ("what changed" should include allergy, condition, and medication changes, which is why provenance and timestamps are recorded now). Neither is built here.

---

## 10. Out of Scope

Explicitly **not** part of this feature. Do not implement any of the following, even partially:

1. **Doctor or clinic view of the patient profile** (FR21) and anything consent-related (FR4, `consent_grant`, the consent gateway).
2. **The patient access log** (FR5 read side, `access_log`). Only audit **writes** happen here.
3. **Viewing or restoring removed/resolved entries** (history view, undo), and any hard delete.
4. **Doctor or admin editing** of allergies, conditions, or medications. Displaying clinician entries read-only is in scope; writing them is not.
5. **Changing sign-in email or password**, password reset, phone verification (OTP/SMS), and any Supabase Auth call.
6. **CNIC** capture or display (`patient.cnic_hash`).
7. **Preferred-language setting** (`user_profile.locale`). Language remains URL-driven.
8. **Profile photo or avatar upload**, and **caregiver/delegated access** (FR7).
9. **Linking medications to the `medicine` catalogue**, importing medications from prescriptions or OCR (FR10–FR12), and reading `prescription`/`prescription_item`.
10. **Any clinical intelligence:** drug-interaction checks, allergy-vs-medicine warnings, dosage validation, ICD-10 coding by the patient, `risk_level`, and any AI output (BL-35).
11. **Vitals, height, or weight** (`vital_sign`), and **diagnoses** (`diagnosis`).
12. **PDF export or sharing** of the profile (FR14).
13. **`Idempotency-Key` header support** (TDD §4.1). Duplicate detection (BL-19) covers retried adds for now.
14. **Optimistic concurrency** (ETag/If-Match). Concurrent edits are last-write-wins, and each edit is audited.
15. **Notifications** of profile changes (FR20).
16. **A profile-completeness meter**, onboarding prompts, and required-field nagging.
17. **Changes to OAuth onboarding** or registration forms (e.g. collecting DOB at signup).
18. **Postgres privilege changes** (`REVOKE UPDATE/DELETE` on `audit_log`). Tracked separately (see `done.md` §3.1).
19. **Client-side (JS) validation** beyond the submit guard, and any SPA-style API consumption from the page.

---

## 11. Testing Requirements

All tests run against the existing harness: in-memory SQLite plus `tests/fakes.py`'s `FakeSupabaseAuth`, with **no network**. Put them in a new `tests/test_profile.py`, plus `tests/test_profile_schemas.py` for pure unit tests. Name each test `test_tN_<description>`, matching AC-N. A test may cover more than one assertion, but **every AC below needs at least one test**.

### 11.1 Fixtures and helpers (add to `tests/conftest.py` or the test module)
- `async def login(client, email)` posts to `/api/v1/auth/login` with `TEST_PASSWORD`, so cookies are stored on `client`.
- `patient_user` fixture: `make_user(db, email="pat@x.com", role=UserRole.PATIENT)`, logged in.
- `second_patient` fixture: another patient with their own entries, for isolation tests.
- `add_allergy_row(db, patient_id, *, recorded_by, substance, severity=None, removed_at=None)` and equivalents for conditions and medications insert rows directly. They are used for legacy (`recorded_by=None`), clinician-owned, and foreign rows.
- `freeze_today` fixture: `monkeypatch.setattr("app.db.types.today_pk", lambda: date(2026, 9, 22))`.
- `caplog`/structlog capture helper for AC-19 (render logs to a string via a `StringIO` sink or `structlog.testing.capture_logs()`).

### 11.2 Test cases

| Test | AC | Setup → action → expected |
|---|---|---|
| **T1** `test_t1_profile_page_renders` | AC-01 | A patient with DOB 1990-01-01 (frozen today 2026-09-22) and one allergy, no conditions, no meds. `GET /en/patient/profile` returns `200`. The HTML contains the passport number, the email, `36 years` (via `translate("profile.age_years", years=36)`), the allergy substance, `profile.empty.conditions`, `profile.empty.medications`, and the basic form prefilled (`value="1990-01-01"`). A removed allergy row is **not** in the HTML. |
| **T2** `test_t2_profile_api_shape_and_order` | AC-02 | Seed allergies with severities `mild`, `severe`, None, and legacy `very much`, plus 2 conditions (one `resolved`) and 2 meds (one removed). `GET /api/v1/me/profile` returns `200`. The body validates as `PatientProfileOut`, the allergy order is `severe, mild, None/legacy` per BL-22, the resolved condition and removed med are absent, and `age_years` is correct. No DOB gives `age_years is None`. |
| **T3** `test_t3_access_control_matrix` | AC-03 | (a) Anonymous: API `401`, web `303` → `/en/login?next=/en/patient/profile`. (b) Doctor and admin: API `403 FORBIDDEN`, web `303` → own area. (c) Suspended patient: API `401`. (d) A patient-role `Profile` with **no** `Patient` row: API `GET` gives `404 NOT_FOUND`, web gives `303` → `/en/onboarding`. Each write endpoint returns `401` anonymously and `403` for a doctor. |
| **T4** `test_t4_update_basics_api_and_web` | AC-04 | API `PATCH {"blood_group":"O+"}` returns `200`; only `blood_group` changes and the other fields are untouched. `PATCH {"full_name":"Sana Iqbal"}` updates **both** `Profile.full_name` and `Patient.full_name`. Web `POST /en/patient/profile` with a full form returns `303` → `/en/patient/profile?saved=basics.updated#basics`. Following it renders the `profile.saved.basics` banner. The dashboard shell shows the new name. |
| **T5** `test_t5_clear_optional_fields` | AC-05 | Set DOB, gender, blood group, phone, and emergency contact. `PATCH` each to `null` → all `None` in the DB. Web submit with those inputs empty → cleared. `PATCH {"full_name": null}` and `{"full_name":"  "}` return `422` with `fields.full_name == "errors.name_required"`, and the DB is unchanged. |
| **T6** `test_t6_basics_validation` | AC-06 | Parametrized over: DOB `2999-01-01` (`errors.date_in_future`), `1899-12-31` (`errors.date_too_early`), `"not-a-date"` (422); gender `"M"` (`errors.choice_invalid`); blood group `"o+"` and `"AB"` (`errors.choice_invalid`); phone `"03001234567"` (`errors.phone_invalid`); full_name 121 chars (`errors.text_too_long`); name with `"\x07"` (`errors.text_invalid`). For each: `422`, the exact key in `details.fields`, and the DB unchanged. Web: an invalid DOB re-renders with status `422`, the typed values preserved in the HTML, `aria-invalid="true"`, and `role="alert"`. **Legacy:** set `patient.gender="Male"` directly, then a web save that changes only the phone succeeds (`303`) and `gender` stays `"Male"`. |
| **T7** `test_t7_protected_fields_rejected` | AC-07 | `PATCH /api/v1/me/profile` with each of `{"email":...}`, `{"passport_no":...}`, `{"role":"admin"}`, `{"status":"suspended"}`, `{"patient_id":...}`, `{"cnic_hash":...}` returns `422`. The web form posting extra fields `role=admin&passport_no=X&email=a@b.c` returns `303` with the extras ignored. Afterwards, email, role, status, and passport are unchanged. Assert that the `FakeSupabaseAuth` recorded **no admin/update calls** (add a call counter to the fake if absent). |
| **T8** `test_t8_add_allergy` | AC-08 | `POST /api/v1/me/allergies {"substance":"  Penicillin ","severity":"severe","reaction":"Rash"}` returns `201`, the body has `substance == "Penicillin"` and `editable is True`, and the DB row has `recorded_by == user.id` and `removed_at is None`. Web `POST /en/patient/profile/allergies` returns `303` → `?saved=allergy.added#allergies`. |
| **T9** `test_t9_edit_allergy` | AC-09 | Own allergy. `PATCH {"severity":"moderate"}` returns `200`, only severity changes, and `updated_at` is set. `GET /en/patient/profile?edit=allergy&id=<id>` renders an edit form with `action=".../allergies/<id>"` prefilled. Web edit submit returns `303` → `?saved=allergy.updated#allergies`. An empty `PATCH {}` returns `200` with no DB change and no audit row. |
| **T10** `test_t10_remove_allergy_soft_and_idempotent` | AC-10 | `DELETE` returns `204`. The row **still exists** with `removed_at` set and is absent from `GET` and the page. A second `DELETE` returns `204` and the audit row count for `profile.allergy.remove` is still 1. `PATCH` on the removed id returns `404`. Web remove → `303` → `?saved=allergy.removed#allergies`. The page HTML contains `<details class="confirm">`. |
| **T11** `test_t11_conditions_crud` | AC-11 | Add → `201` with `status == "active"`. Edit name and onset → `200`. Remove → `204`, the row is kept with `status == "resolved"`, and `icd10_code` and `risk_level` are unchanged (seed values set directly before). Re-adding the same name after resolving is allowed (`201`). Web paths → correct `saved` keys and `#conditions`. |
| **T12** `test_t12_medications_crud` | AC-12 | Add with all fields → `201`, row in `patient_medication`. Edit strength → `200`. Remove → `204` with `removed_at` set and the row kept. Web paths → `medication.*` saved keys. The `patient_medication` table exists in `Base.metadata` with the columns in §7.1. |
| **T13** `test_t13_entry_validation` | AC-13 | Parametrized over: missing/blank substance or name (`errors.text_required`); 1-character name (`errors.text_required`); 121-character name (`errors.text_too_long`); reaction 256 characters; strength 51; notes 501 (`errors.text_too_long`); severity `"high"` (`errors.choice_invalid`); `onset_date` in the future (`errors.date_in_future`); `started_on` before 1900 (`errors.date_too_early`); with DOB 2000-01-01, `started_on` 1999-12-31 (`errors.date_before_birth`); `PATCH {"substance": null}` (`errors.text_required`); control character in notes (`errors.text_invalid`). Each case returns `422` with the exact key and creates or changes no row. |
| **T14** `test_t14_duplicates_rejected` | AC-14 | An active "Penicillin" exists. Add `"penicillin "` → `409 DUPLICATE_ENTRY`. Renaming another allergy to "PENICILLIN" → `409`. After removing the original, adding "Penicillin" → `201`. The duplicate check also matches a **non-editable** active entry with the same name. Web → `409` with the error shown under the `substance` field. The same checks hold for conditions (name) and medications (name) (parametrized). |
| **T15** `test_t15_list_cap` | AC-15 | Monkeypatch `settings.profile_max_entries_per_list = 3`. Add 3 → OK. The 4th → `422 LIST_FULL`, and the rendered message contains "3". Remove one, then adding succeeds. Web → the section banner shows `errors.list_full` with 3. |
| **T16** `test_t16_clinician_and_legacy_entries_read_only` | AC-16 | Insert an allergy with `recorded_by=None` (legacy) and one with `recorded_by=<doctor user id>`. `GET` shows both with `editable: false`. The page shows `profile.recorded_by_clinician` and **no** remove form or edit link for them. `PATCH` and `DELETE` on each → `403 NOT_EDITABLE`, and the rows are unchanged. `?edit=allergy&id=<legacy id>` → normal view (ignored). |
| **T17** `test_t17_ownership_isolation` | AC-17 | Patient B has an allergy. Patient A: `PATCH` and `DELETE` on B's id → `404`. Compare the JSON body with a request using a random `uuid4()`: equal after removing `request_id`. B's row is unchanged. `GET` by A never contains B's entries. Web: A posting to `/en/patient/profile/allergies/<B id>/remove` → `404` page with the `errors.not_found` banner. A malformed id → API `422`, web `404`. The same checks run for conditions and medications (parametrized). |
| **T18** `test_t18_audit_trail` | AC-18 | After a basics update, the `audit_log` row has `action="profile.update"`, `actor_role="patient"`, `subject_patient_id=patient.id`, `resource_type="patient"`, and `detail.fields_changed == ["blood_group"]`, with **no** `"before"`/`"after"` values. An allergy add, update, and remove each write one row, with `after`, `before`+`after`, and `before` holding the clinical fields. A medication update touching `notes` has `"notes"` in `fields_changed` but **not** in `before`/`after`. A rejected request (422/409/403/404) → no new audit rows. A no-op update → no row. |
| **T19** `test_t19_no_phi_in_logs` | AC-19 | Capture structlog output while running basics update, allergy add/edit/remove, condition add, and medication add with distinctive values (`"ZZ-SUBSTANCE-QX"`, `"+923009998877"`, `"ZZ-NOTES-QX"`, `"1990-01-01"`). Assert none of the values appear. Also unit-test `log_config._redact` on a dict containing each BL-28 key → `[redacted]`. |
| **T20** `test_t20_write_rate_limit` | AC-20 | Monkeypatch `settings.profile_write_rate_limit_per_minute = 3`. 3 `PATCH`es → OK. The 4th → `429 RATE_LIMITED` with a `Retry-After: 60` header. Web write → `429` page with the `errors.rate_limited` banner. `GET` is still `200`. A second patient is unaffected (per-user key). |
| **T21** `test_t21_urdu_rtl_and_switch` | AC-21 | `GET /ur/patient/profile` → `lang="ur"`, `dir="rtl"`, and it contains `translate("profile.title","ur")` and `translate("profile.section.allergies","ur")`. The language link on `/en/patient/profile` has `href="/ur/patient/profile"`. `missing_keys()["ur"] == []`. Every key in §5.8 exists in `en.json` (assert explicitly). |
| **T22** `test_t22_accessibility_design_and_no_js` | AC-22 | Page HTML: every `<input>`, `<select>`, and `<textarea>` has an `id` referenced by a `<label for>`; all ids are unique on the page (collect with a regex); forms use `method="post"` without JS; `/static/js/profile.js` is referenced with `defer`. `profile.js` contains `btn.disabled = true` and `aria-busy`. The existing CSS tests (`test_no_raw_hex_in_app_css`, `test_t18_no_physical_direction_css`, `test_t19_focus_ring_defined`) still pass with the new CSS. Assert `app.css` contains `.entry-row`, `.badge--err`, and `.btn--sm` inside the coarse-pointer rule. |
| **T23** `test_t23_output_is_escaped` | AC-23 | Add an allergy `substance="<script>alert(1)</script>"` and a medication `notes="<img src=x onerror=1>"`. The page HTML contains `&lt;script&gt;alert(1)&lt;/script&gt;` and **not** the raw `<script>alert(1)</script>`. The DB stores the raw string. The API returns the raw string (JSON, not HTML). |
| **T24** `test_t24_dashboard_links_to_profile` | AC-24 | `GET /en/patient` contains `href="/en/patient/profile"` and `translate("nav.profile","en")`. The profile page contains `href="/en/patient"` and `aria-current="page"` on the profile nav link. `GET /en/doctor` (verified doctor) and `/en/admin` contain **no** `/patient/profile` link. |
| **T25** `test_t25_migration_is_additive` | AC-25 | Static test: load the new revision module. `down_revision == "6feacefae2d0"`. The source of `upgrade()` (via `inspect.getsource`) contains no `drop_`, `alter_column`, or `rename`. It contains `create_table("patient_medication"`, `ENABLE ROW LEVEL SECURITY`, and a `dialect.name == "postgresql"` guard. `downgrade()` contains `drop_table("patient_medication")`. |
| **T26** `test_t26_regression` | AC-26 | Not a new test function. CI/local gate: `uv run pytest -q` passes all pre-existing tests plus the new ones; `uv run ruff check backend tests` and `uv run ruff format --check backend tests` are clean. |

### 11.3 Unit tests (`tests/test_profile_schemas.py`), supporting T6, T13, and T2
- `clean_text`: trims; collapses `"a   b"` → `"a b"`; `"  "` → `None`; rejects `"\x00"` and `"\x1b"`; keeps Urdu text (`"پینسلین"`) intact.
- `normalise_name`: `"  PeNiCiLLiN "` → `"penicillin"`.
- `compute_age`: `(1990-01-01, 2026-09-22)` → 36; `(1990-09-23, 2026-09-22)` → 35; `(2000-02-29, 2025-02-28)` → 24; `(2000-02-29, 2025-03-01)` → 25; `(None, …)` → None.
- `check_past_date`: boundary `1900-01-01` passes; `today_pk()` passes; `today_pk() + 1 day` fails.
- Each request model rejects an unknown field (`extra="forbid"`).

### 11.4 Manual verification (before opening the PR)
1. `uv run alembic upgrade head` on the dev Supabase project. Then confirm that `patient_medication` exists with RLS enabled and that `allergy`/`chronic_condition` have the new columns (`information_schema`).
2. `uv run backend/app/main.py`, sign in as the seeded patient (`seed_synthetic.py` output), and exercise every form in `/en` and `/ur`, with JavaScript enabled and **disabled**, at 360 px and desktop widths.
3. Confirm the two pre-existing synthetic allergy rows (`recorded_by IS NULL`) show as read-only "Recorded by a clinician".
4. Confirm that no profile value appears in the server log output.
