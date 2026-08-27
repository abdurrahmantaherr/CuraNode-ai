# CuraNode-AI

A cross-hospital health management platform for Pakistan's private healthcare sector, built as a BS Data Science Final Year Project at PUCIT.

Patients in Pakistan carry their medical history on paper, and when the paper is lost the history is gone. CuraNode gives every patient one portable **Medical Passport** that they own and that any clinician they authorise can read instantly, from any participating facility.

> **Project status — authentication core and Medical Passport implemented.**
> This repository currently implements **two** features: *Authentication & Role-Based Access* and *Medical Passport (FR1)*. Registration, sign-in, sessions, role gates, and passport number generation/display are complete and tested. Appointments, document OCR, and the AI orchestrator described in `docs/PRD.md` are **not built yet**.
>
> Identity is delegated to **Supabase Auth**, and the database is **Supabase Postgres** — shared with the wider CuraNode-AI product, so this repo's tables (`user_profile`, `clinic`, `patient`, `doctor`, `clinic_staff`, `doctor_affiliation`) match that shared schema rather than inventing parallel ones. See [How authentication works](#how-authentication-works) below.

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

Open **http://127.0.0.1:8000** — it redirects to the sign-in page.

| URL | What it is |
|---|---|
| `/en/login` · `/ur/login` | Sign in, English or Urdu |
| `/en/register` | Create a patient or doctor account |
| `/docs` | OpenAPI reference for the JSON API |
| `/healthz` | Health check |

### Demo accounts

Seeded by the script above — created as real Supabase Auth users, visible in your project's Dashboard → Authentication. Password for all four: **`CuraNode!2026`**

| Email | Role | Notes |
|---|---|---|
| `ayesha.raza@example.com` | Patient | Has a Medical Passport number |
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
    cache.py           TTL store for lockouts and rate limits
    log_config.py      structured logging with PII redaction
    db/                models (mapped onto the shared Supabase schema), async session
    identity/          router · service · schemas · security (Supabase Auth integration)
    audit/              append-only audit writer
    i18n/               en/ur message catalogues
    web/                server-rendered page routes and form handling
  ops/scripts/          synthetic seed data (creates Supabase Auth users + app rows)
alembic/                 database migrations (additive-only — see Known deviations)
frontend/
  templates/            Jinja2 — base, auth/login, auth/register, shell, partials
  static/css/            tokens.css (design tokens) + app.css
  static/js/             progressive enhancement only
