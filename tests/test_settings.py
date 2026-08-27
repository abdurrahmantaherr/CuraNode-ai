"""Unit tests for the fail-fast startup guards in `app.settings` (SPEC BL-16).

These construct `Settings` directly rather than through the module-level
singleton, so they don't need the `test` environment override that
`conftest.py` sets for the rest of the suite.
"""

from __future__ import annotations

import pytest
from app.settings import Settings
from pydantic import ValidationError


def _base(**overrides: object) -> dict[str, object]:
    base = {
        "environment": "dev",
        "supabase_url": "https://example.supabase.co",
        "supabase_service_role_key": "service-role-key",
    }
    base.update(overrides)
    return base


def test_oauth_enabled_requires_anon_key() -> None:
    with pytest.raises(ValidationError, match="SUPABASE_ANON_KEY"):
        Settings(**_base(oauth_enabled=True, supabase_anon_key=""))


def test_oauth_enabled_with_anon_key_is_fine() -> None:
    s = Settings(**_base(oauth_enabled=True, supabase_anon_key="anon-key"))
    assert s.oauth_enabled is True


def test_pilot_requires_https_public_base_url() -> None:
    with pytest.raises(ValidationError, match="PUBLIC_BASE_URL"):
        Settings(
            **_base(
                environment="pilot",
                cookie_secure=True,
                public_base_url="http://example.com",
            )
        )


def test_pilot_with_https_public_base_url_is_fine() -> None:
    s = Settings(
        **_base(
            environment="pilot",
            cookie_secure=True,
            public_base_url="https://example.com",
        )
    )
    assert s.public_base_url == "https://example.com"


def test_oauth_provider_set_parses_csv() -> None:
    s = Settings(**_base(oauth_providers="google, google,  "))
    assert s.oauth_provider_set == frozenset({"google"})
