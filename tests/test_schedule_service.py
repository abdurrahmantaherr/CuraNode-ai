"""D7 schedule service on SQLite (T6b core; T7 slots and T8 exceptions follow).

"Today" is frozen at Monday 2026-10-12 through `app.db.types.today_pk`, the
way the code reads it. Rows are inserted through the models where a test only
needs state; writes under test go through the service.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, time, timedelta

import pytest
from app.db import types as dbtypes
from app.db.models import (
    Appointment,
    AuditLog,
    DoctorAffiliation,
    DoctorSchedule,
    Patient,
    ScheduleException,
    ScheduleExceptionType,
    UserRole,
)
from app.deps import Actor
from app.errors import ListFull, NotFound, ScheduleConflict, ValidationFailed
from app.schedule import service
from app.schedule.schemas import SlotCreate, SlotUpdate
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

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
