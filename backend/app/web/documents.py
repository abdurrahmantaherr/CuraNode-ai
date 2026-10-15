"""Server-rendered pages for medical documents (FR10–FR13).

ONE upload / review workflow for all four document types. The page is the same
for a prescription, a lab report, a vaccination record and any other document;
only the fields shown on the review step change, and those come from the
document type's extractor (`documents/extractors.py`).

Every rule lives in `documents/service.py`; this module only turns forms into
service calls and results into pages, mirroring the patient-profile handlers.
Every form works without JavaScript (POST + redirect); `documents.js` adds a
file preview, a submitting state and the auto-refresh of the processing page.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..db import types as dbtypes
from ..db.models import UserRole
from ..deps import Actor, OptionalActorDep, SessionDep, check_document_write_rate
from ..documents import drug_names, groups
from ..documents import filters as doc_filters
from ..documents import service as doc_service
from ..documents.router import file_response
from ..documents.schemas import DB_TO_API_TYPE, ConfirmRequest, DocumentDetailOut, ReviewedRow
from ..errors import (
    AppError,
    DeleteFailed,
    DocumentNotDeletable,
    EmptyFile,
    FileTooLarge,
    FileUnavailable,
    InvalidDocumentState,
    NotFound,
    ProcessingInProgress,
    RateLimited,
    StorageUnavailable,
    UnsupportedFileType,
    ValidationFailed,
    message_params,
)
from ..i18n.catalogue import normalise_locale, translate
from ..settings import settings
from .router import _area, _patient_nav, _render, login_redirect

router = APIRouter(tags=["web-documents"])

DOCUMENT_TYPES = ("prescription", "lab_report", "vaccination", "other")
# How many saved records one page of the list shows, and the most uploads-in-progress it
# lists (there are never many; the filters reach the rest).
PAGE_SIZE = 25
IN_PROGRESS_CAP = 100
# A field name may contain a digit ("spo2"): a stricter pattern silently dropped that input.
_ROW_FIELD = re.compile(r"^row-(\d+)-([a-z0-9_]+)$")


# ── Guard & shared context ───────────────────────────────────────────────
def _guard(request: Request, locale: str, actor: Actor | None) -> Response | None:
    """Same redirects as the profile pages (mirrors `_guarded()`), but returning
    a signed-out user to the documents page they asked for."""
    loc = normalise_locale(locale)
    if actor is None:
        return login_redirect(loc, request)
    if actor.role != UserRole.PATIENT.value:
        return RedirectResponse(f"/{loc}/{_area(actor.role)}", status_code=303)
    if not actor.onboarding_complete:
        return RedirectResponse(f"/{loc}/onboarding", status_code=303)
    return None


def _type_label(loc: str, key: str) -> str:
    return translate(f"documents.type.{key}", loc)


def _type_options(loc: str) -> list[dict[str, str]]:
    return [{"value": k, "label": _type_label(loc, k)} for k in DOCUMENT_TYPES]


def _parse_id(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _error_text(loc: str, exc: AppError) -> str:
    return translate(exc.message_key, loc, **message_params(exc))


def _failure_text(loc: str, code: str | None) -> str:
    """The message for a failed document, by its stored failure code. A code with no message
    of its own (an old or unexpected one) gets the general one — a patient is never shown a
    catalogue key or an internal code."""
    generic = translate("errors.ocr_failed", loc)
    if not code:
        return generic
    text = translate(f"errors.{code}", loc)
    return generic if text == f"errors.{code}" else text


def _human_size(size: int | None) -> str:
    if not size:
        return ""
    if size < 1024 * 1024:
        return f"{max(1, round(size / 1024))} KB"
    return f"{size / (1024 * 1024):.1f} MB"


# Thresholds match `normalization.CERTAINTY_WEIGHT` exactly (high=1.0,
# uncertain=0.6, ambiguous=0.3) but compare with `>=` rather than exact
# float equality, since the value has round-tripped through Decimal/JSON —
# `>=` is exact-safe here without being fragile to it. "missing" (no value
# at all) intentionally has no badge: a blank field is already visible as
# blank, and (on the finalized page) already says "Not specified" — a
# confidence badge on top of that would be redundant, not informative.
_CONFIDENCE_LEVELS = (
    (0.9, "high", "badge--ok"),
    (0.45, "uncertain", "badge--warn"),
    (0.0, "ambiguous", "badge--err"),
)


def _confidence_badge(loc: str, value: float | None) -> dict[str, str] | None:
    if value is None:
        return None
    for threshold, key, css_class in _CONFIDENCE_LEVELS:
        if value >= threshold:
            return {"label": translate(f"documents.confidence.{key}", loc), "class": css_class}
    return None


def _local_date(value: datetime | None) -> str:
    """A stored (UTC) timestamp as the calendar date in Pakistan, `YYYY-MM-DD` — the day the
    patient would say it was, not the UTC day (which is yesterday for five hours after midnight)."""
    return dbtypes.pk_date(value).isoformat() if value else ""


def _ctx(loc: str, page_title_key: str) -> dict[str, Any]:
    return {
        "local_date": _local_date,
        "crumb": translate("auth.role.patient", loc),
        "page_title": translate(page_title_key, loc),
        "nav_items": _patient_nav(loc, current="documents"),
        "type_label": lambda key: _type_label(loc, key),
        "human_size": _human_size,
        "failure_text": lambda code: _failure_text(loc, code),
        "confidence_badge": lambda value: _confidence_badge(loc, value),
        "today": dbtypes.today_pk().isoformat(),
    }


# ── Upload ───────────────────────────────────────────────────────────────
def _render_upload(
    request: Request,
    loc: str,
    *,
    status_code: int = 200,
    selected: str = "",
    errors: dict[str, str] | None = None,
    form_error: str | None = None,
) -> Response:
    return _render(
        request,
        "documents/upload.html",
        loc,
        status_code=status_code,
        path_suffix="/patient/documents/new",
        selected=selected,
        errors=errors or {},
        form_error=form_error,
        type_options=_type_options(loc),
        max_mb=settings.document_max_bytes // (1024 * 1024),
        **_ctx(loc, "documents.upload.title"),
    )


@router.get("/{locale}/patient/documents/new", response_class=HTMLResponse)
async def upload_page(
    request: Request, locale: str, actor: OptionalActorDep, type: str = ""
) -> Response:
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    loc = normalise_locale(locale)
    return _render_upload(request, loc, selected=type if type in DOCUMENT_TYPES else "")


@router.post("/{locale}/patient/documents")
async def upload_submit(
    request: Request,
    locale: str,
    session: SessionDep,
    actor: OptionalActorDep,
    document_type: Annotated[str, Form()] = "",
    file: Annotated[UploadFile | None, File()] = None,
) -> Response:
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    selected = document_type if document_type in DOCUMENT_TYPES else ""

    def fail(
        code: int, errors: dict[str, str] | None = None, banner: str | None = None
    ) -> Response:
        return _render_upload(
            request,
            loc,
            status_code=code,
            selected=selected,
            errors=errors,
            form_error=banner,
        )

    try:
        await check_document_write_rate(actor)
    except RateLimited:
        return fail(429, banner=translate("errors.rate_limited", loc))

    if not selected:
        return fail(422, {"document_type": translate("errors.document_type_invalid", loc)})
    if file is None or not file.filename:
        return fail(422, {"file": translate("errors.file_required", loc)})

    data = await file.read(settings.document_max_bytes + 1)
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    try:
        doc = await doc_service.create_upload(
            session,
            actor,
            document_type=selected,
            filename=file.filename,
            declared_mime=file.content_type,
            data=data,
            ip=ip,
            user_agent=ua,
        )
    except ValidationFailed:
        return fail(422, {"document_type": translate("errors.document_type_invalid", loc)})
    except (UnsupportedFileType, FileTooLarge, EmptyFile) as exc:
        return fail(exc.http_status, {"file": _error_text(loc, exc)})
    except StorageUnavailable as exc:
        return fail(exc.http_status, banner=_error_text(loc, exc))
    except NotFound:
        return RedirectResponse(f"/{loc}/onboarding", status_code=303)

    # Common OCR + type-specific extraction. A failure here is a document state
    # the next page explains (retry / cancel), not an error page. `data` is
    # what was just uploaded, handed straight to `process()` so it doesn't
    # immediately re-download the same bytes from Storage.
    try:
        await doc_service.process(
            session, actor, doc.id, data=data, known=doc, ip=ip, user_agent=ua
        )
    except AppError:
        pass
    return RedirectResponse(f"/{loc}/patient/documents/{doc.id}", status_code=303)


# ── List ─────────────────────────────────────────────────────────────────
# The statuses a patient can filter by, in the order they are offered.
_STATUS_FILTERS = ("finalized", "awaiting_review", "processing", "uploaded", "failed")


def _status_options(loc: str) -> list[dict[str, str]]:
    return [
        {"value": key, "label": translate(f"documents.status.{key}", loc)}
        for key in _STATUS_FILTERS
    ]


def _list_url(loc: str, values: dict[str, str]) -> str:
    """The list page's address for these filter values (empty ones left out)."""
    query = urlencode({key: value for key, value in values.items() if value})
    return f"/{loc}/patient/documents" + (f"?{query}" if query else "")


