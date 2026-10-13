"""P6 web: the doctor's view-only queue."""

from __future__ import annotations

import pytest
from app.db.models import UserRole

from tests.booking_helpers import add_session, freeze, login, make_patient, verified_doctor
from tests.conftest import make_user
from tests.queue_helpers import add_entry, booked_today, todays_session


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    freeze(monkeypatch)


@pytest.fixture
async def mine(client, db, clinic):
    doctor, schedule = await todays_session(db, clinic, email="queue.dr@x.com")
    await login(client, "queue.dr@x.com")
    return doctor, schedule


async def person(db, n: int, name: str | None = None):
    _, patient, _ = await make_patient(db, email=f"q{n}@x.com", name=name or f"Patient {n}")
    return patient


async def queue_of(db, clinic, doctor, schedule):
    """One patient in the room, two waiting (the second a walk-in), one expected."""
    room = await booked_today(db, clinic, doctor, schedule, await person(db, 1), "14:00")
    first = await booked_today(db, clinic, doctor, schedule, await person(db, 2), "14:15")
    walker = await booked_today(db, clinic, doctor, schedule, await person(db, 3), "13:07")
    walker.booking_channel = "walk_in"
    await db.commit()
    await booked_today(db, clinic, doctor, schedule, await person(db, 4), "15:30")
    await add_entry(db, room, 1, "in_room")
    await add_entry(db, first, 2)
    await add_entry(db, walker, 3)


async def test_the_dashboard_shows_the_queue_in_order(client, db, clinic, mine):
    doctor, schedule = mine
    await queue_of(db, clinic, doctor, schedule)
    r = await client.get("/en/doctor")
    assert r.status_code == 200 and "Queue today" in r.text
    queue = r.text[r.text.index("Queue today") :]  # leave out "Appointments today" above it
    assert queue.index("Patient 2") < queue.index("Patient 3")  # waiting, in token order
    assert "Patient 1" in queue and "Patient 4" in queue and "Walk-in" in queue
    assert "Shifa International" in queue and "12:00-18:00" in queue


async def test_a_pending_doctor_gets_no_queue_panel(client, db, clinic):
    await make_user(db, email="free.dr@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    await login(client, "free.dr@x.com")
    r = await client.get("/en/doctor")
    assert r.status_code == 200 and "Queue today" not in r.text


async def test_a_verified_doctor_with_no_session_is_told_so(client, db, clinic):
    doctor = await verified_doctor(db, clinic, email="wed.dr@x.com", name="Dr Wed")
    await add_session(db, doctor, clinic, weekday=2)  # Wednesdays only
    await login(client, "wed.dr@x.com")
    r = await client.get("/en/doctor")
    assert "Queue today" in r.text and "No sessions today." in r.text


async def test_the_appointments_page_shows_the_queue_for_today_only(client, db, clinic, mine):
    doctor, schedule = mine
    await queue_of(db, clinic, doctor, schedule)
    today = await client.get("/en/doctor/appointments")
    assert "Queue today" in today.text and "Patient 3" in today.text
    other_day = await client.get("/en/doctor/appointments?date=2026-10-14")
    assert "Queue today" not in other_day.text


async def test_another_doctors_patients_are_not_in_my_queue(client, db, clinic, mine):
    _doctor, _schedule = mine
    rival, rival_schedule = await todays_session(db, clinic, email="r.dr@x.com", name="Dr Rival")
    entry = await booked_today(
        db, clinic, rival, rival_schedule, await person(db, 9, "Secret Person")
    )
    await add_entry(db, entry, 1)
    dashboard = (await client.get("/en/doctor")).text
    day_page = (await client.get("/en/doctor/appointments")).text
    for page in (dashboard, day_page):
        assert "Queue today" in page and "Nobody is waiting." in page  # my own, empty queue
        assert "Secret Person" not in page


async def test_no_buttons_and_no_passport_numbers_for_the_doctor(client, db, clinic, mine):
    doctor, schedule = mine
    await queue_of(db, clinic, doctor, schedule)
    patient = await person(db, 1)
    page = (await client.get("/en/doctor")).text
    assert "Queue today" in page and "Patient 2" in page  # the queue is there to look at
    for word in ("Send in", "Mark seen", "Check in", "Move up", "Make next", "Put back"):
        assert word not in page
    assert patient.passport_no not in page


async def test_names_are_escaped_in_the_doctors_queue(client, db, clinic, mine):
    doctor, schedule = mine
    entry = await booked_today(db, clinic, doctor, schedule, await person(db, 1, "<i>Pat</i>"))
    await add_entry(db, entry, 1)
    page = (await client.get("/en/doctor")).text
    assert "<i>Pat</i>" not in page
    # once in "Appointments today" and once in the queue panel
    assert page.count("&lt;i&gt;Pat&lt;/i&gt;") >= 2


async def test_the_urdu_queue_is_right_to_left(client, db, clinic, mine):
    doctor, schedule = mine
    await queue_of(db, clinic, doctor, schedule)
    r = await client.get("/ur/doctor")
    assert r.status_code == 200 and 'dir="rtl"' in r.text and "آج کی قطار" in r.text
