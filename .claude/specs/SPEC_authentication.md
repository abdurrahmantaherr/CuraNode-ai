# SPEC — Authentication & Role-Based Access

| | |
|---|---|
| **Feature** | Authentication & Role-Based Access |
| **Spec ID** | `SPEC_authentication` |
| **Implements** | PRD `FR1`, `FR3`, `FR6`, `NFR10`, `NFR12`, `NFR13`, `NFR14`, `NFR15` · TDD §4.4, §7.1, §7.2, §8 |
| **Baseline** | `.claude/specs/app-foundation.md` — `FR-03`, `FR-04`, `FR-09`, `FR-12`, `BR-01` |
| **Status** | Ready for implementation |
| **Precedence** | PRD wins on behaviour · TDD wins on implementation · DESIGN.md wins on visual detail. Conflicts are recorded in §10.4, **not** silently resolved. |

---

## ⚠️ Read before implementing — three resolved conflicts

The feature request asked for *"authenticate users as either Patient or Doctor."* Taken literally that contradicts the PRD. Resolutions below are binding for this spec.

**C1 — The Patient/Doctor toggle is built as designed, gated by verification.**
DESIGN.md shows the toggle on both Login and Register. TDD's `RegisterRequest` hardcodes `role="patient"`, and PRD `FR3` assigns doctor registration to clinic admins.
→ **Decision (project owner, overriding the TDD literal): the toggle ships on both screens.** It is reconciled with `FR3` as follows, and the reconciliation is mandatory:

- **Register** — selecting *Doctor* creates a real doctor account with `is_verified = false`. The design's own copy on that tile — *"PMDC verification required"* — is the contract: **self-signup, then clinic-admin verification.** An unverified doctor can reach no patient data whatsoever (`VerifiedDoctorDep` → `403`), so `FR3`'s binding clause — *"a doctor cannot access any patient record until verified"* — holds exactly. Admin-initiated doctor creation remains a separate, additional path in the admin feature.
- **Login** — the toggle is **client-side presentation only**. It is never transmitted and never influences authorisation. Role comes from the account; the server's response drives the redirect. A user who picks *Doctor* but owns a patient account is silently routed to the patient dashboard — no error, because a mismatch message would leak whether a doctor account exists for that number.

**The security invariant that must survive implementation:** self-registration may create a doctor *account*; it may never create doctor *access*. Any change that lets an unverified doctor read a patient record breaks `FR3`, `NFR16`, and `BR-01`.

**C2 — Registration is email + password. No OTP, no SMS, no verification step.**
TDD v1.0 made `phone_e164` the unique identity with a mandatory OTP step. **Removed by project-owner decision**; TDD v1.1 §3.3/§7.1 now matches.
→ **Primary credential is `email`** (`UNIQUE NOT NULL`); `phone_e164` is optional. A new account is `active` on creation and **registration signs the user in immediately** — exactly what the designed *"Create account"* button does. There is no OTP screen and no third auth screen.

*Accepted risk:* no channel proves the address, so a typo at registration is unrecoverable until password reset exists (§10.1). Acceptable while all data is synthetic (`NFR19`); revisit before real patient data.

**C3 — Three roles exist, two get UI in this feature.**
PRD `FR6` requires patient, doctor, and clinic_admin. DESIGN.md has no admin screens.
→ **All three roles are implemented at the API and dependency layer.** Only patient and doctor get screens here. `clinic_admin` authenticates successfully and lands on a placeholder; its console is a separate feature.

---

## 1. Feature Overview

This is the **first feature of the project** and the repository is greenfield (`app-foundation.md` §2). It therefore establishes the application's foundations in addition to authentication itself.

**In this feature's remit:**
- Patient **and doctor** self-registration with email and password (`FR1`), signed in immediately, doctors landing in an unverified state.
- Credential login for all three roles, returning a role-appropriate landing route (`FR6`).
- Session issuance and lifecycle: short-lived access token, rotated refresh token, secure logout (TDD §7.1).
- The `Actor` object and the four FastAPI role dependencies that every future endpoint will depend on (TDD §7.2).
- Account security: Argon2id hashing, failed-login lockout, per-endpoint rate limiting.

**Foundations this feature bootstraps** (unavoidable — nothing exists yet). Each must be built generically, not auth-specific, because every later feature inherits it:

| Foundation | Why it lands here | Contract |
|---|---|---|
| ASGI entrypoint + settings | Nothing runs today | `app-foundation.md` `FR-03` |
| SQLAlchemy async session + Alembic | `users` must persist | TDD §2.1, §3.1 |
| Error envelope + exception handlers | First user-facing errors | TDD §8.2, §8.3 |
| i18n catalogue (`en`/`ur`) + RTL layout | TDD §2.3 makes RTL a Sprint-1 constraint | `FR28`, `NFR13` |
| Design-token stylesheet + form primitives | First form in the product | DESIGN.md §2, §5 |
| Structured logging with PII redaction | Passwords and email addresses must never reach a log | TDD §7.8 |

**Explicitly not in remit:** the consent gateway, clinical records, and anything reading patient data. This feature builds the *lock*; it does not build what is behind the door.

---

## 2. User Story

**Primary**

> As a **patient in Pakistan** who has never held a copy of my own medical record, I want to create an account with my mobile number and sign in securely, so that I have one permanent health identity that follows me between clinics and that only I control access to.

**Secondary**

> As a **doctor in a high-volume clinic**, I want to sign in quickly and land directly on today's work, so that authentication costs me none of the five to eight minutes I have per patient.

> As a **clinic administrator**, I want a doctor who signs themselves up to arrive in my queue as unverified and powerless, so that choosing "Doctor" on a signup form grants an account but never grants access to a single patient record.

> As a **patient who reads Urdu more comfortably than English**, I want to register and sign in entirely in Urdu with correct right-to-left layout, so that the first screen I meet is not a barrier.

---

## 3. Acceptance Criteria

