"""Clinic invite-code generation and hashing.

Plain-text codes are never stored (`clinic_invite_code.code_hash` only) — the
same reasoning as a password, except this app has no password hasher of its
own (Supabase owns those). PBKDF2-HMAC-SHA256 with a random salt is a small,
dependency-free, adequate choice for a code an admin types once at sign-up.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I — easy to read aloud
_ITERATIONS = 200_000


def generate_code() -> str:
    """A 16-character code in 4 dash-separated groups, e.g. `8K2N-QX7P-4VWR-9ZTC`."""
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(16))
    return "-".join(raw[i : i + 4] for i in range(0, 16, 4))


def hash_code(code: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", code.encode("utf-8"), bytes.fromhex(salt), _ITERATIONS)
    return f"{salt}${digest.hex()}"


def verify_code(code: str, stored: str) -> bool:
    if not code or "$" not in stored:
        return False
    salt, _, expected_hex = stored.partition("$")
    try:
        salt_bytes = bytes.fromhex(salt)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", code.encode("utf-8"), salt_bytes, _ITERATIONS)
    return hmac.compare_digest(digest.hex(), expected_hex)
