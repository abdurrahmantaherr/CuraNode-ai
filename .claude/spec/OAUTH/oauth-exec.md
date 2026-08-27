# OAuth Sign-In — Execution Prompt

You are implementing **Google OAuth sign-in** for the CuraNode-AI FYP. Create
all backend modules, frontend templates, CSS, i18n keys, and tests. Follow the
plan exactly, phase by phase, in the order given below.

---

## Before making any changes

Read these files thoroughly:

- `docs/oauth.md` — **the plan. This document executes it; that one defines it.**
- `CLAUDE.md` — architecture rules, especially the Supabase client lifecycles
  and the additive-migration rule
- `docs/PRD.md`, `docs/TDD.md`, `docs/DESIGN.md`, `docs/app-foundation.md`

Then read, in this order, the code you are about to touch:

1. `backend/app/identity/security.py` — read the module docstring in full
   before writing a single Supabase call
2. `backend/app/identity/service.py` — `register()`, `login()`, `_sign_out()`
3. `backend/app/identity/router.py` — `set_session_cookies` / `clear_session_cookies`
4. `backend/app/web/router.py` — `login_submit`, `register_submit`, `_guarded`
5. `backend/app/deps.py` — `_load_actor`, the `Actor` dataclass
6. `backend/app/db/models.py` — `Profile`, `Patient`, `Doctor`, `DoctorAffiliation`
7. `backend/app/settings.py`, `backend/app/cache.py`, `backend/app/errors.py`
8. `tests/conftest.py` and `tests/fakes.py`

---

## Implementation rules

- Follow `docs/oauth.md` in its defined order. Where this document and the plan
  disagree, **the plan wins** — stop and ask rather than improvising.
- Follow the architecture and technical decisions in `TDD.md`, the UI/UX and
  design system in `DESIGN.md`, and the baseline conventions in
  `app-foundation.md`.
- Reuse existing helpers. Specifically: `set_session_cookies`,
  `clear_session_cookies`, `enforce_auth_rate_limit`, `_render`,
  `_base_context`, `_clinic_options`, `_area`, `_sign_out`,
  `_unique_passport_no`, `audit.write`, `cache`, `translate`. Do not write a
  second version of any of these.
- Match the house comment style: cite requirements inline as
  `SPEC AC-xx` / `SPEC BL-xx` / `TDD x.y` / `FRxx` where a line encodes a rule.
  Comments explain *why*, never *what*.
- Enforce every gate on the backend, not only in the template.
- Keep the implementation visually and structurally consistent with the
  existing auth pages.

### Do not

- **Do not** create an Alembic migration. This feature adds no columns and no
  tables. If you believe you need one, stop and ask.
- **Do not** call `sign_in_with_oauth` / `exchange_code_for_session` on the
  `get_supabase_client()` singleton. Those calls mutate client session state;
  use `new_auth_client()`. Getting this wrong shows up later as
  `AuthApiError: User not allowed` on an unrelated admin call.
- **Do not** read role, verification status, or anything else from a JWT.
- **Do not** insert a second `user_profile` row. The `on_auth_user_created`
  trigger has already created one — update it.
- **Do not** create a `Patient` or `Doctor` row in the callback. Role
  provisioning happens only in the onboarding POST.
- **Do not** weaken the session cookies to `SameSite=None`.
- **Do not** surface a provider's raw error text, the auth code, the state, or
  the code verifier to the user or to the logs.
- **Do not** implement anything from the plan's §15 Out of Scope.

---

## Phase 0 — Verify assumptions before writing code

1. Confirm the installed SDK accepts a caller-supplied verifier:

   ```bash
   uv run python -c "import inspect, supabase_auth._async.gotrue_client as g; print(inspect.getsource(g.AsyncGoTrueClient.exchange_code_for_session))"
   ```

   If `code_verifier` is not an accepted param — or the method reads the
   verifier only from the client's own storage — use the raw
   `POST {supabase_url}/auth/v1/token?grant_type=pkce` fallback described in
   plan §5, and normalise the response into the shape `_session_out` expects.
   **Record which path you took in a comment in `oauth.py`.**

2. Confirm the baseline is green before you change anything:

   ```bash
   uv run pytest -q
   ```

**Done when:** you know which exchange mechanism you are using, and the
existing suite passes.

---

## Phase 1 — Configuration

**Files:** `backend/app/settings.py`, `.env.example`

1. Add the five settings fields from plan §4 with their defaults.
2. Extend `model_post_init` with the two new guards — `oauth_enabled` without
   `supabase_anon_key`, and a non-`https` `public_base_url` in `pilot`. Match
   the existing raise-with-a-plain-message style.
