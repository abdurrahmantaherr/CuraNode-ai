"""P5: booking a time, and every way it is refused."""

from __future__ import annotations

import json
from datetime import date, time

import pytest
from app.booking import service, slots
from app.db.models import (
    Appointment,
    AuditLog,
    ScheduleException,
    ScheduleExceptionType,
    UserRole,
    VerificationStatus,
)
from app.deps import Actor
from app.errors import BookingRefused, NotFound
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from tests.booking_helpers import (
    MONDAY,
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
from tests.conftest import add_affiliation, doctor_of, make_user


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    freeze(monkeypatch)


async def world(db, clinic, *, cap=16, **session_kw):
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic, cap=cap, **session_kw)
    _, patient, actor = await make_patient(db)
    return doctor, session, patient, actor


async def another_patient(db, n: int):
    """(patient row, actor) for a second, third... patient."""
    _, patient, actor = await make_patient(db, email=f"p{n}@x.com", name=f"Patient {n}")
    return patient, actor


async def expect_refusal(reason: str, call) -> None:
    with pytest.raises(BookingRefused) as exc:
        await call
    assert exc.value.reason == reason


async def stored(db) -> list[Appointment]:
    return list((await db.execute(select(Appointment))).scalars().all())


# ── The happy path ───────────────────────────────────────────────────────
async def test_a_free_time_is_booked(db, clinic):
    doctor, session, patient, actor = await world(db, clinic)
    appt = await service.book(db, actor, doctor.id, clinic.id, at(WED, "10:00"))
    assert appt.status == "booked" and appt.booking_channel == "online"
    assert appt.schedule_id == session.id
    assert (appt.doctor_id, appt.clinic_id, appt.patient_id) == (doctor.id, clinic.id, patient.id)
    assert slots.as_utc(appt.slot_start) == at(WED, "10:00")
    assert len(await stored(db)) == 1


async def test_a_time_after_midnight_in_pakistan_is_booked_on_its_pakistan_date(db, clinic):
    # Review focus 2.
    doctor, _, _, actor = await world(db, clinic, weekday=THURSDAY, start="00:00", end="01:00")
    thursday = date(2026, 10, 15)
    appt = await service.book(db, actor, doctor.id, clinic.id, at(thursday, "00:15"))
    assert slots.as_utc(appt.slot_start).isoformat() == "2026-10-14T19:15:00+00:00"


async def test_a_naive_start_is_read_as_utc(db, clinic):
    doctor, _, _, actor = await world(db, clinic)
    naive = at(WED, "10:00").replace(tzinfo=None)
    appt = await service.book(db, actor, doctor.id, clinic.id, naive)
    assert slots.as_utc(appt.slot_start) == at(WED, "10:00")


# ── Taken, full, and what frees them ─────────────────────────────────────
async def test_a_taken_time_is_refused(db, clinic):
    doctor, _, _, actor = await world(db, clinic)
    _, other = await another_patient(db, 2)
    await service.book(db, other, doctor.id, clinic.id, at(WED, "10:00"))
    await expect_refusal("taken", service.book(db, actor, doctor.id, clinic.id, at(WED, "10:00")))
    assert len(await stored(db)) == 1


async def test_a_full_session_is_refused(db, clinic):
    doctor, _, _, actor = await world(db, clinic, cap=2)
    (_, first), (_, second) = await another_patient(db, 2), await another_patient(db, 3)
    await service.book(db, first, doctor.id, clinic.id, at(WED, "09:00"))
    await service.book(db, second, doctor.id, clinic.id, at(WED, "09:15"))
    await expect_refusal("full", service.book(db, actor, doctor.id, clinic.id, at(WED, "10:00")))


async def test_cancel_then_rebook_the_same_time_works(db, clinic):
    # Review focus 5 (second half): the partial index ignores cancelled rows.
    doctor, session, _, actor = await world(db, clinic)
    other, _ = await another_patient(db, 2)
    first = await add_appointment(db, other, doctor, clinic, session, at(WED, "10:00"))
    first.status = "cancelled"
    await db.commit()
    appt = await service.book(db, actor, doctor.id, clinic.id, at(WED, "10:00"))
    assert appt.status == "booked" and len(await stored(db)) == 2


# ── The window ───────────────────────────────────────────────────────────
async def test_the_lead_time_boundary(db, clinic):
    doctor, _, _, actor = await world(db, clinic, weekday=MONDAY, start="12:00", end="18:00")
    # Now is Monday 13:00 in Pakistan; the earliest bookable time is 13:30.
    await expect_refusal(
        "too_soon", service.book(db, actor, doctor.id, clinic.id, at(TODAY, "13:15"))
    )
    appt = await service.book(db, actor, doctor.id, clinic.id, at(TODAY, "13:30"))
    assert appt.status == "booked"


