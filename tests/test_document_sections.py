"""OCR text -> named display sections for a FINALIZED document's View/Details
page (`documents/sections.py`) — pure functions, no app, no DB. This is a
presentation layer only: it never changes what `extractors.py` extracts or
what gets stored in `OcrExtraction`, only how the rest of the same OCR text
is labelled for display."""

from __future__ import annotations

import pytest
from app.db.models import DOC_TYPE_LAB, DOC_TYPE_OTHER, DOC_TYPE_RX, DOC_TYPE_VACCINE
from app.documents import sections

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
HbA1c 7.2 % 4.0 - 5.6
Fasting Glucose 142 mg/dL 70-100"""

FULL_PRESCRIPTION = """City Health Clinic
General Practice & Family Medicine
Healthy Families - Better Tomorrow
Shop No. 12, Main Boulevard
Lahore, Pakistan
Ph: 042-3587 1234
Email: info@cityhealthclinic.pk
Patient Name: Ayesha Khan
Age / Gender: 32 years / Female
Address: House No. 45, Model Town, Lahore
Date: 27-09-2026
Patient ID: 2026-4587
Rx
1. Paracetamol 500 mg
Take 1 tablet every 6 hours as needed
for fever or pain.
2. Amoxicillin 500 mg
Take 1 capsule three times daily
for 5 days.
Notes:
- Drink plenty of water.
- If symptoms persist or worsen, please visit again.
Dr. Sara Ahmed
MBBS, FCPS (Internal Medicine)
Consultant Physician"""


def _as_dict(items: list[tuple[str, str, bool]]) -> dict[str, str]:
    return {k: v for k, v, _unreadable in items}


def _unreadable_keys(items: list[tuple[str, str, bool]]) -> set[str]:
    return {k for k, _v, unreadable in items if unreadable}


def test_a_full_prescription_is_split_into_every_section():
    s = sections.structure_document(FULL_PRESCRIPTION, DOC_TYPE_RX)
    facility = _as_dict(s.facility)
    assert facility["hospital_name"] == "City Health Clinic"
    assert facility["phone"] == "042-3587 1234"
    assert facility["email"] == "info@cityhealthclinic.pk"
    # The address is the PATIENT's own (it appears after "Patient Name:" in
    # the fixture) — it must be credited to them, not the facility, even
    # though the facility ALSO has no labelled address of its own here.
    assert "address" not in facility

    patient = _as_dict(s.patient)
    assert patient["patient_name"] == "Ayesha Khan"
    assert patient["age"] == "32"  # the bare number, not "32 years"
    assert patient["gender"] == "Female"
    assert patient["patient_id"] == "2026-4587"
    assert patient["address"] == "House No. 45, Model Town, Lahore"

    provider = _as_dict(s.provider)
    assert provider["doctor_name"] == "Dr. Sara Ahmed"
    assert provider["qualification"] == "MBBS, FCPS"
    assert provider["specialization"] == "Internal Medicine"

    report = _as_dict(s.report)
    assert report["prescription_date"] == "27-09-2026"  # bare "Date:", no "Prescription" prefix

    assert s.notes == [
        "Drink plenty of water.",
        "If symptoms persist or worsen, please visit again.",
    ]


def test_a_facilitys_own_labelled_address_stays_with_the_facility():
    """When the facility's OWN address is genuinely labelled ABOVE the
    patient section (not just an unlabelled letterhead line), it must still
    be credited to the facility, not lost or misattributed to the patient."""
    text = """Northside Clinic
Address: 12 Garden Road, Karachi
Patient Name: Bilal Hussain
Address: House 9, DHA, Lahore"""
    s = sections.structure_document(text, DOC_TYPE_RX)
    assert _as_dict(s.facility)["address"] == "12 Garden Road, Karachi"
    assert _as_dict(s.patient)["address"] == "House 9, DHA, Lahore"


def test_a_repeated_dr_prefix_ocr_artifact_never_becomes_part_of_the_name():
    """A real, observed OCR artifact: "Dr." read twice in a row. The
    recovered name must be the genuine one, not "Dr. Dr"."""
    s = sections.structure_document("Dr. Dr. Sara Ahmed\nMBBS", DOC_TYPE_RX)
    assert _as_dict(s.provider)["doctor_name"] == "Dr. Sara Ahmed"


def test_a_damaged_colon_separator_still_matches_its_label():
    """OCR routinely turns ":" into "." — a label must not become invisible
    just because its own separator was misread."""
    s = sections.structure_document("Clinic\nPatient Name . Zara Ali", DOC_TYPE_RX)
    assert _as_dict(s.patient)["patient_name"] == "Zara Ali"


def test_a_field_with_no_label_in_the_text_is_simply_absent_never_guessed():
    """PRESCRIPTION has no hospital letterhead, no address, no email, no
    qualification line — none of that is invented."""
    s = sections.structure_document(PRESCRIPTION, DOC_TYPE_RX)
    facility = _as_dict(s.facility)
    assert "address" not in facility and "email" not in facility
    assert "qualification" not in _as_dict(s.provider)
    # But what IS there is found: the doctor's own signature line.
    assert _as_dict(s.provider)["doctor_name"] == "Dr. Mehreen Iqbal"


def test_two_labelled_fields_sharing_one_physical_ocr_line_are_split_apart():
    """Real OCR routinely puts more than one label on the same line
    ("Patient: Ayesha Raza   Age: 32") — the first field's value must not
    swallow the second field's label and value too."""
    s = sections.structure_document(PRESCRIPTION, DOC_TYPE_RX)
    patient = _as_dict(s.patient)
    assert patient["patient_name"] == "Ayesha Raza"
    assert patient["age"] == "32"


def test_a_facility_with_no_letterhead_line_names_the_lab_not_a_hospital():
    s = sections.structure_document(LAB_REPORT, DOC_TYPE_LAB)
    facility = _as_dict(s.facility)
    assert facility["lab_name"] == "Chughtai Lab"
    assert "hospital_name" not in facility
    assert _as_dict(s.report)["report_date"] == "28/07/2026"


def test_a_document_opening_with_the_doctors_own_signature_names_the_provider_not_the_facility():
    """PRESCRIPTION opens directly with "Dr. Mehreen Iqbal ..." — no clinic
    letterhead at all. That line must be read as the PROVIDER, not
    mistaken for a facility name."""
    s = sections.structure_document(PRESCRIPTION, DOC_TYPE_RX)
    assert s.facility == []  # no letterheaded name to show
    assert _as_dict(s.provider)["doctor_name"] == "Dr. Mehreen Iqbal"


def test_notes_preserve_every_line_including_ones_that_look_like_medicine_lines():
    text = FULL_PRESCRIPTION
    s = sections.structure_document(text, DOC_TYPE_RX)
    assert len(s.notes) == 2
    assert all(note for note in s.notes)


def test_a_document_with_no_notes_label_still_gets_advice_written_after_the_medicines():
    """No "Advice:" heading, but "Follow up after 2 weeks" sits after the last
    medicine and reads as advice — it is the advice section, not left out."""
    s = sections.structure_document(
        PRESCRIPTION.replace("Advice: plenty of fluids\n", ""), DOC_TYPE_RX
    )
    assert s.notes == ["Follow up after 2 weeks"]


