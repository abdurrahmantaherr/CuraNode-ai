"""Photo quality (`ocr.assess_quality`) — should the patient be asked for a clearer picture?

Pure image tests: pages of printed text are rendered and degraded in known ways, so every
expected verdict is a fact about the image. The thresholds themselves were measured against
how much of a page Tesseract still reads (see the comment above `ocr.QUALITY_BLANK`); these
tests pin the behaviour with a wide margin either side of them, so they do not turn on a
decimal place."""

from __future__ import annotations

import io

import numpy as np
import pytest
from app.documents import ocr
from PIL import Image, ImageFilter

from tests import document_fixtures as fx


async def assess(image: Image.Image, fmt: str = "PNG", mime: str = "image/png", **options):
    return await ocr.assess_quality(fx.image_bytes(image, fmt, **options), mime)


def pdf_bytes(*pages: Image.Image) -> bytes:
    buf = io.BytesIO()
    first, *rest = (page.convert("RGB") for page in pages)
    first.save(buf, format="PDF", resolution=150.0, save_all=True, append_images=rest)
    return buf.getvalue()


async def test_a_sharp_page_needs_no_advice():
    report = await assess(fx.text_page())
    assert report.ok and report.issues == () and report.measured
    assert report.sharpness > ocr._BLURRY_BELOW + 0.05  # clear of the line, not on it
    assert report.contrast > 150


@pytest.mark.parametrize(
    "make",
    [
        lambda page: page.filter(ImageFilter.GaussianBlur(3)),
        lambda page: page.filter(ImageFilter.GaussianBlur(5)),
        lambda page: fx.motion_blurred(page, 15),
    ],
    ids=["gaussian-3px", "gaussian-5px", "motion-15px"],
)
async def test_a_blurry_page_is_reported(make):
    report = await assess(make(fx.text_page()))
    assert report.issues == (ocr.QUALITY_BLURRY,)
    assert report.sharpness < ocr._BLURRY_BELOW - 0.1


async def test_a_phone_photo_with_noise_and_compression_is_still_judged_by_its_blur():
    """The shared degraded fixture: shrunk, softened, noisy and JPEG-compressed, as a real
    photo is. Noise adds edges of its own; it must not make a soft page look sharp."""
    clean = await ocr.assess_quality(fx.render_fixture_2_png(), "image/png")
    photo = await ocr.assess_quality(fx.render_fixture_2_degraded_jpeg(), "image/jpeg")
    assert clean.ok and photo.issues == (ocr.QUALITY_BLURRY,)


async def test_the_slightest_softness_is_not_nagged_about():
    report = await assess(fx.text_page().filter(ImageFilter.GaussianBlur(0.6)))
    assert report.ok


async def test_a_jpeg_of_a_sharp_page_is_still_sharp():
    assert (await assess(fx.text_page(), "JPEG", "image/jpeg", quality=60)).ok


async def test_very_faint_ink_is_reported_but_ordinary_grey_ink_is_not():
    faint = await assess(fx.text_page(ink=238))  # ink and paper barely differ
    assert faint.issues == (ocr.QUALITY_FAINT,) and faint.contrast < ocr._FAINT_BELOW
    assert (await assess(fx.text_page(ink=110))).ok  # grey, not faint


async def test_a_blank_page_is_reported_as_blank_and_not_also_as_blurry_or_faint():
    for page in (Image.new("L", (1240, 1754), 255), Image.new("L", (1240, 1754), 128)):
        report = await assess(page)
        assert report.issues == (ocr.QUALITY_BLANK,)
        assert report.sharpness is None and report.contrast is None


async def test_a_small_image_is_reported_as_low_resolution():
    report = await assess(fx.text_page(500, 700, 12))
    assert ocr.QUALITY_LOW_RESOLUTION in report.issues
    ordinary = await assess(fx.text_page(900, 1270, 22))  # small, but not too small to read
    assert ocr.QUALITY_LOW_RESOLUTION not in ordinary.issues and ordinary.ok


