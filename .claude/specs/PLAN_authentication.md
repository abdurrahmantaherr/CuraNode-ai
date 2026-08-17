# PLAN — Authentication & Role-Based Access

**Destination on approval:** `.claude/specs/PLAN_authentication.md`
**Implements:** `.claude/specs/SPEC_authentication.md` (**requires amendment — see PLAN-T0**) · **Baseline:** `.claude/specs/app-foundation.md`
**Stack (confirmed):** PostgreSQL 18, Redis 8, JWT + rotated refresh, Argon2id, Alembic, Docker Compose.
**Credential (confirmed):** **Email + password. No OTP, no SMS, no phone verification.**

> **ID namespace warning.** `T0…T16` below are **tasks**. `SPEC_authentication.md` uses `T1…T23` for **tests**. Cite plan tasks as `PLAN-T*`, spec tests as `SPEC-T*`.

---

## Context

`curanodeai` is greenfield: four locked dependencies, empty `README.md`, no application code (`app-foundation.md` §2). Authentication is the first feature, so it must stand up the entire application skeleton before it can authenticate anyone — ASGI entrypoint, database layer, migrations, error envelope, i18n with RTL, design tokens, and structured logging all land here because nothing exists to build on.

The outcome: a patient or doctor registers with **name, email, and password**, is signed in immediately, and lands on their own dashboard in English or Urdu — with a self-registered doctor holding an account that grants **zero** patient-data access until a clinic admin verifies them.

**Two deliberate deviations from TDD**, both requiring a documentation amendment (`PLAN-T0`):
1. **Email replaces phone as the identity.** TDD §3.3 makes `phone_e164` `UNIQUE NOT NULL` with `email` optional; this inverts that.
2. **No account-verification step.** TDD §7.1 mandates a registration OTP. Removed entirely. `account_status` still exists, but registration writes `active` directly and `pending_verification` becomes unused for self-signup.

---

## 1. Feature Summary

Email-and-password registration with a functional Patient/Doctor toggle on Register and a presentational one on Login. Registration signs the user in immediately — there is no verification step and no third screen. Sessions are a 15-minute JWT access token plus a 14-day single-use refresh token, both in `HttpOnly; Secure; SameSite=Strict` cookies, with reuse-detection revoking the whole token family. Exposes the `Actor` object and four role dependencies every future feature depends on.

**Load-bearing invariant (unchanged):** self-registration creates a doctor *account*, never doctor *access*. `is_verified=false` on insert; `is_verified`/`verified_by`/`verified_at` never appear in a request model. **Removing OTP does not weaken this — account verification and doctor verification are unrelated mechanisms.**

---

## 2. Pre-conditions & Setup

| # | Pre-condition | Check |
|---|---|---|
| 1 | Python ≥3.13, `uv` ≥0.8 | `python -V`, `uv -V` |
| 2 | Node ≥24 LTS, `pnpm` ≥10 | `node -v`, `pnpm -v` |
| 3 | Docker Engine ≥27 + Compose v2 | `docker compose version` |
| 4 | `JWT_SECRET` ≥32 bytes in `.env` | App refuses to boot otherwise (`BL-16`) |
| 5 | Clinics seeded before any doctor registers | Selector reads `GET /clinics` (`BL-21`) |

**No SMS provider is required.** Redis is still required — refresh-token rotation and rate limiting depend on it.

**Assumptions recorded, not silently chosen:** Noto Nastaliq Urdu for `lang="ur"`; `1024px` sidebar breakpoint; password reset deferred (§10.4). Flag before Phase C if any is wrong.

---

## 3. Task Breakdown

### PLAN-T0 — Amend the spec first (blocking) — ✅ **APPLIED 2026-08-17**

**Done. Do not repeat this task.** `SPEC_authentication.md`, `docs/TDD.md` (now v1.1), and `docs/app-foundation.md` have all been amended. `docs/PRD.md` required no change — `FR1` never mandated a credential channel or a verification step. `docs/DESIGN.md` required no change — it had no OTP or phone content.

The table below is retained as the record of what was changed.