def test_a_document_with_nothing_advice_like_has_an_empty_notes_list():
    text = PRESCRIPTION.replace("Advice: plenty of fluids\nFollow up after 2 weeks", "")
    assert sections.structure_document(text, DOC_TYPE_RX).notes == []


VITALS_RX = """City Health Clinic
Patient Name: Ayesha Khan   Age: 32   Gender: Female
Date: 27-09-2026
Complaint: fever and cough for 3 days   BP: 120/80 mmHg
Pulse: 78 bpm   Temp: 98.6 F   SpO2: 98%   Wt: 62 kg
Diagnosis: Acute bronchitis
Rx
1. Tab. Paracetamol 500 mg 1-1-1 x 3 days
Advice: rest and plenty of fluids
Dr. Sara Ahmed"""

VACCINE_CARD = """Lahore Immunization Centre
Patient Name: Baby Zainab   Date of Birth: 02-03-2024
Vaccine | Dose | Date | Batch No | Next Due
BCG | 1 | 02-03-2024 | B778 |
Pentavalent | 1 | 14-05-2024 | P9921 | 14-06-2024
Polio dose 2 given 14-05-2024
Notes: Keep this card safe."""

OTHER_DOC = """City Hospital
Discharge Summary
Patient Name: Ayesha Khan
Date: 05-10-2026
Diagnosis: Appendicitis
BP: 118/76 mmHg  Pulse: 80 bpm
Advice: avoid heavy lifting for 4 weeks
Dr. Sara Ahmed"""


def test_vaccination_and_other_documents_are_structured_too():
    vaccine = sections.structure_document(VACCINE_CARD, DOC_TYPE_VACCINE)
    assert _as_dict(vaccine.facility)["hospital_name"] == "Lahore Immunization Centre"
    assert _as_dict(vaccine.patient)["patient_name"] == "Baby Zainab"
    other = sections.structure_document(OTHER_DOC, DOC_TYPE_OTHER)
    assert _as_dict(other.facility)["hospital_name"] == "City Hospital"
    assert _as_dict(other.report)["diagnosis"] == "Appendicitis"
    assert other.notes == ["avoid heavy lifting for 4 weeks"]


def test_a_prescriptions_symptoms_diagnosis_and_vitals_are_each_read():
    s = sections.structure_document(VITALS_RX, DOC_TYPE_RX)
    report = _as_dict(s.report)
    # The complaint stops where the next labelled field (BP) starts.
    assert report["complaint"] == "fever and cough for 3 days"
    assert report["diagnosis"] == "Acute bronchitis"
    assert _as_dict(s.vitals) == {
        "blood_pressure": "120/80 mmHg",
        "pulse": "78 bpm",
        "temperature": "98.6°F",
        "spo2": "98%",
        "weight": "62 kg",
    }
    assert [k for k, _v, _u in s.vitals] == [
        "blood_pressure",
        "pulse",
        "temperature",
        "spo2",
        "weight",
    ]  # a fixed display order, not the order the document happened to print them in
    assert s.notes == ["rest and plenty of fluids"]


def test_symptoms_is_read_as_the_complaint():
    for label in ("Symptoms", "Presenting symptoms", "C/O", "Chief complaint"):
        text = f"Patient Name: A B\n{label}: sore throat\nDx: tonsillitis"
        s = sections.structure_document(text, DOC_TYPE_RX)
        assert _as_dict(s.report)["complaint"] == "sore throat", label


@pytest.mark.parametrize(
    "line",
    [
        # Real OCR lines from an instruction: "symptoms" is an ordinary word here, and the
        # period after it is the end of a sentence, not a label's separator.
        "Take 1 tablet at night for allergy symptoms. if",
        "Use the inhaler when you have symptoms - then rest",
        "If symptoms persist or worsen, please visit again.",
    ],
)
def test_the_word_symptoms_in_an_instruction_is_never_a_complaint(line):
    s = sections.structure_document(
        f"Patient Name: A B\n1. Tab. Cetirizine 10 mg\n{line}", DOC_TYPE_RX
    )
    assert "complaint" not in _as_dict(s.report)


def test_a_lab_report_gets_no_vitals_and_a_prescription_gets_no_vaccine_doses():
    assert sections.structure_document(LAB_REPORT, DOC_TYPE_LAB).vitals == []
    assert sections.structure_document(VITALS_RX, DOC_TYPE_RX).entries == []
    assert sections.structure_document(VITALS_RX, DOC_TYPE_LAB).entries == []


def test_vaccine_doses_are_listed_from_the_table_and_from_plain_lines():
    s = sections.structure_document(VACCINE_CARD, DOC_TYPE_VACCINE)
    assert s.entries == [
        {"vaccine_name": "BCG", "dose": "Dose 1", "date_given": "02-03-2024", "batch_no": "B778"},
        {
            "vaccine_name": "Pentavalent",
            "dose": "Dose 1",
            "date_given": "14-05-2024",
            "batch_no": "P9921",
            "next_due": "14-06-2024",
        },
        {"vaccine_name": "Polio", "dose": "Dose 2", "date_given": "14-05-2024"},
    ]
    assert s.notes == ["Keep this card safe."]


def test_a_document_title_is_read_only_from_a_short_label_free_heading():
    other = sections.structure_document(OTHER_DOC, DOC_TYPE_OTHER)
    assert _as_dict(other.report)["document_title"] == "Discharge Summary"
    titled = sections.structure_document(
        "Chughtai Lab\nHbA1c & Glucose Panel\nPatient Name: A B\nReport Date: 28/07/2026\n"
        "HbA1c 7.2 % 4.0-5.6",
        DOC_TYPE_LAB,
    )
    assert _as_dict(titled.report)["document_title"] == "HbA1c & Glucose Panel"
    # A prescription has no title, and a labelled line is never one.
    rx = sections.structure_document(VITALS_RX, DOC_TYPE_RX)
    assert "document_title" not in _as_dict(rx.report)
    labelled = sections.structure_document(
        "Report Date: 28/07/2026\nPatient Name: A B", DOC_TYPE_LAB
    )
    assert "document_title" not in _as_dict(labelled.report)


def test_impossible_vitals_are_left_out_not_shown():
    text = "Patient Name: A B\nBP: 12/80  Pulse: 7  Temp: 12  SpO2: 400"
    assert sections.structure_document(text, DOC_TYPE_RX).vitals == []


def test_empty_text_produces_empty_sections():
    s = sections.structure_document("", DOC_TYPE_RX)
    assert s.is_empty()


def test_an_age_gender_value_separated_by_a_corrupted_exclamation_mark_still_splits():
    """A real, observed OCR artifact: "/" between the age and gender VALUES
    (as opposed to the label separator, already tolerant) misread as "!"."""
    text = "Cry city Health Clinic\nAge ( Gender : 32 years ! Female\nPatient Name . Ayesha Khan"
    s = sections.structure_document(text, DOC_TYPE_RX)
    patient = _as_dict(s.patient)
    assert patient["age"] == "32"
    assert patient["gender"] == "Female"


def test_an_age_with_no_number_at_all_is_kept_as_is_never_invented():
    """No digits anywhere in the age value — nothing to safely extract, so
    the cleaned text is kept rather than dropped or guessed at."""
    s = sections.structure_document("Patient: Zara Ali\nAge: unknown", DOC_TYPE_RX)
    assert _as_dict(s.patient)["age"] == "unknown"


