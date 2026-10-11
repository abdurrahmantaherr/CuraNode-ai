"""Medical-document processing (FR10–FR13): one pipeline for every document type.

    upload -> temporary -> common OCR -> type-specific extraction
           -> review / edit -> Confirm & Save -> finalized record

Runs on the usual harness — in-memory SQLite, the fake Supabase (Auth AND
Storage) double, and a fake OCR engine — so no test touches the network or needs
Tesseract installed (the real OCR boundary is covered in `test_document_ocr.py`).
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta

import pytest
from app import cache as app_cache
from app.audit import writer as audit
from app.db import types as dbtypes
from app.db.models import (
    Allergy,
    AuditLog,
    Document,
    DocumentStatus,
    OcrExtraction,
    OcrJob,
    Patient,
    Profile,
    UserRole,
)
from app.db.sqlfuncs import pk_date
from app.db.types import utcnow
from app.documents import ai_understanding, extractors, groups, ocr
from app.documents import service as doc_service
from app.documents import storage as doc_storage
from app.i18n.catalogue import translate
from app.settings import settings
from app.web.router import safe_next
from httpx import ASGITransport, AsyncClient
from markupsafe import escape
from PIL import ImageFilter
from sqlalchemy import delete, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect
from sqlalchemy.ext.asyncio import AsyncSession

from tests import document_fixtures as fx
from tests.conftest import TEST_PASSWORD, make_user

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PDF = b"%PDF-1.4\n%fake\n" + b"\x00" * 32

RX_TEXT = """Dr. Mehreen Iqbal
Date: 11 Aug 2024
1. Tab. Metformin 500mg  1-0-1  x 30 days
2. Cap Atorvastatin 10 mg once daily for 30 days"""

LAB_TEXT = """Chughtai Lab   Report Date: 28/07/2024
HbA1c 7.2 % 4.0 - 5.6
Fasting Glucose 142 mg/dL 70-100"""

VACCINE_TEXT = "Immunization card\nBCG given 2024-03-02\nPolio dose 2"


# ── Harness ──────────────────────────────────────────────────────────────
class FakeOcr:
    """A controllable engine behind `ocr.get_engine` — records every call."""

    name = "tesseract"

    def __init__(self) -> None:
        self.text = RX_TEXT
        self.layouts: list[str] = []  # the page layout each read asked for
        self.layout = "page"  # the layout the engine was last handed out for
        self.table_text: str | None = None  # what a "table" layout read returns, if different
        self.table_error: str | None = None
        self.error: str | None = None
        self.calls: list[tuple[bytes, str]] = []
        self.hook = None

    async def transcribe(self, data: bytes, mime_type: str) -> tuple[str, None]:
        self.calls.append((data, mime_type))
        if self.hook is not None:
            await self.hook()
        if self.error:
            raise ocr.OcrError(self.error)
        if self.layout == "table":
            if self.table_error:
                raise ocr.OcrError(self.table_error)
            if self.table_text is not None:
                return self.table_text, None
        return self.text, None


@pytest.fixture(autouse=True)
def fake_ocr(monkeypatch) -> FakeOcr:
    engine = FakeOcr()

    def get_engine(layout: str = "page") -> FakeOcr:
        engine.layouts.append(layout)
        engine.layout = layout
        return engine

    monkeypatch.setattr(ocr, "get_engine", get_engine)
    return engine


@pytest.fixture(autouse=True)
def no_real_gemini(monkeypatch):
    """Never let a developer's own real GEMINI_API_KEY (in their local
    .env) leak into the test process — the same caution CLAUDE.md already
    calls for with OAUTH_ENABLED (`Settings` reads `.env` regardless of
    `ENVIRONMENT`). A test that wants Gemini "on" uses `fake_gemini`
    explicitly, never the real API."""
    monkeypatch.setattr(settings, "gemini_api_key", "")


class FakeGemini:
    """A controllable stand-in for
    `ai_understanding.understand_prescription` — records every call and
    returns whatever `.result` is currently set to (`None` by default,
    meaning "Gemini unavailable/fell back", exactly like the real function
    when unconfigured)."""

    def __init__(self) -> None:
        self.result: ai_understanding.UnderstoodResult | None = None
        self.calls: list[tuple[str, list[dict[str, str]]]] = []

    async def understand(self, raw_ocr_text, existing_entities):
        self.calls.append((raw_ocr_text, existing_entities))
        return self.result


@pytest.fixture
def fake_gemini(monkeypatch) -> FakeGemini:
    fake = FakeGemini()
    monkeypatch.setattr(ai_understanding, "understand_prescription", fake.understand)
    return fake


class FakeGeminiDoctor:
    """A controllable stand-in for
    `ai_understanding.validate_doctor_prescription` — the doctor-generated
    pipeline's counterpart to `FakeGemini` above. Records every call and
    returns whatever `.result` is currently set to (`None` by default,
    meaning "Gemini unavailable/fell back" — the doctor path then keeps
    exactly the deterministic `normalize_prescription_rows` result)."""

    def __init__(self) -> None:
        self.result: ai_understanding.UnderstoodResult | None = None
        self.calls: list[list[dict[str, str]]] = []

    async def validate(self, rows):
        self.calls.append(rows)
        return self.result


@pytest.fixture
def fake_gemini_doctor(monkeypatch) -> FakeGeminiDoctor:
    fake = FakeGeminiDoctor()
    monkeypatch.setattr(ai_understanding, "validate_doctor_prescription", fake.validate)
    return fake


async def login(client, email: str) -> None:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": TEST_PASSWORD})
    assert r.status_code == 200, r.text


@pytest.fixture
async def patient_user(client, db) -> Profile:
    user = await make_user(db, email="pat@x.com", role=UserRole.PATIENT)
    await login(client, "pat@x.com")
    return user


@pytest.fixture
async def second_patient(db) -> Profile:
    return await make_user(db, email="other@x.com", role=UserRole.PATIENT)


@asynccontextmanager
async def new_client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        yield c


async def upload(client, doc_type="prescription", name="rx.png", data=PNG, mime="image/png"):
    return await client.post(
        "/api/v1/documents",
        data={"document_type": doc_type},
        files={"file": (name, data, mime)},
    )


async def upload_and_process(client, fake_ocr, doc_type="prescription", text=RX_TEXT) -> dict:
    fake_ocr.text = text
    up = await upload(client, doc_type)
    assert up.status_code == 201, up.text
    doc_id = up.json()["id"]
    r = await client.post(f"/api/v1/documents/{doc_id}/process")
    assert r.status_code == 200, r.text
    return r.json()


def without_ocr_text(html: str) -> str:
    """The page minus the document's own raw text. That block is the original, read as it was —
    it still contains whatever the document says (a blood pressure included); "hidden" means the
    vital-signs SECTION is not on the page, not that the original text is edited."""
    return re.sub(r'<pre class="ocr-text".*?</pre>', "", html, flags=re.DOTALL)


def pakistan_day(moment: datetime) -> str:
    """The calendar day in Pakistan (UTC+5, no daylight saving) of a stored UTC timestamp, worked
    out here with plain arithmetic and not with the code under test. A test that computed
    "today" in UTC passed all day except between midnight and 05:00 in Pakistan."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (moment.astimezone(UTC) + timedelta(hours=5)).date().isoformat()


async def doc_row(db, doc_id) -> Document:
    stmt = select(Document).where(Document.id == uuid.UUID(str(doc_id)))
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one()


async def job_row(db, doc_id) -> OcrJob | None:
    stmt = select(OcrJob).where(OcrJob.document_id == uuid.UUID(str(doc_id)))
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one_or_none()


async def extraction_rows(db, doc_id) -> list[OcrExtraction]:
    job = await job_row(db, doc_id)
    if job is None:
        return []
    stmt = (
        select(OcrExtraction)
        .where(OcrExtraction.job_id == job.id)
        .order_by(OcrExtraction.row_index, OcrExtraction.field_name)
    )
    return list((await db.execute(stmt.execution_options(populate_existing=True))).scalars())


def confirm_body(detail: dict, edits: dict[tuple[int, str], str] | None = None, **extra) -> dict:
    """A confirm payload that keeps every proposed row, applying `edits`."""
    edits = edits or {}
    rows = [
        {
            "index": row["index"],
            "fields": {
                f["name"]: edits.get((row["index"], f["name"]), f["value"]) for f in row["fields"]
            },
        }
        for row in detail["rows"]
    ]
    return {"rows": rows, **extra}


# ═══ Upload ══════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    ("name", "data", "mime"),
    [
        ("rx.pdf", PDF, "application/pdf"),
        ("rx.jpg", JPG, "image/jpeg"),
        ("rx.jpeg", JPG, "image/jpeg"),
        ("rx.png", PNG, "image/png"),
    ],
)
async def test_upload_accepts_pdf_jpg_jpeg_png(
    client, db, fake_supabase, patient_user, name, data, mime
):
    r = await upload(client, "prescription", name, data, mime)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "uploaded"  # temporary — not a record
    assert body["document_type"] == "prescription"
    assert body["mime_type"] == mime and body["size_bytes"] == len(data)

    row = await doc_row(db, body["id"])
    patient = (
        await db.execute(select(Patient).where(Patient.user_id == patient_user.id))
    ).scalar_one()
    assert row.patient_id == patient.id and row.uploaded_by == patient_user.id
    assert row.doc_type == "rx" and row.status == DocumentStatus.UPLOADED.value
    # The original is in the private bucket, under a key that never uses the filename.
    stored = fake_supabase.storage.objects[(settings.document_bucket, row.object_key)]
    assert stored == data
    assert name not in row.object_key


@pytest.mark.parametrize(
    ("api_type", "db_type"),
    [("prescription", "rx"), ("lab_report", "lab"), ("vaccination", "vaccine"), ("other", "other")],
)
async def test_all_four_document_types_share_the_one_document_table(
    client, db, patient_user, api_type, db_type
):
    r = await upload(client, api_type)
    assert r.status_code == 201, r.text
    assert (await doc_row(db, r.json()["id"])).doc_type == db_type


async def test_upload_rejects_an_invalid_document_type(client, db, fake_supabase, patient_user):
    r = await upload(client, "x-ray-of-a-cat")
    assert r.status_code == 422
    assert "document_type" in r.json()["error"]["details"]["fields"]
    assert fake_supabase.storage.objects == {}
    assert (await db.execute(select(Document))).first() is None


@pytest.mark.parametrize(
    ("name", "data", "mime"),
    [
        ("notes.txt", b"just some text", "text/plain"),
        ("pic.gif", b"GIF89a" + b"\x00" * 20, "image/gif"),
        ("evil.pdf", b"MZ\x90\x00" + b"\x00" * 20, "application/pdf"),  # renamed executable
        ("mismatch.pdf", PNG, "application/pdf"),  # bytes say PNG, header says PDF
    ],
)
async def test_upload_rejects_unsupported_files_by_their_bytes(
    client, db, fake_supabase, patient_user, name, data, mime
):
    r = await upload(client, "prescription", name, data, mime)
    assert r.status_code == 415
    assert r.json()["error"]["code"] == "UNSUPPORTED_FILE_TYPE"
    assert fake_supabase.storage.objects == {}
    assert (await db.execute(select(Document))).first() is None


async def test_upload_rejects_files_over_10_mb(client, db, fake_supabase, patient_user):
    assert settings.document_max_bytes == 10 * 1024 * 1024
    r = await upload(client, data=PNG + b"\x00" * settings.document_max_bytes)
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "FILE_TOO_LARGE"
    assert fake_supabase.storage.objects == {}


async def test_the_limit_is_exact(client, patient_user, monkeypatch):
    monkeypatch.setattr(settings, "document_max_bytes", 100)
    assert (await upload(client, data=PNG + b"\x00" * (100 - len(PNG)))).status_code == 201
    assert (await upload(client, data=PNG + b"\x00" * (101 - len(PNG)))).status_code == 413


async def test_a_grossly_oversized_body_is_refused_before_it_is_read(
    client, patient_user, monkeypatch
):
    monkeypatch.setattr(settings, "document_max_bytes", 1024)
    r = await upload(client, data=PNG + b"\x00" * (1024 + 300 * 1024))
    assert r.status_code == 413  # by Content-Length, in middleware
    assert r.json()["error"]["code"] == "FILE_TOO_LARGE"


async def test_upload_rejects_an_empty_file(client, patient_user):
    r = await upload(client, data=b"")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "FILE_EMPTY"


async def test_a_storage_failure_records_nothing(client, db, fake_supabase, patient_user):
    fake_supabase.storage.fail_put = True
    r = await upload(client)
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "STORAGE_UNAVAILABLE"
    assert (await db.execute(select(Document))).first() is None


async def test_ownership_comes_from_the_session_not_the_request(
    client, db, patient_user, second_patient
):
    other = (
        await db.execute(select(Patient).where(Patient.user_id == second_patient.id))
    ).scalar_one()
    r = await client.post(
        "/api/v1/documents",
        data={"document_type": "prescription", "patient_id": str(other.id)},
        files={"file": ("rx.png", PNG, "image/png")},
    )
    assert r.status_code == 201
    mine = (
        await db.execute(select(Patient).where(Patient.user_id == patient_user.id))
    ).scalar_one()
    assert (await doc_row(db, r.json()["id"])).patient_id == mine.id


# ═══ Temporary vs finalized ══════════════════════════════════════════════
async def test_an_unconfirmed_upload_is_not_a_record_at_any_stage(client, fake_ocr, patient_user):
    up = await upload(client)
    assert (await client.get("/api/v1/documents")).json() == []
    in_progress = (await client.get("/api/v1/documents/in-progress")).json()
    assert [d["id"] for d in in_progress] == [up.json()["id"]]

    processed = await client.post(f"/api/v1/documents/{up.json()['id']}/process")
    assert processed.json()["status"] == "awaiting_review"
    # Read and extracted — still not a record until the patient confirms.
    assert (await client.get("/api/v1/documents")).json() == []
    assert len((await client.get("/api/v1/documents/in-progress")).json()) == 1


# ═══ Processing (the common pipeline) ════════════════════════════════════
async def test_processing_runs_common_ocr_once_and_stores_the_result(
    client, db, fake_ocr, fake_supabase, patient_user
):
    detail = await upload_and_process(client, fake_ocr)
    assert detail["status"] == "awaiting_review"

    # The common OCR service was invoked, once, with the stored original.
    assert fake_ocr.calls == [(PNG, "image/png")]

    row = await doc_row(db, detail["id"])
    assert row.ocr_text == RX_TEXT  # OCR text preserved
    job = await job_row(db, detail["id"])
    assert (job.status, job.engine, job.retry_count) == ("done", "tesseract", 0)
    assert job.started_at is not None and job.finished_at is not None


async def test_the_document_is_marked_processing_while_ocr_runs(
    client, db, sessionmaker_, fake_ocr, patient_user
):
    up = await upload(client)
    doc_id = up.json()["id"]
    seen: list[str] = []

    async def peek():
        async with sessionmaker_() as s:
            seen.append(
                (
                    await s.execute(select(Document.status).where(Document.id == uuid.UUID(doc_id)))
                ).scalar_one()
            )

    fake_ocr.hook = peek
    await client.post(f"/api/v1/documents/{doc_id}/process")
    assert seen == ["processing"]


@pytest.mark.parametrize(
    ("api_type", "text", "expected_fields", "expected_rows"),
    [
        ("prescription", RX_TEXT, ["medicine", "dosage", "frequency", "duration"], 2),
        ("lab_report", LAB_TEXT, ["test_name", "result", "unit", "reference_range"], 2),
        # A vaccination record's primary table is its doses; "other" has no primary table
        # (its diagnosis / symptoms / vitals / allergies are the other tables, see `groups`).
        (
            "vaccination",
            VACCINE_TEXT,
            ["vaccine_name", "dose", "date_given", "batch_no", "site", "next_due"],
            2,
        ),
        # an "other" document is read like a prescription (medicines, diagnosis, symptoms...)
        ("other", VACCINE_TEXT, ["medicine", "dosage", "frequency", "duration"], 0),
    ],
)
async def test_the_extractor_is_chosen_by_document_type(
    client, db, fake_ocr, patient_user, api_type, text, expected_fields, expected_rows
):
    detail = await upload_and_process(client, fake_ocr, api_type, text)
    assert detail["field_names"] == expected_fields
    assert len(detail["rows"]) == expected_rows
    assert detail["ocr_text"] == text  # kept for every type, even with no fields
    assert len(fake_ocr.calls) == 1  # one common OCR run regardless of type


async def test_only_the_matching_extractor_runs(client, fake_ocr, patient_user, monkeypatch):
    called: list[str] = []
    for doc_type in list(extractors.EXTRACTORS):

        def spy(text, today, _t=doc_type):
            called.append(_t)
            return extractors.Extraction()

        monkeypatch.setitem(extractors.EXTRACTORS, doc_type, spy)
    await upload_and_process(client, fake_ocr, "lab_report", LAB_TEXT)
    assert called == ["lab"]


async def test_extracted_values_are_returned_for_review(client, fake_ocr, patient_user):
    detail = await upload_and_process(client, fake_ocr)
    first = {f["name"]: f for f in detail["rows"][0]["fields"]}
    assert first["medicine"]["extracted_value"] == "Metformin"
    assert first["medicine"]["corrected_value"] is None
    assert first["medicine"]["value"] == "Metformin"
    assert first["dosage"]["value"] == "500mg"
    assert detail["document_date"] == "2024-08-11"  # the document's own date, proposed
    assert detail["finalized_at"] is None


HANDWRITTEN_GARBLED_MARKER_TEXT = """( D Paracetamol 500 mg
Take 1 tablet every 6 hours as needed
for fever or pain

G. Amoxicillin 500 mg
Take 1 capsule three times daily
for 5 days

3 Vitamin D3,000. IN.
Take ( tablet once daily after meal

( A_Cetirizing 10 mg
Take I tablet at night for allergy symptoms
"""


async def test_the_review_page_populates_fields_end_to_end_for_noisy_handwritten_ocr(
    client, fake_ocr, patient_user
):
    """Full-pipeline regression: OCR text (this shape, not any hardcoded
    medicine) reaches the rendered Review page with real, non-fabricated
    values in each recovered medicine's fields — not just the extractor
    function in isolation."""
    detail = await upload_and_process(
        client, fake_ocr, "prescription", HANDWRITTEN_GARBLED_MARKER_TEXT
    )
    doc_id = detail["id"]
    page = await client.get(f"/en/patient/documents/{doc_id}")
    html = page.text
    assert "( D Paracetamol" in html
    assert "( A_Cetirizing" in html
    amoxicillin_row = {f["name"]: f["value"] for f in detail["rows"][1]["fields"]}
    assert amoxicillin_row["medicine"] == "G. Amoxicillin"
    assert amoxicillin_row["dosage"] == "500 mg"
    assert amoxicillin_row["frequency"] == "three times daily"
    assert amoxicillin_row["duration"] == "5 days"
    assert "vitamin" not in amoxicillin_row["duration"].lower()


async def test_there_are_no_per_document_type_processing_endpoints(app):
    # (Included routers are nested in `app.routes`; the OpenAPI schema lists them flat.)
    paths = set(app.openapi()["paths"])
    assert not [p for p in paths if re.search(r"(prescription|lab|vaccin|other)[\w-]*/process", p)]
    assert "/api/v1/documents/{document_id}/process" in paths


@pytest.mark.parametrize(
    "code", [ocr.OCR_FAILED, ocr.OCR_UNAVAILABLE, ocr.OCR_EMPTY, ocr.OCR_TOO_LONG]
)
async def test_an_ocr_failure_is_a_failed_state_that_can_be_retried(
    client, db, fake_ocr, patient_user, code
):
    fake_ocr.error = code
    up = await upload(client)
    doc_id = up.json()["id"]
    failed = await client.post(f"/api/v1/documents/{doc_id}/process")
    assert failed.status_code == 200
    assert (failed.json()["status"], failed.json()["failure_code"]) == ("failed", code)
    assert (await job_row(db, doc_id)).status == "failed"
    assert (await extraction_rows(db, doc_id)) == []

    # Retry: the same OCR job is reset (one per document), nothing is duplicated.
    fake_ocr.error = None
    ok = await client.post(f"/api/v1/documents/{doc_id}/process")
    assert ok.json()["status"] == "awaiting_review" and ok.json()["failure_code"] is None
    job = await job_row(db, doc_id)
    assert (job.status, job.retry_count) == ("done", 1)
    cells = await extraction_rows(db, doc_id)
    medicine_cells = [
        c for c in cells if c.field_name in ("medicine", "dosage", "frequency", "duration")
    ]
    assert len(medicine_cells) == 8  # 2 medicines x 4 fields, once
    # ...plus the one vital-signs row every prescription always has (all nine fields, even
    # when empty), and not one cell anywhere appears twice.
    assert len(cells) == 8 + 9
    assert len({(c.row_index, c.field_name) for c in cells}) == len(cells)


async def test_an_extraction_bug_is_a_failed_state_not_a_500(
    client, fake_ocr, patient_user, monkeypatch
):
    def boom(text, today):
        raise RuntimeError("bug")

    monkeypatch.setitem(extractors.EXTRACTORS, "rx", boom)
    up = await upload(client)
    r = await client.post(f"/api/v1/documents/{up.json()['id']}/process")
    assert (r.status_code, r.json()["status"], r.json()["failure_code"]) == (
        200,
        "failed",
        "extraction_failed",
    )


async def test_a_lost_original_is_a_failed_state(client, fake_supabase, patient_user):
    up = await upload(client)
    fake_supabase.storage.fail_get = True
    r = await client.post(f"/api/v1/documents/{up.json()['id']}/process")
    assert (r.json()["status"], r.json()["failure_code"]) == ("failed", "storage_failed")


async def test_processing_an_already_read_document_does_not_run_ocr_again(
    client, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr)
    again = await client.post(f"/api/v1/documents/{detail['id']}/process")
    assert again.status_code == 200 and again.json()["status"] == "awaiting_review"
    assert len(fake_ocr.calls) == 1


async def test_a_document_being_read_cannot_be_started_twice(client, db, fake_ocr, patient_user):
    up = await upload(client)
    doc_id = up.json()["id"]
    await db.execute(
        update(Document).where(Document.id == uuid.UUID(doc_id)).values(status="processing")
    )
    db.add(
        OcrJob(
            id=uuid.uuid4(),
            document_id=uuid.UUID(doc_id),
            status="running",
            engine="tesseract",
            started_at=utcnow(),
        )
    )
    await db.commit()

    r = await client.post(f"/api/v1/documents/{doc_id}/process")
    assert r.status_code == 409 and r.json()["error"]["code"] == "PROCESSING_IN_PROGRESS"
    assert fake_ocr.calls == []


async def test_a_stale_processing_document_can_be_reprocessed(client, db, fake_ocr, patient_user):
    from datetime import timedelta

    up = await upload(client)
    doc_id = up.json()["id"]
    await db.execute(
        update(Document).where(Document.id == uuid.UUID(doc_id)).values(status="processing")
    )
    db.add(
        OcrJob(
            id=uuid.uuid4(),
            document_id=uuid.UUID(doc_id),
            status="running",
            engine="tesseract",
            started_at=utcnow() - timedelta(seconds=settings.document_processing_stale_s + 30),
        )
    )
    await db.commit()
    r = await client.post(f"/api/v1/documents/{doc_id}/process")
    assert r.status_code == 200 and r.json()["status"] == "awaiting_review"


async def test_a_late_ocr_result_never_resurrects_a_cancelled_upload(
    client, db, sessionmaker_, fake_ocr, patient_user
):
    up = await upload(client)
    doc_id = up.json()["id"]

    async def cancel_meanwhile():
        async with sessionmaker_() as s:
            await s.execute(
                update(Document).where(Document.id == uuid.UUID(doc_id)).values(status="cancelled")
            )
            await s.commit()

    fake_ocr.hook = cancel_meanwhile
    r = await client.post(f"/api/v1/documents/{doc_id}/process")
    assert r.json()["status"] == "cancelled"
    assert await extraction_rows(db, doc_id) == []


# ═══ Review, edit and Confirm & Save ═════════════════════════════════════
async def test_confirm_and_save_finalizes_and_keeps_both_values(
    client, db, fake_ocr, fake_supabase, patient_user
):
    detail = await upload_and_process(client, fake_ocr)
    body = confirm_body(detail, {(0, "dosage"): "500 mg"})  # the "Panadol 500mg" -> "500 mg" case

    r = await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "finalized" and out["finalized_at"] is not None
    assert out["rows"][0]["fields"][1] == {
        "name": "dosage",
        "extracted_value": "500mg",  # provenance preserved
        "corrected_value": "500 mg",  # the patient's value
        "value": "500 mg",  # the one that counts
        "confidence": 1.0,  # certainty computed at extraction time, unaffected by the edit
        "status": "human_verified",  # the patient has now been through Confirm & Save
    }

    row = await doc_row(db, detail["id"])
    assert row.status == "finalized" and row.finalized_at is not None
    exts = {(e.row_index, e.field_name): e for e in await extraction_rows(db, detail["id"])}
    dosage = exts[(0, "dosage")]
    assert (dosage.extracted_value, dosage.corrected_value) == ("500mg", "500 mg")
    assert dosage.reviewed_by == patient_user.id and dosage.reviewed_at is not None
    # An accepted-as-proposed value stores no correction.
    assert exts[(0, "medicine")].corrected_value is None
    # The original upload is still there and still linked.
    assert fake_supabase.storage.objects[(settings.document_bucket, row.object_key)] == PNG


async def test_a_finalized_record_is_available_once_to_downstream_consumers(
    client, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr)
    await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=confirm_body(detail))

    records = (await client.get("/api/v1/documents")).json()
    assert [r["id"] for r in records] == [detail["id"]]  # one record — no duplicate copies
    record = records[0]
    assert record["document_type"] == "prescription" and record["status"] == "finalized"
    assert record["document_date"] == "2024-08-11" and record["finalized_at"]
    assert record["original_filename"] == "rx.png" and record["mime_type"] == "image/png"
    assert record["ocr_text"] is None  # list responses omit the raw text
    assert [f["value"] for f in record["rows"][0]["fields"]] == [
        "Metformin",
        "500mg",
        "1-0-1",
        "30 days",
    ]
    assert (await client.get("/api/v1/documents/in-progress")).json() == []
    assert (await client.get("/api/v1/documents?type=lab_report")).json() == []
    assert len((await client.get("/api/v1/documents?type=prescription")).json()) == 1


async def test_review_can_remove_a_row_and_add_one_the_ocr_missed(
    client, db, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr)
    body = {
        "rows": [
            # row 0 (Metformin) removed by omission; row 1 kept as-is
            {"index": 1, "fields": {}},
            {"index": 2, "fields": {"medicine": "Panadol", "dosage": "500mg"}},  # added
        ]
    }
    r = await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=body)
    assert r.status_code == 200, r.text
    names = [row["fields"][0]["value"] for row in r.json()["rows"]]
    assert names == ["Atorvastatin", "Panadol"]  # the removed row is not part of the record
    added = [e for e in await extraction_rows(db, detail["id"]) if e.row_index == 2]
    assert all(e.extracted_value is None for e in added)  # provenance: not from the OCR
    assert {e.field_name: e.corrected_value for e in added}["medicine"] == "Panadol"


async def test_a_prescription_or_lab_report_needs_at_least_one_item(
    client, db, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr)
    r = await client.post(f"/api/v1/documents/{detail['id']}/confirm", json={"rows": []})
    assert r.status_code == 422
    assert (await doc_row(db, detail["id"])).status == "awaiting_review"  # nothing finalized


async def test_a_row_needs_its_main_field(client, fake_ocr, patient_user):
    detail = await upload_and_process(client, fake_ocr)
    body = confirm_body(detail, {(0, "medicine"): ""})
    assert (
        await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=body)
    ).status_code == 422


@pytest.mark.parametrize(
    "bad",
    [
        {"rows": [{"index": 0, "fields": {"blood_group": "O+"}}]},
        {"rows": [{"index": 0, "fields": {"medicine": "x" * 256}}]},
        {"rows": [{"index": 0, "fields": {}}, {"index": 0, "fields": {}}]},
    ],
)
async def test_confirm_rejects_unknown_fields_oversized_values_and_duplicate_rows(
    client, fake_ocr, patient_user, bad
):
    detail = await upload_and_process(client, fake_ocr)
    assert (
        await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=bad)
    ).status_code == 422


async def test_vaccination_and_other_finalize_with_text_and_date_only(
    client, db, fake_ocr, patient_user
):
    for api_type in ("vaccination", "other"):
        detail = await upload_and_process(client, fake_ocr, api_type, VACCINE_TEXT)
        r = await client.post(
            f"/api/v1/documents/{detail['id']}/confirm",
            json={"rows": [], "document_date": "2024-03-02"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "finalized" and r.json()["document_date"] == "2024-03-02"
        assert (await doc_row(db, detail["id"])).ocr_text == VACCINE_TEXT
    assert len((await client.get("/api/v1/documents")).json()) == 2


async def test_the_reviewed_document_date_is_validated(client, fake_ocr, patient_user):
    detail = await upload_and_process(client, fake_ocr)
    future = await client.post(
        f"/api/v1/documents/{detail['id']}/confirm",
        json={**confirm_body(detail), "document_date": "2999-01-01"},
    )
    assert future.status_code == 422
    cleared = await client.post(
        f"/api/v1/documents/{detail['id']}/confirm",
        json={**confirm_body(detail), "document_date": None},
    )
    assert cleared.status_code == 200 and cleared.json()["document_date"] is None


async def test_confirming_twice_changes_nothing_the_second_time(client, db, fake_ocr, patient_user):
    detail = await upload_and_process(client, fake_ocr)
    first = await client.post(
        f"/api/v1/documents/{detail['id']}/confirm", json=confirm_body(detail)
    )
    edited = confirm_body(detail, {(0, "medicine"): "Something else"})
    second = await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=edited)
    assert second.status_code == 200
    assert second.json()["finalized_at"] == first.json()["finalized_at"]
    assert (
        second.json()["rows"][0]["fields"][0]["value"] == "Metformin"
    )  # the second body was ignored
    confirms = (
        (await db.execute(select(AuditLog).where(AuditLog.action == audit.DOCUMENT_CONFIRM)))
        .scalars()
        .all()
    )
    assert len(confirms) == 1


async def test_only_a_reviewable_document_can_be_confirmed(client, fake_ocr, patient_user):
    up = await upload(client)  # uploaded, never read
    r = await client.post(f"/api/v1/documents/{up.json()['id']}/confirm", json={"rows": []})
    assert r.status_code == 409 and r.json()["error"]["code"] == "INVALID_DOCUMENT_STATE"


# ═══ Cancel ══════════════════════════════════════════════════════════════
async def test_cancelling_before_confirmation_never_finalizes(
    client, db, fake_ocr, fake_supabase, patient_user
):
    detail = await upload_and_process(client, fake_ocr)
    row = await doc_row(db, detail["id"])
    key = row.object_key

    r = await client.post(f"/api/v1/documents/{detail['id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"

    row = await doc_row(db, detail["id"])
    assert row.status == "cancelled" and row.finalized_at is None
    assert (
        row.ocr_text is None and await extraction_rows(db, detail["id"]) == []
    )  # derived data purged
    assert (settings.document_bucket, key) not in fake_supabase.storage.objects  # temp file removed
    assert (await client.get("/api/v1/documents")).json() == []
    assert (await client.get("/api/v1/documents/in-progress")).json() == []
    # A cancelled upload cannot be confirmed, read, or downloaded.
    assert (
        await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=confirm_body(detail))
    ).status_code == 409
    assert (await client.get(f"/api/v1/documents/{detail['id']}/file")).status_code == 404
    # Cancelling again is harmless.
    assert (await client.post(f"/api/v1/documents/{detail['id']}/cancel")).status_code == 200


async def test_a_finalized_record_cannot_be_cancelled(client, fake_ocr, patient_user):
    detail = await upload_and_process(client, fake_ocr)
    await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=confirm_body(detail))
    r = await client.post(f"/api/v1/documents/{detail['id']}/cancel")
    assert r.status_code == 409
    assert (await client.get(f"/api/v1/documents/{detail['id']}")).json()["status"] == "finalized"


