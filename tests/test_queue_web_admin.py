"""P6 web: the clinic admin's queue page and its actions."""

from __future__ import annotations

import uuid

import pytest
from app.db.models import Appointment, Patient, UserRole
from app.queueing.schemas import SessionKey
from app.settings import settings
from sqlalchemy import select

from tests.booking_helpers import freeze, login, make_patient
from tests.conftest import make_user
from tests.queue_helpers import add_entry, booked_today, entry_of, make_admin, todays_session

PAGE = "/en/admin/queue"


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    freeze(monkeypatch)


@pytest.fixture
async def desk(client, db, clinic):
    """The admin signed in, with a doctor who has a session today."""
    doctor, schedule = await todays_session(db, clinic)
    await make_admin(db, clinic)
    await login(client, "queue.admin@x.com")
    return doctor, schedule


async def patient(db, n: int, name: str | None = None):
    _, row, _ = await make_patient(db, email=f"d{n}@x.com", name=name or f"Patient {n}")
    return row


async def in_queue(db, clinic, doctor, schedule, n: int, token: int, state="waiting", name=None):
    """A booked patient number n, checked in with `token` in `state`."""
    appointment = await booked_today(
        db, clinic, doctor, schedule, await patient(db, n, name), f"14:{n * 5:02d}"
    )
    await add_entry(db, appointment, token, state)
    return appointment


def key_of(doctor, clinic, schedule) -> str:
    return SessionKey(doctor.id, clinic.id, schedule.id, schedule.start_time).encode()


def text_of(response) -> str:
    return response.text.replace("&#39;", "'")


# ── Guards ───────────────────────────────────────────────────────────────
async def test_anonymous_goes_to_login(client):
    r = await client.get(PAGE)
    assert r.status_code == 303 and r.headers["location"] == "/en/login?next=/en/admin/queue"


@pytest.mark.parametrize(
    ("role", "area"), [(UserRole.PATIENT, "patient"), (UserRole.DOCTOR, "doctor")]
)
async def test_other_roles_go_to_their_own_area(client, db, clinic, role, area):
    await make_user(db, email="other@x.com", role=role, clinic_id=clinic.id)
    await login(client, "other@x.com")
    some = uuid.uuid4()
    calls = [
        ("GET", PAGE),
        ("POST", f"{PAGE}/check-in/{some}"),
        ("POST", f"{PAGE}/{some}/send-in"),
        ("POST", f"{PAGE}/{some}/seen"),
        ("POST", f"{PAGE}/walk-in"),
        ("POST", f"{PAGE}/find"),
    ]
    for method, path in calls:
        r = await client.request(method, path)
        assert r.status_code == 303 and r.headers["location"] == f"/en/{area}"


async def test_an_admin_with_no_clinic_goes_to_their_dashboard(client, db):
    await make_user(db, email="lonely@x.com", role=UserRole.CLINIC_ADMIN)
    await login(client, "lonely@x.com")
    r = await client.get(PAGE)
    assert r.status_code == 303 and r.headers["location"] == "/en/admin"


# ── The page ─────────────────────────────────────────────────────────────
async def test_the_page_lists_a_session_and_each_patient_in_the_right_group(
    client, db, clinic, desk
):
    doctor, schedule = desk
    await in_queue(db, clinic, doctor, schedule, 1, 1, "in_room")
    await in_queue(db, clinic, doctor, schedule, 2, 2, "waiting")
    await in_queue(db, clinic, doctor, schedule, 3, 3, "served")
    await booked_today(db, clinic, doctor, schedule, await patient(db, 4), "15:00")
    r = await client.get(PAGE)
    assert r.status_code == 200
    for text in ("Dr Queue", "Shifa International", "12:00-18:00", "Places used: 4 of 16"):
        assert text in r.text
    for name in ("Patient 1", "Patient 2", "Patient 3", "Patient 4"):
        assert name in r.text
    for button in ("Mark seen", "Put back", "Send in", "Move up", "Make next", "Check in"):
        assert button in r.text


