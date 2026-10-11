"""The one OCR step every document type shares.

`run_ocr` is the only entry point the pipeline uses. It returns plain text;
nothing here knows about prescriptions or lab reports. OCR is Tesseract and it
is completely local, free and keyless — no document ever leaves the machine
(`ocr_job.engine = 'tesseract'`). Handwritten documents are deliberately not
supported: only printed text is read.

Inputs are JPEG, PNG and PDF; a PDF is rendered to images locally with
pypdfium2 (no external PDF tool), one image per page, under one shared set of
limits (page cap, pixel cap). Blocking work runs on a small thread pool so it
never stalls the event loop.

Uploaded documents are attacker-controlled input. The engine only decodes them
to pixels and transcribes; the output is stored and shown as text (escaped by the
templates) and read by deterministic extractors — never executed, never fed back
as instructions, and never trusted as a value until the patient confirms it.

No OCR success is ever faked: if Tesseract is not installed, its language data is
unavailable, or a file cannot be read, `OcrError` is raised with a specific
`code` and the document is marked `failed` for the patient to retry or discard.
"""

from __future__ import annotations

import asyncio
import base64
import io
import math
import os
import shutil
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
from itertools import islice
from typing import Any, Protocol

import numpy as np
import pypdfium2 as pdfium
import pytesseract
from PIL import Image, ImageOps, UnidentifiedImageError

from ..log_config import get_logger
from ..settings import settings

log = get_logger()

# Failure codes stored in `document.failure_code` and mapped to messages in the
# i18n catalogue as `errors.<code>`.
OCR_UNAVAILABLE = "ocr_unavailable"
OCR_FAILED = "ocr_failed"
OCR_EMPTY = "ocr_empty"
OCR_TOO_LONG = "ocr_too_long"

ENGINE_TESSERACT = "tesseract"

# Where the official Windows installer puts Tesseract when it is not on PATH.
_WINDOWS_DEFAULT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
# Page segmentation 3 (fully automatic) suits a full page of printed text.
_TESSERACT_CONFIG = "--psm 3"
# How Tesseract should read the page. "page" (the default, above) suits running text and
# letterheads. "table" (page segmentation 4: a single column of variable-size text,
# read row by row) keeps each row of a results table together: the default reads such a
# table column by column, so a lab report's Reference Range column comes out as a
# separate block far from its rows (and a decimal point can be lost on the way).
# Nothing here knows WHICH documents are tables — the caller decides and may ask again.
LAYOUT_PAGE = "page"
LAYOUT_TABLE = "table"
_TESSERACT_CONFIGS = {LAYOUT_PAGE: _TESSERACT_CONFIG, LAYOUT_TABLE: "--psm 4"}
# Messages from Tesseract that mean "not set up" rather than "this file is bad".
_NOT_CONFIGURED_MARKERS = (
    "failed loading language",
    "error opening data file",
    "could not initialize tesseract",
)
# Tesseract reads small type badly; a page whose long side is below this is
# upscaled (a phone photo or a 300-dpi scan is already well above it).
_MIN_LONG_SIDE_PX = 1500
# Output that has fewer letters/digits than this is noise, not a document.
_MIN_ALNUM_CHARS = 3

# OCR is CPU-bound; two workers keep a busy server responsive.
_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ocr")


class OcrError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class LineConfidence:
    """How confidently Tesseract read ONE line of `OcrResult.text` — never
    used to change what text is shown, only as a signal for later (e.g. a
    reviewer's badge). `geo_mean` is the geometric mean of the line's own
    words' confidences; `min_prob` is the single weakest word in the line,
    which a geometric mean alone can hide (one badly-read word pulls
    `min_prob` down sharply while barely moving a longer line's geometric
    mean)."""

    geo_mean: float
    min_prob: float


@dataclass(frozen=True)
class OcrResult:
    text: str
    engine: str
    # One entry per line of `text.splitlines()`, in the same order — `None`
    # for a line with nothing to score (a blank separator between pages,
    # or a line this run didn't produce a confidence for). `None` entirely
    # (not a list) for an engine that has no per-line confidence. Additive
    # and backward compatible: code reading only `.text`/`.engine` is
    # unaffected.
    line_confidences: list[LineConfidence | None] | None = None


