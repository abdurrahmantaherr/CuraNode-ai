"""D7 schedule service on SQLite (T6b core; T7 slots and T8 exceptions follow).

"Today" is frozen at Monday 2026-10-12 through `app.db.types.today_pk`, the
way the code reads it. Rows are inserted through the models where a test only
needs state; writes under test go through the service.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import date, time, timedelta

import pytest
from app.db import types as dbtypes
from app.db.models import (
    Appointment,
    AuditLog,
    Base,
    Clinic,
    Doctor,
    DoctorAffiliation,
    DoctorSchedule,
    Patient,
    Profile,
    ScheduleException,
    ScheduleExceptionType,
    UserRole,
)
from app.deps import Actor
from app.errors import ListFull, NotFound, ScheduleConflict, ValidationFailed
from app.schedule import service
from app.schedule.schemas import ExtraCreate, SlotCreate, SlotUpdate, TimeOffCreate
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.conftest import add_affiliation, doctor_of, make_user

TODAY = date(2026, 10, 12)  # a Monday
MONDAY, TUESDAY = 0, 1


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    monkeypatch.setattr(dbtypes, "today_pk", lambda: TODAY)


def t(hhmm: str) -> time:
    return time.fromisoformat(hhmm)


async def affiliation_at(db, doctor_id, clinic_id) -> DoctorAffiliation:
    return (
        await db.execute(
            select(DoctorAffiliation).where(
                DoctorAffiliation.doctor_id == doctor_id, DoctorAffiliation.clinic_id == clinic_id
            )
        )
    ).scalar_one()


async def add_slot(
    db, affiliation, weekday=MONDAY, start="09:00", end="13:00", cap=16, status="active"
) -> DoctorSchedule:
    row = DoctorSchedule(
        affiliation_id=affiliation.id,
        weekday=weekday,
        start_time=t(start),
        end_time=t(end),
        slot_minutes=15,
        max_tokens=cap,
        status=status,
    )
    db.add(row)
    await db.commit()
    return row


async def add_exception(
    db, slot, kind, day=TODAY, start=None, end=None, cap=None, removed=False
) -> ScheduleException:
    row = ScheduleException(
        schedule_id=slot.id,
        exception_date=day,
        kind=kind,
        start_time=t(start) if start else None,
        end_time=t(end) if end else None,
        max_tokens=cap,
        removed_at=dbtypes.utcnow() if removed else None,
    )
    db.add(row)
    await db.commit()
    return row


@pytest.fixture
async def doc(db, schedule_doctor):
    return await doctor_of(db, schedule_doctor)


@pytest.fixture
async def aff_a(db, doc, clinic):
    return await affiliation_at(db, doc.id, clinic.id)


@pytest.fixture
async def aff_b(db, doc, second_clinic):
    return await affiliation_at(db, doc.id, second_clinic.id)


def spans(windows) -> list[tuple[date, str, str]]:
    return [(w.day, w.start.strftime("%H:%M"), w.end.strftime("%H:%M")) for w in windows]


# ── AC-12: the lock ──────────────────────────────────────────────────────
def test_lock_statement_is_select_for_update():
    sql = str(service.lock_statement(uuid.uuid4()).compile(dialect=postgresql.dialect())).upper()
    assert "FOR UPDATE" in sql
    assert "FROM DOCTOR" in sql


# ── Availability (AC-8) ──────────────────────────────────────────────────
async def test_availability_applies_exceptions_across_dates_and_clinics(db, doc, aff_a, aff_b):
    s = await add_slot(db, aff_a)  # Monday 09–13 at A
    b = await add_slot(db, aff_b, weekday=TUESDAY, start="10:00", end="12:00", cap=8)
    await add_exception(db, s, ScheduleExceptionType.LEAVE, start="11:00", end="13:00")
    await add_exception(db, b, ScheduleExceptionType.EXTRA, start="14:00", end="16:00")
    await add_exception(db, s, ScheduleExceptionType.EMERGENCY, day=TODAY + timedelta(days=7))

    windows = await service.get_effective_availability(db, doc.id, TODAY, TODAY + timedelta(days=8))

    assert spans(windows) == [
        (TODAY, "09:00", "11:00"),  # slot minus the leave window
        (TODAY, "14:00", "16:00"),  # extra at B on Monday
        (TODAY + timedelta(days=1), "10:00", "12:00"),  # B's Tuesday slot
        (TODAY + timedelta(days=8), "10:00", "12:00"),  # next Tuesday; next Monday is off
    ]
    extra = windows[1]
    assert (extra.is_extra, extra.max_tokens, extra.inherited) == (True, 8, True)


async def test_inactive_affiliation_clinic_or_status_gives_no_availability(
    db, doc, aff_a, inactive_clinic
):
    closed = await add_affiliation(db, doc.id, inactive_clinic.id)
    await add_slot(db, closed)  # clinic inactive
    gone = await add_affiliation(db, doc.id, inactive_clinic.id, status="inactive")
    await add_slot(db, gone, start="14:00", end="15:00")  # affiliation inactive
    await add_slot(db, aff_a, start="16:00", end="17:00", status="inactive")  # removed slot
    await add_slot(
        db, aff_a, start="18:00", end="19:00", status="paused"
    )  # another codebase's value

    assert await service.get_effective_availability(db, doc.id, TODAY, TODAY) == []


async def test_removed_exceptions_are_ignored(db, doc, aff_a):
    s = await add_slot(db, aff_a)
    await add_exception(db, s, ScheduleExceptionType.LEAVE, removed=True)
    windows = await service.get_effective_availability(db, doc.id, TODAY, TODAY)
    assert spans(windows) == [(TODAY, "09:00", "13:00")]


async def test_inherited_cap_is_read_live(db, doc, aff_a):
    s = await add_slot(db, aff_a)
    await add_exception(db, s, ScheduleExceptionType.EXTRA, start="13:00", end="15:00")
    [_, before] = await service.get_effective_availability(db, doc.id, TODAY, TODAY)
    assert (before.max_tokens, before.inherited) == (16, True)

    s.max_tokens = 30
    await db.commit()
    [_, after] = await service.get_effective_availability(db, doc.id, TODAY, TODAY)
    assert (after.max_tokens, after.inherited) == (30, True)


async def test_clinic_filter(db, doc, aff_a, aff_b, second_clinic):
    await add_slot(db, aff_a)
    await add_slot(db, aff_b, start="14:00", end="16:00")
    windows = await service.get_effective_availability(
        db, doc.id, TODAY, TODAY, clinic_id=second_clinic.id
    )
    assert spans(windows) == [(TODAY, "14:00", "16:00")]


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (TODAY - timedelta(days=1), TODAY),  # past start
        (TODAY, TODAY + timedelta(days=30)),  # 31 days
        (TODAY + timedelta(days=2), TODAY),  # backwards
    ],
)
async def test_availability_range_rules(db, doc, start, end):
    with pytest.raises(ValidationFailed) as exc:
        await service.get_effective_availability(db, doc.id, start, end)
    assert "errors.schedule_range_invalid" in exc.value.details["fields"].values()


async def test_thirty_days_is_allowed(db, doc, aff_a):
    await add_slot(db, aff_a)
    windows = await service.get_effective_availability(
        db, doc.id, TODAY, TODAY + timedelta(days=29)
    )
    assert len(windows) == 5  # five Mondays in 30 days from a Monday


# ── Scoping ──────────────────────────────────────────────────────────────
async def test_another_doctors_rows_are_invisible(db, doc, aff_a, clinic):
    other_user = await make_user(db, email="other@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    other = await doctor_of(db, other_user)
    await add_slot(db, await affiliation_at(db, other.id, clinic.id))

    assert await service.get_effective_availability(db, doc.id, TODAY, TODAY) == []
    assert await service.find_conflicts(db, doc.id, MONDAY, t("09:00"), t("13:00")) == []


# ── R1 through the service ───────────────────────────────────────────────
async def test_find_conflicts(db, doc, aff_a, aff_b):
    s = await add_slot(db, aff_a)
    [c] = await service.find_conflicts(db, doc.id, MONDAY, t("12:00"), t("14:00"))
    assert c.schedule_id == s.id
    assert await service.find_conflicts(db, doc.id, MONDAY, t("13:00"), t("15:00")) == []
    assert await service.find_conflicts(db, doc.id, MONDAY, t("10:00"), t("11:00"), s.id) == []


# ── T7: weekly slots ─────────────────────────────────────────────────────
def actor_for(user) -> Actor:
    return Actor(user_id=user.id, role=UserRole.DOCTOR.value)


def slot_body(clinic_id, weekdays=("0",), start="09:00", end="13:00", **kw) -> SlotCreate:
    return SlotCreate(
        clinic_id=str(clinic_id), weekdays=list(weekdays), start_time=start, end_time=end, **kw
    )


def update_body(clinic_id, weekday="0", start="09:00", end="13:00", **kw) -> SlotUpdate:
    return SlotUpdate(
        clinic_id=str(clinic_id), weekday=weekday, start_time=start, end_time=end, **kw
    )


async def audit_rows(db, action: str) -> list[dict]:
    rows = await db.execute(select(AuditLog).where(AuditLog.action == action))
    return [json.loads(r.detail) for r in rows.scalars().all()]


async def book(db, slot, clinic, doctor, *, status="booked", when=None) -> None:
    patient_user = await make_user(
        db, email=f"p{uuid.uuid4().hex[:6]}@x.com", role=UserRole.PATIENT
    )
    patient = (
        await db.execute(select(Patient).where(Patient.user_id == patient_user.id))
    ).scalar_one()
    db.add(
        Appointment(
            patient_id=patient.id,
            doctor_id=doctor.id,
            clinic_id=clinic.id,
            schedule_id=slot.id,
            slot_start=when or dbtypes.utcnow() + timedelta(days=3),
            status=status,
        )
    )
    await db.commit()


@pytest.fixture
def me(schedule_doctor) -> Actor:
    return actor_for(schedule_doctor)


async def test_ac1_add_a_slot_on_the_clinics_affiliation(db, me, doc, aff_a, clinic):
    [row] = await service.add_slots(db, me, slot_body(clinic.id))
    assert (row.affiliation_id, row.weekday, row.status) == (aff_a.id, MONDAY, "active")
    assert (row.slot_minutes, row.max_tokens) == (15, 16)  # blank cap stores calc (AC-23)
    windows = await service.get_effective_availability(db, doc.id, TODAY, TODAY)
    assert spans(windows) == [(TODAY, "09:00", "13:00")]


async def test_ac2_overlap_at_another_clinic_names_the_slot(db, me, aff_a, second_clinic, clinic):
    s = await add_slot(db, aff_a)
    with pytest.raises(ScheduleConflict) as exc:
        await service.add_slots(db, me, slot_body(second_clinic.id, start="12:00", end="14:00"))
    details = exc.value.details
    assert (details["clinic"], details["day"], details["start"], details["end"]) == (
        clinic.name,
        MONDAY,
        "09:00",
        "13:00",
    )
    assert (details["conflict_id"], details["clinic_inactive"]) == (str(s.id), "")


async def test_ac2_a_slot_at_an_inactive_clinic_still_blocks_and_is_flagged(
    db, me, doc, inactive_clinic, clinic
):
    closed = await add_affiliation(db, doc.id, inactive_clinic.id, status="inactive")
    await add_slot(db, closed)
    with pytest.raises(ScheduleConflict) as exc:
        await service.add_slots(db, me, slot_body(clinic.id, start="10:00", end="11:00"))
    assert exc.value.details["clinic_inactive"] == "1"


async def test_ac3_back_to_back_is_accepted(db, me, aff_a, clinic, second_clinic):
    await add_slot(db, aff_a)
    await service.add_slots(db, me, slot_body(second_clinic.id, start="13:00", end="15:00"))
    await service.add_slots(db, me, slot_body(clinic.id, start="07:00", end="09:00"))


async def test_ac4_only_an_active_affiliation_at_an_active_clinic(
    db, me, doc, inactive_clinic, clinic
):
    unknown = uuid.uuid4()
    with pytest.raises(ValidationFailed) as exc:
        await service.add_slots(db, me, slot_body(unknown))
    assert exc.value.details["fields"] == {"clinic_id": "errors.schedule_clinic_not_affiliated"}

    await add_affiliation(db, doc.id, inactive_clinic.id)  # clinic inactive
    with pytest.raises(ValidationFailed):
        await service.add_slots(db, me, slot_body(inactive_clinic.id))

    aff = await affiliation_at(db, doc.id, clinic.id)
    aff.status = "inactive"  # affiliation inactive
    await db.commit()
    with pytest.raises(ValidationFailed):
        await service.add_slots(db, me, slot_body(clinic.id))


async def test_ac15_a_slot_over_a_future_extra_at_any_clinic_is_rejected(
    db, me, aff_a, aff_b, clinic
):
    b = await add_slot(db, aff_b, weekday=TUESDAY, start="10:00", end="12:00")
    await add_exception(
        db,
        b,
        ScheduleExceptionType.EXTRA,
        day=TODAY + timedelta(days=7),
        start="09:00",
        end="10:00",
    )
    with pytest.raises(ScheduleConflict) as exc:
        await service.add_slots(db, me, slot_body(clinic.id, start="08:00", end="11:00"))
    assert exc.value.details["date"] == (TODAY + timedelta(days=7)).isoformat()


async def test_ac17_multi_weekday_add_is_all_or_nothing(db, me, aff_a, clinic):
    await add_slot(db, aff_a, weekday=TUESDAY)
    with pytest.raises(ScheduleConflict):
        await service.add_slots(db, me, slot_body(clinic.id, weekdays=("0", "1", "2")))
    count = (await db.execute(select(DoctorSchedule))).scalars().all()
    assert len(count) == 1


async def test_ac17_multi_weekday_add_writes_one_row_and_audit_each(db, me, clinic):
    rows = await service.add_slots(db, me, slot_body(clinic.id, weekdays=("0", "2", "4")))
    assert sorted(r.weekday for r in rows) == [0, 2, 4]
    assert len(await audit_rows(db, "schedule.slot.add")) == 3


async def test_ac18_the_eleventh_slot_on_a_weekday_is_rejected(db, me, aff_a, clinic):
    for hour in range(10):
        await add_slot(db, aff_a, start=f"{hour:02d}:00", end=f"{hour:02d}:30")
    with pytest.raises(ListFull) as exc:
        await service.add_slots(db, me, slot_body(clinic.id, start="20:00", end="21:00"))
    assert exc.value.details == {"max": 10}


async def test_ac22_ac23_minutes_and_cap_are_stored(db, me, clinic):
    [row] = await service.add_slots(
        db, me, slot_body(clinic.id, slot_minutes="20", max_tokens="25")
    )
    assert (row.slot_minutes, row.max_tokens) == (20, 25)


async def test_update_keeps_an_entered_cap_and_blank_resets_to_calc(db, me, aff_a, clinic):
    s = await add_slot(db, aff_a, cap=40)
    row = await service.update_slot(
        db, me, s.id, update_body(clinic.id, end="12:00", max_tokens="40")
    )
    assert row.max_tokens == 40
    row = await service.update_slot(
        db, me, s.id, update_body(clinic.id, end="12:00", max_tokens="")
    )
    assert row.max_tokens == 12  # 3 hours at 15 minutes


async def test_update_can_move_a_slot_to_another_clinic(db, me, aff_a, aff_b, second_clinic):
    s = await add_slot(db, aff_a)
    row = await service.update_slot(db, me, s.id, update_body(second_clinic.id, weekday="3"))
    assert (row.affiliation_id, row.weekday) == (aff_b.id, 3)
    [detail] = await audit_rows(db, "schedule.slot.update")
    assert (detail["before"]["weekday"], detail["after"]["weekday"]) == (0, 3)
    assert detail["after"]["clinic_id"] == str(second_clinic.id)


async def test_update_rejects_its_own_r1_overlap_only_with_others(db, me, aff_a, clinic):
    s = await add_slot(db, aff_a)
    await add_slot(db, aff_a, start="14:00", end="16:00")
    await service.update_slot(db, me, s.id, update_body(clinic.id, start="10:00", end="14:00"))
    with pytest.raises(ScheduleConflict):
        await service.update_slot(db, me, s.id, update_body(clinic.id, start="10:00", end="15:00"))


async def test_a_slot_at_an_inactive_clinic_can_be_removed_not_edited(
    db, me, doc, inactive_clinic, clinic
):
    closed = await add_affiliation(db, doc.id, inactive_clinic.id, status="inactive")
    s = await add_slot(db, closed)
    with pytest.raises(ValidationFailed):
        await service.update_slot(db, me, s.id, update_body(clinic.id))
    await service.remove_slot(db, me, s.id)
    await db.refresh(s)
    assert s.status == "inactive"


async def test_ac19_ac28_upcoming_bookings_lock_the_slot(db, me, doc, aff_a, clinic):
    s = await add_slot(db, aff_a)
    await book(db, s, clinic, doc)
    for body in (
        update_body(clinic.id, max_tokens="20"),
        update_body(clinic.id, slot_minutes="10"),
    ):
        with pytest.raises(ValidationFailed) as exc:
            await service.update_slot(db, me, s.id, body)
        assert exc.value.details["fields"] == {"__all__": "errors.schedule_has_appointments"}
    with pytest.raises(ValidationFailed):
        await service.remove_slot(db, me, s.id)


async def test_cancelled_or_past_bookings_do_not_lock_the_slot(db, me, doc, aff_a, clinic):
    s = await add_slot(db, aff_a)
    await book(db, s, clinic, doc, status="cancelled")
    await book(db, s, clinic, doc, when=dbtypes.utcnow() - timedelta(days=1))
    await service.update_slot(db, me, s.id, update_body(clinic.id, max_tokens="20"))


async def test_ac20_removing_a_slot_soft_removes_its_future_exceptions(db, me, aff_a):
    s = await add_slot(db, aff_a)
    future = await add_exception(db, s, ScheduleExceptionType.LEAVE, day=TODAY + timedelta(days=7))
    extra = await add_exception(
        db,
        s,
        ScheduleExceptionType.EXTRA,
        day=TODAY + timedelta(days=1),
        start="09:00",
        end="10:00",
    )
    past = await add_exception(db, s, ScheduleExceptionType.LEAVE, day=TODAY - timedelta(days=7))

    await service.remove_slot(db, me, s.id)

    for row in (s, future, extra, past):
        await db.refresh(row)
    assert s.status == "inactive"
    assert future.removed_at is not None and extra.removed_at is not None
    assert past.removed_at is None  # history is not touched
    assert len((await db.execute(select(ScheduleException))).scalars().all()) == 3  # none deleted
    assert len(await audit_rows(db, "schedule.slot.remove")) == 1
    assert len(await audit_rows(db, "schedule.exception.remove")) == 2


async def test_ac10_foreign_removed_and_unknown_slots_are_not_found(db, me, aff_a, clinic):
    other_user = await make_user(
        db, email="other2@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id
    )
    foreign = await add_slot(
        db, await affiliation_at(db, (await doctor_of(db, other_user)).id, clinic.id)
    )
    removed = await add_slot(db, aff_a, status="inactive")
    for slot_id in (foreign.id, removed.id, uuid.uuid4()):
        with pytest.raises(NotFound):
            await service.update_slot(db, me, slot_id, update_body(clinic.id))
        with pytest.raises(NotFound):
            await service.remove_slot(db, me, slot_id)


async def test_ac12_every_slot_write_takes_the_lock_first(db, me, aff_a, clinic, monkeypatch):
    calls: list[str] = []
    real = service.lock_doctor

    async def spy(session, doctor_id):
        calls.append("lock")
        await real(session, doctor_id)

    monkeypatch.setattr(service, "lock_doctor", spy)
    [row] = await service.add_slots(db, me, slot_body(clinic.id))
    await service.update_slot(db, me, row.id, update_body(clinic.id, end="12:00"))
    await service.remove_slot(db, me, row.id)
    assert calls == ["lock", "lock", "lock"]


async def test_ac12_a_sequential_double_submit_is_rejected(db, me, clinic):
    await service.add_slots(db, me, slot_body(clinic.id))
    with pytest.raises(ScheduleConflict):
        await service.add_slots(db, me, slot_body(clinic.id))


async def test_ac13_slot_audit_rows_hold_no_patient_and_name_the_row(db, me, clinic):
    [row] = await service.add_slots(db, me, slot_body(clinic.id))
    rows = (await db.execute(select(AuditLog))).scalars().all()
    [entry] = [r for r in rows if r.action == "schedule.slot.add"]
    assert (entry.actor_role, entry.resource_type, entry.subject_patient_id) == (
        "doctor",
        "doctor_schedule",
        None,
    )
    assert json.loads(entry.detail)["resource_id"] == str(row.id)


# ── T8: time off, extra hours, removing exceptions ───────────────────────
def time_off(day=TODAY, clinic_id=None, start="", end="", **kw) -> TimeOffCreate:
    return TimeOffCreate(
        exception_date=day.isoformat(),
        clinic_id=str(clinic_id) if clinic_id else "",
        start_time=start,
        end_time=end,
        **kw,
    )


def extra_body(clinic_id, start, end, day=TODAY, **kw) -> ExtraCreate:
    return ExtraCreate(
        exception_date=day.isoformat(),
        clinic_id=str(clinic_id),
        start_time=start,
        end_time=end,
        **kw,
    )


@pytest.fixture
async def s(db, aff_a) -> DoctorSchedule:
    """Spec §10.1: S = Monday 09:00-13:00 at A."""
    return await add_slot(db, aff_a)


@pytest.fixture
async def b_anchor(db, aff_b) -> DoctorSchedule:
    """B's only slot (Wednesday), so extras at B have an anchor."""
    return await add_slot(db, aff_b, weekday=2, start="10:00", end="12:00", cap=8)


