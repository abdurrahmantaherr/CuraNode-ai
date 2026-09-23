# PLAN — Patient Profile (FR2)

Source of truth: `.claude/specs/patient_profile_spec.md` (the "spec"). Where it conflicts with `docs/TDD.md`, the spec wins. Branch: `feature/patient-profile`.

## 1. Feature Summary
A patient-only "My profile" page (`/{locale}/patient/profile`) and JSON API (`/api/v1/me/profile`, `/api/v1/me/{allergies|conditions|medications}`). Patients use them to view and edit:
- basic details: name, DOB, gender, blood group, phone, emergency contact
- allergies, chronic conditions and current medications

Passport number, email and role are read-only. Entries are edited in place and soft-removed (`removed_at`, or `status='resolved'` for conditions). Nothing is hard-deleted. Every change is audit-logged in the same transaction. Entries recorded by clinicians or legacy entries (`recorded_by` ≠ caller) are read-only. The web pages and the API share one service layer (`backend/app/profile/service.py`).

## 2. Pre-conditions & Setup
- The branch is based on current `main`. `uv sync` is done. `.env` points at the **dev** Supabase project, and migration head `6feacefae2d0` is applied.
- Baseline is green: `uv run pytest -q` (78 tests) and `uv run ruff check backend tests`.
- Seeded synthetic patient exists (`backend/ops/scripts/seed_synthetic.py`) for the manual smoke test.
- No new packages. Avoid `tzdata`/`zoneinfo`; use a fixed UTC+5 offset instead.
- Read first: `CLAUDE.md`, `deps.py`, `db/models.py`, `errors.py`, `audit/writer.py`, `web/router.py::_guarded`, `partials/form_field.html`, `tests/conftest.py`.

## 3. Task Breakdown
| # | Task | Spec ref | Depends |
|---|---|---|---|
| T1 | Settings: `profile_write_rate_limit_per_minute=30`, `profile_max_entries_per_list=50`, plus `.env.example` | §7.5 | — |
| T2 | `db/types.py`: `PKT`, `today_pk()` | §7.4 | — |
| T3 | Models: `Patient.emergency_contact`; new `Allergy`, `ChronicCondition`, `PatientMedication`, using plain `String(20)` for severity/status and SQLite-portable columns | §7.1–7.2 | — |
| T4 | Hand-written additive migration (`down_revision="6feacefae2d0"`): new columns on `allergy`/`chronic_condition`, `patient_medication` table and index, and RLS policies behind a Postgres-only guard. `downgrade()` is the exact reverse | §7.3 | T3 |
| T5 | `errors.py`: `NotFound`, `NotEditable`, `DuplicateEntry`, `ListFull`, and `{max}` interpolation in `envelope()` without changing existing envelopes | §6.2 | — |
| T6 | `deps.py`: hoist `_require_patient`; add `enforce_profile_write_rate_limit`, `ProfileWriteRateLimit`, `check_profile_write_rate` | §6.5 | T1 |
| T7 | `audit/writer.py`: 10 `PROFILE_*` constants and a docstring exception for clinical values/`notes`. `log_config.REDACTED_KEYS` gets the BL-28 keys | BL-24–28 | — |
| T8 | `profile/schemas.py`: `clean_text`, `normalise_name`, `check_past_date`, request models with `extra="forbid"` and `PydanticCustomError` keys, Out models | §6.3 | T2 |
| T9 | `profile/service.py`: `_patient_for`, `get_profile`, `update_profile`, `add/update/remove_*` ×3, `compute_age`. Covers scoping, editability, duplicate check, list cap, DOB check, no-op detection, audit and commit | §4.1–4.6, §6.6 | T3,T5,T7,T8 |
| T10 | `profile/router.py` (module-scope dep imports) and `include_router` in `main.py` between identity and web | §6.4 | T6,T9 |
| T11 | i18n: every §5.8 key in `en.json` and `ur.json` | §5.8 | — |
| T12 | Templates: extract `partials/topbar.html` (doctor/admin output unchanged); extend `form_field.html` (`min/max/maxlength/dir/id_prefix`, `f.textarea`); new `patient/profile.html`; dashboard link | §5.1–5.5 | T11 |
| T13 | CSS appended to `app.css` (tokens and logical properties only; `.btn--sm` in the coarse-pointer rule); new `static/js/profile.js` submit guard | §5.6–5.7 | T12 |
| T14 | Web handlers in `web/router.py`: `_patient_page_guard`, GET and 10 POST routes, PRG with `saved` allow-list, `edit` param, BL-14 unchanged-field drop, error mapping, `nav_items` via `_guarded` | §4.7–4.8 | T9,T12 |
| T15 | Tests: `tests/test_profile.py` (T1–T25) and `tests/test_profile_schemas.py`; conftest helpers; call counter on `FakeSupabaseAuth` if missing | §11 | all |
| T16 | Gate: pytest, ruff check/format; then `alembic upgrade head` on dev and the manual smoke test | §11.4 | T15 |