class OcrEngine(Protocol):
    name: str

    async def transcribe(
        self, data: bytes, mime_type: str
    ) -> tuple[str, list[LineConfidence | None] | None]: ...


# ── Pages -> images ──────────────────────────────────────────────────────
def _to_grey(image: Image.Image) -> Image.Image:
    """Greyscale — dark ink on light paper — whatever mode the file is in. A plain
    `convert("L")` reads two ordinary kinds of file as an empty page: a PNG whose background is
    TRANSPARENT (it drops the alpha channel, so the "paper" turns black and the writing, if it
    was drawn black, vanishes into it) and a 16-bit greyscale scan (values above 255 are
    clipped to white). Transparency is flattened onto white paper; 16-bit and float data are
    stretched across the 0-255 range. Every other mode is converted exactly as before."""
    mode = image.mode
    if mode in ("RGBA", "LA", "PA") or (mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        paper = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(paper, rgba).convert("L")
    if mode in ("I", "F") or mode.startswith("I;16"):
        pixels = np.asarray(image, dtype=np.float32)
        low, high = float(pixels.min()), float(pixels.max())
        if low >= 0 and high <= 255:
            return Image.fromarray(pixels.astype(np.uint8), "L")
        stretched = (pixels - low) * (255.0 / max(high - low, 1.0))
        return Image.fromarray(stretched.astype(np.uint8), "L")
    return image.convert("L")


def _prepare(image: Image.Image) -> Image.Image:
    """Upright (EXIF), greyscale, and large enough to read."""
    image = _to_grey(ImageOps.exif_transpose(image))
    long_side = max(image.size)
    if long_side < _MIN_LONG_SIDE_PX:
        # A continuous scale factor, not an integer multiplier: an image
        # 1px under the floor should land just over it (~1510px), not be
        # needlessly doubled (~3000px) for a 1px shortfall — `math.ceil` on
        # the multiplier itself was rounding the TARGET SIZE up to the next
        # whole multiple of the original size, not up to the floor.
        factor = _MIN_LONG_SIDE_PX / long_side
        image = image.resize(
            (round(image.width * factor), round(image.height * factor)), Image.LANCZOS
        )
    return image


def _image_pages(data: bytes) -> Iterator[Image.Image]:
    try:
        image = Image.open(io.BytesIO(data))
        # `open` only reads the header, so the size can be checked before the
        # pixels are ever decoded (a small file can hold a huge bitmap).
        if image.width * image.height > settings.ocr_max_pixels:
            raise OcrError(OCR_FAILED)
        image.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise OcrError(OCR_FAILED) from exc
    yield _prepare(image)


def _pdf_pages(data: bytes) -> Iterator[Image.Image]:
    try:
        pdf = pdfium.PdfDocument(data)
    except (pdfium.PdfiumError, ValueError, TypeError) as exc:
        # Corrupt, or password-protected: either way it cannot be read.
        raise OcrError(OCR_FAILED) from exc
    try:
        if len(pdf) == 0:
            raise OcrError(OCR_EMPTY)
        if len(pdf) > settings.ocr_max_pages:
            raise OcrError(OCR_TOO_LONG)
        base_scale = settings.ocr_pdf_dpi / 72  # PDF points are 1/72 inch
        for index in range(len(pdf)):
            page = pdf[index]
            try:
                width, height = page.get_size()
                # Never render a page bigger than the pixel cap (a poster-size
                # page would otherwise be enormous at 300 dpi).
                scale = min(base_scale, math.sqrt(settings.ocr_max_pixels / (width * height)))
                yield _prepare(page.render(scale=scale).to_pil())
            except pdfium.PdfiumError as exc:
                raise OcrError(OCR_FAILED) from exc
            finally:
                page.close()
    finally:
        pdf.close()


def _pages(data: bytes, mime_type: str) -> Iterator[Image.Image]:
    return _pdf_pages(data) if mime_type == "application/pdf" else _image_pages(data)


# ═══ Printed text: Tesseract ═════════════════════════════════════════════
def resolve_tesseract_cmd() -> str | None:
    """The Tesseract executable to run: `TESSERACT_CMD` if set, else the one on
    PATH, else the Windows installer's default location. None if not found."""
    configured = settings.tesseract_cmd
    if configured:
        return configured if os.path.isfile(configured) else shutil.which(configured)
    found = shutil.which("tesseract")
    if found:
        return found
    if os.path.isfile(_WINDOWS_DEFAULT_CMD):
        return _WINDOWS_DEFAULT_CMD
    return None


def _reconstruct_tesseract_output(
    data: dict[str, list],
) -> tuple[str, list[LineConfidence | None]]:
    """Turns `pytesseract.image_to_data(..., output_type=Output.DICT)`'s
    word-level rows into the SAME plain text `image_to_string` would have
    produced for the same image — verified byte-for-byte against the real
    Tesseract binary across a printed prescription, a lab report, and a
    tightly-wrapped paragraph (see `tests/test_document_ocr.py`) — plus a
    per-line confidence aligned to the result: one
    entry per `text.splitlines()` line, `None` for a blank paragraph-break
    line. One Tesseract run now answers both "what does it say" and "how
    confident was each word", where two separate calls (`image_to_string`
    then `image_to_data`) would have run the whole page through Tesseract
    twice for no reason.

    Tesseract's own plain-text renderer inserts a blank line between
    PARAGRAPHS (a new `(block_num, par_num)`) but only a single newline
    between LINES within the same paragraph — this mirrors that exactly
    rather than guessing a different spacing convention.
    """
    groups: dict[tuple[int, int, int], list[str]] = {}
    group_confs: dict[tuple[int, int, int], list[float]] = {}
    order: list[tuple[int, int, int]] = []
    for word, block, par, line, conf in zip(
        data["text"], data["block_num"], data["par_num"], data["line_num"], data["conf"]
    ):
        if not word.strip():
            continue
        key = (block, par, line)
        if key not in groups:
            groups[key] = []
            group_confs[key] = []
            order.append(key)
        groups[key].append(word)
        try:
            conf_f = float(conf)
        except (TypeError, ValueError):
            conf_f = -1.0
        if conf_f >= 0:
            group_confs[key].append(conf_f)

    lines: list[str] = []
    confidences: list[LineConfidence | None] = []
    prev_block_par: tuple[int, int] | None = None
    for block, par, _line in order:
        if prev_block_par is not None and (block, par) != prev_block_par:
            lines.append("")
            confidences.append(None)
        line_text = " ".join(groups[(block, par, _line)])
        lines.append(line_text.replace("\f", "\n").replace("\xa0", " "))
        confs = group_confs[(block, par, _line)]
        if confs:
            probs = [c / 100 for c in confs]
            geo_mean = math.exp(sum(math.log(max(prob, 1e-6)) for prob in probs) / len(probs))
            confidences.append(LineConfidence(geo_mean=geo_mean, min_prob=min(probs)))
        else:
            confidences.append(None)
        prev_block_par = (block, par)
    text = ("\n".join(lines) + "\n") if lines else ""
    return text.strip(), confidences


def _read_page(
    image: Image.Image, layout: str = LAYOUT_PAGE
) -> tuple[str, list[LineConfidence | None]]:
    try:
        data = pytesseract.image_to_data(
            image,
            lang=settings.tesseract_lang,
            config=_TESSERACT_CONFIGS[layout],
            timeout=settings.ocr_timeout_s,
            output_type=pytesseract.Output.DICT,
        )
    except pytesseract.TesseractNotFoundError as exc:
        log.warning("ocr_tesseract_not_found")
        raise OcrError(OCR_UNAVAILABLE) from exc
    except pytesseract.TesseractError as exc:
        if any(marker in str(exc).lower() for marker in _NOT_CONFIGURED_MARKERS):
            # Missing language data (e.g. TESSERACT_LANG=eng+urd without urd).
            log.warning("ocr_tesseract_language_unavailable", lang=settings.tesseract_lang)
            raise OcrError(OCR_UNAVAILABLE) from exc
        raise OcrError(OCR_FAILED) from exc
    except RuntimeError as exc:  # pytesseract's "Tesseract process timeout"
        raise OcrError(OCR_FAILED) from exc
    return _reconstruct_tesseract_output(data)


class TesseractEngine:
    """Local Tesseract OCR (`ocr_job.engine = 'tesseract'`) for printed text."""

    name = ENGINE_TESSERACT

    def __init__(self, layout: str = LAYOUT_PAGE) -> None:
        if layout not in _TESSERACT_CONFIGS:
            raise ValueError(f"unknown page layout: {layout!r}")
        self.layout = layout

    async def transcribe(
        self, data: bytes, mime_type: str
    ) -> tuple[str, list[LineConfidence | None] | None]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_POOL, self._transcribe_sync, data, mime_type)

    def _transcribe_sync(
        self, data: bytes, mime_type: str
    ) -> tuple[str, list[LineConfidence | None] | None]:
        cmd = resolve_tesseract_cmd()
        if cmd is None:
            log.warning("ocr_tesseract_not_found")
            raise OcrError(OCR_UNAVAILABLE)
        pytesseract.pytesseract.tesseract_cmd = cmd
        pages: list[tuple[str, list[LineConfidence | None]]] = []
        render_s = 0.0
        recognize_s = 0.0
        mark = time.monotonic()
        for image in _pages(data, mime_type):
            render_s += time.monotonic() - mark
            mark = time.monotonic()
            pages.append(_read_page(image, self.layout))
            recognize_s += time.monotonic() - mark
            mark = time.monotonic()
        log.info(
            "ocr_stage_timing",
            engine=ENGINE_TESSERACT,
            pages=len(pages),
            render_s=round(render_s, 3),
            recognize_s=round(recognize_s, 3),
        )
        # A blank page contributes no section at all (never an empty one
        # joined in) — exactly the existing page-join behaviour, extended
        # to keep `line_confidences` aligned: a `None` entry for the blank
        # line `"\n\n".join` inserts BETWEEN surviving pages.
        kept = [(text, confs) for text, confs in pages if text]
        joined_text = "\n\n".join(text for text, _ in kept)
        line_confidences: list[LineConfidence | None] = []
        for index, (_, confs) in enumerate(kept):
            if index > 0:
                line_confidences.append(None)
            line_confidences.extend(confs)
        return joined_text, line_confidences