async def test_ac6_whole_day_all_clinics_writes_one_row_per_slot(
    db, me, doc, s, aff_b, second_clinic
):
    b_monday = await add_slot(db, aff_b, start="14:00", end="16:00")
    rows = await service.add_time_off(db, me, time_off(reason="Conference"))
    assert sorted(r.schedule_id for r in rows) == sorted([s.id, b_monday.id])
    assert all(r.start_time is None and r.kind is ScheduleExceptionType.LEAVE for r in rows)
    assert await service.get_effective_availability(db, doc.id, TODAY, TODAY) == []
    await db.refresh(s)
    assert (s.start_time, s.end_time, s.status) == (t("09:00"), t("13:00"), "active")


async def test_ac6_window_one_clinic_writes_the_intersection(db, me, doc, s, clinic):
    [row] = await service.add_time_off(
        db, me, time_off(clinic_id=clinic.id, start="12:00", end="15:00", emergency="on")
    )
    assert (row.start_time, row.end_time, row.kind) == (
        t("12:00"),
        t("13:00"),
        ScheduleExceptionType.EMERGENCY,
    )
    windows = await service.get_effective_availability(db, doc.id, TODAY, TODAY)
    assert spans(windows) == [(TODAY, "09:00", "12:00")]


async def test_time_off_that_hits_nothing_is_rejected(db, me, s):
    with pytest.raises(ValidationFailed) as exc:
        await service.add_time_off(db, me, time_off(start="13:00", end="15:00"))
    assert exc.value.details["fields"] == {"__all__": "errors.schedule_nothing_to_block"}


