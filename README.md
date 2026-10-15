# CuraNode-AI

A cross-hospital health management platform for Pakistan's private healthcare sector, built as a BS Data Science Final Year Project at PUCIT.

Patients in Pakistan carry their medical history on paper, and when the paper is lost the history is gone. CuraNode gives every patient one portable **Medical Passport** that they own and that any clinician they authorise can read instantly, from any participating facility.

> **Project status — authentication core, Google OAuth, Medical Passport, the patient profile, and medical documents implemented.**
> This repository currently implements **five** features: *Authentication & Role-Based Access* (password + Google OAuth), *OAuth Onboarding*, *Medical Passport (FR1)*, the *Patient Profile (FR2)*, and *Medical Documents (FR10–FR13)*. Registration, sign-in (password and Google), sessions, role gates, first-time-OAuth-user onboarding, passport number generation/display, and a patient's own basic details / allergies / chronic conditions / current medications (view, add, edit, soft-remove) are complete and tested. Medical documents — upload, local OCR, a review screen per document type, search, download and delete — are complete and tested too: see [How medical documents work](#how-medical-documents-work). Alongside these, other branches merged into `dev` add the consent / access-control work (P4, P9), the doctor dashboard (D4) and the doctor schedule (D7) — described in `CLAUDE.md`. Appointments and the AI orchestrator described in `docs/PRD.md` are **not built yet**.
>
> Identity is delegated to **Supabase Auth**, and the database is **Supabase Postgres** — shared with the wider CuraNode-AI product, so this repo's tables (`user_profile`, `clinic`, `patient`, `doctor`, `clinic_staff`, `doctor_affiliation`, `allergy`, `chronic_condition`) match that shared schema rather than inventing parallel ones. See [How authentication works](#how-authentication-works) and [How the patient profile works](#how-the-patient-profile-works) below.

---

## Quick start

