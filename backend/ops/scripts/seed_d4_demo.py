"""Synthetic demo data for the doctor dashboard (D4). No real patient data.

Creates one VERIFIED doctor and three patients who have each granted that
doctor access, so `/en/doctor` shows a populated "My patients" table:

  - a woman with a date of birth          -> age and "Female"
  - a man with a date of birth            -> age and "Male"
  - a patient with no date of birth       -> "—" for age, "Prefer not to say"

Safe to run more than once: accounts that already exist are reused, and a
grant is only added when no active one exists.

Run from the repo root (needs a filled-in `.env`):
  uv run backend/ops/scripts/seed_d4_demo.py

It writes to the Supabase project in `.env` (Auth users via the service-role
Admin API, plus app rows), so agree with the team before running it against
a shared project.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import date
from pathlib import Path

# Runnable directly: `uv run backend/ops/scripts/seed_d4_demo.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.db.models import (
    AccountStatus,
    Clinic,
    ConsentGrant,
    Doctor,
    DoctorAffiliation,
    LocaleCode,
    Patient,
    Profile,
    UserRole,
    VerificationStatus,
)
from app.db.session import SessionFactory
from app.db.types import utcnow, uuid7
from app.identity.security import generate_passport_no, get_supabase_client
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from supabase import AsyncClient

# Demo-only password shared by every account this script creates.
PASSWORD = "CuraNode!D4demo"

DOCTOR = ("d4.doctor@example.com", "Dr. Kamran Siddiqui", "Internal Medicine", "D4-90001")
VERIFIER = ("d4.admin@example.com", "D4 Demo Admin")
PATIENTS = [
    ("d4.patient1@example.com", "Sana Malik", date(1988, 4, 12), "female"),
    ("d4.patient2@example.com", "Hamza Tariq", date(1975, 9, 30), "male"),
    ("d4.patient3@example.com", "Fatima Noor", None, "prefer_not_to_say"),
]


async def _profile(
    session: AsyncSession, client: AsyncClient, email: str, name: str, role: UserRole
) -> Profile:
    """Reuse the profile for `email`, or create the Auth user and its profile."""
    existing = (
        await session.execute(select(Profile).where(Profile.email == email))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    created = await client.auth.admin.create_user(
        {"email": email, "password": PASSWORD, "email_confirm": True}
    )
    user_id = uuid.UUID(created.user.id)
    # A DB trigger inserts a default profile row when the Auth user is
    # created; update it rather than inserting a second one.
    profile = await session.get(Profile, user_id)
    if profile is None:
        profile = Profile(id=user_id)
        session.add(profile)
    profile.email = email
    profile.role = role
    profile.status = AccountStatus.ACTIVE
    profile.preferred_locale = LocaleCode.EN
    profile.full_name = name
    profile.is_synthetic = True
    await session.flush()
    return profile


async def _verifier(session: AsyncSession, client: AsyncClient) -> Profile:
    """Any existing clinic admin, else a demo one (verification names a human)."""
    admin = (
        await session.execute(select(Profile).where(Profile.role == UserRole.CLINIC_ADMIN).limit(1))
    ).scalar_one_or_none()
    return admin or await _profile(session, client, *VERIFIER, UserRole.CLINIC_ADMIN)


async def _doctor(session: AsyncSession, client: AsyncClient) -> Doctor:
    email, name, specialty, pmdc = DOCTOR
    user = await _profile(session, client, email, name, UserRole.DOCTOR)
    doctor = (
        await session.execute(select(Doctor).where(Doctor.user_id == user.id))
    ).scalar_one_or_none()
    if doctor is None:
        doctor = Doctor(
            id=uuid7(), user_id=user.id, full_name=name, specialty=specialty, pmdc_number=pmdc
        )
        session.add(doctor)
        clinic = (await session.execute(select(Clinic).limit(1))).scalar_one_or_none()
        if clinic is not None:
            session.add(
                DoctorAffiliation(
                    id=uuid7(),
                    doctor_id=doctor.id,
                    clinic_id=clinic.id,
                    start_date=utcnow().date(),
                    status="active",
                )
            )
    if doctor.verification_status != VerificationStatus.VERIFIED:
        verifier = await _verifier(session, client)
        doctor.verification_status = VerificationStatus.VERIFIED
        doctor.verified_by = verifier.id
        doctor.verified_at = utcnow()
    await session.flush()
    return doctor


async def _patient(
    session: AsyncSession,
    client: AsyncClient,
    email: str,
    name: str,
    born: date | None,
    gender: str,
) -> Patient:
    user = await _profile(session, client, email, name, UserRole.PATIENT)
    patient = (
        await session.execute(select(Patient).where(Patient.user_id == user.id))
    ).scalar_one_or_none()
    if patient is None:
        patient = Patient(
            id=uuid7(), user_id=user.id, full_name=name, passport_no=generate_passport_no()
        )
        session.add(patient)
    patient.date_of_birth = born
    patient.gender = gender
    await session.flush()
    return patient


async def _grant(session: AsyncSession, patient: Patient, doctor: Doctor) -> None:
    active = (
        await session.execute(
            select(ConsentGrant.id).where(
                ConsentGrant.patient_id == patient.id,
                ConsentGrant.grantee_type == "doctor",
                ConsentGrant.grantee_id == doctor.id,
                ConsentGrant.revoked_at.is_(None),
            )
        )
    ).first()
    if active is None:
        session.add(
            ConsentGrant(
                id=uuid7(),
                patient_id=patient.id,
                grantee_type="doctor",
                grantee_id=doctor.id,
                scope_sections=[],
                granted_at=utcnow(),
            )
        )


async def seed() -> None:
    client = await get_supabase_client()
    async with SessionFactory() as session:
        doctor = await _doctor(session, client)
        for email, name, born, gender in PATIENTS:
            await _grant(
                session, await _patient(session, client, email, name, born, gender), doctor
            )
        await session.commit()

    print("D4 demo data ready. Password for every account:", PASSWORD)
    print(f"  doctor (verified)  {DOCTOR[0]}   -> sign in, open /en/doctor")
    for email, name, _, _ in PATIENTS:
        print(f"  patient            {email}   ({name}, has granted the doctor access)")


if __name__ == "__main__":
    asyncio.run(seed())
