"""Medical-document JSON API (FR10–FR13).

Every route is behind `PatientDep` — the authenticated session is the only
source of patient identity; no `patient_id` is ever read from a request.

    POST /api/v1/documents                    upload (document_type + file) -> uploaded
    POST /api/v1/documents/{id}/process       common OCR + type-specific extraction
    GET  /api/v1/documents                    finalized records — the read side the
                                              Timeline will use. Filters: type, q (file
                                              or type name), date_from, date_to
    GET  /api/v1/documents/in-progress        uploads not yet confirmed. Filters: type,
                                              status, q, date_from, date_to
    GET  /api/v1/documents/{id}               status + extracted rows for review
    POST /api/v1/documents/{id}/confirm       Confirm & Save (finalizes)
    POST /api/v1/documents/{id}/cancel        discard an unconfirmed upload
    GET  /api/v1/documents/{id}/file          the original upload, exactly as stored
                                              (?download=true to save it, not display it)
    DELETE /api/v1/documents/{id}             delete the patient's own document
                                              (?confirm=true is required for a saved record)

There is deliberately no `/prescription/process`, `/lab/process`, ...: one
pipeline serves every document type, chosen by `document_type`.
"""

from __future__ import annotations

import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Query, Request, Response, UploadFile, status

# Module-scope imports on purpose: with `from __future__ import annotations`
# FastAPI resolves these against this module's globals (see profile/router.py).
from ..deps import DocumentWriteRateLimit, PatientDep, SessionDep
from ..errors import ValidationFailed
from ..settings import settings
from . import filters as doc_filters
from . import service
from .schemas import ConfirmRequest, DocumentDetailOut, DocumentOut, DocumentType

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])


def _ua(request: Request) -> str | None:
    return request.headers.get("user-agent")


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def file_response(
    data: bytes, mime_type: str, filename: str, *, download: bool = False
) -> Response:
    """The original upload — inline to look at, or as an attachment to save (`download`).
    The bytes are exactly the ones stored. Private and uncacheable; `nosniff` (set globally)
    keeps the browser to the declared type."""
    ascii_name = "".join(c if 32 < ord(c) < 127 and c not in '"\\' else "_" for c in filename)
    return Response(
        content=data,
        media_type=mime_type,
        headers={
            "Content-Disposition": (
                f"{'attachment' if download else 'inline'}; "
                f"filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
            ),
            "Cache-Control": "private, no-store",
        },
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=DocumentOut,
    dependencies=[DocumentWriteRateLimit],
)
async def upload_document(
    request: Request,
    actor: PatientDep,
    session: SessionDep,
    document_type: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
) -> DocumentOut:
    # One byte past the limit is enough to know it is too large without ever
    # holding an arbitrarily large body in memory.
    data = await file.read(settings.document_max_bytes + 1)
    doc = await service.create_upload(
        session,
        actor,
        document_type=document_type,
        filename=file.filename,
        declared_mime=file.content_type,
        data=data,
        ip=_ip(request),
        user_agent=_ua(request),
    )
    return service.to_out(doc, actor.user_id)


