"""The OCR boundary: one local engine (Tesseract), no external API. Only printed
documents are read — handwriting is deliberately not supported.

Layers, each testing what it can honestly test:

1. **Everything around the engine runs for real** — Pillow decoding and EXIF
   rotation, pypdfium2 rendering real PDFs to real page images, the page and
   pixel caps, error mapping. Only the single Tesseract call
   (`pytesseract.image_to_data`) is replaced by a recording fake, so these tests
   need no Tesseract binary and no network.
2. **The pipeline through the real engine** — upload -> process -> extraction
   with only that one call faked.
3. **Real Tesseract** — skipped, with a reason, when the binary is not available
   on the machine running the tests.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
import time
import uuid

import numpy as np
import pytesseract
import pytest
from app.db.models import Document, OcrJob, Patient, Profile, UserRole
from app.documents import extractors, ocr
from app.i18n.catalogue import translate
from app.settings import settings
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select

from tests.conftest import TEST_PASSWORD, make_user

RX_LINES = [
    "Dr. Mehreen Iqbal",
    "Tab. Metformin 500mg 1-0-1 x 30 days",
    "Cap Atorvastatin 10 mg once daily",
]


# ── Real files, built locally ────────────────────────────────────────────
def _image(size=(1800, 1200), mode="RGB") -> Image.Image:
    return Image.new(mode, size, "white")


def png_bytes(size=(1800, 1200)) -> bytes:
    buf = io.BytesIO()
    _image(size).save(buf, format="PNG")
    return buf.getvalue()


def jpeg_bytes(size=(1800, 1200), exif_orientation: int | None = None) -> bytes:
    buf = io.BytesIO()
    kwargs = {}
    if exif_orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = exif_orientation
        kwargs["exif"] = exif
    _image(size).save(buf, format="JPEG", **kwargs)
    return buf.getvalue()


def pdf_bytes(pages: int = 1, size=(800, 1000)) -> bytes:
    """A real PDF: `size` px at 100 dpi, i.e. an 8 x 10 inch page."""
    images = [_image(size) for _ in range(pages)]
    buf = io.BytesIO()
    images[0].save(buf, format="PDF", save_all=True, append_images=images[1:], resolution=100.0)
    return buf.getvalue()


def as_bytes(image: Image.Image, fmt: str, **kwargs) -> bytes:
    if fmt == "JPEG":
        image = image.convert("RGB")
    buf = io.BytesIO()
    image.save(buf, format=fmt, **kwargs)
    return buf.getvalue()


# ── The fakes at the two engine boundaries ───────────────────────────────
def _text_as_tesseract_data(text: str) -> dict[str, list]:
    """The exact inverse of `ocr._reconstruct_tesseract_output`: turns plain
    text back into an `image_to_data(..., output_type=Output.DICT)`-shaped
    dict (a blank line starts a new paragraph, any other line continues
    the current one), so a fake built from configured text round-trips
    through production code exactly as that same text — verified directly
    by `test_fake_tesseract_data_round_trips_through_the_real_reconstruction`
    below."""
    rows: dict[str, list] = {"block_num": [], "par_num": [], "line_num": [], "text": [], "conf": []}
    block, par, line = 1, 1, 0
    pending_new_paragraph = False
    for raw_line in text.split("\n"):
        if raw_line.strip() == "":
            pending_new_paragraph = True
            continue
        if pending_new_paragraph:
            par += 1
            line = 0
        line += 1
        pending_new_paragraph = False
        for word in raw_line.split():
            rows["block_num"].append(block)
            rows["par_num"].append(par)
            rows["line_num"].append(line)
            rows["text"].append(word)
            rows["conf"].append(95.0)
    if not rows["text"]:
        rows["block_num"].append(1)
        rows["par_num"].append(1)
        rows["line_num"].append(1)
        rows["text"].append("")
        rows["conf"].append(-1.0)
    return rows


class FakeTesseract:
    def __init__(self, *texts: str, error: Exception | None = None) -> None:
        self.texts = list(texts) or ["Metformin 500mg 1-0-1"]
        self.error = error
        self.calls: list[dict] = []

    def __call__(self, image, lang=None, config="", nice=0, timeout=0, output_type=None):
        self.calls.append({"image": image, "lang": lang, "config": config, "timeout": timeout})
        if self.error:
            raise self.error
        text = self.texts[min(len(self.calls), len(self.texts)) - 1]
        return _text_as_tesseract_data(text)


# ── `_reconstruct_tesseract_output`: pure, no real Tesseract needed ──────
def test_reconstruction_round_trips_through_the_fake_data_builder():
    """`_text_as_tesseract_data` is deliberately the exact inverse of
    `ocr._reconstruct_tesseract_output` — this proves the round trip holds
    for a multi-paragraph, multi-line text, so every OTHER test using
    `fake_tesseract.texts` can trust the configured text comes back
    unchanged through production code."""
    text = "Dr. Mehreen Iqbal\n\nTake 1 tablet\nevery 6 hours\n\nDrink fluids."
    data = _text_as_tesseract_data(text)
    reconstructed, _ = ocr._reconstruct_tesseract_output(data)
    assert reconstructed == text


def test_reconstruction_of_a_blank_page_is_an_empty_string_not_a_stray_newline():
    data = _text_as_tesseract_data("")
    text, confidences = ocr._reconstruct_tesseract_output(data)
    assert text == ""
    assert confidences == []


def test_reconstruction_produces_one_confidence_entry_per_splitlines_entry():
    text = "Line one\n\nLine two\nLine three"
    data = _text_as_tesseract_data(text)
    reconstructed, confidences = ocr._reconstruct_tesseract_output(data)
    assert reconstructed == text
    assert len(confidences) == len(reconstructed.splitlines())
    # The blank paragraph-break line is the one `None` entry.
    blank_index = reconstructed.splitlines().index("")
    assert confidences[blank_index] is None
    assert all(c is not None for i, c in enumerate(confidences) if i != blank_index)


def test_reconstruction_confidence_reflects_real_tesseract_conf_scale():
    """Tesseract's `conf` is 0-100 — this must come back as a 0-1
    probability, the same scale TrOCR's `LineConfidence` already uses."""
    data = {
        "block_num": [1, 1],
        "par_num": [1, 1],
        "line_num": [1, 1],
        "text": ["Metformin", "500mg"],
        "conf": [80.0, 60.0],
    }
    _, confidences = ocr._reconstruct_tesseract_output(data)
    assert len(confidences) == 1
    line = confidences[0]
    assert line is not None
    assert line.min_prob == pytest.approx(0.6)
    assert 0.6 < line.geo_mean < 0.8


