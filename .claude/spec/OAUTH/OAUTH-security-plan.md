# OAuth Security Remediation Plan

**Feature:** Fix for the email-normalization inconsistency identified in
`security-review.md` (Finding 1, MEDIUM)
**Status:** Planned — no code written yet
**Scope of this document:** a plan only. It cites real files and line ranges
in this repository; read the cited code before implementing. Follow the same
implementation-plan conventions as `oauth.md`.

---

## 1. Problem Summary

Password registration/login normalize email addresses (`.strip().lower()`,
via `_normalise_email` in `backend/app/identity/schemas.py:27-32`) before
every DB write and comparison. The OAuth path does not: `oauth.profile_fields`
(`backend/app/identity/oauth.py:88-95`) reads `user.email` straight from the
Supabase/Google identity object, and `service.login_with_oauth`
(`backend/app/identity/service.py`) uses that unnormalized value both to look
up a possible existing account (`_user_by_email(session, fields["email"])`,
line 366) and to persist it (`user.email = fields["email"]`, line 399).

Because `Profile.email` is a plain `String(320)` column
(`backend/app/db/models.py:111`) with no case-insensitive type, and
`_user_by_email` does an exact `Profile.email == email` match
(`service.py:66-68`), a provider-supplied email that differs only in case
from an existing password account's (normalized, lower-cased) email will not
be found by the collision check. This silently defeats the guard at
`service.py:360-369` that exists specifically to stop an unverified OAuth
identity from taking over an existing password account.

**Goal of this plan:** make email comparison and storage case-insensitive
end-to-end for both auth paths, so the existing collision guard cannot be
bypassed by casing alone — without weakening any other invariant documented
in `CLAUDE.md` (trigger-update pattern, additive-only migrations, no ad-hoc
role checks, etc.).

---

## 2. Constraints to Respect

Same ground rules as `oauth.md` §2, plus:

1. **`user_profile` is a shared table this repo does not own.** Any schema
   change must be additive-only and hand-written (no
   `alembic revision --autogenerate`), matching the pattern in
   `alembic/versions/6feacefae2d0_*.py`. CLAUDE.md explicitly notes CITEXT
   was *deliberately avoided* in favor of app-level normalization for this
   schema — this plan should not silently reverse that decision without
   calling it out as a distinct, optional step (§5).
2. **Registration must not become an email-enumeration oracle.** The fix
   must not change the shape of `register()`'s duplicate-email response
   (`service.py:185-200`).
3. **No behavior change to the password flow's existing normalization** —
   it is already correct; this plan only needs to bring the OAuth path in
   line with it, not redesign either.
4. **Audit logging (`audit/writer.py`) must never log raw email** — any new
   log lines or `detail={}` payloads this fix adds must stay inside the
   existing redaction rules in `log_config.py` (`email` is already a
   `REDACTED_KEYS` entry — do not defeat that by, e.g., logging email inside
   an unlisted nested key).
5. **Migrations are additive-only and hand-written**, per CLAUDE.md — the
   optional citext step (§5) is a schema change and must follow that rule if
   pursued.

---

## 3. Root Cause, Precisely

Two separate gaps, both need closing:

- **Gap A — read-side (lookup):** `_user_by_email` in `service.py:66-68`
  compares `Profile.email == email` where `email` is whatever the caller
  passed in. Callers on the password side always pass an already-normalized
  value (Pydantic validator ran first). The OAuth caller
  (`login_with_oauth`, line 366) does not normalize before calling it.
- **Gap B — write-side (persistence):** `login_with_oauth` writes
  `user.email = fields["email"]` (line 399) and the defensive fallback
  `Profile(... email=fields["email"] ...)` (lines 375-380) using the raw
  provider value, so even a single successful OAuth login can leave a
  non-normalized email sitting in `Profile.email`, which then poisons *future*
  lookups (including ones from the password path, e.g. a later
  `register()` duplicate check) even after Gap A is fixed for the OAuth
  call site itself.

Both must be fixed together — fixing only Gap A still leaves inconsistent
data in the table from before the fix (see §6 backfill note) and leaves new
OAuth rows non-normalized if Gap B isn't also closed.

---

## 4. Proposed Fix (application-level, no schema change required)

### 4.1 Normalize at the source: `oauth.py`

Move (or reuse) the existing normalization helper so it's shared rather than
password-schema-only, and apply it inside `profile_fields`:

- Relocate `_normalise_email` out of `identity/schemas.py` into a small
  shared location both `schemas.py` and `oauth.py` can import without a
  circular-import problem — e.g. a new `identity/_email.py` (or add it to
  `identity/security.py`, which `oauth.py` already imports as `security`).
  Naming and placement to be decided at implementation time; the constraint
  is: **one function, one definition, imported everywhere an email is
  normalized**, not two copies that can drift again.
- `oauth.profile_fields` (`oauth.py:88-95`) applies that helper to
  `user.email` before returning it in the `fields` dict, so `fields["email"]`
  is always `.strip().lower()`ed by the time `service.py` ever sees it —
  same guarantee the password path already gets from Pydantic.

This closes Gap A and Gap B in one place, since every downstream use in
`service.login_with_oauth` (`_user_by_email` lookup at line 366, the
fallback `Profile(...)` construction at lines 375-380, and the
`user.email = fields["email"]` write at line 399) already flows through
`profile_fields`'s return value — no changes needed at those call sites
themselves once the source is normalized.

### 4.2 Defense in depth: normalize at the comparison, not just the input

