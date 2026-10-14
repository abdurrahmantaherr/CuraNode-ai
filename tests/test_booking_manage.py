"""P5: cancel, reschedule, and the patient's and doctor's lists."""

from __future__ import annotations

import json
import uuid
from datetime import date

import pytest
from app.booking import service, slots
from app.booking.schemas import DayRow
from app.db.models import Appointment, AuditLog, UserRole
from app.deps import Actor
from app.errors import BookingRefused, NotFound
from sqlalchemy import select

from tests.booking_helpers import (
    THURSDAY,
    TODAY,
    WED,
    add_appointment,
    add_session,
    at,
    freeze,
    make_patient,
    verified_doctor,
)
from tests.conftest import add_affiliation, make_user


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    freeze(monkeypatch)


async def world(db, clinic, **session_kw):
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic, **session_kw)
    _, patient, actor = await make_patient(db)
    return doctor, session, patient, actor


async def other_patient(db, n=2):
    _, patient, actor = await make_patient(db, email=f"p{n}@x.com", name=f"Patient {n}")
    return patient, actor


async def refreshed(db, appointment) -> Appointment:
    return (
        await db.execute(
            select(Appointment)
            .where(Appointment.id == appointment.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def refusal(coro) -> str:
    with pytest.raises(BookingRefused) as exc:
        await coro
    return exc.value.reason


# ── Cancel ───────────────────────────────────────────────────────────────
async def test_a_patient_cancels_their_own_appointment(db, clinic):
    doctor, session, patient, actor = await world(db, clinic)
    appt = await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    await service.cancel(db, actor, appt.id)
    assert (await refreshed(db, appt)).status == "cancelled"


async def test_cancelling_frees_the_time_for_someone_else(db, clinic):
    doctor, session, patient, actor = await world(db, clinic)
    _, other_actor = await other_patient(db)
    appt = await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    await service.cancel(db, actor, appt.id)
    booked = await service.book(db, other_actor, doctor.id, clinic.id, at(WED, "10:00"))
    assert booked.status == "booked"


async def test_someone_elses_or_a_missing_appointment_is_not_found(db, clinic):
    doctor, session, _, actor = await world(db, clinic)
    other, _ = await other_patient(db)
    theirs = await add_appointment(db, other, doctor, clinic, session, at(WED, "10:00"))
    with pytest.raises(NotFound):
        await service.cancel(db, actor, theirs.id)
    with pytest.raises(NotFound):
        await service.cancel(db, actor, uuid.uuid4())
    assert (await refreshed(db, theirs)).status == "booked"


@pytest.mark.parametrize("status", ["cancelled", "done", "checked_in"])
async def test_only_a_booked_appointment_can_be_cancelled(db, clinic, status):
    doctor, session, patient, actor = await world(db, clinic)
    appt = await add_appointment(
        db, patient, doctor, clinic, session, at(WED, "10:00"), status=status
    )
    assert await refusal(service.cancel(db, actor, appt.id)) == "not_cancellable"


async def test_an_appointment_that_has_started_cannot_be_cancelled(db, clinic):
    doctor, session, patient, actor = await world(db, clinic)
    # Now is Monday 13:00 in Pakistan.
    appt = await add_appointment(db, patient, doctor, clinic, session, at(TODAY, "09:00"))
    assert await refusal(service.cancel(db, actor, appt.id)) == "not_cancellable"


async def test_a_cancel_writes_one_audit_row_with_ids_only(db, clinic):
    doctor, session, patient, actor = await world(db, clinic)
    appt = await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    await service.cancel(db, actor, appt.id)
    rows = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "appointment.cancel")))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert json.loads(rows[0].detail) == {
        "appointment_id": str(appt.id),
        "doctor_id": str(doctor.id),
        "clinic_id": str(clinic.id),
    }


async def test_only_a_patient_can_cancel(db, clinic):
    doctor, session, patient, _ = await world(db, clinic)
    appt = await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    with pytest.raises(NotFound):
        await service.cancel(db, Actor(user_id=doctor.user_id, role="doctor"), appt.id)


async def test_cancel_and_reschedule_lock_the_doctor_first(db, clinic, monkeypatch):
    doctor, session, patient, actor = await world(db, clinic)
    calls = []
    real = service.schedule_service.lock_doctor

    async def spy(session_, doctor_id):
        calls.append(doctor_id)
        await real(session_, doctor_id)

    monkeypatch.setattr(service.schedule_service, "lock_doctor", spy)
    first = await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    await service.cancel(db, actor, first.id)
    second = await add_appointment(db, patient, doctor, clinic, session, at(WED, "11:00"))
    await service.reschedule(db, actor, second.id, clinic.id, at(WED, "12:00"))
    assert calls == [doctor.id, doctor.id]