| Spec element | Change |
|---|---|
| Conflict **C2** | Rewrite: email is the credential; OTP removed by project-owner decision |
| `AC-01`, `AC-21` | Registration writes `status='active'` and **returns a session**; credential is email |
| `AC-02`, `AC-03`, `AC-04` | **Delete** (OTP verify, OTP failure modes, OTP send throttle) |
| `AC-05`, `AC-06`, `AC-07` | Re-key from `phone_e164` to `email` |
| New `AC-24` | Duplicate email returns `201` success-shaped, creates nothing, sends nothing |
| §4.2, §5.4, §5.4 OTP screen | **Delete** |
| §6.1 | Remove `/auth/verify-otp` and `/auth/resend-otp` |
| §6.2 | `email: EmailStr` required; `phone_e164` optional; `RegisterAccepted` → `SessionOut` |
| `BL-11` (OTP single-use) | **Delete**; renumber or mark void |
| `SPEC-T2`, `T3`, `T4` | **Delete**; renumber `T5`+ or leave gaps and note them |
| §10.4 item 2 | Password reset is now coherent via email — re-open for decision |

**Net acceptance criteria after amendment: 20** (`AC-01`, `AC-05`–`AC-24`).

### Phase A — Foundation (blocking; nothing exists)

- **PLAN-T1 — Repo & runtime skeleton.** TDD §2.1 dependencies **minus** any SMS package; `main.py` (ASGI app, `/healthz`), `settings.py` (fail-fast on short/missing `JWT_SECRET`), `compose.yaml` with `db` + `cache`, `.env.example`. **Exit:** `docker compose up` + `/healthz` → 200.
- **PLAN-T2 — Database layer.** `db/session.py` (async engine, `pool_size=20, max_overflow=10`), `db/types.py` (UUIDv7), `db/models.py` for `users`, `patients`, `clinics`, `doctors`, `clinic_staff`, `audit_log`. Alembic `0001_identity_baseline` with **`email CITEXT UNIQUE NOT NULL`, `phone_e164 TEXT UNIQUE NULL`** (deviation from TDD §3.3), the `verified_has_verifier` CHECK, and `REVOKE UPDATE, DELETE, TRUNCATE ON audit_log`. **Exit:** `upgrade head` → `downgrade base` clean (`G1`).
- **PLAN-T3 — Cross-cutting concerns.** `errors.py` (TDD §8.2 envelope + §8.3 handlers), `i18n/catalogue.py` + `messages/{en,ur}.json`, `logging.py` (structlog redaction — **now redacts `email` in place of `phone_e164`**). **Exit:** a forced error returns a localised envelope; no secret in logs.
- **PLAN-T4 — Redis layer.** `cache.py` with typed helpers for four keyspaces: `refresh:{hash}`, `refresh_family:{id}`, `ratelimit:auth:{ip}`, `lockout:{user_id}`. **Exit:** round-trip test with TTL assertions.

### Phase B — Backend authentication

- **PLAN-T5 — `identity/security.py`.** Argon2id (`time_cost=3, memory_cost=65536, parallelism=4`), `dummy_verify()`, JWT issue/decode (**no `is_verified` claim**), refresh generation + SHA-256 hashing.
- **PLAN-T6 — `identity/service.py`.** `register()` dispatching on the discriminated union (patient → `patients` + `passport_no`; doctor → `doctors` with `is_verified=False`), writing `status='active'` and **returning a session pair directly**; `login()` (dummy-verify guard, lockout); `rotate_refresh()` (family revocation on reuse); `logout()`.
- **PLAN-T7 — `identity/router.py` + `clinics` route.** Five `/auth/*` endpoints plus `GET /me` and `GET /clinics`; cookie set/clear helpers; 5/min rate limit.
- **PLAN-T8 — `deps.py`.** `Actor`, `current_actor`, four role dependencies. `require_verified_doctor` queries `doctors.is_verified` **per request**.
- **PLAN-T9 — `audit/writer.py`.** Unconditional writes for `auth.register`, `auth.login`, `auth.logout`, `auth.lockout`, `auth.refresh_reuse_detected`.

### Phase C — Frontend

- **PLAN-T10 — Next.js scaffold.** App Router under `[locale]`, next-intl, Tailwind 4 with logical-properties lint rule, `<html dir>` from locale, DESIGN.md tokens as CSS custom properties on `:root` / `[data-theme="dark"]`, Noto Nastaliq Urdu subset + preload.
- **PLAN-T11 — Form primitives.** `Input`, `Select`, `Button`, `RoleToggle`, `FormBanner` — the four states SPEC §5.5 defines (focus ring, field error, submitting, banner). **Sets the precedent for every later form.** No `OtpInput`.
- **PLAN-T12 — Auth screens.** Login (email + password, presentational toggle) and Register (functional toggle; doctor fields progressively revealed, unmounted on switch-back). **Two screens only** — no OTP screen. Forgot-password renders as a stub linking to support.
- **PLAN-T13 — Session & routing.** Middleware guarding `(patient|doctor|admin)`, `next` preservation, refresh-then-retry once on 401, role-based landing, unverified-doctor banner.