@pytest.fixture
def fake_tesseract(monkeypatch) -> FakeTesseract:
    """Tesseract 'installed' (so the lookup succeeds) with `image_to_data`
    replaced by a recorder — production code now makes this ONE call per
    page (see `ocr._read_page`), never `image_to_string` at all."""
    fake = FakeTesseract()
    monkeypatch.setattr(ocr, "resolve_tesseract_cmd", lambda: "tesseract")
    monkeypatch.setattr(pytesseract.pytesseract, "tesseract_cmd", "tesseract")  # restored after
    monkeypatch.setattr(pytesseract, "image_to_data", fake)
    return fake


# ═══ The engine ══════════════════════════════════════════════════════════
def test_the_engine_is_tesseract_and_nothing_else_is_offered():
    assert isinstance(ocr.get_engine(), ocr.TesseractEngine)
    assert not hasattr(ocr, "TrocrEngine") and not hasattr(ocr, "segment_lines")
    assert not [n for n in type(settings).model_fields if "trocr" in n]


async def test_run_ocr_reports_the_engine_used(fake_tesseract):
    result = await ocr.run_ocr(png_bytes(), "image/png")
    assert result.engine == "tesseract"


async def test_the_default_page_layout_is_unchanged_and_the_table_layout_is_opt_in(
    fake_tesseract,
):
    await ocr.run_ocr(png_bytes(), "image/png")
    await ocr.run_ocr(png_bytes(), "image/png", layout=ocr.LAYOUT_TABLE)
    default, table = (call["config"] for call in fake_tesseract.calls)
    assert "--psm 3" in default and "--psm 4" in table


def test_an_unknown_page_layout_is_refused_not_guessed():
    with pytest.raises(ValueError):
        ocr.TesseractEngine("columns")


def test_ocr_is_local_only_no_external_api_or_key():
    assert importlib.util.find_spec("anthropic") is None  # the SDK is not even installed
    fields = type(settings).model_fields
    # The AI-provider keys are deliberately excluded here: Gemini, Groq and
    # OpenRouter are the separate, optional, disclosed AI-understanding stage
    # AFTER OCR (`ai_understanding.py` / `ai_providers.py`), never OCR itself —
    # OCR (this test's actual subject) still has no API key.
    ai_stage = {
        "gemini_api_key",
        "gemini_model",
        "gemini_timeout_s",
        "groq_api_key",
        "groq_model",
        "openrouter_api_key",
        "openrouter_model",
    }
    ocr_fields = [n for n in fields if n not in ai_stage]
    assert not [n for n in ocr_fields if "api_key" in n or n.startswith("anthropic")]


