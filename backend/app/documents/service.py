"""Medical-document business logic — the one place every rule lives.

The JSON API (`router.py`) and the server-rendered pages (`web/documents.py`)
both call these functions, so no rule is implemented twice and no document type
gets its own pipeline.

Lifecycle (shared by all four types):

    uploaded -> processing -> awaiting_review -> finalized
                     \\-> failed (retry -> processing)      \\-> cancelled

Only `finalized` is a medical record. Every list of records reads
`status == 'finalized'`, so an upload that has not been confirmed appears in no
record screen and would appear on no Timeline.

Scoping is the whole game here, exactly as in the patient profile: the app
connects to Postgres with BYPASSRLS, so RLS does NOT protect these queries.
Every lookup filters by the caller's own `patient_id` in application code, and a
foreign, nonexistent, or inaccessible id all surface as the same `NotFound`.
Ownership always comes from the authenticated session — no patient id is ever
accepted from a client.

State transitions that must not race (claiming a document for processing,
confirming it, cancelling it) are single conditional UPDATEs, so a double-click
or a concurrent cancel cannot start OCR twice or resurrect a cancelled upload.

Nothing here logs OCR text or extracted values — they are clinical free text.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import Select, delete, func, select, update
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from .. import cache as app_cache
from ..audit import writer as audit
from ..db import types as dbtypes
from ..db.models import (
    DOC_TYPE_LAB,
    DOC_TYPE_OTHER,
    DOC_TYPE_RX,
    DOC_TYPES,
    IN_PROGRESS_STATUSES,
    OCR_MODE_PRINTED,
    Doctor,
    Document,
    DocumentStatus,
    OcrExtraction,
    OcrJob,
    Patient,
)
from ..db.sqlfuncs import pk_date
from ..db.types import utcnow, uuid7
from ..deps import Actor, remember_patient_id, remembered_patient_id
from ..errors import (
    DeleteFailed,
    DocumentNotDeletable,
    DuplicateEntry,
    EmptyFile,
    FileTooLarge,
    FileUnavailable,
    InvalidDocumentState,
    ListFull,
    NotFound,
    ProcessingInProgress,
    StorageUnavailable,
    UnsupportedFileType,
    ValidationFailed,
)
from ..log_config import get_logger
from ..profile import service as profile_service
from ..profile.schemas import AllergyCreate, MedicationCreate
from ..settings import settings
from . import (
    ai_sections,
    ai_understanding,
    extractors,
    groups,
    lab_flags,
    normalization,
    ocr,
    proposals,
    records,
    sections,
    storage,
    vaccines,
)
from . import (
    filters as doc_filters,
)
from .schemas import (
    API_TO_DB_TYPE,
    DB_TO_API_TYPE,
    ConfirmRequest,
    DocumentDetailOut,
    DocumentOut,
    DocumentSectionsOut,
    FieldOut,
    GroupOut,
    QualityOut,
    RowOut,
    SectionEntry,
    SectionItem,
    SectionTopic,
)

log = get_logger()

# The most rows one Confirm & Save may carry, all tables together.
MAX_CONFIRM_ROWS = 250

ACTOR_ROLE = "patient"
RESOURCE = "document"
# Failure codes that are not OCR-engine errors (see `ocr.py` for those).
STORAGE_FAILED = "storage_failed"
EXTRACTION_FAILED = "extraction_failed"

# Where a document entered the pipeline from — the one provenance mechanism
# both flows share, never duplicated. `create_upload` (patient-uploaded
# image/PDF -> OCR -> extraction -> normalization -> confidence -> patient
# review) always means SOURCE_PATIENT_UPLOAD. `create_doctor_prescription`
# (a verified doctor's already-structured prescription -> the SAME
# normalization + confidence stage, skipping OCR entirely since there is
# nothing to read -> automatic validation -> immediately finalized, no
# patient review) always means SOURCE_DOCTOR_GENERATED. No new column
# stores this: `_document_source` derives it from `Document.uploaded_by`,
# which was already being recorded for every document either way.
SOURCE_PATIENT_UPLOAD = "patient_upload"
SOURCE_DOCTOR_GENERATED = "doctor_generated"
# `ocr_job.engine` for a doctor-authored prescription: no OCR engine ran, so
# 'tesseract' would not be true. Allowed by the
# `ocr_job_engine_check` CHECK constraint as of migration 35c4bc13fa37.
ENGINE_DOCTOR_ENTRY = "doctor_entry"

_MIME_ALIASES = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg"}
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


# ── Small pure helpers ───────────────────────────────────────────────────
def parse_document_type(value: str | None) -> str:
    """The API type -> the database `doc_type`, or a field-level 422."""
    db_type = API_TO_DB_TYPE.get((value or "").strip())
    if db_type is None or db_type not in DOC_TYPES:
        raise ValidationFailed({"fields": {"document_type": "errors.document_type_invalid"}})
    return db_type


def validate_upload(data: bytes, declared_mime: str | None) -> tuple[str, str]:
    """Server-side file validation. Returns `(mime_type, extension)`.

    The file's own bytes decide what it is — the filename and the client's
    Content-Type are only cross-checked, never trusted. A renamed executable
    fails on its bytes; a PNG sent as `application/pdf` fails on the mismatch.
    """
    if not data:
        raise EmptyFile()
    if len(data) > settings.document_max_bytes:
        raise FileTooLarge(settings.document_max_bytes // (1024 * 1024))
    sniffed = storage.sniff(data)
    if sniffed is None:
        raise UnsupportedFileType()
    mime, ext = sniffed
    declared = (declared_mime or "").split(";")[0].strip().lower()
    declared = _MIME_ALIASES.get(declared, declared)
    # A browser may send a generic type, but a *specific* one must agree with
    # the bytes: a PNG declared as `application/pdf` is refused, not trusted.
    is_specific = bool(declared) and declared != "application/octet-stream"
    if is_specific and (declared not in storage.ALLOWED_MIME_TYPES or declared != mime):
        raise UnsupportedFileType()
    return mime, ext


def safe_filename(name: str | None, extension: str) -> str:
    """Display-only. Never used to build a storage path."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = _CONTROL_CHARS.sub("", base).strip()[:255]
    return base or f"document.{extension}"


def clean_value(value: str | None) -> str:
    """A reviewed field value: control characters dropped, whitespace collapsed."""
    if value is None:
        return ""
    return " ".join(_CONTROL_CHARS.sub(" ", value).split())


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive timestamps; treat them as UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _decimal(value: float | None) -> Decimal | None:
    """A `normalization` certainty weight (a plain float — see its own
    module docstring on why it is a category, not a calibrated score) as
    the `Numeric(4, 3)` `OcrExtraction.confidence` / `OcrJob.overall_confidence`
    columns already expect. `str()` first so the binary float never
    introduces spurious trailing digits a `Decimal` would otherwise keep."""
    return None if value is None else Decimal(str(value))


# ── Ownership ────────────────────────────────────────────────────────────
async def _patient_id(session: AsyncSession, actor: Actor) -> uuid.UUID:
    """The caller's own patient id — from their verified user id, looked up once per request
    (the actor loader already did it; see `deps.remember_patient_id`)."""
    known = remembered_patient_id(session, actor.user_id)
    if known is not None:
        return known
    pid = (
        await session.execute(select(Patient.id).where(Patient.user_id == actor.user_id))
    ).scalar_one_or_none()
    if pid is None:
        raise NotFound()
    remember_patient_id(session, actor.user_id, pid)
    return pid


async def _owned(session: AsyncSession, actor: Actor, document_id: uuid.UUID) -> Document:
    """The caller's own document, or `NotFound` — identical for a foreign id,
    a nonexistent one, and one that has no patient record behind it."""
    pid = await _patient_id(session, actor)
    doc = (
        await session.execute(
            select(Document).where(Document.id == document_id, Document.patient_id == pid)
        )
    ).scalar_one_or_none()
    if doc is None:
        raise NotFound()
    return doc


async def _job(session: AsyncSession, document_id: uuid.UUID) -> OcrJob | None:
    return (
        await session.execute(select(OcrJob).where(OcrJob.document_id == document_id))
    ).scalar_one_or_none()


_SECTION_STATUSES = (DocumentStatus.AWAITING_REVIEW.value, DocumentStatus.FINALIZED.value)


async def _stored_ai_sections(session: AsyncSession, doc: Document) -> dict | None:
    """The AI reading of this document's sections stored with its OCR job, or
    `None`. Sections are shown while a document awaits review and once it is
    finalized, so nothing is fetched for any other status."""
    if doc.status not in _SECTION_STATUSES:
        return None
    job = await _job(session, doc.id)
    return job.ai_sections if job is not None else None


# ── Photo quality (advice, never a gate) ─────────────────────────────────
# Whether the original is clear enough to read well is measured from the file
# (`ocr.assess_quality`) once, when the document is read, and kept in the TTL cache — a pure
# optimisation: nothing is stored in the database, and a verdict that is missing (a restart, a
# second worker) is simply measured again from the stored original the next time a page asks.
_QUALITY_STATUSES = (
    DocumentStatus.UPLOADED.value,
    DocumentStatus.AWAITING_REVIEW.value,
    DocumentStatus.FAILED.value,
)
_QUALITY_TTL_S = 6 * 60 * 60
# How long "the store could not be reached" is remembered, so a page viewed again at once does
# not try (and wait on) the store again.
_QUALITY_RETRY_S = 30


def _quality_key(document_id: uuid.UUID) -> str:
    return f"doc_quality:{document_id}"


async def remember_quality(doc: Document, data: bytes) -> ocr.QualityReport | None:
    """Measure `data` (the original) and keep the verdict. A file that cannot be measured is
    remembered as "not judged" so it is not decoded again on every page view. Never raises:
    advice about the photo must not be able to fail an upload or a read."""
    try:
        try:
            report: ocr.QualityReport | None = await ocr.assess_quality(data, doc.mime_type or "")
        except Exception:  # noqa: BLE001 - not an image we can measure: simply not judged
            report = None
        payload = (report or ocr.QualityReport()).to_dict()
        await app_cache.cache.set(_quality_key(doc.id), payload, _QUALITY_TTL_S)
        return report
    except Exception:  # noqa: BLE001
        log.warning("document_quality_unavailable")
        return None


