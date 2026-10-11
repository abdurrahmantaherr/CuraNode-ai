"""A lab result's flag (`documents/lab_flags.py`) — pure functions, no app, no DB.

The flag is display-only and never stored. It comes from the report's own marker when
there is one, and otherwise from comparing the result with the stated range. Several of
the cases below are real: they were exposed by running a scanned CBC report through the
pipeline, where a thousands separator and a lost decimal point each produced a
confidently wrong flag.
"""

from __future__ import annotations

import pytest
from app.documents import lab_flags
from app.documents.lab_flags import (
    ABNORMAL,
    CALCULATED,
    CHECK,
    CRITICAL,
    HIGH,
    LOW,
    NORMAL,
    PRINTED,
    calculated_flag,
    flag_for,
    printed_flag,
)

REPORT = """Test Result Unit Reference Range Flag
HbA1c 7.2 % 4.0-5.6 H
Fasting Glucose 142 mg/dL 70-100 HIGH
Hemoglobin 13.1 g/dL 12-15.5
Creatinine 0.9 mg/dL 0.6-1.2 N
Cholesterol 230H mg/dL <200
Platelets 90 10^3/uL 150-400 L
TSH 6.1 mIU/L 0.4-4.0 *
Potassium 7.1 mmol/L 3.5-5.0 HH
Sodium 140 mmol/L 135-145 (Normal)
Urea 30 mg/dL 15-45 ↑
Hb 11.0 g/dL 12-16 ▼"""


# ── The report's own marker ──────────────────────────────────────────────
@pytest.mark.parametrize(
    ("name", "result", "unit", "expected"),
    [
        ("HbA1c", "7.2", "%", HIGH),
        ("Fasting Glucose", "142", "mg/dL", HIGH),
        ("Cholesterol", "230", "mg/dL", HIGH),  # "230H": glued to the number
        ("Platelets", "90", "10^3/uL", LOW),
        ("TSH", "6.1", "mIU/L", ABNORMAL),  # a bare asterisk
        ("Potassium", "7.1", "mmol/L", CRITICAL),  # HH
        ("Sodium", "140", "mmol/L", NORMAL),  # (Normal)
        ("Urea", "30", "mg/dL", HIGH),  # an arrow
        ("Hb", "11.0", "g/dL", LOW),
    ],
)
def test_the_reports_own_marker_is_read(name, result, unit, expected):
    assert printed_flag(REPORT, name, result, unit) == expected


@pytest.mark.parametrize(
    ("name", "result", "unit"),
    [
        ("Hemoglobin", "13.1", "g/dL"),  # the line carries no marker
        ("Creatinine", "0.9", "mg/dL"),  # a lone "N" is not a marker we trust
        ("Nonexistent", "1", ""),  # not in the report at all
    ],
)
def test_no_marker_is_invented_when_the_report_prints_none(name, result, unit):
    assert printed_flag(REPORT, name, result, unit) is None


def test_a_lower_case_letter_is_never_taken_for_a_marker():
    """A lone lower-case "l" or "h" is an OCR slip or a unit far more often than a flag."""
    assert printed_flag("Glucose 90 mg/dL 70-100 l", "Glucose", "90", "mg/dL") is None
    assert printed_flag("Glucose 90 mg/dL 70-100 h", "Glucose", "90", "mg/dL") is None


def test_a_unit_is_not_mistaken_for_a_marker():
    assert printed_flag("Volume 1.5 L 1.0-2.0", "Volume", "1.5", "L") is None


# ── Calculated from the range ────────────────────────────────────────────
@pytest.mark.parametrize(
    ("result", "reference", "expected"),
    [
        ("142", "70-100", HIGH),
        ("60", "70-100", LOW),
        ("85", "70-100", NORMAL),
        ("70", "70-100", NORMAL),  # the ends of a range are inside it
        ("100", "70-100", NORMAL),
        ("7.2", "4.0-5.6", HIGH),
        ("13.1", "12 – 15.5", NORMAL),  # an en dash
        ("13.1", "12 to 15.5", NORMAL),
        ("5", "<5", HIGH),  # strictly below
        ("5", "<=5", NORMAL),
        ("4.9", "< 5", NORMAL),
        ("250", "<150", HIGH),  # a high value against an upper limit stays High
        ("40", "> 40", LOW),  # strictly above
        ("40", ">=40", NORMAL),
        ("55", "up to 100", NORMAL),
        ("9", "Less than 5", HIGH),
    ],
)
def test_a_flag_is_calculated_from_a_plain_result_and_a_stated_range(result, reference, expected):
    assert calculated_flag(result, reference) == expected