def _otsu(values: np.ndarray) -> int:
    """Otsu's threshold for a uint8 array (dark ink vs light paper)."""
    hist = np.bincount(values.ravel(), minlength=256).astype(np.float64)
    if np.count_nonzero(hist) < 2:
        return 128
    levels = np.arange(256, dtype=np.float64)
    weight_bg = np.cumsum(hist)
    weight_fg = hist.sum() - weight_bg
    sum_bg = np.cumsum(levels * hist)
    with np.errstate(divide="ignore", invalid="ignore"):
        mean_bg = sum_bg / weight_bg
        mean_fg = (sum_bg[-1] - sum_bg) / weight_fg
        between = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
    return int(np.nanargmax(between))


# ═══ Preprocessing report ════════════════════════════════════════════════
# The stage between "upload" and "recognised text": what the engine actually
# RECEIVES after preprocessing. It re-runs the very same page preparation
# (`_pages` / `_prepare`) the engine uses — nothing is stored, so there is
# nothing to drift out of date — and describes the result: sizes, the scale
# applied, contrast, ink, and a preview image of each page. When a read goes
# wrong this is where to look first: a page that came out blank, tiny or faint
# explains it.
_PREVIEW_MAX_SIDE = 1100
# Ink-vs-paper separation (0-255) below which writing is faint enough to be
# missed; and the ink share below which a page has essentially nothing on it.
_LOW_CONTRAST = 60.0
_BLANK_INK_FRACTION = 0.00005
# Under this much ink-to-paper separation there is effectively no writing at
# all (a uniform sheet, or only scanner noise).
_BLANK_CONTRAST = 12.0
_NEAR_PIXEL_CAP = 0.8