async def test_ac4_clinic_specific_time_off_needs_an_affiliation(db, me, s):
    with pytest.raises(ValidationFailed):
        await service.add_time_off(db, me, time_off(clinic_id=uuid.uuid4()))


async def test_ac7_extra_adds_availability_with_its_cap(db, me, doc, s, clinic):
    row = await service.add_extra(db, me, extra_body(clinic.id, "14:00", "16:00", max_tokens="6"))
    assert (row.schedule_id, row.max_tokens, row.kind) == (s.id, 6, ScheduleExceptionType.EXTRA)
    windows = await service.get_effective_availability(db, doc.id, TODAY, TODAY)
    assert spans(windows) == [(TODAY, "09:00", "13:00"), (TODAY, "14:00", "16:00")]
    assert (windows[1].max_tokens, windows[1].inherited) == (6, False)


async def test_ac7_extra_needs_a_slot_at_that_clinic_to_anchor_to(db, me, s, second_clinic):
    with pytest.raises(ValidationFailed) as exc:
        await service.add_extra(db, me, extra_body(second_clinic.id, "14:00", "16:00"))
    assert exc.value.details["fields"] == {"clinic_id": "errors.schedule_extra_needs_slot"}


async def test_ac4_extra_needs_an_active_affiliation(db, me, s):
    with pytest.raises(ValidationFailed):
        await service.add_extra(db, me, extra_body(uuid.uuid4(), "14:00", "16:00"))


