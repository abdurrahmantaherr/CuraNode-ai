"""P5 web: finding a doctor and seeing their free times."""

from __future__ import annotations

import uuid

import pytest
from app.db.models import UserRole

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
from tests.conftest import add_affiliation, doctor_of, make_user

FIND = "/en/patient/doctors"


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


# ── Guards ───────────────────────────────────────────────────────────────
async def test_anonymous_goes_to_login(client):
    r = await client.get(FIND)
    assert r.status_code == 303
    assert r.headers["location"] == "/en/login?next=/en/patient/doctors"


@pytest.mark.parametrize(
    ("role", "area"), [(UserRole.DOCTOR, "doctor"), (UserRole.CLINIC_ADMIN, "admin")]
)
async def test_other_roles_go_to_their_own_area(client, db, clinic, role, area):
    await make_user(db, email="other@x.com", role=role, clinic_id=clinic.id)
    await login(client, "other@x.com")
    for path in (FIND, f"{FIND}/{uuid.uuid4()}"):
        r = await client.get(path)
        assert r.status_code == 303
        assert r.headers["location"] == f"/en/{area}"


# ── Find ─────────────────────────────────────────────────────────────────
async def test_the_page_lists_verified_doctors(patient_client, db, clinic):
    doctor = await verified_doctor(db, clinic)
    r = await patient_client.get(FIND)
    assert r.status_code == 200
    assert "Dr Booking" in r.text and "Cardiology" in r.text and "Shifa International" in r.text
    assert f"/en/patient/doctors/{doctor.id}" in r.text
    assert "Verified" in r.text


async def test_a_pending_doctor_is_not_listed(patient_client, db, clinic):
    await make_user(db, email="pending@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    r = await patient_client.get(FIND)
    assert r.status_code == 200
    assert "No verified doctors match your search." in r.text


async def test_search_by_name_and_by_hospital(patient_client, db, clinic):
    await verified_doctor(db, clinic)
    assert "Dr Booking" in (await patient_client.get(f"{FIND}?q=book")).text
    assert "Dr Booking" in (await patient_client.get(f"{FIND}?q=shifa")).text
    nothing = await patient_client.get(f"{FIND}?q=zzz")
    assert "Dr Booking" not in nothing.text and "No verified doctors match" in nothing.text


async def test_specialty_chips_filter_and_an_unknown_specialty_is_ignored(
    patient_client, db, clinic
):
    await verified_doctor(db, clinic, email="a.dr@x.com", name="Dr Alpha", specialty="Cardiology")
    await verified_doctor(db, clinic, email="b.dr@x.com", name="Dr Beta", specialty="Dermatology")
    page = (await patient_client.get(FIND)).text
    assert "specialty=Cardiology" in page and "specialty=Dermatology" in page
    only = await patient_client.get(f"{FIND}?specialty=Dermatology")
    assert "Dr Beta" in only.text and "Dr Alpha" not in only.text
    ignored = await patient_client.get(f"{FIND}?specialty=Nonsense")
    assert "Dr Alpha" in ignored.text and "Dr Beta" in ignored.text


async def test_names_are_escaped(patient_client, db, clinic):
    await verified_doctor(db, clinic, name="<script>alert(1)</script>")
    r = await patient_client.get(FIND)
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text


async def test_a_very_long_search_is_cut_not_an_error(patient_client, db, clinic):
    await verified_doctor(db, clinic)
    r = await patient_client.get(f"{FIND}?q={'x' * 500}")
    assert r.status_code == 200
    assert f'value="{"x" * 80}"' in r.text and "x" * 81 not in r.text


async def test_an_unknown_notice_is_ignored_and_a_known_one_is_shown(patient_client, db, clinic):
    await verified_doctor(db, clinic)
    assert "cannot be booked" not in (await patient_client.get(f"{FIND}?notice=bogus")).text
    assert "cannot be booked" in (await patient_client.get(f"{FIND}?notice=unavailable")).text


async def test_the_urdu_page_is_right_to_left(patient_client, db, clinic):
    await verified_doctor(db, clinic)
    r = await patient_client.get("/ur/patient/doctors")
    assert r.status_code == 200
    assert 'dir="rtl"' in r.text and "ڈاکٹر تلاش کریں" in r.text
    assert 'href="/en/patient/doctors"' in r.text  # the language switch


async def test_the_patient_nav_has_find_a_doctor(patient_client):
    r = await patient_client.get("/en/patient")
    assert 'href="/en/patient/doctors"' in r.text


# ── A doctor's free times ────────────────────────────────────────────────
async def test_the_doctor_page_lists_only_free_times(patient_client, db, clinic):
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic)
    _, other, _ = await make_patient(db, email="p2@x.com", name="Patient 2")
    await add_appointment(db, other, doctor, clinic, session, at(WED, "09:00"))
    r = await patient_client.get(f"{FIND}/{doctor.id}")
    assert r.status_code == 200
    assert "at=2026-10-14T09:00" not in r.text  # taken
    assert "at=2026-10-14T09:15" in r.text and "at=2026-10-21T12:45" in r.text
    assert "Wednesday" in r.text and "2026-10-14" in r.text
    assert "at=2026-10-28" not in r.text  # beyond 14 days