@dataclass(frozen=True)
class PreprocessNote:
    """Something worth knowing about a page or the document. `code` is a
    stable key the page translates; `params` fill its placeholders."""

    code: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PreprocessedPage:
    index: int
    original_size: tuple[int, int] | None  # an image upload's own size; None for a PDF page
    final_size: tuple[int, int]
    scale: float | None  # None for a PDF page (rendered at a dpi, not scaled)
    megapixels: float
    contrast: float
    ink_fraction: float
    preview_b64: str
    notes: list[PreprocessNote]


@dataclass(frozen=True)
class PreprocessReport:
    engine: str
    from_pdf: bool
    render_dpi: int | None
    pages: list[PreprocessedPage]
    notes: list[PreprocessNote]


def _png_b64(image: Image.Image, max_side: int) -> str:
    preview = image.copy()
    preview.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    preview.convert("L" if preview.mode == "L" else "RGB").save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _ink_fraction(gray: np.ndarray) -> float:
    # `<=`, Otsu's own class boundary: on a perfectly two-level image the
    # threshold IS the ink level, and `<` would find no ink at all.
    return float((gray <= _otsu(gray)).mean()) if gray.size else 0.0


def _contrast(gray: np.ndarray) -> float:
    """How far apart ink and paper are: the mean paper level minus the mean ink
    level (Otsu's split). A plain standard deviation would call a clean page
    "low contrast" just because most of it is white paper."""
    threshold = _otsu(gray)
    ink, paper = gray[gray <= threshold], gray[gray > threshold]
    return float(paper.mean() - ink.mean()) if ink.size and paper.size else 0.0


