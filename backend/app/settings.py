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
    # How long to wait for ONE attempt at a NEW database connection (it is tried again once or
    # twice for a blip — see `db/transient.py`). asyncpg's own default is 60 s, which turns a
    # blip into a minute-long hang; but a slow route to a far-away pooler really can take 10 s
    # for the TLS handshake alone (a dev machine failed to START at 15 s), so not too short.
    # Postgres only.
    database_connect_timeout_s: int = 25
    # A pooled connection idle for longer than this is tested as it is checked out, and
    # replaced if a pooler or router dropped it meanwhile (otherwise the first request after
    # a pause fails and the next one works). A busy connection is never tested. 0 = never.
    database_idle_ping_s: int = 15

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

    # ── Patient profile (FR2) ────────────────────────────────────────────
    profile_write_rate_limit_per_minute: int = 30
    profile_max_entries_per_list: int = 50

    # ── Medical documents (FR10–FR13) ────────────────────────────────────
    # The private Supabase Storage bucket that already exists in the shared
    # project. Originals are read back through the app (ownership-checked),
    # never through a public URL.
    document_bucket: str = "medical-documents"
    document_max_bytes: int = 10 * 1024 * 1024
    document_write_rate_limit_per_minute: int = 20
    # A document stuck in `processing` longer than this (a crashed request) may
    # be re-processed instead of returning "already in progress" forever.
    document_processing_stale_s: int = 120
    # Whether a patient may delete a document they uploaded after it has been confirmed
    # (a saved record). A clinician-authored record is never deletable by the patient,
    # whatever this says. Turn off to keep every saved record permanently.
    document_allow_delete_finalized: bool = True
    # OCR is entirely LOCAL and free — no API key, no external service, and no
    # document ever leaves the machine. Printed documents only, read by
    # Tesseract (see `documents/ocr.py`; ocr_job.engine = 'tesseract').
    # Path to the Tesseract executable. Empty = look on PATH, then the Windows
    # installer's default location.
    tesseract_cmd: str = ""
    # Tesseract language pack(s), e.g. "eng" or "eng+urd" (the pack must be
    # installed alongside Tesseract).
    tesseract_lang: str = "eng"
    # Per-page Tesseract timeout, in seconds.
    ocr_timeout_s: int = 60
    # PDFs are rendered to images locally at this resolution before OCR.
    ocr_pdf_dpi: int = 300
    # A PDF with more pages than this is refused rather than silently truncated
    # (a medical document is never read partially), and keeps OCR inside the
    # PRD's 30 s budget (NFR3).
    ocr_max_pages: int = 5
    # Largest image (in pixels) that will be decoded — a small file can hold a
    # huge bitmap.
    ocr_max_pixels: int = 50_000_000

    # AI semantic understanding (Gemini), an OPTIONAL enrichment stage after
    # deterministic prescription extraction — see `documents/ai_understanding.py`.
    # Entirely opt-in: with no key set, `ai_understanding.is_configured()` is
    # False and the pipeline falls back to the existing deterministic
    # extraction/normalization exactly as before Gemini existed. Unlike OCR,
    # this is the one stage in the pipeline where OCR text leaves the
    # machine (to Google's API) — a deliberate, disclosed exception, only
    # when the operator has explicitly opted in by setting a key.
    gemini_api_key: str = ""
    # A free-tier-eligible, fast model suited to structured extraction.
    # `gemini-2.5-flash` is dead for newly-issued API keys (Google's API
    # itself returns 404 NOT_FOUND pointing here) — confirmed against the
    # real API, not assumed.
    gemini_model: str = "gemini-3.8-flash"
    gemini_timeout_s: int = 20

    # Fallback providers for the same AI stage, tried in `ai_provider_order`
    # when the one before it is unconfigured, rate-limited, down or returns
    # something unusable. Both speak the OpenAI-compatible chat API, send
    # OCR TEXT only (never pixels), and are opt-in exactly like Gemini: no
    # key, no calls. Model ids are operator-tunable defaults, not guarantees
    # — free-tier model catalogues change.
    groq_api_key: str = ""
    # One of the text models on Groq's own free-plan limits page as of
    # 2026-10-08 (https://console.groq.com/docs/rate-limits): 30 requests/min,
    # 1K requests/day, 8K tokens/min, 200K tokens/day. The previous default
    # (llama-3.3-70b-versatile) is no longer listed there. Not exercised
    # against the live API — that needs a key — so if Groq rejects it the
    # chain just moves on; fix it with GROQ_MODEL.
    groq_model: str = "openai/gpt-oss-120b"
    openrouter_api_key: str = ""
    # Checked against OpenRouter's public model list (no key needed) on
    # 2026-10-08: free, instruction-tuned, accepts `response_format`. Free
    # models are rotated often — if this one disappears OpenRouter answers
    # 404, the chain skips it for a while and moves on, and the fix is just
    # this setting (see https://openrouter.ai/models?max_price=0).
    openrouter_model: str = "google/gemma-4-31b-it:free"
    ai_provider_order: str = "gemini,groq,openrouter"
    ai_provider_timeout_s: int = 20
    # How long a provider that just hit a rate limit / quota is skipped, so
    # an exhausted free tier costs one failed call, not one per upload.
    ai_provider_cooldown_s: int = 60
    # Ceiling on the whole chain for one request: later providers are not
    # started once this much time has already been spent.
    ai_total_timeout_s: int = 45
    # The AI reading of a document's SECTIONS (hospital, patient, doctor, dates,
    # advice — `documents/ai_sections.py`) is a SECOND call per prescription /
    # lab upload, on top of the medicines call. Free tiers are small, so it can
    # be switched off on its own without touching the medicines layer.
    ai_sections_enabled: bool = True

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
