"""Type-specific extraction (the only part of the document pipeline that varies
by document type) — pure functions, no app, no DB. The OCR step is tested in
`test_document_ocr.py`."""

from __future__ import annotations

from datetime import date

import pytest
from app.db.models import DOC_TYPE_LAB, DOC_TYPE_OTHER, DOC_TYPE_RX, DOC_TYPE_VACCINE
from app.documents import extractors, normalization

TODAY = date(2026, 9, 27)

PRESCRIPTION = """Dr. Mehreen Iqbal  PMDC 60021-P
Patient: Ayesha Raza   Age: 32
Date: 11 Aug 2026
Rx
1. Tab. Metformin 500mg  1-0-1  x 30 days
2) Cap Atorvastatin 10 mg once daily at night for 30 days
Tab. Panadol 500mg SOS
Tab. Brufen 400mg
   1-0-1 x 5 days
Advice: plenty of fluids
Follow up after 2 weeks"""

LAB_REPORT = """Chughtai Lab   Report Date: 28/07/2026
Patient Name: Ayesha Raza  Age: 32 years
Test | Result | Unit | Reference Range
HbA1c 7.2 % 4.0 - 5.6 H
Fasting Glucose | 142 | mg/dL | 70-100
WBC Count 12,500 /uL 4000-11000
Total Cholesterol 190 mg/dL < 200
Sample ID 45231"""


# ── Prescription ─────────────────────────────────────────────────────────
def test_prescription_extracts_medicine_dosage_frequency_duration():
    rows = extractors.extract_prescription(PRESCRIPTION, TODAY).rows
    assert rows[0] == {
        "medicine": "Metformin",
        "dosage": "500mg",
        "frequency": "1-0-1",
        "duration": "30 days",
    }
    assert rows[1]["medicine"] == "Atorvastatin"
    assert rows[1]["frequency"] == "once daily"


def test_prescription_skips_non_medicine_lines_and_never_invents_fields():
    rows = extractors.extract_prescription(PRESCRIPTION, TODAY).rows
    names = [r["medicine"] for r in rows]
    assert names == ["Metformin", "Atorvastatin", "Panadol", "Brufen"]
    # Only the four fields the shared schema models — nothing else appears.
    assert all(set(r) == {"medicine", "dosage", "frequency", "duration"} for r in rows)
    # "Follow up after 2 weeks" is advice, not the previous medicine's duration.
    assert rows[2]["duration"] == ""


def test_prescription_frequency_wrapped_onto_the_next_line_is_attached():
    brufen = extractors.extract_prescription(PRESCRIPTION, TODAY).rows[3]
    assert brufen["frequency"] == "1-0-1"
    assert brufen["duration"] == "5 days"


def test_a_medicine_with_no_form_word_and_an_ocr_garbled_mg_unit_is_still_extracted():
    """Manual-test regression: a real prescription's OCR read "Paracetamol 500
    mg Qty: 15" as "Paracetamol 500 ag Qty: 15" ("ag" for "mg"). With no
    "Tab./Cap." prefix AND no recognisable unit, the line used to be discarded
    entirely (`not has_form and not strength`) — the medicine, and everything
    else on its line, vanished from the review screen with no trace."""
    row = extractors._prescription_row("Paracetamol 500 ag Qty: 15")
    assert row is not None
    assert row["medicine"] == "Paracetamol"
    # The OCR'd text is never rewritten/corrected — "ag" is shown exactly as
    # read, for the patient to fix during review, never silently "fixed" to "mg".
    assert row["dosage"] == "500 ag"


def test_all_four_medicines_survive_ocr_noise_on_two_of_them():
    text = (
        "Dr. Sara Khan\n"
        "Patient: Ali Raza   Age: 40\n"
        "1. Cetirizine 10mg 1-0-1 x 10 days\n"
        "2. Tab. Vitamin D3 60000 IU once daily for 8 weeks\n"
        "3. Paracetamol 500 ag Qty: 15\n"
        "4. Amoxicillin 500 ag 1-1-1 x 7 days\n"
    )
    rows = extractors.extract_prescription(text, TODAY).rows
    assert [r["medicine"] for r in rows] == [
        "Cetirizine",
        "Vitamin D3",
        "Paracetamol",
        "Amoxicillin",
    ]
    assert rows[2]["dosage"] == "500 ag" and rows[3]["dosage"] == "500 ag"
    assert rows[3]["frequency"] == "1-1-1" and rows[3]["duration"] == "7 days"


@pytest.mark.parametrize(
    "line",
    [
        "Wt: 70 kg",  # a vitals line must not turn into a "medicine" called "Wt"
        "Weight 70 kg",
        "Reg No: 60021-P",
        "Follow up after 2 weeks",
        "Next visit in 30 days",
    ],
)
def test_the_loosened_unit_pattern_does_not_turn_notes_into_medicines(line):
    assert extractors._prescription_row(line) is None


# ── Real manual-test regression: SHEIKH FAMILY CLINIC prescription ────────
# The literal Tesseract output pasted from that Review page's "Text read from
# your document" box (not hand-typed): "2." was misread as "a," (Ibuprofen),
# and "mg" was misread as "mq" (both Ibuprofen and Omeprazole) -- a different
# corruption from the earlier "ag" case, on the OTHER letter of "mg".
SHEIKH_CLINIC_RX = """1. Amoxicillin 500 mg (Cap) Qty: 15
Take 1 capsule three times daily for 5 days.
a, Ibuprofen 400 mq (Tab) Qty: 10
Take | tablet twice daily after meals.
3. Omeprazole 20 mq (Cap) Qty: 10
Take 1 capsule once daily in the morning.
4. Vitamin C 500 mg (Tab) Qty: 10
Take 1 tablet once daily.
5. Paracetamol 500 mg (Tab) Qty: 15
Take 1 tablet if fever or pain (max 3 per day)."""


def test_ibuprofen_survives_a_misread_list_number_and_a_misread_mg_unit():
    row = extractors._prescription_row("a, Ibuprofen 400 mq (Tab) Qty: 10")
    assert row is not None
    assert row["medicine"] == "Ibuprofen"  # not "a, Ibuprofen"
    assert row["dosage"] == "400 mq"  # shown exactly as OCR read it, not "corrected"


def test_omeprazole_survives_an_mg_unit_misread_as_mq():
    row = extractors._prescription_row("3. Omeprazole 20 mq (Cap) Qty: 10")
    assert row is not None
    assert row["medicine"] == "Omeprazole"
    assert row["dosage"] == "20 mq"


def test_all_five_sheikh_clinic_medicines_are_extracted_in_order():
    rows = extractors.extract_prescription(SHEIKH_CLINIC_RX, TODAY).rows
    assert [r["medicine"] for r in rows] == [
        "Amoxicillin",
        "Ibuprofen",
        "Omeprazole",
        "Vitamin C",
        "Paracetamol",
    ]


@pytest.mark.parametrize(
    "line",
    [
        "Dr. Ayesha Malik",
        "P. J Dr. Ayesha Malik",  # signature line
        "lease visit again.",  # OCR noise fragment from a wrapped notes line
        "Wt: 70 mq",  # a vitals line must not become a medicine even with the "mq" unit
        "Reg No: 500 mq",
    ],
)
def test_the_list_marker_and_unit_fixes_do_not_create_new_false_positives(line):
    assert extractors._prescription_row(line) is None


def test_b_complex_keeps_its_own_period_unlike_a_misread_list_number():
    """ "B. Complex" is a real product name (a period, never a comma, and no
    digit was ever misread here) -- the new list-marker alternative must tell
    these apart and leave it alone."""
    row = extractors._prescription_row("B. Complex 500mg once daily")
    assert row is not None
    assert row["medicine"] == "B. Complex"


