"""D7 schedule service on SQLite (T6b core; T7 slots and T8 exceptions follow).

"Today" is frozen at Monday 2026-10-12 through `app.db.types.today_pk`, the
way the code reads it. Rows are inserted through the models where a test only
needs state; writes under test go through the service.
"""

from __future__ import annotations

import uuid
from datetime import date, time, timedelta

import pytest
from app.db import types as dbtypes
from app.db.models import (
    DoctorAffiliation,
    DoctorSchedule,
    ScheduleException,
    ScheduleExceptionType,
    UserRole,
)
from app.errors import ValidationFailed
from app.schedule import service
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