def _source_facts(data: bytes, mime_type: str) -> tuple[tuple[int, int], bool, bool] | None:
    """(size as stored, EXIF rotation flag set?, flag swaps width/height?) of
    an image upload — read from the header alone, after `_pages` has already
    applied the pixel/page defences. `None` for a PDF or an unreadable
    header."""
    if mime_type == "application/pdf":
        return None
    try:
        with Image.open(io.BytesIO(data)) as source:
            orientation = source.getexif().get(274, 1) or 1
            width, height = source.size
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None
    return (width, height), orientation != 1, orientation in (5, 6, 7, 8)


def _describe_sync(data: bytes, mime_type: str) -> PreprocessReport:
    from_pdf = mime_type == "application/pdf"
    pages: list[PreprocessedPage] = []
    for index, image in enumerate(_pages(data, mime_type), start=1):
        if index == 1:
            facts = _source_facts(data, mime_type)
        gray = np.asarray(image)
        contrast = _contrast(gray)
        ink = _ink_fraction(gray)
        notes: list[PreprocessNote] = []
        original: tuple[int, int] | None = None
        scale: float | None = None
        if facts is not None:
            stored, rotated_flag, swapped = facts
            original = (stored[1], stored[0]) if swapped else stored
            scale = max(image.size) / max(original)
            if scale > 1.001:
                notes.append(PreprocessNote("upscaled", {"factor": f"{scale:.1f}"}))
            if rotated_flag:
                notes.append(PreprocessNote("exif_rotated"))
        nothing_on_page = ink < _BLANK_INK_FRACTION or contrast < _BLANK_CONTRAST
        if not nothing_on_page and contrast < _LOW_CONTRAST:
            notes.append(PreprocessNote("low_contrast"))
        if image.width * image.height > _NEAR_PIXEL_CAP * settings.ocr_max_pixels:
            notes.append(PreprocessNote("near_pixel_cap"))
        if nothing_on_page:
            notes.insert(0, PreprocessNote("blank_page"))

        pages.append(
            PreprocessedPage(
                index=index,
                original_size=original,
                final_size=image.size,
                scale=scale,
                megapixels=round(image.width * image.height / 1_000_000, 2),
                contrast=round(contrast, 1),
                ink_fraction=round(ink, 4),
                preview_b64=_png_b64(image, _PREVIEW_MAX_SIDE),
                notes=notes,
            )
        )

    log.info("ocr_preprocess_report", pages=len(pages))
    return PreprocessReport(
        engine=ENGINE_TESSERACT,
        from_pdf=from_pdf,
        render_dpi=settings.ocr_pdf_dpi if from_pdf else None,
        pages=pages,
        notes=[],
    )


