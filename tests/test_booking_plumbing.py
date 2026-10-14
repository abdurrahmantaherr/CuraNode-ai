"""P5: the refusal error, the write budget and the audit constants."""

from __future__ import annotations

import uuid

import pytest
from app.audit import writer as audit
from app.booking.schemas import REFUSAL_REASONS
from app.deps import Actor, check_booking_write_rate
from app.errors import BookingRefused, RateLimited, envelope
from app.i18n.catalogue import translate
from app.settings import settings


@pytest.mark.parametrize("reason", REFUSAL_REASONS)
def test_every_refusal_is_a_409_with_a_translated_message(reason):
    exc = BookingRefused(reason)
    assert exc.http_status == 409 and exc.reason == reason
    assert exc.message_key == f"errors.booking_{reason}"

    body = envelope(exc, "req-1", "en")["error"]
    assert body["code"] == "BOOKING_REFUSED" and body["retryable"] is False
    assert body["details"] == {"reason": reason}
    assert body["message"] != body["message_key"] and "{" not in body["message"]

    urdu = envelope(exc, "req-1", "ur")["error"]["message"]
    assert urdu != body["message"] and urdu != body["message_key"]


def test_the_reasons_are_the_ones_the_slot_logic_and_service_use():
    assert set(REFUSAL_REASONS) == {
        "not_offered",
        "too_soon",
        "too_far",
        "taken",
        "full",
        "overlap",
        "same_day",
        "not_cancellable",
    }


def test_the_taken_message_matches_the_spec():
    assert translate("errors.booking_taken", "en") == (
        "Someone just booked that time. Please pick another."
    )


def _patient_actor(user_id: uuid.UUID) -> Actor:
    return Actor(user_id=user_id, role="patient")


def test_booking_write_budget_default_is_20_per_minute():
    assert settings.booking_write_rate_limit_per_minute == 20


async def test_booking_write_budget_trips_after_the_limit(monkeypatch):
    monkeypatch.setattr(settings, "booking_write_rate_limit_per_minute", 3)
    actor = _patient_actor(uuid.uuid4())
    for _ in range(3):
        await check_booking_write_rate(actor)
    with pytest.raises(RateLimited) as exc:
        await check_booking_write_rate(actor)
    assert exc.value.retry_after_s == 60


async def test_booking_write_budget_is_per_patient(monkeypatch):
    monkeypatch.setattr(settings, "booking_write_rate_limit_per_minute", 1)
    first, second = _patient_actor(uuid.uuid4()), _patient_actor(uuid.uuid4())
    await check_booking_write_rate(first)
    await check_booking_write_rate(second)
    with pytest.raises(RateLimited):
        await check_booking_write_rate(first)


def test_audit_action_names():
    assert audit.APPOINTMENT_BOOK == "appointment.book"
    assert audit.APPOINTMENT_CANCEL == "appointment.cancel"
    assert audit.APPOINTMENT_RESCHEDULE == "appointment.reschedule"