| ID | Criterion |
|---|---|
| **AC-01** | Submitting valid registration details with role **Patient** creates a `users` row with `role='patient'`, `status='active'`, an Argon2id `password_hash`, and a linked `patients` row carrying a unique, permanently assigned `passport_no`. Response is `201`, **sets both session cookies**, and the user is signed in. |
| ~~AC-02~~ | *Void — OTP verification removed (C2).* |
| ~~AC-03~~ | *Void — OTP failure modes removed (C2).* |
| ~~AC-04~~ | *Void — OTP send throttle removed (C2). General auth rate limiting survives as `AC-14`.* |
| **AC-05** | Login with a correct email/password pair on an `active` account returns `200`, sets `HttpOnly; Secure; SameSite=Strict` access and refresh cookies, resets `failed_logins` to 0, stamps `last_login_at`, and returns the caller's role and landing route. |
| **AC-06** | Login with a wrong password, an unknown email, or a `suspended` account returns an identical `401 UNAUTHENTICATED` body in every case, and takes materially the same time (no early return before password verification). Nothing reveals whether the account exists. |
| **AC-07** | After **10** consecutive failed logins the account locks for **15 minutes**; attempts during the lock return `401` with the same generic body and do **not** extend the lock. A successful login after expiry clears the counter. |
| **AC-08** | A doctor whose `doctors.is_verified` is `false` authenticates successfully and reaches their shell, but every `VerifiedDoctorDep` route returns `403 FORBIDDEN`. Verification is re-checked **per request**, never cached from login. |
| **AC-09** | Calling refresh with a valid, unconsumed refresh token issues a new access **and** a new refresh token, and marks the presented token consumed. The old refresh token is thereafter unusable. |
| **AC-10** | Presenting an already-consumed refresh token revokes the **entire token family**, clears both cookies, and returns `401`. The user must log in again. |
| **AC-11** | Logout revokes the presented refresh token's family, clears both cookies with matching attributes, and returns `204`. It is idempotent — a second call also returns `204`. |
| **AC-12** | `GET /api/v1/me` with a valid access cookie returns the caller's identity projection (id, role, name, locale, status, verification flag, clinic ids, passport number where applicable). Without a valid cookie it returns `401`. |
| **AC-13** | `PatientDep`, `VerifiedDoctorDep`, and `ClinicAdminDep` each return `403 FORBIDDEN` on role mismatch. `403` is **never** used for a consent failure — that is `404` and belongs to a later feature (`BR-02`). |
| **AC-14** | Every `/auth/*` endpoint is rate-limited to **5 requests per minute**, keyed by IP for unauthenticated calls. Exceeding returns `429` with `Retry-After`. |
| **AC-15** | Passwords are hashed with Argon2id at `time_cost=3, memory_cost=65536, parallelism=4`. No log line at any level — including `DEBUG` — contains a password, a token, or a raw email address. |
| **AC-16** | On successful authentication the user is routed by role: patient → patient dashboard, doctor → doctor dashboard, clinic_admin → admin placeholder. Deep-linking to another role's route redirects to the caller's own landing route. |
| **AC-17** | An unauthenticated request for a protected page redirects to login, preserving the intended destination, and returns to it after successful sign-in. |
| **AC-18** | Every screen, label, validation message, and error in this feature renders correctly in both `en` and `ur`, with `<html dir>` driven by locale. No physical-direction CSS utility appears in any component. |
| **AC-19** | Every form field exposes a visible focus indicator, an error state with associated message text, and a submitting state that disables the submit control and prevents double submission. |
| **AC-20** | Both cookies are `HttpOnly`, `Secure`, `SameSite=Strict`, `Path=/`. No token is ever written to `localStorage`, `sessionStorage`, or a URL. |
| **AC-21** | Submitting valid registration details with role **Doctor** creates a `users` row with `role='doctor'` plus a `doctors` row with `is_verified=false`, `verified_by=NULL`, `verified_at=NULL`, and the submitted `pmdc_number`, `specialty`, and `primary_clinic_id`. No `patients` row and no `passport_no` are created. The `verified_has_verifier` constraint is satisfied. |
| **AC-22** | A self-registered doctor is `status='active'` and signed in immediately, but **every** `VerifiedDoctorDep` route returns `403` until a clinic admin verifies them. Their shell shows a persistent "awaiting verification" state. Selecting *Doctor* at registration grants an account and grants **zero** patient-data access. |
| **AC-23** | The Login role toggle is enforced **after** the password is verified. Signing in with *Doctor* selected on a patient account (or vice versa) returns `403`, shows a clear message naming the selected role, and issues **no** session cookies; the freshly-minted token pair is discarded. A wrong password with any role selected still returns the generic `401` — the mismatch message is never reachable without the correct password, so it is not an enumeration oracle. |
| **AC-24** | Registering with an email that already exists returns `201` with a body identical to a successful registration, creates no row, and issues **no** session cookies. Registration must not become an email-enumeration oracle. |

---

## 4. Functional Specifications

### 4.1 Registration (`FR1`, `FR3`)

Registration is **role-branched** on the client's selection. Both branches share steps 1–4 and 7–8.

1. Client submits full name, `email`, password, optional `phone_e164`, `preferred_locale`, and `role` ∈ {`patient`, `doctor`}. The doctor branch additionally submits `pmdc_number`, `specialty`, and `primary_clinic_id`.
2. Server validates per §6.2 against the discriminated union. On failure → `422` with field-level details. A doctor payload missing any doctor-only field is a validation error, never a silent patient fallback.
3. Normalise the email: trim, lower-case, store as `CITEXT`. `A@b.com` and `a@b.com` are one account.
4. If the email already exists: **return `201` with the same shape as success, create nothing, and issue no cookies.** Registration must not become an email-enumeration oracle (`AC-24`). The genuine owner is unaffected; an attacker learns nothing.
5. Hash the password (Argon2id). Generate UUIDv7 for `users.id`.

**6a — Patient branch.** Insert `users` (`role='patient'`, `status='active'`) and `patients` in **one transaction**. Generate `passport_no` as `CN-XXXX-XXXX` from a Crockford base-32 alphabet excluding vowels and ambiguous glyphs (`I`, `L`, `O`, `U`). Retry on collision up to 5 times, then fail with `INTERNAL_ERROR`.

