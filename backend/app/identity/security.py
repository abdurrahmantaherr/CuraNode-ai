"""Password hashing, access tokens, and refresh tokens (TDD 7.1).

Three properties here are load-bearing and must not be "optimised":

1. `dummy_verify` burns the same Argon2 cost on an unknown account, so login
   timing cannot reveal whether an email is registered (SPEC BL-04).
2. The access token carries NO `is_verified` claim. Verification is read from
   the database per request, so an admin revoking it takes effect on the next
   call rather than in fifteen minutes (SPEC AC-08, BL-03).
3. Refresh tokens are stored only as SHA-256 digests, so a dump of the cache
   cannot be replayed against the API.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from ..settings import settings

# TDD 7.1 — exact parameters. Changing these is a security decision.
_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)

# A real hash of a throwaway value, used purely to equalise timing.
_DUMMY_HASH = _hasher.hash("dummy-password-for-constant-time-guard")

CLOCK_SKEW_S = 30


class InvalidToken(Exception):
    pass


@dataclass(frozen=True)
class AccessClaims:
    user_id: uuid.UUID
    role: str
    locale: str
    jti: str


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_hash: str
    family_id: str


def hash_password(raw: str) -> str:
    return _hasher.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    try:
        _hasher.verify(hashed, raw)
        return True
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def dummy_verify() -> None:
    """Spend the same time as a real verify when the account does not exist.

    The mismatch is guaranteed and intentional — the CPU cost is the entire
    point, so the exception is swallowed deliberately.
    """
    with suppress(VerifyMismatchError, VerificationError, InvalidHashError):
        _hasher.verify(_DUMMY_HASH, "definitely-not-the-password")


def issue_access_token(
    user_id: uuid.UUID, role: str, locale: str
) -> tuple[str, datetime]:
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=settings.access_token_minutes)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": role,
        "locale": locale,
        "jti": secrets.token_urlsafe(12),
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires


def decode_access_token(token: str) -> AccessClaims:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            leeway=CLOCK_SKEW_S,
        )
        return AccessClaims(
            user_id=uuid.UUID(payload["sub"]),
            role=payload["role"],
            locale=payload.get("locale", "en"),
            jti=payload.get("jti", ""),
        )
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise InvalidToken(str(exc)) from exc


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def new_refresh_token(family_id: str | None = None) -> tuple[str, str, str]:
    """Return (raw, sha256_hash, family_id). 32 bytes of CSPRNG entropy."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_refresh_token(raw), family_id or secrets.token_urlsafe(16)


# ── Medical passport number (FR1) ────────────────────────────────────────
# Crockford-style alphabet with vowels and ambiguous glyphs (I, L, O, U)
# removed, so a passport number read aloud or copied by hand stays unambiguous.
_PASSPORT_ALPHABET = "0123456789BCDFGHJKMNPQRSTVWXYZ"


def generate_passport_no() -> str:
    def block() -> str:
        return "".join(secrets.choice(_PASSPORT_ALPHABET) for _ in range(4))

    return f"CN-{block()}-{block()}"
