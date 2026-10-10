"""Vital signs (`documents/vitals.py`) and vaccine doses (`documents/vaccines.py`) read
from OCR text — pure functions, no app, no DB. Both are display-only: nothing here is
stored, converted or invented."""

from __future__ import annotations

import pytest
from app.documents.vaccines import ENTRY_KEYS, MAX_ENTRIES, extract_vaccine_entries
from app.documents.vitals import VITAL_KEYS, extract_vitals


def _vitals(line: str) -> dict[str, str]:
    return {k: v for k, v, _unreadable in extract_vitals([line])}


# ── Vital signs ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("BP: 120/80 mmHg", {"blood_pressure": "120/80 mmHg"}),
        ("B.P.=130/85", {"blood_pressure": "130/85"}),  # no unit printed, none added
        ("Blood Pressure 140 / 90", {"blood_pressure": "140/90"}),
        ("BP: 280/160 mmHg", {"blood_pressure": "280/160 mmHg"}),  # real, and must show
        ("Pulse: 78 bpm", {"pulse": "78 bpm"}),
        ("HR 88", {"pulse": "88"}),
        ("Pulse rate: 72/min", {"pulse": "72/min"}),
        ("Temp: 98.6 F", {"temperature": "98.6°F"}),
        ("Temperature 37.2 C", {"temperature": "37.2°C"}),
        ("Temp 99", {"temperature": "99"}),  # plausible, unit unknown: shown as written
        ("RR 18/min", {"respiratory_rate": "18/min"}),
        ("Resp. rate: 16", {"respiratory_rate": "16"}),
        ("SpO2: 98%", {"spo2": "98%"}),
        ("Oxygen saturation 97", {"spo2": "97%"}),
        ("Wt: 70 kg", {"weight": "70 kg"}),
        ("Weight 154 lbs", {"weight": "154 lb"}),
        ("Ht: 170 cm", {"height": "170 cm"}),
        ("Height 5'8\"", {"height": "5 ft 8 in"}),
        ("BMI: 24.2", {"bmi": "24.2"}),
        ("RBS: 142 mg/dl", {"blood_sugar": "142 mg/dL (RBS)"}),
        ("Blood sugar 7.8 mmol/L", {"blood_sugar": "7.8 mmol/L"}),
    ],
)
def test_each_vital_is_read_with_only_the_unit_the_document_printed(line, expected):
    assert _vitals(line) == expected


def test_several_vitals_on_one_line_are_each_read():
    line = "BP: 120/80 mmHg   Pulse: 78 bpm   Temp: 98.6 F   SpO2: 98%   Wt: 62 kg"
    assert _vitals(line) == {
        "blood_pressure": "120/80 mmHg",
        "pulse": "78 bpm",
        "temperature": "98.6°F",
        "spo2": "98%",
        "weight": "62 kg",
    }


def test_vitals_come_back_in_a_fixed_display_order_not_the_documents_order():
    found = extract_vitals(["SpO2: 98%", "Pulse: 78", "BP: 120/80"])
    assert [k for k, _v, _u in found] == ["blood_pressure", "pulse", "spo2"]
    assert [k for k, _v, _u in found] == [
        k for k in VITAL_KEYS if k in {"blood_pressure", "pulse", "spo2"}
    ]


@pytest.mark.parametrize(
    "line",
    [
        "BP 12/80",  # a pressure of 12 is an OCR slip
        "BP 80/120",  # systolic below diastolic
        "Pulse: 7",
        "Pulse: 400",
        "Temp: 12",
        "Temp 140 C",  # plausible only as Fahrenheit, and it says Celsius
        "SpO2: 400",
        "Weight: 900 kg",
        "Height: 12 cm",
        "BMI: 300",
        "Weight management advice: eat less",  # a label without a number
        "The patient is on pulse oximetry",
    ],
)
def test_an_impossible_reading_or_a_label_without_a_number_is_left_out(line):
    assert _vitals(line) == {}


def test_the_first_reading_of_a_vital_wins():
    assert _vitals("BP: 120/80") == {"blood_pressure": "120/80"}
    found = extract_vitals(["BP: 120/80", "BP: 150/95"])
    assert {k: v for k, v, _u in found} == {"blood_pressure": "120/80"}


# ── Vaccine doses ────────────────────────────────────────────────────────
def test_a_table_with_a_header_row_is_read_by_its_columns():
    entries = extract_vaccine_entries(
        [
            "Vaccine | Dose | Date | Batch No | Next Due",
            "BCG | 1 | 02-03-2024 | B778 |",
            "Pentavalent | 2 | 14-05-2024 | P9921 | 14-06-2024",
            "MR | Booster | 01-01-2025 | M22 |",
        ]
    )
    assert entries == [
        {"vaccine_name": "BCG", "dose": "Dose 1", "date_given": "02-03-2024", "batch_no": "B778"},
        {
            "vaccine_name": "Pentavalent",
            "dose": "Dose 2",
            "date_given": "14-05-2024",
            "batch_no": "P9921",
            "next_due": "14-06-2024",
        },
        {"vaccine_name": "MR", "dose": "Booster", "date_given": "01-01-2025", "batch_no": "M22"},
    ]


