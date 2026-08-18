"""Application settings.

Fail-fast startup guards live here (SPEC BL-16, BL-17): the application refuses
to boot with a missing or short JWT secret rather than running insecurely.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Local development default. Never used when ENVIRONMENT == "pilot" — the
# validator below rejects it there.
_DEV_SECRET = "dev-only-insecure-secret-change-me-32b"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["dev", "test", "pilot"] = "dev"

    # ── Development server ───────────────────────────────────────────────
    host: str = "127.0.0.1"
    port: int = 8000
    reload: bool = True

    # ── Persistence ──────────────────────────────────────────────────────
    # SQLite locally; swap for a postgresql+psycopg URL without code changes.
    database_url: str = "sqlite+aiosqlite:///./curanode.db"

    # ── Session / tokens (TDD 7.1) ───────────────────────────────────────
    jwt_secret: str = _DEV_SECRET
    jwt_algorithm: str = "HS256"
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

    @field_validator("jwt_secret")
    @classmethod
    def _secret_must_be_strong(cls, v: str) -> str:
        # BL-16 — failing loudly at boot beats running insecurely.
        if len(v.encode()) < 32:
            raise ValueError("JWT_SECRET must be at least 32 bytes")
        return v

    def model_post_init(self, _context: object) -> None:
        if self.environment == "pilot":
            if self.jwt_secret == _DEV_SECRET:
                raise ValueError("The development JWT_SECRET must not be used in pilot")
            if not self.cookie_secure:
                raise ValueError("COOKIE_SECURE must be true in pilot")

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
