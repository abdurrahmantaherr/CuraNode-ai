"""Vital signs read from a document's OCR text — display only.

A prescription or a clinical letter often records the patient's vitals at the
top ("BP: 120/80 mmHg   Pulse: 78 bpm   Temp: 98.6 F   SpO2: 98%"). This module
finds them, deterministically, the same way `sections.py` finds the rest of a
document's labelled details: a regular expression per vital, each anchored on
the vital's own LABEL and capturing only a value of that vital's own SHAPE.

What it never does:

* **Invent or convert.** A value is shown as it is written (a unit is added only
  when the document printed one). 98.6 stays 98.6 whether or not a unit follows.
* **Accept an impossible reading.** A blood pressure of 12/80 or a pulse of 7 is
  an OCR slip, not a measurement, and is left out — but the limits are wide on
  purpose (a BP of 280/160 or a pulse of 40 are real and must show), so only
  true garbage is dropped.
* **Read a vital out of the wrong kind of line.** Nothing here looks for a label
  without its number: "Weight management advice" is not a weight.

A vital is first-occurrence-wins, like every other section field.
"""

from __future__ import annotations

import re

# (key, display order). The keys are the i18n keys (`documents.field.<key>`) and
# the vocabulary the AI sections layer may use for a stored vitals reading.
VITAL_KEYS: tuple[str, ...] = (
    "blood_pressure",
    "pulse",
    "temperature",
    "respiratory_rate",
    "spo2",
    "weight",
    "height",
    "bmi",
    "blood_sugar",
)

_SEP = r"\s*[:=\-]?\s*"
_FLAGS = re.IGNORECASE

_BP = re.compile(
    rf"\b(?:b\.?\s?p\.?|blood\s*pressure){_SEP}(\d{{2,3}})\s*[/\\|]\s*(\d{{2,3}})(?!\d)"
    r"\s*(mm\s*[/.]?\s*hg)?",
    _FLAGS,
)
_PULSE = re.compile(
    rf"\b(?:pulse(?:\s*rate)?|heart\s*rate|h\.?\s?r\.?|p\.?\s?r\.?){_SEP}(\d{{2,3}})\b"
    r"\s*(bpm|/\s*min|per\s*min(?:ute)?)?",
    _FLAGS,
)
_TEMP = re.compile(
    rf"\btemp(?:erature)?\.?{_SEP}(\d{{2,3}}(?:\.\d{{1,2}})?)\s*"
    r"(?:°|º|deg(?:rees?)?\.?)?\s*([cf])?\b",
    _FLAGS,
)
_RESP = re.compile(
    rf"\b(?:r\.?\s?r\.?|resp(?:iratory)?\.?(?:\s*rate)?){_SEP}(\d{{1,2}})\b"
    r"\s*(/\s*min|bpm|breaths?\s*/\s*min|cpm)?",
    _FLAGS,
)
_SPO2 = re.compile(
    rf"\b(?:sp\s?o\s?2|spo₂|o2\s*sat(?:uration)?|oxygen\s*saturation){_SEP}(\d{{2,3}})(?![\d.]\d)\s*%?",
    _FLAGS,
)
_WEIGHT = re.compile(
    rf"\b(?:wt|weight)\b\.?{_SEP}(\d{{1,3}}(?:\.\d{{1,2}})?)(?!\d)\s*"
    r"(kgs?|kilograms?|lbs?|pounds?)?",
    _FLAGS,
)
_HEIGHT_FT_IN = re.compile(
    rf"\b(?:ht|height)\b\.?{_SEP}(\d)\s*(?:'|′|ft\.?|feet)\s*(\d{{1,2}})?\s*(?:\"|″|in(?:ches)?)?",
    _FLAGS,
)
_HEIGHT = re.compile(
    rf"\b(?:ht|height)\b\.?{_SEP}(\d{{1,3}}(?:\.\d{{1,2}})?)(?!\d)\s*(cm|m|ft|in)?\b",
    _FLAGS,
)
_BMI = re.compile(rf"\bbmi\b{_SEP}(\d{{1,2}}(?:\.\d{{1,2}})?)(?!\d)", _FLAGS)
_SUGAR = re.compile(
    r"\b(rbs|fbs|blood\s*(?:sugar|glucose))\b(?:\s*\(\s*(random|fasting)\s*\))?"
    rf"{_SEP}(\d{{1,3}}(?:\.\d{{1,2}})?)(?!\d)\s*(mg\s*/\s*d\s*l|mmol\s*/\s*l)?",
    _FLAGS,
)


def _num(text: str) -> float:
    return float(text)


def _fmt(number: str, unit: str | None, sep: str = " ") -> str:
    return f"{number}{sep}{unit}" if unit else number