async def test_a_pdf_is_measured_page_by_page():
    sharp, blurry = fx.text_page(), fx.text_page().filter(ImageFilter.GaussianBlur(4))
    assert (await ocr.assess_quality(pdf_bytes(sharp), "application/pdf")).ok
    assert (await ocr.assess_quality(pdf_bytes(blurry), "application/pdf")).issues == (
        ocr.QUALITY_BLURRY,
    )
    # one soft page among sharp ones is enough to say so
    mixed = await ocr.assess_quality(pdf_bytes(sharp, blurry), "application/pdf")
    assert mixed.issues == (ocr.QUALITY_BLURRY,)


async def test_a_blank_back_page_is_normal_but_an_entirely_blank_document_is_not():
    blank = Image.new("RGB", (1240, 1754), "white")
    assert (await ocr.assess_quality(pdf_bytes(fx.text_page(), blank), "application/pdf")).ok
    assert (await ocr.assess_quality(pdf_bytes(blank, blank), "application/pdf")).issues == (
        ocr.QUALITY_BLANK,
    )


async def test_a_file_that_is_not_an_image_cannot_be_measured():
    with pytest.raises(ocr.OcrError):
        await ocr.assess_quality(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "image/png")
    with pytest.raises(ocr.OcrError):
        await ocr.assess_quality(b"%PDF-1.4 not really", "application/pdf")


async def test_issues_come_back_in_a_fixed_order():
    report = await assess(fx.text_page(500, 700, 12).filter(ImageFilter.GaussianBlur(3)))
    assert list(report.issues) == [code for code in ocr._QUALITY_ORDER if code in report.issues]
    assert {ocr.QUALITY_BLURRY, ocr.QUALITY_LOW_RESOLUTION} <= set(report.issues)


def test_a_report_survives_the_cache_and_ignores_what_it_does_not_know():
    report = ocr.QualityReport(issues=(ocr.QUALITY_BLURRY,), sharpness=0.2, contrast=80.0)
    assert ocr.QualityReport.from_dict(report.to_dict()) == report
    stored = {"issues": ["blurry", "<script>", 7], "sharpness": 0.2, "contrast": 80.0}
    # cached data is data: an unknown code is dropped, never rendered
    assert ocr.QualityReport.from_dict(stored).issues == (ocr.QUALITY_BLURRY,)
    assert not ocr.QualityReport().measured and ocr.QualityReport().ok


# ── A short page is a sharp page ─────────────────────────────────────────
# The first version of the measure took a percentile over ALL pixels, which on a page with little
# writing lands on blank paper: a sharp 6-line certificate scored 0.075 and a 1-line page 0.0
# (threshold 0.40), so a patient with a short, perfectly good photo was told to retake it.
SHORT_PAGES = [
    (6, 30, 1240, 1754),
    (4, 30, 1240, 1754),
    (2, 30, 1240, 1754),
    (1, 30, 1240, 1754),
    (1, 22, 1240, 1754),
    (2, 44, 1240, 1754),
    (3, 30, 2480, 3508),  # a few lines on a big, mostly empty sheet
    (3, 56, 2480, 3508),
]


@pytest.mark.parametrize(("lines", "font_px", "width", "height"), SHORT_PAGES)
async def test_a_short_sharp_page_is_not_called_blurry(lines, font_px, width, height):
    report = await assess(fx.text_page(width, height, font_px, lines=lines))
    assert ocr.QUALITY_BLURRY not in report.issues, report
    assert report.ok and report.sharpness > ocr._BLURRY_BELOW + 0.05  # clear of the line


@pytest.mark.parametrize(
    ("lines", "font_px", "width", "height"), SHORT_PAGES[:3] + SHORT_PAGES[6:7]
)
async def test_a_short_blurry_page_is_still_called_blurry(lines, font_px, width, height):
    page = fx.text_page(width, height, font_px, lines=lines)
    report = await assess(page.filter(ImageFilter.GaussianBlur(3 * width / 1240)))
    assert report.issues == (ocr.QUALITY_BLURRY,)
    assert report.sharpness < ocr._BLURRY_BELOW - 0.1


