"""D7 web: the schedule page and its forms (routes T10, page T11, AC checks T13).

"Today" is frozen at Monday 2026-10-12. The doctor is pending (unverified)
unless a test says otherwise: D7 lets any doctor manage their schedule.
"""

from __future__ import annotations

import ast
import uuid
from datetime import date, time, timedelta
from pathlib import Path

import pytest
from app.db import types as dbtypes
from app.db.models import (
    Appointment,
    DoctorAffiliation,
    DoctorSchedule,
    Patient,
    Profile,
    ScheduleException,
    ScheduleExceptionType,
    UserRole,
)
from app.i18n.catalogue import missing_keys
from app.settings import settings
from app.web import router as web_router
from sqlalchemy import select

from tests.conftest import TEST_PASSWORD, doctor_of, make_user

TODAY = date(2026, 10, 12)  # a Monday
PAGE = "/en/doctor/schedule"


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    monkeypatch.setattr(dbtypes, "today_pk", lambda: TODAY)


async def login(client, email: str) -> None:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": TEST_PASSWORD})
    assert r.status_code == 200, r.text


@pytest.fixture
async def doctor_client(client, schedule_doctor):
    await login(client, "schedule.doctor@x.com")
    return client


def slot_form(clinic, weekdays=("0",), start="09:00", end="13:00", **extra) -> dict:
    return {
        "clinic_id": str(clinic.id),
        "weekdays": list(weekdays),
        "start_time": start,
        "end_time": end,
        "slot_minutes": "15",
        "max_tokens": "",
        **extra,
    }


async def rows(db, model):
    return (
        (await db.execute(select(model).execution_options(populate_existing=True))).scalars().all()
    )


# ── Guards (AC-11) ───────────────────────────────────────────────────────
async def test_anonymous_goes_to_login(client):
    r = await client.get(PAGE)
    assert r.status_code == 303
    assert r.headers["location"] == "/en/login?next=/en/doctor/schedule"


@pytest.mark.parametrize(
    ("role", "area"), [(UserRole.PATIENT, "patient"), (UserRole.CLINIC_ADMIN, "admin")]
)
async def test_other_roles_go_to_their_own_area(client, db, clinic, role, area):
    await make_user(db, email="other@x.com", role=role, clinic_id=clinic.id)
    await login(client, "other@x.com")
    for method, path in [("GET", PAGE), ("POST", f"{PAGE}/slots"), ("POST", f"{PAGE}/time-off")]:
        r = await client.request(method, path)
        assert r.status_code == 303
        assert r.headers["location"] == f"/en/{area}"


# ── Writes by a pending doctor (AC-24) ───────────────────────────────────
async def test_add_slots_redirects_with_a_saved_banner(doctor_client, db, clinic):
    r = await doctor_client.post(f"{PAGE}/slots", data=slot_form(clinic, weekdays=("0", "2")))
    assert r.status_code == 303
    assert r.headers["location"] == "/en/doctor/schedule?saved=slot_added#slots"
    assert sorted(s.weekday for s in await rows(db, DoctorSchedule)) == [0, 2]


async def test_edit_and_remove_a_slot(doctor_client, db, clinic):
    await doctor_client.post(f"{PAGE}/slots", data=slot_form(clinic))
    [slot] = await rows(db, DoctorSchedule)

    form = {k: v for k, v in slot_form(clinic, end="12:00").items() if k != "weekdays"}
    r = await doctor_client.post(f"{PAGE}/slots/{slot.id}", data={**form, "weekday": "0"})
    assert r.headers["location"].endswith("saved=slot_updated#slots")
    [slot] = await rows(db, DoctorSchedule)
    assert slot.end_time.strftime("%H:%M") == "12:00"

    r = await doctor_client.post(f"{PAGE}/slots/{slot.id}/remove")
    assert r.headers["location"].endswith("saved=slot_removed#slots")
    [slot] = await rows(db, DoctorSchedule)
    assert slot.status == "inactive"


async def test_time_off_extra_and_removal(doctor_client, db, clinic):
    await doctor_client.post(f"{PAGE}/slots", data=slot_form(clinic))
    r = await doctor_client.post(
        f"{PAGE}/time-off",
        data={
            "exception_date": TODAY.isoformat(),
            "clinic_id": "",
            "start_time": "",
            "end_time": "",
        },
    )
    assert r.headers["location"].endswith("saved=time_off_added#exceptions")
    r = await doctor_client.post(
        f"{PAGE}/extra",
        data={
            "exception_date": TODAY.isoformat(),
            "clinic_id": str(clinic.id),
            "start_time": "14:00",
            "end_time": "16:00",
            "max_tokens": "",
        },
    )
    assert r.headers["location"].endswith("saved=extra_added#exceptions")
    exceptions = await rows(db, ScheduleException)
    assert sorted(e.kind.value for e in exceptions) == ["extra", "leave"]

    r = await doctor_client.post(f"{PAGE}/exceptions/{exceptions[0].id}/remove")
    assert r.headers["location"].endswith("saved=exception_removed#exceptions")


