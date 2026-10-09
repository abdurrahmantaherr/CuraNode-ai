"""Synthetic seed data (NFR19 — no real patient data, ever).

Produces the minimum set SPEC §7 requires: clinics, an active patient, a
verified doctor, an UNVERIFIED doctor, and a clinic admin. The unverified
doctor matters most — it is what makes the FR3 lockout demonstrable. The
verified doctor works at two clinics with weekly hours at both (D7), so the
multi-clinic schedule and its conflict rule can be tried.

Credentials are created in Supabase Auth (via the service-role Admin API);
this script only owns the app-side `profiles`/`patients`/`doctors` rows.
It writes to the SHARED database, so only the schema owner runs it, never
from a dev machine and never as part of a task. The schema is applied by the
schema owner via Alembic, not here. The weekly slots it adds use only columns
that `doctor_schedule` already has, so they do not depend on D7's migration.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

# Runnable directly: `uv run backend/ops/scripts/seed_synthetic.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from datetime import UTC, datetime, time

from app.db.models import (
    AccountStatus,
    Clinic,
    ClinicStaff,
    Doctor,
    DoctorAffiliation,
    DoctorSchedule,
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
from supabase import AsyncClient

PASSWORD = "CuraNode!2026"

CLINICS = [
    ("Shifa International", "Islamabad", "F-8 Markaz"),
    ("Maroof International", "Islamabad", "F-10 Markaz"),
    ("Lahore Care Hospital", "Lahore", "Gulberg III"),
]


async def _supabase_user(client: AsyncClient, email: str) -> uuid.UUID:
    created = await client.auth.admin.create_user(
        {"email": email, "password": PASSWORD, "email_confirm": True}
    )
    return uuid.UUID(created.user.id)


async def seed() -> None:
    client = await get_supabase_client()

    async with SessionFactory() as session:
        existing = await session.execute(select(Clinic.id).limit(1))
        if existing.scalar_one_or_none() is not None:
            print("Seed data already present — nothing to do.")
            return

        clinics = [
            Clinic(id=uuid7(), name=name, city=city, address=addr, type="hospital")
            for name, city, addr in CLINICS
        ]
        for c in clinics:
            session.add(c)
        await session.flush()

        async def user(email: str, name: str, role: UserRole, locale: str = "en") -> Profile:
            user_id = await _supabase_user(client, email)
            # A DB trigger (`on_auth_user_created`) already inserted a
            # default `user_profile` row the instant the auth user was
            # created — update it rather than inserting again.
            profile = await session.get(Profile, user_id)
            if profile is None:
                profile = Profile(id=user_id)
                session.add(profile)
            profile.email = email
            profile.role = role
            profile.status = AccountStatus.ACTIVE
            profile.preferred_locale = LocaleCode(locale)
            profile.full_name = name
            profile.is_synthetic = True
            return profile

        patient = await user("ayesha.raza@example.com", "Ayesha Raza", UserRole.PATIENT)
        verified = await user("adnan.haleem@example.com", "Adnan Haleem", UserRole.DOCTOR)
        unverified = await user("nadia.iqbal@example.com", "Nadia Iqbal", UserRole.DOCTOR, "ur")
        admin = await user("front.desk@example.com", "Front Desk", UserRole.CLINIC_ADMIN)

        for u in (patient, verified, unverified, admin):
            session.add(u)
        await session.flush()

        session.add(
            Patient(
                id=uuid7(),
                user_id=patient.id,
                full_name=patient.full_name,
                passport_no=generate_passport_no(),
            )
        )

        verified_doctor = Doctor(
            id=uuid7(),
            user_id=verified.id,
            full_name=verified.full_name,
            specialty="Cardiology",
            pmdc_number="41192",
            verification_status=VerificationStatus.VERIFIED,
            # The CHECK constraint requires a named verifier.
            verified_by=admin.id,
            verified_at=utcnow(),
        )
        session.add(verified_doctor)
        primary = DoctorAffiliation(
            id=uuid7(),
            doctor_id=verified_doctor.id,
            clinic_id=clinics[0].id,
            start_date=datetime.now(UTC).date(),
            status="active",
        )
        # D7: a second active affiliation, so the doctor works at two clinics.
        second = DoctorAffiliation(
            id=uuid7(),
            doctor_id=verified_doctor.id,
            clinic_id=clinics[2].id,
            start_date=datetime.now(UTC).date(),
            status="active",
        )
        session.add_all([primary, second])
        await session.flush()
        # Monday/Wednesday mornings at the first clinic, Monday evening and
        # Thursday afternoon at the second: 15 minutes per patient.
        for affiliation, weekday, start, end, cap in (
            (primary, 0, time(9), time(13), 16),
            (primary, 2, time(9), time(13), 16),
            (second, 0, time(15), time(18), 12),
            (second, 3, time(14), time(18), 16),
        ):
            session.add(
                DoctorSchedule(
                    id=uuid7(),
                    affiliation_id=affiliation.id,
                    weekday=weekday,
                    start_time=start,
                    end_time=end,
                    slot_minutes=15,
                    max_tokens=cap,
                )
            )

        unverified_doctor = Doctor(
            id=uuid7(),
            user_id=unverified.id,
            full_name=unverified.full_name,
            specialty="Dermatology",
            pmdc_number="55871",
            verification_status=VerificationStatus.PENDING,
        )
        session.add(unverified_doctor)
        session.add(
            DoctorAffiliation(
                id=uuid7(),
                doctor_id=unverified_doctor.id,
                clinic_id=clinics[1].id,
                start_date=datetime.now(UTC).date(),
                status="active",
            )
        )

        session.add(
            ClinicStaff(id=uuid7(), user_id=admin.id, clinic_id=clinics[0].id, role="admin")
        )

        await session.commit()

    print("Seeded synthetic data. Password for every account:", PASSWORD)
    print("  patient    ayesha.raza@example.com")
    print("  doctor     adnan.haleem@example.com   (verified; weekly hours at two clinics)")
    print("  doctor     nadia.iqbal@example.com    (UNVERIFIED — locked out of clinical routes)")
    print("  admin      front.desk@example.com")


if __name__ == "__main__":
    asyncio.run(seed())
