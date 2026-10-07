# PLAN — Privacy & Access Control (P9: FR4 per-section, FR5)

Source of truth: `.claude/specs/privacy_access_spec.md` (the "spec"). Where it conflicts with `docs/TDD.md` or `.claude/specs/medical_passport_spec.md`, the spec wins. Branch: `feature/privacy-access`.

## 1. Feature Summary
P9 turns P4's all-or-nothing grants into **per-section** grants (basics, allergies, conditions, medications, prescriptions, lab reports, visits) with a chosen duration (24 h / 7 d / 30 d / until revoked, default 30 d). Patients can change a grant's sections, which revokes it and creates a linked replacement, and they can revoke at any time. Patients find grantees by searching verified doctors and active clinics. A new **consent gateway** (`consent/gateway.py`) is the only door for reading another person's profile sections. It honours doctor grants and clinic grants via active affiliation, returns D2-identical 404s, and writes one `access_log` row per section read in the same transaction. Patients see the access log (FR5) on the API and on `/{locale}/patient/access`, which is rebuilt into "Who can see my passport", "Give access" and "Who looked". The doctor patient page shows shared sections and labels the rest "Not shared by patient".

## 2. Pre-conditions & Setup
- On `feature/privacy-access` (branched from `dev` at `eb20813`). Unrelated local changes (`.gitignore`, `PROGRESS.md`, `.claude/settings.local.json`, `.claude/spec/`, `Network - Shortcut.lnk`) are **not** committed by this work.
- Baseline is green: `uv run pytest -q` → 244 passed.
- No new packages.
- **No writes to the shared Supabase DB.** No `alembic upgrade`. The migration is tested on SQLite only. The live DB is at revision `35c4bc13fa37`, which is unknown to this repo; that has to be reconciled before any apply.
- Read first: spec §4 and §8, `consent/*.py`, `web/router.py` (access + doctor patient handlers), `tests/test_consent.py`, `tests/test_passport_web.py`, `tests/test_doctor_dashboard.py`.

## 3. Task Breakdown
| # | Task | Spec ref | Depends |
|---|---|---|---|
| S1 | **Models + migration.** `ConsentGrant.revoked_by`/`replaced_by` + unique partial index; new `AccessLog` model; hand-written migration (`down_revision="488dbf2b3915"`) with `[]` backfill; docstrings. Tests: unique index rejects a 2nd unrevoked grant; `access_log` action CHECK; migration static check (AC-29). | §7.2–7.3 | — |
| S2 | **Schemas + errors + settings.** `SECTIONS`, `normalise_sections`, `Duration`, new `GrantCreate`/`SectionsUpdate`, extended `ConsentGrantOut`, `GranteeOut`, `AccessLogEntry`/`AccessLogPage`; `ConsentDenied(NotFound)`; `consent_search_rate_limit_per_minute`; `ConsentSearchRateLimit`. Unit tests for validation. | §6.2–6.5 | — |
| S3 | **Consent service.** `grant_consent` (ids or P4 identifiers, sections, duration, duplicate → existing, lapsed → closed), `change_sections`, `revoke_grant` (+`revoked_by`), `list_grants(include_revoked)`, `search_grantees`, audit `consent.change`. Update P4 tests whose behaviour changes (spec §10.2). Tests AC-01…AC-12. | §4.2–4.6 | S1, S2 |
| S4 | **Gateway + doctor-side consistency.** `consent/gateway.py` (`Access`, `allowed_sections`, `read_section`, `read_sections`, section readers); shared doctor-grant filter used by `_granted_patient`/`list_patients_for_doctor` (clinic via affiliation). Tests AC-13…AC-20, AC-23 (service level). | §4.7–4.8 | S1, S3 |
| S5 | **Guard test.** AST scan: no `select(...)` over `Allergy`/`ChronicCondition`/`PatientMedication` outside `profile/service.py` and `consent/gateway.py` (AC-21). | §3 AC-21 | S4 |
| S6 | **Consent API.** Router: grantee search (declared first), list with `include_revoked`, POST (201/200), PUT sections, DELETE; rate limits. Tests per endpoint incl. 404/422/429/403/401 (AC-05, AC-08, AC-11, AC-25, AC-26). | §6.1 | S3 |
| S7 | **Access log.** `consent/access_log.py` (keyset cursor) + `GET /api/v1/me/access-log`. Tests AC-22. | §4.9 | S4, S6 |
| S8 | **Web pages.** Rebuild `patient/access.html` (3 parts, search GET, grant POST, save-sections POST, revoke) and handlers; rebuild `doctor/patient.html` through `gateway.read_sections`; CSS; en/ur keys. Update `test_passport_web.py` for the new form; new web tests AC-17, AC-24, AC-27, AC-28. | §4.10–4.12, §5 | S4, S6, S7 |
| S9 | **Docs.** `CLAUDE.md` (P9 section, test count, `access_log` now used), `PROGRESS.md`, `docs/TDD.md` §3.4 amendment note, `medical_passport_spec.md` pointer to P9 supersessions. | §6.0 | S8 |

