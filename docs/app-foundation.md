# Software Specification Document (Implementation Addendum)

**Status:** Pre-implementation baseline · **Repo:** `curanodeai` · **Date recorded:** 2026-08-17

> **Read this first.** The repository currently contains **no application code**. Tracked files are `.gitignore`, `README.md` (empty), `pyproject.toml`, and `uv.lock`; `docs/` is present but untracked. There is no backend module, no frontend, no database, no migration, and no test.
>
> This document therefore records a **greenfield baseline**. Almost everything below is classified `Designed` or `Planned`, and that is the correct and honest state. Its job is to be the fixed reference point the first feature is written against — not to describe a system that exists.

**Classification used throughout:**
- **Implemented** — exists in the current code of this repository.
- **Designed** — specified in `docs/DESIGN.md` / the design artifact, not built.
- **Planned** — required by `docs/PRD.md` / `docs/TDD.md`, not built.

---

## 1. Scope

This addendum defines the **current implementation baseline** of CuraNode: what runs today, what conventions are already fixed, and what a future feature must not silently break.

It deliberately does **not** duplicate:
- **`PRD.md`** — product requirements, goals, success metrics, timeline, out-of-scope list. Requirement IDs `FR1`–`FR37`, `NFR1`–`NFR27`, `D1`–`D2` live there and are referenced, never restated.
- **`TDD.md`** — architecture, tech-stack pins, database schema, endpoint catalogue, module boundaries, security design.
- **`DESIGN.md`** — the full design system. Section 5 summarises only the baseline a feature must preserve; the token tables stay in `DESIGN.md`.

IDs introduced here (`FR-01`, `BR-01`, `AC-01`, `TC-01`) are **baseline IDs** and are distinct from the PRD's `FR1`–`FR37`. Where a baseline item traces to a PRD or TDD requirement, that ID is cited.

## 2. Runtime Behavior

**Implemented**
- **Nothing runs.** There is no entrypoint, no ASGI app, no `main.py`. `uv run` has no target.
- **Dependency baseline only** (`pyproject.toml`, locked in `uv.lock`): `fastapi>=0.141.1`, `uvicorn>=0.52.1`, `itsdangerous>=2.2.0`, `python-multipart>=0.0.32`. Python `>=3.13`.
- **Git:** single commit (`Initial setup for project`) on `main`.

**What the dependency set implies** (recorded because it is the only implementation signal, and it conflicts with TDD — see §12):
- `itsdangerous` is Starlette's signed-cookie `SessionMiddleware` dependency → a **server-side cookie session** posture.
- `python-multipart` → **HTML form posts and file uploads** handled by FastAPI directly.
- Absent: SQLAlchemy, Alembic, psycopg, Celery, Redis, Argon2, PyJWT, structlog — so **no persistence, no background worker, no password hashing, no structured logging** exist.
- Absent: any Node/Next.js/pnpm manifest — so **no frontend application** exists.

**Planned** (TDD §1.2, §7.1, §8): FastAPI `api` + Next.js `web` + Celery `worker`/`beat` + Postgres + Redis + MinIO + Nginx, all under Docker Compose; JWT access/refresh in `HttpOnly` cookies; the localised error envelope of TDD §8.2.

**Designed** (`DESIGN.md`): loading state exists only as the AI processing pattern (pulsing dot + indeterminate bar). **No empty state, no skeleton, no offline banner, and no field-level error state is designed.** No modal, toast, or tooltip exists in the artifact.

## 3. User Flows

**Implemented:** none. No flow can be executed today.

**Designed** — the prototype is a **screen switcher, not an application**: a single `screen` state, `go(screen)` swaps it and scrolls to top. There is no router, no URL, no history, no session, and no data fetch. All 30 screens are reachable from the sidebar regardless of role. Recorded so it is never mistaken for working navigation.

The design artifact establishes these flow *shapes*, which the first real features should follow:

1. **Sign in** — pick role (Patient/Doctor) → email + password → `Sign in` → lands on that role's dashboard.
2. **Register** — name, CNIC/Patient ID, email, password ×2 → pick role → accept consent policy → `Create account`.
3. **Forgot password** — registered email → `Send reset link` → inline success block appears in place.
4. **Upload → verify (OCR)** — pill stepper → source image + `Run extraction` → processing panel → extracted rows for line-by-line review → confirm. Copy states: *"Nothing enters your record until you verify it."*
5. **Ask Medico** — question → thinking panel → answer carrying `src:` citation chips.
6. **Grant / revoke access** — per-grant card with permission toggles, expiry line, `Extend` / revoke actions.

**Planned** (PRD/TDD): email + password registration with no verification step (TDD v1.1 §7.1), consent-gated record reads returning `404` on denial (D2), booking + live queue polling, doctor "what changed", clinic-admin scheduling and check-in.

## 4. Functional Requirements

Baseline behaviours a future feature must preserve. Items marked *(to establish)* are not yet true — the first feature that touches the area **creates** the behaviour and thereafter must not regress it.

| ID | Requirement | Status |
|---|---|---|
| **FR-01** | The project builds and its dependencies resolve from `uv.lock` without network drift; the lock file is authoritative. | Implemented |
| **FR-02** | Python `>=3.13` is the runtime floor; no feature may introduce a `<3.13` construct or a dependency that blocks it. | Implemented |
| **FR-03** | A single ASGI application exposes the API; one deployable backend, not per-feature services (TDD §1.1). | *(to establish)* |
| **FR-04** | Authentication state is carried in an `HttpOnly`, `Secure`, `SameSite` cookie — never `localStorage` (TDD §7.1). | *(to establish)* |
| **FR-05** | Every clinical record read passes through the single consent gateway, which writes its audit row in the same transaction (TDD Rule 1). | *(to establish)* |
| **FR-06** | Consent denial returns `404`, byte-identical to a nonexistent patient — never `403` (D2, TDD §8.3). | *(to establish)* |
| **FR-07** | Clinical records are insert-only; corrections are new rows referencing the superseded row (D1, TDD Rule 2). | *(to establish)* |
| **FR-08** | No AI-extracted clinical value is persisted without a named human confirmer (FR12, NFR20). | *(to establish)* |
| **FR-09** | Every user-facing string resolves through a message key, so it exists in both `en` and `ur` (FR28, TDD §8.1). | *(to establish)* |
| **FR-10** | Every AI-generated output carries the server-appended disclaimer; it is concatenated in code, never requested from the model (FR31). | *(to establish)* |
| **FR-11** | An AI capability failure degrades one feature and returns a value, never an exception that breaks the page (FR36, TDD §8.4). | *(to establish)* |
| **FR-12** | Design tokens are consumed as CSS custom properties; no raw hex is written into a component (`DESIGN.md` §8 rule 1). | *(to establish)* |

## 5. Visual & UI Baseline

Source of truth is `docs/DESIGN.md`. Only the contract a feature must honour is repeated here.

**Status: entirely Designed. No component, stylesheet, or template is implemented.**

