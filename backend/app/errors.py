"""Error envelope and taxonomy (TDD 8.2, 8.3).

Every user-facing string is a `message_key` resolved through i18n, so errors
appear in Urdu and English alike (FR28, BL-13). `retryable` is explicit because
clients must not infer it from the status code.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .i18n.catalogue import translate
from .log_config import get_logger


class AppError(Exception):
    code = "INTERNAL_ERROR"
    http_status = status.HTTP_500_INTERNAL_SERVER_ERROR
    message_key = "errors.internal"
    retryable = True

    def __init__(self, details: dict[str, Any] | None = None) -> None:
        self.details = details or {}
        super().__init__(self.code)


class ValidationFailed(AppError):
    code = "VALIDATION_FAILED"
    http_status = status.HTTP_422_UNPROCESSABLE_CONTENT
    message_key = "errors.validation_failed"
    retryable = False


class Unauthenticated(AppError):
    """Deliberately generic. Wrong password, unknown email, suspended, and
    locked accounts must be indistinguishable (SPEC BL-04, AC-06)."""

    code = "UNAUTHENTICATED"
    http_status = status.HTTP_401_UNAUTHORIZED
    message_key = "errors.unauthenticated"
    retryable = False


class OAuthPasswordAccountExists(Unauthenticated):
    """Google sign-in reached a Supabase user that was registered with a
    password. Refused (audit H1) — that account signs in with its password.
    Only ever raised after the caller authenticated as the email's Google
    owner, so naming the reason reveals nothing to anyone else."""

    code = "OAUTH_PASSWORD_ACCOUNT"
    message_key = "errors.oauth_email_conflict"


class EmailAlreadyRegistered(AppError):
    """Registration for an email (after trim + lower-case) that already has
    an account. Generic: says nothing about the existing account."""

    code = "EMAIL_ALREADY_REGISTERED"
    http_status = status.HTTP_409_CONFLICT
    message_key = "errors.email_taken"
    retryable = False


class RegistrationFailed(AppError):
    """Registration could not be completed and was rolled back. Deliberately
    generic — never carries a Supabase or database error detail."""

    code = "REGISTRATION_FAILED"
    http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    message_key = "errors.registration_failed"
    retryable = True


class CrossOriginRejected(AppError):
    """A credential form was posted from another origin (login CSRF)."""

    code = "CROSS_ORIGIN_REJECTED"
    http_status = status.HTTP_403_FORBIDDEN
    message_key = "errors.cross_origin"
    retryable = False


class Forbidden(AppError):
    """Role mismatch or unverified doctor ONLY.

    Never used for a consent failure — that is 404 and belongs to the consent
    gateway (SPEC BL-07, decision D2).
    """

    code = "FORBIDDEN"
    http_status = status.HTTP_403_FORBIDDEN
    message_key = "errors.forbidden"
    retryable = False


class OAuthFailed(AppError):
    """Any OAuth failure — a bad/replayed state, a rejected code exchange, an
    email collision, or a provider-side cancellation. Deliberately as generic
    as `Unauthenticated`: the caller never learns which of these happened."""

    code = "OAUTH_FAILED"
    http_status = status.HTTP_400_BAD_REQUEST
    message_key = "errors.oauth_failed"
    retryable = False


class RateLimited(AppError):
    code = "RATE_LIMITED"
    http_status = status.HTTP_429_TOO_MANY_REQUESTS
    message_key = "errors.rate_limited"
    retryable = True

    def __init__(self, retry_after_s: int) -> None:
        self.retry_after_s = retry_after_s
        super().__init__({"retry_after_s": retry_after_s})


class NotFound(AppError):
    """Nonexistent, foreign, or no-longer-active resource. Deliberately one
    shape for all three so ids cannot be probed (patient profile AC-17)."""

    code = "NOT_FOUND"
    http_status = status.HTTP_404_NOT_FOUND
    message_key = "errors.not_found"
    retryable = False


class NotEditable(AppError):
    """The caller owns the patient record but not this entry — it was recorded
    by someone else (a clinician, or a legacy row). Kept separate from
    `Forbidden`, whose contract is role mismatch / unverified doctor only."""

    code = "NOT_EDITABLE"
    http_status = status.HTTP_403_FORBIDDEN
    message_key = "errors.not_editable"
    retryable = False


class DuplicateEntry(AppError):
    code = "DUPLICATE_ENTRY"
    http_status = status.HTTP_409_CONFLICT
    message_key = "errors.duplicate_entry"
    retryable = False


class ListFull(AppError):
    code = "LIST_FULL"
    http_status = status.HTTP_422_UNPROCESSABLE_CONTENT
    message_key = "errors.list_full"
    retryable = False

    def __init__(self, max_entries: int) -> None:
        super().__init__({"max": max_entries})


def message_params(exc: AppError) -> dict[str, Any]:
    """Scalar details double as message placeholders (e.g. `{max}`).
    `translate` only substitutes placeholders the template actually contains,
    so envelopes without one render exactly as before."""
    return {k: v for k, v in exc.details.items() if isinstance(v, (int, str))}


def envelope(exc: AppError, request_id: str, locale: str) -> dict[str, Any]:
    return {
        "error": {
            "code": exc.code,
            "message_key": exc.message_key,
            "message": translate(exc.message_key, locale, **message_params(exc)),
            "details": exc.details,
            "request_id": request_id,
            "retryable": exc.retryable,
        }
    }


def _context(request: Request) -> tuple[str, str]:
    request_id = getattr(request.state, "request_id", "-")
    locale = getattr(request.state, "locale", "en")
    return request_id, locale


async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    request_id, locale = _context(request)
    headers = {}
    if isinstance(exc, RateLimited):
        headers["Retry-After"] = str(exc.retry_after_s)
    return JSONResponse(
        status_code=exc.http_status,
        content=envelope(exc, request_id, locale),
        headers=headers,
    )


async def validation_handler(request: Request, exc: Exception) -> JSONResponse:
    """Map FastAPI's validation errors into the single house envelope."""
    assert isinstance(exc, RequestValidationError)
    request_id, locale = _context(request)
    fields = {".".join(str(p) for p in err["loc"][1:]): err["msg"] for err in exc.errors()}
    err = ValidationFailed({"fields": fields})
    return JSONResponse(status_code=err.http_status, content=envelope(err, request_id, locale))


async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    """Generic message to the caller; the failure is logged, never returned.

    Only the exception TYPE, path and request id are logged — not the message
    or traceback, which for database/Supabase errors can quote the email, a
    PMDC number or other PII the redaction processor cannot see inside a
    string (SPEC BL-14). The request id ties the log line to the response."""
    request_id, locale = _context(request)
    get_logger().error(
        "unhandled_error",
        error_type=type(exc).__name__,
        method=request.method,
        path=request.url.path,
        request_id=request_id,
    )
    return JSONResponse(status_code=500, content=envelope(AppError(), request_id, locale))
