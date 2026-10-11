# SPEC — Privacy & Access Control (P9)

| | |
|---|---|
| **Feature** | Per-section consent grants, the consent gateway every cross-patient read goes through, and the patient's access log |
| **PRD requirement** | **FR4** (M, PAT): grant a named doctor or clinic access, view active grants, withdraw any grant at any time. **FR5** (M, INF): every access to a patient record is recorded with who, when and from which facility, and the patient can see the full list. Also touches **D2** (a doctor without a grant sees nothing, not even that the patient exists), **NFR16** (consent is the only basis for access; no admin override), **NFR17** (access logs cannot be edited or deleted), **NFR13** (Urdu/RTL), **NFR19** (synthetic data). |
| **TDD references** | §1.4 Rule 1 (one consent chokepoint), §3.4 (consent schema; **amended here**, see §6.0), §4.3 (`load_records_for_actor`, realised here as `allowed_sections`/`read_section`), §4.4 (`/me/consents`, `/me/access-log`), §7.2 (authorisation matrix), §7.3 (consent enforcement tests) |
| **Builds on** | `.claude/specs/medical_passport_spec.md` (P4: grant by PMDC/clinic name, list, revoke, doctor lookup), D4 doctor dashboard (`consent_service.list_patients_for_doctor`) |
| **Branch** | `feature/privacy-access` |
| **Status** | Ready for implementation |
| **Decisions already taken (do not revisit)** | (1) **Per-section sharing.** The patient picks which sections each grantee sees. This overrides TDD §3.4 and P4's "all-or-nothing" decision. (2) Grantees are a **verified doctor** or a **clinic**. A clinic grant covers every doctor with an *active* `doctor_affiliation` at that clinic. (3) Expiry choices are 24 hours, 7 days, 30 days, or until revoked. The default is 30 days. (4) **Reuse `consent_grant`** with an additive migration; no new consent table. (5) Section reads are logged in the shared **`access_log`**; grant/revoke/change events go to `audit_log`. (6) Changing sections **revokes the old grant and creates a new one** linked by `replaced_by`. Grants are never edited in place or deleted. |

> **Read before writing code.** The database is the **shared CuraNode-AI Supabase project**. `consent_grant` and `access_log` were verified against the live `information_schema` on **2026-10-07** (P9 step 0); their column lists are in §7.1. Where this spec and `docs/TDD.md` or `medical_passport_spec.md` disagree, **this spec wins**. Where it is silent, stop and ask.
>
> **Migration warning.** The live `alembic_version` is `35c4bc13fa37`, a revision that exists in **no branch of this repo** (repo head: `488dbf2b3915`). `alembic upgrade head` from this repo will fail against the shared DB until that is reconciled with the schema owner. P9's migration is written against the repo head and tested on SQLite only. Applying it needs the user's and the schema owner's explicit OK.

---

## 1. Feature Overview

### 1.1 What it is
P4 let a patient grant all-or-nothing access to a doctor (by PMDC number) or clinic (by name), list active grants and revoke them. A doctor holding a grant could look the patient up, but saw only a stub page. P9 turns that into real, fine-grained access control:

| Part | What the patient / doctor gets |
|---|---|
| **Per-section grants** | The patient chooses sections per grantee: Basics, Allergies, Chronic conditions, Medications, Prescriptions, Lab reports, Visits. They can change the sections later, set an expiry, and revoke. |
| **Grantee search** | The patient searches verified doctors by name or specialty and clinics by name, then picks one. PMDC/clinic-name entry stays supported on the API. |
| **Consent gateway** | One function decides what any actor may read about a patient (`consent/gateway.py`). Every section a doctor reads writes one `access_log` row in the same transaction. |
| **Doctor patient page** | `/{locale}/doctor/patient/{id}` now shows the shared sections. An unshared section reads "Not shared by patient", never as empty. |
| **Access log** | The patient sees who read which section, when, and through which clinic (FR5), plus past grants. |

Sections:

| Key | Label | Source | Content today |
|---|---|---|---|
| `basics` | Basics | `patient` | name, age, gender, blood group, emergency contact |
| `allergies` | Allergies | `allergy` (active: `removed_at IS NULL`) | substance, reaction, severity |
| `conditions` | Chronic conditions | `chronic_condition` (active: `status <> 'resolved'`) | name, since |
| `medications` | Medications | `patient_medication` (active: `removed_at IS NULL`) | name, strength, frequency, since |
| `prescriptions` | Prescriptions | — (P2) | always empty in this version |
| `lab_reports` | Lab reports | — (P2) | always empty in this version |
| `visits` | Visits and diagnoses | — (P3) | always empty in this version |

### 1.2 Why
FR4 and FR5 are Must-haves. P4 shipped FR4's grant/revoke mechanics but no access log, no gateway and no way to share less than everything. A patient who would share allergies with an emergency doctor but not their medication history had no option short of refusing access entirely. D2 and NFR16 also require a single place that decides access, so that P2/P3/D2/D3 reads plug into one checked door instead of each writing its own consent check.