async def quality_of(doc: Document) -> ocr.QualityReport | None:
    """The photo-quality verdict for an unconfirmed document, or `None` when there is none to
    give (a saved record, a file that cannot be measured, or a store that cannot be reached
    right now — the page just shows no advice)."""
    if doc.status not in _QUALITY_STATUSES:
        return None
    try:
        cached = await app_cache.cache.get(_quality_key(doc.id))
    except Exception:  # noqa: BLE001
        cached = None
    if cached is not None:
        if cached.get("unavailable"):
            return None
        return ocr.QualityReport.from_dict(cached)
    try:
        data = await storage.get(doc.object_key)
    except Exception:  # noqa: BLE001
        with contextlib.suppress(Exception):
            await app_cache.cache.set(_quality_key(doc.id), {"unavailable": True}, _QUALITY_RETRY_S)
        return None
    return await remember_quality(doc, data)


def _quality_out(report: ocr.QualityReport | None) -> QualityOut | None:
    if report is None or not report.measured:
        return None
    return QualityOut(
        issues=list(report.issues), sharpness=report.sharpness, contrast=report.contrast
    )


# ── Upload ───────────────────────────────────────────────────────────────
async def create_upload(
    session: AsyncSession,
    actor: Actor,
    *,
    document_type: str | None,
    filename: str | None,
    declared_mime: str | None,
    data: bytes,
    source: str = SOURCE_PATIENT_UPLOAD,
    ip: str | None = None,
    user_agent: str | None = None,
) -> Document:
    """Validate, store the original, and record a TEMPORARY document.

    Nothing is finalized: the row starts at `uploaded` and is invisible to every
    record list until the patient confirms it. `source` is provenance only
    (recorded in the audit trail, see `SOURCE_PATIENT_UPLOAD` above) — it
    does not change validation, storage, or the OCR/extraction pipeline.
    """
    doc_type = parse_document_type(document_type)
    mime, ext = validate_upload(data, declared_mime)
    pid = await _patient_id(session, actor)

    document_id = uuid7()
    key = storage.object_key(pid, document_id, ext)
    try:
        await storage.put(key, data, mime)
    except Exception as exc:
        log.warning("document_storage_put_failed", **storage.describe_failure(exc))
        raise StorageUnavailable() from exc

    doc = Document(
        id=document_id,
        patient_id=pid,
        doc_type=doc_type,
        ocr_mode=OCR_MODE_PRINTED,
        object_key=key,
        mime_type=mime,
        size_bytes=len(data),
        uploaded_by=actor.user_id,
        original_filename=safe_filename(filename, ext),
        status=DocumentStatus.UPLOADED.value,
    )
    session.add(doc)
    try:
        await audit.write(
            session,
            action=audit.DOCUMENT_UPLOAD,
            actor_user_id=actor.user_id,
            actor_role=ACTOR_ROLE,
            subject_patient_id=pid,
            resource_type=RESOURCE,
            ip_address=ip,
            user_agent=user_agent,
            detail={
                "document_id": str(document_id),
                "doc_type": doc_type,
                "size_bytes": len(data),
                "source": source,
            },
        )
        await session.commit()
    except Exception:
        # The row never landed, so the stored file must not be left orphaned.
        await session.rollback()
        await storage.delete(key)
        raise
    return doc


# ── Processing (the common pipeline) ─────────────────────────────────────
async def _owned_or_known(
    session: AsyncSession, actor: Actor, document_id: uuid.UUID, known: Document | None
) -> Document:
    """`_owned`, minus the re-read when the caller is holding the document it created a moment
    ago in this same request (`create_upload` just inserted it; reading it straight back is a
    network round trip for nothing). Ownership is still checked — same id, and the caller's own
    patient (already known, no query) — and a stale status cannot matter: every state change that
    follows is a conditional UPDATE on the status the row is expected to have."""
    if (
        known is not None
        and not sa_inspect(known).expired_attributes  # a session that expires at commit: read it
        and known.id == document_id
        and known.patient_id == await _patient_id(session, actor)
    ):
        return known
    return await _owned(session, actor, document_id)


async def _bring_up_to_date(session: AsyncSession, doc: Document, **changed: object) -> None:
    """Make `doc` show what a Core UPDATE (which bypasses the ORM) just wrote to its row, without
    reading the row back — each read is a network round trip. Done with `set_committed_value`,
    so `doc` is not marked dirty and nothing is flushed. If the session threw its copy away at
    commit (`expire_on_commit`), read it back instead: there is nothing to patch."""
    if sa_inspect(doc).expired_attributes:
        await session.refresh(doc)
        return
    for name, value in changed.items():
        set_committed_value(doc, name, value)