def test_ocr_garbage_after_a_notes_label_is_not_shown_as_a_note():
    text = "Patient Name: Ayesha Khan\nRx\n1. Metformin 500mg\nNotes:\n0 0 0 0 000\na b c d e f\nDrink plenty of water."
    s = sections.structure_document(text, DOC_TYPE_RX)
    assert s.notes == ["Drink plenty of water."]


def test_a_notes_section_that_is_pure_garbage_produces_no_notes_at_all():
    text = "Patient Name: Ayesha Khan\nNotes:\n0 0 0 0 000\n----"
    s = sections.structure_document(text, DOC_TYPE_RX)
    assert s.notes == []


def test_a_degraded_separator_that_defeats_the_next_label_no_longer_produces_garbage():
    """The real-failure mechanism: "Patient ID :" degraded to "Patient (0 -"
    used to leave `_cut_at_next_label` no boundary, so gender swallowed the
    rest of the line ("Male Patient (0 - 2026-3175"). A label starting is
    now recognised even with a junk qualifier, so gender is just "Male"; the
    garbled id itself is still not guessed at. The same scan's "Patient Name |"
    and "Address ;" (colon read as a pipe / semicolon) are read too."""
    text = """Patient Name | Muhammad Usman Date : 15-08-2026
Age / Gender : 28 years / Male Patient (0 - 2026-3175
Address ; House No. 22. Johar Town, Lahore"""
    s = sections.structure_document(text, DOC_TYPE_RX)
    patient = _as_dict(s.patient)
    assert patient["patient_name"] == "Muhammad Usman"
    assert patient["age"] == "28" and patient["gender"] == "Male"
    assert patient["address"] == "House No. 22. Johar Town, Lahore"
    assert "patient_id" not in patient  # a corrupted label is never read as the id
    assert _unreadable_keys(s.patient) == set()


def test_a_pipe_is_not_a_date_separator_so_a_table_header_is_not_a_date():
    s = sections.structure_document("Northside Lab\nDate | Test | Result", DOC_TYPE_LAB)
    assert "report_date" not in _as_dict(s.report)


def test_an_implausibly_long_value_is_rejected_for_a_narrow_field():
    """A gender/age/ID/date field realistically never runs this long — a
    value this size is almost always a swallowed boundary, not a real
    reading, regardless of what specifically caused it. Flagged unreadable
    rather than simply omitted."""
    text = (
        "Patient Name: Ayesha Khan\nGender: Female this is clearly not a real gender value at all"
    )
    s = sections.structure_document(text, DOC_TYPE_RX)
    assert _as_dict(s.patient).get("gender") == ""
    assert "gender" in _unreadable_keys(s.patient)


def test_a_recovered_value_replaces_an_earlier_unreadable_marker_in_place():
    """If a key is first rejected as too long, but a LATER line in the same
    document gives a clean, short value for the same key, the clean value
    wins — the unreadable marker never lingers in the output once a good
    reading exists. (`_Collector.add`'s in-place replacement.)"""
    text = (
        "Patient Name: Ayesha Khan\n"
        "Gender: Female this is clearly not a real gender value at all\n"
        "Gender: Female"
    )
    s = sections.structure_document(text, DOC_TYPE_RX)
    patient = _as_dict(s.patient)
    assert patient["gender"] == "Female"
    assert "gender" not in _unreadable_keys(s.patient)


def test_a_genuinely_long_address_is_still_captured_not_punished_by_the_cap():
    """The length cap is deliberately per-field — an address legitimately
    runs longer than a gender or a date, and must not be rejected just for
    being a normal, slightly verbose real address."""
    text = (
        "Patient Name: Ayesha Khan\n"
        "Address: House 45, Block C, Model Town Extension, Near City Hospital, Lahore"
    )
    s = sections.structure_document(text, DOC_TYPE_RX)
    assert (
        _as_dict(s.patient)["address"]
        == "House 45, Block C, Model Town Extension, Near City Hospital, Lahore"
    )


def test_a_bare_patient_label_still_stops_a_preceding_value():
    """Generalises beyond the one reproduced case: even when the SPECIFIC
    qualifier after "Patient" (ID/Name/No) doesn't survive OCR, recognising
    bare "Patient" as some-field-starting-here still stops a preceding
    field's value from running on, as long as the separator itself
    survived (unlike the double-corruption case above, where it didn't)."""
    text = "Gender: Male Patient: 2026-9999"
    s = sections.structure_document(text, DOC_TYPE_RX)
    assert _as_dict(s.patient)["gender"] == "Male"


def test_the_structuring_generalises_to_facility_and_names_never_seen_elsewhere():
    """A different name, phone, patient and doctor than any other fixture in
    this file, to prove nothing is hardcoded."""
    text = """Northside Diagnostic Center
Phone: 021-1112223
Patient Name: Bilal Hussain
Age: 47
Gender: Male
Test | Result | Unit | Reference Range
Creatinine 1.1 mg/dL 0.7-1.3
Notes:
- Slightly elevated, recommend follow-up in 3 months.
Doctor: Dr. Imran Qureshi"""
    s = sections.structure_document(text, DOC_TYPE_LAB)
    facility = _as_dict(s.facility)
    assert facility["lab_name"] == "Northside Diagnostic Center"
    assert facility["phone"] == "021-1112223"
    patient = _as_dict(s.patient)
    assert patient["patient_name"] == "Bilal Hussain" and patient["age"] == "47"
    assert _as_dict(s.provider)["doctor_name"] == "Dr. Imran Qureshi"
    assert s.notes == ["Slightly elevated, recommend follow-up in 3 months."]


def test_the_bare_patient_label_fallback_does_not_cut_a_notes_sentence_that_starts_with_the_word():
    """The bare "patient" fallback added to `_LABEL_WORDS` exists only to
    stop a NEIGHBOURING field's value at `_cut_at_next_label` — `_extract_notes`
    never calls that function at all, so a free-text remark that happens to
    start with "Patient" is never at risk of being truncated mid-sentence,
    whether it is the line right after the "Notes:" label or a later one."""
    text = """Dr. Samar Abbas  PMDC 11223-P
Patient: Hira Baig   Age: 29
Date: 02 Sep 2026
Diagnosis: Seasonal flu
Notes:
Patient advised to rest and drink plenty of fluids.
Patient complains of fever, follow up in one week if it persists."""
    s = sections.structure_document(text, DOC_TYPE_RX)
    assert s.notes == [
        "Patient advised to rest and drink plenty of fluids.",
        "Patient complains of fever, follow up in one week if it persists.",
    ]


def test_a_notes_sentence_starting_with_lowercase_patient_is_also_left_intact():
    """Same guarantee, case-insensitively, and via a different notes-start
    word ("Advice:") so the fix isn't accidentally specific to "Notes:"."""
    text = """City Clinic
Patient Name: Omar Farooq   Age: 51
Advice:
patient complains of fever and mild headache since yesterday."""
    s = sections.structure_document(text, DOC_TYPE_RX)
    assert s.notes == ["patient complains of fever and mild headache since yesterday."]


