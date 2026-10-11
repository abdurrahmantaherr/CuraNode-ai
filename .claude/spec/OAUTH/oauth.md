# OAuth Sign-In — Implementation Plan

**Feature:** Google sign-in ("Continue with Google") for CuraNode-AI
**Status:** Planned — no code written yet
**Scope of this document:** a plan only. It cites real files and line ranges in
this repository; read the cited code before implementing.

---

## 1. Feature Summary

Add "Continue with Google" to the login and register pages. Identity stays
fully delegated to Supabase Auth — this repo adds no new token minting, no
password handling, and **no new tables**. The browser never sees a token: the
backend builds the provider authorize URL, and a backend callback exchanges the
authorization code (PKCE) for a session server-side, then issues the same
`cn_access`/`cn_refresh` cookies the password flow already issues.

First-time OAuth users complete a short onboarding form (role, name, consent,
and doctor credentials where applicable) before they can reach any dashboard.
Returning users go straight to their area.

**Confirmed decisions:**

| Decision | Choice |
| --- | --- |
| Provider | Google only, behind a provider allow-list so more can be added |
| Flow | Server-side PKCE (authorization code + `code_verifier`) |
| New-user role | Chosen on a post-callback onboarding page, not inferred |

---

## 2. Architectural Constraints to Respect

These come from `CLAUDE.md` and the module docstrings. Violating any of them
breaks an invariant the existing suite or the shared Supabase schema depends
on.

1. **Two Supabase client lifecycles must not be conflated.**
   `security.get_supabase_client()` (`identity/security.py:90-102`) is a
   process-wide singleton for `auth.admin.*` **only**.
   `security.new_auth_client()` (lines 105-113) builds a fresh, single-use
   client for anything that mutates session state. `exchange_code_for_session`
   is exactly such a call — it **must** use `new_auth_client()`.
2. **Role is never read from a token.** `deps._load_actor` (`deps.py:58-102`)
   re-reads `user_profile` / `doctor` / `doctor_affiliation` on every request.
   OAuth changes nothing here.
3. **The `on_auth_user_created` trigger fires for OAuth signups too.** It
   inserts a default `user_profile` row (`role='patient'`, `locale='en'`,
   `status='active'`) the instant a Supabase auth user exists. The callback
   must therefore **update** that row, never insert a second one — the same
   pattern as `service.register()` (`identity/service.py:164-184`).
4. **Migrations are additive and hand-written — and this feature needs none.**
   Provider identities live in Supabase's own `auth.identities`, and
   "onboarding incomplete" is *derived* from the absence of a
   `patient` / `doctor` / `clinic_staff` row rather than stored in a new
   column.
5. **Cookies are issued in exactly one place**, `set_session_cookies`
   (`identity/router.py:30-49`). The OAuth callback reuses it verbatim.
6. **Failure modes are `AppError` subclasses with an i18n `message_key`**
   (`errors.py`), never a raw `HTTPException`.
7. **No ad-hoc role checks.** Anything role-gated goes through the four
   dependencies in `deps.py`.

---

## 3. Two Hazards That Must Be Handled Explicitly

### 3a. `cookie_samesite` defaults to `"strict"`

The hop into `GET /auth/callback` is a cross-site top-level navigation from
Google via Supabase. The `Set-Cookie` on the callback response *is* stored, but
browsers treat the request our subsequent `303` triggers as part of a
cross-site-initiated redirect chain, so a `Strict` session cookie can be
withheld on that first hop — the user lands back on the login page looking
signed out, then appears signed in on reload.

Required handling, in preference order:

- **Preferred:** land the callback on a same-site interstitial template
  (`auth/oauth_complete.html`) that carries the `Set-Cookie` headers and then
  navigates on via `<meta http-equiv="refresh">` (plus a plain link for the
  no-JS/no-refresh case). The next request is same-site-initiated, so `Strict`
  cookies are sent.
- The **state cookie must be `SameSite=Lax`** regardless — `Strict` would drop
  it on the return hop and break every callback. This is not optional.
- The session cookies must **not** be weakened to `SameSite=None`.

### 3b. Role records are irreversible once written