async def _render_list(
    request: Request,
    loc: str,
    session: SessionDep,
    actor: Actor,
    *,
    status_code: int = 200,
    filters: doc_filters.DocumentFilters | None = None,
    problems: dict[str, str] | None = None,
    page_error: str | None = None,
    notice: str | None = None,
    page: int = 1,
) -> Response:
    scoped = filters or doc_filters.DocumentFilters()
    total_records = await doc_service.count_finalized(session, actor, filters=scoped)
    pages = max(1, -(-total_records // PAGE_SIZE))
    page = min(max(page, 1), pages)  # a page past the end is the last page, not an empty one
    records = await doc_service.list_finalized(
        session, actor, filters=scoped, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE
    )
    total_in_progress = await doc_service.count_in_progress(session, actor, filters=scoped)
    in_progress = await doc_service.list_in_progress(
        session, actor, filters=scoped, limit=IN_PROGRESS_CAP
    )
    values = {
        "q": scoped.query,
        "type": DB_TO_API_TYPE.get(scoped.document_type or "", ""),
        "status": scoped.status or "",
        "date_from": scoped.date_from.isoformat() if scoped.date_from else "",
        "date_to": scoped.date_to.isoformat() if scoped.date_to else "",
    }
    return _render(
        request,
        "documents/list.html",
        loc,
        status_code=status_code,
        path_suffix="/patient/documents",
        records=records,
        in_progress=in_progress,
        selected=values["type"],
        values=values,
        filters_active=scoped.active,
        result_count=total_records + total_in_progress,
        page=page,
        pages=pages,
        in_progress_total=total_in_progress,
        # the list's address for page `n`, every filter kept
        page_href=lambda n: _list_url(loc, {**values, "page": str(n) if n > 1 else ""}),
        ignored=[translate(key, loc) for key in (problems or {}).values()],
        type_options=_type_options(loc),
        status_options=_status_options(loc),
        # The type tabs keep every other filter the patient has set.
        tab_href=lambda type_value: _list_url(loc, {**values, "type": type_value}),
        reset_href=f"/{loc}/patient/documents",
        # A document being read finishes by itself: the page follows it.
        auto_refresh=any(item.status == "processing" for item in in_progress),
        page_error=page_error,
        notice=notice,
        **_ctx(loc, "documents.title"),
    )


@router.get("/{locale}/patient/documents", response_class=HTMLResponse)
async def list_page(
    request: Request,
    locale: str,
    session: SessionDep,
    actor: OptionalActorDep,
    type: str = "",
    status: str = "",
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    saved: str = "",
    page: str = "",
) -> Response:
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    filters, problems = doc_filters.parse(
        query=q, document_type=type, status=status, date_from=date_from, date_to=date_to
    )
    return await _render_list(
        request,
        loc,
        session,
        actor,
        filters=filters,
        problems=problems,
        page=int(page) if page.isdigit() and len(page) < 9 else 1,
        notice={
            "confirmed": translate("documents.notice.saved", loc),
            "cancelled": translate("documents.notice.cancelled", loc),
            "deleted": translate("documents.notice.deleted", loc),
        }.get(saved),
    )


# ── Detail: processing / failed / review / finalized ─────────────────────
def _review_field(
    loc: str, name: str, value: str, proposed: str, confidence: float | None, *, medicine: bool
) -> dict[str, Any]:
    return {
        "name": name,
        "label": translate(f"documents.field.{name}", loc),
        "value": value,
        "proposed": proposed,
        "confidence": confidence,
        # Suggestions only, never auto-applied: a possible reading of the medicine name
        # shown BESIDE the patient's own editable text, never instead of it (see
        # `documents/drug_names.py`). Only the medicine field — this is specifically about
        # look-alike/sound-alike DRUG names, not dosage/frequency/duration.
        "suggestions": drug_names.suggest(proposed) if medicine else [],
    }


def _review_groups(
    loc: str,
    detail: DocumentDetailOut,
    submitted: dict[int, dict[str, str]] | None,
    removed: set[int],
) -> list[dict[str, Any]]:
    """View-model for the review form: one entry per TABLE of the document (medicines,
    diagnosis, symptoms, vital signs, allergies, lab results, vaccine doses), each with its
    proposed rows — the values the patient just submitted laid over them after a failed
    save — and one blank row to add a value the OCR missed. A table of layout "list" (the
    vital signs) is a single row whose every field is always shown, so any vital can be
    filled in."""
    submitted = submitted or {}
    views: list[dict[str, Any]] = []
    for group in detail.groups:
        spec = groups.BY_KEY[group.key]
        # The blank row takes the next index in THIS table's own block.
        next_index = max((r.index for r in group.rows), default=spec.index_base - 1) + 1
        rows: list[dict[str, Any]] = []
        for r in group.rows:
            fields = [
                _review_field(
                    loc,
                    f.name,
                    submitted.get(r.index, {}).get(f.name, f.value),
                    f.extracted_value or "",
                    f.confidence,
                    medicine=f.name == "medicine",
                )
                for f in r.fields
            ]
            rows.append(
                {
                    "index": r.index,
                    "removed": r.index in removed,
                    "blank": False,
                    "fields": fields,
                    # A lab row's read-only flag (see `documents/lab_flags.py`).
                    "flag": r.flag,
                    "flag_source": r.flag_source,
                }
            )

        blank = {
            "index": next_index,
            "removed": False,
            "blank": True,
            "flag": None,
            "flag_source": None,
            "fields": [
                _review_field(
                    loc,
                    n,
                    submitted.get(next_index, {}).get(n, ""),
                    "",
                    None,  # a row the patient is adding themselves has no confidence
                    medicine=False,
                )
                for n in group.field_names
            ],
        }
        # A "list" table is one row: the proposed one, or — for a document read before
        # that table existed — the blank one, which Confirm & Save then creates.
        list_row = (rows[0] if rows else blank) if group.layout == "list" else None
        views.append(
            {
                "key": group.key,
                "label": translate(f"documents.group.{group.key}", loc),
                "hint": translate(f"documents.group.{group.key}.hint", loc)
                if group.key == groups.ALLERGIES.key
                else "",
                "field_names": group.field_names,
                "layout": group.layout,
                # Whether the document actually has something in this table (a vital-signs
                # row always exists, found or not): an "other" document shows only these.
                "has_content": any(f["value"] for r in rows for f in r["fields"]),
                "rows": rows if group.layout == "table" else [],
                "blank_row": blank if group.layout == "table" else None,
                "list_row": list_row,
            }
        )
    return views


async def _render_detail(
    request: Request,
    loc: str,
    session: SessionDep,
    actor: Actor,
    document_id: uuid.UUID,
    *,
    status_code: int = 200,
    submitted: dict[int, dict[str, str]] | None = None,
    removed: set[int] | None = None,
    date_value: str | None = None,
    errors: dict[str, str] | None = None,
    banner: str | None = None,
    just_saved: bool = False,
    saved_notes: list[str] | None = None,
    records_failed: bool = False,
) -> Response:
    try:
        detail = await doc_service.get_detail(session, actor, document_id)
    except NotFound:
        return await _render_list(
            request,
            loc,
            session,
            actor,
            status_code=404,
            page_error=translate("errors.not_found", loc),
        )

    review_groups = _review_groups(loc, detail, submitted, removed or set())
    ctx = _ctx(loc, "documents.detail.title")
    if detail.status == "awaiting_review":
        # The topbar title becomes type-specific only for the review step
        # itself ("Review extracted prescription" / "... lab report"); every
        # other status keeps the generic "Medical document" title.
        ctx["page_title"] = translate(f"documents.review.title.{detail.document_type}", loc)
    return _render(
        request,
        "documents/detail.html",
        loc,
        status_code=status_code,
        path_suffix=f"/patient/documents/{document_id}",
        doc=detail,
        review_groups=review_groups,
        date_value=(
            date_value
            if date_value is not None
            else (detail.document_date.isoformat() if detail.document_date else "")
        ),
        errors=errors or {},
        banner=banner,
        just_saved=just_saved,
        saved_notes=saved_notes or [],
        records_failed=records_failed,
        can_preview_image=(detail.mime_type or "").startswith("image/"),
        processing=detail.status in ("uploaded", "processing"),
        **ctx,
    )


@router.get("/{locale}/patient/documents/{document_id}", response_class=HTMLResponse)
async def detail_page(
    request: Request,
    locale: str,
    document_id: str,
    session: SessionDep,
    actor: OptionalActorDep,
    saved: str = "",
    allergies: str = "",
    skipped: str = "",
    meds: str = "",
    medskipped: str = "",
    records: str = "",
    rx: str = "",
    dx: str = "",
    vt: str = "",
    lab: str = "",
    labskip: str = "",
) -> Response:
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    parsed = _parse_id(document_id)
    if parsed is None:
        return await _render_list(
            request,
            loc,
            session,
            actor,
            status_code=404,
            page_error=translate("errors.not_found", loc),
        )
    notes: list[str] = []
    records_failed = False
    if saved == "1":
        if n := _count(allergies):
            notes.append(translate("documents.saved.allergies_added", loc, count=n))
        if n := _count(skipped):
            notes.append(translate("documents.saved.allergies_skipped", loc, count=n))
        if n := _count(meds):
            notes.append(translate("documents.saved.medicines_added", loc, count=n))
        if n := _count(medskipped):
            notes.append(translate("documents.saved.medicines_skipped", loc, count=n))
        if records == "failed":
            records_failed = True
            notes.append(translate("documents.saved.records_failed", loc))
        else:
            parts = (("items", rx), ("diagnoses", dx), ("vitals", vt), ("labs", lab))
            summary = ", ".join(
                translate(f"documents.saved.records_part.{key}", loc, count=_count(value))
                for key, value in parts
                if _count(value)
            )
            if summary:
                notes.append(translate("documents.saved.records_added", loc, summary=summary))
            if n := _count(labskip):
                notes.append(translate("documents.saved.records_lab_skipped", loc, count=n))
    return await _render_detail(
        request,
        loc,
        session,
        actor,
        parsed,
        just_saved=saved == "1",
        saved_notes=notes,
        records_failed=records_failed,
    )


@router.get("/{locale}/patient/documents/{document_id}/file")
async def original_file(
    request: Request,
    locale: str,
    document_id: str,
    session: SessionDep,
    actor: OptionalActorDep,
    download: str = "",
) -> Response:
    """The original upload, exactly as stored — shown (`inline`) or saved (`?download=1`)."""
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    parsed = _parse_id(document_id)

    async def problem(code: int, key: str) -> Response:
        return await _render_list(
            request, loc, session, actor, status_code=code, page_error=translate(key, loc)
        )

    if parsed is None:
        return await problem(404, "errors.file_not_found")
    try:
        data, mime, filename = await doc_service.get_file(session, actor, parsed)
    except NotFound:
        # one answer for a foreign id, an unknown id, a deleted document and a lost file
        return await problem(404, "errors.file_not_found")
    except FileUnavailable:
        return await problem(502, "errors.file_unavailable")
    return file_response(data, mime, filename, download=download == "1")


# ── Preprocessing report: what the reader receives, before any text ──────
@router.get("/{locale}/patient/documents/{document_id}/preprocess", response_class=HTMLResponse)
async def preprocess_page(
    request: Request,
    locale: str,
    document_id: str,
    session: SessionDep,
    actor: OptionalActorDep,
) -> Response:
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    parsed = _parse_id(document_id)

    async def not_found() -> Response:
        return await _render_list(
            request,
            loc,
            session,
            actor,
            status_code=404,
            page_error=translate("errors.not_found", loc),
        )

    if parsed is None:
        return await not_found()
    try:
        doc, report, error_code = await doc_service.preprocess_report(session, actor, parsed)
    except NotFound:
        return await not_found()
    return _render(
        request,
        "documents/preprocess.html",
        loc,
        path_suffix=f"/patient/documents/{parsed}/preprocess",
        doc=doc_service.to_out(doc, actor.user_id),
        report=report,
        error_text=_failure_text(loc, error_code) if error_code else None,
        note_text=lambda note: translate(
            f"documents.preprocess.note.{note.code}", loc, **note.params
        ),
        **_ctx(loc, "documents.preprocess.title"),
    )


# ── Actions: retry, Confirm & Save, cancel ───────────────────────────────
def _redirect_detail(loc: str, document_id: uuid.UUID) -> Response:
    return RedirectResponse(f"/{loc}/patient/documents/{document_id}", status_code=303)


async def _action_prologue(
    request: Request, locale: str, document_id: str, actor: Actor | None
) -> tuple[Response | None, str, uuid.UUID | None]:
    redirect = _guard(request, locale, actor)
    loc = normalise_locale(locale)
    return redirect, loc, _parse_id(document_id)


@router.post("/{locale}/patient/documents/{document_id}/process")
async def retry_process(
    request: Request, locale: str, document_id: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    redirect, loc, parsed = await _action_prologue(request, locale, document_id, actor)
    if redirect is not None:
        return redirect
    assert actor is not None
    if parsed is None:
        return _redirect_detail(loc, uuid.UUID(int=0))
    try:
        await check_document_write_rate(actor)
        await doc_service.process(
            session,
            actor,
            parsed,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except RateLimited:
        return await _render_detail(
            request,
            loc,
            session,
            actor,
            parsed,
            status_code=429,
            banner=translate("errors.rate_limited", loc),
        )
    except (ProcessingInProgress, InvalidDocumentState, NotFound):
        pass  # the detail page shows the document's real state
    return _redirect_detail(loc, parsed)


def _count(value: str) -> int:
    """A count carried in the address after a save: a plain number or nothing (never trusted
    for anything but showing it)."""
    return int(value) if value.isdigit() and int(value) < 10_000 else 0


def _records_query(outcome: Any) -> str:
    """What was copied into the typed health records, as address parameters (counts only)."""
    if outcome.failed:
        return "&records=failed"
    query = ""
    for key, count in (
        ("rx", outcome.prescription_items),
        ("dx", outcome.diagnoses),
        ("vt", outcome.vital_measurements),
        ("lab", outcome.lab_results),
        ("labskip", outcome.lab_skipped),
    ):
        if count:
            query += f"&{key}={count}"
    return query


def _parse_review_form(
    form: Any, names: frozenset[str] | tuple[str, ...]
) -> tuple[dict[int, dict[str, str]], set[int]]:
    submitted: dict[int, dict[str, str]] = {}
    for key, value in form.multi_items():
        m = _ROW_FIELD.match(key)
        if m and m.group(2) in names:
            submitted.setdefault(int(m.group(1)), {})[m.group(2)] = str(value)
    removed = {int(v) for v in form.getlist("remove_row") if str(v).isdigit()}
    return submitted, removed


@router.post("/{locale}/patient/documents/{document_id}/confirm")
async def confirm_submit(
    request: Request, locale: str, document_id: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    redirect, loc, parsed = await _action_prologue(request, locale, document_id, actor)
    if redirect is not None:
        return redirect
    assert actor is not None
    if parsed is None:
        return _redirect_detail(loc, uuid.UUID(int=0))

    try:
        doc = await doc_service.get_document(session, actor, parsed)
    except NotFound:
        return _redirect_detail(loc, parsed)
    names = groups.fields_for(doc.doc_type)

    form = await request.form()
    submitted, removed = _parse_review_form(form, names)
    raw_date = str(form.get("document_date", "")).strip()

    def failure(code: int, errors: dict[str, str], banner: str | None = None) -> Any:
        return _render_detail(
            request,
            loc,
            session,
            actor,
            parsed,
            status_code=code,
            submitted=submitted,
            removed=removed,
            date_value=raw_date,
            errors=errors,
            banner=banner,
        )

    parsed_date: date | None = None
    if raw_date:
        try:
            parsed_date = date.fromisoformat(raw_date)
        except ValueError:
            return await failure(422, {"document_date": translate("errors.date_invalid", loc)})

    body = ConfirmRequest(
        rows=[
            ReviewedRow(index=i, fields=dict(fields))
            for i, fields in submitted.items()
            if i not in removed
        ],
        document_date=parsed_date,
        # The page shows every table of the document, so every one was reviewed.
        groups=[spec.key for spec in groups.groups_for(doc.doc_type)],
        # The medicines the patient ticked "add to my current medicines" on.
        current_medicines=[
            int(v)
            for v in form.getlist("current_medicine")[:100]
            if str(v).isdigit() and int(v) <= 10_000
        ],
    )
    outcome = doc_service.ConfirmOutcome()
    try:
        await check_document_write_rate(actor)
        _doc, outcome = await doc_service.confirm_with_outcome(
            session,
            actor,
            parsed,
            body,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except RateLimited:
        return await failure(429, {}, translate("errors.rate_limited", loc))
    except ValidationFailed as exc:
        await session.rollback()
        fields = exc.details.get("fields", {})
        errors = {k: translate(v, loc) for k, v in fields.items() if k != "rows"}
        banner = translate(fields["rows"], loc) if "rows" in fields else None
        return await failure(422, errors, banner)
    except (InvalidDocumentState, NotFound):
        return _redirect_detail(loc, parsed)

    query = "saved=1"
    if outcome.allergies_added:
        query += f"&allergies={outcome.allergies_added}"
    if outcome.allergies_skipped:
        query += f"&skipped={outcome.allergies_skipped}"
    if outcome.medicines_added:
        query += f"&meds={outcome.medicines_added}"
    if outcome.medicines_skipped:
        query += f"&medskipped={outcome.medicines_skipped}"
    query += _records_query(outcome.records)
    return RedirectResponse(f"/{loc}/patient/documents/{parsed}?{query}", status_code=303)


@router.post("/{locale}/patient/documents/{document_id}/records")
async def add_to_records_submit(
    request: Request, locale: str, document_id: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    """Copy a saved document's confirmed values into the health records again — the way back
    when that failed right after the save. Harmless to press twice."""
    redirect, loc, parsed = await _action_prologue(request, locale, document_id, actor)
    if redirect is not None:
        return redirect
    assert actor is not None
    if parsed is None:
        return _redirect_detail(loc, uuid.UUID(int=0))
    try:
        await check_document_write_rate(actor)
        written = await doc_service.add_to_records(
            session,
            actor,
            parsed,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except RateLimited:
        return await _render_detail(
            request,
            loc,
            session,
            actor,
            parsed,
            status_code=429,
            banner=translate("errors.rate_limited", loc),
        )
    except (InvalidDocumentState, NotFound):
        return _redirect_detail(loc, parsed)
    return RedirectResponse(
        f"/{loc}/patient/documents/{parsed}?saved=1{_records_query(written)}", status_code=303
    )


@router.post("/{locale}/patient/documents/{document_id}/cancel")
async def cancel_submit(
    request: Request, locale: str, document_id: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    redirect, loc, parsed = await _action_prologue(request, locale, document_id, actor)
    if redirect is not None:
        return redirect
    assert actor is not None
    if parsed is None:
        return RedirectResponse(f"/{loc}/patient/documents", status_code=303)
    try:
        await check_document_write_rate(actor)
        await doc_service.cancel(
            session,
            actor,
            parsed,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except RateLimited:
        return await _render_detail(
            request,
            loc,
            session,
            actor,
            parsed,
            status_code=429,
            banner=translate("errors.rate_limited", loc),
        )
    except InvalidDocumentState:
        return _redirect_detail(loc, parsed)  # a finalized record cannot be discarded
    except DeleteFailed as exc:
        # Nothing was discarded (the file could not be removed): say so, and leave it as it was.
        return await _render_detail(
            request,
            loc,
            session,
            actor,
            parsed,
            status_code=exc.http_status,
            banner=_error_text(loc, exc),
        )
    except NotFound:
        pass
    return RedirectResponse(f"/{loc}/patient/documents?saved=cancelled", status_code=303)


# ── Delete (with an explicit confirmation step) ──────────────────────────
async def _render_delete(
    request: Request,
    loc: str,
    session: SessionDep,
    actor: Actor,
    document_id: uuid.UUID,
    *,
    status_code: int = 200,
    banner: str | None = None,
    errors: dict[str, str] | None = None,
) -> Response:
    """The page that asks "are you sure?" — the same step with or without JavaScript (the
    scripted dialog on the list and detail pages submits to the same action)."""
    try:
        doc = doc_service.to_out(
            await doc_service.get_document(session, actor, document_id), actor.user_id
        )
    except NotFound:
        return await _render_list(
            request,
            loc,
            session,
            actor,
            status_code=404,
            page_error=translate("errors.not_found", loc),
        )
    if doc.status == "cancelled":  # already gone: nothing to confirm
        return RedirectResponse(f"/{loc}/patient/documents?saved=deleted", status_code=303)
    if not doc.can_delete:
        return await _render_detail(
            request,
            loc,
            session,
            actor,
            document_id,
            status_code=403,
            banner=translate("errors.document_not_deletable", loc),
        )
    return _render(
        request,
        "documents/delete.html",
        loc,
        status_code=status_code,
        path_suffix=f"/patient/documents/{document_id}/delete",
        doc=doc,
        banner=banner,
        errors=errors or {},
        **_ctx(loc, "documents.delete.title"),
    )


@router.get("/{locale}/patient/documents/{document_id}/delete", response_class=HTMLResponse)
async def delete_page(
    request: Request, locale: str, document_id: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    redirect, loc, parsed = await _action_prologue(request, locale, document_id, actor)
    if redirect is not None:
        return redirect
    assert actor is not None
    if parsed is None:
        return RedirectResponse(f"/{loc}/patient/documents", status_code=303)
    return await _render_delete(request, loc, session, actor, parsed)


@router.post("/{locale}/patient/documents/{document_id}/delete")
async def delete_submit(
    request: Request, locale: str, document_id: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    redirect, loc, parsed = await _action_prologue(request, locale, document_id, actor)
    if redirect is not None:
        return redirect
    assert actor is not None
    if parsed is None:
        return RedirectResponse(f"/{loc}/patient/documents", status_code=303)
    form = await request.form()
    confirmed = str(form.get("confirm", "")) == "1"
    try:
        await check_document_write_rate(actor)
        await doc_service.delete_document(
            session,
            actor,
            parsed,
            confirmed=confirmed,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except RateLimited:
        return await _render_delete(
            request,
            loc,
            session,
            actor,
            parsed,
            status_code=429,
            banner=translate("errors.rate_limited", loc),
        )
    except ValidationFailed as exc:
        await session.rollback()
        key = exc.details.get("fields", {}).get("confirm", "errors.delete_confirm_required")
        return await _render_delete(
            request,
            loc,
            session,
            actor,
            parsed,
            status_code=422,
            errors={"confirm": translate(key, loc)},
        )
    except DeleteFailed as exc:
        # Nothing was reported as deleted: say so, and offer the same step again.
        return await _render_delete(
            request,
            loc,
            session,
            actor,
            parsed,
            status_code=exc.http_status,
            banner=_error_text(loc, exc),
        )
    except DocumentNotDeletable:
        return await _render_detail(
            request,
            loc,
            session,
            actor,
            parsed,
            status_code=403,
            banner=translate("errors.document_not_deletable", loc),
        )
    except InvalidDocumentState:
        return _redirect_detail(loc, parsed)  # it changed state meanwhile: show how it is now
    except NotFound:
        return await _render_list(
            request,
            loc,
            session,
            actor,
            status_code=404,
            page_error=translate("errors.not_found", loc),
        )
    return RedirectResponse(f"/{loc}/patient/documents?saved=deleted", status_code=303)
