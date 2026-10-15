"""A lab result's flag — High / Low / Normal — for display only.

A flag is not something the patient should have to type, so the page works it out from
the confirmed row each time it is shown (`ocr_extraction` has no field for one). Of the two
sources below, only the first ever reaches a stored record: `records.py` copies the report's OWN
printed marker (low / normal / high) into `lab_result.flag`; a calculated flag is never written.
Two sources, in this order:

1. **Printed.** Many reports print their own marker beside the result — `H`, `L`,
   `HIGH`, `LOW`, `*`, an arrow, `(H)`. When the report says so, that is the flag,
   and it is shown as the report's own.
2. **Calculated.** Otherwise, when both the result and the reference range are
   plain numbers, the result is compared with the range. This is arithmetic on two
   values the patient has just reviewed, labelled "calculated" so it is never
   mistaken for the laboratory's own judgement.

A result that is out of range but would be in range with its decimal point restored
("46" against 4.2-5.4) is shown as `check`, not as High: the scan most likely dropped the
point, and a confident wrong flag is worse than none.

What it never does: flag a result that is not a plain number (`Positive`, `<0.5`),
guess a range the report does not state, or trust a printed flag after the patient
has edited the result it was printed beside.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

HIGH, LOW, NORMAL, ABNORMAL, CRITICAL = "high", "low", "normal", "abnormal", "critical"
# Not a verdict: the number looks like it lost a decimal point in the scan ("46" for 4.6),
# so the arithmetic would be wrong. Shown as "check this value", never as High/Low.
CHECK = "check"
PRINTED, CALCULATED = "printed", "calculated"


@dataclass(frozen=True)
class Flag:
    kind: str  # high | low | normal | abnormal | critical
    source: str  # printed | calculated


# ── The report's own marker ──────────────────────────────────────────────
# Single letters must be upper case: a lower-case "l" or "h" is far more likely to
# be an OCR slip or a unit than a marker.
_WORD_FLAGS = {
    "high": HIGH,
    "low": LOW,
    "normal": NORMAL,
    "abnormal": ABNORMAL,
    "critical": CRITICAL,
    "crit": CRITICAL,
}
_LETTER_FLAGS = {"H": HIGH, "L": LOW, "HH": CRITICAL, "LL": CRITICAL, "*": ABNORMAL}
_ARROWS = {"↑": HIGH, "▲": HIGH, "↓": LOW, "▼": LOW}
_TOKEN_SPLIT = re.compile(r"[\s|,;]+")
_BRACKETS = "()[]{}"
_WS = re.compile(r"\s+")


def _token_flag(token: str) -> str | None:
    bare = token.strip(_BRACKETS)
    if bare in _ARROWS:
        return _ARROWS[bare]
    if bare in _LETTER_FLAGS:
        return _LETTER_FLAGS[bare]
    if bare.endswith("*") and bare[:-1] in _LETTER_FLAGS:
        return _LETTER_FLAGS[bare[:-1]]
    return _WORD_FLAGS.get(bare.lower())


def _tail_after_result(line: str, test_name: str, result: str) -> str | None:
    lowered = line.lower()
    name_at = lowered.find(test_name.lower())
    if name_at < 0:
        return None
    result_at = line.find(result, name_at + len(test_name))
    if result_at < 0:
        return None
    return line[result_at + len(result) :]


def printed_flag(ocr_text: str, test_name: str, result: str, unit: str = "") -> str | None:
    """The marker the report itself prints beside this result, or `None`."""
    name = _WS.sub(" ", test_name).strip()
    value = result.strip()
    if not name or not value:
        return None
    for raw in ocr_text.splitlines():
        line = _WS.sub(" ", raw).strip()
        tail = _tail_after_result(line, name, value)
        if tail is None:
            continue
        # "142H": a marker glued straight onto the number.
        if glued := re.match(r"(HH|LL|H|L)(?![A-Za-z])", tail):
            return _LETTER_FLAGS[glued.group(1)]
        rest = tail.strip()
        if unit and rest.lower().startswith(unit.lower()):
            rest = rest[len(unit) :]
        for token in _TOKEN_SPLIT.split(rest):
            if token and (kind := _token_flag(token)):
                return kind
        return None  # the line was found, and it carries no marker
    return None


# ── Arithmetic on the two values ─────────────────────────────────────────
# One number, thousands separators included: "150,000" is one hundred and fifty
# thousand, not "150" and "000".
_NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_UPPER = re.compile(
    rf"^\s*(?P<op><=|≤|<|up\s*to|upto|less\s*than|below|max(?:imum)?)\s*(?P<x>{_NUMBER})",
    re.IGNORECASE,
)
_LOWER = re.compile(
    rf"^\s*(?P<op>>=|≥|>|greater\s*than|above|min(?:imum)?)\s*(?P<x>{_NUMBER})",
    re.IGNORECASE,
)
_SPAN = re.compile(rf"(?P<lo>{_NUMBER})\s*(?:-|–|—|to)\s*(?P<hi>{_NUMBER})", re.IGNORECASE)
_PLAIN = re.compile(rf"^(?:{_NUMBER})$")
_INCLUSIVE = ("<=", "≤", "up to", "upto", "max", "maximum", ">=", "≥", "min", "minimum")


def _plain_number(text: str) -> float | None:
    cleaned = text.strip()
    return float(cleaned.replace(",", "")) if _PLAIN.match(cleaned) else None


def _verdict(value: float, reference_range: str) -> str | None:
    """High / Low / Normal for `value` against the range, or `None` if the range
    cannot be read as one."""
    if m := _UPPER.match(reference_range):
        limit = _plain_number(m.group("x"))
        if limit is None:
            return None
        inclusive = re.sub(r"\s+", " ", m.group("op").lower()) in _INCLUSIVE
        return NORMAL if (value <= limit if inclusive else value < limit) else HIGH
    if m := _LOWER.match(reference_range):
        limit = _plain_number(m.group("x"))
        if limit is None:
            return None
        inclusive = re.sub(r"\s+", " ", m.group("op").lower()) in _INCLUSIVE
        return NORMAL if (value >= limit if inclusive else value > limit) else LOW
    if m := _SPAN.search(reference_range):
        low, high = _plain_number(m.group("lo")), _plain_number(m.group("hi"))
        if low is None or high is None or low >= high:
            return None  # not a range: an OCR slip, never guessed at
        return LOW if value < low else HIGH if value > high else NORMAL
    return None


def reference_limits(reference_range: str) -> tuple[float | None, float | None]:
    """The `(low, high)` a stated reference range gives, for storing as numbers: `13.0 - 17.0` ->
    `(13.0, 17.0)`, `< 5.0` -> `(None, 5.0)`, `> 40` -> `(40.0, None)`. Anything that is not
    plainly one of those — an OCR slip, a word, a span whose ends are the wrong way round — is
    `(None, None)`: a limit is never guessed. It reads a range exactly as `_verdict` does."""
    text = reference_range.strip()
    if m := _UPPER.match(text):
        return None, _plain_number(m.group("x"))
    if m := _LOWER.match(text):
        return _plain_number(m.group("x")), None
    if m := _SPAN.search(text):
        low, high = _plain_number(m.group("lo")), _plain_number(m.group("hi"))
        if low is not None and high is not None and low < high:
            return low, high
    return None, None


def calculated_flag(result: str, reference_range: str) -> str | None:
    """High / Low / Normal from a plain-number result and a stated range — or
    `CHECK` when the result is out of range but would sit INSIDE it with a decimal
    point restored (a scan reading 4.6 as "46"): in that case the honest answer is
    that the number needs a second look, not that it is high."""
    value = _plain_number(result)
    if value is None or not reference_range.strip():
        return None
    verdict = _verdict(value, reference_range)
    # Only against a BOUNDED range ("4.2-5.4"): with a single limit ("<150") dividing a
    # genuinely high value by ten always lands under it, so the test would call every
    # high result a misread. A bounded range is narrow enough for the test to mean something.
    bounded = _SPAN.search(reference_range) and not (
        _UPPER.match(reference_range) or _LOWER.match(reference_range)
    )
    if bounded and verdict in (HIGH, LOW) and not re.search(r"[.,]", result):
        for divisor in (10, 100):
            if _verdict(value / divisor, reference_range) == NORMAL:
                return CHECK
    return verdict


def flag_for(
    *,
    ocr_text: str | None,
    test_name: str,
    result: str,
    unit: str = "",
    reference_range: str = "",
    result_edited: bool = False,
) -> Flag | None:
    """The flag to show beside one result row, or `None` when there is nothing
    trustworthy to say. A printed marker counts only while the result it was
    printed beside is the one the report showed (`result_edited=False`)."""
    if ocr_text and not result_edited and (kind := printed_flag(ocr_text, test_name, result, unit)):
        return Flag(kind, PRINTED)
    if kind := calculated_flag(result, reference_range):
        return Flag(kind, CALCULATED)
    return None