### Phase D — Verification

- **PLAN-T14 — Backend tests** (amended `SPEC-T1`, `T5`–`T15`, `T20`–`T23`).
- **PLAN-T15 — E2E tests** (`SPEC-T16`–`T20`, `T22`–`T23`), every screen in both locales.
- **PLAN-T16 — Gating checks** `G1`–`G7`, with `G7` release-blocking.

---

## 4. File & Module Map

```
compose.yaml · .env.example
backend/
├─ pyproject.toml · alembic.ini · alembic/versions/0001_identity_baseline.py
├─ src/curanode/
│  ├─ main.py · settings.py · deps.py · errors.py · logging.py · cache.py
│  ├─ db/          session.py · models.py · types.py
│  ├─ identity/    router.py · service.py · schemas.py · security.py
│  ├─ audit/       writer.py
│  └─ i18n/        catalogue.py · messages/{en,ur}.json
├─ ops/scripts/seed_synthetic.py
└─ tests/  unit/ · integration/ · fixtures/
frontend/
├─ messages/{en,ur}.json
├─ src/app/[locale]/ (auth)/{login,register}/ · (patient)/ · (doctor)/ · (admin)/
├─ src/components/  ui/{Input,Select,Button,FormBanner}.tsx · auth/RoleToggle.tsx
├─ src/lib/  api/client.ts · hooks/useSession.ts · middleware.ts
├─ src/styles/tokens.css
└─ e2e/auth.spec.ts
```

**Removed versus the OTP design:** `identity/otp.py`, `notifications/providers.py`, `components/auth/OtpInput.tsx`, `app/[locale]/(auth)/verify-otp/`.

**Module boundary (`import-linter`, TDD §5.2):** `identity` may import only `db`, `audit`, `i18n`.

---

## 5. Scaffolding Notes

- **Nothing is reusable** — the repository has no code. Every path above is new.
- **Build foundations generically, not auth-shaped.** `errors.py`, `i18n`, `cache.py`, and the form primitives are inherited by every later feature.
- **Order:** `PLAN-T0` blocks everything. Phase A blocks B. Within B, `T5` → `T6` → `T7`; `T8` needs `T5`. Phase C can start once `T7` publishes the OpenAPI schema.
- **RTL from the first component** (TDD §2.3) — the lint rule lands in `PLAN-T10`, before any screen exists.
- Generate the typed frontend client from FastAPI's OpenAPI output; do not hand-write request types.
- **`notifications/` is not created here.** It returns when appointment reminders (`FR20`) need it.

---

## 6. Integration Points

| Point | Contract | Consumer |
|---|---|---|
| `Actor` + four role dependencies | SPEC §6.3 | **Every future endpoint.** The consent gateway builds directly on this. |
| Error envelope | TDD §8.2 | All features; clients read `retryable`, never infer from status |
| i18n catalogue | `message_key` on every string | All features (`FR28`) |
| Design tokens | CSS custom properties | All UI (`FR-12`) |
| `audit_log` writer | Append-only, unconditional | Consent gateway (`FR5`) |
| Unverified `doctors` rows | `is_verified=false` | Admin verification queue (`FR3`) |
| `users.email` as identity | Unique, required | Password reset, notifications (`FR20`) |

**Deliberately not touched:** consent, records, documents, scheduling, orchestrator.

---

## 7. Test Plan

Full matrix is SPEC §11 **as amended by `PLAN-T0`**. Execution shape:

- **Unit** — Argon2 parameters, JWT encode/decode, redaction processor.
- **Integration** (pytest-asyncio, real Postgres + Redis in Compose) — both registration branches, duplicate-email handling, login/lockout/timing, refresh rotation and reuse, the full role × dependency matrix.
- **E2E** (Playwright) — both screens in `en` **and** `ur`; role landing and cross-role redirect; focus/error/submitting states.
- **No test makes a network call** (TDD §9.2). Coverage ≥90% on `identity/` and `deps.py`.

**Three tests that gate release:** `SPEC-T6` (timing-equal auth failures), `SPEC-T22` (unverified doctor blocked from every clinical route), `G7` (no client value can reach `is_verified`).

**Manual verification:** `docker compose up` → register as Doctor → **land signed-in immediately** → confirm the awaiting-verification banner and `403` on any clinical route → flip `is_verified` in psql → confirm access on the **next request** without re-login.

---

## 8. Edge Cases & Error Handling

