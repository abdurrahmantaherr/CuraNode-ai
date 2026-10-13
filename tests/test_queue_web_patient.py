"""P6 web: the patient's queue card and the badge on the appointments list."""

from __future__ import annotations

from datetime import date

import pytest

from tests.booking_helpers import add_appointment, at, freeze, login, make_patient
from tests.queue_helpers import add_entry, booked_today, make_admin, todays_session

LIST = "/en/patient/appointments"


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    freeze(monkeypatch)


async def person(db, n: int, name: str | None = None):
    _, patient, actor = await make_patient(db, email=f"c{n}@x.com", name=name or f"Patient {n}")
    return patient, actor


@pytest.fixture
async def room(db, clinic):
    doctor, schedule = await todays_session(db, clinic)
    await make_admin(db, clinic)
    return doctor, schedule


async def sign_in(client, n: int):
    await login(client, f"c{n}@x.com")


def card(response) -> str:
    return response.text.replace("&#39;", "'")


async def test_a_booked_patient_who_has_not_arrived_is_told_to_tell_the_desk(
    client, db, clinic, room
):
    doctor, schedule = room
    patient, _ = await person(db, 1)
    appointment = await booked_today(db, clinic, doctor, schedule, patient)
    await sign_in(client, 1)
    r = await client.get(f"{LIST}/{appointment.id}")
    assert "Your place in the queue" in r.text
    assert "Not checked in yet. Tell the front desk when you arrive." in r.text
    assert 'data-autorefresh="15000"' in r.text and "/static/js/queue.js" in r.text


async def test_the_card_shows_position_and_wait_without_other_names(client, db, clinic, room):
    doctor, schedule = room
    people = [await person(db, n) for n in (1, 2, 3, 4)]
    appointments = [
        await booked_today(db, clinic, doctor, schedule, p, f"14:{n * 15:02d}")
        for n, (p, _) in enumerate(people)
    ]
    await add_entry(db, appointments[0], 1, "in_room")
    for token, appointment in enumerate(appointments[1:], start=2):
        await add_entry(db, appointment, token)
    await sign_in(client, 4)
    r = await client.get(f"{LIST}/{appointments[3].id}")
    assert "You are #3 in line, about 45 minutes." in r.text
    assert "People waiting: 3" in r.text
    for other in ("Patient 1", "Patient 2", "Patient 3"):
        assert other not in r.text
    assert "Last updated 13:00" in r.text and "Refresh" in r.text


async def test_the_next_patient_is_told_to_be_ready(client, db, clinic, room):
    doctor, schedule = room
    patient, _ = await person(db, 1)
    appointment = await booked_today(db, clinic, doctor, schedule, patient)
    await add_entry(db, appointment, 1)
    await sign_in(client, 1)
    assert "You're next. Please be ready." in card(await client.get(f"{LIST}/{appointment.id}"))


async def test_in_the_room_and_seen(client, db, clinic, room):
    doctor, schedule = room
    (p1, _), (p2, _) = await person(db, 1), await person(db, 2)
    a1 = await booked_today(db, clinic, doctor, schedule, p1, "14:00")
    a2 = await booked_today(db, clinic, doctor, schedule, p2, "14:15")
    await add_entry(db, a1, 1, "in_room")
    await add_entry(db, a2, 2, "served")
    await sign_in(client, 1)
    inside = await client.get(f"{LIST}/{a1.id}")
    assert "It's your turn. Please go in." in card(inside) and "data-autorefresh" in inside.text
    client.cookies.clear()
    await sign_in(client, 2)
    done = await client.get(f"{LIST}/{a2.id}")
    assert "Seen. Thank you." in done.text
    assert "data-autorefresh" not in done.text  # nothing left to wait for


async def test_there_is_no_card_for_another_day_or_a_cancelled_booking(client, db, clinic, room):
    doctor, schedule = room
    patient, _ = await person(db, 1)
    later = await add_appointment(
        db, patient, doctor, clinic, schedule, at(date(2026, 10, 19), "14:00")
    )
    gone = await booked_today(db, clinic, doctor, schedule, patient, "14:15", status="cancelled")
    await sign_in(client, 1)
    for appointment in (later, gone):
        r = await client.get(f"{LIST}/{appointment.id}")
        assert r.status_code == 200 and "Your place in the queue" not in r.text


async def test_the_card_is_in_urdu_too(client, db, clinic, room):
    doctor, schedule = room
    patient, _ = await person(db, 1)
    appointment = await booked_today(db, clinic, doctor, schedule, patient)
    await add_entry(db, appointment, 1)
    await sign_in(client, 1)
    r = await client.get(f"/ur/patient/appointments/{appointment.id}")
    assert r.status_code == 200 and 'dir="rtl"' in r.text and "قطار" in r.text
    assert "Your place in the queue" not in r.text


async def test_someone_elses_appointment_still_goes_to_the_list(client, db, clinic, room):
    doctor, schedule = room
    (p1, _), _ = await person(db, 1), await person(db, 2)
    appointment = await booked_today(db, clinic, doctor, schedule, p1)
    await sign_in(client, 2)
    r = await client.get(f"{LIST}/{appointment.id}")
    assert r.status_code == 303 and r.headers["location"] == LIST


async def test_the_list_shows_a_badge_for_todays_queued_rows_only(client, db, clinic, room):
    doctor, schedule = room
    patient, _ = await person(db, 1)
    a1 = await booked_today(db, clinic, doctor, schedule, patient, "14:00")
    await add_entry(db, a1, 1)
    await add_appointment(db, patient, doctor, clinic, schedule, at(date(2026, 10, 19), "14:00"))
    await sign_in(client, 1)
    r = await client.get(LIST)
    assert r.text.count("In queue #1") == 1


async def test_the_badge_for_someone_in_the_room(client, db, clinic, room):
    doctor, schedule = room
    patient, _ = await person(db, 1)
    appointment = await booked_today(db, clinic, doctor, schedule, patient)
    await add_entry(db, appointment, 1, "in_room")
    await sign_in(client, 1)
    assert "In the room" in (await client.get(LIST)).text
