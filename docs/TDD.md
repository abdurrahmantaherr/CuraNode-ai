# CuraNode-AI — Technical Design Document

| | |
|---|---|
| **Document** | TDD.md |
| **Version** | 1.1 (authentication amendment) |
| **Implements** | PRD.md v1.0 (FR1–FR37, NFR1–NFR27, decisions D1–D2) |
| **Status** | Draft for team and advisor review |
| **Supersedes** | v1.0 on §3.3, §4.4, §7.1, §7.8, §9.1, §9.5 only. All other sections are unchanged. |

> **Amendment note — v1.1, authentication.** Three changes were made on the project owner's decision. Each is a deviation from v1.0 and each **requires advisor sign-off** (Appendix B items 6 and 7).
>
> 1. **Registration OTP removed** (§7.1). There is no account-verification step; an account is `active` on creation. No replacement email verification was added.
> 2. **Email is the primary credential** (§3.3, §4.4). `users.email` becomes `UNIQUE NOT NULL`; `phone_e164` becomes optional. This inverts v1.0.
> 3. **Doctors may self-register** (§4.4). They always land `is_verified=false` and can read no patient record until a clinic admin verifies them, so `FR3`'s binding clause is preserved.
>
> **PRD.md is unaffected** — it specifies only that "a patient can create an account" (`FR1`) and never mandated a channel or a verification step.

---

> **Note for implementers (human or AI).**
> This document makes the technical decisions the PRD deliberately left open. Where the PRD and this document conflict, **the PRD wins on behaviour and this document wins on implementation** — raise the conflict rather than resolving it silently.
>
> **Where this document is silent, stop and ask.** Do not invent a table, an endpoint, or a third-party service that is not named here.
>
> Requirement IDs from the PRD (FR*, NFR*, D*) are referenced throughout. Every module, table, and endpoint below traces to at least one. See Appendix A for the full traceability matrix.

---

## 1. System Architecture

### 1.1 Architectural style

**A modular monolith, not microservices.** One deployable backend application, internally divided into modules with enforced boundaries, plus one frontend application and one asynchronous worker.

This is a deliberate choice against the instinct to build a "microservice per agent". Four students working for eighteen weeks cannot afford distributed tracing, inter-service contracts, and independent deployment pipelines, and NFR5 caps the pilot at 50 concurrent users and 1,000 patients — a scale a single process handles comfortably. Module boundaries are enforced in code (Section 5.4) so that any module *could* be extracted later, satisfying NFR6 without paying the cost now.

### 1.2 Runtime components

| Component | Responsibility | Scaling unit |
|---|---|---|
| **`web`** | Next.js frontend, server-rendered + PWA. All three role interfaces. | Stateless, horizontally scalable |
| **`api`** | FastAPI backend. All business logic, consent enforcement, AI orchestration. | Stateless, horizontally scalable |
| **`worker`** | Celery worker. Document extraction, embeddings, translation, notifications. | Horizontally scalable by queue |
| **`beat`** | Celery beat. Scheduled jobs (appointment reminders, queue ETA recompute). | Exactly one instance |
| **`db`** | PostgreSQL 18. Single source of truth, including vector index. | Single instance in pilot |
| **`cache`** | Redis 8. Celery broker, queue-position cache, rate limiting, sessions. | Single instance in pilot |
| **`objects`** | MinIO. Uploaded documents, generated PDFs, translated files. | Single instance in pilot |
| **`proxy`** | Nginx. TLS termination, reverse proxy, static assets, upload size limits. | Single instance in pilot |

### 1.3 Component diagram

```
                          ┌─────────────────────────┐
   Patient browser  ──┐   │   proxy (Nginx)         │
   Doctor browser   ──┼──▶│   TLS, routing, limits  │
   Admin browser    ──┘   └───────────┬─────────────┘
                                      │
                    ┌─────────────────┴─────────────────┐
                    ▼                                   ▼
          ┌───────────────────┐               ┌───────────────────┐
          │  web (Next.js)    │──── REST ────▶│   api (FastAPI)   │
          │  SSR + PWA shell  │               │                   │
          └───────────────────┘               │  ┌─────────────┐  │
                                              │  │  identity   │  │
                                              │  │  consent ★  │  │
                                              │  │  records    │  │
                                              │  │  scheduling │  │
                                              │  │  clinical   │  │
                                              │  │  orchestr.  │  │
                                              │  │  audit      │  │
                                              │  └─────────────┘  │
                                              └───┬───────┬───────┘
                                                  │       │
                          ┌───────────────────────┘       └──────────────┐
                          ▼                                              ▼
                 ┌─────────────────┐                          ┌────────────────────┐
                 │  cache (Redis)  │◀────── task queue ───────│  worker (Celery)   │
                 └─────────────────┘                          │  extraction        │
                          │                                   │  embedding         │
                          ▼                                   │  translation       │
                 ┌─────────────────┐   ┌─────────────────┐    │  notification      │
                 │   db (Postgres) │   │ objects (MinIO) │    └─────────┬──────────┘
                 │   + pgvector    │   │  documents      │              │
                 └─────────────────┘   └─────────────────┘              ▼
                                                             ┌────────────────────┐
                                                             │ External providers │
                                                             │ LLM / vision, SMS  │
                                                             └────────────────────┘

★ consent is a mandatory chokepoint — see §7.3
```

### 1.4 Three architectural rules

These are load-bearing. Breaking any of them breaks a PRD requirement.

**Rule 1 — Every clinical read passes through the consent gateway.**
No module issues a `SELECT` against `clinical_records` directly. All reads go through `consent.gateway.load_records_for_actor()` (§4.3), which resolves the actor's grants, filters rows, and writes the audit entry in the same transaction. This is how FR4, FR5, FR21, NFR16, NFR17, and D2 are enforced in one place rather than twenty.

**Rule 2 — Clinical records are append-only.**
`clinical_records` accepts `INSERT` only. The application database role has `UPDATE` and `DELETE` revoked on that table. Corrections are new rows pointing at the row they supersede (D1). This makes the audit trail structurally true rather than a matter of developer discipline.

**Rule 3 — The model never decides a clinical fact.**
Every AI feature computes its facts deterministically in code, then uses the language model *only* to phrase them. "What changed" (FR22) is a SQL diff that the model narrates. Lab interpretation (FR30) flags out-of-range values against a stored reference table, and the model explains the already-flagged values. Extraction (FR11) proposes, and a human confirms (FR12). The model is a translator between structured data and readable prose — never a source of truth. This is how FR31 is satisfied architecturally instead of by prompt instruction alone.

---

## 2. Tech Stack

### 2.1 Pinned versions

Versions below were current as of **August 2026**. Exact patch versions are pinned in `uv.lock` and `pnpm-lock.yaml` at Sprint 0 and are not upgraded mid-sprint.

#### Backend

| Component | Version | Why this one |
|---|---|---|
| Python | **3.13.15** | Not 3.14, despite 3.14 being current. Several data-science and OCR libraries still lag a major release; 3.13 is supported to 2029 and removes a whole class of dependency friction. |
| FastAPI | **`>=0.118,<1.0`** | Async-native, generates OpenAPI automatically, which the frontend consumes for typed clients. |
| Uvicorn | **`>=0.35,<1.0`** | ASGI server, run behind Nginx. |
| Pydantic | **`>=2.11,<3.0`** | Request/response validation and settings. All DTOs in §4 are Pydantic models. |
| SQLAlchemy | **`>=2.0.40,<2.1`** | Async ORM, 2.0 typed style only. No legacy Query API. |
| Alembic | **`>=1.16,<2.0`** | Migrations. Every schema change is a migration; no manual DDL. |
| Celery | **`>=5.5,<6.0`** | Background jobs. Required by NFR3 — extraction must not block a request. |
| psycopg | **`>=3.2,<4.0`** | Postgres driver (v3, not psycopg2). |
| pgvector (Python) | **`>=0.4,<1.0`** | Vector column type for RAG retrieval. |
| Argon2-cffi | **`>=25.1`** | Password hashing. Not bcrypt. |
| PyJWT | **`>=2.10,<3.0`** | Access/refresh tokens. |
| structlog | **`>=25.1`** | Structured JSON logging with PII redaction (§7.8). |
| pytest / pytest-asyncio | **`>=8.4`** / **`>=1.0`** | Test framework. |
| Ruff | **`>=0.12`** | Lint + format. Replaces black, isort, flake8. |
| uv | **`>=0.8`** | Dependency resolution and locking. Not pip/poetry. |

#### Frontend

| Component | Version | Why this one |
|---|---|---|
| Node.js | **24.x LTS** | Active LTS. Not 26 — that is Current until October 2026 and is not a base for a project being marked. |
| Next.js | **16.3.x** | App Router, server components, built-in PWA-compatible output. Turbopack is default and stable. |
| React | **19.2** | Ships with Next 16. |
| TypeScript | **`>=5.9,<6`** | `strict: true`, no exceptions. |
| Tailwind CSS | **4.x** | Utility styling with first-class logical properties — needed for RTL (§2.3). |
| next-intl | **`>=4.3`** | Urdu/English routing, message catalogues, locale-aware formatting (FR28). |
| TanStack Query | **`>=5.90`** | Server state, retry, polling for queue position (FR17). |
| Zod | **`>=4.0`** | Runtime validation of API responses at the boundary. |
| Playwright | **`>=1.55`** | End-to-end tests across all three roles. |
| Vitest | **`>=3.2`** | Unit tests. |
| pnpm | **`>=10`** | Package manager. |

#### Infrastructure

| Component | Version | Why this one |
|---|---|---|
| PostgreSQL | **18.4** | Not 19 — it is in beta until roughly October 2026 and must not be used. |
| pgvector | **`>=0.8`** | Postgres extension. Avoids a separate vector database entirely. |
| Redis | **8.x** | Broker + cache. |
| MinIO | **latest stable** | S3-compatible object storage, runs in Compose, no cloud account required. |
| Nginx | **1.28.x** | Reverse proxy. |
| Docker Engine | **`>=27`** | NFR24. |
| Docker Compose | **v2 spec** | Single `compose.yaml` plus per-environment overlays. |