tests/                   46 tests, run against in-memory SQLite + a fake Supabase client
docs/                    PRD · TDD · DESIGN · app-foundation
.claude/specs/            feature spec and implementation plan
```

## Stack

| Layer | Choice |
|---|---|
| API & pages | FastAPI, server-rendered Jinja2 templates |
| Identity | **Supabase Auth** — password hashing, session issuance, refresh-token rotation |
| Persistence | SQLAlchemy 2.0 (async) over **Supabase Postgres**, migrated with Alembic |
| Sessions | Supabase access token (JWT, ES256/JWKS-verified) + refresh token, both in HttpOnly cookies |
| Styling | Hand-written CSS driven by the tokens in `docs/DESIGN.md` |
| Languages | English and Urdu, with right-to-left layout |

---

## How authentication works

Registration takes a name, email, and password. There is no OTP and no email-verification step — an account is active immediately and the user is signed straight in.

**Credentials and sessions live entirely in Supabase Auth** — this codebase never hashes a password or signs a token itself. `backend/app/identity/security.py` talks to Supabase's Auth API server-side only (the service-role key is never exposed to templates or JS), and verifies access tokens locally against the project's public JWKS. Two Supabase clients matter here and must stay separate: a persistent one for `auth.admin.*` calls, and a **fresh, single-use client per sign-in/refresh** — the SDK's client is stateful (`sign_in_with_password` mutates its session and starts a background auto-refresh timer), so sharing one instance across users or between a sign-in and a later admin call corrupts it.

**A patient or a doctor may self-register.** Choosing *Doctor* creates a real doctor account but always with `verification_status = 'pending'`, so the account grants **zero** access to any patient record until a clinic administrator verifies it. Creating a doctor *account* is never the same as granting doctor *access*.

Properties worth knowing, each covered by a test:

- **The role toggle is enforced.** Signing in as *Doctor* on a patient account is refused with `403` and no session. The check runs after the password is verified, so it can only be reached by someone who already owns the account and is never an account-enumeration oracle.
- **Failures are shape-identical.** A wrong password, an unknown email, and a suspended account return byte-identical `401`s. (Exact *timing* parity — the property Argon2's constant cost gave for free — is no longer this app's to guarantee once password verification is a network call to Supabase; see `tests/test_auth.py`'s note on this.)
- **Registration is not an oracle either.** A duplicate email returns a success-shaped `201` and creates nothing — Supabase is never even called on that path.
- **Verification is never cached in a token.** The access token deliberately carries only a user id; role and verification status are read from the database per request, so revoking a doctor takes effect on their next call.
- **Refresh tokens are single-use**, rotated and reuse-detected by Supabase Auth itself.
- **Lockout stays app-level and independent of Supabase**: accounts lock for 15 minutes after 10 consecutive failures (checked *before* Supabase is ever called), and attempts during a lock do not extend it.
- **Nothing sensitive reaches the logs** — passwords, tokens, emails, phone numbers, and names are dropped by a redaction processor at every level, including `DEBUG`.
- **A Supabase trigger (`on_auth_user_created`) auto-inserts a default `user_profile` row** the instant an auth user is created — registration *updates* that row rather than inserting a second one.

Authorisation is enforced on the backend through four dependencies in `deps.py` — `ActorDep`, `PatientDep`, `VerifiedDoctorDep`, `ClinicAdminDep`. No endpoint performs an ad-hoc role check.

### Endpoints

**JSON API** (`/api/v1`) — `POST /auth/register`, `POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout`, `GET /me`, `GET /clinics`

**Pages** — `GET|POST /{locale}/login`, `GET|POST /{locale}/register`, `POST /{locale}/logout`, and the role-gated areas `GET /{locale}/patient`, `/{locale}/doctor`, `/{locale}/admin`

Locale is a path prefix, so switching language is a route change that keeps you on the same page.

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

No JWT secret to configure — access tokens are verified against the project's public JWKS, fetched from `{SUPABASE_URL}/auth/v1/.well-known/jwks.json`.

A startup guard stops the app rather than let it run unsafely: missing `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY` outside the `test` environment, or any non-synthetic user row outside the `pilot` environment. All development and test data is synthetic by requirement — no real patient data is used at any point.

---

## Development

```bash
uv run pytest -q                    # 46 tests — in-memory SQLite + a fake Supabase client, no network calls
uv run ruff check backend tests     # lint
uv run ruff format backend tests    # format
uv run alembic upgrade head         # apply migrations (additive only)
uv run alembic revision -m "..."    # new migration (hand-write it — see Known deviations)
```

The dev server reloads on changes to `backend/`, `frontend/templates/`, and `frontend/static/`.

Tests run against an in-memory SQLite database with `tests/fakes.py`'s `FakeSupabaseAuth` standing in for Supabase Auth (real JWTs, HS256-signed with a test-only secret — production verifies ES256 against Supabase's real JWKS instead). `tests/test_auth.py` covers the API and security properties; `tests/test_web.py` covers the rendered pages, forms, role gating, and Urdu/RTL.

---

## Documentation

| File | Contents |
|---|---|
| `docs/PRD.md` | Product requirements — `FR1`–`FR37`, `NFR1`–`NFR27` |
| `docs/TDD.md` | Technical design; v1.1 amends authentication |
| `docs/DESIGN.md` | Design system — tokens, layout, components, interaction patterns |
| `docs/app-foundation.md` | Implementation baseline and known documentation gaps |
| `.claude/specs/SPEC_authentication.md` | This feature's specification |
| `.claude/specs/PLAN_authentication.md` | The implementation plan it was built from |

## Known deviations

Recorded rather than hidden.

- **The database schema is shared, not owned.** `user_profile` / `clinic` / `patient` / `doctor` / `clinic_staff` / `doctor_affiliation` are pre-existing tables from the wider CuraNode-AI product's Supabase project — this repo's `db/models.py` maps onto them (different column and table names than the feature spec originally described) rather than creating its own. Only `audit_log` is owned outright here.
- **Alembic migrations are additive-only and hand-written**, not autogenerated — the shared tables above are never created or dropped by this repo's migration, only extended with the columns this feature needs (`user_profile.failed_logins/locked_until/last_login_at/is_synthetic/full_name`, `doctor.verified_by`).
- **The clinic-admin role's value is `"admin"`**, not `"clinic_admin"` — the shared schema's `user_profile_role_check` CHECK constraint only allows `patient`/`doctor`/`staff`/`admin`. `UserRole.CLINIC_ADMIN` (the Python enum member name) is unchanged; only its `.value` differs from the original feature spec.
- **Timing-indistinguishability between a wrong password and an unknown email is no longer guaranteed exactly** — password verification is now a network call to Supabase's GoTrue service, so this app controls response *shape* but not response *timing* the way a local constant-cost Argon2 check did.
- **An in-process cache** stands in for the Redis the TDD specifies, used now only for rate limiting and lockout counters (refresh-token state moved to Supabase). Single-worker only.
- **Server-rendered Jinja templates** replace the Next.js frontend in the TDD. This keeps the project to one deployable and one language.
- **Doctor self-registration and email-without-OTP** amend `FR3` and TDD §3.3/§7.1. Both are recorded in the TDD v1.1 amendment note and need advisor sign-off.

## Not implemented

Password reset (the link renders disabled), the clinic-administrator console, the doctor-verification queue, and email verification. Everything else in `docs/PRD.md` — appointments and queueing, document upload and OCR, the AI orchestrator and its four capabilities — is future work.
