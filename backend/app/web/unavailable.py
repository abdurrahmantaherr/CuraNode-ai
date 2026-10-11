"""What a person sees when the database cannot be reached.

A connection that times out, is refused or drops mid-request is not a bug in this application,
and the same request will probably work a moment later — so it answers 503 with a plain "try
again" message instead of a bare 500 (and, for a page, instead of raw JSON). The API gets the
usual error envelope, `retryable: true` and a `Retry-After`. Nothing here touches the database
(it is the thing that is down), and nothing from the failure — no SQL, no host — is shown.

Which failures count is decided by `is_database_unavailable`, deliberately narrow: a `ValueError`
or an `IntegrityError` is a bug and stays a 500. The handler is registered for the two broad
parents (`OSError`, SQLAlchemy's `DBAPIError`) because Starlette picks handlers by class, and
re-raises anything that is not clearly "could not talk to the database".
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from ..db.transient import is_network_error
from ..errors import ServiceUnavailable, envelope
from ..log_config import get_logger
from .router import _render

log = get_logger()

# What `main.py` registers the handler for. (`TimeoutError`, `ConnectionError` and
# `socket.gaierror` are all `OSError`s.)
HANDLED_BASES: tuple[type[Exception], ...] = (OSError, DBAPIError)

RETRY_AFTER_S = 5


def is_database_unavailable(exc: BaseException) -> bool:
    """True only for "could not talk to the database" — see the module docstring."""
    if isinstance(exc, DBAPIError):
        # a dropped or never-opened connection, not a query that was wrong
        return isinstance(exc, OperationalError | InterfaceError) or bool(
            exc.connection_invalidated
        )
    return is_network_error(exc)


def _wants_page(request: Request) -> bool:
    """The API answers in JSON; everything else a browser can open is a page."""
    return not request.url.path.startswith("/api/")


async def database_unavailable_handler(request: Request, exc: Exception) -> Response:
    if not is_database_unavailable(exc):
        raise exc  # not an outage: take the ordinary 500 path, exactly as if this did not exist
    request_id = getattr(request.state, "request_id", "-")
    # The class name only: an `OperationalError`'s message can carry SQL and its parameters. The
    # request id is the "Reference" the person is shown, so it can be found in the log.
    log.warning(
        "database_unavailable",
        error=type(exc).__name__,
        path=request.url.path,
        method=request.method,
        request_id=request_id,
    )
    err = ServiceUnavailable()
    locale = getattr(request.state, "locale", "en")
    headers = {"Retry-After": str(RETRY_AFTER_S), "Cache-Control": "no-store"}
    if _wants_page(request):
        try:
            response = _render(
                request,
                "errors/unavailable.html",
                locale,
                status_code=err.http_status,
                request_id=request_id,
                can_retry=request.method == "GET",
                retry_url=request.url.path + (f"?{request.url.query}" if request.url.query else ""),
            )
            response.headers.update(headers)
            return response
        except Exception:  # noqa: BLE001 - a broken page must not hide the 503 itself
            log.warning("unavailable_page_failed")
    return JSONResponse(
        status_code=err.http_status, content=envelope(err, request_id, locale), headers=headers
    )