async def test_the_page_has_a_queue_item_in_the_admin_menu(client, desk):
    assert 'href="/en/admin/queue"' in (await client.get(PAGE)).text
    assert 'href="/en/admin/queue"' in (await client.get("/en/admin")).text


async def test_an_admin_with_no_sessions_today_is_told_so(client, db, clinic):
    await make_admin(db, clinic)
    await login(client, "queue.admin@x.com")
    r = await client.get(PAGE)
    assert r.status_code == 200 and "No doctor sessions today" in r.text


async def test_no_passport_number_is_on_the_page(client, db, clinic, desk):
    doctor, schedule = desk
    appointment = await in_queue(db, clinic, doctor, schedule, 1, 1)
    queued = await db.get(Patient, appointment.patient_id)
    r = await client.get(PAGE)
    assert "Patient 1" in r.text and queued.passport_no not in r.text


async def test_names_are_escaped(client, db, clinic, desk):
    doctor, schedule = desk
    await in_queue(db, clinic, doctor, schedule, 1, 1, name="<b>Pat</b>")
    r = await client.get(PAGE)
    assert "<b>Pat</b>" not in r.text and "&lt;b&gt;Pat&lt;/b&gt;" in r.text


async def test_the_urdu_page_is_right_to_left(client, db, clinic, desk):
    doctor, schedule = desk
    await in_queue(db, clinic, doctor, schedule, 1, 1)
    r = await client.get("/ur/admin/queue")
    assert r.status_code == 200 and 'dir="rtl"' in r.text and "قطار" in r.text
    assert 'href="/en/admin/queue"' in r.text  # the language switch


async def test_a_saved_flag_shows_a_banner_and_an_unknown_one_does_not(client, desk):
    assert "Checked in." in (await client.get(f"{PAGE}?saved=checked_in")).text
    assert "Checked in." not in (await client.get(f"{PAGE}?saved=bogus")).text


# ── Actions ──────────────────────────────────────────────────────────────
async def test_checking_in_redirects_with_a_notice_and_queues_the_patient(client, db, clinic, desk):
    doctor, schedule = desk
    appointment = await booked_today(db, clinic, doctor, schedule, await patient(db, 1))
    r = await client.post(f"{PAGE}/check-in/{appointment.id}")
    assert r.status_code == 303 and r.headers["location"] == f"{PAGE}?saved=checked_in"
    assert (await entry_of(db, appointment)).state == "waiting"


async def test_a_refused_check_in_shows_the_reason(client, db, clinic, desk):
    doctor, schedule = desk
    cancelled = await booked_today(
        db, clinic, doctor, schedule, await patient(db, 1), status="cancelled"
    )
    r = await client.post(f"{PAGE}/check-in/{cancelled.id}")
    assert r.status_code == 409 and "That appointment can't be checked in." in text_of(r)
    assert await entry_of(db, cancelled) is None


async def test_unknown_malformed_and_foreign_ids_go_back_to_the_queue(
    client, db, clinic, second_clinic, desk
):
    _doctor, _schedule = desk
    other, other_schedule = await todays_session(
        db, second_clinic, email="o.dr@x.com", name="Dr Other"
    )
    foreign = await booked_today(db, second_clinic, other, other_schedule, await patient(db, 1))
    for path in (
        f"{PAGE}/check-in/{uuid.uuid4()}",
        f"{PAGE}/check-in/junk",
        f"{PAGE}/check-in/{foreign.id}",
        f"{PAGE}/{uuid.uuid4()}/send-in",
        f"{PAGE}/junk/seen",
    ):
        r = await client.post(path)
        assert r.status_code == 303 and r.headers["location"] == PAGE
    assert await entry_of(db, foreign) is None


