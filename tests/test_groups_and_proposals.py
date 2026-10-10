"""The tables of a document (`documents/groups.py`) and the rows proposed for the ones the
extractor does not read (`documents/proposals.py`) — pure functions, no app, no DB."""

from __future__ import annotations

import pytest
from app.db.models import DOC_TYPE_LAB, DOC_TYPE_OTHER, DOC_TYPE_RX, DOC_TYPE_VACCINE
from app.documents import extractors, groups, proposals


# ── groups ───────────────────────────────────────────────────────────────
def test_no_field_name_belongs_to_two_tables_and_every_one_fits_the_column():
    seen: dict[str, str] = {}
    for table in groups.ALL_GROUPS:
        for name in table.fields:
            assert name not in seen, f"{name} is in both {seen[name]} and {table.key}"
            seen[name] = table.key
            assert len(name) <= 20  # ocr_extraction.field_name is varchar(20)
    assert set(seen) == set(groups.ALL_FIELDS)


def test_a_row_is_recognised_as_belonging_to_a_table_by_its_fields_alone():
    assert groups.group_of({"medicine", "dosage"}) is groups.MEDICINES
    assert groups.group_of({"diagnosis"}) is groups.DIAGNOSES
    assert groups.group_of({"pulse", "bmi"}) is groups.VITALS
    assert groups.group_of({"medicine", "diagnosis"}) is None  # spans two tables
    assert groups.group_of({"nonsense"}) is None
    assert groups.group_of(set()) is None


def test_each_tables_row_indexes_live_in_a_block_of_their_own():
    extras = [
        t for t in groups.ALL_GROUPS if t not in (groups.MEDICINES, groups.RESULTS, groups.VACCINES)
    ]
    bases = [t.index_base for t in extras]
    assert len(set(bases)) == len(bases) and all(b >= groups.INDEX_BLOCK for b in bases)
    for table in extras:
        assert groups.row_in_block(table, table.index_base)
        assert groups.row_in_block(table, table.index_base + groups.INDEX_BLOCK - 1)
        assert not groups.row_in_block(table, table.index_base - 1)
        assert not groups.row_in_block(table, table.index_base + groups.INDEX_BLOCK)
        assert table.max_rows < groups.INDEX_BLOCK
    # a document type has ONE table in the first block, so those can never collide
    for doc_type, tables in groups.GROUPS.items():
        assert sum(1 for t in tables if t.index_base == 0) <= 1, doc_type


def test_which_tables_each_document_type_has():
    keys = {t: [g.key for g in gs] for t, gs in groups.GROUPS.items()}
    assert keys == {
        DOC_TYPE_RX: ["medicines", "diagnoses", "symptoms", "vitals", "allergies"],
        DOC_TYPE_LAB: ["results"],
        DOC_TYPE_VACCINE: ["vaccines"],
        # an "other" medical document is read like a prescription
        DOC_TYPE_OTHER: ["medicines", "diagnoses", "symptoms", "vitals", "allergies"],
    }
    assert groups.PRIMARY[DOC_TYPE_OTHER] is groups.MEDICINES
    # the field tables the rest of the code reads are derived, not repeated
    assert extractors.FIELDS[DOC_TYPE_RX] == groups.MEDICINES.fields
    assert extractors.FIELDS[DOC_TYPE_OTHER] == groups.MEDICINES.fields
    assert extractors.REQUIRED_FIELD == {DOC_TYPE_RX: "medicine", DOC_TYPE_LAB: "test_name"}


def test_only_a_prescription_and_a_lab_report_must_keep_a_row_in_their_primary_table():
    assert groups.min_rows(DOC_TYPE_RX, groups.MEDICINES) == 1
    assert groups.min_rows(DOC_TYPE_LAB, groups.RESULTS) == 1
    # an "other" document with no medicine, or a vaccination card with no readable dose, can
    # still be saved
    assert groups.min_rows(DOC_TYPE_OTHER, groups.MEDICINES) == 0
    assert groups.min_rows(DOC_TYPE_VACCINE, groups.VACCINES) == 0
    assert groups.min_rows(DOC_TYPE_RX, groups.DIAGNOSES) == 0


# ── proposals ────────────────────────────────────────────────────────────
def _rows(doc_type, text, ai=None):
    return {spec.key: rows for spec, rows in proposals.extra_rows(doc_type, text, ai)}


FULL = """City Health Clinic
Patient Name: Ayesha Khan   Age: 32
Complaint: fever, cough, sore throat
BP: 120/80 mmHg   Pulse: 78 bpm
Diagnosis: Acute bronchitis; Hypertension
Allergies: Penicillin (rash), Sulfa drugs
Rx
1. Tab. Paracetamol 500 mg 1-1-1 x 3 days"""


def test_a_prescriptions_other_tables_are_proposed_from_what_it_says():
    rows = _rows(DOC_TYPE_RX, FULL)
    assert list(rows) == ["diagnoses", "symptoms", "vitals", "allergies"]  # never the medicines
    assert rows["diagnoses"] == [{"diagnosis": "Acute bronchitis"}, {"diagnosis": "Hypertension"}]
    assert rows["symptoms"] == [
        {"symptom": "fever"},
        {"symptom": "cough"},
        {"symptom": "sore throat"},
    ]
    assert rows["allergies"] == [
        {"allergy_substance": "Penicillin", "allergy_reaction": "rash"},
        {"allergy_substance": "Sulfa drugs"},
    ]
    (vitals,) = rows["vitals"]
    assert vitals["blood_pressure"] == "120/80 mmHg" and vitals["pulse"] == "78 bpm"
    assert list(vitals) == list(groups.VITALS.fields) and vitals["bmi"] == ""


