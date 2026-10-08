"""In-process fake for Supabase Auth, so tests never touch the network.

Mirrors just the subset of `supabase.AsyncClient` used by
`app.identity.service`: `auth.admin.create_user` / `delete_user` /
`update_user_by_id` / `sign_out`, `auth.sign_in_with_password`,
`auth.refresh_session` and `auth.exchange_code_for_session`. Access tokens are real JWTs, but signed
with a plain HS256 test secret rather than the project's real asymmetric
(ES256/JWKS) keys — `app.identity.security` verifies against a live JWKS
endpoint in production, which tests must not touch. `conftest.py`
monkeypatches `decode_supabase_access_token` itself (both where it's imported
directly and where it's used as a module attribute) to `fake_decode_token`
below, so the rest of the app's verification *call sites* are still
exercised for real — only the signature-checking mechanism is swapped.

Refresh-token rotation and reuse-family revocation are implemented to match
Supabase's documented behavior: replaying an already-rotated refresh token
invalidates every token ever issued in that session's family, not just the
replayed one.

`sign_in_with_password` mirrors GoTrue in answering `email_not_confirmed`
only AFTER the password matched, for users created unconfirmed. `fail_next`
lets a test inject an `AuthApiError` into the next call of a named method.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import jwt
from app.identity.security import AccessClaims, InvalidToken
from supabase_auth.errors import AuthApiError

TEST_JWT_SECRET = "test-only-supabase-jwt-secret-not-the-real-project-key"


async def fake_decode_token(token: str) -> AccessClaims:
    try:
        payload = jwt.decode(token, TEST_JWT_SECRET, algorithms=["HS256"], audience="authenticated")
        return AccessClaims(
            user_id=uuid.UUID(payload["sub"]),
            session_id=payload.get("session_id"),
            issued_at=payload.get("iat"),
        )
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise InvalidToken(str(exc)) from exc


class _Obj:
    """A cheap stand-in for the SDK's pydantic response models — attribute
    access only, nothing else is used by the app."""

    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class FakeAdminAPI:
    def __init__(self, backend: FakeSupabaseAuth) -> None:
        self._backend = backend

    async def create_user(self, attrs: dict[str, Any]) -> _Obj:
        self._backend.calls.append("admin.create_user")
        self._backend.maybe_fail("admin.create_user")
        email = attrs["email"]
        if email in self._backend.users:
            raise AuthApiError(
                "A user with this email address has already been registered",
                422,
                "email_exists",
            )
        user_id = secrets.token_hex(16)
        # Format as a UUID string so `uuid.UUID(...)` in service.py accepts it.
        user_id = f"{user_id[:8]}-{user_id[8:12]}-{user_id[12:16]}-{user_id[16:20]}-{user_id[20:]}"
        self._backend.users[email] = {
            "id": user_id,
            "password": attrs["password"],
            "confirmed": bool(attrs.get("email_confirm")),
            "providers": {"email"},
        }
        return _Obj(user=_Obj(id=user_id))

    async def delete_user(self, id: str, should_soft_delete: bool = False) -> None:
        self._backend.calls.append("admin.delete_user")
        self._backend.maybe_fail("admin.delete_user")
        self._backend.deleted_user_ids.append(id)
        for email, record in list(self._backend.users.items()):
            if record["id"] == id:
                del self._backend.users[email]

    async def update_user_by_id(self, uid: str, attributes: dict[str, Any]) -> _Obj:
        self._backend.calls.append("admin.update_user_by_id")
        for record in self._backend.users.values():
            if record["id"] == uid and "password" in attributes:
                record["password"] = attributes["password"]
        return _Obj(user=_Obj(id=uid))

    async def sign_out(self, jwt_token: str, scope: str = "global") -> None:
        self._backend.calls.append("admin.sign_out")
        self._backend.revoked_access_tokens.add(jwt_token)

    def __getattr__(self, name: str) -> Any:
        # Any admin method the fake does not model is still recorded, so a
        # test can assert it was never reached.
        async def _unmodelled(*_a: Any, **_kw: Any) -> None:
            self._backend.calls.append(f"admin.{name}")

        return _unmodelled


class FakeAuthClient:
    def __init__(self, backend: FakeSupabaseAuth) -> None:
        self._backend = backend
        self.admin = FakeAdminAPI(backend)

    def __getattr__(self, name: str) -> Any:
        # Unmodelled user-level calls (e.g. `update_user`) are recorded too.
        async def _unmodelled(*_a: Any, **_kw: Any) -> None:
            self._backend.calls.append(name)

        return _unmodelled

    async def sign_in_with_password(self, creds: dict[str, Any]) -> _Obj:
        self._backend.calls.append("sign_in_with_password")
        self._backend.sign_in_emails.append(creds["email"])
        record = self._backend.users.get(creds["email"])
        if record is None or record["password"] != creds["password"]:
            raise AuthApiError("Invalid login credentials", 400, "invalid_credentials")
        # GoTrue checks the password first, then confirmation.
        if not record["confirmed"]:
            raise AuthApiError("Email not confirmed", 400, "email_not_confirmed")
        session = self._backend.issue_session(record["id"])
        return _Obj(user=self._backend.user_obj(creds["email"]), session=session)

    async def exchange_code_for_session(self, params: dict[str, Any]) -> _Obj:
        """Mirrors the real SDK's PKCE exchange: validates the verifier
        against the challenge the code was minted with, single-use."""
        backend = self._backend
        code = params.get("auth_code")
        verifier = params.get("code_verifier") or ""
        entry = backend.oauth_codes.pop(code, None)
        if entry is None:
            raise AuthApiError("Invalid Auth Code", 400, "bad_oauth_code")
        challenge, user_id, email, user_metadata = entry
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        if computed != challenge:
            raise AuthApiError("Invalid Verifier", 400, "bad_code_verifier")
        # GoTrue's automatic identity linking: an OAuth identity minted for
        # an existing auth user (same id) is linked onto it, and the
        # provider-verified email marks the user confirmed.
        record = backend.users.get(email)
        if record is not None and record["id"] == user_id:
            record["providers"].add("google")
            record["confirmed"] = True
        session = backend.issue_session(user_id)
        providers = (
            sorted(record["providers"]) if record and record["id"] == user_id else ["google"]
        )
        return _Obj(
            user=_Obj(
                id=user_id,
                email=email,
                user_metadata=user_metadata,
                email_confirmed_at=datetime.now(UTC),
                identities=[_Obj(provider=p) for p in providers],
            ),
            session=session,
        )

    async def refresh_session(self, refresh_token: str | None = None) -> _Obj:
        backend = self._backend
        backend.calls.append("refresh_session")
        if not refresh_token:
            raise AuthApiError("Invalid Refresh Token", 400, "refresh_token_not_found")

        entry = backend.refresh_tokens.get(refresh_token)
        if entry is None:
            # A token we recognise as already-spent being presented again is
            # theft: kill every token in that family, mirroring Supabase.
            family_id = backend.spent_tokens.get(refresh_token)
            if family_id is not None:
                backend.revoke_family(family_id)
            raise AuthApiError("Invalid Refresh Token", 400, "refresh_token_not_found")

        user_id, family_id = entry
        del backend.refresh_tokens[refresh_token]
        backend.spent_tokens[refresh_token] = family_id
        session = backend.issue_session(user_id, family_id=family_id)
        return _Obj(user=_Obj(id=user_id), session=session)


class FakeSupabaseAuth:
    """Stand-in for `supabase.AsyncClient` — only `.auth` is implemented."""

    def __init__(self, jwt_secret: str = TEST_JWT_SECRET) -> None:
        self._jwt_secret = jwt_secret
        # email -> {"id", "password", "confirmed", "providers"}
        self.users: dict[str, dict[str, Any]] = {}
        self.refresh_tokens: dict[str, tuple[str, str]] = {}
        self.spent_tokens: dict[str, str] = {}
        self.revoked_families: set[str] = set()
        self.revoked_access_tokens: set[str] = set()
        # code -> (challenge, user_id, email, user_metadata)
        self.oauth_codes: dict[str, tuple[str, str, str, dict[str, Any]]] = {}
        # email -> user_id, for OAuth identities `authorize()` has minted
        # before — a *returning* OAuth user must get the same identity back.
        self.oauth_identities: dict[str, str] = {}
        self.deleted_user_ids: list[str] = []
        # Every address a password sign-in was attempted for, in order.
        self.sign_in_emails: list[str] = []
        # method name -> error to raise on its next call (see `fail_next`).
        self._failures: dict[str, AuthApiError] = {}
        # Every Auth API call the app makes, in order — lets a test assert a
        # feature never touches Supabase Auth at all (patient profile AC-07).
        self.calls: list[str] = []
        self.auth = FakeAuthClient(self)

    def register(self, user_id: str, email: str, password: str, *, confirmed: bool = True) -> None:
        """Back-door for fixtures that create a `Profile` row directly,
        bypassing the register endpoint."""
        self.users[email] = {
            "id": user_id,
            "password": password,
            "confirmed": confirmed,
            "providers": {"email"},
        }

    def fail_next(self, method: str, error: AuthApiError) -> None:
        self._failures[method] = error

    def maybe_fail(self, method: str) -> None:
        error = self._failures.pop(method, None)
        if error is not None:
            raise error

    def user_obj(self, email: str) -> _Obj:
        record = self.users[email]
        return _Obj(
            id=record["id"],
            email=email,
            email_confirmed_at=datetime.now(UTC) if record["confirmed"] else None,
            identities=[_Obj(provider=p) for p in sorted(record["providers"])],
        )

    def authorize(
        self,
        provider: str,
        email: str,
        *,
        challenge: str,
        user_metadata: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> str:
        """Test-side stand-in for the browser's round trip through Google:
        mints an auth code bound to the PKCE challenge the start route sent.

        Repeat calls for the same email (no explicit `user_id`) return the
        same identity, matching a returning OAuth user. `user_id` lets a test
        force a specific Supabase auth identity: a *different* id is the
        email-collision case (docs/oauth.md §8b step 2), while the id of an
        existing password account simulates GoTrue's automatic linking.
        """
        if user_id is None:
            user_id = self.oauth_identities.get(email) or str(uuid.uuid4())
        self.oauth_identities.setdefault(email, user_id)
        code = secrets.token_urlsafe(24)
        self.oauth_codes[code] = (challenge, user_id, email, user_metadata or {})
        return code

    def revoke_family(self, family_id: str) -> None:
        self.revoked_families.add(family_id)
        dead = [t for t, (_, f) in self.refresh_tokens.items() if f == family_id]
        for t in dead:
            del self.refresh_tokens[t]

    def issue_session(self, user_id: str, family_id: str | None = None) -> _Obj:
        family_id = family_id or secrets.token_urlsafe(12)
        now = int(time.time())
        expires_at = now + 900
        access_token = jwt.encode(
            {
                "sub": user_id,
                "aud": "authenticated",
                "iat": now,
                "exp": expires_at,
                # Supabase keeps one session_id across refreshes of a login.
                "session_id": family_id,
                # Unique per token, as Supabase's are; without it two tokens
                # minted in the same second would be byte-identical.
                "jti": secrets.token_hex(8),
            },
            self._jwt_secret,
            algorithm="HS256",
        )
        refresh_token = secrets.token_urlsafe(24)
        self.refresh_tokens[refresh_token] = (user_id, family_id)
        return _Obj(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=900,
            expires_at=expires_at,
        )