Each step is done test-first: write the failing tests, implement, run `uv run pytest -q` and `uv run ruff check backend tests`, then commit (`feat(P9): …` / `test(P9): …` / `docs(P9): …`). Never push.

## 4. File & Module Map
**New**
- `backend/app/consent/gateway.py`, `backend/app/consent/access_log.py`
- `alembic/versions/<rev>_consent_grant_sections_replaced_by.py`
- `tests/test_privacy_access.py` (service, gateway, API, guard), `tests/test_privacy_access_web.py` (pages)

**Modified**
- `backend/app/db/models.py` (`ConsentGrant`, new `AccessLog`, `AuditLog` docstring)
- `backend/app/consent/schemas.py`, `service.py`, `router.py`
- `backend/app/errors.py` (`ConsentDenied`), `backend/app/deps.py` (search rate limit), `backend/app/settings.py`, `.env.example`
- `backend/app/audit/writer.py` (`CONSENT_CHANGE`)
- `backend/app/web/router.py` (access page handlers, doctor patient page)
- `frontend/templates/patient/access.html`, `frontend/templates/doctor/patient.html`, `frontend/static/css/app.css`
- `backend/app/i18n/messages/en.json`, `ur.json`
- `tests/test_consent.py`, `tests/test_passport_web.py` (behaviour changes only)
- `CLAUDE.md`, `PROGRESS.md`, `docs/TDD.md`, `.claude/specs/medical_passport_spec.md`

**Not touched:** identity/OAuth, `profile/` (except nothing; the gateway has its own readers), D4 `doctor/rows.py` (reused read-only).

## 5. Scaffolding Notes
- Migration: create the file by hand (`uv run alembic revision -m "consent_grant: revoked_by, replaced_by, one active grant"`, then **replace the body**). Use `op.batch_alter_table` for the column adds so SQLite can run it; Postgres-only bits behind `op.get_bind().dialect.name == "postgresql"`.
- Unique partial index in the model: `Index(..., unique=True, postgresql_where=text("revoked_at IS NULL"), sqlite_where=text("revoked_at IS NULL"))` so `create_all` in tests enforces BL-04.
- When revoking and inserting in one transaction, **flush the revoke before adding the new row**; the index would otherwise fire.
- `AccessLog.ip_address`: `String(64).with_variant(postgresql.INET(), "postgresql")`.
- Router: `PatientDep`/`SessionDep`/rate-limit deps imported at module scope (string annotations). Declare `/consents/grantees` before `/consents/{grant_id}...`.
- POST returning 201 or 200: set `response.status_code = 200` on the injected `Response` when the grant already existed.
- Web multi-checkbox: `form.getlist("sections")`.
- `ConsentDenied` subclasses `NotFound`; keep `code="NOT_FOUND"` so envelopes are byte-identical.