# ═══ Printed: Tesseract, around the real call ════════════════════════════
@pytest.mark.parametrize(
    ("data", "mime"),
    [(png_bytes(), "image/png"), (jpeg_bytes(), "image/jpeg")],
    ids=["png", "jpeg"],
)
async def test_images_are_decoded_and_read_with_the_configured_language(fake_tesseract, data, mime):
    result = await ocr.run_ocr(data, mime)
    assert (result.text, result.engine) == ("Metformin 500mg 1-0-1", "tesseract")
    (call,) = fake_tesseract.calls
    assert isinstance(call["image"], Image.Image) and call["image"].mode == "L"  # greyscale page
    assert call["lang"] == settings.tesseract_lang == "eng"
    assert call["timeout"] == settings.ocr_timeout_s
    assert "--psm" in call["config"]


async def test_a_small_image_is_upscaled_so_tesseract_can_read_it(fake_tesseract):
    await ocr.run_ocr(png_bytes((300, 200)), "image/png")
    assert max(fake_tesseract.calls[0]["image"].size) >= 1500


async def test_the_upscale_reaches_the_floor_without_overshooting_it(fake_tesseract):
    """An image only 1px under the floor must land just over it (~1510px),
    not be needlessly doubled (~3000px) for a 1px shortfall — the previous
    `math.ceil` on the scale MULTIPLIER rounded up to the next whole
    multiple of the original size, not up to the floor itself. The existing
    `>= 1500` test above can't tell an efficient resize from a wasteful one;
    this one can."""
    await ocr.run_ocr(png_bytes((1499, 1000)), "image/png")
    long_side = max(fake_tesseract.calls[0]["image"].size)
    assert 1500 <= long_side <= 1550


async def test_an_exif_rotated_photo_is_turned_upright_first(fake_tesseract):
    # A phone photo stored sideways (EXIF orientation 6 = rotate 90 degrees).
    await ocr.run_ocr(jpeg_bytes((1800, 900), exif_orientation=6), "image/jpeg")
    assert fake_tesseract.calls[0]["image"].size == (900, 1800)


async def test_a_pdf_is_rendered_locally_page_by_page_and_the_text_joined(fake_tesseract):
    fake_tesseract.texts = ["First page text", "Second page text"]
    result = await ocr.run_ocr(pdf_bytes(pages=2), "application/pdf")
    assert result.text == "First page text\n\nSecond page text"
    assert len(fake_tesseract.calls) == 2
    width, height = fake_tesseract.calls[0]["image"].size  # an 8 x 10 inch page at 300 dpi
    assert abs(width - 2400) <= 2 and abs(height - 3000) <= 2


async def test_the_pdf_render_resolution_is_configurable(monkeypatch, fake_tesseract):
    monkeypatch.setattr(settings, "ocr_pdf_dpi", 200)
    await ocr.run_ocr(pdf_bytes(), "application/pdf")
    assert abs(fake_tesseract.calls[0]["image"].size[0] - 1600) <= 2


async def test_a_pdf_with_too_many_pages_is_refused_not_silently_truncated(
    monkeypatch, fake_tesseract
):
    monkeypatch.setattr(settings, "ocr_max_pages", 2)
    with pytest.raises(ocr.OcrError) as exc:
        await ocr.run_ocr(pdf_bytes(pages=3), "application/pdf")
    assert exc.value.code == ocr.OCR_TOO_LONG
    assert fake_tesseract.calls == []  # not one page was read


async def test_a_huge_pdf_page_is_rendered_within_the_pixel_cap(monkeypatch, fake_tesseract):
    monkeypatch.setattr(settings, "ocr_max_pixels", 6_000_000)  # A4 at 300 dpi would be ~8.7 MP
    await ocr.run_ocr(pdf_bytes(), "application/pdf")
    width, height = fake_tesseract.calls[0]["image"].size
    assert width * height <= 6_100_000


async def test_an_image_larger_than_the_pixel_cap_is_refused_before_decoding(
    monkeypatch, fake_tesseract
):
    monkeypatch.setattr(settings, "ocr_max_pixels", 10_000)
    with pytest.raises(ocr.OcrError) as exc:
        await ocr.run_ocr(png_bytes((400, 400)), "image/png")
    assert exc.value.code == ocr.OCR_FAILED
    assert fake_tesseract.calls == []