# ═══ Security ════════════════════════════════════════════════════════════
ENDPOINTS = [
    ("post", "/api/v1/documents"),
    ("get", "/api/v1/documents"),
    ("get", "/api/v1/documents/in-progress"),
    ("get", f"/api/v1/documents/{uuid.uuid4()}"),
    ("post", f"/api/v1/documents/{uuid.uuid4()}/process"),
    ("post", f"/api/v1/documents/{uuid.uuid4()}/confirm"),
    ("post", f"/api/v1/documents/{uuid.uuid4()}/cancel"),
    ("get", f"/api/v1/documents/{uuid.uuid4()}/file"),
]


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
async def test_every_endpoint_requires_authentication(client, method, path):
    r = await getattr(client, method)(path)
    assert r.status_code == 401


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
async def test_a_doctor_has_no_access(client, db, method, path):
    await make_user(db, email="doc@x.com", role=UserRole.DOCTOR, is_verified=False)
    await login(client, "doc@x.com")
    r = await getattr(client, method)(path)
    assert r.status_code == 403


async def test_a_patient_cannot_reach_another_patients_document(
    app, client, db, fake_ocr, patient_user, second_patient
):
    detail = await upload_and_process(client, fake_ocr)
    doc_id = detail["id"]

    async with new_client(app) as other:
        await login(other, "other@x.com")
        foreign = [
            await other.get(f"/api/v1/documents/{doc_id}"),
            await other.get(f"/api/v1/documents/{doc_id}/file"),
            await other.post(f"/api/v1/documents/{doc_id}/process"),
            await other.post(f"/api/v1/documents/{doc_id}/confirm", json=confirm_body(detail)),
            await other.post(f"/api/v1/documents/{doc_id}/cancel"),
        ]
        missing = await other.get(f"/api/v1/documents/{uuid.uuid4()}")
        assert [r.status_code for r in foreign] == [404] * 5
        # Identical to "does not exist": an id can never be probed.
        for r in foreign:
            a, b = r.json()["error"], missing.json()["error"]
            assert {k: a[k] for k in ("code", "message", "details")} == {
                k: b[k] for k in ("code", "message", "details")
            }
        assert (await other.get("/api/v1/documents")).json() == []
        assert (await other.get("/api/v1/documents/in-progress")).json() == []

    # Nothing the other patient tried changed the document.
    row = await doc_row(db, doc_id)
    assert row.status == "awaiting_review"
    assert len(fake_ocr.calls) == 1


async def test_the_original_is_served_only_to_its_owner_with_safe_headers(
    client, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr)
    r = await client.get(f"/api/v1/documents/{detail['id']}/file")
    assert r.status_code == 200 and r.content == PNG
    assert r.headers["content-type"] == "image/png"
    assert "no-store" in r.headers["cache-control"]
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_document_writes_are_rate_limited(client, patient_user, monkeypatch):
    monkeypatch.setattr(settings, "document_write_rate_limit_per_minute", 2)
    assert (await upload(client)).status_code == 201
    assert (await upload(client)).status_code == 201
    r = await upload(client)
    assert r.status_code == 429 and r.headers["retry-after"] == "60"


async def test_audit_rows_are_written_without_any_document_content(
    client, db, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr)
    await client.post(
        f"/api/v1/documents/{detail['id']}/confirm",
        json=confirm_body(detail, {(0, "dosage"): "999mg"}),
    )
    rows = (
        (await db.execute(select(AuditLog).where(AuditLog.action.like("document.%"))))
        .scalars()
        .all()
    )
    assert sorted(r.action for r in rows) == [
        "document.confirm",
        "document.process",
        "document.upload",
    ]
    blob = " ".join(r.detail for r in rows)
    for secret in ("Metformin", "Atorvastatin", "500mg", "999mg", "Iqbal"):
        assert secret not in blob
    assert all(r.actor_user_id == patient_user.id and r.resource_type == "document" for r in rows)


# ═══ Web pages: the artifact-style flow, end to end ══════════════════════
def _web_upload(client, doc_type="prescription", name="rx.png", data=PNG, mime="image/png"):
    return client.post(
        "/en/patient/documents",
        data={"document_type": doc_type},
        files={"file": (name, data, mime)},
    )


def _doc_id_from(location: str) -> str:
    m = re.search(r"/patient/documents/([0-9a-f-]{36})", location)
    assert m, location
    return m.group(1)


async def test_web_pages_require_a_patient_session(client):
    for path in (
        "/en/patient/documents",
        "/en/patient/documents/new",
        f"/en/patient/documents/{uuid.uuid4()}",
    ):
        r = await client.get(path)
        assert r.status_code == 303 and r.headers["location"].startswith("/en/login?next=")


async def test_web_a_doctor_is_sent_to_their_own_area(client, db):
    await make_user(db, email="doc@x.com", role=UserRole.DOCTOR)
    await login(client, "doc@x.com")
    r = await client.get("/en/patient/documents")
    assert r.status_code == 303 and r.headers["location"] == "/en/doctor"


async def test_web_upload_page_offers_the_four_types_and_the_limits(client, patient_user):
    r = await client.get("/en/patient/documents/new")
    assert r.status_code == 200
    for label in ("Prescription", "Lab report", "Vaccination record", "Other medical document"):
        assert label in r.text
    assert 'enctype="multipart/form-data"' in r.text
    assert "PDF, JPG or PNG · up to 10 MB" in r.text
    assert 'href="/en/patient/documents"' in r.text  # the Documents nav link


async def test_web_full_flow_upload_review_edit_confirm_and_refresh(
    client, db, fake_ocr, patient_user
):
    up = await _web_upload(client)
    assert up.status_code == 303
    doc_id = _doc_id_from(up.headers["location"])

    # Review: original beside the extracted, editable values. Not yet a record.
    review = await client.get(up.headers["location"])
    assert review.status_code == 200
    assert "Confirm &amp; Save" in review.text
    assert 'name="row-0-medicine"' in review.text and 'value="Metformin"' in review.text
    assert f"/en/patient/documents/{doc_id}/file" in review.text
    listing = await client.get("/en/patient/documents")
    assert "Awaiting your review" in listing.text and "Your saved records" in listing.text
    assert "No saved documents yet." in listing.text

    # Edit a value and Confirm & Save.
    form = {
        "row-0-medicine": "Metformin",
        "row-0-dosage": "500 mg",
        "row-0-frequency": "1-0-1",
        "row-0-duration": "30 days",
        "row-1-medicine": "Atorvastatin",
        "row-1-dosage": "10 mg",
        "row-1-frequency": "once daily",
        "row-1-duration": "30 days",
        "document_date": "2024-08-11",
    }
    saved = await client.post(f"/en/patient/documents/{doc_id}/confirm", data=form)
    assert saved.status_code == 303 and saved.headers["location"].endswith("?saved=1")

    page = await client.get(saved.headers["location"])
    assert "Saved to your record" in page.text and "500 mg" in page.text
    assert "Edited by you" in page.text  # the corrected dosage is marked
    # Refreshing after saving still shows the finalized record.
    again = await client.get(f"/en/patient/documents/{doc_id}")
    assert (
        again.status_code == 200
        and "500 mg" in again.text
        and "Confirm &amp; Save" not in again.text
    )
    assert (await doc_row(db, doc_id)).status == "finalized"

    listing = await client.get("/en/patient/documents")
    assert "No saved documents yet." not in listing.text and "rx.png" in listing.text
    assert (await client.get(f"/en/patient/documents/{doc_id}/file")).content == PNG


async def test_web_all_four_types_use_the_same_pages(client, fake_ocr, patient_user):
    for doc_type, text in (
        ("prescription", RX_TEXT),
        ("lab_report", LAB_TEXT),
        ("vaccination", VACCINE_TEXT),
        ("other", VACCINE_TEXT),
    ):
        fake_ocr.text = text
        up = await _web_upload(client, doc_type)
        assert up.status_code == 303
        page = await client.get(up.headers["location"])
        assert page.status_code == 200 and "Confirm &amp; Save" in page.text
        if doc_type == "lab_report":
            assert 'name="row-0-test_name"' in page.text
        if doc_type == "vaccination":
            assert 'name="row-0-vaccine_name"' in page.text  # the doses are reviewable rows
        if doc_type == "other":
            # an "other" document is reviewed through the same tables a prescription has:
            # medicines, diagnosis, symptoms, vital signs, allergies
            assert 'name="row-0-medicine"' in page.text
            assert 'name="row-1000-diagnosis"' in page.text
            assert 'name="row-3000-blood_pressure"' in page.text
        if doc_type in ("vaccination", "other"):
            assert "No individual details are extracted" not in page.text
            assert 'name="document_date"' in page.text


@pytest.mark.parametrize(
    ("name", "data", "mime", "code", "message"),
    [
        (
            "notes.txt",
            b"plain text",
            "text/plain",
            415,
            "Only PDF, JPG and PNG files can be uploaded.",
        ),
        ("empty.png", b"", "image/png", 422, "That file is empty."),
    ],
)
async def test_web_upload_errors_are_shown_on_the_form(
    client, patient_user, name, data, mime, code, message
):
    r = await _web_upload(client, "prescription", name, data, mime)
    assert r.status_code == code and message in r.text
    assert "checked" in r.text  # the chosen document type is kept


async def test_web_oversized_upload_shows_the_limit(client, patient_user, monkeypatch):
    monkeypatch.setattr(settings, "document_max_bytes", 2 * 1024 * 1024)
    r = await _web_upload(client, data=PNG + bytes(2 * 1024 * 1024))
    assert r.status_code == 413
    assert "That file is too large. The limit is 2 MB." in r.text


async def test_web_missing_type_or_file_is_a_field_error(client, patient_user):
    no_type = await client.post(
        "/en/patient/documents", data={}, files={"file": ("a.png", PNG, "image/png")}
    )
    assert (
        no_type.status_code == 422
        and translate("errors.document_type_invalid", "en") in no_type.text
    )
    no_file = await client.post("/en/patient/documents", data={"document_type": "prescription"})
    assert no_file.status_code == 422 and translate("errors.file_required", "en") in no_file.text


async def test_web_ocr_failure_offers_retry_and_discard(client, db, fake_ocr, patient_user):
    fake_ocr.error = ocr.OCR_UNAVAILABLE
    up = await _web_upload(client)
    assert up.status_code == 303  # not an error page
    doc_id = _doc_id_from(up.headers["location"])
    page = await client.get(up.headers["location"])
    assert escape(translate("errors.ocr_unavailable", "en")) in page.text
    assert "Try again" in page.text and "Discard" in page.text

    fake_ocr.error = None
    retry = await client.post(f"/en/patient/documents/{doc_id}/process")
    assert retry.status_code == 303
    assert "Confirm &amp; Save" in (await client.get(retry.headers["location"])).text


async def test_web_upload_does_not_re_read_storage_for_the_bytes_it_just_stored(
    client, db, fake_ocr, patient_user, monkeypatch
):
    """The web upload route uploads the file, then immediately processes it in
    the same request. `process()` used to re-download those exact bytes from
    Storage a moment after storing them — a wasted round-trip. It must now
    reuse what the upload already read, calling `storage.get` zero times."""
    gets, puts = [], []
    orig_get, orig_put = doc_storage.get, doc_storage.put

    async def counted_get(key):
        gets.append(key)
        return await orig_get(key)

    async def counted_put(key, data, mime):
        puts.append(key)
        return await orig_put(key, data, mime)

    monkeypatch.setattr(doc_storage, "get", counted_get)
    monkeypatch.setattr(doc_storage, "put", counted_put)

    fake_ocr.error = ocr.OCR_UNAVAILABLE  # force a real re-process on retry, below
    up = await _web_upload(client)
    assert up.status_code == 303
    assert len(puts) == 1
    assert gets == []  # the whole point of the fix: no read-back after the write

    # A retry has no bytes already in hand (nothing was just uploaded), so it
    # must still fetch the original from Storage exactly as before the fix.
    fake_ocr.error = None
    doc_id = _doc_id_from(up.headers["location"])
    retry = await client.post(f"/en/patient/documents/{doc_id}/process")
    assert retry.status_code == 303
    assert gets == [puts[0]]


async def test_web_confirm_errors_keep_what_the_patient_typed(client, db, fake_ocr, patient_user):
    up = await _web_upload(client)
    doc_id = _doc_id_from(up.headers["location"])
    # Every row removed -> "add at least one item"; the typed date is kept.
    form = {
        "remove_row": ["0", "1"],
        "row-0-medicine": "Metformin",
        "row-1-medicine": "Atorvastatin",
        "document_date": "2024-08-11",
    }
    r = await client.post(f"/en/patient/documents/{doc_id}/confirm", data=form)
    assert r.status_code == 422 and translate("errors.document_rows_required", "en") in r.text
    assert 'value="2024-08-11"' in r.text
    assert (await doc_row(db, doc_id)).status == "awaiting_review"

    bad_date = await client.post(
        f"/en/patient/documents/{doc_id}/confirm", data={"document_date": "not-a-date"}
    )
    assert bad_date.status_code == 422 and translate("errors.date_invalid", "en") in bad_date.text


async def test_web_discard_before_confirming_saves_nothing(client, db, fake_ocr, patient_user):
    up = await _web_upload(client)
    doc_id = _doc_id_from(up.headers["location"])
    r = await client.post(f"/en/patient/documents/{doc_id}/cancel")
    assert r.status_code == 303 and r.headers["location"] == "/en/patient/documents?saved=cancelled"
    assert (await doc_row(db, doc_id)).status == "cancelled"
    listing = await client.get(r.headers["location"])
    assert "The upload was discarded. Nothing was saved." in listing.text
    assert "No saved documents yet." in listing.text
    assert "Awaiting your review" not in listing.text


async def test_web_another_patients_document_is_a_plain_not_found(
    app, client, fake_ocr, patient_user, second_patient
):
    up = await _web_upload(client)
    doc_id = _doc_id_from(up.headers["location"])
    async with new_client(app) as other:
        await login(other, "other@x.com")
        page = await other.get(f"/en/patient/documents/{doc_id}")
        assert page.status_code == 404 and escape(translate("errors.not_found", "en")) in page.text
        assert "rx.png" not in page.text
        assert (await other.get(f"/en/patient/documents/{doc_id}/file")).status_code == 404
        for action in ("process", "confirm", "cancel"):
            r = await other.post(f"/en/patient/documents/{doc_id}/{action}", data={})
            assert r.status_code in (303, 404)
    # The owner's document is untouched.
    assert "Confirm &amp; Save" in (await client.get(f"/en/patient/documents/{doc_id}")).text


async def test_web_a_malformed_id_is_a_not_found_page_not_a_crash(client, patient_user):
    r = await client.get("/en/patient/documents/not-a-uuid")
    assert r.status_code == 404


async def test_web_urdu_pages_render_right_to_left(client, fake_ocr, patient_user):
    r = await client.get("/ur/patient/documents/new")
    assert r.status_code == 200 and 'dir="rtl"' in r.text
    assert translate("documents.upload.title", "ur") in r.text
    up = await client.post(
        "/ur/patient/documents",
        data={"document_type": "prescription"},
        files={"file": ("rx.png", PNG, "image/png")},
    )
    page = await client.get(up.headers["location"])
    assert page.status_code == 200 and translate("documents.action.confirm", "ur") in page.text


async def test_web_ocr_text_is_escaped_never_rendered_as_markup(client, fake_ocr, patient_user):
    fake_ocr.text = "<script>alert(1)</script>\nTab. Metformin 500mg 1-0-1 x 30 days"
    up = await _web_upload(client)
    page = await client.get(up.headers["location"])
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text


# ═══ Printed documents only: the one OCR engine is Tesseract ═════════════
async def upload_plain(client, doc_type="prescription", **extra_form):
    return await client.post(
        "/api/v1/documents",
        data={"document_type": doc_type, **extra_form},
        files={"file": ("rx.png", PNG, "image/png")},
    )


async def test_a_document_is_read_as_printed_and_the_api_no_longer_reports_a_mode(
    client, db, fake_ocr, patient_user
):
    up = await upload_plain(client)
    assert up.status_code == 201 and "ocr_mode" not in up.json()
    assert (await doc_row(db, up.json()["id"])).ocr_mode == "printed"


async def test_an_old_client_still_sending_a_handwritten_choice_is_read_as_printed(
    client, db, fake_ocr, patient_user
):
    """Handwriting is no longer offered. An `ocr_mode` field from an older form is
    ignored — never an error, and never a reason to use another engine."""
    up = await upload_plain(client, ocr_mode="handwritten")
    assert up.status_code == 201
    assert (await doc_row(db, up.json()["id"])).ocr_mode == "printed"
    r = await client.post(f"/api/v1/documents/{up.json()['id']}/process?ocr_mode=handwritten")
    assert r.status_code == 200 and r.json()["status"] == "awaiting_review"
    job = await job_row(db, up.json()["id"])
    assert (job.engine, job.status) == ("tesseract", "done")


async def test_the_common_pipeline_reads_every_type_with_tesseract(
    client, db, fake_ocr, patient_user
):
    up = await upload_plain(client)
    r = await client.post(f"/api/v1/documents/{up.json()['id']}/process")
    assert r.status_code == 200 and r.json()["status"] == "awaiting_review"
    assert len(fake_ocr.calls) == 1
    job = await job_row(db, up.json()["id"])
    assert (job.engine, job.status) == ("tesseract", "done")
    assert r.json()["rows"][0]["fields"][0]["extracted_value"] == "Metformin"


async def test_ocr_is_a_proposal_and_never_finalizes_on_its_own(client, db, fake_ocr, patient_user):
    up = await upload_plain(client)
    doc_id = up.json()["id"]
    processed = await client.post(f"/api/v1/documents/{doc_id}/process")
    assert processed.json()["status"] == "awaiting_review"
    assert (await client.get("/api/v1/documents")).json() == []  # not a record yet
    assert (await doc_row(db, doc_id)).finalized_at is None

    done = await client.post(
        f"/api/v1/documents/{doc_id}/confirm", json=confirm_body(processed.json())
    )
    assert done.json()["status"] == "finalized"  # only Confirm & Save does that
    assert [r["id"] for r in (await client.get("/api/v1/documents")).json()] == [doc_id]


async def test_a_failed_document_can_be_retried(client, db, fake_ocr, patient_user):
    fake_ocr.error = ocr.OCR_EMPTY
    up = await upload_plain(client)
    doc_id = up.json()["id"]
    failed = await client.post(f"/api/v1/documents/{doc_id}/process")
    assert (failed.json()["status"], failed.json()["failure_code"]) == ("failed", ocr.OCR_EMPTY)

    fake_ocr.error = None
    retried = await client.post(f"/api/v1/documents/{doc_id}/process")
    assert retried.json()["status"] == "awaiting_review"
    assert (await job_row(db, doc_id)).engine == "tesseract"  # one job per document, reset on retry


async def test_a_document_an_earlier_version_marked_handwritten_is_read_as_printed_on_retry(
    client, db, fake_ocr, patient_user
):
    fake_ocr.error = ocr.OCR_FAILED
    up = await upload_plain(client)
    doc_id = up.json()["id"]
    await client.post(f"/api/v1/documents/{doc_id}/process")
    await db.execute(
        update(Document).where(Document.id == uuid.UUID(doc_id)).values(ocr_mode="handwritten")
    )
    await db.commit()

    fake_ocr.error = None
    retried = await client.post(f"/api/v1/documents/{doc_id}/process")
    assert retried.json()["status"] == "awaiting_review"
    assert (await doc_row(db, doc_id)).ocr_mode == "printed"
    assert (await job_row(db, doc_id)).engine == "tesseract"


async def test_another_patient_cannot_re_run_ocr_on_my_document(
    app, client, fake_ocr, patient_user, second_patient
):
    up = await upload_plain(client)
    async with new_client(app) as other:
        await login(other, "other@x.com")
        r = await other.post(f"/api/v1/documents/{up.json()['id']}/process")
        assert r.status_code == 404
    assert fake_ocr.calls == []


async def test_the_audit_trail_records_the_engine_but_still_no_document_content(
    client, db, fake_ocr, patient_user
):
    up = await upload_plain(client)
    await client.post(f"/api/v1/documents/{up.json()['id']}/process")
    rows = (
        (await db.execute(select(AuditLog).where(AuditLog.action.like("document.%"))))
        .scalars()
        .all()
    )
    blob = " ".join(r.detail for r in rows)
    assert '"engine": "tesseract"' in blob and "ocr_mode" not in blob
    assert "Metformin" not in blob


async def test_web_upload_offers_printed_documents_only(client, patient_user):
    page = (await client.get("/en/patient/documents/new")).text
    assert 'name="ocr_mode"' not in page
    assert "Handwritten document" not in page and "TrOCR" not in page
    assert "Printed documents only" in page


async def test_web_upload_ignores_an_old_handwritten_field(client, db, fake_ocr, patient_user):
    up = await client.post(
        "/en/patient/documents",
        data={"document_type": "prescription", "ocr_mode": "handwritten"},
        files={"file": ("rx.png", PNG, "image/png")},
    )
    assert up.status_code == 303
    doc_id = _doc_id_from(up.headers["location"])
    page = (await client.get(up.headers["location"])).text
    assert "Handwritten" not in page and "Confirm &amp; Save" in page  # still review-first
    assert (await doc_row(db, doc_id)).ocr_mode == "printed"


async def test_web_a_failed_read_offers_a_plain_retry(client, db, fake_ocr, patient_user):
    fake_ocr.error = ocr.OCR_EMPTY
    up = await client.post(
        "/en/patient/documents",
        data={"document_type": "prescription"},
        files={"file": ("rx.png", PNG, "image/png")},
    )
    doc_id = _doc_id_from(up.headers["location"])
    page = (await client.get(up.headers["location"])).text
    assert "Only printed documents can be read" in page
    assert 'name="ocr_mode"' not in page and "handwritten option" not in page

    fake_ocr.error = None
    retry = await client.post(f"/en/patient/documents/{doc_id}/process")
    assert retry.status_code == 303
    assert "Confirm &amp; Save" in (await client.get(retry.headers["location"])).text


# ═══ Records list: no query-per-document (was an N+1) ═════════════════════
async def _finalize_one(client, fake_ocr) -> None:
    detail = await upload_and_process(client, fake_ocr)
    r = await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=confirm_body(detail))
    assert r.status_code == 200, r.text


async def test_records_list_query_count_does_not_grow_with_the_number_of_documents(
    client, fake_ocr, patient_user, monkeypatch
):
    """`list_finalized` used to run one extra query per document (its OCR job,
    then its extraction rows) — cheap on local SQLite, but a real round-trip
    each on a remote Postgres. Confirms the batched version runs the SAME
    number of queries whether there is 1 finalized document or 3."""
    from sqlalchemy.ext.asyncio import AsyncSession

    calls: list[int] = []
    orig_execute = AsyncSession.execute

    async def counted_execute(self, *a, **kw):
        calls.append(1)
        return await orig_execute(self, *a, **kw)

    monkeypatch.setattr(AsyncSession, "execute", counted_execute)

    await _finalize_one(client, fake_ocr)
    calls.clear()
    r1 = await client.get("/api/v1/documents")
    assert r1.status_code == 200 and len(r1.json()) == 1
    queries_for_one = len(calls)

    await _finalize_one(client, fake_ocr)
    await _finalize_one(client, fake_ocr)
    calls.clear()
    r3 = await client.get("/api/v1/documents")
    assert r3.status_code == 200 and len(r3.json()) == 3
    queries_for_three = len(calls)

    assert queries_for_three == queries_for_one  # flat, not one more per document


async def test_records_list_handles_zero_documents(client, patient_user):
    r = await client.get("/api/v1/documents")
    assert r.status_code == 200 and r.json() == []


async def test_records_list_rows_are_unchanged_for_multiple_documents(
    client, fake_ocr, patient_user
):
    first = await upload_and_process(client, fake_ocr, "prescription", RX_TEXT)
    await client.post(
        f"/api/v1/documents/{first['id']}/confirm",
        json=confirm_body(first, {(0, "dosage"): "999mg"}),
    )
    second = await upload_and_process(client, fake_ocr, "lab_report", LAB_TEXT)
    await client.post(f"/api/v1/documents/{second['id']}/confirm", json=confirm_body(second))

    listed = (await client.get("/api/v1/documents")).json()
    by_id = {d["id"]: d for d in listed}
    assert by_id[first["id"]]["rows"][0]["fields"][1]["value"] == "999mg"  # dosage, corrected
    assert by_id[first["id"]]["field_names"] == ["medicine", "dosage", "frequency", "duration"]
    assert by_id[second["id"]]["field_names"] == ["test_name", "result", "unit", "reference_range"]
    assert len(by_id[second["id"]]["rows"]) == 2  # HbA1c, Fasting Glucose


FOUR_MEDICINE_COMPOUND_TEXT = """1 Metformin 500 mg 500 mg Twice daily 30 days
(Tab) after meals

2 Atorvastatin 10 mg 10 mg Once daily 30 days
(Tab) at night

3 Losartan 50 mg 50 mg Once daily 30 days
(Tab) in the morning

4 Calcium + Vitamin D3 600 mg / 400 IU Once daily 30 days
(Tab) after breakfast
"""


async def test_all_four_structured_entities_reach_the_rendered_review_page(
    client, fake_ocr, patient_user
):
    """Not just `parse_prescription_entities` in isolation: the full pipeline
    (upload -> process -> persisted `OcrExtraction` rows -> rendered Review
    HTML) must carry every recovered medicine through to the actual page the
    patient sees, including a compound name ("+") and a compound dosage
    ("/")."""
    detail = await upload_and_process(client, fake_ocr, "prescription", FOUR_MEDICINE_COMPOUND_TEXT)
    doc_id = detail["id"]
    assert len(detail["rows"]) == 4
    page = await client.get(f"/en/patient/documents/{doc_id}")
    html = page.text
    for name in ("Metformin", "Atorvastatin", "Losartan", "Calcium + Vitamin D3"):
        assert f'value="{name}"' in html, f"{name} missing from rendered review page"
    assert 'value="600 mg / 400 IU"' in html


# ═══ Finalized View/Details: original document + structured sections ═════
FULL_PRESCRIPTION_TEXT = """City Health Clinic
General Practice & Family Medicine
Healthy Families - Better Tomorrow
Shop No. 12, Main Boulevard
Lahore, Pakistan
Ph: 042-3587 1234
Email: info@cityhealthclinic.pk
Patient Name: Ayesha Khan
Age / Gender: 32 years / Female
Address: House No. 45, Model Town, Lahore
Date: 27-09-2026
Patient ID: 2026-4587
Rx
1. Paracetamol 500 mg
Take 1 tablet every 6 hours as needed
for fever or pain.
2. Amoxicillin 500 mg
Take 1 capsule three times daily
for 5 days.
Notes:
- Drink plenty of water.
- If symptoms persist or worsen, please visit again.
Dr. Sara Ahmed
MBBS, FCPS (Internal Medicine)
Consultant Physician"""

FULL_LAB_TEXT = """Northside Diagnostic Center
Phone: 021-1112223
Patient Name: Bilal Hussain
Age: 47
Test | Result | Unit | Reference Range
Creatinine 1.1 mg/dL 0.7-1.3
Notes:
- Slightly elevated, recommend follow-up.
Doctor: Dr. Imran Qureshi"""

# The real (heavily degraded) handwritten OCR text captured earlier in this
# engagement, used here only to prove the structuring stage degrades
# gracefully — never crashes, never fabricates — on genuinely noisy input.
REAL_HANDWRITTEN_TEXT = """Cry city Health Clinic . Shop No. 12 Main Boulevard
Mr. General practice a family Medicine . . p. 02327234
# Healthy Families " Better Tomorrow . # Email_infoccityhealthclinic_pk
patientName . Ayesha Khan ... . # Dates " 27-09-2026
Age ( Gender : 32 years ! Female " " I Will Patient I'D : 2026-4587
Address : ' House No. 45 , Model Town , Lahore
( D. Paracetamol . 500 mg. 100 000
Take 1 tablet . every 6 hours as needed
for fever . or pain .
notes :
- Drink plenty of water . . 0 0 1
- If symptoms persist or worsen , " Mrs.
please visit again . 0 0 0 0 0 000
a b c d e f a b c d e f g h ( 1 0 Drive . Dr. Sara Ahmed
MBBS , FCS ( Internal Medicine )
Consultant Physician"""


async def test_the_finalized_page_shows_structured_sections_for_a_prescription(
    client, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", FULL_PRESCRIPTION_TEXT)
    await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=confirm_body(detail))
    page = await client.get(f"/en/patient/documents/{detail['id']}")
    html = page.text
    # Who it is from, for whom, and when — as the document says it, then upload day.
    assert "Document details" in html
    for label, value in (
        ("Hospital / Clinic Name", "City Health Clinic"),
        ("Doctor Name", "Dr. Sara Ahmed"),
        ("Patient Name", "Ayesha Khan"),
    ):
        assert label in html and value in html, label
    assert "Uploaded on" in html
    # ...and NOTHING else about the document: no age, id, address, qualifications.
    page = without_ocr_text(html)
    for gone in (
        "Hospital / Clinic Information",
        "Patient Information",
        "Doctor Information",
        "Prescription Information",
        "MBBS, FCPS",
        "Internal Medicine",
        "Model Town",
        "2026-4587",
        "info@cityhealthclinic.pk",
    ):
        assert gone not in page, gone
    assert ">Advice</h3>" in html
    assert "Drink plenty of water." in html
    assert "If symptoms persist or worsen, please visit again." in html
    # The original uploaded document is still shown, inline (a PNG image).
    assert f'src="/en/patient/documents/{detail["id"]}/file"' in html
    # The raw OCR text is preserved, not deleted — just folded away.
    assert "Text read from your document" in html
    assert "<details>" in html and "<details open>" not in html


