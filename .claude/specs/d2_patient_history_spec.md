# SPEC — D2 Patient History Access (consent gateway)

| | |
|---|---|
| **Feature** | A verified doctor reading a consenting patient's clinical record (demographics, allergies, conditions, medications, encounters, prescriptions, lab reports) through a single consent gateway |
| **PRD requirement** | **D2** (a doctor without a grant sees nothing — not the record, not the fact it exists), **NFR16** (consent is the only basis for access), **NFR17** (audit rows cannot be edited or deleted), **FR21** (a doctor consuming a granted record) |
| **Builds on** | `.claude/specs/medical_passport_spec.md` (grants, BL-07/BL-09/BL-10 live-checked access and identical `NotFound`) |
| **Branch** | `feature/D2-patient-history-access` |
| **Status** | Draft — **§1 (summary), §3 (acceptance criteria), §8 (business rules) and §9 (out of scope)**, describing what is built in `consent/gateway.py` and `clinical/schemas.py`. The page/route layer is **not yet built**; ACs for it are marked *(page — not yet built)*. Where this spec is silent, stop and ask; do not invent a column, endpoint, or behaviour. |
| **Decisions already taken (do not revisit)** | (1) Live Supabase clinical tables are mapped directly (Branch A) — no migration, no writes. (2) Doctor-only grants count in this version. (3) "What Changed" uses the prior `record.read` audit row as its anchor. |

> **Read before writing code.** The clinical tables (`encounter`, `diagnosis`, `vital_sign`, `prescription`, `prescription_item`, `medicine`, `lab_report`, `lab_result`) belong to the wider CuraNode-AI schema and are **read-only** here; their models are in `db/models.py`. The app connects as `postgres` with `BYPASSRLS`, so RLS does not protect these queries — scoping is application code.

---

## 1. Feature Overview

### 1.1 What it is
`consent/gateway.py` exposes one function:

```
load_patient_for_doctor(session, actor, patient_id, *, purpose="patient_record_view", user_agent=None) -> PatientRecordOut
```

It checks the doctor's grant live (via `consent.service.patient_and_grant_for_doctor`, which reuses the existing grant rules), loads the record, writes one `record.read` audit row, and commits both together.

### 1.2 What is returned (`clinical/schemas.py` → `PatientRecordOut`)
| Field | Content |
|---|---|
| `patient` | id, full name, date of birth, gender, blood group (no passport number, no emergency contact) |
| `allergies` | substance, reaction, severity, recorded_at, recorded_by |
| `conditions` | name, ICD-10, onset date, status, risk level, recorded_by |
| `medications` | name, strength, frequency, start date, recorded_by |
| `encounters` | visit time, doctor, clinic, chief complaint, clinical notes, AI summary, `what_changed_diff`; each with `diagnoses`, `vitals`, and `prescriptions` (each with `items`, each item with its `medicine`) |
| `lab_reports` | encounter link, document id, lab name, report date, status; each with `results` (test, LOINC, value, unit, reference range, flag) |

### 1.3 Where it sits
`consent/gateway.py` is the **only** module allowed to query the doctor-owned clinical tables. `tests/test_consent.py::test_no_direct_clinical_queries` fails the build if `select(Encounter | Diagnosis | Prescription | LabReport | LabResult)` appears in any other file under `backend/`.

### 1.4 Constraints inherited from the codebase
1. Role and verification come from the database on every call, never from the token.
2. Every query filters by `patient_id` in application code.
3. Denials are `NotFound`; `Forbidden` is never used for a consent failure.
4. Nothing logs or audits a passport number, PMDC number or clinic name; `detail` holds ids and `purpose` only.

---

## 3. Acceptance Criteria

