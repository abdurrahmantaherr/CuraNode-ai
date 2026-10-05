"""D4 v1 — doctor patient dashboard: consent query (this file's first half)
and the page (second half).

Visibility is the whole feature, so most tests here are about who must NOT
appear.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from app.consent import service as consent_service
from app.db.models import ConsentGrant, Doctor, Patient, Profile, UserRole
from app.db.types import utcnow, uuid7
from app.deps import Actor
from app.errors import NotFound
from sqlalchemy import select

from tests.conftest import make_user


# ── Helpers ──────────────────────────────────────────────────────────────
async def patient_of(db, user: Profile) -> Patient:
    stmt = select(Patient).where(Patient.user_id == user.id)
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one()


async def doctor_of(db, user: Profile) -> Doctor:
    stmt = select(Doctor).where(Doctor.user_id == user.id)
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one()


async def make_patient(db, email: str, full_name: str, **fields) -> Patient:
    user = await make_user(db, email=email, role=UserRole.PATIENT)
    patient = await patient_of(db, user)
    patient.full_name = full_name
    for key, value in fields.items():
        setattr(patient, key, value)
    await db.commit()
    return patient


async def grant(db, patient: Patient, doctor: Doctor, *, revoked=False, expires_in=None):
    row = ConsentGrant(
        id=uuid7(),
        patient_id=patient.id,
        grantee_type="doctor",
        grantee_id=doctor.id,
        scope_sections=[],
        granted_at=utcnow(),
        revoked_at=utcnow() if revoked else None,
        expires_at=(utcnow() + expires_in) if expires_in is not None else None,
    )
    db.add(row)
    await db.commit()
    return row


def as_actor(doctor: Doctor, *, verified: bool = True) -> Actor:
    return Actor(user_id=doctor.user_id, role="doctor", is_verified_doctor=verified)


@pytest.fixture
async def admin(db, clinic) -> Profile:
    return await make_user(db, email="adm@x.com", role=UserRole.CLINIC_ADMIN, clinic_id=clinic.id)


@pytest.fixture
async def doctor(db, clinic, admin) -> Doctor:
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
async def other_doctor(db, clinic, admin) -> Doctor:
    user = await make_user(
        db,
        email="dr2@x.com",
        role=UserRole.DOCTOR,
        clinic_id=clinic.id,
        is_verified=True,
        verifier_id=admin.id,
    )
    return await doctor_of(db, user)


# ── list_patients_for_doctor ─────────────────────────────────────────────
async def test_lists_only_patients_who_granted_this_doctor(db, doctor, other_doctor):
    mine = await make_patient(db, "p1@x.com", "Mine")
    theirs = await make_patient(db, "p2@x.com", "Theirs")
    await make_patient(db, "p3@x.com", "Nobody")  # no grant at all
    await grant(db, mine, doctor)
    await grant(db, theirs, other_doctor)

    got = await consent_service.list_patients_for_doctor(db, as_actor(doctor))

    assert [p.full_name for p in got] == ["Mine"]


async def test_revoked_grant_is_not_listed(db, doctor):
    p = await make_patient(db, "p1@x.com", "Revoked")
    await grant(db, p, doctor, revoked=True)
    assert await consent_service.list_patients_for_doctor(db, as_actor(doctor)) == []


async def test_expired_grant_is_not_listed(db, doctor):
    p = await make_patient(db, "p1@x.com", "Expired")
    await grant(db, p, doctor, expires_in=timedelta(days=-1))
    assert await consent_service.list_patients_for_doctor(db, as_actor(doctor)) == []


async def test_unexpired_grant_is_listed(db, doctor):
    p = await make_patient(db, "p1@x.com", "Future")
    await grant(db, p, doctor, expires_in=timedelta(days=30))
    got = await consent_service.list_patients_for_doctor(db, as_actor(doctor))
    assert [x.full_name for x in got] == ["Future"]


async def test_sorted_by_name(db, doctor):
    for i, name in enumerate(["Zainab", "Ali", "Bilal"]):
        p = await make_patient(db, f"p{i}@x.com", name)
        await grant(db, p, doctor)
    got = await consent_service.list_patients_for_doctor(db, as_actor(doctor))
    assert [p.full_name for p in got] == ["Ali", "Bilal", "Zainab"]


async def test_two_active_grants_list_the_patient_once(db, doctor):
    # Review focus 3 — the database has no unique constraint behind BL-04.
    p = await make_patient(db, "p1@x.com", "Twice")
    await grant(db, p, doctor)
    await grant(db, p, doctor)
    got = await consent_service.list_patients_for_doctor(db, as_actor(doctor))
    assert [x.full_name for x in got] == ["Twice"]


async def test_unverified_doctor_gets_not_found(db, clinic):
    user = await make_user(db, email="new@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    pending = await doctor_of(db, user)
    with pytest.raises(NotFound):
        await consent_service.list_patients_for_doctor(db, as_actor(pending, verified=True))


async def test_non_doctor_gets_not_found(db):
    user = await make_user(db, email="pat@x.com", role=UserRole.PATIENT)
    actor = Actor(user_id=user.id, role="patient")
    with pytest.raises(NotFound):
        await consent_service.list_patients_for_doctor(db, actor)