# ── Correct sections, each thing in its own section ──────────────────────
# Probed against realistic synthetic prescriptions: a notes word inside a
# medicine line opened "advice" mid-prescription and swept the medicines in;
# the clinic name was lost when the doctor came first or the name wrapped; a
# gender sharing a line with the age was dropped; advice with no heading was
# never found; footers could leak in.
MID_LINE_INSTRUCTIONS = """SHEIKH FAMILY CLINIC
General Practice & Family Medicine
Plot 12, Johar Town, Lahore  Ph: 042-35123456
Patient: Ali Khan   Age: 34   Gender: Male
Date: 12-09-2026
Rx
1. Tab. Panadol 500mg  1+1+1  x 5 days  as per instructions
2. Syp. Calpol 5ml TDS for 3 days
3. Cap. Amoxil 500mg BD x 7 days
Advice: Take plenty of fluids and rest
Follow up after 1 week
Dr. Sara Ahmed
MBBS, FCPS
Clinic Timings: Mon-Sat 5pm-9pm
Closed on Friday"""


def test_a_notes_word_inside_a_medicine_line_does_not_open_the_advice_section():
    s = sections.structure_document(MID_LINE_INSTRUCTIONS, DOC_TYPE_RX)
    assert s.notes == ["Take plenty of fluids and rest", "Follow up after 1 week"]
    assert not any("Calpol" in n or "Amoxil" in n for n in s.notes)


def test_advice_stops_at_the_next_labelled_field_and_never_swallows_it():
    text = "City Clinic\nPatient: Ali\nAdvice: Rest\nDiagnosis: Flu\nDate: 12-09-2026"
    s = sections.structure_document(text, DOC_TYPE_RX)
    assert s.notes == ["Rest"]
    report = _as_dict(s.report)
    assert report["diagnosis"] == "Flu" and report["prescription_date"] == "12-09-2026"


def test_advice_stops_at_page_footer_text_before_the_signature():
    text = "City Clinic\nPatient: Ali\nAdvice: Rest\nClinic Timings: 5pm-9pm\nwww.cityclinic.pk"
    assert sections.structure_document(text, DOC_TYPE_RX).notes == ["Rest"]


def test_advice_never_includes_a_medicine_line():
    text = "City Clinic\nPatient: Ali\nAdvice: Rest\nTab. Panadol 500mg BD x 3 days\nDr. Sara Ahmed"
    s = sections.structure_document(text, DOC_TYPE_RX)
    assert s.notes == ["Rest"]


def test_a_compound_heading_leaves_no_junk_in_the_first_advice_line():
    text = "City Clinic\nPatient: Ali\nNotes / Advice: Drink water\nDr. Sara Ahmed"
    assert sections.structure_document(text, DOC_TYPE_RX).notes == ["Drink water"]


def test_advice_with_no_heading_is_found_after_the_last_medicine():
    text = """Rakhshanda Hospital
Name: Zainab  Age: 7  Sex: F
Rx
Syp Calpol 120mg/5ml 5ml TDS x 3 days
Tab Zyrtec 10mg OD at night x 5 days
Drink plenty of fluids
Avoid oily food
Review after 3 days
Dr. Ayesha Malik (Paediatrician)"""
    s = sections.structure_document(text, DOC_TYPE_RX)
    assert s.notes == ["Drink plenty of fluids", "Avoid oily food", "Review after 3 days"]


def test_a_non_advice_line_after_the_medicines_is_not_taken_for_advice():
    text = """City Clinic
Patient: Ali
Tab. Panadol 500mg BD x 3 days
House 5, Block B, Lahore
Dr. Sara Ahmed"""
    assert sections.structure_document(text, DOC_TYPE_RX).notes == []


def test_the_clinic_name_is_found_when_the_doctor_comes_first():
    text = """Dr. Sara Ahmed MBBS FCPS
Consultant Physician
Al-Shifa Medical Centre
Main Boulevard, Gulberg, Lahore
Patient Name: Hina Raza  Age/Gender: 28 / Female"""
    s = sections.structure_document(text, DOC_TYPE_RX)
    assert _as_dict(s.facility)["hospital_name"] == "Al-Shifa Medical Centre"
    assert _as_dict(s.provider)["doctor_name"] == "Dr. Sara Ahmed"


def test_a_clinic_name_wrapped_over_two_lines_is_kept_whole():
    text = "City Health\nMedical Complex & Hospital\nPhone: 021-111222333\nPatient: Bilal"
    s = sections.structure_document(text, DOC_TYPE_RX)
    assert _as_dict(s.facility)["hospital_name"] == "City Health Medical Complex & Hospital"


def test_a_tagline_under_the_clinic_name_is_not_glued_onto_it():
    text = "SHEIKH FAMILY CLINIC\nGeneral Practice & Family Medicine\nPatient: Ali"
    s = sections.structure_document(text, DOC_TYPE_RX)
    assert _as_dict(s.facility)["hospital_name"] == "SHEIKH FAMILY CLINIC"


def test_a_document_that_opens_with_only_the_doctor_has_no_invented_clinic():
    s = sections.structure_document("Dr. Sara Ahmed MBBS\nPatient: Ali\nAge: 30", DOC_TYPE_RX)
    assert "hospital_name" not in _as_dict(s.facility)


def test_a_lab_name_is_found_the_same_way():
    text = "Report printed for you\nNorthside Diagnostic Center\nPatient Name: Bilal\nAge: 47"
    s = sections.structure_document(text, DOC_TYPE_LAB)
    assert _as_dict(s.facility)["lab_name"] == "Northside Diagnostic Center"


def test_age_and_gender_on_one_line_are_both_read():
    s = sections.structure_document("City Clinic\nName: Zainab  Age: 7  Sex: F", DOC_TYPE_RX)
    patient = _as_dict(s.patient)
    assert patient == {"patient_name": "Zainab", "age": "7", "gender": "F"}


def test_a_bare_name_label_is_the_patient_but_a_doctor_name_label_is_not():
    s = sections.structure_document(
        "City Clinic\nDoctor Name: Dr. Sara Ahmed\nName: Zainab", DOC_TYPE_RX
    )
    assert _as_dict(s.patient)["patient_name"] == "Zainab"


def test_complaint_and_diagnosis_land_in_the_prescription_details():
    text = (
        "City Clinic\nPatient: Ali\nC/O: Fever and sore throat for 3 days\nDiagnosis: Pharyngitis"
    )
    report = _as_dict(sections.structure_document(text, DOC_TYPE_RX).report)
    assert report["complaint"] == "Fever and sore throat for 3 days"
    assert report["diagnosis"] == "Pharyngitis"


def test_a_lab_interpretation_stops_at_the_signature():
    text = """Northside Diagnostic Center
Patient Name: Bilal
Interpretation: Mildly raised, repeat in 3 months.
Doctor: Dr. Imran Qureshi"""
    assert sections.structure_document(text, DOC_TYPE_LAB).notes == [
        "Mildly raised, repeat in 3 months."
    ]