# ── The page (T11) ───────────────────────────────────────────────────────
async def test_empty_page_renders_for_a_pending_doctor(doctor_client):
    r = await doctor_client.get(PAGE)
    assert r.status_code == 200
    body = r.text
    assert "My schedule" in body
    assert "You have no weekly hours yet." in body
    assert 'action="/en/doctor/schedule/slots"' in body  # the add form
    assert "Time off applies only to the hours already on your schedule" in body  # AC-25


async def test_week_view_lists_and_capacity_render(doctor_client, clinic):
    await doctor_client.post(f"{PAGE}/slots", data=slot_form(clinic))
    await doctor_client.post(
        f"{PAGE}/extra",
        data={
            "exception_date": TODAY.isoformat(),
            "clinic_id": str(clinic.id),
            "start_time": "13:00",
            "end_time": "15:00",
            "max_tokens": "",
        },
    )
    r = await doctor_client.get(PAGE)
    body = r.text
    assert r.status_code == 200
    assert "09:00–13:00" in body and "13:00–15:00" in body  # AC-1, the week view
    assert "Up to 16 patients" in body
    assert "Up to 16 patients (from your Monday hours)" in body  # AC-33, inherited cap
    assert "15 min per patient · up to 16 patients" in body
    assert "Shifa International" in body


async def test_a_failed_add_rerenders_with_the_conflict_and_the_values(doctor_client, clinic):
    await doctor_client.post(f"{PAGE}/slots", data=slot_form(clinic))
    r = await doctor_client.post(
        f"{PAGE}/slots", data=slot_form(clinic, start="12:00", end="14:00")
    )
    assert r.status_code == 409
    assert "This overlaps your Monday 09:00–13:00 hours at Shifa International." in r.text
    assert 'value="12:00"' in r.text  # the doctor's input is kept
    assert 'value="8"' in r.text  # blank cap pre-filled with calc (R13)


async def test_a_field_error_is_shown_next_to_the_field(doctor_client, clinic):
    r = await doctor_client.post(f"{PAGE}/slots", data=slot_form(clinic, weekdays=()))
    assert r.status_code == 422
    assert "Choose at least one day." in r.text


async def test_urdu_page_is_rtl(doctor_client):
    r = await doctor_client.get("/ur/doctor/schedule")
    assert r.status_code == 200
    assert 'dir="rtl"' in r.text
    assert "میرا شیڈول" in r.text


# ── Dashboard links and nav (T12, AC-24) ─────────────────────────────────
async def test_pending_dashboard_has_only_the_schedule_link(doctor_client):
    r = await doctor_client.get("/en/doctor")
    body = r.text
    assert r.status_code == 200
    assert 'href="/en/doctor/schedule"' in body
    assert "Manage schedule" in body
    assert "passport-lookup" not in body  # FR3: no patient-data affordance
    assert "This area arrives with a later feature" in body  # still the placeholder page


async def test_verified_dashboard_has_lookup_and_schedule_links(client, db, clinic):
    admin = await make_user(
        db, email="verifier@x.com", role=UserRole.CLINIC_ADMIN, clinic_id=clinic.id
    )
    await make_user(
        db,
        email="verified@x.com",
        role=UserRole.DOCTOR,
        clinic_id=clinic.id,
        is_verified=True,
        verifier_id=admin.id,
    )
    await login(client, "verified@x.com")
    body = (await client.get("/en/doctor")).text
    assert 'href="/en/doctor/passport-lookup"' in body
    assert 'href="/en/doctor/schedule"' in body
    schedule = (await client.get(PAGE)).text
    assert 'href="/en/doctor/passport-lookup"' in schedule  # a verified doctor's nav keeps lookup


async def test_pending_schedule_nav_has_no_lookup(doctor_client):
    body = (await doctor_client.get(PAGE)).text
    assert 'aria-current="page"' in body
    assert "passport-lookup" not in body


# ── Integration (T13) ────────────────────────────────────────────────────
async def _add_slot_via_page(client, clinic, **kw):
    r = await client.post(f"{PAGE}/slots", data=slot_form(clinic, **kw))
    assert r.status_code == 303, r.text


