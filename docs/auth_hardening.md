# Authentication hardening — operations notes

Companion to the authentication security audit fixes (C1, H1–H3, M1–M6 and
the low-severity clean-up). It records what code cannot do on its own:
Supabase dashboard settings, migration roll-out, and the trade-offs that were
chosen deliberately.

## 1. Supabase dashboard settings

Registration has **no email-verification step** (product decision): users are
created with `email_confirm=true` through the admin API, so no confirmation
email is sent and the "Confirm email" setting, SMTP and email templates do
not affect registration.

| # | Where (Supabase dashboard) | Set to | Why |
|---|---|---|---|
| 1 | Authentication → Sessions / JWT → **JWT expiry** | `900` seconds (match `ACCESS_TOKEN_MINUTES=15`) | Optional — the app already refuses any access token older than `ACCESS_TOKEN_MINUTES` (§4), whatever this is set to. |
| 2 | Authentication → Sign In / Providers → **identity linking** | either setting is safe for this app | The app refuses Google sign-in for any Supabase user that has a password identity (§2), so automatic linking cannot hand a squatted account to a Google user. Turning automatic linking *off* additionally stops Supabase from attaching the Google identity at all. |

**OAuth (`OAUTH_ENABLED`)**: defaults to `false`. Because password
registration does not prove email ownership, the app only lets Google sign
in to accounts that were created through Google. Keep it `false` in any
environment that runs code without that guard.

## 2. What changed, by finding

- **C1/H2 — Data API side door.** Migration `a3c1f0e7b2d4` adds BEFORE
  triggers that refuse, for `anon`/`authenticated` callers that are not
  admins, any change to `doctor.verification_status/verified_by/verified_at/
  pmdc_number/user_id` (and any pre-verified INSERT), to
  `user_profile.email/is_synthetic/failed_logins/locked_until`, and to
  `patient.passport_uid/user_id`. The app's own `postgres` connection,
  `service_role`, the `handle_auth_user_*` sync triggers and `is_admin()`
  callers are unaffected. It also adds `uq_user_profile_email_lower`.
- **H1 — OAuth pre-account-takeover.** Without email verification, anyone
  can register `victim@gmail.com` with their own password. If the real owner
  later used Google, Supabase's automatic linking would attach their Google
  identity to that attacker-controlled account. `service.login_with_oauth`
  therefore refuses (and revokes the fresh session for) any Google sign-in
  whose Supabase user has a password (`email`) identity, or whose identity
  list is missing. The owner of a password account keeps signing in with the
  password; Google-only accounts are unaffected. The reverse order (Google
  first, then a password registration) is refused as a duplicate email.
- **H3/M4 — lockout.** Atomic reserve-then-call: each password attempt
  claims a slot with one `UPDATE … WHERE failed_logins < 10 RETURNING` before
  Supabase is called, so concurrency cannot exceed 10 checks per window; an
  expired lock starts a fresh window instead of re-locking on the next single
  failure.
- **M1 — enumeration.** Unknown-email and locked logins make one decoy
  Supabase sign-in, so they cost the same round trip as a wrong password.
  Registration deliberately does NOT hide existence (product decision): an
  already-registered address gets `409 EMAIL_ALREADY_REGISTERED` ("An account
  with this email already exists. Please log in."). The per-IP auth rate
  limit bounds probing.
- **M2 — registration atomicity.** PMDC numbers are checked before Supabase
  is called; anything failing after `admin.create_user` rolls back and
  deletes exactly that just-created user. A lost race — Supabase Auth's
  unique-email constraint answering `email_exists` — returns the same 409 and
  never deletes anyone.
- **M3 — logout.** See §4.
- **M5** — every non-static response carries `Cache-Control: no-store`.
- **M6** — login and registration (web + API) refuse a foreign `Origin` /
  `Referer`. The OAuth callback is a GET and is not subject to it.

Deliberately **not** added: a partial unique index for active consent
grants. "Active" means unrevoked *and* unexpired, and an index predicate
cannot use `now()`; `WHERE revoked_at IS NULL` would let an expired grant
block a new one, changing consent rule BL-05/BL-12. Concurrent grants are
serialised by a `SELECT … FOR UPDATE` on the patient row instead.

## 3. Rolling out the migrations

`a3c1f0e7b2d4` and `b7d2e9c4a1f6` touch tables owned by the wider CuraNode
product (`doctor`, `user_profile`, `patient`, and TRUNCATE privileges on
shared tables). They have **not** been applied to any shared database.

1. Get sign-off from the shared-schema owners — the triggers change what
   their clients may write through the Data API (by design).
2. The live database's `alembic_version` was `35c4bc13fa37` on 2026-10-08,
   a revision this repository does not contain. Reconcile that before running
   `alembic upgrade head` against it (`alembic upgrade --sql` renders the SQL
   for review without connecting).
3. Pre-checks already run read-only on 2026-10-08: 0 case-insensitive email
   duplicates, 0 duplicate active consent grants.

## 4. Session invalidation trade-off (M3)

Access tokens are stateless JWTs verified locally (JWKS). Logout revokes
Supabase's refresh tokens (`scope=global`), and additionally:

- `deps.token_is_current` refuses any access token older than
  `ACCESS_TOKEN_MINUTES` (default 15), regardless of Supabase's JWT expiry;
- logout records the session id and an "issued before" timestamp in the
  process cache, so the logged-out token and every older token of that user
  are refused immediately.

No database lookup per request was added. The cost of that choice: the
revocation markers live in the in-process cache, so with several workers (or
after a restart) a copied token can survive up to `ACCESS_TOKEN_MINUTES`.
That bound is the same as before the markers existed; a shared Redis cache
removes it (not introduced — the project has no Redis yet).

## 5. Known limitations / follow-ups

- **Rate limiting is per process** (`cache.InMemoryCache`) and keyed on the
  TCP peer address. Behind a reverse proxy, start uvicorn with
  `--proxy-headers --forwarded-allow-ips=<proxy ip>` or every user shares one
  bucket. Lockout does not depend on it (it is in the database).
- **No email verification.** An address can be registered by someone who
  does not own it (squatting); the real owner then gets "already exists".
  Resolving that needs an administrator, or a future verification /
  password-reset flow. Google sign-in is refused for password accounts
  because of this (§2, H1).
- **No password-reset flow yet.** Out of scope by request.
- **GoTrue's own timing** (bcrypt runs only for existing users) can still
  leak account existence to a very precise attacker; the decoy evens out the
  network round trip only.
- **Supabase's per-IP auth limits** see the app server's IP for every user.
  The decoy sign-ins add to that count.

## 6. Running the Postgres-level tests

`tests/test_pg_authority_guards.py` runs the migrations' SQL against a real
Postgres with a Supabase-shaped stub. It is skipped unless
`CURANODE_PG_TEST_URL` names a **local** superuser connection (anything not
on localhost is refused):

```bash
# any local Postgres 13+; e.g. the Supabase CLI stack or a throwaway server
CURANODE_PG_TEST_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:54322/postgres \
  uv run pytest tests/test_pg_authority_guards.py -v
```

Each test creates and drops its own database.