- **Layout** — fixed `262px` sticky dark sidebar + fluid main column; sticky header (`--surface`, hairline bottom border); content centred at `1240px` desktop / `412px` mobile; prose capped ≈`64ch`.
- **Navigation** — dark nav column (its own `--nav-*` scale, near-identical in both themes) grouped by role; active row = filled background + white text + weight 600 + brand dot, all three at once. Mobile replaces it with a 5-item sticky bottom tab bar.
- **Typography** — three families with fixed jobs: **Newsreader** (headings, numeric values), **Public Sans** (all UI text), **IBM Plex Mono** (IDs, dates, sizes, citations). Body sits at 12.5–13.5px; weights 400/500/600.
- **Color & theme** — all colour via CSS custom properties on `:root`, overridden wholesale on `body[data-theme="dark"]`. `--brand` (teal) and `--ai` (violet) are runtime-configurable; **hardcoding either breaks theming**. Status colour is semantic: `--ok` confirmed/in-range, `--warn` pending/borderline, `--err` abnormal/destructive.
- **The AI marking rule** — anything machine-generated is visually marked with `--ai` and, where it asserts a fact, carries mono `src:` citation chips. This is the product's core trust signal, not decoration.
- **Reusable patterns** — card (`--surface`, `1px --line`, `16px` radius, `20–22px` padding, `var(--shadow)`); list row (`11px` radius, `--surface-2`, left colour bar); pill/segmented control (`999px`); badge (`-soft` fill + matching solid text); timeline (colour-coded dots on a `--line-2` rail); toggle (`36×20px`); AI panel; chat bubble; dropzone.
- **Buttons** — `10px` radius, weight 600, five variants: primary, secondary, AI, destructive, disabled.
- **Inputs** — `11px 13px`, `10px` radius, `1px --line-2`, `--surface-2` fill, `600 12px` label above. `accent-color: var(--brand)` on checkboxes.
- **Responsive** — intrinsic only: `repeat(auto-fit, minmax(Xpx, 1fr))` + `flex-wrap` + `min-width:0`. **There are zero `@media` queries in the artifact**, and desktop/mobile is a manual toggle, so the real breakpoint is undecided (see §12).

## 6. Form Specifications

**Implemented:** none. **Designed:** four forms, all static — no validation, no submit handler, no request.

| Form | Fields | Designed behaviour | Missing |
|---|---|---|---|
| Login | role toggle, email, password, "keep me signed in" | `Sign in` routes to the picked role's dashboard | Validation, error state, loading state |
| Register | full name, CNIC/Patient ID, email, password, confirm password, role, consent checkbox | `Create account` routes to dashboard | Password match check, field errors, submit state |
| Forgot password | registered email | Reveals an inline `--ok-soft` success block; copy names a 15-min expiry and a 0:45 resend timer | Real send, timer, failure state |
| Grant access | doctor name / PMDC number | Static input + button in a dashed panel | Search, selection, confirmation |

**Baseline form contract for the first real form** (from TDD §8.7, NFR8): fields validated server-side with Pydantic; errors returned as the §8.2 envelope with a `message_key`; form state preserved on failure and cleared only on confirmed success; every mutation carries a client-generated `Idempotency-Key`; mutations are never auto-retried.

**Not designed at all:** error borders, helper/error text, disabled-while-submitting state, success toast. The first form to be built must define these, and every later form must match it.

## 7. State & Data Behavior

**Implemented:** none — no session, no store, no persistence layer, no database file.

**Designed (prototype only, not a model to copy):** one flat component state object — `screen`, `theme`, `device`, `role`, plus per-screen scratch flags (`resetSent`, `ocr`, `ai`, `rxSent`, `book`, `slot`). Theme is applied by writing `data-theme` to `<body>` on mount and update. All content is hardcoded literals. Nothing persists across reload.

**Planned baseline** (TDD §7.1, §10.3): `Actor` resolved once per request from the auth dependency and passed explicitly — never read from a global; short-lived access token with a rotated refresh token; server state on the client via TanStack Query with polling for queue position; Redis caching where consent lookups are invalidated **immediately** on revoke, never TTL-expired.

Database schema is **not** restated here — it is TDD §3.

## 8. Business Rules

Rules already binding on this project. `BR-01`–`BR-04` are settled decisions inherited from PRD/TDD and must never be "improved" by a feature; `BR-05`–`BR-08` are design-level rules the artifact establishes.