# ── Real manual-test regression: a numbered marker that lost its punctuation
# entirely (kept the digit but dropped the "." / ")") ─────────────────────
def test_atorvastatin_survives_a_list_number_that_lost_its_punctuation():
    """Manual-test regression: real OCR read item "2." of a 4-medicine
    prescription as a bare "2 " (no "." or ")") directly followed by "Tab.".
    `_DOSE_UNIT` then matched "2 Tab" as if it meant "2 tablets" at position
    0, cutting the medicine name down to nothing and dropping the whole row."""
    row = extractors._prescription_row("2 Tab. Atorvastatin 10 mg 1 tablet once daily 30 days")
    assert row is not None
    assert row["medicine"] == "Atorvastatin"
    assert row["dosage"] == "10 mg"
    assert row["frequency"] == "once daily"
    assert row["duration"] == "30 days"


def test_all_four_medicines_are_extracted_when_one_list_number_loses_its_punctuation():
    text = (
        "1. Tab. Metformin 500 mg 1 tablet twice daily 30 days\n"
        "(after meals)\n"
        "2 Tab. Atorvastatin 10 mg 1 tablet once daily 30 days\n"
        "(at night)\n"
        "3. Tab. Pantoprazole 40 mg 1 tablet once daily 30 days\n"
        "(before breakfast)\n"
        "4. Tab. Vitamin D3 60K 1 capsule once weekly 8 weeks\n"
        "(for 8 weeks)"
    )
    rows = extractors.extract_prescription(text, TODAY).rows
    assert [r["medicine"] for r in rows] == [
        "Metformin",
        "Atorvastatin",
        "Pantoprazole",
        "Vitamin D3 60K",
    ]


@pytest.mark.parametrize(
    "line",
    [
        "Take 2 tablets after meals",  # a real dosage instruction, not a list marker
        "2 tablets to be taken daily",
    ],
)
def test_a_bare_digit_before_a_form_word_is_only_stripped_at_line_start(line):
    """The new `_LIST_MARKER` alternative is anchored to the start of the
    line, so a genuine "N tablets" instruction appearing anywhere else must
    never be mistaken for a list marker."""
    assert extractors._prescription_row(line) is None


def test_prescription_table_layout():
    text = "Medicine | Dosage | Frequency | Duration\nAmlodipine | 5mg | once daily | 30 days"
    assert extractors.extract_prescription(text, TODAY).rows == [
        {
            "medicine": "Amlodipine",
            "dosage": "5mg",
            "frequency": "once daily",
            "duration": "30 days",
        }
    ]


# ── Real manual-test regression: dosage wrapped onto its own line ─────────
# The literal Tesseract output for a real 4-medicine prescription. Each
# medicine's dosage strength ("500 mg") landed on the physical line AFTER
# its name/form/frequency/duration -- the single-line-only extractor could
# never see the two halves of one medicine at once, so it dropped 2-4 of the
# 4 medicines depending on how badly each line's own list marker survived
# OCR. This is the architectural bug: OCR -> text was fine; text -> rows
# assumed one physical line is one entity, which real wrapped prescriptions
# routinely violate.
WRAPPED_DOSAGE_RX = """Prescription Date: 18-09-2025

ile Tab. Metformin 1 tablet twice daily Twice daily 30 days
500 mg (after meals)

2. | Tab. Atorvastatin 1 tablet once daily Once daily 30 days
10 mg (at night)

3. Tab. Pantoprazole 1 tablet once daily Once daily 30 days
40 mg (before breakfast)

4. Tab. Vitamin D3 60K 1 capsule Once weekly 8 weeks
500 mg (for 8 weeks)"""


def test_all_four_medicines_are_extracted_from_the_real_wrapped_dosage_prescription():
    rows = extractors.extract_prescription(WRAPPED_DOSAGE_RX, TODAY).rows
    assert [r["medicine"] for r in rows] == [
        "Metformin",
        "Atorvastatin",
        "Pantoprazole",
        "Vitamin D3 60K",
    ]
    # The dosage STRENGTH comes from the wrapped line, not the "1 tablet"
    # quantity that happens to be on the medicine's own first line.
    assert [r["dosage"] for r in rows] == ["500 mg", "10 mg", "40 mg", "500 mg"]
    assert rows[1]["frequency"] == "once daily" and rows[1]["duration"] == "30 days"


def test_the_wrapped_dosage_prescription_uses_its_own_printed_date_not_upload_date():
    result = extractors.extract_prescription(WRAPPED_DOSAGE_RX, TODAY)
    assert result.document_date == date(2025, 9, 18)


def test_a_medicine_name_with_no_dosage_on_either_line_still_keeps_its_frequency():
    # A block can reconstruct fields from anywhere in it, not only dosage.
    text = "Tab. Metformin\ntwice daily 30 days"
    assert extractors.extract_prescription(text, TODAY).rows == [
        {"medicine": "Metformin", "dosage": "", "frequency": "twice daily", "duration": "30 days"}
    ]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Tab. Metformin 500mg", {"dosage": "500mg", "frequency": "", "duration": ""}),
        ("Tab. Metformin", {"dosage": "", "frequency": "", "duration": ""}),
        (
            "Tab. Metformin 500mg twice daily",
            {"dosage": "500mg", "frequency": "twice daily", "duration": ""},
        ),
    ],
)
def test_a_medicine_survives_with_any_combination_of_missing_optional_fields(text, expected):
    """The medicine itself must never disappear because dosage, frequency or
    duration (any subset of them) is missing -- only the field itself stays
    empty, never guessed."""
    rows = extractors.extract_prescription(text, TODAY).rows
    assert len(rows) == 1 and rows[0]["medicine"] == "Metformin"
    for field, value in expected.items():
        assert rows[0][field] == value


@pytest.mark.parametrize(
    "text,names",
    [
        ("Tab. Vitamin B12 500mg once daily", ["Vitamin B12"]),
        ("Tab. Co-Amoxiclav 625 500mg twice daily", ["Co-Amoxiclav 625"]),
        ("Cap. Omega-3 1000mg once daily", ["Omega-3"]),
    ],
)
def test_medicine_names_containing_numbers_are_extracted_whole(text, names):
    assert [r["medicine"] for r in extractors.extract_prescription(text, TODAY).rows] == names


@pytest.mark.parametrize(
    "text",
    [
        "1. Tab. Metformin 500mg",
        "1) Tab. Metformin 500mg",
        "- Tab. Metformin 500mg",
        "* Tab. Metformin 500mg",
        "• Tab. Metformin 500mg",
        "Tab. Metformin 500mg",  # no marker at all
    ],
)
def test_every_list_marker_style_reaches_the_same_clean_medicine(text):
    assert extractors.extract_prescription(text, TODAY).rows[0]["medicine"] == "Metformin"


def test_a_header_line_between_two_medicines_does_not_merge_them():
    text = (
        "Tab. Metformin 500mg once daily\nAdvice: take with food\nTab. Atorvastatin 10mg once daily"
    )
    rows = extractors.extract_prescription(text, TODAY).rows
    assert [r["medicine"] for r in rows] == ["Metformin", "Atorvastatin"]


def test_a_continuation_line_never_crosses_into_the_next_medicines_own_line():
    """A line with its own list marker or form word always starts its OWN
    block, even while a previous block is still open — it must never be
    swallowed as that previous medicine's continuation."""
    text = "Tab. Metformin 500mg\nTab. Atorvastatin 10mg"
    rows = extractors.extract_prescription(text, TODAY).rows
    assert [r["medicine"] for r in rows] == ["Metformin", "Atorvastatin"]
    assert rows[0]["dosage"] == "500mg"  # not "10mg" stolen from the next line