UNREADABLE_GENDER_TEXT = """City Health Clinic
Patient Name: Ayesha Khan
Gender: Female this is clearly not a real gender value at all
Date: 27-09-2026
Rx
1. Paracetamol 500 mg"""


async def test_an_unreadable_section_field_is_flagged_not_silently_omitted(
    client, fake_ocr, patient_user
):
    """Item 3 of the OCR/extraction fix-up: a field dropped by
    `sections._MAX_VALUE_LEN` (gender, here) now comes back from the API
    with `unreadable: true` and `value: ""`, rather than being missing from
    the list entirely — the caller can tell "attempted, not readable"
    apart from "never in the document" (see `SectionItem.unreadable`,
    additive and `False` by default)."""
    detail = await upload_and_process(client, fake_ocr, "prescription", UNREADABLE_GENDER_TEXT)
    await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=confirm_body(detail))

    api_detail = (await client.get(f"/api/v1/documents/{detail['id']}")).json()
    patient_items = {item["key"]: item for item in api_detail["sections"]["patient"]}
    assert patient_items["gender"]["unreadable"] is True
    assert patient_items["gender"]["value"] == ""
    # A normal, successfully-read field is unaffected: still just key/value,
    # with the new field present but False.
    assert patient_items["patient_name"]["unreadable"] is False
    assert patient_items["patient_name"]["value"] == "Ayesha Khan"


async def test_the_existing_detail_page_renders_an_unreadable_field_without_crashing(
    client, fake_ocr, patient_user
):
    """The protected View/Details page template was deliberately left
    untouched for this step (no "could not read reliably" text yet — that
    waits for a locale-aware follow-up) — this only proves the template's
    existing unconditional `{{ item.value }}` rendering handles the new
    empty-string case safely: no "None", no server error, the page still
    renders 200 with the rest of the patient section intact."""
    detail = await upload_and_process(client, fake_ocr, "prescription", UNREADABLE_GENDER_TEXT)
    await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=confirm_body(detail))

    page = await client.get(f"/en/patient/documents/{detail['id']}")
    assert page.status_code == 200
    html = page.text
    assert "None" not in html
    assert "Patient Name" in html and "Ayesha Khan" in html


async def test_the_finalized_page_shows_structured_sections_for_a_lab_report(
    client, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "lab_report", FULL_LAB_TEXT)
    await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=confirm_body(detail))
    page = await client.get(f"/en/patient/documents/{detail['id']}")
    html = page.text
    assert "Laboratory Information" in html and "Northside Diagnostic Center" in html
    assert "021-1112223" in html
    assert "Patient Information" in html and "Bilal Hussain" in html
    assert "Doctor Information" in html and "Dr. Imran Qureshi" in html
    assert "Notes / Interpretation" in html
    assert "Slightly elevated, recommend follow-up." in html
    assert "Creatinine" in html  # the existing test-results table, unaffected


async def test_the_finalized_page_offers_a_pdf_link_not_an_inline_image(
    client, fake_ocr, patient_user
):
    fake_ocr.text = RX_TEXT
    up = await upload(client, "prescription", "rx.pdf", PDF, "application/pdf")
    assert up.status_code == 201, up.text
    doc_id = up.json()["id"]
    await client.post(f"/api/v1/documents/{doc_id}/process")
    detail = (await client.get(f"/api/v1/documents/{doc_id}")).json()
    await client.post(f"/api/v1/documents/{doc_id}/confirm", json=confirm_body(detail))
    page = await client.get(f"/en/patient/documents/{doc_id}")
    html = page.text
    assert "Open original PDF" in html
    assert f'<img src="/en/patient/documents/{doc_id}/file"' not in html


async def test_the_finalized_page_degrades_gracefully_on_severely_noisy_ocr_text(
    client, fake_ocr, patient_user
):
    """The structuring stage must never crash and never fabricate a field on
    real, heavily degraded handwritten OCR text — it may simply recover
    less than it would from clean text."""
    detail = await upload_and_process(client, fake_ocr, "prescription", REAL_HANDWRITTEN_TEXT)
    await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=confirm_body(detail))
    page = await client.get(f"/en/patient/documents/{detail['id']}")
    assert page.status_code == 200
    html = page.text
    # What IS clearly legible in the noise is still recovered.
    assert "Doctor Name" in html and "Dr. Sara Ahmed" in html
    assert ">Advice</h3>" in html


# ═══ AI Understanding / Normalization: confidence, status, provenance ═════
NOISY_MARKER_RX_TEXT = "& Metformin 500 mg\nTwice daily\nfor 30 days"


async def test_confidence_and_status_are_persisted_and_returned_through_the_api(
    client, db, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", NOISY_MARKER_RX_TEXT)
    fields = {f["name"]: f for f in detail["rows"][0]["fields"]}
    assert fields["medicine"]["value"] == "Metformin"  # the "&" was cleaned
    assert fields["medicine"]["confidence"] == 0.6  # "uncertain": noise was removed
    assert fields["dosage"]["confidence"] == 1.0  # "high": read cleanly
    assert fields["medicine"]["status"] == "ai_extracted"  # not yet reviewed

    job = await job_row(db, detail["id"])
    assert job.overall_confidence is not None

    # Persisted, not just computed in-memory: a fresh DB read confirms it.
    exts = {e.field_name: e for e in await extraction_rows(db, detail["id"]) if e.row_index == 0}
    assert float(exts["medicine"].confidence) == 0.6

    await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=confirm_body(detail))
    after = (await client.get(f"/api/v1/documents/{detail['id']}")).json()
    reviewed = {f["name"]: f for f in after["rows"][0]["fields"]}
    assert reviewed["medicine"]["status"] == "human_verified"  # Confirm & Save changed this
    assert reviewed["medicine"]["confidence"] == 0.6  # unaffected by review itself


async def test_lab_and_vaccination_rows_have_no_fabricated_confidence(
    client, fake_ocr, patient_user
):
    lab = await upload_and_process(client, fake_ocr, "lab_report", LAB_TEXT)
    for f in lab["rows"][0]["fields"]:
        assert f["confidence"] is None  # normalization never runs for lab


async def test_document_upload_records_its_source_in_the_audit_trail(client, db, patient_user):
    up = await upload(client, "prescription")
    assert up.status_code == 201
    row = (
        (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.action == audit.DOCUMENT_UPLOAD,
                    AuditLog.subject_patient_id
                    == (
                        await db.execute(
                            select(Patient.id).where(Patient.user_id == patient_user.id)
                        )
                    ).scalar_one(),
                )
            )
        )
        .scalars()
        .first()
    )
    assert row is not None and json.loads(row.detail)["source"] == "patient_upload"


# ═══ Doctor-generated prescriptions: no OCR, no patient review ════════════
@pytest.fixture
async def verified_doctor(db, patient_user) -> Profile:
    # `verified_by` needs a real `user_profile` row to point at (a FK, not
    # just a flag) — reusing `patient_user.id` here is exactly the pattern
    # `test_profile.py`'s own verified-doctor fixture already uses.
    return await make_user(
        db, email="doc@x.com", role=UserRole.DOCTOR, is_verified=True, verifier_id=patient_user.id
    )


async def create_doctor_prescription(app, medicines, *, patient_id, notes="", email="doc@x.com"):
    """A verified doctor's own authenticated client posting a structured
    prescription — a SEPARATE actor from whichever `client`/`patient_user`
    the calling test is using, exactly like the existing `new_client`
    pattern used for other cross-actor tests in this file."""
    async with new_client(app) as doctor_client:
        await login(doctor_client, email)
        return await doctor_client.post(
            "/api/v1/doctor/prescriptions",
            json={"patient_id": str(patient_id), "medicines": medicines, "notes": notes},
        )


async def _patient_id_for(db, user: Profile) -> uuid.UUID:
    return (await db.execute(select(Patient.id).where(Patient.user_id == user.id))).scalar_one()


async def test_a_verified_doctor_can_create_a_structured_prescription(
    app, db, patient_user, verified_doctor
):
    pid = await _patient_id_for(db, patient_user)
    r = await create_doctor_prescription(
        app,
        [
            {
                "medicine": "Metformin",
                "dosage": "500 mg",
                "frequency": "Twice daily",
                "duration": "30 days",
            }
        ],
        patient_id=pid,
        notes="Follow up after 2 weeks.",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "finalized"
    assert body["source"] == "doctor_generated"
    assert body["finalized_at"] is not None
    fields = {f["name"]: f for f in body["rows"][0]["fields"]}
    assert fields["medicine"]["value"] == "Metformin"
    assert fields["dosage"]["value"] == "500 mg"


async def test_doctor_generated_prescription_reuses_the_existing_normalization_and_confidence(
    app, db, patient_user, verified_doctor
):
    """The same `normalization.py` stage a patient upload's OCR'd entities
    pass through — proven by submitting the same kind of leading-noise-symbol
    input the OCR path already has a regression test for, and checking the
    same certainty weight comes out."""
    pid = await _patient_id_for(db, patient_user)
    r = await create_doctor_prescription(
        app, [{"medicine": "& Atorvastatin", "dosage": "10 mg"}], patient_id=pid
    )
    assert r.status_code == 201, r.text
    fields = {f["name"]: f for f in r.json()["rows"][0]["fields"]}
    assert fields["medicine"]["value"] == "Atorvastatin"  # "&" cleaned, same as OCR path
    assert fields["medicine"]["confidence"] == 0.6  # "uncertain": noise was removed
    assert fields["dosage"]["confidence"] == 1.0  # "high": read cleanly


async def test_doctor_generated_prescription_never_enters_awaiting_review(
    app, db, patient_user, verified_doctor
):
    pid = await _patient_id_for(db, patient_user)
    r = await create_doctor_prescription(
        app, [{"medicine": "Losartan", "dosage": "50 mg"}], patient_id=pid
    )
    doc_id = r.json()["id"]
    row = await doc_row(db, doc_id)
    assert row.status == "finalized"  # never observed as "awaiting_review"

    # Confirm & Save on an already-finalized document is the EXISTING,
    # already-tested no-op (`confirm()`'s own docstring: "Confirming twice
    # is harmless") — proof that nothing here is waiting for it.
    async with new_client(app) as patient_client:
        await login(patient_client, "pat@x.com")
        confirm_res = await patient_client.post(
            f"/api/v1/documents/{doc_id}/confirm", json={"rows": []}
        )
        assert confirm_res.status_code == 200
        assert confirm_res.json()["status"] == "finalized"


async def test_automatic_validation_rejects_a_prescription_with_no_real_medicine(
    app, db, patient_user, verified_doctor
):
    pid = await _patient_id_for(db, patient_user)
    r = await create_doctor_prescription(
        app,
        [{"medicine": "& "}],
        patient_id=pid,  # noise-only after normalization
    )
    assert r.status_code == 422, r.text


async def test_a_doctor_generated_prescription_is_stored_as_the_original_text_document(
    app, db, patient_user, verified_doctor
):
    pid = await _patient_id_for(db, patient_user)
    r = await create_doctor_prescription(
        app,
        [{"medicine": "Amoxicillin", "dosage": "500 mg", "frequency": "Three times daily"}],
        patient_id=pid,
        notes="Complete the full course.",
    )
    doc_id = r.json()["id"]
    assert r.json()["mime_type"] == "text/plain"

    async with new_client(app) as patient_client:
        await login(patient_client, "pat@x.com")
        file_res = await patient_client.get(f"/api/v1/documents/{doc_id}/file")
        assert file_res.status_code == 200
        assert b"Amoxicillin" in file_res.content
        assert b"Complete the full course." in file_res.content


async def test_a_doctor_generated_prescriptions_provider_section_names_the_authoring_doctor(
    app, db, patient_user, verified_doctor
):
    """The Document Details page's "Doctor Information" section must be
    populated for a doctor-generated prescription too, not just a patient
    upload — `verified_doctor` is `full_name="Test Person"`,
    `specialty="Cardiology"` (see `make_user`)."""
    pid = await _patient_id_for(db, patient_user)
    r = await create_doctor_prescription(
        app, [{"medicine": "Metformin", "dosage": "500 mg"}], patient_id=pid
    )
    body = r.json()
    provider = {item["key"]: item["value"] for item in body["sections"]["provider"]}
    assert provider["doctor_name"] == "Dr. Test Person"
    assert provider["specialization"] == "Cardiology"


async def test_doctor_generated_prescription_is_retrievable_through_the_patient_document_system(
    app, db, patient_user, verified_doctor
):
    """No separate "doctor documents" system: the SAME `GET /api/v1/documents`
    and `GET /api/v1/documents/{id}` a patient upload uses."""
    pid = await _patient_id_for(db, patient_user)
    r = await create_doctor_prescription(
        app, [{"medicine": "Vitamin D3", "dosage": "1000 IU"}], patient_id=pid
    )
    doc_id = r.json()["id"]

    async with new_client(app) as patient_client:
        await login(patient_client, "pat@x.com")
        listing = (await patient_client.get("/api/v1/documents")).json()
        assert any(d["id"] == doc_id and d["source"] == "doctor_generated" for d in listing)

        detail = (await patient_client.get(f"/api/v1/documents/{doc_id}")).json()
        assert detail["source"] == "doctor_generated"
        assert detail["rows"][0]["fields"][0]["value"] == "Vitamin D3"
        # The same structured/sections presentation a patient upload gets.
        assert detail["sections"] is not None


async def test_an_unverified_doctor_cannot_create_a_prescription(app, db, patient_user):
    await make_user(db, email="unverified@x.com", role=UserRole.DOCTOR, is_verified=False)
    pid = await _patient_id_for(db, patient_user)
    r = await create_doctor_prescription(
        app, [{"medicine": "Metformin"}], patient_id=pid, email="unverified@x.com"
    )
    assert r.status_code == 403


async def test_a_nonexistent_patient_id_is_rejected(app, verified_doctor):
    r = await create_doctor_prescription(app, [{"medicine": "Metformin"}], patient_id=uuid.uuid4())
    assert r.status_code == 404


async def test_patient_uploaded_documents_still_require_review_unaffected_by_the_doctor_flow(
    client, fake_ocr, patient_user
):
    """Regression: the patient-upload pipeline's review requirement must be
    completely unchanged by the new doctor-generated path existing
    alongside it."""
    detail = await upload_and_process(client, fake_ocr)
    assert detail["status"] == "awaiting_review"
    assert detail["source"] == "patient_upload"
    listing = (await client.get("/api/v1/documents")).json()
    assert listing == []  # not finalized -> not a record yet, exactly as before


# ═══ Gemini AI understanding: patient-upload integration ═══════════════════
NOISY_GEMINI_TEXT = "& Metformin 500 mg\nTake 1 tablet twice daily\nfor 30 days"


async def test_patient_upload_uses_geminis_understood_fields_when_available(
    client, fake_ocr, fake_gemini, patient_user
):
    fake_gemini.result = ai_understanding.UnderstoodResult(
        rows=[
            {
                "medicine": "Metformin",
                "dosage": "500 mg",
                "frequency": "Twice daily",
                "duration": "30 days",
            }
        ],
        certainty=[
            {"medicine": "uncertain", "dosage": "high", "frequency": "high", "duration": "high"}
        ],
    )
    detail = await upload_and_process(client, fake_ocr, "prescription", NOISY_GEMINI_TEXT)
    fields = {f["name"]: f for f in detail["rows"][0]["fields"]}
    assert fields["medicine"]["value"] == "Metformin"  # Gemini's cleaned value, not "& Metformin"
    assert fields["medicine"]["confidence"] == 0.6  # "uncertain" -> the shared certainty weight
    assert len(fake_gemini.calls) == 1
    assert fake_gemini.calls[0][0] == NOISY_GEMINI_TEXT  # given the RAW OCR text, unedited
    # Still awaiting_review: Gemini enriches the fields, it never finalizes anything.
    assert detail["status"] == "awaiting_review"


async def test_patient_upload_falls_back_to_deterministic_extraction_when_gemini_declines(
    client, fake_ocr, fake_gemini, patient_user
):
    fake_gemini.result = None  # unavailable/failed/rejected — the real function's own contract
    detail = await upload_and_process(client, fake_ocr, "prescription", RX_TEXT)
    assert len(fake_gemini.calls) == 1  # it WAS tried
    fields = {f["name"]: f for f in detail["rows"][0]["fields"]}
    assert fields["medicine"]["value"] == "Metformin"  # the deterministic path's own result


async def test_raw_ocr_text_is_preserved_unchanged_even_when_gemini_rewrites_the_fields(
    client, fake_ocr, fake_gemini, patient_user
):
    fake_gemini.result = ai_understanding.UnderstoodResult(
        rows=[{"medicine": "Metformin", "dosage": "500 mg", "frequency": "", "duration": ""}],
        certainty=[
            {
                "medicine": "uncertain",
                "dosage": "high",
                "frequency": "missing",
                "duration": "missing",
            }
        ],
    )
    detail = await upload_and_process(client, fake_ocr, "prescription", NOISY_GEMINI_TEXT)
    assert detail["ocr_text"] == NOISY_GEMINI_TEXT  # exactly what OCR produced, "&" and all


async def test_confirm_and_save_still_marks_gemini_sourced_fields_human_verified(
    client, fake_ocr, fake_gemini, patient_user
):
    fake_gemini.result = ai_understanding.UnderstoodResult(
        rows=[
            {
                "medicine": "Metformin",
                "dosage": "500 mg",
                "frequency": "Twice daily",
                "duration": "30 days",
            }
        ],
        certainty=[
            {"medicine": "uncertain", "dosage": "high", "frequency": "high", "duration": "high"}
        ],
    )
    detail = await upload_and_process(client, fake_ocr, "prescription", NOISY_GEMINI_TEXT)
    saved = await client.post(
        f"/api/v1/documents/{detail['id']}/confirm", json=confirm_body(detail)
    )
    assert saved.status_code == 200
    out = saved.json()
    assert out["status"] == "finalized"
    fields = {f["name"]: f for f in out["rows"][0]["fields"]}
    assert fields["medicine"]["status"] == "human_verified"
    assert fields["medicine"]["value"] == "Metformin"


async def test_lab_reports_never_call_gemini(client, fake_ocr, fake_gemini, patient_user):
    await upload_and_process(client, fake_ocr, "lab_report", LAB_TEXT)
    assert fake_gemini.calls == []


async def test_document_details_page_shows_gemini_understood_medicines_after_confirmation(
    client, fake_ocr, fake_gemini, patient_user
):
    fake_gemini.result = ai_understanding.UnderstoodResult(
        rows=[
            {
                "medicine": "Metformin",
                "dosage": "500 mg",
                "frequency": "Twice daily",
                "duration": "30 days",
            }
        ],
        certainty=[
            {"medicine": "uncertain", "dosage": "high", "frequency": "high", "duration": "high"}
        ],
    )
    detail = await upload_and_process(client, fake_ocr, "prescription", NOISY_GEMINI_TEXT)
    await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=confirm_body(detail))
    page = await client.get(f"/en/patient/documents/{detail['id']}")
    assert "Metformin" in page.text
    assert "& Metformin" not in page.text.replace(
        "Metformin", "", 1
    )  # only the clean value shows in the table


# ═══ Gemini AI understanding: doctor-generated pipeline ═══════════════════
# Doctor-typed input is not noisy OCR text, so it goes through
# `validate_doctor_prescription` (never `understand_prescription`, the
# OCR-text entry point `fake_gemini` stands in for) — see that function's
# docstring: it can only downgrade certainty, never rewrite a doctor's value.
async def test_doctor_generated_prescription_calls_the_shared_gemini_validation_layer(
    app, db, patient_user, verified_doctor, fake_gemini_doctor, fake_gemini
):
    fake_gemini_doctor.result = ai_understanding.UnderstoodResult(
        rows=[
            {
                "medicine": "Metformin",
                "dosage": "500 mg",
                "frequency": "Twice daily",
                "duration": "30 days",
            }
        ],
        certainty=[{"medicine": "high", "dosage": "high", "frequency": "high", "duration": "high"}],
    )
    pid = await _patient_id_for(db, patient_user)
    r = await create_doctor_prescription(
        app,
        [
            {
                "medicine": "Metformin",
                "dosage": "500 mg",
                "frequency": "Twice daily",
                "duration": "30 days",
            }
        ],
        patient_id=pid,
    )
    assert r.status_code == 201, r.text
    assert len(fake_gemini_doctor.calls) == 1
    assert fake_gemini.calls == []  # the OCR-text entry point is never touched by this path


async def test_doctor_generated_fields_use_the_same_certainty_categories_and_confidence_weights(
    app, db, patient_user, verified_doctor, fake_gemini_doctor
):
    """Same categories, same `CERTAINTY_WEIGHT` mapping as the patient path
    (high=1.0, uncertain=0.6, ambiguous=0.3, missing=null) — no second
    confidence system for doctors."""
    fake_gemini_doctor.result = ai_understanding.UnderstoodResult(
        rows=[
            {"medicine": "Metformin", "dosage": "500 mg", "frequency": "", "duration": "30 days"}
        ],
        certainty=[
            {
                "medicine": "high",
                "dosage": "uncertain",
                "frequency": "missing",
                "duration": "ambiguous",
            }
        ],
    )
    pid = await _patient_id_for(db, patient_user)
    r = await create_doctor_prescription(
        app,
        [{"medicine": "Metformin", "dosage": "500 mg", "frequency": "", "duration": "30 days"}],
        patient_id=pid,
    )
    assert r.status_code == 201, r.text
    fields = {f["name"]: f for f in r.json()["rows"][0]["fields"]}
    assert fields["medicine"]["confidence"] == 1.0
    assert fields["dosage"]["confidence"] == 0.6
    assert fields["frequency"]["confidence"] is None
    assert fields["duration"]["confidence"] == 0.3


async def test_doctor_generated_never_lets_gemini_invent_a_blank_field(
    app, db, patient_user, verified_doctor, fake_gemini_doctor
):
    fake_gemini_doctor.result = ai_understanding.UnderstoodResult(
        rows=[{"medicine": "Metformin", "dosage": "500 mg", "frequency": "", "duration": ""}],
        certainty=[
            {"medicine": "high", "dosage": "high", "frequency": "missing", "duration": "missing"}
        ],
    )
    pid = await _patient_id_for(db, patient_user)
    r = await create_doctor_prescription(
        app, [{"medicine": "Metformin", "dosage": "500 mg"}], patient_id=pid
    )
    fields = {f["name"]: f for f in r.json()["rows"][0]["fields"]}
    assert fields["frequency"]["value"] == ""
    assert fields["duration"]["value"] == ""


async def test_doctor_generated_gemini_failure_falls_back_to_deterministic_normalization(
    app, db, patient_user, verified_doctor, fake_gemini_doctor
):
    fake_gemini_doctor.result = None  # unavailable/failed/rejected
    pid = await _patient_id_for(db, patient_user)
    r = await create_doctor_prescription(
        app, [{"medicine": "Metformin", "dosage": "500 mg"}], patient_id=pid
    )
    assert r.status_code == 201, r.text
    assert len(fake_gemini_doctor.calls) == 1  # it WAS tried
    fields = {f["name"]: f for f in r.json()["rows"][0]["fields"]}
    assert fields["medicine"]["value"] == "Metformin"
    assert fields["medicine"]["confidence"] == 1.0  # the deterministic path's own "high"


async def test_doctor_generated_prescription_is_automatically_finalized_and_never_reviewed(
    app, db, patient_user, verified_doctor, fake_gemini_doctor
):
    fake_gemini_doctor.result = ai_understanding.UnderstoodResult(
        rows=[{"medicine": "Metformin", "dosage": "500 mg", "frequency": "", "duration": ""}],
        certainty=[
            {"medicine": "high", "dosage": "high", "frequency": "missing", "duration": "missing"}
        ],
    )
    pid = await _patient_id_for(db, patient_user)
    r = await create_doctor_prescription(
        app, [{"medicine": "Metformin", "dosage": "500 mg"}], patient_id=pid
    )
    body = r.json()
    assert body["status"] == "finalized"  # never awaiting_review, with or without Gemini
    assert body["source"] == "doctor_generated"


async def test_doctor_generated_prescription_appears_in_the_patients_documents(
    client, db, app, patient_user, verified_doctor, fake_gemini_doctor
):
    fake_gemini_doctor.result = None
    pid = await _patient_id_for(db, patient_user)
    await create_doctor_prescription(
        app, [{"medicine": "Metformin", "dosage": "500 mg"}], patient_id=pid
    )
    listing = (await client.get("/api/v1/documents")).json()
    assert len(listing) == 1
    assert listing[0]["document_type"] == "prescription"


async def test_doctor_generated_processing_never_calls_ocr(
    app, db, fake_ocr, patient_user, verified_doctor, fake_gemini_doctor
):
    """Structured doctor input has no scanned original — Gemini validation
    must not trigger, or be preceded by, an OCR read."""
    fake_gemini_doctor.result = None
    pid = await _patient_id_for(db, patient_user)
    r = await create_doctor_prescription(
        app, [{"medicine": "Metformin", "dosage": "500 mg"}], patient_id=pid
    )
    assert r.status_code == 201, r.text
    assert fake_ocr.calls == []


# ═══ Document Details page: structured, meaningful presentation ═══════════
# A real-shaped document with a facility letterhead, patient info (age/gender
# on one corrupted-separator line), a provider signature block, a
# genuinely-missing field (Paracetamol has no duration), and a notes section
# that mixes one real remark with one line of pure OCR noise.
STRUCTURED_RX_TEXT = """City Health Clinic
Ph: 042-3587 1234
Email: info@cityhealthclinic.pk
Patient Name: Ayesha Khan
Age / Gender: 32 years / Female
Patient ID: 2026-4587
Date: 27-09-2026
Rx
1. Paracetamol 500 mg
Take 1 tablet every 6 hours as needed
2. Amoxicillin 500 mg
Take 1 capsule three times daily
for 5 days.
Notes:
0 0 0 0 000
Drink plenty of water.
Dr. Sara Ahmed
MBBS, FCPS (Internal Medicine)"""


async def _finalized_structured_page(client, fake_ocr, **edits) -> tuple[dict, str]:
    detail = await upload_and_process(client, fake_ocr, "prescription", STRUCTURED_RX_TEXT)
    saved = await client.post(
        f"/api/v1/documents/{detail['id']}/confirm", json=confirm_body(detail, edits)
    )
    assert saved.status_code == 200, saved.text
    page = await client.get(f"/en/patient/documents/{detail['id']}")
    assert page.status_code == 200
    return saved.json(), page.text


async def test_finalized_page_shows_hospital_patient_and_provider_sections(
    client, fake_ocr, patient_user
):
    _, html = await _finalized_structured_page(client, fake_ocr)
    assert "City Health Clinic" in html
    assert "Ayesha Khan" in html
    assert "32" in html
    assert "Female" in html
    assert "Dr. Sara Ahmed" in html


async def test_finalized_medicines_table_shows_not_specified_for_a_missing_field(
    client, fake_ocr, patient_user
):
    """Paracetamol's duration was never stated in the document — the table
    must say so plainly, never guess a duration, never leave a blank cell
    that reads as though nothing is there."""
    _, html = await _finalized_structured_page(client, fake_ocr)
    assert "Paracetamol" in html
    assert "Not specified" in html


async def test_patient_correction_at_confirm_replaces_the_shown_value_on_the_finalized_page(
    client, fake_ocr, patient_user
):
    """The AI-proposed dosage ("500 mg") is edited to "250 mg" at Confirm &
    Save — the finalized page's structured table must show the patient's
    own correction. (The raw OCR text, still preserved further down the
    page for provenance, legitimately still reads "500 mg" — that's the
    ORIGINAL machine reading, not the finalized field, and is correct.)"""
    detail = await upload_and_process(client, fake_ocr, "prescription", STRUCTURED_RX_TEXT)
    body = confirm_body(detail, {(0, "dosage"): "250 mg"})  # was "500 mg"
    saved = await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=body)
    assert saved.status_code == 200, saved.text
    fields = {f["name"]: f for f in saved.json()["rows"][0]["fields"]}
    assert fields["dosage"]["value"] == "250 mg"
    page = await client.get(f"/en/patient/documents/{detail['id']}")
    assert "250 mg" in page.text


async def test_ocr_garbage_notes_are_not_shown_as_advice_on_the_finalized_page(
    client, fake_ocr, patient_user
):
    """The garbage line must not appear in the rendered NOTES list — it may
    still legitimately appear further down in the preserved raw-OCR `<pre>`
    block, which is a separate, explicitly-provenance section, not "advice"."""
    _, html = await _finalized_structured_page(client, fake_ocr)
    notes_start = html.index('class="notes-list"')
    notes_end = html.index("</ul>", notes_start)
    notes_html = html[notes_start:notes_end]
    assert "Drink plenty of water." in notes_html
    assert "0 0 0 0" not in notes_html


async def test_raw_ocr_text_is_collapsed_by_default_when_structured_sections_are_found(
    client, fake_ocr, patient_user
):
    """The raw OCR dump must still be present (provenance/debugging) but
    must NOT be the first thing the patient sees when structured sections
    were successfully found."""
    _, html = await _finalized_structured_page(client, fake_ocr)
    assert "City Health Clinic" in html  # the OCR text itself is still in the page
    details_tag = html[html.index("<details") : html.index("<details") + 20]
    assert " open" not in details_tag  # collapsed, not expanded, by default


async def test_original_image_remains_accessible_on_the_finalized_page(
    client, fake_ocr, patient_user
):
    detail, html = await _finalized_structured_page(client, fake_ocr)
    assert f'src="/en/patient/documents/{detail["id"]}/file"' in html


async def test_raw_ocr_text_is_never_overwritten_in_the_database(
    client, fake_ocr, db, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", STRUCTURED_RX_TEXT)
    await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=confirm_body(detail))
    doc = await doc_row(db, detail["id"])
    assert doc.ocr_text == STRUCTURED_RX_TEXT


async def test_doctor_generated_document_uses_the_same_structured_page_in_patient_documents(
    app, db, patient_user, verified_doctor
):
    """The same `detail.html` structured presentation a patient upload gets
    — hospital/patient/provider sections, medicines table — not a separate
    doctor-document template, and no Doctor Dashboard UI involved."""
    pid = await _patient_id_for(db, patient_user)
    r = await create_doctor_prescription(
        app,
        [{"medicine": "Vitamin D3", "dosage": "3000 IU", "frequency": "Once daily"}],
        patient_id=pid,
        notes="Follow up in 3 months.",
    )
    doc_id = r.json()["id"]

    async with new_client(app) as patient_client:
        await login(patient_client, "pat@x.com")
        page = await patient_client.get(f"/en/patient/documents/{doc_id}")
        assert page.status_code == 200
        assert "Vitamin D3" in page.text
        assert "3000 IU" in page.text
        assert "Dr. Test Person" in page.text  # the "Doctor Information" section
        assert "Follow up in 3 months." in page.text


