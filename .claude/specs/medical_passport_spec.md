# SPEC — Medical Passport

| | |
|---|---|
| **Feature** | Medical Passport (permanent passport identity, plus granting, viewing, and revoking doctor/clinic access to it) |
| **PRD requirement** | **FR1** (M, PAT) — one Medical Passport, issued once, that stays with the patient permanently. **FR4** (M, PAT) — grant a named doctor or clinic access, view all active grants, withdraw any grant at any time. Also touches **D2** (a doctor without a grant sees nothing — not the record, not the fact it exists), **NFR16** (consent is the only basis for access; no administrative override), **NFR17** (access/audit logs cannot be edited or deleted). **FR5** (the patient's access-log view) and **FR21** (a doctor consuming a granted record) are related but out of scope here — see §10. |
| **TDD references** | §3.4 (`consent_grants` schema — written before the shared-schema reconciliation, **not yet verified** against the live `information_schema`, unlike `patient`/`allergy`/`chronic_condition`, which were verified 2026-09-22 for FR2 — see `.claude/specs/patient_profile_spec.md`), §4.3 (consent endpoints), §4.4 (Medical Passport endpoints), §7.2 (authorisation / D2 enforcement), §8.2/§8.3 (error envelope) |
| **Branch** | `feature/P4-medical-passport` |
| **Status** | Draft — **this document specifies §1 (summary), §3 (acceptance criteria), and §8 (business rules) only**, at the requester's scope. Functional specifications, UI/UX, the exact API contract, and data requirements (the sections numbered 4–7, 9–11 in `patient_profile_spec.md`'s structure) are not yet written and must be completed — including live-schema verification of `consent_grants` — before implementation begins. |
| **Decisions already taken (do not revisit)** | (1) Grants are all-or-nothing over the patient's record — no partial scoping (share prescriptions but not lab reports) is offered **in this version's business rules or UI**, even though the live schema's `scope_sections` column exists (see note below) — this mirrors how `expires_at` is provisioned ahead of a timed-expiry UI (see (3)). (2) The QR code encodes `passport_no` alone, never any other patient data. (3) Grant expiry is a stored, nullable column, but this version's UI offers only "until revoked" — no expiry picker. |

> **Read before writing code.** The database is the **shared CuraNode-AI Supabase project**, not the TDD §3.4 schema. The live table is named **`consent_grant`** (singular) — TDD §3.4's `consent_grants` was unverified and is wrong. It **has now been verified** against the live `information_schema` (2026-10-01), the way `patient`/`allergy`/`chronic_condition` were for FR2 — see `backend/app/db/models.py`'s `ConsentGrant` docstring for the verified column list, including `scope_sections` (`jsonb`, non-nullable, default `[]`) and `grantee_id` (polymorphic, no FK). One more discrepancy from TDD §3.4 found during verification: the live `idx_consent_grant_active_lookup` index is a **plain, non-unique** partial index, not the unique `uq_consent_one_active` constraint TDD §3.4 assumed — BL-04 ("at most one active grant") must be enforced in application code, not relied on as a DB-level guarantee. Where this spec and `docs/TDD.md` disagree, the live schema wins. Where this spec is silent — including anything in §4–7/§9–11 that hasn't been written yet — stop and ask; do not invent a column, endpoint, or behaviour.

---

## 1. Feature Overview

### 1.1 What it is
The Medical Passport is the patient's single, portable digital health identity (FR1): a `passport_no` issued exactly once, at the moment a `patient` row is created, that never changes and follows the patient across every participating clinic. This feature is the **consent layer** that sits in front of that identity — it does not expose the clinical record itself (that's FR21/FR8, out of scope here). Concretely, it gives a patient:
- A passport page showing their `passport_no` and a QR code that encodes it, so it can be shown or scanned at a clinic.
- The ability to grant access to a specific **verified doctor**, identified by PMDC number, or to a specific **clinic**, identified by name.
- A list of their own currently active grants.
- The ability to revoke any grant, at any time, with the revocation taking effect on the very next request — no propagation delay, no cache.

