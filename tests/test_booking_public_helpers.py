"""P6: the small public wrappers the queue uses from the booking service."""

from __future__ import annotations

import pytest
from app.booking import service
from app.db.models import UserRole, VerificationStatus

from tests.booking_helpers import (
    NOW,
    TODAY,
    WED,
    add_appointment,
    at,
    freeze,
    make_patient,
    verified_doctor,
)
from tests.conftest import add_affiliation, make_user
from tests.queue_helpers import todays_session


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    freeze(monkeypatch)


def test_now_follows_the_frozen_booking_clock():
    assert service.now() == NOW


async def test_is_bookable_only_for_a_verified_doctor_at_a_live_hospital(db, clinic, second_clinic):
    doctor = await verified_doctor(db, clinic)
    assert await service.is_bookable(db, doctor.id, clinic.id) is True
    assert await service.is_bookable(db, doctor.id, second_clinic.id) is False
    doctor.verification_status = VerificationStatus.REJECTED
    doctor.verified_by = None
    doctor.verified_at = None
    await db.commit()
    assert await service.is_bookable(db, doctor.id, clinic.id) is False


async def test_a_pending_doctor_is_not_bookable(db, clinic):
    await make_user(db, email="p@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    assert await service.visible_pairs(db) == []


async def test_visible_pairs_lists_each_live_hospital_by_name(db, clinic, second_clinic):
    doctor = await verified_doctor(db, clinic)
    await add_affiliation(db, doctor.id, second_clinic.id)
    pairs = await service.visible_pairs(db)
    assert [(d.id, c.name) for d, c in pairs] == [
        (doctor.id, "Maroof International"),
        (doctor.id, "Shifa International"),
    ]


async def test_session_windows_returns_the_days_windows(db, clinic):
    doctor, schedule = await todays_session(db, clinic)
    [window] = await service.session_windows(db, doctor.id, clinic.id, TODAY)
    assert window.schedule_id == schedule.id and window.slot_minutes == 15
    assert (window.start.hour, window.end.hour) == (12, 18)


async def test_active_starts_leaves_out_cancelled(db, clinic):
    doctor, schedule = await todays_session(db, clinic)
    _, patient, _ = await make_patient(db)
    live = await add_appointment(db, patient, doctor, clinic, schedule, at(TODAY, "14:00"))
    await add_appointment(
        db, patient, doctor, clinic, schedule, at(TODAY, "14:15"), status="cancelled"
    )
    lo, hi = at(TODAY, "12:00"), at(TODAY, "18:00")
    starts = await service.active_starts(db, schedule.id, lo, hi)
    assert starts == [at(TODAY, "14:00")] and live.status == "booked"
    assert await service.active_starts(db, schedule.id, at(WED, "12:00"), at(WED, "18:00")) == []
