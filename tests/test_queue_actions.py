"""P6: check in, send in, mark seen, put back and move forward."""

from __future__ import annotations

import json
import uuid
from datetime import date

import pytest
from app.db.models import Appointment, AuditLog, VerificationStatus
from app.deps import Actor
from app.errors import NotFound, QueueRefused
from app.queueing import service
from sqlalchemy import select

from tests.booking_helpers import (
    MONDAY,
    TODAY,
    add_appointment,
    add_session,
    at,
    freeze,
    make_patient,
    verified_doctor,
)
from tests.queue_helpers import add_entry, booked_today, entry_of, make_admin, todays_session


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    freeze(monkeypatch)


async def person(db, n: int):
    _, patient, _ = await make_patient(db, email=f"a{n}@x.com", name=f"Patient {n}")
    return patient


async def world(db, clinic):
    doctor, schedule = await todays_session(db, clinic)
    _, admin = await make_admin(db, clinic)
    return doctor, schedule, admin


async def reason_of(call) -> str:
    with pytest.raises(QueueRefused) as exc:
        await call
    return exc.value.reason


async def status_of(db, appointment) -> str:
    row = (
        await db.execute(
            select(Appointment)
            .where(Appointment.id == appointment.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return row.status


async def three_waiting(db, clinic, doctor, schedule):
    """Three patients waiting with tokens 1, 2, 3."""
    appointments = []
    for n in (1, 2, 3):
        appointment = await booked_today(
            db, clinic, doctor, schedule, await person(db, n), f"14:{n * 15:02d}"
        )
        await add_entry(db, appointment, n)
        appointments.append(appointment)
    return appointments


async def tokens(db, appointments) -> list[int | None]:
    return [(await entry_of(db, a)).token_number for a in appointments]


# ── Check in ─────────────────────────────────────────────────────────────
async def test_checking_in_queues_the_patient_and_marks_the_appointment(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    appointment = await booked_today(db, clinic, doctor, schedule, await person(db, 1))
    await service.check_in(db, admin, appointment.id)
    entry = await entry_of(db, appointment)
    assert (entry.state, entry.token_number) == ("waiting", 1)
    assert await status_of(db, appointment) == "checked_in"


async def test_tokens_follow_arrival_order_and_are_never_reused(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    first = await booked_today(db, clinic, doctor, schedule, await person(db, 1), "15:00")
    second = await booked_today(db, clinic, doctor, schedule, await person(db, 2), "14:00")
    third = await booked_today(db, clinic, doctor, schedule, await person(db, 3), "14:30")
    await service.check_in(db, admin, first.id)
    await service.check_in(db, admin, second.id)
    first_entry = await entry_of(db, first)
    await service.send_in(db, admin, first_entry.id)
    await service.mark_seen(db, admin, first_entry.id)  # token 1 is now served
    await service.check_in(db, admin, third.id)
    assert await tokens(db, [first, second, third]) == [1, 2, 3]  # 3, not a reused 1


async def test_checking_in_twice_does_nothing(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    appointment = await booked_today(db, clinic, doctor, schedule, await person(db, 1))
    await service.check_in(db, admin, appointment.id)
    await service.check_in(db, admin, appointment.id)
    entries = (await db.execute(select(AuditLog).where(AuditLog.action == "queue.check_in"))).all()
    assert (await entry_of(db, appointment)).token_number == 1 and len(entries) == 1


async def test_check_in_locks_the_doctor_before_it_reads_the_queue(db, clinic, monkeypatch):
    # Review focus 1: two staff acting at once must not read the same highest token.
    doctor, schedule, admin = await world(db, clinic)
    appointment = await booked_today(db, clinic, doctor, schedule, await person(db, 1))
    calls: list[str] = []
    real_lock = service.schedule_service.lock_doctor
    real_read = service._session_entries

    async def spy_lock(session, doctor_id):
        calls.append("lock")
        await real_lock(session, doctor_id)

    async def spy_read(*args, **kwargs):
        calls.append("read")
        return await real_read(*args, **kwargs)

    monkeypatch.setattr(service.schedule_service, "lock_doctor", spy_lock)
    monkeypatch.setattr(service, "_session_entries", spy_read)
    await service.check_in(db, admin, appointment.id)
    assert calls.index("lock") < calls.index("read")


async def test_a_cancelled_or_done_appointment_cannot_be_checked_in(db, clinic):
    # Review focus 4.
    doctor, schedule, admin = await world(db, clinic)
    cancelled = await booked_today(
        db, clinic, doctor, schedule, await person(db, 1), status="cancelled"
    )
    done = await booked_today(db, clinic, doctor, schedule, await person(db, 2), status="done")
    assert await reason_of(service.check_in(db, admin, cancelled.id)) == "not_booked"
    assert await reason_of(service.check_in(db, admin, done.id)) == "not_booked"
    assert await entry_of(db, cancelled) is None


async def test_tomorrows_appointment_is_not_for_today(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    tomorrow = await add_appointment(
        db, await person(db, 1), doctor, clinic, schedule, at(date(2026, 10, 13), "14:00")
    )
    assert await reason_of(service.check_in(db, admin, tomorrow.id)) == "not_today"


async def test_an_appointment_just_after_midnight_in_pakistan_is_today(db, clinic, monkeypatch):
    # Review focus 3: 00:10 Pakistan time is the previous day in UTC.
    freeze(monkeypatch, now=at(TODAY, "00:20"))
    doctor = await verified_doctor(db, clinic, email="night.dr@x.com", name="Dr Night")
    schedule = await add_session(db, doctor, clinic, weekday=MONDAY, start="00:00", end="01:00")
    _, admin = await make_admin(db, clinic)
    appointment = await add_appointment(
        db, await person(db, 1), doctor, clinic, schedule, at(TODAY, "00:10")
    )
    await service.check_in(db, admin, appointment.id)
    assert (await entry_of(db, appointment)).state == "waiting"


async def test_check_in_is_refused_once_the_session_has_ended(db, clinic, monkeypatch):
    doctor, schedule, admin = await world(db, clinic)
    appointment = await booked_today(db, clinic, doctor, schedule, await person(db, 1))
    freeze(monkeypatch, now=at(TODAY, "18:30"))
    assert await reason_of(service.check_in(db, admin, appointment.id)) == "session_over"


async def test_early_arrival_before_the_session_starts_is_fine(db, clinic, monkeypatch):
    doctor, schedule, admin = await world(db, clinic)
    appointment = await booked_today(db, clinic, doctor, schedule, await person(db, 1))
    freeze(monkeypatch, now=at(TODAY, "11:30"))
    await service.check_in(db, admin, appointment.id)
    assert (await entry_of(db, appointment)).state == "waiting"


async def test_a_doctor_who_is_no_longer_verified_cannot_receive_check_ins(db, clinic):
    # Review focus 4.
    doctor, schedule, admin = await world(db, clinic)
    appointment = await booked_today(db, clinic, doctor, schedule, await person(db, 1))
    doctor.verification_status = VerificationStatus.REJECTED
    doctor.verified_by = None
    doctor.verified_at = None
    await db.commit()
    assert await reason_of(service.check_in(db, admin, appointment.id)) == "doctor_unavailable"


async def test_an_appointment_with_no_schedule_has_no_session(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    appointment = await booked_today(db, clinic, doctor, schedule, await person(db, 1))
    appointment.schedule_id = None
    await db.commit()
    assert await reason_of(service.check_in(db, admin, appointment.id)) == "session_over"


async def test_another_clinics_admin_and_other_roles_get_not_found(db, clinic, second_clinic):
    # Review focus 5.
    doctor, schedule, _ = await world(db, clinic)
    appointment = await booked_today(db, clinic, doctor, schedule, await person(db, 1))
    _, outsider = await make_admin(db, second_clinic, email="out.admin@x.com")
    with pytest.raises(NotFound):
        await service.check_in(db, outsider, appointment.id)
    with pytest.raises(NotFound):
        await service.check_in(db, Actor(user_id=uuid.uuid4(), role="patient"), appointment.id)
    with pytest.raises(NotFound):
        await service.check_in(db, outsider, uuid.uuid4())
    assert await entry_of(db, appointment) is None


async def test_a_check_in_writes_one_audit_row_with_ids_only(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    appointment = await booked_today(db, clinic, doctor, schedule, await person(db, 1))
    await service.check_in(db, admin, appointment.id)
    rows = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "queue.check_in")))
        .scalars()
        .all()
    )
    entry = await entry_of(db, appointment)
    assert len(rows) == 1
    assert json.loads(rows[0].detail) == {
        "appointment_id": str(appointment.id),
        "doctor_id": str(doctor.id),
        "clinic_id": str(clinic.id),
        "entry_id": str(entry.id),
    }
    assert rows[0].actor_role == "admin"


# ── Send in, mark seen, put back ─────────────────────────────────────────
async def test_send_in_moves_a_waiting_patient_into_the_room(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    [first, *_] = await three_waiting(db, clinic, doctor, schedule)
    await service.send_in(db, admin, (await entry_of(db, first)).id)
    assert (await entry_of(db, first)).state == "in_room"


async def test_any_waiting_patient_can_be_sent_in_not_only_the_first(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    [_, second, _] = await three_waiting(db, clinic, doctor, schedule)
    await service.send_in(db, admin, (await entry_of(db, second)).id)
    assert (await entry_of(db, second)).state == "in_room"


async def test_only_one_patient_can_be_in_the_room(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    [first, second, _] = await three_waiting(db, clinic, doctor, schedule)
    await service.send_in(db, admin, (await entry_of(db, first)).id)
    reason = await reason_of(service.send_in(db, admin, (await entry_of(db, second)).id))
    assert reason == "room_occupied" and (await entry_of(db, second)).state == "waiting"


async def test_a_second_click_on_the_same_patient_is_not_waiting_any_more(db, clinic):
    # Review focus 1: two staff click "send in" for the same patient.
    doctor, schedule, admin = await world(db, clinic)
    [first, *_] = await three_waiting(db, clinic, doctor, schedule)
    entry_id = (await entry_of(db, first)).id
    await service.send_in(db, admin, entry_id)
    assert await reason_of(service.send_in(db, admin, entry_id)) == "not_waiting"


async def test_mark_seen_finishes_the_visit(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    [first, *_] = await three_waiting(db, clinic, doctor, schedule)
    entry_id = (await entry_of(db, first)).id
    await service.send_in(db, admin, entry_id)
    await service.mark_seen(db, admin, entry_id)
    assert (await entry_of(db, first)).state == "served"
    assert await status_of(db, first) == "done"


async def test_the_room_is_free_again_after_mark_seen(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    [first, second, _] = await three_waiting(db, clinic, doctor, schedule)
    first_id, second_id = (await entry_of(db, first)).id, (await entry_of(db, second)).id
    await service.send_in(db, admin, first_id)
    await service.mark_seen(db, admin, first_id)
    await service.send_in(db, admin, second_id)
    assert (await entry_of(db, second)).state == "in_room"


async def test_only_someone_in_the_room_can_be_marked_seen_or_put_back(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    [first, *_] = await three_waiting(db, clinic, doctor, schedule)
    entry_id = (await entry_of(db, first)).id
    assert await reason_of(service.mark_seen(db, admin, entry_id)) == "not_in_room"
    assert await reason_of(service.put_back(db, admin, entry_id)) == "not_in_room"
    await service.send_in(db, admin, entry_id)
    await service.mark_seen(db, admin, entry_id)
    assert await reason_of(service.mark_seen(db, admin, entry_id)) == "not_in_room"


async def test_put_back_keeps_the_place_and_the_appointment_status(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    [first, second, _] = await three_waiting(db, clinic, doctor, schedule)
    entry_id = (await entry_of(db, first)).id
    await service.send_in(db, admin, entry_id)
    await service.put_back(db, admin, entry_id)
    entry = await entry_of(db, first)
    assert (entry.state, entry.token_number) == ("waiting", 1)
    assert await status_of(db, first) == "checked_in"
    assert (await entry_of(db, second)).token_number == 2


async def test_entries_of_another_clinic_or_another_day_cannot_be_acted_on(
    db, clinic, second_clinic
):
    doctor, schedule, admin = await world(db, clinic)
    [first, *_] = await three_waiting(db, clinic, doctor, schedule)
    _, outsider = await make_admin(db, second_clinic, email="out.admin@x.com")
    with pytest.raises(NotFound):
        await service.send_in(db, outsider, (await entry_of(db, first)).id)
    old = await add_appointment(
        db, await person(db, 9), doctor, clinic, schedule, at(date(2026, 10, 5), "14:00")
    )
    stale = await add_entry(db, old, 1)
    assert await reason_of(service.send_in(db, admin, stale.id)) == "not_today"


async def test_queue_actions_write_audit_rows_with_ids_only(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    [first, *_] = await three_waiting(db, clinic, doctor, schedule)
    entry_id = (await entry_of(db, first)).id
    await service.send_in(db, admin, entry_id)
    await service.put_back(db, admin, entry_id)
    await service.send_in(db, admin, entry_id)
    await service.mark_seen(db, admin, entry_id)
    actions = [
        row.action
        for row in (await db.execute(select(AuditLog))).scalars()
        if row.action.startswith("queue.")
    ]
    assert sorted(actions) == ["queue.put_back", "queue.seen", "queue.send_in", "queue.send_in"]
    for row in (await db.execute(select(AuditLog))).scalars():
        if row.action.startswith("queue."):
            assert set(json.loads(row.detail)) == {
                "appointment_id",
                "doctor_id",
                "clinic_id",
                "entry_id",
            }


# ── Move forward ─────────────────────────────────────────────────────────
async def test_move_up_swaps_with_the_patient_ahead(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    appointments = await three_waiting(db, clinic, doctor, schedule)
    await service.move_up(db, admin, (await entry_of(db, appointments[2])).id)
    assert await tokens(db, appointments) == [1, 3, 2]


async def test_moving_up_the_first_in_line_changes_nothing_and_writes_nothing(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    appointments = await three_waiting(db, clinic, doctor, schedule)
    await service.move_up(db, admin, (await entry_of(db, appointments[0])).id)
    assert await tokens(db, appointments) == [1, 2, 3]
    rows = (await db.execute(select(AuditLog).where(AuditLog.action == "queue.move"))).all()
    assert rows == []


async def test_make_next_puts_the_patient_first(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    appointments = await three_waiting(db, clinic, doctor, schedule)
    await service.make_next(db, admin, (await entry_of(db, appointments[2])).id)
    assert await tokens(db, appointments) == [2, 3, 1]


async def test_a_move_keeps_tokens_unique_and_leaves_the_room_and_the_seen_alone(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    waiting = await three_waiting(db, clinic, doctor, schedule)
    in_room = await booked_today(db, clinic, doctor, schedule, await person(db, 7), "15:30")
    seen = await booked_today(db, clinic, doctor, schedule, await person(db, 8), "15:45")
    await add_entry(db, in_room, 4, "in_room")
    await add_entry(db, seen, 5, "served")
    await service.make_next(db, admin, (await entry_of(db, waiting[2])).id)
    assert await tokens(db, waiting) == [2, 3, 1]
    assert (await entry_of(db, in_room)).token_number == 4
    assert (await entry_of(db, seen)).token_number == 5
    everyone = await tokens(db, [*waiting, in_room, seen])
    assert len(set(everyone)) == 5


async def test_only_a_waiting_patient_can_be_moved(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    [first, *_] = await three_waiting(db, clinic, doctor, schedule)
    entry_id = (await entry_of(db, first)).id
    await service.send_in(db, admin, entry_id)
    assert await reason_of(service.move_up(db, admin, entry_id)) == "not_waiting"
    assert await reason_of(service.make_next(db, admin, entry_id)) == "not_waiting"


async def test_a_move_in_one_session_does_not_touch_another(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    other, other_schedule = await todays_session(db, clinic, email="o.dr@x.com", name="Dr Other")
    mine = await three_waiting(db, clinic, doctor, schedule)
    theirs = await booked_today(db, clinic, other, other_schedule, await person(db, 9), "14:00")
    await add_entry(db, theirs, 1)
    await service.make_next(db, admin, (await entry_of(db, mine[2])).id)
    assert (await entry_of(db, theirs)).token_number == 1
