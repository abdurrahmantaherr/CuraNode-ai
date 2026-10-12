"""P5: the database refuses two active appointments at one time (the backstop
behind the per-doctor lock). The table has no other uniqueness."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from tests.booking_helpers import (
    WED,
    add_appointment,
    add_session,
    at,
    make_patient,
    verified_doctor,
)


async def setup(db, clinic):
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic)
    _, first, _ = await make_patient(db, email="a@x.com", name="A")
    _, second, _ = await make_patient(db, email="b@x.com", name="B")
    return doctor, session, first, second


async def test_two_booked_appointments_at_one_time_are_refused(db, clinic):
    doctor, session, first, second = await setup(db, clinic)
    await add_appointment(db, first, doctor, clinic, session, at(WED, "10:00"))
    with pytest.raises(IntegrityError):
        await add_appointment(db, second, doctor, clinic, session, at(WED, "10:00"))


async def test_a_done_appointment_still_holds_its_time(db, clinic):
    doctor, session, first, second = await setup(db, clinic)
    await add_appointment(db, first, doctor, clinic, session, at(WED, "10:00"), status="done")
    with pytest.raises(IntegrityError):
        await add_appointment(db, second, doctor, clinic, session, at(WED, "10:00"))


async def test_a_cancelled_appointment_frees_its_time(db, clinic):
    doctor, session, first, second = await setup(db, clinic)
    await add_appointment(db, first, doctor, clinic, session, at(WED, "10:00"), status="cancelled")
    await add_appointment(db, second, doctor, clinic, session, at(WED, "10:00"))


async def test_cancelled_rows_may_repeat_a_time(db, clinic):
    doctor, session, first, second = await setup(db, clinic)
    await add_appointment(db, first, doctor, clinic, session, at(WED, "10:00"), status="cancelled")
    await add_appointment(db, second, doctor, clinic, session, at(WED, "10:00"), status="cancelled")


async def test_different_times_do_not_clash(db, clinic):
    doctor, session, first, second = await setup(db, clinic)
    await add_appointment(db, first, doctor, clinic, session, at(WED, "10:00"))
    await add_appointment(db, second, doctor, clinic, session, at(WED, "10:15"))