@pytest.mark.parametrize(
    ("data", "mime"),
    [
        (b"\x89PNG\r\n\x1a\n" + b"not really a png", "image/png"),
        (b"\xff\xd8\xff" + b"truncated", "image/jpeg"),
        (b"%PDF-1.4 this is not a real pdf", "application/pdf"),
    ],
    ids=["bad-png", "bad-jpeg", "bad-pdf"],
)
async def test_a_file_that_looks_right_but_cannot_be_decoded_is_a_failure_not_a_crash(
    fake_tesseract, data, mime
):
    with pytest.raises(ocr.OcrError) as exc:
        await ocr.run_ocr(data, mime)
    assert exc.value.code == ocr.OCR_FAILED
    assert fake_tesseract.calls == []


@pytest.mark.parametrize("noise", ["", "  \n\n ", "~ . ,", "ab"])
async def test_output_with_no_real_text_is_reported_as_empty(fake_tesseract, noise):
    fake_tesseract.texts = [noise]
    with pytest.raises(ocr.OcrError) as exc:
        await ocr.run_ocr(png_bytes(), "image/png")
    assert exc.value.code == ocr.OCR_EMPTY


async def test_tesseract_page_breaks_and_non_breaking_spaces_are_normalised(fake_tesseract):
    fake_tesseract.texts = ["Metformin\xa0500mg\f"]
    assert (await ocr.run_ocr(png_bytes(), "image/png")).text == "Metformin 500mg"


async def test_urdu_text_is_cleaned_the_same_way_as_english(fake_tesseract):
    """The cleaning step (`\\f`/`\\xa0` normalisation, `.strip()`) is plain
    string manipulation with no script-specific logic — it must behave
    identically for Urdu (Arabic script, right-to-left) as for English.
    ``مریض کا نام: عائشہ رضا`` is "Patient's name: Ayesha Raza"."""
    fake_tesseract.texts = ["\xa0مریض کا نام:\xa0عائشہ رضا\f"]
    result = await ocr.run_ocr(png_bytes(), "image/png")
    assert result.text == "مریض کا نام: عائشہ رضا"


async def test_genuine_urdu_text_passes_the_empty_gate(fake_tesseract):
    """`run_ocr`'s `OCR_EMPTY` gate counts `str.isalnum()` characters — this
    must recognise Urdu letters as real content, not just ASCII, or every
    genuinely-read Urdu document would be wrongly reported as unreadable.
    No Latin character appears anywhere in this text at all."""
    fake_tesseract.texts = ["دوا کا نام: میٹفارمین"]  # "Medicine name: Metformin"
    result = await ocr.run_ocr(png_bytes(), "image/png")
    assert result.text == "دوا کا نام: میٹفارمین"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (pytesseract.TesseractNotFoundError(), ocr.OCR_UNAVAILABLE),
        (pytesseract.TesseractError(1, "Failed loading language 'urd'"), ocr.OCR_UNAVAILABLE),
        (
            pytesseract.TesseractError(1, "Error opening data file eng.traineddata"),
            ocr.OCR_UNAVAILABLE,
        ),
        (pytesseract.TesseractError(1, "Image too small to scale"), ocr.OCR_FAILED),
        (RuntimeError("Tesseract process timeout"), ocr.OCR_FAILED),
    ],
    ids=["not-found", "language-missing", "data-missing", "bad-image", "timeout"],
)
async def test_tesseract_errors_map_to_specific_failure_codes(fake_tesseract, error, code):
    fake_tesseract.error = error
    with pytest.raises(ocr.OcrError) as exc:
        await ocr.run_ocr(png_bytes(), "image/png")
    assert exc.value.code == code


async def test_without_tesseract_installed_ocr_is_unavailable_and_nothing_is_faked(monkeypatch):
    monkeypatch.setattr(ocr, "resolve_tesseract_cmd", lambda: None)
    called = []
    monkeypatch.setattr(pytesseract, "image_to_string", lambda *a, **k: called.append(1) or "text")
    with pytest.raises(ocr.OcrError) as exc:
        await ocr.run_ocr(png_bytes(), "image/png")
    assert exc.value.code == ocr.OCR_UNAVAILABLE and called == []


def test_the_configured_tesseract_path_wins(monkeypatch, tmp_path):
    exe = tmp_path / "tesseract.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(settings, "tesseract_cmd", str(exe))
    monkeypatch.setattr(ocr.shutil, "which", lambda name: "/usr/bin/tesseract")
    assert ocr.resolve_tesseract_cmd() == str(exe)