async def test_blur_is_found_on_a_dense_page_too():
    """The other half of the same lesson: capping the sample at a few steep edges let a few
    surviving sharp ones hide moderate blur on a dense page. A 2 px blur is flagged."""
    dense = fx.text_page()
    assert (await assess(dense.filter(ImageFilter.GaussianBlur(2)))).issues == (ocr.QUALITY_BLURRY,)
    assert (await assess(dense.filter(ImageFilter.GaussianBlur(0.6)))).ok


async def test_a_page_with_almost_nothing_on_it_is_not_judged_blurry():
    """A page of one tiny stamp has too few edges to say anything about: it is not called
    blurry (and not blank either — there is ink)."""
    page = Image.new("L", (1240, 1754), 255)
    page.paste(0, (600, 800, 640, 806))
    report = await assess(page)
    assert ocr.QUALITY_BLURRY not in report.issues


async def test_only_the_first_pages_of_a_long_pdf_are_measured():
    sharp, blurry = fx.text_page(), fx.text_page().filter(ImageFilter.GaussianBlur(4))
    first_pages = await ocr.assess_quality(pdf_bytes(blurry, sharp, sharp), "application/pdf")
    assert first_pages.issues == (ocr.QUALITY_BLURRY,)
    # a blurry page beyond the first few is not looked at: judging a handful says as much as all
    late = await ocr.assess_quality(pdf_bytes(sharp, sharp, sharp, blurry), "application/pdf")
    assert late.ok


# ── Every image mode is read as the page it is ───────────────────────────
def _transparent(page: Image.Image) -> Image.Image:
    """Black writing on a TRANSPARENT background (an exported PNG): alpha carries the ink."""
    grey = np.asarray(page.convert("L"))
    rgba = np.zeros((*grey.shape, 4), dtype=np.uint8)
    rgba[..., 3] = 255 - grey
    return Image.fromarray(rgba, "RGBA")


def _sixteen_bit(page: Image.Image) -> Image.Image:
    return Image.fromarray(np.asarray(page.convert("L")).astype(np.uint16) * 257)


@pytest.mark.parametrize(
    "make",
    [
        _transparent,
        _sixteen_bit,
        lambda page: page.convert("P"),
        lambda page: page.convert("1"),
        lambda page: page.convert("CMYK"),
        lambda page: page.convert("LA"),
    ],
    ids=["transparent-png", "16-bit-grey", "palette", "1-bit", "cmyk", "grey-alpha"],
)
async def test_an_unusual_image_mode_is_a_page_of_writing_not_a_blank_one(make):
    """A plain greyscale conversion read a transparent-background PNG and a 16-bit scan as an
    empty page (the alpha was dropped; values above 255 were clipped to white)."""
    image = make(fx.text_page())
    fmt, mime = ("JPEG", "image/jpeg") if image.mode == "CMYK" else ("PNG", "image/png")
    buf = io.BytesIO()
    image.save(buf, format=fmt)
    report = await ocr.assess_quality(buf.getvalue(), mime)
    assert ocr.QUALITY_BLANK not in report.issues and report.ok, (image.mode, report)
    prepared = ocr._prepare(image)
    assert prepared.mode == "L" and np.asarray(prepared).min() < 60 < np.asarray(prepared).max()


def test_an_ordinary_image_is_prepared_exactly_as_before():
    """The conversion fix must not change a single pixel of the files that already worked."""
    page = fx.text_page(900, 1200, 24)
    for mode in ("L", "RGB"):
        image = page.convert(mode)
        before = ocr.ImageOps.exif_transpose(image).convert("L")
        # `_prepare` also enlarges: compare at the size the reader receives
        if max(before.size) < ocr._MIN_LONG_SIDE_PX:
            factor = ocr._MIN_LONG_SIDE_PX / max(before.size)
            before = before.resize(
                (round(before.width * factor), round(before.height * factor)), Image.LANCZOS
            )
        assert np.array_equal(np.asarray(ocr._prepare(image)), np.asarray(before)), mode