# ═══ Confidence badge: visible on both the review and finalized pages ═════
async def test_the_review_page_shows_a_confidence_badge_per_field(
    client, fake_ocr, fake_gemini, patient_user
):
    fake_gemini.result = ai_understanding.UnderstoodResult(
        rows=[
            {"medicine": "Metformin", "dosage": "500 mg", "frequency": "", "duration": "30 days"}
        ],
        certainty=[
            {
                "medicine": "high",
                "dosage": "uncertain",
                "frequency": "missing",
                "duration": "ambiguous",
            }
        ],
    )
    detail = await upload_and_process(client, fake_ocr, "prescription", NOISY_GEMINI_TEXT)
    page = await client.get(f"/en/patient/documents/{detail['id']}")
    assert page.status_code == 200
    assert "High confidence" in page.text
    assert "Uncertain" in page.text
    assert "Unclear" in page.text  # "ambiguous"
    # "missing" (duration's empty here isn't it -- frequency is) gets no badge at all:
    # nothing to flag on a field that's simply blank.


async def test_the_review_page_shows_a_drug_name_suggestion_beside_the_original(
    client, fake_ocr, fake_gemini, patient_user
):
    """Step 7: a near-miss medicine name gets a suggestion hint on the
    Review page — beside the original, editable text, never replacing it.
    `fake_gemini.result` stays `None` (deterministic fallback), so the
    extractor's own OCR-cleaned name is what reaches `drug_names.suggest`."""
    text = "Dr. Samar Abbas\nDate: 11 Aug 2024\n1. Tab. Cetirizing 10mg once daily for 10 days"
    detail = await upload_and_process(client, fake_ocr, "prescription", text)
    page = await client.get(f"/en/patient/documents/{detail['id']}")
    assert page.status_code == 200
    assert "Did you mean:" in page.text
    assert "Cetirizine" in page.text
    # The input itself is untouched — the OCR'd text is still what's shown
    # and still what the patient would submit if they changed nothing.
    assert 'value="Cetirizing"' in page.text


async def test_the_review_page_shows_no_suggestion_for_an_already_known_medicine_name(
    client, fake_ocr, fake_gemini, patient_user
):
    """`RX_TEXT`'s "Metformin"/"Atorvastatin" are both already-known names —
    no suggestion hint should appear for either."""
    detail = await upload_and_process(client, fake_ocr, "prescription", RX_TEXT)
    page = await client.get(f"/en/patient/documents/{detail['id']}")
    assert page.status_code == 200
    assert "Did you mean:" not in page.text


async def test_the_finalized_page_shows_a_confidence_badge_for_an_unedited_field(
    client, fake_ocr, fake_gemini, patient_user
):
    fake_gemini.result = ai_understanding.UnderstoodResult(
        rows=[
            {
                "medicine": "Metformin",
                "dosage": "500 mg",
                "frequency": "Twice daily",
                "duration": "30 days",
            }
        ],
        certainty=[
            {"medicine": "uncertain", "dosage": "high", "frequency": "high", "duration": "high"}
        ],
    )
    detail = await upload_and_process(client, fake_ocr, "prescription", NOISY_GEMINI_TEXT)
    saved = await client.post(
        f"/api/v1/documents/{detail['id']}/confirm", json=confirm_body(detail)
    )
    assert saved.status_code == 200, saved.text
    page = await client.get(f"/en/patient/documents/{detail['id']}")
    assert "Uncertain" in page.text  # the medicine field's own certainty, unedited


async def test_the_finalized_page_hides_the_confidence_badge_for_a_field_the_patient_edited(
    client, fake_ocr, fake_gemini, patient_user
):
    """Once the patient has corrected a value, the AI's original certainty
    about the OLD value is no longer meaningful — show "Edited by you"
    instead of a (now-stale) confidence badge, never both."""
    fake_gemini.result = ai_understanding.UnderstoodResult(
        rows=[{"medicine": "Metformin", "dosage": "500 mg", "frequency": "", "duration": ""}],
        certainty=[
            {
                "medicine": "ambiguous",
                "dosage": "high",
                "frequency": "missing",
                "duration": "missing",
            }
        ],
    )
    detail = await upload_and_process(client, fake_ocr, "prescription", NOISY_GEMINI_TEXT)
    body = confirm_body(detail, {(0, "medicine"): "Metformin HCl"})
    saved = await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=body)
    assert saved.status_code == 200, saved.text
    page = await client.get(f"/en/patient/documents/{detail['id']}")
    rec_start = page.text.index("Metformin HCl")
    row_html = page.text[rec_start : rec_start + 400]
    assert "Edited by you" in row_html
    assert "Unclear" not in row_html  # the stale "ambiguous" badge must not also show


# ═══ Preprocessing report page ════════════════════════════════════════════
def _real_png(lines: int = 4) -> bytes:
    """A decodable page (the module's `PNG` is not a real image) with `lines`
    rows of ink on it."""
    import io

    from PIL import Image, ImageDraw

    page = Image.new("L", (1600, 200 + lines * 114), 255)
    draw = ImageDraw.Draw(page)
    for i in range(lines):
        y = 100 + i * 114
        draw.line([(100 + k * 10, y + (44 if k % 2 else 0)) for k in range(16)], fill=0, width=4)
        draw.line([(320 + k * 10, y + (44 if k % 2 else 0)) for k in range(16)], fill=0, width=4)
    buf = io.BytesIO()
    page.save(buf, format="PNG")
    return buf.getvalue()


async def _uploaded_real_png(client, fake_ocr, lines: int = 4) -> str:
    up = await upload(client, data=_real_png(lines))
    assert up.status_code == 201, up.text
    return up.json()["id"]


async def test_the_preprocess_page_shows_what_the_reader_receives(client, fake_ocr, patient_user):
    doc_id = await _uploaded_real_png(client, fake_ocr)
    page = await client.get(f"/en/patient/documents/{doc_id}/preprocess")
    assert page.status_code == 200
    html = page.text
    assert "Preprocessing report" in html and "Page 1" in html
    assert "data:image/png;base64," in html
    assert "1600 × 656 px" in html  # the original, before any preprocessing
    assert "Prepared for the reader" in html  # what the engine actually receives


async def test_the_preprocess_page_has_no_handwriting_view_even_if_one_is_asked_for(
    client, fake_ocr, patient_user
):
    doc_id = await _uploaded_real_png(client, fake_ocr, lines=4)
    for query in ("", "?mode=printed", "?mode=handwritten"):
        page = await client.get(f"/en/patient/documents/{doc_id}/preprocess{query}")
        assert page.status_code == 200
        assert "Line strips" not in page.text and "Lines found" not in page.text
        assert "Show as read by" not in page.text and "andwrit" not in page.text


async def test_the_preprocess_page_never_runs_an_ocr_engine_or_changes_the_document(
    client, fake_ocr, patient_user, db
):
    doc_id = await _uploaded_real_png(client, fake_ocr)
    before = await doc_row(db, doc_id)
    page = await client.get(f"/en/patient/documents/{doc_id}/preprocess")
    after = await doc_row(db, doc_id)
    assert page.status_code == 200
    assert fake_ocr.calls == []  # no engine was asked to read anything
    assert (after.status, after.ocr_text) == (before.status, before.ocr_text)
    assert after.status == "uploaded"


async def test_an_undecodable_file_explains_itself_instead_of_erroring(
    client, fake_ocr, patient_user
):
    up = await upload(client)  # the module's PNG is a fake image
    page = await client.get(f"/en/patient/documents/{up.json()['id']}/preprocess")
    assert page.status_code == 200
    assert "could not be prepared" in page.text
    assert escape(translate("errors.ocr_failed", "en")) in page.text


async def test_the_review_failed_and_uploaded_pages_link_to_the_report_but_finalized_does_not(
    client, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", RX_TEXT)
    link = f"/en/patient/documents/{detail['id']}/preprocess"
    review = await client.get(f"/en/patient/documents/{detail['id']}")
    assert link in review.text and "View preprocessing report" in review.text

    await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=confirm_body(detail))
    finalized = await client.get(f"/en/patient/documents/{detail['id']}")
    assert link not in finalized.text  # the finished record page is untouched

    fake_ocr.error = ocr.OCR_FAILED
    failed = await upload(client)
    await client.post(f"/api/v1/documents/{failed.json()['id']}/process")
    failed_page = await client.get(f"/en/patient/documents/{failed.json()['id']}")
    assert f"/en/patient/documents/{failed.json()['id']}/preprocess" in failed_page.text

    waiting = await upload(client)  # status: uploaded, not yet read
    waiting_page = await client.get(f"/en/patient/documents/{waiting.json()['id']}")
    assert f"/en/patient/documents/{waiting.json()['id']}/preprocess" in waiting_page.text


async def test_another_patient_cannot_see_the_preprocess_report(
    app, client, fake_ocr, patient_user, second_patient
):
    doc_id = await _uploaded_real_png(client, fake_ocr)
    async with new_client(app) as other:
        await login(other, "other@x.com")
        page = await other.get(f"/en/patient/documents/{doc_id}/preprocess")
        assert page.status_code == 404 and escape(translate("errors.not_found", "en")) in page.text
        assert "data:image" not in page.text


async def test_the_preprocess_page_for_a_bad_id_or_a_cancelled_upload_is_not_found(
    client, fake_ocr, patient_user
):
    assert (await client.get("/en/patient/documents/not-a-uuid/preprocess")).status_code == 404
    doc_id = await _uploaded_real_png(client, fake_ocr)
    await client.post(f"/api/v1/documents/{doc_id}/cancel")
    assert (await client.get(f"/en/patient/documents/{doc_id}/preprocess")).status_code == 404


async def test_the_preprocess_page_renders_in_urdu_right_to_left(client, fake_ocr, patient_user):
    doc_id = await _uploaded_real_png(client, fake_ocr)
    page = await client.get(f"/ur/patient/documents/{doc_id}/preprocess")
    assert page.status_code == 200 and 'dir="rtl"' in page.text
    assert translate("documents.preprocess.title", "ur") in page.text


# ═══ AI-read sections: stored with the OCR job, merged on the saved record ═
NOISY_LETTERHEAD_RX = """Cry city Health Clinic . Shop No. 12 Main Boulevard
Patient Name : Ayesha Khan Date : 27-09-2026
Age / Gender : 32 years / Female Patient ID : 2026-4587
1. Tab. Metformin 500mg twice daily x 30 days
Dr. Sara Ahmed"""

AI_READING = {
    "version": 1,
    "facility": {"hospital_name": "City Health Clinic"},
    "patient": {"patient_name": "Ayesha Khan", "patient_id": "9999-0000"},
    "notes": ["Drink plenty of water"],
}


def _fake_ai_sections(monkeypatch, reading):
    from app.documents import ai_sections

    calls = []

    async def fake(text, doc_type):
        calls.append((text, doc_type))
        return reading

    monkeypatch.setattr(ai_sections, "extract", fake)
    return calls


async def test_the_ai_reading_is_stored_with_the_ocr_job(
    client, db, fake_ocr, fake_gemini, patient_user, monkeypatch
):
    calls = _fake_ai_sections(monkeypatch, AI_READING)
    detail = await upload_and_process(client, fake_ocr, "prescription", NOISY_LETTERHEAD_RX)
    assert (await job_row(db, detail["id"])).ai_sections == AI_READING
    assert calls == [(NOISY_LETTERHEAD_RX, "rx")]


async def test_the_saved_record_shows_the_ai_reading_merged_over_the_deterministic_sections(
    client, fake_ocr, fake_gemini, patient_user, monkeypatch
):
    _fake_ai_sections(monkeypatch, AI_READING)
    detail = await upload_and_process(client, fake_ocr, "prescription", NOISY_LETTERHEAD_RX)
    await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=confirm_body(detail))

    sections_out = (await client.get(f"/api/v1/documents/{detail['id']}")).json()["sections"]
    facility = {i["key"]: i["value"] for i in sections_out["facility"]}
    patient = {i["key"]: i["value"] for i in sections_out["patient"]}
    # free text: the validated AI reading replaces the noisy "Cry city Health Clinic"
    assert facility["hospital_name"] == "City Health Clinic"
    # a fixed-shape id the extractor already read is NOT overridden by the AI
    assert patient["patient_id"] == "2026-4587"
    assert sections_out["notes"] == ["Drink plenty of water"]
    page = (await client.get(f"/en/patient/documents/{detail['id']}")).text
    # The structured row shows the clean name; the raw OCR text, kept unchanged
    # below it, still has the noisy original.
    assert re.search(r"Hospital / Clinic Name</dt>\s*<dd>City Health Clinic</dd>", page)


async def test_without_an_ai_reading_the_saved_record_is_exactly_the_deterministic_one(
    client, db, fake_ocr, fake_gemini, patient_user, monkeypatch
):
    _fake_ai_sections(monkeypatch, None)
    detail = await upload_and_process(client, fake_ocr, "prescription", NOISY_LETTERHEAD_RX)
    assert (await job_row(db, detail["id"])).ai_sections is None
    await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=confirm_body(detail))
    facility = {
        i["key"]: i["value"]
        for i in (await client.get(f"/api/v1/documents/{detail['id']}")).json()["sections"][
            "facility"
        ]
    }
    assert facility["hospital_name"] == "Cry city Health Clinic"


async def test_a_retry_discards_the_previous_attempts_ai_reading(
    client, db, fake_ocr, fake_gemini, patient_user, monkeypatch
):
    from datetime import timedelta

    _fake_ai_sections(monkeypatch, AI_READING)
    detail = await upload_and_process(client, fake_ocr, "prescription", NOISY_LETTERHEAD_RX)
    assert (await job_row(db, detail["id"])).ai_sections == AI_READING

    # The document is stuck in `processing` (a crashed worker) and is re-read.
    doc_uuid = uuid.UUID(detail["id"])
    await db.execute(update(Document).where(Document.id == doc_uuid).values(status="processing"))
    await db.execute(
        update(OcrJob)
        .where(OcrJob.document_id == doc_uuid)
        .values(started_at=utcnow() - timedelta(seconds=settings.document_processing_stale_s + 30))
    )
    await db.commit()
    _fake_ai_sections(monkeypatch, None)  # the AI is down this time
    r = await client.post(f"/api/v1/documents/{detail['id']}/process")
    assert r.status_code == 200
    assert (await job_row(db, detail["id"])).ai_sections is None


async def test_an_ai_failure_never_fails_the_upload(
    client, fake_ocr, fake_gemini, patient_user, monkeypatch
):
    from app.documents import ai_providers

    monkeypatch.setattr(settings, "groq_api_key", "k")

    async def down(contents, system_instruction, response_model=None):
        return None  # every provider failed

    monkeypatch.setattr(ai_understanding, "call_llm", down)
    ai_providers.reset_state()
    detail = await upload_and_process(client, fake_ocr, "prescription", NOISY_LETTERHEAD_RX)
    assert detail["status"] == "awaiting_review" and detail["rows"]


async def test_the_medicines_and_sections_ai_calls_run_at_the_same_time(
    client, fake_ocr, fake_gemini, patient_user, monkeypatch
):
    """Each fake waits for the OTHER to have started: run one after the other
    and the first would wait out its timeout — they must overlap."""
    import asyncio

    from app.documents import ai_sections

    medicines_started, sections_started = asyncio.Event(), asyncio.Event()

    async def understand(raw_text, existing):
        medicines_started.set()
        await asyncio.wait_for(sections_started.wait(), timeout=2)

    async def extract(text, doc_type):
        sections_started.set()
        await asyncio.wait_for(medicines_started.wait(), timeout=2)

    monkeypatch.setattr(ai_understanding, "understand_prescription", understand)
    monkeypatch.setattr(ai_sections, "extract", extract)
    detail = await upload_and_process(client, fake_ocr, "prescription", NOISY_LETTERHEAD_RX)
    assert detail["status"] == "awaiting_review"


async def test_a_lab_report_also_gets_its_sections_read_but_never_its_medicines(
    client, db, fake_ocr, fake_gemini, patient_user, monkeypatch
):
    calls = _fake_ai_sections(monkeypatch, {"version": 1, "facility": {"lab_name": "Chughtai Lab"}})
    detail = await upload_and_process(client, fake_ocr, "lab_report", LAB_TEXT)
    assert calls == [(LAB_TEXT, "lab")]
    assert fake_gemini.calls == []  # the medicines layer still never sees a lab report
    assert (await job_row(db, detail["id"])).ai_sections["facility"] == {"lab_name": "Chughtai Lab"}


# ═══ Clinical details, vaccine doses and lab flags ════════════════════════
# What a patient asked for: a prescription shows every medicine plus its diagnosis,
# symptoms, vital signs and advice; a lab report shows each test with its result,
# range and flag; vaccination records and other documents are sectioned too. All of
# it appears while reviewing AND on the saved record, and none of it needs a new
# database column (the sections are computed from the OCR text; the lab flag is
# worked out from the row when it is read).
CLINICAL_RX = """City Health Clinic
Patient Name: Ayesha Khan   Age: 32   Gender: Female
Date: 27-09-2026
Complaint: fever and cough for 3 days
BP: 120/80 mmHg   Pulse: 78 bpm   Temp: 98.6 F   SpO2: 98%
Diagnosis: Acute bronchitis
Rx
1. Tab. Paracetamol 500 mg 1-1-1 x 3 days
2. Cap Amoxicillin 500 mg 1-1-1 x 5 days
Advice: rest and plenty of fluids
Dr. Sara Ahmed"""

FLAGGED_LAB = """Chughtai Lab   Report Date: 28/07/2026
Patient Name: Ayesha Raza
HbA1c 7.2 % 4.0-5.6 H
Fasting Glucose 142 mg/dL 70-100
Hemoglobin 13.1 g/dL 12-15.5
Creatinine 0.9 mg/dL 0.6-1.2 L"""

CARD_TEXT = """Lahore Immunization Centre
Patient Name: Baby Zainab   Date of Birth: 02-03-2024
Vaccine | Dose | Date | Batch No | Next Due
BCG | 1 | 02-03-2024 | B778 |
Pentavalent | 1 | 14-05-2024 | P9921 | 14-06-2024
Notes: Keep this card safe."""

DISCHARGE_TEXT = """City Hospital
Discharge Summary
Patient Name: Ayesha Khan
Diagnosis: Appendicitis
BP: 118/76 mmHg  Pulse: 80 bpm
Advice: avoid heavy lifting for 4 weeks"""


async def test_a_prescription_under_review_shows_its_clinical_details_as_editable_tables(
    client, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", CLINICAL_RX)
    assert detail["status"] == "awaiting_review"
    tables = {g["key"]: g for g in detail["groups"]}
    assert list(tables) == ["medicines", "diagnoses", "symptoms", "vitals", "allergies"]

    def values(key):
        return [[f["value"] for f in row["fields"]] for row in tables[key]["rows"]]

    assert values("diagnoses") == [["Acute bronchitis"]]
    assert values("symptoms") == [["fever and cough for 3 days"]]
    (vitals_row,) = tables["vitals"]["rows"]
    assert {f["name"]: f["value"] for f in vitals_row["fields"] if f["value"]} == {
        "blood_pressure": "120/80 mmHg",
        "pulse": "78 bpm",
        "temperature": "98.6°F",
        "spo2": "98%",
    }
    assert len(vitals_row["fields"]) == 9  # every vital is there to fill in, found or not
    assert tables["allergies"]["rows"] == []
    assert [r["fields"][0]["value"] for r in detail["rows"]] == ["Paracetamol", "Amoxicillin"]

    # What a table holds is not repeated as a computed, read-only section.
    assert detail["sections"]["clinical"] == [] and detail["sections"]["vitals"] == []
    assert detail["sections"]["notes"] == ["rest and plenty of fluids"]

    html = (await client.get(f"/en/patient/documents/{detail['id']}")).text
    for name in (
        "row-1000-diagnosis",
        "row-2000-symptom",
        "row-4000-allergy_substance",  # the blank row to add an allergy
    ):
        assert f'name="{name}"' in html, name
    assert 'value="Acute bronchitis"' in html
    assert "Allergies you keep here are added to your profile" in html
    assert "Symptoms &amp; Diagnosis" not in html  # the read-only duplicate is gone
    # The vital signs are a table of their own, with every vital there to fill in.
    page = without_ocr_text(html)
    for vital in groups.VITALS.fields:
        assert f'name="row-3000-{vital}"' in page, vital
    assert 'value="120/80 mmHg"' in page and 'value="78 bpm"' in page and "Vital signs" in page
    assert ">Advice</h3>" in html and "rest and plenty of fluids" in html


async def test_a_vaccination_record_has_its_doses_as_editable_rows(
    client, db, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "vaccination", CARD_TEXT)
    assert detail["field_names"] == [
        "vaccine_name",
        "dose",
        "date_given",
        "batch_no",
        "site",
        "next_due",
    ]
    assert [r["fields"][0]["value"] for r in detail["rows"]] == ["BCG", "Pentavalent"]
    assert detail["sections"]["entries"] == []  # the dose table is the rows now
    assert {i["key"]: i["value"] for i in detail["sections"]["patient"]}["date_of_birth"] == (
        "02-03-2024"
    )

    review = (await client.get(f"/en/patient/documents/{detail['id']}")).text
    for expected in ('name="row-0-vaccine_name"', 'value="Pentavalent"', 'value="P9921"'):
        assert expected in review, expected

    # The patient corrects a batch number; the proposal is kept beside the correction.
    body = confirm_body(detail, {(1, "batch_no"): "P9922"})
    saved = (await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=body)).json()
    assert saved["rows"][1]["fields"][3]["value"] == "P9922"
    cell = next(e for e in await extraction_rows(db, detail["id"]) if e.field_name == "batch_no")
    assert (cell.extracted_value, cell.corrected_value) in {("B778", None), ("P9921", "P9922")}
    page = (await client.get(f"/en/patient/documents/{detail['id']}")).text
    for expected in (
        "Vaccinations",
        ">Vaccine</th>",
        ">Batch No.</th>",
        "P9922",
        "Edited by you",
        "14-06-2024",
        "Date of Birth",
    ):
        assert expected in page, expected


async def test_an_other_document_is_reviewed_and_saved_through_the_same_tables(
    client, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "other", DISCHARGE_TEXT)
    assert [g["key"] for g in detail["groups"]] == [
        "medicines",
        "diagnoses",
        "symptoms",
        "vitals",
        "allergies",
    ]
    # the medicines table is its primary one, and (unlike a prescription) may be empty
    assert detail["field_names"] == ["medicine", "dosage", "frequency", "duration"]
    assert detail["rows"] == []
    await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=confirm_body(detail))
    html = (await client.get(f"/en/patient/documents/{detail['id']}")).text
    for expected in (
        "Hospital / Clinic Name",
        "City Hospital",
        "Appendicitis",
        "avoid heavy lifting for 4 weeks",
    ):
        assert expected in html, expected
    page = without_ocr_text(html)
    assert "Vital signs" in page and "118/76 mmHg" in page and "80 bpm" in page
    saved = (await client.get(f"/api/v1/documents/{detail['id']}")).json()
    (vitals,) = table_of(saved, "vitals")["rows"]
    assert {f["name"]: f["value"] for f in vitals["fields"] if f["value"]} == {
        "blood_pressure": "118/76 mmHg",
        "pulse": "80 bpm",
    }


async def test_the_new_tables_render_in_urdu_right_to_left(
    client, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", CLINICAL_RX)
    page = await client.get(f"/ur/patient/documents/{detail['id']}")
    assert page.status_code == 200 and 'dir="rtl"' in page.text
    for key in (
        "documents.group.medicines",
        "documents.group.diagnoses",
        "documents.group.allergies",
        "documents.section.advice",
    ):
        assert translate(key, "ur") in page.text, key


async def test_the_saved_prescription_shows_the_same_details_after_confirming(
    client, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", CLINICAL_RX)
    done = await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=confirm_body(detail))
    assert done.json()["status"] == "finalized"
    assert done.json()["sections"] == detail["sections"]
    html = (await client.get(f"/en/patient/documents/{detail['id']}")).text
    assert "Acute bronchitis" in html and "Paracetamol" in html
    page = without_ocr_text(html)
    assert "Vital signs" in page and "98.6°F" in page and "120/80 mmHg" in page


async def test_sections_are_never_shown_for_a_failed_or_waiting_document(
    client, fake_ocr, patient_user
):
    waiting = await upload(client)
    assert (await client.get(f"/api/v1/documents/{waiting.json()['id']}")).json()[
        "sections"
    ] is None
    fake_ocr.error = ocr.OCR_EMPTY
    failed = await upload(client)
    body = (await client.post(f"/api/v1/documents/{failed.json()['id']}/process")).json()
    assert body["status"] == "failed" and body["sections"] is None


async def test_lab_rows_carry_a_flag_printed_by_the_report_or_calculated_from_the_range(
    client, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "lab_report", FLAGGED_LAB)
    by_test = {r["fields"][0]["value"]: (r["flag"], r["flag_source"]) for r in detail["rows"]}
    assert by_test == {
        "HbA1c": ("high", "printed"),  # the report's own "H"
        "Fasting Glucose": ("high", "calculated"),  # 142 > 100, nothing printed
        "Hemoglobin": ("normal", "calculated"),  # inside 12-15.5
        "Creatinine": ("low", "printed"),  # the report's own "L", whatever the arithmetic says
    }


async def test_the_review_and_saved_lab_pages_show_the_flag_and_say_when_it_was_calculated(
    client, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "lab_report", FLAGGED_LAB)
    review = (await client.get(f"/en/patient/documents/{detail['id']}")).text
    await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=confirm_body(detail))
    saved = (await client.get(f"/en/patient/documents/{detail['id']}")).text
    assert (
        ">Flag</th>" in saved and ">Flag</th>" not in review
    )  # the review step has no extra column
    for html in (review, saved):
        assert "badge--err" in html and ">High<" in html and ">Normal<" in html and ">Low<" in html
        assert "calculated from the reference range" in html  # only the calculated ones say so
        assert html.count("calculated from the reference range") == 2  # glucose + haemoglobin


async def test_a_flag_follows_the_patients_correction_and_a_printed_one_is_dropped_with_it(
    client, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "lab_report", FLAGGED_LAB)
    hba1c = next(r for r in detail["rows"] if r["fields"][0]["value"] == "HbA1c")
    body = confirm_body(detail, {(hba1c["index"], "result"): "5.0"})
    saved = (await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=body)).json()
    row = next(r for r in saved["rows"] if r["fields"][0]["value"] == "HbA1c")
    # The report said "H" beside 7.2; the patient says the result is 5.0, which is inside
    # 4.0-5.6 — so the printed marker no longer applies and the flag is recalculated.
    assert (row["flag"], row["flag_source"]) == ("normal", "calculated")


async def test_a_flag_is_never_stored_and_no_new_field_name_is_needed(
    client, db, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "lab_report", FLAGGED_LAB)
    names = {e.field_name for e in await extraction_rows(db, detail["id"])}
    assert names == {"test_name", "result", "unit", "reference_range"}


async def test_other_document_types_have_no_flag(client, fake_ocr, fake_gemini, patient_user):
    detail = await upload_and_process(client, fake_ocr, "prescription", CLINICAL_RX)
    assert all(r["flag"] is None and r["flag_source"] is None for r in detail["rows"])


async def test_a_vaccination_record_has_its_sections_read_by_the_ai_and_stored(
    client, db, fake_ocr, fake_gemini, patient_user, monkeypatch
):
    from app.documents import ai_sections

    monkeypatch.setattr(settings, "groq_api_key", "k")  # AI configured: the REAL extract runs
    asked: list[str] = []

    async def spy(contents, system_instruction, response_model=None):
        asked.append(contents)
        return ai_sections._AiSections.model_validate(
            {"vaccines": [{"name": "BCG", "date_given": "02-03-2024"}]}
        )

    monkeypatch.setattr(ai_understanding, "call_llm", spy)
    detail = await upload_and_process(
        client, fake_ocr, "vaccination", "Immunization card\nBCG 02-03-2024"
    )
    assert ai_sections.is_enabled()
    assert len(asked) == 1 and "DOCUMENT TYPE: vaccination record" in asked[0]
    stored = (await job_row(db, detail["id"])).ai_sections
    assert stored["entries"] == [{"vaccine_name": "BCG", "date_given": "02-03-2024"}]
    assert fake_gemini.calls == []  # the medicines layer still never sees a vaccination record


# ═══ Lab reports: a second, table-shaped read when the ranges are missing ═══
# Tesseract's default layout reads a results table column by column, so the Reference
# Range column comes out as a separate block, far from its rows. A lab report that
# comes back without its ranges is read once more in table layout and the better
# read is kept — never for another document type, and never at the cost of rows.
DETACHED_RANGES = """Chughtai Lab   Report Date: 28/07/2026
Patient Name: Ayesha Raza
Hemoglobin 128 g/dL
Fasting Glucose 142 mg/dL
Reference Range
12-15.5
70-100"""

ROW_WISE = """Chughtai Lab   Report Date: 28/07/2026
Patient Name: Ayesha Raza
Hemoglobin 12.8 g/dL 12-15.5
Fasting Glucose 142 mg/dL 70-100"""


async def test_a_lab_report_missing_its_ranges_is_read_again_in_table_layout(
    client, db, fake_ocr, patient_user
):
    fake_ocr.table_text = ROW_WISE
    detail = await upload_and_process(client, fake_ocr, "lab_report", DETACHED_RANGES)
    assert fake_ocr.layouts == ["page", "table"]
    rows = {r["fields"][0]["value"]: r["fields"] for r in detail["rows"]}
    assert rows["Hemoglobin"][1]["value"] == "12.8"  # the decimal the page read had dropped
    assert rows["Hemoglobin"][3]["value"] == "12-15.5"
    assert (await doc_row(db, detail["id"])).ocr_text == ROW_WISE  # and the text shown is that read
    assert [r["flag"] for r in detail["rows"]] == ["normal", "high"]


async def test_a_lab_report_that_already_has_every_range_is_read_once(
    client, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "lab_report", ROW_WISE)
    assert fake_ocr.layouts == ["page"]
    assert len(detail["rows"]) == 2


async def test_the_table_read_is_kept_only_if_it_recovers_more_ranges_without_losing_rows(
    client, db, fake_ocr, patient_user
):
    # Same number of ranges: not better, so the first read stands.
    fake_ocr.table_text = DETACHED_RANGES
    first = await upload_and_process(client, fake_ocr, "lab_report", DETACHED_RANGES)
    assert (await doc_row(db, first["id"])).ocr_text == DETACHED_RANGES

    # More ranges but fewer rows: the rows are the document, so the first read stands.
    fake_ocr.table_text = "Hemoglobin 12.8 g/dL 12-15.5"
    second = await upload_and_process(client, fake_ocr, "lab_report", DETACHED_RANGES)
    assert (await doc_row(db, second["id"])).ocr_text == DETACHED_RANGES
    assert len(second["rows"]) == 2