def test_a_bad_configured_path_is_not_silently_replaced(monkeypatch):
    monkeypatch.setattr(settings, "tesseract_cmd", r"C:\nowhere\tesseract.exe")
    monkeypatch.setattr(ocr.shutil, "which", lambda name: None)
    assert ocr.resolve_tesseract_cmd() is None


def test_tesseract_is_found_on_path_then_at_the_windows_default(monkeypatch):
    monkeypatch.setattr(settings, "tesseract_cmd", "")
    monkeypatch.setattr(ocr.shutil, "which", lambda name: "/usr/bin/tesseract")
    assert ocr.resolve_tesseract_cmd() == "/usr/bin/tesseract"

    monkeypatch.setattr(ocr.shutil, "which", lambda name: None)
    monkeypatch.setattr(ocr.os.path, "isfile", lambda p: p == ocr._WINDOWS_DEFAULT_CMD)
    assert ocr.resolve_tesseract_cmd() == ocr._WINDOWS_DEFAULT_CMD

    monkeypatch.setattr(ocr.os.path, "isfile", lambda p: False)
    assert ocr.resolve_tesseract_cmd() is None


async def test_tesseract_runs_off_the_event_loop(monkeypatch, fake_tesseract):
    """A slow Tesseract call must not stall other requests."""

    def slow(image, **kwargs):
        time.sleep(0.3)
        return _text_as_tesseract_data("Metformin 500mg")

    monkeypatch.setattr(pytesseract, "image_to_data", slow)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    task = asyncio.create_task(ticker())
    await ocr.run_ocr(png_bytes(), "image/png")
    task.cancel()
    assert ticks >= 5  # the loop kept running while Tesseract worked


# ── Per-line confidence: length stays in sync with text.splitlines() ─────


# ═══ Model loading and caching ═══════════════════════════════════════════


# ═══ Observability: per-stage timing ══════════════════════════════════════
def _events(captured: list[dict], event: str) -> list[dict]:
    return [c for c in captured if c.get("event") == event]


async def test_tesseract_stage_timing_is_logged_per_document(fake_tesseract):
    import structlog.testing

    fake_tesseract.texts = ["First page", "Second page"]
    with structlog.testing.capture_logs() as captured:
        await ocr.run_ocr(pdf_bytes(pages=2), "application/pdf")

    stage = _events(captured, "ocr_stage_timing")
    assert len(stage) == 1
    assert stage[0]["engine"] == ocr.ENGINE_TESSERACT
    assert stage[0]["pages"] == 2
    assert stage[0]["render_s"] >= 0 and stage[0]["recognize_s"] >= 0

    doc = _events(captured, "ocr_document_timing")
    assert len(doc) == 1
    assert doc[0]["engine"] == ocr.ENGINE_TESSERACT
    assert doc[0]["total_s"] >= 0 and doc[0]["outcome"] == "ok"


async def test_document_timing_records_the_empty_outcome(fake_tesseract):
    import structlog.testing

    fake_tesseract.texts = ["~"]  # fails the OCR_EMPTY gate
    with structlog.testing.capture_logs() as captured, pytest.raises(ocr.OcrError):
        await ocr.run_ocr(png_bytes(), "image/png")

    doc = _events(captured, "ocr_document_timing")
    assert len(doc) == 1 and doc[0]["outcome"] == "empty"


async def test_timing_logs_never_contain_ocr_text(fake_tesseract):
    """Nothing here logs OCR text or extracted values — that principle must
    hold for the new timing instrumentation too, not just the existing
    warning-level logs it sits alongside."""
    import structlog.testing

    secret = "Patient Name: Ayesha Raza, Diagnosis: ZZ-SECRET-CONDITION-QX"
    fake_tesseract.texts = [secret]
    with structlog.testing.capture_logs() as captured:
        await ocr.run_ocr(png_bytes(), "image/png")

    assert secret not in json.dumps(captured, default=str)


# ═══ Failure messages ════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "code",
    [
        ocr.OCR_UNAVAILABLE,
        ocr.OCR_FAILED,
        ocr.OCR_EMPTY,
        ocr.OCR_TOO_LONG,
    ],
)
def test_every_ocr_failure_has_a_message_in_both_languages(code):
    for locale in ("en", "ur"):
        assert translate(f"errors.{code}", locale) != f"errors.{code}"
    assert "printed" in translate("errors.ocr_empty", "en")  # only printed documents are read
    # A document that failed under an earlier version still explains itself.
    assert translate("errors.ocr_model_unavailable", "en") != "errors.ocr_model_unavailable"


