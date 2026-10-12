"""Shared setup for the booking tests (P5): a frozen clock, a verified doctor
with a weekly session, and patients. Helpers rather than fixtures, so each test
file decides what it needs."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

from app.booking import slots
from app.db import types as dbtypes
from app.db.models import (
    Appointment,
    Doctor,
    DoctorAffiliation,
    DoctorSchedule,
    Patient,
    Profile,
    UserRole,
)
from app.deps import Actor
from sqlalchemy import select

from tests.conftest import make_user

NOW = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)  # Monday 13:00 in Pakistan
TODAY = date(2026, 10, 12)
MONDAY, WEDNESDAY, THURSDAY = 0, 2, 3
WED = date(2026, 10, 14)  # the first Wednesday after NOW


def freeze(monkeypatch, now: datetime = NOW) -> None:
    """Freeze the booking clock and D7's `today` together."""
    monkeypatch.setattr("app.booking.service._now", lambda: now)
    monkeypatch.setattr(dbtypes, "today_pk", lambda: now.astimezone(dbtypes.PKT).date())


def at(day: date, hhmm: str) -> datetime:
    """The aware UTC instant of a Pakistan wall-clock time (what `slot_start` holds)."""
    return slots.to_utc(day, time.fromisoformat(hhmm))


async def verified_doctor(
    db, clinic, *, email="booking.dr@x.com", name="Dr Booking", specialty="Cardiology"
) -> Doctor:
    admin = await make_user(
        db, email=f"admin.{email}", role=UserRole.CLINIC_ADMIN, clinic_id=clinic.id
    )
    user = await make_user(
        db,
        email=email,
        role=UserRole.DOCTOR,
        clinic_id=clinic.id,
        is_verified=True,
        verifier_id=admin.id,
    )
    doctor = (await db.execute(select(Doctor).where(Doctor.user_id == user.id))).scalar_one()
    doctor.full_name = name
    doctor.specialty = specialty
    await db.commit()
    return doctor


async def add_session(
    db,
    doctor: Doctor,
    clinic,
    *,
    weekday: int = WEDNESDAY,
    start: str = "09:00",
    end: str = "13:00",
    minutes: int = 15,
    cap: int = 16,
) -> DoctorSchedule:
    affiliation = (
        await db.execute(
            select(DoctorAffiliation).where(
                DoctorAffiliation.doctor_id == doctor.id, DoctorAffiliation.clinic_id == clinic.id
            )
        )
    ).scalar_one()
    row = DoctorSchedule(
        affiliation_id=affiliation.id,
        weekday=weekday,
        start_time=time.fromisoformat(start),
        end_time=time.fromisoformat(end),
        slot_minutes=minutes,
        max_tokens=cap,
        status="active",
    )
    db.add(row)
    await db.commit()
    return row


async def make_patient(
    db, *, email: str = "booking.pat@x.com", name: str = "Pat Booking"
) -> tuple[Profile, Patient, Actor]:
    user = await make_user(db, email=email, role=UserRole.PATIENT)
    patient = (await db.execute(select(Patient).where(Patient.user_id == user.id))).scalar_one()
    patient.full_name = name
    await db.commit()
    return user, patient, Actor(user_id=user.id, role="patient")


async def add_appointment(
    db, patient, doctor, clinic, schedule, starts_at: datetime, status: str = "booked"
) -> Appointment:
    row = Appointment(
        patient_id=patient.id,
        doctor_id=doctor.id,
        clinic_id=clinic.id,
        schedule_id=schedule.id,
        slot_start=starts_at,
        status=status,
        booking_channel="online",
    )
    db.add(row)
    await db.commit()
    return row