async def test_each_hospital_has_its_own_times(patient_client, db, clinic, second_clinic):
    doctor = await verified_doctor(db, clinic)
    await add_affiliation(db, doctor.id, second_clinic.id)
    await add_session(db, doctor, clinic, weekday=2, start="09:00", end="10:00")
    await add_session(db, doctor, second_clinic, weekday=3, start="09:00", end="10:00")
    here = (await patient_client.get(f"{FIND}/{doctor.id}?clinic={clinic.id}")).text
    there = (await patient_client.get(f"{FIND}/{doctor.id}?clinic={second_clinic.id}")).text
    assert "at=2026-10-14T09:00" in here and "at=2026-10-15T09:00" not in here
    assert "at=2026-10-15T09:00" in there and "at=2026-10-14T09:00" not in there
    assert f"clinic={clinic.id}" in here and f"clinic={second_clinic.id}" in here


async def test_an_unknown_hospital_falls_back_to_the_first(patient_client, db, clinic):
    doctor = await verified_doctor(db, clinic)
    await add_session(db, doctor, clinic)
    r = await patient_client.get(f"{FIND}/{doctor.id}?clinic=not-a-uuid")
    assert r.status_code == 200 and "at=2026-10-14T09:00" in r.text


async def test_a_doctor_with_no_sessions_says_so(patient_client, db, clinic):
    doctor = await verified_doctor(db, clinic)
    r = await patient_client.get(f"{FIND}/{doctor.id}")
    assert r.status_code == 200 and "No free times at this hospital" in r.text


@pytest.mark.parametrize("target", ["not-a-uuid", str(uuid.uuid4())])
async def test_an_unknown_doctor_shows_the_find_page_with_a_404(patient_client, target):
    r = await patient_client.get(f"{FIND}/{target}")
    assert r.status_code == 404 and "find that" in r.text


async def test_a_pending_doctor_cannot_be_opened(patient_client, db, clinic):
    user = await make_user(db, email="pending@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    pending = await doctor_of(db, user)
    r = await patient_client.get(f"{FIND}/{pending.id}")
    assert r.status_code == 404 and "find that" in r.text  # the find page, not a missing route


async def test_a_time_just_after_midnight_is_shown_under_its_pakistan_date(
    patient_client, db, clinic
):
    # Review focus 2: 00:15 on Thursday the 15th is 19:15 UTC on the 14th.
    doctor = await verified_doctor(db, clinic)
    await add_session(db, doctor, clinic, weekday=THURSDAY, start="00:00", end="01:00")
    r = await patient_client.get(f"{FIND}/{doctor.id}")
    assert "at=2026-10-15T00:15" in r.text
    assert "Thursday" in r.text and "2026-10-15" in r.text
    assert ">00:15<" in r.text


# ── The booking being moved ──────────────────────────────────────────────
async def test_a_booking_being_moved_is_named_and_carried_in_the_links(
    patient_client, db, clinic, me
):
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic)
    mine = await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    r = await patient_client.get(f"{FIND}/{doctor.id}?moving={mine.id}")
    assert "Moving your appointment on 2026-10-14 at 10:00" in r.text
    assert f"moving={mine.id}" in r.text


async def test_a_moving_id_that_is_not_the_callers_or_not_changeable_is_ignored(
    patient_client, db, clinic, me
):
    doctor = await verified_doctor(db, clinic)
    rival = await verified_doctor(db, clinic, email="rival@x.com", name="Dr Rival")
    session = await add_session(db, doctor, clinic)
    rival_session = await add_session(db, rival, clinic)
    _, other, _ = await make_patient(db, email="p2@x.com", name="Patient 2")
    foreign = await add_appointment(db, other, doctor, clinic, session, at(WED, "10:00"))
    started = await add_appointment(db, me, doctor, clinic, session, at(TODAY, "09:00"))
    elsewhere = await add_appointment(db, me, rival, clinic, rival_session, at(WED, "11:00"))
    for moving in (foreign.id, started.id, elsewhere.id, "junk"):
        r = await patient_client.get(f"{FIND}/{doctor.id}?moving={moving}")
        assert r.status_code == 200
        assert "Moving your appointment" not in r.text and "moving=" not in r.text
