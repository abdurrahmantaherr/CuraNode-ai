"""Display helpers for the booking pages (P5): small pure functions, no database."""

from __future__ import annotations

from datetime import datetime

from ..i18n.catalogue import translate
from . import slots

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
