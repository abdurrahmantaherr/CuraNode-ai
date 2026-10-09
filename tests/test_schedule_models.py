"""D7 T2: `DoctorSchedule` and `ScheduleException` map onto the shared tables.

The test database is built from the models (`create_all`), so these prove the
models mirror every live CHECK plus D7's, and that `kind` is stored in the
real column `type`.
"""

from __future__ import annotations

from datetime import date, time

import pytest
import sqlalchemy as sa
from app.db.models import (
    DoctorAffiliation,
    DoctorSchedule,
    ScheduleException,
    ScheduleExceptionType,
    UserRole,
)
from sqlalchemy import select

from tests.conftest import make_user

DAY = date(2026, 10, 12)


@pytest.fixture
async def affiliation(db, clinic) -> DoctorAffiliation:
    user = await make_user(db, email="doc@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    await db.refresh(user, ["doctor"])
    return (
        await db.execute(
            select(DoctorAffiliation).where(DoctorAffiliation.doctor_id == user.doctor.id)
        )
    ).scalar_one()


def slot_for(affiliation, **overrides) -> DoctorSchedule:
    values = {
        "affiliation_id": affiliation.id,
        "weekday": 0,
        "start_time": time(9),
        "end_time": time(13),
        "slot_minutes": 15,
        "max_tokens": 16,
    }
    values.update(overrides)
    return DoctorSchedule(**values)


async def refused(db, row) -> None:
    db.add(row)
    with pytest.raises(sa.exc.IntegrityError):
        await db.flush()
    await db.rollback()


async def test_a_slot_round_trips_with_defaults(db, affiliation):
    slot = slot_for(affiliation)
    db.add(slot)
    await db.commit()

    loaded = await db.get(DoctorSchedule, slot.id)
    assert loaded is not None
    assert loaded.id.version == 7
    assert (loaded.status, loaded.start_time, loaded.end_time) == ("active", time(9), time(13))


@pytest.mark.parametrize(
    "overrides",
    [
        {"slot_minutes": 7},
        {"slot_minutes": 0},
        {"slot_minutes": 65},
        {"max_tokens": 0},
        {"weekday": 7},
        {"start_time": time(13), "end_time": time(9)},
    ],
)
async def test_slot_checks(db, affiliation, overrides):
    await refused(db, slot_for(affiliation, **overrides))


async def test_an_exception_stores_kind_in_the_type_column(db, affiliation):
    slot = slot_for(affiliation)
    db.add(slot)
    await db.flush()
    db.add(
        ScheduleException(
            schedule_id=slot.id,
            exception_date=DAY,
            kind=ScheduleExceptionType.EXTRA,
            start_time=time(13),
            end_time=time(15),
        )
    )
    await db.commit()

    stored = (await db.execute(sa.text("SELECT type, max_tokens FROM schedule_exception"))).one()
    assert tuple(stored) == ("extra", None)
    row = (await db.execute(select(ScheduleException))).scalar_one()
    assert row.kind is ScheduleExceptionType.EXTRA
    assert row.removed_at is None


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": ScheduleExceptionType.EXTRA},  # an extra needs times
        {"kind": ScheduleExceptionType.LEAVE, "start_time": time(10)},  # one time only
        {"kind": ScheduleExceptionType.LEAVE, "start_time": time(11), "end_time": time(10)},
        {"kind": ScheduleExceptionType.LEAVE, "max_tokens": 3},  # a cap on time off
        {
            "kind": ScheduleExceptionType.EXTRA,
            "start_time": time(13),
            "end_time": time(15),
            "max_tokens": 0,
        },
    ],
)
async def test_exception_checks(db, affiliation, fields):
    slot = slot_for(affiliation)
    db.add(slot)
    await db.commit()
    await refused(db, ScheduleException(schedule_id=slot.id, exception_date=DAY, **fields))