# ── Reschedule ───────────────────────────────────────────────────────────
async def test_reschedule_books_the_new_time_and_cancels_the_old(db, clinic):
    doctor, session, patient, actor = await world(db, clinic)
    old = await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    new = await service.reschedule(db, actor, old.id, clinic.id, at(date(2026, 10, 21), "11:00"))
    assert new.id != old.id and new.status == "booked" and new.patient_id == patient.id
    assert slots.as_utc(new.slot_start) == at(date(2026, 10, 21), "11:00")
    assert (await refreshed(db, old)).status == "cancelled"


async def test_reschedule_to_another_time_with_the_same_doctor_the_same_day(db, clinic):
    # Review focus 1: the old booking must not block its own replacement.
    doctor, session, patient, actor = await world(db, clinic)
    old = await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    new = await service.reschedule(db, actor, old.id, clinic.id, at(WED, "11:00"))
    assert new.status == "booked" and (await refreshed(db, old)).status == "cancelled"


async def test_reschedule_into_a_full_session_the_old_booking_was_part_of(db, clinic):
    # The old booking leaves the count before the new one is checked, so a
    # session at its cap still accepts the move within itself.
    doctor, session, patient, actor = await world(db, clinic, cap=1)
    old = await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    new = await service.reschedule(db, actor, old.id, clinic.id, at(WED, "10:15"))
    assert new.status == "booked" and (await refreshed(db, old)).status == "cancelled"


async def test_a_failed_reschedule_leaves_the_old_booking_untouched(db, clinic):
    doctor, session, patient, actor = await world(db, clinic)
    other, _ = await other_patient(db)
    old = await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    await add_appointment(db, other, doctor, clinic, session, at(WED, "11:00"))
    assert (
        await refusal(service.reschedule(db, actor, old.id, clinic.id, at(WED, "11:00"))) == "taken"
    )
    assert (await refreshed(db, old)).status == "booked"
    assert len((await db.execute(select(Appointment))).scalars().all()) == 2


async def test_reschedule_to_another_hospital_of_the_same_doctor(db, clinic, second_clinic):
    doctor, session, patient, actor = await world(db, clinic)
    await add_affiliation(db, doctor.id, second_clinic.id)
    await add_session(db, doctor, second_clinic)
    old = await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    new = await service.reschedule(db, actor, old.id, second_clinic.id, at(WED, "10:15"))
    assert new.clinic_id == second_clinic.id and new.doctor_id == doctor.id


async def test_reschedule_of_someone_elses_appointment_is_not_found(db, clinic):
    doctor, session, _, actor = await world(db, clinic)
    other, _ = await other_patient(db)
    theirs = await add_appointment(db, other, doctor, clinic, session, at(WED, "10:00"))
    with pytest.raises(NotFound):
        await service.reschedule(db, actor, theirs.id, clinic.id, at(WED, "11:00"))


async def test_a_cancelled_appointment_cannot_be_rescheduled(db, clinic):
    doctor, session, patient, actor = await world(db, clinic)
    old = await add_appointment(
        db, patient, doctor, clinic, session, at(WED, "10:00"), status="cancelled"
    )
    reason = await refusal(service.reschedule(db, actor, old.id, clinic.id, at(WED, "11:00")))
    assert reason == "not_cancellable"


async def test_an_appointment_that_has_started_cannot_be_rescheduled(db, clinic):
    doctor, session, patient, actor = await world(db, clinic)
    old = await add_appointment(db, patient, doctor, clinic, session, at(TODAY, "09:00"))
    reason = await refusal(service.reschedule(db, actor, old.id, clinic.id, at(WED, "11:00")))
    assert reason == "not_cancellable"
    assert (await refreshed(db, old)).status == "booked"


async def test_a_reschedule_writes_one_audit_row_naming_what_it_replaces(db, clinic):
    doctor, session, patient, actor = await world(db, clinic)
    old = await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    new = await service.reschedule(db, actor, old.id, clinic.id, at(WED, "11:00"))
    rows = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "appointment.reschedule")))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    detail = json.loads(rows[0].detail)
    assert detail["appointment_id"] == str(new.id) and detail["replaces"] == str(old.id)