| ID | Criterion | Test |
|---|---|---|
| **AC-01** | **Authorised read.** A verified doctor with an active grant naming them gets a `PatientRecordOut` for that patient, containing the sections in §1.2. | `test_gateway_returns_record_and_one_audit_row` |
| **AC-02** | **Encounter order.** Encounters are returned newest `visit_datetime` first. | same |
| **AC-03** | **One audit row.** A successful read writes exactly one `record.read` row whose `detail` keys are exactly `patient_id`, `doctor_id`, `grant_id`, `purpose`, and whose `doctor_id` is the granted doctor's id. | same |
| **AC-04** | **Denial is `NotFound`.** Unknown patient id, patient with no grant for this doctor, revoked or expired grant, unverified doctor, and non-doctor all raise the same `NotFound`. | `test_gateway_denial_is_not_found_and_writes_nothing` |
| **AC-05** | **Denial writes nothing.** A denied read writes no `record.read` row. | same |
| **AC-06** | **Audit failure rolls back.** If the audit write raises, the exception propagates, nothing is committed, and no record is returned. | `test_gateway_audit_failure_rolls_back` |
| **AC-07** | **Gateway is the only reader.** No file under `backend/` other than `consent/gateway.py` selects from the guarded clinical models. | `test_no_direct_clinical_queries` |
| **AC-08** | **Excluded entries.** Removed allergies (`removed_at` set), removed medications, and resolved chronic conditions are absent from the result. | *(test to be added)* |
| **AC-09** | **Lab report order.** Lab reports are returned newest `report_date` first, each with its results. | *(test to be added)* |
| **AC-10** | **Patient scoping.** Another patient's encounters, prescriptions, lab reports and entries never appear in the result. | *(test to be added)* |
| **AC-11** | **Revocation is immediate.** The first read after a revoke raises `NotFound`; nothing is cached. | *(test to be added)* |
| **AC-12** | **Doctor page.** *(page — not yet built)* The record is shown on `/{locale}/doctor/patient/{patient_id}`; a denial renders the same not-found page as an unissued id, never a 403. |
| **AC-13** | **No sensitive values in logs or audit.** *(page — not yet built)* No passport number, PMDC number or clinic name appears in logs, URLs or audit detail. |
| **AC-14** | **Translations.** *(page — not yet built)* Every new user-facing string exists in `en.json` and `ur.json`. |

---

## 8. Business Logic (numbered rules)

| ID | Rule |
|---|---|
| **BL-01** | Only a **verified doctor** holding an **active grant naming that doctor** may read a patient's record. Active = not revoked and not expired, checked against the database on every call (consent BL-07). |
| **BL-02** | Every read goes through `consent/gateway.py`. No other module queries the clinical tables (enforced by AC-07). |
| **BL-03** | Every successful read writes **exactly one** `record.read` audit row (`actor_role="doctor"`, `resource_type="patient_record"`, `subject_patient_id` set) in the same transaction as the read. |
| **BL-04** | The audit `detail` is `{patient_id, doctor_id, grant_id, purpose}` — ids and purpose only; never names, PMDC numbers, passport numbers or clinical content. |
| **BL-05** | If the audit write fails, the whole read is rolled back and the error propagates. A record is never returned without its audit row. |
| **BL-06** | Every denial — not a verified doctor, no such patient, never granted, revoked, expired — raises the identical `NotFound`, never `Forbidden`/403 and never a distinct consent error. A denial writes no audit row. |
| **BL-07** | Only **doctor grants** (`grantee_type='doctor'`, `grantee_id` = the doctor's id) count in this version. Clinic grants do not unlock anything. |
| **BL-08** | Every query filters by `patient_id` in application code (directly, or through the patient's own encounter/report ids). RLS is never relied on. |
| **BL-09** | `purpose` is a keyword argument defaulting to `patient_record_view`. |
| **BL-10** | Resolved chronic conditions (`status='resolved'`) and removed allergies/medications (`removed_at` set) are excluded. |
| **BL-11** | Encounters are ordered newest `visit_datetime` first; lab reports newest `report_date` first. Prescriptions within an encounter are newest `issue_date`, then `created_at`, first; vitals newest `recorded_at` first. |
| **BL-12** | The read is **read-only**: nothing in this feature inserts, updates or deletes clinical rows. |
| **BL-13** | The reads run sequentially on one `AsyncSession` (a session does not support concurrent operations), so the read and the audit write share one transaction. |
| **BL-14** | If two active grants name the same doctor and patient, the most recently granted one is the `grant_id` recorded. |

---

## 9. Out of Scope

- **Clinic grants unlocking affiliated doctors.** What a clinic grant allows for that clinic's doctors is not specified (BL-07).
- **LLM narrative for "What Changed".** `encounter.what_changed_diff` and `ai_summary` are returned as stored; nothing generates or edits them. The diff anchor (the prior `record.read` audit row) is decided but not implemented.
- **Writing encounters, diagnoses, vitals, prescriptions or lab data** from this feature, and patient-facing access to those tables.
- **The doctor page and route** for the record (AC-12 to AC-14).
- **Partial scoping** by `scope_sections`, and timed expiry UI — grants stay all-or-nothing and "until revoked".
- **The patient's view of who read their record** (FR5).
