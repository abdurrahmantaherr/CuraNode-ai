"""The app works with OR without migration c8e5a41f9b27.

The shared database is migrated by its owner, not by the app, so a build with the records feature can
be running before the migration is applied. Until then the new columns do not exist, and nothing may
break: the timeline, the doctor's patient page, saving a document and deleting a saved one all behave
as they did before, nothing is copied, and the moment the migration is applied the copying starts by
itself. Here the four tables are put back to the shape the shared database has TODAY.
"""

from __future__ import annotations

import pytest
from app.db.models import (
    Base,
    Diagnosis,
    Prescription,
    PrescriptionItem,
    Profile,
    UserRole,
    VitalSign,
)
from app.documents import ocr, records
from sqlalchemy import func, select

from tests.conftest import make_user
from tests.test_documents import FakeOcr, login, upload_and_process
from tests.timeline_support import create_admin, create_doctor, create_patient, create_story, grant
from tests.timeline_support import login as login_as

# The four tables as the shared database has them before the migration.
OLD_SHAPE = {
    "prescription": """CREATE TABLE prescription (
        prescription_id CHAR(32) PRIMARY KEY,
        encounter_id CHAR(32) NOT NULL, patient_id CHAR(32) NOT NULL, doctor_id CHAR(32) NOT NULL,
        issue_date DATE NOT NULL, source VARCHAR(20) NOT NULL, status VARCHAR(20) NOT NULL,
        created_at DATETIME NOT NULL,
        CONSTRAINT prescription_source_check CHECK (source IN ('digital', 'ocr_upload')))""",
    "prescription_item": """CREATE TABLE prescription_item (
        item_id CHAR(32) PRIMARY KEY, prescription_id CHAR(32) NOT NULL, medicine_id CHAR(32) NOT NULL,
        dosage VARCHAR(50), frequency VARCHAR(50), duration_days INTEGER, route VARCHAR(30),
        instructions TEXT)""",
    "diagnosis": """CREATE TABLE diagnosis (
        diagnosis_id CHAR(32) PRIMARY KEY, encounter_id CHAR(32) NOT NULL, icd10_code VARCHAR(20),
        description VARCHAR(255), severity VARCHAR(20), is_chronic BOOLEAN NOT NULL)""",
    "vital_sign": """CREATE TABLE vital_sign (
        vital_id CHAR(32) PRIMARY KEY, encounter_id CHAR(32) NOT NULL UNIQUE,
        height_cm NUMERIC, weight_kg NUMERIC, bp_systolic INTEGER, bp_diastolic INTEGER,
        pulse_bpm INTEGER, temperature_c NUMERIC, spo2 INTEGER, recorded_at DATETIME NOT NULL)""",
}
CHANGED = list(OLD_SHAPE)
# The columns the old tables have (what a row keeps when it is carried across).
OLD_COLUMNS = {
    "prescription": "prescription_id, encounter_id, patient_id, doctor_id, issue_date, source, status, created_at",
    "prescription_item": "item_id, prescription_id, medicine_id, dosage, frequency, duration_days, route, instructions",
    "diagnosis": "diagnosis_id, encounter_id, icd10_code, description, severity, is_chronic",
    "vital_sign": "vital_id, encounter_id, height_cm, weight_kg, bp_systolic, bp_diastolic, pulse_bpm, temperature_c, spo2, recorded_at",
}


@pytest.fixture(autouse=True)
def fake_ocr(monkeypatch) -> FakeOcr:
    engine = FakeOcr()

    def get_engine(layout: str = "page") -> FakeOcr:
        engine.layout = layout
        return engine

    monkeypatch.setattr(ocr, "get_engine", get_engine)
    return engine


async def make_old_shape(engine) -> None:
    """Put the four tables back to the shape the shared database has TODAY, keeping their rows
    (the visit story is seeded first, with the models, and then carried across)."""
    async with engine.begin() as conn:
        for name in CHANGED:
            columns = OLD_COLUMNS[name]
            rows = (await conn.exec_driver_sql(f"SELECT {columns} FROM {name}")).all()
            await conn.exec_driver_sql(f"DROP TABLE {name}")
            await conn.exec_driver_sql(OLD_SHAPE[name])
            if rows:
                marks = ", ".join("?" for _ in rows[0])
                await conn.exec_driver_sql(
                    f"INSERT INTO {name} ({columns}) VALUES ({marks})", [tuple(r) for r in rows]
                )


@pytest.fixture
async def old_database(engine):
    """The database before the migration (nothing in the four tables yet)."""
    await make_old_shape(engine)


async def apply_the_migration(engine):
    """The same four tables as they are after it (built from the models)."""
    async with engine.begin() as conn:
        for name in CHANGED:
            await conn.exec_driver_sql(f"DROP TABLE {name}")
        await conn.run_sync(
            lambda c: Base.metadata.create_all(c, tables=[Base.metadata.tables[n] for n in CHANGED])
        )


@pytest.fixture
async def patient_user(client, db, old_database) -> Profile:
    user = await make_user(db, email="pat@x.com", role=UserRole.PATIENT)
    await login(client, "pat@x.com")
    return user


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
    {"index": 1000, "fields": {"diagnosis": "Type 2 diabetes mellitus"}},
]
RX_GROUPS = ["medicines", "diagnoses", "symptoms", "vitals", "allergies"]