**6b — Doctor branch.** Insert `users` (`role='doctor'`, `status='active'`) and `doctors` in **one transaction**, with:

```
is_verified   = FALSE          # non-negotiable — never settable by the client
verified_by   = NULL
verified_at   = NULL
pmdc_number   = <submitted>    # recorded, NOT validated against any registry (TDD §3.3)
specialty     = <submitted>
primary_clinic_id = <submitted>
```

No `patients` row and no `passport_no` are created. `primary_clinic_id` must reference an existing clinic — it is `NOT NULL` in TDD §3.3, and it is what routes this doctor into the correct clinic admin's verification queue. An unknown clinic id → `422`.

7. **The client may never set `is_verified`, `verified_by`, `verified_at`, or `status`.** These fields are absent from every request model. If they appear in a payload they are discarded, not honoured.
8. Issue the session pair (§4.3), set both cookies, and return `201 SessionOut` — **the user is signed in and lands on their role's dashboard.**
9. Write `auth.register` to `audit_log`, recording the assigned role.

### 4.1.1 The unverified-doctor state (`FR3`)

A self-registered doctor is `status='active'` with `is_verified=false` from the moment of registration. This state is fully specified because the design does not show it:

- They authenticate normally and receive a session.
- `ActorDep` resolves with `is_verified_doctor=false`.
- **Every** `VerifiedDoctorDep` route returns `403 FORBIDDEN`. There is no partial access, no read-only mode, and no preview of patient data.
- Their shell renders a persistent, non-dismissible banner (§5.4.1) explaining that a clinic administrator must verify them.
- The row is visible to that clinic's admins as a pending verification. Consuming that queue belongs to the admin feature; **this feature creates the rows it reads.**

### 4.2 *(void — OTP issuance and verification, removed per C2)*

No account-verification channel exists. `account_status` retains `pending_verification` in the enum for future use, but **no self-registration path writes it**.

### 4.3 Session issuance (TDD §7.1)

**Access token** — JWT, `HS256`, secret ≥32 bytes from environment (startup fails if absent or short). Lifetime **15 minutes**. Claims: `sub` (user id), `role`, `locale`, `jti`, `iat`, `exp`. **`is_verified` is deliberately excluded** — it is read from the database per request so an admin's revocation takes effect on the next call, not in fifteen minutes (`AC-08`).

**Refresh token** — 32 bytes of CSPRNG entropy, base64url-encoded. Lifetime **14 days**. Stored **hashed** in Redis under `refresh:{token_hash}` with value `{user_id, family_id, issued_at, consumed: false}`.

**Rotation and reuse detection** — every refresh consumes the presented token and issues a new one in the same `family_id`. Presenting a token already marked `consumed` is treated as theft: **delete every key in that family** (`refresh_family:{family_id}` holds the set) and force re-login (`AC-10`).

**Delivery** — both tokens in cookies: `HttpOnly`, `Secure`, `SameSite=Strict`, `Path=/`. Access cookie `Max-Age` 900s; refresh cookie 1209600s. Never in a response body, never in a URL.

### 4.4 Login (`FR6`)

1. Normalise the submitted email (trim, lower-case), then look up by `email`. **If not found, still execute a dummy Argon2 verify** against a fixed hash, then return the generic `401` (`AC-06`). Timing must not leak existence.
2. If `locked_until > now()` → generic `401`, do not extend the lock.
3. Verify the password. On failure: increment `failed_logins`; at 10, set `locked_until = now() + 15 minutes`; return generic `401`.
4. If `status != 'active'` → generic `401`. With OTP removed, `suspended` is the only reachable non-active state, and it must stay indistinguishable from a wrong password.
5. On success: reset `failed_logins`, clear `locked_until`, stamp `last_login_at`, issue the session pair, write `audit_log` (`action='auth.login'`), return role and landing route.

### 4.5 Actor resolution and role gates (TDD §7.2)

`current_actor` runs on every authenticated request: decode and validate the access JWT → load the user → confirm `status='active'` → resolve `clinic_ids` (from `clinic_staff` for admins, `doctors.primary_clinic_id` for doctors) → resolve `is_verified_doctor` **by live query** → construct `Actor`.

Exactly four dependencies exist. **No endpoint anywhere in the codebase may perform an ad-hoc role check** (TDD §7.2):

```python
ActorDep          = Annotated[Actor, Depends(current_actor)]
PatientDep        = Annotated[Actor, Depends(require_role("patient"))]
VerifiedDoctorDep = Annotated[Actor, Depends(require_verified_doctor)]
ClinicAdminDep    = Annotated[Actor, Depends(require_role("clinic_admin"))]
```

### 4.6 Frontend session handling

- Middleware guards every route under `[locale]/(patient|doctor|admin)`. Unauthenticated → redirect to `/{locale}/login?next=<path>`.
- A `401` on any call triggers **one** silent refresh-then-retry. A second `401` clears client state and redirects to login (TDD §8.3).
- Role mismatch on a route → redirect to the caller's own landing route, never a raw `403` page.
- Locale switch is a route change under `[locale]`, preserving the current path (`FR28`).

---

## 5. UI/UX Requirements

Design source of truth is `docs/DESIGN.md`. Only deltas and gaps are specified here.

### 5.1 Inherited baseline (do not re-derive)

Auth screens use the **centred single-card layout**: card at `max-width 430px` (login/forgot) or `470px` (register), `--surface` background, `1px solid var(--line)`, `16px` radius, `24px` padding, `var(--shadow)`; brand logo mark above a serif `400 30px/1.15` heading; `--muted` subtitle; stacked labelled fields; full-width primary button; footer switch link. Mono footer line: *"Records are encrypted · access is logged."*

Inputs: `11px 13px`, `10px` radius, `1px solid var(--line-2)`, `--surface-2` fill, `13.5px`. Labels `600 12px` above the field. Primary button: `--brand` fill, white text, `10px` radius, weight 600, hover `--brand-2`.