# ── Real manual-test regression: handwritten prescription layout ──────────
# The handwritten pipeline's cleaned OCR text has NO list markers at all, and
# each medicine's form word sits in parentheses AFTER the name instead of as
# a "Tab."/"Cap." prefix before it — a layout the wrapped-dosage fix above
# does not by itself cover, since it has neither a marker nor a leading form
# word for `_looks_like_new_medicine` to recognise.
HANDWRITTEN_RX = """Date : 15-08-2026

Amoxicillin 500 mg (Cap)
Take 1 capsule three times daily for 5 days.

Ibuprofen 400 mg (Tab)
Take 1 tablet twice daily after meals.

Omeprazole 20 mg (Cap)
Take 1 capsule once daily in the morning.

Vitamin C 500 mg (Tab)
Take 1 tablet once daily.

Paracetamol 500 mg (Tab)
Take 1 tablet if fever or pain (max 3 per day)."""


def test_all_five_handwritten_medicines_are_extracted_with_no_list_markers_at_all():
    rows = extractors.extract_prescription(HANDWRITTEN_RX, TODAY).rows
    assert [r["medicine"] for r in rows] == [
        "Amoxicillin",
        "Ibuprofen",
        "Omeprazole",
        "Vitamin C",
        "Paracetamol",
    ]
    assert [r["dosage"] for r in rows] == ["500 mg", "400 mg", "20 mg", "500 mg", "500 mg"]


def test_the_handwritten_prescription_date_is_extracted():
    assert extractors.extract_prescription(HANDWRITTEN_RX, TODAY).document_date == date(2026, 8, 15)


def test_paracetamol_gets_no_fabricated_duration():
    """ "Take 1 tablet if fever or pain (max 3 per day)." has no fixed
    duration in it anywhere — the field must stay empty, never invented."""
    rows = extractors.extract_prescription(HANDWRITTEN_RX, TODAY).rows
    paracetamol = next(r for r in rows if r["medicine"] == "Paracetamol")
    assert paracetamol["duration"] == ""


def test_a_medicine_name_before_a_strength_starts_a_new_block_even_mid_block():
    """The new "name then strength, no marker" signal must interrupt an
    already-open block, not just open one when nothing is active — otherwise
    a run of unmarked medicines collapses into a single garbled entry."""
    text = "Amoxicillin 500 mg (Cap)\nIbuprofen 400 mg (Tab)\nOmeprazole 20 mg (Cap)"
    rows = extractors.extract_prescription(text, TODAY).rows
    assert [r["medicine"] for r in rows] == ["Amoxicillin", "Ibuprofen", "Omeprazole"]


def test_a_bare_dosage_continuation_is_still_absorbed_not_mistaken_for_a_new_medicine():
    """The new signal requires real letters BEFORE the strength match — a
    continuation fragment with nothing in front of its dosage ("500 mg
    (after meals)") must still attach to the medicine above it, exactly as
    the printed wrapped-dosage fix already relies on."""
    text = "Tab. Metformin 1 tablet twice daily\n500 mg (after meals)"
    rows = extractors.extract_prescription(text, TODAY).rows
    assert len(rows) == 1
    assert rows[0]["medicine"] == "Metformin" and rows[0]["dosage"] == "500 mg"


# ── Real manual-test regression: handwritten instructions across 2-3 lines,
# and realistic OCR spacing noise ─────────────────────────────────────────
# The manual test's literal 4-medicine text already worked once retyped
# cleanly — the actual bug only showed up with the SAME shape written the
# way real OCR actually produces it: irregular double/triple spaces between
# words. Every field pattern here (`_STRENGTH`, `_FREQUENCY`, ...)
# deliberately allows at most one space between tokens, so two spaces was
# silently unmatching every one of them and dropping the whole medicine —
# not a layout problem, a whitespace-normalisation gap shared with (and
# already half-fixed for) the lab extractor.
FOUR_MEDICINE_RX = """Paracetamol 500 mg
Take 1 tablet every 6 hours as needed
for fever or pain

Amoxicillin 500 mg
Take 1 capsule three times daily
for 5 days

Vitamin D3 1000 IU
Take 1 tablet once daily after meal

Cetirizine 10 mg
Take 1 tablet at night for allergy symptoms"""


def test_all_four_medicines_with_three_line_wrapped_instructions_are_extracted():
    rows = extractors.extract_prescription(FOUR_MEDICINE_RX, TODAY).rows
    assert [r["medicine"] for r in rows] == [
        "Paracetamol",
        "Amoxicillin",
        "Vitamin D3",
        "Cetirizine",
    ]
    assert [r["dosage"] for r in rows] == ["500 mg", "500 mg", "1000 IU", "10 mg"]


def test_frequency_and_duration_are_found_across_wrapped_continuation_lines():
    rows = extractors.extract_prescription(FOUR_MEDICINE_RX, TODAY).rows
    paracetamol, amoxicillin = rows[0], rows[1]
    assert paracetamol["frequency"] == "every 6 hours"
    assert paracetamol["duration"] == ""  # "as needed" / "for fever or pain": no fixed duration
    assert amoxicillin["frequency"] == "three times daily"
    assert amoxicillin["duration"] == "5 days"  # from the THIRD line of the block


def test_missing_duration_and_frequency_stay_empty_not_fabricated():
    rows = extractors.extract_prescription(FOUR_MEDICINE_RX, TODAY).rows
    vitamin_d3, cetirizine = rows[2], rows[3]
    assert vitamin_d3["duration"] == "" and cetirizine["duration"] == ""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Cholecalciferol 60000 IU 1 capsule Once weekly 8 weeks", "Once weekly"),
        ("Folic Acid 5 mg 1 tablet twice weekly", "twice weekly"),
        ("Rosuvastatin 20 mg thrice weekly", "thrice weekly"),
        ("Methotrexate 7.5 mg 2 times a week", "2 times a week"),
        ("Alendronate 70 mg once monthly", "once monthly"),
        ("Vitamin B12 injection 1000 mcg weekly", "weekly"),
    ],
)
def test_weekly_and_monthly_frequencies_are_recognised_not_just_daily(text, expected):
    """The real-world bug: a "once weekly" dosing schedule (common for
    vitamin D, biologics, weekly injections) used to read back with an
    empty frequency — `_FREQUENCY` only ever recognised the "daily"/"day"
    unit, never "weekly"/"week" or "monthly"/"month", even though the
    phrase shapes are identical. None of these medicine names appear
    anywhere else in this file."""
    rows = extractors.parse_prescription_entities(text)
    assert rows[0]["frequency"] == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Omeprazole 20 mg once daily", "once daily"),
        ("Ibuprofen 400 mg twice daily", "twice daily"),
        ("Amoxicillin 500 mg three times daily", "three times daily"),
        ("Paracetamol 500 mg every 6 hours", "every 6 hours"),
        ("Cetirizine 10 mg at night", "at night"),
    ],
)
def test_spelled_out_multi_word_counts_are_recognised_not_truncated(text, expected):
    """The real-failure bug: "three times daily" used to read back as just
    "daily" — the only multi-word-count pattern required a DIGIT and a
    mandatory "a"/"per" ("3 times a day"), so a spelled-out count with no
    "a"/"per" before the unit matched nothing at its own position, and the
    scan silently picked up the bare "daily" alternative later, discarding
    "three times" entirely. None of these medicines appear anywhere else
    in this file."""
    rows = extractors.parse_prescription_entities(text)
    assert rows[0]["frequency"] == expected


@pytest.mark.parametrize(
    "noisy",
    [
        "Paracetamol  500  mg\nTake  1  tablet  every  6  hours  as  needed",
        "Paracetamol 500 mg\nTake 1 tablet  every 6  hours   as needed",
        "Paracetamol   500 mg\nTake 1 tablet every 6 hours as needed,",
    ],
)
def test_irregular_ocr_spacing_does_not_drop_the_medicine(noisy):
    """The actual manual-test bug: multiple/irregular spaces between words
    (common in real, especially handwritten, OCR output) used to make every
    field pattern silently stop matching, discarding the whole medicine even
    though the OCR text plainly contained it."""
    rows = extractors.extract_prescription(noisy, TODAY).rows
    assert len(rows) == 1
    assert rows[0]["medicine"] == "Paracetamol"
    assert rows[0]["dosage"] == "500 mg"
    assert rows[0]["frequency"] == "every 6 hours"