async def test_ac9_a_past_exception_cannot_be_removed(db, me, s):
    past = await add_exception(db, s, ScheduleExceptionType.LEAVE, day=TODAY - timedelta(days=7))
    with pytest.raises(ValidationFailed) as exc:
        await service.remove_exception(db, me, past.id)
    assert exc.value.details["fields"] == {"__all__": "errors.schedule_past_date"}


async def test_removing_an_exception_soft_removes_and_audits(db, me, doc, s):
    off = await add_exception(db, s, ScheduleExceptionType.LEAVE, day=TODAY)
    await service.remove_exception(db, me, off.id)
    await db.refresh(off)
    assert off.removed_at is not None
    assert spans(await service.get_effective_availability(db, doc.id, TODAY, TODAY)) == [
        (TODAY, "09:00", "13:00")
    ]
    with pytest.raises(NotFound):  # removing twice looks like a foreign id
        await service.remove_exception(db, me, off.id)


async def test_ac10_foreign_or_unknown_exception_is_not_found(db, me, clinic):
    other_user = await make_user(
        db, email="other3@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id
    )
    other_slot = await add_slot(
        db, await affiliation_at(db, (await doctor_of(db, other_user)).id, clinic.id)
    )
    foreign = await add_exception(db, other_slot, ScheduleExceptionType.LEAVE)
    for exception_id in (foreign.id, uuid.uuid4()):
        with pytest.raises(NotFound):
            await service.remove_exception(db, me, exception_id)


