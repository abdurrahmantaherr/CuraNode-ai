"""P5 web: the confirm step, booking, and moving a booking."""

from __future__ import annotations

import uuid

import pytest
from app.db.models import (
    Appointment,
    ScheduleException,
    ScheduleExceptionType,
    UserRole,
    VerificationStatus,
)
from app.settings import settings
from sqlalchemy import select

from tests.booking_helpers import (
    MONDAY,
    TODAY,
    WED,
    add_appointment,
    add_session,
    at,
    freeze,
    login,
    make_patient,
    verified_doctor,
)
from tests.conftest import make_user

FIND = "/en/patient/doctors"
BOOK = "/en/patient/appointments"


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    freeze(monkeypatch)


@pytest.fixture
async def me(db):
    _, patient, _ = await make_patient(db)
    return patient


@pytest.fixture
async def patient_client(client, me):
    await login(client, "booking.pat@x.com")
    return client


def confirm_url(doctor, clinic, when: str, moving=None) -> str:
    extra = f"&moving={moving.id}" if moving is not None else ""
    return f"{FIND}/{doctor.id}/confirm?clinic={clinic.id}&at={when}{extra}"


def book_form(doctor, clinic, when: str) -> dict:
    return {"doctor_id": str(doctor.id), "clinic_id": str(clinic.id), "slot": when}


async def appointments(db) -> list[Appointment]:
    result = await db.execute(select(Appointment).execution_options(populate_existing=True))
    return list(result.scalars().all())


async def world(db, clinic, **session_kw):
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic, **session_kw)
    return doctor, session


# ── Guards ───────────────────────────────────────────────────────────────
async def test_anonymous_is_sent_to_sign_in(client, db, clinic):
    doctor, _ = await world(db, clinic)
    r = await client.get(confirm_url(doctor, clinic, "2026-10-14T10:00"))
    assert r.status_code == 303 and r.headers["location"].startswith("/en/login?next=/en/patient/")
    r = await client.post(BOOK, data=book_form(doctor, clinic, "2026-10-14T10:00"))
    assert r.status_code == 303 and r.headers["location"] == "/en/login"


@pytest.mark.parametrize(
    ("role", "area"), [(UserRole.DOCTOR, "doctor"), (UserRole.CLINIC_ADMIN, "admin")]
)
async def test_other_roles_go_to_their_own_area(client, db, clinic, role, area):
    doctor, _ = await world(db, clinic)
    await make_user(db, email="other@x.com", role=role, clinic_id=clinic.id)
    await login(client, "other@x.com")
    calls = [
        ("GET", confirm_url(doctor, clinic, "2026-10-14T10:00")),
        ("POST", BOOK),
        ("POST", f"{BOOK}/{uuid.uuid4()}/reschedule"),
    ]
    for method, path in calls:
        r = await client.request(method, path)
        assert r.status_code == 303 and r.headers["location"] == f"/en/{area}"
    assert await appointments(db) == []


# ── The confirm step ─────────────────────────────────────────────────────
async def test_the_confirm_page_shows_the_choice_and_posts_it(patient_client, db, clinic):
    doctor, _ = await world(db, clinic)
    r = await patient_client.get(confirm_url(doctor, clinic, "2026-10-14T10:00"))
    assert r.status_code == 200
    for text in ("Dr Booking", "Cardiology", "Shifa International", "Wednesday", "2026-10-14"):
        assert text in r.text
    assert ">10:00<" in r.text
    assert 'action="/en/patient/appointments"' in r.text
    assert f'name="doctor_id" value="{doctor.id}"' in r.text
    assert f'name="clinic_id" value="{clinic.id}"' in r.text
    assert 'name="slot" value="2026-10-14T10:00"' in r.text
    assert "Confirm booking" in r.text
    assert await appointments(db) == []  # looking books nothing


async def test_the_confirm_page_uses_pakistan_time_after_midnight(patient_client, db, clinic):
    doctor, _ = await world(db, clinic, weekday=3, start="00:00", end="01:00")
    r = await patient_client.get(confirm_url(doctor, clinic, "2026-10-15T00:15"))
    assert r.status_code == 200 and "Thursday" in r.text and "2026-10-15" in r.text
    assert 'name="slot" value="2026-10-15T00:15"' in r.text