def test_a_symptom_is_split_at_commas_only_so_a_duration_stays_with_its_symptom():
    rows = _rows(DOC_TYPE_RX, "Patient Name: A B\nComplaint: fever and cough for 3 days")
    assert rows["symptoms"] == [{"symptom": "fever and cough for 3 days"}]


def test_a_diagnosis_is_split_at_semicolons_only_so_one_with_a_comma_stays_whole():
    rows = _rows(DOC_TYPE_RX, "Patient Name: A B\nDiagnosis: Diabetes mellitus, type 2")
    assert rows["diagnoses"] == [{"diagnosis": "Diabetes mellitus, type 2"}]


def test_nothing_is_invented_for_a_document_that_says_nothing():
    rows = _rows(DOC_TYPE_RX, "Rx\n1. Tab. Metformin 500 mg 1-0-1 x 30 days")
    assert rows["diagnoses"] == rows["symptoms"] == rows["allergies"] == []
    (vitals,) = rows["vitals"]  # the one row that is always there, to be filled in
    assert set(vitals.values()) == {""}


def test_a_statement_that_there_are_no_allergies_proposes_none():
    for statement in ("NKDA", "No known allergies", "Nil", "None"):
        rows = _rows(DOC_TYPE_RX, f"Patient Name: A B\nAllergies: {statement}")
        assert rows["allergies"] == [], statement


def test_a_vaccination_records_primary_table_is_its_doses():
    text = "Vaccine | Dose | Date\nBCG | 1 | 02-03-2024\nPolio dose 2 given 14-05-2024"
    (spec, rows) = proposals.extra_rows(DOC_TYPE_VACCINE, text)[0]
    assert spec is groups.VACCINES
    assert rows[0]["vaccine_name"] == "BCG" and rows[1]["vaccine_name"] == "Polio"


def test_an_other_document_gets_the_clinical_tables_and_a_lab_report_gets_none():
    assert list(_rows(DOC_TYPE_OTHER, "Discharge Summary\nDiagnosis: Appendicitis")) == [
        "diagnoses",
        "symptoms",
        "vitals",
        "allergies",
    ]
    assert proposals.extra_rows(DOC_TYPE_LAB, "HbA1c 7.2 % 4.0-5.6") == []


def test_tables_can_be_skipped():
    out = proposals.extra_rows(DOC_TYPE_RX, FULL, skip=frozenset({"vitals", "allergies"}))
    assert [spec.key for spec, _ in out] == ["diagnoses", "symptoms"]


def test_the_ai_reading_fills_a_table_the_text_reader_left_empty_but_never_adds_to_one_it_filled():
    ai = {
        "version": 1,
        "allergies": [{"allergy_substance": "Latex"}],
        "report": {"diagnosis": "Migraine"},
        "entries": [{"vaccine_name": "BCG"}],
    }
    scrambled = "Patient Name: A B\nRx\n1. Tab. Metformin 500 mg"
    rows = _rows(DOC_TYPE_RX, scrambled, ai)
    assert rows["allergies"] == [{"allergy_substance": "Latex"}]  # nothing deterministic: the AI's
    assert rows["diagnoses"] == [{"diagnosis": "Migraine"}]
    # ... but a list the text reader DID read is never mixed with the model's
    rows = _rows(DOC_TYPE_RX, FULL, ai)
    assert [r["allergy_substance"] for r in rows["allergies"]] == ["Penicillin", "Sulfa drugs"]
    # the model's vaccine doses are for a vaccination record only
    assert "vaccines" not in rows
    (_, doses) = proposals.extra_rows(DOC_TYPE_VACCINE, "Immunization card", ai)[0]
    assert doses == [{"vaccine_name": "BCG"}]


def test_a_table_never_proposes_more_rows_than_it_may_hold():
    text = "Patient Name: A B\nDiagnosis: " + "; ".join(f"Dx {i}" for i in range(18))
    assert len(_rows(DOC_TYPE_RX, text)["diagnoses"]) == groups.DIAGNOSES.max_rows


@pytest.mark.parametrize("junk", ["", "   ", "\n\n"])
def test_empty_text_proposes_only_the_empty_vitals_row(junk):
    out = {spec.key: rows for spec, rows in proposals.extra_rows(DOC_TYPE_RX, junk)}
    assert out["diagnoses"] == [] and len(out["vitals"]) == 1


def test_every_field_name_survives_the_review_forms_own_parser():
    """The page form posts `row-<index>-<field>`; a field its parser cannot read is silently
    dropped — `spo2` once was, because its name has a digit, so an edit to it was lost."""
    from app.web.documents import _ROW_FIELD

    for name in groups.ALL_FIELDS:
        match = _ROW_FIELD.match(f"row-3000-{name}")
        assert match is not None and match.group(2) == name, name