`doctor.pmdc_number` is `UNIQUE NOT NULL` and `patient.passport_uid` is a
permanent, user-facing identifier. A `Patient` row must **not** be created at
callback time for someone who meant to register as a doctor. This is the whole
reason role provisioning is deferred to the onboarding POST (§8-§9) instead of
happening in the callback.

Note also that `AccountStatus.PENDING_VERIFICATION` is **not** a usable
"role not yet chosen" state: `deps._load_actor` (`deps.py:68`) rejects any
profile whose status is not `ACTIVE`, so parking OAuth users there would log
them straight back out.

---

## 4. Configuration

### `backend/app/settings.py`

New fields on `Settings`:

| Field | Default | Purpose |
| --- | --- | --- |
| `supabase_anon_key: str` | `""` | `apikey` header for the PKCE token exchange |
| `public_base_url: str` | `"http://127.0.0.1:8000"` | Builds the absolute `redirect_to` |
| `oauth_enabled: bool` | `False` | Master switch; hides the buttons when off |
| `oauth_providers: str` | `"google"` | Comma-separated allow-list, parsed to a frozenset |
| `oauth_state_ttl_s: int` | `600` | Lifetime of a pending authorization |

Extend `model_post_init` in the same fail-fast style as the existing guards
(`settings.py:54-62`):

- `oauth_enabled` set but `supabase_anon_key` empty → raise.
- `environment == "pilot"` and `public_base_url` not `https://` → raise.

### `.env.example`

Add the five variables above with commented guidance.

### Supabase dashboard prerequisites (document these)

1. Enable the **Google** provider; set its client id and secret.
2. Add `{public_base_url}/auth/callback` to the project's redirect allow-list.
3. Review the project's **identity-linking** setting — see the email-collision
   guard in §7, step 2.

---

## 5. New Module: `backend/app/identity/oauth.py`

Keeps provider mechanics out of `service.py`.

```python
PROVIDERS: frozenset[str] = frozenset({"google"})

def is_enabled(provider: str) -> bool
    # settings.oauth_enabled AND provider in the parsed allow-list

@dataclass(frozen=True)
class PendingAuth:
    verifier: str
    locale: str
    next_path: str | None
    created_at: datetime

def make_pkce_pair() -> tuple[str, str]
    # secrets.token_urlsafe(64) verifier;
    # challenge = base64url-unpadded(sha256(verifier))

def authorize_url(provider: str, *, challenge: str, state: str,
                  redirect_to: str) -> str
    # {supabase_url}/auth/v1/authorize
    #   ?provider=...&redirect_to=...&state=...
    #   &code_challenge=...&code_challenge_method=s256

async def exchange_code(code: str, verifier: str)
    # (await security.new_auth_client()).auth.exchange_code_for_session(
    #     {"auth_code": code, "code_verifier": verifier})

def profile_fields(user) -> dict
    # email, plus full_name from user.user_metadata:
    #   "full_name" -> "name" -> email local-part
```

**Verification step for the implementer:** confirm that the installed
`supabase-auth` version accepts `code_verifier` in `exchange_code_for_session`:

```bash
uv run python -c "import inspect, supabase_auth._async.gotrue_client as g; print(inspect.getsource(g.AsyncGoTrueClient.exchange_code_for_session))"
```

If the SDK reads the verifier only from its own storage (which is
per-client and therefore useless with the throwaway-client pattern), fall back
to a direct `POST {supabase_url}/auth/v1/token?grant_type=pkce` with the
`apikey` header and an `{"auth_code", "code_verifier"}` body, and normalise the
response into the same shape `_session_out` expects
(`.session.access_token` / `.refresh_token` / `.expires_at` / `.expires_in`,
`.user.id`).

---

## 6. State Handling — Reuse `backend/app/cache.py`

No new storage layer. Add one keyspace helper next to `lockout_key`
(`cache.py:85-99`):

```python
def oauth_state_key(state: str) -> str:
    return f"oauth_state:{state}"
```

**On start:**
- `state = secrets.token_urlsafe(32)`
- store the `PendingAuth` under `oauth_state_key(state)` with
  `settings.oauth_state_ttl_s`
