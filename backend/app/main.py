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
_APP_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _APP_DIR.parent

# Running this file directly (`uv run backend/app/main.py`) makes CPython
# auto-prepend the script's OWN directory (`backend/app/`) to sys.path[0].
# `backend/app/profile/` (the patient-profile feature) then shadows the
# standard-library `profile` module for any code that does a bare
# `import profile` — which the stdlib's own `cProfile.py` does internally,
# breaking any lazy import that pulls in `cProfile` (observed: PyTorch's
# `torch._dynamo`, back when handwriting recognition was supported). `backend/` (added below) is the only entry this project's
# imports actually need, so the auto-inserted one is removed, not replaced.
if sys.path and Path(sys.path[0]).resolve() == _APP_DIR:
    sys.path.pop(0)

if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import asyncio
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from app.consent.router import router as consent_router
from app.db.models import Profile
from app.db.session import SessionFactory, engine
from app.documents.doctor_router import router as doctor_documents_router
from app.documents.router import router as documents_router
from app.errors import (
    AppError,
    FileTooLarge,
    app_error_handler,
    unhandled_handler,
    validation_handler,
)
from app.i18n.catalogue import normalise_locale
from app.identity.router import router as identity_router
from app.log_config import configure_logging, get_logger
from app.paths import STATIC_DIR, TEMPLATES_DIR
from app.profile.router import router as profile_router
from app.settings import settings
from app.web.booking import router as web_booking_router
from app.web.documents import router as web_documents_router
from app.web.router import router as web_router
from app.web.unavailable import (
    HANDLED_BASES,
    database_unavailable_handler,
    is_database_unavailable,
)

log = get_logger()


# The start-up check needs ONE database connection, and on a slow or flaky route the first
# connection is the one most likely to stall (a real dev machine failed to start twice on a TLS
# handshake that timed out). Starting is worth a few patient tries; after them the server still
# refuses to start — the check cannot be skipped — but with a plain message, not a traceback.
STARTUP_DB_ATTEMPTS = 4
STARTUP_DB_PAUSE_S = 3.0


async def _startup_checks() -> None:
    # BL-17 — no real data may sit in a non-pilot environment (NFR19).
    if settings.environment == "pilot":
        return
    for attempt in range(1, STARTUP_DB_ATTEMPTS + 1):
        try:
            async with SessionFactory() as session:
                rows = await session.execute(
                    select(Profile.id).where(Profile.is_synthetic.is_(False)).limit(1)
                )
                real_data_found = rows.scalar_one_or_none() is not None
            break
        except Exception as exc:
            if not is_database_unavailable(exc):
                raise
            if attempt == STARTUP_DB_ATTEMPTS:
                raise RuntimeError(
                    f"Could not reach the database after {STARTUP_DB_ATTEMPTS} tries "
                    f"({type(exc).__name__}). Check the network and DATABASE_URL, then start again."
                ) from exc
            log.warning("startup_database_unreachable", attempt=attempt, error=type(exc).__name__)
            await asyncio.sleep(STARTUP_DB_PAUSE_S * attempt)
    if real_data_found:
        raise RuntimeError(
            "Non-synthetic user rows found outside the pilot environment (NFR19). Refusing to start."
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


_UPLOAD_PATH = re.compile(r"^(?:/api/v1/documents|/(?:en|ur)/patient/documents)/?$")
# Multipart framing and the form's other fields on top of the file itself.
_UPLOAD_OVERHEAD_BYTES = 256 * 1024


@app.middleware("http")
async def limit_upload_size(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Refuse an oversized document upload from its Content-Length header,
    before the multipart body is read. (The service re-checks the real bytes;
    this only stops a huge body being buffered to disk first.) `request_context`
    is registered after this, so it wraps this and has already put the locale
    and request id on `request.state` for the error envelope."""
    if request.method == "POST" and _UPLOAD_PATH.match(request.url.path):
        declared = request.headers.get("content-length", "")
        too_large = settings.document_max_bytes + _UPLOAD_OVERHEAD_BYTES
        if declared.isdigit() and int(declared) > too_large:
            return await app_error_handler(
                request, FileTooLarge(settings.document_max_bytes // (1024 * 1024))
            )
    return await call_next(request)


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
# The database cannot be reached: 503, not a bare 500. Registered for the broad parents; the
# handler re-raises anything that is not clearly an outage (see web/unavailable.py).
for _base in HANDLED_BASES:
    app.add_exception_handler(_base, database_unavailable_handler)
app.add_exception_handler(Exception, unhandled_handler)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.include_router(identity_router)
app.include_router(profile_router)
app.include_router(consent_router)
app.include_router(documents_router)
app.include_router(doctor_documents_router)
app.include_router(web_documents_router)
app.include_router(web_booking_router)
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
