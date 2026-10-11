# Security Review Findings — CuraNode-AI (OAuth focus)

Reviewed: `backend/app/identity/oauth.py`, `backend/app/identity/service.py`
(`login_with_oauth`/`complete_onboarding`), the OAuth/onboarding routes in
`backend/app/web/router.py`, `backend/app/identity/security.py`,
`backend/app/identity/router.py`, `backend/app/identity/schemas.py`,
`backend/app/deps.py`, `backend/app/settings.py`, `backend/app/cache.py`,
`backend/app/errors.py`, `backend/app/log_config.py`,
`backend/app/audit/writer.py`, `backend/app/db/models.py`, and the auth
templates (`frontend/templates/auth/*.html`, `shell.html`,
`partials/form_field.html`).

## Overall assessment

The OAuth implementation is unusually well-hardened for a newest feature:

- PKCE is implemented correctly (challenge/verifier bound and verified at the
  IdP, not re-validated in-app).
- CSRF state is triple-checked (query param, `SameSite=Lax` cookie, server-side
  cache entry) and consumed exactly once regardless of outcome.
- The account-collision guard (`service.py:360-369`) correctly runs *before*
  the fallback `Profile` row could be autoflushed into the same lookup.
- Role escalation to `admin` is impossible via any client-controlled field
  (both `register_submit` and `onboarding_submit` clamp the role form field to
  `patient`/`doctor` only).
- All user-facing form text and translated strings render through Jinja's
  default autoescaping — including the `translate()` helper's naive
  `str.replace` interpolation, which still passes through `{{ }}` escaping at
  render time, so no XSS despite the helper itself not sanitizing its inputs.
- JWTs are verified with an explicit algorithm allow-list (`ES256` only) plus
  audience check, against project-bound JWKS — no algorithm-confusion path.
- Role/verification status is always re-read from the DB per request
  (`deps._load_actor`) rather than trusted from the token, so revocation is
  immediate.

No HIGH-confidence, concretely exploitable vulnerability (meeting the >80%
confidence bar) was found in the OAuth code or in the reviewed
password-auth/deps code.

## Findings

### 1. Email-normalization inconsistency between OAuth and password paths

- **Files:** `backend/app/identity/oauth.py:88-95` (`profile_fields`),
  `backend/app/identity/service.py:399` (`user.email = fields["email"]`) and
  `:366` (`_user_by_email(session, fields["email"])`), contrasted with
  `backend/app/identity/schemas.py:27-32` (`_normalise_email`).
- **Severity:** MEDIUM
- **Category:** Broken Authentication / Access Control (account-linking guard
  bypass via inconsistent normalization)
- **Description:** Password registration/login normalize email via
  `_normalise_email` (`.strip().lower()`) before every DB comparison and
  write, but the OAuth path uses the provider's raw `user.email` verbatim —
  never lowercased — both for the account-collision lookup
  (`_user_by_email(session, fields["email"])`) and for what gets persisted
  into `Profile.email` on every OAuth login. `Profile.email` has no
  case-insensitive (citext) type in the model (`db/models.py:111`, plain
  `String(320)`), and the lookup is an exact SQL string match. A Google (or
  future OAuth provider) account whose ID-token `email` claim differs in case
  from an existing password account's normalized, lower-cased address would
  not be found by `_user_by_email`, silently bypassing the "unverified
  provider email must never take over an existing password account" guard
  the code's own comments describe as load-bearing.
- **Exploit scenario (precondition-heavy, hence MEDIUM not HIGH):** Requires
  an attacker who legitimately controls (or can register) an OAuth-provider
  account whose canonical `email` claim is a differently-cased variant of a
  victim's existing password-account address (plausible for Google
  Workspace/custom-domain accounts, and increasingly relevant as more
  providers are added — the code is already structured for that via the
  `PROVIDERS` frozenset). On first Google sign-in, the account-collision
  check is bypassed, and a fresh `Profile`/role record is silently created
  (or merged, depending on Supabase's own auto-linking behavior) under the
  mismatched-case email rather than being refused as intended.
- **Fix:** Apply the same `_normalise_email` normalization to
  `fields["email"]` in `oauth.profile_fields` (or in
  `service.login_with_oauth` before the `_user_by_email` lookup and before
  persisting `user.email`), and prefer a case-insensitive column
  type/comparison (`citext` or `func.lower()` on both sides) for defense in
  depth — CLAUDE.md notes CITEXT was deliberately avoided in favor of
  app-level normalization, so that normalization needs to cover both auth
  paths, not just the password one.

## Ruled out (investigated, not included as findings)

- **`new_auth_client()` using the service-role key rather than
  `supabase_anon_key` for the PKCE exchange** (`security.py:105-113`): matches
  the pre-existing password-flow design intentionally, per
  `.claude/spec/OAUTH/oauth.md` §4/§5 (the anon key was only ever meant for a
  raw-HTTP fallback path that the SDK made unnecessary). The key never leaves
  the server. Not a new issue introduced by OAuth.
- **`next_path` meta-refresh in `oauth_complete.html`**: fully server-computed
  from an allow-listed locale (`normalise_locale`) and internal role, not
  attacker-influenced, so no open-redirect/XSS path.
- **PKCE code/verifier mixing across sessions**: prevented at the IdP
  (Supabase/Google), not just in-app — the authorization server rejects any
  `code`/`verifier` pair that doesn't match the original `code_challenge`.
- **Stateless JWT access tokens not being revoked on logout/sign-out**: a
  known, intentional tradeoff (15-minute `access_token_minutes` window);
  suspension/lockout are enforced per-request from the DB regardless of JWT
  validity, so this doesn't create a bypass.
