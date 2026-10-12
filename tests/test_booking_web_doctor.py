"""P5 web: the doctor's appointments page and the dashboard panel.

The doctor sees name, time and status only; booking is not consent (FR4)."""

from __future__ import annotations

from datetime import date

import pytest
from app.db.models import UserRole
from app.i18n.catalogue import translate

from tests.booking_helpers import (
    MONDAY,
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

PAGE = "/en/doctor/appointments"


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    freeze(monkeypatch)


@pytest.fixture
async def mine(db, clinic):
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic, weekday=MONDAY, start="09:00", end="18:00")
    return doctor, session


@pytest.fixture
async def doctor_client(client, mine):
    await login(client, "booking.dr@x.com")
    return client


# ── Guards ───────────────────────────────────────────────────────────────
async def test_anonymous_goes_to_login(client):
    r = await client.get(PAGE)
    assert r.status_code == 303
    assert r.headers["location"] == "/en/login?next=/en/doctor/appointments"


@pytest.mark.parametrize(
    ("role", "area"), [(UserRole.PATIENT, "patient"), (UserRole.CLINIC_ADMIN, "admin")]
)
async def test_other_roles_go_to_their_own_area(client, db, clinic, role, area):
    await make_user(db, email="other@x.com", role=role, clinic_id=clinic.id)
    await login(client, "other@x.com")
    r = await client.get(PAGE)
    assert r.status_code == 303 and r.headers["location"] == f"/en/{area}"