async def test_the_whole_visit_through_the_buttons(client, db, clinic, desk):
    doctor, schedule = desk
    first = await in_queue(db, clinic, doctor, schedule, 1, 1)
    second = await in_queue(db, clinic, doctor, schedule, 2, 2)
    one, two = (await entry_of(db, first)).id, (await entry_of(db, second)).id

    r = await client.post(f"{PAGE}/{one}/send-in")
    assert r.headers["location"] == f"{PAGE}?saved=sent_in"
    r = await client.post(f"{PAGE}/{two}/send-in")  # the room is taken
    assert r.status_code == 409 and "Someone is already in the room" in r.text
    r = await client.post(f"{PAGE}/{one}/put-back")
    assert r.headers["location"] == f"{PAGE}?saved=put_back"
    r = await client.post(f"{PAGE}/{two}/make-next")
    assert r.headers["location"] == f"{PAGE}?saved=moved"
    assert (await entry_of(db, second)).token_number == 1
    r = await client.post(f"{PAGE}/{one}/move-up")
    assert r.headers["location"] == f"{PAGE}?saved=moved"
    assert (await entry_of(db, first)).token_number == 1  # swapped back
    r = await client.post(f"{PAGE}/{one}/send-in")
    r = await client.post(f"{PAGE}/{one}/seen")
    assert r.headers["location"] == f"{PAGE}?saved=seen"
    assert (await entry_of(db, first)).state == "served"


async def test_another_clinics_admin_cannot_act_on_the_queue(
    client, db, clinic, second_clinic, desk
):
    doctor, schedule = desk
    waiting = await in_queue(db, clinic, doctor, schedule, 1, 1)
    await make_admin(db, second_clinic, email="out.admin@x.com")
    client.cookies.clear()
    await login(client, "out.admin@x.com")
    r = await client.post(f"{PAGE}/{(await entry_of(db, waiting)).id}/send-in")
    assert r.status_code == 303 and r.headers["location"] == PAGE
    assert (await entry_of(db, waiting)).state == "waiting"
    assert "Patient 1" not in (await client.get(PAGE)).text


async def test_the_desk_write_rate_limit_applies(client, db, clinic, desk, monkeypatch):
    monkeypatch.setattr(settings, "queue_write_rate_limit_per_minute", 1)
    doctor, schedule = desk
    first = await booked_today(db, clinic, doctor, schedule, await patient(db, 1), "14:00")
    second = await booked_today(db, clinic, doctor, schedule, await patient(db, 2), "14:15")
    assert (await client.post(f"{PAGE}/check-in/{first.id}")).status_code == 303
    r = await client.post(f"{PAGE}/check-in/{second.id}")
    assert r.status_code == 429 and "Too many attempts" in r.text
    assert await entry_of(db, second) is None


# ── Walk-ins ─────────────────────────────────────────────────────────────
async def test_a_name_search_shows_masked_results_for_that_session(client, db, clinic, desk):
    doctor, schedule = desk
    found = await patient(db, 1, "Ayesha Raza")
    key = key_of(doctor, clinic, schedule)
    r = await client.post(f"{PAGE}/find", data={"find": "ayesha", "session": key})
    assert r.status_code == 200 and "Ayesha Raza" in r.text and "Add to queue" in r.text
    assert found.passport_no not in r.text and found.passport_no[-4:] in r.text
    assert "••" in r.text


async def test_an_exact_passport_number_finds_the_patient(client, db, clinic, desk):
    doctor, schedule = desk
    found = await patient(db, 1, "Ayesha Raza")
    key = key_of(doctor, clinic, schedule)
    r = await client.post(f"{PAGE}/find", data={"find": found.passport_no, "session": key})
    assert "Ayesha Raza" in r.text and "••" in r.text
    # the number appears once, in the search box where it was typed, never in a result
    assert r.text.count(found.passport_no) == 1