## 4. File & Module Map
**New**
- `backend/app/profile/__init__.py`, `schemas.py`, `service.py`, `router.py`
- `alembic/versions/<rev>_patient_profile_provenance_soft_remove_medication.py`
- `frontend/templates/patient/profile.html`
- `frontend/templates/partials/topbar.html`
- `frontend/static/js/profile.js`
- `tests/test_profile.py`, `tests/test_profile_schemas.py`

**Modified**
- `backend/app/settings.py`, `.env.example`
- `backend/app/db/types.py`, `backend/app/db/models.py`
- `backend/app/errors.py`, `backend/app/deps.py`
- `backend/app/audit/writer.py`, `backend/app/log_config.py`
- `backend/app/i18n/messages/en.json`, `ur.json`
- `backend/app/main.py`, `backend/app/web/router.py`
- `frontend/templates/shell.html`, `frontend/templates/partials/form_field.html`
- `frontend/static/css/app.css`
- `tests/conftest.py`, `tests/fakes.py` (only if a call counter is needed)

**Not touched:** `static/js/auth.js`, identity/OAuth modules, and `GET /api/v1/me`/`MeOut`.

## 5. Scaffolding Notes
- Migration: `uv run alembic revision -m "patient profile: entry provenance, soft-remove, patient_medication"`, then **replace the body by hand**. Never use `--autogenerate`.
- Models follow the existing `mapped_column("<db_name>", ...)` style, e.g. `id` ↔ `allergy_id`. Use `default=utcnow` and `default=uuid7` in Python so SQLite tests work.
- Custom validators raise `PydanticCustomError("<type>", "errors.<key>")` so that `msg` is exactly the i18n key.
- Import `PatientDep`/`SessionDep`/`ProfileWriteRateLimit` at **module scope** in `profile/router.py`. With `from __future__ import annotations`, local imports turn them into query params.
- Callers use `from ..db import types as dbtypes; dbtypes.today_pk()` so tests can monkeypatch it in one place.
- Form field ids come from `id_prefix` (`basics-`, `allergy-new-`, `allergy-{id}-`, …) so every id on the page is unique.

## 6. Integration Points
- **Auth/RBAC:** API uses `PatientDep`. Web uses `OptionalActorDep` + `_patient_page_guard`, which redirects to login?next / own area / onboarding. No ad-hoc role checks.
- **DB:** shared tables `user_profile` (write `full_name`, `phone` only), `patient`, `allergy`, `chronic_condition`; repo-owned `patient_medication`, `audit_log`. The app role has BYPASSRLS, so every query filters by the caller's `patient_id`.
- **Audit:** `audit.writer.write()` inside the mutation transaction. `resource_id` goes in `detail`.
- **Rate limit:** `cache.incr(ratelimit_key("profile_write", user_id), 60)`, a counter shared by API and web.
- **i18n/RTL:** `translate()` plus catalogue-completeness test. Locale comes from the URL prefix.
- **Shell:** `_guarded()` passes `nav_items` for patients only. The dashboard gets a "My profile" link.
- **Supabase Auth:** **never called** by this feature (BL-06).