- **and** set a short-lived cookie `cn_oauth_state` (HttpOnly, `Secure` per
  settings, `SameSite=Lax`, `max_age=oauth_state_ttl_s`) holding the same value

**On callback:** require the query `state`, the cookie, and the cache entry to
all agree, then `cache.delete(...)` before doing anything else — single-use.
The cookie is what binds the pending authorization to the originating browser;
the cache entry alone is process-wide and would otherwise be replayable by a
third party. This is the CSRF defence for the flow — the app has no other CSRF
machinery.

The `cn_oauth_state` cookie is deleted on **every** callback exit path,
success or failure.

---

## 7. Routes

All three go in `backend/app/web/router.py`, following the existing
`_base_context` / `_render` / `_area` / `_clinic_options` helpers.

### `POST /{locale}/auth/oauth/{provider}`

A **POST**, not a link, so a prefetch, an `<img>`, or a stray crawler cannot
start a flow.

1. `await enforce_auth_rate_limit(request)` (catch `RateLimited` → 429 login
   page, matching `login_submit`'s pattern at lines 113-115).
2. Unknown or disabled provider → re-render `auth/login.html` with
   `errors.oauth_unavailable`. No redirect.
3. `make_pkce_pair()`, generate state, store `PendingAuth` (carrying the
   locale and the `next` query param), write an `AUTH_OAUTH_START` audit row.
4. `303` to `authorize_url(...)`, setting the `cn_oauth_state` cookie.

### `GET /auth/callback`

**Not locale-prefixed** — the redirect URI registered with Supabase has to be a
single fixed string, and the locale is recovered from `PendingAuth.locale`. The
`main.py` locale middleware already falls back to the `cn_locale` cookie for
unprefixed paths, so nothing there changes.

Order of operations:

1. `enforce_auth_rate_limit`.
2. Provider `error` / `error_description` query params → generic failure.
3. State triple-check (query + cookie + cache), consume the cache entry.
4. `oauth.exchange_code(code, pending.verifier)`.
5. `service.login_with_oauth(...)` (§8).
6. `set_session_cookies(response, pair)`.
7. Render the same-site interstitial (§3a) pointing at
   `/{loc}/onboarding` when onboarding is required, else
   `pending.next_path` or `/{loc}/{_area(role)}`.

Every failure path renders `auth/login.html` with **one generic**
`errors.oauth_failed` message. Never surface the provider's raw error text, the
code, the state, or the verifier to the user.

### `GET` / `POST /{locale}/onboarding`

See §9.

---

## 8. Service Layer — `backend/app/identity/service.py`

### 8a. Refactor first (reuse, not duplication)

Lift the role-provisioning block out of `register()` (lines 186-217 — the
`Patient` + passport branch and the `Doctor` + `DoctorAffiliation` branch) into:

```python
async def provision_role_records(
    session, user, *, role, full_name,
    pmdc_number=None, specialty=None, primary_clinic_id=None,
) -> None
```

`register()` calls it; onboarding calls it. `_unique_passport_no` and the
"`verification_status` is never sourced from the request" rule
(`service.py:192-197`) stay exactly as they are — that line is what keeps
self-registration from becoming self-authorisation, and it applies identically
to OAuth doctors.

### 8b. `login_with_oauth`

```python
async def login_with_oauth(
    session, exchanged, *, provider: str, ip=None, user_agent=None,
) -> tuple[SessionOut, security.TokenPair, bool]   # bool = onboarding_required
```

1. `user_id = uuid.UUID(exchanged.user.id)`; `session.get(Profile, user_id)` —
   the trigger has already committed the row (same assumption and same
   defensive `if user is None` fallback as `register()`).
2. **Email-collision guard (security-critical).** If a *different* `Profile`
   already holds this email, do **not** sign in: revoke the just-issued session
   with `_sign_out(...)` and raise `Unauthenticated`. An unverified provider
   email must never be able to take over a password account. Supabase's own
   "link identity on verified email" setting is the intended mechanism for
   genuine linking and should be reviewed in the dashboard (§4).
3. `status != AccountStatus.ACTIVE` → `_sign_out` + `Unauthenticated`,
   mirroring the suspended-account handling at lines 282-287.
4. `locked_until` in the future → `_sign_out` + `Unauthenticated`. OAuth
   **honours** an existing lock but never increments `failed_logins` — there is
   no password to brute-force, so incrementing would only give an attacker a
   way to lock a victim out.
5. Backfill from `oauth.profile_fields`: set `email`; set `full_name` **only if
   currently `NULL`**; set `is_synthetic = settings.environment != "pilot"`.
   Never touch `role` — that is onboarding's job.
6. Clear `failed_logins` / `locked_until`, set `last_login_at`, delete the
   lockout cache key (mirrors lines 289-292).
7. `onboarding_required` = no `Patient`, `Doctor`, or `ClinicStaff` row exists
   for this user.
8. Write the `AUTH_OAUTH_LOGIN` audit row, then `_session_out(user,
   exchanged.session)` and `commit()`.

### 8c. `complete_onboarding`

```python
async def complete_onboarding(session, user, body, *, ip=None, user_agent=None) -> SessionOut
```

Re-checks that no role record exists (idempotency against a double-submit),
writes `role`, `full_name`, `phone_e164`, `preferred_locale`, calls
`provision_role_records(...)`, writes `AUTH_OAUTH_ONBOARDED`, commits.

---

## 9. Onboarding Page

New template `frontend/templates/auth/onboarding.html`, modelled on
`auth/register.html`:

- role tiles (patient / doctor) — functional, gating `#doctor-fields` exactly
  as the register page does
- full name, prefilled from Google
- optional phone
- consent checkbox
- doctor-only PMDC / specialty / clinic block, fed by the existing
  `_clinic_options(session)` helper (`web/router.py:69-71`)
- **no** email and **no** password fields

`POST` mirrors `register_submit`'s presentation-layer validation (name
required, consent required, doctor fields required when role is doctor) and
reuses the doctor-field logic, but not the password rules. Add
`OnboardingRequest` / `DoctorOnboardingRequest` to `identity/schemas.py` rather
than bending `_RegisterBase`, which requires `password`.