async def _claim_for_processing(session: AsyncSession, doc: Document) -> None:
    """Atomically move a document to `processing`. Losing the race means
    another request already holds it, so OCR is never started twice. Only
    printed documents are read now, so a document that an earlier version had
    marked handwritten is simply read as printed on its retry."""
    result = await session.execute(
        update(Document)
        .where(Document.id == doc.id, Document.status == doc.status)
        .values(
            status=DocumentStatus.PROCESSING.value,
            failure_code=None,
            ocr_mode=OCR_MODE_PRINTED,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await session.rollback()
        raise ProcessingInProgress()


async def _reset_job(session: AsyncSession, doc: Document) -> OcrJob:
    """One OCR job per document (UNIQUE in the shared schema): a retry resets
    it and discards the previous attempt's proposals."""
    now = utcnow()
    job = await _job(session, doc.id)
    if job is None:
        job = OcrJob(
            id=uuid7(),
            document_id=doc.id,
            status="running",
            engine=ocr.ENGINE_TESSERACT,
            retry_count=0,
            started_at=now,
        )
        session.add(job)
    else:
        await session.execute(delete(OcrExtraction).where(OcrExtraction.job_id == job.id))
        job.status = "running"
        job.engine = ocr.ENGINE_TESSERACT
        job.retry_count += 1
        job.started_at = now
        job.finished_at = None
        job.overall_confidence = None
        job.ai_sections = None
    return job


async def _fail(
    session: AsyncSession,
    actor: Actor,
    doc: Document,
    job: OcrJob,
    code: str,
    *,
    ocr_text: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> Document:
    """Mark a document `failed` — but only if it is still ours (`processing`);
    a concurrent cancel wins."""
    now = utcnow()
    result = await session.execute(
        update(Document)
        .where(Document.id == doc.id, Document.status == DocumentStatus.PROCESSING.value)
        .values(status=DocumentStatus.FAILED.value, failure_code=code, ocr_text=ocr_text)
        .execution_options(synchronize_session=False)
    )
    job.status = "failed"
    job.finished_at = now
    if result.rowcount == 1:
        await audit.write(
            session,
            action=audit.DOCUMENT_PROCESS,
            actor_user_id=actor.user_id,
            actor_role=ACTOR_ROLE,
            subject_patient_id=doc.patient_id,
            resource_type=RESOURCE,
            ip_address=ip,
            user_agent=user_agent,
            detail={"document_id": str(doc.id), "outcome": "failed", "code": code},
        )
    await session.commit()
    await session.refresh(doc)
    return doc


async def _apply_ai_understanding(
    doc_type: str, text: str, extraction: extractors.Extraction
) -> extractors.Extraction:
    """Gemini semantic understanding (`ai_understanding.py`) — an OPTIONAL
    swap of the deterministic rows/certainty for Gemini's own reading of
    the SAME OCR text, only when Gemini is configured, succeeds, and passes
    its own safety checks. Falls back to `extraction` completely unchanged
    otherwise (`understand_prescription` never raises). Prescriptions only:
    lab/vaccination/other never had a "medicines" concept to understand.
    Never touches `document_date` — that stays whatever the deterministic
    pass already found."""
    if doc_type != DOC_TYPE_RX:
        return extraction
    understood = await ai_understanding.understand_prescription(text, extraction.rows)
    if understood is None:
        return extraction
    return extractors.Extraction(
        rows=understood.rows,
        document_date=extraction.document_date,
        field_certainty=understood.certainty,
    )


def _propose_extra_tables(
    doc_type: str, text: str, ai_reading: object
) -> list[tuple[groups.GroupSpec, list[dict[str, str]]]]:
    """The proposed rows of the document's other tables (see `proposals.py`). These are
    an extra on top of the medicines / lab results, so any trouble reading them costs
    only themselves — never the upload."""
    try:
        return proposals.extra_rows(doc_type, text, ai_reading)
    except Exception:  # noqa: BLE001
        log.warning("document_extra_tables_failed")
        return []


async def _apply_ai_layers(
    doc_type: str, text: str, extraction: extractors.Extraction
) -> tuple[extractors.Extraction, dict | None]:
    """The two optional AI layers — the medicines reading (`ai_understanding`)
    and the sections reading (`ai_sections`) — run CONCURRENTLY, so a document
    waits for the slower provider call once, not for both in turn. Each returns
    its input unchanged / `None` on any failure (neither raises), so a failed or
    unconfigured layer costs nothing but its own improvement."""
    understood, sections_reading = await asyncio.gather(
        _apply_ai_understanding(doc_type, text, extraction),
        ai_sections.extract(text, doc_type),
    )
    return understood, sections_reading


def _lab_range_counts(text: str) -> tuple[int, int]:
    """`(result rows, rows that have a reference range)` the lab extractor finds in
    `text` — counts only, so nothing clinical is ever logged."""
    rows = extractors.extract_lab(text).rows
    return len(rows), sum(1 for row in rows if row["reference_range"])


async def _read_document(doc_type: str, data: bytes, mime_type: str) -> ocr.OcrResult:
    """The OCR read for a document — one pass, except for a lab report whose first
    read left some results without their reference range.

    Tesseract's default page layout reads a results table column by column, so the
    Reference Range column can come out as a separate block, detached from its rows
    (and a decimal point can be lost on the way — "12.8" read as "128", which would
    flag a normal result High). The table layout keeps each row together. It is NOT
    used for everything — it reads other documents worse — so a second read happens
    only for a lab report that needs it, and is adopted only if it recovers strictly
    more ranges without losing rows. Any trouble with the second read falls back to
    the first; it can never fail an upload."""
    result = await ocr.run_ocr(data, mime_type)
    if doc_type != DOC_TYPE_LAB:
        return result
    try:
        rows, with_range = _lab_range_counts(result.text)
        if rows and with_range == rows:
            return result
        table = await ocr.run_ocr(data, mime_type, layout=ocr.LAYOUT_TABLE)
        table_rows, table_with_range = _lab_range_counts(table.text)
    except Exception:  # noqa: BLE001 - an extra read must never fail the document
        return result
    adopt = (table_with_range > with_range and table_rows >= rows) or (rows == 0 and table_rows > 0)
    log.info(
        "ocr_table_layout_read",
        adopted=adopt,
        rows=rows,
        with_range=with_range,
        table_rows=table_rows,
        table_with_range=table_with_range,
    )
    return table if adopt else result


async def process(
    session: AsyncSession,
    actor: Actor,
    document_id: uuid.UUID,
    *,
    data: bytes | None = None,
    known: Document | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> Document:
    """Upload -> temporary -> common OCR -> type-specific extraction -> review.

    Runs the SAME steps for every document type; only the extractor differs.
    `data` lets a caller that JUST uploaded these exact bytes (`create_upload`,
    a few lines up the call stack) hand them over directly instead of having
    this function immediately re-download what it only just stored — a full
    extra Storage round-trip for nothing. A retry has no such bytes in hand and
    fetches the stored original as before. `known` is the same idea for the document row: the
    one `create_upload` just returned, so it is not read straight back (see `_owned_or_known`).
    Returns the document in `awaiting_review` (or `failed`, with a
    `failure_code` — OCR trouble is a state the patient can retry, not a 500).
    """
    doc = await _owned_or_known(session, actor, document_id, known)

    if doc.status == DocumentStatus.AWAITING_REVIEW.value:
        return doc  # already read; nothing to redo
    if doc.status in (DocumentStatus.FINALIZED.value, DocumentStatus.CANCELLED.value):
        raise InvalidDocumentState()
    if doc.status == DocumentStatus.PROCESSING.value:
        job = await _job(session, doc.id)
        started = _aware(job.started_at) if job else None
        stale = started is None or utcnow() - started > timedelta(
            seconds=settings.document_processing_stale_s
        )
        if not stale:
            raise ProcessingInProgress()

    await _claim_for_processing(session, doc)
    job = await _reset_job(session, doc)
    await session.commit()  # `processing` is now visible to a page refresh
    await _bring_up_to_date(
        session,
        doc,
        status=DocumentStatus.PROCESSING.value,
        failure_code=None,
        ocr_mode=OCR_MODE_PRINTED,
    )

    if data is None:
        try:
            data = await storage.get(doc.object_key)
        except Exception as exc:  # noqa: BLE001
            log.warning("document_storage_get_failed", **storage.describe_failure(exc))
            return await _fail(
                session, actor, doc, job, STORAGE_FAILED, ip=ip, user_agent=user_agent
            )

    # Is this photo clear enough? Advice for the review / failed page; never affects the read.
    # It is measured WHILE the page is read (a second worker thread), not before it: done in
    # sequence it added a quarter to a half to every read, and always finishes before the
    # response so the page that follows already has its verdict.
    judging = asyncio.ensure_future(remember_quality(doc, data))
    try:
        try:
            result = await _read_document(doc.doc_type, data, doc.mime_type or "")
        except ocr.OcrError as exc:
            return await _fail(session, actor, doc, job, exc.code, ip=ip, user_agent=user_agent)
        except Exception:  # noqa: BLE001 - an engine bug is still a retryable failure
            log.warning("document_ocr_unexpected_error")
            return await _fail(
                session, actor, doc, job, ocr.OCR_FAILED, ip=ip, user_agent=user_agent
            )
    finally:
        await judging  # never raises: `remember_quality` swallows everything

    try:
        extraction = extractors.get_extractor(doc.doc_type)(result.text, None)
        extraction, ai_sections_reading = await _apply_ai_layers(
            doc.doc_type, result.text, extraction
        )
        extra_tables = _propose_extra_tables(doc.doc_type, result.text, ai_sections_reading)
    except Exception:  # noqa: BLE001
        log.warning("document_extraction_unexpected_error")
        return await _fail(
            session,
            actor,
            doc,
            job,
            EXTRACTION_FAILED,
            ocr_text=result.text,
            ip=ip,
            user_agent=user_agent,
        )

    # Only move on if the document is still ours: a cancel that arrived while
    # OCR was running must not be overwritten by a late result.
    moved = await session.execute(
        update(Document)
        .where(Document.id == doc.id, Document.status == DocumentStatus.PROCESSING.value)
        .values(
            status=DocumentStatus.AWAITING_REVIEW.value,
            failure_code=None,
            ocr_text=result.text,
            document_date=extraction.document_date,
        )
        .execution_options(synchronize_session=False)
    )
    if moved.rowcount != 1:
        job.status = "failed"
        job.finished_at = utcnow()
        await session.commit()
        await session.refresh(doc)
        return doc

    # The primary table (medicines / lab results), then every other table; each table has
    # its own block of `row_index` values (`groups.INDEX_BLOCK`), so a row can never be taken
    # for another table's.
    primary = groups.PRIMARY.get(doc.doc_type)
    if primary is not None and primary.from_extractor:
        for index, row in enumerate(extraction.rows):
            row_certainty = (
                extraction.field_certainty[index] if extraction.field_certainty else None
            )
            for name in primary.fields:
                level = row_certainty.get(name) if row_certainty else None
                weight = normalization.CERTAINTY_WEIGHT.get(level) if level else None
                session.add(
                    OcrExtraction(
                        id=uuid7(),
                        job_id=job.id,
                        field_name=name,
                        extracted_value=row.get(name) or None,
                        confidence=_decimal(weight),
                        row_index=index,
                    )
                )
    extra_count = 0
    for spec, table_rows in extra_tables:
        for offset, row in enumerate(table_rows):
            index = spec.index_base + offset
            for name in spec.fields:
                session.add(
                    OcrExtraction(
                        id=uuid7(),
                        job_id=job.id,
                        field_name=name,
                        extracted_value=(row.get(name) or None),
                        row_index=index,
                    )
                )
            extra_count += 1
    job.status = "done"
    job.engine = result.engine
    job.finished_at = utcnow()
    job.ai_sections = ai_sections_reading
    job.overall_confidence = (
        _decimal(normalization.aggregate_confidence(extraction.field_certainty))
        if extraction.field_certainty
        else None
    )
    await audit.write(
        session,
        action=audit.DOCUMENT_PROCESS,
        actor_user_id=actor.user_id,
        actor_role=ACTOR_ROLE,
        subject_patient_id=doc.patient_id,
        resource_type=RESOURCE,
        ip_address=ip,
        user_agent=user_agent,
        detail={
            "document_id": str(doc.id),
            "doc_type": doc.doc_type,
            "outcome": "awaiting_review",
            "engine": result.engine,
            "rows": len(extraction.rows),
            "other_rows": extra_count,
        },
    )
    await session.commit()
    await _bring_up_to_date(
        session,
        doc,
        status=DocumentStatus.AWAITING_REVIEW.value,
        failure_code=None,
        ocr_text=result.text,
        document_date=extraction.document_date,
    )
    return doc


# ── Reading ──────────────────────────────────────────────────────────────
def can_retry(doc: Document) -> bool:
    """A document whose reading FAILED can be read again. Nothing else can: an upload not yet
    read has its own "Read" action, one being read is already running, one awaiting review has
    proposals the patient may be editing, and a finalized record is never read again."""
    return doc.status == DocumentStatus.FAILED.value


def can_delete(doc: Document, owner_user_id: uuid.UUID | None) -> bool:
    """Whether the document's patient may delete it. An unconfirmed upload always; a saved
    record only if the patient uploaded it themselves (a clinician-authored record can never
    be erased by the patient — the same rule as a profile entry's `recorded_by`) and the
    deployment allows it (`DOCUMENT_ALLOW_DELETE_FINALIZED`). A deleted document is gone."""
    if doc.status == DocumentStatus.CANCELLED.value:
        return False
    if doc.status == DocumentStatus.FINALIZED.value:
        return (
            settings.document_allow_delete_finalized
            and owner_user_id is not None
            and doc.uploaded_by == owner_user_id
        )
    return True


def to_out(doc: Document, owner_user_id: uuid.UUID | None = None) -> DocumentOut:
    return DocumentOut(
        id=doc.id,
        document_type=DB_TO_API_TYPE[doc.doc_type],  # type: ignore[arg-type]
        status=doc.status,  # type: ignore[arg-type]
        failure_code=doc.failure_code,
        original_filename=doc.original_filename,
        mime_type=doc.mime_type,
        size_bytes=doc.size_bytes,
        uploaded_at=doc.uploaded_at,
        document_date=doc.document_date,
        finalized_at=doc.finalized_at,
        can_retry=can_retry(doc),
        can_delete=can_delete(doc, owner_user_id),
        can_download=doc.status != DocumentStatus.CANCELLED.value,
    )


async def _extractions(session: AsyncSession, doc: Document) -> list[OcrExtraction]:
    job = await _job(session, doc.id)
    if job is None:
        return []
    rows = await session.execute(
        select(OcrExtraction)
        .where(OcrExtraction.job_id == job.id)
        .order_by(OcrExtraction.row_index, OcrExtraction.id)
    )
    return list(rows.scalars().all())


def _final_value(ext: OcrExtraction | None) -> str:
    if ext is None:
        return ""
    return ext.corrected_value if ext.corrected_value is not None else (ext.extracted_value or "")


def _lab_row_flag(cells: dict[str, OcrExtraction], ocr_text: str | None) -> lab_flags.Flag | None:
    """The flag for one lab result row (see `lab_flags.py`). A printed marker is
    looked up against the report's own text, so the row's ORIGINAL values find
    the line; the patient's corrected range and result are what a calculated
    flag compares — and a printed marker is not trusted once the result beside
    it has been edited."""

    def original(name: str) -> str:
        return (cells[name].extracted_value or "") if name in cells else ""

    def final(name: str) -> str:
        return _final_value(cells.get(name))

    result = cells.get("result")
    result_edited = bool(
        result is not None
        and result.corrected_value is not None
        and result.corrected_value != (result.extracted_value or "")
    )
    return lab_flags.flag_for(
        ocr_text=ocr_text,
        test_name=original("test_name") or final("test_name"),
        result=final("result"),
        unit=original("unit") or final("unit"),
        reference_range=final("reference_range"),
        result_edited=result_edited,
    )


def _build_rows(
    extractions: list[OcrExtraction],
    names: tuple[str, ...],
    *,
    only_kept: bool,
    doc_type: str | None = None,
    ocr_text: str | None = None,
) -> list[RowOut]:
    """One document's `RowOut`s from its (already-fetched) extraction cells —
    pure and query-free, so it is shared by both the single-document path
    (`_rows`, one query already run) and the batched records-list path
    (`_rows_by_document`, one query for every document)."""
    if not names:
        return []
    by_row: dict[int, dict[str, OcrExtraction]] = {}
    for ext in extractions:
        if ext.field_name in names:  # a document has several tables; this is one
            by_row.setdefault(ext.row_index or 0, {})[ext.field_name] = ext
    out: list[RowOut] = []
    for index in sorted(by_row):
        cells = by_row[index]
        fields = [
            FieldOut(
                name=name,
                extracted_value=cells[name].extracted_value if name in cells else None,
                corrected_value=cells[name].corrected_value if name in cells else None,
                value=_final_value(cells.get(name)),
                confidence=(
                    float(cells[name].confidence)
                    if name in cells and cells[name].confidence is not None
                    else None
                ),
                status="human_verified"
                if name in cells and cells[name].reviewed_at
                else "ai_extracted",
            )
            for name in names
        ]
        # A row the patient emptied out was removed during review.
        if only_kept and not any(f.value for f in fields):
            continue
        flag = _lab_row_flag(cells, ocr_text) if doc_type == DOC_TYPE_LAB else None
        out.append(
            RowOut(
                index=index,
                fields=fields,
                flag=flag.kind if flag else None,
                flag_source=flag.source if flag else None,  # type: ignore[arg-type]
            )
        )
    return out


def _build_groups(
    extractions: list[OcrExtraction],
    doc_type: str,
    *,
    only_kept: bool,
    ocr_text: str | None,
) -> list[GroupOut]:
    """Every table of the document, each built from its own cells only."""
    return [
        GroupOut(
            key=spec.key,
            field_names=list(spec.fields),
            layout=spec.layout,  # type: ignore[arg-type]
            rows=_build_rows(
                extractions, spec.fields, only_kept=only_kept, doc_type=doc_type, ocr_text=ocr_text
            ),
        )
        for spec in groups.groups_for(doc_type)
    ]


async def _rows_by_document(
    session: AsyncSession, docs: list[Document]
) -> dict[uuid.UUID, list[RowOut]]:
    """The same rows `_rows` builds for one document, batched for many: one
    query for every document's OCR job, then one for every job's extraction
    rows — instead of `_extractions`' two queries repeated per document,
    which turned the records list into an N+1 (one round-trip per row shown,
    each costing real time against a remote Supabase Postgres)."""
    if not docs:
        return {}
    jobs = (
        (await session.execute(select(OcrJob).where(OcrJob.document_id.in_([d.id for d in docs]))))
        .scalars()
        .all()
    )
    job_by_doc = {j.document_id: j for j in jobs}
    extractions: list[OcrExtraction] = []
    job_ids = [j.id for j in jobs]
    if job_ids:
        extractions = list(
            (
                await session.execute(
                    select(OcrExtraction)
                    .where(OcrExtraction.job_id.in_(job_ids))
                    .order_by(OcrExtraction.row_index, OcrExtraction.id)
                )
            )
            .scalars()
            .all()
        )
    by_job: dict[uuid.UUID, list[OcrExtraction]] = {}
    for ext in extractions:
        by_job.setdefault(ext.job_id, []).append(ext)
    return {
        doc.id: _build_rows(
            by_job.get(job_by_doc[doc.id].id, []) if doc.id in job_by_doc else [],
            extractors.FIELDS[doc.doc_type],
            only_kept=True,
            doc_type=doc.doc_type,
            ocr_text=doc.ocr_text,
        )
        for doc in docs
    }


_CLINICAL_KEYS = ("complaint", "diagnosis")


def _sections_out(
    doc: Document, ai_reading: object = None, covered: frozenset[str] = frozenset()
) -> DocumentSectionsOut | None:
    """The structured presentation of a document's OCR text — display only,
    computed fresh from `doc.ocr_text` each time (nothing new is stored). Shown
    while the document awaits review (so the patient can check it against the
    original) and on its saved record, for every document type. `None` for any
    other status or a missing OCR text, so the template falls back to the raw
    text exactly as before."""
    if doc.status not in _SECTION_STATUSES or not doc.ocr_text:
        return None
    parsed = sections.apply_ai_sections(
        sections.structure_document(doc.ocr_text, doc.doc_type), ai_reading
    )
    if parsed.is_empty():
        return None

    def items(found: list[tuple[str, str, bool]]) -> list[SectionItem]:
        return [SectionItem(key=k, value=v, unreadable=u) for k, v, u in found]

    # Symptoms and diagnosis are shown as the clinical picture of the document,
    # apart from its administrative details (dates, title).
    # What a reviewable table already holds is shown THERE (editable, then saved), never a
    # second time as a computed, read-only section — and a document with no rows for a
    # table (one saved before the tables existed) still falls back to the computed one.
    hidden = {
        key
        for key, table in (
            ("complaint", groups.SYMPTOMS.key),
            ("diagnosis", groups.DIAGNOSES.key),
        )
        if table in covered
    }
    clinical = [
        entry for entry in parsed.report if entry[0] in _CLINICAL_KEYS and entry[0] not in hidden
    ]
    report = [entry for entry in parsed.report if entry[0] not in _CLINICAL_KEYS]
    vitals_shown = [] if groups.VITALS.key in covered else parsed.vitals
    doses = [] if groups.VACCINES.key in covered else parsed.entries
    return DocumentSectionsOut(
        facility=items(parsed.facility),
        patient=items(parsed.patient),
        provider=items(parsed.provider),
        report=items(report),
        clinical=items(clinical),
        vitals=items(vitals_shown),
        entries=[SectionEntry(values=entry) for entry in doses],
        entry_fields=[key for key in vaccines.ENTRY_KEYS if any(key in entry for entry in doses)],
        notes=parsed.notes,
        topics=[SectionTopic(key=key, lines=lines) for key, lines in parsed.topics],
    )


def _document_source(doc: Document, patient_user_id: uuid.UUID) -> str:
    """ "patient_upload" or "doctor_generated" — derived from who actually
    created the row (`Document.uploaded_by`, already stored for every
    document) compared against the PATIENT the document belongs to (never
    the current caller — a doctor reading back the prescription they just
    created is not the patient, so that comparison would be wrong). When
    they match, the patient uploaded it; when they differ, someone else (a
    doctor) did. No new column: this is exactly "reuse the existing source
    mechanism"."""
    return SOURCE_PATIENT_UPLOAD if doc.uploaded_by == patient_user_id else SOURCE_DOCTOR_GENERATED


async def patient_owner_user_id(session: AsyncSession, patient_id: uuid.UUID) -> uuid.UUID:
    """The `user_profile.user_id` behind a `patient_id` — needed to derive
    `_document_source` when the caller reading a document back is not the
    patient themselves (the doctor who just authored it, in
    `doctor_router.py`). Every other caller already has this for free
    (`actor.user_id`, since they ARE the patient — see `_patient_id`)."""
    owner = await session.scalar(select(Patient.user_id).where(Patient.id == patient_id))
    if owner is None:
        raise NotFound()
    return owner


async def to_detail(
    session: AsyncSession, doc: Document, patient_user_id: uuid.UUID, *, include_text: bool = True
) -> DocumentDetailOut:
    base = to_out(doc, patient_user_id)
    extractions = await _extractions(session, doc)
    # While awaiting review every proposed row is shown; once finalized, only the rows the
    # patient kept.
    tables = _build_groups(
        extractions,
        doc.doc_type,
        only_kept=doc.status == DocumentStatus.FINALIZED.value,
        ocr_text=doc.ocr_text,
    )
    primary = groups.PRIMARY.get(doc.doc_type)
    covered = frozenset(
        spec.key
        for spec in groups.groups_for(doc.doc_type)
        if any(e.field_name in spec.fields for e in extractions)
    )
    return DocumentDetailOut(
        **base.model_dump(),
        ocr_text=doc.ocr_text if include_text else None,
        field_names=list(extractors.FIELDS[doc.doc_type]),
        rows=next((t.rows for t in tables if primary is not None and t.key == primary.key), []),
        groups=tables,
        sections=_sections_out(doc, await _stored_ai_sections(session, doc), covered)
        if include_text
        else None,
        source=_document_source(doc, patient_user_id),
        quality=_quality_out(await quality_of(doc)) if include_text else None,
    )


async def get_detail(
    session: AsyncSession, actor: Actor, document_id: uuid.UUID
) -> DocumentDetailOut:
    doc = await _owned(session, actor, document_id)
    return await to_detail(session, doc, actor.user_id)


async def get_document(session: AsyncSession, actor: Actor, document_id: uuid.UUID) -> Document:
    return await _owned(session, actor, document_id)


def _finalized_stmt(
    pid: uuid.UUID, document_type: str | None, filters: doc_filters.DocumentFilters | None
) -> Select | None:  # type: ignore[type-arg]
    """The finalized records of patient `pid` that the filters leave, or `None` when the
    filters can match none of them (a search for failed uploads has nothing to say about
    saved records)."""
    stmt = select(Document).where(
        Document.patient_id == pid, Document.status == DocumentStatus.FINALIZED.value
    )
    if document_type is not None:
        stmt = stmt.where(Document.doc_type == parse_document_type(document_type))
    if filters is not None:
        scoped = filters.only(status_in=(DocumentStatus.FINALIZED.value,))
        if scoped is None:
            return None
        stmt = doc_filters.apply(stmt, scoped)
    return stmt


async def count_finalized(
    session: AsyncSession,
    actor: Actor,
    *,
    document_type: str | None = None,
    filters: doc_filters.DocumentFilters | None = None,
) -> int:
    """How many finalized records the filters leave (for paging)."""
    stmt = _finalized_stmt(await _patient_id(session, actor), document_type, filters)
    if stmt is None:
        return 0
    return int(
        (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    )


async def list_finalized(
    session: AsyncSession,
    actor: Actor,
    *,
    document_type: str | None = None,
    filters: doc_filters.DocumentFilters | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[DocumentDetailOut]:
    """The patient's finalized records — the read side the document screens and
    the future Medical Timeline share. Unconfirmed uploads are never here. `filters`
    (search, type, date) only ever NARROW the patient's own records; `limit` / `offset` take
    one page of them, newest document first."""
    stmt = _finalized_stmt(await _patient_id(session, actor), document_type, filters)
    if stmt is None:
        return []
    stmt = stmt.order_by(
        func.coalesce(Document.document_date, pk_date(Document.finalized_at)).desc(),
        Document.finalized_at.desc(),
        Document.id.desc(),  # a total order, so a page never repeats or skips a record
    )
    if offset:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    docs = list((await session.execute(stmt)).scalars().all())
    rows_by_doc = await _rows_by_document(session, docs)
    return [
        DocumentDetailOut(
            **to_out(d, actor.user_id).model_dump(),
            ocr_text=None,
            field_names=list(extractors.FIELDS[d.doc_type]),
            rows=rows_by_doc.get(d.id, []),
            source=_document_source(d, actor.user_id),
        )
        for d in docs
    ]


def _in_progress_stmt(pid: uuid.UUID, filters: doc_filters.DocumentFilters | None) -> Select | None:  # type: ignore[type-arg]
    in_progress = tuple(s.value for s in IN_PROGRESS_STATUSES)
    stmt = select(Document).where(Document.patient_id == pid, Document.status.in_(in_progress))
    if filters is not None:
        scoped = filters.only(status_in=in_progress)
        if scoped is None:
            return None
        stmt = doc_filters.apply(stmt, scoped)
    return stmt


async def count_in_progress(
    session: AsyncSession, actor: Actor, *, filters: doc_filters.DocumentFilters | None = None
) -> int:
    stmt = _in_progress_stmt(await _patient_id(session, actor), filters)
    if stmt is None:
        return 0
    return int(
        (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    )


async def list_in_progress(
    session: AsyncSession,
    actor: Actor,
    *,
    filters: doc_filters.DocumentFilters | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[DocumentOut]:
    """Uploads the patient has not confirmed yet (to resume, retry or delete), newest first."""
    stmt = _in_progress_stmt(await _patient_id(session, actor), filters)
    if stmt is None:
        return []
    stmt = stmt.order_by(Document.uploaded_at.desc(), Document.id.desc())
    if offset:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    return [to_out(d, actor.user_id) for d in (await session.execute(stmt)).scalars().all()]


@dataclass(frozen=True)
class FinalizedDocument:
    """One SAVED document as the medical timeline reads it (P3): the document, who wrote it
    (`source`), the rows of its main table that may be relied on, and its structured sections.
    Everything is computed from stored data — nothing new is stored."""

    doc: Document
    source: str  # SOURCE_PATIENT_UPLOAD | SOURCE_DOCTOR_GENERATED
    rows: list[RowOut]
    parsed: sections.DocumentSections | None
    # Prescription items some other feature made out of this document's cells (`ocr_extraction
    # .item_id`): when present, the document is the source of those prescriptions and the
    # timeline lists them once. Empty for every document this app has produced so far.
    item_ids: frozenset[uuid.UUID] = frozenset()


async def finalized_documents(
    session: AsyncSession, patient: Patient, doc_types: tuple[str, ...]
) -> list[FinalizedDocument]:
    """Every FINALIZED document of these types belonging to ONE patient, newest upload first —
    in two queries however many there are (the documents with their OCR job, and their extracted
    cells).

    Scoped by `patient.id` alone: the caller is the consent gateway (or the patient reading their
    own record), which has already decided this reader may see them. Only SAVED documents: an
    upload still awaiting review is not a record (see "An upload is never a record").

    What counts as confirmed: a document the patient uploaded and saved shows only the rows the
    patient reviewed (`reviewed_at` set — a table the review step never covered stays a mere
    proposal and is left out); a document a doctor wrote has no extraction to confirm, so all its
    rows stand."""
    found = (
        await session.execute(
            select(Document, OcrJob)
            .outerjoin(OcrJob, OcrJob.document_id == Document.id)  # one job per document
            .where(
                Document.patient_id == patient.id,
                Document.status == DocumentStatus.FINALIZED.value,
                Document.doc_type.in_(doc_types),
            )
            .order_by(Document.uploaded_at.desc(), Document.id)
        )
    ).all()
    if not found:
        return []
    docs = [doc for doc, _ in found]
    jobs = [job for _, job in found if job is not None]
    job_by_doc = {job.document_id: job for job in jobs}
    cells_by_job: dict[uuid.UUID, list[OcrExtraction]] = {}
    if jobs:
        for cell in (
            (
                await session.execute(
                    select(OcrExtraction)
                    .where(OcrExtraction.job_id.in_([job.id for job in jobs]))
                    .order_by(OcrExtraction.row_index, OcrExtraction.id)
                )
            )
            .scalars()
            .all()
        ):
            cells_by_job.setdefault(cell.job_id, []).append(cell)
    out: list[FinalizedDocument] = []
    for doc in docs:
        job = job_by_doc.get(doc.id)
        source = _document_source(doc, patient.user_id)
        rows = _build_rows(
            cells_by_job.get(job.id, []) if job else [],
            extractors.FIELDS[doc.doc_type],
            only_kept=True,
            doc_type=doc.doc_type,
            ocr_text=doc.ocr_text,
        )
        if source == SOURCE_PATIENT_UPLOAD:
            rows = [r for r in rows if any(f.status == "human_verified" for f in r.fields)]
        parsed = (
            sections.apply_ai_sections(
                sections.structure_document(doc.ocr_text, doc.doc_type),
                job.ai_sections if job else None,
            )
            if doc.ocr_text
            else None
        )
        item_ids = frozenset(
            cell.item_id for cell in (cells_by_job.get(job.id, []) if job else []) if cell.item_id
        )
        out.append(
            FinalizedDocument(doc=doc, source=source, rows=rows, parsed=parsed, item_ids=item_ids)
        )
    return out


@dataclass(frozen=True)
class InProgressDocument:
    """An upload that is not a record yet — all the timeline may show of it: what it is, what it is
    called, when it was uploaded and where it stands. Never what was read from it."""

    id: uuid.UUID
    doc_type: str
    status: str
    failure_code: str | None
    name: str | None
    uploaded_at: datetime


IN_PROGRESS_LIMIT = 100


async def in_progress_documents(
    session: AsyncSession, patient: Patient
) -> list[InProgressDocument]:
    """The patient's uploads that are still being read, waiting for their review, or failed —
    newest first, in one query. An upload is not a record until the patient confirms it, so the
    timeline lists these apart from the saved history and without any extracted value."""
    statuses = [s.value for s in IN_PROGRESS_STATUSES]
    rows = await session.execute(
        select(
            Document.id,
            Document.doc_type,
            Document.status,
            Document.failure_code,
            Document.original_filename,
            Document.uploaded_at,
        )
        .where(Document.patient_id == patient.id, Document.status.in_(statuses))
        .order_by(Document.uploaded_at.desc(), Document.id)
        .limit(IN_PROGRESS_LIMIT)
    )
    return [InProgressDocument(*row) for row in rows.all()]


async def get_file(
    session: AsyncSession, actor: Actor, document_id: uuid.UUID
) -> tuple[bytes, str, str]:
    """The original upload, EXACTLY as stored (never a preprocessed copy, never OCR output),
    for the owner only. A cancelled or deleted document's file is gone, so it reads as not
    found; a file the store cannot be asked for right now is `FileUnavailable`, which says so
    instead of telling the patient their file is lost."""
    doc = await _owned(session, actor, document_id)
    if doc.status == DocumentStatus.CANCELLED.value:
        raise NotFound()
    try:
        data = await storage.get(doc.object_key)
    except Exception as exc:
        if storage.is_missing(exc):
            raise NotFound() from exc  # the file is not there
        log.warning("document_storage_get_failed", **storage.describe_failure(exc))
        raise FileUnavailable() from exc  # the store would not answer: try again later
    return data, doc.mime_type or "application/octet-stream", doc.original_filename or "document"


async def preprocess_report(
    session: AsyncSession, actor: Actor, document_id: uuid.UUID
) -> tuple[Document, ocr.PreprocessReport | None, str | None]:
    """What the OCR engine receives for this document after preprocessing (see
    `ocr.describe_preprocessing`), for the owner only. Computed fresh from the
    stored original, never stored. A file that cannot be prepared at all comes back as
    `(doc, None, error_code)` — the page explains it — not an exception, since
    "why can't it be read" is exactly what this report is for. A cancelled
    upload's file has been deleted, so it reads as not found."""
    doc = await _owned(session, actor, document_id)
    if doc.status == DocumentStatus.CANCELLED.value:
        raise NotFound()
    try:
        data = await storage.get(doc.object_key)
    except Exception as exc:
        raise NotFound() from exc
    try:
        report = await ocr.describe_preprocessing(data, doc.mime_type or "")
    except ocr.OcrError as exc:
        return doc, None, exc.code
    return doc, report, None


# ── Review -> Confirm & Save ─────────────────────────────────────────────
def _validate_date(value: date | None) -> date | None:
    if value is None:
        return None
    if value > dbtypes.today_pk() or value.year < 1900:
        raise ValidationFailed({"fields": {"document_date": "errors.date_invalid"}})
    return value


@dataclass(frozen=True)
class ConfirmOutcome:
    """What Confirm & Save did BEYOND finalizing the document."""

    # Allergies the patient confirmed that were added to their profile's allergy list.
    allergies_added: int = 0
    # Confirmed allergies that were not added: already on the list, the list is full, or
    # the entry was not valid for the profile. The document is finalized either way.
    allergies_skipped: int = 0
    # Medicines the patient ticked "add to my current medicines" on, added to their profile —
    # and the ticked ones that were not (already on the list, the list is full, not valid).
    medicines_added: int = 0
    medicines_skipped: int = 0
    # What was copied into the typed clinical tables (counts only; `failed` = it could not be,
    # the document is saved anyway and `add_to_records` tries again).
    records: records.RecordsOutcome = field(default_factory=records.RecordsOutcome)


def _severity_for_profile(value: str) -> str | None:
    """The profile only knows mild / moderate / severe / unknown. Anything else typed in
    review is left out rather than guessed at — the allergy is still recorded."""
    lowered = value.strip().lower()
    return lowered if lowered in ("mild", "moderate", "severe", "unknown") else None


async def _add_allergies_to_profile(
    session: AsyncSession,
    actor: Actor,
    rows: list[dict[str, str]],
    *,
    user_agent: str | None,
) -> ConfirmOutcome:
    """The allergies the patient confirmed, added to THEIR OWN profile through the same
    service the profile page uses — so the same rules apply (a duplicate is refused, the
    list has a cap, the entry is audited and is editable by the patient who recorded it).
    One that cannot be added never blocks the others, and never un-finalizes the
    document that was already saved."""
    added = skipped = 0
    for cells in rows:
        try:
            body = AllergyCreate(
                substance=cells["allergy_substance"],
                reaction=cells.get("allergy_reaction") or None,
                severity=_severity_for_profile(cells.get("allergy_severity", "")),
            )
            await profile_service.add_allergy(session, actor, body, user_agent=user_agent)
            added += 1
        except (PydanticValidationError, ValidationFailed, DuplicateEntry, ListFull, NotFound):
            await session.rollback()
            skipped += 1
    return ConfirmOutcome(allergies_added=added, allergies_skipped=skipped)


async def _add_medicines_to_profile(
    session: AsyncSession,
    actor: Actor,
    rows: list[dict[str, str]],
    *,
    user_agent: str | None,
) -> tuple[int, int]:
    """The medicines the patient ticked, added to THEIR OWN current-medicines list through the
    profile service (same rules as the profile page: a duplicate is refused, the list has a cap,
    the entry is audited and editable by the patient who recorded it). A prescription is history,
    not "currently taking", so this happens only for a medicine the patient chose. One that cannot
    be added never blocks the others and never un-finalizes the saved document.
    Returns `(added, skipped)`."""
    added = skipped = 0
    for cells in rows:
        spill: list[str] = []
        strength = records.fit_to_column(cells.get("dosage", "").strip(), 50, "Dosage", spill)
        frequency = records.fit_to_column(
            cells.get("frequency", "").strip(), 50, "Frequency", spill
        )
        if cells.get("duration", "").strip():
            spill.append(f"Duration: {cells['duration'].strip()}")
        try:
            body = MedicationCreate(
                name=cells["medicine"],
                strength=strength,
                frequency=frequency,
                notes="; ".join(spill) or None,
            )
            await profile_service.add_medication(session, actor, body, user_agent=user_agent)
            added += 1
        except (PydanticValidationError, ValidationFailed, DuplicateEntry, ListFull, NotFound):
            await session.rollback()
            skipped += 1
    return added, skipped


RECORD_DOC_TYPES = (DOC_TYPE_RX, DOC_TYPE_LAB, DOC_TYPE_OTHER)


def _facts_of(doc: Document, *, document_date: date | None, finalized_at: datetime | None):
    return records.DocumentFacts(
        id=doc.id,
        patient_id=doc.patient_id,
        doc_type=doc.doc_type,
        document_date=document_date,
        uploaded_at=doc.uploaded_at,
        finalized_at=finalized_at,
        ocr_text=doc.ocr_text,
    )


async def _write_records(
    session: AsyncSession,
    actor: Actor,
    facts: records.DocumentFacts,
    rows: list[records.RowData],
    ai_reading: object,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> records.RecordsOutcome:
    """Copy the confirmed values into the typed clinical tables, as ONE transaction of its own
    (every row, its audit entry, then one commit — or nothing). It runs after the document was
    saved, so a failure here never undoes the save: it is logged (the error CLASS only) and
    reported as `failed`, and `add_to_records` can run it again."""
    try:
        parsed = None
        if facts.doc_type == DOC_TYPE_LAB and facts.ocr_text:
            parsed = sections.apply_ai_sections(
                sections.structure_document(facts.ocr_text, facts.doc_type), ai_reading
            )
        outcome = await records.write(session, doc=facts, rows=rows, parsed=parsed)
        if outcome.stored:
            await audit.write(
                session,
                action=audit.DOCUMENT_RECORDS,
                actor_user_id=actor.user_id,
                actor_role=ACTOR_ROLE,
                subject_patient_id=facts.patient_id,
                resource_type=RESOURCE,
                ip_address=ip,
                user_agent=user_agent,
                detail={
                    "document_id": str(facts.id),
                    "doc_type": facts.doc_type,
                    "prescription_items": outcome.prescription_items,
                    "diagnoses": outcome.diagnoses,
                    "vital_measurements": outcome.vital_measurements,
                    "lab_reports": outcome.lab_reports,
                    "lab_results": outcome.lab_results,
                },
            )
        await session.commit()
        return outcome
    except Exception as exc:  # noqa: BLE001 - a records failure must never undo a saved document
        await session.rollback()
        log.warning("document_records_failed", error=type(exc).__name__)
        return records.RecordsOutcome(failed=True)


def _existing_groups(
    existing: list[OcrExtraction],
) -> dict[int, groups.GroupSpec]:
    names_by_row: dict[int, set[str]] = {}
    for ext in existing:
        names_by_row.setdefault(ext.row_index or 0, set()).add(ext.field_name)
    found: dict[int, groups.GroupSpec] = {}
    for index, names in names_by_row.items():
        if (spec := groups.group_of(names)) is not None:
            found[index] = spec
    return found


async def confirm(
    session: AsyncSession,
    actor: Actor,
    document_id: uuid.UUID,
    body: ConfirmRequest,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> Document:
    """Confirm & Save (see `confirm_with_outcome`) — returns just the document."""
    doc, _outcome = await confirm_with_outcome(
        session, actor, document_id, body, ip=ip, user_agent=user_agent
    )
    return doc


async def confirm_with_outcome(
    session: AsyncSession,
    actor: Actor,
    document_id: uuid.UUID,
    body: ConfirmRequest,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[Document, ConfirmOutcome]:
    """Confirm & Save — the ONLY way a document becomes a medical record.

    The patient's reviewed values are stored beside (never over) what the
    engine proposed. Confirming twice is harmless: the second call finds the
    record already finalized and changes nothing.

    A document has several tables (`groups.py`): medicines, diagnosis, symptoms, vital
    signs, allergies, vaccine doses. `body.groups` names the ones this request REVIEWS;
    for those, a row left out of the request is a row the patient removed. A table the
    request does not name is left exactly as proposed — stored, but never marked
    reviewed — so a client that only knows about the medicines can never silently
    erase the rest. Omitted `groups` means just the document's primary table. Only a
    reviewed allergies table can add anything to the patient's profile.
    """
    doc = await _owned(session, actor, document_id)
    if doc.status == DocumentStatus.FINALIZED.value:
        return doc, ConfirmOutcome()
    if doc.status != DocumentStatus.AWAITING_REVIEW.value:
        raise InvalidDocumentState()

    specs = groups.groups_for(doc.doc_type)
    allowed = groups.fields_for(doc.doc_type)
    primary = groups.PRIMARY.get(doc.doc_type)
    if body.groups is None:
        reviewed = {primary.key} if primary is not None else set()
    else:
        reviewed = set(body.groups)
        if not reviewed <= {spec.key for spec in specs}:
            raise ValidationFailed({"fields": {"rows": "errors.validation_failed"}})

    job = await _job(session, doc.id)
    existing = await _extractions(session, doc)
    by_cell = {(e.row_index or 0, e.field_name): e for e in existing}
    existing_rows = {e.row_index or 0 for e in existing}
    existing_group = _existing_groups(existing)

    # 1. Plan and validate everything before touching any state.
    submitted: dict[int, tuple[groups.GroupSpec, dict[str, str]]] = {}
    per_group: dict[str, int] = {}
    for row in body.rows:
        if row.index in submitted:
            raise ValidationFailed({"fields": {"rows": "errors.validation_failed"}})
        names = set(row.fields)
        if names - allowed:
            raise ValidationFailed({"fields": {"rows": "errors.validation_failed"}})
        spec = groups.group_of(names) if names else existing_group.get(row.index)
        # A row belongs to ONE table, a table the request reviews, and — if the row
        # already exists — the table it was proposed in.
        if (
            spec is None
            or spec.key not in reviewed
            or existing_group.get(row.index, spec) is not spec
            or not groups.row_in_block(spec, row.index)
        ):
            raise ValidationFailed({"fields": {"rows": "errors.validation_failed"}})
        cells = {n: clean_value(row.fields[n]) for n in row.fields}
        if any(len(v) > extractors.MAX_VALUE_LEN for v in cells.values()):
            raise ValidationFailed({"fields": {"rows": "errors.value_too_long"}})
        if any(cells.values()):  # an empty "add a row" line holds nothing and counts for nothing
            per_group[spec.key] = per_group.get(spec.key, 0) + 1
            if per_group[spec.key] > spec.max_rows:
                raise ValidationFailed({"fields": {"rows": "errors.validation_failed"}})
        submitted[row.index] = (spec, cells)
    if len(submitted) > MAX_CONFIRM_ROWS:
        raise ValidationFailed({"fields": {"rows": "errors.validation_failed"}})

    final: dict[int, tuple[groups.GroupSpec, dict[str, str]]] = {}
    for index in existing_rows | set(submitted):
        spec = submitted[index][0] if index in submitted else existing_group.get(index)
        if spec is None or spec.key not in reviewed:
            continue  # not a table this request reviews: leave it exactly as proposed
        cells = {n: "" for n in spec.fields}
        if index in submitted:
            for n in spec.fields:
                if n in submitted[index][1]:
                    cells[n] = submitted[index][1][n]
                elif (index, n) in by_cell:  # left out of the request: unchanged
                    cells[n] = _final_value(by_cell[(index, n)])
        # index in existing_rows but not submitted = the patient removed the row
        final[index] = (spec, cells)

    kept = {i: (spec, c) for i, (spec, c) in final.items() if any(c.values())}
    for spec in specs:
        if spec.key not in reviewed:
            continue
        rows_here = [c for s, c in kept.values() if s is spec]
        needed = groups.min_rows(doc.doc_type, spec)
        if needed and len(rows_here) < needed:
            raise ValidationFailed({"fields": {"rows": "errors.document_rows_required"}})
        if spec.required is not None and any(not c[spec.required] for c in rows_here):
            raise ValidationFailed({"fields": {"rows": "errors.document_row_incomplete"}})
    new_date = (
        _validate_date(body.document_date)
        if "document_date" in body.model_fields_set
        else doc.document_date
    )

    # 2. Claim the transition. Losing means a concurrent confirm/cancel got
    #    there first; a confirm that already finalized is fine, anything else
    #    is a conflict.
    now = utcnow()
    claimed = await session.execute(
        update(Document)
        .where(Document.id == doc.id, Document.status == DocumentStatus.AWAITING_REVIEW.value)
        .values(
            status=DocumentStatus.FINALIZED.value,
            finalized_at=now,
            document_date=new_date,
            failure_code=None,
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        await session.rollback()
        await session.refresh(doc)
        if doc.status == DocumentStatus.FINALIZED.value:
            return doc, ConfirmOutcome()
        raise InvalidDocumentState()

    # What the typed tables are written from, as plain values (see `records.DocumentFacts`).
    facts = _facts_of(doc, document_date=new_date, finalized_at=now)
    ai_reading = job.ai_sections if job is not None else None

    # 3. Store the reviewed values next to the proposals.
    corrected_fields = 0
    cell_ids: dict[int, dict[str, uuid.UUID]] = {}
    edited: dict[int, set[str]] = {}
    for index, (spec, cells) in final.items():
        for n in spec.fields:
            ext = by_cell.get((index, n))
            if ext is None:
                if index in existing_rows or not any(cells.values()):
                    continue
                # A row the patient added during review.
                if job is None:
                    continue
                ext = OcrExtraction(
                    id=uuid7(), job_id=job.id, field_name=n, extracted_value=None, row_index=index
                )
                session.add(ext)
            proposed = ext.extracted_value or ""
            ext.corrected_value = None if cells[n] == proposed else cells[n]
            ext.reviewed_by = actor.user_id
            ext.reviewed_at = now
            cell_ids.setdefault(index, {})[n] = ext.id
            if ext.corrected_value is not None:
                corrected_fields += 1
                edited.setdefault(index, set()).add(n)

    allergy_rows = [c for s, c in kept.values() if s is groups.ALLERGIES]
    await audit.write(
        session,
        action=audit.DOCUMENT_CONFIRM,
        actor_user_id=actor.user_id,
        actor_role=ACTOR_ROLE,
        subject_patient_id=doc.patient_id,
        resource_type=RESOURCE,
        ip_address=ip,
        user_agent=user_agent,
        detail={
            "document_id": str(doc.id),
            "doc_type": doc.doc_type,
            "rows": len(kept),
            "fields_corrected": corrected_fields,
            "tables_reviewed": sorted(reviewed),
            # a count only: what the allergies ARE stays out of this audit blob
            "allergies_confirmed": len(allergy_rows),
        },
    )
    await session.commit()
    await session.refresh(doc)

    # 4. The document is saved. THEN the confirmed allergies join the patient's profile —
    #    each through the profile's own service (its own transaction and audit), so one
    #    that cannot be added never undoes the document or blocks the rest.
    outcome = ConfirmOutcome()
    if groups.ALLERGIES.key in reviewed and allergy_rows:
        outcome = await _add_allergies_to_profile(
            session, actor, allergy_rows, user_agent=user_agent
        )
        await session.refresh(doc)

    # 5. Medicines the patient ticked "add to my current medicines" on — only kept rows of the
    #    medicines table; the same service and rules as the profile page.
    ticked = [
        kept[i][1]
        for i in dict.fromkeys(body.current_medicines)
        if i in kept and kept[i][0] is groups.MEDICINES
    ]
    if ticked:
        added, skipped = await _add_medicines_to_profile(
            session, actor, ticked, user_agent=user_agent
        )
        outcome = replace(outcome, medicines_added=added, medicines_skipped=skipped)
        if skipped:
            await session.refresh(doc)  # a skipped one rolled the session back

    # 6. The confirmed values, copied into the typed clinical tables (prescription, diagnosis,
    #    vital_sign, lab_report ...). A failure here leaves the saved document as it is.
    if facts.doc_type in RECORD_DOC_TYPES and kept:
        rows = [
            records.RowData(
                index=i,
                group=spec.key,
                values=c,
                cell_ids=cell_ids.get(i, {}),
                edited=frozenset(edited.get(i, ())),
            )
            for i, (spec, c) in kept.items()
        ]
        written = await _write_records(
            session, actor, facts, rows, ai_reading, ip=ip, user_agent=user_agent
        )
        outcome = replace(outcome, records=written)
        if written.failed:
            await session.refresh(doc)
    return doc, outcome


async def add_to_records(
    session: AsyncSession,
    actor: Actor,
    document_id: uuid.UUID,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> records.RecordsOutcome:
    """Run the copy into the typed tables again for a document the patient already saved — the
    way back when it failed the first time. It reads what the patient CONFIRMED (the cells that
    carry `reviewed_at`) from the database, so it needs nothing from the request; a document that
    already has records is left alone, so pressing it twice cannot double anything. Only the
    patient's own upload: a document a doctor wrote is not theirs to copy."""
    doc = await _owned(session, actor, document_id)
    if (
        doc.status != DocumentStatus.FINALIZED.value
        or doc.doc_type not in RECORD_DOC_TYPES
        or doc.uploaded_by != actor.user_id
    ):
        raise InvalidDocumentState()
    # Two presses of "Try again" at once must not both pass the "already has records" check: the
    # second waits here until the first has committed, then finds the records and does nothing.
    # (A prescription is also guarded by a unique index, but a lab report and diagnoses are not,
    # and a loser there would report a failure for a copy that in fact succeeded.)
    await session.execute(select(Document.id).where(Document.id == doc.id).with_for_update())
    job = await _job(session, doc.id)
    existing = await _extractions(session, doc)
    spec_of = _existing_groups(existing)
    by_row: dict[int, list[OcrExtraction]] = {}
    for cell in existing:
        by_row.setdefault(cell.row_index or 0, []).append(cell)
    rows: list[records.RowData] = []
    for index, cells in by_row.items():
        spec = spec_of.get(index)
        if spec is None or not any(c.reviewed_at for c in cells):
            continue  # a proposal the patient never reviewed is not a confirmed row
        values = {c.field_name: _final_value(c) for c in cells}
        if not any(values.values()):
            continue
        rows.append(
            records.RowData(
                index=index,
                group=spec.key,
                values=values,
                cell_ids={c.field_name: c.id for c in cells},
                edited=frozenset(c.field_name for c in cells if c.corrected_value is not None),
            )
        )
    facts = _facts_of(doc, document_date=doc.document_date, finalized_at=doc.finalized_at)
    return await _write_records(
        session,
        actor,
        facts,
        rows,
        job.ai_sections if job is not None else None,
        ip=ip,
        user_agent=user_agent,
    )


# ── Doctor-generated prescription (no OCR, no patient review) ────────────
def _doctor_display_name(doctor: Doctor) -> str:
    """ "Dr. <name>" — unless `full_name` already carries that prefix itself
    (avoids the same "Dr. Dr." artifact `sections.py` already guards
    against on the OCR path, here at the source instead)."""
    name = doctor.full_name
    return name if re.match(r"(?i:dr)\.?\s", name) else f"Dr. {name}"


def _render_prescription_text(
    patient: Patient, doctor: Doctor, medicines: list[dict[str, str]], notes: str
) -> str:
    """A standardised, OCR-friendly rendering of a doctor's already-structured
    prescription — this becomes `document.ocr_text` and the stored "original"
    artifact. It is not re-parsed by anything (the structured `medicines`
    input is what actually becomes the row data; see
    `create_doctor_prescription`); it exists so the finalized document has a
    real, readable source text — the same role `document.ocr_text` plays for
    a patient's scanned upload — and so the EXISTING `sections.py` display
    (unchanged, unmodified) picks up the patient name/doctor/date/notes the
    same way it already does for a patient upload, via the same labels it
    already recognises (so the Document Details page's "Doctor Information"
    section is populated for a doctor-generated document too, instead of
    always being empty)."""
    lines = [
        f"Prescription Date: {dbtypes.today_pk().isoformat()}",
        f"Patient Name: {patient.full_name}",
        f"Doctor: {_doctor_display_name(doctor)}",
        f"Specialization: {doctor.specialty}" if doctor.specialty else None,
        "",
        "PRESCRIPTION",
    ]
    for m in medicines:
        lines += [
            "",
            "Medicine",
            m.get("medicine", ""),
            m.get("dosage", ""),
            m.get("frequency", ""),
            m.get("duration", ""),
        ]
    if notes:
        lines += ["", "Notes:", notes]
    return "\n".join(line for line in lines if line is not None)


async def create_doctor_prescription(
    session: AsyncSession,
    actor: Actor,
    *,
    patient_id: uuid.UUID,
    medicines: list[dict[str, str]],
    notes: str = "",
    ip: str | None = None,
    user_agent: str | None = None,
) -> Document:
    """A verified doctor's already-structured prescription for one of their
    patients — NOT an OCR document, and never patient-reviewed.

    Reuses exactly the same normalization + confidence stage a patient
    upload's OCR'd entities pass through inside `process()`
    (`normalization.normalize_prescription_rows`,
    `normalization.CERTAINTY_WEIGHT`/`aggregate_confidence`), and the same
    `OcrExtraction`/`OcrJob` storage shape, so the result is retrievable
    through the exact same `get_detail`/`list_finalized`/`GET
    /api/v1/documents` a patient upload is. The only real difference is
    everything AFTER normalization: there is no OCR step (the input is
    already structured, so `extractors.parse_prescription_entities` — which
    exists to recover structure from raw OCR text — has nothing to do here),
    no `awaiting_review` state, and no Confirm & Save. The doctor has
    already authored this; automatic validation (the same non-empty-medicine
    rule `confirm()` enforces) stands in for patient review, and the
    document is finalized in the same call that creates it.

    Calls `ai_understanding.validate_doctor_prescription` — NOT
    `understand_prescription` (that stage exists to semantically re-read
    NOISY OCR TEXT, and there is none here). The doctor path's Gemini call
    only ever VALIDATES: it can downgrade a field's certainty to "ambiguous"
    when its own reading disagrees with what the doctor typed, but it can
    never overwrite `rows` — the stored values are always exactly what the
    doctor entered, matching `normalize_prescription_rows`'s own output when
    Gemini is unavailable, fails, or disagrees with itself (a row-count
    mismatch). This is deliberate: unlike OCR text, there's no noise here to
    justify a rewrite, and rewriting a doctor's deliberate phrasing without
    their confirmation (there is no review step on this path) is a risk with
    no corresponding benefit.
    """
    patient = await session.get(Patient, patient_id)
    if patient is None:
        raise NotFound()
    doctor = await session.scalar(select(Doctor).where(Doctor.user_id == actor.user_id))
    if doctor is None:  # defensive only — VerifiedDoctorDep already confirmed this row exists
        raise NotFound()

    entities = [
        {
            "medicine": m.get("medicine", ""),
            "dosage": m.get("dosage", ""),
            "frequency": m.get("frequency", ""),
            "duration": m.get("duration", ""),
        }
        for m in medicines
    ]
    rows, certainty = normalization.normalize_prescription_rows(entities)

    understood = await ai_understanding.validate_doctor_prescription(rows)
    if understood is not None:
        rows, certainty = understood.rows, understood.certainty

    # Automatic validation — the same rule `confirm()` enforces for a
    # patient-reviewed prescription (SPEC: every kept row needs a medicine
    # name), just applied automatically instead of after patient review.
    # `rows` here is always the doctor's own (possibly Gemini-assessed)
    # values — Gemini can never have invented or removed a medicine name.
    required = extractors.REQUIRED_FIELD[DOC_TYPE_RX]
    if not any(row.get(required) for row in rows):
        raise ValidationFailed({"fields": {"medicines": "errors.document_row_incomplete"}})

    text = _render_prescription_text(patient, doctor, medicines, notes)
    data = text.encode("utf-8")
    document_id = uuid7()
    key = storage.object_key(patient_id, document_id, "txt")
    try:
        await storage.put(key, data, "text/plain")
    except Exception as exc:
        log.warning("document_storage_put_failed", **storage.describe_failure(exc))
        raise StorageUnavailable() from exc

    now = utcnow()
    try:
        doc = Document(
            id=document_id,
            patient_id=patient_id,
            doc_type=DOC_TYPE_RX,
            ocr_mode=OCR_MODE_PRINTED,  # unused (no OCR ran); a valid, harmless default
            object_key=key,
            mime_type="text/plain",
            size_bytes=len(data),
            uploaded_by=actor.user_id,  # the doctor — see `_document_source`
            original_filename=f"prescription-{document_id}.txt",
            status=DocumentStatus.FINALIZED.value,  # no awaiting_review, ever
            ocr_text=text,
            document_date=dbtypes.today_pk(),
            finalized_at=now,
        )
        session.add(doc)

        job = OcrJob(
            id=uuid7(),
            document_id=document_id,
            status="done",
            engine=ENGINE_DOCTOR_ENTRY,
            retry_count=0,
            started_at=now,
            finished_at=now,
            overall_confidence=_decimal(normalization.aggregate_confidence(certainty)),
        )
        session.add(job)
        await session.flush()  # job.id is needed for the OcrExtraction rows below

        field_names = extractors.FIELDS[DOC_TYPE_RX]
        for index, row in enumerate(rows):
            row_certainty = certainty[index]
            for name in field_names:
                level = row_certainty.get(name)
                weight = normalization.CERTAINTY_WEIGHT.get(level) if level else None
                session.add(
                    OcrExtraction(
                        id=uuid7(),
                        job_id=job.id,
                        field_name=name,
                        extracted_value=row.get(name) or None,
                        confidence=_decimal(weight),
                        row_index=index,
                    )
                )

        await audit.write(
            session,
            action=audit.DOCUMENT_UPLOAD,
            actor_user_id=actor.user_id,
            actor_role="doctor",
            subject_patient_id=patient_id,
            resource_type=RESOURCE,
            ip_address=ip,
            user_agent=user_agent,
            detail={
                "document_id": str(document_id),
                "doc_type": DOC_TYPE_RX,
                "source": SOURCE_DOCTOR_GENERATED,
                "outcome": "finalized",
                "engine": ENGINE_DOCTOR_ENTRY,
                "rows": len(rows),
            },
        )
        await session.commit()
    except Exception:
        await session.rollback()
        await storage.delete(key)
        raise
    await session.refresh(doc)
    return doc


async def _tombstone(
    session: AsyncSession,
    document_id: uuid.UUID,
    deletable: list[str],
    *,
    scrub: bool,
    remove_records: bool = False,
) -> bool:
    """The database half of retiring a document, in the caller's transaction: ONE conditional
    UPDATE claims the row (taking its row lock, so a concurrent confirm or retry waits and then
    finds it gone) and what was read from it is purged. `False`: the document is no longer in a
    state this may be done to (it was confirmed, or already retired, meanwhile)."""
    values: dict[str, object] = {
        "status": DocumentStatus.CANCELLED.value,
        "ocr_text": None,
        "failure_code": None,
    }
    if scrub:  # a deletion also forgets the file name and the date the document stated
        values["document_date"] = None
        values["original_filename"] = None
    claimed = await session.execute(
        update(Document)
        .where(Document.id == document_id, Document.status.in_(deletable))
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        return False
    if remove_records:  # the typed rows written from this document go with it
        await records.remove_for_document(session, document_id)
    job = await _job(session, document_id)
    if job is not None:
        await session.execute(delete(OcrExtraction).where(OcrExtraction.job_id == job.id))
        job.ai_sections = None  # the model's reading of the page is clinical text too
        if job.status in ("queued", "running"):
            job.status = "failed"
            job.finished_at = utcnow()
    return True


async def _retire(
    session: AsyncSession,
    actor: Actor,
    doc: Document,
    *,
    deletable: list[str],
    scrub: bool,
    action: str,
    detail: dict[str, object],
    ip: str | None,
    user_agent: str | None,
    remove_records: bool = False,
) -> Document:
    """Discard or delete a document — the one routine behind both, so neither can leave a file
    behind or report a success that did not happen.

    Order: (1) claim the row and purge what was read from it (`_tombstone`) and write the audit
    entry, all uncommitted; (2) remove the stored file, and the store must CONFIRM — if it does
    not, everything is rolled back, nothing changed, `DeleteFailed`; (3) commit. If the commit
    fails after the file is gone, the record would be left pointing at nothing, so the database
    half is done AGAIN once in a fresh transaction (an ambiguous failure — the first commit
    landed after all — is recognised, and not audited twice); only if that fails too is
    `DeleteFailed` raised, and retrying the deletion finishes it (removing a file that is already
    gone is not an error)."""
    # plain values, taken now: a rollback expires `doc`, and an expired row cannot be read here
    document_id, patient_id, key = doc.id, doc.patient_id, doc.object_key

    async def record() -> bool:
        if not await _tombstone(
            session, document_id, deletable, scrub=scrub, remove_records=remove_records
        ):
            return False
        await audit.write(
            session,
            action=action,
            actor_user_id=actor.user_id,
            actor_role=ACTOR_ROLE,
            subject_patient_id=patient_id,
            resource_type=RESOURCE,
            ip_address=ip,
            user_agent=user_agent,
            detail=detail,
        )
        return True

    if not await record():  # it changed state under us (confirmed, deleted, ...)
        await session.rollback()
        await session.refresh(doc)
        if doc.status == DocumentStatus.CANCELLED.value:
            return doc
        raise InvalidDocumentState()

    try:
        await storage.remove(key)
    except Exception as exc:
        await session.rollback()
        log.warning("document_storage_delete_failed", **storage.describe_failure(exc))
        raise DeleteFailed() from exc

    try:
        await session.commit()
    except Exception:  # noqa: BLE001
        await session.rollback()
        log.warning("document_delete_commit_failed")
        try:
            if await record():
                await session.commit()
            else:  # the first commit had landed after all
                await session.rollback()
                await session.refresh(doc)
                if doc.status != DocumentStatus.CANCELLED.value:
                    raise DeleteFailed()
        except DeleteFailed:
            raise
        except Exception as exc:
            await session.rollback()
            log.warning("document_delete_retry_failed")
            raise DeleteFailed() from exc
    await app_cache.cache.delete(_quality_key(document_id))  # nothing left to advise on
    await session.refresh(doc)
    return doc


async def cancel(
    session: AsyncSession,
    actor: Actor,
    document_id: uuid.UUID,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> Document:
    """Discard an unconfirmed upload. Nothing is finalized; the stored file and everything read
    out of it are purged — and the file's removal is CONFIRMED (see `_retire`), so a discard
    never leaves an orphaned file behind. A finalized record cannot be cancelled here."""
    doc = await _owned(session, actor, document_id)
    if doc.status == DocumentStatus.CANCELLED.value:
        await storage.delete(doc.object_key)  # best-effort: a leftover of an earlier discard
        return doc
    if doc.status == DocumentStatus.FINALIZED.value:
        raise InvalidDocumentState()
    return await _retire(
        session,
        actor,
        doc,
        deletable=[s.value for s in IN_PROGRESS_STATUSES],
        scrub=False,
        action=audit.DOCUMENT_CANCEL,
        detail={"document_id": str(doc.id), "doc_type": doc.doc_type},
        ip=ip,
        user_agent=user_agent,
    )


async def delete_document(
    session: AsyncSession,
    actor: Actor,
    document_id: uuid.UUID,
    *,
    confirmed: bool = False,
    ip: str | None = None,
    user_agent: str | None = None,
) -> Document:
    """A patient deletes one of their own documents.

    What goes: the stored original, the text read from it, everything extracted from it (the
    proposals and the confirmed values), the model's reading of its sections, its file name and
    the date it stated. What stays, on purpose: a tombstone row (`status = 'cancelled'`, the same
    state a discarded upload already has — never listed, never served), so the audit trail's
    `document.delete` entry still points at something; and everything the document ALREADY put
    elsewhere — an allergy it added to the patient's profile is the patient's own profile entry
    now and is removed only from the profile page, never silently from here.

    Rules (each enforced here, whatever the page shows):
    * only the patient's own document — a foreign or unknown id is the same `NotFound`;
    * a saved record needs `confirmed`, was uploaded by the patient (not written by a clinician)
      and the deployment must allow it (`can_delete`), else `DocumentNotDeletable`;
    * deleting a deleted document is a no-op — a double click never fails.

    The mechanics (claim, confirmed file removal, commit, and what happens if one fails) are
    `_retire`'s.
    """
    doc = await _owned(session, actor, document_id)
    if doc.status == DocumentStatus.CANCELLED.value:
        await storage.delete(doc.object_key)  # best-effort: a leftover of an earlier discard
        return doc
    finalized = doc.status == DocumentStatus.FINALIZED.value
    if not can_delete(doc, actor.user_id):
        raise DocumentNotDeletable()
    if finalized and not confirmed:
        raise ValidationFailed({"fields": {"confirm": "errors.delete_confirm_required"}})
    return await _retire(
        session,
        actor,
        doc,
        deletable=[DocumentStatus.FINALIZED.value]
        if finalized
        else [s.value for s in IN_PROGRESS_STATUSES],
        scrub=True,
        action=audit.DOCUMENT_DELETE,
        detail={
            "document_id": str(doc.id),
            "doc_type": doc.doc_type,
            "previous_status": doc.status,
        },
        ip=ip,
        user_agent=user_agent,
        # a saved document may have records written from it; one that never was saved has none
        remove_records=finalized and doc.doc_type in RECORD_DOC_TYPES,
    )
