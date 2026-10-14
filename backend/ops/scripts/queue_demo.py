"""Evaluation demo: the repo's local demo plus a live queue. DEV ONLY - lives on the
`demo/evaluation` branch and must NEVER be merged into dev or main.

Runs the real app on a throwaway SQLite file with fake sign-in (no Supabase, no shared
database), then adds: Dr Ayesha at Shifa with an all-day session today, four booked
patients, one registered patient who is not booked (for the walk-in search), and a
consent grant from Zainab to Dr Ayesha (so the doctor's patient list and record page
have something to show). Everything resets on every start. Run from the repo root:

    uv run python backend/ops/scripts/queue_demo.py

Password for every account: LocalDemo-P9-2026. Pick the matching role tile at sign-in.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import date, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location(
    "local_demo", ROOT / "backend/ops/scripts/local_demo.py"
)
demo = importlib.util.module_from_spec(spec)
sys.modules["local_demo"] = demo
spec.loader.exec_module(demo)

import uvicorn
from app.booking import slots
from app.db import session as session_mod
from app.db.models import (
    Appointment,
    Clinic,
    ConsentGrant,
    Doctor,
    DoctorAffiliation,
    DoctorSchedule,
    Patient,
    UserRole,
)
from app.db.types import today_pk, utcnow, uuid7
from app.identity.security import generate_passport_no
from app.schedule.schemas import calc_max_tokens
from sqlalchemy import select


async def extra() -> list[str]:
    notes: list[str] = []
    async with session_mod.SessionFactory() as s:
        shifa = (
            await s.execute(select(Clinic).where(Clinic.name == "Shifa Demo Clinic"))
        ).scalar_one()
        ayesha = (
            await s.execute(select(Doctor).where(Doctor.pmdc_number == "DEMO-1001"))
        ).scalar_one()
        affiliation = DoctorAffiliation(
            id=uuid7(), doctor_id=ayesha.id, clinic_id=shifa.id, start_date=date(2026, 1, 1)
        )
        s.add(affiliation)
        await s.flush()
        begin, finish = time(0, 30), time(23, 45)
        today = today_pk()
        schedule = DoctorSchedule(
            id=uuid7(),
            affiliation_id=affiliation.id,
            weekday=today.weekday(),
            start_time=begin,
            end_time=finish,
            slot_minutes=15,
            max_tokens=calc_max_tokens(begin, finish, 15),
        )
        s.add(schedule)
        await s.flush()

        people = []
        for email, name in (
            ("patient3@example.com", "Hira Noor"),
            ("patient4@example.com", "Zain Malik"),
        ):
            prof = demo._profile(email, UserRole.PATIENT, name)
            s.add(prof)
            await s.flush()
            p = Patient(
                id=uuid7(), user_id=prof.id, full_name=name, passport_no=generate_passport_no()
            )
            s.add(p)
            people.append(p)
        notwalk_prof = demo._profile("patient5@example.com", UserRole.PATIENT, "Ayesha Raza")
        s.add(notwalk_prof)
        await s.flush()
        walkin = Patient(
            id=uuid7(),
            user_id=notwalk_prof.id,
            full_name="Ayesha Raza",
            passport_no=generate_passport_no(),
        )
        s.add(walkin)
        await s.flush()

        existing = {
            n: p
            for n, p in (
                (p.full_name, p) for p in (await s.execute(select(Patient))).scalars().all()
            )
        }
        order = [
            existing["Zainab Demo"],
            existing["Omar Demo"],
            people[0],
            people[1],
        ]
        for i, patient in enumerate(order):
            s.add(
                Appointment(
                    patient_id=patient.id,
                    doctor_id=ayesha.id,
                    clinic_id=shifa.id,
                    schedule_id=schedule.id,
                    slot_start=slots.to_utc(today, time(10, 15 * i)),
                    status="booked",
                    booking_channel="online",
                )
            )
        s.add(
            ConsentGrant(
                id=uuid7(),
                patient_id=existing["Zainab Demo"].id,
                grantee_type="doctor",
                grantee_id=ayesha.id,
                scope_sections=["basics", "allergies", "conditions", "medications"],
                granted_at=utcnow(),
            )
        )
        await s.commit()
        notes.append(f"walk-in search target: Ayesha Raza, passport {walkin.passport_no}")
    return notes


def main() -> None:
    if demo.DB_PATH.exists():
        demo.DB_PATH.unlink()
    demo._install_fake_auth()
    accounts = asyncio.run(demo._seed())
    notes = asyncio.run(extra())
    asyncio.run(session_mod.engine.dispose())
    print("=" * 70)
    print(f"QUEUE DEMO http://127.0.0.1:{demo.PORT}/en/login   password: {demo.DEMO_PASSWORD}")
    for _, email in accounts:
        print("  ", email)
    print(
        "   patient3@example.com (Hira Noor), patient4@example.com (Zain Malik), patient5@example.com"
    )
    for n in notes:
        print("  ", n)
    print("=" * 70, flush=True)
    from app.main import app

    uvicorn.run(app, host="127.0.0.1", port=demo.PORT, log_level="warning")


if __name__ == "__main__":
    main()