### It cannot be skipped

Add `onboarding_complete: bool = True` to the `Actor` dataclass
(`deps.py:41-56`) and compute it in `_load_actor`, which already queries
`Doctor` / `ClinicStaff` (lines 74-92) — add the `Patient` lookup for the
patient branch. `_guarded` (`web/router.py:287-337`) then redirects any
incomplete actor to `/{loc}/onboarding` before rendering a dashboard, so a
deep link cannot bypass it.

Do **not** gate the JSON `ActorDep` on this — `/api/v1/me` must keep working
for a mid-onboarding user.

---

## 10. Templates, i18n, Audit, Logging

**Templates.** Add an `{% if oauth_enabled %}` block above the credential form
in `auth/login.html` and `auth/register.html`: a divider plus a `POST` form to
`/{{ locale }}/auth/oauth/google`. Pass `oauth_enabled` through
`_base_context` so every page gets it for free.

**CSS.** `tests/test_web.py` enforces `test_no_raw_hex_in_app_css` and
`test_design_tokens_match_design_doc` — the provider button must be styled with
existing tokens from `frontend/static/css/tokens.css`.

**i18n.** New keys in **both** `backend/app/i18n/messages/en.json` and
`ur.json` — `test_t18_catalogue_is_complete` fails on any key present in one
and missing from the other:

```
auth.oauth.google
auth.oauth.divider
auth.onboarding.title
auth.onboarding.subtitle
auth.onboarding.submit
errors.oauth_failed
errors.oauth_unavailable
errors.oauth_email_conflict
```

**Audit.** New action constants in `backend/app/audit/writer.py` beside the
existing ones (lines 22-26):

```python
AUTH_OAUTH_START     = "auth.oauth_start"
AUTH_OAUTH_LOGIN     = "auth.oauth_login"
AUTH_OAUTH_ONBOARDED = "auth.oauth_onboarded"
```

`detail` carries `{"provider": "google"}` only — never the token, the code, the
state, or the verifier.

**Logging.** Confirm `log_config.py`'s PII redaction covers `code`, `state`,
and `code_verifier`; extend the redaction list if it does not.

---

## 11. Test Plan

### Extend `tests/fakes.py`