On the other side of the same mechanism, a verified doctor who is not an active grantee for a patient must be unable to tell the patient's record — or the patient's existence — apart from a passport number that was never issued (D2).

### 1.2 Why
PRD FR1: *"A patient can create an account and is issued one Medical Passport that stays with them permanently, regardless of which clinics they visit."* FR4: *"A patient can grant a named doctor or clinic access to their Medical Passport, view all currently active grants, and withdraw any grant at any time."* Both are Must-have and both are foundational: FR21 (a doctor viewing a consented patient's history) and every later clinical feature depend on a consent grant existing and being checked on every read.

Decision **D2** (Appendix A) is the reason this feature is stricter than an ordinary permission check: *"Records the patient has not shared are entirely invisible. A doctor without an access grant sees nothing — not the record, and not the fact that a record exists."* The PRD's own rationale is that a distinguishable "access denied" response would leak the existence of sensitive care (mental health, reproductive health, HIV) to a doctor the patient deliberately did not share it with — which is exactly what NFR16 ("patient consent is the only basis on which a doctor or clinic may view a record — there is no administrative override") is meant to prevent.

### 1.3 Where it sits in the codebase (tentative — to be finalised in the functional spec)
By analogy with `backend/app/profile/` (FR2), this feature will most likely live in its own package (e.g. `backend/app/consent/` or `backend/app/passport/`: `schemas.py`, `service.py`, `router.py`), with the JSON API and any server-rendered page calling the same service functions so no rule is implemented twice. The exact package name and route paths are a functional-spec decision, not fixed here.

### 1.4 Key constraints inherited from the codebase (non-negotiable)
1. **Role and verification come from the DB on every request.** Consent-management endpoints (grant/list/revoke) are patient-only — use `PatientDep`. A doctor's *consumption* side of this feature (which this document does not specify) would use `VerifiedDoctorDep`; an unverified doctor must never be grantable in the first place (BL-02).
2. **Migrations are additive and hand-written**, never `--autogenerate` (CLAUDE.md).
3. **The app connects to Postgres as `postgres` with `BYPASSRLS`.** Every grant lookup must filter by the caller's own `patient_id` (or, on the doctor side, by the caller's own doctor/clinic identity) in application code — RLS on the shared schema does not protect this app's queries.
4. **Every user-facing string goes through `translate()`** and exists in both `en.json` and `ur.json`.
5. **CSS uses tokens only, logical properties only, no raw hex**, if/when a passport page is built.
6. **Errors are `AppError` subclasses.** D2 is already anticipated in the codebase: `errors.py`'s `Forbidden` docstring states *"Never used for a consent failure — that is 404 and belongs to the consent gateway"* — meaning the existing `NotFound` class is reused for D2, not a new error type (BL-09, BL-10).

---

## 2. User Story

**Primary.** *As a registered patient, I want to see my passport number and its QR code, and control exactly which doctors and clinics can see my medical record, so that I decide who has access and can take it away instantly if I no longer trust them.*

**Supporting stories**
- *As a patient,* I want a revoked grant to work immediately — not after a delay, not after the doctor's session expires — so that "revoke" reliably means revoke (BL-07).
- *As a patient,* I don't want a doctor I never granted access to be able to tell, in any way, that I even exist as a patient with a record — including through a distinguishable error message (D2, BL-09).
- *As a verified doctor,* when I look up a patient I have no grant for, I expect the same response whether or not that patient exists, so that I gain no information from a denied lookup (D2, BL-10).

**Not the actor here:** an unverified doctor (who cannot reach this feature at all, per FR3), and anything that consumes a granted record's contents (FR21) — that is a separate feature this spec does not cover.

---

## 3. Acceptance Criteria

Each AC will need a matching test `Tn` once §11 (Testing Requirements) is written, following the same `test_tN_<description>` convention as `patient_profile_spec.md`.

