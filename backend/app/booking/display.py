"""Display helpers for the booking pages (P5): small pure functions, no database."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from ..i18n.catalogue import translate
from . import slots
from .schemas import DayRow

STATUS_BADGE = {
    "booked": "badge--ok",
    "checked_in": "badge--ok",
    "done": "badge--neutral",
    "cancelled": "badge--err",
}


def status_label(status: str, locale: str) -> str:
    """The translated name of an appointment status. A status this version does
    not know is shown as stored, never as a catalogue key."""
    key = f"booking.status.{status}"
    text = translate(key, locale)
    return status if text == key else text


def status_badge(status: str) -> str:
    return STATUS_BADGE.get(status, "badge--neutral")


def hhmm(value: datetime) -> str:
    """The Pakistan wall-clock time of a stored instant, `HH:MM`."""
    return slots.to_local(value).strftime("%H:%M")


@dataclass(frozen=True)
class DayRowOut:
    """One line of a doctor's day, ready to draw. Name, time and status only: a
    booking is not consent, so nothing else about the patient is read here."""

    time: str
    name: str
    status: str  # translated
    badge: str  # a CSS class
    cancelled: bool


def build_day_rows(rows: Iterable[DayRow], locale: str) -> list[DayRowOut]:
    return [
        DayRowOut(
            time=hhmm(row.starts_at),
            name=row.patient_name,
            status=status_label(row.status, locale),
            badge=status_badge(row.status),
            cancelled=row.status == "cancelled",
        )
        for row in rows
    ]