# ═══ The whole pipeline through the real engines ═════════════════════════
async def _login(client, email: str) -> None:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": TEST_PASSWORD})
    assert r.status_code == 200, r.text


@pytest.fixture
async def patient(client, db) -> Profile:
    user = await make_user(db, email="ocr@x.com", role=UserRole.PATIENT)
    await _login(client, "ocr@x.com")
    return user


async def _upload(client, data: bytes, mime: str, name: str, doc_type="prescription"):
    return await client.post(
        "/api/v1/documents",
        data={"document_type": doc_type},
        files={"file": (name, data, mime)},
    )


async def _job(db, doc_id) -> OcrJob:
    stmt = select(OcrJob).where(OcrJob.document_id == uuid.UUID(str(doc_id)))
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one()


@pytest.mark.parametrize(
    ("data", "mime", "name"),
    [
        (png_bytes(), "image/png", "rx.png"),
        (jpeg_bytes(), "image/jpeg", "rx.jpg"),
        (jpeg_bytes(), "image/jpeg", "rx.jpeg"),
        (pdf_bytes(pages=2), "application/pdf", "rx.pdf"),
    ],
    ids=["png", "jpg", "jpeg", "pdf"],
)
async def test_pipeline_printed_runs_upload_process_and_extraction_through_tesseract(
    client, db, patient, fake_tesseract, data, mime, name
):
    fake_tesseract.texts = [
        "Dr. Mehreen Iqbal\nTab. Metformin 500mg 1-0-1 x 30 days",
        "Cap Atorvastatin 10 mg once daily",
    ]
    up = await _upload(client, data, mime, name)
    assert up.status_code == 201 and up.json()["status"] == "uploaded"
    r = await client.post(f"/api/v1/documents/{up.json()['id']}/process")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "awaiting_review" and "ocr_mode" not in body
    assert body["rows"][0]["fields"][0]["extracted_value"] == "Metformin"

    job = await _job(db, body["id"])
    assert (job.engine, job.status) == ("tesseract", "done")
    assert len(fake_tesseract.calls) == (2 if mime == "application/pdf" else 1)


async def test_pipeline_a_pdf_that_cannot_be_read_becomes_a_retryable_failed_document(
    client, db, patient, fake_tesseract
):
    up = await _upload(client, b"%PDF-1.4 definitely not a pdf", "application/pdf", "bad.pdf")
    assert up.status_code == 201  # the bytes start like a PDF, so the upload is accepted
    r = await client.post(f"/api/v1/documents/{up.json()['id']}/process")
    assert (r.status_code, r.json()["status"], r.json()["failure_code"]) == (
        200,
        "failed",
        "ocr_failed",
    )


