"""Shared setup for the queue tests (P6): an admin, today's session, queue entries.
Time comes from `tests.booking_helpers.freeze`: "now" is Monday 13:00 in Pakistan."""

from __future__ import annotations

from app.db.models import Appointment, Profile, QueueEntry, UserRole
from app.deps import Actor
from sqlalchemy import select

from tests.booking_helpers import MONDAY, TODAY, add_appointment, add_session, at, verified_doctor
from tests.conftest import make_user


async def make_admin(db, clinic, *, email="queue.admin@x.com") -> tuple[Profile, Actor]:
    user = await make_user(db, email=email, role=UserRole.CLINIC_ADMIN, clinic_id=clinic.id)
    actor = Actor(
        user_id=user.id,
        role=UserRole.CLINIC_ADMIN.value,
        clinic_ids=frozenset({clinic.id}),
    )
    return user, actor


async def todays_session(
    db, clinic, *, cap=16, minutes=15, email="queue.dr@x.com", name="Dr Queue"
):
    """A verified doctor with a Monday 12:00-18:00 session (today, when time is frozen)."""
    doctor = await verified_doctor(db, clinic, email=email, name=name)
    schedule = await add_session(
        db, doctor, clinic, weekday=MONDAY, start="12:00", end="18:00", minutes=minutes, cap=cap
    )
    return doctor, schedule


async def booked_today(
    db, clinic, doctor, schedule, patient, hhmm="14:00", status="booked"
) -> Appointment:
    return await add_appointment(
        db, patient, doctor, clinic, schedule, at(TODAY, hhmm), status=status
    )


async def add_entry(db, appointment, token, state="waiting") -> QueueEntry:
    """A queue entry for `appointment`, with the appointment status that goes with its state."""
    entry = QueueEntry(appointment_id=appointment.id, token_number=token, state=state)
    appointment.status = "done" if state == "served" else "checked_in"
    db.add(entry)
    await db.commit()
    return entry


async def entry_of(db, appointment) -> QueueEntry | None:
    return (
        await db.execute(
            select(QueueEntry)
            .where(QueueEntry.appointment_id == appointment.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
