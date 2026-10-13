"""Constants and plain result types for the live queue (P6)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time

STATE_WAITING = "waiting"
STATE_IN_ROOM = "in_room"
STATE_SERVED = "served"
CHANNEL_WALK_IN = "walk_in"
SEARCH_MIN = 3  # the shortest walk-in search text
SEARCH_LIMIT = 8  # the most results a walk-in search returns

# Why a queue action is refused; `errors.queue_<reason>` is the message.
REFUSAL_REASONS = (
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
)


@dataclass(frozen=True)
class SessionKey:
    """Which session a walk-in joins: a schedule row plus the window's start (an
    extra shares its anchor's `schedule_id`, so the start tells them apart)."""

    doctor_id: uuid.UUID
    clinic_id: uuid.UUID
    schedule_id: uuid.UUID
    start: time

    def encode(self) -> str:
        return f"{self.doctor_id}:{self.clinic_id}:{self.schedule_id}:{self.start.strftime('%H%M')}"

    @classmethod
    def decode(cls, value: str) -> SessionKey | None:
        try:
            doctor, clinic, schedule, hhmm = value.split(":")
            return cls(
                uuid.UUID(doctor),
                uuid.UUID(clinic),
                uuid.UUID(schedule),
                time(int(hhmm[:2]), int(hhmm[2:])),
            )
        except ValueError:
            return None


@dataclass(frozen=True)
class Line:
    """One patient in one session. `state` is `expected` (booked, not arrived),
    `waiting`, `in_room` or `served`. `booked_at` is Pakistan wall-clock: the
    booked time, or a walk-in's arrival."""

    appointment_id: uuid.UUID
    entry_id: uuid.UUID | None
    patient_name: str
    state: str
    token: int | None
    booked_at: datetime
    walk_in: bool


@dataclass(frozen=True)
class SessionView:
    """One doctor session today, with its patients grouped by queue state."""

    key: SessionKey
    doctor_name: str
    clinic_name: str
    day: date
    start: time
    end: time
    slot_minutes: int
    cap: int
    used: int
    in_room: Line | None
    waiting: tuple[Line, ...]
    expected: tuple[Line, ...]
    seen: tuple[Line, ...]


@dataclass(frozen=True)
class PatientHit:
    """A walk-in search result: the passport number is masked."""

    patient_id: uuid.UUID
    name: str
    passport: str


@dataclass(frozen=True)
class QueueStatus:
    """What a patient sees about their own place. `state` is `expected`,
    `waiting`, `next`, `in_room` or `served`; `live` says the page should keep
    refreshing itself."""

    state: str
    position: int | None
    wait_minutes: int | None
    waiting_total: int
    live: bool