## 6. Integration Points
- **Auth/RBAC:** API `PatientDep`; web `_patient_page_guard` / `_doctor_page_guard`. The gateway re-reads the doctor's verification and affiliations live.
- **DB:** `consent_grant` (shared, additive columns), `access_log` (shared, insert + select only), profile tables (select only, via the gateway for non-owners), `audit_log`.
- **Audit:** `audit.writer.write()` in the same transaction for grant/change/revoke. `access_log` rows written by the gateway only.
- **Rate limit:** `consent_write` (existing, 10/min) for writes; `consent_search` (new, 30/min) for search, shared by API and web.
- **i18n/RTL:** new keys in both catalogues; catalogue-completeness tests already exist.
- **D4 dashboard:** keeps calling `consent_service.list_patients_for_doctor`, which now also honours clinic grants.

## 7. Test Plan
- `tests/test_privacy_access.py`: `test_acNN_*` per spec §11.2 for AC-01…AC-23, AC-25…AC-27, AC-29.
- `tests/test_privacy_access_web.py`: AC-17 (doctor page), AC-24 (access page incl. Urdu RTL, no pre-ticked sections, 30 d default, PRG banners), AC-28.
- Updated: `tests/test_consent.py` (sections required, duplicate → 200, default expiry), `tests/test_passport_web.py` (grant form via search).
- Gate after every step: `uv run pytest -q`, `uv run ruff check backend tests`; at the end also `uv run ruff format --check backend tests`.
- Manual (spec §11.4): local server, both locales, patient + doctor flows. **No migration apply.**

## 8. Edge Cases & Error Handling
| Case | Handling |
|---|---|
| `sections` empty / unknown key / duplicates | 422 / 422 / de-duplicated silently |
| Grant to unverified doctor or inactive clinic | 404, identical to unknown |
| Active grant exists | 200 with the existing grant, no write |
| Expired-but-unrevoked grant exists | Closed (`revoked_at`, `replaced_by`) in the same transaction as the new grant |
| Change sections on revoked/expired/foreign/missing grant | 404, identical body |
| Change to identical sections | 200, no write, no audit |
| Concurrent grants racing past the service check | Unique index raises `IntegrityError` → rolled back; the API answers 409 `DUPLICATE_ENTRY` (rare race only) |
| Doctor affiliation ended | Clinic grant no longer applies on the next request |
| Legacy grant with `[]` (pre-migration) | Nothing shared → gateway `ConsentDenied`; D4 still lists the patient |
| `access_log` insert fails | Read rolled back, no data, 500 |
| Grantee row deleted (`grantee_id` has no FK) | Name shown as "Account no longer available"; grant still revocable |
| Search `q` with `%`/`_` | Escaped; matched literally |
| Malformed access-log cursor | 422 `errors.cursor_invalid` |
| Unknown `saved` param | Ignored |

## 9. Rollback Strategy
- **Code:** revert the P9 commits (or the merge). The P4 API shape is mostly kept (`pmdc_number`/`clinic_name`, `scope_sections`), so P4 clients keep working until the revert.
- **Schema (only if it was ever applied):** `alembic downgrade 488dbf2b3915` drops the unique index and the two columns. The `[]` → all-sections backfill is **not** reversed, on purpose: restoring `[]` would silently hide records from doctors the patient had granted. Data is synthetic only.
- **Partial:** the migration can stay applied with old code. The new columns are nullable, and old code ignores them. The unique index matches what P4 already enforced in code.

## 10. Definition of Done
- [ ] AC-01…AC-30 each covered by a passing test.
- [ ] All 244 pre-existing tests pass (changed only per spec §10.2); ruff check and format clean.
- [ ] Migration is additive, `downgrade()` reverses it, it is exercised on SQLite, and it is **not** applied to the shared DB.
- [ ] Guard test in place; no profile-table select outside `profile/service.py` and `consent/gateway.py`.
- [ ] Every new string in `en.json` and `ur.json`; Urdu flagged for native-speaker review.
- [ ] No PHI/PMDC/clinic name/search text in logs or `audit_log.detail`.
- [ ] Spec, plan and docs (`CLAUDE.md`, `PROGRESS.md`, TDD §3.4 note) committed on `feature/privacy-access`; nothing pushed.
