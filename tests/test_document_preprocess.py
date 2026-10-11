"""The preprocessing report (`ocr.describe_preprocessing`): what the OCR engine
receives after preprocessing. It reuses the engine's own page preparation, so
these tests check it describes THAT — not a parallel re-implementation — plus
the diagnostics that make it useful (what was changed, what looks wrong)."""

from __future__ import annotations

import io

import pytest
from app.documents import ocr
from app.settings import settings
from PIL import Image, ImageDraw

from tests.test_document_ocr import as_bytes


def _text_page(size=(1800, 1200)) -> Image.Image:
    page = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(page)
    for row in range(8):
        draw.rectangle((80, 100 + row * 90, 900, 130 + row * 90), fill="black")
    return page


async def test_a_report_describes_the_page_the_reader_receives():
    report = await ocr.describe_preprocessing(as_bytes(_text_page(), "PNG"), "image/png")

    assert (report.engine, report.from_pdf) == ("tesseract", False)
    (page,) = report.pages
    assert page.index == 1
    assert page.original_size == (1800, 1200) and page.final_size == (1800, 1200)
    assert page.scale == pytest.approx(1.0)
    assert page.contrast > 200 and 0.05 < page.ink_fraction < 0.5
    assert page.notes == [] and page.preview_b64


async def test_the_page_shown_is_exactly_the_prepared_page_the_engine_gets():
    data = as_bytes(_text_page((900, 600)), "PNG")
    report = await ocr.describe_preprocessing(data, "image/png")
    (prepared,) = ocr._pages(data, "image/png")
    assert report.pages[0].final_size == prepared.size


async def test_a_small_image_is_reported_as_enlarged_with_the_factor():
    report = await ocr.describe_preprocessing(as_bytes(_text_page((750, 500)), "PNG"), "image/png")
    page = report.pages[0]
    assert page.original_size == (750, 500) and max(page.final_size) >= 1500
    assert page.scale == pytest.approx(2.0, abs=0.01)
    (note,) = [n for n in page.notes if n.code == "upscaled"]
    assert note.params["factor"] == "2.0"


async def test_a_rotated_photo_reports_its_exif_flag_and_the_displayed_size():
    image = _text_page((1800, 1200))
    exif = image.getexif()
    exif[274] = 6  # "rotate 90": width and height swap once applied
    buf = io.BytesIO()
    image.save(buf, format="JPEG", exif=exif)

    report = await ocr.describe_preprocessing(buf.getvalue(), "image/jpeg")

    page = report.pages[0]
    assert page.original_size == (1200, 1800) and page.final_size == (1200, 1800)
    assert any(n.code == "exif_rotated" for n in page.notes)


async def test_a_blank_page_is_called_blank():
    blank = Image.new("RGB", (1700, 900), "white")
    report = await ocr.describe_preprocessing(as_bytes(blank, "PNG"), "image/png")
    assert next(n.code for n in report.pages[0].notes) == "blank_page"


async def test_a_faint_page_is_flagged_low_contrast():
    faint = Image.new("RGB", (1800, 1200), (235, 235, 235))
    ImageDraw.Draw(faint).rectangle((100, 100, 1500, 200), fill=(205, 205, 205))
    report = await ocr.describe_preprocessing(as_bytes(faint, "PNG"), "image/png")
    assert any(n.code == "low_contrast" for n in report.pages[0].notes)


async def test_a_sparse_but_real_page_is_not_mistaken_for_a_blank_one():
    sparse = Image.new("RGB", (1700, 900), "white")
    ImageDraw.Draw(sparse).line([(100, 100 + k * 6) for k in range(60)], fill="black", width=4)
    report = await ocr.describe_preprocessing(as_bytes(sparse, "PNG"), "image/png")
    assert not any(n.code in ("blank_page", "low_contrast") for n in report.pages[0].notes)


async def test_a_pdf_reports_each_rendered_page_and_its_dpi():
    report = await ocr.describe_preprocessing(_two_page_pdf(), "application/pdf")
    assert report.from_pdf is True and report.render_dpi == settings.ocr_pdf_dpi
    assert [p.index for p in report.pages] == [1, 2]
    assert all(p.original_size is None and p.scale is None for p in report.pages)


async def test_an_unreadable_file_fails_with_the_same_code_a_real_read_would():
    with pytest.raises(ocr.OcrError) as exc:
        await ocr.describe_preprocessing(b"not an image", "image/png")
    assert exc.value.code == ocr.OCR_FAILED


async def test_the_report_never_contains_recognised_text_or_calls_an_engine(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("an OCR engine was called while only describing preprocessing")

    monkeypatch.setattr(ocr, "get_engine", boom)
    report = await ocr.describe_preprocessing(as_bytes(_text_page(), "PNG"), "image/png")
    assert not hasattr(report.pages[0], "text")


def _two_page_pdf() -> bytes:
    pages = [_text_page((800, 600)), _text_page((800, 600))]
    buf = io.BytesIO()
    pages[0].save(buf, format="PDF", save_all=True, append_images=pages[1:], resolution=100.0)
    return buf.getvalue()