async def test_a_failed_second_read_never_fails_the_document(client, fake_ocr, patient_user):
    fake_ocr.table_error = ocr.OCR_FAILED
    detail = await upload_and_process(client, fake_ocr, "lab_report", DETACHED_RANGES)
    assert detail["status"] == "awaiting_review" and len(detail["rows"]) == 2
    assert fake_ocr.layouts == ["page", "table"]


async def test_a_lab_report_with_no_rows_at_first_is_given_the_table_read_too(
    client, fake_ocr, patient_user
):
    fake_ocr.table_text = ROW_WISE
    detail = await upload_and_process(
        client, fake_ocr, "lab_report", "Chughtai Lab\nPatient Name: Ayesha Raza\nHemoglobin"
    )
    assert fake_ocr.layouts == ["page", "table"] and len(detail["rows"]) == 2


async def test_no_other_document_type_is_ever_read_in_table_layout(
    client, fake_ocr, fake_gemini, patient_user
):
    fake_ocr.table_text = ROW_WISE
    for doc_type, text in (
        ("prescription", RX_TEXT),
        ("vaccination", VACCINE_TEXT),
        ("other", "Discharge Summary\nAppendicitis"),
    ):
        fake_ocr.layouts.clear()
        await upload_and_process(client, fake_ocr, doc_type, text)
        assert fake_ocr.layouts == ["page"], doc_type


# ═══ Reviewing every table: diagnosis, symptoms, vitals, allergies, doses ══
# A document has several tables of rows (`documents/groups.py`), all stored as
# `ocr_extraction` rows and all edited in the same review step. The contract worth pinning:
# a table the confirm request does not name is left exactly as proposed (so a client that
# only knows the medicines can never erase the rest), and allergies reach the patient's
# profile only when the patient has reviewed that table.
ALLERGIC_RX = """City Health Clinic
Patient Name: Ayesha Khan   Age: 32
Date: 27-09-2026
Allergies: Penicillin (rash), Sulfa drugs
Diagnosis: Acute bronchitis; Hypertension
Rx
1. Tab. Paracetamol 500 mg 1-1-1 x 3 days
Dr. Sara Ahmed"""

NO_ALLERGY_RX = ALLERGIC_RX.replace("Allergies: Penicillin (rash), Sulfa drugs", "Allergies: NKDA")


def confirm_everything(detail: dict, edits=None, remove=(), add=()) -> dict:
    """A confirm payload that REVIEWS every table of the document, as the web page does."""
    edits = edits or {}
    rows = []
    for table in detail["groups"]:
        for row in table["rows"]:
            if row["index"] in remove:
                continue
            fields = {
                f["name"]: edits.get((row["index"], f["name"]), f["value"]) for f in row["fields"]
            }
            rows.append({"index": row["index"], "fields": fields})
    rows.extend(add)
    return {"rows": rows, "groups": [table["key"] for table in detail["groups"]]}


def web_form(detail: dict, edits=None, remove=(), add=None) -> dict:
    """What the review form posts: every input of every table, `row-<index>-<field>`."""
    edits = edits or {}
    data: dict = {}
    for table in detail["groups"]:
        for row in table["rows"]:
            for f in row["fields"]:
                data[f"row-{row['index']}-{f['name']}"] = edits.get(
                    (row["index"], f["name"]), f["value"]
                )
    for (index, name), value in (add or {}).items():
        data[f"row-{index}-{name}"] = value
    if remove:
        data["remove_row"] = [str(i) for i in remove]
    # The date input is always posted, with the proposed date in it unless the patient changed it.
    data["document_date"] = detail.get("document_date") or ""
    return data


async def profile_allergies(db, user) -> list[Allergy]:
    stmt = (
        select(Allergy)
        .join(Patient, Patient.id == Allergy.patient_id)
        .where(Patient.user_id == user.id, Allergy.removed_at.is_(None))
        .order_by(Allergy.substance)
    )
    return list((await db.execute(stmt.execution_options(populate_existing=True))).scalars())


def table_of(detail: dict, key: str) -> dict:
    return next(g for g in detail["groups"] if g["key"] == key)


# ── Proposals ────────────────────────────────────────────────────────────
async def test_every_table_of_a_document_is_proposed_in_its_own_block_of_row_indexes(
    client, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", ALLERGIC_RX)
    indexes = {g["key"]: [r["index"] for r in g["rows"]] for g in detail["groups"]}
    assert indexes == {
        "medicines": [0],
        "diagnoses": [1000, 1001],  # "Acute bronchitis; Hypertension"
        "symptoms": [],
        "vitals": [3000],
        "allergies": [4000, 4001],
    }
    # a client that adds a medicine at "the next index" cannot land on another table's row
    assert max(indexes["medicines"]) + 1 < 1000


async def test_a_document_with_nothing_for_a_table_still_has_its_vitals_row_to_fill_in(
    client, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", RX_TEXT)
    (row,) = table_of(detail, "vitals")["rows"]
    assert [f["value"] for f in row["fields"]] == [""] * 9
    assert table_of(detail, "diagnoses")["rows"] == [] == table_of(detail, "allergies")["rows"]


async def test_the_lab_report_has_one_table_and_no_clinical_ones(client, fake_ocr, patient_user):
    detail = await upload_and_process(client, fake_ocr, "lab_report", FLAGGED_LAB)
    assert [g["key"] for g in detail["groups"]] == ["results"]


# ── The confirm contract ─────────────────────────────────────────────────
async def test_confirming_with_only_the_medicines_leaves_every_other_table_exactly_as_proposed(
    client, db, fake_ocr, fake_gemini, patient_user
):
    """The legacy call (`rows` only, no `groups`): the diagnosis and vitals are neither
    erased nor marked reviewed, and no allergy is written to the profile."""
    detail = await upload_and_process(client, fake_ocr, "prescription", ALLERGIC_RX)
    saved = (
        await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=confirm_body(detail))
    ).json()
    assert saved["status"] == "finalized"
    assert [r["fields"][0]["value"] for r in table_of(saved, "diagnoses")["rows"]] == [
        "Acute bronchitis",
        "Hypertension",
    ]
    assert [r["fields"][0]["value"] for r in table_of(saved, "allergies")["rows"]] == [
        "Penicillin",
        "Sulfa drugs",
    ]
    cells = await extraction_rows(db, detail["id"])
    extras = [c for c in cells if c.field_name in ("diagnosis", "allergy_substance")]
    assert extras and all(c.reviewed_at is None and c.corrected_value is None for c in extras)
    medicine = next(c for c in cells if c.field_name == "medicine")
    assert medicine.reviewed_at is not None
    assert await profile_allergies(db, patient_user) == []  # unreviewed: never written


async def test_reviewing_a_table_means_a_row_left_out_is_a_row_removed(
    client, db, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", ALLERGIC_RX)
    body = confirm_everything(detail, remove={1001})  # drop "Hypertension"
    saved = (await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=body)).json()
    assert [r["fields"][0]["value"] for r in table_of(saved, "diagnoses")["rows"]] == [
        "Acute bronchitis"
    ]
    cell = next(
        c
        for c in await extraction_rows(db, detail["id"])
        if c.field_name == "diagnosis" and c.row_index == 1001
    )
    assert cell.extracted_value == "Hypertension"  # what the document said is kept
    assert cell.corrected_value == "" and cell.reviewed_at is not None  # and what they decided


async def test_a_corrected_value_is_stored_beside_the_proposal_for_every_table(
    client, db, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", CLINICAL_RX)
    body = confirm_everything(
        detail, {(1000, "diagnosis"): "Acute viral bronchitis", (3000, "pulse"): "82 bpm"}
    )
    saved = (await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=body)).json()
    cells = {(c.row_index, c.field_name): c for c in await extraction_rows(db, detail["id"])}
    diagnosis = cells[(1000, "diagnosis")]
    assert (diagnosis.extracted_value, diagnosis.corrected_value) == (
        "Acute bronchitis",
        "Acute viral bronchitis",
    )
    assert cells[(3000, "pulse")].extracted_value == "78 bpm"
    assert cells[(3000, "pulse")].corrected_value == "82 bpm"
    assert cells[(3000, "blood_pressure")].corrected_value is None  # accepted as read
    assert cells[(3000, "blood_pressure")].reviewed_by == patient_user.id
    page = (await client.get(f"/en/patient/documents/{saved['id']}")).text
    assert "Acute viral bronchitis" in page and "Edited by you" in page
    assert "82 bpm" in page  # the corrected vital sign is what the saved record shows


async def test_a_row_the_patient_adds_to_any_table_is_stored_as_theirs(
    client, db, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", RX_TEXT)
    add = [
        {"index": 1000, "fields": {"diagnosis": "Migraine"}},
        {"index": 2000, "fields": {"symptom": "Headache"}},
        {"index": 4000, "fields": {"allergy_substance": "Latex"}},
    ]
    body = confirm_everything(detail, add=add)
    saved = (await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=body)).json()
    assert [r["fields"][0]["value"] for r in table_of(saved, "diagnoses")["rows"]] == ["Migraine"]
    assert [r["fields"][0]["value"] for r in table_of(saved, "symptoms")["rows"]] == ["Headache"]
    added = [c for c in await extraction_rows(db, detail["id"]) if c.field_name == "diagnosis"]
    assert [(c.extracted_value, c.corrected_value) for c in added] == [(None, "Migraine")]


async def test_a_medicine_added_at_the_next_index_never_lands_on_another_tables_row(
    client, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", ALLERGIC_RX)
    next_index = len(detail["rows"])  # what a client that only knows the medicines would pick
    body = {"rows": [{"index": next_index, "fields": {"medicine": "Panadol"}}]}
    body["rows"] = [{"index": r["index"], "fields": {}} for r in detail["rows"]] + body["rows"]
    r = await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=body)
    assert r.status_code == 200, r.text
    assert [row["fields"][0]["value"] for row in r.json()["rows"]] == ["Paracetamol", "Panadol"]


@pytest.mark.parametrize(
    "bad_body",
    [
        {"groups": ["no_such_table"]},
        {"groups": ["results"]},  # a lab table on a prescription
        # a row for a table the request does not review
        {"rows": [{"index": 1000, "fields": {"diagnosis": "X"}}]},
        # a row in the wrong block of indexes
        {"groups": ["diagnoses"], "rows": [{"index": 5, "fields": {"diagnosis": "X"}}]},
        # one row spanning two tables
        {
            "groups": ["medicines", "diagnoses"],
            "rows": [{"index": 1005, "fields": {"diagnosis": "X", "medicine": "Y"}}],
        },
        # a field that belongs to no table of a prescription
        {"rows": [{"index": 1, "fields": {"vaccine_name": "BCG"}}]},
        # a row that exists in one table, claimed by another
        {
            "groups": ["medicines", "symptoms"],
            "rows": [{"index": 0, "fields": {"symptom": "cough"}}],
        },
    ],
)
async def test_a_confirm_that_mixes_up_the_tables_is_refused_and_changes_nothing(
    client, db, fake_ocr, fake_gemini, patient_user, bad_body
):
    detail = await upload_and_process(client, fake_ocr, "prescription", ALLERGIC_RX)
    body = {"rows": [{"index": 0, "fields": {}}], **bad_body}
    if "rows" in bad_body:
        body["rows"] = [{"index": 0, "fields": {}}, *bad_body["rows"]]
    r = await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=body)
    assert r.status_code == 422, r.text
    assert (await doc_row(db, detail["id"])).status == "awaiting_review"


async def test_a_table_is_capped_but_an_empty_add_a_row_line_costs_nothing(
    client, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", RX_TEXT)
    too_many = [{"index": 1000 + i, "fields": {"diagnosis": f"D{i}"}} for i in range(11)]
    over = confirm_everything(detail, add=too_many)
    assert (
        await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=over)
    ).status_code == 422
    ten = confirm_everything(
        detail,
        add=[*too_many[:10], {"index": 1010, "fields": {"diagnosis": ""}}],  # + an empty line
    )
    assert (
        await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=ten)
    ).status_code == 200


async def test_a_prescription_still_needs_a_medicine_whatever_else_is_reviewed(
    client, db, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", ALLERGIC_RX)
    body = confirm_everything(detail, remove={0})  # every medicine removed
    r = await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=body)
    assert r.status_code == 422
    assert (await doc_row(db, detail["id"])).status == "awaiting_review"


# ── Allergies join the patient's profile ─────────────────────────────────
async def test_confirmed_allergies_join_the_patients_own_profile(
    client, db, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", ALLERGIC_RX)
    saved = await client.post(
        f"/api/v1/documents/{detail['id']}/confirm", json=confirm_everything(detail)
    )
    assert saved.status_code == 200
    allergies = await profile_allergies(db, patient_user)
    assert [(a.substance, a.reaction) for a in allergies] == [
        ("Penicillin", "rash"),
        ("Sulfa drugs", None),
    ]
    # recorded BY the patient, so the profile page lets them edit or remove it later
    assert all(a.recorded_by == patient_user.id for a in allergies)
    profile = (await client.get("/api/v1/me/profile")).json()
    assert sorted(a["substance"] for a in profile["allergies"]) == ["Penicillin", "Sulfa drugs"]
    assert all(a["editable"] for a in profile["allergies"])


async def test_what_the_patient_corrects_or_removes_is_what_reaches_the_profile(
    client, db, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", ALLERGIC_RX)
    body = confirm_everything(
        detail,
        edits={(4000, "allergy_substance"): "Penicillin V", (4000, "allergy_severity"): "SEVERE"},
        remove={4001},  # "Sulfa drugs" was not an allergy after all
    )
    await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=body)
    (allergy,) = await profile_allergies(db, patient_user)
    assert (allergy.substance, allergy.reaction, allergy.severity) == (
        "Penicillin V",
        "rash",
        "severe",
    )


async def test_a_severity_the_profile_does_not_know_is_left_out_but_the_allergy_is_kept(
    client, db, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", ALLERGIC_RX)
    body = confirm_everything(detail, edits={(4000, "allergy_severity"): "very bad"}, remove={4001})
    await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=body)
    (allergy,) = await profile_allergies(db, patient_user)
    assert allergy.substance == "Penicillin" and allergy.severity is None


async def test_no_known_allergies_adds_nothing(client, db, fake_ocr, fake_gemini, patient_user):
    detail = await upload_and_process(client, fake_ocr, "prescription", NO_ALLERGY_RX)
    assert table_of(detail, "allergies")["rows"] == []
    await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=confirm_everything(detail))
    assert await profile_allergies(db, patient_user) == []


async def test_an_allergy_already_on_the_profile_is_skipped_and_the_document_still_saves(
    client, db, fake_ocr, fake_gemini, patient_user
):
    created = await client.post("/api/v1/me/allergies", json={"substance": "penicillin"})
    assert created.status_code == 201, created.text
    detail = await upload_and_process(client, fake_ocr, "prescription", ALLERGIC_RX)
    saved = await client.post(
        f"/api/v1/documents/{detail['id']}/confirm", json=confirm_everything(detail)
    )
    assert saved.json()["status"] == "finalized"
    names = sorted(a.substance.casefold() for a in await profile_allergies(db, patient_user))
    assert names == ["penicillin", "sulfa drugs"]  # one duplicate refused, the other added


async def test_the_web_confirm_says_how_many_allergies_were_added_and_how_many_were_not(
    client, db, fake_ocr, fake_gemini, patient_user
):
    await client.post("/api/v1/me/allergies", json={"substance": "Sulfa drugs"})
    detail = await upload_and_process(client, fake_ocr, "prescription", ALLERGIC_RX)
    r = await client.post(f"/en/patient/documents/{detail['id']}/confirm", data=web_form(detail))
    assert r.status_code == 303
    assert r.headers["location"].endswith("?saved=1&allergies=1&skipped=1")
    page = (await client.get(r.headers["location"])).text
    assert "Added to your profile&#39;s allergy list: 1." in page or (
        "Added to your profile's allergy list: 1." in page
    )
    assert "Not added (already on your list, or the list is full): 1." in page


async def test_the_audit_trail_counts_the_allergies_but_never_names_them(
    client, db, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", ALLERGIC_RX)
    await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=confirm_everything(detail))
    rows = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "document.confirm")))
        .scalars()
        .all()
    )
    blob = " ".join(r.detail for r in rows)
    assert '"allergies_confirmed": 2' in blob
    assert "Penicillin" not in blob and "Sulfa" not in blob and "Acute bronchitis" not in blob


async def test_another_patients_profile_is_never_touched(
    app, client, db, fake_ocr, fake_gemini, patient_user, second_patient
):
    detail = await upload_and_process(client, fake_ocr, "prescription", ALLERGIC_RX)
    await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=confirm_everything(detail))
    assert len(await profile_allergies(db, patient_user)) == 2
    assert await profile_allergies(db, second_patient) == []


