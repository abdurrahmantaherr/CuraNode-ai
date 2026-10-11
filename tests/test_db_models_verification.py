"""D-VERIFY — the two new tables exist and round-trip (model layer only;
business rules belong to `verification/service.py`)."""

from __future__ import annotations

import pytest
from app.db.models import (
    ClinicInviteCode,
    Doctor,
    DoctorVerification,
    UserRole,
    VerificationDecision,
)
from app.db.types import utcnow, uuid7
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from tests.conftest import make_user


async def test_doctor_verification_round_trips(db, clinic):
    user = await make_user(db, email="dr@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    doctor = (await db.execute(select(Doctor).where(Doctor.user_id == user.id))).scalar_one()
    admin = await make_user(db, email="adm@x.com", role=UserRole.CLINIC_ADMIN, clinic_id=clinic.id)

    row = DoctorVerification(
        id=uuid7(),
        doctor_id=doctor.id,
        decided_by=admin.id,
        decision=VerificationDecision.APPROVE,
        reason=None,
        checked_number=True,
        checked_name=True,
        checked_licence=True,
        note="Checked on PMDC, all clear.",
        decided_at=utcnow(),
    )
    db.add(row)
    await db.commit()

    got = (
        await db.execute(
            select(DoctorVerification).where(DoctorVerification.doctor_id == doctor.id)
        )
    ).scalar_one()
    assert got.decision == VerificationDecision.APPROVE
    assert got.checked_number is True
    assert got.note == "Checked on PMDC, all clear."


async def test_clinic_invite_code_round_trips(db, clinic):
    db.add(ClinicInviteCode(clinic_id=clinic.id, code_hash="salt$hash", is_active=True))
    await db.commit()

    got = (
        await db.execute(select(ClinicInviteCode).where(ClinicInviteCode.clinic_id == clinic.id))
    ).scalar_one()
    assert got.code_hash == "salt$hash"
    assert got.is_active is True


async def test_clinic_invite_code_clinic_id_is_the_primary_key(db, clinic):
    # One code per clinic — a second insert for the same clinic must fail,
    # not silently create a second active code.
    db.add(ClinicInviteCode(clinic_id=clinic.id, code_hash="first", is_active=True))
    await db.commit()
    db.add(ClinicInviteCode(clinic_id=clinic.id, code_hash="second", is_active=True))
    with pytest.raises(IntegrityError):
        await db.commit()