# ── Bugs found by running real scans (synthetic prescriptions/lab report) ──
# The OCR lines below are copied from what Tesseract actually returned for
# those scans — letterheads read column by column, colons read as "=:",
# separators dropped, logos read as stray letters.
@pytest.mark.parametrize(
    ("letterhead", "expected"),
    [
        # name and address in two columns on ONE OCR line, with a logo letter in front
        ("G CITY HEALTH CLINIC Shop No. 12, Main Boulevard", "CITY HEALTH CLINIC"),
        ("SHEIKH FAMILY CLINIC Plot # 12, Street 4", "SHEIKH FAMILY CLINIC"),
        # a logo read as a symbol and a letter in front of the name
        ("a » Sunrise Medical Centre", "Sunrise Medical Centre"),
        # the address column was read BEFORE the name, and the name shares its
        # line with a phone fragment
        (
            (
                "Q Block B, 12th Main Road,\nGreen Park Extension,\nNew Delhi - 110016\n"
                "Sunrise Medical Centre —¢ +21114567 8900\n(pa info@sunrisemedicalcentre.in"
            ),
            "Sunrise Medical Centre",
        ),
        # a stylised logo: the name stacked over two lines with noise between
        (
            (
                "&\nQ Q Block B, 12th Main Road,\nSunrise 9 eee\n. hi - 110016\n"
                "Medical Centre eae\n& +9111 4567 8900"
            ),
            "Sunrise Medical Centre",
        ),
    ],
)
def test_the_facility_name_is_cut_away_from_its_neighbouring_column_and_logo_noise(
    letterhead, expected
):
    text = f"{letterhead}\nPatient Name : Priya Sharma\nAge/Gender : 29/ Female"
    s = sections.structure_document(text, DOC_TYPE_RX)
    assert _as_dict(s.facility)["hospital_name"] == expected


@pytest.mark.parametrize(
    "name", ["Medical Complex & Hospital", "Al-Noor Hospital", "St. Mary Hospital"]
)
def test_a_name_that_merely_contains_a_symbol_or_saint_is_not_cut_short(name):
    s = sections.structure_document(f"{name}\nPatient Name : Ali", DOC_TYPE_RX)
    assert _as_dict(s.facility)["hospital_name"] == name


def test_an_unrelated_name_word_above_is_not_glued_onto_a_complete_clinic_name():
    text = "Green Park Extension, New Delhi\nSunrise Medical Centre\nPatient Name : Priya"
    s = sections.structure_document(text, DOC_TYPE_RX)
    assert _as_dict(s.facility)["hospital_name"] == "Sunrise Medical Centre"


def test_labels_whose_separator_ocr_dropped_are_still_read_and_still_stop_the_value():
    text = """HEALTHCARE PLUS CLINIC
Patient Name : Fatima Ali Date 10-08-2026
Age/Gender : 45 years / Female Patient ID 2026-7890
Contact No. : 0300-9876543"""
    s = sections.structure_document(text, DOC_TYPE_RX)
    patient = _as_dict(s.patient)
    assert patient["patient_name"] == "Fatima Ali"
    assert patient["age"] == "45" and patient["gender"] == "Female"
    assert patient["patient_id"] == "2026-7890"
    assert patient["phone"] == "0300-9876543"
    assert _as_dict(s.report)["prescription_date"] == "10-08-2026"
    assert _unreadable_keys(s.patient) == set()


def test_a_colon_read_as_equals_colon_and_a_missing_colon_before_dr_are_both_read():
    text = """Sunrise Medical Centre
Prescription Date: 18-09-2025
Patient Name =: Priya Sharma Doctor Dr. Neha Kapoor
Age/Gender : 29/ Female Specialization : General Physician"""
    s = sections.structure_document(text, DOC_TYPE_RX)
    assert _as_dict(s.patient)["patient_name"] == "Priya Sharma"
    assert _as_dict(s.provider)["doctor_name"] == "Dr. Neha Kapoor"
    assert _as_dict(s.provider)["specialization"] == "General Physician"


def test_ordinary_words_after_a_label_word_are_not_taken_for_a_label():
    """The no-separator rule fires only when a date, an id or "Dr." follows —
    "Date of birth" / "Patient advised" / "Doctor visit" must stay plain text."""
    for value in ("Ali Khan date of birth", "Ali Khan Patient advised", "Ali Khan doctor visit"):
        assert sections._cut_at_next_label(value, 0) == value


def test_the_sign_off_after_the_advice_list_is_not_advice():
    text = """Sunrise Medical Centre
Patient Name : Priya Sharma
Tab. Metformin 500mg twice daily x 30 days
Advice:
- Maintain a healthy diet
- Regular exercise
- Follow up after 4 weeks
For any queries, contact the clinic.
Get well soon!
Dr. Neha Kapoor"""
    notes = sections.structure_document(text, DOC_TYPE_RX).notes
    assert notes == ["Maintain a healthy diet", "Regular exercise", "Follow up after 4 weeks"]


def test_contact_the_clinic_inside_real_advice_is_kept():
    text = (
        "City Clinic\nPatient: Ali\nAdvice:\n- Contact the clinic if fever persists\nDr. Sara Ahmed"
    )
    assert sections.structure_document(text, DOC_TYPE_RX).notes == [
        "Contact the clinic if fever persists"
    ]


@pytest.mark.parametrize("signature", ["P. J Dr. Ayesha Malik", "0 Dr. Dr. Sara Ahmed"])
def test_a_signature_line_with_ocr_scraps_in_front_ends_the_advice(signature):
    text = f"City Clinic\nPatient: Ali\nNotes:\n- Drink plenty of water.\n{signature}"
    assert sections.structure_document(text, DOC_TYPE_RX).notes == ["Drink plenty of water."]


def test_advice_that_names_a_doctor_is_not_mistaken_for_a_signature():
    text = "City Clinic\nPatient: Ali\nAdvice:\n- See Dr. Ahmed after 1 week\n- Rest"
    assert sections.structure_document(text, DOC_TYPE_RX).notes == [
        "See Dr. Ahmed after 1 week",
        "Rest",
    ]


def test_a_wrapped_advice_bullet_is_joined_and_ocr_scraps_are_trimmed():
    """One sentence wrapped over two lines ("...or worsen," / "please visit
    again.") — and the real scan read its first line with scraps at both ends."""
    text = (
        "City Clinic\nPatient: Ali\nNotes:\n- Drink plenty of water.\n"
        "'= |e symptoms persist or worsen, Oe\nplease visit again.\nDr. Sara Ahmed"
    )
    assert sections.structure_document(text, DOC_TYPE_RX).notes == [
        "Drink plenty of water.",
        "symptoms persist or worsen, please visit again.",
    ]


def test_a_misread_bullet_symbol_and_a_junk_fragment_do_not_become_advice():
    text = (
        "City Clinic\nPatient: Ali\nNotes:\n¢ Drink plenty of water.\nVo\n"
        "¢ Follow up after 2 weeks."
    )
    assert sections.structure_document(text, DOC_TYPE_RX).notes == [
        "Drink plenty of water.",
        "Follow up after 2 weeks.",
    ]


def test_a_numbered_advice_line_keeps_its_leading_number_and_real_short_words():
    text = "City Clinic\nPatient: Ali\nAdvice:\n- 2 glasses of water daily\n- Go to ER if worse"
    assert sections.structure_document(text, DOC_TYPE_RX).notes == [
        "2 glasses of water daily",
        "Go to ER if worse",
    ]


# ── Merging a stored AI reading over the deterministic sections ──────────
def _parsed(**overrides):
    base = sections.structure_document(
        "Cry city Health Clinic . Shop No. 12\n"
        "Patient Name : Ayesha Khan Date : 27-09-2026\n"
        "Age / Gender : 32 years / Female Patient ID : 2026-4587\n"
        "Notes:\n- Drink plenty of water.\nDr. Sara Ahmed",
        DOC_TYPE_RX,
    )
    for name, value in overrides.items():
        setattr(base, name, value)
    return base