async def _foreign_slot_and_exception(db, clinic):
    other_user = await make_user(
        db, email="foreign@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id
    )
    other = await doctor_of(db, other_user)
    aff = (
        await db.execute(select(DoctorAffiliation).where(DoctorAffiliation.doctor_id == other.id))
    ).scalar_one()
    slot = DoctorSchedule(
        affiliation_id=aff.id,
        weekday=0,
        start_time=time(9),
        end_time=time(13),
        slot_minutes=15,
        max_tokens=16,
    )
    db.add(slot)
    await db.flush()
    exception = ScheduleException(
        schedule_id=slot.id, exception_date=TODAY, kind=ScheduleExceptionType.LEAVE
    )
    db.add(exception)
    await db.commit()
    return slot, exception


async def test_ac10_foreign_unknown_and_malformed_ids_look_the_same(doctor_client, db, clinic):
    foreign_slot, foreign_exception = await _foreign_slot_and_exception(db, clinic)
    edit = {k: v for k, v in slot_form(clinic).items() if k != "weekdays"} | {"weekday": "0"}
    routes = [
        ("slots/{id}", edit),
        ("slots/{id}/remove", {}),
        ("exceptions/{id}/remove", {}),
    ]
    for path, data in routes:
        foreign_id = foreign_exception.id if path.startswith("exceptions") else foreign_slot.id
        responses = []
        for target in (foreign_id, uuid.uuid4(), "not-a-uuid"):
            r = await doctor_client.post(f"{PAGE}/{path.format(id=target)}", data=data)
            responses.append((r.status_code, "We couldn&#39;t find that." in r.text))
        assert responses == [(404, True)] * 3, (path, responses)
    # Nothing of the other doctor's changed.
    await db.refresh(foreign_slot)
    await db.refresh(foreign_exception)
    assert (foreign_slot.status, foreign_exception.removed_at) == ("active", None)