async def test_a_day_beyond_the_horizon_is_refused_and_the_last_day_is_allowed(db, clinic):
    doctor, _, _, actor = await world(db, clinic, weekday=MONDAY, start="14:00", end="18:00")
    await expect_refusal(
        "too_far", service.book(db, actor, doctor.id, clinic.id, at(date(2026, 11, 2), "14:00"))
    )
    appt = await service.book(db, actor, doctor.id, clinic.id, at(date(2026, 10, 26), "14:00"))
    assert appt.status == "booked"


@pytest.mark.parametrize(
    ("day", "hhmm"),
    [
        (WED, "10:07"),  # off the grid
        (WED, "13:00"),  # after the session
        (WED, "08:45"),  # before the session
        (date(2026, 10, 15), "10:00"),  # a day with no session
    ],
)
async def test_a_time_the_session_does_not_offer_is_refused(db, clinic, day, hhmm):
    doctor, _, _, actor = await world(db, clinic)
    await expect_refusal(
        "not_offered", service.book(db, actor, doctor.id, clinic.id, at(day, hhmm))
    )
    assert await stored(db) == []


async def test_a_time_removed_after_the_page_loaded_is_not_booked(db, clinic):
    # Review focus 3: the page listed 10:00, then the doctor took the day off.
    doctor, session, _, actor = await world(db, clinic)
    options = await service.list_options(db, doctor.id, clinic.id)
    assert at(WED, "10:00") in options[0].times
    db.add(
        ScheduleException(
            schedule_id=session.id, exception_date=WED, kind=ScheduleExceptionType.LEAVE
        )
    )
    await db.commit()
    await expect_refusal(
        "not_offered", service.book(db, actor, doctor.id, clinic.id, at(WED, "10:00"))
    )
    assert await stored(db) == []


async def test_an_extra_session_has_its_own_cap_and_count(db, clinic):
    # Review focus 4: the weekly window and an extra on the same date share a
    # schedule_id but are separate sessions.
    doctor, session, _, actor = await world(db, clinic, cap=2)
    db.add(
        ScheduleException(
            schedule_id=session.id,
            exception_date=WED,
            kind=ScheduleExceptionType.EXTRA,
            start_time=time(16, 0),
            end_time=time(18, 0),
            max_tokens=1,
        )
    )
    await db.commit()
    (_, p2), (_, p3), (_, p4) = (
        await another_patient(db, 2),
        await another_patient(db, 3),
        await another_patient(db, 4),
    )
    await service.book(db, p2, doctor.id, clinic.id, at(WED, "09:00"))
    await service.book(db, p3, doctor.id, clinic.id, at(WED, "09:15"))
    await expect_refusal("full", service.book(db, actor, doctor.id, clinic.id, at(WED, "10:00")))
    appt = await service.book(db, actor, doctor.id, clinic.id, at(WED, "16:00"))  # the extra
    assert appt.status == "booked"
    await expect_refusal("full", service.book(db, p4, doctor.id, clinic.id, at(WED, "16:15")))


async def test_an_extra_without_its_own_cap_inherits_the_weekly_cap(db, clinic):
    # Spec test 13: an extra's cap comes from its anchor slot (D7 R14), but the
    # extra is counted apart from the weekly window.
    doctor, session, _, actor = await world(db, clinic, cap=2)
    db.add(
        ScheduleException(
            schedule_id=session.id,
            exception_date=WED,
            kind=ScheduleExceptionType.EXTRA,
            start_time=time(16, 0),
            end_time=time(18, 0),
        )
    )
    await db.commit()
    (_, p2), (_, p3) = await another_patient(db, 2), await another_patient(db, 3)
    await service.book(db, p2, doctor.id, clinic.id, at(WED, "16:00"))
    await service.book(db, p3, doctor.id, clinic.id, at(WED, "16:15"))
    await expect_refusal("full", service.book(db, actor, doctor.id, clinic.id, at(WED, "16:30")))
    weekly = await service.book(db, actor, doctor.id, clinic.id, at(WED, "09:00"))
    assert weekly.status == "booked"  # the weekly window was not used up by the extra


# ── The patient's own rules ──────────────────────────────────────────────
async def test_overlap_with_another_doctor_is_refused_but_back_to_back_is_not(db, clinic):
    doctor_a, _, _, actor = await world(db, clinic)
    doctor_b = await verified_doctor(db, clinic, email="b.dr@x.com", name="Dr B")
    await add_session(db, doctor_b, clinic, minutes=30)
    await service.book(db, actor, doctor_a.id, clinic.id, at(WED, "10:15"))  # 10:15-10:30
    # Dr B's 10:00 turn lasts 30 minutes (10:00-10:30): it overlaps.
    await expect_refusal(
        "overlap", service.book(db, actor, doctor_b.id, clinic.id, at(WED, "10:00"))
    )
    # 10:30-11:00 starts exactly when the first one ends.
    appt = await service.book(db, actor, doctor_b.id, clinic.id, at(WED, "10:30"))
    assert appt.status == "booked"