async def test_malformed_parameters_go_back_to_the_doctor_page(patient_client, db, clinic):
    doctor, _ = await world(db, clinic)
    for query in ("", "?clinic=x&at=2026-10-14T10:00", f"?clinic={clinic.id}&at=junk"):
        r = await patient_client.get(f"{FIND}/{doctor.id}/confirm{query}")
        assert r.status_code == 303 and r.headers["location"] == f"/en/patient/doctors/{doctor.id}"


async def test_an_unknown_doctor_or_foreign_hospital_is_handled(
    patient_client, db, clinic, second_clinic
):
    doctor, _ = await world(db, clinic)
    r = await patient_client.get(
        f"{FIND}/{uuid.uuid4()}/confirm?clinic={clinic.id}&at=2026-10-14T10:00"
    )
    assert r.status_code == 404
    r = await patient_client.get(confirm_url(doctor, second_clinic, "2026-10-14T10:00"))
    assert r.status_code == 303 and r.headers["location"] == f"/en/patient/doctors/{doctor.id}"


@pytest.mark.parametrize("when", ["2026-10-14T10:07", "2026-10-14T13:00", "2026-10-15T10:00"])
async def test_a_time_that_is_not_offered_shows_the_doctor_page_again(
    patient_client, db, clinic, when
):
    doctor, _ = await world(db, clinic)
    r = await patient_client.get(confirm_url(doctor, clinic, when))
    assert r.status_code == 409 and "That time is not available" in r.text
    assert "at=2026-10-14T09:00" in r.text  # fresh times to pick from


async def test_a_taken_time_is_not_confirmable(patient_client, db, clinic):
    doctor, session = await world(db, clinic)
    _, other, _ = await make_patient(db, email="p2@x.com", name="Patient 2")
    await add_appointment(db, other, doctor, clinic, session, at(WED, "10:00"))
    r = await patient_client.get(confirm_url(doctor, clinic, "2026-10-14T10:00"))
    assert r.status_code == 409


async def test_the_confirm_page_for_a_move_posts_to_reschedule(patient_client, db, clinic, me):
    doctor, session = await world(db, clinic)
    mine = await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    r = await patient_client.get(confirm_url(doctor, clinic, "2026-10-14T11:00", moving=mine))
    assert r.status_code == 200
    assert f'action="/en/patient/appointments/{mine.id}/reschedule"' in r.text
    assert "Confirm new time" in r.text
    assert "This replaces your appointment on 2026-10-14 at 10:00." in r.text


async def test_a_moving_id_that_is_not_the_callers_is_ignored_on_confirm(
    patient_client, db, clinic
):
    doctor, session = await world(db, clinic)
    _, other, _ = await make_patient(db, email="p2@x.com", name="Patient 2")
    theirs = await add_appointment(db, other, doctor, clinic, session, at(WED, "10:00"))
    r = await patient_client.get(confirm_url(doctor, clinic, "2026-10-14T11:00", moving=theirs))
    assert r.status_code == 200
    assert 'action="/en/patient/appointments"' in r.text and "reschedule" not in r.text


# ── Booking ──────────────────────────────────────────────────────────────
async def test_booking_saves_and_redirects_to_the_confirmation(patient_client, db, clinic, me):
    doctor, session = await world(db, clinic)
    r = await patient_client.post(BOOK, data=book_form(doctor, clinic, "2026-10-14T10:00"))
    [saved] = await appointments(db)
    assert r.status_code == 303
    assert r.headers["location"] == f"/en/patient/appointments/{saved.id}?saved=booked"
    assert saved.patient_id == me.id and saved.doctor_id == doctor.id
    assert saved.schedule_id == session.id and saved.status == "booked"
    assert saved.booking_channel == "online"


async def test_booking_a_time_just_after_midnight_in_pakistan(patient_client, db, clinic):
    doctor, _ = await world(db, clinic, weekday=3, start="00:00", end="01:00")
    r = await patient_client.post(BOOK, data=book_form(doctor, clinic, "2026-10-15T00:15"))
    [saved] = await appointments(db)
    assert r.status_code == 303
    assert saved.slot_start.replace(tzinfo=None).isoformat() == "2026-10-14T19:15:00"


async def test_a_taken_time_shows_the_page_again_with_fresh_times(patient_client, db, clinic):
    doctor, session = await world(db, clinic)
    _, other, _ = await make_patient(db, email="p2@x.com", name="Patient 2")
    await add_appointment(db, other, doctor, clinic, session, at(WED, "10:00"))
    r = await patient_client.post(BOOK, data=book_form(doctor, clinic, "2026-10-14T10:00"))
    assert r.status_code == 409 and "Someone just booked that time" in r.text
    assert "at=2026-10-14T10:00" not in r.text and "at=2026-10-14T10:15" in r.text
    assert len(await appointments(db)) == 1