@pytest.mark.parametrize(
    "name,strength,instruction",
    [
        ("Losartan", "50 mg", "Take 1 tablet once daily for blood pressure"),
        ("Metronidazole", "400 mg", "Take 1 tablet three times daily for 7 days"),
        ("Azithromycin", "250 mg", "Take 1 tablet once daily for 3 days"),
    ],
)
def test_the_extractor_generalises_to_medicine_names_never_seen_in_any_fixture(
    name, strength, instruction
):
    """No medicine name is special-cased anywhere in the extractor — these
    three appear nowhere else in this test file."""
    text = f"{name} {strength}\n{instruction}"
    rows = extractors.extract_prescription(text, TODAY).rows
    assert len(rows) == 1
    assert rows[0]["medicine"] == name
    assert rows[0]["dosage"] == strength


def test_a_medicines_wrapped_instructions_are_never_assigned_to_the_next_medicine():
    """Even with the wider continuation-line allowance, a line that itself
    carries a strong "new medicine" signal (name + strength together) always
    closes the previous block and starts its own — it must never be read as
    a fourth continuation line of the medicine above it."""
    text = (
        "Paracetamol 500 mg\n"
        "Take 1 tablet every 6 hours as needed\n"
        "for fever or pain\n"
        "Amoxicillin 500 mg\n"
        "Take 1 capsule three times daily for 5 days"
    )
    rows = extractors.extract_prescription(text, TODAY).rows
    assert [r["medicine"] for r in rows] == ["Paracetamol", "Amoxicillin"]
    assert "capsule" not in rows[0]["frequency"] and "capsule" not in rows[0]["duration"]
    assert rows[1]["duration"] == "5 days"  # not stolen into Paracetamol's block


# ── Regression: a medicine whose own strength is unreadable still gets a
# row, and never corrupts its neighbour ────────────────────────────────────
# A real manual-test case: a numbered list marker OCR'd down to a bare digit
# ("3 Vitamin D3,000. IN." for what was presumably "3. Vitamin D3 1000 IU")
# so badly that its own strength no longer matches `_STRENGTH` at all. Early
# on, such a line — recognised as neither a new medicine nor clearly "not a
# medicine" — was blindly absorbed as a continuation of whatever block
# happened to be open, corrupting that block's data and then causing the
# FOLLOWING (genuinely unrelated) line to be dropped once the continuation
# cap was hit. A later fix stopped the corruption but still dropped the
# medicine itself entirely. The dosage can never be recovered from text this
# degraded (no value is fabricated for it), but the NAME is still legible,
# and its own continuation line still carries a real frequency — both are
# now recovered into their own entity instead of being discarded.
GARBLED_MARKER_RX = """( D Paracetamol 500 mg
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


def test_a_medicine_with_an_unreadable_strength_still_gets_its_own_row():
    extraction = extractors.extract_prescription(GARBLED_MARKER_RX, TODAY)
    rows = extraction.rows
    # "( D " and "( A_" are safely stripped (no real medicine name ever
    # legitimately starts with "(" — see `normalization._PAREN_MARKER_
    # PREFIX`). "G." is deliberately left alone: the same shape is also a
    # real product name ("B. Complex"), and there is no reliable way to
    # tell them apart from text shape alone.
    assert [r["medicine"] for r in rows] == [
        "Paracetamol",
        "G. Amoxicillin",
        "Vitamin D3",
        "Cetirizing",
    ]
    vitamin_d3 = rows[2]
    assert vitamin_d3["dosage"] == ""  # unrecoverable from "D3,000. IN." — never guessed
    # But NOT the same as a dose that was simply never written: "D3,000. IN."
    # is the shape a genuinely-present-but-garbled strength leaves behind, so
    # this must be flagged for the patient to read off the original, not
    # silently reported as if nothing was there at all (CERTAINTY_MISSING).
    assert extraction.field_certainty[2]["dosage"] == normalization.CERTAINTY_AMBIGUOUS
    assert vitamin_d3["frequency"] == "once daily"  # from its own continuation line

    amoxicillin = rows[1]
    assert amoxicillin["dosage"] == "500 mg"
    assert amoxicillin["frequency"] == "three times daily"
    assert amoxicillin["duration"] == "5 days"
    assert "vitamin" not in amoxicillin["duration"].lower()


def test_the_unreadable_strength_recovery_generalises_to_a_name_never_seen_elsewhere():
    """Same garbled-marker shape, an unrelated fake name/strength/numbering
    (never used in any other fixture in this file) and a DIFFERENT digit
    corruption pattern — a stray leading digit token before the comma,
    rather than one fused onto a letter — to prove neither the medicine
    identity nor this exact corruption shape is hardcoded anywhere."""
    text = (
        "9 Ferronix Complex 2,500. XR. 0 0\n"
        "Take one capsule twice daily after food\n"
        "\n"
        "5 Omeprazole 20 mg\n"
        "Take 1 capsule before breakfast\n"
        "for 14 days\n"
    )
    rows = extractors.parse_prescription_entities(text)
    assert [r["medicine"] for r in rows] == ["Ferronix Complex", "Omeprazole"]
    assert rows[0]["dosage"] == ""  # unrecoverable, never guessed
    assert rows[0]["frequency"] == "twice daily"
    assert rows[1]["dosage"] == "20 mg" and rows[1]["duration"] == "14 days"


@pytest.mark.parametrize(
    "line",
    [
        "1 Tablet before meals",  # a real dosage continuation, not a marker
        "2 puffs twice daily",
        "3 times a day",
        "6 hours",  # lowercase word after the digit: never treated as a marker
    ],
)
def test_a_genuine_digit_led_continuation_is_never_treated_as_an_orphaned_marker(line):
    assert extractors._looks_like_orphaned_marker(line) is False
    # And, in context, it must still work as an ordinary continuation line,
    # not be mistaken for the start of a new (nameless) medicine.
    assert extractors._looks_like_new_medicine(line) is False


# ── Structured intermediate representation ──────────────────────────────
# `parse_prescription_entities` is the named RAW OCR -> STRUCTURED JSON stage:
# raw OCR text in, `[{"medicine", "dosage", "frequency", "duration"}, ...]`
# out — the exact shape the Review table's fields are populated from. These
# tests call it directly rather than through `extract_prescription`, so the
# intermediate representation itself is what's under test, independent of
# document-date extraction.
def test_a_single_medicine_becomes_one_structured_entity():
    text = "Metformin 500 mg\nTake 1 tablet twice daily\nfor 30 days"
    entities = extractors.parse_prescription_entities(text)
    assert entities == [
        {
            "medicine": "Metformin",
            "dosage": "500 mg",
            "frequency": "twice daily",
            "duration": "30 days",
        }
    ]


def test_multiple_medicines_each_become_their_own_structured_entity():
    entities = extractors.parse_prescription_entities(FOUR_MEDICINE_RX)
    assert isinstance(entities, list)
    assert all(isinstance(e, dict) for e in entities)
    assert [e["medicine"] for e in entities] == [
        "Paracetamol",
        "Amoxicillin",
        "Vitamin D3",
        "Cetirizine",
    ]
    assert all(set(e) == {"medicine", "dosage", "frequency", "duration"} for e in entities)


def test_dosage_frequency_and_duration_are_read_from_wrapped_continuation_lines():
    text = "Metformin 500 mg\nTake 1 tablet twice daily\nfor 30 days"
    [entity] = extractors.parse_prescription_entities(text)
    assert entity["dosage"] == "500 mg"
    assert entity["frequency"] == "twice daily"
    assert entity["duration"] == "30 days"


def test_a_field_genuinely_absent_from_the_ocr_text_stays_empty_in_the_structured_entity():
    text = "Metformin 500 mg\nTake twice daily"
    [entity] = extractors.parse_prescription_entities(text)
    assert entity["medicine"] == "Metformin"
    assert entity["frequency"] == "twice daily"
    assert entity["duration"] == ""  # never guessed


def test_ocr_whitespace_and_punctuation_noise_does_not_remove_an_identifiable_medicine():
    text = "Metformin   500  mg\nTake 1 tablet  twice   daily,\nfor 30 days."
    [entity] = extractors.parse_prescription_entities(text)
    assert entity["medicine"] == "Metformin"
    assert entity["dosage"] == "500 mg"
    assert entity["duration"] == "30 days"


def test_a_new_medicine_block_is_never_merged_into_the_previous_one():
    text = (
        "Medicine A 500 mg\n"
        "Take 1 tablet daily\n"
        "for 10 days\n"
        "Medicine B 250 mg\n"
        "Take 1 capsule twice daily\n"
        "for 7 days"
    )
    entities = extractors.parse_prescription_entities(text)
    assert [e["medicine"] for e in entities] == ["Medicine A", "Medicine B"]
    assert entities[0]["duration"] == "10 days"
    assert entities[1]["duration"] == "7 days"
    assert "Medicine B" not in entities[0]["duration"] and "capsule" not in entities[0]["duration"]


def test_printed_and_handwritten_style_ocr_text_share_the_same_structuring_stage():
    """No separate code path per engine: the same `parse_prescription_entities`
    call structures both a clean, printed-style prescription and a noisy,
    handwritten-style one."""
    printed_entities = extractors.parse_prescription_entities(PRESCRIPTION)
    handwritten_entities = extractors.parse_prescription_entities(FOUR_MEDICINE_RX)
    assert printed_entities[0]["medicine"] == "Metformin"
    assert handwritten_entities[0]["medicine"] == "Paracetamol"
    for entities in (printed_entities, handwritten_entities):
        assert all(set(e) == {"medicine", "dosage", "frequency", "duration"} for e in entities)


def test_extract_prescription_rows_match_structuring_when_nothing_needs_normalizing():
    """`extract_prescription` adds the document date and runs the structured
    entities through normalization — for text with no OCR-noise symbols to
    clean, normalization is a no-op, so the rows are unchanged from
    structuring's own output."""
    assert extractors.extract_prescription(PRESCRIPTION, TODAY).rows == (
        extractors.parse_prescription_entities(PRESCRIPTION)
    )


