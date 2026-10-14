"""P6: the QueueEntry model matches the shared queue_entry table."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.db.models import QueueEntry
from sqlalchemy.exc import IntegrityError

from tests.booking_helpers import (
    TODAY,
    add_appointment,
    add_session,
    at,
    make_patient,
    verified_doctor,
)

CONFIRMED = Path(__file__).parent / "fixtures" / "queue_entry_columns.json"


def test_the_model_has_exactly_the_columns_the_schema_owner_confirmed():
    confirmed = json.loads(CONFIRMED.read_text(encoding="utf8"))
    table = QueueEntry.__table__
    assert {c["name"] for c in confirmed} == {col.name for col in table.columns}
    for c in confirmed:
        assert table.columns[c["name"]].nullable == c["nullable"], c["name"]


def test_the_state_check_allows_the_three_live_states():
    checks = [str(c.sqltext) for c in QueueEntry.__table__.constraints if hasattr(c, "sqltext")]
    assert any("'waiting'" in c and "'in_room'" in c and "'served'" in c for c in checks)


async def appointment_for(db, clinic, email="q1@x.com"):
    doctor = await verified_doctor(db, clinic, email=email)
    schedule = await add_session(db, doctor, clinic, weekday=0, start="12:00", end="18:00")
    _, patient, _ = await make_patient(db, email=f"pat.{email}")
    return await add_appointment(db, patient, doctor, clinic, schedule, at(TODAY, "14:00"))


async def test_an_entry_can_be_saved_and_read_back(db, clinic):
    appointment = await appointment_for(db, clinic)
    db.add(QueueEntry(appointment_id=appointment.id, token_number=1, state="waiting"))
    await db.commit()
    saved = (await db.execute(QueueEntry.__table__.select())).one()
    assert saved.token_number == 1 and saved.state == "waiting"


async def test_one_entry_per_appointment(db, clinic):
    appointment = await appointment_for(db, clinic)
    db.add(QueueEntry(appointment_id=appointment.id, token_number=1, state="waiting"))
    await db.commit()
    db.add(QueueEntry(appointment_id=appointment.id, token_number=2, state="waiting"))
    with pytest.raises(IntegrityError):
        await db.commit()


async def test_only_the_three_states_are_allowed(db, clinic):
    appointment = await appointment_for(db, clinic)
    db.add(QueueEntry(appointment_id=appointment.id, token_number=1, state="called"))
    with pytest.raises(IntegrityError):
        await db.commit()