`FakeSupabaseAuth` currently mirrors only `admin.create_user`,
`admin.sign_out`, `sign_in_with_password`, and `refresh_session`. Add, in the
same attribute-only `_Obj` style:

- `authorize(provider, email, *, challenge, user_metadata=None) -> str` — a
  test-side helper that mints an auth code bound to the challenge.
- `FakeAuthClient.exchange_code_for_session(params)` — validates the verifier
  against the stored challenge, raises `AuthApiError` on mismatch or replay,
  and returns `_Obj(user=..., session=issue_session(...))` in the same shape
  the password path already returns.

Keep issuing HS256 test JWTs — `conftest.py`'s existing `fake_decode_token`
monkeypatch (lines 88-90) then covers the OAuth path with no change at all.

### New `tests/test_oauth.py`

| # | Case | Expected |
| --- | --- | --- |
| T1 | `POST /en/auth/oauth/google` | 303 to `…/auth/v1/authorize`, `cn_oauth_state` set |
| T2 | Unknown or disabled provider | login page + `errors.oauth_unavailable`, no redirect |
| T3 | Callback with missing/mismatched state | 400 login page, no session cookies |
| T4 | Callback with a replayed state | rejected (cache entry already consumed) |
| T5 | Callback, brand-new user | lands on `/en/onboarding`, session cookies set |
| T6 | Onboarding as patient | `Patient` row + passport, then `/en/patient` |
| T7 | Onboarding as doctor | `Doctor` PENDING + `DoctorAffiliation`, unverified banner |
| T8 | Returning, already-complete user | straight to their area, no onboarding |
| T9 | Suspended profile | generic failure, `sign_out` called, no cookies |
| T10 | Email belongs to a different `user_id` | refused, session revoked |
| T11 | Deep link to `/en/patient` mid-onboarding | 303 back to `/en/onboarding` |
| T12 | `catalogue.missing_keys()` | still empty |
| T13 | `cn_oauth_state` cookie attributes | `SameSite=Lax`, HttpOnly, short max-age |
| T14 | Existing password login/register suite | unchanged (regression) |

---

## 12. Edge Cases & Error Handling

| Case | Handling |
| --- | --- |
| User cancels at Google | `error=access_denied` → login page, `errors.oauth_failed` |
| State expired (> TTL) | Treated as invalid; user restarts the flow |
| Code exchange raises `AuthApiError` | Generic `errors.oauth_failed`; nothing written |
| Provider returns no email | Refuse — `email` is `NOT NULL` on `user_profile` |
| Onboarding submitted twice | Second submit is a no-op (role record already exists) |
| PMDC number already taken | `ValidationFailed` on the PMDC field, form re-rendered |
| OAuth user later uses password login | Fails as normal — no password exists in Supabase |

---

## 13. Rollout & Rollback

`oauth_enabled=False` is the kill switch: the buttons disappear, both routes
return the unavailable error, and every existing password path is untouched.

Because no migration and no schema change ship with this feature, rollback is a
config flip plus a code revert — there is no data to undo. Existing sessions,
password logins, lockout counters, and audit history are all unaffected.

---

## 14. Definition of Done

- [ ] `uv run pytest -q` passes, including the new `tests/test_oauth.py`
- [ ] `uv run ruff check backend tests` and `ruff format` are clean
- [ ] `catalogue.missing_keys()` returns empty for `ur`
- [ ] Google sign-in works end-to-end against a real Supabase project with
      the Google provider enabled
- [ ] A brand-new Google user cannot reach `/en/patient` or `/en/doctor`
      without completing onboarding
- [ ] A suspended profile signing in with Google gets the same generic failure
      as a wrong password, and the issued Supabase session is revoked
- [ ] `oauth_enabled=False` fully hides and disables the feature
- [ ] No new Alembic migration was created

---

## 15. Out of Scope

- Account-linking UI (attaching Google to an existing password account from a
  settings page)
- Apple, Microsoft, or any provider beyond Google
- Mobile deep links / native app redirect schemes
- OAuth for the JSON API — `/api/v1/auth/*` stays password-only
- Password reset (still unimplemented; the login page's "Forgot password?" is
  inert)