### 5.2 The role toggle — build exactly as designed

Both toggles ship with the designed selected-state treatment: `1px solid var(--brand)` border, `var(--brand-soft)` fill, `var(--brand)` text; unselected is `1px solid var(--line-2)`, `var(--surface-2)` fill, `var(--ink)` text. Two equal columns, `8px` gap, `10px` radius.

**Login — `I am a`** (`11px` padding, label only). **Client-side state; never sent to the server** (`AC-23`). It sets the card's subheading copy and nothing else. The post-login redirect is driven solely by `SessionOut.role`. Do **not** add a `role` field to `LoginRequest`, and do not warn the user when their selection differs from their account.

**Register — `Register as`** (`12px` padding, two-line tiles). This one is functional and drives the branch in §4.1. Keep the designed subtitles verbatim — they are the user's notice of what they are signing up for:

| Tile | Subtitle | Effect |
|---|---|---|
| Patient | *"Hold and share my records"* | Patient branch (§4.1 5a) |
| Doctor | *"PMDC verification required"* | Doctor branch (§4.1 5b), `is_verified=false` |

Selecting *Doctor* progressively reveals the three doctor-only fields (§5.3) with the designed `16px` card gap. Switching back to *Patient* clears and unmounts them so they cannot be submitted.

### 5.3 Changed and added fields

| Screen | Design | Build |
|---|---|---|
| Login | Email + password + role toggle | **Email + password + role toggle** — as designed (toggle presentational) |
| Register (both) | Name, CNIC, email, password ×2, role, consent | **Name, email, password ×2, role toggle, consent** — CNIC removed, optional phone added |
| Register (doctor only) | — | **+ PMDC number, specialty, clinic** — all three required |

**Doctor-only fields have no design.** Build them on the established input pattern (§5.1):
- **PMDC number** — text input, `13.5px`, mono-hinted placeholder `e.g. 41192`. Helper text below at `12px var(--muted)`: *"Recorded for your clinic to verify. Not checked against any registry."* This is honest — TDD §3.3 records but does not validate it.
- **Specialty** — `<select>` styled to match the input pattern.
- **Clinic** — `<select>` of existing clinics, showing name and city. **Required**, because it determines which administrator verifies this doctor.

### 5.4.1 New — unverified-doctor banner (no design exists)

Rendered persistently in the doctor shell whenever `is_verified_doctor` is false. Use the designed `--warn` status treatment: `--warn-soft` fill, `1px solid var(--warn)`, `12px` radius, `14px` padding, warning glyph, `13px` message. Non-dismissible. Copy: *"Your account is awaiting verification by {clinic name}. You'll be able to access patient records once verified."* No patient-data affordance is rendered anywhere behind it.

### 5.4 *(void — OTP verification screen, removed per C2)*

**This feature ships two auth screens only: Login and Register.** There is no third screen and no `OtpInput` component. On successful registration the user is signed in and redirected straight to their role's dashboard.

### 5.5 New patterns this feature must define

`app-foundation.md` §12 item 10 records these as missing. **This feature sets the precedent for the entire product** — every later form copies it.

**Focus indicator (accessibility defect fix).** DESIGN.md sets `outline:none` with no replacement, failing `NFR14`. Define: `outline: 2px solid var(--brand); outline-offset: 2px` on `:focus-visible` for every interactive element. Never remove an outline without replacing it.

**Field error state.** Border → `var(--err)`; message below at `12px`, `var(--err)`, weight 500, prefixed by a `14px` error glyph. Message is bound via `aria-describedby`; the input carries `aria-invalid="true"`. Errors appear on blur and on submit, never on first keystroke.

**Submitting state.** Button text is replaced by a `cnSpin` spinner at `14px` plus the localised label; button takes `--brand` at 70% opacity, `cursor: not-allowed`, `aria-busy="true"`. The form is disabled wholesale for the duration.

**Form-level error banner.** For non-field errors (locked account, rate limit): `--err-soft` fill, `1px solid var(--err)`, `12px` radius, `14px` padding, error glyph + `13px` message. Sits directly above the submit button.

**Success callout.** Reuse the designed `--ok-soft` block from the Forgot-password screen.

### 5.6 Urdu and RTL (`FR28`, `NFR13`, TDD §2.3)

Non-negotiable from the first component — retrofitting is the specific failure TDD §2.3 warns against.

1. **Logical CSS properties only.** `ms-*`, `me-*`, `ps-*`, `pe-*`, `text-start`, `text-end`. Never `ml-*`, `pl-*`, `text-left`. A lint rule fails CI on any physical-direction utility.
2. `<html dir>` set from locale in the root layout. **No component reads or hardcodes direction.**
3. **The three design fonts have no Urdu coverage** (`app-foundation.md` §12 item 6). Add **Noto Nastaliq Urdu** for `lang="ur"`, subset and `preload`ed — an unsubsetted Urdu font is a multi-hundred-kilobyte download that would breach `NFR1` on its own (TDD §10.5). Latin fonts remain for `lang="en"`.
4. Email addresses and the optional `+92` phone field stay **LTR in both locales** (`dir="ltr"` on those inputs) — addresses and digit sequences are not mirrored.
5. Every component ships a Playwright assertion in both locales. *A component that has not been seen in Urdu is not done.*

### 5.7 Accessibility

Touch targets ≥`44×44px` (`NFR14`). All inputs have programmatically associated `<label>`s. The password field has a show/hide toggle with an `aria-label`. Form errors are announced via `role="alert"`. Full keyboard operability with a visible focus ring throughout. Text contrast ≥4.5:1 in **both** themes — verify `--muted` on `--surface-2` in dark mode specifically.

---

## 6. API Contract

Base path `/api/v1`. All responses use the TDD §8.2 envelope on error. Every request carries `X-Request-Id`, echoed on the response.

### 6.1 Endpoints

