"""Google sign-in via Supabase Auth's server-side PKCE flow (docs/oauth.md).

Only the mechanics that are specific to OAuth live here — session cookies,
role provisioning, and the trigger-update pattern stay in `service.py` and
`web/router.py`, exactly as they do for the password flow.

`exchange_code` MUST run on a `security.new_auth_client()` instance, never on
`security.get_supabase_client()`: the code exchange saves session state onto
the SDK client (same hazard `security.py`'s module docstring describes for
`sign_in_with_password`), so a shared client would leak one caller's session
into another's request. The installed `supabase-auth` SDK's
`exchange_code_for_session` accepts a caller-supplied `code_verifier`
directly in its params (verified against the installed version — it does not
fall back to the client's own token storage), so no raw HTTP fallback is
needed.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from ..settings import settings
from . import security

PROVIDERS: frozenset[str] = frozenset({"google"})


def is_enabled(provider: str) -> bool:
    return (
        settings.oauth_enabled
        and provider in settings.oauth_provider_set
        and (provider in PROVIDERS)
    )


@dataclass(frozen=True)
class PendingAuth:
    """What a pending authorization needs to remember between the redirect to
    Google and the callback — carried in the cache, never in the URL."""

    verifier: str
    locale: str
    next_path: str | None
    created_at: datetime


def make_pkce_pair() -> tuple[str, str]:
    """Returns (verifier, challenge). Challenge is the base64url-unpadded
    SHA-256 digest of the verifier, per RFC 7636."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorize_url(provider: str, *, challenge: str, redirect_to: str) -> str:
    """No `state` param here — matches the real SDK's own `/authorize` call
    exactly (see `AsyncGoTrueClient._get_url_for_provider`). GoTrue manages
    its own internal state for the round trip to the provider; a caller
    -supplied `state` query param on this endpoint is NOT a pass-through slot
    — it collides with GoTrue's own state validation and comes back as
    `bad_oauth_state`. Our own CSRF-binding state instead rides inside
    `redirect_to`'s own query string, which Supabase preserves verbatim and
    just appends `?code=...` to.
    """
    params = {
        "provider": provider,
        "redirect_to": redirect_to,
        "code_challenge": challenge,
        "code_challenge_method": "s256",
    }
    return f"{settings.supabase_url}/auth/v1/authorize?{urlencode(params)}"


async def exchange_code(code: str, verifier: str) -> Any:
    client = await security.new_auth_client()
    return await client.auth.exchange_code_for_session(
        {"auth_code": code, "code_verifier": verifier}
    )


def profile_fields(user: Any) -> dict[str, str]:
    """Pulls what this app needs from the Supabase auth user Google handed
    back. `full_name` falls back through the metadata keys Google's provider
    is documented to populate, then to the email's local part."""
    email = user.email
    metadata = getattr(user, "user_metadata", None) or {}
    full_name = metadata.get("full_name") or metadata.get("name") or email.split("@")[0]
    return {"email": email, "full_name": full_name}


def new_state() -> str:
    return secrets.token_urlsafe(32)


def utcnow() -> datetime:
    return datetime.now(UTC)
