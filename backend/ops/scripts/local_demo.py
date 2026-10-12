"""Local P9 + D7 demo — runs the real app with NO Supabase and NO shared database.

DEV ONLY. Never deploy this. It:
  * points the app at a throwaway SQLite file in your temp folder (recreated on
    every start), creating every table from the ORM models — including the P9
    `consent_grant.revoked_by` / `replaced_by` columns the shared DB lacks;
  * swaps Supabase Auth for the in-process fake the test suite uses
    (`tests/fakes.py`), so sign-in never leaves this machine;
  * seeds synthetic demo accounts and prints their logins, including a doctor
    with weekly hours at two clinics (D7 schedule page);
  * serves http://127.0.0.1:8010 (DEMO_PORT to change; Ctrl+C to stop).

Every Supabase / database setting from `.env` is overridden below *before*
the app is imported, so `.env` can stay pointed at the shared project.

Run from the repo root:
    uv run python backend/ops/scripts/local_demo.py

Limitations: self-registration and Google sign-in don't work here (the shared
schema's `on_auth_user_created` trigger and Google don't exist locally) — use
the demo accounts. Everything resets when the server stops.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import date, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DB_PATH = Path(tempfile.gettempdir()) / "curanode_p9_demo.sqlite3"
# Not 8000, so it can run next to the normal dev server. Override with DEMO_PORT.
PORT = int(os.environ.get("DEMO_PORT", "8010"))

# ── Override .env before anything from `app` is imported ──────────────────
os.environ.update(
    {
        "ENVIRONMENT": "dev",
        "DATABASE_URL": f"sqlite+aiosqlite:///{DB_PATH.as_posix()}",
        # Placeholders only: the fake below replaces every Supabase call.
        "SUPABASE_URL": "http://supabase.invalid",
        "SUPABASE_SERVICE_ROLE_KEY": "local-demo-placeholder-not-a-key",
        "SUPABASE_ANON_KEY": "",
        "OAUTH_ENABLED": "false",
        "COOKIE_SECURE": "false",  # plain http on 127.0.0.1
        "RELOAD": "false",
        "PUBLIC_BASE_URL": f"http://127.0.0.1:{PORT}",
    }
)
for p in (ROOT / "backend", ROOT):
    sys.path.insert(0, str(p))

import uvicorn
from app.db import session as session_mod
from app.db.models import (
    Allergy,
    Base,
    ChronicCondition,
    Clinic,
    ClinicStaff,
    Doctor,
    DoctorAffiliation,
    DoctorSchedule,
    LocaleCode,
    Patient,
    PatientMedication,
    Profile,
    ScheduleException,
    ScheduleExceptionType,
    UserRole,
    VerificationStatus,
)
from app.db.types import today_pk, utcnow, uuid7
from app.identity import security as identity_security
from app.identity.security import generate_passport_no
from app.schedule.schemas import calc_max_tokens

from tests.fakes import FakeSupabaseAuth, fake_decode_token

# Synthetic demo password shared by every demo account (local fake auth only).
DEMO_PASSWORD = "LocalDemo-P9-2026"

FAKE = FakeSupabaseAuth()


async def _fake_client() -> FakeSupabaseAuth:
    return FAKE


def _install_fake_auth() -> None:
    identity_security.get_supabase_client = _fake_client  # type: ignore[assignment]
    identity_security.new_auth_client = _fake_client  # type: ignore[assignment]
    identity_security.decode_supabase_access_token = fake_decode_token  # type: ignore[assignment]
    import app.deps

    app.deps.decode_supabase_access_token = fake_decode_token  # type: ignore[assignment]


def _profile(email: str, role: UserRole, name: str) -> Profile:
    user_id = uuid7()
    FAKE.register(str(user_id), email, DEMO_PASSWORD)
    return Profile(
        id=user_id,
        email=email,
        role=role,
        preferred_locale=LocaleCode.EN,
        full_name=name,
        is_synthetic=True,
    )


def _slot(affiliation: DoctorAffiliation, weekday: int, start: str, end: str) -> DoctorSchedule:
    begin, finish = time.fromisoformat(start), time.fromisoformat(end)
    return DoctorSchedule(
        id=uuid7(),
        affiliation_id=affiliation.id,
        weekday=weekday,
        start_time=begin,
        end_time=finish,
        slot_minutes=15,
        max_tokens=calc_max_tokens(begin, finish, 15),
    )


def _next(weekday: int) -> date:
    """The next date (after today) falling on `weekday`, 0 = Monday."""
    today = today_pk()
    return today + timedelta(days=(weekday - today.weekday()) % 7 or 7)


async def _seed_schedule(s, bilal_shifa, bilal_city, sara_shifa) -> None:
    """D7 demo: Dr Bilal works at two clinics, so the conflict message is one
    form submit away (try Monday 12:00-14:00 at City Demo Hospital)."""
    shifa_mon = _slot(bilal_shifa, 0, "09:00", "13:00")
    shifa_wed = _slot(bilal_shifa, 2, "09:00", "13:00")
    city_mon = _slot(bilal_city, 0, "15:00", "18:00")
    city_tue = _slot(bilal_city, 1, "14:00", "18:00")
    city_thu = _slot(bilal_city, 3, "14:00", "18:00")
    sara_sat = _slot(sara_shifa, 5, "10:00", "12:00")
    s.add_all([shifa_mon, shifa_wed, city_mon, city_tue, city_thu, sara_sat])
    await s.flush()
    s.add_all(
        [
            ScheduleException(
                id=uuid7(),
                schedule_id=shifa_wed.id,
                exception_date=_next(2),
                kind=ScheduleExceptionType.LEAVE,
                start_time=time(11, 0),
                end_time=time(13, 0),
                reason="Department meeting",
            ),
            ScheduleException(
                id=uuid7(),
                schedule_id=city_mon.id,  # the earliest slot at City: the extra's anchor
                exception_date=_next(5),
                kind=ScheduleExceptionType.EXTRA,
                start_time=time(10, 0),
                end_time=time(13, 0),
            ),
        ]
    )


async def _seed() -> list[tuple[str, str]]:
    async with session_mod.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    accounts: list[tuple[str, str]] = []
    async with session_mod.SessionFactory() as s:
        shifa = Clinic(id=uuid7(), name="Shifa Demo Clinic", type="clinic", city="Islamabad")
        city = Clinic(id=uuid7(), name="City Demo Hospital", type="hospital", city="Lahore")
        s.add_all([shifa, city])

        admin = _profile("admin@example.com", UserRole.CLINIC_ADMIN, "Demo Clinic Admin")
        s.add(admin)
        await s.flush()
        s.add(ClinicStaff(id=uuid7(), user_id=admin.id, clinic_id=shifa.id, role="admin"))
        accounts.append(("Clinic admin (no record access, ever)", admin.email))

        def doctor(email: str, name: str, specialty: str, pmdc: str, verified: bool) -> Doctor:
            prof = _profile(email, UserRole.DOCTOR, name)
            s.add(prof)
            return Doctor(
                id=uuid7(),
                user=prof,
                full_name=name,
                specialty=specialty,
                pmdc_number=pmdc,
                verification_status=(
                    VerificationStatus.VERIFIED if verified else VerificationStatus.PENDING
                ),
                verified_by=admin.id if verified else None,
                verified_at=utcnow() if verified else None,
            )

        ayesha = doctor("dr.ayesha@example.com", "Dr Ayesha Khan", "Cardiology", "DEMO-1001", True)
        bilal = doctor("dr.bilal@example.com", "Dr Bilal Ahmed", "Dermatology", "DEMO-1002", True)
        pending = doctor(
            "dr.pending@example.com", "Dr Sara Pending", "Pediatrics", "DEMO-1003", False
        )
        s.add_all([ayesha, bilal, pending])
        await s.flush()
        bilal_shifa = DoctorAffiliation(
            id=uuid7(), doctor_id=bilal.id, clinic_id=shifa.id, start_date=date(2026, 1, 1)
        )
        bilal_city = DoctorAffiliation(
            id=uuid7(), doctor_id=bilal.id, clinic_id=city.id, start_date=date(2026, 3, 1)
        )
        sara_shifa = DoctorAffiliation(
            id=uuid7(), doctor_id=pending.id, clinic_id=shifa.id, start_date=date(2026, 6, 1)
        )
        s.add_all([bilal_shifa, bilal_city, sara_shifa])
        await s.flush()
        await _seed_schedule(s, bilal_shifa, bilal_city, sara_shifa)
        accounts += [
            ("Verified doctor, no clinic (Dr Ayesha Khan)", ayesha.user.email),
            ("Verified doctor at TWO clinics, with a schedule (Dr Bilal Ahmed)", bilal.user.email),
            ("Unverified doctor; can still manage a schedule (Dr Sara)", pending.user.email),
        ]

        pat_prof = _profile("patient@example.com", UserRole.PATIENT, "Zainab Demo")
        s.add(pat_prof)
        await s.flush()
        patient = Patient(
            id=uuid7(),
            user_id=pat_prof.id,
            full_name="Zainab Demo",
            passport_no=generate_passport_no(),
            date_of_birth=date(1990, 5, 14),
            gender="female",
            blood_group="O+",
            emergency_contact="Synthetic Contact +923001234567",
        )
        s.add(patient)
        await s.flush()
        s.add_all(
            [
                Allergy(
                    patient_id=patient.id,
                    substance="Penicillin",
                    reaction="Rash",
                    severity="severe",
                ),
                Allergy(patient_id=patient.id, substance="Peanuts", severity="moderate"),
                ChronicCondition(patient_id=patient.id, name="Asthma", onset_date=date(2012, 3, 1)),
                PatientMedication(
                    patient_id=patient.id,
                    name="Salbutamol",
                    strength="100 mcg",
                    frequency="As needed",
                ),
            ]
        )
        accounts.append((f"Patient with a record (passport {patient.passport_no})", pat_prof.email))

        other = _profile("patient2@example.com", UserRole.PATIENT, "Omar Demo")
        s.add(other)
        await s.flush()
        s.add(
            Patient(
                id=uuid7(),
                user_id=other.id,
                full_name="Omar Demo",
                passport_no=generate_passport_no(),
            )
        )
        accounts.append(("Second patient (empty record)", other.email))
        await s.commit()
    return accounts


def main() -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()  # fresh demo every start
    _install_fake_auth()
    accounts = asyncio.run(_seed())
    # The seed's event loop is gone; uvicorn starts its own, so drop pooled
    # connections bound to the old one.
    asyncio.run(session_mod.engine.dispose())

    print("\n" + "=" * 72)
    print(" CuraNode-AI - LOCAL P9 + D7 DEMO (fake auth, SQLite; no Supabase, no shared DB)")
    print(f" Database file : {DB_PATH}")
    print(f" Open          : http://127.0.0.1:{PORT}/en/login")
    print(f" Password      : {DEMO_PASSWORD}   (same for every account)")
    print("-" * 72)
    for label, email in accounts:
        print(f"  {email:<26} {label}")
    print("=" * 72)
    print(" D7 schedule   : sign in as dr.bilal@example.com, open")
    print(f"                 http://127.0.0.1:{PORT}/en/doctor/schedule")
    print(" Stop with Ctrl+C. Everything resets on the next start.\n")

    from app.main import app

    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