# ── AI Understanding / Normalization, exercised through the real block-
# structuring stage (not just `normalization.py` in isolation) ────────────
def test_a_leading_noise_symbol_before_a_medicine_name_is_cleaned_end_to_end():
    text = "& Metformin 500 mg\nTwice daily\nfor 30 days"
    result = extractors.extract_prescription(text, TODAY)
    assert result.rows == [
        {
            "medicine": "Metformin",
            "dosage": "500 mg",
            "frequency": "Twice daily",
            "duration": "30 days",
        }
    ]
    assert result.field_certainty == [
        {
            "medicine": "uncertain",
            "dosage": "high",
            "frequency": "high",
            "duration": "high",
        }
    ]


def test_a_pipe_prefixed_medicine_name_is_cleaned_end_to_end():
    text = "| Amoxicillin 500 mg\nThree times daily\nfor 5 days"
    result = extractors.extract_prescription(text, TODAY)
    assert result.rows[0]["medicine"] == "Amoxicillin"


def test_noise_symbols_never_merge_two_medicines_across_a_boundary():
    text = "& Metformin 500 mg\nTwice daily\n& Atorvastatin 10 mg\nOnce daily"
    result = extractors.extract_prescription(text, TODAY)
    assert [r["medicine"] for r in result.rows] == ["Metformin", "Atorvastatin"]
    assert "atorvastatin" not in result.rows[0]["frequency"].lower()


def test_field_certainty_is_none_for_a_document_with_no_recovered_medicines():
    result = extractors.extract_prescription("Dr. Smith\nAdvice: rest well", TODAY)
    assert result.rows == [] and result.field_certainty == []


# ── Regression: a form-word annotation on its own continuation line, and a
# bare (unpunctuated) numbered marker, must not lose or corrupt an entity ──
# A real manual-test case: every medicine's dosage-form annotation
# ("(Tab)") lands on ITS OWN line, after the dose/frequency/duration line,
# rather than leading the medicine's name the way every earlier fixture had
# it ("1. Tab. Metformin..."). Before this fix, `_looks_like_new_medicine`
# treated ANY form word near a line's start as a new medicine, so "(Tab)
# after meals" was wrongly split into its own (garbage) entity instead of
# staying attached to the medicine above it; separately, a numbered marker
# that kept its digit but dropped both its punctuation and any form word
# ("1 Metformin" for "1. Metformin") was left glued onto the medicine name.
FOUR_MEDICINE_COMPOUND_RX = """1 Metformin 500 mg 500 mg Twice daily 30 days
(Tab) after meals

2 Atorvastatin 10 mg 10 mg Once daily 30 days
(Tab) at night

3 Losartan 50 mg 50 mg Once daily 30 days
(Tab) in the morning

4 Calcium + Vitamin D3 600 mg / 400 IU Once daily 30 days
(Tab) after breakfast
"""


def test_all_medicines_survive_a_trailing_form_word_on_its_own_continuation_line():
    entities = extractors.parse_prescription_entities(FOUR_MEDICINE_COMPOUND_RX)
    assert [e["medicine"] for e in entities] == [
        "Metformin",
        "Atorvastatin",
        "Losartan",
        "Calcium + Vitamin D3",  # a name containing "+" is never split on it
    ]
    assert [e["dosage"] for e in entities] == ["500 mg", "10 mg", "50 mg", "600 mg / 400 IU"]
    assert [e["frequency"] for e in entities] == ["Twice daily"] + ["Once daily"] * 3
    assert all(e["duration"] == "30 days" for e in entities)
    # None of "(Tab) after meals" etc. leaked out as an entity of its own.
    assert len(entities) == 4


@pytest.mark.parametrize(
    "line,expected",
    [
        ("(Tab) after meals", False),
        ("(Tab) at night", False),
        ("(Cap) in the morning", False),
        ("1. Tab. Metformin 500mg", True),
        ("Cap Atorvastatin 10 mg", True),
    ],
)
def test_a_form_word_only_starts_a_new_medicine_when_a_name_follows_it(line, expected):
    assert extractors._looks_like_new_medicine(line) is expected


def test_a_bare_numbered_marker_without_punctuation_is_stripped_from_the_name():
    text = "1 Metformin 500 mg\nTake 1 tablet twice daily"
    [entity] = extractors.parse_prescription_entities(text)
    assert entity["medicine"] == "Metformin"


def test_a_genuine_digit_led_drug_name_is_never_mistaken_for_a_bare_marker():
    """ "3TC" (lamivudine) has no space before its letters, unlike a marker
    ("1 Metformin") — `_BARE_NUM_MARKER` must never touch it."""
    text = "3TC 150 mg\nTake 1 tablet twice daily"
    [entity] = extractors.parse_prescription_entities(text)
    assert entity["medicine"] == "3TC"


# ── Ten generic synthetic regression cases (post-OCR structuring) ─────────
def test_case_1_printed_all_fields_on_one_line():
    [e] = extractors.parse_prescription_entities("Ibuprofen 400 mg twice daily for 5 days")
    assert e == {
        "medicine": "Ibuprofen",
        "dosage": "400 mg",
        "frequency": "twice daily",
        "duration": "5 days",
    }