def test_free_text_is_replaced_by_the_validated_ai_reading():
    merged = sections.apply_ai_sections(
        _parsed(),
        {
            "version": 1,
            "facility": {"hospital_name": "City Health Clinic"},
            "patient": {"patient_name": "Ayesha Khan"},
            "provider": {"doctor_name": "Dr. Sara Ahmed", "specialization": "Internal Medicine"},
        },
    )
    assert _as_dict(merged.facility)["hospital_name"] == "City Health Clinic"
    assert _as_dict(merged.provider)["specialization"] == "Internal Medicine"  # a new item
    assert next(k for k, _v, _u in merged.facility) == "hospital_name"  # position kept


def test_a_fixed_shape_value_the_extractor_read_is_never_overridden():
    merged = sections.apply_ai_sections(
        _parsed(),
        {
            "version": 1,
            "patient": {"patient_id": "9999-0000", "age": "99", "gender": "Male"},
            "report": {"prescription_date": "01-01-2000"},
        },
    )
    patient = _as_dict(merged.patient)
    assert patient["patient_id"] == "2026-4587" and patient["age"] == "32"
    assert patient["gender"] == "Female"
    assert _as_dict(merged.report)["prescription_date"] == "27-09-2026"


def test_a_fixed_shape_value_the_extractor_missed_or_could_not_read_is_filled_in():
    unreadable = _parsed(patient=[("patient_name", "Ayesha Khan", False), ("gender", "", True)])
    merged = sections.apply_ai_sections(
        unreadable, {"version": 1, "patient": {"gender": "Female", "age": "32"}}
    )
    assert _as_dict(merged.patient)["gender"] == "Female"
    assert _unreadable_keys(merged.patient) == set()
    assert _as_dict(merged.patient)["age"] == "32"  # missing -> appended


def test_the_ai_advice_replaces_the_advice_only_when_it_has_some():
    assert sections.apply_ai_sections(_parsed(), {"version": 1, "notes": ["Rest well"]}).notes == [
        "Rest well"
    ]
    assert sections.apply_ai_sections(_parsed(), {"version": 1, "notes": []}).notes == [
        "Drink plenty of water."
    ]


@pytest.mark.parametrize(
    "stored",
    [
        None,
        {},
        "not a dict",
        ["a", "list"],
        {"version": 2, "facility": {"hospital_name": "Future Format Clinic"}},
        {"facility": {"hospital_name": "No Version Clinic"}},
    ],
)
def test_a_missing_or_unrecognised_payload_changes_nothing(stored):
    parsed = _parsed()
    merged = sections.apply_ai_sections(parsed, stored)
    assert merged == parsed


def test_stored_json_is_data_so_bad_shapes_keys_and_lengths_are_ignored():
    merged = sections.apply_ai_sections(
        _parsed(),
        {
            "version": 1,
            "facility": {
                "hospital_name": 12345,  # not a string
                "<script>": "x",  # not in the vocabulary
                "phone": "9" * 500,  # longer than a phone number can be
                "email": "   ",  # blank
            },
            "patient": "not a dict",
            "notes": ["fine", 7, None, ""],
        },
    )
    assert _as_dict(merged.facility)["hospital_name"] == "Cry city Health Clinic"
    assert "<script>" not in _as_dict(merged.facility)
    assert "phone" not in _as_dict(merged.facility) and "email" not in _as_dict(merged.facility)
    assert merged.notes == ["fine"]


def test_merging_never_mutates_the_deterministic_result():
    parsed = _parsed()
    before = (list(parsed.facility), list(parsed.patient), list(parsed.notes))
    sections.apply_ai_sections(
        parsed, {"version": 1, "facility": {"hospital_name": "X Clinic"}, "notes": ["n"]}
    )
    assert (list(parsed.facility), list(parsed.patient), list(parsed.notes)) == before


def test_the_ai_vocabulary_is_exactly_what_structure_document_can_produce():
    produced = set()
    for doc_type in (DOC_TYPE_RX, DOC_TYPE_LAB):
        s = sections.structure_document(FULL_PRESCRIPTION + "\n" + LAB_REPORT, doc_type)
        for name in ("facility", "patient", "provider", "report"):
            produced |= {k for k, _v, _u in getattr(s, name)}
    allowed = set().union(*sections.AI_SECTION_KEYS.values())
    assert produced <= allowed


# ═══ A real lab scan read in table layout (synthetic patient) ═════════════
# Reading a results table row by row puts the header fields of two columns on one line, and
# the scan misreads a colon as "+". Every line below is real OCR output.
CBC_SCAN = """CityCare Diagnostics
Accurate Results. Better Care. & +92 42 3576 1234

LABORATORY REPORT
Patient Name : Ayesha Raza Sample Collection Date : 16-09-2025 10:24 AM
Patient ID : CD-45872 Report Date + 16-09-2025 02:17 PM
Age / Gender 28 Years / Female Lab No. L-78452
Ref. By : Dr. Sana Malik (General Physician)

COMPLETE BLOOD COUNT (CBC)

Test Name Result Unit Reference Range
Hemoglobin (Hb) 12.8 g/dL 11.5 = 15.5

Remarks:

All parameters are within normal limits.

Dr. Farah Ahmed
Consultant Pathologist
MBBS, FCPS (Pathology)

Scan for online report

This is a computer generated report and does not require a signature."""


def test_two_columns_sharing_a_line_do_not_swallow_each_other():
    s = sections.structure_document(CBC_SCAN, DOC_TYPE_LAB)
    patient = _as_dict(s.patient)
    assert patient["patient_name"] == "Ayesha Raza"  # not "Ayesha Raza Sample"
    assert patient["patient_id"] == "CD-45872"  # not "CD-45872 Report Date + 16-09-2025 ..."
    assert patient["age"] == "28" and patient["gender"] == "Female"  # the colon was dropped
    report = _as_dict(s.report)
    assert report["collection_date"] == "16-09-2025 10:24 AM"
    assert report["report_date"] == "16-09-2025 02:17 PM"  # "+" is a misread colon
    assert report["document_title"] == "COMPLETE BLOOD COUNT (CBC)"


def test_the_referring_doctor_is_kept_apart_from_the_one_who_signs():
    provider = _as_dict(sections.structure_document(CBC_SCAN, DOC_TYPE_LAB).provider)
    assert provider["referred_by"] == "Dr. Sana Malik (General Physician)"
    assert provider["doctor_name"] == "Dr. Farah Ahmed"
    assert provider["qualification"] == "MBBS, FCPS" and provider["specialization"] == "Pathology"


def test_the_remarks_stop_before_the_signature_and_the_page_footer():
    assert sections.structure_document(CBC_SCAN, DOC_TYPE_LAB).notes == [
        "All parameters are within normal limits."
    ]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # the value sits in another column: only the next field's NAME is left behind
        ("Patient ID : CD-45872 Report Date", "CD-45872"),
        ("Patient ID : CD-45872 Lab No.", "CD-45872"),
        ("Patient ID : CD-45872", "CD-45872"),
    ],
)
def test_a_next_fields_name_left_at_the_end_of_a_value_is_not_part_of_it(line, expected):
    patient = _as_dict(
        sections.structure_document(f"Patient Name : A B\n{line}", DOC_TYPE_LAB).patient
    )
    assert patient["patient_id"] == expected