async def test_a_pending_doctor_is_sent_to_their_dashboard(client, db, clinic):
    await make_user(db, email="pending@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    await login(client, "pending@x.com")
    r = await client.get(PAGE)
    assert r.status_code == 303 and r.headers["location"] == "/en/doctor"


# ── The day's list ───────────────────────────────────────────────────────
async def test_todays_appointments_show_time_name_and_status(doctor_client, db, clinic, mine):
    doctor, session = mine
    _, patient, _ = await make_patient(db)
    await add_appointment(db, patient, doctor, clinic, session, at(TODAY, "14:00"))
    r = await doctor_client.get(PAGE)
    assert r.status_code == 200
    assert "Pat Booking" in r.text and ">14:00<" in r.text and "Confirmed" in r.text
    assert 'value="2026-10-12"' in r.text  # the date picker starts on today


async def test_only_name_time_and_status_reach_the_page(doctor_client, db, clinic, mine):
    doctor, session = mine
    _, patient, _ = await make_patient(db)
    patient.passport_no = "CN-SECRET-77"
    await db.commit()
    await add_appointment(db, patient, doctor, clinic, session, at(TODAY, "14:00"))
    r = await doctor_client.get(PAGE)
    assert "Pat Booking" in r.text and "CN-SECRET-77" not in r.text


async def test_a_cancelled_appointment_is_shown_as_cancelled(doctor_client, db, clinic, mine):
    doctor, session = mine
    _, patient, _ = await make_patient(db)
    await add_appointment(
        db, patient, doctor, clinic, session, at(TODAY, "14:00"), status="cancelled"
    )
    r = await doctor_client.get(PAGE)
    assert "Pat Booking" in r.text and "Cancelled" in r.text


async def test_an_empty_day_says_so(doctor_client):
    assert "No appointments on this day." in (await doctor_client.get(PAGE)).text


async def test_the_date_picker_chooses_the_day(doctor_client, db, clinic, mine):
    doctor, session = mine
    _, patient, _ = await make_patient(db)
    await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    on_wed = await doctor_client.get(f"{PAGE}?date=2026-10-14")
    assert "Pat Booking" in on_wed.text and "Wednesday" in on_wed.text
    assert "Pat Booking" not in (await doctor_client.get(PAGE)).text  # today is Monday
    assert "Pat Booking" not in (await doctor_client.get(f"{PAGE}?date=2026-10-15")).text


@pytest.mark.parametrize(
    "raw", ["junk", "", "0001-01-01", "9999-12-31", "2028-01-01", "2026-13-40"]
)
async def test_a_bad_or_far_away_date_shows_today(doctor_client, raw):
    r = await doctor_client.get(f"{PAGE}?date={raw}")
    assert r.status_code == 200 and 'value="2026-10-12"' in r.text


async def test_the_day_links_step_by_one_day(doctor_client):
    r = await doctor_client.get(f"{PAGE}?date=2026-10-14")
    assert "date=2026-10-13" in r.text and "date=2026-10-15" in r.text


async def test_a_time_just_after_midnight_belongs_to_its_pakistan_day(
    doctor_client, db, clinic, mine
):
    # Review focus 2: 00:15 on Thursday the 15th is 19:15 UTC on the 14th.
    doctor, _ = mine
    thursday_session = await add_session(
        db, doctor, clinic, weekday=THURSDAY, start="00:00", end="01:00"
    )
    _, patient, _ = await make_patient(db)
    await add_appointment(
        db, patient, doctor, clinic, thursday_session, at(date(2026, 10, 15), "00:15")
    )
    on_thursday = await doctor_client.get(f"{PAGE}?date=2026-10-15")
    on_wednesday = await doctor_client.get(f"{PAGE}?date=2026-10-14")
    assert "Pat Booking" in on_thursday.text and ">00:15<" in on_thursday.text
    assert "Pat Booking" not in on_wednesday.text


async def test_another_doctors_patients_are_not_listed(doctor_client, db, clinic):
    rival = await verified_doctor(db, clinic, email="rival@x.com", name="Dr Rival")
    rival_session = await add_session(db, rival, clinic, weekday=MONDAY, start="09:00", end="18:00")
    _, patient, _ = await make_patient(db)
    await add_appointment(db, patient, rival, clinic, rival_session, at(TODAY, "14:00"))
    r = await doctor_client.get(PAGE)
    assert r.status_code == 200 and "No appointments on this day." in r.text
    assert "Pat Booking" not in r.text


async def test_names_are_escaped(doctor_client, db, clinic, mine):
    doctor, session = mine
    _, patient, _ = await make_patient(db, name="<b>Pat</b>")
    await add_appointment(db, patient, doctor, clinic, session, at(TODAY, "14:00"))
    r = await doctor_client.get(PAGE)
    assert "<b>Pat</b>" not in r.text and "&lt;b&gt;Pat&lt;/b&gt;" in r.text


async def test_the_urdu_page_is_right_to_left(doctor_client):
    r = await doctor_client.get("/ur/doctor/appointments")
    assert r.status_code == 200 and 'dir="rtl"' in r.text and "اپائنٹمنٹس" in r.text
    assert 'href="/en/doctor/appointments?date=2026-10-12"' in r.text  # the language switch


# ── The nav and the dashboard panel ──────────────────────────────────────
async def test_a_verified_doctors_nav_has_appointments(doctor_client):
    r = await doctor_client.get("/en/doctor")
    assert 'href="/en/doctor/appointments"' in r.text


async def test_the_dashboard_panel_lists_today_above_my_patients(doctor_client, db, clinic, mine):
    doctor, session = mine
    _, patient, _ = await make_patient(db)
    await add_appointment(db, patient, doctor, clinic, session, at(TODAY, "14:00"))
    r = await doctor_client.get("/en/doctor")
    assert r.status_code == 200
    assert "Appointments today" in r.text and "Pat Booking" in r.text and ">14:00<" in r.text
    patients_title = translate("doctor_dashboard.patients.title", "en")
    assert r.text.index("Appointments today") < r.text.index(patients_title)


async def test_the_dashboard_panel_is_empty_with_no_appointments(doctor_client):
    r = await doctor_client.get("/en/doctor")
    assert "Appointments today" in r.text and "No appointments today." in r.text


async def test_the_dashboard_panel_only_shows_today(doctor_client, db, clinic, mine):
    doctor, session = mine
    _, patient, _ = await make_patient(db)
    await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    r = await doctor_client.get("/en/doctor")
    assert "No appointments today." in r.text  # the panel is there, and empty
    assert "Pat Booking" not in r.text


async def test_a_pending_doctor_sees_no_panel_and_no_nav_entry(client, db, clinic):
    await make_user(db, email="pending@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    await login(client, "pending@x.com")
    r = await client.get("/en/doctor")
    assert r.status_code == 200
    assert "Appointments today" not in r.text
    assert "/en/doctor/appointments" not in r.text