async def test_ac17_time_off_over_several_slots_is_all_or_nothing(db, me, s, monkeypatch):
    monkeypatch.setattr(service, "MAX_UPCOMING_EXCEPTIONS", 1)
    await add_slot(
        db, (await db.get(DoctorAffiliation, s.affiliation_id)), start="14:00", end="15:00"
    )
    with pytest.raises(ListFull):
        await service.add_time_off(db, me, time_off())  # would write 2 rows, only 1 allowed
    assert (await db.execute(select(ScheduleException))).scalars().all() == []


async def test_ac18_the_201st_upcoming_exception_row_is_rejected(db, me, s, clinic):
    db.add_all(
        ScheduleException(schedule_id=s.id, exception_date=TODAY, kind=ScheduleExceptionType.LEAVE)
        for _ in range(200)
    )
    await db.commit()
    with pytest.raises(ListFull) as exc:
        await service.add_extra(db, me, extra_body(clinic.id, "14:00", "16:00"))
    assert exc.value.details == {"max": 200}


async def test_ac13_exception_audit_records_reason_length_never_the_text(db, me, s, clinic):
    await service.add_extra(
        db, me, extra_body(clinic.id, "14:00", "16:00", reason="Private family matter")
    )
    [detail] = await audit_rows(db, "schedule.exception.add")
    assert detail["reason_length"] == len("Private family matter")
    assert "Private family matter" not in json.dumps(detail)