| ID | Criterion |
|---|---|
| **AC-01** | **View passport page.** An active patient who opens the passport page gets `200` with their `passport_no` displayed and a QR code that encodes it rendered on the page. |
| **AC-02** | **Grant access to a doctor.** A patient can grant access to a doctor by entering that doctor's PMDC number. The grant succeeds only when the PMDC number belongs to an existing, **verified** doctor (BL-02); a PMDC number that doesn't exist, or belongs to an unverified doctor, is rejected. |
| **AC-03** | **Grant access to a clinic.** A patient can grant access to a clinic by entering the clinic's name. The grant succeeds only when a clinic with that name exists (BL-03). |
| **AC-04** | **List active grants.** A patient can see the list of their own currently active grants (who/what was granted access, and when). Revoked and expired grants do not appear in this list (BL-05). |
| **AC-05** | **Revoke a grant.** A patient can revoke any one of their own grants. |
| **AC-06** | **Anonymous → 401.** No, invalid, or expired session on any consent endpoint (grant, list, revoke) returns `401 UNAUTHENTICATED` on the API; the web equivalent redirects to login. |
| **AC-07** | **Doctor role → 403.** A doctor (verified or not) calling a patient's consent-management endpoint (grant/list/revoke) gets `403 FORBIDDEN` — this is a role mismatch, not a D2 case, so it uses the ordinary `Forbidden` error, not `NotFound`. |
| **AC-08** | **Doctor without a grant → 404.** A verified doctor looking up a patient for whom they hold no active grant gets `404 NOT_FOUND`, with a response body indistinguishable (apart from `request_id`) from looking up a `passport_no` that was never issued (D2, BL-09, BL-10). |
| **AC-09** | **Doctor with a revoked grant → 404.** A verified doctor whose grant for a patient has been revoked gets the same `404 NOT_FOUND` as AC-08 — revoked and never-granted are not distinguishable from the response (BL-10). |
| **AC-10** | **Doctor with an active grant → sees the patient.** A verified doctor holding an active, unrevoked, unexpired grant for a patient can successfully look that patient up (the content returned is FR21's concern, not this feature's; this AC covers only that the lookup itself succeeds rather than 404s). |
| **AC-11** | **Revocation takes effect immediately.** Immediately after a patient revokes a grant, the very next doctor request for that patient — with no delay, retry, or re-login — returns `404`, matching AC-09 (BL-07). |
| **AC-12** | **No patient data in application logs.** No field from this feature — passport number, doctor PMDC number, clinic name, or grant detail — appears in any structlog output at any level, following the same redaction pattern as the patient-profile feature. |
| **AC-13** | **Urdu translations exist for all new strings.** Every new user-facing string introduced by this feature exists in both `en.json` and `ur.json`; the existing catalogue-completeness test still passes. |
| **AC-14** | **QR code renders without JavaScript.** The passport page's QR code is visible and usable with JavaScript disabled (e.g. a server-rendered `<img>` or inline `<svg>`), consistent with the rest of the app's no-JS-required posture. |

---

## 8. Business Logic (numbered rules)

Numbered to sit alongside `patient_profile_spec.md`'s `BL-01`…`BL-36` in spirit; this document's rules are independent (`BL-01`…`BL-12` here) and will be renumbered into a single sequence if the two specs are ever merged.

**Identity**
- **BL-01** A patient has **exactly one** `passport_no` (`patient.passport_uid`), issued once when their `patient` row is created (registration or onboarding), and this feature — like FR2 before it — never writes to it. It is the same value the QR code encodes (BL-11) and the value a doctor looks a patient up by (FR21, out of scope here).

**Granting access**
- **BL-02** A patient may grant access to a doctor by PMDC number. The grant is created **only if** a `Doctor` row with that `pmdc_number` exists **and** `verification_status == VERIFIED`. Granting to a nonexistent or unverified PMDC number is rejected — this mirrors FR3's rule that an unverified doctor reaches no patient data, extended here to mean an unverified doctor cannot even be made a grantee.
- **BL-03** A patient may grant access to a clinic by name. The grant is created only if a `Clinic` row with that name exists.
- **BL-04** At most **one active grant** may exist for a given patient–doctor pair at a time (and, by the same mechanism, one active grant per patient–clinic pair). TDD §3.4 describes this as a `uq_consent_one_active` **unique** index on `(patient_id, grantee_type, grantee_id) WHERE revoked_at IS NULL`, but the live schema's equivalent (`idx_consent_grant_active_lookup`) is a plain, **non-unique** partial index (verified 2026-10-01) — so this rule gets **no help from a DB constraint** and must be enforced entirely in service code (check for an existing active grant before inserting a new one). Granting again after a revoke is allowed and creates a new grant row; it does not resurrect the old one.

