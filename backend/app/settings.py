"""Application settings.

Fail-fast startup guards live here (SPEC BL-16, BL-17): the application refuses
to boot with a missing or short JWT secret rather than running insecurely.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["dev", "test", "pilot"] = "dev"

    # ── Development server ───────────────────────────────────────────────
    host: str = "127.0.0.1"
    port: int = 8000
    reload: bool = True

    # ── Persistence ──────────────────────────────────────────────────────
    # Supabase Postgres (session pooler or direct connection). Tests override
    # this with an in-memory SQLite URL — the model has no Postgres-only types.
    database_url: str = "postgresql+asyncpg://user:pass@localhost:5432/postgres"

    # ── Supabase Auth (identity is fully delegated to Supabase) ─────────
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    # Access tokens are verified against this project's public JWKS
    # (asymmetric ES256 signing keys) — no shared secret to configure.

    access_token_minutes: int = 15
    refresh_token_days: int = 14

    # Cookies are HttpOnly + SameSite=Strict always. `Secure` must be true in
    # pilot; it is relaxed for local http development only (SPEC AC-20).
    cookie_secure: bool = True
    cookie_samesite: Literal["strict", "lax", "none"] = "strict"
    access_cookie_name: str = "cn_access"
    refresh_cookie_name: str = "cn_refresh"

    # ── Account security (TDD 7.1) ───────────────────────────────────────
    max_failed_logins: int = 10
    lockout_minutes: int = 15
    auth_rate_limit_per_minute: int = 5

    # ── i18n (FR28) ──────────────────────────────────────────────────────
    default_locale: Literal["en", "ur"] = "en"

    # ── OAuth (Google sign-in via Supabase Auth) ─────────────────────────
    # Anon key is required for the PKCE token exchange (`apikey` header) —
    # the service-role key is deliberately never used for a user-facing call.
    supabase_anon_key: str = ""
    public_base_url: str = "http://127.0.0.1:8000"
    oauth_enabled: bool = False
    oauth_providers: str = "google"
    oauth_state_ttl_s: int = 600

    def model_post_init(self, _context: object) -> None:
        # BL-16 — failing loudly at boot beats running insecurely or against
        # a misconfigured Supabase project.
        if self.environment != "test" and (
            not self.supabase_url or not self.supabase_service_role_key
        ):
            raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")
        if self.environment == "pilot" and not self.cookie_secure:
            raise ValueError("COOKIE_SECURE must be true in pilot")
        if self.oauth_enabled and not self.supabase_anon_key:
            raise ValueError("SUPABASE_ANON_KEY must be set when OAUTH_ENABLED is true")
        if self.environment == "pilot" and not self.public_base_url.startswith("https://"):
            raise ValueError("PUBLIC_BASE_URL must be https:// in pilot")

    @property
    def oauth_provider_set(self) -> frozenset[str]:
        return frozenset(p.strip().lower() for p in self.oauth_providers.split(",") if p.strip())

    @property
    def access_cookie_max_age(self) -> int:
        return self.access_token_minutes * 60

    @property
    def refresh_cookie_max_age(self) -> int:
        return self.refresh_token_days * 24 * 3600


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