async def test_ac11_incomplete_onboarding_goes_to_onboarding(client, db, clinic):
    user = await make_user(db, email="half@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    doctor = await doctor_of(db, user)
    for aff in (
        await db.execute(select(DoctorAffiliation).where(DoctorAffiliation.doctor_id == doctor.id))
    ).scalars():
        await db.delete(aff)
    await db.delete(doctor)
    await db.commit()
    await login(client, "half@x.com")
    r = await client.get(PAGE)
    assert (r.status_code, r.headers["location"]) == (303, "/en/onboarding")


async def test_ac11_redirects_render_no_schedule_data(client, db, clinic):
    await make_user(db, email="pat@x.com", role=UserRole.PATIENT)
    await login(client, "pat@x.com")
    r = await client.get(PAGE)
    assert r.status_code == 303 and "slot" not in r.text.lower()


async def test_ac14_ac25_urdu_page(doctor_client, clinic):
    await _add_slot_via_page(doctor_client, clinic)
    r = await doctor_client.get("/ur/doctor/schedule")
    body = r.text
    assert 'dir="rtl"' in body
    assert "پیر" in body  # weekday.0
    assert "چھٹی صرف اُن اوقات پر لاگو ہوتی ہے" in body  # schedule.time_off_note
    assert missing_keys() == {"ur": []}


async def test_ac2_conflict_message_in_urdu(doctor_client, clinic):
    await _add_slot_via_page(doctor_client, clinic)
    r = await doctor_client.post(
        "/ur/doctor/schedule/slots", data=slot_form(clinic, start="12:00", end="14:00")
    )
    assert r.status_code == 409
    assert "یہ Shifa International میں آپ کے پیر 09:00–13:00 کے اوقات سے ٹکراتا ہے۔" in r.text


def test_ac24_no_schedule_handler_uses_the_verified_doctor_gate():
    source = Path(web_router.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {
        "_doctor_schedule_guard",
        "_render_schedule",
        "_schedule_failure",
    }
    checked = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and (
            node.name in names or node.name.startswith("doctor_schedule_")
        ):
            text = ast.get_source_segment(source, node) or ""
            assert "VerifiedDoctorDep" not in text and "require_verified_doctor" not in text
            assert "is_verified_doctor" not in text or node.name == "_render_schedule"
            checked += 1
    assert checked == len(names) + 7  # the guard, two helpers, and seven routes


async def test_ac28_a_booked_slot_shows_the_locked_message(doctor_client, db, clinic):
    await _add_slot_via_page(doctor_client, clinic)
    [slot] = (await db.execute(select(DoctorSchedule))).scalars().all()
    patient_user = await make_user(db, email="booked@x.com", role=UserRole.PATIENT)
    patient = (
        await db.execute(select(Patient).where(Patient.user_id == patient_user.id))
    ).scalar_one()
    doctor = await doctor_of(db, await _user(db, "schedule.doctor@x.com"))
    db.add(
        Appointment(
            patient_id=patient.id,
            doctor_id=doctor.id,
            clinic_id=clinic.id,
            schedule_id=slot.id,
            slot_start=dbtypes.utcnow() + timedelta(days=2),
        )
    )
    await db.commit()

    body = (await doctor_client.get(PAGE)).text
    assert "This slot has upcoming bookings, so it can&#39;t be changed yet." in body
    assert f"/slots/{slot.id}/remove" not in body
    edit = {k: v for k, v in slot_form(clinic).items() if k != "weekdays"} | {"weekday": "0"}
    r = await doctor_client.post(f"{PAGE}/slots/{slot.id}", data=edit | {"max_tokens": "20"})
    assert r.status_code == 422
    assert "These hours have upcoming bookings" in r.text


async def test_ac33_the_inherited_cap_follows_the_anchor_on_the_page(doctor_client, db, clinic):
    await _add_slot_via_page(doctor_client, clinic)
    await doctor_client.post(
        f"{PAGE}/extra",
        data={
            "exception_date": TODAY.isoformat(),
            "clinic_id": str(clinic.id),
            "start_time": "13:00",
            "end_time": "15:00",
            "max_tokens": "",
        },
    )
    [slot] = (await db.execute(select(DoctorSchedule))).scalars().all()
    edit = {k: v for k, v in slot_form(clinic).items() if k != "weekdays"} | {"weekday": "0"}
    await doctor_client.post(f"{PAGE}/slots/{slot.id}", data=edit | {"max_tokens": "30"})
    body = (await doctor_client.get(PAGE)).text
    assert "Up to 30 patients (from your Monday hours)" in body
    [extra] = (await db.execute(select(ScheduleException))).scalars().all()
    await db.refresh(extra)
    assert extra.max_tokens is None


async def test_edit_mode_shows_the_slot_form(doctor_client, db, clinic):
    await _add_slot_via_page(doctor_client, clinic)
    [slot] = (await db.execute(select(DoctorSchedule))).scalars().all()
    body = (await doctor_client.get(f"{PAGE}?edit={slot.id}")).text
    assert f'action="/en/doctor/schedule/slots/{slot.id}"' in body
    assert "Calculated: 16" in body


async def test_an_inactive_clinic_slot_shows_the_badge_and_no_edit(doctor_client, db, clinic):
    await _add_slot_via_page(doctor_client, clinic)
    doctor = await doctor_of(db, await _user(db, "schedule.doctor@x.com"))
    aff = (
        await db.execute(
            select(DoctorAffiliation).where(
                DoctorAffiliation.doctor_id == doctor.id, DoctorAffiliation.clinic_id == clinic.id
            )
        )
    ).scalar_one()
    aff.status = "inactive"
    await db.commit()
    body = (await doctor_client.get(PAGE)).text
    assert "Inactive clinic" in body
    assert "?edit=" not in body


async def test_writes_are_rate_limited(doctor_client, clinic, monkeypatch):
    monkeypatch.setattr(settings, "schedule_write_rate_limit_per_minute", 1)
    await _add_slot_via_page(doctor_client, clinic)
    r = await doctor_client.post(f"{PAGE}/slots", data=slot_form(clinic, weekdays=("1",)))
    assert r.status_code == 429


async def test_week_and_clinic_filter(doctor_client, clinic, second_clinic):
    await _add_slot_via_page(doctor_client, clinic)
    await _add_slot_via_page(doctor_client, second_clinic, weekdays=("1",))
    body = (await doctor_client.get(f"{PAGE}?clinic={second_clinic.id}")).text
    assert body.count('class="slot-block ') == 1  # only Tuesday at the second clinic
    nxt = (await doctor_client.get(f"{PAGE}?week=2026-10-19")).text
    assert "Week of 2026-10-19" in nxt and nxt.count('class="slot-block ') == 2


async def _user(db, email: str):
    return (await db.execute(select(Profile).where(Profile.email == email))).scalar_one()


# ── The view starts today (regression: on a Sunday the calendar week was all past)
async def test_on_a_sunday_the_default_view_shows_the_coming_week(
    doctor_client, clinic, monkeypatch
):
    await _add_slot_via_page(doctor_client, clinic)  # Monday 09:00-13:00
    monkeypatch.setattr(dbtypes, "today_pk", lambda: date(2026, 10, 18))  # a Sunday
    body = (await doctor_client.get(PAGE)).text
    assert "Week of 2026-10-18" in body
    assert body.count('class="slot-block ') == 1  # tomorrow, Monday 2026-10-19
    assert "09:00–13:00" in body
    assert "week__day--past" not in body


async def test_the_add_form_says_hours_go_only_at_linked_clinics(doctor_client):
    body = (await doctor_client.get(PAGE)).text
    assert "You are linked to 2 clinic(s)." in body  # schedule_doctor: two clinics
    assert "ask that hospital&#39;s admin to add you" in body