| ID | Rule | Source |
|---|---|---|
| **BR-01** | Patient consent is the only basis for record access. There is no administrative override, and clinic admins never see clinical records. | NFR16, TDD §7.2 |
| **BR-02** | A doctor without a grant sees nothing — not the record, not the fact one exists. Denial is indistinguishable from absence. | D2 |
| **BR-03** | Records are append-only and contradictions are never reconciled; both entries survive, each attributed to its source. | D1 |
| **BR-04** | The model never decides a clinical fact. Facts are computed deterministically in code; the model only phrases them. | TDD Rule 3 |
| **BR-05** | Nothing extracted by AI enters the record without explicit human confirmation of every line. | FR12, NFR20 |
| **BR-06** | AI-generated content is always visually distinguishable from verified clinical fact (the `--ai` marking rule). | DESIGN §8 rule 2 |
| **BR-07** | Status colour carries meaning and is never used decoratively. | DESIGN §8 rule 3 |
| **BR-08** | Exactly four AI capabilities exist. A fifth requires a migration, a PRD amendment, and advisor sign-off. | FR35, TDD §3.2 |

## 9. Rebuild Requirements

To reproduce the current baseline on a fresh machine:

1. Python `>=3.13` and `uv >=0.8`.
2. Clone the repository; `uv sync` resolves exactly the four runtime dependencies from `uv.lock`.
3. Confirm `docs/PRD.md`, `docs/TDD.md`, `docs/DESIGN.md` and this file are present.
4. **Nothing starts.** There is no server to run, no page to open, no migration to apply, and no test to execute. A successful rebuild of this baseline is a resolved environment and the document set — nothing more.

The design artifact (`CuraNode-AI.dc.html` + `support.js`, outside this repo — see `DESIGN.md` §9) opens standalone in a browser and is the visual reference. It is **not** part of the application and must not be imported into it.

## 10. Acceptance Criteria

| ID | Criterion | Status |
|---|---|---|
| **AC-01** | `uv sync` succeeds on Python 3.13 and installs only the four locked runtime dependencies. | Verifiable now |
| **AC-02** | The repository contains no application source, so no runtime behaviour can be claimed as implemented. | True now |
| **AC-03** | `docs/PRD.md`, `docs/TDD.md`, `docs/DESIGN.md` and this addendum are present and mutually consistent except where §12 records a conflict. | True now |
| **AC-04** | The first feature introduces an ASGI entrypoint, a health endpoint, and a test that runs in CI. | Pending |
| **AC-05** | The first UI work consumes `DESIGN.md` tokens as CSS custom properties, with light and dark both rendering correctly. | Pending |
| **AC-06** | The first authenticated route sets an `HttpOnly` cookie and resolves an `Actor` through a dependency, not a global. | Pending |
| **AC-07** | No feature merges while any §12 blocking gap it depends on remains unresolved. | Pending |

## 11. Test Cases

Baseline suite only. The substantive tests belong to the features that introduce the behaviour.

| ID | Test | Expected | Status |
|---|---|---|---|
| **TC-01** | Resolve dependencies from the lock file on a clean environment | Succeeds; no unlocked dependency pulled | Runnable |
| **TC-02** | Assert the interpreter satisfies `>=3.13` | Passes | Runnable |
| **TC-03** | Start the ASGI app and request the health endpoint | `200` with a stable body | Pending (needs FR-03) |
| **TC-04** | Request a record as a doctor with no grant | `404`, body byte-identical to a nonexistent patient | Pending (BR-02) |
| **TC-05** | Attempt `UPDATE` / `DELETE` on the clinical records table as the app role | Rejected by the database | Pending (BR-03) |
| **TC-06** | Insert a document-sourced record without a confirmer | Rejected by constraint | Pending (BR-05) |
| **TC-07** | Render the base layout in light and dark | Tokens resolve; no hardcoded hex present | Pending (FR-12) |

## 12. Documentation Gaps

Recorded, not resolved. Items marked **BLOCKING** should be decided before the first feature that touches them.