### 1.3 Where it sits in the codebase
- `backend/app/consent/` (exists): `schemas.py`, `service.py`, `router.py` are extended; **new** `gateway.py` (the read path) and `access_log.py` (the patient's log view).
- `backend/app/web/router.py`: the existing `/{locale}/patient/access` page and `/{locale}/doctor/patient/{id}` page are extended.
- `backend/app/db/models.py`: `ConsentGrant` gains two columns and a unique partial index; **new** `AccessLog` maps the shared `access_log`.
- `alembic/versions/<rev>_consent_grant_sections_replaced_by.py`: hand-written, additive.

### 1.4 Key constraints inherited from the codebase (non-negotiable)
1. **BYPASSRLS.** The app connects as `postgres`, so the RLS policies on `consent_grant`/`access_log` do nothing for app queries. Every query is scoped in code: a patient's grants by their own `patient_id`, a doctor's reads by the gateway.
2. **Roles from the DB on every request** (`PatientDep`, `_doctor_page_guard`). No ad-hoc role checks outside `deps.py` and the existing web guards.
3. **D2.** Every doctor-side failure (unverified, no such patient, never granted, revoked, expired, nothing shared) is the same `NotFound`. Never `Forbidden`.
4. **Migrations additive and hand-written.** Never `--autogenerate`.
5. **Every string through `translate()`**, present in `en.json` and `ur.json`.
6. **CSS tokens and logical properties only.** Every control works without JavaScript.
7. **No PII in structlog output** (passport number, PMDC number, clinic name, section contents).

---

## 2. User Story

**Primary.** *As a patient, I want to choose exactly which parts of my record each doctor or clinic can see, for how long, and to see every time someone looked, so that sharing my record never means sharing all of it forever.*

**Supporting stories**
- *As a patient,* I want to find my doctor by name rather than needing their PMDC number.
- *As a patient,* I want to widen or narrow what a doctor sees without revoking and re-granting by hand.
- *As a patient,* I want access to end on its own after a visit (24 h / 7 d / 30 d).
- *As a verified doctor,* I want to see clearly which sections the patient chose not to share, so I never mistake "hidden" for "none".
- *As a doctor at a clinic the patient granted,* I want the same access as if the patient had named me.

---

## 3. Acceptance Criteria

| ID | Criterion |
|---|---|
| **AC-01** | **Grant with sections.** `POST /api/v1/me/consents` with a grantee, `sections` (≥ 1 valid key) and `duration` creates a grant with exactly those sections (de-duplicated, stored in canonical order) → `201`. |
| **AC-02** | **Sections required and validated.** An empty `sections`, an unknown section key, or a missing field → `422 VALIDATION_FAILED` naming `sections`. Nothing is written. |
| **AC-03** | **Duration.** `duration` ∈ `24h`/`7d`/`30d`/`until_revoked`, default `30d`; `expires_at` = `granted_at` + duration, or `NULL` for `until_revoked`. Any other value → `422`. |
| **AC-04** | **Grantee by id or by P4 identifier.** Exactly one of `doctor_id`, `clinic_id`, `pmdc_number`, `clinic_name`. Unknown or unverified doctor, unknown or inactive clinic → `404 NOT_FOUND` (unknown ≡ unverified, P4 BL-02). |
| **AC-05** | **Duplicate grant returns the existing one.** Granting to a grantee who already has an active grant → `200` with that grant, unchanged. No write, no audit row. |
| **AC-06** | **Lapsed grant does not block.** A grant that expired but was never revoked does not block a new grant; it is closed (`revoked_at` set, `replaced_by` → the new grant) in the same transaction. |
| **AC-07** | **Change sections.** `PUT /api/v1/me/consents/{id}/sections` on an active own grant revokes it (`revoked_at`, `revoked_by` = caller, `replaced_by` = new id) and creates a new grant for the same grantee and same `expires_at` → `200` with the new grant. Same sections as now → `200` with the current grant, no write. |
| **AC-08** | **Foreign ids are invisible.** Revoke or change on another patient's grant, a nonexistent id, or (for change) a revoked/expired own grant → `404`, body identical apart from `request_id`. |
| **AC-09** | **Revoke records who.** `DELETE /api/v1/me/consents/{id}` sets `revoked_at` and `revoked_by` = caller. Repeat → `204`, no second write (P4 BL-06). |
| **AC-10** | **List.** `GET /api/v1/me/consents` returns active grants newest first; `?include_revoked=true` also returns revoked and expired grants, after the active ones. Each item carries `sections`, `active`, `expires_at`, `revoked_at`, `replaced_by`, `grantee_name`, `grantee_detail`. |
| **AC-11** | **Grantee search.** `GET /api/v1/me/consents/grantees?q=` (2–80 chars) returns at most 10 verified doctors matching name or specialty and at most 10 active clinics matching name, case-insensitive. Never returns unverified doctors, PMDC numbers or user ids. `q` shorter than 2 chars → `422`. |
| **AC-12** | **Audit.** Grant, revoke and change each write exactly one `audit_log` row (`consent.grant`, `consent.revoke`, `consent.change`) with ids and section keys only. A no-op writes none. |
| **AC-13** | **Gateway, own record.** The patient themself is allowed every section. No `access_log` row is written for a patient reading their own record. |
| **AC-14** | **Gateway, doctor grant.** A verified doctor with an active doctor grant is allowed exactly that grant's sections. |
| **AC-15** | **Gateway, clinic grant.** A verified doctor with an active affiliation at a clinic holding an active clinic grant is allowed that grant's sections. Doctor and clinic grants combine as a union. An ended affiliation (`status <> 'active'`) gives nothing. |
| **AC-16** | **Gateway, denied ≡ missing.** Unverified doctor, clinic admin, another patient, revoked grant, expired grant, a grant with no sections, or a nonexistent patient → `ConsentDenied` (a `NotFound`). The doctor page renders the same 404 for all of them. |
| **AC-17** | **Unshared reads "Not shared".** On the doctor patient page, every section outside the allowed set is labelled "Not shared by patient", and its data is never queried. An allowed but empty section reads "Nothing recorded". |
| **AC-18** | **One log row per section read.** Each section a doctor reads writes one `access_log` row: `action='read'`, `user_id` = doctor's user id, `patient_id`, `consent_id` = the grant that allowed it, `resource_type='section.<key>'`, `resource_id` = patient id, `is_break_glass=false`, `ip_address`. |
| **AC-19** | **No read without a log row.** If the `access_log` write fails, the page returns an error and no section data; nothing is committed. |
| **AC-20** | **Purpose is mandatory.** `allowed_sections`/`read_sections` refuse an empty `purpose` with `ValueError`. |
| **AC-21** | **One door.** A test fails if any module under `backend/app/` other than `profile/service.py` and `consent/gateway.py` selects from `Allergy`, `ChronicCondition` or `PatientMedication`. |
| **AC-22** | **Access log API.** `GET /api/v1/me/access-log?cursor=&limit=` returns the caller's own `access_log` rows newest first (default 50, max 100), each with `occurred_at`, `section`, `accessor_name`, `accessor_specialty`, `clinic_name`, plus `next_cursor`. Rows by the patient's own user id and other patients' rows never appear. A malformed cursor → `422`. |
| **AC-23** | **Doctor-side clinic grants.** Passport lookup, the doctor patient page and the D4 dashboard list all honour clinic grants through an active affiliation, consistently with the gateway. |
| **AC-24** | **Web page.** `/{locale}/patient/access` shows "Who can see my passport" (cards with per-section checkboxes + Save, and Revoke with a confirm step), "Give access" (search → pick → sections, none ticked → duration, default 30 days → submit), and "Who looked" (access-log table, newest 50, plus past grants). Works without JS, in `en` and `ur` (RTL). Linked from the patient dashboard (existing link). |
| **AC-25** | **Access matrix.** Anonymous → API `401`, web redirect to login. Doctor/admin on any `/me/consents*` or `/me/access-log` endpoint → `403`. |
| **AC-26** | **Rate limits.** Grant/change/revoke share the existing consent-write budget (10/min). Grantee search has its own budget (30/min). Over → `429` with `Retry-After: 60`. |
| **AC-27** | **No PII in logs.** No PMDC number, clinic name, passport number, search query or section content appears in structlog output. |
| **AC-28** | **Catalogue complete.** Every new string exists in `en.json` and `ur.json`. |
| **AC-29** | **Migration.** The migration only adds (two nullable columns, one unique partial index, a data backfill of `[]` grants). `downgrade()` reverses it. Tested on SQLite; Postgres-only statements behind a dialect check. |
| **AC-30** | **Existing suites.** All pre-existing tests pass, updated only where this spec changes behaviour (§10.2). |

---

## 4. Functional Specifications

### 4.1 Resolving the actor's patient
Unchanged from P4: `profile.service._patient_for(session, actor)` returns the caller's `Patient` or raises `NotFound` (web → onboarding redirect).

### 4.2 Grant (`service.grant_consent`)
1. Validate the body (§6.3). `sections` is normalised to canonical order (§7.4).
2. Resolve the grantee: `doctor_id` → `Doctor` verified; `pmdc_number` → `Doctor` verified (P4 BL-02); `clinic_id` → `Clinic` with `status='active'`; `clinic_name` → exactly one clinic, case-insensitive (P4 BL-03). Any miss → `NotFound`.
3. Look up an unrevoked grant for (patient, grantee):
   - active → return it with `created=False` (router → `200`). No write.
   - expired but not revoked → mark it to be closed (step 5).
4. Insert the new grant (`scope_sections` = sections, `expires_at` from duration).
5. Close the lapsed grant: `revoked_at = now`, `revoked_by = NULL` (system, not the patient), `replaced_by = new.id`. The lapsed grant is revoked **before** the insert is flushed so the unique partial index is never violated.
6. `audit_log` `consent.grant`: `{resource_id, grantee_type, grantee_id, sections, expires_at}`. Commit.

### 4.3 Change sections (`service.change_sections`)
1. Load the grant by id **and** caller's `patient_id`, and require it to be active. Otherwise `NotFound` (AC-08).
2. Normalise `sections`. If equal to the stored list → return the current grant, no write.
3. Set `revoked_at = now`, `revoked_by = caller`; flush. Insert the new grant with the same grantee and `expires_at`; flush. Set old `replaced_by = new.id`.
4. `audit_log` `consent.change`: `{resource_id: new, replaces: old, grantee_type, grantee_id, sections_before, sections_after}`. Commit.

### 4.4 Revoke (`service.revoke_grant`)
P4 behaviour plus `revoked_by = caller`. Repeat revoke is a no-op.

### 4.5 List (`service.list_grants(include_revoked=False)`)
Active grants, newest first. With `include_revoked`, all of the patient's grants: active first (newest first), then the rest by `granted_at` desc. Names resolved in one query per grantee type (existing `_grantee_names`, extended to return specialty/city as `grantee_detail`).

### 4.6 Grantee search (`service.search_grantees`)
`q` trimmed, 2–80 chars. `LIKE '%q%'` with `%`, `_` and `\` escaped, case-insensitive (`func.lower`). Doctors: `verification_status='verified'` and (`full_name` or `specialty` matches), order by name, limit 10. Clinics: `status='active'` and `name` matches, order by name, limit 10. Returns `{type, id, name, detail}` (`detail` = specialty / city). Requires the caller to be a patient with a `patient` row (`_patient_for`), so the search is not an open directory for other roles.

### 4.7 Gateway (`consent/gateway.py`)
```python
@dataclass(frozen=True)
class Access:
    patient: Patient
    sections: frozenset[str]          # what may be read
    consent_for: dict[str, uuid.UUID] # section -> the grant that allows it (doctor grant preferred)
    is_owner: bool

async def allowed_sections(session, actor, patient_id, *, purpose) -> Access
async def read_section(session, actor, access, section, *, purpose) -> SectionData
async def read_sections(session, actor, patient_id, *, purpose) -> PassportView
```
`allowed_sections` rules, in order:
1. `purpose.strip()` empty → `ValueError`.
2. Patient missing → `ConsentDenied`.
3. `actor.role == 'patient'` and the patient's `user_id == actor.user_id` → all sections, `is_owner=True`.
4. `actor.role == 'doctor'`: load the `Doctor` row **live** (not `actor.is_verified_doctor`, so a revocation of verification is seen inside the same request). Not verified → `ConsentDenied`. Collect active grants where (`grantee_type='doctor'` and `grantee_id = doctor.id`) or (`grantee_type='clinic'` and `grantee_id` ∈ the doctor's **active** affiliations' clinic ids, read live). Union their sections. Empty union → `ConsentDenied`.
5. Anything else (clinic admin, other patient, staff) → `ConsentDenied`.

`read_section(section)`: `section not in access.sections` → `ConsentDenied`. Otherwise query that section's rows **filtered by `access.patient.id`**, then, unless `is_owner`, add one `AccessLog` row (AC-18) and `flush()`. A flush failure propagates, so the caller never gets data without a log row (AC-19).

`read_sections`: `allowed_sections`, then `read_section` for every allowed section in canonical order, then `commit()`. On any exception: `rollback()`, re-raise. Returns `PassportView(patient_id, sections: dict[key -> SectionData | None])`, where `None` means "not shared" (AC-17).

`ConsentDenied` is a subclass of `NotFound` with the same `code` and `http_status`, so the JSON envelope is byte-identical (D2) and existing `except NotFound` handlers keep working.

### 4.8 Doctor-side consistency (`service._granted_patient`, `list_patients_for_doctor`)
Both gain the clinic-via-affiliation branch (AC-23), sharing one helper with the gateway (`doctor_grant_filter(doctor_id, clinic_ids)` plus `live_doctor`). The D4 dashboard still lists a patient whose only active grant has an empty section list, which keeps it a pure "who granted me" list; opening that patient gives the D2 404 (AC-16, BL-16). Only legacy pre-migration rows can be empty, and the migration backfills them.

### 4.9 Access log (`consent/access_log.py`)
`list_access_log(session, actor, *, cursor, limit)`:
- `patient = _patient_for(...)`; rows `WHERE patient_id = patient.id AND (user_id IS NULL OR user_id <> actor.user_id)`, order `occurred_at DESC, log_id DESC`, `limit + 1` to compute `next_cursor`.
- Cursor = urlsafe base64 of `"<occurred_at ISO>|<log_id>"`; decoded and applied as a keyset `(occurred_at, log_id) < (c_at, c_id)`. Undecodable → `ValidationFailed({"fields": {"cursor": "errors.cursor_invalid"}})`.
- Accessor: `Doctor` by `user_id` (name, specialty); missing → name `None` (shown as "Account no longer available"). Clinic: when the row's `consent_id` grant is a clinic grant, that clinic's name; otherwise `None`.
- `section` = `resource_type` with the `section.` prefix removed (unknown prefixes shown raw).

### 4.10 Web: patient access page
`GET /{locale}/patient/access?q=&saved=` renders three parts (§5). Forms:

| Form | Method / action | Fields | Success |
|---|---|---|---|
| Search | `GET /{locale}/patient/access#grant` | `q` | page re-renders with results as radios |
| Give access | `POST /{locale}/patient/access` | `grantee` = `doctor:<uuid>` or `clinic:<uuid>`, `sections` (checkbox, repeated), `duration` | `303 ?saved=granted` (or `?saved=exists` when it already existed) |
| Save sections | `POST /{locale}/patient/access/{id}/sections` | `sections` (repeated) | `303 ?saved=changed` |
| Revoke | `POST /{locale}/patient/access/{id}/revoke` | — | `303 ?saved=revoked` |

`saved` is an allow-list (`granted`, `exists`, `changed`, `revoked`); anything else is ignored (P4 pattern).

### 4.11 Web: doctor patient page
`GET /{locale}/doctor/patient/{id}` calls `gateway.read_sections(purpose="doctor.patient_view")`. `ConsentDenied`/`NotFound` → the existing lookup page with `404` (unchanged D2 response). Other exceptions → the page re-raises (generic 500); no partial page.

### 4.12 Web error mapping (access page)
| Exception | Status | Shown |
|---|---|---|
| `ValidationError` / `ValidationFailed` on grant | 422 | field errors on `grantee`, `sections`, `duration` |
| `ValidationFailed` on save sections | 422 | page banner `errors.sections_required` |
| `NotFound` on grant | 404 | `grantee` field error `errors.grantee_not_found` |
| `NotFound` on change/revoke | 404 | page banner `errors.not_found` |
| `RateLimited` | 429 | page banner `errors.rate_limited` |
| anything else | 500 | page banner `errors.internal`, session rolled back |

---

## 5. UI/UX Requirements

### 5.1 `frontend/templates/patient/access.html` (rewritten, extends `base.html`)
Order on the page:
1. Banners (`page_error`, `saved`) and the lead paragraph (`access.subtitle`, reworded for sections).
2. **"Who can see my passport"** (`#active`): one card per active grant: grantee name, a type badge (Doctor/Clinic), detail (specialty / city), "Since {date}", "Until {date}" or "Until you revoke". A form with one checkbox per section (checked = shared) and **Save** (posts `/sections`). A `<details class="confirm">` **Revoke** with a confirm button. Empty state: `access.empty`.
3. **"Give access"** (`#grant`):
   - Search form (GET, `q`, submit "Search"). With `q` present: results as radio tiles `grantee=doctor:<id>` / `clinic:<id>`, showing name, type, detail. No results: `access.search.none`. Search error (too short): field error.
   - The grant form (POST) is shown only when there are results. It holds the radios, a fieldset of 7 section checkboxes **none pre-ticked**, a duration radio group (24 h, 7 days, **30 days checked**, until I revoke), and a **Give access** button. Re-rendered with the submitted values on error.
4. **"Who looked"** (`#log`): a table with columns When (PKT, `YYYY-MM-DD HH:MM`), Who (name + specialty), Clinic (or "—"), Section. Newest 50. Empty state `access.log.empty`. Under it, **"Past access"**: revoked/expired grants (name, type, sections, "Ended {date}", "Replaced by a newer grant" when `replaced_by` is set). Hidden when empty.

Section labels use `section.<key>` keys. Sections not yet backed by data (`prescriptions`, `lab_reports`, `visits`) carry a hint `access.section_hint.later` ("Appears once added").

### 5.2 `frontend/templates/doctor/patient.html` (rewritten)
Passport number (LTR) as now, then one panel per section in canonical order:
- Not shared: the panel heading plus a muted `doctor_patient.not_shared` ("Not shared by patient").
- Shared and empty: `doctor_patient.nothing_recorded`.
- Basics: a definition list (name, age, gender, blood group, emergency contact; missing → "—").
- Lists: one row per entry with the fields from §1.1.

### 5.3 CSS
Append to `frontend/static/css/app.css`, tokens and logical properties only: `.section-checks` (wrapping grid of checkbox labels), `.grant-card`, `.access-log` table styling (reuse existing table/`entry-row` patterns where they exist), `.muted-note`.

### 5.4 i18n keys (add to `en.json` and `ur.json`)
`section.basics`, `section.allergies`, `section.conditions`, `section.medications`, `section.prescriptions`, `section.lab_reports`, `section.visits`;
`access.section.who_can_see`, `access.section.log`, `access.section.past`, `access.until`, `access.until_revoked`, `access.ended_on`, `access.replaced`, `access.field.sections`, `access.field.duration`, `access.duration.24h`, `access.duration.7d`, `access.duration.30d`, `access.duration.until_revoked`, `access.search.label`, `access.search.action`, `access.search.none`, `access.search.pick`, `access.action.save_sections`, `access.saved.changed`, `access.saved.exists`, `access.section_hint.later`, `access.log.when`, `access.log.who`, `access.log.clinic`, `access.log.section`, `access.log.empty`;
`doctor_patient.not_shared`, `doctor_patient.nothing_recorded`, `doctor_patient.basics.*` (name, age, gender, blood_group, emergency_contact);
`errors.sections_required`, `errors.section_invalid`, `errors.duration_invalid`, `errors.grantee_not_found`, `errors.query_too_short`, `errors.cursor_invalid`.
Existing keys reworded: `access.title` → "Who can see my passport", `access.subtitle`, `doctor_patient.access_confirmed`. Obsolete after this change: `access.field.grantee*`, `access.grantee.*_hint`, `doctor_patient.record_pending`. Keep them in the catalogues (harmless) and stop rendering them.

---

## 6. API Contract

### 6.0 Deviations from TDD and P4, recorded
- **TDD §3.4** (all-or-nothing consent) is **amended**: grants carry `scope_sections`; `[]` means nothing is shared. Recorded in `docs/TDD.md` §3.4 as a dated amendment note.
- **P4 BL-04** (duplicate → `409`) becomes **return the existing grant, `200`** (AC-05).
- **P4 BL-12** ("until revoked" only) is superseded by AC-03.
- **TDD §4.3** `load_records_for_actor` is realised as `allowed_sections`/`read_section` over sections, not `clinical_records` (which does not exist yet).

### 6.1 Endpoints (all under `PatientDep`)
| Method | Path | Body / query | Success | Errors |
|---|---|---|---|---|
| `GET` | `/api/v1/me/consents/grantees` | `q` | `200 list[GranteeOut]` | 401, 403, 422, 429 |
| `GET` | `/api/v1/me/consents` | `include_revoked: bool = false` | `200 list[ConsentGrantOut]` | 401, 403, 404 |
| `POST` | `/api/v1/me/consents` | `GrantCreate` | `201` new / `200` existing, `ConsentGrantOut` | 401, 403, 404, 422, 429 |
| `PUT` | `/api/v1/me/consents/{grant_id}/sections` | `SectionsUpdate` | `200 ConsentGrantOut` | 401, 403, 404, 422, 429 |
| `DELETE` | `/api/v1/me/consents/{grant_id}` | — | `204` | 401, 403, 404, 422, 429 |
| `GET` | `/api/v1/me/access-log` | `cursor: str \| None`, `limit: int = 50 (1..100)` | `200 AccessLogPage` | 401, 403, 404, 422 |

`/me/consents/grantees` is declared **before** any `/me/consents/{grant_id}` route.

### 6.2 Errors
New: `ConsentDenied(NotFound)` in `errors.py` (same code `NOT_FOUND`, same status). No other new error classes.

### 6.3 Schemas (`consent/schemas.py`)
```python
SECTIONS: tuple[str, ...] = ("basics", "allergies", "conditions", "medications",
                             "prescriptions", "lab_reports", "visits")
Duration = Literal["24h", "7d", "30d", "until_revoked"]

class GrantCreate(BaseModel):            # extra="forbid"
    doctor_id: uuid.UUID | None = None
    clinic_id: uuid.UUID | None = None
    pmdc_number: str | None = None
    clinic_name: str | None = None
    sections: list[str]                  # required, >=1, each in SECTIONS -> canonical order
    duration: Duration = "30d"
    # exactly one grantee identifier, else errors.choice_invalid

class SectionsUpdate(BaseModel):         # extra="forbid"
    sections: list[str]                  # same validation as above

class ConsentGrantOut(BaseModel):
    id, patient_id, grantee_type, grantee_id
    grantee_name: str | None
    grantee_detail: str | None           # specialty / city
    scope_sections: list[str]            # kept for P4 compatibility
    sections: list[str]                  # same value; the name new clients use
    granted_at, expires_at, revoked_at
    replaced_by: uuid.UUID | None
    active: bool

class GranteeOut(BaseModel):
    type: Literal["doctor", "clinic"]; id: uuid.UUID; name: str; detail: str | None

class AccessLogEntry(BaseModel):
    occurred_at: datetime; section: str
    accessor_name: str | None; accessor_specialty: str | None; clinic_name: str | None

class AccessLogPage(BaseModel):
    items: list[AccessLogEntry]; next_cursor: str | None
```
Validation errors raise `PydanticCustomError(<type>, "errors.<key>")` (`sections_required`, `section_invalid`, `choice_invalid`), so `msg` is the i18n key.

### 6.4 Service signatures
```python
# consent/service.py
async def grant_consent(session, actor, *, doctor_id=None, clinic_id=None, pmdc_number=None,
                        clinic_name=None, sections, duration="30d", user_agent=None
                        ) -> tuple[ConsentGrantOut, bool]          # (grant, created)
async def change_sections(session, actor, grant_id, *, sections, user_agent=None) -> ConsentGrantOut
async def revoke_grant(session, actor, grant_id, *, user_agent=None) -> ConsentGrantOut
async def list_grants(session, actor, *, include_revoked=False) -> list[ConsentGrantOut]
async def search_grantees(session, actor, q: str) -> list[GranteeOut]
# consent/gateway.py: see §4.7
# consent/access_log.py
async def list_access_log(session, actor, *, cursor=None, limit=50) -> AccessLogPage
```

### 6.5 Rate limits (`deps.py`)
Existing `ConsentWriteRateLimit` on POST/PUT/DELETE. New `check_consent_search_rate` / `ConsentSearchRateLimit` (key `consent_search`, `settings.consent_search_rate_limit_per_minute = 30`), used by the API search route and the web search.

---

## 7. Data Requirements

### 7.1 Tables (verified live 2026-10-07)
**`consent_grant`** (shared): `consent_id uuid PK`, `patient_id uuid NOT NULL FK patient ON DELETE CASCADE`, `grantee_type varchar NOT NULL CHECK in ('doctor','clinic')`, `grantee_id uuid NOT NULL` (no FK), `scope_sections jsonb NOT NULL DEFAULT '[]'`, `granted_at timestamptz NOT NULL DEFAULT now()`, `expires_at timestamptz NULL`, `revoked_at timestamptz NULL`. Indexes: PK, `idx_consent_grant_patient_id`, `idx_consent_grant_grantee`, `idx_consent_grant_active_lookup` (non-unique partial). 3 rows, all active doctor grants with `[]`, no duplicates, none lapsed.

**`access_log`** (shared, 0 rows, unmapped until now): `log_id uuid PK DEFAULT gen_random_uuid()`, `user_id uuid NULL FK user_profile ON DELETE SET NULL`, `patient_id uuid NULL FK patient ON DELETE SET NULL`, `consent_id uuid NULL FK consent_grant` (no action), `resource_type varchar NOT NULL`, `resource_id uuid NOT NULL`, `action varchar NOT NULL CHECK in ('read','write','export')`, `is_break_glass bool NOT NULL DEFAULT false`, `ip_address inet NULL`, `occurred_at timestamptz NOT NULL DEFAULT now()`. Indexes on `user_id`, `patient_id`, `consent_id`, `occurred_at`.

**`audit_log`** (repo-owned): unchanged.

### 7.2 ORM models
- `ConsentGrant` adds `revoked_by: Uuid FK user_profile.user_id ON DELETE SET NULL, nullable` and `replaced_by: Uuid FK consent_grant.consent_id, nullable`; `__table_args__` adds `Index("uq_consent_grant_one_active", "patient_id", "grantee_type", "grantee_id", unique=True, postgresql_where=revoked_at IS NULL, sqlite_where=revoked_at IS NULL)`. The docstring is updated (no longer all-or-nothing; uniqueness is now DB-enforced).
- New `AccessLog` (`__tablename__ = "access_log"`) mapping §7.1 exactly. `id` ↔ `log_id`, default `uuid7`. `ip_address` is `String(64).with_variant(INET, "postgresql")`. `action` and `resource_type` are `String(40)`. The model carries a `CheckConstraint` on `action` so SQLite tests enforce it too. Docstring: shared, append-only, never updated or deleted by the app (NFR17).
- `AuditLog`'s docstring sentence about `access_log` not fitting is updated: auth events stay in `audit_log`; record reads go to `access_log`.

### 7.3 Migration (hand-written, additive; `down_revision = "488dbf2b3915"`)
```
upgrade():
  add_column consent_grant.revoked_by  uuid NULL FK user_profile(user_id) ON DELETE SET NULL
  add_column consent_grant.replaced_by uuid NULL FK consent_grant(consent_id)
  backfill: UPDATE consent_grant SET scope_sections = <all 7 keys>
            WHERE scope_sections = '[]'   (Postgres: jsonb compare; SQLite: text compare)
  create_index uq_consent_grant_one_active UNIQUE (patient_id, grantee_type, grantee_id)
               WHERE revoked_at IS NULL
downgrade():
  drop_index uq_consent_grant_one_active; drop_column replaced_by; drop_column revoked_by
  (the backfill is not reversed: [] would turn real grants into "nothing shared")
```
Column adds use `batch_alter_table` so SQLite can run it. No `access_log` DDL: the table exists and needs no change. **Do not run against the shared DB** without the OKs noted at the top.

### 7.4 Derived data
- Canonical section order = `SECTIONS`. Stored lists are de-duplicated and sorted by that order, so "same sections" is a plain list comparison.
- `active` = `revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now)`.
- `expires_at` = `granted_at + {24h: 1 day, 7d: 7 days, 30d: 30 days}`.
- Age on the doctor page = `doctor.rows.age_in_years(dob, today_pk())`.

### 7.5 Settings
`consent_search_rate_limit_per_minute: int = 30` (+ `.env.example`).

### 7.6 Data classification
Section contents are clinical PHI and are never logged. `access_log` holds ids only. `audit_log.detail` holds ids, section keys and an ISO `expires_at`; never names, PMDC numbers or search text.

---

## 8. Business Logic (numbered rules)

**Grants**
- **BL-01** A grant names exactly one grantee: a **verified** doctor or an **active** clinic. Unknown and unverified are indistinguishable (`NotFound`).
- **BL-02** A new grant lists **at least one** section, every key from `SECTIONS`. Stored de-duplicated in canonical order.
- **BL-03** `scope_sections = []` means **nothing shared**. It is never written by P9; it exists only on legacy rows and is backfilled by the migration.
- **BL-04** At most **one unrevoked grant** per (patient, grantee type, grantee id), enforced by the unique partial index **and** checked in the service.
- **BL-05** Granting to a grantee with an active grant returns that grant unchanged (`200`, no write, no audit).
- **BL-06** A lapsed grant (expired, not revoked) is closed when a new grant for the same grantee is created: `revoked_at = now`, `revoked_by = NULL`, `replaced_by = new`.
- **BL-07** Duration ∈ {24h, 7d, 30d, until revoked}; default 30d.
- **BL-08** Changing sections never edits a grant: revoke (`revoked_by` = patient) + insert (same grantee, same `expires_at`) + link via `replaced_by`. Only an **active** grant can be changed.
- **BL-09** Grants are never deleted. Revoke sets `revoked_at` and `revoked_by`; a repeat is a no-op.
- **BL-10** Every grant/change/revoke lookup is filtered by the caller's own `patient_id`. Foreign, missing and (for change) inactive ids are the same `NotFound`.
- **BL-11** Each successful grant/change/revoke writes exactly one `audit_log` row in the same transaction; no-ops write none.

**Gateway**
- **BL-12** All reads of another person's profile sections go through `consent/gateway.py`. `profile/service.py` reads only the caller's own rows.
- **BL-13** The owner sees every section; no access-log row is written for the owner.
- **BL-14** A verified doctor is allowed the union of sections over active grants naming the doctor, or naming a clinic where the doctor has an **active** affiliation. Verification and affiliations are read live per call.
- **BL-15** Clinic admins, staff, other patients and unverified doctors are never allowed anything (NFR16: no admin override).
- **BL-16** Denied, revoked, expired, empty-section, unverified and nonexistent all raise `ConsentDenied` (a `NotFound`) → identical 404.
- **BL-17** A section outside the allowed set is never queried, and is shown as "Not shared by patient", never as empty.
- **BL-18** Every non-owner section read writes one `access_log` row before data is returned, in the same transaction; failure → no data, rollback.
- **BL-19** `consent_id` on the log row is the doctor grant when one allows the section, else the clinic grant that does.
- **BL-20** `purpose` is mandatory and non-blank. It is not stored (no column exists); it is part of the call contract for every future caller.

**Access log**
- **BL-21** The patient sees only rows whose `patient_id` is their own and whose `user_id` is not their own, newest first.
- **BL-22** The app never updates or deletes `access_log` rows (NFR17). `is_break_glass` is always `false` (no emergency override in P9).
- **BL-23** The clinic shown is the clinic of the grant that allowed the read; for a doctor grant, none.

**Web**
- **BL-24** No section is pre-ticked when giving access; 30 days is pre-selected.
- **BL-25** `saved`, `q` and form values are never reflected unescaped; `saved` is an allow-list.

---

## 9. Dependencies

### 9.1 Internal code (read before changing)
`consent/service.py`, `consent/schemas.py`, `consent/router.py`, `profile/service.py::_patient_for`, `profile/schemas.py::_err`, `deps.py` (rate limits), `errors.py`, `audit/writer.py`, `web/router.py` (`_render_access`, `_access_failure`, `doctor_patient_page`, `_guarded`), `doctor/rows.py::age_in_years`, `db/models.py`, `tests/conftest.py`, `tests/test_consent.py`, `tests/test_passport_web.py`, `tests/test_doctor_dashboard.py`.

### 9.2 Database objects relied on
`consent_grant`, `access_log`, `patient`, `doctor`, `clinic`, `doctor_affiliation`, `allergy`, `chronic_condition`, `patient_medication`, `audit_log`, `user_profile`.

### 9.3 Packages
None new.

### 9.4 Prerequisites
P4 (consent module, passport), P1/FR2 (profile tables), D4 (doctor dashboard). Applying the migration to the shared DB is blocked on reconciling the unknown live revision `35c4bc13fa37` and on the schema owner's OK.

### 9.5 Downstream
D2 (doctor opens a patient), D3 ("what changed"), P2/P3 doctor-side reads: each calls `gateway.allowed_sections`/`read_section` and adds its own section reader, instead of writing its own consent check.

---

## 10. Out of Scope

### 10.1 Not built
1. Sharing single records (one lab report but not another).
2. Caregiver/delegated access (FR7).
3. Break-glass/emergency access (`is_break_glass` stays false).
4. Data for `prescriptions`, `lab_reports`, `visits` (P2/P3); the sections exist and read as empty.
5. A patient-chosen custom expiry date; only the four durations.
6. Notifications to a grantee on grant/revoke (FR20).
7. Storing `purpose` (no column in the shared `access_log`).
8. A clinic-admin view of grants; admins get nothing (NFR16).
9. Applying the migration to the shared Supabase DB.

### 10.2 Behaviour changes to existing tests
- P4 `test_consent.py`: grants now need `sections`; duplicate → `200` existing (was `409`); `expires_at` defaults to 30 days (was `NULL`); `scope_sections` now holds the sections.
- `test_passport_web.py`: the grant form is now search → pick → sections → duration; the doctor patient page shows sections instead of "arrives later".
- Updated, not deleted: each changed assertion keeps testing the same rule under its new shape.

---

## 11. Testing Requirements

### 11.1 Fixtures and helpers
Reuse `make_user`, `clinic`, `login`. New in `tests/test_privacy_access.py`: `verified_doctor`, `clinic_doctor` (verified, affiliated with `clinic`), `ended_affiliation_doctor`, `patient_user`, `second_patient`, `grant_row(..., sections=..., expires_at=..., revoked_at=...)`, `add_allergy/condition/medication` rows, `access_rows(db)`.

### 11.2 Test cases (`tests/test_privacy_access.py`, one or more `test_acNN_*` per AC)
| Test | Covers |
|---|---|
| `test_ac01_grant_with_sections_canonical_order` | AC-01, BL-02 |
| `test_ac02_sections_required_and_validated` (param: `[]`, `["x"]`, missing) | AC-02 |
| `test_ac03_duration_sets_expiry` (param ×4) + `_invalid_duration` | AC-03 |
| `test_ac04_grant_by_doctor_id_clinic_id_pmdc_name` + `_unknown_and_unverified_identical_404` + `_exactly_one_identifier` | AC-04 |
| `test_ac05_duplicate_returns_existing_without_writes` | AC-05 |
| `test_ac06_lapsed_grant_closed_and_linked` | AC-06, BL-06 |
| `test_ac07_change_sections_revokes_and_links` + `_same_sections_is_noop` | AC-07 |
| `test_ac08_foreign_missing_inactive_identical_404` | AC-08 |
| `test_ac09_revoke_sets_revoked_by_and_is_idempotent` | AC-09 |
| `test_ac10_list_active_and_include_revoked` | AC-10 |
| `test_ac11_search_verified_doctors_and_clinics_only` + `_short_query_422` + `_wildcards_escaped` | AC-11 |
| `test_ac12_one_audit_row_per_change_no_names` | AC-12 |
| `test_ac13_owner_gets_all_sections_and_no_log_rows` | AC-13 |
| `test_ac14_doctor_grant_sections_only` | AC-14 |
| `test_ac15_clinic_grant_via_active_affiliation_and_union` + `_ended_affiliation_denied` | AC-15 |
| `test_ac16_denied_cases_raise_consent_denied` (param: unverified, admin, other patient, revoked, expired, empty, missing) | AC-16 |
| `test_ac17_doctor_page_shows_not_shared_and_nothing_recorded` | AC-17 |
| `test_ac18_one_access_log_row_per_section_read` | AC-18, BL-19 |
| `test_ac19_log_failure_returns_no_data` | AC-19 |
| `test_ac20_purpose_required` | AC-20 |
| `test_ac21_no_direct_profile_table_reads_outside_gateway` (AST scan) | AC-21 |
| `test_ac22_access_log_own_rows_newest_first_paged` + `_bad_cursor_422` + `_excludes_own_reads` | AC-22 |
| `test_ac23_lookup_dashboard_and_page_honour_clinic_grants` | AC-23 |
| `test_ac24_access_page_*` (renders 3 parts, search → grant, save sections, revoke, Urdu RTL, no section pre-ticked, 30d default) | AC-24 |
| `test_ac25_access_matrix` | AC-25 |
| `test_ac26_search_and_write_rate_limits` | AC-26 |
| `test_ac27_no_pii_in_logs` | AC-27 |
| `test_ac28_catalogue_complete` | AC-28 |
| `test_ac29_migration_is_additive` (static check of the migration source) | AC-29 |

### 11.3 Gate
`uv run pytest -q` (244 existing + new), `uv run ruff check backend tests`, `uv run ruff format --check backend tests`.

### 11.4 Manual verification (before a PR)
1. `uv run backend/app/main.py`, sign in as a seeded patient: search a doctor, grant two sections for 24 h, change to three, revoke. Check `/ur` renders RTL.
2. Sign in as the doctor: the patient page shows the shared sections and "Not shared" for the rest. As the patient, "Who looked" lists the reads.
3. Applying the migration to the shared DB: only after the OKs in the header, and after reconciling revision `35c4bc13fa37`.