async def test_two_appointments_with_one_doctor_on_one_day_are_refused(db, clinic):
    doctor, _, _, actor = await world(db, clinic)
    await service.book(db, actor, doctor.id, clinic.id, at(WED, "10:00"))
    await expect_refusal(
        "same_day", service.book(db, actor, doctor.id, clinic.id, at(WED, "11:00"))
    )
    next_week = await service.book(db, actor, doctor.id, clinic.id, at(date(2026, 10, 21), "11:00"))
    assert next_week.status == "booked"


async def test_a_cancelled_appointment_does_not_count_against_the_patient(db, clinic):
    doctor, session, patient, actor = await world(db, clinic)
    await add_appointment(
        db, patient, doctor, clinic, session, at(WED, "10:00"), status="cancelled"
    )
    appt = await service.book(db, actor, doctor.id, clinic.id, at(WED, "10:00"))
    assert appt.status == "booked"


# ── Who may be booked, and by whom ───────────────────────────────────────
async def test_a_pending_doctor_cannot_be_booked(db, clinic):
    _, _, _, actor = await world(db, clinic)
    user = await make_user(db, email="pending@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    pending = await doctor_of(db, user)
    await add_session(db, pending, clinic)
    with pytest.raises(NotFound):
        await service.book(db, actor, pending.id, clinic.id, at(WED, "10:00"))


async def test_a_doctor_unverified_after_the_page_opened_cannot_be_booked(db, clinic):
    # Review focus 5 (first half).
    doctor, _, _, actor = await world(db, clinic)
    doctor.verification_status = VerificationStatus.REJECTED
    doctor.verified_by = None
    doctor.verified_at = None
    await db.commit()
    with pytest.raises(NotFound):
        await service.book(db, actor, doctor.id, clinic.id, at(WED, "10:00"))


async def test_a_hospital_the_doctor_does_not_work_at_is_not_found(db, clinic, second_clinic):
    doctor, _, _, actor = await world(db, clinic)
    with pytest.raises(NotFound):
        await service.book(db, actor, doctor.id, second_clinic.id, at(WED, "10:00"))


async def test_an_inactive_affiliation_cannot_be_booked(db, clinic, second_clinic):
    doctor, _, _, actor = await world(db, clinic)
    await add_affiliation(db, doctor.id, second_clinic.id, status="inactive")
    await add_session(db, doctor, second_clinic)
    with pytest.raises(NotFound):
        await service.book(db, actor, doctor.id, second_clinic.id, at(WED, "10:00"))


async def test_only_a_patient_can_book(db, clinic):
    doctor, _, _, _ = await world(db, clinic)
    doctor_actor = Actor(user_id=doctor.user_id, role="doctor")
    with pytest.raises(NotFound):
        await service.book(db, doctor_actor, doctor.id, clinic.id, at(WED, "10:00"))


# ── Locking, the database backstop, audit ────────────────────────────────
async def test_booking_locks_the_doctor_first(db, clinic, monkeypatch):
    doctor, _, _, actor = await world(db, clinic)
    calls = []
    real = service.schedule_service.lock_doctor

    async def spy(session, doctor_id):
        calls.append(doctor_id)
        await real(session, doctor_id)

    monkeypatch.setattr(service.schedule_service, "lock_doctor", spy)
    await service.book(db, actor, doctor.id, clinic.id, at(WED, "10:00"))
    assert calls == [doctor.id]
    # The statement behind it is the per-doctor row lock (D7 AC-12 asserts the same thing).
    lock_sql = str(
        service.schedule_service.lock_statement(doctor.id).compile(dialect=postgresql.dialect())
    )
    assert "FOR UPDATE" in lock_sql


async def test_the_index_is_the_backstop_if_a_race_slips_past_the_checks(db, clinic, monkeypatch):
    doctor, session, _, actor = await world(db, clinic)
    other, _ = await another_patient(db, 2)
    await add_appointment(db, other, doctor, clinic, session, at(WED, "10:00"))

    async def blind(*args, **kwargs):  # a concurrent booking the checks did not see
        return {session.id: []}

    monkeypatch.setattr(service, "_active_starts", blind)
    await expect_refusal("taken", service.book(db, actor, doctor.id, clinic.id, at(WED, "10:00")))
    assert len(await stored(db)) == 1


async def test_a_booking_writes_one_audit_row_with_ids_only(db, clinic):
    doctor, _, patient, actor = await world(db, clinic)
    appt = await service.book(db, actor, doctor.id, clinic.id, at(WED, "10:00"))
    rows = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "appointment.book")))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert json.loads(rows[0].detail) == {
        "appointment_id": str(appt.id),
        "doctor_id": str(doctor.id),
        "clinic_id": str(clinic.id),
    }
    assert rows[0].subject_patient_id == patient.id and rows[0].actor_role == "patient"
