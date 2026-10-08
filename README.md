# CuraNode-AI

A cross-hospital health management platform for Pakistan's private healthcare sector, built as a BS Data Science Final Year Project at PUCIT.

Patients in Pakistan carry their medical history on paper, and when the paper is lost the history is gone. CuraNode gives every patient one portable **Medical Passport** that they own and that any clinician they authorise can read instantly, from any participating facility.

> **Project status — authentication core (security-hardened), Google OAuth, Medical Passport with consent, and the patient profile implemented.**
> This repository currently implements: *Authentication & Role-Based Access* (password + Google OAuth), *OAuth Onboarding*, *Medical Passport (FR1)* with patient-controlled sharing (*consent, FR4*), and the *Patient Profile (FR2)*. Registration, sign-in (password and Google), sessions, role gates, first-time-OAuth-user onboarding, passport number generation/display with a QR code, granting/revoking a verified doctor's or clinic's access, a verified doctor's consent-gated passport lookup, and a patient's own basic details / allergies / chronic conditions / current medications (view, add, edit, soft-remove) are complete and tested. The authentication layer has been through a security audit; the fixes are summarised under [Security hardening](#security-hardening) and documented in `docs/auth_hardening.md`. Appointments, document OCR, and the AI orchestrator described in `docs/PRD.md` are **not built yet**.
>
> Identity is delegated to **Supabase Auth**, and the database is **Supabase Postgres** — shared with the wider CuraNode-AI product, so this repo's tables (`user_profile`, `clinic`, `patient`, `doctor`, `clinic_staff`, `doctor_affiliation`, `allergy`, `chronic_condition`) match that shared schema rather than inventing parallel ones. See [How authentication works](#how-authentication-works) and [How the patient profile works](#how-the-patient-profile-works) below.

---

## Quick start

Requires **Python ≥3.13**, [**uv**](https://docs.astral.sh/uv/), and a **Supabase project** (Postgres + Auth). No Docker, no local database server, no Node.

```bash
uv sync                                                # install dependencies
cp .env.example .env                                   # fill in your Supabase project's values
uv run alembic upgrade head                            # apply the schema (additive only — see below)
uv run python backend/ops/scripts/seed_synthetic.py    # create demo accounts in Supabase Auth
uv run backend/app/main.py                              # start the app
```

Open **http://127.0.0.1:8000** — it redirects to the sign-in page. Use the same host as `PUBLIC_BASE_URL` throughout (`127.0.0.1`, not `localhost`): the browser treats them as different sites, so a Google sign-in started on one and returned to the other loses its state cookie and fails.

| URL | What it is |
|---|---|
| `/en/login` · `/ur/login` | Sign in, English or Urdu |
| `/en/register` | Create a patient or doctor account |
| `/en/onboarding` | First-time Google sign-in users choose a role here |
| `/en/patient/profile` · `/ur/patient/profile` | A signed-in patient's own basic details, allergies, chronic conditions, and current medications |
| `/en/patient/passport` | The patient's Medical Passport number and QR code |
| `/en/patient/access` | Grant or revoke a verified doctor's (by PMDC number) or a clinic's access |
| `/en/doctor/passport-lookup` | A verified doctor looks up a patient who has granted them access |
| `/docs` | OpenAPI reference for the JSON API |
| `/healthz` | Health check |

"Continue with Google" only appears once `OAUTH_ENABLED=true` and `SUPABASE_ANON_KEY` are set — see [Configuration](#configuration). It's off by default.

### Demo accounts

Seeded by the script above — created as real Supabase Auth users, visible in your project's Dashboard → Authentication. Password for all four: **`CuraNode!2026`**

| Email | Role | Notes |
|---|---|---|
| `ayesha.raza@example.com` | Patient | Has a Medical Passport number; sign in and open **My profile** to see it |
| `adnan.haleem@example.com` | Doctor | Verified — full doctor access |
| `nadia.iqbal@example.com` | Doctor | **Unverified** — blocked from all clinical routes; defaults to Urdu |
| `front.desk@example.com` | Clinic admin (`role = "admin"`) | Lands on a placeholder |

To verify the pending doctor and watch access appear on the *next request* without re-login, run against your Supabase Postgres project (e.g. via the SQL editor, or `psql`):

```sql
update doctor d
set verification_status = 'verified',
    verified_by = (select user_id from user_profile where email = 'front.desk@example.com'),
    verified_at = now()
from user_profile u
where d.user_id = u.user_id and u.email = 'nadia.iqbal@example.com';
```

---

## Project layout

```
backend/
  app/
    main.py            ASGI app, middleware, startup guards, dev server
    settings.py        configuration + fail-fast boot checks
    paths.py           single source of truth for directory locations
    deps.py            Actor + the four role dependencies
    errors.py          error envelope and taxonomy
    cache.py           TTL store for rate limits, OAuth state, and logout revocation markers
    log_config.py      structured logging with PII redaction
    db/                models (mapped onto the shared Supabase schema), async session
    identity/          router · service · schemas · security · oauth (Supabase Auth + Google sign-in)
    audit/              append-only audit writer
    profile/            patient profile (FR2): schemas · service · router — basics, allergies, conditions, medications
    consent/            Medical Passport sharing (FR4): grant/revoke access, consent-gated doctor lookup
    i18n/               en/ur message catalogues
    web/                server-rendered page routes and form handling (incl. OAuth + onboarding + patient-profile routes)
  ops/scripts/          synthetic seed data (creates Supabase Auth users + app rows)
alembic/                 database migrations (additive-only — see Known deviations)
frontend/
  templates/            Jinja2 — base, auth/{login,register,onboarding,oauth_complete}, patient/{profile,passport,access}, doctor/{lookup,patient}, shell, partials
  static/css/            tokens.css (design tokens) + app.css
  static/js/             progressive enhancement only (auth.js, profile.js)
tests/                   in-memory SQLite + a fake Supabase client; test_pg_authority_guards.py needs a local Postgres
docs/                    PRD · TDD · DESIGN · app-foundation · auth_hardening.md · prompts/oauth.md (OAuth plan)
.claude/specs/           patient_profile_spec.md, medical_passport_spec.md — feature sources of truth
```

## Stack

| Layer | Choice |
|---|---|
| API & pages | FastAPI, server-rendered Jinja2 templates |
| Identity | **Supabase Auth** — password hashing, session issuance, refresh-token rotation, Google OAuth (server-side PKCE) |
| Persistence | SQLAlchemy 2.0 (async) over **Supabase Postgres**, migrated with Alembic |
| Sessions | Supabase access token (JWT, ES256/JWKS-verified) + refresh token, both in HttpOnly cookies |
| Styling | Hand-written CSS driven by the tokens in `docs/DESIGN.md` |
| Languages | English and Urdu, with right-to-left layout |

---

## How authentication works

Registration takes a name, email, and password. There is no OTP and no email-verification step — an account is active immediately, and the user then signs in through the normal login form. An email that is already registered (compared after trimming and lower-casing) is refused with "An account with this email already exists. Please log in."

**Credentials and sessions live entirely in Supabase Auth** — this codebase never hashes a password or signs a token itself. `backend/app/identity/security.py` talks to Supabase's Auth API server-side only (the service-role key is never exposed to templates or JS), and verifies access tokens locally against the project's public JWKS. Two Supabase clients matter here and must stay separate: a persistent one for `auth.admin.*` calls, and a **fresh, single-use client per sign-in/refresh** — the SDK's client is stateful (`sign_in_with_password` mutates its session and starts a background auto-refresh timer), so sharing one instance across users or between a sign-in and a later admin call corrupts it.

**A patient or a doctor may self-register.** Choosing *Doctor* creates a real doctor account but always with `verification_status = 'pending'`, so the account grants **zero** access to any patient record until a clinic administrator verifies it. Creating a doctor *account* is never the same as granting doctor *access*.

Properties worth knowing, each covered by a test:

- **The role toggle is enforced.** Signing in as *Doctor* on a patient account is refused with `403` and no session. The check runs after the password is verified, so it can only be reached by someone who already owns the account and is never an account-enumeration oracle.
- **Login failures are shape-identical.** A wrong password, an unknown email, a locked account, and a suspended account return byte-identical `401`s, and unknown-email/locked logins still make one decoy round trip to Supabase so they aren't instantly distinguishable by response time. (Exact timing parity is still not guaranteed — Supabase itself only runs bcrypt for existing users.)
- **Duplicate registration is refused, not silently accepted.** A duplicate creates nothing and Supabase is never called; two concurrent registrations for the same address are settled by Supabase Auth's unique-email constraint (one `201`, one `409`). Because registration doesn't verify email ownership, Google sign-in is refused for password-registered accounts (see `docs/auth_hardening.md`).
- **Verification is never cached in a token.** The access token deliberately carries only a user id; role and verification status are read from the database per request, so revoking a doctor takes effect on their next call.
- **Refresh tokens are single-use**, rotated and reuse-detected by Supabase Auth itself.
- **Lockout stays app-level and independent of Supabase**: an account locks for 15 minutes after 10 failed passwords. Each attempt atomically reserves a slot in the database *before* Supabase is called, so parallel guesses can't exceed 10; attempts during a lock don't extend it; and once a lock expires the next window starts fresh (one wrong guess can't re-lock it).
- **Nothing sensitive reaches the logs** — passwords, tokens, emails, phone numbers, and names are dropped by a redaction processor at every level, including `DEBUG`.
- **A Supabase trigger (`on_auth_user_created`) auto-inserts a default `user_profile` row** the instant an auth user is created — registration *updates* that row rather than inserting a second one.

Authorisation is enforced on the backend through four dependencies in `deps.py` — `ActorDep`, `PatientDep`, `VerifiedDoctorDep`, `ClinicAdminDep` — mirrored for browser pages by guards in `web/router.py` that redirect instead of returning a bare 403.

### Security hardening

From the authentication security audit (details, trade-offs and roll-out steps in `docs/auth_hardening.md`; regression tests in `tests/test_auth_security.py` and `tests/test_pg_authority_guards.py`):

- **Logout really ends the session.** Supabase revokes the refresh tokens, and the app also refuses the logged-out access token (and any older token of that user) immediately, plus any access token older than `ACCESS_TOKEN_MINUTES` — checked locally, no database lookup per request.
- **Login CSRF is blocked.** Login and registration (web and JSON) refuse a foreign `Origin`/`Referer`; the Google callback is exempt because it is a cross-site navigation by design.
- **Private pages are never cached.** Every non-static response carries `Cache-Control: no-store`, so the Back button can't show a patient's data after logout on a shared clinic computer. A CSP header is sent everywhere and HSTS in `pilot`.
- **Registration is atomic.** If anything fails after the Supabase user was created, that user is deleted again; a duplicate PMDC number is refused before Supabase is called; Supabase and database errors never reach the user.
- **Input hygiene.** Names are trimmed before length checks and may not contain control characters; whitespace-only passwords are refused; a client-supplied `X-Request-Id` is only echoed if it is a short, inert token; unhandled errors are logged by type only (no PII).
- **The Supabase Data API can't be used to escalate.** Migration `a3c1f0e7b2d4` adds triggers so a signed-in user can't change their own doctor verification/PMDC number, profile email/lockout counters/synthetic flag, or passport number through Supabase's REST API; `b7d2e9c4a1f6` revokes TRUNCATE from the API roles. **Both are written but not yet applied to the shared database** — see `docs/auth_hardening.md` §3.

### Google sign-in (OAuth)

Off by default (`OAUTH_ENABLED=false`) — see [Configuration](#configuration) to turn it on. When enabled, "Continue with Google" appears on the login and register pages and runs a server-side PKCE flow entirely in `backend/app/identity/oauth.py` and the OAuth routes in `backend/app/web/router.py`; the browser never sees a token.

- **A caller-supplied `state` on Supabase's own `/authorize` call is a trap, not a pass-through slot.** The real `supabase-py` SDK never sends one (confirmed by reading `AsyncGoTrueClient._get_url_for_provider`) — GoTrue manages its own internal state for the round trip to the provider, and a client-supplied `state=` query param collides with that, coming back as `error_code=bad_oauth_state`. This app's own CSRF-binding state instead rides **inside** the `redirect_to` URL's own query string (`.../auth/callback?state=...`), which Supabase preserves verbatim and just appends `?code=...` to.
- **The state is single-use and browser-bound**, checked three ways on the callback: the query param (from `redirect_to`), an HttpOnly `cn_oauth_state` cookie set at the start of the flow, and a cache entry — all three must agree, and the cache entry is consumed before anything else happens.
- **The state cookie is `SameSite=Lax`, not `Strict`.** It has to survive the cross-site return hop from Google via Supabase. The session cookies (`cn_access`/`cn_refresh`) stay `Strict` as always; a same-site interstitial page (`auth/oauth_complete.html`) bounces the browser through one same-site request first so those `Strict` cookies aren't dropped on arrival.
- **A brand-new Google user always lands on `/{locale}/onboarding` first** — never on a dashboard. The Supabase trigger that auto-creates a `user_profile` row on any new auth user (password or OAuth) defaults its role to `patient`, but no `Patient`/`Doctor` row exists yet for an OAuth signup, so `deps.py`'s `Actor.onboarding_complete` is `False` and every dashboard route bounces back to onboarding until a role is chosen. `doctor.pmdc_number` is `UNIQUE NOT NULL` and a patient's passport number is permanent, so this choice is never made automatically.
- **Google sign-in is refused for accounts registered with a password.** Password registration doesn't verify email ownership, so someone could register `victim@gmail.com` with their own password; when the real owner later used Google, Supabase's automatic identity linking would attach their Google identity to that account. So if the Supabase user behind a Google sign-in has a password identity, the sign-in is refused, the session Supabase just issued is revoked, and the user sees "An account with this email already exists. Sign in with your password instead." Accounts created through Google are unaffected. This also applies to password accounts that had Google linked earlier — they sign in with the password.
- **An email that already belongs to a *different* `user_profile` row is refused** the same way — see `service.login_with_oauth`.
- **OAuth honours an existing lockout but never causes one** — there's no password on this path to brute-force.

### Endpoints

**JSON API** (`/api/v1`) — `POST /auth/register` (`201` on success with no session — sign in next; `409 EMAIL_ALREADY_REGISTERED` for a taken email), `POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout`, `GET /me`, `GET /clinics`. (OAuth is web-only; there's no JSON equivalent.) Patient profile: `GET|PATCH /me/profile`, `POST|PATCH|DELETE /me/allergies[/{id}]`, `POST|PATCH|DELETE /me/conditions[/{id}]`, `POST|PATCH|DELETE /me/medications[/{id}]` — all behind `PatientDep` and a per-patient write rate limit. Consent: `GET|POST /me/consents`, `DELETE /me/consents/{id}` (patients only).

**Pages** — `GET|POST /{locale}/login`, `GET|POST /{locale}/register`, `POST /{locale}/logout`, `POST /{locale}/auth/oauth/google`, `GET /auth/callback` (not locale-prefixed — it's the fixed redirect URI registered with Supabase), `GET|POST /{locale}/onboarding`, the role-gated areas `GET /{locale}/patient`, `/{locale}/doctor`, `/{locale}/admin`, `GET|POST /{locale}/patient/profile[/allergies|conditions|medications[/{id}[/remove]]]` for the patient profile, `GET /{locale}/patient/passport`, `GET|POST /{locale}/patient/access[/{id}/revoke]`, and `GET|POST /{locale}/doctor/passport-lookup` + `GET /{locale}/doctor/patient/{id}` for the Medical Passport.

Locale is a path prefix, so switching language is a route change that keeps you on the same page.

---

## How the patient profile works

A signed-in patient's **My profile** page (`/{locale}/patient/profile`) and its matching JSON API (`/api/v1/me/...`) let them view and maintain their own basic details, allergies, chronic conditions, and current medications. The passport number, sign-in email, and role are always read-only here.

- **One service layer, two front doors.** `backend/app/profile/service.py` holds every business rule; both the JSON API (`profile/router.py`) and the server-rendered page (handlers in `web/router.py`) call the same functions, so a rule is never implemented twice.
- **Nothing is ever hard-deleted.** Removing an allergy or medication sets `removed_at`; removing a chronic condition sets `status = 'resolved'`. The row stays in the database — only soft-removed and never shown again. Editing an entry keeps its history: every successful add, edit, or removal writes one row to `audit_log` in the same transaction, with before/after values for clinical fields (never for `notes`, which may hold free-text PII).
- **An entry is editable only by whoever recorded it.** `recorded_by == caller's user_id` is the whole rule — a `NULL` `recorded_by` (a legacy row) or another user's id (a clinician's entry) is shown as "Recorded by a clinician" with no Edit/Remove controls, and the API returns `403 NOT_EDITABLE` if you try anyway. A patient can never erase something a clinician wrote.
- **Supabase RLS does not protect these queries.** This app connects to Postgres as `postgres` with `BYPASSRLS`, so every query in `profile/service.py` filters by the caller's own `patient_id` in application code. The new `patient_medication` table still gets RLS policies (mirroring the shared tables) so it isn't left open through Supabase's own Data API — that's a different door this app doesn't use, but others might.
- **Ownership isolation is airtight.** A foreign entry id, a nonexistent one, and an id belonging to an already-removed entry all return the *identical* `404 NOT_FOUND` body — so an id can never be probed to learn whether it belongs to someone else.
- **Every form works without JavaScript.** Add, edit, and remove are all plain HTML forms (POST + redirect); removing an entry is a two-step `<details>` confirmation, no confirm dialog required. `profile.js` only adds a submit-button spinner.

See `.claude/specs/patient_profile_spec.md` for the full business-rule (`BL-01`…`BL-36`) and acceptance-criteria (`AC-01`…`AC-26`) numbering this feature was built against — it takes precedence over `docs/TDD.md` wherever the two disagree, since the TDD predates the reconciliation with the shared database schema.

---

## Configuration

Copy `.env.example` to `.env` and fill in your Supabase project's values (Project Settings → API and → Database in the Supabase dashboard).

| Variable | Default | Notes |
|---|---|---|
| `ENVIRONMENT` | `dev` | `dev` · `test` · `pilot` |
| `DATABASE_URL` | — | Supabase Postgres connection string (`postgresql+asyncpg://...`). Use the **session pooler** (port 5432) if the direct-connection host is IPv6-only and unreachable on your network. |
| `SUPABASE_URL` | — | e.g. `https://your-project-ref.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | — | Server-side only; used for `auth.admin.*` calls and never sent to templates/JS |
| `COOKIE_SECURE` | `true` | `.env` sets `false` for local http; forced `true` in pilot |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | |
| `DEFAULT_LOCALE` | `en` | `en` or `ur` |
| `OAUTH_ENABLED` | `false` | Master switch for "Continue with Google". Requires `SUPABASE_ANON_KEY` when `true` |
| `SUPABASE_ANON_KEY` | — | Project Settings → API → anon/public key. Safe to expose to a browser; used here only server-side for the PKCE token exchange |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:8000` | This app's absolute base URL — builds the OAuth `redirect_to` and is an allowed `Origin` for login/registration. Browse the app on this exact host. Must be `https://` in `pilot` |
| `OAUTH_PROVIDERS` | `google` | Comma-separated allow-list; only `google` is implemented |
| `OAUTH_STATE_TTL_S` | `600` | How long a pending Google sign-in stays valid |
| `PROFILE_WRITE_RATE_LIMIT_PER_MINUTE` | `30` | Patient-profile write requests (API + web) one patient may make per 60-second window |
| `PROFILE_MAX_ENTRIES_PER_LIST` | `50` | Maximum active allergies / conditions / medications per patient, per list |
| `CONSENT_WRITE_RATE_LIMIT_PER_MINUTE` | `10` | Grant/revoke requests one patient may make per 60-second window |
| `ACCESS_TOKEN_MINUTES` | `15` | Session cookie lifetime, and the maximum age of any access token the app accepts |
| `REFRESH_TOKEN_DAYS` | `14` | Refresh cookie lifetime |
| `MAX_FAILED_LOGINS` / `LOCKOUT_MINUTES` | `10` / `15` | Account lockout threshold and duration |
| `AUTH_RATE_LIMIT_PER_MINUTE` | `5` | Auth requests per client IP per minute (in-process — see Known deviations) |

No JWT secret to configure — access tokens are verified against the project's public JWKS, fetched from `{SUPABASE_URL}/auth/v1/.well-known/jwks.json`.

A startup guard stops the app rather than let it run unsafely: missing `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY` outside the `test` environment, `OAUTH_ENABLED=true` without `SUPABASE_ANON_KEY`, a non-`https` `PUBLIC_BASE_URL` in `pilot`, or any non-synthetic user row outside the `pilot` environment. All development and test data is synthetic by requirement — no real patient data is used at any point.

To turn Google sign-in on, three things need to line up:
1. `.env`: set `OAUTH_ENABLED=true` and `SUPABASE_ANON_KEY` to your project's real anon key.
2. Supabase dashboard → Authentication → Providers: enable **Google**, with its client ID/secret set.
3. Supabase dashboard → Authentication → URL Configuration → Redirect URLs: add `{PUBLIC_BASE_URL}/auth/callback` exactly (e.g. `http://127.0.0.1:8000/auth/callback` for local dev).

Then open the app on the `PUBLIC_BASE_URL` host. Remember that accounts registered with a password can't use the Google button (see [Google sign-in](#google-sign-in-oauth)).

---

## Development

```bash
uv run pytest -q                    # in-memory SQLite + a fake Supabase client, no network calls
uv run ruff check backend tests alembic     # lint
uv run ruff format backend tests alembic    # format
# Postgres-level tests for the Data-API guard migrations — LOCAL Postgres only (refuses any other host):
CURANODE_PG_TEST_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/postgres uv run pytest tests/test_pg_authority_guards.py
uv run alembic upgrade head         # apply migrations (additive only)
uv run alembic revision -m "..."    # new migration (hand-write it — see Known deviations)
```

The dev server reloads on changes to `backend/`, `frontend/templates/`, and `frontend/static/`.

Tests run against an in-memory SQLite database with `tests/fakes.py`'s `FakeSupabaseAuth` standing in for Supabase Auth (real JWTs, HS256-signed with a test-only secret — production verifies ES256 against Supabase's real JWKS instead). `tests/test_auth.py` covers the password API and security properties; `tests/test_web.py` covers the rendered pages, forms, role gating, and Urdu/RTL; `tests/test_oauth.py` covers the full Google sign-in flow (state binding, replay/CSRF rejection, onboarding, email collision, lockout/suspension) end-to-end against `FakeSupabaseAuth`'s PKCE code-exchange stand-in; `tests/test_oauth_primitives.py` and `tests/test_settings.py` cover the OAuth helper functions and config guards in isolation; `tests/test_profile.py` covers the patient profile end-to-end (one test per acceptance criterion, `AC-01`…`AC-25`) and `tests/test_profile_schemas.py` covers its validation helpers in isolation; `tests/test_consent.py` and `tests/test_passport_web.py` cover consent and the passport pages; `tests/test_auth_security.py` holds one regression test per audit finding. `tests/test_pg_authority_guards.py` runs the guard migrations' own SQL against a real Postgres with a Supabase-shaped stub (roles, `auth.uid()`, the live RLS policies) and is skipped unless `CURANODE_PG_TEST_URL` is set.

**A note on `.env` and the test suite:** `Settings` reads `.env` regardless of `ENVIRONMENT`, and `conftest.py` doesn't override every OAuth variable — so a developer's local `OAUTH_ENABLED=true` in `.env` is visible to the test process too. Any test asserting OAuth-disabled behaviour must `monkeypatch` `oauth_enabled` explicitly rather than relying on it being off by default (see `tests/test_oauth.py::test_t2_disabled_oauth_is_refused`).

---

## Documentation

| File | Contents |
|---|---|
| `docs/PRD.md` | Product requirements — `FR1`–`FR37`, `NFR1`–`NFR27` |
| `docs/TDD.md` | Technical design; v1.1 amends authentication |
| `docs/DESIGN.md` | Design system — tokens, layout, components, interaction patterns |
| `docs/app-foundation.md` | Implementation baseline and known documentation gaps |
| `docs/auth_hardening.md` | Authentication security fixes — Supabase dashboard settings, migration roll-out, session trade-offs, known limitations |
| `docs/prompts/oauth.md` | Google OAuth feature plan — architecture, hazards, file-by-file design |
| `docs/prompts/oauth-exec.md` | The phased execution prompt the OAuth implementation was built from |
| `.claude/specs/patient_profile_spec.md` | Patient profile (FR2) feature spec — functional/business rules (`BL-01`…`BL-36`), API contract, and test plan (`AC-01`…`AC-26`). Wins over `docs/TDD.md` where they disagree |
| `.claude/specs/SPEC_patient_profile_plan.md` | The execution plan the patient-profile implementation was built from |
| `.claude/specs/medical_passport_spec.md` | Medical Passport + consent (FR1/FR4) feature spec |

## Known deviations

Recorded rather than hidden.

- **The database schema is shared, not owned.** `user_profile` / `clinic` / `patient` / `doctor` / `clinic_staff` / `doctor_affiliation` / `allergy` / `chronic_condition` are pre-existing tables from the wider CuraNode-AI product's Supabase project — this repo's `db/models.py` maps onto them (different column and table names than the feature spec originally described) rather than creating its own. Only `audit_log` and `patient_medication` are owned outright here.
- **Alembic migrations are additive-only and hand-written**, not autogenerated — the shared tables above are never created or dropped by this repo's migrations, only extended with the columns each feature needs (`user_profile.failed_logins/locked_until/last_login_at/is_synthetic/full_name`, `doctor.verified_by`; the patient profile's migration adds `allergy.recorded_by/updated_at/removed_at` and `chronic_condition.recorded_by/recorded_at/updated_at`, plus the new `patient_medication` table with RLS policies). The security migrations `a3c1f0e7b2d4` (guard triggers + case-insensitive unique email) and `b7d2e9c4a1f6` (TRUNCATE revoked) touch shared tables and have **not** been applied; they need the shared-schema owners' sign-off first.
- **Supabase RLS is not this app's authorization boundary.** The app connects to Postgres as `postgres` with `BYPASSRLS`, so row-level security policies (including the ones this repo's own migration adds to `patient_medication`) protect Supabase's own Data API, not this backend — every query here scopes itself explicitly by `patient_id` in application code.
- **A patient-profile entry is never hard-deleted.** Allergies and medications get `removed_at` set; a chronic condition moves to `status='resolved'`. There's no "restore" or history view in this feature — see the patient-profile spec's "Out of scope" section.
- **The clinic-admin role's value is `"admin"`**, not `"clinic_admin"` — the shared schema's `user_profile_role_check` CHECK constraint only allows `patient`/`doctor`/`staff`/`admin`. `UserRole.CLINIC_ADMIN` (the Python enum member name) is unchanged; only its `.value` differs from the original feature spec.
- **Timing-indistinguishability between a wrong password and an unknown email is no longer guaranteed exactly** — password verification is now a network call to Supabase's GoTrue service, so this app controls response *shape* but not response *timing* the way a local constant-cost Argon2 check did.
- **An in-process cache** stands in for the Redis the TDD specifies, used for rate limits, OAuth state, and logout revocation markers (lockout lives in the database; refresh-token state in Supabase). Single-worker only: with several workers or after a restart these reset, and behind a reverse proxy the per-IP limit needs uvicorn's `--proxy-headers`/`--forwarded-allow-ips` or every client shares one bucket.
- **Registration reveals whether an email is registered** (an explicit "already exists" message, by product decision); only the per-IP rate limit bounds probing. Login does not reveal it.
- **Server-rendered Jinja templates** replace the Next.js frontend in the TDD. This keeps the project to one deployable and one language.
- **Doctor self-registration and email-without-OTP** amend `FR3` and TDD §3.3/§7.1. Both are recorded in the TDD v1.1 amendment note and need advisor sign-off.
- **Supabase's `/authorize` endpoint does not accept a caller-supplied `state` query param** the way a generic OAuth provider would — passing one causes Supabase's own state validation to fail with `bad_oauth_state` once Google redirects back. Discovered by testing against a live project; not documented anywhere obvious in Supabase's docs. This app's CSRF-binding state instead rides inside the `redirect_to` URL's own query string. See `backend/app/identity/oauth.py`'s `authorize_url` docstring.
- **OAuth is web-only.** `/api/v1/auth/*` has no OAuth equivalent — only the server-rendered login/register pages offer "Continue with Google".

## Not implemented

Password reset (the link renders disabled), email verification (deliberately — registration is active immediately), account linking (Google sign-in is refused for password-registered accounts, and there is no flow to connect the two), Apple/Microsoft/other OAuth providers, the clinic-administrator console, and the doctor-verification queue. A doctor or clinic-admin view of a patient's profile (FR21, gated by consent — `consent_grant`, `access_log`) is not built; a patient's profile page is visible only to that patient. There is no history/restore view for removed entries, no PDF export, and no linking of a current medication to the shared `medicine` catalogue or a prescription. Everything else in `docs/PRD.md` — appointments and queueing, document upload and OCR, the AI orchestrator and its four capabilities — is future work.