# ── The web form ─────────────────────────────────────────────────────────
async def test_the_review_form_round_trips_every_table(
    client, db, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", CLINICAL_RX)
    data = web_form(
        detail,
        edits={(1000, "diagnosis"): "Acute viral bronchitis"},
        add={(2001, "symptom"): "Sore throat", (4000, "allergy_substance"): "Latex"},
    )
    r = await client.post(f"/en/patient/documents/{detail['id']}/confirm", data=data)
    assert r.status_code == 303 and "saved=1" in r.headers["location"]
    saved = (await client.get(f"/api/v1/documents/{detail['id']}")).json()
    assert [row["fields"][0]["value"] for row in table_of(saved, "diagnoses")["rows"]] == [
        "Acute viral bronchitis"
    ]
    assert [row["fields"][0]["value"] for row in table_of(saved, "symptoms")["rows"]] == [
        "fever and cough for 3 days",
        "Sore throat",
    ]
    assert [a.substance for a in await profile_allergies(db, patient_user)] == ["Latex"]


async def test_removing_a_row_in_the_review_form_removes_it_from_the_record(
    client, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", ALLERGIC_RX)
    data = web_form(detail, remove=[1001])
    # the browser still sends the removed row's inputs; the checkbox is what removes it
    r = await client.post(f"/en/patient/documents/{detail['id']}/confirm", data=data)
    assert r.status_code == 303
    saved = (await client.get(f"/api/v1/documents/{detail['id']}")).json()
    assert [row["fields"][0]["value"] for row in table_of(saved, "diagnoses")["rows"]] == [
        "Acute bronchitis"
    ]


async def test_a_failed_save_puts_the_patients_typing_back_in_every_table(
    client, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", ALLERGIC_RX)
    data = web_form(
        detail,
        edits={(0, "medicine"): "", (1000, "diagnosis"): "Typed by me"},  # no medicine: refused
    )
    r = await client.post(f"/en/patient/documents/{detail['id']}/confirm", data=data)
    assert r.status_code == 422
    assert 'value="Typed by me"' in r.text and 'name="row-0-medicine"' in r.text


# ── Documents saved before the tables existed ────────────────────────────
async def test_a_record_saved_before_the_tables_existed_still_shows_the_computed_sections(
    client, db, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", CLINICAL_RX)
    await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=confirm_body(detail))
    # Take away every cell that only the new tables have — what an older record looks like.
    job = await job_row(db, detail["id"])
    await db.execute(
        delete(OcrExtraction).where(
            OcrExtraction.job_id == job.id,
            OcrExtraction.field_name.notin_(("medicine", "dosage", "frequency", "duration")),
        )
    )
    await db.commit()
    again = (await client.get(f"/api/v1/documents/{detail['id']}")).json()
    sections = again["sections"]
    assert {i["key"]: i["value"] for i in sections["clinical"]} == {
        "complaint": "fever and cough for 3 days",
        "diagnosis": "Acute bronchitis",
    }
    assert {i["key"] for i in sections["vitals"]} >= {"blood_pressure", "pulse"}
    page = without_ocr_text((await client.get(f"/en/patient/documents/{detail['id']}")).text)
    assert "Symptoms &amp; Diagnosis" in page and "120/80 mmHg" in page


async def test_a_saved_record_shows_a_table_only_when_the_patient_kept_a_row_in_it(
    client, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", RX_TEXT)
    await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=confirm_everything(detail))
    page = (await client.get(f"/en/patient/documents/{detail['id']}")).text
    assert "Metformin" in page
    for heading in ("Vital signs", "Allergies", "Symptoms</h3>", "Diagnosis</h3>"):
        assert heading not in page, heading  # nothing was found, nothing was added


# ═══ Vital signs: a table of the review page, stored and reviewed like the rest ═
# The patient sees the nine vitals, with whatever the document stated filled in, and can
# correct, add or clear any of them; what they confirm is what the saved record shows.
VITALS_ONLY_VALUES = {"blood_pressure": "120/80 mmHg", "pulse": "78 bpm", "temperature": "98.6°F"}


async def vitals_cells(db, doc_id) -> dict[str, OcrExtraction]:
    return {
        c.field_name: c
        for c in await extraction_rows(db, doc_id)
        if c.field_name in groups.VITALS.fields
    }


async def test_the_vital_signs_are_stored_when_the_document_is_read(
    client, db, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", CLINICAL_RX)
    cells = await vitals_cells(db, detail["id"])
    assert {n: c.extracted_value for n, c in cells.items() if c.extracted_value} == {
        **VITALS_ONLY_VALUES,
        "spo2": "98%",
    }
    assert set(cells) == set(groups.VITALS.fields) and len(cells) == 9  # every vital has a cell
    assert all(c.row_index == 3000 for c in cells.values())  # one row, in the vitals block


async def test_the_vital_signs_are_a_table_of_the_review_page_with_every_vital_to_fill_in(
    client, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", CLINICAL_RX)
    page = without_ocr_text((await client.get(f"/en/patient/documents/{detail['id']}")).text)
    assert "Vital signs" in page
    for vital in groups.VITALS.fields:
        assert f'name="row-3000-{vital}"' in page, vital
    assert 'value="120/80 mmHg"' in page and 'value="78 bpm"' in page


async def test_confirm_and_save_stores_the_vital_signs_the_patient_reviewed(
    client, db, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", CLINICAL_RX)
    data = web_form(
        detail, edits={(3000, "pulse"): "82 bpm", (3000, "weight"): "70 kg", (3000, "spo2"): ""}
    )
    r = await client.post(f"/en/patient/documents/{detail['id']}/confirm", data=data)
    assert r.status_code == 303
    cells = await vitals_cells(db, detail["id"])
    assert (cells["pulse"].extracted_value, cells["pulse"].corrected_value) == ("78 bpm", "82 bpm")
    assert (cells["weight"].extracted_value, cells["weight"].corrected_value) == (None, "70 kg")
    assert cells["spo2"].extracted_value == "98%" and cells["spo2"].corrected_value == ""  # cleared
    assert cells["blood_pressure"].corrected_value is None  # accepted as read...
    assert cells["blood_pressure"].reviewed_at is not None  # ...and checked by the patient
    assert cells["blood_pressure"].reviewed_by == patient_user.id

    page = without_ocr_text((await client.get(f"/en/patient/documents/{detail['id']}")).text)
    assert "Vital signs" in page and "120/80 mmHg" in page
    assert (
        "82 bpm" in page and "70 kg" in page and "Edited by you" not in page.split("Vital signs")[0]
    )
    assert "78 bpm" not in page and "98%" not in page  # replaced / cleared, not shown


async def test_a_vital_sign_the_document_never_stated_is_not_listed_on_the_saved_record(
    client, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", CLINICAL_RX)
    await client.post(f"/en/patient/documents/{detail['id']}/confirm", data=web_form(detail))
    page = without_ocr_text((await client.get(f"/en/patient/documents/{detail['id']}")).text)
    for stated in ("Blood Pressure", "Pulse", "Temperature"):
        assert stated in page, stated
    for never_stated in ("Height", "BMI", "Blood Sugar", "Respiratory Rate"):
        assert never_stated not in page, never_stated


# ── An "other" medical document is read like a prescription ──────────────
OTHER_WITH_MEDICINES = """City Hospital
Discharge Summary
Date: 05-10-2026
Patient Name: Ayesha Khan
Complaint: abdominal pain
Diagnosis: Appendicitis
Allergies: Penicillin (rash)
BP: 118/76 mmHg  Pulse: 80 bpm
1. Tab. Ceftriaxone 1 g twice daily x 5 days
2. Tab. Paracetamol 500 mg 1-1-1
Advice: avoid heavy lifting for 4 weeks
Dr. Sara Ahmed"""


async def test_an_other_document_gets_medicines_diagnosis_symptoms_and_advice(
    client, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "other", OTHER_WITH_MEDICINES)
    assert [r["fields"][0]["value"] for r in detail["rows"]] == ["Ceftriaxone", "Paracetamol"]
    assert [r["fields"][0]["value"] for r in table_of(detail, "diagnoses")["rows"]] == [
        "Appendicitis"
    ]
    assert [r["fields"][0]["value"] for r in table_of(detail, "symptoms")["rows"]] == [
        "abdominal pain"
    ]
    assert [r["fields"][0]["value"] for r in table_of(detail, "allergies")["rows"]] == [
        "Penicillin"
    ]
    page = without_ocr_text((await client.get(f"/en/patient/documents/{detail['id']}")).text)
    for expected in (
        'name="row-0-medicine"',
        'value="Ceftriaxone"',
        'name="row-1000-diagnosis"',
        'name="row-2000-symptom"',
        'name="row-4000-allergy_substance"',
        ">Advice and recommendations</h3>",
        "avoid heavy lifting for 4 weeks",
    ):
        assert expected in page, expected
    assert 'name="row-3000-blood_pressure"' in page


async def test_an_other_document_can_be_saved_without_any_medicine(
    client, db, fake_ocr, patient_user
):
    text = "Referral letter\nPatient Name: A B\nDiagnosis: Migraine\nAdvice: rest"
    detail = await upload_and_process(client, fake_ocr, "other", text)
    assert detail["rows"] == []
    r = await client.post(f"/en/patient/documents/{detail['id']}/confirm", data=web_form(detail))
    assert r.status_code == 303
    assert (await doc_row(db, detail["id"])).status == "finalized"
    saved = (await client.get(f"/api/v1/documents/{detail['id']}")).json()
    assert [r["fields"][0]["value"] for r in table_of(saved, "diagnoses")["rows"]] == ["Migraine"]


async def test_a_prescription_without_a_medicine_still_cannot_be_saved(
    client, db, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", ALLERGIC_RX)
    data = web_form(detail, remove=[0])  # the only medicine removed
    r = await client.post(f"/en/patient/documents/{detail['id']}/confirm", data=data)
    assert r.status_code == 422
    assert (await doc_row(db, detail["id"])).status == "awaiting_review"


async def test_an_other_documents_confirmed_allergies_still_reach_the_profile(
    client, db, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "other", OTHER_WITH_MEDICINES)
    await client.post(f"/en/patient/documents/{detail['id']}/confirm", data=web_form(detail))
    assert [(a.substance, a.reaction) for a in await profile_allergies(db, patient_user)] == [
        ("Penicillin", "rash")
    ]


async def test_the_advice_is_a_section_of_its_own_in_review_and_on_the_saved_record(
    client, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", CLINICAL_RX)
    review = without_ocr_text((await client.get(f"/en/patient/documents/{detail['id']}")).text)
    assert review.count(">Advice</h3>") == 1  # once, not repeated in the "other details" block
    assert "rest and plenty of fluids" in review
    await client.post(f"/en/patient/documents/{detail['id']}/confirm", data=web_form(detail))
    saved = without_ocr_text((await client.get(f"/en/patient/documents/{detail['id']}")).text)
    assert saved.count(">Advice</h3>") == 1 and "rest and plenty of fluids" in saved
    # A lab report keeps its own heading for what a laboratory says about the results.
    lab = await upload_and_process(
        client, fake_ocr, "lab_report", FLAGGED_LAB + "\nRemarks: repeat in 3 months"
    )
    lab_page = without_ocr_text((await client.get(f"/en/patient/documents/{lab['id']}")).text)
    assert "Notes / Interpretation" in lab_page and ">Advice</h3>" not in lab_page


# ═══ The compact layout: only the fields asked for, in the order asked for ═══
# A prescription and an "other" medical document show: hospital / clinic, doctor, patient (as
# the document names them), the document date, the upload day — then medicines, advice,
# diagnosis, symptoms, vital signs, allergies. Nothing else about the document.
FULL_RX_FOR_LAYOUT = """City Health Clinic
Shop No. 12, Main Boulevard, Lahore
Ph: 042-3587 1234
Email: info@cityhealthclinic.pk
Patient Name: Ayesha Khan
Age / Gender: 32 years / Female
Address: House No. 45, Model Town, Lahore
Date: 27-09-2026
Patient ID: 2026-4587
Complaint: fever and cough
BP: 120/80 mmHg   Pulse: 78 bpm
Diagnosis: Acute bronchitis
Allergies: Penicillin (rash)
Rx
1. Tab. Paracetamol 500 mg 1-1-1 x 3 days
Notes:
- Drink plenty of water.
Dr. Sara Ahmed
MBBS, FCPS (Internal Medicine)"""

# What must NOT be on the page for such a document (it is all still in the raw text).
NOT_ON_THE_PAGE = (
    "Patient Information",
    "Doctor Information",
    "Hospital / Clinic Information",
    "Prescription Information",
    "MBBS, FCPS",
    "Internal Medicine",
    "Model Town",
    "2026-4587",
    "042-3587",
    "info@cityhealthclinic.pk",
    "Female",
)


def positions(page: str, *needles: str) -> list[int]:
    """Where each needle first appears in `page` (asserting that it does)."""
    found = []
    for needle in needles:
        assert needle in page, needle
        found.append(page.index(needle))
    return found


async def test_the_review_page_of_a_prescription_holds_only_the_asked_for_fields_in_order(
    client, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", FULL_RX_FOR_LAYOUT)
    page = without_ocr_text((await client.get(f"/en/patient/documents/{detail['id']}")).text)
    for gone in NOT_ON_THE_PAGE:
        assert gone not in page, gone
    assert "Other details found in your document" not in page
    # who / for whom, as the document says it
    for label, value in (
        ("Hospital / Clinic Name", "City Health Clinic"),
        ("Doctor Name", "Dr. Sara Ahmed"),
        ("Patient Name", "Ayesha Khan"),
    ):
        assert label in page and value in page, label
    # the document date is the editable one, the upload day is read-only
    assert 'name="document_date"' in page and 'value="2026-09-27"' in page
    assert "Uploaded on" in page
    # ...then, in the order asked for
    order = positions(
        page,
        "Hospital / Clinic Name",
        "Doctor Name",
        "Patient Name",
        'name="document_date"',
        "Uploaded on",
        'id="grp-medicines-h"',
        ">Advice</h3>",
        'id="grp-diagnoses-h"',
        'id="grp-symptoms-h"',
        'id="grp-vitals-h"',
        'id="grp-allergies-h"',
    )
    assert order == sorted(order)
    assert page.count('name="document_date"') == 1  # moved to the top, not repeated at the bottom


async def test_the_saved_prescription_holds_the_same_fields_in_the_same_order(
    client, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", FULL_RX_FOR_LAYOUT)
    await client.post(f"/en/patient/documents/{detail['id']}/confirm", data=web_form(detail))
    page = without_ocr_text((await client.get(f"/en/patient/documents/{detail['id']}")).text)
    for gone in NOT_ON_THE_PAGE:
        assert gone not in page, gone
    order = positions(
        page,
        "Document details",
        "Hospital / Clinic Name",
        "Doctor Name",
        "Patient Name",
        "Document date",
        "Uploaded on",
        'data-group="medicines"',
        ">Advice</h3>",
        'data-group="diagnoses"',
        'data-group="symptoms"',
        'data-group="vitals"',
        'data-group="allergies"',
    )
    assert order == sorted(order)
    for expected in ("2026-09-27", "Paracetamol", "Drink plenty of water.", "Acute bronchitis"):
        assert expected in page, expected
    assert "fever and cough" in page and "120/80 mmHg" in page and "Penicillin" in page


async def test_the_upload_day_is_shown_and_is_the_day_it_was_uploaded(
    client, db, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", FULL_RX_FOR_LAYOUT)
    uploaded = pakistan_day((await doc_row(db, detail["id"])).uploaded_at)
    page = without_ocr_text((await client.get(f"/en/patient/documents/{detail['id']}")).text)
    assert page.index("Uploaded on") < page.index(f"{uploaded}", page.index("Uploaded on")) + 1
    assert uploaded in page[page.index("Uploaded on") :][:200]


async def test_a_name_the_reader_did_not_find_says_so_instead_of_vanishing(
    client, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", RX_TEXT)
    page = without_ocr_text((await client.get(f"/en/patient/documents/{detail['id']}")).text)
    for label in ("Hospital / Clinic Name", "Patient Name", "Doctor Name"):
        row = page[page.index(label) :][:200]
        assert label in page and ("Not specified" in row or "Dr." in row or "Mehreen" in row)


async def test_a_lab_report_and_a_vaccination_card_do_not_use_the_prescription_layout(
    client, fake_ocr, patient_user
):
    """Each type has the page its content calls for: a prescription's "Document details" block,
    medicines and advice are not forced onto a lab report or a vaccination card."""
    lab = await upload_and_process(client, fake_ocr, "lab_report", FULL_LAB_TEXT)
    lab_page = without_ocr_text((await client.get(f"/en/patient/documents/{lab['id']}")).text)
    assert "Document details" not in lab_page and "Laboratory Information" in lab_page
    card = await upload_and_process(client, fake_ocr, "vaccination", CARD_TEXT)
    card_page = without_ocr_text((await client.get(f"/en/patient/documents/{card['id']}")).text)
    assert "Document details" not in card_page and 'name="row-0-vaccine_name"' in card_page


async def test_the_raw_text_is_kept_but_folded_away_on_a_prescription_page(
    client, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", FULL_RX_FOR_LAYOUT)
    review = (await client.get(f"/en/patient/documents/{detail['id']}")).text
    assert "Text read from your document" in review and "<details open>" not in review
    assert "MBBS, FCPS" in review  # still there, for checking against the original


# ═══ A layout for each type of document ═══════════════════════════════════
# A prescription keeps its compact layout (above). A lab report, a vaccination card and an
# "other" document each get the page their content calls for, showing only what the reader found,
# with the day it was uploaded always apart from the date written on the document.
LAB_TWO_NAMES_TWO_DATES = """City General Hospital
Chughtai Diagnostic Laboratory
Complete Blood Count (CBC)
Patient Name: Bilal Hussain   Age: 41   Gender: Male
Referred By: Dr. Imran Qureshi
Sample Collected: 10-08-2026
Report Date: 11-08-2026
Test Result Unit Reference Range
Hemoglobin 13.1 g/dL 12-15.5
WBC Count 11.8 x10^3/uL 4.0-11.0 H
Platelets 250 x10^3/uL 150-450
Comments: Slightly elevated WBC, recommend follow-up."""

CERTIFICATE_TEXT = """National Immunization Centre
Vaccination Certificate
Patient Name: Baby Zainab
Date of Birth: 02-03-2024
Vaccine: Pentavalent
Dose: 2
Date of Vaccination: 14-06-2026
Batch No: P9921
Site: Left thigh
Next Dose Due: 14-07-2027
Notes: Keep this card safe."""

DISCHARGE_WITH_PASSAGES = """City Hospital
Reference Laboratory
Discharge Summary
Patient Name: Ayesha Khan
Date of Admission: 02-09-2026
Date of Discharge: 06-09-2026
Diagnosis: Appendicitis
Procedure: Laparoscopic appendectomy
Findings: Inflamed, non-perforated appendix.
No collection in the pelvis.
Hospital Course: Uneventful recovery.
Condition at Discharge: Stable
Advice: Avoid heavy lifting for 4 weeks.
Recommendations: Review in clinic after 10 days.
Dr. Sara Ahmed"""

BARE_CERTIFICATE = """Medical Certificate
Patient Name: Ayesha Khan
This is to certify that the patient needs rest for 3 days.
Dr. Sara Ahmed"""


async def _review_page(client, detail) -> str:
    return without_ocr_text((await client.get(f"/en/patient/documents/{detail['id']}")).text)


async def _saved_page(client, detail, **edits) -> str:
    r = await client.post(
        f"/en/patient/documents/{detail['id']}/confirm", data=web_form(detail, **edits)
    )
    assert r.status_code == 303, r.text
    return without_ocr_text((await client.get(f"/en/patient/documents/{detail['id']}")).text)


async def test_a_lab_report_is_reviewed_in_a_lab_layout(client, fake_ocr, patient_user):
    detail = await upload_and_process(client, fake_ocr, "lab_report", LAB_TWO_NAMES_TWO_DATES)
    page = await _review_page(client, detail)
    # the laboratory and the hospital are each their own field; then who, the report's own
    # information with its date, then every test, then the report's notes — in that order
    order = positions(
        page,
        "Laboratory Information",
        "Chughtai Diagnostic Laboratory",
        "City General Hospital",
        "Patient Information",
        "Bilal Hussain",
        "Doctor Information",
        "Dr. Imran Qureshi",
        "Report Information",
        "Complete Blood Count (CBC)",
        "Collection Date",
        'name="document_date"',
        "Uploaded on",
        'id="grp-results-h"',
        "Notes / Interpretation",
    )
    assert order == sorted(order)
    # nothing of a prescription's
    for gone in (
        'id="grp-medicines-h"',
        'id="grp-diagnoses-h"',
        'id="grp-vitals-h"',
        'id="grp-allergies-h"',
        ">Advice</h3>",
        "Document details",
        "Other details found",
    ):
        assert gone not in page, gone
    assert [r["fields"][0]["value"] for r in detail["rows"]] == [
        "Hemoglobin",
        "WBC Count",
        "Platelets",
    ]


async def test_the_lab_report_date_is_the_reports_own_not_the_collection_date(
    client, db, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "lab_report", LAB_TWO_NAMES_TWO_DATES)
    assert (
        detail["document_date"] == "2026-08-11"
    )  # "Report Date", printed after "Sample Collected"
    page = await _review_page(client, detail)
    assert 'value="2026-08-11"' in page and "Report date" in page
    # it is the field's value, not also a read-only row beside it; the collection date (a different
    # date) still is shown, as printed
    assert "<dt>Report Date</dt>" not in page
    assert "<dt>Collection Date</dt>" in page and "10-08-2026" in page


async def test_the_day_a_file_was_uploaded_is_never_the_date_on_the_document(
    client, db, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "lab_report", LAB_TWO_NAMES_TWO_DATES)
    uploaded = pakistan_day((await doc_row(db, detail["id"])).uploaded_at)
    assert uploaded != detail["document_date"]
    page = await _saved_page(client, detail)
    assert uploaded in page[page.index("Uploaded on") :][:200]
    assert "2026-08-11" in page[page.index("Report date") :][:200]


async def test_the_saved_lab_report_has_the_same_layout_with_the_flag_column(
    client, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "lab_report", LAB_TWO_NAMES_TWO_DATES)
    page = await _saved_page(client, detail)
    order = positions(
        page,
        "Laboratory Information",
        "Patient Information",
        "Doctor Information",
        "Report Information",
        "Collection Date",
        "Report date",
        "Uploaded on",
        "Original file",
        'data-group="results"',
        "Notes / Interpretation",
    )
    assert order == sorted(order)
    assert ">Flag</th>" in page and "Slightly elevated WBC, recommend follow-up." in page
    for gone in ('data-group="medicines"', ">Advice</h3>", "Document details"):
        assert gone not in page, gone


async def test_a_lab_report_shows_only_the_details_it_carries(client, fake_ocr, patient_user):
    text = "Chughtai Lab\nPatient Name: A B\nHemoglobin 13.1 g/dL 12-15.5"
    detail = await upload_and_process(client, fake_ocr, "lab_report", text)
    page = await _review_page(client, detail)
    assert "Laboratory Name" in page and "Chughtai Lab" in page and "A B" in page
    for gone in (
        "Hospital / Clinic Name",
        "Doctor Information",
        "Referred By",
        "Collection Date",
        "Notes / Interpretation",
        "Not specified",
        "None",
    ):
        assert gone not in page, gone


async def test_a_hospital_only_lab_letterhead_is_the_hospital_not_a_laboratory(
    client, fake_ocr, patient_user
):
    text = "City General Hospital\nPatient Name: A B\nHemoglobin 13.1 g/dL 12-15.5"
    detail = await upload_and_process(client, fake_ocr, "lab_report", text)
    names = {i["key"]: i["value"] for i in detail["sections"]["facility"]}
    assert names == {"hospital_name": "City General Hospital"}


async def test_a_vaccination_certificate_is_reviewed_as_one_complete_dose(
    client, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "vaccination", CERTIFICATE_TEXT)
    (row,) = detail["rows"]
    assert {f["name"]: f["value"] for f in row["fields"]} == {
        "vaccine_name": "Pentavalent",
        "dose": "Dose 2",
        "date_given": "14-06-2026",
        "batch_no": "P9921",
        "site": "Left thigh",
        "next_due": "14-07-2027",
    }
    assert detail["document_date"] == "2026-06-14"  # the date of vaccination, not the birth date
    page = await _review_page(client, detail)
    order = positions(
        page,
        "Hospital / Clinic Information",
        "National Immunization Centre",
        "Patient Information",
        "Baby Zainab",
        "Document Information",
        'name="document_date"',
        "Uploaded on",
        'id="grp-vaccines-h"',
    )
    assert order == sorted(order)
    # a vaccination card has no tests, medicines or advice — and its title is not a facility
    for gone in (
        'id="grp-results-h"',
        'id="grp-medicines-h"',
        ">Advice</h3>",
        "Notes /",
        "Keep this card safe.",
        "Vaccination Certificate",
    ):
        assert gone not in page, gone


async def test_the_saved_vaccination_card_has_the_same_layout(client, fake_ocr, patient_user):
    detail = await upload_and_process(client, fake_ocr, "vaccination", CERTIFICATE_TEXT)
    page = await _saved_page(client, detail)
    order = positions(
        page,
        "Hospital / Clinic Information",
        "Patient Information",
        "Document Information",
        "Document date",
        "Uploaded on",
        "Original file",
        'data-group="vaccines"',
    )
    assert order == sorted(order)
    for expected in ("Pentavalent", "Dose 2", "14-06-2026", "P9921", "Left thigh", "14-07-2027"):
        assert expected in page, expected
    for gone in (">Advice</h3>", "Keep this card safe.", 'data-group="results"'):
        assert gone not in page, gone


async def test_a_vaccination_table_card_keeps_its_rows_and_loses_its_advice(
    client, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "vaccination", CARD_TEXT)
    assert [r["fields"][0]["value"] for r in detail["rows"]] == ["BCG", "Pentavalent"]
    page = await _review_page(client, detail)
    assert 'value="Pentavalent"' in page and "Keep this card safe." not in page
    assert "Date of Birth" in page  # the patient's own details are still shown


async def test_an_other_document_shows_its_own_passages_and_only_the_tables_it_fills(
    client, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "other", DISCHARGE_WITH_PASSAGES)
    sections = detail["sections"]
    assert [(t["key"], t["lines"]) for t in sections["topics"]] == [
        ("procedure", ["Laparoscopic appendectomy"]),
        ("findings", ["Inflamed, non-perforated appendix.", "No collection in the pelvis."]),
        ("hospital_course", ["Uneventful recovery."]),
        ("discharge_condition", ["Stable"]),
    ]
    assert sections["notes"] == [
        "Avoid heavy lifting for 4 weeks.",
        "Review in clinic after 10 days.",
    ]
    assert detail["document_date"] == "2026-09-06"  # the discharge, not the admission

    page = await _review_page(client, detail)
    order = positions(
        page,
        "Document details",
        "Hospital / Clinic Name",
        "Laboratory Name",
        "Patient Name",
        "Title",
        'name="document_date"',
        "Uploaded on",
        ">Procedure</h3>",
        ">Findings</h3>",
        ">Hospital course</h3>",
        ">Condition at discharge</h3>",
        ">Advice and recommendations</h3>",
        'id="grp-diagnoses-h"',  # the one table with something in it
        '<details class="add-more">',
    )
    assert order == sorted(order)
    # the tables with nothing in them wait behind the disclosure, not on the page
    disclosure = page.index('<details class="add-more">')
    for key in ("medicines", "symptoms", "vitals", "allergies"):
        assert page.index(f'id="grp-{key}-h"') > disclosure, key
    # the page already shows what the document says, so the raw text is folded away
    assert "<details open>" not in page


async def test_a_bare_other_document_hides_every_empty_table_and_keeps_the_text_open(
    client, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "other", BARE_CERTIFICATE)
    review = (await client.get(f"/en/patient/documents/{detail['id']}")).text
    page = without_ocr_text(review)
    # its heading is a title, not a hospital
    assert "<dt>Title</dt>" in page and "Hospital / Clinic Name" not in page
    assert "Ayesha Khan" in page and "Dr. Sara Ahmed" in page
    disclosure = page.index('<details class="add-more">')
    assert page.index('id="grp-medicines-h"') > disclosure
    assert "Add details the reader did not find" in page
    # nothing else was found in it, so its text is what the patient reads
    assert "<details open>" in review and "needs rest for 3 days" in review

    saved = await _saved_page(client, detail)
    assert 'data-group="' not in saved and "Medical Certificate" in saved
    assert "Hospital / Clinic Name" not in saved


async def test_a_table_the_reader_left_empty_can_still_be_filled_in_and_is_saved(
    client, db, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "other", BARE_CERTIFICATE)
    saved = await _saved_page(client, detail, add={(1000, "diagnosis"): "Viral fever"})
    assert "Viral fever" in saved
    api = (await client.get(f"/api/v1/documents/{detail['id']}")).json()
    assert [r["fields"][0]["value"] for r in table_of(api, "diagnoses")["rows"]] == ["Viral fever"]


async def test_the_saved_other_document_has_the_same_layout_and_no_empty_table(
    client, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "other", DISCHARGE_WITH_PASSAGES)
    page = await _saved_page(client, detail)
    order = positions(
        page,
        "Document details",
        "Document date",
        "Uploaded on",
        ">Procedure</h3>",
        ">Findings</h3>",
        ">Advice and recommendations</h3>",
        'data-group="diagnoses"',
    )
    assert order == sorted(order)
    assert "2026-09-06" in page and "Appendicitis" in page
    for gone in ('data-group="medicines"', 'data-group="allergies"', 'data-group="vitals"'):
        assert gone not in page, gone


async def test_the_new_layouts_render_in_urdu_right_to_left(client, fake_ocr, patient_user):
    other = await upload_and_process(client, fake_ocr, "other", DISCHARGE_WITH_PASSAGES)
    page = await client.get(f"/ur/patient/documents/{other['id']}")
    assert page.status_code == 200 and 'dir="rtl"' in page.text
    for key in (
        "documents.topic.findings",
        "documents.topic.hospital_course",
        "documents.section.advice_other",
        "documents.review.add_more",
    ):
        assert translate(key, "ur") in page.text, key
    lab = await upload_and_process(client, fake_ocr, "lab_report", LAB_TWO_NAMES_TWO_DATES)
    lab_page = await client.get(f"/ur/patient/documents/{lab['id']}")
    assert translate("documents.review.date_lab", "ur") in lab_page.text


# ═══ Document management: search, view / download, delete, retry ═════════
# What the patient can do with the documents they have: find them, look at (or keep a copy of)
# the original exactly as uploaded, delete one, and try a failed one again — every action
# scoped to their own documents and checked on the server.
async def seed_document(
    client, fake_ocr, *, name, doc_type="prescription", text=RX_TEXT, finalize=False
) -> dict:
    """An upload read by the (fake) OCR — left awaiting review, or confirmed if `finalize`."""
    fake_ocr.text = text
    up = await upload(client, doc_type, name=name)
    assert up.status_code == 201, up.text
    r = await client.post(f"/api/v1/documents/{up.json()['id']}/process")
    assert r.status_code == 200, r.text
    detail = r.json()
    if finalize:
        done = await client.post(
            f"/api/v1/documents/{detail['id']}/confirm", json=confirm_body(detail)
        )
        assert done.status_code == 200, done.text
        detail = done.json()
    return detail


OTHER_TEXT = "Chest X-ray report\nPatient Name: A B\nDate: 03-01-2025"


@pytest.fixture
async def library(client, fake_ocr, patient_user) -> dict[str, dict]:
    """Five documents of the signed-in patient, one of each interesting kind."""
    docs = {
        "lab": await seed_document(
            client,
            fake_ocr,
            name="Lab-Results-June.png",
            doc_type="lab_report",
            text=LAB_TEXT,
            finalize=True,
        ),
        "xray": await seed_document(
            client,
            fake_ocr,
            name="xray-chest.png",
            doc_type="other",
            text=OTHER_TEXT,
            finalize=True,
        ),
        "rx": await seed_document(client, fake_ocr, name="prescription-may.png", finalize=True),
        "review": await seed_document(
            client, fake_ocr, name="scan-007.png", doc_type="lab_report", text=LAB_TEXT
        ),
    }
    fake_ocr.error = ocr.OCR_EMPTY
    up = await upload(client, "prescription", name="failed-one.png")
    failed = (await client.post(f"/api/v1/documents/{up.json()['id']}/process")).json()
    fake_ocr.error = None
    assert failed["status"] == "failed"
    docs["failed"] = failed
    return docs


def names(response) -> list[str]:
    assert response.status_code == 200, response.text
    return sorted(d["original_filename"] for d in response.json())


# ── Search and filters ───────────────────────────────────────────────────
async def test_search_finds_documents_by_file_name_whatever_the_case(client, library):
    assert names(await client.get("/api/v1/documents?q=lab-results")) == ["Lab-Results-June.png"]
    assert names(await client.get("/api/v1/documents?q=RESULTS")) == ["Lab-Results-June.png"]
    assert names(await client.get("/api/v1/documents/in-progress?q=SCAN")) == ["scan-007.png"]
    assert names(await client.get("/api/v1/documents?q=nothing-like-this")) == []


async def test_search_treats_percent_and_underscore_as_ordinary_characters(client, library):
    for wildcard in ("%", "_", "%%", "_.png"):
        assert names(await client.get("/api/v1/documents", params={"q": wildcard})) == [], wildcard


async def test_search_matches_a_document_types_name_in_either_language(client, library):
    # "scan-007.png" has no "lab" in its name — it is found because it IS a lab report
    assert names(await client.get("/api/v1/documents/in-progress?q=lab")) == ["scan-007.png"]
    assert names(await client.get("/api/v1/documents?q=lab")) == ["Lab-Results-June.png"]
    urdu = translate("documents.type.lab_report", "ur")
    assert names(await client.get("/api/v1/documents/in-progress", params={"q": urdu})) == [
        "scan-007.png"
    ]
    # two letters would match every type: only the file name counts ("ot" is in "Other")
    assert names(await client.get("/api/v1/documents?q=ot")) == []
    assert names(await client.get("/api/v1/documents?q=oth")) == ["xray-chest.png"]


async def test_the_type_status_and_date_filters_combine(client, library):
    assert names(await client.get("/api/v1/documents?type=other")) == ["xray-chest.png"]
    assert names(await client.get("/api/v1/documents/in-progress?status=failed")) == [
        "failed-one.png"
    ]
    assert names(await client.get("/api/v1/documents/in-progress?status=awaiting_review")) == [
        "scan-007.png"
    ]
    assert names(
        await client.get("/api/v1/documents/in-progress?type=prescription&status=failed")
    ) == ["failed-one.png"]
    assert names(await client.get("/api/v1/documents/in-progress?type=other&status=failed")) == []
    # the document's own date; both ends included
    assert names(await client.get("/api/v1/documents?date_from=2024-08-01")) == [
        "prescription-may.png",
        "xray-chest.png",
    ]
    assert names(await client.get("/api/v1/documents?date_to=2024-08-11")) == [
        "Lab-Results-June.png",
        "prescription-may.png",
    ]
    assert names(await client.get("/api/v1/documents?date_from=2024-08-11&date_to=2024-08-11")) == [
        "prescription-may.png"
    ]
    # all of them at once
    both = "q=pres&type=prescription&date_from=2024-01-01&date_to=2024-12-31"
    assert names(await client.get(f"/api/v1/documents?{both}")) == ["prescription-may.png"]
    assert names(await client.get("/api/v1/documents?q=pres&type=other")) == []


async def test_a_document_with_no_date_of_its_own_is_dated_by_its_upload_day(client, library):
    today = pakistan_day(utcnow())
    # the failed upload has no date of its own: it is dated the day it was uploaded
    assert names(await client.get(f"/api/v1/documents/in-progress?date_from={today}")) == [
        "failed-one.png"
    ]
    # the one under review has the date its report states, not the day it was uploaded
    on_report = "date_from=2024-07-28&date_to=2024-07-28"
    assert names(await client.get(f"/api/v1/documents/in-progress?{on_report}")) == ["scan-007.png"]
    assert names(await client.get("/api/v1/documents/in-progress?date_to=2000-01-01")) == []


async def test_a_status_filter_never_mixes_saved_records_with_unsaved_uploads(client, library):
    assert names(await client.get("/api/v1/documents/in-progress?status=finalized")) == []
    # the records list is finalized by definition
    assert len(names(await client.get("/api/v1/documents"))) == 3


@pytest.mark.parametrize(
    ("query", "field"),
    [
        ("status=cancelled", "status"),
        ("status=bogus", "status"),
        ("date_from=2024-13-45", "date_from"),
        ("date_from=24/01/01", "date_from"),
        ("date_to=yesterday", "date_to"),
        ("date_from=1800-01-01", "date_from"),
        ("date_from=2025-02-01&date_to=2025-01-01", "date_to"),
    ],
)
async def test_an_invalid_filter_is_reported_not_guessed_at(client, library, query, field):
    r = await client.get(f"/api/v1/documents/in-progress?{query}")
    assert r.status_code == 422, r.text
    assert field in r.json()["error"]["details"]["fields"]


async def test_an_unknown_type_and_a_very_long_search_are_handled(client, library):
    assert (await client.get("/api/v1/documents?type=bogus")).status_code == 422
    r = await client.get("/api/v1/documents", params={"q": "x" * 5000})
    assert r.status_code == 200 and r.json() == []


async def test_search_and_filters_only_ever_see_the_patients_own_documents(
    app, client, library, fake_ocr, second_patient
):
    async with new_client(app) as other:
        await login(other, "other@x.com")
        fake_ocr.text = LAB_TEXT
        up = await upload(other, "lab_report", name="Lab-Results-June.png")  # the same name
        await other.post(f"/api/v1/documents/{up.json()['id']}/process")
        mine = names(await client.get("/api/v1/documents/in-progress?q=lab-results"))
        theirs = names(await other.get("/api/v1/documents/in-progress?q=lab-results"))
        assert mine == [] and theirs == ["Lab-Results-June.png"]  # mine is finalized, theirs not
        assert names(await other.get("/api/v1/documents?q=xray")) == []
        assert names(await other.get("/api/v1/documents/in-progress?status=failed")) == []
    assert names(await client.get("/api/v1/documents?q=lab-results")) == ["Lab-Results-June.png"]


async def test_the_list_page_searches_filters_and_resets(client, library):
    page = (await client.get("/en/patient/documents")).text
    assert "Clear filters" not in page  # nothing is filtered yet
    for name in ("Lab-Results-June.png", "xray-chest.png", "scan-007.png", "failed-one.png"):
        assert name in page, name

    page = (await client.get("/en/patient/documents?q=xray")).text
    assert "xray-chest.png" in page and "Lab-Results-June.png" not in page
    assert "Matching documents: 1" in page and "Clear filters" in page
    assert 'href="/en/patient/documents"' in page  # the way back to everything

    # the type tabs keep the search; the search keeps the type
    page = (await client.get("/en/patient/documents?q=lab&type=lab_report")).text
    assert "q=lab&amp;type=prescription" in page
    assert 'name="type" value="lab_report"' in page


async def test_the_list_page_says_so_when_nothing_matches_and_when_a_filter_is_invalid(
    client, library
):
    page = (await client.get("/en/patient/documents?q=zzzz")).text
    assert "No documents match your search." in page and "Clear filters" in page
    assert "Matching documents: 0" in page
    assert "No saved documents yet." not in page

    page = (await client.get("/en/patient/documents?status=bogus&date_from=nope&type=nope")).text
    assert "Some filters were not valid and were left out" in page
    assert "That status isn&#39;t recognised." in page and "Dates must look like" in page
    assert "That document type isn&#39;t recognised." in page
    assert "Lab-Results-June.png" in page  # an invalid filter narrows nothing

    page = (await client.get("/en/patient/documents?date_from=2025-02-01&date_to=2025-01-01")).text
    assert "must not be after" in page and "Lab-Results-June.png" in page


async def test_a_status_filter_on_the_page_shows_only_that_kind_of_document(client, library):
    page = (await client.get("/en/patient/documents?status=failed")).text
    assert "failed-one.png" in page and "scan-007.png" not in page
    assert (
        "Your saved records" not in page
    )  # nothing saved matches: the section stays out of the way
    page = (await client.get("/en/patient/documents?status=finalized")).text
    assert "Lab-Results-June.png" in page and "failed-one.png" not in page
    assert "Awaiting your review" not in page


async def test_a_patient_with_no_documents_sees_the_plain_empty_state(client, patient_user):
    page = (await client.get("/en/patient/documents")).text
    assert "No saved documents yet." in page and "No documents match" not in page


async def test_the_list_follows_a_document_that_is_being_read(client, db, library):
    assert "data-autorefresh" not in (await client.get("/en/patient/documents")).text
    await db.execute(
        update(Document)
        .where(Document.id == uuid.UUID(library["review"]["id"]))
        .values(status="processing")
    )
    await db.commit()
    page = (await client.get("/en/patient/documents?q=scan")).text
    assert 'data-autorefresh="5000"' in page  # a reload keeps the search: it is in the address
    assert "Reading…" in page


# ── View and download the original ───────────────────────────────────────
async def test_the_original_can_be_downloaded_exactly_as_uploaded(
    client, fake_ocr, fake_supabase, patient_user
):
    fake_ocr.text = LAB_TEXT
    up = await upload(client, "lab_report", name="report.pdf", data=PDF, mime="application/pdf")
    doc_id = up.json()["id"]
    await client.post(f"/api/v1/documents/{doc_id}/process")
    stored = next(iter(fake_supabase.storage.objects.values()))

    shown = await client.get(f"/api/v1/documents/{doc_id}/file")
    saved = await client.get(f"/api/v1/documents/{doc_id}/file?download=true")
    assert shown.content == saved.content == stored == PDF  # never re-encoded, never OCR output
    assert shown.headers["content-disposition"].startswith("inline;")
    assert saved.headers["content-disposition"].startswith("attachment;")
    assert 'filename="report.pdf"' in saved.headers["content-disposition"]
    assert saved.headers["content-type"] == "application/pdf"
    assert "no-store" in saved.headers["cache-control"]  # private, never cached


async def test_the_page_opens_and_downloads_the_same_original(client, fake_ocr, patient_user):
    detail = await upload_and_process(client, fake_ocr)
    base = f"/en/patient/documents/{detail['id']}/file"
    shown, saved = await client.get(base), await client.get(f"{base}?download=1")
    assert shown.content == saved.content == PNG
    assert shown.headers["content-disposition"].startswith("inline;")
    assert saved.headers["content-disposition"].startswith("attachment;")


async def test_a_download_is_offered_wherever_the_original_is_still_held(
    client, library, patient_user
):
    page = (await client.get(f"/en/patient/documents/{library['lab']['id']}")).text
    assert f"/documents/{library['lab']['id']}/file?download=1" in page
    assert ">Open original<" in page and ">Download<" in page
    page = (await client.get("/en/patient/documents")).text
    assert page.count("/file?download=1") == 5  # one for every document on the list


async def test_another_patient_can_neither_open_nor_download_the_original(
    app, client, library, second_patient
):
    doc_id = library["lab"]["id"]
    async with new_client(app) as other:
        await login(other, "other@x.com")
        missing = uuid.uuid4()
        api = [
            await other.get(f"/api/v1/documents/{doc_id}/file"),
            await other.get(f"/api/v1/documents/{doc_id}/file?download=true"),
        ]
        web = [
            await other.get(f"/en/patient/documents/{doc_id}/file"),
            await other.get(f"/en/patient/documents/{doc_id}/file?download=1"),
        ]
        unknown = await other.get(f"/en/patient/documents/{missing}/file?download=1")
    assert [r.status_code for r in api + web] == [404] * 4
    for r in web:
        # the same answer as for an id that does not exist, and nothing of the document in it
        assert unknown.status_code == 404 and "couldn&#39;t find that file" in r.text
        assert "Lab-Results-June.png" not in r.text


async def test_a_missing_original_is_a_clear_not_found(
    client, fake_ocr, fake_supabase, patient_user
):
    detail = await upload_and_process(client, fake_ocr)
    fake_supabase.storage.objects.clear()  # the file is gone from the store
    api = await client.get(f"/api/v1/documents/{detail['id']}/file")
    assert api.status_code == 404 and api.json()["error"]["code"] == "NOT_FOUND"
    web = await client.get(f"/en/patient/documents/{detail['id']}/file?download=1")
    assert web.status_code == 404 and "We couldn&#39;t find that file." in web.text


async def test_an_unreachable_store_is_a_retryable_error_that_says_nothing_internal(
    client, fake_ocr, fake_supabase, patient_user
):
    detail = await upload_and_process(client, fake_ocr)
    fake_supabase.storage.fail_get = True
    api = await client.get(f"/api/v1/documents/{detail['id']}/file")
    assert api.status_code == 502 and api.json()["error"]["code"] == "FILE_UNAVAILABLE"
    web = await client.get(f"/en/patient/documents/{detail['id']}/file")
    assert web.status_code == 502 and "can&#39;t reach your stored files" in web.text
    for body in (api.text, web.text):
        assert "storage unavailable" not in body and "Traceback" not in body
        assert "RuntimeError" not in body
    fake_supabase.storage.fail_get = False  # and it works again once the store does
    assert (await client.get(f"/api/v1/documents/{detail['id']}/file")).content == PNG


async def test_a_deleted_documents_original_is_no_longer_served(client, fake_ocr, patient_user):
    detail = await upload_and_process(client, fake_ocr)
    assert (await client.delete(f"/api/v1/documents/{detail['id']}")).status_code == 200
    assert (await client.get(f"/api/v1/documents/{detail['id']}/file")).status_code == 404
    assert (await client.get(f"/en/patient/documents/{detail['id']}/file")).status_code == 404


def test_the_stores_errors_are_told_apart():
    from app.documents import storage as store
    from storage3.exceptions import StorageApiError

    assert store.is_missing(KeyError("k")) and store.is_missing(FileNotFoundError())
    assert store.is_missing(StorageApiError("Object not found", "not_found", 404))
    assert store.is_missing(StorageApiError("Object not found", "NoSuchKey", 400))
    assert not store.is_missing(StorageApiError("Internal error", "InternalError", 500))
    assert not store.is_missing(RuntimeError("storage unavailable"))
    assert not store.is_missing(TimeoutError())


# ── Delete ───────────────────────────────────────────────────────────────
async def delete_audit(db, doc_id) -> list[AuditLog]:
    stmt = select(AuditLog).where(AuditLog.action == "document.delete")
    rows = (await db.execute(stmt.execution_options(populate_existing=True))).scalars().all()
    return [r for r in rows if json.loads(r.detail).get("document_id") == str(doc_id)]


async def test_an_unconfirmed_upload_is_deleted_with_its_file_and_everything_read_from_it(
    client, db, fake_ocr, fake_supabase, patient_user
):
    detail = await upload_and_process(client, fake_ocr)
    doc_id = detail["id"]
    job = await job_row(db, doc_id)
    job.ai_sections = {"version": 1, "facility": {"hospital_name": "City Clinic"}}
    await db.commit()
    assert fake_supabase.storage.objects and await extraction_rows(db, doc_id)

    r = await client.delete(f"/api/v1/documents/{doc_id}")
    assert r.status_code == 200
    assert (r.json()["status"], r.json()["can_delete"], r.json()["can_download"]) == (
        "cancelled",
        False,
        False,
    )

    row = await doc_row(db, doc_id)
    assert (row.status, row.ocr_text, row.original_filename, row.document_date) == (
        "cancelled",
        None,
        None,
        None,
    )
    assert fake_supabase.storage.objects == {}  # no file left behind
    assert await extraction_rows(db, doc_id) == []
    assert (await job_row(db, doc_id)).ai_sections is None  # the model's reading is clinical text
    (entry,) = await delete_audit(db, doc_id)
    assert json.loads(entry.detail) == {
        "document_id": doc_id,
        "doc_type": "rx",
        "previous_status": "awaiting_review",
    }  # ids and a status — never the name, the text or a value
    assert entry.actor_user_id == patient_user.id
    # gone from every list, still not a record
    assert (await client.get("/api/v1/documents/in-progress")).json() == []
    assert (await client.get("/api/v1/documents")).json() == []


@pytest.mark.parametrize("state", ["uploaded", "failed"])
async def test_an_upload_that_was_never_read_or_could_not_be_read_can_be_deleted_too(
    client, db, fake_ocr, fake_supabase, patient_user, state
):
    if state == "failed":
        fake_ocr.error = ocr.OCR_EMPTY
    up = await upload(client)
    doc_id = up.json()["id"]
    if state == "failed":
        await client.post(f"/api/v1/documents/{doc_id}/process")
        assert (await doc_row(db, doc_id)).status == "failed"
    r = await client.delete(f"/api/v1/documents/{doc_id}")
    assert r.status_code == 200 and (await doc_row(db, doc_id)).status == "cancelled"
    assert fake_supabase.storage.objects == {}


async def test_a_saved_record_is_deleted_only_when_the_patient_confirms(
    client, db, fake_ocr, fake_supabase, patient_user
):
    detail = await seed_document(client, fake_ocr, name="keep-me.png", finalize=True)
    doc_id = detail["id"]
    assert detail["can_delete"] is True

    no = await client.delete(f"/api/v1/documents/{doc_id}")
    assert no.status_code == 422 and "confirm" in no.json()["error"]["details"]["fields"]
    no = await client.delete(f"/api/v1/documents/{doc_id}?confirm=false")
    assert no.status_code == 422
    row = await doc_row(db, doc_id)
    assert row.status == "finalized" and row.original_filename == "keep-me.png"
    assert fake_supabase.storage.objects and await extraction_rows(db, doc_id)
    assert await delete_audit(db, doc_id) == []

    yes = await client.delete(f"/api/v1/documents/{doc_id}?confirm=true")
    assert yes.status_code == 200 and yes.json()["status"] == "cancelled"
    row = await doc_row(db, doc_id)
    assert (row.status, row.ocr_text, row.original_filename) == ("cancelled", None, None)
    assert row.finalized_at is not None  # the tombstone still says it was once a saved record
    assert fake_supabase.storage.objects == {} and await extraction_rows(db, doc_id) == []
    (entry,) = await delete_audit(db, doc_id)
    assert json.loads(entry.detail)["previous_status"] == "finalized"
    assert (await client.get("/api/v1/documents")).json() == []


async def test_deleting_a_record_never_removes_what_it_added_to_the_profile(
    client, db, fake_ocr, fake_gemini, patient_user
):
    detail = await upload_and_process(client, fake_ocr, "prescription", ALLERGIC_RX)
    done = await client.post(
        f"/api/v1/documents/{detail['id']}/confirm", json=confirm_everything(detail)
    )
    assert done.status_code == 200
    before = [a.substance for a in await profile_allergies(db, patient_user)]
    assert before == ["Penicillin", "Sulfa drugs"]

    r = await client.delete(f"/api/v1/documents/{detail['id']}?confirm=true")
    assert r.status_code == 200
    assert [a.substance for a in await profile_allergies(db, patient_user)] == before


async def test_deleting_one_document_leaves_the_others_alone(client, db, library, fake_supabase):
    r = await client.delete(f"/api/v1/documents/{library['lab']['id']}?confirm=true")
    assert r.status_code == 200
    assert names(await client.get("/api/v1/documents")) == [
        "prescription-may.png",
        "xray-chest.png",
    ]
    assert names(await client.get("/api/v1/documents/in-progress")) == [
        "failed-one.png",
        "scan-007.png",
    ]
    assert len(fake_supabase.storage.objects) == 4
    for kept in ("rx", "xray", "review", "failed"):
        assert (await doc_row(db, library[kept]["id"])).status != "cancelled"


async def test_deleting_twice_is_harmless(client, db, fake_ocr, patient_user):
    detail = await upload_and_process(client, fake_ocr)
    first = await client.delete(f"/api/v1/documents/{detail['id']}")
    second = await client.delete(f"/api/v1/documents/{detail['id']}")
    assert (first.status_code, second.status_code) == (200, 200)
    assert len(await delete_audit(db, detail["id"])) == 1  # one deletion, one entry


async def test_a_clinicians_record_cannot_be_deleted_by_the_patient(
    app, client, db, fake_supabase, patient_user, verified_doctor
):
    pid = await _patient_id_for(db, patient_user)
    made = await create_doctor_prescription(
        app, [{"medicine": "Metformin", "dosage": "500 mg"}], patient_id=pid
    )
    assert made.status_code == 201, made.text
    doc_id = made.json()["id"]
    detail = (await client.get(f"/api/v1/documents/{doc_id}")).json()
    assert detail["source"] == "doctor_generated" and detail["can_delete"] is False

    for url in (
        f"/api/v1/documents/{doc_id}?confirm=true",
        f"/api/v1/documents/{doc_id}",
    ):
        r = await client.delete(url)
        assert r.status_code == 403 and r.json()["error"]["code"] == "DOCUMENT_NOT_DELETABLE"
    page = await client.get(f"/en/patient/documents/{doc_id}/delete")
    assert page.status_code == 403 and "can&#39;t be deleted from your account" in page.text
    post = await client.post(f"/en/patient/documents/{doc_id}/delete", data={"confirm": "1"})
    assert post.status_code == 403
    assert (
        "data-delete-url"
        not in (await client.get("/en/patient/documents")).text.split("<dialog")[0]
    )  # no Delete control on its row either
    row = await doc_row(db, doc_id)
    assert row.status == "finalized" and fake_supabase.storage.objects
    assert await delete_audit(db, doc_id) == []


async def test_a_deployment_can_keep_every_saved_record(
    client, db, fake_ocr, patient_user, monkeypatch
):
    saved = await seed_document(client, fake_ocr, name="a.png", finalize=True)
    unsaved = await seed_document(client, fake_ocr, name="b.png")
    monkeypatch.setattr(settings, "document_allow_delete_finalized", False)
    assert (await client.get(f"/api/v1/documents/{saved['id']}")).json()["can_delete"] is False
    r = await client.delete(f"/api/v1/documents/{saved['id']}?confirm=true")
    assert r.status_code == 403 and (await doc_row(db, saved["id"])).status == "finalized"
    # an upload that was never saved is not a record: it can still go
    assert (await client.delete(f"/api/v1/documents/{unsaved['id']}")).status_code == 200


async def test_another_patient_cannot_delete_my_document(
    app, client, db, fake_supabase, library, second_patient
):
    doc_id = library["lab"]["id"]
    async with new_client(app) as other:
        await login(other, "other@x.com")
        responses = [
            await other.delete(f"/api/v1/documents/{doc_id}?confirm=true"),
            await other.delete(f"/api/v1/documents/{uuid.uuid4()}?confirm=true"),
            await other.get(f"/en/patient/documents/{doc_id}/delete"),
            await other.post(f"/en/patient/documents/{doc_id}/delete", data={"confirm": "1"}),
        ]
    assert [r.status_code for r in responses] == [404] * 4
    a, b = (r.json()["error"] for r in responses[:2])
    assert {k: a[k] for k in ("code", "message", "details")} == {
        k: b[k] for k in ("code", "message", "details")
    }
    assert (await doc_row(db, doc_id)).status == "finalized"
    assert len(fake_supabase.storage.objects) == 5
    assert await delete_audit(db, doc_id) == []


async def test_a_file_that_cannot_be_removed_means_nothing_was_deleted_and_it_can_be_retried(
    client, db, fake_ocr, fake_supabase, patient_user, monkeypatch
):
    detail = await seed_document(client, fake_ocr, name="stuck.png", finalize=True)
    doc_id = detail["id"]
    real_remove = doc_storage.remove

    async def store_down(key):
        raise RuntimeError("storage unavailable: secret-internal-detail")

    monkeypatch.setattr(doc_storage, "remove", store_down)
    r = await client.delete(f"/api/v1/documents/{doc_id}?confirm=true")
    assert r.status_code == 502 and r.json()["error"]["code"] == "DELETE_FAILED"
    assert "secret-internal-detail" not in r.text  # nothing internal reaches the patient
    row = await doc_row(db, doc_id)
    assert (row.status, row.original_filename) == ("finalized", "stuck.png")  # untouched
    assert row.ocr_text is not None and await extraction_rows(db, doc_id)
    assert fake_supabase.storage.objects  # the file is still there
    assert await delete_audit(db, doc_id) == []  # a deletion that did not happen is not logged
    assert len((await client.get("/api/v1/documents")).json()) == 1  # still a record

    monkeypatch.setattr(doc_storage, "remove", real_remove)  # the store comes back
    again = await client.delete(f"/api/v1/documents/{doc_id}?confirm=true")
    assert again.status_code == 200 and (await doc_row(db, doc_id)).status == "cancelled"
    assert fake_supabase.storage.objects == {} and len(await delete_audit(db, doc_id)) == 1


async def test_a_file_already_gone_from_the_store_does_not_stop_a_deletion(
    client, db, fake_ocr, fake_supabase, patient_user
):
    detail = await seed_document(client, fake_ocr, name="lost.png")
    fake_supabase.storage.objects.clear()
    r = await client.delete(f"/api/v1/documents/{detail['id']}")
    assert r.status_code == 200 and (await doc_row(db, detail["id"])).status == "cancelled"


async def test_a_document_saved_while_it_was_being_deleted_is_not_deleted(
    client, db, sessionmaker_, fake_ocr, fake_supabase, patient_user, monkeypatch
):
    detail = await upload_and_process(client, fake_ocr)
    doc_id = detail["id"]
    real_owned = doc_service._owned

    async def finalized_meanwhile(session, actor, document_id):
        doc = await real_owned(session, actor, document_id)  # still awaiting review as read...
        async with sessionmaker_() as other:  # ...but confirmed by another request just now
            await other.execute(
                update(Document).where(Document.id == document_id).values(status="finalized")
            )
            await other.commit()
        return doc

    monkeypatch.setattr(doc_service, "_owned", finalized_meanwhile)
    r = await client.delete(f"/api/v1/documents/{doc_id}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "INVALID_DOCUMENT_STATE"
    assert (await doc_row(db, doc_id)).status == "finalized"
    assert fake_supabase.storage.objects  # the claim comes first: the file was never touched
    assert await extraction_rows(db, doc_id) and await delete_audit(db, doc_id) == []


async def test_a_malformed_document_id_is_rejected(client, patient_user):
    assert (await client.delete("/api/v1/documents/not-a-uuid")).status_code == 422
    redirect = await client.get("/en/patient/documents/not-a-uuid/delete")
    assert redirect.status_code == 303 and redirect.headers["location"] == "/en/patient/documents"


# ── Delete: the pages ────────────────────────────────────────────────────
async def test_the_list_offers_delete_only_where_it_is_allowed_and_one_dialog(client, library):
    page = (await client.get("/en/patient/documents")).text
    assert page.count('data-delete-url="') == 5
    assert page.count('<dialog id="delete-dialog"') == 1
    assert 'data-delete-saved="1"' in page and 'data-delete-saved="0"' in page
    assert f'href="/en/patient/documents/{library["lab"]["id"]}/delete"' in page  # works without JS


async def test_the_confirmation_page_spells_out_what_goes_and_what_stays(client, library):
    saved = (await client.get(f"/en/patient/documents/{library['lab']['id']}/delete")).text
    assert "Delete this document?" in saved and "Lab-Results-June.png" in saved
    assert "stays in your profile" in saved and "can&#39;t be undone" in saved
    assert 'type="checkbox" name="confirm"' in saved  # a saved record needs the box ticked
    unsaved = (await client.get(f"/en/patient/documents/{library['review']['id']}/delete")).text
    assert "hasn&#39;t been saved to your record yet" in unsaved
    assert 'type="hidden" name="confirm"' in unsaved and 'type="checkbox"' not in unsaved


async def test_a_saved_record_is_deleted_from_the_page_only_with_the_box_ticked(
    client, db, library, fake_supabase
):
    doc_id = library["lab"]["id"]
    refused = await client.post(f"/en/patient/documents/{doc_id}/delete", data={})
    assert refused.status_code == 422 and "Tick the box to confirm" in refused.text
    assert (await doc_row(db, doc_id)).status == "finalized"

    done = await client.post(f"/en/patient/documents/{doc_id}/delete", data={"confirm": "1"})
    assert done.status_code == 303
    assert done.headers["location"] == "/en/patient/documents?saved=deleted"
    page = (await client.get(done.headers["location"])).text
    assert "The document was deleted." in page and "Lab-Results-June.png" not in page
    assert (await doc_row(db, doc_id)).status == "cancelled"
    assert len(fake_supabase.storage.objects) == 4


async def test_an_unsaved_upload_is_deleted_from_the_page_in_one_confirmed_step(
    client, db, library
):
    doc_id = library["review"]["id"]
    done = await client.post(f"/en/patient/documents/{doc_id}/delete", data={"confirm": "1"})
    assert done.status_code == 303 and (await doc_row(db, doc_id)).status == "cancelled"
    # without the confirming field the server does nothing for a saved record
    assert (await doc_row(db, library["rx"]["id"])).status == "finalized"


async def test_a_failed_deletion_is_never_shown_as_a_success(
    client, db, library, fake_supabase, monkeypatch
):
    async def store_down(key):
        raise RuntimeError("down")

    monkeypatch.setattr(doc_storage, "remove", store_down)
    doc_id = library["lab"]["id"]
    r = await client.post(f"/en/patient/documents/{doc_id}/delete", data={"confirm": "1"})
    assert r.status_code == 502
    assert "has not been deleted" in r.text and "The document was deleted." not in r.text
    assert "Delete permanently" in r.text  # the same step is offered again
    assert (await doc_row(db, doc_id)).status == "finalized"
    assert len(fake_supabase.storage.objects) == 5


async def test_a_deleted_document_is_gone_from_its_own_pages(client, library):
    doc_id = library["xray"]["id"]
    await client.post(f"/en/patient/documents/{doc_id}/delete", data={"confirm": "1"})
    detail = await client.get(f"/en/patient/documents/{doc_id}")
    again = await client.get(f"/en/patient/documents/{doc_id}/delete")
    # a repeat of the deletion has nothing left to ask
    assert again.status_code == 303 and again.headers["location"].endswith("saved=deleted")
    assert "xray-chest.png" not in (await client.get("/en/patient/documents")).text
    assert detail.status_code == 200 and "Download" not in detail.text  # nothing to download


async def test_the_delete_pages_render_in_urdu_right_to_left(client, library):
    page = await client.get(f"/ur/patient/documents/{library['lab']['id']}/delete")
    assert page.status_code == 200 and 'dir="rtl"' in page.text
    for key in (
        "documents.delete.title",
        "documents.action.delete_confirm",
        "documents.action.keep",
    ):
        assert translate(key, "ur") in page.text, key
    listing = await client.get("/ur/patient/documents?q=zzzz")
    for key in ("documents.empty.filtered", "documents.filter.reset", "documents.filter.apply"):
        assert translate(key, "ur") in listing.text, key


# ── Retry ────────────────────────────────────────────────────────────────
async def test_only_a_failed_document_can_be_retried(client, db, fake_ocr, patient_user):
    fake_ocr.error = ocr.OCR_EMPTY
    failed = await upload(client)
    await client.post(f"/api/v1/documents/{failed.json()['id']}/process")
    fake_ocr.error = None
    uploaded = await upload(client)  # stored, not yet read
    review = await seed_document(client, fake_ocr, name="r.png")
    final = await seed_document(client, fake_ocr, name="f.png", finalize=True)
    processing = await upload(client)
    await db.execute(
        update(Document)
        .where(Document.id == uuid.UUID(processing.json()["id"]))
        .values(status="processing")
    )
    await db.commit()

    async def flags(doc_id):
        body = (await client.get(f"/api/v1/documents/{doc_id}")).json()
        return body["status"], body["can_retry"]

    assert await flags(failed.json()["id"]) == ("failed", True)
    assert await flags(uploaded.json()["id"]) == ("uploaded", False)
    assert await flags(review["id"]) == ("awaiting_review", False)
    assert await flags(final["id"]) == ("finalized", False)
    assert await flags(processing.json()["id"]) == ("processing", False)
    page = (await client.get("/en/patient/documents")).text
    assert page.count("/process") == 1 and page.count(">Try again<") == 1  # the failed one alone


async def test_a_retry_reads_the_same_original_again_without_piling_up_results(
    client, db, fake_ocr, fake_supabase, patient_user
):
    fake_ocr.error = ocr.OCR_EMPTY
    up = await upload(client, name="blurry.png")
    doc_id = up.json()["id"]
    first = await client.post(f"/api/v1/documents/{doc_id}/process")
    assert first.json()["status"] == "failed" and first.json()["failure_code"] == "ocr_empty"
    before = await doc_row(db, doc_id)
    key = before.object_key

    fake_ocr.error, fake_ocr.text = None, RX_TEXT
    again = await client.post(f"/api/v1/documents/{doc_id}/process")
    assert again.status_code == 200 and again.json()["status"] == "awaiting_review"
    assert again.json()["failure_code"] is None and again.json()["can_retry"] is False

    row = await doc_row(db, doc_id)
    assert (row.object_key, row.original_filename, row.ocr_mode) == (key, "blurry.png", "printed")
    assert fake_supabase.storage.objects[(settings.document_bucket, key)] == PNG  # the original
    assert fake_ocr.calls[-1][0] == PNG  # ...is what was read again
    job = await job_row(db, doc_id)
    assert job.retry_count == 1
    cells = await extraction_rows(db, doc_id)
    assert len({(c.row_index, c.field_name) for c in cells}) == len(cells)  # no duplicates
    assert len([c for c in cells if c.field_name == "medicine"]) == 2


async def test_retrying_again_while_the_first_retry_runs_starts_nothing_twice(
    client, db, fake_ocr, patient_user
):
    fake_ocr.error = ocr.OCR_EMPTY
    up = await upload(client)
    doc_id = up.json()["id"]
    await client.post(f"/api/v1/documents/{doc_id}/process")
    fake_ocr.error, fake_ocr.text = None, RX_TEXT
    calls_before = len(fake_ocr.calls)

    second: dict = {}

    async def second_click_meanwhile():
        r = await client.post(f"/api/v1/documents/{doc_id}/process")
        second["status"], second["code"] = r.status_code, r.json()["error"]["code"]

    fake_ocr.hook = second_click_meanwhile
    first = await client.post(f"/api/v1/documents/{doc_id}/process")
    assert first.status_code == 200 and first.json()["status"] == "awaiting_review"
    assert (second["status"], second["code"]) == (409, "PROCESSING_IN_PROGRESS")
    assert len(fake_ocr.calls) == calls_before + 1  # read once, not twice


async def test_a_saved_record_can_never_be_read_again_or_have_its_values_replaced(
    client, db, fake_ocr, patient_user
):
    detail = await seed_document(client, fake_ocr, name="final.png", finalize=True)
    doc_id = detail["id"]
    before = [
        (c.row_index, c.field_name, c.extracted_value, c.corrected_value, c.reviewed_at)
        for c in await extraction_rows(db, doc_id)
    ]
    calls = len(fake_ocr.calls)
    fake_ocr.text = "Dr. Someone Else\n1. Tab. Different 10 mg"

    api = await client.post(f"/api/v1/documents/{doc_id}/process")
    assert api.status_code == 409 and api.json()["error"]["code"] == "INVALID_DOCUMENT_STATE"
    web = await client.post(f"/en/patient/documents/{doc_id}/process")
    assert web.status_code == 303  # back to the page, which shows the record as it is
    assert len(fake_ocr.calls) == calls  # the reader was never asked
    after = [
        (c.row_index, c.field_name, c.extracted_value, c.corrected_value, c.reviewed_at)
        for c in await extraction_rows(db, doc_id)
    ]
    assert after == before
    # the medicines the patient confirmed are still marked as human-verified
    assert all(item[4] is not None for item in before if item[1] in groups.MEDICINES.fields)
    assert (await doc_row(db, doc_id)).status == "finalized"
    assert [d["id"] for d in (await client.get("/api/v1/documents")).json()] == [doc_id]


async def test_retrying_a_document_that_is_already_read_changes_nothing(
    client, db, fake_ocr, patient_user
):
    detail = await seed_document(client, fake_ocr, name="mine.png")
    calls = len(fake_ocr.calls)
    r = await client.post(f"/api/v1/documents/{detail['id']}/process")
    assert r.status_code == 200 and r.json()["status"] == "awaiting_review"
    assert len(fake_ocr.calls) == calls and (await job_row(db, detail["id"])).retry_count == 0


async def test_a_failed_document_is_retried_from_the_list_and_the_page_shows_the_outcome(
    client, db, fake_ocr, patient_user
):
    fake_ocr.error = ocr.OCR_EMPTY
    up = await upload(client, name="blurry.png")
    doc_id = up.json()["id"]
    await client.post(f"/api/v1/documents/{doc_id}/process")
    assert "blurry.png" in (await client.get("/en/patient/documents")).text

    fake_ocr.error, fake_ocr.text = None, RX_TEXT
    r = await client.post(f"/en/patient/documents/{doc_id}/process")
    assert r.status_code == 303 and r.headers["location"] == f"/en/patient/documents/{doc_id}"
    assert (await doc_row(db, doc_id)).status == "awaiting_review"
    assert ">Try again<" not in (await client.get("/en/patient/documents")).text


async def test_someone_elses_document_cannot_be_retried(
    app, client, db, library, fake_ocr, second_patient
):
    doc_id = library["failed"]["id"]
    calls = len(fake_ocr.calls)
    async with new_client(app) as other:
        await login(other, "other@x.com")
        api = await other.post(f"/api/v1/documents/{doc_id}/process")
        web = await other.post(f"/en/patient/documents/{doc_id}/process")
    assert api.status_code == 404 and web.status_code == 303  # the page just shows its own list
    assert len(fake_ocr.calls) == calls
    row = await doc_row(db, doc_id)
    assert (row.status, row.failure_code) == ("failed", "ocr_empty")


async def test_a_failure_with_no_message_of_its_own_gets_the_general_one(client, db, library):
    doc_id = uuid.UUID(library["failed"]["id"])
    await db.execute(
        update(Document).where(Document.id == doc_id).values(failure_code="weird_internal_code")
    )
    await db.commit()
    for url in ("/en/patient/documents", f"/en/patient/documents/{doc_id}"):
        page = (await client.get(url)).text
        assert "We couldn&#39;t read this document." in page, url
        assert "weird_internal_code" not in page and "errors.weird" not in page, url
    urdu = translate("errors.ocr_failed", "ur")
    assert urdu in (await client.get("/ur/patient/documents")).text


async def test_the_status_is_the_same_on_the_list_and_on_the_document(client, library):
    # (what the list says, what the document's own page says): the review step has its own
    # "needs verification" badge instead of the status chip
    expected = {
        "lab": ("Saved", "Saved"),
        "review": ("Needs your review", "Needs verification"),
        "failed": ("Couldn&#39;t read", "Couldn&#39;t read"),
    }
    listing = (await client.get("/en/patient/documents")).text
    for key, (on_list, on_page) in expected.items():
        detail = (await client.get(f"/en/patient/documents/{library[key]['id']}")).text
        assert on_list in listing and on_page in detail, key
    # and a retry control appears only on the one that can be retried
    failed_page = (await client.get(f"/en/patient/documents/{library['failed']['id']}")).text
    review_page = (await client.get(f"/en/patient/documents/{library['review']['id']}")).text
    assert ">Try again<" in failed_page and ">Try again<" not in review_page


async def test_deleting_shares_the_document_write_budget(
    client, db, fake_ocr, patient_user, monkeypatch
):
    a = await seed_document(client, fake_ocr, name="a.png")
    b = await seed_document(client, fake_ocr, name="b.png")
    c = await seed_document(client, fake_ocr, name="c.png")  # six writes so far
    monkeypatch.setattr(settings, "document_write_rate_limit_per_minute", 8)
    assert (await client.delete(f"/api/v1/documents/{a['id']}")).status_code == 200
    assert (await client.delete(f"/api/v1/documents/{b['id']}")).status_code == 200
    api = await client.delete(f"/api/v1/documents/{c['id']}")
    assert api.status_code == 429 and api.headers["retry-after"] == "60"
    web = await client.post(f"/en/patient/documents/{c['id']}/delete", data={"confirm": "1"})
    assert web.status_code == 429 and "Too many attempts" in web.text
    assert (await doc_row(db, c["id"])).status == "awaiting_review"  # nothing was deleted


async def test_a_search_string_is_shown_back_harmlessly(client, patient_user):
    page = (
        await client.get("/en/patient/documents", params={"q": '"><script>alert(1)</script>'})
    ).text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


# ═══ Photo quality: "please take a clearer picture" ══════════════════════
# A blurry, faint or tiny photo is read badly without any error — so the patient is told, on the
# review page and on the failed page, and offered a clearer upload. It is advice only: the
# values can still be reviewed, corrected and saved, and nothing about the read changes.
def sharp_png() -> bytes:
    return fx.image_bytes(fx.text_page())


def blurry_png() -> bytes:
    return fx.image_bytes(fx.text_page().filter(ImageFilter.GaussianBlur(4)))


async def upload_photo(client, fake_ocr, data: bytes, *, doc_type="prescription", text=RX_TEXT):
    fake_ocr.text = text
    up = await upload(client, doc_type, name="photo.png", data=data, mime="image/png")
    assert up.status_code == 201, up.text
    r = await client.post(f"/api/v1/documents/{up.json()['id']}/process")
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def assessments(monkeypatch) -> list[str]:
    """Every call to the photo-quality measure, by MIME type (the real measure still runs)."""
    calls: list[str] = []
    real = ocr.assess_quality

    async def counting(data: bytes, mime_type: str):
        calls.append(mime_type)
        return await real(data, mime_type)

    monkeypatch.setattr(ocr, "assess_quality", counting)
    return calls


async def test_a_blurry_photo_is_met_with_a_request_for_a_clearer_one(
    client, fake_ocr, patient_user
):
    detail = await upload_photo(
        client, fake_ocr, blurry_png(), doc_type="lab_report", text=LAB_TEXT
    )
    assert detail["status"] == "awaiting_review"
    assert detail["quality"]["issues"] == ["blurry"] and detail["quality"]["sharpness"] < 0.4

    page = (await client.get(f"/en/patient/documents/{detail['id']}")).text
    assert 'data-quality="blurry"' in page
    for expected in (
        "This photo may be hard to read",
        "The photo looks blurry.",
        "Hold the phone steady",
        "You can still review and correct the values below",
        "Upload a clearer photo",
        '/en/patient/documents/new?type=lab_report"',  # the same kind of document, pre-chosen
    ):
        assert expected in page, expected
    # advice, not a gate: the review form is all there and the record can still be saved
    assert 'name="row-0-test_name"' in page and 'id="review-form"' in page
    done = await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=confirm_body(detail))
    assert done.status_code == 200 and done.json()["status"] == "finalized"
    assert done.json()["quality"] is None  # a saved record is not judged


async def test_a_phone_photo_full_of_noise_and_blur_is_also_met_with_the_request(
    client, fake_ocr, patient_user
):
    fake_ocr.text = RX_TEXT
    up = await upload(
        client,
        "prescription",
        name="photo.jpg",
        data=fx.render_fixture_2_degraded_jpeg(),
        mime="image/jpeg",
    )
    detail = (await client.post(f"/api/v1/documents/{up.json()['id']}/process")).json()
    assert detail["quality"]["issues"] == ["blurry"]


async def test_a_clear_photo_gets_no_advice(client, fake_ocr, patient_user):
    detail = await upload_photo(client, fake_ocr, sharp_png())
    assert detail["quality"]["issues"] == [] and detail["quality"]["sharpness"] > 0.45
    page = (await client.get(f"/en/patient/documents/{detail['id']}")).text
    assert "data-quality" not in page and "hard to read" not in page
    assert "Upload a clearer photo" not in page


async def test_a_failed_read_of_a_blurry_photo_says_why_and_asks_for_a_clearer_one(
    client, fake_ocr, patient_user
):
    fake_ocr.error = ocr.OCR_EMPTY
    detail = await upload_photo(client, fake_ocr, blurry_png())
    assert detail["status"] == "failed" and detail["quality"]["issues"] == ["blurry"]
    page = (await client.get(f"/en/patient/documents/{detail['id']}")).text
    assert "No readable text was found" in page  # the failure, as before
    assert 'data-quality="blurry"' in page and "Hold the phone steady" in page
    assert "You can still review" not in page  # there is nothing to review on a failed read
    assert ">Try again<" in page  # and the document can still be read again

    # a clear photo that failed for another reason is not blamed on the photo
    other = await upload_photo(client, fake_ocr, sharp_png())
    page = (await client.get(f"/en/patient/documents/{other['id']}")).text
    assert "No readable text was found" in page and "data-quality" not in page


async def test_several_problems_are_each_named_with_their_own_advice(
    client, fake_ocr, patient_user
):
    tiny_and_soft = fx.image_bytes(fx.text_page(500, 700, 12).filter(ImageFilter.GaussianBlur(3)))
    detail = await upload_photo(client, fake_ocr, tiny_and_soft)
    assert detail["quality"]["issues"] == ["blurry", "low_resolution"]
    page = (await client.get(f"/en/patient/documents/{detail['id']}")).text
    assert 'data-quality="blurry low_resolution"' in page
    assert "The photo looks blurry." in page and "The image is small" in page
    assert "Take the photo at full quality" in page


async def test_the_request_for_a_clearer_photo_is_also_shown_in_urdu(
    client, fake_ocr, patient_user
):
    detail = await upload_photo(client, fake_ocr, blurry_png())
    page = (await client.get(f"/ur/patient/documents/{detail['id']}")).text
    for key in (
        "documents.quality.title",
        "documents.quality.issue.blurry",
        "documents.quality.tip.blurry",
        "documents.quality.action",
    ):
        assert translate(key, "ur") in page, key


async def test_the_quality_check_changes_nothing_about_what_is_read(client, fake_ocr, patient_user):
    sharp = await upload_photo(client, fake_ocr, sharp_png())
    blurry = await upload_photo(client, fake_ocr, blurry_png())

    def values(detail):
        return [[(f["name"], f["value"]) for f in row["fields"]] for row in detail["rows"]]

    assert values(sharp) == values(blurry) and values(sharp)
    assert [g["key"] for g in sharp["groups"]] == [g["key"] for g in blurry["groups"]]


async def test_a_photo_is_measured_once_and_the_verdict_kept_for_every_page_view(
    client, fake_ocr, patient_user, assessments
):
    detail = await upload_photo(client, fake_ocr, blurry_png())
    assert assessments == ["image/png"]
    for _ in range(2):
        await client.get(f"/api/v1/documents/{detail['id']}")
        assert (
            'data-quality="blurry"'
            in (await client.get(f"/en/patient/documents/{detail['id']}")).text
        )
    assert assessments == ["image/png"]  # still the one, taken while the document was read


async def test_a_lost_verdict_is_measured_again_from_the_stored_original(
    client, fake_ocr, patient_user, assessments
):
    detail = await upload_photo(client, fake_ocr, blurry_png())
    app_cache.cache.clear()  # a restart, or another worker
    page = (await client.get(f"/en/patient/documents/{detail['id']}")).text
    assert 'data-quality="blurry"' in page and len(assessments) == 2


async def test_a_file_that_cannot_be_measured_is_simply_not_judged(
    client, fake_ocr, fake_supabase, patient_user, assessments
):
    detail = await upload_and_process(client, fake_ocr)  # the harness's stand-in "PNG" is no image
    assert detail["quality"] is None
    page = await client.get(f"/en/patient/documents/{detail['id']}")
    assert page.status_code == 200 and "data-quality" not in page.text
    await client.get(f"/api/v1/documents/{detail['id']}")
    assert len(assessments) == 1  # an unmeasurable file is remembered as such, not retried

    # and a store that cannot be reached right now shows no advice rather than an error
    other = await upload_photo(client, fake_ocr, blurry_png())
    app_cache.cache.clear()
    fake_supabase.storage.fail_get = True
    page = await client.get(f"/en/patient/documents/{other['id']}")
    api = await client.get(f"/api/v1/documents/{other['id']}")
    assert page.status_code == 200 and "data-quality" not in page.text
    assert api.status_code == 200 and api.json()["quality"] is None


async def test_saved_records_and_the_list_never_measure_a_photo(
    client, fake_ocr, patient_user, assessments
):
    detail = await upload_photo(client, fake_ocr, blurry_png())
    await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=confirm_body(detail))
    before = len(assessments)
    app_cache.cache.clear()
    await client.get("/en/patient/documents")
    await client.get(f"/en/patient/documents/{detail['id']}")
    await client.get("/api/v1/documents")
    assert len(assessments) == before
    assert "data-quality" not in (await client.get(f"/en/patient/documents/{detail['id']}")).text


async def test_a_deleted_documents_verdict_is_forgotten(client, fake_ocr, patient_user):
    detail = await upload_photo(client, fake_ocr, blurry_png())
    key = f"doc_quality:{detail['id']}"
    assert await app_cache.cache.get(key) is not None
    assert (await client.delete(f"/api/v1/documents/{detail['id']}")).status_code == 200
    assert await app_cache.cache.get(key) is None


async def test_another_patient_never_sees_my_photos_verdict(
    app, client, fake_ocr, patient_user, second_patient
):
    detail = await upload_photo(client, fake_ocr, blurry_png())
    async with new_client(app) as other:
        await login(other, "other@x.com")
        api = await other.get(f"/api/v1/documents/{detail['id']}")
        web = await other.get(f"/en/patient/documents/{detail['id']}")
    assert api.status_code == 404 and "quality" not in api.text
    assert web.status_code == 404 and "data-quality" not in web.text


# ═══ Hardening after the audit ═══════════════════════════════════════════
# Each section below pins a problem found by auditing the branch, so it cannot come back.
async def insert_documents(
    db,
    patient_user,
    count: int,
    *,
    status: str = "finalized",
    prefix: str = "bulk",
    uploaded_at=None,
    finalized_at=None,
    dated: bool = True,
) -> list[Document]:
    """`count` documents written straight to the database: the newest-dated first
    (`prefix-000`), each a day older than the last unless `dated` is false."""
    pid = (
        await db.execute(select(Patient.id).where(Patient.user_id == patient_user.id))
    ).scalar_one()
    today = dbtypes.today_pk()
    docs = []
    for i in range(count):
        doc = Document(
            id=uuid.uuid4(),
            patient_id=pid,
            doc_type="rx",
            object_key=f"patients/{pid}/{uuid.uuid4()}.png",
            mime_type="image/png",
            size_bytes=10,
            uploaded_by=patient_user.id,
            uploaded_at=uploaded_at or utcnow(),
            original_filename=f"{prefix}-{i:03d}.png",
            status=status,
            document_date=(today - timedelta(days=i)) if dated else None,
            finalized_at=(finalized_at or utcnow()) if status == "finalized" else None,
        )
        db.add(doc)
        docs.append(doc)
    await db.commit()
    return docs


# ── Dates are Pakistan's ─────────────────────────────────────────────────
# Timestamps are stored in UTC; a patient means the day in Pakistan. A document uploaded at 1 am on
# the 10th there is 20:00 on the 9th in UTC, and used to be listed — and found by a date filter —
# under the 9th.
LATE_NIGHT_UTC = datetime(2026, 10, 9, 20, 0, tzinfo=UTC)  # 01:00 on 10 October in Pakistan


def test_a_timestamp_is_dated_in_pakistan():
    assert dbtypes.pk_date(LATE_NIGHT_UTC) == date(2026, 10, 10)
    assert dbtypes.pk_date(datetime(2026, 10, 9, 18, 59, tzinfo=UTC)) == date(2026, 10, 9)
    assert dbtypes.pk_date(datetime(2026, 10, 9, 19, 0, tzinfo=UTC)) == date(2026, 10, 10)
    assert dbtypes.pk_date(datetime(2026, 10, 9, 20, 0)) == date(2026, 10, 10)  # noqa: DTZ001 - a naive stamp is UTC


def test_the_pakistan_date_is_written_for_each_database_it_runs_on():
    from app.db.models import Document as D

    expression = pk_date(D.uploaded_at)
    postgres = str(expression.compile(dialect=postgresql.dialect()))
    assert "AT TIME ZONE 'Asia/Karachi'" in postgres and "::date" in postgres
    sqlite = str(expression.compile(dialect=sqlite_dialect()))
    assert "'+5 hours'" in sqlite


async def test_an_upload_made_after_midnight_in_pakistan_is_dated_that_day(
    client, db, patient_user
):
    (doc,) = await insert_documents(
        db, patient_user, 1, status="awaiting_review", uploaded_at=LATE_NIGHT_UTC, dated=False
    )
    tenth = "date_from=2026-10-10&date_to=2026-10-10"
    ninth = "date_from=2026-10-09&date_to=2026-10-09"
    found = (await client.get(f"/api/v1/documents/in-progress?{tenth}")).json()
    assert [d["id"] for d in found] == [str(doc.id)]
    assert (await client.get(f"/api/v1/documents/in-progress?{ninth}")).json() == []

    listing = (await client.get("/en/patient/documents")).text
    assert "· 2026-10-10" in listing and "2026-10-09" not in listing
    detail = (await client.get(f"/en/patient/documents/{doc.id}")).text
    assert "Uploaded 2026-10-10" in detail and "2026-10-09" not in detail


async def test_a_documents_own_date_is_never_replaced_by_the_upload_day(client, db, patient_user):
    (doc,) = await insert_documents(
        db, patient_user, 1, status="awaiting_review", uploaded_at=LATE_NIGHT_UTC
    )
    doc.document_date = date(2025, 3, 1)
    await db.commit()
    params = "date_from=2025-03-01&date_to=2025-03-01"
    assert len((await client.get(f"/api/v1/documents/in-progress?{params}")).json()) == 1
    tenth = "date_from=2026-10-10&date_to=2026-10-10"
    assert (await client.get(f"/api/v1/documents/in-progress?{tenth}")).json() == []


async def test_records_with_no_date_of_their_own_are_ordered_by_their_pakistan_day(
    client, db, patient_user
):
    (_late,) = await insert_documents(
        db, patient_user, 1, prefix="late", finalized_at=LATE_NIGHT_UTC, dated=False
    )
    (older,) = await insert_documents(db, patient_user, 1, prefix="older", dated=False)
    older.document_date = date(2026, 10, 9)
    older.finalized_at = datetime(2026, 10, 9, 21, 0, tzinfo=UTC)  # later in UTC than `late`...
    await db.commit()
    names_in_order = [
        d["original_filename"] for d in (await client.get("/api/v1/documents")).json()
    ]
    # ...but `late` is the 10th in Pakistan and `older` the 9th
    assert names_in_order == ["late-000.png", "older-000.png"]


# ── The list is paged ────────────────────────────────────────────────────
async def test_the_list_page_shows_one_page_of_records_at_a_time(client, db, patient_user):
    await insert_documents(db, patient_user, 60)
    seen: list[str] = []
    for number, expected in ((1, 25), (2, 25), (3, 10)):
        html = (await client.get(f"/en/patient/documents?page={number}")).text
        rows = re.findall(r"bulk-\d{3}\.png", html)
        names = list(dict.fromkeys(rows))
        assert len(names) == expected, (number, len(names))
        seen += names
        assert f"Page {number} of 3" in html
        assert ('rel="prev"' in html) == (number > 1)
        assert ('rel="next"' in html) == (number < 3)
    assert seen == [f"bulk-{i:03d}.png" for i in range(60)]  # newest first; none twice, none lost


async def test_a_page_number_that_makes_no_sense_shows_a_real_page(client, db, patient_user):
    await insert_documents(db, patient_user, 30)
    past_the_end = (await client.get("/en/patient/documents", params={"page": "999"})).text
    assert "Page 2 of 2" in past_the_end  # past the end is the last page, not an empty one
    for odd in ("99999999999999999999", "0", "-3", "abc", "1.5", ""):
        html = (await client.get("/en/patient/documents", params={"page": odd})).text
        assert "Page 1 of 2" in html, odd  # anything that is not a sensible number is page 1


async def test_the_pager_keeps_every_filter_and_counts_what_matched(client, db, patient_user):
    await insert_documents(db, patient_user, 60)
    await insert_documents(db, patient_user, 5, prefix="other")
    html = (await client.get("/en/patient/documents?q=bulk&status=finalized")).text
    assert "Matching documents: 60" in html and "Page 1 of 3" in html
    assert "q=bulk&amp;status=finalized&amp;page=2" in html
    assert "other-000.png" not in html


async def test_a_single_page_needs_no_pager(client, db, patient_user):
    await insert_documents(db, patient_user, 5)
    html = (await client.get("/en/patient/documents")).text
    assert "Page 1 of" not in html and 'aria-label="Pages of documents"' not in html


async def test_a_long_list_of_uploads_in_progress_says_it_is_cut_short(
    client, db, patient_user, monkeypatch
):
    from app.web import documents as web_documents

    monkeypatch.setattr(web_documents, "IN_PROGRESS_CAP", 2)
    await insert_documents(db, patient_user, 3, status="awaiting_review", prefix="wip")
    html = (await client.get("/en/patient/documents")).text
    assert "Showing the 2 newest of 3 uploads in progress." in html
    assert html.count("wip-") >= 2 and "Matching documents" not in html  # no filter, no count


async def test_the_api_pages_its_lists_and_says_how_many_there_are(client, db, patient_user):
    await insert_documents(db, patient_user, 7)
    await insert_documents(db, patient_user, 4, status="failed", prefix="bad")
    page_one = await client.get("/api/v1/documents?limit=3")
    page_two = await client.get("/api/v1/documents?limit=3&offset=3")
    page_three = await client.get("/api/v1/documents?limit=3&offset=6")
    assert page_one.headers["x-total-count"] == "7"
    got = [d["original_filename"] for r in (page_one, page_two, page_three) for d in r.json()]
    assert got == [f"bulk-{i:03d}.png" for i in range(7)]
    assert (await client.get("/api/v1/documents?offset=50")).json() == []
    progress = await client.get("/api/v1/documents/in-progress?limit=2")
    assert progress.headers["x-total-count"] == "4" and len(progress.json()) == 2
    for bad in ("limit=0", "limit=501", "limit=abc", "offset=-1", "offset=x"):
        assert (await client.get(f"/api/v1/documents?{bad}")).status_code == 422, bad


# ── The API never quietly ignores what it was asked ──────────────────────
async def test_a_misspelt_or_unknown_parameter_is_refused_not_ignored(client, library):
    for url in ("/api/v1/documents", "/api/v1/documents/in-progress"):
        for query in ("stauts=failed", "page=2", "sort=name", "q=lab&foo=1"):
            r = await client.get(f"{url}?{query}")
            assert r.status_code == 422, (url, query)
            fields = r.json()["error"]["details"]["fields"]
            assert set(fields) <= {"stauts", "page", "sort", "foo"} and fields


async def test_the_records_endpoint_understands_status(client, library):
    everything = await client.get("/api/v1/documents")
    saved = await client.get("/api/v1/documents?status=finalized")
    assert len(saved.json()) == len(everything.json()) == 3
    for not_a_record in ("failed", "awaiting_review", "processing", "uploaded"):
        r = await client.get(f"/api/v1/documents?status={not_a_record}")
        assert r.status_code == 200 and r.json() == [], not_a_record  # no record has that status
    assert (await client.get("/api/v1/documents?status=cancelled")).status_code == 422
    assert (await client.get("/api/v1/documents?status=bogus")).status_code == 422


# ── Discarding is as strict as deleting ──────────────────────────────────
async def cancel_audit(db, doc_id) -> list[AuditLog]:
    rows = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "document.cancel")))
        .scalars()
        .all()
    )
    return [r for r in rows if json.loads(r.detail).get("document_id") == str(doc_id)]


async def test_a_discard_whose_file_cannot_be_removed_discards_nothing(
    client, db, fake_ocr, fake_supabase, patient_user, monkeypatch
):
    detail = await upload_and_process(client, fake_ocr)
    doc_id = detail["id"]
    real_remove = doc_storage.remove

    async def store_down(key):
        raise RuntimeError("storage unavailable: secret-internal-detail")

    monkeypatch.setattr(doc_storage, "remove", store_down)
    api = await client.post(f"/api/v1/documents/{doc_id}/cancel")
    assert api.status_code == 502 and api.json()["error"]["code"] == "DELETE_FAILED"
    assert "secret-internal-detail" not in api.text
    web = await client.post(f"/en/patient/documents/{doc_id}/cancel")
    assert web.status_code == 502 and "has not been deleted" in web.text
    row = await doc_row(db, doc_id)
    assert row.status == "awaiting_review" and row.ocr_text is not None  # nothing changed
    assert fake_supabase.storage.objects and await extraction_rows(db, doc_id)
    assert await cancel_audit(db, doc_id) == []

    monkeypatch.setattr(doc_storage, "remove", real_remove)  # the store is back
    done = await client.post(f"/api/v1/documents/{doc_id}/cancel")
    assert done.status_code == 200 and done.json()["status"] == "cancelled"
    assert fake_supabase.storage.objects == {} and len(await cancel_audit(db, doc_id)) == 1


async def test_discarding_clears_the_models_reading_of_the_page_too(
    client, db, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr)
    job = await job_row(db, detail["id"])
    job.ai_sections = {"version": 1, "facility": {"hospital_name": "City Clinic"}}
    await db.commit()
    await client.post(f"/api/v1/documents/{detail['id']}/cancel")
    assert (await job_row(db, detail["id"])).ai_sections is None


@pytest.mark.parametrize("how", ["cancel", "delete"])
async def test_repeating_a_discard_or_delete_removes_a_file_an_earlier_one_left(
    client, db, fake_ocr, fake_supabase, patient_user, how
):
    """An earlier version removed the file best-effort: a failure left it behind with the
    document already marked gone. Doing it again now finishes the job."""
    detail = await upload_and_process(client, fake_ocr)
    doc_id = detail["id"]
    key = (await doc_row(db, doc_id)).object_key
    stored = dict(fake_supabase.storage.objects)

    async def retire():
        if how == "cancel":
            return await client.post(f"/api/v1/documents/{doc_id}/cancel")
        return await client.delete(f"/api/v1/documents/{doc_id}")

    first = await retire()
    assert first.status_code == 200 and fake_supabase.storage.objects == {}
    fake_supabase.storage.objects.update(stored)  # the leftover: gone from the app, still stored
    assert (settings.document_bucket, key) in fake_supabase.storage.objects
    again = await retire()
    assert again.status_code == 200
    assert fake_supabase.storage.objects == {}


# ── A commit that fails after the file is gone ───────────────────────────
def flaky_commit(monkeypatch, fake_supabase, *, failures: int, apply_first: bool = False):
    """Make `AsyncSession.commit` raise `failures` times once the stored file has been removed
    (the moment that matters). With `apply_first` the commit really happens and THEN raises — a
    failure the application cannot tell from one that did nothing."""
    real = AsyncSession.commit
    state = {"left": failures, "raised": 0}

    async def commit(self):
        if state["left"] and fake_supabase.storage.objects == {}:
            state["left"] -= 1
            state["raised"] += 1
            if apply_first:
                await real(self)
            raise RuntimeError("connection reset by peer: secret-internal-detail")
        return await real(self)

    monkeypatch.setattr(AsyncSession, "commit", commit)
    return state


async def test_one_failed_commit_after_the_file_is_gone_is_retried_and_succeeds(
    client, db, fake_ocr, fake_supabase, patient_user, monkeypatch
):
    detail = await seed_document(client, fake_ocr, name="a.png", finalize=True)
    state = flaky_commit(monkeypatch, fake_supabase, failures=1)
    r = await client.delete(f"/api/v1/documents/{detail['id']}?confirm=true")
    assert state["raised"] == 1
    assert r.status_code == 200 and r.json()["status"] == "cancelled"  # the patient never sees it
    assert (await doc_row(db, detail["id"])).status == "cancelled"
    assert await extraction_rows(db, detail["id"]) == []
    assert len(await delete_audit(db, detail["id"])) == 1


async def test_a_commit_that_landed_but_looked_like_a_failure_is_not_done_twice(
    client, db, fake_ocr, fake_supabase, patient_user, monkeypatch
):
    detail = await seed_document(client, fake_ocr, name="a.png", finalize=True)
    state = flaky_commit(monkeypatch, fake_supabase, failures=1, apply_first=True)
    r = await client.delete(f"/api/v1/documents/{detail['id']}?confirm=true")
    assert state["raised"] == 1 and r.status_code == 200
    assert (await doc_row(db, detail["id"])).status == "cancelled"
    assert len(await delete_audit(db, detail["id"])) == 1  # one deletion, one entry


async def test_two_failed_commits_leave_the_record_and_deleting_again_finishes_it(
    client, db, fake_ocr, fake_supabase, patient_user, monkeypatch
):
    detail = await seed_document(client, fake_ocr, name="a.png", finalize=True)
    state = flaky_commit(monkeypatch, fake_supabase, failures=2)
    r = await client.delete(f"/api/v1/documents/{detail['id']}?confirm=true")
    assert state["raised"] == 2
    assert r.status_code == 502 and r.json()["error"]["code"] == "DELETE_FAILED"
    assert "secret-internal-detail" not in r.text
    assert (
        await doc_row(db, detail["id"])
    ).status == "finalized"  # intact, though its file is gone
    assert await delete_audit(db, detail["id"]) == []
    again = await client.delete(f"/api/v1/documents/{detail['id']}?confirm=true")
    assert again.status_code == 200 and (await doc_row(db, detail["id"])).status == "cancelled"
    assert len(await delete_audit(db, detail["id"])) == 1


# ── Back to the page you asked for after signing in ──────────────────────
@pytest.mark.parametrize(
    "value",
    [
        "/en/patient/documents",
        "/en/patient/documents/0192a1b2-0000-7000-8000-000000000000",
        "/ur/patient/documents?q=lab&status=finalized&page=2",
        "/en/patient",
        "/en/doctor/prescriptions/new",
        "/en/admin",
    ],
)
def test_a_page_inside_a_signed_in_area_is_a_safe_place_to_return_to(value):
    assert safe_next(value) == value


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "//evil.example/en/patient",
        "https://evil.example/en/patient",
        "http://localhost/en/patient",
        "javascript:alert(1)",
        "/\\evil.example",
        "/en/patient/documents\r\nSet-Cookie: x=1",
        "/en/patient/documents\x00",
        "/en/login",
        "/en/register",
        "/api/v1/documents",
        "/static/js/documents.js",
        "/fr/patient/documents",
        "en/patient/documents",
        "/en/patientevil",
        "/en/patient documents",
        "/en/patient/" + "a" * 600,
        ["/en/patient"],
    ],
)
def test_anything_that_could_lead_elsewhere_is_not(value):
    assert safe_next(value) is None


async def test_a_signed_out_visitor_is_sent_back_to_their_page_with_its_filters(app):
    async with new_client(app) as anon:
        r = await anon.get("/en/patient/documents?q=lab&status=failed", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == (
            "/en/login?next=/en/patient/documents%3Fq%3Dlab%26status%3Dfailed"
        )
        page = await anon.get(r.headers["location"])
        assert 'name="next" value="/en/patient/documents?q=lab&amp;status=failed"' in page.text


async def test_a_form_submission_has_no_page_to_come_back_to(app):
    async with new_client(app) as anon:
        r = await anon.post(
            f"/en/patient/documents/{uuid.uuid4()}/delete",
            data={"confirm": "1"},
            follow_redirects=False,
        )
        assert r.status_code == 303 and r.headers["location"] == "/en/login"


async def test_signing_in_returns_to_the_page_that_was_asked_for(app, second_patient):
    async with new_client(app) as visitor:
        asked = "/en/patient/documents?q=lab&status=finalized"
        login_url = (await visitor.get(asked, follow_redirects=False)).headers["location"]
        form = await visitor.get(login_url)
        assert 'name="next"' in form.text
        done = await visitor.post(
            "/en/login",
            data={
                "email": "other@x.com",
                "password": TEST_PASSWORD,
                "next": "/en/patient/documents?q=lab&status=finalized",
            },
            follow_redirects=False,
        )
        assert done.status_code == 303 and done.headers["location"] == asked
        assert (await visitor.get(asked)).status_code == 200  # and it is open to them now


@pytest.mark.parametrize(
    "unsafe", ["//evil.example", "https://evil.example/x", "/api/v1/documents", "/\\evil.example"]
)
async def test_signing_in_ignores_a_return_address_that_leads_elsewhere(
    app, second_patient, unsafe
):
    async with new_client(app) as visitor:
        page = await visitor.get("/en/login", params={"next": unsafe})
        assert 'name="next"' not in page.text
        done = await visitor.post(
            "/en/login",
            data={"email": "other@x.com", "password": TEST_PASSWORD, "next": unsafe},
            follow_redirects=False,
        )
        assert done.status_code == 303 and done.headers["location"] == "/en/patient"


async def test_a_failed_sign_in_keeps_the_page_to_return_to(app, second_patient):
    async with new_client(app) as visitor:
        bad = await visitor.post(
            "/en/login",
            data={
                "email": "other@x.com",
                "password": "wrong-password",
                "next": "/en/patient/documents",
            },
        )
        assert bad.status_code == 401
        assert 'name="next" value="/en/patient/documents"' in bad.text


async def test_someone_already_signed_in_who_follows_a_sign_in_link_goes_straight_there(
    app, client, patient_user
):
    r = await client.get(
        "/en/login", params={"next": "/en/patient/documents?q=x"}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/en/patient/documents?q=x"
    plain = await client.get("/en/login", follow_redirects=False)
    assert plain.headers["location"] == "/en/patient"


async def test_a_doctor_sent_to_a_patient_page_lands_in_their_own_area(
    app, client, patient_user, verified_doctor
):
    async with new_client(app) as visitor:
        done = await visitor.post(
            "/en/login",
            data={"email": "doc@x.com", "password": TEST_PASSWORD, "next": "/en/patient/documents"},
            follow_redirects=False,
        )
        assert done.headers["location"] == "/en/patient/documents"
        onward = await visitor.get("/en/patient/documents", follow_redirects=False)
        assert onward.status_code == 303 and onward.headers["location"] == "/en/doctor"


# ── The photo-quality verdict ────────────────────────────────────────────
async def test_an_upload_not_yet_read_is_already_told_its_photo_is_blurry(
    client, fake_ocr, patient_user
):
    up = await upload(client, "prescription", name="p.png", data=blurry_png(), mime="image/png")
    doc_id = up.json()["id"]  # stored, never read
    assert (await client.get(f"/api/v1/documents/{doc_id}")).json()["status"] == "uploaded"
    page = (await client.get(f"/en/patient/documents/{doc_id}")).text
    assert 'data-quality="blurry"' in page and "Read document" in page
    sharp = await upload(client, "prescription", name="s.png", data=sharp_png(), mime="image/png")
    assert (
        "data-quality" not in (await client.get(f"/en/patient/documents/{sharp.json()['id']}")).text
    )


async def test_an_unreachable_store_is_tried_once_not_on_every_view(
    client, fake_ocr, fake_supabase, patient_user, monkeypatch
):
    detail = await upload_photo(client, fake_ocr, blurry_png())
    app_cache.cache.clear()
    fake_supabase.storage.fail_get = True
    attempts: list[str] = []
    real_get = doc_storage.get

    async def counting_get(key):
        attempts.append(key)
        return await real_get(key)

    monkeypatch.setattr(doc_storage, "get", counting_get)
    for _ in range(3):
        assert (await client.get(f"/en/patient/documents/{detail['id']}")).status_code == 200
    assert len(attempts) == 1  # the other two views remembered that it could not be reached

    app_cache.cache.clear()  # and once the store answers again the verdict comes back
    fake_supabase.storage.fail_get = False
    page = (await client.get(f"/en/patient/documents/{detail['id']}")).text
    assert 'data-quality="blurry"' in page


async def test_the_photo_is_judged_while_it_is_read_not_before_or_after(
    client, fake_ocr, patient_user, monkeypatch
):
    """The judging overlaps the reading: it waits here for the reader to start, which it could
    never do if it ran to completion first."""
    reader_started = asyncio.Event()
    judged = asyncio.Event()
    real = ocr.assess_quality

    async def hook():
        reader_started.set()
        await asyncio.wait_for(judged.wait(), timeout=10)  # and the reader waits for the judge

    async def judging(data, mime):
        await asyncio.wait_for(reader_started.wait(), timeout=10)
        report = await real(data, mime)
        judged.set()
        return report

    fake_ocr.hook = hook
    monkeypatch.setattr(ocr, "assess_quality", judging)
    detail = await upload_photo(client, fake_ocr, blurry_png())
    assert detail["status"] == "awaiting_review" and detail["quality"]["issues"] == ["blurry"]


# ── The cleanup tool for files an earlier discard left behind ─────────────
async def test_the_cleanup_tool_removes_only_the_files_of_discarded_documents(
    client, db, fake_ocr, fake_supabase, patient_user
):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "ops" / "scripts"))
    from purge_cancelled_files import purge_cancelled_files

    kept = await seed_document(client, fake_ocr, name="kept.png", finalize=True)
    waiting = await seed_document(client, fake_ocr, name="waiting.png")
    gone = await seed_document(client, fake_ocr, name="gone.png")
    objects_before = dict(fake_supabase.storage.objects)
    await client.post(f"/api/v1/documents/{gone['id']}/cancel")
    fake_supabase.storage.objects.update(objects_before)  # as if the old best-effort removal failed
    leftover = (await doc_row(db, gone["id"])).object_key
    assert len(fake_supabase.storage.objects) == 3

    assert await purge_cancelled_files(db, apply=False) == (1, 0, 0)  # a dry run changes nothing
    assert len(fake_supabase.storage.objects) == 3
    assert await purge_cancelled_files(db, apply=True) == (1, 1, 0)
    remaining = {key for (_bucket, key) in fake_supabase.storage.objects}
    assert leftover not in remaining and len(remaining) == 2  # the record and the waiting upload
    assert (await doc_row(db, kept["id"])).status == "finalized"
    assert (await doc_row(db, waiting["id"])).status == "awaiting_review"
    assert await purge_cancelled_files(db, apply=True) == (1, 1, 0)  # harmless to repeat


async def test_the_cleanup_tool_reports_a_file_it_could_not_remove_and_carries_on(
    client, db, fake_ocr, fake_supabase, patient_user, monkeypatch
):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "ops" / "scripts"))
    from purge_cancelled_files import purge_cancelled_files

    docs = [await seed_document(client, fake_ocr, name=f"{n}.png") for n in "abc"]
    for d in docs:
        await client.post(f"/api/v1/documents/{d['id']}/cancel")
    bad_key = (await doc_row(db, docs[1]["id"])).object_key
    real = doc_storage.remove

    async def remove(key):
        if key == bad_key:
            raise RuntimeError("down")
        return await real(key)

    monkeypatch.setattr(doc_storage, "remove", remove)
    assert await purge_cancelled_files(db, apply=True) == (3, 2, 1)
