"""P6: the queue constants and the session key."""

from __future__ import annotations

import uuid
from datetime import time

import pytest
from app.queueing import schemas
from app.queueing.schemas import SessionKey


def key() -> SessionKey:
    return SessionKey(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), time(14, 5))


def test_a_session_key_round_trips():
    original = key()
    assert SessionKey.decode(original.encode()) == original


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "junk",
        "a:b:c:d",
        "a:b:c",
        f"{uuid.uuid4()}:{uuid.uuid4()}:{uuid.uuid4()}:2561",
        f"{uuid.uuid4()}:{uuid.uuid4()}:{uuid.uuid4()}:9",
    ],
)
def test_a_tampered_session_key_is_none(bad):
    assert SessionKey.decode(bad) is None


def test_the_constants():
    assert (schemas.STATE_WAITING, schemas.STATE_IN_ROOM, schemas.STATE_SERVED) == (
        "waiting",
        "in_room",
        "served",
    )
    assert schemas.CHANNEL_WALK_IN == "walk_in"
    assert (schemas.SEARCH_MIN, schemas.SEARCH_LIMIT) == (3, 8)


def test_the_refusal_reasons_are_the_ones_the_service_uses():
    assert set(schemas.REFUSAL_REASONS) == {
        "not_today",
        "not_booked",
        "session_over",
        "doctor_unavailable",
        "room_occupied",
        "session_full",
        "has_booking_today",
        "already_queued",
        "not_waiting",
        "not_in_room",
        "search_too_short",
        "try_again",
    }