Even with 4.1, keep `_user_by_email` (`service.py:66-68`) robust against any
*future* caller that forgets to pre-normalize (the same category of bug that
caused this finding). Change the query to compare on a normalized form on
both sides, e.g. `func.lower(Profile.email) == email.strip().lower()`
(or normalize `email` before the call and assert callers do so — see §4.3
for why the DB-side comparison is the more durable choice).

This is the more defensive of the two options because it doesn't rely on
every call site remembering to normalize before calling — it makes the
*lookup itself* case-insensitive regardless of caller discipline.

### 4.3 Decide: normalize-before-call convention, or comparison-is-inherently-case-insensitive convention

Pick one as the project's actual invariant (both closes this bug, but they
have different failure modes for the *next* bug):

- **Option A (normalize-at-call-site):** every caller of `_user_by_email`
  and every writer of `Profile.email` must pass/store an already-normalized
  value; `_user_by_email` trusts its input. Cheaper, but re-introduces this
  exact bug class if a future call site (a third auth provider, an admin
  tool, a data-fix script) forgets to normalize first.
- **Option B (case-insensitive-by-construction):** `_user_by_email` always
  lower-cases both sides of the comparison itself (§4.2), independent of
  whether the caller already normalized. A future forgetful caller degrades
  to "works correctly by accident" instead of "silently reintroduces the
  collision-guard bypass."

**Recommendation: implement both 4.1 and 4.2.** 4.1 keeps stored data clean
(so `Profile.email` displayed anywhere, e.g. `MeOut.email` in
`schemas.py:128`, is never surprisingly mixed-case), and 4.2 makes the
security-critical lookup itself resilient to future regressions — cheap
insurance given it's a one-line `WHERE` clause change.

---

## 5. Optional, Larger Follow-up (schema-level, separate PR)

Not required to close this finding, but worth recording since CLAUDE.md
flags CITEXT as a considered-and-rejected option for this exact reason:

- Revisit whether `user_profile.email` should become `citext` at the
  database level. This is a **schema change to a table this repo does not
  own** (`user_profile` belongs to the wider CuraNode-AI product per
  CLAUDE.md §"Database schema is shared, not owned") — any such change must
  be proposed to, and applied by, whoever owns that schema, not unilaterally
  via this repo's additive-only Alembic migrations. Out of scope for this
  plan; call out as a follow-up conversation with the schema owners, not an
  action item here.

---

## 6. Data Backfill Consideration

If any OAuth users have already signed in before this fix ships, their
`Profile.email` may already contain non-normalized (mixed-case) values from
the pre-fix code path.

- **Investigate before implementing:** query production/pilot data (once
  this plan moves to implementation) for any `user_profile.email` row where
  `email != lower(trim(email))`.
- If any exist, a one-off, hand-run data-correction (not a migration — this
  is a data fix, not a schema change) should normalize them, guarded by a
  check that the lower-cased form doesn't already collide with a different
  existing row (if it does, that pre-existing pair itself represents the
  exact account-confusion this fix prevents going forward, and needs manual
  review, not an automated merge).
- This step should NOT block shipping the code fix — it closes the
  forward-looking hole regardless of legacy data. Treat backfill as a
  fast-follow, not a blocker, and track it separately from the tests below.

---

## 7. Test Plan (to write alongside the fix, still no code yet — listed here for scope)

Add to `tests/test_oauth.py` (pattern-matching its existing `t*` naming, see
`test_t10_email_collision_with_different_identity_is_refused` for the
closest existing analog):

1. **New test — casing bypass is closed:** an existing password account with
   normalized email `victim@example.com`; a fake OAuth identity returns
   `user.email == "Victim@Example.com"` (or `"VICTIM@EXAMPLE.COM"`) for a
   *different* Supabase user id. Assert `login_with_oauth` still refuses
   (`Unauthenticated`) exactly as `test_t10` already asserts for the
   exact-case collision — this is the regression test for Gap A.
2. **New test — persisted email is normalized:** a brand-new OAuth user
   whose provider identity returns a mixed-case email. After
   `login_with_oauth` completes, assert the persisted `Profile.email` is
   lower-cased — regression test for Gap B.
3. **New test (optional, cheap) — `_user_by_email` is case-insensitive
   directly:** unit-level test on `service._user_by_email` with a stored
   lower-case row and a mixed-case query string, asserting a match — direct
   test of §4.2's defense-in-depth change, independent of the OAuth flow.
4. Re-run `test_t10_email_collision_with_different_identity_is_refused` and
   `test_provider_cancellation_is_a_generic_failure` /
   `test_bad_verifier_is_rejected` unmodified to confirm no regression to
   the existing collision/failure-shape guarantees.
5. Run `tests/test_web.py`'s onboarding/register coverage unmodified to
   confirm the password path's behavior and response shapes are unaffected
   (per constraint §2.3).

---

## 8. Rollout / Risk Notes

- This is a pure application-logic change (no migration required for the
  recommended fix in §4) — low deployment risk, no downtime consideration.
- No change to cookie behavior, session issuance, PKCE, or the CSRF state
  handling — those remain exactly as reviewed and are not touched by this
  plan.
- No change to `errors.py` failure shapes — `Unauthenticated` continues to
  be raised the same way for a real collision; this fix only widens *which*
  cases are correctly detected as a collision, it does not add a new
  error path or change any response shape.

---

## 9. Out of Scope

- Any other finding, ruled-out item, or general hardening note from
  `security-review.md` — this plan addresses Finding 1 only, per the
  instruction that produced it.
- The optional citext schema change (§5) — tracked as a follow-up, not part
  of this plan's implementation.
