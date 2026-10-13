"""P6: the refusal error, the desk's rate limits and the audit constants."""

from __future__ import annotations

import uuid

import pytest
from app.audit import writer as audit
from app.deps import Actor, check_queue_lookup_rate, check_queue_write_rate
from app.errors import QueueRefused, RateLimited, envelope
from app.i18n.catalogue import translate
from app.queueing.schemas import REFUSAL_REASONS
from app.settings import settings


@pytest.mark.parametrize("reason", REFUSAL_REASONS)
def test_every_refusal_is_a_409_with_a_translated_message(reason):
    exc = QueueRefused(reason)
    assert exc.http_status == 409 and exc.reason == reason
    assert exc.message_key == f"errors.queue_{reason}"

    body = envelope(exc, "req-1", "en")["error"]
    assert body["code"] == "QUEUE_REFUSED" and body["retryable"] is False
    assert body["details"] == {"reason": reason}
    assert body["message"] != body["message_key"] and "{" not in body["message"]

    urdu = envelope(exc, "req-1", "ur")["error"]["message"]
    assert urdu != body["message"] and urdu != body["message_key"]


def test_the_room_message_tells_the_desk_what_to_do():
    assert translate("errors.queue_room_occupied", "en") == (
        "Someone is already in the room. Mark them seen first."
    )


def _admin(user_id: uuid.UUID) -> Actor:
    return Actor(user_id=user_id, role="admin")


def test_the_default_limits():
    assert settings.queue_write_rate_limit_per_minute == 120
    assert settings.queue_lookup_rate_limit_per_minute == 30


async def test_the_write_budget_trips_after_the_limit(monkeypatch):
    monkeypatch.setattr(settings, "queue_write_rate_limit_per_minute", 3)
    actor = _admin(uuid.uuid4())
    for _ in range(3):
        await check_queue_write_rate(actor)
    with pytest.raises(RateLimited) as exc:
        await check_queue_write_rate(actor)
    assert exc.value.retry_after_s == 60


async def test_the_lookup_budget_is_separate_from_the_write_budget(monkeypatch):
    monkeypatch.setattr(settings, "queue_write_rate_limit_per_minute", 1)
    monkeypatch.setattr(settings, "queue_lookup_rate_limit_per_minute", 2)
    actor = _admin(uuid.uuid4())
    await check_queue_write_rate(actor)
    with pytest.raises(RateLimited):
        await check_queue_write_rate(actor)
    await check_queue_lookup_rate(actor)
    await check_queue_lookup_rate(actor)
    with pytest.raises(RateLimited):
        await check_queue_lookup_rate(actor)


async def test_the_budgets_are_per_person(monkeypatch):
    monkeypatch.setattr(settings, "queue_lookup_rate_limit_per_minute", 1)
    first, second = _admin(uuid.uuid4()), _admin(uuid.uuid4())
    await check_queue_lookup_rate(first)
    await check_queue_lookup_rate(second)
    with pytest.raises(RateLimited):
        await check_queue_lookup_rate(first)


def test_audit_action_names():
    assert audit.QUEUE_CHECK_IN == "queue.check_in"
    assert audit.QUEUE_SEND_IN == "queue.send_in"
    assert audit.QUEUE_SEEN == "queue.seen"
    assert audit.QUEUE_PUT_BACK == "queue.put_back"
    assert audit.QUEUE_MOVE == "queue.move"
    assert audit.QUEUE_WALK_IN == "queue.walk_in"
    assert audit.QUEUE_FIND_PATIENT == "queue.find_patient"