3. Mirror all five variables into `.env.example` with a short comment each.
4. Document the Supabase dashboard prerequisites (plan §4) in `.env.example`
   or `README.md` — enable Google, set client id/secret, allow-list
   `{public_base_url}/auth/callback`, review identity linking.

**Test:** `uv run pytest -q` — must still pass. `ENVIRONMENT=test` means the
new guards stay dormant in the suite; add a direct unit test that constructs
`Settings(environment="pilot", ...)` and asserts each guard raises.

**Done when:** settings load in `test` and `dev` unchanged, and both new guards
are proven to fire.

---

## Phase 2 — OAuth primitives

**Files:** `backend/app/identity/oauth.py` (new), `backend/app/cache.py`,
`backend/app/errors.py`, `backend/app/audit/writer.py`,
`backend/app/log_config.py`

1. Create `oauth.py` with exactly the surface in plan §5: `PROVIDERS`,
   `is_enabled`, `PendingAuth`, `make_pkce_pair`, `authorize_url`,
   `exchange_code`, `profile_fields`. Give it a module docstring in the style
   of `security.py`'s, stating why the throwaway client is mandatory here.
2. Add `oauth_state_key(state)` to `cache.py` beside `lockout_key`.
3. Add an `AppError` subclass for OAuth failure with `message_key
   = "errors.oauth_failed"`. Do not use a raw `HTTPException`.
4. Add the three audit action constants from plan §10.
5. Verify `log_config.py` redacts `code`, `state`, and `code_verifier`; extend
   the redaction list if it does not.

**Test:** unit tests for `make_pkce_pair` (challenge is base64url-unpadded
SHA-256 of the verifier, no `=` padding) and `authorize_url` (all five query
params present and correctly encoded).

**Done when:** the primitives are covered by unit tests and nothing else in the
app imports them yet.

---

## Phase 3 — Service layer

**Files:** `backend/app/identity/service.py`, `backend/app/identity/schemas.py`

1. **Refactor first.** Extract `provision_role_records(...)` out of
   `register()` per plan §8a and have `register()` call it. Run the suite —
   `uv run pytest -q` must be green **before** you add anything new. This step
   must be behaviour-preserving.
2. Add `OnboardingRequest` / `DoctorOnboardingRequest` to `schemas.py`. Reuse
   `_normalise_email`'s conventions where relevant, but do **not** add a
   `password` field and do not bend `_RegisterBase`.
3. Implement `login_with_oauth(...)` following plan §8b step by step. Steps 2,
   3 and 4 (email collision, suspended, locked) each revoke the freshly issued
   session with `_sign_out(...)` before raising — a session that is issued but
   not handed over must never be left live.
4. Implement `complete_onboarding(...)` per plan §8c, including the
   already-provisioned re-check.

**Test:** `uv run pytest -q`. The existing register/login tests are the
regression guard for step 1.

**Done when:** the refactor changed no behaviour, and both new service
functions exist with no route calling them yet.

---

## Phase 4 — Onboarding gate

**Files:** `backend/app/deps.py`, `backend/app/web/router.py`

1. Add `onboarding_complete: bool = True` to the `Actor` dataclass and compute
   it in `_load_actor`. The doctor and clinic-admin branches already query
   their role tables — add the `Patient` lookup for the patient branch. Do not
   add a second query where an existing one already answers the question.
2. In `_guarded`, redirect an actor with `onboarding_complete is False` to
   `/{loc}/onboarding` before any dashboard renders.
3. Leave `ActorDep` and the JSON API ungated — `/api/v1/me` must keep working
   mid-onboarding.

**Test:** `uv run pytest -q`. Every existing user fixture creates a role row,
so all current tests must stay green — if any break, your default is wrong.

**Done when:** the gate exists and the full suite is unchanged.

---

## Phase 5 — Routes

**Files:** `backend/app/web/router.py`

Implement the three routes from plan §7 in this order:

1. `POST /{locale}/auth/oauth/{provider}` — rate limit, provider check, PKCE
   pair, state stored in cache **and** in the `SameSite=Lax` `cn_oauth_state`
   cookie, `AUTH_OAUTH_START` audit row, 303 to the authorize URL.
2. `GET /auth/callback` — unprefixed. Follow the seven-step order in plan §7
   exactly. Consume the state before doing anything else. Clear
   `cn_oauth_state` on every exit path. Every failure renders
   `auth/login.html` with the single generic `errors.oauth_failed`.