| Method | Path | Auth | Success | Requirement |
|---|---|---|---|---|
| `POST` | `/auth/register` | — | `201` | `FR1` |
| `POST` | `/auth/login` | — | `200` | `FR6` |
| `POST` | `/auth/refresh` | refresh cookie | `200` | `FR6` |
| `POST` | `/auth/logout` | access cookie | `204` | `FR6` |
| `GET` | `/me` | access cookie | `200` | `FR6` |
| `GET` | `/clinics` | — | `200` | Serves the doctor-registration clinic selector (`BL-21`) |

> `/auth/verify-otp` and `/auth/resend-otp` are **removed** per C2. `POST /auth/register` now returns `201` with cookies set, so it is the only endpoint needed to get a new user signed in.

### 6.2 Request and response models

```python
from __future__ import annotations
from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, EmailStr, Field

# ── Registration ──────────────────────────────────────────────────────────
class _RegisterBase(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr                                    # primary credential
    password: str = Field(min_length=10, max_length=128)
    phone_e164: str | None = Field(default=None, pattern=r"^\+92[0-9]{10}$")
    preferred_locale: Literal["en", "ur"] = "en"

class PatientRegisterRequest(_RegisterBase):
    role: Literal["patient"] = "patient"

class DoctorRegisterRequest(_RegisterBase):
    """FR3: creates the account only. is_verified is server-set FALSE and is
    deliberately absent here — it is not a client-supplied field."""
    role: Literal["doctor"]
    pmdc_number: str = Field(min_length=3, max_length=32)
    specialty: str = Field(min_length=2, max_length=80)
    primary_clinic_id: UUID

RegisterRequest = Annotated[
    PatientRegisterRequest | DoctorRegisterRequest,
    Field(discriminator="role"),
]

# NOTE: `RegisterAccepted` is removed. Registration returns `SessionOut` with
# cookies set — the user is signed in immediately (C2). A duplicate email
# returns an identical SessionOut-shaped 201 with NO cookies (AC-24).

# ── Login ─────────────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    """The Login screen's Patient/Doctor toggle is presentational and is NOT
    represented here. Adding a `role` field would let the client influence
    authorisation — role comes from the account (AC-23, BL-19)."""
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)

class SessionOut(BaseModel):
    """Returned by register, login, and refresh. Tokens are in cookies only."""
    user_id: UUID
    role: Literal["patient", "doctor", "clinic_admin"]
    full_name: str
    locale: Literal["en", "ur"]
    landing_route: str                          # "/patient", "/doctor", "/admin"
    access_expires_at: datetime

# ── Identity ──────────────────────────────────────────────────────────────
class MeOut(BaseModel):
    user_id: UUID
    role: Literal["patient", "doctor", "clinic_admin"]
    full_name: str
    email: EmailStr
    phone_masked: str | None
    locale: Literal["en", "ur"]
    status: Literal["pending_verification", "active", "suspended"]
    is_verified_doctor: bool                    # false for non-doctors
    clinic_ids: list[UUID]                      # empty for patients
    passport_no: str | None                     # patients only
    last_login_at: datetime | None

# ── Actor (TDD §4.2) — internal, never serialised to a client ─────────────
class Actor(BaseModel):
    user_id: UUID
    role: Literal["patient", "doctor", "clinic_admin"]
    clinic_ids: frozenset[UUID]
    is_verified_doctor: bool
    locale: Literal["en", "ur"]
    request_id: str
    ip_address: str | None
```

### 6.3 Service and dependency signatures

```python
# identity/service.py
async def register(session: AsyncSession, body: RegisterRequest,
                   *, request_id: str, ip: str | None) -> tuple[SessionOut, TokenPair | None]:
    """Dispatches on body.role to the patient or doctor branch (§4.1).
    Both branches are one transaction. is_verified is never read from `body`.
    Returns TokenPair=None on the duplicate-email path so the router emits an
    identical body with no cookies (AC-24)."""
async def login(session: AsyncSession, body: LoginRequest,
                *, request_id: str, ip: str | None) -> tuple[SessionOut, TokenPair]: ...
async def rotate_refresh(session: AsyncSession, presented: str,
                         *, request_id: str) -> tuple[SessionOut, TokenPair]: ...
async def logout(session: AsyncSession, presented: str | None) -> None: ...

# identity/security.py
def hash_password(raw: str) -> str: ...
def verify_password(raw: str, hashed: str) -> bool: ...
def dummy_verify() -> None: ...                       # constant-time guard, §4.4.1
def issue_access_token(user_id: UUID, role: str, locale: str) -> tuple[str, datetime]: ...
def decode_access_token(token: str) -> AccessClaims: ...   # raises InvalidToken
def new_refresh_token() -> tuple[str, str]: ...            # (raw, sha256_hash)

# identity/otp.py — REMOVED. No OTP module exists (C2).

# deps.py
async def current_actor(request: Request, session: SessionDep) -> Actor: ...
def require_role(role: str) -> Callable[..., Awaitable[Actor]]: ...
async def require_verified_doctor(actor: ActorDep, session: SessionDep) -> Actor: ...
```

### 6.4 Error codes

| Code | HTTP | Retryable | Raised when |
|---|---|---|---|
| `VALIDATION_FAILED` | 422 | no | Schema violation; missing doctor-only field; unknown `primary_clinic_id` |
| `UNAUTHENTICATED` | 401 | no | Bad credentials, unknown account, suspended, locked, invalid/expired/reused token |
| ~~`ACCOUNT_UNVERIFIED`~~ | — | — | *Void — no account-verification state is reachable (C2).* |
| `FORBIDDEN` | 403 | no | Role mismatch, unverified doctor. **Never a consent failure** |
| `RATE_LIMITED` | 429 | yes | Auth endpoint throttle (5/min). Carries `Retry-After` |
| `INTERNAL_ERROR` | 500 | yes | Unexpected. Generic message; detail logged only |

---

## 7. Data Requirements

Schema is defined in TDD §3.3 and **not restated**. This feature creates migration `0001_identity_baseline` covering:

**Enums** — `user_role`, `account_status`, `locale_code` (TDD §3.2).