async def test_a_short_or_empty_search_says_so(client, db, clinic, desk):
    doctor, schedule = desk
    key = key_of(doctor, clinic, schedule)
    r = await client.post(f"{PAGE}/find", data={"find": "ab", "session": key})
    assert "Type at least 3 characters." in r.text
    r = await client.post(f"{PAGE}/find", data={"find": "zzzzzz", "session": key})
    assert "No patient found." in r.text


async def test_the_lookup_rate_limit_shows_a_message_not_an_error(
    client, db, clinic, desk, monkeypatch
):
    monkeypatch.setattr(settings, "queue_lookup_rate_limit_per_minute", 1)
    doctor, schedule = desk
    await patient(db, 1, "Ayesha Raza")
    key = key_of(doctor, clinic, schedule)
    assert (
        "Ayesha Raza"
        in (await client.post(f"{PAGE}/find", data={"find": "ayesha", "session": key})).text
    )
    r = await client.post(f"{PAGE}/find", data={"find": "ayesha", "session": key})
    assert r.status_code == 200 and "Too many attempts" in r.text


async def test_adding_a_walk_in_queues_them_and_shows_them_in_the_list(client, db, clinic, desk):
    doctor, schedule = desk
    walker = await patient(db, 1, "Ayesha Raza")
    key = key_of(doctor, clinic, schedule)
    r = await client.post(f"{PAGE}/walk-in", data={"session": key, "patient_id": str(walker.id)})
    assert r.status_code == 303 and r.headers["location"] == f"{PAGE}?saved=walk_in"
    appointment = (
        await db.execute(select(Appointment).where(Appointment.patient_id == walker.id))
    ).scalar_one()
    assert appointment.booking_channel == "walk_in"
    page = await client.get(r.headers["location"])
    assert "Ayesha Raza" in page.text and "Walk-in" in page.text
    assert "Walk-in added to the queue." in page.text


async def test_a_walk_in_for_a_full_session_shows_the_reason(client, db, clinic):
    doctor, schedule = await todays_session(db, clinic, cap=1)
    await make_admin(db, clinic)
    await login(client, "queue.admin@x.com")
    await booked_today(db, clinic, doctor, schedule, await patient(db, 1), "14:00")
    walker = await patient(db, 2)
    key = key_of(doctor, clinic, schedule)
    r = await client.post(f"{PAGE}/walk-in", data={"session": key, "patient_id": str(walker.id)})
    assert r.status_code == 409 and "That session is full." in r.text


async def test_a_tampered_walk_in_form_creates_nothing(client, db, clinic, desk):
    doctor, schedule = desk
    walker = await patient(db, 1)
    key = key_of(doctor, clinic, schedule)
    for data in (
        {"session": "junk", "patient_id": str(walker.id)},
        {"session": key, "patient_id": "junk"},
        {"session": key},
        {},
    ):
        r = await client.post(f"{PAGE}/walk-in", data=data)
        assert r.status_code == 303 and r.headers["location"] == PAGE
    assert (await db.execute(select(Appointment))).all() == []


async def test_the_search_is_a_post_so_the_typed_text_never_reaches_a_url(client, db, clinic, desk):
    # Final review: a GET search put passport numbers and names into URLs and access logs.
    doctor, schedule = desk
    found = await patient(db, 1, "Ayesha Raza")
    page = await client.get(PAGE)
    assert 'method="post" action="/en/admin/queue/find' in page.text
    assert 'method="get"' not in page.text
    r = await client.post(
        f"{PAGE}/find",
        data={"find": found.passport_no, "session": key_of(doctor, clinic, schedule)},
    )
    assert r.status_code == 200 and "Ayesha Raza" in r.text
    assert "find=" not in str(r.url) and found.passport_no not in str(r.url)


async def test_a_search_in_the_address_bar_is_ignored(client, db, clinic, desk):
    doctor, schedule = desk
    await patient(db, 1, "Ayesha Raza")
    key = key_of(doctor, clinic, schedule)
    r = await client.get(PAGE, params={"find": "ayesha", "session": key})
    assert r.status_code == 200 and "Ayesha Raza" not in r.text