async def test_a_full_session_is_explained(patient_client, db, clinic):
    doctor, session = await world(db, clinic, cap=1)
    _, other, _ = await make_patient(db, email="p2@x.com", name="Patient 2")
    await add_appointment(db, other, doctor, clinic, session, at(WED, "09:00"))
    r = await patient_client.post(BOOK, data=book_form(doctor, clinic, "2026-10-14T10:00"))
    assert r.status_code == 409 and "That session is full" in r.text


async def test_two_with_one_doctor_in_a_day_is_explained(patient_client, db, clinic, me):
    doctor, session = await world(db, clinic)
    await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    r = await patient_client.post(BOOK, data=book_form(doctor, clinic, "2026-10-14T11:00"))
    assert r.status_code == 409 and "with this doctor that day" in r.text
    assert len(await appointments(db)) == 1


async def test_an_overlap_with_another_doctor_is_explained(patient_client, db, clinic, me):
    doctor_a, _ = await world(db, clinic)
    doctor_b = await verified_doctor(db, clinic, email="b.dr@x.com", name="Dr B")
    session_b = await add_session(db, doctor_b, clinic)
    await add_appointment(db, me, doctor_b, clinic, session_b, at(WED, "10:00"))
    r = await patient_client.post(BOOK, data=book_form(doctor_a, clinic, "2026-10-14T10:00"))
    assert r.status_code == 409 and "You already have an appointment at that time" in r.text


async def test_a_time_too_soon_is_explained(patient_client, db, clinic):
    doctor, _ = await world(db, clinic, weekday=MONDAY, start="12:00", end="18:00")
    r = await patient_client.post(BOOK, data=book_form(doctor, clinic, f"{TODAY}T13:15"))
    assert r.status_code == 409 and "too soon" in r.text


async def test_a_time_removed_after_the_page_loaded_is_refused(patient_client, db, clinic):
    # Review focus 3, through the page.
    doctor, session = await world(db, clinic)
    page = await patient_client.get(f"{FIND}/{doctor.id}")
    assert "at=2026-10-14T10:00" in page.text
    db.add(
        ScheduleException(
            schedule_id=session.id, exception_date=WED, kind=ScheduleExceptionType.LEAVE
        )
    )
    await db.commit()
    r = await patient_client.post(BOOK, data=book_form(doctor, clinic, "2026-10-14T10:00"))
    assert r.status_code == 409 and "That time is not available" in r.text
    assert await appointments(db) == []


async def test_a_doctor_unverified_after_the_page_loaded_cannot_be_booked(
    patient_client, db, clinic
):
    # Review focus 5, through the page.
    doctor, _ = await world(db, clinic)
    doctor.verification_status = VerificationStatus.REJECTED
    doctor.verified_by = None
    doctor.verified_at = None
    await db.commit()
    r = await patient_client.post(BOOK, data=book_form(doctor, clinic, "2026-10-14T10:00"))
    assert (
        r.status_code == 303 and r.headers["location"] == "/en/patient/doctors?notice=unavailable"
    )
    assert await appointments(db) == []


async def test_cancel_then_rebook_the_same_time_through_the_page(patient_client, db, clinic, me):
    # Review focus 5, second half.
    doctor, session = await world(db, clinic)
    await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"), status="cancelled")
    r = await patient_client.post(BOOK, data=book_form(doctor, clinic, "2026-10-14T10:00"))
    assert r.status_code == 303 and "saved=booked" in r.headers["location"]


async def test_tampered_forms_do_not_book(patient_client, db, clinic):
    doctor, _ = await world(db, clinic)
    r = await patient_client.post(BOOK, data={"doctor_id": "junk", "clinic_id": "", "slot": ""})
    assert r.status_code == 404
    r = await patient_client.post(
        BOOK, data={"doctor_id": str(doctor.id), "clinic_id": "junk", "slot": "2026-10-14T10:00"}
    )
    assert r.status_code == 303 and r.headers["location"] == f"/en/patient/doctors/{doctor.id}"
    r = await patient_client.post(BOOK, data={"doctor_id": str(doctor.id)})
    assert r.status_code == 303
    assert await appointments(db) == []