def test_case_2_printed_fields_wrapped_across_multiple_lines():
    text = "Omeprazole 20 mg\nonce daily\nfor 14 days"
    [e] = extractors.parse_prescription_entities(text)
    assert e == {
        "medicine": "Omeprazole",
        "dosage": "20 mg",
        "frequency": "once daily",
        "duration": "14 days",
    }


def test_case_3_handwritten_noisy_spacing_and_punctuation():
    text = "Amoxicillin   500  mg,\nTake  1  capsule   three times daily,\nfor 5 days."
    [e] = extractors.parse_prescription_entities(text)
    assert e["medicine"] == "Amoxicillin" and e["dosage"] == "500 mg" and e["duration"] == "5 days"


def test_case_4_multiple_medicines_in_one_prescription():
    entities = extractors.parse_prescription_entities(FOUR_MEDICINE_COMPOUND_RX)
    assert len(entities) == 4


def test_case_5_missing_frequency_stays_empty():
    [e] = extractors.parse_prescription_entities("Metformin 500 mg\nfor 30 days")
    assert e["medicine"] == "Metformin" and e["frequency"] == ""


def test_case_6_missing_duration_stays_empty():
    [e] = extractors.parse_prescription_entities("Metformin 500 mg\nTwice daily")
    assert e["medicine"] == "Metformin" and e["duration"] == ""


def test_case_7_dosage_on_a_continuation_line():
    # The block-starting line still needs a signal of its own (here, the
    # form word "Tab.") — a bare name alone on its own line, with even the
    # strength on a later line, is a known, deliberately out-of-scope
    # limitation (see `_looks_like_new_medicine`'s docstring).
    text = "1. Tab. Metformin\n500 mg\nTwice daily for 30 days"
    [e] = extractors.parse_prescription_entities(text)
    assert e["medicine"] == "Metformin" and e["dosage"] == "500 mg"


def test_case_8_frequency_and_duration_on_separate_continuation_lines():
    text = "Metformin 500 mg\nTwice daily\nfor 30 days"
    [e] = extractors.parse_prescription_entities(text)
    assert e["frequency"] == "Twice daily" and e["duration"] == "30 days"


def test_case_9_medicine_names_containing_numbers_or_plus():
    text = "Calcium + Vitamin D3 600 mg / 400 IU\nOnce daily after breakfast"
    [e] = extractors.parse_prescription_entities(text)
    assert e["medicine"] == "Calcium + Vitamin D3"
    assert e["dosage"] == "600 mg / 400 IU"


def test_case_10_ocr_list_marker_corruption():
    text = "1 Losartan 50 mg\nOnce daily for 30 days"
    [e] = extractors.parse_prescription_entities(text)
    assert e["medicine"] == "Losartan"  # the bare "1 " marker never leaks into the name


# ── Lab report ───────────────────────────────────────────────────────────
def test_lab_report_extracts_test_result_unit_and_reference_range():
    rows = extractors.extract_lab(LAB_REPORT, TODAY).rows
    assert rows[0] == {
        "test_name": "HbA1c",
        "result": "7.2",
        "unit": "%",
        "reference_range": "4.0-5.6",
    }
    assert rows[1] == {
        "test_name": "Fasting Glucose",
        "result": "142",
        "unit": "mg/dL",
        "reference_range": "70-100",
    }


def test_lab_report_handles_thousands_units_and_open_ranges_and_skips_headers():
    rows = extractors.extract_lab(LAB_REPORT, TODAY).rows
    assert [r["test_name"] for r in rows] == [
        "HbA1c",
        "Fasting Glucose",
        "WBC Count",
        "Total Cholesterol",
    ]
    wbc = rows[2]
    assert (wbc["result"], wbc["unit"], wbc["reference_range"]) == ("12,500", "/uL", "4000-11000")
    assert rows[3]["reference_range"] == "< 200"  # "Age", "Sample ID", header: skipped


@pytest.mark.parametrize(
    "line",
    [
        "PatientID:0501",  # the reported bug: no space, so a bare `patient\b` never matched
        "PatientID: 0501",
        "Patient ID: 0501",
        "Patient ID:0501",
        "PatientName: Ayesha Raza",
        "Patient Name: Ayesha Raza",  # already worked (a space gives `patient` its `\b`)
        "Sample ID 45231",  # already covered elsewhere; kept as a not-a-regression check
        "Lab No: 123",
        "LabID: 123",  # "lab\s*(?:no|id)" already had no boundary requirement
        "Ref No: 19-01",
    ],
)
def test_patient_and_lab_metadata_lines_never_become_a_result_row(line):
    assert extractors._NOT_A_LAB_LINE.match(line) is not None
    assert extractors._lab_row(line) is None


def test_a_patientid_header_next_to_real_results_extracts_only_the_real_rows():
    text = (
        "Chughtai Lab   Report Date: 28/07/2026\n"
        "Patient Name: Ayesha Raza   PatientID:0501   Ref No: 19-01\n"
        "HbA1c 7.2 % 4.0 - 5.6\n"
        "Fasting Glucose 142 mg/dL 70-100"
    )
    rows = extractors.extract_lab(text, TODAY).rows
    assert [r["test_name"] for r in rows] == ["HbA1c", "Fasting Glucose"]
    assert not any(r["test_name"] == "PatientID" for r in rows)


# ── Real manual-test regression: CityCare Diagnostics lab report ──────────
# The literal Tesseract output pasted from that Review page: an OCR-garbled
# clinic-header/logo line that satisfies the permissive unit pattern, and
# four genuine results whose reference ranges the OLD code couldn't parse
# ("=" instead of "-", thousands-commas, and outright OCR garbage) and so
# discarded entirely -- not just the range, the whole row.
CITYCARE_LAB = """CityCare Diagnostics 6 eee
Hemoglobin (Hb) 12.8 g/dL 11.5 = 15.5
Total Leukocyte Count (TLC) 7,200 /uL 4,000 — 11,000
Platelet Count 245,000 Jul 150,000 — 450,000
Monocytes 6.1 % 7— j\\0)
Fasting Glucose 142 mg/dL 70-100"""


def test_the_citycare_header_line_is_not_extracted_as_a_fake_result():
    assert extractors._lab_row("CityCare Diagnostics 6 eee") is None


@pytest.mark.parametrize("unit", ["eee", "aaa", "xxxx"])
def test_repeated_letter_units_are_rejected(unit):
    assert extractors._lab_row(f"Something 6 {unit}") is None


def test_a_legitimate_short_unit_is_not_caught_by_the_repeated_letter_guard():
    # "mg", "fl", "pg" etc. are two letters or non-repeating -- never rejected.
    assert extractors._lab_row("Hemoglobin 12.8 g/dL 11.5-15.5") is not None


def test_hemoglobin_survives_an_equals_sign_read_in_place_of_a_dash():
    row = extractors._lab_row("Hemoglobin (Hb) 12.8 g/dL 11.5 = 15.5")
    assert row is not None
    assert row["reference_range"] == "11.5-15.5"  # normalised, never fabricated


def test_tlc_and_platelet_ranges_survive_thousands_commas():
    tlc = extractors._lab_row("Total Leukocyte Count (TLC) 7,200 /uL 4,000 — 11,000")
    assert tlc is not None
    assert tlc["result"] == "7,200"
    assert tlc["reference_range"] == "4,000-11,000"

    platelets = extractors._lab_row("Platelet Count 245,000 Jul 150,000 — 450,000")
    assert platelets is not None
    assert platelets["result"] == "245,000"
    assert platelets["reference_range"] == "150,000-450,000"


def test_monocytes_survives_a_reference_range_that_is_pure_garbage():
    row = extractors._lab_row("Monocytes 6.1 % 7— j\\0)")
    assert row is not None
    assert row["test_name"] == "Monocytes"
    assert row["result"] == "6.1"
    assert row["unit"] == "%"
    # Genuinely unparseable: left blank, never guessed.
    assert row["reference_range"] == ""