**Design ↔ PRD conflicts**
1. **BLOCKING — Partial consent scoping.** `DESIGN.md` §5 shows a *Record access control* screen with per-category toggles (labs vs prescriptions, per grantee). TDD §3.4 states grants are explicitly **all-or-nothing** and that partial scoping is not implemented. The design promises a capability the data model refuses.
2. **BLOCKING — Self-selected doctor role at signup.** The designed Login and Register screens let a user pick "Doctor" directly. FR3 and TDD's `RegisterRequest` force `role="patient"`; doctors are created and verified by clinic admins.
3. ~~**Registration identity mismatch.**~~ **Resolved** by TDD v1.1: email is now the primary credential and the OTP step is removed, matching the designed flow. CNIC remains dropped — confirm it is not required. *Residual risk:* no channel verifies an email address, so a typo at registration is unrecoverable until password reset exists.
4. **Telehealth appears in the design.** Doctor screens show "CuraNode Telehealth", "Video consults", and video slots in the schedule grid. PRD §6.2 puts video consultation and telemedicine explicitly out of scope.
5. **Clinical analytics overreach.** The designed analytics screen shows cohort recall lists ("LDL above target on statin >12 weeks — 23 patients", "Statin-eligible, not prescribed"). FR25 limits doctor analytics to own-practice volume, common diagnoses, and prescribed medicines; PRD §6.1/§6.2 cut deep analytics and clinical decision support.

**Design coverage gaps**
6. **BLOCKING — No Urdu, no RTL anywhere in the design.** FR28 and NFR13 require the full patient interface in Urdu with correct right-to-left layout, and TDD §2.3 makes RTL a *Sprint 1 architectural constraint*. The artifact is English-only and LTR throughout, has no language switcher, and its three fonts (Newsreader, Public Sans, IBM Plex Mono) have no Urdu coverage. A Urdu font stack and the RTL behaviour of every pattern are undesigned.
7. **No clinic-administrator screens.** The design covers Patient and Doctor only. FR6 requires three roles, and TDD ships an `admin` module and `(admin)` routes for scheduling, queue, doctor verification, and translation.
8. **No AI disclaimer component.** FR31 requires an unmissable disclaimer on every AI output. No such component appears in the design, though AI panels and citation chips do.
9. **No queue-position UI.** FR17's live queue position and wait estimate — including the deliberately nullable estimate with a confidence level (TDD §4.4) — has no designed screen.
10. **Missing interaction states** (already listed in `DESIGN.md` §10, repeated because they block the first form and the first route): no focus ring (`outline:none` with no replacement — an accessibility defect against NFR14), no field error state, no empty state, no skeleton, no modal/toast/tooltip, no transitions, and no defined breakpoint.

**Stack conflict**
11. **BLOCKING — The dependency set does not match the TDD.** `pyproject.toml` pulls `itsdangerous` (signed-cookie sessions) and `python-multipart` (server-rendered form posts), and pulls **no** SQLAlchemy, Alembic, psycopg, Celery, Argon2, or PyJWT. TDD §2.1 pins all of those plus a separate **Next.js 16 + Tailwind 4 + next-intl** frontend, and TDD §7.1 specifies JWT with rotated refresh tokens rather than cookie sessions. Either the repo is mid-setup or a simpler server-rendered stack has been chosen without amending the TDD. TDD Appendix B item 1 already flags stack confirmation as needing sign-off. Note also that `DESIGN.md`'s tokens are plain CSS custom properties, not Tailwind theme values — the mapping is unspecified.

**Process**
12. **BLOCKING for FR29/FR33 — `docs/chatbot-scope.md` does not exist.** PRD Appendix A open question 3 and TDD §6.6 both state the chatbot's refusal boundary must be written and signed off before those features are implemented.
13. **PRD open questions 1 and 2 remain unanswered** — PUCIT milestone dates and the partner clinic. The nine-sprint plan is unpinned.
14. **`docs/` is untracked.** PRD, TDD, and DESIGN are not committed, so the baseline they define is not yet version-controlled (NFR26).