**Tables** — `users`, `patients`, `clinics`, `doctors`, `clinic_staff` exactly as TDD §3.3, including the `verified_has_verifier` CHECK constraint. **No schema change is required to support self-registered doctors:** the constraint is `is_verified = FALSE OR (verified_by IS NOT NULL AND verified_at IS NOT NULL)`, which an unverified self-signup satisfies with `is_verified=false, verified_by=NULL`. `doctors.primary_clinic_id` stays `NOT NULL` and is supplied by the registration form (`BL-21`).

**Clinics must be seedable before a doctor can register.** The clinic selector reads from `clinics`, so `ops/scripts/seed_synthetic.py` must populate at least three across Lahore, Karachi, and Islamabad. A `GET /api/v1/clinics` list endpoint (id, name, city only — no sensitive data, no auth required) is added to serve the selector.

**Audit** — `audit_log` per TDD §3.8, plus `REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM curanode_app`. Auth writes `auth.register`, `auth.login`, `auth.logout`, `auth.lockout`, `auth.refresh_reuse_detected`.

**Redis keyspace** (this feature owns these; document in `docs/ADR/`):

| Key | Value | TTL |
|---|---|---|
| `refresh:{token_hash}` | `{user_id, family_id, issued_at, consumed}` | 14 d |
| `refresh_family:{family_id}` | set of token hashes | 14 d |
| `ratelimit:auth:{ip}` | counter | 60 s |
| `lockout:{user_id}` | mirror of `locked_until` | 900 s |

**Seed data** — `ops/scripts/seed_synthetic.py` must produce at minimum: one active patient, one **verified** doctor, one **unverified** doctor, one clinic admin, one clinic, and one `pending_verification` patient. Every row carries the synthetic marker required by TDD §7.6's startup check. **`NFR19`: no real data, ever.**

**Retention** — `users` and `patients` are never hard-deleted. `audit_log` is append-only and immutable by database grant (`NFR17`).

---

## 8. Business Logic

Numbered rules. **BL-01 through BL-06 are security-critical**; a change to any of them requires advisor sign-off.

1. **BL-01** — Self-registration may create a `patient` **or** a `doctor` account. A self-registered doctor is **always** `is_verified=false`. `is_verified`, `verified_by`, and `verified_at` are server-controlled, absent from every request model, and discarded if submitted.
2. **BL-02** — Creating a doctor *account* is not the same as granting doctor *access*. Until a clinic admin verifies them, an unverified doctor is denied every clinical route (`FR3`). **This is the invariant the whole toggle rests on — no implementation may weaken it.** `clinic_admin` accounts still cannot be self-registered by any path in this feature.
3. **BL-03** — A doctor's `is_verified` is read from the database on **every** request, never trusted from a token claim. Revocation takes effect on the next call (TDD §7.2).
4. **BL-04** — All authentication failures return one indistinguishable response. Wrong password, unknown number, suspended, and locked are externally identical, including timing (§4.4.1).
5. **BL-05** — Registration never reveals whether an email is already registered. A duplicate returns the success shape, creates nothing, and issues no cookies (`AC-24`).
6. **BL-06** — Refresh tokens are single-use. Reuse is treated as compromise and revokes the entire family (`AC-10`).
7. **BL-07** — `403 FORBIDDEN` signals **role mismatch only**. Consent denial is `404` and is never surfaced through this feature's handlers (`BR-02`, TDD §8.3).
8. **BL-08** — `passport_no` is generated once at registration and is immutable thereafter (`FR1`). It is human-facing and is never used as a foreign key.
9. **BL-09** — A new account is `active` immediately and the user is signed in by the registration response. No self-registration path writes `pending_verification`; the enum value is retained but unused.
10. **BL-10** — Lockout is 10 consecutive failures → 15 minutes. Attempts during a lock do not extend it, preventing an attacker from locking a user out indefinitely.
11. **BL-11** — *Void (OTP removed, C2).* Replaced by: **email is normalised** — trimmed and lower-cased before lookup or insert, stored as `CITEXT`, so casing can never create a second account for the same address.
12. **BL-12** — Tokens live in cookies only. Writing a token to `localStorage`, `sessionStorage`, a URL, or a response body is prohibited — an XSS bug must not become a credential leak (TDD §7.1).
13. **BL-13** — Every user-facing string resolves through an i18n `message_key`. No raw English is returned by the API (TDD §8.1).
14. **BL-14** — Passwords, tokens, `authorization` headers, `email`, `phone_e164`, and `full_name` are dropped by the structlog redaction processor before emission, at every level including `DEBUG` (TDD v1.1 §7.8).
15. **BL-15** — Every authentication outcome writes exactly one `audit_log` row. Audit writes are never conditional and never best-effort.
16. **BL-16** — The application refuses to start if the JWT secret is absent or shorter than 32 bytes. Failing loudly at boot beats running insecurely.
17. **BL-17** — Per TDD §7.6, the application refuses to start when `ENVIRONMENT != "pilot"` and any `users` row lacks the synthetic marker.
18. **BL-18** — Clinic admins have **no** clinical-record access, now or ever, via any role gate this feature defines (`BR-01`, `NFR16`).
19. **BL-19** — *(Revised — supersedes the original "silent routing" rule.)* The Login role toggle never influences **authorisation**: the granted role always comes from the account, never from the request, and `LoginRequest` still has no `role` field. It does gate **completion of sign-in** at the web layer — a mismatch is refused and the session discarded. The refusal is safe because it sits behind password verification: an attacker without the password only ever sees the generic `401`, and an attacker with the password already owns the account. The original rule silently routed mismatches to the account's own area, which was indistinguishable from the toggle being broken.
20. **BL-20** — `pmdc_number` is recorded, never validated against an external registry (TDD §3.3, PRD Appendix A assumption 4). The UI must say so plainly rather than implying a check has occurred.
21. **BL-21** — A doctor's `primary_clinic_id` is mandatory at registration because it determines which administrator is responsible for verifying them. A doctor with no clinic would be unverifiable and therefore permanently inert.

---

## 9. Dependencies

### 9.1 Packages to add

Current `pyproject.toml` has only `fastapi`, `uvicorn`, `itsdangerous`, `python-multipart` (`app-foundation.md` §2). Add per TDD §2.1:

```
sqlalchemy>=2.0.40,<2.1     alembic>=1.16,<2.0      psycopg[binary]>=3.2,<4.0
pydantic>=2.11,<3.0         pydantic-settings       argon2-cffi>=25.1
pyjwt>=2.10,<3.0            redis>=5.0              structlog>=25.1
uuid7                       pytest>=8.4             pytest-asyncio>=1.0
ruff>=0.12                  import-linter
```

> `itsdangerous` is currently present and implies signed-cookie sessions. **This spec follows TDD §7.1 (JWT + rotated refresh).** If cookie sessions are the intended direction instead, that is a stack decision requiring sign-off — see §10.4.

Frontend (nothing exists today): Next.js 16.3.x, React 19.2, TypeScript ≥5.9 `strict`, Tailwind CSS 4.x, next-intl ≥4.3, TanStack Query ≥5.90, Zod ≥4.0, Playwright ≥1.55, Vitest ≥3.2.

### 9.2 Infrastructure

PostgreSQL 18.4 and Redis 8.x, both via `compose.yaml` (`NFR24`). Redis is **required**, not optional — refresh-token rotation and rate limiting depend on it.

### 9.3 External services

**None.** With OTP removed, authentication makes no external call at all — no SMS gateway, no email provider. `SMSProvider` is deferred entirely to appointment notifications (`FR20`, TDD v1.1 §9.5). No test makes a network call (TDD §9.2).

### 9.4 Blocked by

Nothing. This is the first feature and has no upstream dependency.

### 9.5 Blocks

Every feature that reads or writes patient data. The consent gateway, records, documents, scheduling, and all clinical endpoints depend on `Actor` and the four role dependencies defined here.

---

## 10. Out of Scope

### 10.1 Deferred to a later feature

- **Password reset / forgot password.** The designed screen exists, but TDD still defines no reset endpoint. **Now the highest-priority follow-up:** with email as the identity, the designed reset-link flow is finally coherent — and with no OTP and no reset, a forgotten password currently means a permanently inaccessible account. Requires a decision (§10.4 item 2).
- **The doctor verification workflow** — the admin-facing queue, the verify/reject action, and the notification to the doctor (`FR3`). This feature *creates* unverified doctor rows and enforces their powerlessness; the admin feature *consumes* them. Until it ships, verification is performed by direct database update in development.
- **Admin-initiated doctor creation** and all `clinic_admin` account creation.
- **Clinic-admin console.** Admins authenticate and land on a placeholder only (conflict C3).
- **Caregiver delegated access** (`FR7`, priority C).
- **Profile editing** (`FR2`) — `PATCH /me` is not built here; `GET /me` is.
- **Email verification of any kind.** Explicitly excluded per C2 — no confirmation link, no code. Revisit before real patient data (§10.4 item 3).
- Social login, MFA, "remember this device", session listing and remote revocation, CAPTCHA.

### 10.2 Never in this version

Per PRD §6.2 and TDD §7.2: no administrative override of patient consent (`NFR16`), no biometric authentication, no native mobile app, no offline authentication (`NFR9`).

### 10.3 Adjacent but excluded

The consent gateway, clinical records, appointments, documents, and every AI capability. This feature must not import from, or add code to, those modules.

### 10.4 Open decisions — resolve before or during implementation

| # | Decision | Blocks | Owner |
|---|---|---|---|
| **1** | **Stack confirmation.** `itsdangerous` cookie sessions vs TDD's JWT + Redis rotation. TDD Appendix B item 1 already flags this. | Implementation approach | Advisor |
| **2** | **Password reset.** Now coherent via email link, as designed. **Recommended as the immediate next feature** — without OTP or reset, a forgotten password is unrecoverable. | §10.1 item 1 | Advisor |
| **3** | **No email verification.** An address is never proven; a typo at registration is unrecoverable. Accepted for a synthetic-data pilot; must be revisited before real patient data (`NFR19`). | Pilot readiness | Advisor |
| **4** | **Urdu font selection.** Noto Nastaliq Urdu proposed; no design decision exists. | `AC-18` | Design |
| **5** | **Responsive breakpoint.** DESIGN.md has zero media queries and a manual device toggle; the real sidebar → bottom-tab threshold is undecided (`app-foundation.md` §12 item 10). | Layout shell | Design |
| **6** | **CNIC.** Designed on Register, absent from TDD's `patients` table. Dropped here — confirm it is not required. | Registration fields | Advisor |
| **7** | **Doctor self-registration amends `FR3` and TDD §4.4**, both of which assign doctor creation to clinic admins. Decided by the project owner and implemented per conflict C1; **`PRD.md` and `TDD.md` should be amended to match** so the documents stop contradicting the build. | Doc consistency | Advisor |
| **8** | **Abuse surface, now larger.** Anyone may create an unverified doctor account against any clinic, and with OTP gone there is **no proof-of-identity step at all** — only the 5/min rate limit remains. If junk verification queues become a problem, options are a clinic invite code or admin-only creation. | Admin feature | Advisor |
| **9** | **Email-without-OTP amends TDD §3.3/§7.1.** Applied in TDD v1.1; PRD.md needed no change (`FR1` never mandated a channel). Requires advisor sign-off. | Doc consistency | Advisor |

---

## 11. Testing Requirements

Test IDs map **1:1** to the acceptance criteria in §3. **T2, T3, and T4 are void** — their acceptance criteria were removed with OTP. **Net: 20 live tests.** No fakes are needed for external services because authentication makes no external call; **none makes a network call** (TDD §9.2).

