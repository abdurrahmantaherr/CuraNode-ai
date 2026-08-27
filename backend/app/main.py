"""ASGI application entrypoint.

Runnable two ways:

    uv run backend/app/main.py                 # direct script
    uv run uvicorn app.main:app --app-dir backend

Startup guards run before the app serves anything (SPEC BL-16, BL-17): a weak
JWT secret or non-synthetic data outside pilot is a refusal to boot, not a
warning.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Executing a module that lives inside a package as a script leaves it with no
# package context, so relative imports would fail. Put `backend/` on the path
# and import absolutely instead.
_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from app.db.models import Profile
from app.db.session import SessionFactory, engine
from app.errors import (
    AppError,
    app_error_handler,
    unhandled_handler,
    validation_handler,
)
from app.i18n.catalogue import normalise_locale
from app.identity.router import router as identity_router
from app.log_config import configure_logging, get_logger
from app.paths import STATIC_DIR, TEMPLATES_DIR
from app.settings import settings
from app.web.router import router as web_router

log = get_logger()


async def _startup_checks() -> None:
    # BL-17 — no real data may sit in a non-pilot environment (NFR19).
    if settings.environment != "pilot":
        async with SessionFactory() as session:
            rows = await session.execute(
                select(Profile.id).where(Profile.is_synthetic.is_(False)).limit(1)
            )
            if rows.scalar_one_or_none() is not None:
                raise RuntimeError(
                    "Non-synthetic user rows found outside the pilot environment "
                    "(NFR19). Refusing to start."
                )


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    await _startup_checks()
    log.info("startup", environment=settings.environment)
    yield
    await engine.dispose()


app = FastAPI(
    title="CuraNode-AI",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
)


@app.middleware("http")
async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Attach a correlation id and the active locale to every request."""
    request.state.request_id = request.headers.get("X-Request-Id", secrets.token_urlsafe(12))

    # Locale comes from the URL prefix, falling back to a cookie then default.
    parts = request.url.path.strip("/").split("/")
    if parts and parts[0] in ("en", "ur"):
        locale = parts[0]
    else:
        locale = normalise_locale(request.cookies.get("cn_locale"))
    request.state.locale = locale

    response = await call_next(request)
    response.headers["X-Request-Id"] = request.state.request_id
    # TDD 7.7 — application hardening headers.
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


app.add_exception_handler(AppError, app_error_handler)
app.add_exception_handler(RequestValidationError, validation_handler)
app.add_exception_handler(Exception, unhandled_handler)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.include_router(identity_router)
app.include_router(web_router)


@app.get("/healthz", include_in_schema=False)
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok", "environment": settings.environment})


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(f"/{settings.default_locale}/login")


def run() -> None:
    """Development server. `app_dir` is what lets the reloader's subprocess
    import `app.main` without inheriting this process's sys.path."""
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
        app_dir=str(_BACKEND_DIR),
        # Templates and CSS live outside the Python package, so the reloader
        # has to be told to watch them too.
        reload_dirs=[str(_BACKEND_DIR), str(TEMPLATES_DIR), str(STATIC_DIR)],
    )


if __name__ == "__main__":
    run()