def test_the_full_citycare_report_extracts_every_real_row_and_no_fake_one():
    rows = extractors.extract_lab(CITYCARE_LAB, TODAY).rows
    assert [r["test_name"] for r in rows] == [
        "Hemoglobin (Hb)",
        "Total Leukocyte Count (TLC)",
        "Platelet Count",
        "Monocytes",
        "Fasting Glucose",
    ]
    assert not any(r["test_name"].startswith("CityCare") for r in rows)
    assert rows[3]["reference_range"] == ""  # Monocytes: garbage range, blank not fabricated


@pytest.mark.parametrize(
    "line",
    [
        "Chughtai Lab   Report Date: 28/07/2026",  # a date, not a result
        "Ref. By : Dr. Sana Malik (General Physician)",
    ],
)
def test_non_result_lines_with_a_bare_number_are_still_rejected(line):
    """Guards the boundary the new tolerant-range parsing must not cross:
    a number embedded in ordinary prose/dates, with no unit and nothing
    range-shaped following it, must still be rejected outright."""
    assert extractors._lab_row(line) is None


# ── Lab report: reference range wrapped onto its own line ─────────────────
def test_a_reference_range_wrapped_onto_the_next_line_is_attached():
    text = "Hemoglobin 13.2 g/dL\n11.5-15.5"
    rows = extractors.extract_lab(text, TODAY).rows
    assert rows == [
        {
            "test_name": "Hemoglobin",
            "result": "13.2",
            "unit": "g/dL",
            "reference_range": "11.5-15.5",
        }
    ]


def test_a_wrapped_range_is_only_taken_when_the_row_did_not_already_have_one():
    text = "Hemoglobin 13.2 g/dL 11.5-15.5\n70-100"
    rows = extractors.extract_lab(text, TODAY).rows
    assert len(rows) == 1
    assert rows[0]["reference_range"] == "11.5-15.5"  # not overwritten by the stray next line


def test_a_wrapped_range_line_is_not_swallowed_into_the_next_real_result():
    text = "Hemoglobin 13.2 g/dL\nFasting Glucose 142 mg/dL 70-100"
    rows = extractors.extract_lab(text, TODAY).rows
    assert [r["test_name"] for r in rows] == ["Hemoglobin", "Fasting Glucose"]
    assert rows[0]["reference_range"] == ""  # the next line was a real test, not a range
    assert rows[1]["reference_range"] == "70-100"


def test_an_unrelated_short_line_does_not_become_a_wrapped_range():
    text = "Hemoglobin 13.2 g/dL\nDr. Ahmed"
    rows = extractors.extract_lab(text, TODAY).rows
    assert rows == [
        {"test_name": "Hemoglobin", "result": "13.2", "unit": "g/dL", "reference_range": ""}
    ]


def test_multiple_lab_rows_with_mixed_wrapped_and_inline_ranges():
    text = (
        "Hemoglobin 13.2 g/dL\n"
        "11.5-15.5\n"
        "Fasting Glucose 142 mg/dL 70-100\n"
        "Platelet Count 245,000 /uL"
    )
    rows = extractors.extract_lab(text, TODAY).rows
    assert [(r["test_name"], r["reference_range"]) for r in rows] == [
        ("Hemoglobin", "11.5-15.5"),
        ("Fasting Glucose", "70-100"),
        ("Platelet Count", ""),  # no range anywhere for this one -- left blank
    ]


# ── Vaccination record / other: text and date only ───────────────────────
def test_the_vaccination_extractor_reads_no_rows_because_its_doses_come_from_the_sections():
    result = extractors.get_extractor(DOC_TYPE_VACCINE)("BCG given 2026-03-02\nPolio dose 2", TODAY)
    # The extractor itself reads no rows for a vaccination record: its doses are proposed from
    # the document's sections (`proposals.py`).
    assert result.rows == []
    assert extractors.FIELDS[DOC_TYPE_VACCINE] == (
        "vaccine_name",
        "dose",
        "date_given",
        "batch_no",
        "site",
        "next_due",
    )
    assert result.document_date == date(2026, 3, 2)
    # Normalization/certainty is a prescription-only stage (see
    # `extract_prescription`); this type never touches it.
    assert result.field_certainty is None


# ── Other medical document: medicines, held to a stricter standard ───────
def _other(text: str) -> list[tuple[str, str]]:
    return [(r["medicine"], r["dosage"]) for r in extractors.extract_other(text, TODAY).rows]


def test_an_other_documents_primary_table_is_the_medicines_one_a_prescription_has():
    assert extractors.FIELDS[DOC_TYPE_OTHER] == extractors.FIELDS[DOC_TYPE_RX]
    # ...but a prescription must keep a medicine to be saved, and an "other" document need not
    assert extractors.REQUIRED_FIELD == {DOC_TYPE_RX: "medicine", DOC_TYPE_LAB: "test_name"}


def test_the_medicines_an_other_document_lists_are_proposed_and_its_date_is_found():
    text = (
        "Discharge Summary\nDate: 11 Aug 2026\nDiagnosis: Appendicitis\n"
        "1. Tab. Ceftriaxone 1 g twice daily x 5 days\n2. Tab. Paracetamol 500 mg 1-1-1"
    )
    result = extractors.extract_other(text, TODAY)
    assert [(r["medicine"], r["dosage"]) for r in result.rows] == [
        ("Ceftriaxone", "1 g"),
        ("Paracetamol", "500 mg"),
    ]
    assert result.document_date == date(2026, 8, 11)
    assert len(result.field_certainty) == len(result.rows)  # one certainty per kept row


def test_an_other_document_never_turns_arbitrary_text_into_medicines():
    """The prescription reader is built for a page of medicine lines; given a clinic letter or
    a lab-flavoured note it could read "HbA1c 7.2 %" as a medicine. Here it may not."""
    assert _other("BCG Tab. 500mg given\nDr. Ahmed\nHbA1c 7.2 % 4.0-5.6\n1. Polio dose 2") == []
    assert _other("Referral letter\nPlease see this patient for review.") == []


@pytest.mark.parametrize(
    "line",
    [
        "Glucose 142 mg/dL 70-100",
        "Cholesterol 230 mg/dL",
        "Urea 30 mg/dl",
        "ALT 40 U/L",
        "Sodium 140 mmol/L",
        "Hemoglobin 13.1 g/dL 12-15.5",
    ],
)
def test_a_laboratory_value_is_never_a_medicine_in_an_other_document(line):
    assert _other(line) == []


def test_a_medicine_without_a_dose_is_left_for_the_patient_to_add_and_a_syrup_strength_is_kept():
    assert _other("1. Syp. Calpol") == []
    assert _other("1. Syp. Augmentin 156 mg/5 ml 5 ml twice daily x 5 days") == [
        ("Augmentin", "156 mg/5 ml")
    ]


def test_lab_extraction_never_runs_the_prescription_normalization_stage():
    result = extractors.extract_lab(LAB_REPORT, TODAY)
    assert result.field_certainty is None
    assert result.rows == extractors.extract_lab(LAB_REPORT, TODAY).rows  # unchanged, deterministic


def test_the_vaccination_extractor_never_produces_rows_regardless_of_content():
    """A vaccination record's doses come from its sections, not from this extractor, so
    whatever the text holds, it never invents a row (and gets the date right, covered
    separately)."""
    noisy = "BCG Tab. 500mg given\nDr. Ahmed\nHbA1c 7.2 % 4.0-5.6\n1. Polio dose 2"
    assert extractors.get_extractor(DOC_TYPE_VACCINE)(noisy, TODAY).rows == []


