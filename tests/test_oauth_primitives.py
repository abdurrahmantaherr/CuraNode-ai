"""Unit tests for `app.identity.oauth`'s pure helpers (docs/oauth.md §5)."""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

from app.identity import oauth


def test_make_pkce_pair_challenge_is_unpadded_b64url_sha256_of_verifier() -> None:
    verifier, challenge = oauth.make_pkce_pair()
    assert "=" not in challenge
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
    assert challenge == expected.rstrip(b"=").decode("ascii")


def test_make_pkce_pair_is_random_each_call() -> None:
    v1, c1 = oauth.make_pkce_pair()
    v2, c2 = oauth.make_pkce_pair()
    assert v1 != v2
    assert c1 != c2


def test_authorize_url_carries_all_pkce_params(monkeypatch) -> None:
    monkeypatch.setattr(oauth.settings, "supabase_url", "https://proj.supabase.co")
    url = oauth.authorize_url(
        "google",
        challenge="the-challenge",
        redirect_to="https://app.example.com/auth/callback",
    )
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "proj.supabase.co"
    assert parsed.path == "/auth/v1/authorize"
    qs = parse_qs(parsed.query)
    assert qs["provider"] == ["google"]
    assert qs["code_challenge"] == ["the-challenge"]
    assert qs["code_challenge_method"] == ["s256"]
    assert qs["redirect_to"] == ["https://app.example.com/auth/callback"]


def test_authorize_url_has_no_top_level_state_param(monkeypatch) -> None:
    """Regression guard: a caller-supplied `state` on Supabase's own
    `/authorize` call collides with GoTrue's internal state handling and
    comes back as `bad_oauth_state` (confirmed against a live project) — our
    state must ride inside `redirect_to` instead, never as a sibling param."""
    monkeypatch.setattr(oauth.settings, "supabase_url", "https://proj.supabase.co")
    url = oauth.authorize_url(
        "google", challenge="c", redirect_to="https://app.example.com/auth/callback?state=xyz"
    )
    qs = parse_qs(urlparse(url).query)
    assert "state" not in qs


def test_is_enabled_false_when_master_switch_off(monkeypatch) -> None:
    monkeypatch.setattr(oauth.settings, "oauth_enabled", False)
    monkeypatch.setattr(oauth.settings, "oauth_providers", "google")
    assert oauth.is_enabled("google") is False


def test_is_enabled_false_for_unknown_provider(monkeypatch) -> None:
    monkeypatch.setattr(oauth.settings, "oauth_enabled", True)
    monkeypatch.setattr(oauth.settings, "oauth_providers", "google")
    assert oauth.is_enabled("facebook") is False


def test_is_enabled_true_when_switch_on_and_provider_allow_listed(monkeypatch) -> None:
    monkeypatch.setattr(oauth.settings, "oauth_enabled", True)
    monkeypatch.setattr(oauth.settings, "oauth_providers", "google")
    assert oauth.is_enabled("google") is True


class _FakeUser:
    def __init__(self, email: str, user_metadata: dict) -> None:
        self.email = email
        self.user_metadata = user_metadata


def test_profile_fields_prefers_full_name_then_name_then_email_local_part() -> None:
    full = _FakeUser("person@example.com", {"full_name": "Ayesha Khan"})
    assert oauth.profile_fields(full)["full_name"] == "Ayesha Khan"

    name_only = _FakeUser("person@example.com", {"name": "Ayesha"})
    assert oauth.profile_fields(name_only)["full_name"] == "Ayesha"

    neither = _FakeUser("person@example.com", {})
    assert oauth.profile_fields(neither)["full_name"] == "person"