# ── The patient's lists ──────────────────────────────────────────────────
async def test_my_appointments_splits_upcoming_and_past(db, clinic):
    doctor, session, patient, actor = await world(db, clinic)
    later = await add_appointment(
        db, patient, doctor, clinic, session, at(date(2026, 10, 21), "10:00")
    )
    soon = await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    await add_appointment(db, patient, doctor, clinic, session, at(TODAY, "09:00"))  # started
    await add_appointment(
        db, patient, doctor, clinic, session, at(WED, "11:00"), status="cancelled"
    )
    await add_appointment(db, patient, doctor, clinic, session, at(WED, "12:00"), status="done")
    upcoming, past = await service.my_appointments(db, actor)
    assert [r.id for r in upcoming] == [soon.id, later.id]
    assert len(past) == 3 and past[0].starts_at >= past[-1].starts_at
    assert all(r.can_change for r in upcoming)
    assert not any(r.can_change for r in past)


async def test_a_row_carries_names_and_pakistan_time(db, clinic):
    doctor, session, patient, actor = await world(db, clinic)
    await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    (row,), _ = await service.my_appointments(db, actor)
    assert row.doctor_name == "Dr Booking" and row.specialty == "Cardiology"
    assert row.clinic_name == "Shifa International" and row.status == "booked"
    assert row.starts_at.date() == WED and row.starts_at.strftime("%H:%M") == "10:00"


async def test_my_appointments_never_includes_other_patients(db, clinic):
    doctor, session, _, actor = await world(db, clinic)
    other, _ = await other_patient(db)
    await add_appointment(db, other, doctor, clinic, session, at(WED, "10:00"))
    assert await service.my_appointments(db, actor) == ([], [])


async def test_appointment_for_patient_is_own_only(db, clinic):
    doctor, session, patient, actor = await world(db, clinic)
    other, _ = await other_patient(db)
    mine = await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    theirs = await add_appointment(db, other, doctor, clinic, session, at(WED, "11:00"))
    assert (await service.appointment_for_patient(db, actor, mine.id)).id == mine.id
    with pytest.raises(NotFound):
        await service.appointment_for_patient(db, actor, theirs.id)


# ── The doctor's day ─────────────────────────────────────────────────────
def doctor_actor(doctor) -> Actor:
    return Actor(user_id=doctor.user_id, role="doctor", is_verified_doctor=True)


async def test_the_doctors_day_lists_name_time_and_status_by_time(db, clinic):
    doctor, session, patient, _ = await world(db, clinic)
    other, _ = await other_patient(db, 3)
    await add_appointment(db, other, doctor, clinic, session, at(WED, "11:00"), status="cancelled")
    await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    rows = await service.doctor_day(db, doctor_actor(doctor), WED)
    assert [(r.patient_name, r.starts_at.strftime("%H:%M"), r.status) for r in rows] == [
        ("Pat Booking", "10:00", "booked"),
        ("Patient 3", "11:00", "cancelled"),
    ]


def test_the_doctors_day_has_only_name_time_and_status():
    # Booking is not consent: nothing else about the patient may be added here.
    assert set(DayRow.__dataclass_fields__) == {"starts_at", "patient_name", "status"}


async def test_the_doctors_day_excludes_other_days_and_other_doctors(db, clinic):
    doctor, session, patient, _ = await world(db, clinic)
    rival = await verified_doctor(db, clinic, email="rival@x.com", name="Dr Rival")
    rival_session = await add_session(db, rival, clinic)
    other, _ = await other_patient(db)
    await add_appointment(db, patient, doctor, clinic, session, at(date(2026, 10, 21), "10:00"))
    await add_appointment(db, other, rival, clinic, rival_session, at(WED, "10:00"))
    assert await service.doctor_day(db, doctor_actor(doctor), WED) == []


async def test_a_time_just_after_midnight_belongs_to_its_pakistan_day(db, clinic):
    # Review focus 2: 00:15 on Thursday the 15th is 19:15 UTC on the 14th.
    doctor, session, patient, _ = await world(
        db, clinic, weekday=THURSDAY, start="00:00", end="01:00"
    )
    thursday = date(2026, 10, 15)
    await add_appointment(db, patient, doctor, clinic, session, at(thursday, "00:15"))
    assert len(await service.doctor_day(db, doctor_actor(doctor), thursday)) == 1
    assert await service.doctor_day(db, doctor_actor(doctor), WED) == []


async def test_a_pending_doctor_or_a_patient_sees_no_appointments(db, clinic):
    _, _, _, actor = await world(db, clinic)
    user = await make_user(db, email="pending@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    with pytest.raises(NotFound):
        await service.doctor_day(db, Actor(user_id=user.id, role="doctor"), WED)
    with pytest.raises(NotFound):
        await service.doctor_day(db, actor, WED)
