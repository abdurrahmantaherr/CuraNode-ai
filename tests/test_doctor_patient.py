"""D2 doctor patient page tests (`/{locale}/doctor/patient/{patient_id}`).

`test_gateway_*` in `test_consent.py` covers the gateway itself; this file
covers the server-rendered page built on it. The AC numbers below are this
page's test plan (AC-12…AC-14 are the spec's page criteria; AC-15…AC-18 extend
them), where a test also proves a spec rule the comment says which.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

import pytest
import structlog.testing
from app.db.models import (
    Allergy,
    AuditLog,
    ChronicCondition,
    ConsentGrant,
    Doctor,
    Encounter,
    LabReport,
    LabResult,
    Patient,
    PatientMedication,
    Profile,
    UserRole,
)
from app.db.types import utcnow, uuid7
from app.i18n.catalogue import missing_keys, translate
from markupsafe import escape
from sqlalchemy import select

from tests.conftest import TEST_PASSWORD, make_user


# ── Helpers ──────────────────────────────────────────────────────────────
async def login(client, email: str) -> None:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": TEST_PASSWORD})
    assert r.status_code == 200, r.text


def in_html(key: str, locale: str = "en", **params) -> str:
    return str(escape(translate(key, locale, **params)))


async def patient_of(db, user: Profile) -> Patient:
    stmt = select(Patient).where(Patient.user_id == user.id)
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one()


async def doctor_of(db, user: Profile) -> Doctor:
    stmt = select(Doctor).where(Doctor.user_id == user.id)
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one()


async def read_rows(db) -> list[AuditLog]:
    rows = await db.execute(
        select(AuditLog)
        .where(AuditLog.action == "record.read")
        .order_by(AuditLog.id)
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


@pytest.fixture
async def admin(db, clinic) -> Profile:
    return await make_user(db, email="adm@x.com", role=UserRole.CLINIC_ADMIN, clinic_id=clinic.id)


@pytest.fixture
async def verified_doctor(db, clinic, admin) -> Doctor:
    user = await make_user(
        db,
        email="dr@x.com",
        role=UserRole.DOCTOR,
        clinic_id=clinic.id,
        is_verified=True,
        verifier_id=admin.id,
    )
    return await doctor_of(db, user)


@pytest.fixture
async def patient(db) -> Patient:
    user = await make_user(db, email="pat@x.com", role=UserRole.PATIENT)
    p = await patient_of(db, user)
    p.full_name = "Ayesha Rahim"
    await db.commit()
    return p


@pytest.fixture
async def grant(db, patient, verified_doctor) -> ConsentGrant:
    row = ConsentGrant(
        id=uuid7(),
        patient_id=patient.id,
        grantee_type="doctor",
        grantee_id=verified_doctor.id,
        scope_sections=[],
        granted_at=utcnow(),
    )
    db.add(row)
    await db.commit()
    return row


async def seed_record(db, patient, doctor, clinic) -> None:
    db.add_all(
        [
            Allergy(id=uuid7(), patient_id=patient.id, substance="Penicillin", severity="severe"),
            ChronicCondition(id=uuid7(), patient_id=patient.id, name="Hypertension"),
            PatientMedication(id=uuid7(), patient_id=patient.id, name="Amlodipine"),
        ]
    )
    enc = Encounter(
        id=uuid7(),
        patient_id=patient.id,
        doctor_id=doctor.id,
        clinic_id=clinic.id,
        visit_datetime=utcnow() - timedelta(days=2),
        chief_complaint="Persistent headache",
    )
    report = LabReport(
        id=uuid7(),
        patient_id=patient.id,
        document_id=uuid7(),
        lab_name="City Lab",
        report_date=utcnow().date(),
        status="final",
    )
    db.add_all([enc, report])
    await db.flush()
    db.add(
        LabResult(
            id=uuid7(),
            report_id=report.id,
            test_name="Hemoglobin",
            value=Decimal("9.5"),
            unit="g/dL",
            flag="L",
        )
    )
    await db.commit()


def url(patient: Patient, locale: str = "en") -> str:
    return f"/{locale}/doctor/patient/{patient.id}"


# ── AC-12 — the record is shown ──────────────────────────────────────────
async def test_ac12_page_shows_every_section(client, db, clinic, patient, verified_doctor, grant):
    await seed_record(db, patient, verified_doctor, clinic)
    await login(client, "dr@x.com")
    r = await client.get(url(patient))
    assert r.status_code == 200
    html = r.text
    assert "Ayesha Rahim" in html
    for key in (
        "profile.section.allergies",
        "profile.section.conditions",
        "profile.section.medications",
        "doctor_patient.section.visits",
        "doctor_patient.section.labs",
    ):
        assert in_html(key) in html, key
    for text in (
        "Penicillin",
        "Hypertension",
        "Amlodipine",
        "Persistent headache",
        clinic.name,
        "City Lab",
        "Hemoglobin",
    ):
        assert text in html, text
    # An abnormal flag is shown prominently.
    assert 'class="badge badge--err">L<' in html


async def test_ac12_empty_record_shows_empty_states(client, patient, verified_doctor, grant):
    await login(client, "dr@x.com")
    html = (await client.get(url(patient))).text
    assert in_html("doctor_patient.no_allergies") in html
    assert in_html("doctor_patient.empty.visits") in html
    assert in_html("doctor_patient.empty.labs") in html


# ── AC-13 — What changed ─────────────────────────────────────────────────
async def test_ac13_first_view_then_changes(client, db, patient, verified_doctor, grant):
    await login(client, "dr@x.com")
    first = await client.get(url(patient))
    assert in_html("doctor_patient.changed.first_visit") in first.text

    # Recorded after the first view; a second apart keeps the order unambiguous.
    later = utcnow() + timedelta(seconds=1)
    db.add_all(
        [
            Allergy(id=uuid7(), patient_id=patient.id, substance="Aspirin", recorded_at=later),
            ChronicCondition(id=uuid7(), patient_id=patient.id, name="Asthma", recorded_at=later),
            PatientMedication(
                id=uuid7(), patient_id=patient.id, name="Salbutamol", recorded_at=later
            ),
        ]
    )
    await db.commit()

    second = await client.get(url(patient))
    assert in_html("doctor_patient.changed.first_visit") not in second.text
    assert in_html("doctor_patient.changed.new_allergy", name="Aspirin") in second.text
    assert in_html("doctor_patient.changed.new_condition", name="Asthma") in second.text
    assert in_html("doctor_patient.changed.new_medication", name="Salbutamol") in second.text


async def test_ac13_no_changes_message(client, patient, verified_doctor, grant):
    await login(client, "dr@x.com")
    await client.get(url(patient))
    second = await client.get(url(patient))
    assert in_html("doctor_patient.changed.none") in second.text


# ── AC-14 — Urdu ─────────────────────────────────────────────────────────
async def test_ac14_urdu_page_renders_rtl(client, db, clinic, patient, verified_doctor, grant):
    await seed_record(db, patient, verified_doctor, clinic)
    await login(client, "dr@x.com")
    r = await client.get(url(patient, "ur"))
    assert r.status_code == 200
    assert 'dir="rtl"' in r.text and 'lang="ur"' in r.text
    for key in ("doctor_patient.section.visits", "doctor_patient.changed.first_visit"):
        assert in_html(key, "ur") in r.text, key
    assert missing_keys()["ur"] == []


# ── AC-15 — revoke is immediate ──────────────────────────────────────────
async def test_ac15_revoked_grant_is_404_on_next_request(
    client, db, patient, verified_doctor, grant
):
    await login(client, "dr@x.com")
    assert (await client.get(url(patient))).status_code == 200

    row = await db.get(ConsentGrant, grant.id, populate_existing=True)
    row.revoked_at = utcnow()
    await db.commit()

    after = await client.get(url(patient))
    assert after.status_code == 404
    assert in_html("lookup.not_found") in after.text
    assert "Ayesha Rahim" not in after.text


# ── AC-16 — no grant is indistinguishable from no patient ────────────────
async def test_ac16_no_grant_matches_nonexistent_patient(client, db, patient, verified_doctor):
    await login(client, "dr@x.com")
    ungranted = await client.get(url(patient))
    unknown = await client.get(f"/en/doctor/patient/{uuid7()}")
    assert ungranted.status_code == unknown.status_code == 404
    assert ungranted.text == unknown.text
    assert "Ayesha Rahim" not in ungranted.text
    assert await read_rows(db) == []


# ── AC-17 — one record.read row per successful view ──────────────────────
async def test_ac17_each_view_writes_one_audit_row(client, db, patient, verified_doctor, grant):
    await login(client, "dr@x.com")
    for expected in (1, 2, 3):
        assert (await client.get(url(patient))).status_code == 200
        rows = await read_rows(db)
        assert len(rows) == expected
    detail = json.loads(rows[-1].detail)
    assert set(detail) == {"patient_id", "doctor_id", "grant_id", "purpose"}
    assert detail["patient_id"] == str(patient.id)
    assert rows[-1].actor_user_id == verified_doctor.user_id
    assert rows[-1].subject_patient_id == patient.id


# ── AC-18 / AC-13 (spec) — no passport number in logs or audit ───────────
async def test_ac18_no_passport_no_in_logs_or_audit(
    client, db, patient, verified_doctor, grant, caplog
):
    await login(client, "dr@x.com")
    with structlog.testing.capture_logs() as captured:
        assert (await client.get(url(patient))).status_code == 200
        assert (await client.get(url(patient))).status_code == 200
    dumped = json.dumps(captured, default=str) + caplog.text
    audit = "".join(r.detail for r in await read_rows(db))
    for secret in (patient.passport_no, verified_doctor.pmdc_number):
        assert secret not in dumped
        assert secret not in audit