def _bp(line: str) -> str | None:
    m = _BP.search(line)
    if not m:
        return None
    systolic, diastolic = int(m.group(1)), int(m.group(2))
    if not (40 <= systolic <= 300 and 20 <= diastolic <= 200 and systolic > diastolic):
        return None
    return _fmt(f"{systolic}/{diastolic}", "mmHg" if m.group(3) else None)


def _pulse(line: str) -> str | None:
    m = _PULSE.search(line)
    if not m or not 20 <= int(m.group(1)) <= 250:
        return None
    unit = m.group(2)
    if not unit:
        return m.group(1)
    return f"{m.group(1)} bpm" if unit.lower() == "bpm" else f"{m.group(1)}/min"


def _temp(line: str) -> str | None:
    m = _TEMP.search(line)
    if not m:
        return None
    value, unit = _num(m.group(1)), (m.group(2) or "").upper()
    # An unlabelled value is only checked for plausibility, never converted:
    # 30-45 reads as Celsius, 85-115 as Fahrenheit, anything else is not a temperature.
    if not (30 <= value <= 45 or 85 <= value <= 115):
        return None
    if unit == "C" and not 30 <= value <= 45:
        return None
    if unit == "F" and not 85 <= value <= 115:
        return None
    return _fmt(m.group(1), f"°{unit}" if unit else None, "")


def _resp(line: str) -> str | None:
    m = _RESP.search(line)
    if not m or not 4 <= int(m.group(1)) <= 80:
        return None
    return _fmt(m.group(1), "/min" if m.group(2) else None, "")


def _spo2(line: str) -> str | None:
    m = _SPO2.search(line)
    if not m or not 50 <= int(m.group(1)) <= 100:
        return None
    return f"{m.group(1)}%"


def _weight(line: str) -> str | None:
    m = _WEIGHT.search(line)
    if not m:
        return None
    value, unit = _num(m.group(1)), (m.group(2) or "").lower()
    pounds = unit.startswith(("lb", "pound"))
    if not (2 <= value <= 900 if pounds else 1 <= value <= 400):
        return None
    shown = "lb" if pounds else ("kg" if unit else None)
    return _fmt(m.group(1), shown)


def _height(line: str) -> str | None:
    if m := _HEIGHT_FT_IN.search(line):
        feet = int(m.group(1))
        inches = int(m.group(2)) if m.group(2) else 0
        if 1 <= feet <= 8 and inches <= 11:
            return f"{feet} ft {inches} in" if m.group(2) else f"{feet} ft"
        return None
    m = _HEIGHT.search(line)
    if not m:
        return None
    value, unit = _num(m.group(1)), (m.group(2) or "").lower()
    if unit == "m":
        ok = 0.3 <= value <= 2.5
    elif unit in ("ft", "in"):
        ok = 1 <= value <= 8 if unit == "ft" else 12 <= value <= 100
    else:  # "cm", or no unit at all: a bare number is only plausible as centimetres
        ok = 30 <= value <= 250
    return _fmt(m.group(1), unit or None) if ok else None


def _bmi(line: str) -> str | None:
    m = _BMI.search(line)
    return m.group(1) if m and 8 <= _num(m.group(1)) <= 80 else None


def _sugar(line: str) -> str | None:
    m = _SUGAR.search(line)
    if not m:
        return None
    value = _num(m.group(3))
    unit = re.sub(r"\s+", "", m.group(4) or "").replace("dl", "dL").replace("mmol", "mmol")
    if unit.lower().startswith("mmol"):
        ok, unit = 1 <= value <= 45, "mmol/L"
    else:
        ok, unit = 20 <= value <= 800, "mg/dL" if unit else ""
    if not ok:
        return None
    label = (m.group(2) or m.group(1)).strip().lower()
    qualifier = {"rbs": "RBS", "fbs": "FBS", "random": "random", "fasting": "fasting"}.get(label)
    shown = _fmt(m.group(3), unit or None)
    return f"{shown} ({qualifier})" if qualifier else shown


_READERS = {
    "blood_pressure": _bp,
    "pulse": _pulse,
    "temperature": _temp,
    "respiratory_rate": _resp,
    "spo2": _spo2,
    "weight": _weight,
    "height": _height,
    "bmi": _bmi,
    "blood_sugar": _sugar,
}


def extract_vitals(lines: list[str]) -> list[tuple[str, str, bool]]:
    """`(key, value, unreadable)` for each vital found, in `VITAL_KEYS` order.
    `unreadable` is always False here (a vital is either read or absent) — the
    tuple shape matches every other section so the pieces can be shared."""
    found: dict[str, str] = {}
    for line in lines:
        for key, reader in _READERS.items():
            if key in found:
                continue
            if (value := reader(line)) is not None:
                found[key] = value
    return [(key, found[key], False) for key in VITAL_KEYS if key in found]
