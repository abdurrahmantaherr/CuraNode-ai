"""Constants and plain result types for appointment booking (P5)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime

# Which appointment statuses count toward a session's cap and block a time.
# `cancelled` is the only one that frees them.
ACTIVE_STATUSES = ("booked", "checked_in", "done")
# The statuses a patient still "holds": what the overlap and same-day rules look at.
HELD_STATUSES = ("booked", "checked_in")
CHANNEL_ONLINE = "online"
LEAD_MINUTES = 30  # the earliest bookable time is this far from now
HORIZON_DAYS = 14  # and the latest day is this far ahead
SEARCH_LIMIT = 50
DEFAULT_MINUTES = 15  # a held appointment with no schedule row (a later walk-in)
# Why a booking, reschedule or cancel is refused; `errors.booking_<reason>` is the message.
REFUSAL_REASONS = (
    "not_offered",
    "too_soon",
    "too_far",
    "taken",
    "full",
    "overlap",
    "same_day",
    "not_cancellable",
)


@dataclass(frozen=True)
class ClinicRef:
    id: uuid.UUID
    name: str
    city: str | None


@dataclass(frozen=True)
class DoctorCard:
    id: uuid.UUID
    name: str
    specialty: str | None
    clinics: tuple[ClinicRef, ...]


@dataclass(frozen=True)
class DayOptions:
    """The free times on one Pakistan date, as aware UTC instants."""

    day: date
    times: tuple[datetime, ...]


@dataclass(frozen=True)
class AppointmentRow:
    """One of the patient's own appointments. `starts_at` is Pakistan wall-clock;
    `upcoming` is booked/checked-in and not yet started; `can_change` is a
    `booked` appointment that has not started (cancel or reschedule)."""

    id: uuid.UUID
    doctor_id: uuid.UUID
    doctor_name: str
    specialty: str | None
    clinic_id: uuid.UUID
    clinic_name: str
    starts_at: datetime
    status: str
    upcoming: bool
    can_change: bool


@dataclass(frozen=True)
class DayRow:
    """One line of the doctor's day: name, time and status, nothing else."""

    starts_at: datetime
    patient_name: str
    status: str