def _filters(
    *,
    q: str | None = None,
    type: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> doc_filters.DocumentFilters:
    """The request's search / filter parameters, or a 422 naming each one that is not valid."""
    parsed, problems = doc_filters.parse(
        query=q, document_type=type, status=status, date_from=date_from, date_to=date_to
    )
    if problems:
        raise ValidationFailed({"fields": problems})
    return parsed


# The only query parameters the two list endpoints take. Anything else is REFUSED rather than
# ignored: a misspelt filter (`?stauts=failed`) that was silently dropped would hand back every
# document as though it had been the answer.
_LIST_PARAMS = frozenset({"type", "status", "q", "date_from", "date_to", "limit", "offset"})
MAX_PAGE = 500


def _only_known_params(request: Request) -> None:
    unknown = sorted(set(request.query_params) - _LIST_PARAMS)
    if unknown:
        raise ValidationFailed({"fields": {name: "errors.unknown_parameter" for name in unknown}})


@router.get("", response_model=list[DocumentDetailOut])
async def list_records(
    request: Request,
    response: Response,
    actor: PatientDep,
    session: SessionDep,
    type: DocumentType | None = None,
    status: str | None = None,
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = MAX_PAGE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[DocumentDetailOut]:
    """Finalized records, newest first. `status` only ever matches "finalized" (anything else
    is simply not a record: the result is empty). `X-Total-Count` is how many the filters
    match; `limit` / `offset` page through them."""
    _only_known_params(request)
    scoped = _filters(q=q, status=status, date_from=date_from, date_to=date_to)
    response.headers["X-Total-Count"] = str(
        await service.count_finalized(session, actor, document_type=type, filters=scoped)
    )
    return await service.list_finalized(
        session, actor, document_type=type, filters=scoped, limit=limit, offset=offset
    )


@router.get("/in-progress", response_model=list[DocumentOut])
async def list_in_progress(
    request: Request,
    response: Response,
    actor: PatientDep,
    session: SessionDep,
    type: DocumentType | None = None,
    status: str | None = None,
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = MAX_PAGE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[DocumentOut]:
    _only_known_params(request)
    scoped = _filters(q=q, type=type, status=status, date_from=date_from, date_to=date_to)
    response.headers["X-Total-Count"] = str(
        await service.count_in_progress(session, actor, filters=scoped)
    )
    return await service.list_in_progress(
        session, actor, filters=scoped, limit=limit, offset=offset
    )


@router.get("/{document_id}", response_model=DocumentDetailOut)
async def get_document(
    document_id: uuid.UUID, actor: PatientDep, session: SessionDep
) -> DocumentDetailOut:
    return await service.get_detail(session, actor, document_id)


@router.post(
    "/{document_id}/process",
    response_model=DocumentDetailOut,
    dependencies=[DocumentWriteRateLimit],
)
async def process_document(
    document_id: uuid.UUID,
    request: Request,
    actor: PatientDep,
    session: SessionDep,
) -> DocumentDetailOut:
    """Runs the common OCR + extraction (also how a failed document is retried)."""
    doc = await service.process(
        session,
        actor,
        document_id,
        ip=_ip(request),
        user_agent=_ua(request),
    )
    return await service.to_detail(session, doc, actor.user_id)


@router.post(
    "/{document_id}/confirm",
    response_model=DocumentDetailOut,
    dependencies=[DocumentWriteRateLimit],
)
async def confirm_document(
    document_id: uuid.UUID,
    request: Request,
    actor: PatientDep,
    session: SessionDep,
    body: ConfirmRequest,
) -> DocumentDetailOut:
    doc = await service.confirm(
        session, actor, document_id, body, ip=_ip(request), user_agent=_ua(request)
    )
    return await service.to_detail(session, doc, actor.user_id)


@router.post(
    "/{document_id}/cancel",
    response_model=DocumentOut,
    dependencies=[DocumentWriteRateLimit],
)
async def cancel_document(
    document_id: uuid.UUID, request: Request, actor: PatientDep, session: SessionDep
) -> DocumentOut:
    doc = await service.cancel(
        session, actor, document_id, ip=_ip(request), user_agent=_ua(request)
    )
    return service.to_out(doc, actor.user_id)


@router.delete(
    "/{document_id}",
    response_model=DocumentOut,
    dependencies=[DocumentWriteRateLimit],
)
async def delete_document(
    document_id: uuid.UUID,
    request: Request,
    actor: PatientDep,
    session: SessionDep,
    confirm: bool = False,
) -> DocumentOut:
    """Delete the caller's own document (see `service.delete_document` for exactly what goes
    and what stays). A saved record needs `?confirm=true`."""
    doc = await service.delete_document(
        session,
        actor,
        document_id,
        confirmed=confirm,
        ip=_ip(request),
        user_agent=_ua(request),
    )
    return service.to_out(doc, actor.user_id)


@router.get("/{document_id}/file")
async def get_original(
    document_id: uuid.UUID,
    actor: PatientDep,
    session: SessionDep,
    download: bool = False,
) -> Response:
    data, mime, filename = await service.get_file(session, actor, document_id)
    return file_response(data, mime, filename, download=download)
