"""Seeding helpers for the medical-timeline tests (P3). Everything here is synthetic.

Rows are written straight into the in-memory database in the exact shape the application itself
leaves them: a finalized document the patient uploaded has its cells marked `reviewed_at` (that is
what Confirm & Save does), a document a doctor wrote has none to confirm, an upload still awaiting
review has neither.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

from app.db.models import (
    AccessLog,
    Allergy,
    ChronicCondition,
    ConsentGrant,
    Diagnosis,
    Doctor,
    Document,
    Encounter,
    Medicine,
    OcrExtraction,
    OcrJob,
    Patient,
    Prescription,
    PrescriptionItem,
    Profile,
    UserRole,
)
from app.db.types import utcnow, uuid7
from app.documents import groups
from sqlalchemy import select

from tests.conftest import TEST_PASSWORD, make_user

# The tests' own clock: fixed, so "newest first" never depends on when the suite runs.
ZONE = UTC


def at(year: int, month: int, day: int, hour: int = 9, minute: int = 0) -> datetime:
    """A UTC instant (what the database stores)."""
    return datetime(year, month, day, hour, minute, tzinfo=ZONE)


async def login(client, email: str) -> None:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": TEST_PASSWORD})
    assert r.status_code == 200, r.text


async def patient_of(db, user: Profile) -> Patient:
    stmt = select(Patient).where(Patient.user_id == user.id)
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one()


async def doctor_of(db, user: Profile) -> Doctor:
    stmt = select(Doctor).where(Doctor.user_id == user.id)
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one()


async def access_rows(db) -> list[AccessLog]:
    rows = await db.execute(select(AccessLog).execution_options(populate_existing=True))
    return list(rows.scalars().all())


async def grant(db, patient, *, grantee_type, grantee_id, sections, **kw) -> ConsentGrant:
    row = ConsentGrant(
        id=uuid7(),
        patient_id=patient.id,
        grantee_type=grantee_type,
        grantee_id=grantee_id,
        scope_sections=list(sections),
        granted_at=kw.pop("granted_at", utcnow()),
        **kw,
    )
    db.add(row)
    await db.commit()
    return row


# ── Doctor-side rows ─────────────────────────────────────────────────────
async def add_visit(
    db,
    patient: Patient,
    doctor: Doctor,
    clinic,
    *,
    when: datetime,
    complaint: str | None = None,
    notes: str | None = None,
    ai_summary: str | None = None,
    diagnoses: tuple[tuple[str | None, str | None], ...] = (),
) -> Encounter:
    visit = Encounter(
        id=uuid7(),
        patient_id=patient.id,
        doctor_id=doctor.id,
        clinic_id=clinic.id,
        visit_datetime=when,
        chief_complaint=complaint,
        clinical_notes=notes,
        ai_summary=ai_summary,
    )
    db.add(visit)
    await db.flush()
    for description, code in diagnoses:
        db.add(
            Diagnosis(id=uuid7(), encounter_id=visit.id, description=description, icd10_code=code)
        )
    await db.commit()
    return visit


async def add_prescription(
    db,
    patient: Patient,
    doctor: Doctor,
    visit: Encounter,
    *,
    issued: date,
    items: tuple[tuple[str, str | None, str | None, str | None, int | None], ...],
    source: str = "digital",
) -> Prescription:
    """`items`: (brand name, strength, dosage, frequency, duration in days)."""
    rx = Prescription(
        id=uuid7(),
        encounter_id=visit.id,
        patient_id=patient.id,
        doctor_id=doctor.id,
        issue_date=issued,
        source=source,
    )
    db.add(rx)
    await db.flush()
    for brand, strength, dosage, frequency, days in items:
        medicine = Medicine(
            id=uuid7(), generic_name=brand.lower(), brand_name=brand, strength=strength
        )
        db.add(medicine)
        await db.flush()
        db.add(
            PrescriptionItem(
                id=uuid7(),
                prescription_id=rx.id,
                medicine_id=medicine.id,
                dosage=dosage,
                frequency=frequency,
                duration_days=days,
            )
        )
    await db.commit()
    return rx


# ── Documents ────────────────────────────────────────────────────────────
# doc_type -> its primary table's field names (the real ones, from `groups`)
_FIELDS = {doc_type: spec.fields for doc_type, spec in groups.PRIMARY.items() if spec}


async def add_document(
    db,
    patient: Patient,
    *,
    doc_type: str,
    uploaded_by: uuid.UUID,
    rows: tuple[dict[str, str], ...] = (),
    status: str = "finalized",
    document_date: date | None = None,
    uploaded_at: datetime | None = None,
    ocr_text: str | None = None,
    reviewed: bool = True,
    engine: str = "tesseract",
    name: str = "scan.pdf",
) -> Document:
    """A document with one OCR job and one cell per field of each row.

    `reviewed` marks the cells as confirmed by the patient (`reviewed_at`), which is what
    "Confirm & Save" does; a document a doctor wrote is built with `reviewed=False` and
    `engine="doctor_entry"`."""
    doc = Document(
        id=uuid7(),
        patient_id=patient.id,
        doc_type=doc_type,
        object_key=f"patients/{patient.id}/{uuid7()}.pdf",
        mime_type="application/pdf",
        size_bytes=1234,
        uploaded_by=uploaded_by,
        uploaded_at=uploaded_at or utcnow(),
        original_filename=name,
        status=status,
        ocr_text=ocr_text,
        document_date=document_date,
        finalized_at=utcnow() if status == "finalized" else None,
    )
    db.add(doc)
    await db.flush()
    job = OcrJob(id=uuid7(), document_id=doc.id, status="done", engine=engine)
    db.add(job)
    await db.flush()
    for index, row in enumerate(rows):
        for field in _FIELDS[doc_type]:
            db.add(
                OcrExtraction(
                    id=uuid7(),
                    job_id=job.id,
                    field_name=field,
                    extracted_value=row.get(field),
                    row_index=index,
                    reviewed_by=uploaded_by if reviewed else None,
                    reviewed_at=utcnow() if reviewed else None,
                )
            )
    await db.commit()
    return doc


METFORMIN = {
    "medicine": "Metformin",
    "dosage": "500 mg",
    "frequency": "twice daily",
    "duration": "30 days",
}
HBA1C = {"test_name": "HbA1c", "result": "7.2", "unit": "%", "reference_range": "4.0-5.6"}
HEPATITIS_B = {"vaccine_name": "Hepatitis B", "dose": "1", "date_given": "2026-02-10"}


def days_ago(n: int) -> datetime:
    return utcnow() - timedelta(days=n)


# ── The people and the story the tests share ─────────────────────────────
async def create_admin(db, clinic) -> Profile:
    return await make_user(db, email="adm@x.com", role=UserRole.CLINIC_ADMIN, clinic_id=clinic.id)


async def create_doctor(db, clinic, admin: Profile) -> Doctor:
    user = await make_user(
        db,
        email="dr@x.com",
        role=UserRole.DOCTOR,
        clinic_id=clinic.id,
        is_verified=True,
        verifier_id=admin.id,
    )
    doctor = await doctor_of(db, user)
    doctor.full_name, doctor.specialty = "Sana Malik", "Endocrinology"
    await db.commit()
    return doctor


async def create_patient(db, email: str = "pat@x.com") -> Patient:
    return await patient_of(db, await make_user(db, email=email, role=UserRole.PATIENT))


async def create_story(db, clinic, doctor: Doctor, patient: Patient) -> SimpleNamespace:
    """One patient's whole record: a visit and its prescription, three saved documents (one with
    no date), an allergy, a condition a clinician recorded, and a vaccination record. Each
    document was uploaded long after the date it states — so the order proves the clinical
    date, not the upload date, decides where it sits."""
    visit = await add_visit(
        db,
        patient,
        doctor,
        clinic,
        when=at(2026, 7, 28, 9, 30),
        complaint="Endocrinology follow-up\nSecond line",
        notes="Review again in three months.",
        ai_summary="Stable on current treatment.",
        diagnoses=(("Type 2 diabetes mellitus", "E11"),),
    )
    rx = await add_prescription(
        db,
        patient,
        doctor,
        visit,
        issued=date(2026, 7, 28),
        items=(("Glucophage", "500 mg", "1 tablet", "twice daily", 30),),
    )
    rx_doc = await add_document(
        db,
        patient,
        doc_type="rx",
        uploaded_by=patient.user_id,
        rows=(METFORMIN,),
        document_date=date(2026, 8, 11),
        uploaded_at=at(2026, 9, 1),
        ocr_text="Metformin 500 mg twice daily",
    )
    lab = await add_document(
        db,
        patient,
        doc_type="lab",
        uploaded_by=patient.user_id,
        rows=(HBA1C,),
        document_date=date(2026, 6, 15),
        uploaded_at=at(2026, 10, 1),
    )
    undated_lab = await add_document(
        db,
        patient,
        doc_type="lab",
        uploaded_by=patient.user_id,
        rows=(HBA1C,),
        document_date=None,
        uploaded_at=at(2026, 10, 5),
    )
    vaccine = await add_document(
        db,
        patient,
        doc_type="vaccine",
        uploaded_by=patient.user_id,
        rows=(HEPATITIS_B,),
        document_date=date(2026, 2, 10),
        uploaded_at=at(2026, 3, 1),
    )
    allergy = Allergy(
        id=uuid7(),
        patient_id=patient.id,
        substance="Peanuts",
        severity="severe",
        recorded_at=at(2026, 5, 1),
        recorded_by=patient.user_id,
    )
    condition = ChronicCondition(
        id=uuid7(),
        patient_id=patient.id,
        name="Asthma",
        onset_date=date(2020, 3, 1),
        recorded_at=at(2026, 5, 2),
        recorded_by=doctor.user_id,
    )
    db.add_all([allergy, condition])
    await db.commit()
    return SimpleNamespace(
        visit=visit,
        rx=rx,
        rx_doc=rx_doc,
        lab=lab,
        undated_lab=undated_lab,
        vaccine=vaccine,
        allergy=allergy,
        condition=condition,
        # newest first, by the date each thing HAPPENED
        order=[
            f"rx_document:{rx_doc.id}",  # 11 Aug 2026
            f"encounter:{visit.id}",  # 28 Jul 2026, 14:30 in Pakistan
            f"prescription:{rx.id}",  # 28 Jul 2026, no time of day
            f"lab_document:{lab.id}",  # 15 Jun 2026
            f"allergy:{allergy.id}",  # 1 May 2026 (the day it was recorded)
            f"document:{vaccine.id}",  # 10 Feb 2026
            f"condition:{condition.id}",  # onset 1 Mar 2020
            f"lab_document:{undated_lab.id}",  # no date: last
        ],
    )
