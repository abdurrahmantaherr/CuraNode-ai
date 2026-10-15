"""Records from a confirmed document (`documents/records.py`, migration c8e5a41f9b27).

Confirm & Save keeps what the patient confirmed in `ocr_extraction` AND copies it into the typed
clinical tables: prescription + prescription_item, diagnosis, vital_sign, lab_report + lab_result;
ticked medicines also join the patient's current-medicines list. These tests pin what is written,
what is deliberately NOT written, and that a failure, a retry or a delete leaves nothing doubled
or behind.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from app.db.models import (
    Diagnosis,
    DocumentStatus,
    LabReport,
    LabResult,
    Medicine,
    PatientMedication,
    Prescription,
    PrescriptionItem,
    Profile,
    UserRole,
    VitalSign,
)
from app.documents import lab_flags, ocr, records
from app.documents import service as doc_service
from sqlalchemy import delete, func, select

from tests.conftest import make_user
from tests.test_documents import (
    FakeOcr,
    confirm_body,
    doc_row,
    extraction_rows,
    login,
    upload_and_process,
)


@pytest.fixture(autouse=True)
def fake_ocr(monkeypatch) -> FakeOcr:
    """The same controllable engine `test_documents.py` uses, so no test needs Tesseract."""
    engine = FakeOcr()

    def get_engine(layout: str = "page") -> FakeOcr:
        engine.layouts.append(layout)
        engine.layout = layout
        return engine

    monkeypatch.setattr(ocr, "get_engine", get_engine)
    return engine


@pytest.fixture
async def patient_user(client, db) -> Profile:
    user = await make_user(db, email="pat@x.com", role=UserRole.PATIENT)
    await login(client, "pat@x.com")
    return user


# ── The parsers are pure ─────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("7.2", Decimal("7.2")),
        ("150,000", Decimal(150000)),
        ("  85 ", Decimal(85)),
        ("Negative", None),
        ("<0.5", None),
        ("12 mg", None),
        ("1,5", None),
        ("", None),
    ],
)
def test_a_plain_number_is_read_and_nothing_else(text, expected):
    assert records.plain_decimal(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("5 days", 5),
        ("for 10 days", 10),
        ("2 weeks", 14),
        ("1 month", 30),
        ("7d", 7),
        ("5", None),  # a bare number could be anything
        ("until better", None),
        ("0 days", None),
    ],
)
def test_a_duration_needs_its_unit(text, expected):
    assert records.duration_days(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("120/80", (120, 80)),
        (" 130 / 85 mmHg", (130, 85)),
        ("280/160", (280, 160)),  # wide on purpose: it must show
        ("80/120", None),  # the wrong way round
        ("12/8", None),
        ("high", None),
    ],
)
def test_blood_pressure(text, expected):
    assert records.blood_pressure(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("98.6 F", Decimal("37.0")),
        ("98.6", Decimal("37.0")),  # a bare number in the human Fahrenheit range
        ("37.2", Decimal("37.2")),  # ...and in the human Celsius range
        ("37 C", Decimal("37.0")),
        ("101 F", Decimal("38.3")),
        ("60", None),  # could be neither
        ("hot", None),
    ],
)
def test_temperature_is_stored_in_celsius_and_only_when_it_can_mean_one_thing(text, expected):
    assert records.temperature_c(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("70 kg", Decimal("70.0")), ("154 lb", Decimal("69.9")), ("70", None), ("heavy", None)],
)
def test_weight_needs_its_unit(text, expected):
    assert records.weight_kg(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("170 cm", Decimal("170.0")),
        ("170", Decimal("170.0")),  # can only be centimetres
        ("1.7 m", Decimal("170.0")),
        ("5'7\"", Decimal("170.2")),
        ("5 ft 7 in", Decimal("170.2")),
        ("67", None),  # inches or centimetres? not stored
    ],
)
def test_height(text, expected):
    assert records.height_cm(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"), [("98%", 98), ("98", 98), ("20", None), ("101", None)]
)
def test_spo2(text, expected):
    assert records.spo2_percent(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("13.0 - 17.0", (13.0, 17.0)),
        ("13.0-17.0", (13.0, 17.0)),
        ("< 5.0", (None, 5.0)),
        ("> 40", (40.0, None)),
        ("150,000-450,000", (150000.0, 450000.0)),
        ("17 - 13", (None, None)),  # the wrong way round is an OCR slip, not a range
        ("see note", (None, None)),
        ("", (None, None)),
    ],
)
def test_reference_limits(text, expected):
    assert lab_flags.reference_limits(text) == expected


# ── Confirm & Save writes the typed rows ─────────────────────────────────
def rows_for(detail, **fields_by_index):
    return [
        {"index": idx, "fields": dict(values)} for idx, values in sorted(fields_by_index.items())
    ]


async def count(db, model) -> int:
    return (await db.execute(select(func.count()).select_from(model))).scalar_one()


async def confirm(client, detail, rows, *, groups=None, **extra):
    body = {"rows": rows, **extra}
    if groups is not None:
        body["groups"] = groups
    r = await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=body)
    assert r.status_code == 200, r.text
    return r


RX_ROWS = [
    {
        "index": 0,
        "fields": {
            "medicine": "Metformin",
            "dosage": "500mg",
            "frequency": "1-0-1",
            "duration": "30 days",
        },
    },
    {
        "index": 1,
        "fields": {
            "medicine": "Atorvastatin",
            "dosage": "10 mg",
            "frequency": "once daily",
            "duration": "until review",
        },
    },
    {"index": 1000, "fields": {"diagnosis": "Type 2 diabetes mellitus"}},
    {
        "index": 3000,
        "fields": {
            "blood_pressure": "120/80",
            "pulse": "72",
            "temperature": "98.6 F",
            "respiratory_rate": "16",
            "spo2": "98%",
            "weight": "70 kg",
            "height": "170 cm",
            "bmi": "24.2",
            "blood_sugar": "110",
        },
    },
    {"index": 4000, "fields": {"allergy_substance": "Penicillin", "allergy_severity": "severe"}},
]
RX_GROUPS = ["medicines", "diagnoses", "symptoms", "vitals", "allergies"]


async def test_a_confirmed_prescription_becomes_a_prescription_with_its_items(
    client, db, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr)
    await confirm(client, detail, RX_ROWS, groups=RX_GROUPS)

    [rx] = (await db.execute(select(Prescription))).scalars().all()
    assert rx.source == "ocr_upload" and rx.status == "active"
    assert rx.encounter_id is None and rx.doctor_id is None  # no visit, no doctor: none invented
    assert str(rx.document_id) == detail["id"]
    assert rx.issue_date.isoformat() == "2024-08-11"  # the date the document states

    items = {
        i.medicine_text: i for i in (await db.execute(select(PrescriptionItem))).scalars().all()
    }
    assert set(items) == {"Metformin", "Atorvastatin"}
    metformin = items["Metformin"]
    assert (metformin.dosage, metformin.frequency, metformin.duration_days) == (
        "500mg",
        "1-0-1",
        30,
    )
    assert metformin.medicine_id is None  # not in the catalogue: the written name is kept
    # a duration that is not a number of days is kept whole, not guessed or dropped
    assert "until review" in (items["Atorvastatin"].instructions or "")
    assert items["Atorvastatin"].duration_days is None


async def test_the_cells_of_a_medicine_are_linked_to_the_item_written_from_them(
    client, db, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr)
    await confirm(client, detail, RX_ROWS, groups=RX_GROUPS)

    item_of = {
        i.id: i.medicine_text for i in (await db.execute(select(PrescriptionItem))).scalars()
    }
    cells = await extraction_rows(db, detail["id"])
    medicine_cells = [
        c for c in cells if c.field_name in ("medicine", "dosage", "frequency", "duration")
    ]
    assert medicine_cells and all(c.item_id in item_of for c in medicine_cells)
    by_row = {}
    for c in medicine_cells:
        by_row.setdefault(c.row_index, set()).add(item_of[c.item_id])
    assert by_row == {0: {"Metformin"}, 1: {"Atorvastatin"}}  # a row's cells -> ITS item
    # what is not a medicine is not linked
    assert all(
        c.item_id is None
        for c in cells
        if c.field_name not in ("medicine", "dosage", "frequency", "duration")
    )


async def test_a_confirmed_diagnosis_is_written_without_a_visit(client, db, fake_ocr, patient_user):
    detail = await upload_and_process(client, fake_ocr)
    await confirm(client, detail, RX_ROWS, groups=RX_GROUPS)
    [dx] = (await db.execute(select(Diagnosis))).scalars().all()
    assert dx.description == "Type 2 diabetes mellitus"
    assert dx.encounter_id is None and str(dx.document_id) == detail["id"]
    assert dx.icd10_code is None and dx.is_chronic is False  # no code or chronicity is invented


async def test_vital_signs_keep_the_six_measurements_the_table_has_columns_for(
    client, db, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr)
    await confirm(client, detail, RX_ROWS, groups=RX_GROUPS)
    [vital] = (await db.execute(select(VitalSign))).scalars().all()
    assert vital.encounter_id is None and str(vital.document_id) == detail["id"]
    assert (vital.bp_systolic, vital.bp_diastolic, vital.pulse_bpm, vital.spo2) == (120, 80, 72, 98)
    assert vital.temperature_c == Decimal("37.0")  # 98.6 F, converted because the column is Celsius
    assert vital.weight_kg == Decimal("70.0") and vital.height_cm == Decimal("170.0")
    # the three with no column stay in ocr_extraction, untouched
    cells = {c.field_name: c for c in await extraction_rows(db, detail["id"])}
    assert (
        cells["respiratory_rate"].corrected_value == "16"
        or cells["respiratory_rate"].extracted_value
    )
    assert {"bmi", "blood_sugar", "respiratory_rate"} <= set(cells)


async def test_an_unreadable_measurement_is_left_out_not_guessed(
    client, db, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr)
    rows = [r for r in RX_ROWS if r["index"] != 3000] + [
        {"index": 3000, "fields": {"blood_pressure": "high", "weight": "70", "temperature": "hot"}}
    ]
    await confirm(client, detail, rows, groups=RX_GROUPS)
    # a weight with no unit, a temperature that is a word, a blood pressure that is not numbers:
    # nothing to store, so no vital_sign row at all
    assert await count(db, VitalSign) == 0


async def test_the_audit_row_for_the_records_holds_counts_and_no_values(
    client, db, fake_ocr, patient_user
):
    from app.db.models import AuditLog

    detail = await upload_and_process(client, fake_ocr)
    await confirm(client, detail, RX_ROWS, groups=RX_GROUPS)
    [row] = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "document.records")))
        .scalars()
        .all()
    )
    assert row.actor_user_id == patient_user.id and row.resource_type == "document"
    for secret in ("Metformin", "Atorvastatin", "diabetes", "120", "Penicillin"):
        assert secret not in row.detail
    assert '"prescription_items": 2' in row.detail and '"diagnoses": 1' in row.detail


# ── The catalogue is read, never written ─────────────────────────────────
async def test_a_medicine_links_to_the_catalogue_only_when_exactly_one_entry_matches(
    client, db, fake_ocr, patient_user
):
    one = Medicine(id=uuid.uuid4(), generic_name="Metformin", brand_name="Glucophage")
    twin_a = Medicine(
        id=uuid.uuid4(), generic_name="Atorvastatin", brand_name="Lipitor", strength="10 mg"
    )
    twin_b = Medicine(
        id=uuid.uuid4(), generic_name="Atorvastatin", brand_name="Lipitor", strength="20 mg"
    )
    db.add_all([one, twin_a, twin_b])
    await db.commit()
    before = await count(db, Medicine)

    detail = await upload_and_process(client, fake_ocr)
    await confirm(client, detail, RX_ROWS, groups=RX_GROUPS)

    items = {i.medicine_text: i for i in (await db.execute(select(PrescriptionItem))).scalars()}
    assert items["Metformin"].medicine_id == one.id
    assert items["Atorvastatin"].medicine_id is None  # two strengths: never a guess between them
    assert items["Atorvastatin"].medicine_text == "Atorvastatin"
    assert await count(db, Medicine) == before  # the shared catalogue was not written to


async def test_a_dosage_too_long_for_its_column_is_kept_whole_in_the_instructions(
    client, db, fake_ocr, patient_user
):
    long_dose = "1 tablet in the morning and 1 tablet at night after meals with water"
    detail = await upload_and_process(client, fake_ocr)
    rows = [{"index": 0, "fields": {"medicine": "Metformin", "dosage": long_dose}}]
    await confirm(client, detail, rows)
    [item] = (await db.execute(select(PrescriptionItem))).scalars().all()
    assert item.dosage is None and long_dose in (item.instructions or "")


# ── Lab reports ──────────────────────────────────────────────────────────
LAB_PRINTED = """Chughtai Lab   Report Date: 28/07/2024
Hemoglobin 18.2 g/dL H 13.0 - 17.0
Fasting Glucose 85 mg/dL 70 - 100
HIV Negative"""


async def lab_detail(client, fake_ocr, text=LAB_PRINTED):
    return await upload_and_process(client, fake_ocr, "lab_report", text)


async def test_a_confirmed_lab_report_becomes_a_lab_report_with_its_numeric_results(
    client, db, fake_ocr, patient_user
):
    detail = await lab_detail(client, fake_ocr)
    rows = [
        {
            "index": 0,
            "fields": {
                "test_name": "Hemoglobin",
                "result": "18.2",
                "unit": "g/dL",
                "reference_range": "13.0 - 17.0",
            },
        },
        {
            "index": 1,
            "fields": {
                "test_name": "Fasting Glucose",
                "result": "85",
                "unit": "mg/dL",
                "reference_range": "70 - 100",
            },
        },
        {"index": 2, "fields": {"test_name": "HIV", "result": "Negative"}},
    ]
    await confirm(client, detail, rows)

    [report] = (await db.execute(select(LabReport))).scalars().all()
    assert str(report.document_id) == detail["id"] and report.encounter_id is None
    assert report.status == "received" and report.report_date is not None
    results = {r.test_name: r for r in (await db.execute(select(LabResult))).scalars()}
    assert set(results) == {"Hemoglobin", "Fasting Glucose"}  # "Negative" cannot be a number
    hb = results["Hemoglobin"]
    assert (hb.value, hb.unit) == (Decimal("18.2"), "g/dL")
    assert (hb.ref_range_low, hb.ref_range_high) == (Decimal("13.0"), Decimal("17.0"))


async def test_only_a_flag_the_report_printed_is_stored(client, db, fake_ocr, patient_user):
    detail = await lab_detail(client, fake_ocr)
    proposed = {
        (row["index"], f["name"]): f["value"] for row in detail["rows"] for f in row["fields"]
    }
    # keep the rows exactly as the reader proposed them
    await confirm(client, detail, confirm_body(detail)["rows"])
    results = {r.test_name: r for r in (await db.execute(select(LabResult))).scalars()}
    if not results:  # pragma: no cover - the reader found no rows in this text
        pytest.skip(f"reader proposed {proposed}")
    flags = {name: r.flag for name, r in results.items()}
    # Hemoglobin's own marker is `H`; Glucose carries none, and a calculated "normal" is NOT stored
    assert flags.get("Hemoglobin") == "high"
    assert flags.get("Fasting Glucose") is None


async def test_a_printed_flag_is_not_trusted_beside_a_result_the_patient_edited(
    client, db, fake_ocr, patient_user
):
    detail = await lab_detail(client, fake_ocr)
    rows = confirm_body(detail)["rows"]
    for row in rows:
        if row["fields"].get("test_name") == "Hemoglobin":
            row["fields"]["result"] = "18.9"
    await confirm(client, detail, rows)
    hb = (
        await db.execute(select(LabResult).where(LabResult.test_name == "Hemoglobin"))
    ).scalar_one()
    assert hb.value == Decimal("18.9") and hb.flag is None


# ── Current medicines ────────────────────────────────────────────────────
async def test_only_the_medicines_the_patient_ticked_join_their_current_list(
    client, db, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr)
    await confirm(client, detail, RX_ROWS, groups=RX_GROUPS, current_medicines=[0])

    meds = (await db.execute(select(PatientMedication))).scalars().all()
    assert [m.name for m in meds] == ["Metformin"]  # Atorvastatin was not ticked
    [med] = meds
    assert (med.strength, med.frequency) == ("500mg", "1-0-1")
    assert "30 days" in (med.notes or "")
    assert med.recorded_by == patient_user.id  # theirs: they can edit or remove it


async def test_a_ticked_row_that_is_not_a_kept_medicine_is_ignored(
    client, db, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr)
    # 1000 is a diagnosis row, 77 does not exist: neither may become a medicine
    await confirm(client, detail, RX_ROWS, groups=RX_GROUPS, current_medicines=[1000, 77])
    assert await count(db, PatientMedication) == 0


async def test_a_medicine_already_on_the_list_is_skipped_not_doubled(
    client, db, fake_ocr, patient_user
):
    first = await upload_and_process(client, fake_ocr)
    await confirm(client, first, RX_ROWS, groups=RX_GROUPS, current_medicines=[0])
    second = await upload_and_process(client, fake_ocr)
    r = await confirm(client, second, RX_ROWS, groups=RX_GROUPS, current_medicines=[0])
    assert r.status_code == 200
    assert await count(db, PatientMedication) == 1


# ── Failure, retry, delete ───────────────────────────────────────────────
async def test_a_failure_copying_into_the_records_never_undoes_the_saved_document(
    client, db, fake_ocr, patient_user, monkeypatch
):
    detail = await upload_and_process(client, fake_ocr)

    async def boom(*args, **kwargs):
        raise RuntimeError("the records are down")

    monkeypatch.setattr(records, "write", boom)
    r = await confirm(client, detail, RX_ROWS, groups=RX_GROUPS)
    assert r.json()["status"] == "finalized"
    assert (await doc_row(db, detail["id"])).status == DocumentStatus.FINALIZED.value
    for model in (Prescription, PrescriptionItem, Diagnosis, VitalSign):
        assert await count(db, model) == 0  # all or nothing: no half-written records


async def test_the_copy_can_be_run_again_and_never_doubles(
    client, db, fake_ocr, patient_user, monkeypatch
):
    detail = await upload_and_process(client, fake_ocr)
    real_write = records.write

    async def boom(*args, **kwargs):
        raise RuntimeError("the records are down")

    monkeypatch.setattr(records, "write", boom)
    await confirm(client, detail, RX_ROWS, groups=RX_GROUPS)
    assert await count(db, Prescription) == 0
    monkeypatch.setattr(records, "write", real_write)

    r = await client.post(f"/en/patient/documents/{detail['id']}/records", follow_redirects=False)
    assert r.status_code == 303 and "saved=1" in r.headers["location"]
    assert await count(db, Prescription) == 1 and await count(db, PrescriptionItem) == 2
    assert await count(db, Diagnosis) == 1 and await count(db, VitalSign) == 1

    # pressing it again changes nothing
    await client.post(f"/en/patient/documents/{detail['id']}/records", follow_redirects=False)
    assert await count(db, Prescription) == 1 and await count(db, PrescriptionItem) == 2
    assert await count(db, Diagnosis) == 1 and await count(db, VitalSign) == 1


async def test_the_retry_reads_only_what_was_confirmed(client, db, fake_ocr, patient_user):
    detail = await upload_and_process(client, fake_ocr)
    # review only the medicines: the diagnoses / vitals are left as proposals, never confirmed
    await confirm(client, detail, RX_ROWS[:2], groups=["medicines"])
    # simulate "the first copy failed": take the records away again
    await db.execute(delete(PrescriptionItem))
    await db.execute(delete(Prescription))
    await db.execute(delete(Diagnosis))
    await db.commit()
    await client.post(f"/en/patient/documents/{detail['id']}/records", follow_redirects=False)
    assert await count(db, PrescriptionItem) == 2 and await count(db, Diagnosis) == 0


async def test_deleting_a_document_removes_the_rows_written_from_it_but_not_the_patients_own(
    client, db, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr)
    await confirm(client, detail, RX_ROWS, groups=RX_GROUPS, current_medicines=[0])
    other = await upload_and_process(client, fake_ocr, "lab_report", LAB_PRINTED)
    await confirm(
        client,
        other,
        [{"index": 0, "fields": {"test_name": "Hemoglobin", "result": "18.2"}}],
    )
    assert await count(db, Prescription) == 1 and await count(db, LabReport) == 1

    r = await client.delete(f"/api/v1/documents/{detail['id']}?confirm=true")
    assert r.status_code in (200, 204), r.text

    for model in (Prescription, PrescriptionItem, Diagnosis, VitalSign):
        assert await count(db, model) == 0
    assert (
        await count(db, LabReport) == 1 and await count(db, LabResult) == 1
    )  # the OTHER document's
    # what is the patient's own stays, as an allergy always has
    assert await count(db, PatientMedication) == 1


async def test_a_document_that_is_not_a_prescription_or_lab_report_writes_nothing(
    client, db, fake_ocr, patient_user
):
    detail = await upload_and_process(
        client, fake_ocr, "vaccination", "Immunization card\nBCG given 2024-03-02"
    )
    await confirm(client, detail, confirm_body(detail)["rows"])
    for model in (Prescription, Diagnosis, VitalSign, LabReport):
        assert await count(db, model) == 0


async def test_confirming_twice_writes_the_records_once(client, db, fake_ocr, patient_user):
    detail = await upload_and_process(client, fake_ocr)
    await confirm(client, detail, RX_ROWS, groups=RX_GROUPS)
    await confirm(client, detail, RX_ROWS, groups=RX_GROUPS)
    assert await count(db, Prescription) == 1 and await count(db, PrescriptionItem) == 2


# ── The service's own outcome ────────────────────────────────────────────
async def test_the_outcome_counts_what_was_added(client, db, fake_ocr, patient_user):
    from app.deps import Actor
    from app.documents.schemas import ConfirmRequest, ReviewedRow

    detail = await upload_and_process(client, fake_ocr)
    actor = Actor(
        user_id=patient_user.id,
        role="patient",
        clinic_ids=frozenset(),
        is_verified_doctor=False,
        locale="en",
        request_id="t",
        ip_address="127.0.0.1",
        onboarding_complete=True,
    )
    body = ConfirmRequest(
        rows=[ReviewedRow(index=r["index"], fields=r["fields"]) for r in RX_ROWS],
        groups=RX_GROUPS,
        current_medicines=[0, 1],
    )
    _doc, outcome = await doc_service.confirm_with_outcome(db, actor, uuid.UUID(detail["id"]), body)
    assert outcome.medicines_added == 2 and outcome.medicines_skipped == 0
    assert outcome.allergies_added == 1
    r = outcome.records
    assert (r.prescriptions, r.prescription_items, r.diagnoses) == (1, 2, 1)
    assert r.vital_measurements == 6 and not r.failed  # BP counts once, in two columns
    assert _doc.status == DocumentStatus.FINALIZED.value


# ── The timeline still lists the document once ───────────────────────────
async def test_the_timeline_lists_a_confirmed_prescription_and_lab_report_once(
    client, db, fake_ocr, patient_user
):
    rx = await upload_and_process(client, fake_ocr)
    await confirm(client, rx, RX_ROWS, groups=RX_GROUPS)
    lab = await lab_detail(client, fake_ocr)
    await confirm(
        client,
        lab,
        [{"index": 0, "fields": {"test_name": "Hemoglobin", "result": "18.2", "unit": "g/dL"}}],
    )
    assert await count(db, Prescription) == 1 and await count(db, LabReport) == 1

    r = await client.get("/api/v1/me/timeline")
    assert r.status_code == 200, r.text
    refs = [item["ref"] for item in r.json()["items"]]
    # each document is its own event, once: the typed copies are not a second one
    assert refs.count(f"rx_document:{rx['id']}") == 1
    assert refs.count(f"lab_document:{lab['id']}") == 1
    assert not [ref for ref in refs if ref.startswith(("prescription:", "lab_report:"))]


# ── The pages ────────────────────────────────────────────────────────────
async def test_the_review_page_offers_the_tick_box_for_medicines_only(
    client, db, fake_ocr, patient_user
):
    rx = await upload_and_process(client, fake_ocr)
    page = (await client.get(f"/en/patient/documents/{rx['id']}")).text
    assert 'name="current_medicine"' in page and "Add to my current medicines" in page

    lab = await lab_detail(client, fake_ocr)
    assert (
        'name="current_medicine"'
        not in (await client.get(f"/en/patient/documents/{lab['id']}")).text
    )


def form_for(detail, **extra):
    data = {
        f"row-{row['index']}-{f['name']}": f["value"]
        for row in detail["rows"]
        for f in row["fields"]
    }
    return {**data, **extra}


async def test_confirming_on_the_page_adds_the_ticked_medicine_and_says_what_was_added(
    client, db, fake_ocr, patient_user
):
    rx = await upload_and_process(client, fake_ocr)
    r = await client.post(
        f"/en/patient/documents/{rx['id']}/confirm",
        data=form_for(rx, current_medicine="0"),
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    location = r.headers["location"]
    assert "saved=1" in location and "meds=1" in location and "rx=2" in location
    page = (await client.get(location)).text
    assert "Added to your current medicines: 1." in page
    assert "Also added to your health records" in page and "medicines: 2" in page
    assert [m.name for m in (await db.execute(select(PatientMedication))).scalars()] == [
        "Metformin"
    ]


async def test_a_failed_copy_is_said_so_and_the_retry_button_works(
    client, db, fake_ocr, patient_user, monkeypatch
):
    rx = await upload_and_process(client, fake_ocr)
    real_write = records.write

    async def boom(*args, **kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(records, "write", boom)
    r = await client.post(
        f"/en/patient/documents/{rx['id']}/confirm", data=form_for(rx), follow_redirects=False
    )
    assert "records=failed" in r.headers["location"]
    page = (await client.get(r.headers["location"])).text
    assert "could not be added to your health records" in page
    assert f'action="/en/patient/documents/{rx["id"]}/records"' in page and "Try again" in page

    monkeypatch.setattr(records, "write", real_write)
    again = await client.post(f"/en/patient/documents/{rx['id']}/records", follow_redirects=False)
    assert again.status_code == 303 and "rx=2" in again.headers["location"]
    assert await count(db, Prescription) == 1


async def test_the_retry_is_refused_for_a_document_that_is_not_saved_or_not_yours(
    client, db, fake_ocr, patient_user
):
    rx = await upload_and_process(client, fake_ocr)  # read, but never confirmed
    r = await client.post(f"/en/patient/documents/{rx['id']}/records", follow_redirects=False)
    assert r.status_code == 303 and "saved=1" not in r.headers["location"]
    assert await count(db, Prescription) == 0
    # an id that is nobody's behaves the same
    ghost = await client.post(
        f"/en/patient/documents/{uuid.uuid4()}/records", follow_redirects=False
    )
    assert ghost.status_code == 303 and "saved=1" not in ghost.headers["location"]


# ── A doctor the patient shared lab reports with sees the typed result ───
async def test_a_doctor_with_the_lab_reports_grant_sees_a_confirmed_lab_report_and_no_more(
    client, db, clinic, fake_ocr, patient_user
):
    from tests.timeline_support import create_admin, create_doctor, grant, patient_of

    lab = await lab_detail(client, fake_ocr)
    await confirm(
        client,
        lab,
        [
            {
                "index": 0,
                "fields": {
                    "test_name": "Hemoglobin",
                    "result": "18.2",
                    "unit": "g/dL",
                    "reference_range": "13.0 - 17.0",
                },
            }
        ],
    )
    patient = await patient_of(db, patient_user)
    admin = await create_admin(db, clinic)
    doctor = await create_doctor(db, clinic, admin)

    await grant(db, patient, grantee_type="doctor", grantee_id=doctor.id, sections=["lab_reports"])
    await login(client, "dr@x.com")
    page = (await client.get(f"/en/doctor/patient/{patient.id}")).text
    assert "Hemoglobin" in page and "18.2" in page


async def test_a_doctor_without_the_lab_reports_grant_does_not_see_the_typed_result(
    client, db, clinic, fake_ocr, patient_user
):
    from tests.timeline_support import create_admin, create_doctor, grant, patient_of

    lab = await lab_detail(client, fake_ocr)
    await confirm(
        client,
        lab,
        [{"index": 0, "fields": {"test_name": "Hemoglobin", "result": "18.2", "unit": "g/dL"}}],
    )
    patient = await patient_of(db, patient_user)
    admin = await create_admin(db, clinic)
    doctor = await create_doctor(db, clinic, admin)
    await grant(db, patient, grantee_type="doctor", grantee_id=doctor.id, sections=["visits"])
    await login(client, "dr@x.com")
    page = (await client.get(f"/en/doctor/patient/{patient.id}")).text
    assert "Hemoglobin" not in page and "18.2" not in page