@pytest.mark.parametrize(
    ("result", "reference", "expected"),
    [
        # Thousands separators: "150,000" is one number. Read as "150" and "000" these
        # ranges came out as 0-450 and 0-11, and normal counts were flagged High.
        ("245,000", "150,000-450,000", NORMAL),
        ("7,200", "4,000-11,000", NORMAL),
        ("500,000", "150,000-450,000", HIGH),
        ("90,000", "150,000-450,000", LOW),
        ("1,200", "1000-2000", NORMAL),
    ],
)
def test_thousands_separators_are_one_number_not_two(result, reference, expected):
    assert calculated_flag(result, reference) == expected


@pytest.mark.parametrize(
    ("result", "reference"),
    [
        ("46", "4.2-5.4"),  # RBC 4.6 read as "46"
        ("61", "2-10"),  # monocytes 6.1 read as "61"
        ("128", "11.5-15.5"),  # haemoglobin 12.8 read as "128"
        ("07", "0-2"),  # 0.7
    ],
)
def test_a_result_that_would_be_in_range_with_its_decimal_point_is_checked_not_flagged(
    result, reference
):
    """The scan most likely dropped the point; calling it High would be a confident
    wrong answer, so the honest flag is "check this value"."""
    assert calculated_flag(result, reference) == CHECK


@pytest.mark.parametrize(
    ("result", "reference", "expected"),
    [
        ("142", "70-100", HIGH),  # 14.2 is still far outside: a genuine High
        ("12.8", "11.5-15.5", NORMAL),  # the point is there: nothing to doubt
        ("250", "<150", HIGH),  # a single limit is too loose for the test to mean anything
        ("42", "<6.0", HIGH),
        # A thousands separator says the digits were read as written: 150 would be in
        # range, but "1,500" is not a number that lost a decimal point.
        ("1,500", "100-200", HIGH),
    ],
)
def test_the_decimal_check_does_not_swallow_real_abnormal_results(result, reference, expected):
    assert calculated_flag(result, reference) == expected


@pytest.mark.parametrize(
    ("result", "reference"),
    [
        ("Positive", "Negative"),
        ("<0.5", "0-1"),  # a bound, not a number
        ("5", ""),  # no range stated
        ("5", "see note"),
        ("7", "10-5"),  # not a range: an OCR slip, never guessed at
        ("", "70-100"),
    ],
)
def test_nothing_is_calculated_when_either_value_is_not_plain(result, reference):
    assert calculated_flag(result, reference) is None


# ── Both together ────────────────────────────────────────────────────────
def test_a_printed_marker_wins_over_the_arithmetic():
    flag = flag_for(
        ocr_text=REPORT,
        test_name="Creatinine",
        result="0.9",
        unit="mg/dL",
        reference_range="0.6-1.2",
    )
    assert flag == lab_flags.Flag(NORMAL, CALCULATED)  # no marker printed -> calculated
    flag = flag_for(
        ocr_text=REPORT,
        test_name="Platelets",
        result="90",
        unit="10^3/uL",
        reference_range="150-400",
    )
    assert flag == lab_flags.Flag(LOW, PRINTED)


def test_a_printed_marker_is_not_trusted_once_the_result_beside_it_was_edited():
    flag = flag_for(
        ocr_text=REPORT,
        test_name="HbA1c",
        result="5.0",
        unit="%",
        reference_range="4.0-5.6",
        result_edited=True,
    )
    assert flag == lab_flags.Flag(NORMAL, CALCULATED)


def test_there_is_no_flag_when_nothing_trustworthy_can_be_said():
    assert flag_for(ocr_text=None, test_name="X", result="Positive", reference_range="") is None
    assert flag_for(ocr_text="", test_name="X", result="5", reference_range="") is None
