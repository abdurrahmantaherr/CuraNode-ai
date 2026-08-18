# CuraNode-AI

A cross-hospital health management platform for Pakistan's private healthcare sector, built as a BS Data Science Final Year Project at PUCIT.

Patients in Pakistan carry their medical history on paper, and when the paper is lost the history is gone. CuraNode gives every patient one portable **Medical Passport** that they own and that any clinician they authorise can read instantly, from any participating facility.

> **Project status — authentication core and Medical Passport implemented.**
> This repository currently implements **two** features: *Authentication & Role-Based Access* and *Medical Passport (FR1)*. Registration, sign-in, sessions, role gates, and passport number generation/display are complete and tested. Appointments, document OCR, and the AI orchestrator described in `docs/PRD.md` are **not built yet**.

---

## Quick start

Requires **Python ≥3.13** and [**uv**](https://docs.astral.sh/uv/). Nothing else — no Docker, no database server, no Node.

```bash
uv sync                                        # install dependencies
uv run python backend/ops/scripts/seed_synthetic.py   # create the database + demo accounts
uv run backend/app/main.py                     # start the app
```

Open **http://127.0.0.1:8000** — it redirects to the sign-in page.

| URL | What it is |
|---|---|
| `/en/login` · `/ur/login` | Sign in, English or Urdu |
| `/en/register` | Create a patient or doctor account |
| `/docs` | OpenAPI reference for the JSON API |
| `/healthz` | Health check |

### Demo accounts

Seeded by the script above. Password for all four: **`CuraNode!2026`**

| Email | Role | Notes |
|---|---|---|
| `ayesha.raza@example.com` | Patient | Has a Medical Passport number |
| `adnan.haleem@example.com` | Doctor | Verified — full doctor access |
| `nadia.iqbal@example.com` | Doctor | **Unverified** — blocked from all clinical routes; defaults to Urdu |
| `front.desk@example.com` | Clinic admin | Lands on a placeholder |

To verify the pending doctor and watch access appear on the *next request* without re-login:

```bash
uv run python -c "import sqlite3; c=sqlite3.connect('curanode.db'); c.execute(\"update doctors set is_verified=1, verified_by=user_id, verified_at=datetime('now') where pmdc_number='55871'\"); c.commit()"
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
    cache.py           TTL store for sessions, lockouts, rate limits
    log_config.py      structured logging with PII redaction
    db/                models, async session, UUIDv7
    identity/          router · service · schemas · security
    audit/             append-only audit writer
    i18n/              en/ur message catalogues
    web/               server-rendered page routes and form handling
  ops/scripts/         synthetic seed data
frontend/
  templates/           Jinja2 — base, auth/login, auth/register, shell, partials
  static/css/          tokens.css (design tokens) + app.css
  static/js/           progressive enhancement only
tests/                 48 tests
docs/                  PRD · TDD · DESIGN · app-foundation
.claude/specs/         feature spec and implementation plan
```

## Stack

| Layer | Choice |
|---|---|
| API & pages | FastAPI, server-rendered Jinja2 templates |
| Persistence | SQLAlchemy 2.0 (async) over SQLite |
| Passwords | Argon2id (`time_cost=3, memory_cost=65536, parallelism=4`) |
| Sessions | JWT access token (15 min) + rotating refresh token (14 days), both in cookies |
| Styling | Hand-written CSS driven by the tokens in `docs/DESIGN.md` |
| Languages | English and Urdu, with right-to-left layout |

---

## How authentication works

Registration takes a name, email, and password. There is no OTP and no email-verification step — an account is active immediately and the user is signed straight in.

**A patient or a doctor may self-register.** Choosing *Doctor* creates a real doctor account but always with `is_verified = false`, so the account grants **zero** access to any patient record until a clinic administrator verifies it. Creating a doctor *account* is never the same as granting doctor *access*.

Properties worth knowing, each covered by a test:

- **The role toggle is enforced.** Signing in as *Doctor* on a patient account is refused with `403` and no session. The check runs after the password is verified, so it can only be reached by someone who already owns the account and is never an account-enumeration oracle.
- **Failures are indistinguishable.** A wrong password, an unknown email, and a suspended account return byte-identical `401`s, and take the same time — unknown emails still pay the full Argon2 cost.
- **Registration is not an oracle either.** A duplicate email returns a success-shaped `201` and creates nothing.
- **Verification is never cached in a token.** The access token deliberately carries no `is_verified` claim; it is read from the database per request, so revoking a doctor takes effect on their next call.
- **Refresh tokens are single-use.** Replaying a spent token is treated as theft and revokes the entire token family.
- **Accounts lock** for 15 minutes after 10 consecutive failures, and attempts during a lock do not extend it.
- **Nothing sensitive reaches the logs** — passwords, tokens, emails, phone numbers, and names are dropped by a redaction processor at every level, including `DEBUG`.

Authorisation is enforced on the backend through four dependencies in `deps.py` — `ActorDep`, `PatientDep`, `VerifiedDoctorDep`, `ClinicAdminDep`. No endpoint performs an ad-hoc role check.

### Endpoints

**JSON API** (`/api/v1`) — `POST /auth/register`, `POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout`, `GET /me`, `GET /clinics`

**Pages** — `GET|POST /{locale}/login`, `GET|POST /{locale}/register`, `POST /{locale}/logout`, and the role-gated areas `GET /{locale}/patient`, `/{locale}/doctor`, `/{locale}/admin`

Locale is a path prefix, so switching language is a route change that keeps you on the same page.

---

## Configuration

Copy `.env.example` to `.env`. Defaults suit local development.

| Variable | Default | Notes |
|---|---|---|
| `ENVIRONMENT` | `dev` | `dev` · `test` · `pilot` |
| `DATABASE_URL` | `sqlite+aiosqlite:///./curanode.db` | Accepts a `postgresql+psycopg://` URL unchanged |
| `JWT_SECRET` | dev placeholder | **Must be ≥32 bytes** or the app refuses to start |
| `COOKIE_SECURE` | `true` | `.env` sets `false` for local http; forced `true` in pilot |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | |
| `DEFAULT_LOCALE` | `en` | `en` or `ur` |

Two startup guards will stop the app rather than let it run unsafely: a short or missing `JWT_SECRET`, and any non-synthetic user row outside the `pilot` environment. All development and test data is synthetic by requirement — no real patient data is used at any point.

---

## Development

```bash
uv run pytest -q                    # 48 tests
uv run ruff check backend tests     # lint
uv run ruff format backend tests    # format
rm curanode.db                      # reset the database, then re-seed
```

The dev server reloads on changes to `backend/`, `frontend/templates/`, and `frontend/static/`.

Tests run against an in-memory SQLite database with a fresh cache per test, and **no test makes a network call**. `tests/test_auth.py` covers the API and security properties; `tests/test_web.py` covers the rendered pages, forms, role gating, and Urdu/RTL.

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

Recorded rather than hidden. Each is a deliberate choice for a project that has to run on a student laptop.

- **SQLite and an in-process cache** stand in for the PostgreSQL 18 and Redis 8 that `docs/TDD.md` specifies. Both sit behind the same interfaces, so switching is a URL change plus a Redis-backed `Cache` implementation. The in-process cache is single-worker only.
- **Server-rendered Jinja templates** replace the Next.js frontend in the TDD. This keeps the project to one deployable and one language.
- **No Alembic migrations yet.** SQLite creates its schema at startup. Migrations are needed before PostgreSQL.
- **Doctor self-registration and email-without-OTP** amend `FR3` and TDD §3.3/§7.1. Both are recorded in the TDD v1.1 amendment note and need advisor sign-off.

## Not implemented

Password reset (the link renders disabled), the clinic-administrator console, the doctor-verification queue, and email verification. Everything else in `docs/PRD.md` — appointments and queueing, document upload and OCR, the AI orchestrator and its four capabilities — is future work.
