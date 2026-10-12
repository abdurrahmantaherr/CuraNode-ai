"""P5 booking: the pure slot logic. No database, no clock."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta

import pytest
from app.booking import slots
from app.booking.slots import Window

D = date(2026, 10, 14)  # a Wednesday
NOW = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)  # Monday 13:00 in Pakistan
EARLIEST = NOW + timedelta(minutes=30)
LAST_DAY = date(2026, 10, 26)


def window(start="09:00", end="13:00", minutes=15, cap=16, day=D) -> Window:
    return Window(
        schedule_id=uuid.uuid4(),
        clinic_id=uuid.uuid4(),
        day=day,
        start=time.fromisoformat(start),
        end=time.fromisoformat(end),
        slot_minutes=minutes,
        max_tokens=cap,
    )


def at(day: date, hhmm: str) -> datetime:
    return slots.to_utc(day, time.fromisoformat(hhmm))


# ── The grid ─────────────────────────────────────────────────────────────
def test_grid_is_start_plus_n_times_slot_minutes():
    got = slots.grid_times(window())
    assert len(got) == 16
    assert got[0] == time(9, 0) and got[1] == time(9, 15) and got[-1] == time(12, 45)


def test_leftover_minutes_are_not_a_slot():
    assert len(slots.grid_times(window(end="13:10"))) == 16


def test_a_window_shorter_than_one_slot_has_no_times():
    assert slots.grid_times(window(end="09:10")) == []


def test_the_last_slot_must_end_by_the_window_end():
    got = slots.grid_times(window(start="09:00", end="09:45", minutes=20))
    assert got == [time(9, 0), time(9, 20)]


# ── Time conversion ──────────────────────────────────────────────────────
def test_pakistan_wall_time_converts_to_utc():
    assert at(D, "10:00") == datetime(2026, 10, 14, 5, 0, tzinfo=UTC)


def test_a_time_after_midnight_in_pakistan_is_the_previous_day_in_utc():
    # Review focus 2.
    assert at(date(2026, 10, 15), "00:30") == datetime(2026, 10, 14, 19, 30, tzinfo=UTC)


def test_naive_values_from_sqlite_are_read_as_utc():
    naive = datetime(2026, 10, 14, 5, 0)  # noqa: DTZ001 - SQLite hands back naive stamps
    assert slots.as_utc(naive) == at(D, "10:00")


def test_to_local_gives_the_pakistan_wall_clock():
    local = slots.to_local(datetime(2026, 10, 14, 19, 30, tzinfo=UTC))
    assert local.date() == date(2026, 10, 15) and local.time() == time(0, 30)


def test_parse_and_format_local_round_trip():
    value = "2026-10-14T10:30"
    parsed = slots.parse_local(value)
    assert parsed == at(D, "10:30")
    assert slots.format_local(parsed) == value


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "nonsense",
        "2026-10-14",
        "2026-10-14T10:30:00",
        "2026-13-01T10:00",
        "0001-01-01T00:00",  # before UTC year 1 once Pakistan time is converted
        "0001-01-01T04:59",
    ],
)
def test_parse_local_rejects_anything_else(bad):
    with pytest.raises(ValueError):
        slots.parse_local(bad)


# ── Window membership ────────────────────────────────────────────────────
def test_window_bounds_are_the_utc_instants_of_start_and_end():
    lo, hi = slots.window_bounds(window())
    assert lo == at(D, "09:00") and hi == at(D, "13:00")


def test_in_window_is_half_open():
    w = window()
    assert slots.in_window(w, at(D, "09:00"))
    assert slots.in_window(w, at(D, "12:59"))
    assert not slots.in_window(w, at(D, "13:00"))
    assert not slots.in_window(w, at(D, "08:59"))


def test_in_window_uses_the_pakistan_day_not_the_utc_day():
    w = window(start="00:00", end="01:00", day=date(2026, 10, 15))
    assert slots.in_window(w, at(date(2026, 10, 15), "00:15"))  # 19:15 UTC the day before
    assert not slots.in_window(w, at(date(2026, 10, 14), "00:15"))


# ── Bookable times ───────────────────────────────────────────────────────
def test_an_empty_session_offers_the_whole_grid():
    assert len(slots.bookable_times(window(), [], earliest=EARLIEST, last_day=LAST_DAY)) == 16


def test_taken_times_are_removed():
    booked = [at(D, "09:00"), at(D, "09:15")]
    got = slots.bookable_times(window(), booked, earliest=EARLIEST, last_day=LAST_DAY)
    assert len(got) == 14 and at(D, "09:00") not in got and at(D, "09:30") in got


def test_a_full_session_offers_nothing():
    w = window(cap=2)
    booked = [at(D, "09:00"), at(D, "09:15")]
    assert slots.bookable_times(w, booked, earliest=EARLIEST, last_day=LAST_DAY) == []


def test_times_before_the_lead_are_hidden_and_the_boundary_is_included():
    got = slots.bookable_times(window(), [], earliest=at(D, "09:30"), last_day=LAST_DAY)
    assert got[0] == at(D, "09:30") and len(got) == 14


def test_nothing_beyond_the_last_day():
    assert slots.bookable_times(window(), [], earliest=EARLIEST, last_day=date(2026, 10, 13)) == []


def test_booked_values_may_be_naive():
    naive = datetime(2026, 10, 14, 4, 0)  # noqa: DTZ001 - SQLite hands back naive stamps
    got = slots.bookable_times(window(), [naive], earliest=EARLIEST, last_day=LAST_DAY)
    assert at(D, "09:00") not in got and len(got) == 15


# ── Refusal reasons ──────────────────────────────────────────────────────
def refuse(w, start, booked=(), earliest=EARLIEST, last_day=LAST_DAY):
    return slots.refusal(w, list(booked), start, earliest=earliest, last_day=last_day)


def test_a_good_time_has_no_refusal():
    assert refuse(window(), at(D, "10:00")) is None


@pytest.mark.parametrize(
    ("start", "expected"),
    [
        (at(D, "10:07"), "not_offered"),  # off the grid
        (at(D, "13:00"), "not_offered"),  # after the window
        (at(date(2026, 10, 15), "10:00"), "not_offered"),  # another day
    ],
)
def test_times_the_session_does_not_offer(start, expected):
    assert refuse(window(), start) == expected


def test_too_soon():
    assert refuse(window(), at(D, "09:00"), earliest=at(D, "09:30")) == "too_soon"


def test_too_far():
    assert refuse(window(), at(D, "10:00"), last_day=date(2026, 10, 13)) == "too_far"


def test_taken():
    assert refuse(window(), at(D, "10:00"), booked=[at(D, "10:00")]) == "taken"


def test_full():
    w = window(cap=2)
    assert refuse(w, at(D, "10:00"), booked=[at(D, "09:00"), at(D, "09:15")]) == "full"


def test_taken_is_reported_before_full():
    w = window(cap=1)
    assert refuse(w, at(D, "09:00"), booked=[at(D, "09:00")]) == "taken"


# ── Overlap and day ──────────────────────────────────────────────────────
def test_overlap_is_half_open():
    assert slots.overlap(at(D, "09:00"), 15, at(D, "09:15"), 15) is False  # back to back
    assert slots.overlap(at(D, "09:00"), 30, at(D, "09:15"), 15) is True
    assert slots.overlap(at(D, "09:00"), 15, at(D, "09:00"), 15) is True


def test_same_local_day_uses_pakistan_days():
    assert slots.same_local_day(at(D, "00:15"), at(D, "23:45")) is True
    assert slots.same_local_day(at(D, "23:45"), at(date(2026, 10, 15), "00:15")) is False
