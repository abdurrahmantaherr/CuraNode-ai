"""P6: what the admin, the doctor and the patient see of a queue."""

from __future__ import annotations

import uuid
from datetime import date, time

import pytest
from app.deps import Actor
from app.errors import NotFound
from app.queueing import service
from app.queueing.schemas import SessionKey

from tests.booking_helpers import (
    add_appointment,
    add_session,
    at,
    freeze,
    make_patient,
    verified_doctor,
)
from tests.queue_helpers import add_entry, booked_today, make_admin, todays_session


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    freeze(monkeypatch)


async def person(db, n: int):
    """(patient row, actor) for patient number n."""
    _, patient, actor = await make_patient(db, email=f"v{n}@x.com", name=f"Patient {n}")
    return patient, actor


async def world(db, clinic):
    doctor, schedule = await todays_session(db, clinic)
    _, admin = await make_admin(db, clinic)
    return doctor, schedule, admin


def doctor_actor(doctor) -> Actor:
    return Actor(user_id=doctor.user_id, role="doctor", is_verified_doctor=True)


# ── The admin's view ─────────────────────────────────────────────────────
async def test_the_admin_sees_each_patient_in_the_right_group(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    (p1, _), (p2, _), (p3, _), (p4, _) = [await person(db, n) for n in (1, 2, 3, 4)]
    await booked_today(db, clinic, doctor, schedule, p1, "14:00")  # expected
    a2 = await booked_today(db, clinic, doctor, schedule, p2, "14:15")
    await add_entry(db, a2, 1, "waiting")
    a3 = await booked_today(db, clinic, doctor, schedule, p3, "14:30")
    await add_entry(db, a3, 2, "in_room")
    a4 = await booked_today(db, clinic, doctor, schedule, p4, "14:45")
    await add_entry(db, a4, 3, "served")
    [view] = await service.admin_sessions(db, admin)
    assert (view.doctor_name, view.clinic_name) == ("Dr Queue", "Shifa International")
    assert (view.start, view.end, view.slot_minutes, view.cap, view.used) == (
        time(12, 0),
        time(18, 0),
        15,
        16,
        4,
    )
    assert view.key == SessionKey(doctor.id, clinic.id, schedule.id, time(12, 0))
    assert [line.patient_name for line in view.expected] == ["Patient 1"]
    assert [line.patient_name for line in view.waiting] == ["Patient 2"]
    assert view.in_room is not None and view.in_room.patient_name == "Patient 3"
    assert [line.patient_name for line in view.seen] == ["Patient 4"]


async def test_the_waiting_list_follows_tokens_not_booked_times(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    (late, _), (early, _) = await person(db, 1), await person(db, 2)
    a_late = await booked_today(db, clinic, doctor, schedule, late, "15:00")
    a_early = await booked_today(db, clinic, doctor, schedule, early, "14:00")
    await add_entry(db, a_late, 1)  # arrived first, though booked later
    await add_entry(db, a_early, 2)
    [view] = await service.admin_sessions(db, admin)
    assert [line.patient_name for line in view.waiting] == ["Patient 1", "Patient 2"]
    assert [line.token for line in view.waiting] == [1, 2]


async def test_a_walk_in_is_flagged_and_counts_toward_the_places_used(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    (booked, _), (walker, _) = await person(db, 1), await person(db, 2)
    await booked_today(db, clinic, doctor, schedule, booked, "14:00")
    arrival = await booked_today(db, clinic, doctor, schedule, walker, "13:07")
    arrival.booking_channel = "walk_in"
    await db.commit()
    await add_entry(db, arrival, 1)
    [view] = await service.admin_sessions(db, admin)
    assert view.used == 2
    assert [(line.patient_name, line.walk_in) for line in view.waiting] == [("Patient 2", True)]
    assert [line.walk_in for line in view.expected] == [False]


async def test_cancelled_appointments_and_other_days_are_not_listed(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    (p1, _), (p2, _), (p3, _) = [await person(db, n) for n in (1, 2, 3)]
    await booked_today(db, clinic, doctor, schedule, p1, "14:00", status="cancelled")
    await add_appointment(
        db, p2, doctor, clinic, schedule, at(date(2026, 10, 19), "14:00")
    )  # next Monday
    await booked_today(db, clinic, doctor, schedule, p3, "14:15")
    [view] = await service.admin_sessions(db, admin)
    assert [line.patient_name for line in view.expected] == ["Patient 3"] and view.used == 1


async def test_the_admin_sees_only_their_own_clinics(db, clinic, second_clinic):
    await todays_session(db, clinic)
    await todays_session(db, second_clinic, email="other.dr@x.com", name="Dr Other")
    _, admin = await make_admin(db, clinic)
    views = await service.admin_sessions(db, admin)
    assert [(v.doctor_name, v.clinic_name) for v in views] == [("Dr Queue", "Shifa International")]


async def test_a_doctor_with_no_session_today_has_no_panel(db, clinic):
    _, _, admin = await world(db, clinic)
    quiet = await verified_doctor(db, clinic, email="quiet.dr@x.com", name="Dr Quiet")
    await add_session(db, quiet, clinic, weekday=2)  # Wednesdays only
    assert [v.doctor_name for v in await service.admin_sessions(db, admin)] == ["Dr Queue"]


async def test_only_an_admin_with_a_clinic_can_see_the_admin_view(db, clinic):
    await world(db, clinic)
    with pytest.raises(NotFound):
        await service.admin_sessions(db, Actor(user_id=uuid.uuid4(), role="patient"))
    with pytest.raises(NotFound):
        await service.admin_sessions(db, Actor(user_id=uuid.uuid4(), role="admin"))  # no clinics


# ── The doctor's view ────────────────────────────────────────────────────
async def test_the_doctor_sees_only_their_own_queue(db, clinic):
    doctor, schedule, _ = await world(db, clinic)
    rival, rival_schedule = await todays_session(db, clinic, email="r.dr@x.com", name="Dr Rival")
    (mine, _), (theirs, _) = await person(db, 1), await person(db, 2)
    a = await booked_today(db, clinic, doctor, schedule, mine, "14:00")
    await add_entry(db, a, 1)
    await booked_today(db, clinic, rival, rival_schedule, theirs, "14:15")
    [view] = await service.doctor_queue(db, doctor_actor(doctor))
    assert view.doctor_name == "Dr Queue"
    assert [line.patient_name for line in view.waiting] == ["Patient 1"]
    assert view.expected == ()


async def test_only_a_verified_doctor_can_see_a_doctors_queue(db, clinic):
    doctor, _, admin = await world(db, clinic)
    with pytest.raises(NotFound):
        await service.doctor_queue(db, admin)
    with pytest.raises(NotFound):
        await service.doctor_queue(
            db, Actor(user_id=doctor.user_id, role="doctor", is_verified_doctor=False)
        )


# ── The patient's view ───────────────────────────────────────────────────
async def test_a_booked_patient_who_has_not_arrived_is_expected(db, clinic):
    doctor, schedule, _ = await world(db, clinic)
    patient, actor = await person(db, 1)
    appointment = await booked_today(db, clinic, doctor, schedule, patient)
    status = await service.patient_status(db, actor, appointment.id)
    assert (status.state, status.position, status.wait_minutes, status.live) == (
        "expected",
        None,
        None,
        True,
    )


async def test_the_first_in_line_with_an_empty_room_is_next_with_no_wait(db, clinic):
    doctor, schedule, _ = await world(db, clinic)
    patient, actor = await person(db, 1)
    appointment = await booked_today(db, clinic, doctor, schedule, patient)
    await add_entry(db, appointment, 1)
    status = await service.patient_status(db, actor, appointment.id)
    assert (status.state, status.position, status.wait_minutes) == ("next", 1, 0)


async def test_position_and_wait_count_those_ahead_and_the_room(db, clinic):
    doctor, schedule, _ = await world(db, clinic)
    people = [await person(db, n) for n in (1, 2, 3, 4)]
    appointments = [
        await booked_today(db, clinic, doctor, schedule, p, f"14:{n * 15:02d}")
        for n, (p, _) in enumerate(people)
    ]
    await add_entry(db, appointments[0], 1, "in_room")
    await add_entry(db, appointments[1], 2)
    await add_entry(db, appointments[2], 3)
    await add_entry(db, appointments[3], 4)
    status = await service.patient_status(db, people[3][1], appointments[3].id)
    # two waiting ahead plus the one in the room, 15 minutes each
    assert (status.state, status.position, status.wait_minutes, status.waiting_total) == (
        "waiting",
        3,
        45,
        3,
    )


async def test_in_the_room_and_seen(db, clinic):
    doctor, schedule, _ = await world(db, clinic)
    (p1, actor1), (p2, actor2) = await person(db, 1), await person(db, 2)
    a1 = await booked_today(db, clinic, doctor, schedule, p1, "14:00")
    a2 = await booked_today(db, clinic, doctor, schedule, p2, "14:15")
    await add_entry(db, a1, 1, "in_room")
    await add_entry(db, a2, 2, "served")
    inside = await service.patient_status(db, actor1, a1.id)
    done = await service.patient_status(db, actor2, a2.id)
    assert (inside.state, inside.live) == ("in_room", True)
    assert (done.state, done.live) == ("served", False)


async def test_no_status_for_cancelled_or_other_days(db, clinic):
    doctor, schedule, _ = await world(db, clinic)
    patient, actor = await person(db, 1)
    cancelled = await booked_today(
        db, clinic, doctor, schedule, patient, "14:00", status="cancelled"
    )
    nextweek = await add_appointment(
        db, patient, doctor, clinic, schedule, at(date(2026, 10, 19), "14:00")
    )
    assert await service.patient_status(db, actor, cancelled.id) is None
    assert await service.patient_status(db, actor, nextweek.id) is None


async def test_someone_elses_or_unknown_appointment_is_not_found(db, clinic):
    doctor, schedule, _ = await world(db, clinic)
    (p1, _), (_, actor2) = await person(db, 1), await person(db, 2)
    appointment = await booked_today(db, clinic, doctor, schedule, p1)
    with pytest.raises(NotFound):
        await service.patient_status(db, actor2, appointment.id)
    with pytest.raises(NotFound):
        await service.patient_status(db, actor2, uuid.uuid4())


async def test_only_a_patient_has_a_status(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    patient, _ = await person(db, 1)
    appointment = await booked_today(db, clinic, doctor, schedule, patient)
    with pytest.raises(NotFound):
        await service.patient_status(db, admin, appointment.id)


async def test_badges_cover_only_todays_live_appointments(db, clinic):
    doctor, schedule, _ = await world(db, clinic)
    patient, actor = await person(db, 1)
    a1 = await booked_today(db, clinic, doctor, schedule, patient, "14:00")
    await add_entry(db, a1, 1)
    await add_appointment(db, patient, doctor, clinic, schedule, at(date(2026, 10, 19), "14:00"))
    await booked_today(db, clinic, doctor, schedule, patient, "14:15", status="cancelled")
    badges = await service.patient_badges(db, actor)
    assert list(badges) == [a1.id] and badges[a1.id].state == "next"
