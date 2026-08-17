"""Synthetic seed data (NFR19 — no real patient data, ever).

Produces the minimum set SPEC §7 requires: clinics, an active patient, a
verified doctor, an UNVERIFIED doctor, and a clinic admin. The unverified
doctor matters most — it is what makes the FR3 lockout demonstrable.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Runnable directly: `uv run backend/ops/scripts/seed_synthetic.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.db.models import (
    AccountStatus,
    Base,
    Clinic,
    ClinicStaff,
    Doctor,
    LocaleCode,
    Patient,
    User,
    UserRole,
)
from app.db.session import SessionFactory, engine
from app.db.types import utcnow, uuid7
from app.identity.security import generate_passport_no, hash_password
from sqlalchemy import select

PASSWORD = "CuraNode!2026"

CLINICS = [
    ("Shifa International", "Islamabad", "F-8 Markaz"),
    ("Maroof International", "Islamabad", "F-10 Markaz"),
    ("Lahore Care Hospital", "Lahore", "Gulberg III"),
]


async def seed() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with SessionFactory() as session:
        existing = await session.execute(select(Clinic.id).limit(1))
        if existing.scalar_one_or_none() is not None:
            print("Seed data already present — nothing to do.")
            return

        clinics = [
            Clinic(id=uuid7(), name=name, city=city, address=addr)
            for name, city, addr in CLINICS
        ]
        for c in clinics:
            session.add(c)
        await session.flush()

        def user(email: str, name: str, role: UserRole, locale: str = "en") -> User:
            return User(
                id=uuid7(),
                email=email,
                password_hash=hash_password(PASSWORD),
                role=role,
                status=AccountStatus.ACTIVE,
                preferred_locale=LocaleCode(locale),
                full_name=name,
                is_synthetic=True,
            )

        patient = user("ayesha.raza@example.com", "Ayesha Raza", UserRole.PATIENT)
        verified = user("adnan.haleem@example.com", "Adnan Haleem", UserRole.DOCTOR)
        unverified = user("nadia.iqbal@example.com", "Nadia Iqbal", UserRole.DOCTOR, "ur")
        admin = user("front.desk@example.com", "Front Desk", UserRole.CLINIC_ADMIN)

        for u in (patient, verified, unverified, admin):
            session.add(u)
        await session.flush()

        session.add(Patient(user_id=patient.id, passport_no=generate_passport_no()))
        session.add(
            Doctor(
                user_id=verified.id,
                primary_clinic_id=clinics[0].id,
                specialty="Cardiology",
                pmdc_number="41192",
                is_verified=True,
                # The CHECK constraint requires a named verifier.
                verified_by=admin.id,
                verified_at=utcnow(),
            )
        )
        session.add(
            Doctor(
                user_id=unverified.id,
                primary_clinic_id=clinics[1].id,
                specialty="Dermatology",
                pmdc_number="55871",
                is_verified=False,
            )
        )
        session.add(ClinicStaff(user_id=admin.id, clinic_id=clinics[0].id))

        await session.commit()

    print("Seeded synthetic data. Password for every account:", PASSWORD)
    print("  patient    ayesha.raza@example.com")
    print("  doctor     adnan.haleem@example.com   (verified)")
    print("  doctor     nadia.iqbal@example.com    (UNVERIFIED — locked out of clinical routes)")
    print("  admin      front.desk@example.com")


if __name__ == "__main__":
    asyncio.run(seed())