# ── Selection by document type ───────────────────────────────────────────
def test_every_document_type_has_exactly_one_extractor():
    assert set(extractors.EXTRACTORS) == {
        DOC_TYPE_RX,
        DOC_TYPE_LAB,
        DOC_TYPE_VACCINE,
        DOC_TYPE_OTHER,
    }
    assert extractors.get_extractor(DOC_TYPE_RX) is extractors.extract_prescription
    assert extractors.get_extractor(DOC_TYPE_LAB) is extractors.extract_lab
    assert extractors.get_extractor(DOC_TYPE_VACCINE) is extractors.extract_text_only
    assert extractors.get_extractor(DOC_TYPE_OTHER) is extractors.extract_other


# ── Document date ────────────────────────────────────────────────────────
def test_document_date_prefers_a_labelled_date_and_ignores_birth_dates_and_the_future():
    text = "DOB: 14/03/1994\nDate: 11 Aug 2026\nNext visit 01/01/2027"
    assert extractors.find_document_date(text, TODAY) == date(2026, 8, 11)


def test_document_date_none_when_absent():
    assert extractors.find_document_date("no dates in here", TODAY) is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Prescription Date: 18-09-2025", date(2025, 9, 18)),
        ("Report Date: 28/07/2026", date(2026, 7, 28)),
        ("Sample Collection Date: 16-09-2025 10:24 AM", date(2025, 9, 16)),
        ("Date administered: 12/05/2026", date(2026, 5, 12)),
        ("Vaccination Date: 02-03-2026", date(2026, 3, 2)),
    ],
)
def test_the_relevant_labelled_date_is_found_for_every_document_type(text, expected):
    """The SAME general, keyword-driven date finder backs every extractor —
    a prescription date, a lab report/collection date, and a vaccination
    date are found the same way, not by hardcoding any one document's date
    or format."""
    assert extractors.find_document_date(text, TODAY) == expected


def test_document_date_is_never_fabricated_from_an_upload_or_today_date():
    """`find_document_date` only ever looks at the document's own text — it
    is never handed "today" or an upload timestamp to fall back to, so a
    document with no printed date correctly stays undated rather than
    silently taking on the day it was uploaded."""
    for extractor in (
        extractors.extract_prescription,
        extractors.extract_lab,
        extractors.extract_text_only,
    ):
        assert extractor("No date anywhere in this document.", TODAY).document_date is None


# ── Real manual-test regression: a date with its own separator OCR'd away ──
def test_a_labelled_date_survives_its_day_month_separator_being_dropped():
    """Manual-test regression: handwritten OCR read "Date : 15-08-2026" as
    approximately "Date . : 1508-2026" — the thinner separator between day
    and month was lost while the one before the year survived. The date is
    still found, generically, because the label is what makes this
    trustworthy -- not because "1508" is hardcoded anywhere."""
    assert extractors.find_document_date("Date . : 1508-2026", TODAY) == date(2026, 8, 15)


@pytest.mark.parametrize(
    "text",
    [
        "Ref No: 1508-2026",  # the same digit shape, but no date label at all
        "Invoice: 1508-2026",
        "1508-2026",  # no label, no context
    ],
)
def test_an_unlabelled_fused_date_is_never_guessed(text):
    """Unlike every other date shape here, the fused day-month pattern is
    ambiguous enough (many unrelated ID/reference-number shapes look just
    like it) that it is only ever trusted right after an explicit label."""
    assert extractors.find_document_date(text, TODAY) is None


def test_a_fused_date_still_respects_the_future_and_range_checks():
    assert extractors.find_document_date("Date: 3299-2026", TODAY) is None  # month 99: invalid
    assert extractors.find_document_date(f"Date: 0101-{TODAY.year + 1}", TODAY) is None  # future


# ── Frequency written 1+1+1; advice must not leak into a medicine ────────
def test_a_plus_separated_frequency_is_read_like_a_dash_separated_one():
    rows = extractors.parse_prescription_entities("Tab. Panadol 500mg 1+1+1 x 5 days")
    assert rows == [
        {"medicine": "Panadol", "dosage": "500mg", "frequency": "1+1+1", "duration": "5 days"}
    ]


def test_general_advice_after_the_last_medicine_never_becomes_its_duration():
    rows = extractors.parse_prescription_entities(
        "1. Tab. Zyrtec 10mg OD\nDrink plenty of fluids\nAvoid oily food for 2 days"
    )
    assert rows == [{"medicine": "Zyrtec", "dosage": "10mg", "frequency": "OD", "duration": ""}]


def test_a_medicines_own_wrapped_instruction_still_counts():
    rows = extractors.parse_prescription_entities(
        "1. Tab. Metformin 500mg\nTake 1 tablet twice daily\nfor 30 days"
    )
    assert rows[0]["frequency"] == "twice daily" and rows[0]["duration"] == "30 days"


# ── Which date is the document's own ─────────────────────────────────────
@pytest.mark.parametrize(
    "text",
    [
        "Sample Collected: 10-08-2026\nReceived Date: 10-08-2026\nReport Date: 11-08-2026",
        "Report Date: 11-08-2026\nCollection Date: 10-08-2026",
        "Specimen Date: 09-08-2026\nSample Received: 10-08-2026\nReported: 11-08-2026",
    ],
)
def test_a_reports_own_date_beats_the_earlier_collection_and_received_dates(text):
    """ "Sample Collected" and "Report Date" were both just "a labelled date", and the earlier one
    won: a report dated the 11th came out as the 10th."""
    assert extractors.find_document_date(text, TODAY) == date(2026, 8, 11)


def test_a_collection_date_is_still_used_when_it_is_the_only_date_the_report_gives():
    assert extractors.find_document_date("Sample Collected: 10-08-2026", TODAY) == date(2026, 8, 10)


def test_the_discharge_date_is_a_discharge_summarys_date_not_the_admission():
    text = "Date of Admission: 02-09-2026\nDate of Discharge: 06-09-2026"
    assert extractors.find_document_date(text, TODAY) == date(2026, 9, 6)


def test_the_date_of_vaccination_is_a_vaccination_cards_date_not_the_birth_or_next_due_date():
    text = "Date of Birth: 02-03-2024\nDate of Vaccination: 14-06-2026\nNext Dose Due: 14-07-2026"
    assert extractors.find_document_date(text, TODAY) == date(2026, 6, 14)


def test_any_labelled_date_still_beats_an_unlabelled_one():
    assert extractors.find_document_date("01-01-2026\nCollected: 10-08-2026", TODAY) == date(
        2026, 8, 10
    )


# ── A "Report Date" whose value the reader put in a later column ──────────
TWO_COLUMN_LAB_HEADER = """CityCare Diagnostics
LABORATORY REPORT
Patient Name : Ayesha Raza
Patient ID : CD-45872 Report Date
Age / Gender 28 Years / Female Lab No.
Ref. By : Dr. Sana Malik (General Physician)
Sample Collection Date : 15-09-2026 10:24 AM

16-09-2026 02:17 PM
L-78452"""


def test_a_report_date_whose_value_sits_alone_in_a_later_column_is_found():
    """The collection date is printed under its own label; the report date's label was left
    dangling and its value is the line that is nothing but a date. The report's own date is the
    16th — not the 15th, which is the earlier of the two."""
    assert extractors.find_document_date(TWO_COLUMN_LAB_HEADER, TODAY) == date(2026, 9, 16)
    lines = TWO_COLUMN_LAB_HEADER.splitlines()
    index, start, end = extractors.find_detached_report_date(lines)
    assert lines[index][start:end] == "16-09-2026"


@pytest.mark.parametrize(
    "text",
    [
        "Report Date: 16-09-2026\n\n15-09-2026 02:17 PM",  # the label has its value
        "Patient ID : CD-1 Report Date\nCollected on 15-09-2026",  # a line with other words
        "16-09-2026 02:17 PM\nPatient ID : CD-1 Report Date",  # a lone date BEFORE the label
        "Patient ID : CD-1 Report Date\nNo date here",
    ],
)
def test_a_lone_date_is_only_a_detached_report_date_after_a_label_left_dangling(text):
    assert extractors.find_detached_report_date(text.splitlines()) is None
