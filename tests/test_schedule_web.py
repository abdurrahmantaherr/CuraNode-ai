"""D7 web: the schedule page and its forms (routes T10, page T11, AC checks T13).

"Today" is frozen at Monday 2026-10-12. The doctor is pending (unverified)
unless a test says otherwise: D7 lets any doctor manage their schedule.
"""

from __future__ import annotations

from datetime import date

import pytest
from app.db import types as dbtypes
from app.db.models import DoctorSchedule, ScheduleException, UserRole
from sqlalchemy import select

from tests.conftest import TEST_PASSWORD, make_user

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
