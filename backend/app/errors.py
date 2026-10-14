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


class ConsentDenied(NotFound):
    """The consent gateway's refusal (P9 BL-16). A `NotFound` in every way the
    caller can observe (same code, status and envelope), so a doctor can't
    tell "not shared with you" from "no such patient" (D2). The subclass
    exists only so code and tests can name the reason."""


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


class ScheduleConflict(AppError):
    """A doctor's schedule window would overlap another of their windows
    (D7 R1, R1b, R4, R4b). The pure engine reports conflicts as plain data;
    the schedule service raises this. Scalar `details` (`clinic`, `day`,
    `start`, `end`, `conflict_id`) name the conflicting window and fill the
    message placeholders."""

    code = "SCHEDULE_CONFLICT"
    http_status = status.HTTP_409_CONFLICT
    message_key = "errors.schedule_overlap"
    retryable = False


class BookingRefused(AppError):
    """A booking, reschedule or cancel the rules refuse (P5, spec 3.2): the time
    is not offered, too soon, too far ahead, taken, the session is full, the
    patient already has an overlapping appointment or one with that doctor that
    day, or the appointment can no longer be changed. `reason` picks the
    message (`errors.booking_<reason>`). A 409: the page can be shown again with
    fresh times."""

    code = "BOOKING_REFUSED"
    http_status = status.HTTP_409_CONFLICT
    message_key = "errors.booking_not_offered"
    retryable = False

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.message_key = f"errors.booking_{reason}"
        super().__init__({"reason": reason})


class QueueRefused(AppError):
    """A queue action the rules refuse (P6, spec 3.2): the appointment is not for
    today or cannot be checked in, the session has ended, the doctor is not
    available, someone is already in the room, the session is full, the patient
    already has a booking today or is already queued, the patient is no longer
    waiting or not in the room, the search is too short, or a walk-in's time was
    just taken. `reason` picks the message (`errors.queue_<reason>`). A 409: the
    page is shown again with the reason."""

    code = "QUEUE_REFUSED"
    http_status = status.HTTP_409_CONFLICT
    message_key = "errors.queue_not_today"
    retryable = False

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.message_key = f"errors.queue_{reason}"
        super().__init__({"reason": reason})


class UnsupportedFileType(AppError):
    """Not a PDF, JPEG or PNG — judged by the file's own bytes, not its name."""

    code = "UNSUPPORTED_FILE_TYPE"
    http_status = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    message_key = "errors.file_type_unsupported"
    retryable = False


class FileTooLarge(AppError):
    code = "FILE_TOO_LARGE"
    http_status = status.HTTP_413_CONTENT_TOO_LARGE
    message_key = "errors.file_too_large"
    retryable = False

    def __init__(self, max_mb: int) -> None:
        super().__init__({"max_mb": max_mb})


class EmptyFile(AppError):
    code = "FILE_EMPTY"
    http_status = status.HTTP_422_UNPROCESSABLE_CONTENT
    message_key = "errors.file_empty"
    retryable = False


class StorageUnavailable(AppError):
    """The object store refused or lost the file. Nothing was recorded."""

    code = "STORAGE_UNAVAILABLE"
    http_status = status.HTTP_502_BAD_GATEWAY
    message_key = "errors.upload_failed"
    retryable = True


class FileUnavailable(AppError):
    """The original file exists as far as we know but the store would not give it back (an
    outage, a timeout). Different from `NotFound`, which says it is not there."""

    code = "FILE_UNAVAILABLE"
    http_status = status.HTTP_502_BAD_GATEWAY
    message_key = "errors.file_unavailable"
    retryable = True


class ServiceUnavailable(AppError):
    """The database could not be reached (a timeout, a refused or dropped connection). Nothing
    was changed and the same request will probably work in a moment, so it is `retryable` — and
    it is 503, not the opaque 500 that means "a bug on our side"."""

    code = "SERVICE_UNAVAILABLE"
    http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    message_key = "errors.service_unavailable"
    retryable = True


class DeleteFailed(AppError):
    """A deletion that did not complete. Nothing is reported as deleted; the patient may retry."""

    code = "DELETE_FAILED"
    http_status = status.HTTP_502_BAD_GATEWAY
    message_key = "errors.document_delete_failed"
    retryable = True


class DocumentNotDeletable(AppError):
    """The document is the patient's but may not be deleted by them: it was written by a
    clinician, or saved records are configured not to be deletable. Same reasoning as
    `NotEditable` for profile entries — a patient can never erase what a clinician recorded."""

    code = "DOCUMENT_NOT_DELETABLE"
    http_status = status.HTTP_403_FORBIDDEN
    message_key = "errors.document_not_deletable"
    retryable = False


class InvalidDocumentState(AppError):
    """The document is not in a state that allows this action (e.g. cancelling
    a finalized record, confirming one that was cancelled)."""

    code = "INVALID_DOCUMENT_STATE"
    http_status = status.HTTP_409_CONFLICT
    message_key = "errors.document_state"
    retryable = False


class ProcessingInProgress(AppError):
    code = "PROCESSING_IN_PROGRESS"
    http_status = status.HTTP_409_CONFLICT
    message_key = "errors.document_processing"
    retryable = True


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
    """Generic message to the caller; the detail is logged, never returned."""
    request_id, locale = _context(request)
    return JSONResponse(status_code=500, content=envelope(AppError(), request_id, locale))