**Viewing and revoking grants**
- **BL-05** A patient can list all of their own currently **active** grants: `revoked_at IS NULL`, and, once `expires_at` is populated (BL-12), not yet expired. Revoked and expired grants are excluded from this list — this feature does not include a grant-history view.
- **BL-06** A patient can revoke any of their own grants at any time, active or already revoked; revoking an already-revoked grant is a harmless no-op (the same idempotency pattern as the patient-profile feature's soft-remove, `patient_profile_spec.md` BL-10 / AC-10).
- **BL-07** Revocation is checked live, per request, directly against the database — never cached, and never inferred from a token claim. There is no propagation delay: the request immediately following the one that set `revoked_at` must already observe the revocation. This mirrors `deps.py`'s `require_verified_doctor`, which re-checks verification from the DB on every call rather than trusting a cached value.
- **BL-08** A grant is **never hard-deleted** by this feature. Revoking sets `revoked_at` (and, per TDD §3.4, `revoked_by`); the row is kept, so the history of who had access and when survives for later audit/access-log review (FR5, NFR17).

**Doctor-side enforcement (D2)**
- **BL-09** A verified doctor with no active grant for a patient gets exactly nothing: not the record, not a message confirming or denying the patient's existence, and no response shape that differs from looking up a patient who was never issued a passport at all. This is enforced with the existing `NotFound` error class (`errors.py`) — never `Forbidden`, whose docstring already states it must never be used for a consent failure.
- **BL-10** A revoked or expired grant produces the **identical** `404 NOT_FOUND` envelope (differing only in `request_id`) as looking up a `passport_no` that was never issued. No code path in this feature may let a doctor distinguish "revoked," "expired," "never granted," and "no such patient" from one another.

**QR code**
- **BL-11** The QR code encodes `passport_no` **alone** — no name, date of birth, clinical data, or URL parameter carrying any other patient attribute. Anyone who scans or photographs it learns nothing beyond a passport number, and a bare passport number grants no access by itself (BL-09 still applies to whoever subsequently looks it up).

**Grant expiry**
- **BL-12** `expires_at` is a stored, nullable column on the grant (TDD §3.4), and the "active" checks in BL-05 and BL-09 must account for it once populated. This version's UI offers only "until revoked" when creating a grant — there is no expiry-date picker — so `expires_at` is always `NULL` for grants created through this feature's forms. The column exists so a future feature can add timed grants without a migration; implementing that UI is explicitly out of scope here.

---

## 9. Out of Scope (partial — see Status)

Recorded here only where it directly bounds the business rules above; a complete Out of Scope section belongs in the functional-spec pass.

1. **Anything a doctor sees once a grant exists** — the patient's history, profile, or record content (FR8, FR21). This feature stops at "the lookup succeeds"; what it returns is a separate feature.
2. **The patient's access-log view** (FR5 read side) — who accessed the record and when. This feature's grants are what the access log will be built on top of, but the log itself is not built here.
3. **A timed-expiry UI** for grants (BL-12) — the column is provisioned, the picker is not.
4. **Caregiver/delegated access** (FR7) and any grantee type other than a single doctor or a single clinic.
5. **Partial/scoped consent** (e.g. sharing prescriptions but not lab reports) — grants are all-or-nothing (§header, "Decisions already taken"). The live `consent_grant.scope_sections` column (`jsonb`, non-nullable, default `[]`, verified 2026-10-01) provisions for this the same way `expires_at` provisions for BL-12's timed expiry — this feature always writes it as `[]`/leaves it at its default and never reads it to restrict what a doctor/clinic sees. A future feature can light it up without a migration; implementing that is explicitly out of scope here.