| Case | Behaviour |
|---|---|
| Duplicate email at registration | `201` success-shaped, creates nothing, no session issued — no email-enumeration oracle (`AC-24`) |
| Unknown email at login | Dummy Argon2 verify, then generic `401` — timing must not leak (`BL-04`) |
| Login during lockout | Generic `401`; lock **not** extended (`BL-10`) |
| Suspended account | Generic `401`, indistinguishable from wrong password |
| Refresh token replay | Whole family revoked, cookies cleared, `401` (`BL-06`) |
| `is_verified` injected into a payload | Discarded, never honoured (`G7`) |
| Doctor payload missing PMDC/clinic | `422` — never a silent patient fallback |
| Unknown `primary_clinic_id` | `422` |
| Role toggle mismatch at login | Silently routed by account role, no message (`BL-19`) |
| Email case/whitespace variance | Normalised: trimmed, lower-cased, stored `CITEXT` — `A@b.com` and `a@b.com` are one account |
| Redis unavailable | Auth fails closed with `503`; never degrade to unauthenticated access |
| Clock skew on JWT | 30s leeway on `exp` only |
| Passport collision | Retry ×5, then `INTERNAL_ERROR` |

**Accepted risk of removing OTP:** email addresses are unverified, so a user may register with an address they do not control, and a typo is unrecoverable without password reset. Acceptable for a synthetic-data pilot (`NFR19`); revisit before any real patient data (§10.4).

---

## 9. Rollback Strategy

Feature branch `feat/authentication`, squash-merged. No production data exists, so rollback is cheap.

1. **Code** — revert the merge commit.
2. **Schema** — `alembic downgrade base`. `0001` is the first migration, so downgrade returns an empty database. Verified by `G1` before merge.
3. **Redis** — flush the four keyspaces; all TTL-bounded, no durable state. Effect is forced re-login.
4. **Containers** — `docker compose down -v`.

**Partial failure:** the frontend can be reverted independently of the backend, which stands alone behind its OpenAPI contract. The reverse is not true — revert Phase C first and keep the API.

---

## 10. Definition of Done

- [ ] `SPEC_authentication.md` amended per `PLAN-T0`; no document still references OTP or phone-primary identity.
- [ ] All 20 amended acceptance criteria pass.
- [ ] Amended SPEC tests plus `G1`–`G7` green in CI.
- [ ] `G7` privilege-escalation sweep passes — **release-blocking**.
- [ ] Patient and doctor can register with email and are signed in immediately.
- [ ] A self-registered doctor is denied **every** `VerifiedDoctorDep` route until verified.
- [ ] Both screens render correctly in `en` and `ur`, `dir` driven by locale; no physical-direction utility survives the lint rule.
- [ ] `import-linter` passes; `identity` imports only `db`, `audit`, `i18n`.
- [ ] No password, token, or email in any log at any level.
- [ ] `docker compose up` on a clean machine brings the stack up in <1 hour from docs alone (`NFR25`).
- [ ] Login p95 ≤400 ms API-side; auth pages LCP ≤2.5 s on simulated 3G.
- [ ] Coverage ≥90% on `identity/` and `deps.py`.
- [ ] `PRD.md` and `TDD.md` amended for doctor self-registration **and** email-without-OTP, or both deviations formally accepted by the advisor.

---

## Verification (end-to-end)

```bash
docker compose up -d db cache
cd backend && uv sync && uv run alembic upgrade head
uv run python -m ops.scripts.seed_synthetic
uv run uvicorn curanode.main:app --reload      # /healthz → 200, /docs → OpenAPI
uv run pytest -q --cov=curanode                # amended SPEC tests + G1–G7
cd ../frontend && pnpm install && pnpm dev     # /en/login and /ur/login
pnpm exec playwright test                      # E2E, both locales
```

---

## Open decisions (unchanged unless noted)

| # | Decision | Status |
|---|---|---|
| 1 | Stack | **Resolved** — Postgres + Redis + JWT |
| 2 | **Password reset** | **Re-opened.** Now coherent: email is the identity, and the designed reset-link screen fits. Strongly recommended as the next feature — without OTP or reset, a forgotten password is unrecoverable. |
| 3 | Email verification link | **Deferred.** Explicitly excluded here; revisit before real patient data. |
| 4 | Urdu font — Noto Nastaliq Urdu | Assumed |
| 5 | Breakpoint — `1024px` | Assumed |
| 6 | CNIC dropped from Register | Confirm not required |
| 7 | Doctor self-registration amends `FR3`/TDD §4.4 | Needs doc amendment |
| 8 | Email-without-OTP amends TDD §3.3/§7.1 | **New** — needs doc amendment |
| 9 | Abuse surface: unverified doctor accounts | Mitigated by rate limit; invite code if it becomes real |
