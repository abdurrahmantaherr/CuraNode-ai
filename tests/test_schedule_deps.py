"""D7 T3: `DoctorDep`, the schedule write budget, and `ScheduleConflict`.

`DoctorDep` admits any doctor, verified or pending (spec §3, decision 6), and
nobody else. The schedule write budget counts per doctor. `ScheduleConflict`
fills its message from scalar details, in both locales.
"""

from __future__ import annotations

import uuid

import pytest
from app.db.models import UserRole

# `DoctorDep` MUST be imported at module scope. `from __future__ import
# annotations` makes FastAPI resolve the route annotations below from this
# module's globals.
from app.deps import Actor, DoctorDep, check_schedule_write_rate
from app.errors import RateLimited, ScheduleConflict, envelope
from app.settings import settings

from tests.conftest import TEST_PASSWORD, make_user

PROBE = "/api/v1/_test/schedule_probe"


async def login(client, email: str) -> None:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": TEST_PASSWORD})
    assert r.status_code == 200, r.text


@pytest.fixture
def probe(app):
    @app.get(PROBE)
    async def _probe(actor: DoctorDep):  # type: ignore[no-untyped-def]
        return {"role": actor.role, "verified": actor.is_verified_doctor}

    return PROBE


# ── DoctorDep ────────────────────────────────────────────────────────────
async def test_doctor_dep_admits_a_pending_doctor(client, db, clinic, probe):
    await make_user(db, email="pending@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    await login(client, "pending@x.com")

    r = await client.get(probe)
    assert r.status_code == 200
    assert r.json() == {"role": "doctor", "verified": False}


async def test_doctor_dep_admits_a_verified_doctor(client, db, clinic, probe):
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

    r = await client.get(probe)
    assert r.status_code == 200
    assert r.json() == {"role": "doctor", "verified": True}


@pytest.mark.parametrize("role", [UserRole.PATIENT, UserRole.CLINIC_ADMIN])
async def test_doctor_dep_rejects_other_roles(client, db, clinic, probe, role):
    await make_user(db, email="other@x.com", role=role, clinic_id=clinic.id)
    await login(client, "other@x.com")

    r = await client.get(probe)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


async def test_doctor_dep_rejects_anonymous(client, probe):
    r = await client.get(probe)
    assert r.status_code == 401


# ── Schedule write budget ────────────────────────────────────────────────
def _doctor_actor(user_id: uuid.UUID) -> Actor:
    return Actor(user_id=user_id, role=UserRole.DOCTOR.value)


def test_schedule_write_budget_default_is_30_per_minute():
    assert settings.schedule_write_rate_limit_per_minute == 30


async def test_schedule_write_budget_trips_after_the_limit(monkeypatch):
    monkeypatch.setattr(settings, "schedule_write_rate_limit_per_minute", 3)
    actor = _doctor_actor(uuid.uuid4())

    for _ in range(3):
        await check_schedule_write_rate(actor)
    with pytest.raises(RateLimited) as exc:
        await check_schedule_write_rate(actor)
    assert exc.value.retry_after_s == 60


async def test_schedule_write_budget_is_per_doctor(monkeypatch):
    monkeypatch.setattr(settings, "schedule_write_rate_limit_per_minute", 1)
    first, second = _doctor_actor(uuid.uuid4()), _doctor_actor(uuid.uuid4())

    await check_schedule_write_rate(first)
    await check_schedule_write_rate(second)  # a different doctor's own budget
    with pytest.raises(RateLimited):
        await check_schedule_write_rate(first)


# ── ScheduleConflict ─────────────────────────────────────────────────────
DETAILS = {
    "clinic": "Shifa International",
    "day": "Monday",
    "start": "09:00",
    "end": "13:00",
    "conflict_id": "c0ffee00-0000-7000-8000-000000000000",
}


def test_schedule_conflict_envelope_names_the_window():
    body = envelope(ScheduleConflict(DETAILS), "req-1", "en")["error"]

    assert body["code"] == "SCHEDULE_CONFLICT"
    assert body["message_key"] == "errors.schedule_overlap"
    assert body["retryable"] is False
    assert body["details"] == DETAILS
    assert body["message"] == "This overlaps your Monday 09:00–13:00 hours at Shifa International."


def test_schedule_conflict_is_409():
    assert ScheduleConflict.http_status == 409


def test_schedule_conflict_message_is_filled_in_urdu():
    message = envelope(ScheduleConflict(DETAILS), "req-1", "ur")["error"]["message"]

    for value in ("Shifa International", "Monday", "09:00", "13:00"):
        assert value in message
    assert "{" not in message
