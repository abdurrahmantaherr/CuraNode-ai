"""OCR accuracy harness — character/word error rate for the OCR engine
(Tesseract, printed documents), against a small set of SYNTHETIC documents with
known ground-truth text. No real patient data is used anywhere in this script.

    uv run backend/ops/scripts/ocr_accuracy_harness.py

Runs each synthetic document through the exact same `ocr.run_ocr()` entry
point the server uses (same engine selection, same page decoding, same
`OcrResult`), so a result here reflects the real pipeline, not a stand-in.
Also captures the per-stage timing this harness's Phase-1/2 sibling work
added (`ocr_stage_timing`/`ocr_document_timing` structured-log events), so
this one run gives both accuracy and timing numbers side by side.

The engine is skipped (not failed) if it isn't available in this
environment, exactly like the test suite's `requires_tesseract` marker.
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# Runnable directly: `uv run backend/ops/scripts/ocr_accuracy_harness.py`
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "backend"))

from app.documents import ai_understanding, extractors, ocr
from PIL import Image, ImageDraw, ImageFont

from tests.document_fixtures import (
    FIXTURE_1_TEXT,
    FIXTURE_1_TRUTH,
    FIXTURE_2_TRUTH,
    render_fixture_2_degraded_jpeg,
    render_fixture_2_png,
)


# ── Edit distance (dependency-free; no new package for a dev-only script) ──
def _edit_distance(a: Sequence, b: Sequence) -> int:
    """Classic Levenshtein distance, generic over characters or words."""
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            current[j] = min(
                previous[j] + 1,  # deletion
                current[j - 1] + 1,  # insertion
                previous[j - 1] + (ca != cb),  # substitution
            )
        previous = current
    return previous[-1]


def _cer(hypothesis: str, reference: str) -> float:
    if not reference:
        return 0.0 if not hypothesis else 1.0
    return _edit_distance(hypothesis, reference) / len(reference)


def _wer(hypothesis: str, reference: str) -> float:
    ref_words = reference.split()
    if not ref_words:
        return 0.0 if not hypothesis.split() else 1.0
    return _edit_distance(hypothesis.split(), ref_words) / len(ref_words)


# ── Field-level extraction accuracy (medicine/dose/frequency/duration) ────
_ROW_FIELDS = ("medicine", "dosage", "frequency", "duration")
# Fixed, not "today": keeps `find_document_date`'s future/past checks
# deterministic across runs of this harness, on any day it's run.
_REFERENCE_DATE = date(2026, 9, 30)


def field_accuracy(rows: list[dict], truth: list) -> dict[str, tuple[int, int]]:
    """Exact-match accuracy per field, zipped to `truth` by row position.
    Returns {field: (correct, total)}. A row the pipeline never produced
    counts as a miss on every field, not a skip."""
    correct = dict.fromkeys(_ROW_FIELDS, 0)
    for i, t in enumerate(truth):
        row = rows[i] if i < len(rows) else {}
        for f in _ROW_FIELDS:
            if (row.get(f) or "").strip() == getattr(t, f).strip():
                correct[f] += 1
    return {f: (correct[f], len(truth)) for f in _ROW_FIELDS}


# ── Synthetic documents (clearly marked as such; no real patient data) ────
@dataclass(frozen=True)
class SyntheticDoc:
    label: str
    ground_truth: str  # what the rendered image actually says, line by line


PRINTED_DOCS = [
    SyntheticDoc(
        "printed-simple",
        "SYNTHETIC PATIENT\nTab. Metformin 500mg 1-0-1 x 30 days\nCap Atorvastatin 10 mg once daily for 30 days",
    ),
    SyntheticDoc(
        "printed-wrapped",
        "SYNTHETIC PATIENT\nParacetamol 500 mg\nTake 1 tablet every 6 hours as needed\nAmoxicillin 500 mg\nTake 1 capsule three times daily\nfor 5 days",
    ),
    SyntheticDoc(
        "printed-lab",
        "SYNTHETIC LAB REPORT\nHbA1c 7.2 % 4.0 - 5.6\nFasting Glucose 142 mg/dL 70-100",
    ),
]


def _font(size: int) -> ImageFont.ImageFont:
    candidates = [
        r"C:\Windows\Fonts\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial.ttf",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def _render(doc: SyntheticDoc) -> bytes:
    lines = doc.ground_truth.splitlines()
    image = Image.new("RGB", (1700, 200 + 110 * len(lines)), "white")
    draw = ImageDraw.Draw(image)
    font = _font(56)
    for index, line in enumerate(lines):
        draw.text((80, 80 + 110 * index), line, fill="black", font=font)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


# ── Run one engine over its document set ───────────────────────────────────
@dataclass
class Result:
    label: str
    cer: float
    wer: float
    total_s: float
    stage: dict


async def _run_one(doc: SyntheticDoc) -> Result | str:
    import structlog.testing

    data = _render(doc)
    with structlog.testing.capture_logs() as captured:
        try:
            result = await ocr.run_ocr(data, "image/png")
        except ocr.OcrError as exc:
            return f"OcrError({exc.code})"
    stage = next((c for c in captured if c.get("event") == "ocr_stage_timing"), {})
    doc_log = next((c for c in captured if c.get("event") == "ocr_document_timing"), {})
    return Result(
        label=doc.label,
        cer=round(_cer(result.text, doc.ground_truth), 3),
        wer=round(_wer(result.text, doc.ground_truth), 3),
        total_s=doc_log.get("total_s", 0.0),
        stage=stage,
    )


async def _run_engine(docs: list[SyntheticDoc]) -> list[Result | str]:
    return [await _run_one(doc) for doc in docs]


def _print_results(engine_label: str, results: list[Result | str]) -> None:
    print(f"\n{engine_label}")
    print("-" * len(engine_label))
    ok = [r for r in results if isinstance(r, Result)]
    for r in results:
        if isinstance(r, str):
            print(f"  (skipped/failed: {r})")
            continue
        extra = ""
        if r.stage:
            parts = [f"{k}={v}" for k, v in r.stage.items() if k not in ("event", "engine")]
            extra = "  [" + ", ".join(parts) + "]"
        print(f"  {r.label:<24} CER={r.cer:<6} WER={r.wer:<6} total_s={r.total_s}{extra}")
    if ok:
        mean_cer = round(sum(r.cer for r in ok) / len(ok), 3)
        mean_wer = round(sum(r.wer for r in ok) / len(ok), 3)
        print(f"  {'MEAN':<24} CER={mean_cer:<6} WER={mean_wer}")


# ── Real-failure fixtures (Step 0 of the OCR/extraction accuracy work) ────
# Fixture 1 is deliberately text-only (bypasses OCR) — it isolates the
# extraction/normalization/confidence stages from OCR-engine randomness,
# using the exact garbled-marker corruption already documented in
# `test_document_extractors.py`'s `GARBLED_MARKER_RX`. Fixture 2 goes
# through real Tesseract, in both a clean and a realistically-degraded
# rendering (see `tests/document_fixtures.py` for why both exist).
async def _run_named_fixture(label: str, text: str, truth: list) -> None:
    extraction = extractors.extract_prescription(text, _REFERENCE_DATE)
    understood = await ai_understanding.understand_prescription(text, extraction.rows)
    rows = understood.rows if understood else extraction.rows
    gemini_state = "succeeded" if understood else "unavailable/fell back to deterministic"
    acc = field_accuracy(rows, truth)
    print(f"\n{label}  (Gemini: {gemini_state})")
    print("-" * len(label))
    for field_name, (correct, total) in acc.items():
        print(f"  {field_name:<10} {correct}/{total}")
    for i, row in enumerate(rows):
        print(f"  row {i}: {row}")


async def _run_real_failure_fixtures() -> None:
    print("\n" + "=" * 70)
    print("Real-failure fixtures (field-level accuracy)")
    print("=" * 70)

    await _run_named_fixture("Fixture 1 (garbled list markers)", FIXTURE_1_TEXT, FIXTURE_1_TRUTH)

    clean = await ocr.run_ocr(render_fixture_2_png(), "image/png")
    await _run_named_fixture("Fixture 2 - clean render", clean.text, FIXTURE_2_TRUTH)

    try:
        degraded = await ocr.run_ocr(render_fixture_2_degraded_jpeg(), "image/jpeg")
        await _run_named_fixture("Fixture 2 - degraded render", degraded.text, FIXTURE_2_TRUTH)
    except ocr.OcrError as exc:
        print(f"\nFixture 2 - degraded render: OcrError({exc.code})")


async def _main_async() -> int:
    print("OCR accuracy harness - synthetic documents only, no real patient data.")
    print(f"Tesseract: {ocr.resolve_tesseract_cmd() or 'NOT FOUND'}")

    start = time.monotonic()
    printed = await _run_engine(PRINTED_DOCS)
    _print_results("Tesseract (printed)", printed)

    await _run_real_failure_fixtures()

    print(f"\nTotal harness time: {round(time.monotonic() - start, 1)}s")
    return 0


def main() -> int:
    return asyncio.run(_main_async())


if __name__ == "__main__":
    raise SystemExit(main())