async def test_pipeline_without_tesseract_fails_cleanly_and_keeps_the_upload(
    client, db, patient, monkeypatch
):
    monkeypatch.setattr(ocr, "resolve_tesseract_cmd", lambda: None)
    up = await _upload(client, png_bytes(), "image/png", "rx.png")
    r = await client.post(f"/api/v1/documents/{up.json()['id']}/process")
    assert (r.json()["status"], r.json()["failure_code"]) == ("failed", "ocr_unavailable")
    stmt = select(Document).where(Document.id == uuid.UUID(up.json()["id"]))
    row = (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one()
    assert row.status == "failed"  # the original is kept so the patient can retry
    assert (await db.execute(select(Patient))).scalars().first() is not None


async def test_pipeline_a_pdf_over_the_page_limit_says_so(
    client, patient, fake_tesseract, monkeypatch
):
    monkeypatch.setattr(settings, "ocr_max_pages", 1)
    up = await _upload(client, pdf_bytes(pages=2), "application/pdf", "long.pdf")
    r = await client.post(f"/api/v1/documents/{up.json()['id']}/process")
    assert (r.json()["status"], r.json()["failure_code"]) == ("failed", "ocr_too_long")
    assert fake_tesseract.calls == []


# ═══ Real engines (skipped when not installed; they never download) ══════
def _real_tesseract_problem() -> str | None:
    cmd = ocr.resolve_tesseract_cmd()
    if cmd is None:
        return "Tesseract is not installed (not on PATH; set TESSERACT_CMD)"
    pytesseract.pytesseract.tesseract_cmd = cmd
    try:
        if "eng" not in pytesseract.get_languages(config=""):
            return "Tesseract's English language data (eng) is not installed"
    except Exception as exc:  # noqa: BLE001 - any failure to run it means "not usable here"
        return f"Tesseract could not be run: {type(exc).__name__}"
    return None


_TESSERACT_PROBLEM = _real_tesseract_problem()
requires_tesseract = pytest.mark.skipif(
    _TESSERACT_PROBLEM is not None, reason=_TESSERACT_PROBLEM or ""
)


def _font(size: int) -> ImageFont.ImageFont:
    for path in (
        r"C:\Windows\Fonts\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial.ttf",
    ):
        if os.path.isfile(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def printed_page(lines: list[str]) -> Image.Image:
    """A clean, black-on-white 'printed' page — what Tesseract is for."""
    image = Image.new("RGB", (1700, 200 + 110 * len(lines)), "white")
    draw = ImageDraw.Draw(image)
    font = _font(56)
    for index, line in enumerate(lines):
        draw.text((80, 80 + 110 * index), line, fill="black", font=font)
    return image


@requires_tesseract
@pytest.mark.parametrize(
    ("fmt", "mime", "kwargs"),
    [
        ("PNG", "image/png", {}),
        ("JPEG", "image/jpeg", {}),
        ("PDF", "application/pdf", {"resolution": 150.0}),
    ],
    ids=["png", "jpeg", "pdf"],
)
async def test_real_tesseract_reads_a_printed_prescription(fmt, mime, kwargs):
    data = as_bytes(printed_page(RX_LINES), fmt, **kwargs)
    result = await ocr.run_ocr(data, mime)
    assert result.engine == "tesseract"
    assert "metformin" in result.text.lower() and "500" in result.text
    rows = extractors.extract_prescription(result.text).rows
    assert any(r["medicine"].lower().startswith("metformin") for r in rows)


@requires_tesseract
async def test_real_tesseract_reads_a_printed_lab_report():
    lines = [
        "Chughtai Lab Report",
        "Fasting Glucose 142 mg/dL 70-100",
        "Hemoglobin 13.1 g/dL 12-15.5",
    ]
    result = await ocr.run_ocr(as_bytes(printed_page(lines), "PNG"), "image/png")
    assert "142" in result.text and "glucose" in result.text.lower()
    assert any("Glucose" in r["test_name"] for r in extractors.extract_lab(result.text).rows)


@requires_tesseract
async def test_real_tesseract_reads_a_multi_page_pdf():
    first, second = printed_page(["Page one Metformin"]), printed_page(["Page two Atorvastatin"])
    buf = io.BytesIO()
    first.save(buf, format="PDF", save_all=True, append_images=[second], resolution=150.0)
    text = (await ocr.run_ocr(buf.getvalue(), "application/pdf")).text.lower()
    assert "metformin" in text and "atorvastatin" in text


@requires_tesseract
async def test_a_blank_page_between_two_text_pages_leaves_no_stray_blank_section():
    """`TesseractEngine._transcribe_sync`'s page join (`"\\n\\n".join(page
    for page in pages if page)`) must DROP a blank page's empty string
    entirely, not join it in as an empty segment — a 3-page PDF with a
    blank middle page must read as exactly 2 page-sections, not 3 (one of
    them empty), and must not fabricate an extra blank line where the
    blank page was."""
    first = printed_page(["Page one Metformin"])
    blank = Image.new("RGB", (1700, 400), "white")
    third = printed_page(["Page three Atorvastatin"])
    buf = io.BytesIO()
    first.save(buf, format="PDF", save_all=True, append_images=[blank, third], resolution=150.0)
    result = await ocr.run_ocr(buf.getvalue(), "application/pdf")
    text = result.text.lower()
    assert "metformin" in text and "atorvastatin" in text
    sections = [s for s in result.text.split("\n\n")]
    assert len(sections) == 2  # not 3 — the blank page contributed no section at all
    assert all(s.strip() for s in sections)  # neither surviving section is itself blank


@requires_tesseract
async def test_real_tesseract_finds_no_text_on_a_blank_page():
    blank = as_bytes(Image.new("RGB", (1700, 900), "white"), "PNG")
    with pytest.raises(ocr.OcrError) as exc:
        await ocr.run_ocr(blank, "image/png")
    assert exc.value.code == ocr.OCR_EMPTY


@requires_tesseract
@pytest.mark.parametrize(
    "lines",
    [
        RX_LINES,
        [
            "Chughtai Lab   Report Date: 28/07/2026",
            "Patient Name: Ayesha Raza  Age: 32 years",
            "HbA1c 7.2 % 4.0 - 5.6",
            "Fasting Glucose 142 mg/dL 70-100",
        ],
        [
            "Dr. Mehreen Iqbal  PMDC 60021-P",
            "Patient: Ayesha Raza   Age: 32",
            "Date: 11 Aug 2026",
            "1. Tab. Metformin 500mg  1-0-1  x 30 days",
            "2) Cap Atorvastatin 10 mg once daily for 30 days",
        ],
    ],
    ids=["rx_lines", "lab_report", "full_prescription"],
)
def test_the_image_to_data_switch_keeps_exact_text_parity_with_image_to_string(lines):
    """THE gate for replacing `image_to_string` with a single `image_to_data`
    call (so one Tesseract run returns both text and per-word confidence,
    instead of two separate runs): `ocr._read_page`'s reconstructed text
    must exactly match what `image_to_string` itself would have produced
    for the SAME image, verified directly against the real binary — not
    just "close enough", byte for byte. If this ever fails, the
    reconstruction in `ocr._reconstruct_tesseract_output` has a real gap
    and must be fixed before anything relies on the new text."""
    import pytesseract as real_pytesseract

    page = printed_page(lines)
    old_text = real_pytesseract.image_to_string(page, lang="eng", config=ocr._TESSERACT_CONFIG)
    old_text = old_text.replace("\f", "\n").replace("\xa0", " ").strip()
    new_text, _ = ocr._read_page(page)
    assert new_text == old_text


@requires_tesseract
def test_a_tightly_wrapped_paragraph_also_keeps_text_parity():
    """The OTHER branch of the reconstruction (several LINES within the
    SAME paragraph, joined with a single newline, not a blank one) —
    `printed_page`'s large line spacing makes every drawn line its own
    paragraph, so this needs its own tightly-spaced image to exercise."""
    import pytesseract as real_pytesseract

    image = Image.new("RGB", (1200, 400), "white")
    draw = ImageDraw.Draw(image)
    draw.multiline_text(
        (60, 60),
        "Take 1 tablet every 6 hours\nas needed for fever or pain.\nDrink plenty of fluids.",
        fill="black",
        font=_font(48),
        spacing=14,
    )
    old_text = real_pytesseract.image_to_string(image, lang="eng", config=ocr._TESSERACT_CONFIG)
    old_text = old_text.replace("\f", "\n").replace("\xa0", " ").strip()
    new_text, _ = ocr._read_page(image)
    assert new_text == old_text


@requires_tesseract
async def test_real_tesseract_now_reports_a_real_per_line_confidence():
    """Superseded by the `image_to_data` switch: Tesseract now reports a
    real per-line confidence too, the same `LineConfidence` shape TrOCR
    uses — a single `image_to_data` call carries per-WORD confidence
    (`conf`, 0-100) alongside the text, reduced per line exactly like
    TrOCR's per-token probabilities are (see `ocr._reconstruct_tesseract_
    output`). A clean, confidently-read line scores high on both the
    geometric mean and the weakest-word minimum."""
    data = as_bytes(printed_page(["Metformin 500mg"]), "PNG")
    result = await ocr.run_ocr(data, "image/png")
    assert result.line_confidences is not None
    assert len(result.line_confidences) == len(result.text.splitlines())
    line = result.line_confidences[0]
    assert line is not None
    assert line.geo_mean > 0.8
    assert line.min_prob > 0.5


# ── Image modes that used to read as an empty page ───────────────────────
def _transparent_png(page: Image.Image) -> Image.Image:
    grey = np.asarray(page.convert("L"))
    rgba = np.zeros((*grey.shape, 4), dtype=np.uint8)
    rgba[..., 3] = 255 - grey  # black writing on a transparent background
    return Image.fromarray(rgba, "RGBA")


def _sixteen_bit(page: Image.Image) -> Image.Image:
    return Image.fromarray(np.asarray(page.convert("L")).astype(np.uint16) * 257)


@requires_tesseract
@pytest.mark.parametrize("make", [_transparent_png, _sixteen_bit], ids=["transparent", "16-bit"])
async def test_real_tesseract_reads_a_transparent_png_and_a_16_bit_scan(make):
    """A plain greyscale conversion dropped the alpha channel (the "paper" turned black and the
    writing vanished into it) and clipped 16-bit values to white: both came back as a blank page
    and the document failed with `ocr_empty`."""
    data = as_bytes(make(printed_page(RX_LINES)), "PNG")
    result = await ocr.run_ocr(data, "image/png")
    assert "metformin" in result.text.lower() and "500" in result.text