def test_plain_lines_are_read_by_their_labels_and_shapes():
    entries = extract_vaccine_entries(
        [
            "BCG given 2024-03-02",
            "Polio dose 2",
            "OPV 3  Date: 12-05-2024  Batch No: A1234  Left thigh",
            "Hepatitis B 2nd dose given 10/06/2024 next due 10/12/2024",
            "COVID-19 (Pfizer) Dose 1 - 12 Mar 2021",
        ]
    )
    assert entries == [
        {"vaccine_name": "BCG", "date_given": "2024-03-02"},
        {"vaccine_name": "Polio", "dose": "Dose 2"},
        {
            "vaccine_name": "OPV",
            "dose": "Dose 3",
            "date_given": "12-05-2024",
            "batch_no": "A1234",
            "site": "Left thigh",
        },
        {
            "vaccine_name": "Hepatitis B",
            "dose": "2nd dose",
            "date_given": "10/06/2024",
            "next_due": "10/12/2024",
        },
        {"vaccine_name": "COVID-19", "dose": "Dose 1", "date_given": "12 Mar 2021"},
    ]


@pytest.mark.parametrize(
    "line",
    [
        "Polio vaccine is important for all children under five years old in Pakistan.",
        "Please keep this card safe",
        "Immunization card",
        "Next visit: bring this card and the child's birth certificate",
        "Typhoid fever is spread by contaminated food and water, so wash your hands.",
    ],
)
def test_a_sentence_that_merely_mentions_a_vaccine_is_not_a_dose(line):
    assert extract_vaccine_entries([line]) == []


def test_a_bare_vaccine_name_in_a_short_list_item_is_kept():
    assert extract_vaccine_entries(["BCG", "OPV"]) == [
        {"vaccine_name": "BCG"},
        {"vaccine_name": "OPV"},
    ]


def test_a_column_the_card_does_not_show_is_absent_never_filled_in():
    (entry,) = extract_vaccine_entries(["Typhoid conjugate dose 1 given 05-05-2025"])
    assert set(entry) <= set(ENTRY_KEYS)
    assert "batch_no" not in entry and "site" not in entry and "next_due" not in entry


def test_an_identical_dose_is_listed_once_and_the_list_is_capped():
    assert len(extract_vaccine_entries(["BCG given 02-03-2024", "BCG given 02-03-2024"])) == 1
    many = [f"BCG dose {1 + i % 9} given 0{1 + i % 9}-03-20{10 + i // 9}" for i in range(80)]
    assert len(extract_vaccine_entries(many)) <= MAX_ENTRIES


# ── A certificate written as "Label: value" lines ─────────────────────────
def test_a_certificate_of_labelled_lines_is_one_complete_dose():
    lines = [
        "Vaccine: Pentavalent",
        "Dose: 2",
        "Date of Vaccination: 14-06-2026",
        "Batch No: P9921",
        "Site: Left thigh",
        "Next Dose Due: 14-07-2027",
    ]
    assert extract_vaccine_entries(lines) == [
        {
            "vaccine_name": "Pentavalent",
            "dose": "Dose 2",
            "date_given": "14-06-2026",
            "batch_no": "P9921",
            "site": "Left thigh",
            "next_due": "14-07-2027",
        }
    ]


def test_each_vaccine_label_opens_its_own_dose_and_keeps_its_own_details():
    lines = [
        "Vaccine: BCG",
        "Date: 02-03-2024",
        "Batch: B778",
        "Vaccine: Pentavalent",
        "Dose: 1",
        "Date: 14-05-2024",
        "Next dose: 14-06-2024",
    ]
    assert extract_vaccine_entries(lines) == [
        {"vaccine_name": "BCG", "date_given": "02-03-2024", "batch_no": "B778"},
        {
            "vaccine_name": "Pentavalent",
            "dose": "Dose 1",
            "date_given": "14-05-2024",
            "next_due": "14-06-2024",
        },
    ]


def test_several_labelled_facts_on_one_line_are_each_read():
    (entry,) = extract_vaccine_entries(
        ["Vaccine: Typhoid Conjugate (TCV)   Dose: 1   Batch: T4471"]
    )
    assert entry == {
        "vaccine_name": "Typhoid Conjugate (TCV)",
        "dose": "Dose 1",
        "batch_no": "T4471",
    }


def test_a_date_printed_before_the_vaccine_belongs_to_it_only_when_it_is_the_only_one():
    one = extract_vaccine_entries(["Date of Vaccination: 14-06-2026", "Vaccine: TCV", "Dose: 1"])
    assert one == [{"vaccine_name": "TCV", "dose": "Dose 1", "date_given": "14-06-2026"}]
    two = extract_vaccine_entries(
        ["Date of Vaccination: 14-06-2026", "Vaccine: TCV", "Dose: 1", "Vaccine: MR", "Dose: 1"]
    )
    assert all("date_given" not in entry for entry in two)  # whose date it is, is not guessed


def test_a_bare_date_ahead_of_any_vaccine_is_the_documents_not_a_doses():
    (entry,) = extract_vaccine_entries(["Date: 14-06-2026", "Vaccine: TCV", "Dose: 1"])
    assert "date_given" not in entry


def test_a_labelled_value_that_is_not_what_its_label_says_is_left_out():
    (entry,) = extract_vaccine_entries(
        ["Vaccine: BCG", "Date of Vaccination: yesterday", "Batch No: -", "Dose: 1"]
    )
    assert entry == {"vaccine_name": "BCG", "dose": "Dose 1"}


def test_a_lone_vaccine_label_reads_as_the_line_reader_always_did():
    assert extract_vaccine_entries(["Vaccine: BCG"]) == [{"vaccine_name": "BCG"}]


def test_a_table_card_is_still_read_by_its_columns():
    lines = ["Vaccine | Dose | Date | Batch No", "BCG | 1 | 02-03-2024 | B778"]
    assert extract_vaccine_entries(lines) == [
        {"vaccine_name": "BCG", "dose": "Dose 1", "date_given": "02-03-2024", "batch_no": "B778"}
    ]