async def test_the_write_rate_limit_applies(patient_client, db, clinic, monkeypatch):
    monkeypatch.setattr(settings, "booking_write_rate_limit_per_minute", 1)
    doctor, _ = await world(db, clinic)
    first = await patient_client.post(BOOK, data=book_form(doctor, clinic, "2026-10-14T10:00"))
    second = await patient_client.post(BOOK, data=book_form(doctor, clinic, "2026-10-14T10:15"))
    assert first.status_code == 303
    assert second.status_code == 429 and "Too many attempts" in second.text
    assert len(await appointments(db)) == 1


async def test_names_are_escaped_on_the_confirm_page(patient_client, db, clinic):
    doctor, _ = await world(db, clinic)
    doctor.full_name = "<b>Dr</b>"
    await db.commit()
    r = await patient_client.get(confirm_url(doctor, clinic, "2026-10-14T10:00"))
    assert "<b>Dr</b>" not in r.text and "&lt;b&gt;Dr&lt;/b&gt;" in r.text


# ── Moving a booking ─────────────────────────────────────────────────────
async def move(client, appointment, clinic, when: str):
    return await client.post(
        f"{BOOK}/{appointment.id}/reschedule", data={"clinic_id": str(clinic.id), "slot": when}
    )


async def test_moving_books_the_new_time_and_cancels_the_old(patient_client, db, clinic, me):
    doctor, session = await world(db, clinic)
    old = await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    r = await move(patient_client, old, clinic, "2026-10-21T11:00")
    rows = {a.id: a for a in await appointments(db)}
    [new] = [a for a in rows.values() if a.id != old.id]
    assert r.status_code == 303
    assert r.headers["location"] == f"/en/patient/appointments/{new.id}?saved=rescheduled"
    assert rows[old.id].status == "cancelled" and new.status == "booked"


async def test_moving_to_another_time_with_the_same_doctor_the_same_day(
    patient_client, db, clinic, me
):
    # Review focus 1, through the page.
    doctor, session = await world(db, clinic)
    old = await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    r = await move(patient_client, old, clinic, "2026-10-14T11:00")
    assert r.status_code == 303 and "saved=rescheduled" in r.headers["location"]


async def test_a_refused_move_leaves_the_old_booking_and_stays_in_move_mode(
    patient_client, db, clinic, me
):
    doctor, session = await world(db, clinic)
    _, other, _ = await make_patient(db, email="p2@x.com", name="Patient 2")
    old = await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    await add_appointment(db, other, doctor, clinic, session, at(WED, "11:00"))
    r = await move(patient_client, old, clinic, "2026-10-14T11:00")
    assert r.status_code == 409 and "Someone just booked that time" in r.text
    assert f"moving={old.id}" in r.text  # still moving that booking
    assert {a.status for a in await appointments(db)} == {"booked"} and len(
        await appointments(db)
    ) == 2


async def test_moving_someone_elses_or_an_unknown_booking_goes_to_the_list(
    patient_client, db, clinic
):
    doctor, session = await world(db, clinic)
    _, other, _ = await make_patient(db, email="p2@x.com", name="Patient 2")
    theirs = await add_appointment(db, other, doctor, clinic, session, at(WED, "10:00"))
    r = await move(patient_client, theirs, clinic, "2026-10-14T11:00")
    assert r.status_code == 303 and r.headers["location"] == "/en/patient/appointments"
    r = await patient_client.post(
        f"{BOOK}/{uuid.uuid4()}/reschedule", data={"clinic_id": str(clinic.id), "slot": "x"}
    )
    assert r.status_code == 303 and r.headers["location"] == "/en/patient/appointments"
    r = await patient_client.post(f"{BOOK}/junk/reschedule", data={})
    assert r.status_code == 303 and r.headers["location"] == "/en/patient/appointments"
    assert {a.status for a in await appointments(db)} == {"booked"}


async def test_moving_a_started_booking_is_refused(patient_client, db, clinic, me):
    doctor, session = await world(db, clinic)
    started = await add_appointment(db, me, doctor, clinic, session, at(TODAY, "09:00"))
    r = await move(patient_client, started, clinic, "2026-10-14T11:00")
    assert r.status_code == 409 and "can't be changed any more" in r.text.replace("&#39;", "'")
    assert (await appointments(db))[0].status == "booked"


async def test_a_move_with_a_bad_time_goes_back_to_move_mode(patient_client, db, clinic, me):
    doctor, session = await world(db, clinic)
    old = await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    r = await move(patient_client, old, clinic, "junk")
    assert r.status_code == 303
    assert r.headers["location"] == f"/en/patient/doctors/{doctor.id}?moving={old.id}"