### 2.2 Deliberately excluded

- **A separate vector database** (Pinecone, Qdrant, Chroma). pgvector inside the existing Postgres is sufficient at 1,000 patients and removes a container, a backup target, and a consistency problem.
- **Kubernetes.** Compose meets NFR24 and NFR25. Kubernetes would consume a sprint and deliver nothing the pilot needs.
- **A separate agent framework** (LangChain, LangGraph, CrewAI). The orchestrator routes to exactly four capabilities (FR35). That is a dictionary lookup and a try/except, not a framework. Adding one buys abstraction the project will never use and makes failure modes harder to explain to a panel.
- **GraphQL.** REST + OpenAPI generates typed clients for free.
- **Microservices.** See §1.1.
- **A native mobile app.** Explicitly out of scope in PRD §6.2.

### 2.3 Right-to-left support

Urdu RTL (NFR13) is a Sprint 1 architectural constraint, not a Sprint 8 styling task. Two rules from the first component onward:

1. **Logical CSS properties only.** `ms-4` / `me-4` / `ps-2` / `text-start`, never `ml-4` / `pl-2` / `text-left`. Enforced by a lint rule that fails CI on physical direction utilities.
2. **Direction from the locale.** `<html dir>` is set from the active locale in the root layout. No component reads or hardcodes direction.

Every component's Playwright test runs in both locales. A component that has not been seen in Urdu is not done.

---

## 3. Data Model

### 3.1 Conventions

- Primary keys are `UUID v7` (time-ordered, index-friendly), generated in the application.
- All timestamps are `TIMESTAMPTZ`, stored in UTC. Display timezone is `Asia/Karachi`, applied in the frontend only.
- Money and physical measurements use `NUMERIC`, never `FLOAT`.
- Soft delete does not exist for clinical data (Rule 2). Non-clinical tables use `deleted_at TIMESTAMPTZ NULL`.
- Every table has `created_at`; mutable tables also have `updated_at`.

### 3.2 Enumerated types

```sql
CREATE TYPE user_role       AS ENUM ('patient', 'doctor', 'clinic_admin');
CREATE TYPE account_status  AS ENUM ('pending_verification', 'active', 'suspended');
CREATE TYPE locale_code     AS ENUM ('en', 'ur');
CREATE TYPE grantee_type    AS ENUM ('doctor', 'clinic');
CREATE TYPE record_type     AS ENUM (
    'visit', 'diagnosis', 'prescription', 'lab_report',
    'allergy', 'condition', 'medication', 'note'
);
CREATE TYPE record_source   AS ENUM ('doctor_entry', 'patient_upload', 'patient_self_report');
CREATE TYPE document_status AS ENUM ('uploaded', 'scanning', 'extracting', 'awaiting_review', 'confirmed', 'rejected', 'failed');
CREATE TYPE appt_status     AS ENUM ('booked', 'rescheduled', 'cancelled_by_patient', 'cancelled_by_clinic', 'completed', 'no_show');
CREATE TYPE queue_state     AS ENUM ('expected', 'checked_in', 'called', 'in_consultation', 'completed', 'left');
CREATE TYPE ai_capability   AS ENUM ('extraction', 'retrieval', 'translation', 'conversation');
CREATE TYPE ai_outcome      AS ENUM ('success', 'refused', 'failed', 'timeout', 'circuit_open');
```

`ai_capability` has exactly four values and this is enforced at the type level, because FR35 says exactly four. Adding a fifth requires a migration, a PRD amendment, and advisor sign-off — which is the point.

### 3.3 Identity and organisation

```sql
CREATE TABLE users (
    id                UUID PRIMARY KEY,
    email             CITEXT UNIQUE NOT NULL,         -- primary credential
    phone_e164        TEXT UNIQUE,                    -- optional, +923001234567
    password_hash     TEXT NOT NULL,                  -- argon2id
    role              user_role NOT NULL,
    status            account_status NOT NULL DEFAULT 'pending_verification',
    preferred_locale  locale_code NOT NULL DEFAULT 'en',
    full_name         TEXT NOT NULL,
    last_login_at     TIMESTAMPTZ,
    failed_logins     SMALLINT NOT NULL DEFAULT 0,
    locked_until      TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_users_role_status ON users (role, status);

CREATE TABLE clinics (
    id           UUID PRIMARY KEY,
    name         TEXT NOT NULL,
    city         TEXT NOT NULL,                       -- Lahore | Karachi | Islamabad
    address      TEXT NOT NULL,
    phone_e164   TEXT,
    timezone     TEXT NOT NULL DEFAULT 'Asia/Karachi',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE patients (                               -- FR1: the Medical Passport
    user_id        UUID PRIMARY KEY REFERENCES users(id),
    passport_no    TEXT UNIQUE NOT NULL,              -- human-readable, e.g. CN-7K2M-4RQX
    date_of_birth  DATE,
    gender         TEXT,
    blood_group    TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`passport_no` is issued once and never changes, satisfying "stays with them permanently" in FR1. It is deliberately not the primary key: it is shown to humans, and human-facing identifiers should never be foreign keys.

```sql
CREATE TABLE doctors (                                -- FR3
    user_id       UUID PRIMARY KEY REFERENCES users(id),
    primary_clinic_id UUID NOT NULL REFERENCES clinics(id),
    specialty     TEXT NOT NULL,
    pmdc_number   TEXT,                               -- registration number, recorded not validated
    is_verified   BOOLEAN NOT NULL DEFAULT FALSE,
    verified_by   UUID REFERENCES users(id),
    verified_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT verified_has_verifier
        CHECK (is_verified = FALSE OR (verified_by IS NOT NULL AND verified_at IS NOT NULL))
);
CREATE INDEX idx_doctors_search ON doctors (primary_clinic_id, specialty) WHERE is_verified;