def test_a_bare_date_label_never_takes_a_collection_or_received_date_for_the_documents_own():
    for label in ("Sample Collection Date", "Received Date", "Specimen Date"):
        s = sections.structure_document(f"Patient Name: A B\n{label} : 16-09-2025", DOC_TYPE_LAB)
        assert "report_date" not in _as_dict(s.report), label
    s = sections.structure_document("Patient Name: A B\nDate : 16-09-2025", DOC_TYPE_LAB)
    assert _as_dict(s.report)["report_date"] == "16-09-2025"


@pytest.mark.parametrize("label", ["Referred by", "Ref. By", "Ref By", "Referring Doctor"])
def test_every_way_of_naming_the_referring_doctor_is_read_as_one(label):
    s = sections.structure_document(f"Patient Name: A B\n{label} : Dr. Sana Malik", DOC_TYPE_LAB)
    provider = _as_dict(s.provider)
    assert provider["referred_by"] == "Dr. Sana Malik" and "doctor_name" not in provider


# ═══ Allergies ═════════════════════════════════════════════════════════════
# An allergy goes into the patient's safety record once confirmed, so the reader is strict:
# only a real label opens it, and "none" produces nothing.
def _allergies(*lines: str) -> list[dict[str, str]]:
    return sections._extract_allergies(list(lines))


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Allergies: Penicillin, Sulfa drugs", ["Penicillin", "Sulfa drugs"]),
        ("Known drug allergy: Aspirin", ["Aspirin"]),
        ("Food allergies: peanuts and shellfish", ["peanuts", "shellfish"]),
        ("Allergic to Latex", ["Latex"]),
        ("Patient Name: A B   Allergies: Latex   Age: 32", ["Latex"]),  # another field follows
        ("Allergies: Penicillin / Sulfa", ["Penicillin", "Sulfa"]),
        ("Allergies: Severe penicillin allergy", ["penicillin"]),  # "allergy" is not the allergen
    ],
)
def test_the_allergens_a_document_names_are_read(line, expected):
    assert [a["allergy_substance"] for a in _allergies(line)] == expected


def test_a_reaction_and_a_severity_are_read_when_the_document_gives_them():
    assert _allergies("Known drug allergy: Penicillin (rash, hives)") == [
        {"allergy_substance": "Penicillin", "allergy_reaction": "rash, hives"}
    ]
    assert _allergies("Allergy: Aspirin - severe rash") == [
        {"allergy_substance": "Aspirin", "allergy_reaction": "rash", "allergy_severity": "severe"}
    ]
    assert _allergies("Allergy: mild dust allergy") == [
        {"allergy_substance": "dust", "allergy_severity": "mild"}
    ]


@pytest.mark.parametrize(
    "statement",
    ["NKDA", "NKA", "No known allergies", "No known drug allergies", "Nil", "None", "N/A"],
)
def test_a_statement_that_there_are_no_allergies_produces_nothing(statement):
    assert _allergies(f"Allergies: {statement}") == []


@pytest.mark.parametrize(
    "line",
    [
        # the word inside a sentence, with a period or a dash that is not a label's separator
        "Take 1 tablet at night for allergy symptoms. if",
        "Use for allergy - twice daily",
        "If allergic reaction occurs, stop the medicine",
        "Allergy clinic opens at 9 am",
        # a value that is a sentence, not an allergen
        "Allergy: this patient has been told by several doctors over many years not to take it",
        "Allergies:",
    ],
)
def test_the_word_allergy_is_not_a_label_unless_it_is_one(line):
    assert _allergies(line) == []


def test_each_allergy_is_listed_once_and_the_list_is_capped():
    assert len(_allergies("Allergies: Latex, latex, Latex")) <= 2
    many = ", ".join(f"Substance{chr(65 + i)}" for i in range(20))
    assert len(_allergies(f"Allergies: {many}")) == 10


def test_allergies_are_read_for_a_prescription_and_other_documents_only():
    text = "Patient Name: A B\nAllergies: Latex"
    assert sections.structure_document(text, DOC_TYPE_RX).allergies == [
        {"allergy_substance": "Latex"}
    ]
    assert sections.structure_document(text, DOC_TYPE_OTHER).allergies
    assert sections.structure_document(text, DOC_TYPE_LAB).allergies == []
    assert sections.structure_document(text, DOC_TYPE_VACCINE).allergies == []


def test_allergies_are_never_a_displayed_section():
    """They are proposed as reviewable rows; a document with ONLY an allergy has no sections."""
    only_allergies = sections.DocumentSections(allergies=[{"allergy_substance": "Latex"}])
    assert only_allergies.is_empty()  # nothing to show as a section: it is a reviewable table
    s = sections.structure_document("Patient Name: A B\nAllergies: Latex", DOC_TYPE_RX)
    assert s.allergies == [{"allergy_substance": "Latex"}]
    assert not any("allerg" in key for key, _v, _u in s.patient + s.report + s.facility)


def test_the_ai_reading_supplies_allergies_only_when_the_text_reader_found_none():
    ai = {"version": 1, "allergies": [{"allergy_substance": "Latex"}, {"allergy_reaction": "x"}]}
    parsed = sections.structure_document("Patient Name: A B", DOC_TYPE_RX)
    assert sections.apply_ai_sections(parsed, ai).allergies == [{"allergy_substance": "Latex"}]
    found = sections.structure_document("Patient Name: A B\nAllergies: Penicillin", DOC_TYPE_RX)
    assert sections.apply_ai_sections(found, ai).allergies == [{"allergy_substance": "Penicillin"}]


def test_stored_allergy_json_is_data_so_bad_shapes_and_keys_are_ignored():
    ai = {
        "version": 1,
        "allergies": [
            "not a dict",
            {"allergy_substance": 7},
            {"allergy_substance": "x" * 500},
            {"allergy_substance": "Latex", "<script>": "y", "allergy_severity": "mild"},
        ],
    }
    parsed = sections.structure_document("Patient Name: A B", DOC_TYPE_RX)
    assert sections.apply_ai_sections(parsed, ai).allergies == [
        {"allergy_substance": "Latex", "allergy_severity": "mild"}
    ]


# ═══ The hospital and the laboratory, the document's title, and an "other" document's passages ═══
def _facility(text: str, doc_type: str) -> dict[str, str]:
    return _as_dict(sections.structure_document(text, doc_type).facility)


def test_a_lab_report_naming_a_hospital_and_a_laboratory_gives_each_its_own_field():
    text = "Aga Khan University Hospital\nClinical Laboratory\nPatient Name: A B\nReport Date: 28/07/2026"
    s = sections.structure_document(text, DOC_TYPE_LAB)
    assert _as_dict(s.facility) == {
        "lab_name": "Clinical Laboratory",
        "hospital_name": "Aga Khan University Hospital",
    }
    assert [k for k, _v, _u in s.facility] == ["lab_name", "hospital_name"]  # the lab leads


def test_a_lab_reports_only_name_is_a_laboratory_unless_it_says_it_is_a_hospital():
    assert _facility("Chughtai Lab\nPatient Name: A B", DOC_TYPE_LAB) == {
        "lab_name": "Chughtai Lab"
    }
    assert _facility("Chughtai\nPatient Name: A B", DOC_TYPE_LAB) == {"lab_name": "Chughtai"}
    assert _facility("City General Hospital\nPatient Name: A B", DOC_TYPE_LAB) == {
        "hospital_name": "City General Hospital"
    }


