"""P5 web: my appointments, the confirmation, cancel, and the reschedule entry."""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from app.db.models import Appointment, UserRole, VerificationStatus
from app.settings import settings
from sqlalchemy import select

from tests.booking_helpers import (
    THURSDAY,
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
LIST = "/en/patient/appointments"


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


async def world(db, clinic, **session_kw):
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic, **session_kw)
    return doctor, session


async def status_of(db, appointment) -> str:
    row = (
        await db.execute(
            select(Appointment)
            .where(Appointment.id == appointment.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return row.status


# ── Guards ───────────────────────────────────────────────────────────────
async def test_anonymous_is_sent_to_sign_in(client):
    r = await client.get(LIST)
    assert (
        r.status_code == 303 and r.headers["location"] == "/en/login?next=/en/patient/appointments"
    )
    r = await client.post(f"{LIST}/{uuid.uuid4()}/cancel")
    assert r.status_code == 303 and r.headers["location"] == "/en/login"


@pytest.mark.parametrize(
    ("role", "area"), [(UserRole.DOCTOR, "doctor"), (UserRole.CLINIC_ADMIN, "admin")]
)
async def test_other_roles_go_to_their_own_area(client, db, clinic, role, area):
    await make_user(db, email="other@x.com", role=role, clinic_id=clinic.id)
    await login(client, "other@x.com")
    some = uuid.uuid4()
    calls = [
        ("GET", LIST),
        ("GET", f"{LIST}/{some}"),
        ("POST", f"{LIST}/{some}/cancel"),
        ("GET", f"{LIST}/{some}/reschedule"),
    ]
    for method, path in calls:
        r = await client.request(method, path)
        assert r.status_code == 303 and r.headers["location"] == f"/en/{area}"


# ── The list ─────────────────────────────────────────────────────────────
async def test_the_list_splits_upcoming_and_past(patient_client, db, clinic, me):
    doctor, session = await world(db, clinic)
    await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    await add_appointment(db, me, doctor, clinic, session, at(TODAY, "09:00"))  # started
    await add_appointment(db, me, doctor, clinic, session, at(WED, "11:00"), status="cancelled")
    r = await patient_client.get(LIST)
    assert r.status_code == 200
    assert "Upcoming" in r.text and "Past" in r.text
    assert "Dr Booking" in r.text and "Shifa International" in r.text
    assert "2026-10-14 10:00" in r.text and "2026-10-12 09:00" in r.text
    assert "Confirmed" in r.text and "Cancelled" in r.text
    # Only the one upcoming row can be cancelled or moved.
    assert r.text.count('/cancel"') == 1 and r.text.count('/reschedule"') == 1


async def test_empty_states(patient_client):
    r = await patient_client.get(LIST)
    assert "You have no upcoming appointments." in r.text and "Nothing here yet." in r.text
    assert 'href="/en/patient/doctors"' in r.text


async def test_the_list_never_shows_another_patients_appointments(patient_client, db, clinic):
    doctor, session = await world(db, clinic)
    _, other, _ = await make_patient(db, email="p2@x.com", name="Patient 2")
    await add_appointment(db, other, doctor, clinic, session, at(WED, "10:00"))
    r = await patient_client.get(LIST)
    assert r.status_code == 200 and "You have no upcoming appointments." in r.text
    assert "2026-10-14 10:00" not in r.text and "Patient 2" not in r.text


async def test_the_list_survives_the_doctor_being_unverified_later(patient_client, db, clinic, me):
    doctor, session = await world(db, clinic)
    await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    doctor.verification_status = VerificationStatus.REJECTED
    doctor.verified_by = None
    doctor.verified_at = None
    await db.commit()
    r = await patient_client.get(LIST)
    assert r.status_code == 200 and "Dr Booking" in r.text


async def test_names_are_escaped_in_the_list(patient_client, db, clinic, me):
    doctor, session = await world(db, clinic)
    doctor.full_name = "<i>Dr</i>"
    await db.commit()
    await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    r = await patient_client.get(LIST)
    assert "<i>Dr</i>" not in r.text and "&lt;i&gt;Dr&lt;/i&gt;" in r.text


async def test_the_urdu_list_is_right_to_left(patient_client, db, clinic, me):
    doctor, session = await world(db, clinic)
    await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    r = await patient_client.get("/ur/patient/appointments")
    assert r.status_code == 200
    assert 'dir="rtl"' in r.text and "میری اپائنٹمنٹس" in r.text and "کنفرم" in r.text


async def test_the_patient_nav_has_appointments(patient_client):
    r = await patient_client.get("/en/patient")
    assert 'href="/en/patient/appointments"' in r.text


async def test_a_saved_flag_shows_a_banner_and_an_unknown_one_does_not(patient_client):
    assert (
        "Your appointment was cancelled."
        in (await patient_client.get(f"{LIST}?saved=cancelled")).text
    )
    assert "was cancelled" not in (await patient_client.get(f"{LIST}?saved=bogus")).text


# ── The confirmation ─────────────────────────────────────────────────────
async def test_the_confirmation_shows_date_and_time_only(patient_client, db, clinic, me):
    doctor, session = await world(db, clinic)
    mine = await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    r = await patient_client.get(f"{LIST}/{mine.id}?saved=booked")
    assert r.status_code == 200
    for text in (
        "Your appointment is confirmed.",
        "Dr Booking",
        "Shifa International",
        "Wednesday",
        "2026-10-14",
        "Confirmed",
    ):
        assert text in r.text
    assert ">10:00<" in r.text
    assert "Token" not in r.text  # no token number is shown (the CSS file name is lowercase)
    assert f"/en/patient/appointments/{mine.id}/cancel" in r.text
    assert f"/en/patient/appointments/{mine.id}/reschedule" in r.text
    assert 'href="/en/patient/appointments"' in r.text


async def test_the_confirmation_for_a_time_after_midnight_shows_pakistan_time(
    patient_client, db, clinic, me
):
    doctor, session = await world(db, clinic, weekday=THURSDAY, start="00:00", end="01:00")
    mine = await add_appointment(db, me, doctor, clinic, session, at(date(2026, 10, 15), "00:15"))
    r = await patient_client.get(f"{LIST}/{mine.id}")
    assert "Thursday" in r.text and "2026-10-15" in r.text and ">00:15<" in r.text


async def test_a_past_or_cancelled_appointment_has_no_actions(patient_client, db, clinic, me):
    doctor, session = await world(db, clinic)
    cancelled = await add_appointment(
        db, me, doctor, clinic, session, at(WED, "10:00"), status="cancelled"
    )
    r = await patient_client.get(f"{LIST}/{cancelled.id}")
    assert r.status_code == 200 and "/cancel" not in r.text and "/reschedule" not in r.text


async def test_someone_elses_unknown_or_malformed_confirmation_goes_to_the_list(
    patient_client, db, clinic
):
    doctor, session = await world(db, clinic)
    _, other, _ = await make_patient(db, email="p2@x.com", name="Patient 2")
    theirs = await add_appointment(db, other, doctor, clinic, session, at(WED, "10:00"))
    for target in (theirs.id, uuid.uuid4(), "junk"):
        r = await patient_client.get(f"{LIST}/{target}")
        assert r.status_code == 303 and r.headers["location"] == LIST


# ── Cancel ───────────────────────────────────────────────────────────────
async def test_cancelling_redirects_to_the_list_with_a_notice(patient_client, db, clinic, me):
    doctor, session = await world(db, clinic)
    mine = await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    r = await patient_client.post(f"{LIST}/{mine.id}/cancel")
    assert r.status_code == 303 and r.headers["location"] == f"{LIST}?saved=cancelled"
    assert await status_of(db, mine) == "cancelled"
    page = await patient_client.get(r.headers["location"])
    assert "Your appointment was cancelled." in page.text


async def test_cancelling_frees_the_time_on_the_doctor_page(patient_client, db, clinic, me):
    doctor, session = await world(db, clinic)
    mine = await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    assert "at=2026-10-14T10:00" not in (await patient_client.get(f"{FIND}/{doctor.id}")).text
    await patient_client.post(f"{LIST}/{mine.id}/cancel")
    assert "at=2026-10-14T10:00" in (await patient_client.get(f"{FIND}/{doctor.id}")).text


async def test_someone_elses_or_unknown_cancel_changes_nothing(patient_client, db, clinic):
    doctor, session = await world(db, clinic)
    _, other, _ = await make_patient(db, email="p2@x.com", name="Patient 2")
    theirs = await add_appointment(db, other, doctor, clinic, session, at(WED, "10:00"))
    for target in (theirs.id, uuid.uuid4(), "junk"):
        r = await patient_client.post(f"{LIST}/{target}/cancel")
        assert r.status_code == 303 and r.headers["location"] == LIST
    assert await status_of(db, theirs) == "booked"


async def test_a_started_or_already_cancelled_appointment_cannot_be_cancelled(
    patient_client, db, clinic, me
):
    doctor, session = await world(db, clinic)
    started = await add_appointment(db, me, doctor, clinic, session, at(TODAY, "09:00"))
    gone = await add_appointment(
        db, me, doctor, clinic, session, at(WED, "10:00"), status="cancelled"
    )
    for target in (started, gone):
        r = await patient_client.post(f"{LIST}/{target.id}/cancel")
        assert r.status_code == 409
        assert "can't be changed any more" in r.text.replace("&#39;", "'")
    assert await status_of(db, started) == "booked"


async def test_the_cancel_write_rate_limit_applies(patient_client, db, clinic, me, monkeypatch):
    monkeypatch.setattr(settings, "booking_write_rate_limit_per_minute", 1)
    doctor, session = await world(db, clinic)
    first = await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    second = await add_appointment(db, me, doctor, clinic, session, at(WED, "10:15"))
    assert (await patient_client.post(f"{LIST}/{first.id}/cancel")).status_code == 303
    r = await patient_client.post(f"{LIST}/{second.id}/cancel")
    assert r.status_code == 429 and "Too many attempts" in r.text
    assert await status_of(db, second) == "booked"


# ── The reschedule entry ─────────────────────────────────────────────────
async def test_reschedule_opens_the_doctor_page_in_move_mode(patient_client, db, clinic, me):
    doctor, session = await world(db, clinic)
    mine = await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    r = await patient_client.get(f"{LIST}/{mine.id}/reschedule")
    assert r.status_code == 303
    assert r.headers["location"] == (
        f"/en/patient/doctors/{doctor.id}?clinic={clinic.id}&moving={mine.id}"
    )
    page = await patient_client.get(r.headers["location"])
    assert "Moving your appointment on 2026-10-14 at 10:00" in page.text


async def test_reschedule_of_a_started_one_is_explained_and_of_a_foreign_one_goes_to_the_list(
    patient_client, db, clinic, me
):
    doctor, session = await world(db, clinic)
    _, other, _ = await make_patient(db, email="p2@x.com", name="Patient 2")
    started = await add_appointment(db, me, doctor, clinic, session, at(TODAY, "09:00"))
    theirs = await add_appointment(db, other, doctor, clinic, session, at(WED, "10:00"))
    r = await patient_client.get(f"{LIST}/{started.id}/reschedule")
    assert r.status_code == 409 and "can't be changed any more" in r.text.replace("&#39;", "'")
    for target in (theirs.id, uuid.uuid4(), "junk"):
        r = await patient_client.get(f"{LIST}/{target}/reschedule")
        assert r.status_code == 303 and r.headers["location"] == LIST


# ── The whole journey ────────────────────────────────────────────────────
async def test_find_book_see_move_and_cancel(patient_client, db, clinic):
    doctor, _ = await world(db, clinic)
    form = {"doctor_id": str(doctor.id), "clinic_id": str(clinic.id), "slot": "2026-10-14T10:00"}
    booked = await patient_client.post(LIST, data=form)
    confirmation = await patient_client.get(booked.headers["location"])
    assert "Your appointment is confirmed." in confirmation.text

    listing = await patient_client.get(LIST)
    assert "2026-10-14 10:00" in listing.text

    appointment_id = booked.headers["location"].split("/")[-1].split("?")[0]
    moved = await patient_client.post(
        f"{LIST}/{appointment_id}/reschedule",
        data={"clinic_id": str(clinic.id), "slot": "2026-10-21T11:00"},
    )
    assert moved.status_code == 303 and "saved=rescheduled" in moved.headers["location"]
    page = await patient_client.get(moved.headers["location"])
    assert "Your appointment has been moved." in page.text and "2026-10-21" in page.text

    new_id = moved.headers["location"].split("/")[-1].split("?")[0]
    cancelled = await patient_client.post(f"{LIST}/{new_id}/cancel")
    assert cancelled.headers["location"] == f"{LIST}?saved=cancelled"
    final = await patient_client.get(LIST)
    assert "You have no upcoming appointments." in final.text