| ID | Covers | Type | Test |
|---|---|---|---|
| **T1** | AC-01 | Integration | Register as **Patient** → `201`; assert `users.role='patient'`, `status='active'`, `password_hash` starts `$argon2id$`, a `patients` row exists with a well-formed unique `passport_no`, and **both session cookies are set**. |
| ~~T2~~ | — | — | *Void — AC-02 removed with OTP.* |
| ~~T3~~ | — | — | *Void — AC-03 removed with OTP.* |
| ~~T4~~ | — | — | *Void — AC-04 removed with OTP.* |
| **T5** | AC-05 | Integration | Login on an active account → `200`; assert cookie attributes, `failed_logins=0`, `last_login_at` stamped, correct `landing_route` per role, `auth.login` audit row written. |
| **T6** | AC-06 | Integration + timing | Assert byte-identical `401` bodies for wrong password, unknown email, and suspended account. Assert median response times across 50 runs fall within 20% of each other (dummy-verify guard). |
| **T7** | AC-07 | Integration | 10 failed logins → `locked_until` set ~15 min out. Correct password during lock → `401`, and assert `locked_until` is **unchanged**. Advance clock → login succeeds, counter cleared. |
| **T8** | AC-08 | Integration | Unverified doctor logs in successfully; a `VerifiedDoctorDep` route → `403`. Flip `is_verified=true` in the database **without re-login**; the same route now → `200`. |
| **T9** | AC-09 | Integration | Refresh with a valid token → new access **and** new refresh issued; the presented token is marked consumed and a second use fails. |
| **T10** | AC-10 | Integration | Refresh once, then replay the consumed token → `401`, both cookies cleared, every key in the family deleted, `auth.refresh_reuse_detected` audited. |
| **T11** | AC-11 | Integration | Logout → `204`, cookies cleared with matching attributes, family revoked. Second logout → `204` (idempotent). |
| **T12** | AC-12 | Integration | `GET /me` per role asserts the correct projection: `passport_no` present for patients and `null` otherwise; `clinic_ids` empty for patients; `phone_masked` null when no phone was supplied. Without a cookie → `401`. |
| **T13** | AC-13 | Integration | Matrix across all three roles × all four dependencies. Assert `403` on every mismatch and that **no** handler in this feature can emit `404` for an authorisation failure. |
| **T14** | AC-14 | Integration | 5 requests in 60 s to each `/auth/*` endpoint succeed; the 6th → `429` with `Retry-After`. |
| **T15** | AC-15 | Unit + integration | Assert Argon2id parameters exactly. Capture all log output across register → login → refresh → logout and assert no password, token, or raw email address appears at any level including `DEBUG`. |
| **T16** | AC-16 | E2E (Playwright) | Each role signs in and lands on its own route. A patient deep-linking to `/doctor` is redirected to `/patient`. |
| **T17** | AC-17 | E2E | Unauthenticated request for a protected page → redirected to login with `next` preserved → after sign-in, lands on the originally requested page. |
| **T18** | AC-18 | E2E | Both auth screens render in `en` and `ur`. Assert `<html dir="rtl">` under `ur`, that the Urdu font loads, that the email and optional `+92` phone inputs remain LTR, and that validation messages are localised. **Plus a CI lint rule failing on any physical-direction utility.** |
| **T19** | AC-19 | E2E + component | Assert a visible focus ring on every interactive element; an invalid field carries `aria-invalid` and an `aria-describedby` message; the submit button disables and shows a spinner in flight; a double-click submits exactly once. |
| **T20** | AC-20 | Integration + E2E | Assert `HttpOnly`, `Secure`, `SameSite=Strict`, `Path=/` on both cookies. Assert no token string appears in any response body, any URL, `localStorage`, or `sessionStorage`. |
| **T21** | AC-21 | Integration | Register as **Doctor** → `201`; assert `users.role='doctor'`, a `doctors` row with `is_verified=false`, `verified_by IS NULL`, `verified_at IS NULL`, and the submitted PMDC/specialty/clinic; assert **no** `patients` row and no `passport_no`. Then: (a) POST with `is_verified=true` injected into the body → the field is ignored and the row is still `false`; (b) POST omitting `pmdc_number` → `422`, no row created; (c) POST with an unknown `primary_clinic_id` → `422`. |
| **T22** | AC-22 | Integration + E2E | Self-register a doctor. Assert they are signed in immediately with `status='active'`, `is_verified_doctor=false`, and that **every** `VerifiedDoctorDep` route returns `403`. Assert the shell renders the non-dismissible awaiting-verification banner and exposes no patient-data affordance. Then set `is_verified=true` in the database **without re-login** and assert the same routes return `200` on the next request. |
| **T23** | AC-23 | E2E + contract | Sign in with *Doctor* selected on a patient account → lands on the patient dashboard, no error shown. Assert the login request payload contains **no** `role` key, and assert `LoginRequest` rejects an injected `role` field. |
| **T24** | AC-24 | Integration | Register an email, then register the same email again (and again with different casing/whitespace, e.g. ` A@B.com `) → each returns `201` with a body byte-identical to the first success, **no** second row is created, and **no** cookies are set on the duplicate responses. |

### Additional gating checks

| Check | Assertion |
|---|---|
| **G1 — Migration reversibility** | `alembic upgrade head` then `downgrade base` runs clean on an empty database. |
| **G2 — Audit immutability** | `UPDATE` and `DELETE` on `audit_log` as the application role are rejected by the database (`NFR17`). |
| **G3 — Module boundaries** | `import-linter` passes: `identity` imports only `db`, `audit`, `i18n` (TDD §5.2). |
| **G4 — Startup guards** | App refuses to start with a missing or <32-byte JWT secret (`BL-16`), and with non-synthetic data outside pilot (`BL-17`). |
| **G5 — Performance** | Login p95 ≤400 ms API-side; auth pages LCP ≤2.5 s on simulated 3G (`NFR1`, TDD §10.1). Argon2 parameters must be verified against this budget — if they breach it, raise it rather than silently weakening the hash. |
| **G6 — Coverage** | ≥90% line coverage on `identity/` and `deps.py`. Every branch of §4.4 login **and both branches of §4.1 registration** are exercised. |
| **G7 — Privilege-escalation sweep** | A dedicated test module asserts there is **no** code path by which a client-supplied value reaches `doctors.is_verified`, `verified_by`, or `verified_at`. Grep-level assertion plus a fuzz test injecting those keys into every auth request body. This is the single most important test in the feature — it is what keeps the toggle safe. |