## 7. Test Plan
- `tests/test_profile_schemas.py` (unit): `clean_text`, `normalise_name`, `compute_age` (including Feb-29), `check_past_date` bounds, `extra="forbid"` on every request model.
- `tests/test_profile.py`: one `test_tN_*` per AC-01…AC-25 (spec §11.2), including:
  - view: web and API
  - access matrix
  - basics: update, clear, validation, legacy values
  - protected fields
  - CRUD ×3 with soft remove and idempotency
  - entry validation, duplicates, list cap
  - read-only clinician rows
  - ownership isolation: identical 404 bodies
  - audit rows and no-op/rejected requests
  - no PHI in logs
  - rate limit and `Retry-After`
  - Urdu/RTL, a11y/no-JS/unique ids, output escaping
  - nav links
  - static migration check
- Fixtures: `login`, `patient_user`, `second_patient`, `add_*_row` helpers, `freeze_today` (2026-09-22), structlog `capture_logs`.
- Gate (AC-26): `uv run pytest -q` (78 existing + new), `uv run ruff check backend tests`, `uv run ruff format --check backend tests`.
- Manual (§11.4):
  1. `alembic upgrade head` on dev, then verify in `information_schema` and RLS.
  2. Use every form in `/en` and `/ur`, with JS on and off, at 360 px and desktop widths.
  3. The 2 legacy allergies show as read-only.
  4. Server logs contain no profile values.

## 8. Edge Cases & Error Handling
| Case | Handling |
|---|---|
| Patient-role user with no `patient` row (OAuth onboarding) | API `404`; web `303` → `/onboarding` |
| Foreign, nonexistent, or inactive id (PATCH) | `404 NOT_FOUND`, identical body apart from `request_id` |
| Malformed UUID | API `422`; web treated as `404` |
| Clinician or legacy entry (`recorded_by` NULL/other) | `403 NOT_EDITABLE`; no controls in the UI |
| Repeat DELETE | `204`, no second audit row |
| No-op PATCH / empty body | `200`, no write, no audit |
| Duplicate normalised name (active only, including non-editable) | `409 DUPLICATE_ENTRY` |
| Cap reached | `422 LIST_FULL` with `{max}` |
| Since-date before stored DOB | `422 errors.date_before_birth` (service check) |
| Legacy `gender`/`blood_group`/`severity`/`status` values | Shown raw; untouched legacy value never blocks a web save (BL-14) |
| `full_name` null/blank | `422 errors.name_required` |
| Extra/protected fields | API `422` (`extra="forbid"`); web ignores them |
| XSS text in free fields | Stored verbatim; Jinja autoescape on render |
| Rate limit exceeded | `429` + `Retry-After: 60`; web shows a banner |
| Unexpected exception | Session rolled back; web `500` banner `errors.internal`; no partial commit |
| Web failure | Full page re-render with the failing section's values and errors; other sections come from the DB |
| Unknown `saved`/`edit` params | Silently ignored (no reflection, no existence check) |

## 9. Rollback Strategy
- **Code:** everything is additive in new modules plus small edits. Revert the feature commit(s) or the merge commit. Routes, templates and CSS disappear with no data impact.
- **Schema:** `uv run alembic downgrade 6feacefae2d0` drops the policies, index and `patient_medication` table, then the added columns. **Data warning:** this destroys patient medications and entry provenance/soft-remove markers. Export `patient_medication` and the new columns first if they hold anything worth keeping. Dev/pilot data is synthetic only.
- **Partial rollback:** the migration is safe to leave applied while code is reverted. The new columns are nullable/defaulted, and the old code ignores them.
- Never run the migration or downgrade against a non-synthetic environment without sign-off.

## 10. Definition of Done
- [ ] AC-01…AC-26 each covered by a passing `test_tN_*`; schema unit tests pass.
- [ ] All 78 pre-existing tests pass; ruff check and format are clean.
- [ ] Migration applied to dev Supabase; `patient_medication` has RLS enabled; `upgrade()` has no drop/alter/rename.
- [ ] Every new string exists in `en.json` and `ur.json`; the catalogue test passes; the Urdu copy is flagged for native-speaker review.
- [ ] No raw hex or physical-direction CSS; the page works without JS at 360 px.
- [ ] No Supabase Auth calls, no PHI in logs, no values in `profile.update` audit detail, and `notes` never in audit values.
- [ ] Manual smoke test (§11.4) done in both locales.
- [ ] Nothing from spec §10 (out of scope) implemented.