def test_an_other_document_names_a_laboratory_only_when_it_has_one():
    s = sections.structure_document(
        "City Hospital\nReference Laboratory\nUltrasound Report\nPatient Name: A B", DOC_TYPE_OTHER
    )
    assert _as_dict(s.facility) == {
        "hospital_name": "City Hospital",
        "lab_name": "Reference Laboratory",
    }
    assert [k for k, _v, _u in s.facility] == ["hospital_name", "lab_name"]  # the hospital leads
    assert _facility("City Hospital\nDischarge Summary\nPatient Name: A B", DOC_TYPE_OTHER) == {
        "hospital_name": "City Hospital"
    }


def test_a_prescription_and_a_vaccination_card_keep_one_hospital_name_as_they_always_had():
    assert _facility("Chughtai Lab\nPatient Name: A B", DOC_TYPE_RX) == {
        "hospital_name": "Chughtai Lab"
    }
    assert _facility("Lahore Immunization Centre\nPatient Name: A B", DOC_TYPE_VACCINE) == {
        "hospital_name": "Lahore Immunization Centre"
    }


@pytest.mark.parametrize(
    ("first_line", "doc_type"),
    [
        ("Medical Certificate", DOC_TYPE_OTHER),
        ("Discharge Summary", DOC_TYPE_OTHER),
        ("Complete Blood Count (CBC)", DOC_TYPE_LAB),
        ("Immunization Card", DOC_TYPE_VACCINE),
        ("Vaccination Certificate", DOC_TYPE_VACCINE),
    ],
)
def test_a_documents_title_is_never_taken_for_its_facility(first_line, doc_type):
    assert _facility(f"{first_line}\nPatient Name: A B", doc_type) == {}


def test_a_labelled_hospital_or_laboratory_is_taken_over_a_guess():
    text = "Hospital: City Hospital\nLab Name: Chughtai Lab\nPatient Name: A B"
    assert _facility(text, DOC_TYPE_LAB) == {
        "hospital_name": "City Hospital",
        "lab_name": "Chughtai Lab",
    }


def test_a_collection_point_or_a_lab_number_is_not_the_facility():
    text = (
        "Chughtai Lab\nSample Collection Centre: Gulshan Branch\nLab No: 88213\nPatient Name: A B"
    )
    assert _facility(text, DOC_TYPE_LAB) == {"lab_name": "Chughtai Lab"}
    assert _facility(text, DOC_TYPE_OTHER) == {"lab_name": "Chughtai Lab"}


def test_a_tagline_under_a_name_is_not_a_second_facility():
    text = "City Hospital\nQuality diagnostic services since\nPatient Name: A B"
    assert _facility(text, DOC_TYPE_OTHER) == {"hospital_name": "City Hospital"}


DISCHARGE = """City Hospital
Discharge Summary
Patient Name: Ayesha Khan
Diagnosis: Appendicitis
Procedure: Laparoscopic appendectomy
Findings: Inflamed, non-perforated appendix.
No collection in the pelvis.
Hospital Course: Uneventful recovery, tolerating
an oral diet on day 2.
Condition at Discharge: Stable
Advice: Avoid heavy lifting for 4 weeks.
Recommendations: Review in clinic after 10 days.
Dr. Sara Ahmed
MBBS, FCPS"""


def test_an_other_documents_headed_passages_are_read_under_its_own_headings():
    s = sections.structure_document(DISCHARGE, DOC_TYPE_OTHER)
    assert s.topics == [
        ("procedure", ["Laparoscopic appendectomy"]),
        ("findings", ["Inflamed, non-perforated appendix.", "No collection in the pelvis."]),
        ("hospital_course", ["Uneventful recovery, tolerating an oral diet on day 2."]),
        ("discharge_condition", ["Stable"]),
    ]
    assert {key for key, _lines in s.topics} <= set(sections.TOPIC_KEYS)


def test_advice_and_recommendations_are_one_list_and_end_at_the_signature():
    s = sections.structure_document(DISCHARGE, DOC_TYPE_OTHER)
    assert s.notes == ["Avoid heavy lifting for 4 weeks.", "Review in clinic after 10 days."]


def test_a_passage_ends_at_the_signature_and_at_the_next_heading():
    text = "Findings: Normal study.\nNo effusion.\nDr. Sara Ahmed\nMBBS"
    assert sections.structure_document(text, DOC_TYPE_OTHER).topics == [
        ("findings", ["Normal study.", "No effusion."])
    ]


def test_only_an_other_document_has_passages():
    for doc_type in (DOC_TYPE_RX, DOC_TYPE_LAB, DOC_TYPE_VACCINE):
        assert sections.structure_document(DISCHARGE, doc_type).topics == []


@pytest.mark.parametrize(
    "text",
    [
        "Patient Name: A B\nFindings were discussed with the patient.",  # a sentence, not a heading
        "Patient Name: A B\nFindings:\nDr. Sara Ahmed",  # a heading with nothing under it
        "Patient Name: A B\nThe procedure went well.",
    ],
)
def test_a_word_inside_a_sentence_or_a_heading_with_nothing_under_it_is_not_a_passage(text):
    assert sections.structure_document(text, DOC_TYPE_OTHER).topics == []


def test_a_passage_is_capped_so_it_can_never_swallow_the_page():
    body = "\n".join(f"Observation number {i} was noted here." for i in range(40))
    (topic,) = sections.structure_document(f"Findings: first\n{body}", DOC_TYPE_OTHER).topics
    assert len(topic[1]) <= 12


def test_a_stored_ai_reading_never_replaces_or_removes_the_passages():
    parsed = sections.structure_document(DISCHARGE, DOC_TYPE_OTHER)
    merged = sections.apply_ai_sections(parsed, {"version": 1, "notes": ["Rest."]})
    assert merged.topics == parsed.topics and merged.notes == ["Rest."]
    assert sections.DocumentSections().is_empty()
    assert not sections.DocumentSections(topics=[("findings", ["x"])]).is_empty()


def test_a_recommendation_heading_opens_the_advice_section_of_any_document():
    text = "Patient Name: A B\nRecommendations: Repeat the scan in 6 weeks."
    assert sections.structure_document(text, DOC_TYPE_OTHER).notes == [
        "Repeat the scan in 6 weeks."
    ]


def test_a_lab_report_whose_report_date_value_sits_in_a_later_column_still_has_one():
    """The real scan this came from: "Report Date" ends a header line and its value is a line of
    its own far below. The collection date is a different (labelled) date and is kept as printed."""
    text = (
        "CityCare Diagnostics\nPatient Name : A B\nPatient ID : CD-1 Report Date\n"
        "Sample Collection Date : 15-09-2026 10:24 AM\n\n16-09-2026 02:17 PM"
    )
    report = _as_dict(sections.structure_document(text, DOC_TYPE_LAB).report)
    assert report["report_date"] == "16-09-2026"
    assert report["collection_date"] == "15-09-2026 10:24 AM"
    # only a lab report reads a "Report Date" at all: a prescription's own label is another one
    rx = _as_dict(sections.structure_document(text, DOC_TYPE_RX).report)
    assert "report_date" not in rx and rx.get("prescription_date") != "16-09-2026"