Requires **Python ≥3.13**, [**uv**](https://docs.astral.sh/uv/), a **Supabase project** (Postgres + Auth), and — to read uploaded documents — a local **[Tesseract OCR](#installing-tesseract-ocr)** install for printed text (handwritten documents are not supported). OCR is entirely local: **no API key, no paid service, no document leaves the machine** (one optional exception, off unless you set a key: an AI reading that sends the OCR *text* — never the image — to a model provider; see *Optional AI reading* below). No Docker, no local database server, no Node.

```bash
uv sync                                                # install dependencies
cp .env.example .env                                   # fill in your Supabase project's values
uv run alembic upgrade head                            # apply the schema (additive only — see below)
uv run python backend/ops/scripts/seed_synthetic.py    # create demo accounts in Supabase Auth
uv run backend/app/main.py                              # start the app
```

Open **http://127.0.0.1:8000** — it redirects to the sign-in page. Starting the server needs one database connection (it tries a few times before giving up); if something does not behave, see [Troubleshooting](#troubleshooting).

| URL | What it is |
|---|---|
| `/en/login` · `/ur/login` | Sign in, English or Urdu |
| `/en/register` | Create a patient or doctor account |
| `/en/onboarding` | First-time Google sign-in users choose a role here |
| `/en/patient/profile` · `/ur/patient/profile` | A signed-in patient's own basic details, allergies, chronic conditions, and current medications |
| `/en/patient/documents` · `/ur/patient/documents` | Upload a prescription, lab report, vaccination record or other document; review, edit and confirm what was read |
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
    cache.py           TTL store for lockouts and rate limits
    log_config.py      structured logging with PII redaction
    db/                models (mapped onto the shared Supabase schema), async session + connection guards (idle check, connect retry)
    identity/          router · service · schemas · security · oauth (Supabase Auth + Google sign-in)
    audit/              append-only audit writer
    profile/            patient profile (FR2): schemas · service · router — basics, allergies, conditions, medications
    consent/            grants, the consent gateway and the access log (FR4/FR5, P4 + P9)
    doctor/             display rows for the doctor dashboard (D4)
    schedule/           doctor schedule (D7): pure overlap/availability engine, schemas, service
    documents/          medical documents (FR10–FR13): storage · ocr · extractors · sections · groups · service · router · optional AI layer — one pipeline for all four types; doctor_router (API-only doctor prescription)
    timeline/           medical history timeline (P3, FR8/FR9): schemas · events (pure) · sources · service · router — patient and consented doctor, through the consent gateway
    i18n/               en/ur message catalogues
    web/                server-rendered page routes and form handling (OAuth, onboarding, patient profile, documents) + the "database unreachable" 503 page
  ops/scripts/          synthetic seed data (creates Supabase Auth users + app rows); purge_cancelled_files.py
alembic/                 database migrations (additive-only — see Known deviations)
frontend/
  templates/            Jinja2 — base, auth/{login,register,onboarding,oauth_complete}, patient/profile, documents/{upload,list,detail,delete}, errors/unavailable, shell, partials
  static/css/            tokens.css (design tokens) + app.css
  static/js/             progressive enhancement only (auth.js, profile.js, documents.js)
tests/                   1,350+ tests, run against in-memory SQLite + a fake Supabase client
docs/                    PRD · TDD · DESIGN · app-foundation
.claude/specs/           patient_profile_spec.md — the source of truth for the patient profile feature
```

## Stack

| Layer | Choice |
|---|---|
| API & pages | FastAPI, server-rendered Jinja2 templates |
| Identity | **Supabase Auth** — password hashing, session issuance, refresh-token rotation, Google OAuth (server-side PKCE) |
| Persistence | SQLAlchemy 2.0 (async) over **Supabase Postgres**, migrated with Alembic |
| Sessions | Supabase access token (JWT, ES256/JWKS-verified) + refresh token, both in HttpOnly cookies |
| Styling | Hand-written CSS driven by the tokens in `docs/DESIGN.md` |
| Documents | **Tesseract** OCR (local, printed text only), Pillow + pypdfium2 for images and PDFs; optional Gemini / Groq / OpenRouter reading of the OCR *text* |
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

### Google sign-in (OAuth)

Off by default (`OAUTH_ENABLED=false`) — see [Configuration](#configuration) to turn it on. When enabled, "Continue with Google" appears on the login and register pages and runs a server-side PKCE flow entirely in `backend/app/identity/oauth.py` and the OAuth routes in `backend/app/web/router.py`; the browser never sees a token.

- **A caller-supplied `state` on Supabase's own `/authorize` call is a trap, not a pass-through slot.** The real `supabase-py` SDK never sends one (confirmed by reading `AsyncGoTrueClient._get_url_for_provider`) — GoTrue manages its own internal state for the round trip to the provider, and a client-supplied `state=` query param collides with that, coming back as `error_code=bad_oauth_state`. This app's own CSRF-binding state instead rides **inside** the `redirect_to` URL's own query string (`.../auth/callback?state=...`), which Supabase preserves verbatim and just appends `?code=...` to.
- **The state is single-use and browser-bound**, checked three ways on the callback: the query param (from `redirect_to`), an HttpOnly `cn_oauth_state` cookie set at the start of the flow, and a cache entry — all three must agree, and the cache entry is consumed before anything else happens.
- **The state cookie is `SameSite=Lax`, not `Strict`.** It has to survive the cross-site return hop from Google via Supabase. The session cookies (`cn_access`/`cn_refresh`) stay `Strict` as always; a same-site interstitial page (`auth/oauth_complete.html`) bounces the browser through one same-site request first so those `Strict` cookies aren't dropped on arrival.
- **A brand-new Google user always lands on `/{locale}/onboarding` first** — never on a dashboard. The Supabase trigger that auto-creates a `user_profile` row on any new auth user (password or OAuth) defaults its role to `patient`, but no `Patient`/`Doctor` row exists yet for an OAuth signup, so `deps.py`'s `Actor.onboarding_complete` is `False` and every dashboard route bounces back to onboarding until a role is chosen. `doctor.pmdc_number` is `UNIQUE NOT NULL` and a patient's passport number is permanent, so this choice is never made automatically.
- **An unverified Google email can't take over an existing password account.** If the email Google returns already belongs to a *different* `user_profile` row, the sign-in is refused and the session Supabase just issued is revoked — see `service.login_with_oauth`.
- **OAuth honours an existing lockout but never causes one** — there's no password on this path to brute-force.

### Endpoints

**JSON API** (`/api/v1`) — `POST /auth/register`, `POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout`, `GET /me`, `GET /clinics`. (OAuth is web-only; there's no JSON equivalent.) Patient profile: `GET|PATCH /me/profile`, `POST|PATCH|DELETE /me/allergies[/{id}]`, `POST|PATCH|DELETE /me/conditions[/{id}]`, `POST|PATCH|DELETE /me/medications[/{id}]` — all behind `PatientDep` and a per-patient write rate limit. Documents: `POST|GET /documents`, `GET /documents/in-progress`, `GET|DELETE /documents/{id}`, `POST /documents/{id}/process|confirm|cancel`, `GET /documents/{id}/file` (same guard and rate limit; the list endpoints take `q`, `type`, `status`, `date_from`, `date_to`, `limit`, `offset`), and `POST /doctor/prescriptions` (verified doctors, API only — see *Doctor-written prescription* below).

**Pages** — `GET|POST /{locale}/login`, `GET|POST /{locale}/register`, `POST /{locale}/logout`, `POST /{locale}/auth/oauth/google`, `GET /auth/callback` (not locale-prefixed — it's the fixed redirect URI registered with Supabase), `GET|POST /{locale}/onboarding`, the role-gated areas `GET /{locale}/patient`, `/{locale}/doctor`, `/{locale}/admin`, and `GET|POST /{locale}/patient/profile[/allergies|conditions|medications[/{id}[/remove]]]` for the patient profile, and `GET|POST /{locale}/patient/documents`, `GET /{locale}/patient/documents/new`, `GET /{locale}/patient/documents/{id}` plus its `/file`, `/preprocess`, `/process`, `/confirm`, `/cancel`, `/delete` and `/records` (copy a saved document into the health records again) actions for documents.

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

## How medical documents work

A patient uploads a **prescription, lab report, vaccination record, or other medical document** (PDF, JPG, JPEG or PNG, up to 10 MB) at `/{locale}/patient/documents`. All four types go through **one** pipeline; only the extraction step differs:

```
Upload -> temporary (uploaded) -> common OCR (processing) -> type-specific extraction
       -> review / edit (awaiting_review) -> Confirm & Save -> finalized record
```

- **Uploading never creates a record.** A document is `uploaded` / `processing` / `awaiting_review` / `failed` until the patient clicks **Confirm & Save**; only `finalized` documents appear in the record list and the Medical Timeline. Discarding one purges the stored file and everything read from it.
- **"Please upload a clearer photo".** When a photo or scan looks blurry, very faint, tiny or blank, the review page (and the failed page) say so, explain how to take a better one, and offer an upload of the same kind of document. It is advice only: the values can still be reviewed, corrected and saved. The thresholds were measured against how much of a page Tesseract still reads; nothing about the read itself changes.
- **Managing documents.** The list can be searched (file name or document type) and filtered by type, status and date (Pakistan days), all at once, with a one-click reset, and is paged (25 a page; the API takes `limit` / `offset` and returns `X-Total-Count`, and rejects a parameter it does not know instead of ignoring it). Any document's original can be opened or downloaded exactly as uploaded, a failed one can be retried from the list, and a document can be deleted after an explicit confirmation: the file and everything read from it are removed, a record that it was deleted stays in the audit log (no content), and anything it already added to the profile (an allergy) stays there. Deleting (and discarding an upload) only reports success once the stored file is confirmed gone — if the file store is down it fails and changes nothing, so no file is ever left behind. A record written by a clinician can never be deleted by the patient, and `DOCUMENT_ALLOW_DELETE_FINALIZED=false` makes every saved record permanent. `uv run python backend/ops/scripts/purge_cancelled_files.py` (add `--apply` to act) removes files an earlier version left behind.
- **What Confirm & Save writes.** What the patient confirmed stays in the document's own tables, and a copy is written to the health-record tables that fit it: a prescription becomes a `prescription` with its medicines, diagnoses become `diagnosis` rows, vital signs become one `vital_sign` row (blood pressure, pulse, temperature, SpO2, weight, height), and a lab report becomes a `lab_report` with its numeric results (a High / Low / Normal flag only when the report printed one itself). None of these invents a visit or a doctor: each says which document it came from, and deleting the document removes them. Each medicine has an "Add to my current medicines" tick, because a prescription is history, not what you are taking now. Allergies still join the profile as before. If the copy fails the document is still saved and a **Try again** button appears. The copying needs migration `c8e5a41f9b27` on the database (run by the schema owner); until then the app works exactly as before and nothing is copied, and it starts by itself once the migration is applied.
- **One service, two front doors.** `backend/app/documents/service.py` holds every rule; the JSON API (`/api/v1/documents`, `documents/router.py`) and the pages (`web/documents.py`) both call it. There is one processing endpoint (`POST /api/v1/documents/{id}/process`), not one per document type.
- **Reused shared tables.** Metadata and state live in the shared `document` table (`doc_type`: `rx` / `lab` / `vaccine` / `other`); the OCR run in `ocr_job`; each proposed value in `ocr_extraction`, which keeps the engine's `extracted_value` and the patient's `corrected_value` side by side. The original file lives in the existing private `medical-documents` Storage bucket and is only ever served back through an ownership-checked route.
- **What is read, per document type.** Extraction is deterministic (regular expressions over the OCR text — an uploaded document is data, never instructions) and each type has its own layout for review and for the saved record. A **prescription**: medicines (medicine / dosage / frequency / duration), advice, diagnosis, symptoms, vital signs, allergies. A **lab report**: laboratory / hospital / patient / referring-doctor details, the test results (test / result / unit / reference range, each with a **High / Low / Normal** flag taken from the report's own marker or worked out from the stated range — never stored), and notes. A **vaccination card**: the facility, the patient, and every dose (vaccine, dose, date given, batch, site, next due). **Other** documents: whatever details were found, the document's own headed passages (findings, conclusion, procedure...), and the prescription tables that have content. Only what the reader found is shown. Every value is a proposal: the patient can edit it, remove it or add a row, and each field carries a certainty badge so uncertain readings stand out. A medicine name that nearly matches a known drug gets a *suggestion* beside it, never a silent correction.
- **Allergies reach the patient's profile only on Confirm & Save.** An allergy the patient kept is added to their profile's allergy list by the same service the profile page uses (so a duplicate or a full list is skipped, the document is saved regardless, and the entry stays editable by the patient). The document's audit entry records a count, never what the allergies are.
- **OCR is completely local, free and keyless** — no API key, no paid or external OCR service, and the document itself (file and pixels) never leaves the machine. **Only printed documents are read**, by **Tesseract** (`ocr_job.engine = 'tesseract'`); handwritten documents are deliberately not supported, and the upload form says so. (The `document.ocr_mode` column belongs to the shared schema and still exists; it is always written as `printed`, and a document an earlier version had marked handwritten is simply read as printed when it is retried.) There are no separate prescription/lab OCR paths: one pipeline serves every type. JPG / JPEG / PNG are decoded with Pillow; a PDF is rendered to page images locally with pypdfium2 (no external PDF tool) and every page is read, under a page cap (`OCR_MAX_PAGES`, default 5 — a longer PDF is refused, not truncated) and a pixel cap, with the work kept off the web server's event loop. OCR output is only ever a proposal: it goes through the review/edit screen and needs an explicit **Confirm & Save**. If Tesseract is unavailable (not installed, or a language pack missing) the document lands in `failed` with a specific message and can be retried.
- **Optional AI reading — the one place text leaves the machine.** Off unless you set a key (`GEMINI_API_KEY`, `GROQ_API_KEY` and/or `OPENROUTER_API_KEY`). When on, the OCR *text* (never the image) is sent to the first provider in `AI_PROVIDER_ORDER` that answers — Gemini, then Groq, then OpenRouter, so one provider's rate limit or outage just moves on to the next — to help read the medicines and the document's sections. Nothing it says is trusted: every answer goes through the same deterministic checks (a value is kept only if the OCR text itself evidences it), and everything still goes through the review screen and an explicit **Confirm & Save**. With no key, or if every provider fails, the deterministic reading is used, so an upload never fails because of this layer. Use synthetic data while on free tiers. `AI_SECTIONS_ENABLED=false` switches off the second (sections) call alone to save quota.
- **Ownership** always comes from the session. Every query filters by the caller's own `patient_id`, and a foreign or missing document id is the same `404`. The file's real type (PDF/JPEG/PNG) is checked from its bytes and the size from the bytes read, server-side. Audit rows record counts only — never OCR text or values.
- **Doctor-written prescription (API only).** `POST /api/v1/doctor/prescriptions` lets a *verified* doctor submit already-structured medicines for a patient. There is no OCR and no patient review: the result is finalized in the same call, marked as written by the doctor (so the patient can read it but never delete it), and read back through the same patient endpoints. No screen uses it yet. **Known gap:** it checks that the patient exists but not consent or a doctor–patient relationship, so any verified doctor can write to any patient id — wire in the consent gateway before a screen calls it.
- **How long an upload takes.** One request does it all: store the file, read it (OCR itself is about 2 s), the optional AI reading, and save the result — about 14 database round trips. On a slow route to a far-away Supabase region each round trip is about a second, and the AI reading is the largest single wait (4–12 s, and 20 s+ if a provider times out and the next one answers). `AI_PROVIDER_ORDER` (put the fastest provider first) and `AI_SECTIONS_ENABLED=false` shorten it.

The migration `c3a91d7e5f20` is additive (new columns, including `document.ocr_mode`) and *relaxes* three shared CHECK constraints (`document.doc_type` gains `vaccine`; `ocr_extraction.field_name` gains the lab fields; `ocr_job.engine` gains `trocr`) — review it, then `uv run alembic upgrade head`.

The migration `e3a52c8d7f14` is additive too: it widens `ocr_extraction`'s `field_name` CHECK (8 names -> 28, a strict superset; no table or column is added) so that, besides the medicines and lab results, a patient can review and edit a document's **diagnosis, symptoms, vital signs, allergies and vaccine doses** — each a table of rows reviewed in the same step. On Confirm & Save, any allergies the patient kept are added to their profile's allergy list (duplicates and a full list are skipped, and the document is saved regardless). Review it, then `uv run alembic upgrade head`.

Two more small, additive migrations belong to this feature: `35c4bc13fa37` lets `ocr_job.engine` hold `'doctor_entry'` (for the doctor-written prescription), and `b7e24c19a053` adds one nullable JSONB column, `ocr_job.ai_sections`, for the optional AI reading of a document's sections.

The shared Supabase database this project uses is already at `e3a52c8d7f14`, so `alembic upgrade head` changes nothing there — but all four migration files must be in the repository, or `alembic` stops with "Can't locate revision". `uv run alembic heads` should always show exactly one head.

## How the medical timeline works

`/{locale}/patient/timeline` is the patient's whole record as one chronological list (FR8) — visits, prescriptions, lab reports, diagnoses, documented conditions, allergies, medicines they take, vaccination records — newest first, with the **All / Consultations / Labs / Prescriptions / Vaccinations / Other Medical Documents** tabs, a **search** box, and filters for date range, hospital / clinic and doctor (FR9). Treatments / procedures are not a tab: the data model has no such record. Each card has its date, what it is, who wrote it and a **View details** page; every control is a plain link or GET form, and the filters live in the address.

- **Placed by when it happened, never when it was uploaded.** A visit sits at its visit time, a prescription at its issue date, a document at the date the document itself states (Pakistan days). A condition or allergy with no date of its own sits on the day it was recorded and says so; a document that states no date is listed last, apart, with its upload day.
- **Says who vouches for it.** Every event carries a label: *Doctor-confirmed*, *Recorded by a clinician*, *Entered by the patient*, *Source not recorded*, or, for a document the patient uploaded, read, reviewed and saved, *✓ Reviewed & confirmed* on their own page (a doctor sees *AI-read · patient-confirmed*: a program read the paper and the patient saved it — real, but not a clinician's statement). A model-written visit summary is labelled and kept apart from what the doctor wrote.
- **An upload is not history.** Only saved (finalized) documents appear, and from a patient's own upload only the rows they confirmed. An upload that is being read, waits for review, or failed is listed apart, in "Documents in progress", with its status and a link to review it — and nothing read from it. A saved document reaches the timeline by itself, in the category of its type; deleting it removes it again. Nothing missing is filled in: an empty field is not shown.
- **One door for patient and doctor.** `backend/app/timeline/` (`events.py` is pure; `sources.py` is the only reader of the visit / prescription tables; `service.py` goes through `consent/gateway.py`). A verified doctor reaches a patient's timeline from the patient page that the passport lookup leads to (the QR code holds only the passport number), and sees only the sections the patient shared — each read written to the access log — with the same 404 for every refusal. Vaccination records and other documents are in no consent section, so they are the patient's alone, and a doctor is never given a link to a stored file.
- **JSON API:** `GET /api/v1/me/timeline` (filters `type`, `date_from`, `date_to`, `facility`, `doctor`; paged with `limit` / `offset`; a parameter it does not know is refused) and `GET /api/v1/me/timeline/{kind}/{id}`; a verified doctor uses `GET /api/v1/doctor/patients/{patient_id}/timeline[/{kind}/{id}]`.
- **Search and dates.** The search looks at what the reader can read in an event — medicines, diagnoses, tests and results, doctor, hospital, notes and, for the patient only, file names — all words must match, any case. Date ranges are inclusive Pakistan calendar days over the event's clinical date; an event with no date is left out by a range, never given one. A search or range that is not valid is refused (API) or explained beside the field (page).
- **One real thing, one event.** A document a `lab_report` row points at is that lab event; a document that became a prescription (`ocr_extraction.item_id`) is listed on that prescription, not beside it; a document attached to a visit (`document.encounter_id`) and the visit show each other. Links come from the schema, never from similar names.
- **Speed.** A timeline load is 9 database queries for the patient (17 for a doctor, whose extra ones are the consent checks and the access log) however long the history is, pinned by a test. Deleting a document tells the patient on the confirmation page that its timeline entry goes too, and that allergies already added to the profile stay.
- **No migration.** The timeline reads the existing shared `encounter`, `diagnosis`, `prescription`, `prescription_item`, `medicine` and `lab_report` tables (mapped read-only) and the saved `document` rows; the doctor-side tables are empty until the doctor features write them.

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
| `PUBLIC_BASE_URL` | `http://127.0.0.1:8000` | This app's absolute base URL — builds the OAuth `redirect_to`. Must be `https://` in `pilot` |
| `OAUTH_PROVIDERS` | `google` | Comma-separated allow-list; only `google` is implemented |
| `OAUTH_STATE_TTL_S` | `600` | How long a pending Google sign-in stays valid |
| `PROFILE_WRITE_RATE_LIMIT_PER_MINUTE` | `30` | Patient-profile write requests (API + web) one patient may make per 60-second window |
| `PROFILE_MAX_ENTRIES_PER_LIST` | `50` | Maximum active allergies / conditions / medications per patient, per list |
| `TESSERACT_CMD` | *(auto)* | Path to the Tesseract executable. Empty = look on `PATH`, then the Windows installer's default location |
| `TESSERACT_LANG` | `eng` | Tesseract language pack(s), e.g. `eng+urd` (the pack must be installed) |
| `OCR_TIMEOUT_S` | `60` | Per-page Tesseract timeout, in seconds |
| `OCR_PDF_DPI` / `OCR_MAX_PAGES` / `OCR_MAX_PIXELS` | `300` / `5` / `50000000` | PDF render resolution, longest PDF read, and the largest image decoded |
| `DATABASE_CONNECT_TIMEOUT_S` | `25` | How long to wait for ONE attempt at a NEW database connection (asyncpg's own default is 60 s); a blip — a failed DNS lookup, a stalled handshake, a reset — is tried again a couple of times. When the database still cannot be reached the app answers 503 "try again" instead of hanging or a bare 500 |
| `DATABASE_IDLE_PING_S` | `15` | A pooled connection that sat idle longer than this is tested before it is used, and replaced if the pooler or a router dropped it meanwhile (otherwise the first request after a pause fails). `0` = never test |
| `DOCUMENT_BUCKET` | `medical-documents` | Private Supabase Storage bucket for the original files |
| `DOCUMENT_MAX_BYTES` | `10485760` | Upload limit (10 MB), checked server-side |
| `DOCUMENT_WRITE_RATE_LIMIT_PER_MINUTE` | `20` | Upload / process / confirm / cancel / delete requests one patient may make per minute (OCR is a paid call) |
| `DOCUMENT_ALLOW_DELETE_FINALIZED` | `true` | Whether a patient may delete a saved record they uploaded themselves (a clinician's record never can be); `false` keeps every saved record permanently |
| `DOCUMENT_PROCESSING_STALE_S` | `120` | How long a document may sit in `processing` before it counts as stuck and can be processed again |
| `GEMINI_API_KEY` / `GROQ_API_KEY` / `OPENROUTER_API_KEY` | *(empty = off)* | Optional AI reading of the OCR text (see above). Each provider is used only if its key is set |
| `GEMINI_MODEL` / `GROQ_MODEL` / `OPENROUTER_MODEL` | see `.env.example` | Model ids; free-tier ids rotate, so a provider that answers 404 just needs its id updated (the chain skips it meanwhile) |
| `AI_PROVIDER_ORDER` | `gemini,groq,openrouter` | Which providers to try, in order |
| `GEMINI_TIMEOUT_S` | `20` | Timeout of Gemini's own call (the fallback providers use `AI_PROVIDER_TIMEOUT_S`) |
| `AI_PROVIDER_TIMEOUT_S` / `AI_PROVIDER_COOLDOWN_S` / `AI_TOTAL_TIMEOUT_S` | `20` / `60` / `45` | Per-call timeout, how long a rate-limited provider is skipped, and the ceiling for the whole chain |
| `AI_SECTIONS_ENABLED` | `true` | `false` switches off the second AI call (the sections reading) to save quota; the medicines reading is unaffected |

No JWT secret to configure — access tokens are verified against the project's public JWKS, fetched from `{SUPABASE_URL}/auth/v1/.well-known/jwks.json`.

A startup guard stops the app rather than let it run unsafely: missing `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY` outside the `test` environment, `OAUTH_ENABLED=true` without `SUPABASE_ANON_KEY`, a non-`https` `PUBLIC_BASE_URL` in `pilot`, or any non-synthetic user row outside the `pilot` environment. All development and test data is synthetic by requirement — no real patient data is used at any point.

To turn Google sign-in on, three things need to line up:
1. `.env`: set `OAUTH_ENABLED=true` and `SUPABASE_ANON_KEY` to your project's real anon key.
2. Supabase dashboard → Authentication → Providers: enable **Google**, with its client ID/secret set.
3. Supabase dashboard → Authentication → URL Configuration → Redirect URLs: add `{PUBLIC_BASE_URL}/auth/callback` exactly (e.g. `http://127.0.0.1:8000/auth/callback` for local dev).

---

## Development

```bash
uv run pytest -q                    # 1,350+ tests — in-memory SQLite + a fake Supabase client, no network calls
uv run ruff check backend tests     # lint
uv run ruff format backend tests    # format
uv run alembic upgrade head         # apply migrations (additive only)
uv run alembic revision -m "..."    # new migration (hand-write it — see Known deviations)
```

The dev server reloads on changes to `backend/`, `frontend/templates/`, and `frontend/static/`.

Tests run against an in-memory SQLite database with `tests/fakes.py`'s `FakeSupabaseAuth` standing in for Supabase Auth (real JWTs, HS256-signed with a test-only secret — production verifies ES256 against Supabase's real JWKS instead). `tests/test_auth.py` covers the password API and security properties; `tests/test_web.py` covers the rendered pages, forms, role gating, and Urdu/RTL; `tests/test_oauth.py` covers the full Google sign-in flow (state binding, replay/CSRF rejection, onboarding, email collision, lockout/suspension) end-to-end against `FakeSupabaseAuth`'s PKCE code-exchange stand-in; `tests/test_oauth_primitives.py` and `tests/test_settings.py` cover the OAuth helper functions and config guards in isolation; `tests/test_profile.py` covers the patient profile end-to-end (one test per acceptance criterion, `AC-01`…`AC-25`) and `tests/test_profile_schemas.py` covers its validation helpers in isolation. Documents are covered by `tests/test_documents.py` (the whole pipeline, management, quality advice, delete), the extraction and OCR files (`test_document_*.py`, `test_vitals_and_vaccines.py`, `test_lab_flags.py`, `test_groups_and_proposals.py`), the AI layer against simulated providers (`test_ai_*.py`, never the network), and database behaviour by `test_db_resilience.py`, `test_db_connection_guards.py` and `test_upload_roundtrips.py` (which pins how many database round trips an upload makes). The few PostgreSQL-only statements (case-insensitive search with an escape character, `AT TIME ZONE 'Asia/Karachi'`, the delete row lock) were also checked once against a real PostgreSQL 16, since the suite itself is SQLite. A test that needs "today" must use Pakistan time (UTC+5), not UTC — two tests that did not failed between midnight and 05:00 in Pakistan.

**A note on `.env` and the test suite:** `Settings` reads `.env` regardless of `ENVIRONMENT`, and `conftest.py` doesn't override every OAuth variable — so a developer's local `OAUTH_ENABLED=true` in `.env` is visible to the test process too. Any test asserting OAuth-disabled behaviour must `monkeypatch` `oauth_enabled` explicitly rather than relying on it being off by default (see `tests/test_oauth.py::test_t2_disabled_oauth_is_refused`).

---

### Installing Tesseract OCR

**Printed** documents are read with Tesseract, installed **outside** Python (`uv sync` installs only the thin `pytesseract` wrapper):

| OS | Install |
|---|---|
| Windows | The [UB Mannheim installer](https://github.com/UB-Mannheim/tesseract/wiki) (default location `C:\Program Files\Tesseract-OCR`, found automatically), or `choco install tesseract` |
| macOS | `brew install tesseract` |
| Linux | `sudo apt install tesseract-ocr` |

Confirm with `tesseract --version` (or set `TESSERACT_CMD` if it is not on `PATH`). English data (`eng`) ships with it; add Urdu with the `urd` language pack and `TESSERACT_LANG=eng+urd`. Tests do not need it: the OCR boundary tests run everywhere, and the few that use the real binary are skipped when it is missing.

## Troubleshooting

**"This page couldn't load" (HTTP 503).** The database could not be reached — a network blip, a failed DNS lookup, or the pooler dropped a connection. The app already retries opening a connection and replaces one that sat idle, so this page appears only when that did not work. Reload; if it keeps coming, check the network (VPN, hotspot), try a public DNS (1.1.1.1 or 8.8.8.8), and check `DATABASE_URL` (use the **session pooler** if the direct host is IPv6-only). The page's *Reference* is the request id: the server log line `database_unavailable` carries the same `request_id`, plus the kind of failure.

**The server stops with "Application startup failed".** Starting needs one database connection and tries four times; this means the database stayed unreachable. Same checks as above, then start again.

**A page keeps loading and the server log shows no request.** Two copies of the server may be running: Windows lets a second one bind port 8000 without an error, and the browser reaches the old one. Check for more than one listener, and stop the older one (Ctrl+C in its terminal, or `Stop-Process -Id <pid>`):

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen
```

**Uploads feel slow.** See *How long an upload takes* above. The AI reading is usually the biggest wait: set `AI_PROVIDER_ORDER` with the fastest provider first, or `AI_SECTIONS_ENABLED=false`. Most of the rest is database round trips, which depend on how far you are from the Supabase region.

**A document lands in "failed".** The page says why. "Automatic reading isn't available" means Tesseract (or a language pack) is missing — see [Installing Tesseract OCR](#installing-tesseract-ocr); a storage message means the file store could not be reached, and **Try again** works once it can.

## Documentation

| File | Contents |
|---|---|
| `docs/PRD.md` | Product requirements — `FR1`–`FR37`, `NFR1`–`NFR27` |
| `docs/TDD.md` | Technical design; v1.1 amends authentication |
| `docs/DESIGN.md` | Design system — tokens, layout, components, interaction patterns |
| `docs/app-foundation.md` | Implementation baseline and known documentation gaps |
| `.claude/specs/patient_profile_spec.md` | Patient profile (FR2) feature spec — functional/business rules (`BL-01`…`BL-36`), API contract, and test plan (`AC-01`…`AC-26`). Wins over `docs/TDD.md` where they disagree |
| `.claude/specs/SPEC_patient_profile_plan.md` | The execution plan the patient-profile implementation was built from |

## Known deviations

Recorded rather than hidden.

- **The database schema is shared, not owned.** `user_profile` / `clinic` / `patient` / `doctor` / `clinic_staff` / `doctor_affiliation` / `allergy` / `chronic_condition` are pre-existing tables from the wider CuraNode-AI product's Supabase project — this repo's `db/models.py` maps onto them (different column and table names than the feature spec originally described) rather than creating its own. Only `audit_log` and `patient_medication` are owned outright here.
- **Alembic migrations are additive-only and hand-written**, not autogenerated — the shared tables above are never created or dropped by this repo's migrations, only extended with the columns each feature needs (`user_profile.failed_logins/locked_until/last_login_at/is_synthetic/full_name`, `doctor.verified_by`; the patient profile's migration adds `allergy.recorded_by/updated_at/removed_at` and `chronic_condition.recorded_by/recorded_at/updated_at`, plus the new `patient_medication` table with RLS policies).
- **Supabase RLS is not this app's authorization boundary.** The app connects to Postgres as `postgres` with `BYPASSRLS`, so row-level security policies (including the ones this repo's own migration adds to `patient_medication`) protect Supabase's own Data API, not this backend — every query here scopes itself explicitly by `patient_id` in application code.
- **A patient-profile entry is never hard-deleted.** Allergies and medications get `removed_at` set; a chronic condition moves to `status='resolved'`. There's no "restore" or history view in this feature — see the patient-profile spec's "Out of scope" section.
- **The clinic-admin role's value is `"admin"`**, not `"clinic_admin"` — the shared schema's `user_profile_role_check` CHECK constraint only allows `patient`/`doctor`/`staff`/`admin`. `UserRole.CLINIC_ADMIN` (the Python enum member name) is unchanged; only its `.value` differs from the original feature spec.
- **Timing-indistinguishability between a wrong password and an unknown email is no longer guaranteed exactly** — password verification is now a network call to Supabase's GoTrue service, so this app controls response *shape* but not response *timing* the way a local constant-cost Argon2 check did.
- **An in-process cache** stands in for the Redis the TDD specifies, used now only for rate limiting and lockout counters (refresh-token state moved to Supabase). Single-worker only.
- **Server-rendered Jinja templates** replace the Next.js frontend in the TDD. This keeps the project to one deployable and one language.
- **Doctor self-registration and email-without-OTP** amend `FR3` and TDD §3.3/§7.1. Both are recorded in the TDD v1.1 amendment note and need advisor sign-off.
- **Supabase's `/authorize` endpoint does not accept a caller-supplied `state` query param** the way a generic OAuth provider would — passing one causes Supabase's own state validation to fail with `bad_oauth_state` once Google redirects back. Discovered by testing against a live project; not documented anywhere obvious in Supabase's docs. This app's CSRF-binding state instead rides inside the `redirect_to` URL's own query string. See `backend/app/identity/oauth.py`'s `authorize_url` docstring.
- **OAuth is web-only.** `/api/v1/auth/*` has no OAuth equivalent — only the server-rendered login/register pages offer "Continue with Google".
- **Dates are Pakistan days.** Timestamps are stored in UTC, but the upload day the app shows, orders by and filters on is the UTC+5 calendar day (Pakistan has no daylight saving, so a fixed offset is exact). A deployment in another time zone would need this to become a setting.
- **A deleted or discarded document leaves an empty `cancelled` row** — no file, text, file name or values — so the audit trail's entry still points at something.
- **Connection handling is tuned for a slow, far-away database** — an idle pooled connection is tested only after `DATABASE_IDLE_PING_S` rather than on every use (`pool_pre_ping`, which costs a round trip per request), and opening a connection is retried for network blips but never for a wrong password.

## Not implemented

Password reset (the link renders disabled), account linking (a Google identity cannot be attached to an existing password account, or vice versa), Apple/Microsoft/other OAuth providers, the clinic-administrator console, the doctor-verification queue, and email verification. A doctor or clinic-admin view of a patient's profile (FR21, gated by consent — `consent_grant`, `access_log`) is not built; a patient's profile page is visible only to that patient. There is no history/restore view for removed entries, no PDF export, and no linking of a current medication to the shared `medicine` catalogue or a prescription. Documents: OCR is local only — Tesseract for printed text, with no cloud or vision-model engine; handwritten documents are not supported; accuracy has not been measured against the PRD's labelled test set (the extraction rules and the photo-quality thresholds were tuned on synthetic scans and a handful of samples); the Urdu wording of the newer messages has not been reviewed by a native speaker; a finalized document is not written into the doctor-owned `prescription` / `lab_report` tables (those need an encounter and prescribing doctor a patient upload does not have); the doctor-written prescription endpoint has no screen and no consent check yet; and the timeline's doctor-side sources (`encounter`, `diagnosis`, `prescription`, `prescription_item`) have no screen that writes them yet, so until the doctor features exist the timeline is built from saved documents and the profile entries alone (an AI interpretation of lab results — `ai_interpretation` — is not read either; that waits for the AI features). Everything else in `docs/PRD.md` — appointments and queueing, the AI orchestrator and its four capabilities — is future work.