async def test_ac33_a_blank_extra_cap_follows_the_anchor(db, me, doc, s, clinic):
    row = await service.add_extra(db, me, extra_body(clinic.id, "14:00", "16:00"))
    assert row.max_tokens is None
    await service.update_slot(db, me, s.id, update_body(clinic.id, max_tokens="30"))
    [_, extra] = await service.get_effective_availability(db, doc.id, TODAY, TODAY)
    assert (extra.max_tokens, extra.inherited) == (30, True)
    await db.refresh(row)
    assert row.max_tokens is None


async def test_ac12_every_exception_write_takes_the_lock_first(db, me, s, clinic, monkeypatch):
    calls: list[str] = []
    real = service.lock_doctor

    async def spy(session, doctor_id):
        calls.append("lock")
        await real(session, doctor_id)

    monkeypatch.setattr(service, "lock_doctor", spy)
    extra = await service.add_extra(db, me, extra_body(clinic.id, "14:00", "16:00"))
    [off] = await service.add_time_off(db, me, time_off(day=TODAY + timedelta(days=7)))
    await service.remove_exception(db, me, extra.id)
    await service.remove_exception(db, me, off.id)
    assert calls == ["lock"] * 4


# Spec §10.1, U1–U10, at service level (the engine-level versions are in
# tests/test_schedule_engine.py). d = TODAY, a Monday.
async def test_u1_service_extra_overlapping_its_anchor(db, me, s, clinic):
    with pytest.raises(ScheduleConflict) as exc:
        await service.add_extra(db, me, extra_body(clinic.id, "12:00", "14:00"))
    d = exc.value.details
    assert (d["clinic"], d["start"], d["end"], d["conflict_id"]) == (
        clinic.name,
        "09:00",
        "13:00",
        str(s.id),
    )