CREATE TABLE clinic_staff (
    user_id    UUID NOT NULL REFERENCES users(id),
    clinic_id  UUID NOT NULL REFERENCES clinics(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, clinic_id)
);
```

The `verified_has_verifier` constraint makes FR3's "cannot access until verified" impossible to bypass by setting a boolean — verification always names a responsible human.

### 3.4 Consent

```sql
CREATE TABLE consent_grants (                         -- FR4, NFR16, D2
    id            UUID PRIMARY KEY,
    patient_id    UUID NOT NULL REFERENCES patients(user_id),
    grantee_type  grantee_type NOT NULL,
    grantee_id    UUID NOT NULL,                      -- doctors.user_id OR clinics.id
    granted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ,                        -- NULL = until revoked
    revoked_at    TIMESTAMPTZ,
    revoked_by    UUID REFERENCES users(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_consent_active ON consent_grants (patient_id, grantee_type, grantee_id)
    WHERE revoked_at IS NULL;
CREATE UNIQUE INDEX uq_consent_one_active ON consent_grants (patient_id, grantee_type, grantee_id)
    WHERE revoked_at IS NULL;
```

Grants are all-or-nothing over the patient's record. Partial scoping (share prescriptions but not lab reports) is **not** implemented — it multiplies the consent gateway's complexity and the PRD does not require it. Revocation sets `revoked_at`; grant rows are never deleted, so the history of who had access when survives.

### 3.5 Clinical records — append-only

```sql
CREATE TABLE clinical_records (                       -- FR8, FR23, D1
    id               UUID PRIMARY KEY,
    patient_id       UUID NOT NULL REFERENCES patients(user_id),
    record_type      record_type NOT NULL,
    source           record_source NOT NULL,
    occurred_at      TIMESTAMPTZ NOT NULL,            -- when the clinical event happened
    recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    author_user_id   UUID NOT NULL REFERENCES users(id),
    clinic_id        UUID REFERENCES clinics(id),     -- NULL for patient self-report
    visit_id         UUID REFERENCES clinical_records(id),
    document_id      UUID REFERENCES documents(id),
    payload          JSONB NOT NULL,
    supersedes_id    UUID REFERENCES clinical_records(id),
    superseded_at    TIMESTAMPTZ,                     -- set on the OLD row by trigger, see below
    confirmed_by     UUID REFERENCES users(id),       -- FR12: NULL is not permitted for AI-sourced
    confirmed_at     TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ai_sourced_must_be_confirmed
        CHECK (document_id IS NULL OR (confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL))
);
CREATE INDEX idx_records_timeline ON clinical_records (patient_id, occurred_at DESC);
CREATE INDEX idx_records_type     ON clinical_records (patient_id, record_type, occurred_at DESC);
CREATE INDEX idx_records_clinic   ON clinical_records (clinic_id, occurred_at DESC);
CREATE INDEX idx_records_current  ON clinical_records (patient_id, record_type)
    WHERE superseded_at IS NULL;
```

`ai_sourced_must_be_confirmed` is the database-level enforcement of FR12. A row that came from a document cannot exist without a named human confirmer. This is the single most important constraint in the schema: it makes "nothing extracted is stored as fact without human confirmation" a property of the data, not a promise in a code review.

`superseded_at` is the one permitted exception to Rule 2, applied by a trigger that may set only that column and only from NULL:

```sql
CREATE OR REPLACE FUNCTION mark_superseded() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.supersedes_id IS NOT NULL THEN
        UPDATE clinical_records
           SET superseded_at = NEW.recorded_at
         WHERE id = NEW.supersedes_id AND superseded_at IS NULL;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_mark_superseded AFTER INSERT ON clinical_records
    FOR EACH ROW EXECUTE FUNCTION mark_superseded();
```

**Payload shapes by `record_type`** (validated by Pydantic before insert, and by a JSON Schema `CHECK` in migration `0007`):

```jsonc
// prescription
{ "medicines": [ { "name": "Metformin", "strength": "500mg", "frequency": "BD",
                   "duration_days": 30, "instructions": "after meals" } ] }
// lab_report
{ "panel": "CBC",
  "results": [ { "analyte": "Haemoglobin", "value": 10.2, "unit": "g/dL",
                 "ref_low": 13.0, "ref_high": 17.0, "flag": "low" } ] }
// diagnosis
{ "text": "Type 2 diabetes mellitus", "icd10": null, "certainty": "confirmed" }
// visit
{ "complaint": "...", "examination": "...", "notes": "..." }
// allergy | condition | medication
{ "name": "Penicillin", "severity": "severe", "onset": "2019", "notes": "..." }
```

`ref_low` / `ref_high` / `flag` are populated by the deterministic reference-range check (§6.5), never by the model.

### 3.6 Documents and extraction

```sql
CREATE TABLE documents (                              -- FR10
    id            UUID PRIMARY KEY,
    patient_id    UUID NOT NULL REFERENCES patients(user_id),
    uploaded_by   UUID NOT NULL REFERENCES users(id),
    object_key    TEXT NOT NULL,                      -- MinIO key
    mime_type     TEXT NOT NULL,
    size_bytes    BIGINT NOT NULL,
    sha256        TEXT NOT NULL,
    page_count    SMALLINT,
    status        document_status NOT NULL DEFAULT 'uploaded',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (patient_id, sha256)                       -- idempotent re-upload, NFR8
);

CREATE TABLE extractions (                            -- FR11, FR13
    id                UUID PRIMARY KEY,
    document_id       UUID NOT NULL REFERENCES documents(id),
    ai_request_id     UUID REFERENCES ai_requests(id),
    engine            TEXT NOT NULL,                  -- 'tesseract' | 'vision-llm'
    engine_version    TEXT NOT NULL,
    proposed_type     record_type NOT NULL,
    proposed_payload  JSONB NOT NULL,
    field_confidence  JSONB NOT NULL,                 -- {"medicines[0].name": 0.94, ...}
    overall_confidence NUMERIC(4,3) NOT NULL,
    status            document_status NOT NULL,
    error_code        TEXT,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at      TIMESTAMPTZ
);
CREATE INDEX idx_extractions_doc ON extractions (document_id, started_at DESC);

CREATE TABLE extraction_reviews (                     -- FR12
    id             UUID PRIMARY KEY,
    extraction_id  UUID NOT NULL REFERENCES extractions(id),
    reviewed_by    UUID NOT NULL REFERENCES users(id),
    accepted       BOOLEAN NOT NULL,
    final_payload  JSONB NOT NULL,
    fields_changed TEXT[] NOT NULL DEFAULT '{}',      -- feeds the accuracy metric, PRD §7.2
    created_record_id UUID REFERENCES clinical_records(id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`fields_changed` exists solely to produce the PRD's "corrections required per extracted document ≤ 2" metric without a manual labelling exercise. Every correction a patient makes is a free data point.

### 3.7 Scheduling and queue

```sql
CREATE TABLE doctor_schedules (                       -- FR18
    id            UUID PRIMARY KEY,
    doctor_id     UUID NOT NULL REFERENCES doctors(user_id),
    clinic_id     UUID NOT NULL REFERENCES clinics(id),
    weekday       SMALLINT NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    start_time    TIME NOT NULL,
    end_time      TIME NOT NULL,
    slot_minutes  SMALLINT NOT NULL CHECK (slot_minutes BETWEEN 5 AND 60),
    valid_from    DATE NOT NULL,
    valid_to      DATE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (end_time > start_time)
);

CREATE TABLE schedule_exceptions (                    -- FR18: days off
    id          UUID PRIMARY KEY,
    doctor_id   UUID NOT NULL REFERENCES doctors(user_id),
    on_date     DATE NOT NULL,
    is_available BOOLEAN NOT NULL DEFAULT FALSE,
    start_time  TIME,
    end_time    TIME,
    reason      TEXT,
    UNIQUE (doctor_id, on_date)
);

CREATE TABLE appointments (                           -- FR15, FR16
    id               UUID PRIMARY KEY,
    patient_id       UUID NOT NULL REFERENCES patients(user_id),
    doctor_id        UUID NOT NULL REFERENCES doctors(user_id),
    clinic_id        UUID NOT NULL REFERENCES clinics(id),
    scheduled_start  TIMESTAMPTZ NOT NULL,
    scheduled_end    TIMESTAMPTZ NOT NULL,
    status           appt_status NOT NULL DEFAULT 'booked',
    booked_by        UUID NOT NULL REFERENCES users(id),   -- self-booking metric, PRD §7.1
    rescheduled_from UUID REFERENCES appointments(id),
    cancel_reason    TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_appt_slot ON appointments (doctor_id, scheduled_start)
    WHERE status IN ('booked', 'rescheduled');
CREATE INDEX idx_appt_patient ON appointments (patient_id, scheduled_start DESC);
CREATE INDEX idx_appt_doctor_day ON appointments (doctor_id, scheduled_start);
```

`uq_appt_slot` is the double-booking guard. It is a partial unique index rather than application-level checking because two patients tapping "book" simultaneously is a real race, and the database is the only place that can settle it.

```sql
CREATE TABLE queue_entries (                          -- FR17, FR19
    id              UUID PRIMARY KEY,
    appointment_id  UUID UNIQUE REFERENCES appointments(id),
    doctor_id       UUID NOT NULL REFERENCES doctors(user_id),
    clinic_id       UUID NOT NULL REFERENCES clinics(id),
    service_date    DATE NOT NULL,
    position        INTEGER NOT NULL,
    state           queue_state NOT NULL DEFAULT 'expected',
    priority_flag   BOOLEAN NOT NULL DEFAULT FALSE,   -- FR26, suggestion only
    priority_reason TEXT,
    checked_in_at   TIMESTAMPTZ,
    called_at       TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    reordered_by    UUID REFERENCES users(id),        -- FR19: who moved this, for accountability
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_queue_position ON queue_entries (doctor_id, service_date, position)
    WHERE state NOT IN ('completed', 'left') DEFERRABLE INITIALLY DEFERRED;
```

The unique index is `DEFERRABLE` because reordering (FR19) shuffles several positions inside one transaction and would otherwise trip the constraint mid-update.

### 3.8 AI, audit, and supporting tables

```sql
CREATE TABLE ai_requests (                            -- FR37
    id              UUID PRIMARY KEY,
    capability      ai_capability NOT NULL,
    requester_id    UUID REFERENCES users(id),
    patient_id      UUID REFERENCES patients(user_id),
    provider        TEXT NOT NULL,
    model_id        TEXT NOT NULL,
    input_digest    TEXT NOT NULL,                    -- sha256 of input; NOT the input itself
    outcome         ai_outcome NOT NULL,
    refusal_reason  TEXT,                             -- FR33
    latency_ms      INTEGER,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    error_code      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_ai_requests_eval ON ai_requests (capability, outcome, created_at DESC);

CREATE TABLE audit_log (                              -- FR5, NFR17
    id              BIGSERIAL PRIMARY KEY,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_user_id   UUID REFERENCES users(id),
    actor_role      user_role,
    action          TEXT NOT NULL,                    -- 'record.read', 'consent.revoke', ...
    subject_patient_id UUID,
    resource_type   TEXT,
    resource_id     UUID,
    clinic_id       UUID REFERENCES clinics(id),
    consent_grant_id UUID REFERENCES consent_grants(id),
    ip_address      INET,
    user_agent      TEXT,
    detail          JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_audit_patient ON audit_log (subject_patient_id, occurred_at DESC);
CREATE INDEX idx_audit_actor   ON audit_log (actor_user_id, occurred_at DESC);

REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM curanode_app;
REVOKE UPDATE, DELETE, TRUNCATE ON clinical_records FROM curanode_app;
```

Those two `REVOKE` statements are how NFR17 and Rule 2 become true rather than aspirational. They run in migration `0002` and are asserted by a test that attempts an `UPDATE` and expects it to fail.

```sql
CREATE TABLE record_embeddings (                      -- FR22 retrieval support
    record_id   UUID PRIMARY KEY REFERENCES clinical_records(id),
    patient_id  UUID NOT NULL REFERENCES patients(user_id),
    chunk_text  TEXT NOT NULL,
    embedding   VECTOR(1024) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_embeddings_hnsw ON record_embeddings
    USING hnsw (embedding vector_cosine_ops);

CREATE TABLE lab_reference_ranges (                   -- FR30, deterministic source of truth
    id         UUID PRIMARY KEY,
    analyte    TEXT NOT NULL,
    unit       TEXT NOT NULL,
    sex        TEXT,                                  -- NULL = any
    age_min_y  SMALLINT, age_max_y SMALLINT,
    ref_low    NUMERIC, ref_high NUMERIC,
    source     TEXT NOT NULL,                         -- citation for the range
    UNIQUE (analyte, unit, sex, age_min_y, age_max_y)
);

CREATE TABLE chat_sessions (
    id UUID PRIMARY KEY, user_id UUID NOT NULL REFERENCES users(id),
    locale locale_code NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE chat_messages (
    id UUID PRIMARY KEY, session_id UUID NOT NULL REFERENCES chat_sessions(id),
    role TEXT NOT NULL CHECK (role IN ('user','assistant')),
    content TEXT NOT NULL, ai_request_id UUID REFERENCES ai_requests(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE notifications (                          -- FR20
    id UUID PRIMARY KEY, user_id UUID NOT NULL REFERENCES users(id),
    channel TEXT NOT NULL CHECK (channel IN ('sms','email','in_app')),
    template_key TEXT NOT NULL, locale locale_code NOT NULL,
    params JSONB NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'queued',
    attempts SMALLINT NOT NULL DEFAULT 0, sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`lab_reference_ranges.source` is mandatory. Every range must cite where it came from, so the panel can ask "where did this number come from" and get an answer.

---

## 4. API / Function Design

### 4.1 Conventions

- Base path `/api/v1`. Breaking changes require `/v2`.
- Authentication: `Authorization: Bearer <access_token>`.
- All list endpoints are cursor-paginated: `?cursor=<opaque>&limit=<1..100>`.
- All requests carry `X-Request-Id`; generated at the proxy if absent, echoed in every response and log line.
- Errors use the envelope in §8.2 without exception.
- Mutating endpoints accept `Idempotency-Key` and are safe to retry (NFR8).

### 4.2 Core domain types

```python
from __future__ import annotations
from datetime import date, datetime, time
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Sequence
from uuid import UUID
from pydantic import BaseModel, Field

class Actor(BaseModel):
    """Resolved caller. Constructed once per request by the auth dependency."""
    user_id: UUID
    role: Literal["patient", "doctor", "clinic_admin"]
    clinic_ids: frozenset[UUID]         # empty for patients
    is_verified_doctor: bool
    locale: Literal["en", "ur"]
    request_id: str
    ip_address: str | None

class RecordFilters(BaseModel):
    types: Sequence[str] | None = None       # FR9
    clinic_id: UUID | None = None
    author_user_id: UUID | None = None
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None
    include_superseded: bool = False

class ClinicalRecordOut(BaseModel):
    id: UUID
    record_type: str
    source: str
    occurred_at: datetime
    recorded_at: datetime
    author_name: str
    clinic_name: str | None
    payload: dict[str, Any]
    supersedes_id: UUID | None
    superseded_at: datetime | None
    document_id: UUID | None

class RecordPage(BaseModel):
    items: list[ClinicalRecordOut]
    next_cursor: str | None
    total_estimate: int
```

### 4.3 The consent gateway — the most important function in the system

```python
class ConsentDenied(Exception):
    """Raised internally. NEVER surfaced to a caller as 403 — see below."""

async def load_records_for_actor(
    session: AsyncSession,
    actor: Actor,
    patient_id: UUID,
    *,
    filters: RecordFilters | None = None,
    limit: int = 50,
    cursor: str | None = None,
    purpose: str,
) -> RecordPage:
    """Sole permitted read path for clinical_records. Enforces FR4/FR5/FR21/D2.

    Access rules, evaluated in order:
      1. actor.role == 'patient' and actor.user_id == patient_id  -> allowed (own record)
      2. actor.role == 'doctor'  and actor.is_verified_doctor
         and an active consent_grant exists for (patient, doctor)
         or for (patient, one of actor.clinic_ids)                -> allowed
      3. anything else                                            -> ConsentDenied

    On success, writes exactly one audit_log row (action='record.read') in the
    SAME transaction as the SELECT. If the audit write fails, the read is rolled
    back and no data is returned. Auditing is not best-effort.

    `purpose` is free text recorded in audit_log.detail. It is mandatory so that
    every call site must state why it is reading a patient's record.

    D2 ENFORCEMENT: the HTTP layer converts ConsentDenied to **404 Not Found**,
    never 403 Forbidden. A 403 would confirm that a record exists for a patient
    the doctor has no right to know about, which is exactly the leak D2 forbids.
    This conversion happens in one exception handler; do not catch ConsentDenied
    anywhere else.
    """

async def grant_consent(
    session: AsyncSession, actor: Actor, *,
    grantee_type: Literal["doctor", "clinic"],
    grantee_id: UUID,
    expires_at: datetime | None = None,
) -> ConsentGrantOut: ...

async def revoke_consent(
    session: AsyncSession, actor: Actor, grant_id: UUID
) -> ConsentGrantOut: ...

async def list_consent_grants(
    session: AsyncSession, actor: Actor, *, include_revoked: bool = False
) -> list[ConsentGrantOut]: ...
```

### 4.4 HTTP endpoints

#### Identity and consent

| Method | Path | Auth | Requirement |
|---|---|---|---|
| `POST` | `/api/v1/auth/register` | — | FR1 |
| `POST` | `/api/v1/auth/login` | — | FR6 |
| `POST` | `/api/v1/auth/refresh` | refresh | FR6 |
| `POST` | `/api/v1/auth/logout` | bearer | FR6 |
| `GET` | `/api/v1/me` | bearer | FR6 |
| `PATCH` | `/api/v1/me` | bearer | FR2 |
| `GET` | `/api/v1/me/consents` | patient | FR4 |
| `POST` | `/api/v1/me/consents` | patient | FR4 |
| `DELETE` | `/api/v1/me/consents/{grant_id}` | patient | FR4 |
| `GET` | `/api/v1/me/access-log` | patient | FR5 |

```python
@router.post("/auth/register", status_code=201, response_model=RegisterAccepted)
async def register(body: RegisterRequest, session: SessionDep) -> RegisterAccepted: ...

class RegisterRequest(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr                                     # primary credential
    password: str = Field(min_length=10, max_length=128)
    phone_e164: str | None = Field(default=None, pattern=r"^\+92[0-9]{10}$")
    preferred_locale: Literal["en", "ur"] = "en"
    role: Literal["patient", "doctor"] = "patient"      # doctors self-register unverified, FR3

@router.get("/me/access-log", response_model=Page[AccessLogEntry])
async def my_access_log(
    actor: PatientDep, session: SessionDep,
    cursor: str | None = None, limit: int = Query(50, le=100),
) -> Page[AccessLogEntry]: ...
    # FR5. Reads audit_log WHERE subject_patient_id = actor.user_id
```

#### Medical Passport

| Method | Path | Auth | Requirement |
|---|---|---|---|
| `GET` | `/api/v1/patients/{patient_id}/records` | patient(self) \| doctor | FR8, FR9, FR21 |
| `POST` | `/api/v1/patients/{patient_id}/records` | doctor | FR23 |
| `GET` | `/api/v1/patients/{patient_id}/records/{record_id}` | patient(self) \| doctor | FR8 |
| `POST` | `/api/v1/patients/{patient_id}/records/{record_id}/correction` | author | D1 |
| `GET` | `/api/v1/patients/{patient_id}/summary.pdf` | patient(self) | FR14 |
| `GET` | `/api/v1/patients/{patient_id}/what-changed` | doctor | FR22 |

```python
@router.get("/patients/{patient_id}/records", response_model=RecordPage)
async def list_records(
    patient_id: UUID, actor: ActorDep, session: SessionDep,
    types: Annotated[list[str] | None, Query()] = None,
    clinic_id: UUID | None = None,
    occurred_from: datetime | None = None,
    occurred_to: datetime | None = None,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=100),
) -> RecordPage:
    return await load_records_for_actor(
        session, actor, patient_id,
        filters=RecordFilters(types=types, clinic_id=clinic_id,
                              occurred_from=occurred_from, occurred_to=occurred_to),
        limit=limit, cursor=cursor, purpose="timeline_view",
    )

@router.post("/patients/{patient_id}/records", status_code=201,
             response_model=ClinicalRecordOut)
async def create_record(
    patient_id: UUID, body: CreateRecordRequest,
    actor: VerifiedDoctorDep, session: SessionDep,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> ClinicalRecordOut: ...   # FR23

class CreateRecordRequest(BaseModel):
    record_type: Literal["visit","diagnosis","prescription","lab_report",
                         "allergy","condition","medication","note"]
    occurred_at: datetime
    payload: dict[str, Any]
    visit_id: UUID | None = None
    appointment_id: UUID | None = None
```

#### Documents and extraction

| Method | Path | Auth | Requirement |
|---|---|---|---|
| `POST` | `/api/v1/documents` | patient | FR10 |
| `GET` | `/api/v1/documents/{id}` | owner | FR10 |
| `GET` | `/api/v1/documents/{id}/extraction` | owner | FR11, FR13 |
| `POST` | `/api/v1/documents/{id}/extraction/review` | owner | FR12 |
| `GET` | `/api/v1/documents/{id}/interpretation` | owner | FR30 |

```python
@router.post("/documents", status_code=202, response_model=DocumentAccepted)
async def upload_document(
    actor: PatientDep, session: SessionDep,
    file: Annotated[UploadFile, File()],
    expected_type: Annotated[str | None, Form()] = None,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> DocumentAccepted:
    """FR10. Returns 202 immediately with a document id and status='extracting'.
    Extraction runs in the worker (NFR3). Client polls GET .../extraction.
    Deduplicated by (patient_id, sha256) so a retried upload after a dropped
    connection returns the original document rather than creating a second (NFR8).
    """

@router.post("/documents/{document_id}/extraction/review", status_code=201,
             response_model=ClinicalRecordOut)
async def review_extraction(
    document_id: UUID, body: ReviewRequest,
    actor: PatientDep, session: SessionDep,
) -> ClinicalRecordOut:
    """FR12. This is the ONLY path by which an extracted record enters
    clinical_records. Sets confirmed_by/confirmed_at, satisfying the
    ai_sourced_must_be_confirmed constraint. Rejecting sets document status
    to 'rejected' and creates nothing."""

class ReviewRequest(BaseModel):
    accepted: bool
    final_payload: dict[str, Any]      # patient's corrected version
    occurred_at: datetime
```

#### Appointments and queue

| Method | Path | Auth | Requirement |
|---|---|---|---|
| `GET` | `/api/v1/doctors` | any | FR15 |
| `GET` | `/api/v1/doctors/{id}/availability` | any | FR15 |
| `POST` | `/api/v1/appointments` | patient | FR16 |
| `PATCH` | `/api/v1/appointments/{id}` | patient \| admin | FR16 |
| `DELETE` | `/api/v1/appointments/{id}` | patient \| admin | FR16 |
| `GET` | `/api/v1/appointments/{id}/queue-position` | patient | FR17 |
| `GET` | `/api/v1/clinics/{id}/queue` | admin \| doctor | FR19, FR24 |
| `PATCH` | `/api/v1/queue/{entry_id}` | admin | FR19 |
| `PUT` | `/api/v1/doctors/{id}/schedule` | admin | FR18 |

```python
@router.get("/doctors/{doctor_id}/availability", response_model=AvailabilityOut)
async def get_availability(
    doctor_id: UUID, session: SessionDep,
    from_date: date, to_date: date,
) -> AvailabilityOut:
    """FR15. Computes free slots from doctor_schedules minus schedule_exceptions
    minus existing appointments. Max range 30 days; wider returns 422."""

@router.post("/appointments", status_code=201, response_model=AppointmentOut)
async def book_appointment(
    body: BookRequest, actor: PatientDep, session: SessionDep,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> AppointmentOut:
    """FR16. Relies on uq_appt_slot to settle concurrent booking.
    IntegrityError on that index maps to 409 SLOT_TAKEN, not 500."""

@router.get("/appointments/{appointment_id}/queue-position",
            response_model=QueuePositionOut)
async def queue_position(
    appointment_id: UUID, actor: PatientDep, session: SessionDep
) -> QueuePositionOut:
    """FR17. Served from Redis (key `queue:{doctor_id}:{date}`, TTL 30s).
    Client polls every 15s — see §10.4 for why polling, not WebSockets."""

class QueuePositionOut(BaseModel):
    position: int                       # 1-based; ahead_of_you = position - 1
    ahead_of_you: int
    estimated_wait_minutes: int | None  # None when there is no basis to estimate
    estimate_confidence: Literal["low", "medium", "high"]
    state: str
    computed_at: datetime
```

`estimated_wait_minutes` is nullable and paired with a confidence level on purpose. A confidently wrong wait time is worse than no wait time — a patient who is told "10 minutes" and waits 50 loses trust in the whole platform. The estimate is the median of the last 10 completed consultations for that doctor on that day, and returns `None` with `confidence="low"` until at least three have completed.

#### Doctor

| Method | Path | Auth | Requirement |
|---|---|---|---|
| `GET` | `/api/v1/doctor/patients/search` | doctor | FR21 |
| `GET` | `/api/v1/doctor/today` | doctor | FR24 |
| `GET` | `/api/v1/doctor/analytics` | doctor | FR25 |

```python
@router.get("/doctor/patients/search", response_model=list[PatientSearchResult])
async def search_patients(
    actor: VerifiedDoctorDep, session: SessionDep,
    passport_no: str | None = None, phone_e164: str | None = None,
) -> list[PatientSearchResult]:
    """FR21. Exact-match lookup ONLY on passport number or phone. No fuzzy or
    partial search: a browsable index of patients is a privacy hazard and is not
    what a doctor at the point of care needs. Returns [] rather than 404 when no
    match, so absence of consent and absence of patient are indistinguishable (D2)."""
```

#### AI

| Method | Path | Auth | Requirement |
|---|---|---|---|
| `POST` | `/api/v1/ai/chat` | any | FR29, FR33 |
| `POST` | `/api/v1/ai/translate` | admin \| doctor | FR32 |
| `GET` | `/api/v1/patients/{id}/what-changed` | doctor | FR22 |
| `GET` | `/api/v1/documents/{id}/interpretation` | owner | FR30 |

```python
@router.post("/ai/chat", response_model=ChatReply)
async def chat(
    body: ChatRequest, actor: ActorDep, session: SessionDep
) -> ChatReply: ...

class ChatRequest(BaseModel):
    session_id: UUID | None = None
    message: str = Field(min_length=1, max_length=2000)
    locale: Literal["en", "ur"]

class ChatReply(BaseModel):
    session_id: UUID
    message: str
    locale: Literal["en", "ur"]
    refused: bool                       # FR33
    refusal_category: Literal["clinical_advice","emergency","distress",
                              "out_of_scope"] | None
    disclaimer: str                     # FR31 — server-side, never model-generated
    sources: list[str]                  # help-article ids used
```

### 4.5 The orchestrator

```python
class AICapability(StrEnum):
    EXTRACTION   = "extraction"
    RETRIEVAL    = "retrieval"
    TRANSLATION  = "translation"
    CONVERSATION = "conversation"

class AgentRequest(BaseModel):
    capability: AICapability
    payload: dict[str, Any]
    actor: Actor
    patient_id: UUID | None = None
    timeout_s: float = 30.0

class AgentResult(BaseModel):
    ok: bool
    data: dict[str, Any] | None
    outcome: Literal["success","refused","failed","timeout","circuit_open"]
    refusal_reason: str | None = None
    user_message_key: str | None = None     # i18n key, never raw English (FR36)
    ai_request_id: UUID

class Agent(Protocol):
    capability: AICapability
    version: str
    async def run(self, req: AgentRequest) -> AgentResult: ...

class Orchestrator:
    """FR34. Owns routing, timeouts, circuit breaking, and logging.
    Registry is fixed at four entries (FR35); registering a fifth raises."""

    def __init__(self, agents: Mapping[AICapability, Agent]) -> None:
        if set(agents) != set(AICapability):
            raise ConfigurationError("exactly four capabilities required (FR35)")

    async def dispatch(self, req: AgentRequest) -> AgentResult:
        """1. Open ai_requests row (outcome pending).
           2. Check circuit breaker for the capability; if open, return
              circuit_open WITHOUT calling the provider.
           3. Run agent with timeout.
           4. Record outcome, latency, tokens (FR37).
           5. Never raise to the caller — always return an AgentResult (FR36).
              A failing agent degrades one feature; the platform stays up."""
```

### 4.6 Agent signatures

```python
class ExtractionAgent:                                  # FR11, FR13
    async def run(self, req: AgentRequest) -> AgentResult:
        """payload: {document_id, object_key, mime_type, expected_type?}
        returns:  {proposed_type, proposed_payload, field_confidence,
                   overall_confidence}
        Two-pass: printed text via local OCR; if mean confidence < 0.70,
        retry via vision model. Never writes to clinical_records (FR12)."""

class RetrievalAgent:                                   # FR22
    async def run(self, req: AgentRequest) -> AgentResult:
        """payload: {patient_id, since: datetime, doctor_id}
        returns:  {changes: [...], narrative: str}

        The `changes` list is computed in SQL — a deterministic diff of records
        with recorded_at > since. The model receives that list and produces
        `narrative` only. It is never given free access to the record set and
        never decides what counts as a change (Rule 3)."""

class TranslationAgent:                                 # FR32
    async def run(self, req: AgentRequest) -> AgentResult:
        """payload: {text | object_key, source_lang, target_lang}
        returns:  {translated_text, object_key?}
        Refuses source text over 20,000 characters."""

class ConversationAgent:                                # FR29, FR31, FR33
    async def run(self, req: AgentRequest) -> AgentResult:
        """payload: {session_id, message, locale, history}
        returns:  {message, refused, refusal_category, sources}

        Pipeline: pre-classify -> retrieve help articles -> generate -> post-check.
        Scope is HOW TO USE THE PLATFORM. Any clinical question is refused with
        a redirect. See §6.6."""
```

---

## 5. Module Breakdown

### 5.1 Repository layout

```
curanode/
├── compose.yaml                    compose.override.yaml   compose.prod.yaml
├── .env.example
├── docs/  PRD.md  TDD.md  ADR/
├── backend/
│   ├── pyproject.toml  uv.lock  Dockerfile
│   ├── alembic/versions/
│   ├── src/curanode/
│   │   ├── main.py  settings.py  deps.py  errors.py
│   │   ├── db/            models.py  session.py  types.py
│   │   ├── identity/      router.py  service.py  schemas.py  security.py
│   │   ├── consent/       router.py  gateway.py  schemas.py      ★
│   │   ├── records/       router.py  service.py  schemas.py  payloads.py
│   │   ├── documents/     router.py  service.py  storage.py
│   │   ├── scheduling/    router.py  availability.py  queue.py
│   │   ├── clinical/      router.py  service.py  analytics.py
│   │   ├── admin/         router.py  service.py
│   │   ├── orchestrator/  core.py  registry.py  breaker.py  schemas.py
│   │   ├── agents/        extraction.py  retrieval.py  translation.py
│   │   │                  conversation.py  providers/
│   │   ├── audit/         writer.py  reader.py
│   │   ├── i18n/          catalogue.py  messages/{en,ur}.json
│   │   ├── notifications/ service.py  templates/
│   │   └── tasks/         celery_app.py  extraction.py  embeddings.py
│   └── tests/  unit/  integration/  fixtures/  seed/
├── frontend/
│   ├── package.json  pnpm-lock.yaml  Dockerfile
│   ├── messages/en.json  messages/ur.json
│   ├── src/app/[locale]/(patient|doctor|admin)/...
│   ├── src/components/  src/lib/api/  src/lib/hooks/
│   └── e2e/
└── ops/  nginx/  minio/  scripts/seed_synthetic.py
```

### 5.2 Module responsibilities and ownership

| Module | PRD workstream | Requirements | May import |
|---|---|---|---|
| `identity` | INF | FR1, FR3, FR6 | `db`, `audit`, `i18n` |
| `consent` ★ | PAT | FR4, NFR16, D2 | `db`, `audit` |
| `records` | PAT | FR2, FR8, FR9, FR14, D1 | `db`, `consent`, `audit` |
| `documents` | PAT | FR10 | `db`, `consent`, `orchestrator`, `tasks` |
| `scheduling` | INF | FR15–FR20 | `db`, `audit`, `notifications` |
| `clinical` | DOC | FR21–FR27 | `db`, `consent`, `orchestrator` |
| `admin` | INF | FR3, FR18, FR19, FR32 | `db`, `consent`, `orchestrator` |
| `orchestrator` | AI | FR34–FR37 | `agents`, `db` |
| `agents` | AI | FR11–FR13, FR22, FR29–FR33 | `db` (read-only), providers |
| `audit` | INF | FR5, NFR17 | `db` |
| `i18n` | INF | FR28, NFR13 | — |
| `notifications` | PAT | FR20 | `db`, `i18n` |

### 5.3 Dependency rules

```
routers ──▶ services ──▶ consent gateway ──▶ db
                    └──▶ orchestrator ──▶ agents ──▶ providers
```

1. `records`, `clinical`, and `admin` **must not** import `db.models.ClinicalRecord` directly. They go through `consent.gateway`.
2. `agents` **must not** import any service module. Agents receive data; they do not fetch it.
3. `audit` imports nothing but `db`. Nothing may make audit writes conditional.
4. No module imports a sibling's `service.py` — cross-module calls go through the owning module's public `__init__`.

Enforced in CI by `import-linter` with a contract file. A violating PR fails the build.

### 5.4 Frontend structure

```
src/app/[locale]/
├── (patient)/  passport/  upload/  appointments/  queue/  consents/  access-log/  chat/
├── (doctor)/   today/  patient/[passportNo]/  visit/[appointmentId]/  analytics/
└── (admin)/    schedules/  queue/  doctors/  translate/
```

Every route is under `[locale]`, so a language switch is a route change that preserves the path — satisfying FR28's "without losing the user's place" without any client state juggling.

---

## 6. Data Flow

### 6.1 Prescription upload → confirmed record (FR10 → FR11 → FR12)

```
Patient          web              api            MinIO      worker        provider
  │  photo        │                │               │           │              │
  ├──────────────▶│                │               │           │              │
  │               ├── POST /documents ────────────▶│           │              │
  │               │                ├── put object ▶│           │              │
  │               │                ├── INSERT documents (status=extracting)   │
  │               │                ├── enqueue extract_document ─────────────▶│
  │               │◀── 202 {id} ───┤               │           │              │
  │◀── "reading" ─┤                │               │           │              │
  │               │                │               │◀ get obj ─┤              │
  │               │                │               │           ├── local OCR ─┤
  │               │                │               │           │  conf < 0.70?│
  │               │                │               │           ├── vision ───▶│
  │               │                │               │           │◀─────────────┤
  │               │                │◀ INSERT extractions, status=awaiting_review
  │               │  poll GET /extraction (2s, backoff to 5s, cap 60s)         │
  │◀── proposed fields, low-confidence highlighted (FR13) ──────┤             │
  ├── corrects 1 field, taps Confirm ──▶ POST /extraction/review              │
  │               │                ├── INSERT clinical_records                 │
  │               │                │   (confirmed_by=patient, document_id set) │
  │               │                ├── INSERT extraction_reviews(fields_changed)│
  │               │                ├── enqueue embed_record                     │
  │◀── record on timeline ─────────┤                                           │
```

If the worker never completes, the document sits at `extracting` and the poll endpoint returns `status=extracting` with `elapsed_s`. After 120 s the client shows a retry action. Nothing is lost (NFR8) — the object is already stored and the row already exists.

### 6.2 Doctor opens a patient (FR21 → FR5 → FR22)

```
Doctor taps patient in today's queue
  └─▶ GET /doctor/patients/search?passport_no=...
       └─▶ exact match only; [] if no match OR no consent (indistinguishable, D2)
  └─▶ GET /patients/{id}/records
       └─▶ load_records_for_actor(purpose="consultation")
            ├─ resolve grants  ─── none? ──▶ ConsentDenied ──▶ HTTP 404
            ├─ SELECT ... WHERE patient_id = ? ORDER BY occurred_at DESC LIMIT 50
            └─ INSERT audit_log(action='record.read', consent_grant_id=...)
               (same transaction — audit failure rolls back the read)
  └─▶ GET /patients/{id}/what-changed?since=<doctor's last visit>
       ├─ SQL diff: records WHERE recorded_at > since        ← facts, deterministic
       └─ Orchestrator.dispatch(RETRIEVAL, {changes})        ← prose only
            └─ agent failure ──▶ AgentResult(ok=False) ──▶ UI shows the raw diff
                                  as a plain list. The doctor still gets the facts.
```

That last line matters. FR36 says a failing capability must not take the platform down; here it means a doctor who loses the summariser still sees exactly what changed, just without the sentence. Degradation is to *less polish*, never to *less information*.

### 6.3 Booking and queue (FR15 → FR17)

```
GET /doctors/{id}/availability     schedules − exceptions − booked  →  free slots
POST /appointments                 INSERT; uq_appt_slot settles races → 409 SLOT_TAKEN
                                   → INSERT queue_entries(state=expected, position=append)
                                   → enqueue notification (FR20)

Clinic day:
  admin PATCH /queue/{id} state=checked_in     → recompute positions in one tx
  admin PATCH /queue/{id} position=2           → DEFERRABLE index allows the shuffle
  api writes Redis queue:{doctor}:{date} (TTL 30s)
  patient GET /queue-position every 15s        → served from Redis, DB on miss
```

### 6.4 Translation (FR32)

```
admin POST /ai/translate {object_key|text, source_lang, target_lang}
  → Orchestrator.dispatch(TRANSLATION)
  → provider call
  → text ≤ 4000 chars: synchronous response
    text > 4000 chars: 202 + job id, result written to MinIO, polled
```

### 6.5 Lab interpretation (FR30 → FR31) — deterministic first

```
1. Confirmed lab_report record exists (already human-verified, FR12).
2. For each analyte: look up lab_reference_ranges by (analyte, unit, sex, age).
   Compute flag ∈ {low, normal, high} IN CODE. No model involvement.
3. Analytes with no reference range are marked "no reference available"
   and are NOT sent for explanation. The system does not guess a range.
4. Orchestrator.dispatch(CONVERSATION, {flagged_analytes, locale})
   → model produces plain-language phrasing of the ALREADY-FLAGGED values.
5. Post-check rejects output containing diagnosis or medication language (§6.6).
6. Server appends the FR31 disclaimer from i18n/messages/{locale}.json.
   The disclaimer is concatenated in code, not requested from the model,
   so it cannot be omitted, reworded, or translated away.
```

### 6.6 Chatbot safety pipeline (FR29, FR31, FR33)

```
message
  │
  ├─▶ PRE-CHECK (deterministic, before any model call)
  │     • emergency patterns (chest pain, bleeding, breathing difficulty,
  │       unconsciousness) in Urdu and English
  │     • self-harm / distress patterns in Urdu and English
  │     • clinical-advice patterns (dosage, "should I take", "is this serious")
  │   match ──▶ refuse with the fixed template for that category.
  │             No model call is made. Response is a static, reviewed,
  │             bilingual message pointing to the treating doctor or,
  │             for emergencies, to emergency care.
  │
  ├─▶ RETRIEVE help articles (platform documentation only — never patient records)
  ├─▶ GENERATE (system prompt: platform support scope only)
  ├─▶ POST-CHECK output for clinical assertions; on match, discard the
  │   generated text and fall back to the out-of-scope refusal
  └─▶ APPEND server-side disclaimer (FR31) and return
```

Two properties are non-negotiable. **The pre-check runs before the model**, so an emergency or distress message never depends on a model behaving correctly. **The refusal templates are static, reviewed text**, held in `i18n/messages/{en,ur}.json`, written by the team and approved by the advisor — not generated.

The PRD lists the scope boundary as an open question blocking FR29 and FR33. That list becomes `docs/chatbot-scope.md`, and these two features do not enter a sprint until it exists and is signed off.

---

## 7. Security

### 7.1 Authentication

- Argon2id password hashing (`time_cost=3, memory_cost=65536, parallelism=4`).
- **Registration is email + password with no verification step.** An account is `active` on creation and the user is signed in immediately. Phone OTP was specified here in v1.0 and has been removed — see the amendment note at the head of this document.
  *Accepted consequence:* an email address is never proven, so a user may register with an address they do not control and a typo is unrecoverable until password reset exists. Acceptable while all data is synthetic (`NFR19`); **revisit before any real patient data enters the system.**
- JWT access token, 15-minute lifetime, `HS256` in the pilot with a 32-byte secret from the environment.
- Refresh token, 14 days, stored hashed in Redis, **rotated on every use**; reuse of a consumed refresh token revokes the whole family and forces re-login.
- Tokens are delivered in `HttpOnly; Secure; SameSite=Strict` cookies. Not `localStorage` — an XSS bug should not become a credential leak.
- Account lock after 10 failed logins for 15 minutes.

### 7.2 Authorisation

Three FastAPI dependencies, and no ad-hoc role checks anywhere else:

```python
ActorDep          = Annotated[Actor, Depends(current_actor)]
PatientDep        = Annotated[Actor, Depends(require_role("patient"))]
VerifiedDoctorDep = Annotated[Actor, Depends(require_verified_doctor)]  # FR3
ClinicAdminDep    = Annotated[Actor, Depends(require_role("clinic_admin"))]
```

`require_verified_doctor` checks `doctors.is_verified` on every request, not at login. An admin revoking verification takes effect on the next call, not in fifteen minutes.

| Resource | Patient (self) | Patient (other) | Verified doctor | Unverified doctor | Clinic admin |
|---|---|---|---|---|---|
| Own profile | RW | — | RW | RW | RW |
| Clinical records | R | — | R *with consent* | — | — |
| Create records | — | — | W *with consent* | — | — |
| Consent grants | RW | — | R (own) | — | — |
| Access log | R (own) | — | — | — | — |
| Schedules | R | R | R (own) | R (own) | RW (own clinic) |
| Queue | R (own entry) | — | R (own) | — | RW (own clinic) |
| Doctor verification | — | — | — | — | W (own clinic) |

Clinic admins have **no** access to clinical records. They manage schedules and queues, which requires patient names and appointment times, not medical history. This is least privilege and it is also what NFR16 requires — there is no administrative override.

### 7.3 Consent enforcement

Single chokepoint, described in §4.3. Three tests gate every merge:

1. `test_no_direct_record_queries` — AST-scans the codebase for `select(ClinicalRecord)` outside `consent/gateway.py`. Fails the build on any hit.
2. `test_consent_denied_returns_404` — asserts the status code is 404 and the body is byte-identical to a genuinely-nonexistent patient (D2).
3. `test_audit_rollback` — forces the audit insert to fail and asserts no records are returned.

### 7.4 Data protection

- TLS 1.2+ at the proxy; HTTP redirects to HTTPS. Self-signed in dev, Let's Encrypt in pilot.
- Postgres volume on an encrypted host filesystem (NFR15). Full-disk encryption is the pilot's at-rest story; column-level encryption is not implemented and its absence is documented rather than implied.
- MinIO server-side encryption enabled; objects are never publicly readable. Downloads use presigned URLs with 5-minute expiry.
- Secrets from environment only. `.env` is git-ignored; `.env.example` holds placeholders. A pre-commit hook (`detect-secrets`) blocks committed credentials.
- Backups: nightly `pg_dump` to a MinIO bucket, 7-day retention, restore rehearsed once before M6. An untested backup is not a backup.

### 7.5 Upload safety (FR10)

Magic-byte type verification (not the declared MIME, not the extension); allowlist `image/jpeg`, `image/png`, `image/webp`, `application/pdf`; 15 MB per file, enforced at Nginx *and* the application; EXIF stripped on ingest; original filenames discarded and replaced by UUID keys; files served only through presigned URLs with `Content-Disposition: attachment` and `X-Content-Type-Options: nosniff`.

### 7.6 Sending health data to third parties

This deserves stating plainly rather than burying in §9.

Extraction, retrieval summarisation, translation, and chat all involve sending content to an external provider. That content can include a named patient's medications and diagnoses. Three controls apply:

1. **De-identification before transmission.** Patient name, phone, passport number, address, and record UUIDs are stripped and replaced by positional placeholders before any provider call. The mapping stays local and is re-applied to the response. Implemented once in `agents/providers/redaction.py` and unit-tested with adversarial cases.
2. **Synthetic data only during development** (NFR19). Enforced by a startup check: if `ENVIRONMENT != "pilot"` and any `users` row lacks the synthetic marker, the application refuses to start.
3. **Explicit advisor sign-off before any real patient data reaches a provider.** This is a checklist item at M6, not a formality. If a partner clinic supplies real records, this decision is re-opened before the first upload.

### 7.7 Injection and application hardening

Parameterised queries only, via SQLAlchemy — no f-string SQL, enforced by a Ruff rule. Pydantic validation on every request body. CSP without `unsafe-inline`. `X-Frame-Options: DENY`. CORS restricted to the known frontend origin. Rate limits: 5/min on auth endpoints, 10/min on AI endpoints per user, 100/min general, applied per user where authenticated and per IP where not.

Prompt injection is treated as a real threat, because uploaded documents are attacker-controlled text that reaches a model. Document content is passed in a clearly delimited user block, never concatenated into the system prompt, and extraction output is validated against the Pydantic payload schema before use — a document instructing the model to "ignore previous instructions" produces a schema violation and a failed extraction, not an action.

### 7.8 Logging discipline

Structured JSON via structlog. A redaction processor drops `password`, `token`, `authorization`, `email`, `phone_e164`, `full_name`, and any key matching `payload` before emission. **Clinical payloads are never logged at any level, including DEBUG.** `ai_requests.input_digest` stores a hash, never the input. Logs retain `request_id`, `user_id`, route, status, and duration — enough to debug, not enough to leak.

---

## 8. Error-Handling Strategy

### 8.1 Principles

1. **Fail loudly in development, gracefully in production.** No silent `except: pass`.
2. **Never lose user work** (NFR8). Uploads are idempotent; forms preserve state on failure; every mutating endpoint is retry-safe.
3. **The user gets a message key, never a stack trace or a raw English string.** Message keys resolve through `i18n` so every error appears in Urdu and English (FR28).
4. **AI failure is expected, not exceptional** (FR36). Handled by return value, not exception.
5. **Correlate everything.** One `request_id` from browser to worker to provider.

### 8.2 Error envelope

```json
{
  "error": {
    "code": "SLOT_TAKEN",
    "message_key": "errors.appointment.slot_taken",
    "message": "That time was just booked by someone else. Please pick another slot.",
    "details": { "doctor_id": "...", "scheduled_start": "..." },
    "request_id": "01J8X...",
    "retryable": true
  }
}
```

`message` is pre-localised to the caller's locale. `retryable` tells the client whether to offer a retry button — clients must not infer this from the status code.

### 8.3 Error taxonomy

| Code | HTTP | Retryable | Notes |
|---|---|---|---|
| `VALIDATION_FAILED` | 422 | no | Field-level details |
| `UNAUTHENTICATED` | 401 | no | Triggers refresh-then-retry once |
| `FORBIDDEN` | 403 | no | Role mismatch — **never** consent failure |
| `NOT_FOUND` | 404 | no | Also consent denial (D2) |
| `SLOT_TAKEN` | 409 | yes | From `uq_appt_slot` |
| `DUPLICATE_UPLOAD` | 200 | — | Returns the existing document, not an error |
| `FILE_TOO_LARGE` | 413 | no | |
| `UNSUPPORTED_FILE_TYPE` | 415 | no | Magic-byte check failed |
| `RATE_LIMITED` | 429 | yes | `Retry-After` header |
| `AI_UNAVAILABLE` | 503 | yes | Circuit open |
| `AI_TIMEOUT` | 504 | yes | Exceeded `timeout_s` |
| `AI_REFUSED` | 200 | — | FR33 refusal is a valid outcome, not an error |
| `EXTRACTION_FAILED` | 200 | yes | Document kept; manual entry offered |
| `INTERNAL_ERROR` | 500 | yes | Generic message; details logged only |

Two entries carry weight. `AI_REFUSED` returns **200** because a refusal is the system working correctly — treating it as an error would tempt a client to retry it, and retrying a refusal until it succeeds is exactly the failure mode FR33 exists to prevent. `NOT_FOUND` covering consent denial is the D2 implementation and must never be "improved" into a more helpful 403.

### 8.4 AI degradation matrix (FR36)

| Capability fails | User sees | Platform impact |
|---|---|---|
| Extraction | "Automatic reading unavailable — enter details manually" + manual form | Upload and storage still work |
| Retrieval | Raw structured diff as a plain list, no narrative | Full history still visible |
| Translation | "Translation unavailable, try again shortly" | Everything else unaffected |
| Conversation | Static FAQ links + "contact your clinic" | Everything else unaffected |

Every row degrades to a working manual path. None blocks a clinical workflow.

### 8.5 Circuit breaker

Per capability: open after 5 consecutive failures or a 50% failure rate over 20 calls; half-open after 60 s; close after 3 consecutive successes. While open, `dispatch` returns `circuit_open` **without calling the provider**, so a provider outage costs one timeout, not one per user.

### 8.6 Retry policy

| Operation | Retries | Backoff |
|---|---|---|
| Provider call | 2 | 1 s, 4 s + jitter |
| Celery extraction task | 3 | 30 s, 2 min, 10 min |
| SMS send | 3 | 1 min, 5 min, 30 min |
| DB deadlock | 3 | 50 ms, 200 ms, 1 s |
| Anything user-visible and synchronous | 0 | Return and let the user decide |

Non-idempotent operations are never retried automatically. `INSERT clinical_records` gets one attempt; a failure surfaces to the doctor, who re-submits.

### 8.7 Frontend handling

TanStack Query retries idempotent GETs twice with exponential backoff and never retries mutations automatically. Every mutation carries a client-generated `Idempotency-Key`, so a user-initiated retry cannot double-book or double-record. Route-level error boundaries prevent one failing panel from blanking the screen. Forms hold their state in `sessionStorage` while a submit is in flight and clear it only on confirmed success (NFR8). Loss of connectivity shows a persistent banner — there is no offline mode (NFR9) and the UI says so plainly rather than appearing to work.

---

## 9. Third-Party Integrations

### 9.1 Summary

| Service | Purpose | Requirements | Failure mode |
|---|---|---|---|
| LLM provider (Anthropic API) | Retrieval narrative, translation, chat, fallback extraction | FR22, FR29–FR33 | Circuit breaker → §8.4 |
| Tesseract OCR (self-hosted) | First-pass printed text | FR11 | Falls through to vision model |
| SMS gateway (Pakistani provider) | Appointment notifications only | FR20 | Queue and retry; email fallback |
| SMTP | Email notifications | FR20 | Queued, non-blocking |
| MinIO (self-hosted) | Object storage | FR10, FR14, FR32 | Upload fails cleanly, nothing lost |

### 9.2 Provider abstraction

Every external call goes through an interface so that swapping providers is a config change, not a refactor:

```python
class LLMProvider(Protocol):
    async def complete(self, *, system: str, messages: list[Message],
                       max_tokens: int, timeout_s: float,
                       response_schema: type[BaseModel] | None = None) -> LLMResponse: ...

class VisionProvider(Protocol):
    async def extract(self, *, image_bytes: bytes, media_type: str,
                      instruction: str, response_schema: type[BaseModel]) -> LLMResponse: ...

class SMSProvider(Protocol):
    async def send(self, *, to_e164: str, body: str, locale: str) -> SMSReceipt: ...
```

A `FakeLLMProvider`, `FakeVisionProvider`, and `FakeSMSProvider` ship in `tests/fixtures/` and are the default in the test and CI environments. **No test makes a network call.** This is what keeps the suite fast and the bill at zero.

### 9.3 OCR strategy

Two passes, cheapest first:

1. **Tesseract** with English and Urdu language data, run in the worker container. Free, local, no data leaves the machine. Good on printed prescriptions, poor on handwriting.
2. **Vision model** if mean per-field confidence is below 0.70. Costs money and sends data externally (§7.6), so it is a fallback rather than the default.

Both write `engine` and `engine_version` to `extractions`, which is what makes the PRD's separate accuracy targets for printed (≥85%) and handwritten (≥60%) measurable rather than aspirational.

### 9.4 Cost control

An FYP has no budget, and an unmonitored API key is how a student project produces a surprise invoice.

- Hard monthly spend cap configured at the provider console.
- Per-user daily quotas: 20 extractions, 50 chat messages, 10 translations. Exceeded → `RATE_LIMITED` with a clear message.
- `ai_requests` records token counts on every call; a weekly script reports spend by capability.
- Development and CI default to fakes. Real providers are enabled only by explicit environment flag.
- Sprint-level alert at 70% of the monthly cap.

### 9.5 SMS provider

The specific gateway is selected before appointment notifications (FR20) are built, after checking rates and delivery reliability for Pakistani numbers, and recorded in `docs/ADR/0003-sms-provider.md`. Until then, `FakeSMSProvider` logs to the console, which is sufficient for all development.

**SMS is no longer on the authentication path.** With registration OTP removed (§7.1), no SMS provider is required for M1 at all — sign-in works with no gateway configured. The interface is retained solely for FR20 and can be deferred to the scheduling sprint.

---

## 10. Scalability & Performance

### 10.1 Budgets

Derived directly from NFR1–NFR5. Every number is asserted by a test, not hoped for.

| Path | Requirement | Budget (p95) | Enforced by |
|---|---|---|---|
| Doctor consultation screens | NFR1 | 3.0 s end-to-end | Playwright timing assertion |
| — API portion | | 400 ms | pytest-benchmark |
| Patient timeline, 100 records | NFR2 | 5.0 s / 600 ms API | Load test |
| Document extraction | NFR3 | 30 s | Worker task timeout |
| Chatbot first token | NFR4 | 5.0 s | Streaming assertion |
| Queue position | — | 200 ms | Redis-served |
| Availability lookup, 30 days | — | 500 ms | Query benchmark |

### 10.2 Database

The four queries that will decide whether NFR1 and NFR2 are met:

```sql
-- Timeline (idx_records_timeline)
SELECT ... FROM clinical_records
 WHERE patient_id = $1 AND ($2::record_type[] IS NULL OR record_type = ANY($2))
   AND occurred_at < $3                       -- keyset cursor, never OFFSET
 ORDER BY occurred_at DESC LIMIT $4;

-- What changed (idx_records_timeline)
SELECT ... FROM clinical_records
 WHERE patient_id = $1 AND recorded_at > $2 AND superseded_at IS NULL
 ORDER BY recorded_at DESC;

-- Availability (idx_appt_doctor_day)
SELECT scheduled_start FROM appointments
 WHERE doctor_id = $1 AND scheduled_start BETWEEN $2 AND $3
   AND status IN ('booked','rescheduled');

-- Today's queue (uq_queue_position)
SELECT ... FROM queue_entries
 WHERE doctor_id = $1 AND service_date = $2 AND state NOT IN ('completed','left')
 ORDER BY position;
```

Rules: keyset pagination everywhere, never `OFFSET`; `EXPLAIN ANALYZE` in a test asserts no sequential scan on `clinical_records`; `pg_stat_statements` enabled; connection pool sized `pool_size=20, max_overflow=10`, which comfortably covers 50 concurrent users.

### 10.3 Caching

| Data | Store | TTL | Invalidated by |
|---|---|---|---|
| Queue positions | Redis hash | 30 s | Any queue mutation |
| Doctor availability | Redis | 60 s | Booking, cancellation, schedule edit |
| Consent grant lookups | Redis | 60 s | Grant or revoke — **immediate** |
| Help articles | In-process | Process life | Deploy |
| Doctor analytics | Redis | 15 min | Time |

Consent caching is the one to be careful with. A revoked grant must stop working immediately (FR4), so `revoke_consent` deletes the cache key inside the same transaction. If that delete fails, the transaction rolls back. A 60-second window of stale consent is not acceptable, and the cache is not permitted to create one.

### 10.4 Why polling, not WebSockets, for the queue

FR17 says "live position". The obvious implementation is a WebSocket. The right one here is a 15-second poll, for three reasons: Pakistani mobile connections drop frequently and WebSocket reconnection logic is a genuine source of bugs; a 15-second staleness window is invisible in a waiting room where the queue moves every several minutes; and a stateless poll scales horizontally without sticky sessions. Redis absorbs the load — 50 patients polling every 15 seconds is roughly 3.3 requests per second, which is nothing.

If a future version needs true real-time, Server-Sent Events over the same endpoint is the upgrade path. It is not needed for the pilot.

### 10.5 Frontend performance

Server components by default; client components only where interactivity demands it. Route-level code splitting, so the patient bundle never ships doctor or admin code. Images served as WebP via `next/image`. Timeline virtualised beyond 50 entries. Urdu web fonts subset and `preload`ed — an unsubsetted Urdu font is a multi-hundred-kilobyte download and would breach NFR1 on its own. Budgets: initial JS ≤ 200 KB gzipped, LCP ≤ 2.5 s on a simulated 3G connection, asserted in CI by Lighthouse.

### 10.6 Worker scaling

Three queues with separate concurrency so that a burst of one job type cannot starve another:

| Queue | Concurrency | Jobs |
|---|---|---|
| `extraction` | 4 | Document reading (slow, external) |
| `embeddings` | 2 | Vector generation (batched) |
| `notifications` | 8 | SMS, email (fast, I/O-bound) |

### 10.7 Load testing

A Locust scenario runs before M6 and again before M7, simulating 50 concurrent users in the realistic mix (60% patients browsing timelines, 25% doctors in consultation, 15% admins managing queues), sustained for 10 minutes. Pass criteria: all §10.1 budgets met at p95, zero 5xx responses, no connection-pool exhaustion. The results go into the evaluation report (NFR27) whether or not they are flattering.

### 10.8 What is deliberately not built

No read replicas, no sharding, no CDN, no autoscaling, no multi-region deployment. NFR5 caps the pilot at 50 concurrent users and NFR6 explicitly says national scale is not a requirement. The design keeps the doors open — stateless API and worker containers, no local session state, all state in Postgres, Redis, or MinIO — so growth is a matter of adding containers rather than rewriting. Building any of it now would consume sprints and demonstrate nothing.

---

## Appendix A — Requirements traceability

| PRD | Module | Endpoint / function | Table |
|---|---|---|---|
| FR1 | identity | `POST /auth/register` | `users`, `patients` |
| FR2 | records | `PATCH /me` | `patients`, `clinical_records` |
| FR3 | admin | `POST /clinics/{id}/doctors` | `doctors` |
| FR4 | consent | `/me/consents` | `consent_grants` |
| FR5 | audit | `GET /me/access-log` | `audit_log` |
| FR6 | identity | `require_role` dependencies | `users` |
| FR7 *(C)* | consent | deferred | — |
| FR8 | records | `GET /patients/{id}/records` | `clinical_records` |
| FR9 *(S)* | records | query params on the above | — |
| FR10 | documents | `POST /documents` | `documents` |
| FR11 | agents | `ExtractionAgent.run` | `extractions` |
| FR12 | documents | `POST /extraction/review` | `extraction_reviews` |
| FR13 *(S)* | agents | `field_confidence` | `extractions` |
| FR14 *(S)* | records | `GET /summary.pdf` | — |
| FR15 | scheduling | `GET /doctors/{id}/availability` | `doctor_schedules` |
| FR16 | scheduling | `/appointments` | `appointments` |
| FR17 | scheduling | `GET /queue-position` | `queue_entries` |
| FR18 | admin | `PUT /doctors/{id}/schedule` | `doctor_schedules` |
| FR19 | admin | `PATCH /queue/{id}` | `queue_entries` |
| FR20 *(S)* | notifications | worker task | `notifications` |
| FR21 | clinical | `GET /doctor/patients/search` | via gateway |
| FR22 | agents | `RetrievalAgent.run` | `clinical_records` |
| FR23 | records | `POST /patients/{id}/records` | `clinical_records` |
| FR24 | clinical | `GET /doctor/today` | `appointments` |
| FR25 *(S)* | clinical | `GET /doctor/analytics` | `clinical_records` |
| FR26 *(C)* | scheduling | `priority_flag` | `queue_entries` |
| FR27 *(C)* | agents | deferred | — |
| FR28 | i18n | `[locale]` routing | — |
| FR29 | agents | `POST /ai/chat` | `chat_messages` |
| FR30 | agents | `GET /interpretation` | `lab_reference_ranges` |
| FR31 | agents | server-appended disclaimer | — |
| FR32 | admin | `POST /ai/translate` | — |
| FR33 | agents | pre/post-check pipeline | `ai_requests` |
| FR34 | orchestrator | `Orchestrator.dispatch` | `ai_requests` |
| FR35 | orchestrator | `AICapability` enum | — |
| FR36 | orchestrator | `AgentResult`, breaker | — |
| FR37 *(S)* | orchestrator | request logging | `ai_requests` |
| D1 | records | `supersedes_id`, revoked UPDATE | `clinical_records` |
| D2 | consent | `ConsentDenied` → 404 | — |
| NFR15–19 | §7 | — | — |
| NFR24–26 | ops | `compose.yaml` | — |

## Appendix B — Decisions requiring sign-off before Sprint 1

1. **Stack confirmation.** §2 records my recommendation, not a team vote. Python/FastAPI plus Next.js suits a Data Science cohort, but if the team is stronger in JavaScript end-to-end, say so now — after M1 the cost of changing is a sprint.
2. **Chatbot scope document.** `docs/chatbot-scope.md` must exist and be approved before FR29 and FR33 enter a sprint (PRD Appendix A, open question 3).
3. **Third-party health data transmission** (§7.6). Advisor approval required before any real patient record reaches an external provider.
4. **SMS provider selection** (§9.5). No longer a Sprint 0 blocker now that OTP is removed — deferred to the FR20 scheduling sprint, still recorded as an ADR.
6. **Removal of registration OTP** (§7.1) and **email as primary credential** (§3.3). Both amend v1.0 and are recorded in the header note. Requires advisor sign-off, as does the absence of any email-verification replacement.
7. **Doctor self-registration** (§4.4 `RegisterRequest`). `FR3` assigns doctor creation to clinic admins; self-signup is now permitted but always lands `is_verified=false`, preserving `FR3`'s access clause. Requires advisor sign-off.
5. **Lab reference range sources** (§3.8). Which published ranges are used, and who reviews them. Ranges without a citation do not go in the table.
