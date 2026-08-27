"""In-process fake for Supabase Auth, so tests never touch the network.

Mirrors just the subset of `supabase.AsyncClient` used by
`app.identity.service`: `auth.admin.create_user`, `auth.admin.sign_out`,
`auth.sign_in_with_password`, `auth.refresh_session`. Access tokens are real
JWTs, but signed with a plain HS256 test secret rather than the project's
real asymmetric (ES256/JWKS) keys — `app.identity.security` verifies against
a live JWKS endpoint in production, which tests must not touch. `conftest.py`
monkeypatches `decode_supabase_access_token` itself (both where it's imported
directly and where it's used as a module attribute) to `fake_decode_token`
below, so the rest of the app's verification *call sites* are still
exercised for real — only the signature-checking mechanism is swapped.

Refresh-token rotation and reuse-family revocation are implemented to match
Supabase's documented behavior: replaying an already-rotated refresh token
invalidates every token ever issued in that session's family, not just the
replayed one.
"""

from __future__ import annotations

import secrets
import time
import uuid
from typing import Any

import jwt
from app.identity.security import AccessClaims, InvalidToken
from supabase_auth.errors import AuthApiError

TEST_JWT_SECRET = "test-only-supabase-jwt-secret-not-the-real-project-key"


async def fake_decode_token(token: str) -> AccessClaims:
    try:
        payload = jwt.decode(token, TEST_JWT_SECRET, algorithms=["HS256"], audience="authenticated")
        return AccessClaims(user_id=uuid.UUID(payload["sub"]))
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
        self._backend.users[email] = {"id": user_id, "password": attrs["password"]}
        return _Obj(user=_Obj(id=user_id))

    async def sign_out(self, jwt_token: str, scope: str = "global") -> None:
        self._backend.revoked_access_tokens.add(jwt_token)


class FakeAuthClient:
    def __init__(self, backend: FakeSupabaseAuth) -> None:
        self._backend = backend
        self.admin = FakeAdminAPI(backend)

    async def sign_in_with_password(self, creds: dict[str, Any]) -> _Obj:
        record = self._backend.users.get(creds["email"])
        if record is None or record["password"] != creds["password"]:
            raise AuthApiError("Invalid login credentials", 400, "invalid_credentials")
        session = self._backend.issue_session(record["id"])
        return _Obj(user=_Obj(id=record["id"]), session=session)

    async def refresh_session(self, refresh_token: str | None = None) -> _Obj:
        backend = self._backend
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
        self.users: dict[str, dict[str, str]] = {}
        self.refresh_tokens: dict[str, tuple[str, str]] = {}
        self.spent_tokens: dict[str, str] = {}
        self.revoked_families: set[str] = set()
        self.revoked_access_tokens: set[str] = set()
        self.auth = FakeAuthClient(self)

    def register(self, user_id: str, email: str, password: str) -> None:
        """Back-door for fixtures that create a `Profile` row directly,
        bypassing the register endpoint."""
        self.users[email] = {"id": user_id, "password": password}

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
            {"sub": user_id, "aud": "authenticated", "iat": now, "exp": expires_at},
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