async def count(db, model) -> int:
    return (await db.execute(select(func.count()).select_from(model))).scalar_one()


async def confirm(client, detail, **extra):
    body = {"rows": RX_ROWS, "groups": RX_GROUPS, **extra}
    r = await client.post(f"/api/v1/documents/{detail['id']}/confirm", json=body)
    assert r.status_code == 200, r.text
    return r


# ── Pages that read the visit tables ─────────────────────────────────────
async def test_the_patients_timeline_still_loads_without_the_migration(client, db, engine, clinic):
    admin = await create_admin(db, clinic)
    doctor = await create_doctor(db, clinic, admin)
    patient = await create_patient(db)
    story = await create_story(db, clinic, doctor, patient)
    visit_id, rx_id = story.visit.id, story.rx.id
    await make_old_shape(
        engine
    )  # the visit, its diagnosis and prescription now sit in the old tables
    await login_as(client, "pat@x.com")
    r = await client.get("/api/v1/me/timeline")
    assert r.status_code == 200, r.text
    refs = [item["ref"] for item in r.json()["items"]]
    assert f"encounter:{visit_id}" in refs and f"prescription:{rx_id}" in refs


async def test_the_doctors_patient_page_and_timeline_still_load_without_the_migration(
    client, db, engine, clinic
):
    admin = await create_admin(db, clinic)
    doctor = await create_doctor(db, clinic, admin)
    patient = await create_patient(db)
    await create_story(db, clinic, doctor, patient)
    await grant(
        db,
        patient,
        grantee_type="doctor",
        grantee_id=doctor.id,
        sections=["visits", "prescriptions", "lab_reports"],
    )
    patient_id = patient.id
    await make_old_shape(engine)
    await login_as(client, "dr@x.com")
    page = await client.get(f"/en/doctor/patient/{patient_id}")
    assert page.status_code == 200 and "Type 2 diabetes mellitus" in page.text
    timeline = await client.get(f"/api/v1/doctor/patients/{patient_id}/timeline")
    assert timeline.status_code == 200, timeline.text


# ── Saving and deleting a document ───────────────────────────────────────
async def test_confirming_still_saves_the_document_and_copies_nothing(
    client, db, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr)
    r = await confirm(client, detail, current_medicines=[0])
    assert r.json()["status"] == "finalized"
    for model in (Prescription, PrescriptionItem, Diagnosis, VitalSign):
        assert await count(db, model) == 0
    # the ticked medicine still joins the current list: that never needed the migration
    from app.db.models import PatientMedication

    assert await count(db, PatientMedication) == 1


async def test_the_web_confirm_says_nothing_about_a_failed_copy_when_the_database_is_not_ready(
    client, db, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr)
    form = {
        f"row-{row['index']}-{f['name']}": f["value"]
        for row in detail["rows"]
        for f in row["fields"]
    }
    r = await client.post(
        f"/en/patient/documents/{detail['id']}/confirm", data=form, follow_redirects=False
    )
    assert r.status_code == 303
    assert "records=failed" not in r.headers["location"]  # nothing failed: it was never attempted
    assert "Try again" not in (await client.get(r.headers["location"])).text


async def test_a_saved_document_can_still_be_deleted_without_the_migration(
    client, db, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr)
    await confirm(client, detail)
    r = await client.delete(f"/api/v1/documents/{detail['id']}?confirm=true")
    assert r.status_code in (200, 204), r.text


async def test_the_retry_does_nothing_and_breaks_nothing_without_the_migration(
    client, db, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr)
    await confirm(client, detail)
    r = await client.post(f"/en/patient/documents/{detail['id']}/records", follow_redirects=False)
    assert r.status_code == 303
    assert await count(db, Prescription) == 0


# ── The moment the migration is applied, it starts by itself ─────────────
async def test_copying_starts_by_itself_once_the_migration_is_applied(
    client, db, engine, fake_ocr, patient_user, monkeypatch
):
    first = await upload_and_process(client, fake_ocr)
    await confirm(client, first)
    assert await count(db, Prescription) == 0  # not ready: nothing copied, nothing failed

    await apply_the_migration(engine)
    # a "not ready" answer is only remembered for a few seconds: no restart needed
    clock = {"now": records.monotonic()}
    monkeypatch.setattr(records, "monotonic", lambda: clock["now"] + 60)

    second = await upload_and_process(client, fake_ocr)
    await confirm(client, second)
    assert await count(db, Prescription) == 1
    assert await count(db, Diagnosis) == 1

    # ...and the earlier document can be copied with the retry
    r = await client.post(f"/en/patient/documents/{first['id']}/records", follow_redirects=False)
    assert r.status_code == 303
    assert await count(db, Prescription) == 2


async def test_a_ready_answer_is_remembered_and_a_not_ready_one_is_not_for_long(
    db, engine, old_database, monkeypatch
):
    assert await records.is_ready(db) is False
    assert await records.is_ready(db) is False  # remembered for a moment: no second look

    await apply_the_migration(engine)
    assert await records.is_ready(db) is False  # still the remembered "no"
    monkeypatch.setattr(records, "monotonic", lambda: records._probe_state["asked_at"] + 60)
    assert await records.is_ready(db) is True
    assert await records.is_ready(db) is True  # and now it is remembered for good