3. `GET` / `POST /{locale}/onboarding` — per plan §9, reusing
   `_clinic_options` and mirroring `register_submit`'s presentation-layer
   validation minus the password rules.

Handle the `SameSite=Strict` hazard (plan §3a) with the same-site interstitial:
`frontend/templates/auth/oauth_complete.html`, carrying the `Set-Cookie`
headers, a `<meta http-equiv="refresh">`, and a plain visible link as the
no-JS/no-refresh fallback.

**Done when:** the flow is wired end to end, even though the buttons do not
exist yet.

---

## Phase 6 — Templates, CSS, i18n

**Files:** `frontend/templates/auth/{login,register,onboarding,oauth_complete}.html`,
`frontend/static/css/app.css`, `backend/app/i18n/messages/{en,ur}.json`,
`backend/app/web/router.py`

1. Expose `oauth_enabled` through `_base_context` so every page has it.
2. Add the `{% if oauth_enabled %}` provider block to `login.html` and
   `register.html` — a divider plus a POST form. Use the `f.*` macros from
   `partials/form_field.html`.
3. Build `onboarding.html` from `register.html`'s structure: functional role
   tiles gating `#doctor-fields`, name prefilled, phone, consent, doctor block.
   No email field, no password field.
4. Add all eight i18n keys from plan §10 to **both** `en.json` and `ur.json`.
   Urdu strings must be real translations, not English placeholders.
5. Style with existing tokens from `tokens.css` only — `test_no_raw_hex_in_app_css`
   will fail on a literal hex value.

**Test:** `uv run pytest tests/test_web.py -v` — `test_t18_catalogue_is_complete`,
`test_no_raw_hex_in_app_css`, and `test_design_tokens_match_design_doc` are the
ones that will catch mistakes here.

**Done when:** both auth pages render the button, RTL included, and the CSS and
catalogue tests pass.

---

## Phase 7 — Tests

**Files:** `tests/fakes.py`, `tests/test_oauth.py` (new)

1. Extend `FakeSupabaseAuth` per plan §11: `authorize(...)` and
   `exchange_code_for_session(params)`, following the existing attribute-only
   `_Obj` pattern and raising `AuthApiError` on verifier mismatch or code
   replay. Keep issuing HS256 test JWTs so the existing `fake_decode_token`
   monkeypatch keeps working untouched.
2. Write `tests/test_oauth.py` covering **all fourteen** cases T1–T14 in plan
   §11, named after the case ids the way `test_auth.py` names its own.
3. Add the plan §12 edge cases that are not already covered by T1–T14.

**Test:** `uv run pytest tests/test_oauth.py -v`, then the full suite.

**Done when:** all fourteen cases pass and the pre-existing 46 tests still do.

---

## Phase 8 — Final verification

Run, in order:

```bash
uv run ruff check backend tests
uv run ruff format backend tests
uv run pytest -q
```

Then verify by hand against a real Supabase project with the Google provider
enabled and `OAUTH_ENABLED=true`:

1. Google sign-in works end to end from `/en/login` **and** from `/en/register`.
2. A brand-new Google user lands on onboarding and **cannot** reach
   `/en/patient` or `/en/doctor` by typing the URL until it is completed.
3. Onboarding as a patient produces a `Patient` row with a passport number;
   onboarding as a doctor produces a `PENDING` `Doctor` plus an active
   `DoctorAffiliation`, and the dashboard shows the unverified banner.
4. A returning Google user goes straight to their dashboard with no onboarding.
5. Cancelling at Google's consent screen returns a generic error, not a stack
   trace and not the provider's message.
6. A suspended profile signing in with Google gets the same generic failure as
   a wrong password, and the issued Supabase session is revoked.
7. The state is single-use: replaying a callback URL fails.
8. `OAUTH_ENABLED=false` hides both buttons and disables both routes.
9. Password login, register, refresh, and logout all still work.
10. Both flows work in `/ur/` with correct RTL layout.

Finally, confirm the Definition of Done checklist in `docs/oauth.md` §14, and
confirm `git status` shows **no** new file under `alembic/versions/`.

---

## If you get stuck

Stop and ask rather than guessing on any of these:

- The SDK does not accept a caller-supplied `code_verifier` **and** the raw
  `grant_type=pkce` fallback also fails.
- Session cookies still are not sent after the interstitial (plan §3a).
- Supabase links or fails to link a Google identity to an existing email in a
  way the plan's §8b step 2 guard does not anticipate.
- Any point where the only way forward appears to be a schema change.