async def test_u2_service_back_to_back_after(db, me, s, clinic):
    await service.add_extra(db, me, extra_body(clinic.id, "13:00", "15:00"))


async def test_u3_service_back_to_back_before(db, me, s, clinic):
    await service.add_extra(db, me, extra_body(clinic.id, "07:00", "09:00"))


async def test_u4_service_extra_inside_the_anchor(db, me, s, clinic):
    with pytest.raises(ScheduleConflict):
        await service.add_extra(db, me, extra_body(clinic.id, "10:00", "11:00"))


async def test_u5_service_extra_fills_freed_time(db, me, s, clinic):
    await service.add_time_off(db, me, time_off(clinic_id=clinic.id, start="11:00", end="13:00"))
    await service.add_extra(db, me, extra_body(clinic.id, "11:00", "13:00"))


async def test_u6_service_removing_that_time_off_is_rejected(db, me, s, clinic):
    [off] = await service.add_time_off(
        db, me, time_off(clinic_id=clinic.id, start="11:00", end="13:00")
    )
    fill = await service.add_extra(db, me, extra_body(clinic.id, "11:00", "13:00"))
    with pytest.raises(ScheduleConflict) as exc:
        await service.remove_exception(db, me, off.id)
    assert exc.value.details["conflict_id"] == str(fill.id)  # AC-16


