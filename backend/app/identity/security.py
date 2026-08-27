"""Supabase Auth integration and the medical passport generator.

Password hashing, JWT signing, and refresh-token rotation are delegated
entirely to Supabase Auth — this module no longer performs any of that
itself. Two things remain here:

1. `decode_supabase_access_token` verifies the access token Supabase issued
   against the project's public JWKS (asymmetric ES256 signing keys — this
   project has no shared HS256 secret). Verification is local once the
   signing key is cached; only the first request for a given `kid` pays a
   network round trip. It only yields `user_id` and expiry; role/verification
   are never trusted from the token and are read fresh from
   `profiles`/`doctors` on every request in `deps.py` (SPEC AC-08, BL-03).
2. `get_supabase_client` returns a service-role client used server-side only
   (never exposed to templates/JS) to call Supabase's Auth API — for
   `auth.admin.*` calls ONLY. `new_auth_client` builds a fresh, throwaway
   client for `sign_in_with_password`/`refresh_session`: those calls mutate
   the SDK client's internal session state (and start a background
   auto-refresh timer), so sharing one client instance across end-user
   sign-ins — or between a sign-in and a later admin call — would leak one
   caller's session into another's request.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from dataclasses import dataclass

import jwt
from supabase import AsyncClient, create_async_client

from ..settings import settings

CLOCK_SKEW_S = 30


class InvalidToken(Exception):
    pass


@dataclass(frozen=True)
class AccessClaims:
    user_id: uuid.UUID


@dataclass(frozen=True)
class TokenPair:
    """The session Supabase returned, in the shape the cookie layer expects."""

    access_token: str
    refresh_token: str


_jwks_client: jwt.PyJWKClient | None = None


def _get_jwks_client() -> jwt.PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(
            f"{settings.supabase_url}/auth/v1/.well-known/jwks.json",
            cache_keys=True,
        )
    return _jwks_client


async def decode_supabase_access_token(token: str) -> AccessClaims:
    try:
        # PyJWKClient does a blocking HTTP fetch on a cache miss; keep that
        # off the event loop rather than stalling every concurrent request.
        signing_key = await asyncio.to_thread(_get_jwks_client().get_signing_key_from_jwt, token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256"],
            leeway=CLOCK_SKEW_S,
            audience="authenticated",
        )
        return AccessClaims(user_id=uuid.UUID(payload["sub"]))
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise InvalidToken(str(exc)) from exc


_client: AsyncClient | None = None
_client_lock = asyncio.Lock()


async def get_supabase_client() -> AsyncClient:
    """A process-wide singleton, built lazily on first use (async construction
    can't happen at import time). Use for `auth.admin.*` calls only — never
    call `sign_in_with_password`/`refresh_session` on this instance (see
    `new_auth_client`)."""
    global _client
    if _client is None:
        async with _client_lock:
            if _client is None:
                _client = await create_async_client(
                    settings.supabase_url, settings.supabase_service_role_key
                )
    return _client


async def new_auth_client() -> AsyncClient:
    """A fresh, single-use client for `sign_in_with_password`/`refresh_session`.

    These calls save session state onto the client instance itself (and start
    a background auto-refresh timer) — a shared client would leak one
    caller's session into another's request, or into a later admin call on
    the same instance.
    """
    return await create_async_client(settings.supabase_url, settings.supabase_service_role_key)


# ── Medical passport number (FR1) ────────────────────────────────────────
# Crockford-style alphabet with vowels and ambiguous glyphs (I, L, O, U)
# removed, so a passport number read aloud or copied by hand stays unambiguous.
_PASSPORT_ALPHABET = "0123456789BCDFGHJKMNPQRSTVWXYZ"


def generate_passport_no() -> str:
    def block() -> str:
        return "".join(secrets.choice(_PASSPORT_ALPHABET) for _ in range(4))

    return f"CN-{block()}-{block()}"