async def describe_preprocessing(data: bytes, mime_type: str) -> PreprocessReport:
    """What the engine would receive for this file after preprocessing — see
    the section comment above. Raises `OcrError` for a file that cannot be
    prepared at all (the same defences and codes as a real read)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_POOL, _describe_sync, data, mime_type)


# ═══ Photo quality: should the patient take a clearer picture? ═══════════
# Tesseract reads a sharp, well-lit page well, and a blurry one badly — and a blurry page
# does not fail loudly: it yields plausible-looking WRONG text. So the page the engine would
# receive (`_pages` / `_prepare`, exactly as the preprocessing report) is measured, and a page
# that is blurry, very faint, tiny or blank is reported so the patient can be asked for a
# clearer photo or scan. This is advice only: it never blocks anything, never changes what is
# read, and every value still goes through review and Confirm & Save.
#
# The thresholds are measured, not guessed — and measured on BOTH kinds of page. Synthetic
# documents were degraded in known ways (Gaussian blur, motion blur, low resolution, heavy JPEG,
# faint ink) and each version was run through Tesseract; the share of the words it still read
# was compared with each candidate measure. The pages were dense prescriptions and lab reports
# AND sparse ones (1-6 lines on an A4 sheet, large margins, two sizes of canvas): the first
# version of this measure was calibrated on dense pages only, and read every sparse page —
# a short certificate, a vaccination card — as blurry (it took a percentile over all pixels,
# which on a page with little writing lands on blank paper). Now:
#   * sharp pages never scored below 0.51 on `_sharpness` (sparse pages 0.51-0.62, dense
#     pages 0.61-0.76) — none of 20 was flagged;
#   * Gaussian blur >= 1.4 px, motion blur >= 5 px and a soft or ~500 px photo were flagged on
#     17-20 of 20 pages — where Tesseract's read starts to fall away (Gaussian 2 px: median word
#     recall 80 %, motion 9 px: 9 %) — while 0.6 px blur (read at 90-100 %) was never flagged
#     and 1.0 px (read at 84-100 %) on one page;
#   * uniform fading barely hurt (contrast 21-24 was still read at 88-97 %), so "faint" is
#     reserved for very low contrast;
#   * below ~800 px on the long side a photo is read at 90 % (700 px) and 71 % (500 px) or less.
# The edge measure is taken per DIRECTION (the weaker of horizontal and vertical): motion blur
# leaves the edges parallel to the motion sharp, so one overall figure hides it.
# Real phone photos may sit differently from these renders — these are named constants.
QUALITY_BLANK = "blank"
QUALITY_BLURRY = "blurry"
QUALITY_FAINT = "faint"
QUALITY_LOW_RESOLUTION = "low_resolution"
_QUALITY_ORDER = (QUALITY_BLANK, QUALITY_BLURRY, QUALITY_FAINT, QUALITY_LOW_RESOLUTION)
# Edges are measured on the page scaled down to this long side, so blur is judged relative to
# the page (and so to the size of its type), not to how many pixels the camera had.
_SHARPNESS_SIDE = 1600
_BLURRY_BELOW = 0.42
_FAINT_BELOW = 25.0
_LOW_RESOLUTION_LONG_SIDE = 800
# An "edge" pixel is one whose gradient is at least this share of the ink-to-paper contrast.
_EDGE_FLOOR = 0.10
# Fewer edge pixels than this in a direction: too little writing to judge sharpness there.
_MIN_EDGE_PIXELS = 200
# The edge strength reported is this percentile of the edge pixels' gradients: the steep end of
# the page's edges, which blur flattens. (A fixed count of the very steepest pixels, tried first,
# let a few surviving sharp edges hide the blur on a dense page.)
_EDGE_PERCENTILE = 0.90
# A long PDF is judged on its first pages: the quality of a handful says as much as every page
# does, and measuring all of them would double the time a long document takes to read.
_QUALITY_MAX_PAGES = 3


@dataclass(frozen=True)
class QualityReport:
    """How well the page can be read. `issues` holds the codes above (empty = fine);
    `sharpness` and `contrast` are the weakest page's figures, `None` if no page had enough
    writing to measure."""

    issues: tuple[str, ...] = ()
    sharpness: float | None = None
    contrast: float | None = None

    @property
    def ok(self) -> bool:
        return not self.issues

    @property
    def measured(self) -> bool:
        return self.sharpness is not None or self.contrast is not None or bool(self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {"issues": list(self.issues), "sharpness": self.sharpness, "contrast": self.contrast}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QualityReport:
        issues = tuple(i for i in data.get("issues", []) if i in _QUALITY_ORDER)
        return cls(issues=issues, sharpness=data.get("sharpness"), contrast=data.get("contrast"))


def _edge_strength(gradient: np.ndarray, contrast: float) -> float | None:
    """How steep the page's edges are in one direction, relative to its ink contrast: the 90th
    percentile of the gradients of its EDGE pixels. `None` when there are too few edge pixels
    to say. (Not a percentile of ALL pixels — on a page with little writing that lands on blank
    paper and reports a sharp page as blurry.)"""
    magnitude = np.abs(gradient).ravel()
    edges = magnitude[magnitude > _EDGE_FLOOR * contrast]
    if edges.size < _MIN_EDGE_PIXELS:
        return None
    rank = int(_EDGE_PERCENTILE * (edges.size - 1))
    return float(np.partition(edges, rank)[rank]) / contrast


def _sharpness(gray: np.ndarray, contrast: float) -> float | None:
    """The weaker of the horizontal and vertical edge strengths (see `_edge_strength`), on the
    page scaled to `_SHARPNESS_SIDE`. A sharp printed page measures about 0.5-0.7, whether it
    is dense or has three lines on it; blur drives it down. `None`: too little writing to judge."""
    image = Image.fromarray(gray)
    longest = max(image.size)
    if longest > _SHARPNESS_SIDE:
        factor = _SHARPNESS_SIDE / longest
        image = image.resize(
            (round(image.width * factor), round(image.height * factor)), Image.LANCZOS
        )
    pixels = np.asarray(image, dtype=np.float32)
    gy, gx = np.gradient(pixels)
    contrast = max(contrast, 1.0)
    horizontal, vertical = _edge_strength(gx, contrast), _edge_strength(gy, contrast)
    if horizontal is None and vertical is None:
        return None
    # a direction with no strong edges left at all (heavy motion blur removes one) is the weakest
    return min(horizontal or 0.0, vertical or 0.0)


def _assess_sync(data: bytes, mime_type: str) -> QualityReport:
    sharpness: list[float] = []
    contrasts: list[float] = []
    found: set[str] = set()
    blank_pages = 0
    pages = 0
    facts = None
    with closing(_pages(data, mime_type)) as rendered:
        for index, image in enumerate(islice(rendered, _QUALITY_MAX_PAGES), start=1):
            pages += 1
            if index == 1:
                facts = _source_facts(data, mime_type)
            gray = np.asarray(image)
            contrast = _contrast(gray)
            if _ink_fraction(gray) < _BLANK_INK_FRACTION or contrast < _BLANK_CONTRAST:
                blank_pages += 1
                continue
            contrasts.append(contrast)
            if contrast < _FAINT_BELOW:
                found.add(QUALITY_FAINT)
            measured = _sharpness(gray, contrast)
            if measured is not None:
                sharpness.append(measured)
                if measured < _BLURRY_BELOW:
                    found.add(QUALITY_BLURRY)
    if pages and blank_pages == pages:
        found.add(
            QUALITY_BLANK
        )  # a blank back page of a longer document is normal; all of it is not
    if facts is not None:
        stored, _flag, swapped = facts
        original = (stored[1], stored[0]) if swapped else stored
        if max(original) < _LOW_RESOLUTION_LONG_SIDE:
            found.add(QUALITY_LOW_RESOLUTION)
    return QualityReport(
        issues=tuple(code for code in _QUALITY_ORDER if code in found),
        sharpness=round(min(sharpness), 3) if sharpness else None,
        contrast=round(min(contrasts), 1) if contrasts else None,
    )


async def assess_quality(data: bytes, mime_type: str) -> QualityReport:
    """Measure the file the way the engine would receive it. Raises `OcrError` for a file that
    cannot be prepared at all (the same defences and codes as a real read)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_POOL, _assess_sync, data, mime_type)


# ═══ The shared entry point ══════════════════════════════════════════════
def get_engine(layout: str = LAYOUT_PAGE) -> OcrEngine:
    """The OCR engine: Tesseract. (A function, not a constant, so a test can
    swap it for a fake without touching the pipeline.)"""
    return TesseractEngine(layout)


async def run_ocr(data: bytes, mime_type: str, *, layout: str = LAYOUT_PAGE) -> OcrResult:
    engine = get_engine(layout)
    start = time.monotonic()
    text, line_confidences = await engine.transcribe(data, mime_type)
    text = text.strip()
    total_s = round(time.monotonic() - start, 3)
    if sum(ch.isalnum() for ch in text) < _MIN_ALNUM_CHARS:
        log.info("ocr_document_timing", engine=engine.name, total_s=total_s, outcome="empty")
        raise OcrError(OCR_EMPTY)
    log.info("ocr_document_timing", engine=engine.name, total_s=total_s, outcome="ok")
    return OcrResult(text=text, engine=engine.name, line_confidences=line_confidences)