async def test_u7_service_second_extra_overlapping_the_first(db, me, s, clinic):
    first = await service.add_extra(db, me, extra_body(clinic.id, "13:00", "15:00"))
    with pytest.raises(ScheduleConflict) as exc:
        await service.add_extra(db, me, extra_body(clinic.id, "14:00", "16:00"))
    assert exc.value.details["conflict_id"] == str(first.id)


async def test_u8_service_editing_the_anchor_onto_its_extra(db, me, s, clinic):
    extra = await service.add_extra(db, me, extra_body(clinic.id, "13:00", "15:00"))
    with pytest.raises(ScheduleConflict) as exc:
        await service.update_slot(db, me, s.id, update_body(clinic.id, end="14:00"))
    assert exc.value.details["conflict_id"] == str(extra.id)


async def test_u9_service_availability_keeps_anchor_and_extra_apart(db, me, doc, s, clinic):
    extra = await service.add_extra(db, me, extra_body(clinic.id, "13:00", "15:00", max_tokens="4"))
    windows = await service.get_effective_availability(db, doc.id, TODAY, TODAY)
    assert spans(windows) == [(TODAY, "09:00", "13:00"), (TODAY, "13:00", "15:00")]
    assert [(w.exception_id, w.max_tokens) for w in windows] == [(None, 16), (extra.id, 4)]


async def test_u10_service_extra_at_another_clinic_overlapping_s(
    db, me, s, b_anchor, second_clinic
):
    with pytest.raises(ScheduleConflict) as exc:
        await service.add_extra(db, me, extra_body(second_clinic.id, "12:00", "14:00"))
    assert exc.value.details["conflict_id"] == str(s.id)


# ── T16 audit: the last two gaps ─────────────────────────────────────────
async def test_ac4_extra_at_an_inactive_clinic_is_rejected(db, me, doc, inactive_clinic):
    closed = await add_affiliation(db, doc.id, inactive_clinic.id)
    await add_slot(db, closed)  # a slot exists there, but the clinic is inactive
    with pytest.raises(ValidationFailed) as exc:
        await service.add_extra(db, me, extra_body(inactive_clinic.id, "14:00", "16:00"))
    assert exc.value.details["fields"] == {"clinic_id": "errors.schedule_clinic_not_affiliated"}


# AC-12, truly concurrent: Postgres only. It creates and DROPS every table in
# the database it is given, so it runs only against a throwaway local test
# database named in D7_TEST_POSTGRES_URL — never the shared Supabase DB.
PG_URL = os.environ.get("D7_TEST_POSTGRES_URL", "")


def _safe_pg_url(url: str) -> bool:
    database = url.rsplit("/", 1)[-1].split("?", 1)[0]
    return (
        url.startswith("postgresql+asyncpg://")
        and "supabase" not in url.lower()
        and "test" in database.lower()
    )


@pytest.mark.skipif(not PG_URL, reason="needs a throwaway local Postgres in D7_TEST_POSTGRES_URL")
async def test_ac12_concurrent_overlapping_adds_on_postgres():
    assert _safe_pg_url(PG_URL), "refusing: not a local *test* Postgres database"
    engine = create_async_engine(PG_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as s:
            user = Profile(
                id=dbtypes.uuid7(),
                email="pg.doctor@x.com",
                role=UserRole.DOCTOR,
                full_name="PG Doctor",
                is_synthetic=True,
            )
            clinic = Clinic(id=dbtypes.uuid7(), name="PG Clinic", type="clinic")
            s.add_all([user, clinic])
            await s.flush()
            doctor = Doctor(
                id=dbtypes.uuid7(), user_id=user.id, full_name="PG Doctor", pmdc_number="PG-1"
            )
            s.add(doctor)
            await s.flush()
            s.add(
                DoctorAffiliation(
                    id=dbtypes.uuid7(),
                    doctor_id=doctor.id,
                    clinic_id=clinic.id,
                    start_date=TODAY,
                    status="active",
                )
            )
            await s.commit()
        actor = Actor(user_id=user.id, role=UserRole.DOCTOR.value)

        async def attempt(start: str, end: str) -> str:
            async with factory() as s:
                try:
                    await service.add_slots(s, actor, slot_body(clinic.id, start=start, end=end))
                    return "ok"
                except ScheduleConflict:
                    return "conflict"

        results = await asyncio.gather(attempt("09:00", "13:00"), attempt("12:00", "14:00"))
        assert sorted(results) == ["conflict", "ok"]
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
