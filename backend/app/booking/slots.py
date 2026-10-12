"""Pure booking logic (spec 3.1, 3.2): the time grid, the session cap, the window.

No database and no clock: callers pass the data and "now". Times that cross
the boundary are aware UTC instants; wall-clock times are Pakistan time
(`PKT`), the same zone D7 uses. SQLite hands stored timestamps back naive, so
every value read from the database goes through `as_utc` first.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from ..db.types import PKT


@dataclass(frozen=True)
class Window:
    """One bookable session window on one date, as D7 reports it, plus the
    doctor's minutes per patient. `schedule_id` is the weekly slot (an extra's
    anchor); each window has its own cap and its own count."""

    schedule_id: uuid.UUID
    clinic_id: uuid.UUID
    day: date
    start: time
    end: time
    slot_minutes: int
    max_tokens: int


# ── Time ─────────────────────────────────────────────────────────────────
def to_utc(day: date, wall: time) -> datetime:
    """The aware UTC instant of a Pakistan wall-clock time on `day`."""
    return datetime.combine(day, wall, tzinfo=PKT).astimezone(UTC)


def as_utc(value: datetime) -> datetime:
    """`value` as an aware UTC instant. A naive value (SQLite) is already UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def to_local(value: datetime) -> datetime:
    return as_utc(value).astimezone(PKT)


def parse_local(value: str) -> datetime:
    """`2026-10-14T10:30` (Pakistan time) as an aware UTC instant. Anything else,
    seconds included, is a `ValueError`."""
    naive = datetime.strptime(value, "%Y-%m-%dT%H:%M")  # noqa: DTZ007 - made aware below
    try:
        return naive.replace(tzinfo=PKT).astimezone(UTC)
    except OverflowError:  # the first hours of year 1 have no UTC equivalent
        raise ValueError(value) from None


def format_local(value: datetime) -> str:
    return to_local(value).strftime("%Y-%m-%dT%H:%M")


# ── The grid ─────────────────────────────────────────────────────────────
def grid_times(window: Window) -> list[time]:
    """`start + n × slot_minutes`, each whole slot ending by the window's end.
    Leftover minutes are not a whole turn (D7 R12)."""
    found: list[time] = []
    cursor = datetime.combine(window.day, window.start)
    end = datetime.combine(window.day, window.end)
    step = timedelta(minutes=window.slot_minutes)
    while cursor + step <= end:
        found.append(cursor.time())
        cursor += step
    return found


def window_bounds(window: Window) -> tuple[datetime, datetime]:
    return to_utc(window.day, window.start), to_utc(window.day, window.end)


def in_window(window: Window, slot_start: datetime) -> bool:
    local = to_local(slot_start)
    return local.date() == window.day and window.start <= local.time() < window.end


# ── What may be booked ───────────────────────────────────────────────────
def refusal(
    window: Window,
    booked: Sequence[datetime],
    slot_start: datetime,
    *,
    earliest: datetime,
    last_day: date,
) -> str | None:
    """None when `slot_start` may be booked in `window`, else why not:
    `not_offered`, `too_soon`, `too_far`, `taken` or `full`. `booked` holds the
    starts of the active appointments already in this window."""
    start = as_utc(slot_start)
    local = start.astimezone(PKT)
    if local.date() != window.day or local.time() not in grid_times(window):
        return "not_offered"
    if start < earliest:
        return "too_soon"
    if window.day > last_day:
        return "too_far"
    if start in {as_utc(b) for b in booked}:
        return "taken"
    if len(booked) >= window.max_tokens:
        return "full"
    return None


def bookable_times(
    window: Window, booked: Sequence[datetime], *, earliest: datetime, last_day: date
) -> list[datetime]:
    """The free grid times of `window`, as aware UTC instants."""
    if window.day > last_day or len(booked) >= window.max_tokens:
        return []
    taken = {as_utc(b) for b in booked}
    return [
        start
        for wall in grid_times(window)
        if (start := to_utc(window.day, wall)) >= earliest and start not in taken
    ]


# ── A patient's own appointments ─────────────────────────────────────────
def overlap(a_start: datetime, a_minutes: int, b_start: datetime, b_minutes: int) -> bool:
    """Half-open: back-to-back appointments do not overlap (D7's convention)."""
    a_end = a_start + timedelta(minutes=a_minutes)
    b_end = b_start + timedelta(minutes=b_minutes)
    return a_start < b_end and b_start < a_end


def same_local_day(a: datetime, b: datetime) -> bool:
    return to_local(a).date() == to_local(b).date()
