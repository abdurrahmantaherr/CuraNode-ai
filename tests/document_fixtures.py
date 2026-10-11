"""Shared regression fixtures for the OCR/extraction accuracy work — real
failure modes, reduced to reproducible, fully synthetic inputs with known
ground truth. Used by both the pytest baseline tests and the standalone
accuracy harness (`backend/ops/scripts/ocr_accuracy_harness.py`).

Nothing here is a real patient record. Fixture 1's text matches
`test_document_extractors.py`'s own `GARBLED_MARKER_RX` (the same garbled-
marker corruption pattern, documented there independently) rather than a
scanned image, so the extraction/normalization/confidence stages can be
tested precisely, without real-OCR-engine randomness in the loop. Fixture 2
is a freshly PIL-rendered synthetic clinic letterhead/prescription, in the
SAME style every other OCR test image in this repo already uses (see
`printed_page()`/`writing_page()` in `tests/test_document_ocr.py`) — no
binary image file is committed, and no claim is made that it reproduces any
specific OCR engine's exact character-level misreadings pixel-for-pixel;
it reproduces the STRUCTURE and CONTENT of the real failure report, and
whatever a real engine actually does with it is reported as observed, not
assumed.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont


@dataclass(frozen=True)
class MedicineGroundTruth:
    medicine: str
    dosage: str
    frequency: str
    duration: str


@dataclass(frozen=True)
class SectionGroundTruth:
    """Matches `sections.py`'s own key vocabulary, so a fixture's ground
    truth can be compared directly against `DocumentSections`."""

    facility: dict[str, str] = field(default_factory=dict)
    patient: dict[str, str] = field(default_factory=dict)
    provider: dict[str, str] = field(default_factory=dict)
    report: dict[str, str] = field(default_factory=dict)


# ═══ Fixture 1 — handwriting-style prescription (garbled list markers) ════
# Ground truth per the real failure report. Quantity is deliberately NOT
# part of this ground truth: the current schema (`extractors.FIELDS`) has
# no "quantity" field at all, and the field-level accuracy check this task
# asks for is explicitly scoped to medicine/dose/frequency/duration only.
FIXTURE_1_TRUTH: list[MedicineGroundTruth] = [
    MedicineGroundTruth("Paracetamol", "500 mg", "every 6 hours", ""),
    MedicineGroundTruth("Amoxicillin", "500 mg", "three times daily", "5 days"),
    MedicineGroundTruth("Vitamin D3", "1000 IU", "once daily", ""),
    MedicineGroundTruth("Cetirizine", "10 mg", "at night", ""),
]
# Instructions the current schema has nowhere to put ("as needed", "after
# meal") — tracked here so a later step that adds a notes/instructions
# field has something to check against; not part of today's 4-field schema.
FIXTURE_1_LOST_INSTRUCTIONS = ["as needed", "after meal"]

FIXTURE_1_TEXT = """( D Paracetamol 500 mg
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


# ═══ Fixture 2 — printed form with handwritten-style fields ═══════════════
FIXTURE_2_TRUTH: list[MedicineGroundTruth] = [
    MedicineGroundTruth("Amoxicillin", "500 mg", "three times daily", "5 days"),
    MedicineGroundTruth("Ibuprofen", "400 mg", "twice daily", ""),
    MedicineGroundTruth("Omeprazole", "20 mg", "once daily", ""),
    MedicineGroundTruth("Vitamin C", "500 mg", "once daily", ""),
    # PRN/conditional ("if fever or pain, max 3 per day") — not a fixed-
    # schedule phrase this pipeline is designed to capture; left blank in
    # ground truth rather than asserting a specific expected reading.
    MedicineGroundTruth("Paracetamol", "500 mg", "", ""),
]
FIXTURE_2_SECTIONS = SectionGroundTruth(
    facility={
        # The letterhead itself is rendered in caps — `sections.py` never
        # normalises case (never guesses), so the ground truth must match
        # the literal rendered text, not a "nicer" title-cased version.
        "hospital_name": "SHEIKH FAMILY CLINIC",
        "phone": "042-3587 2299",
        "email": "info@sheikhfamilyclinic.pk",
    },
    patient={
        "patient_name": "Muhammad Usman",
        "age": "28",
        "gender": "Male",
        "address": "House No. 22, Johar Town, Lahore",
        "patient_id": "2026-3175",
    },
    provider={
        "doctor_name": "Dr. Ayesha Malik",
        "qualification": "MBBS, FCPS",
        "specialization": "Internal Medicine",
    },
    report={"prescription_date": "15-08-2026"},
)
FIXTURE_2_NOTES = [
    "Drink plenty of water.",
    "Complete the full course of antibiotics.",
    "If symptoms persist or worsen, please visit again.",
]


def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [r"C:\Windows\Fonts\arialbd.ttf"] if bold else [r"C:\Windows\Fonts\arial.ttf"]
    candidates += ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/Library/Fonts/Arial.ttf"]
    for path in candidates:
        if os.path.isfile(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def render_fixture_2_png() -> bytes:
    """A synthetic clinic-letterhead prescription: facility header, a
    Patient/Date/Age-Gender/Address/Patient-ID block, a numbered Rx list,
    a Notes section, and a doctor signature block — the same overall shape
    as the real failure report, built fresh with PIL like every other OCR
    fixture image in this repo."""
    width, height = 1050, 1500
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font, label_font, body_font = _font(34, bold=True), _font(22), _font(24)

    y = 40
    draw.text((60, y), "SHEIKH FAMILY CLINIC", fill="black", font=title_font)
    y += 44
    draw.text((60, y), "Your Health Our Priority", fill="black", font=label_font)
    y += 30
    draw.text(
        (60, y),
        "General Medicine | Family Medicine | Preventive Care",
        fill="black",
        font=label_font,
    )
    y += 30
    draw.text((60, y), "Plot # 12, Street 4, DHA Phase 6, Lahore", fill="black", font=label_font)
    y += 28
    draw.text(
        (60, y),
        "Phone: 042-3587 2299   Email: info@sheikhfamilyclinic.pk",
        fill="black",
        font=label_font,
    )
    y += 50

    draw.text((60, y), "Patient Name : Muhammad Usman", fill="black", font=body_font)
    draw.text((620, y), "Date : 15-08-2026", fill="black", font=body_font)
    y += 34
    draw.text((60, y), "Age / Gender : 28 years / Male", fill="black", font=body_font)
    draw.text((620, y), "Patient ID : 2026-3175", fill="black", font=body_font)
    y += 34
    draw.text((60, y), "Address : House No. 22, Johar Town, Lahore", fill="black", font=body_font)
    y += 60

    draw.text((60, y), "Rx", fill="black", font=title_font)
    y += 60
    for index, m in enumerate(FIXTURE_2_TRUTH, 1):
        line1 = f"{index}. {m.medicine}  {m.dosage}"
        draw.text((80, y), line1, fill="black", font=body_font)
        y += 32
        instr = {
            "Amoxicillin": "Take 1 capsule three times daily for 5 days.",
            "Ibuprofen": "Take 1 tablet twice daily after meals.",
            "Omeprazole": "Take 1 capsule once daily in the morning.",
            "Vitamin C": "Take 1 tablet once daily.",
            "Paracetamol": "Take 1 tablet if fever or pain (max 3 per day).",
        }[m.medicine]
        draw.text((100, y), instr, fill="black", font=body_font)
        y += 50

    y += 10
    draw.text((60, y), "Notes:", fill="black", font=body_font)
    y += 32
    for note in FIXTURE_2_NOTES:
        draw.text((80, y), f"- {note}", fill="black", font=body_font)
        y += 30

    y += 60
    draw.text((620, y), "Dr. Ayesha Malik", fill="black", font=body_font)
    y += 30
    draw.text((620, y), "MBBS, FCPS (Internal Medicine)", fill="black", font=body_font)
    y += 30
    draw.text((620, y), "Consultant Physician", fill="black", font=body_font)

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def render_fixture_2_degraded_jpeg() -> bytes:
    """The same document as `render_fixture_2_png`, but degraded the way a
    real phone photo of a printed page actually degrades real OCR: a
    downscale/upscale resampling pass (loses real detail no amount of
    sharpening recovers), a slight rotation, gaussian blur, per-pixel
    noise, and a lossy JPEG re-encode. A cleanly-rendered PNG (what
    `render_fixture_2_png` alone produces) reads back essentially
    perfectly through Tesseract — real scans don't, and this fixture
    exists specifically to exercise that gap.

    This is NOT a claim that it reproduces any specific original photo's
    exact corruption byte-for-byte — it won't, and doesn't try to. What it
    reliably reproduces is the SAME CLASS of failure a real scan causes in
    this pipeline: a label whose separator degrades past what `sections.py`
    recognises causes the PRECEDING field to swallow the rest of the line
    (observed here: patient_name lost entirely, gender absorbing a
    corrupted "Patient ID" into itself) — the same mechanism behind the
    original report's "gender followed by garbage" symptom, even though
    the exact garbage differs. Degradation parameters were hand-tuned
    (see Step 0 of the OCR/extraction accuracy work) to land in a
    realistic middle ground: severe enough to reproduce real failure
    classes, not so severe that nothing recognisable survives. The RNG is
    seeded, so this fixture is exactly reproducible across runs.
    """
    image = Image.open(io.BytesIO(render_fixture_2_png())).convert("RGB")
    width, height = image.size
    image = image.resize((int(width * 0.6), int(height * 0.6)), Image.BILINEAR).resize(
        (width, height), Image.BILINEAR
    )
    image = image.rotate(1.5, expand=True, fillcolor="white")
    image = image.filter(ImageFilter.GaussianBlur(radius=1.3))
    array = np.array(image).astype(np.int16)
    noise = np.random.default_rng(0).normal(0, 9, array.shape)
    array = np.clip(array + noise, 0, 255).astype(np.uint8)
    image = Image.fromarray(array)
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=45)
    return buf.getvalue()


# ── Synthetic photos of a printed page, for the photo-quality check ──────
# `ocr.assess_quality` decides whether a patient should be asked for a clearer picture. These
# render a page of printed lines and degrade it in known ways, so the measure can be tested
# against pages whose condition is a fact rather than an opinion.
_QUALITY_PAGE_LINES = [
    "City Health Clinic",
    "Patient Name: Ayesha Khan    Date: 27-09-2026",
    "1. Tab. Metformin 500 mg 1-0-1 x 30 days",
    "2. Cap Atorvastatin 10 mg once daily for 30 days",
    "3. Tab. Paracetamol 500 mg 1-1-1 x 3 days",
    "Advice: drink plenty of water and rest",
    "Dr. Sara Ahmed MBBS FCPS",
] * 4


def text_page(
    width: int = 1240,
    height: int = 1754,
    font_px: int = 30,
    ink: int = 15,
    paper: int = 255,
    lines: int | None = None,
) -> Image.Image:
    """A sharp greyscale page of printed lines (A4 at 150 dpi by default). `lines` keeps only
    the first few — a SHORT document (a certificate, a vaccination card) on the same big sheet."""
    image = Image.new("L", (width, height), paper)
    draw = ImageDraw.Draw(image)
    font = _font(font_px)
    for index, line in enumerate(_QUALITY_PAGE_LINES[:lines]):
        draw.text((70, 70 + index * (font_px + 24)), line, fill=ink, font=font)
    return image


def motion_blurred(image: Image.Image, length: int) -> Image.Image:
    """The page smeared sideways, as by a hand that moved while the shutter was open."""
    pixels = np.asarray(image.convert("L"), dtype=np.float32)
    total = np.zeros_like(pixels)
    for step in range(length):
        total += np.roll(pixels, step - length // 2, axis=1)
    return Image.fromarray((total / length).astype(np.uint8))


def image_bytes(image: Image.Image, fmt: str = "PNG", **options: object) -> bytes:
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format=fmt, **options)
    return buf.getvalue()
