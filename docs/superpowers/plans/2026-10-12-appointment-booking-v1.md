# Appointment Booking v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A patient finds a verified doctor, sees real open times, books, cancels or reschedules an appointment, and the doctor sees their day's appointments.

**Architecture:** A new `booking` package holds pure slot logic (`slots.py`) and the service (`service.py`) that consumes D7's `get_effective_availability` and `lock_doctor`. Pages live in their own router module `web/booking.py`. No new table: one additive partial unique index on the existing `appointment` table is the database backstop behind the per-doctor lock.

**Tech Stack:** FastAPI, SQLAlchemy 2 (async), Jinja2, Alembic (hand-written, additive), pytest (`asyncio_mode = "auto"`, in-memory SQLite, `FakeSupabaseAuth`), ruff.

**Spec:** `docs/superpowers/specs/2026-10-12-appointment-booking-design.md`

## Global Constraints

- Branch `feature/appointment-booking`, from `dev`. Never push, merge, or run the migration against the shared Supabase project without Zarwa's go-ahead.
- Booking window: from **30 minutes from now** up to **14 days ahead**; cancel or reschedule **any time before the appointment starts**. All wall-clock times are Pakistan time (`dbtypes.PKT`); `slot_start` is stored as an aware UTC instant.
- A time is `session start + n × slot_minutes`, and the whole slot must fit inside the session.
- "Active" means `booked`, `checked_in` or `done`; `cancelled` frees both the time and the cap.
- Every appointment sets `schedule_id` and `booking_channel = "online"`.
- Only **verified** doctors with at least one **live** hospital are searchable or bookable.
- The doctor sees **name, time and status only**: no passport number, nothing clinical. Booking never grants record access.
- Reschedule is one transaction: a failed rebook leaves the old booking untouched.
- A foreign or nonexistent appointment id is one identical `NotFound`. Audit rows hold ids only.
- No new table. One hand-written, additive migration (the partial unique index); `tests/test_privacy_access.py`'s migration-chain guard must match the new head.
- Every user-facing string is an i18n key present in both `en.json` and `ur.json` (`missing_keys()` stays empty). Every action works without JavaScript (post, then redirect).
- Pages go in `backend/app/web/booking.py`; edits to `web/router.py`, `models.py`, `main.py` and the i18n files stay small and additive (teammates are editing them).
- Add no CSS: reuse existing classes (a repo test forbids raw hex colours in `app.css`).
- Style: ruff-clean (`uv run ruff check backend tests` and `uv run ruff format backend tests`). Run tests with `uv run pytest -q`.
- Commit messages end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

Inputs the spec implies but its numbered tests do not obviously cover. Each has a test in the task named.

1. **Reschedule to another time with the same doctor on the same day** must succeed: the old booking is excluded from the overlap and same-day checks. [Task 6]
2. **A time just after midnight in Pakistan** (its UTC date is the day before) is listed under its Pakistan date, is bookable, and appears in the doctor's list for that date. [Tasks 4, 5, 6]
3. **A time removed after the patient loaded the page** (time off added meanwhile) is refused as not offered and nothing is booked. [Task 5]
4. **An extra session on the same date as the weekly window** has its own cap and its own count: filling the weekly window does not fill the extra. [Task 5]
5. **A doctor un-verified after the patient opened the page** cannot be booked (`NotFound`), and **cancelling then rebooking the same time** works (the partial index ignores cancelled rows). [Task 5]

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/booking/__init__.py` (new) | Package marker. |
| `backend/app/booking/schemas.py` (new) | Constants (active statuses, limits) and the plain result types the service returns. |
| `backend/app/booking/slots.py` (new) | **Pure logic, no database, no clock.** Grid, window membership, refusal reasons, bookable times, overlap, time conversion. |
| `backend/app/booking/service.py` (new) | Search, options, book, reschedule, cancel, the patient's lists, the doctor's day. Owns every rule. |
| `backend/app/booking/display.py` (new) | Pure display helpers: status labels and badges, Pakistan `HH:MM`, the doctor's day rows (name, time, status only). |
| `backend/app/web/booking.py` (new) | The patient and doctor pages. |
| `backend/app/errors.py` (modify) | `BookingRefused`. |
| `backend/app/settings.py`, `backend/app/deps.py` (modify) | `booking_write_rate_limit_per_minute`, `check_booking_write_rate`. |
| `backend/app/audit/writer.py` (modify) | Three action constants. |
| `backend/app/db/models.py` (modify) | The partial unique index on `Appointment`. |
| `alembic/versions/92b61c6e9db4_booking_active_slot_index.py` (new) | The index, additive. |
| `backend/app/main.py` (modify) | Include the router. |
| `backend/app/web/router.py` (modify) | Nav entries; the doctor dashboard panel. |
| `frontend/templates/booking/*.html` (new), `frontend/templates/doctor/_appointments_today.html` (new), `frontend/templates/shell.html` (modify) | Pages, and the "Appointments today" panel on the doctor home page. |
| `CLAUDE.md` (modify) | Describe the feature, its rules and its commands (Task 11). |
| `backend/app/i18n/messages/en.json`, `ur.json` (modify) | New keys per task. |
| `tests/booking_helpers.py` (new) | Shared setup: frozen clock, verified doctor with a session, patients. |
| `tests/test_booking_*.py` (new) | One file per task. |

**i18n helper used by several tasks.** Each task that adds keys runs this snippet with its own `EN` and `UR` dicts. It refuses to overwrite an existing key and keeps each file's line endings:

```bash
uv run python - <<'EOF'
import json, pathlib

EN = {}  # filled in by the task
UR = {}  # filled in by the task

for loc, pairs in (("en", EN), ("ur", UR)):
    p = pathlib.Path(f"backend/app/i18n/messages/{loc}.json")
    raw = p.read_text(encoding="utf8")
    clash = set(pairs) & set(json.loads(raw))
    assert not clash, sorted(clash)
    nl = "\r\n" if "\r\n" in raw else "\n"
    body = raw.rstrip()
    assert body.endswith("}")
    body = body[:-1].rstrip()
    add = "".join(
        f",{nl}  {json.dumps(k, ensure_ascii=False)}: {json.dumps(v, ensure_ascii=False)}"
        for k, v in pairs.items()
    )
    p.write_bytes((body + add + nl + "}" + nl).encode("utf8"))
    json.loads(p.read_text(encoding="utf8"))
print("ok")
EOF
```

---

### Task 1: Pure slot logic

**Files:**
- Create: `backend/app/booking/__init__.py`
- Create: `backend/app/booking/schemas.py`
- Create: `backend/app/booking/slots.py`
- Test: `tests/test_booking_slots.py`

**Interfaces:**
- Produces (`schemas.py`): `ACTIVE_STATUSES = ("booked", "checked_in", "done")`, `HELD_STATUSES = ("booked", "checked_in")`, `CHANNEL_ONLINE = "online"`, `LEAD_MINUTES = 30`, `HORIZON_DAYS = 14`, `SEARCH_LIMIT = 50`, `DEFAULT_MINUTES = 15`, and frozen dataclasses `ClinicRef(id, name, city)`, `DoctorCard(id, name, specialty, clinics)`, `DayOptions(day, times)`, `AppointmentRow(id, doctor_id, doctor_name, specialty, clinic_id, clinic_name, starts_at, status, upcoming, can_change)`, `DayRow(starts_at, patient_name, status)`.
- Produces (`slots.py`): `Window(schedule_id, clinic_id, day, start, end, slot_minutes, max_tokens)`; `to_utc(day: date, wall: time) -> datetime`; `as_utc(value: datetime) -> datetime`; `to_local(value: datetime) -> datetime`; `grid_times(window) -> list[time]`; `window_bounds(window) -> tuple[datetime, datetime]`; `in_window(window, slot_start) -> bool`; `refusal(window, booked, slot_start, *, earliest, last_day) -> str | None`; `bookable_times(window, booked, *, earliest, last_day) -> list[datetime]`; `overlap(a_start, a_minutes, b_start, b_minutes) -> bool`; `same_local_day(a, b) -> bool`; `parse_local(value: str) -> datetime`; `format_local(value: datetime) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_booking_slots.py`:

```python
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
    assert slots.as_utc(datetime(2026, 10, 14, 5, 0)) == at(D, "10:00")


def test_to_local_gives_the_pakistan_wall_clock():
    local = slots.to_local(datetime(2026, 10, 14, 19, 30, tzinfo=UTC))
    assert local.date() == date(2026, 10, 15) and local.time() == time(0, 30)


def test_parse_and_format_local_round_trip():
    value = "2026-10-14T10:30"
    parsed = slots.parse_local(value)
    assert parsed == at(D, "10:30")
    assert slots.format_local(parsed) == value


@pytest.mark.parametrize("bad", ["", "nonsense", "2026-10-14", "2026-10-14T10:30:00", "2026-13-01T10:00"])
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
    got = slots.bookable_times(
        window(), [datetime(2026, 10, 14, 4, 0)], earliest=EARLIEST, last_day=LAST_DAY
    )
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_booking_slots.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'app.booking'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/booking/__init__.py`:

```python
"""Appointment booking (P5, FR15, FR16)."""
```

Create `backend/app/booking/schemas.py`:

```python
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
```

Create `backend/app/booking/slots.py`:

```python
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
    naive = datetime.strptime(value, "%Y-%m-%dT%H:%M")
    return naive.replace(tzinfo=PKT).astimezone(UTC)


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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_booking_slots.py -q`
Expected: all pass (about 35).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check backend tests && uv run ruff format backend tests
git add backend/app/booking tests/test_booking_slots.py
git commit -m "feat(booking): add the pure slot logic and result types"
```

---

### Task 2: The database safety index, and the shared test helpers

**Files:**
- Modify: `backend/app/db/models.py` (the `Appointment` class's `__table_args__`)
- Create: `alembic/versions/92b61c6e9db4_booking_active_slot_index.py`
- Create: `tests/booking_helpers.py`
- Test: `tests/test_booking_index.py`, `tests/test_booking_migration.py`
- Modify: `tests/test_privacy_access.py` (the migration-chain guard)

**Interfaces:**
- Consumes: `slots.to_utc` (Task 1).
- Produces (`tests/booking_helpers.py`): `NOW`, `TODAY`, `MONDAY`, `WEDNESDAY`, `THURSDAY`, `WED` (the first Wednesday after `NOW`, 2026-10-14); `freeze(monkeypatch, now=NOW)`; `at(day, "HH:MM") -> datetime`; `async verified_doctor(db, clinic, *, email, name, specialty) -> Doctor`; `async add_session(db, doctor, clinic, *, weekday, start, end, minutes, cap) -> DoctorSchedule`; `async make_patient(db, *, email, name) -> tuple[Profile, Patient, Actor]`; `async add_appointment(db, patient, doctor, clinic, schedule, starts_at, status="booked") -> Appointment`.

- [ ] **Step 1: Write the helpers and the failing tests**

Create `tests/booking_helpers.py`:

```python
"""Shared setup for the booking tests (P5): a frozen clock, a verified doctor
with a weekly session, and patients. Helpers rather than fixtures, so each test
file decides what it needs."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

from app.booking import slots
from app.db import types as dbtypes
from app.db.models import (
    Appointment,
    Doctor,
    DoctorAffiliation,
    DoctorSchedule,
    Patient,
    Profile,
    UserRole,
)
from app.deps import Actor
from sqlalchemy import select

from tests.conftest import make_user

NOW = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)  # Monday 13:00 in Pakistan
TODAY = date(2026, 10, 12)
MONDAY, WEDNESDAY, THURSDAY = 0, 2, 3
WED = date(2026, 10, 14)  # the first Wednesday after NOW


def freeze(monkeypatch, now: datetime = NOW) -> None:
    """Freeze the booking clock and D7's `today` together."""
    monkeypatch.setattr("app.booking.service._now", lambda: now)
    monkeypatch.setattr(dbtypes, "today_pk", lambda: now.astimezone(dbtypes.PKT).date())


def at(day: date, hhmm: str) -> datetime:
    """The aware UTC instant of a Pakistan wall-clock time (what `slot_start` holds)."""
    return slots.to_utc(day, time.fromisoformat(hhmm))


async def verified_doctor(
    db, clinic, *, email="booking.dr@x.com", name="Dr Booking", specialty="Cardiology"
) -> Doctor:
    admin = await make_user(
        db, email=f"admin.{email}", role=UserRole.CLINIC_ADMIN, clinic_id=clinic.id
    )
    user = await make_user(
        db,
        email=email,
        role=UserRole.DOCTOR,
        clinic_id=clinic.id,
        is_verified=True,
        verifier_id=admin.id,
    )
    doctor = (await db.execute(select(Doctor).where(Doctor.user_id == user.id))).scalar_one()
    doctor.full_name = name
    doctor.specialty = specialty
    await db.commit()
    return doctor


async def add_session(
    db,
    doctor: Doctor,
    clinic,
    *,
    weekday: int = WEDNESDAY,
    start: str = "09:00",
    end: str = "13:00",
    minutes: int = 15,
    cap: int = 16,
) -> DoctorSchedule:
    affiliation = (
        await db.execute(
            select(DoctorAffiliation).where(
                DoctorAffiliation.doctor_id == doctor.id, DoctorAffiliation.clinic_id == clinic.id
            )
        )
    ).scalar_one()
    row = DoctorSchedule(
        affiliation_id=affiliation.id,
        weekday=weekday,
        start_time=time.fromisoformat(start),
        end_time=time.fromisoformat(end),
        slot_minutes=minutes,
        max_tokens=cap,
        status="active",
    )
    db.add(row)
    await db.commit()
    return row


async def make_patient(
    db, *, email: str = "booking.pat@x.com", name: str = "Pat Booking"
) -> tuple[Profile, Patient, Actor]:
    user = await make_user(db, email=email, role=UserRole.PATIENT)
    patient = (await db.execute(select(Patient).where(Patient.user_id == user.id))).scalar_one()
    patient.full_name = name
    await db.commit()
    return user, patient, Actor(user_id=user.id, role="patient")


async def add_appointment(
    db, patient, doctor, clinic, schedule, starts_at: datetime, status: str = "booked"
) -> Appointment:
    row = Appointment(
        patient_id=patient.id,
        doctor_id=doctor.id,
        clinic_id=clinic.id,
        schedule_id=schedule.id,
        slot_start=starts_at,
        status=status,
        booking_channel="online",
    )
    db.add(row)
    await db.commit()
    return row
```

Create `tests/test_booking_index.py`:

```python
"""P5: the database refuses two active appointments at one time (the backstop
behind the per-doctor lock). The table has no other uniqueness."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from tests.booking_helpers import (
    WED,
    add_appointment,
    add_session,
    at,
    make_patient,
    verified_doctor,
)


async def setup(db, clinic):
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic)
    _, first, _ = await make_patient(db, email="a@x.com", name="A")
    _, second, _ = await make_patient(db, email="b@x.com", name="B")
    return doctor, session, first, second


async def test_two_booked_appointments_at_one_time_are_refused(db, clinic):
    doctor, session, first, second = await setup(db, clinic)
    await add_appointment(db, first, doctor, clinic, session, at(WED, "10:00"))
    with pytest.raises(IntegrityError):
        await add_appointment(db, second, doctor, clinic, session, at(WED, "10:00"))


async def test_a_done_appointment_still_holds_its_time(db, clinic):
    doctor, session, first, second = await setup(db, clinic)
    await add_appointment(db, first, doctor, clinic, session, at(WED, "10:00"), status="done")
    with pytest.raises(IntegrityError):
        await add_appointment(db, second, doctor, clinic, session, at(WED, "10:00"))


async def test_a_cancelled_appointment_frees_its_time(db, clinic):
    doctor, session, first, second = await setup(db, clinic)
    await add_appointment(db, first, doctor, clinic, session, at(WED, "10:00"), status="cancelled")
    await add_appointment(db, second, doctor, clinic, session, at(WED, "10:00"))


async def test_cancelled_rows_may_repeat_a_time(db, clinic):
    doctor, session, first, second = await setup(db, clinic)
    await add_appointment(db, first, doctor, clinic, session, at(WED, "10:00"), status="cancelled")
    await add_appointment(db, second, doctor, clinic, session, at(WED, "10:00"), status="cancelled")


async def test_different_times_do_not_clash(db, clinic):
    doctor, session, first, second = await setup(db, clinic)
    await add_appointment(db, first, doctor, clinic, session, at(WED, "10:00"))
    await add_appointment(db, second, doctor, clinic, session, at(WED, "10:15"))
```

Create `tests/test_booking_migration.py`:

```python
"""P5: the hand-written migration 92b61c6e9db4, exercised on SQLite only. It is
never run against the shared database here."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "92b61c6e9db4_booking_active_slot_index.py"
INDEX = "uq_appointment_active_slot"


def _load():
    spec = importlib.util.spec_from_file_location("booking_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _engine():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE appointment (appointment_id VARCHAR(32) PRIMARY KEY, "
                "schedule_id VARCHAR(32), slot_start DATETIME NOT NULL, status VARCHAR(20) NOT NULL)"
            )
        )
    return engine


def _run(engine, fn_name: str) -> None:
    module = _load()
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            getattr(module, fn_name)()


def test_revision_chain():
    module = _load()
    assert module.revision == "92b61c6e9db4"
    assert module.down_revision == "a7d3c89106f9"


def test_upgrade_creates_a_unique_partial_index():
    engine = _engine()
    _run(engine, "upgrade")
    indexes = {i["name"]: i for i in sa.inspect(engine).get_indexes("appointment")}
    assert indexes[INDEX]["unique"]
    assert indexes[INDEX]["column_names"] == ["schedule_id", "slot_start"]


def insert(conn, key: str, status: str) -> None:
    conn.execute(
        sa.text("INSERT INTO appointment VALUES (:id, 's1', '2026-10-14 05:00:00', :status)"),
        {"id": key, "status": status},
    )


def test_the_index_refuses_two_active_rows_and_ignores_cancelled_ones():
    engine = _engine()
    _run(engine, "upgrade")
    with engine.begin() as conn:
        insert(conn, "a", "cancelled")
        insert(conn, "b", "cancelled")
        insert(conn, "c", "booked")
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        insert(conn, "d", "booked")


def test_downgrade_drops_the_index():
    engine = _engine()
    _run(engine, "upgrade")
    _run(engine, "downgrade")
    assert INDEX not in {i["name"] for i in sa.inspect(engine).get_indexes("appointment")}
```

In `tests/test_privacy_access.py`, update the migration-chain guard (find `def test_ac29_revision_chain_is_single_and_unbroken`): change `assert script.get_heads() == ["a7d3c89106f9"]` to `["92b61c6e9db4"]` and add `"92b61c6e9db4",  # P5 booking index` as the first entry of the `chain` list.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_booking_index.py tests/test_booking_migration.py tests/test_privacy_access.py -q`
Expected: the index tests fail with `DID NOT RAISE IntegrityError`; the migration tests fail because the migration file does not exist; the chain guard fails (head is still `a7d3c89106f9`).

- [ ] **Step 3: Add the index to the model and write the migration**

In `backend/app/db/models.py`, in `class Appointment`, change `__table_args__` to add the unique index (`text` is already imported):

```python
    __table_args__ = (
        CheckConstraint(
            "status IN ('booked', 'checked_in', 'done', 'cancelled')",
            name="appointment_status_check",
        ),
        Index("idx_appointment_schedule_id", "schedule_id"),
        # P5: at most one non-cancelled appointment per schedule time. The booking
        # service refuses a taken time under a per-doctor lock; this makes the
        # database refuse it too. `schedule_id` is nullable and NULLs never clash.
        Index(
            "uq_appointment_active_slot",
            "schedule_id",
            "slot_start",
            unique=True,
            postgresql_where=text("status <> 'cancelled'"),
            sqlite_where=text("status <> 'cancelled'"),
        ),
    )
```

Create `alembic/versions/92b61c6e9db4_booking_active_slot_index.py`:

```python
"""booking v1: one active appointment per schedule time

Additive only, hand-written. A partial unique index on the shared `appointment`
table, which the booking work (P5) owns: at most one non-cancelled appointment
per `(schedule_id, slot_start)`. The booking service already refuses a taken
time under a per-doctor lock; this makes the database refuse it too, so two
patients can never hold one time even if a race, a script or another tool
slips past the app. The table had no rows on the shared project when this was
written; re-check before applying (`SELECT count(*) FROM appointment`).

Revision ID: 92b61c6e9db4
Revises: a7d3c89106f9
Create Date: 2026-10-12 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "92b61c6e9db4"
down_revision: str | Sequence[str] | None = "a7d3c89106f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_appointment_active_slot",
        "appointment",
        ["schedule_id", "slot_start"],
        unique=True,
        postgresql_where=sa.text("status <> 'cancelled'"),
        sqlite_where=sa.text("status <> 'cancelled'"),
    )


def downgrade() -> None:
    op.drop_index("uq_appointment_active_slot", table_name="appointment")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_booking_index.py tests/test_booking_migration.py tests/test_privacy_access.py -q`
Expected: all pass. Then `uv run alembic heads` prints `92b61c6e9db4 (head)`.

- [ ] **Step 5: Run the full suite, lint, commit**

```bash
uv run pytest -q
uv run ruff check backend tests && uv run ruff format backend tests
git add backend/app/db/models.py alembic/versions/92b61c6e9db4_booking_active_slot_index.py tests/booking_helpers.py tests/test_booking_index.py tests/test_booking_migration.py tests/test_privacy_access.py
git commit -m "feat(booking): add the one-active-booking-per-time index"
```

Do **not** run `alembic upgrade head` against the shared Supabase project here; that is Task 10, after Zarwa's go-ahead.

---

### Task 3: Plumbing: the refusal error, the rate limit, audit constants

**Files:**
- Modify: `backend/app/errors.py` (after `ScheduleConflict`)
- Modify: `backend/app/settings.py`
- Modify: `backend/app/deps.py`
- Modify: `backend/app/audit/writer.py`
- Modify: `backend/app/i18n/messages/en.json`, `ur.json`
- Test: `tests/test_booking_plumbing.py`

**Interfaces:**
- Produces: `BookingRefused(reason: str)` (409; `message_key = f"errors.booking_{reason}"`; `.reason`); `settings.booking_write_rate_limit_per_minute` (default 20); `check_booking_write_rate(actor: Actor) -> None` (raises `RateLimited(retry_after_s=60)`); `audit.APPOINTMENT_BOOK = "appointment.book"`, `audit.APPOINTMENT_CANCEL = "appointment.cancel"`, `audit.APPOINTMENT_RESCHEDULE = "appointment.reschedule"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_booking_plumbing.py`:

```python
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
```

`REFUSAL_REASONS` does not exist yet: add it in this task's Step 3 to `backend/app/booking/schemas.py`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_booking_plumbing.py -q`
Expected: collection error, `ImportError: cannot import name 'REFUSAL_REASONS'`.

- [ ] **Step 3: Write the code**

Append to `backend/app/booking/schemas.py`, directly after `DEFAULT_MINUTES`:

```python
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
```

In `backend/app/errors.py`, directly after the `ScheduleConflict` class:

```python
class BookingRefused(AppError):
    """A booking, reschedule or cancel the rules refuse (P5, spec 3.2): the time
    is not offered, too soon, too far ahead, taken, the session is full, the
    patient already has an overlapping appointment or one with that doctor that
    day, or the appointment can no longer be changed. `reason` picks the
    message (`errors.booking_<reason>`). A 409: the page can be shown again with
    fresh times."""

    code = "BOOKING_REFUSED"
    http_status = status.HTTP_409_CONFLICT
    message_key = "errors.booking_not_offered"
    retryable = False

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.message_key = f"errors.booking_{reason}"
        super().__init__({"reason": reason})
```

In `backend/app/settings.py`, directly after `schedule_write_rate_limit_per_minute: int = 30`:

```python

    # ── Appointment booking (P5) ─────────────────────────────────────────
    booking_write_rate_limit_per_minute: int = 20
```

In `backend/app/deps.py`, directly after `check_document_write_rate` (copy its shape):

```python
async def check_booking_write_rate(actor: Actor) -> None:
    """Counts one booking write attempt (book, reschedule, cancel). Shared by every
    web handler, so they all draw from one per-patient window."""
    count = await cache.incr(ratelimit_key("booking_write", str(actor.user_id)), 60)
    if count > settings.booking_write_rate_limit_per_minute:
        raise RateLimited(retry_after_s=60)
```

In `backend/app/audit/writer.py`, directly after `DOCTOR_DASHBOARD_VIEW = "doctor.dashboard.view"` (before the next comment block):

```python

# Emitted by appointment booking (P5). `detail` holds ids only: never a name,
# a time of day or a note.
APPOINTMENT_BOOK = "appointment.book"
APPOINTMENT_CANCEL = "appointment.cancel"
APPOINTMENT_RESCHEDULE = "appointment.reschedule"
```

Add the eight refusal messages with the i18n helper (Global section) using:

```python
EN = {
    "errors.booking_not_offered": "That time is not available. Please pick another.",
    "errors.booking_too_soon": "That time is too soon. Please pick a time at least 30 minutes from now.",
    "errors.booking_too_far": "That day is too far ahead. You can book up to 14 days ahead.",
    "errors.booking_taken": "Someone just booked that time. Please pick another.",
    "errors.booking_full": "That session is full. Please pick another day.",
    "errors.booking_overlap": "You already have an appointment at that time.",
    "errors.booking_same_day": "You already have an appointment with this doctor that day.",
    "errors.booking_not_cancellable": "This appointment can't be changed any more.",
}
UR = {
    "errors.booking_not_offered": "یہ وقت دستیاب نہیں۔ براہ کرم کوئی اور وقت منتخب کریں۔",
    "errors.booking_too_soon": "یہ وقت بہت قریب ہے۔ براہ کرم کم از کم 30 منٹ بعد کا وقت منتخب کریں۔",
    "errors.booking_too_far": "یہ دن بہت دور ہے۔ آپ زیادہ سے زیادہ 14 دن پہلے بکنگ کر سکتے ہیں۔",
    "errors.booking_taken": "ابھی کسی نے یہ وقت بک کر لیا ہے۔ براہ کرم کوئی اور وقت منتخب کریں۔",
    "errors.booking_full": "یہ سیشن بھر چکا ہے۔ براہ کرم کوئی اور دن منتخب کریں۔",
    "errors.booking_overlap": "اس وقت آپ کی پہلے سے ایک اپائنٹمنٹ موجود ہے۔",
    "errors.booking_same_day": "اس دن آپ کی اس ڈاکٹر کے ساتھ پہلے سے اپائنٹمنٹ موجود ہے۔",
    "errors.booking_not_cancellable": "اس اپائنٹمنٹ میں اب تبدیلی نہیں ہو سکتی۔",
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_booking_plumbing.py -q`
Expected: all pass (8 parametrized + 6).

- [ ] **Step 5: Run the full suite, lint, commit**

```bash
uv run pytest -q
uv run ruff check backend tests && uv run ruff format backend tests
git add backend/app/errors.py backend/app/settings.py backend/app/deps.py backend/app/audit/writer.py backend/app/booking/schemas.py backend/app/i18n tests/test_booking_plumbing.py
git commit -m "feat(booking): add the refusal error, write budget and audit actions"
```

---

### Task 4: Service: find doctors and list the free times

**Files:**
- Create: `backend/app/booking/service.py`
- Test: `tests/test_booking_search.py`

**Interfaces:**
- Consumes: `slots.Window`, `slots.window_bounds`, `slots.bookable_times`, `slots.as_utc` (Task 1); `schemas.*` (Task 1); `schedule.service.get_effective_availability(session, doctor_id, date_from, date_to, clinic_id) -> list[engine.EffectiveWindow]` (D7; it refuses a range that starts in the past or spans more than 30 days, and we ask for at most 15); `tests/booking_helpers.py` (Task 2).
- Produces (`service.py`): `_now() -> datetime` (the one clock; tests replace it); `current_day() -> date` (today in Pakistan); `async search_doctors(session, *, query="", specialty="") -> list[DoctorCard]`; `async specialty_options(session) -> list[str]`; `async get_doctor(session, doctor_id) -> DoctorCard` (`NotFound` unless visible); `async list_options(session, doctor_id, clinic_id) -> list[DayOptions]` (`NotFound` unless the doctor is visible and works at that live hospital); internals `_visible_doctors`, `_windows`, `_active_starts` that Tasks 5 and 6 reuse.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_booking_search.py`:

```python
"""P5: who a patient can find, and the free times they can pick."""

from __future__ import annotations

from datetime import date, time

import pytest
from app.booking import service
from app.db import types as dbtypes
from app.db.models import ScheduleException, ScheduleExceptionType, UserRole, VerificationStatus
from app.errors import NotFound

from tests.booking_helpers import (
    MONDAY,
    THURSDAY,
    TODAY,
    WED,
    add_appointment,
    add_session,
    at,
    freeze,
    make_patient,
    verified_doctor,
)
from tests.conftest import add_affiliation, doctor_of, make_user


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    freeze(monkeypatch)


# ── Search ───────────────────────────────────────────────────────────────
async def test_a_verified_doctor_is_found_with_their_live_hospitals(db, clinic, second_clinic):
    doctor = await verified_doctor(db, clinic)
    await add_affiliation(db, doctor.id, second_clinic.id)
    cards = await service.search_doctors(db)
    assert [c.name for c in cards] == ["Dr Booking"]
    assert {cl.name for cl in cards[0].clinics} == {"Shifa International", "Maroof International"}


async def test_a_pending_doctor_is_not_found(db, clinic):
    await make_user(db, email="pending@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    assert await service.search_doctors(db) == []


async def test_a_rejected_doctor_is_not_found(db, clinic):
    doctor = await verified_doctor(db, clinic)
    doctor.verification_status = VerificationStatus.REJECTED
    doctor.verified_by = None
    doctor.verified_at = None
    await db.commit()
    assert await service.search_doctors(db) == []


async def test_a_doctor_with_no_live_hospital_is_not_found(db, inactive_clinic):
    await verified_doctor(db, inactive_clinic)
    assert await service.search_doctors(db) == []


async def test_an_inactive_affiliation_hides_only_that_hospital(db, clinic, second_clinic):
    doctor = await verified_doctor(db, clinic)
    await add_affiliation(db, doctor.id, second_clinic.id, status="inactive")
    cards = await service.search_doctors(db)
    assert [cl.name for cl in cards[0].clinics] == ["Shifa International"]


async def test_search_matches_the_doctor_name_ignoring_case(db, clinic):
    await verified_doctor(db, clinic)
    assert len(await service.search_doctors(db, query="BOOK")) == 1
    assert await service.search_doctors(db, query="zzz") == []


async def test_search_matches_a_hospital_name(db, clinic):
    await verified_doctor(db, clinic)
    assert len(await service.search_doctors(db, query="shifa")) == 1


async def test_the_specialty_filter_is_exact(db, clinic):
    await verified_doctor(db, clinic, email="a.dr@x.com", name="Dr A", specialty="Cardiology")
    await verified_doctor(db, clinic, email="b.dr@x.com", name="Dr B", specialty="Dermatology")
    cards = await service.search_doctors(db, specialty="Dermatology")
    assert [c.name for c in cards] == ["Dr B"]


async def test_specialty_options_are_distinct_and_sorted(db, clinic):
    await verified_doctor(db, clinic, email="a.dr@x.com", name="Dr A", specialty="Dermatology")
    await verified_doctor(db, clinic, email="b.dr@x.com", name="Dr B", specialty="Cardiology")
    await verified_doctor(db, clinic, email="c.dr@x.com", name="Dr C", specialty="Cardiology")
    assert await service.specialty_options(db) == ["Cardiology", "Dermatology"]


async def test_get_doctor_returns_a_visible_doctor_and_hides_the_rest(db, clinic):
    doctor = await verified_doctor(db, clinic)
    assert (await service.get_doctor(db, doctor.id)).name == "Dr Booking"
    pending = await make_user(db, email="p@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    with pytest.raises(NotFound):
        await service.get_doctor(db, (await doctor_of(db, pending)).id)


# ── The free times ───────────────────────────────────────────────────────
async def test_options_list_the_free_times_for_each_session_day(db, clinic):
    doctor = await verified_doctor(db, clinic)
    await add_session(db, doctor, clinic)  # Wednesdays 09:00-13:00
    days = await service.list_options(db, doctor.id, clinic.id)
    # Today is Monday the 12th, so the horizon ends on the 26th: Wednesday the
    # 28th is too far.
    assert [d.day for d in days] == [WED, date(2026, 10, 21)]
    assert len(days[0].times) == 16
    assert days[0].times[0] == at(WED, "09:00") and days[0].times[-1] == at(WED, "12:45")


async def test_taken_times_are_not_offered(db, clinic):
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic)
    _, patient, _ = await make_patient(db)
    await add_appointment(db, patient, doctor, clinic, session, at(WED, "09:00"))
    days = await service.list_options(db, doctor.id, clinic.id)
    assert len(days[0].times) == 15 and at(WED, "09:00") not in days[0].times


async def test_a_cancelled_booking_does_not_block_its_time(db, clinic):
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic)
    _, patient, _ = await make_patient(db)
    await add_appointment(db, patient, doctor, clinic, session, at(WED, "09:00"), status="cancelled")
    assert len((await service.list_options(db, doctor.id, clinic.id))[0].times) == 16


async def test_a_full_session_drops_that_day(db, clinic):
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic, cap=1)
    _, patient, _ = await make_patient(db)
    await add_appointment(db, patient, doctor, clinic, session, at(WED, "09:00"))
    days = await service.list_options(db, doctor.id, clinic.id)
    assert [d.day for d in days] == [date(2026, 10, 21)]


async def test_time_off_hides_the_whole_day(db, clinic):
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic)
    db.add(
        ScheduleException(
            schedule_id=session.id, exception_date=WED, kind=ScheduleExceptionType.LEAVE
        )
    )
    await db.commit()
    assert [d.day for d in await service.list_options(db, doctor.id, clinic.id)] == [
        date(2026, 10, 21)
    ]


async def test_partial_time_off_hides_only_those_times(db, clinic):
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic)
    db.add(
        ScheduleException(
            schedule_id=session.id,
            exception_date=WED,
            kind=ScheduleExceptionType.LEAVE,
            start_time=time(9, 0),
            end_time=time(11, 0),
        )
    )
    await db.commit()
    first_day = (await service.list_options(db, doctor.id, clinic.id))[0]
    assert first_day.times[0] == at(WED, "11:00") and len(first_day.times) == 8


async def test_extra_hours_add_a_session(db, clinic):
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic)
    friday = date(2026, 10, 16)
    db.add(
        ScheduleException(
            schedule_id=session.id,
            exception_date=friday,
            kind=ScheduleExceptionType.EXTRA,
            start_time=time(16, 0),
            end_time=time(17, 0),
            max_tokens=2,
        )
    )
    await db.commit()
    days = {d.day: d for d in await service.list_options(db, doctor.id, clinic.id)}
    assert list(days[friday].times) == [
        at(friday, "16:00"),
        at(friday, "16:15"),
        at(friday, "16:30"),
        at(friday, "16:45"),
    ]


async def test_todays_past_times_are_hidden_by_the_lead(db, clinic):
    # Now is Monday 13:00 in Pakistan. The morning session is over, so only the
    # later Mondays are offered; an afternoon session that starts after the
    # 30 minute lead brings today back.
    doctor = await verified_doctor(db, clinic)
    await add_session(db, doctor, clinic, weekday=MONDAY, start="09:00", end="13:00")
    days = await service.list_options(db, doctor.id, clinic.id)
    assert [d.day for d in days] == [date(2026, 10, 19), date(2026, 10, 26)]
    await add_session(db, doctor, clinic, weekday=MONDAY, start="14:00", end="18:00")
    today = (await service.list_options(db, doctor.id, clinic.id))[0]
    assert today.day == TODAY and today.times[0] == at(TODAY, "14:00")


async def test_the_last_day_is_included_and_the_next_is_not(db, clinic):
    doctor = await verified_doctor(db, clinic)
    await add_session(db, doctor, clinic, weekday=MONDAY, start="14:00", end="18:00")
    days = [d.day for d in await service.list_options(db, doctor.id, clinic.id)]
    # Today, then the 19th, then the 26th (today + 14 days); November the 2nd is out.
    assert days == [TODAY, date(2026, 10, 19), date(2026, 10, 26)]


async def test_options_are_per_hospital(db, clinic, second_clinic):
    doctor = await verified_doctor(db, clinic)
    await add_affiliation(db, doctor.id, second_clinic.id)
    await add_session(db, doctor, clinic, weekday=2, start="09:00", end="10:00")
    await add_session(db, doctor, second_clinic, weekday=3, start="09:00", end="10:00")
    here = await service.list_options(db, doctor.id, clinic.id)
    there = await service.list_options(db, doctor.id, second_clinic.id)
    assert {d.day.weekday() for d in here} == {2} and {d.day.weekday() for d in there} == {3}


async def test_a_hospital_the_doctor_is_not_at_is_not_found(db, clinic, second_clinic):
    doctor = await verified_doctor(db, clinic)
    with pytest.raises(NotFound):
        await service.list_options(db, doctor.id, second_clinic.id)


async def test_a_pending_doctor_has_no_options(db, clinic):
    user = await make_user(db, email="p@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    doctor = await doctor_of(db, user)
    await add_session(db, doctor, clinic)
    with pytest.raises(NotFound):
        await service.list_options(db, doctor.id, clinic.id)


async def test_a_time_after_midnight_in_pakistan_is_listed_under_its_pakistan_date(db, clinic):
    # Review focus 2: 00:15 on Thursday the 15th is 19:15 UTC on the 14th.
    doctor = await verified_doctor(db, clinic)
    await add_session(db, doctor, clinic, weekday=THURSDAY, start="00:00", end="01:00")
    days = await service.list_options(db, doctor.id, clinic.id)
    thursday = date(2026, 10, 15)
    assert days[0].day == thursday
    first = days[0].times[0]
    assert first == at(thursday, "00:00")
    assert first.date() == date(2026, 10, 14)  # the UTC date is the day before
    assert first.astimezone(dbtypes.PKT).date() == thursday
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_booking_search.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'app.booking.service'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/booking/service.py`:

```python
"""Appointment booking business logic (P5, FR15, FR16; spec 3.2).

Every rule lives here: who is visible, which times are free, who may book,
cancel or move what. Every write locks the doctor first (D7's per-doctor
`SELECT ... FOR UPDATE`), re-reads what is taken and the session's count, then
writes; the partial unique index on `appointment` is the backstop. The pure
grid and cap logic is in `slots.py`.

Scoping: the app connects with BYPASSRLS, so every patient query filters by the
caller's own `patient_id`, resolved from `actor.user_id` and never read from a
request. A foreign or nonexistent appointment is one identical `NotFound`.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import types as dbtypes
from ..db.models import (
    Appointment,
    Clinic,
    Doctor,
    DoctorAffiliation,
    DoctorSchedule,
    VerificationStatus,
)
from ..errors import NotFound
from ..schedule import service as schedule_service
from . import slots
from .schemas import (
    ACTIVE_STATUSES,
    HORIZON_DAYS,
    LEAD_MINUTES,
    SEARCH_LIMIT,
    ClinicRef,
    DayOptions,
    DoctorCard,
)

LIVE = "active"  # doctor_affiliation.status / clinic.status


def _now() -> datetime:
    """The current instant, UTC. Tests replace this one function to freeze the clock."""
    return dbtypes.utcnow()


def current_day() -> date:
    """Today in Pakistan."""
    return _now().astimezone(dbtypes.PKT).date()


# ── Who is visible ───────────────────────────────────────────────────────
async def _visible_doctors(
    session: AsyncSession, doctor_id: uuid.UUID | None = None
) -> dict[uuid.UUID, tuple[Doctor, dict[uuid.UUID, Clinic]]]:
    """Verified doctors and their live hospitals: the only doctors a patient may
    see or book (D7 decision 6). A doctor with no live hospital is absent."""
    stmt = (
        select(Doctor, Clinic)
        .join(DoctorAffiliation, DoctorAffiliation.doctor_id == Doctor.id)
        .join(Clinic, Clinic.id == DoctorAffiliation.clinic_id)
        .where(
            Doctor.verification_status == VerificationStatus.VERIFIED,
            DoctorAffiliation.status == LIVE,
            Clinic.status == LIVE,
        )
        .order_by(Doctor.full_name, Doctor.id)
    )
    if doctor_id is not None:
        stmt = stmt.where(Doctor.id == doctor_id)
    found: dict[uuid.UUID, tuple[Doctor, dict[uuid.UUID, Clinic]]] = {}
    for doctor, clinic in (await session.execute(stmt)).all():
        found.setdefault(doctor.id, (doctor, {}))[1][clinic.id] = clinic
    return found


def _card(doctor: Doctor, clinics: dict[uuid.UUID, Clinic]) -> DoctorCard:
    return DoctorCard(
        id=doctor.id,
        name=doctor.full_name,
        specialty=doctor.specialty,
        clinics=tuple(
            ClinicRef(id=c.id, name=c.name, city=c.city)
            for c in sorted(clinics.values(), key=lambda c: c.name)
        ),
    )


async def search_doctors(
    session: AsyncSession, *, query: str = "", specialty: str = ""
) -> list[DoctorCard]:
    """Visible doctors whose name or any hospital name contains `query`
    (case-insensitive), optionally of one exact `specialty`, by name."""
    cards = [_card(d, c) for d, c in (await _visible_doctors(session)).values()]
    if specialty:
        cards = [c for c in cards if c.specialty == specialty]
    needle = query.strip().casefold()
    if needle:
        cards = [
            c
            for c in cards
            if needle in c.name.casefold() or any(needle in cl.name.casefold() for cl in c.clinics)
        ]
    return cards[:SEARCH_LIMIT]


async def specialty_options(session: AsyncSession) -> list[str]:
    visible = (await _visible_doctors(session)).values()
    return sorted({d.specialty for d, _ in visible if d.specialty})


async def get_doctor(session: AsyncSession, doctor_id: uuid.UUID) -> DoctorCard:
    found = await _visible_doctors(session, doctor_id)
    if doctor_id not in found:
        raise NotFound()
    doctor, clinics = found[doctor_id]
    return _card(doctor, clinics)


# ── Sessions and what is already booked ──────────────────────────────────
async def _windows(
    session: AsyncSession,
    doctor_id: uuid.UUID,
    clinic_id: uuid.UUID,
    date_from: date,
    date_to: date,
) -> list[slots.Window]:
    """D7's effective session windows at one hospital, with the doctor's minutes
    per patient attached. This is the only way availability is read (D7 §6)."""
    found = await schedule_service.get_effective_availability(
        session, doctor_id, date_from, date_to, clinic_id
    )
    if not found:
        return []
    rows = await session.execute(
        select(DoctorSchedule.id, DoctorSchedule.slot_minutes).where(
            DoctorSchedule.id.in_({w.schedule_id for w in found})
        )
    )
    minutes = {schedule_id: slot_minutes for schedule_id, slot_minutes in rows.all()}
    return [
        slots.Window(
            schedule_id=w.schedule_id,
            clinic_id=w.clinic_id,
            day=w.day,
            start=w.start,
            end=w.end,
            slot_minutes=minutes[w.schedule_id],
            max_tokens=w.max_tokens,
        )
        for w in found
    ]


async def _active_starts(
    session: AsyncSession,
    schedule_ids: set[uuid.UUID],
    lo: datetime,
    hi: datetime,
    *,
    exclude: uuid.UUID | None = None,
) -> dict[uuid.UUID, list[datetime]]:
    """The starts (aware UTC) of the active appointments of these schedules in
    `[lo, hi)`, grouped by schedule. `exclude` leaves one appointment out (the
    one a reschedule is replacing)."""
    stmt = select(Appointment.schedule_id, Appointment.slot_start).where(
        Appointment.schedule_id.in_(schedule_ids),
        Appointment.status.in_(ACTIVE_STATUSES),
        Appointment.slot_start >= lo,
        Appointment.slot_start < hi,
    )
    if exclude is not None:
        stmt = stmt.where(Appointment.id != exclude)
    grouped: dict[uuid.UUID, list[datetime]] = {sid: [] for sid in schedule_ids}
    for schedule_id, start in (await session.execute(stmt)).all():
        grouped[schedule_id].append(slots.as_utc(start))
    return grouped


async def list_options(
    session: AsyncSession, doctor_id: uuid.UUID, clinic_id: uuid.UUID
) -> list[DayOptions]:
    """The free times at one hospital for the next 14 days, only days that have
    any. Read-only: it books nothing and takes no lock."""
    found = await _visible_doctors(session, doctor_id)
    if doctor_id not in found or clinic_id not in found[doctor_id][1]:
        raise NotFound()
    earliest = _now() + timedelta(minutes=LEAD_MINUTES)
    today = current_day()
    last_day = today + timedelta(days=HORIZON_DAYS)
    windows = await _windows(session, doctor_id, clinic_id, today, last_day)
    if not windows:
        return []
    bounds = [slots.window_bounds(w) for w in windows]
    taken = await _active_starts(
        session,
        {w.schedule_id for w in windows},
        min(lo for lo, _ in bounds),
        max(hi for _, hi in bounds),
    )
    by_day: dict[date, list[datetime]] = {}
    for window, (lo, hi) in zip(windows, bounds, strict=True):
        booked = [s for s in taken[window.schedule_id] if lo <= s < hi]
        times = slots.bookable_times(window, booked, earliest=earliest, last_day=last_day)
        if times:
            by_day.setdefault(window.day, []).extend(times)
    return [DayOptions(day=d, times=tuple(sorted(ts))) for d, ts in sorted(by_day.items())]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_booking_search.py -q`
Expected: all pass (about 21).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check backend tests && uv run ruff format backend tests
uv run pytest tests/test_booking_slots.py tests/test_booking_search.py -q
git add backend/app/booking/service.py tests/test_booking_search.py
git commit -m "feat(booking): find verified doctors and list their free times"
```

---

### Task 5: Service: book

**Files:**
- Modify: `backend/app/booking/service.py` (imports; append the functions below)
- Test: `tests/test_booking_book.py`

**Interfaces:**
- Consumes: Task 4's `_now`, `current_day`, `_visible_doctors`, `_windows`, `_active_starts`; `slots.*`; `schedule_service.lock_doctor(session, doctor_id)` (D7); `audit.APPOINTMENT_BOOK` / `audit.APPOINTMENT_RESCHEDULE` and `BookingRefused` (Task 3).
- Produces: `async book(session, actor, doctor_id, clinic_id, slot_start, *, user_agent=None) -> Appointment`; internals `_patient(session, actor) -> Patient`, `_check_patient_rules(...)`, and `_place(session, actor, doctor_id, clinic_id, slot_start, *, replacing, user_agent)` which Task 6's `reschedule` reuses. `Actor` is `app.deps.Actor`; refusals raise `BookingRefused(reason)`, anything not bookable or not the caller's raises `NotFound`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_booking_book.py`:

```python
"""P5: booking a time, and every way it is refused."""

from __future__ import annotations

import json
from datetime import date, time

import pytest
from app.booking import service, slots
from app.db.models import (
    Appointment,
    AuditLog,
    ScheduleException,
    ScheduleExceptionType,
    UserRole,
    VerificationStatus,
)
from app.deps import Actor
from app.errors import BookingRefused, NotFound
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from tests.booking_helpers import (
    MONDAY,
    THURSDAY,
    TODAY,
    WED,
    add_appointment,
    add_session,
    at,
    freeze,
    make_patient,
    verified_doctor,
)
from tests.conftest import add_affiliation, doctor_of, make_user


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    freeze(monkeypatch)


async def world(db, clinic, *, cap=16, **session_kw):
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic, cap=cap, **session_kw)
    _, patient, actor = await make_patient(db)
    return doctor, session, patient, actor


async def another_patient(db, n: int):
    """(patient row, actor) for a second, third... patient."""
    _, patient, actor = await make_patient(db, email=f"p{n}@x.com", name=f"Patient {n}")
    return patient, actor


async def expect_refusal(reason: str, call) -> None:
    with pytest.raises(BookingRefused) as exc:
        await call
    assert exc.value.reason == reason


async def stored(db) -> list[Appointment]:
    return list((await db.execute(select(Appointment))).scalars().all())


# ── The happy path ───────────────────────────────────────────────────────
async def test_a_free_time_is_booked(db, clinic):
    doctor, session, patient, actor = await world(db, clinic)
    appt = await service.book(db, actor, doctor.id, clinic.id, at(WED, "10:00"))
    assert appt.status == "booked" and appt.booking_channel == "online"
    assert appt.schedule_id == session.id
    assert (appt.doctor_id, appt.clinic_id, appt.patient_id) == (doctor.id, clinic.id, patient.id)
    assert slots.as_utc(appt.slot_start) == at(WED, "10:00")
    assert len(await stored(db)) == 1


async def test_a_time_after_midnight_in_pakistan_is_booked_on_its_pakistan_date(db, clinic):
    # Review focus 2.
    doctor, _, _, actor = await world(db, clinic, weekday=THURSDAY, start="00:00", end="01:00")
    thursday = date(2026, 10, 15)
    appt = await service.book(db, actor, doctor.id, clinic.id, at(thursday, "00:15"))
    assert slots.as_utc(appt.slot_start).isoformat() == "2026-10-14T19:15:00+00:00"


async def test_a_naive_start_is_read_as_utc(db, clinic):
    doctor, _, _, actor = await world(db, clinic)
    naive = at(WED, "10:00").replace(tzinfo=None)
    appt = await service.book(db, actor, doctor.id, clinic.id, naive)
    assert slots.as_utc(appt.slot_start) == at(WED, "10:00")


# ── Taken, full, and what frees them ─────────────────────────────────────
async def test_a_taken_time_is_refused(db, clinic):
    doctor, _, _, actor = await world(db, clinic)
    _, other = await another_patient(db, 2)
    await service.book(db, other, doctor.id, clinic.id, at(WED, "10:00"))
    await expect_refusal("taken", service.book(db, actor, doctor.id, clinic.id, at(WED, "10:00")))
    assert len(await stored(db)) == 1


async def test_a_full_session_is_refused(db, clinic):
    doctor, _, _, actor = await world(db, clinic, cap=2)
    (_, first), (_, second) = await another_patient(db, 2), await another_patient(db, 3)
    await service.book(db, first, doctor.id, clinic.id, at(WED, "09:00"))
    await service.book(db, second, doctor.id, clinic.id, at(WED, "09:15"))
    await expect_refusal("full", service.book(db, actor, doctor.id, clinic.id, at(WED, "10:00")))


async def test_cancel_then_rebook_the_same_time_works(db, clinic):
    # Review focus 5 (second half): the partial index ignores cancelled rows.
    doctor, session, _, actor = await world(db, clinic)
    other, _ = await another_patient(db, 2)
    first = await add_appointment(db, other, doctor, clinic, session, at(WED, "10:00"))
    first.status = "cancelled"
    await db.commit()
    appt = await service.book(db, actor, doctor.id, clinic.id, at(WED, "10:00"))
    assert appt.status == "booked" and len(await stored(db)) == 2


# ── The window ───────────────────────────────────────────────────────────
async def test_the_lead_time_boundary(db, clinic):
    doctor, _, _, actor = await world(db, clinic, weekday=MONDAY, start="12:00", end="18:00")
    # Now is Monday 13:00 in Pakistan; the earliest bookable time is 13:30.
    await expect_refusal("too_soon", service.book(db, actor, doctor.id, clinic.id, at(TODAY, "13:15")))
    appt = await service.book(db, actor, doctor.id, clinic.id, at(TODAY, "13:30"))
    assert appt.status == "booked"


async def test_a_day_beyond_the_horizon_is_refused_and_the_last_day_is_allowed(db, clinic):
    doctor, _, _, actor = await world(db, clinic, weekday=MONDAY, start="14:00", end="18:00")
    await expect_refusal(
        "too_far", service.book(db, actor, doctor.id, clinic.id, at(date(2026, 11, 2), "14:00"))
    )
    appt = await service.book(db, actor, doctor.id, clinic.id, at(date(2026, 10, 26), "14:00"))
    assert appt.status == "booked"


@pytest.mark.parametrize(
    ("day", "hhmm"),
    [
        (WED, "10:07"),  # off the grid
        (WED, "13:00"),  # after the session
        (WED, "08:45"),  # before the session
        (date(2026, 10, 15), "10:00"),  # a day with no session
    ],
)
async def test_a_time_the_session_does_not_offer_is_refused(db, clinic, day, hhmm):
    doctor, _, _, actor = await world(db, clinic)
    await expect_refusal("not_offered", service.book(db, actor, doctor.id, clinic.id, at(day, hhmm)))
    assert await stored(db) == []


async def test_a_time_removed_after_the_page_loaded_is_not_booked(db, clinic):
    # Review focus 3: the page listed 10:00, then the doctor took the day off.
    doctor, session, _, actor = await world(db, clinic)
    options = await service.list_options(db, doctor.id, clinic.id)
    assert at(WED, "10:00") in options[0].times
    db.add(
        ScheduleException(
            schedule_id=session.id, exception_date=WED, kind=ScheduleExceptionType.LEAVE
        )
    )
    await db.commit()
    await expect_refusal("not_offered", service.book(db, actor, doctor.id, clinic.id, at(WED, "10:00")))
    assert await stored(db) == []


async def test_an_extra_session_has_its_own_cap_and_count(db, clinic):
    # Review focus 4: the weekly window and an extra on the same date share a
    # schedule_id but are separate sessions.
    doctor, session, _, actor = await world(db, clinic, cap=2)
    db.add(
        ScheduleException(
            schedule_id=session.id,
            exception_date=WED,
            kind=ScheduleExceptionType.EXTRA,
            start_time=time(16, 0),
            end_time=time(18, 0),
            max_tokens=1,
        )
    )
    await db.commit()
    (_, p2), (_, p3), (_, p4) = (
        await another_patient(db, 2),
        await another_patient(db, 3),
        await another_patient(db, 4),
    )
    await service.book(db, p2, doctor.id, clinic.id, at(WED, "09:00"))
    await service.book(db, p3, doctor.id, clinic.id, at(WED, "09:15"))
    await expect_refusal("full", service.book(db, actor, doctor.id, clinic.id, at(WED, "10:00")))
    appt = await service.book(db, actor, doctor.id, clinic.id, at(WED, "16:00"))  # the extra
    assert appt.status == "booked"
    await expect_refusal("full", service.book(db, p4, doctor.id, clinic.id, at(WED, "16:15")))


async def test_an_extra_without_its_own_cap_inherits_the_weekly_cap(db, clinic):
    # Spec test 13: an extra's cap comes from its anchor slot (D7 R14), but the
    # extra is counted apart from the weekly window.
    doctor, session, _, actor = await world(db, clinic, cap=2)
    db.add(
        ScheduleException(
            schedule_id=session.id,
            exception_date=WED,
            kind=ScheduleExceptionType.EXTRA,
            start_time=time(16, 0),
            end_time=time(18, 0),
        )
    )
    await db.commit()
    (_, p2), (_, p3) = await another_patient(db, 2), await another_patient(db, 3)
    await service.book(db, p2, doctor.id, clinic.id, at(WED, "16:00"))
    await service.book(db, p3, doctor.id, clinic.id, at(WED, "16:15"))
    await expect_refusal("full", service.book(db, actor, doctor.id, clinic.id, at(WED, "16:30")))
    weekly = await service.book(db, actor, doctor.id, clinic.id, at(WED, "09:00"))
    assert weekly.status == "booked"  # the weekly window was not used up by the extra


# ── The patient's own rules ──────────────────────────────────────────────
async def test_overlap_with_another_doctor_is_refused_but_back_to_back_is_not(db, clinic):
    doctor_a, _, _, actor = await world(db, clinic)
    doctor_b = await verified_doctor(db, clinic, email="b.dr@x.com", name="Dr B")
    await add_session(db, doctor_b, clinic, minutes=30)
    await service.book(db, actor, doctor_a.id, clinic.id, at(WED, "10:15"))  # 10:15-10:30
    # Dr B's 10:00 turn lasts 30 minutes (10:00-10:30): it overlaps.
    await expect_refusal("overlap", service.book(db, actor, doctor_b.id, clinic.id, at(WED, "10:00")))
    # 10:30-11:00 starts exactly when the first one ends.
    appt = await service.book(db, actor, doctor_b.id, clinic.id, at(WED, "10:30"))
    assert appt.status == "booked"


async def test_two_appointments_with_one_doctor_on_one_day_are_refused(db, clinic):
    doctor, _, _, actor = await world(db, clinic)
    await service.book(db, actor, doctor.id, clinic.id, at(WED, "10:00"))
    await expect_refusal("same_day", service.book(db, actor, doctor.id, clinic.id, at(WED, "11:00")))
    next_week = await service.book(db, actor, doctor.id, clinic.id, at(date(2026, 10, 21), "11:00"))
    assert next_week.status == "booked"


async def test_a_cancelled_appointment_does_not_count_against_the_patient(db, clinic):
    doctor, session, patient, actor = await world(db, clinic)
    await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"), status="cancelled")
    appt = await service.book(db, actor, doctor.id, clinic.id, at(WED, "10:00"))
    assert appt.status == "booked"


# ── Who may be booked, and by whom ───────────────────────────────────────
async def test_a_pending_doctor_cannot_be_booked(db, clinic):
    _, _, _, actor = await world(db, clinic)
    user = await make_user(db, email="pending@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    pending = await doctor_of(db, user)
    await add_session(db, pending, clinic)
    with pytest.raises(NotFound):
        await service.book(db, actor, pending.id, clinic.id, at(WED, "10:00"))


async def test_a_doctor_unverified_after_the_page_opened_cannot_be_booked(db, clinic):
    # Review focus 5 (first half).
    doctor, _, _, actor = await world(db, clinic)
    doctor.verification_status = VerificationStatus.REJECTED
    doctor.verified_by = None
    doctor.verified_at = None
    await db.commit()
    with pytest.raises(NotFound):
        await service.book(db, actor, doctor.id, clinic.id, at(WED, "10:00"))


async def test_a_hospital_the_doctor_does_not_work_at_is_not_found(db, clinic, second_clinic):
    doctor, _, _, actor = await world(db, clinic)
    with pytest.raises(NotFound):
        await service.book(db, actor, doctor.id, second_clinic.id, at(WED, "10:00"))


async def test_an_inactive_affiliation_cannot_be_booked(db, clinic, second_clinic):
    doctor, _, _, actor = await world(db, clinic)
    await add_affiliation(db, doctor.id, second_clinic.id, status="inactive")
    await add_session(db, doctor, second_clinic)
    with pytest.raises(NotFound):
        await service.book(db, actor, doctor.id, second_clinic.id, at(WED, "10:00"))


async def test_only_a_patient_can_book(db, clinic):
    doctor, _, _, _ = await world(db, clinic)
    doctor_actor = Actor(user_id=doctor.user_id, role="doctor")
    with pytest.raises(NotFound):
        await service.book(db, doctor_actor, doctor.id, clinic.id, at(WED, "10:00"))


# ── Locking, the database backstop, audit ────────────────────────────────
async def test_booking_locks_the_doctor_first(db, clinic, monkeypatch):
    doctor, _, _, actor = await world(db, clinic)
    calls = []
    real = service.schedule_service.lock_doctor

    async def spy(session, doctor_id):
        calls.append(doctor_id)
        await real(session, doctor_id)

    monkeypatch.setattr(service.schedule_service, "lock_doctor", spy)
    await service.book(db, actor, doctor.id, clinic.id, at(WED, "10:00"))
    assert calls == [doctor.id]
    # The statement behind it is the per-doctor row lock (D7 AC-12 asserts the same thing).
    lock_sql = str(
        service.schedule_service.lock_statement(doctor.id).compile(dialect=postgresql.dialect())
    )
    assert "FOR UPDATE" in lock_sql


async def test_the_index_is_the_backstop_if_a_race_slips_past_the_checks(db, clinic, monkeypatch):
    doctor, session, _, actor = await world(db, clinic)
    other, _ = await another_patient(db, 2)
    await add_appointment(db, other, doctor, clinic, session, at(WED, "10:00"))

    async def blind(*args, **kwargs):  # a concurrent booking the checks did not see
        return {session.id: []}

    monkeypatch.setattr(service, "_active_starts", blind)
    await expect_refusal("taken", service.book(db, actor, doctor.id, clinic.id, at(WED, "10:00")))
    assert len(await stored(db)) == 1


async def test_a_booking_writes_one_audit_row_with_ids_only(db, clinic):
    doctor, _, patient, actor = await world(db, clinic)
    appt = await service.book(db, actor, doctor.id, clinic.id, at(WED, "10:00"))
    rows = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "appointment.book")))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert json.loads(rows[0].detail) == {
        "appointment_id": str(appt.id),
        "doctor_id": str(doctor.id),
        "clinic_id": str(clinic.id),
    }
    assert rows[0].subject_patient_id == patient.id and rows[0].actor_role == "patient"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_booking_book.py -q`
Expected: failures with `AttributeError: module 'app.booking.service' has no attribute 'book'`.

- [ ] **Step 3: Write the implementation**

In `backend/app/booking/service.py`, replace the import block (from `import uuid` down to the end of the `from .schemas import (...)` block) with:

```python
import uuid
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import writer as audit
from ..db import types as dbtypes
from ..db.models import (
    Appointment,
    Clinic,
    Doctor,
    DoctorAffiliation,
    DoctorSchedule,
    Patient,
    UserRole,
    VerificationStatus,
)
from ..deps import Actor
from ..errors import BookingRefused, NotFound
from ..schedule import service as schedule_service
from . import slots
from .schemas import (
    ACTIVE_STATUSES,
    CHANNEL_ONLINE,
    DEFAULT_MINUTES,
    HELD_STATUSES,
    HORIZON_DAYS,
    LEAD_MINUTES,
    SEARCH_LIMIT,
    ClinicRef,
    DayOptions,
    DoctorCard,
)
```

Append to `backend/app/booking/service.py`:

```python


# ── Booking ──────────────────────────────────────────────────────────────
async def _patient(session: AsyncSession, actor: Actor) -> Patient:
    """The caller's own patient row. Anyone but a patient is `NotFound`."""
    if actor.role != UserRole.PATIENT.value:
        raise NotFound()
    patient = (
        await session.execute(select(Patient).where(Patient.user_id == actor.user_id))
    ).scalar_one_or_none()
    if patient is None:
        raise NotFound()
    return patient


async def _check_patient_rules(
    session: AsyncSession,
    patient_id: uuid.UUID,
    doctor_id: uuid.UUID,
    start: datetime,
    minutes: int,
    *,
    ignore: uuid.UUID | None,
) -> None:
    """The patient cannot hold two overlapping appointments (any doctor) or two
    with one doctor on one Pakistan day. `ignore` leaves out the appointment a
    reschedule is replacing."""
    day_start = slots.to_utc(slots.to_local(start).date(), time(0, 0))
    stmt = (
        select(Appointment.doctor_id, Appointment.slot_start, DoctorSchedule.slot_minutes)
        .join(DoctorSchedule, DoctorSchedule.id == Appointment.schedule_id, isouter=True)
        .where(
            Appointment.patient_id == patient_id,
            Appointment.status.in_(HELD_STATUSES),
            Appointment.slot_start >= day_start - timedelta(hours=1),
            Appointment.slot_start < day_start + timedelta(days=1, hours=1),
        )
    )
    if ignore is not None:
        stmt = stmt.where(Appointment.id != ignore)
    held = [
        (held_doctor, slots.as_utc(held_start), held_minutes or DEFAULT_MINUTES)
        for held_doctor, held_start, held_minutes in (await session.execute(stmt)).all()
    ]
    for _, held_start, held_minutes in held:
        if slots.overlap(start, minutes, held_start, held_minutes):
            raise BookingRefused("overlap")
    for held_doctor, held_start, _ in held:
        if held_doctor == doctor_id and slots.same_local_day(start, held_start):
            raise BookingRefused("same_day")


async def _place(
    session: AsyncSession,
    actor: Actor,
    doctor_id: uuid.UUID,
    clinic_id: uuid.UUID,
    slot_start: datetime,
    *,
    replacing: Appointment | None,
    user_agent: str | None,
) -> Appointment:
    """Book `slot_start`; when `replacing` is given, cancel that appointment in
    the same transaction (a reschedule). Lock the doctor, re-read what is taken,
    check every rule, then write. Any refusal leaves everything untouched."""
    patient = await _patient(session, actor)
    await schedule_service.lock_doctor(session, doctor_id)

    found = await _visible_doctors(session, doctor_id)
    if doctor_id not in found or clinic_id not in found[doctor_id][1]:
        raise NotFound()

    now = _now()
    if replacing is not None:
        await session.refresh(replacing)
        if replacing.status != "booked" or slots.as_utc(replacing.slot_start) <= now:
            raise BookingRefused("not_cancellable")

    earliest = now + timedelta(minutes=LEAD_MINUTES)
    last_day = current_day() + timedelta(days=HORIZON_DAYS)
    start = slots.as_utc(slot_start)
    if start < earliest:
        raise BookingRefused("too_soon")
    day = slots.to_local(start).date()
    if day > last_day:
        raise BookingRefused("too_far")

    windows = await _windows(session, doctor_id, clinic_id, day, day)
    window = next((w for w in windows if slots.in_window(w, start)), None)
    if window is None:
        raise BookingRefused("not_offered")

    ignore = replacing.id if replacing is not None else None
    lo, hi = slots.window_bounds(window)
    booked = (await _active_starts(session, {window.schedule_id}, lo, hi, exclude=ignore))[
        window.schedule_id
    ]
    reason = slots.refusal(window, booked, start, earliest=earliest, last_day=last_day)
    if reason is not None:
        raise BookingRefused(reason)
    await _check_patient_rules(
        session, patient.id, doctor_id, start, window.slot_minutes, ignore=ignore
    )

    if replacing is not None:
        replacing.status = "cancelled"
        await session.flush()  # frees its time before the new row is inserted
    appointment = Appointment(
        patient_id=patient.id,
        doctor_id=doctor_id,
        clinic_id=clinic_id,
        schedule_id=window.schedule_id,
        slot_start=start,
        status="booked",
        booking_channel=CHANNEL_ONLINE,
    )
    session.add(appointment)
    try:
        await session.flush()
    except IntegrityError:
        # The partial unique index: a concurrent booking the checks did not see.
        await session.rollback()
        raise BookingRefused("taken") from None

    detail = {
        "appointment_id": str(appointment.id),
        "doctor_id": str(doctor_id),
        "clinic_id": str(clinic_id),
    }
    action = audit.APPOINTMENT_BOOK
    if replacing is not None:
        detail["replaces"] = str(replacing.id)
        action = audit.APPOINTMENT_RESCHEDULE
    await audit.write(
        session,
        action=action,
        actor_user_id=actor.user_id,
        actor_role=actor.role,
        subject_patient_id=patient.id,
        resource_type="appointment",
        ip_address=actor.ip_address,
        user_agent=user_agent,
        detail=detail,
    )
    await session.commit()
    return appointment


async def book(
    session: AsyncSession,
    actor: Actor,
    doctor_id: uuid.UUID,
    clinic_id: uuid.UUID,
    slot_start: datetime,
    *,
    user_agent: str | None = None,
) -> Appointment:
    """Book `slot_start` with a verified doctor at one of their live hospitals.
    Raises `NotFound` (not a patient, doctor or hospital not bookable) or
    `BookingRefused(reason)`."""
    return await _place(
        session, actor, doctor_id, clinic_id, slot_start, replacing=None, user_agent=user_agent
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_booking_book.py -q`
Expected: all pass (about 29).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check backend tests --fix && uv run ruff format backend tests
uv run pytest tests/test_booking_search.py tests/test_booking_book.py -q
git add backend/app/booking/service.py tests/test_booking_book.py
git commit -m "feat(booking): book a time under the doctor lock"
```

---

### Task 6: Service: cancel, reschedule, and the lists

**Files:**
- Modify: `backend/app/booking/service.py` (imports; append the functions below)
- Test: `tests/test_booking_manage.py`

**Interfaces:**
- Consumes: Task 5's `_patient`, `_place`; `schemas.AppointmentRow`, `schemas.DayRow` (Task 1); `schedule_service.my_doctor(session, actor) -> Doctor` and `schedule_service.lock_doctor` (D7).
- Produces: `async cancel(session, actor, appointment_id, *, user_agent=None) -> None`; `async reschedule(session, actor, appointment_id, clinic_id, slot_start, *, user_agent=None) -> Appointment`; `async my_appointments(session, actor) -> tuple[list[AppointmentRow], list[AppointmentRow]]` (upcoming soonest first, past newest first); `async appointment_for_patient(session, actor, appointment_id) -> AppointmentRow` (`NotFound` unless the caller's own); `async doctor_day(session, actor, day: date) -> list[DayRow]` (verified doctors only; every status, by time).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_booking_manage.py`:

```python
"""P5: cancel, reschedule, and the patient's and doctor's lists."""

from __future__ import annotations

import json
import uuid
from datetime import date

import pytest
from app.booking import service, slots
from app.booking.schemas import DayRow
from app.db.models import Appointment, AuditLog, UserRole
from app.deps import Actor
from app.errors import BookingRefused, NotFound
from sqlalchemy import select

from tests.booking_helpers import (
    THURSDAY,
    TODAY,
    WED,
    add_appointment,
    add_session,
    at,
    freeze,
    make_patient,
    verified_doctor,
)
from tests.conftest import add_affiliation, make_user


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    freeze(monkeypatch)


async def world(db, clinic, **session_kw):
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic, **session_kw)
    _, patient, actor = await make_patient(db)
    return doctor, session, patient, actor


async def other_patient(db, n=2):
    _, patient, actor = await make_patient(db, email=f"p{n}@x.com", name=f"Patient {n}")
    return patient, actor


async def refreshed(db, appointment) -> Appointment:
    return (
        await db.execute(
            select(Appointment)
            .where(Appointment.id == appointment.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def refusal(coro) -> str:
    with pytest.raises(BookingRefused) as exc:
        await coro
    return exc.value.reason


# ── Cancel ───────────────────────────────────────────────────────────────
async def test_a_patient_cancels_their_own_appointment(db, clinic):
    doctor, session, patient, actor = await world(db, clinic)
    appt = await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    await service.cancel(db, actor, appt.id)
    assert (await refreshed(db, appt)).status == "cancelled"


async def test_cancelling_frees_the_time_for_someone_else(db, clinic):
    doctor, session, patient, actor = await world(db, clinic)
    _, other_actor = await other_patient(db)
    appt = await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    await service.cancel(db, actor, appt.id)
    booked = await service.book(db, other_actor, doctor.id, clinic.id, at(WED, "10:00"))
    assert booked.status == "booked"


async def test_someone_elses_or_a_missing_appointment_is_not_found(db, clinic):
    doctor, session, _, actor = await world(db, clinic)
    other, _ = await other_patient(db)
    theirs = await add_appointment(db, other, doctor, clinic, session, at(WED, "10:00"))
    with pytest.raises(NotFound):
        await service.cancel(db, actor, theirs.id)
    with pytest.raises(NotFound):
        await service.cancel(db, actor, uuid.uuid4())
    assert (await refreshed(db, theirs)).status == "booked"


@pytest.mark.parametrize("status", ["cancelled", "done", "checked_in"])
async def test_only_a_booked_appointment_can_be_cancelled(db, clinic, status):
    doctor, session, patient, actor = await world(db, clinic)
    appt = await add_appointment(
        db, patient, doctor, clinic, session, at(WED, "10:00"), status=status
    )
    assert await refusal(service.cancel(db, actor, appt.id)) == "not_cancellable"


async def test_an_appointment_that_has_started_cannot_be_cancelled(db, clinic):
    doctor, session, patient, actor = await world(db, clinic)
    # Now is Monday 13:00 in Pakistan.
    appt = await add_appointment(db, patient, doctor, clinic, session, at(TODAY, "09:00"))
    assert await refusal(service.cancel(db, actor, appt.id)) == "not_cancellable"


async def test_a_cancel_writes_one_audit_row_with_ids_only(db, clinic):
    doctor, session, patient, actor = await world(db, clinic)
    appt = await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    await service.cancel(db, actor, appt.id)
    rows = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "appointment.cancel")))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert json.loads(rows[0].detail) == {
        "appointment_id": str(appt.id),
        "doctor_id": str(doctor.id),
        "clinic_id": str(clinic.id),
    }


async def test_only_a_patient_can_cancel(db, clinic):
    doctor, session, patient, _ = await world(db, clinic)
    appt = await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    with pytest.raises(NotFound):
        await service.cancel(db, Actor(user_id=doctor.user_id, role="doctor"), appt.id)


async def test_cancel_and_reschedule_lock_the_doctor_first(db, clinic, monkeypatch):
    doctor, session, patient, actor = await world(db, clinic)
    calls = []
    real = service.schedule_service.lock_doctor

    async def spy(session_, doctor_id):
        calls.append(doctor_id)
        await real(session_, doctor_id)

    monkeypatch.setattr(service.schedule_service, "lock_doctor", spy)
    first = await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    await service.cancel(db, actor, first.id)
    second = await add_appointment(db, patient, doctor, clinic, session, at(WED, "11:00"))
    await service.reschedule(db, actor, second.id, clinic.id, at(WED, "12:00"))
    assert calls == [doctor.id, doctor.id]


# ── Reschedule ───────────────────────────────────────────────────────────
async def test_reschedule_books_the_new_time_and_cancels_the_old(db, clinic):
    doctor, session, patient, actor = await world(db, clinic)
    old = await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    new = await service.reschedule(db, actor, old.id, clinic.id, at(date(2026, 10, 21), "11:00"))
    assert new.id != old.id and new.status == "booked" and new.patient_id == patient.id
    assert slots.as_utc(new.slot_start) == at(date(2026, 10, 21), "11:00")
    assert (await refreshed(db, old)).status == "cancelled"


async def test_reschedule_to_another_time_with_the_same_doctor_the_same_day(db, clinic):
    # Review focus 1: the old booking must not block its own replacement.
    doctor, session, patient, actor = await world(db, clinic)
    old = await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    new = await service.reschedule(db, actor, old.id, clinic.id, at(WED, "11:00"))
    assert new.status == "booked" and (await refreshed(db, old)).status == "cancelled"


async def test_reschedule_into_a_full_session_the_old_booking_was_part_of(db, clinic):
    # The old booking leaves the count before the new one is checked, so a
    # session at its cap still accepts the move within itself.
    doctor, session, patient, actor = await world(db, clinic, cap=1)
    old = await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    new = await service.reschedule(db, actor, old.id, clinic.id, at(WED, "10:15"))
    assert new.status == "booked" and (await refreshed(db, old)).status == "cancelled"


async def test_a_failed_reschedule_leaves_the_old_booking_untouched(db, clinic):
    doctor, session, patient, actor = await world(db, clinic)
    other, _ = await other_patient(db)
    old = await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    await add_appointment(db, other, doctor, clinic, session, at(WED, "11:00"))
    assert await refusal(service.reschedule(db, actor, old.id, clinic.id, at(WED, "11:00"))) == "taken"
    assert (await refreshed(db, old)).status == "booked"
    assert len((await db.execute(select(Appointment))).scalars().all()) == 2


async def test_reschedule_to_another_hospital_of_the_same_doctor(db, clinic, second_clinic):
    doctor, session, patient, actor = await world(db, clinic)
    await add_affiliation(db, doctor.id, second_clinic.id)
    await add_session(db, doctor, second_clinic)
    old = await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    new = await service.reschedule(db, actor, old.id, second_clinic.id, at(WED, "10:15"))
    assert new.clinic_id == second_clinic.id and new.doctor_id == doctor.id


async def test_reschedule_of_someone_elses_appointment_is_not_found(db, clinic):
    doctor, session, _, actor = await world(db, clinic)
    other, _ = await other_patient(db)
    theirs = await add_appointment(db, other, doctor, clinic, session, at(WED, "10:00"))
    with pytest.raises(NotFound):
        await service.reschedule(db, actor, theirs.id, clinic.id, at(WED, "11:00"))


async def test_a_cancelled_appointment_cannot_be_rescheduled(db, clinic):
    doctor, session, patient, actor = await world(db, clinic)
    old = await add_appointment(
        db, patient, doctor, clinic, session, at(WED, "10:00"), status="cancelled"
    )
    reason = await refusal(service.reschedule(db, actor, old.id, clinic.id, at(WED, "11:00")))
    assert reason == "not_cancellable"


async def test_an_appointment_that_has_started_cannot_be_rescheduled(db, clinic):
    doctor, session, patient, actor = await world(db, clinic)
    old = await add_appointment(db, patient, doctor, clinic, session, at(TODAY, "09:00"))
    reason = await refusal(service.reschedule(db, actor, old.id, clinic.id, at(WED, "11:00")))
    assert reason == "not_cancellable"
    assert (await refreshed(db, old)).status == "booked"


async def test_a_reschedule_writes_one_audit_row_naming_what_it_replaces(db, clinic):
    doctor, session, patient, actor = await world(db, clinic)
    old = await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    new = await service.reschedule(db, actor, old.id, clinic.id, at(WED, "11:00"))
    rows = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "appointment.reschedule")))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    detail = json.loads(rows[0].detail)
    assert detail["appointment_id"] == str(new.id) and detail["replaces"] == str(old.id)


# ── The patient's lists ──────────────────────────────────────────────────
async def test_my_appointments_splits_upcoming_and_past(db, clinic):
    doctor, session, patient, actor = await world(db, clinic)
    later = await add_appointment(
        db, patient, doctor, clinic, session, at(date(2026, 10, 21), "10:00")
    )
    soon = await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    await add_appointment(db, patient, doctor, clinic, session, at(TODAY, "09:00"))  # started
    await add_appointment(
        db, patient, doctor, clinic, session, at(WED, "11:00"), status="cancelled"
    )
    await add_appointment(db, patient, doctor, clinic, session, at(WED, "12:00"), status="done")
    upcoming, past = await service.my_appointments(db, actor)
    assert [r.id for r in upcoming] == [soon.id, later.id]
    assert len(past) == 3 and past[0].starts_at >= past[-1].starts_at
    assert all(r.can_change for r in upcoming)
    assert not any(r.can_change for r in past)


async def test_a_row_carries_names_and_pakistan_time(db, clinic):
    doctor, session, patient, actor = await world(db, clinic)
    await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    (row,), _ = await service.my_appointments(db, actor)
    assert row.doctor_name == "Dr Booking" and row.specialty == "Cardiology"
    assert row.clinic_name == "Shifa International" and row.status == "booked"
    assert row.starts_at.date() == WED and row.starts_at.strftime("%H:%M") == "10:00"


async def test_my_appointments_never_includes_other_patients(db, clinic):
    doctor, session, _, actor = await world(db, clinic)
    other, _ = await other_patient(db)
    await add_appointment(db, other, doctor, clinic, session, at(WED, "10:00"))
    assert await service.my_appointments(db, actor) == ([], [])


async def test_appointment_for_patient_is_own_only(db, clinic):
    doctor, session, patient, actor = await world(db, clinic)
    other, _ = await other_patient(db)
    mine = await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    theirs = await add_appointment(db, other, doctor, clinic, session, at(WED, "11:00"))
    assert (await service.appointment_for_patient(db, actor, mine.id)).id == mine.id
    with pytest.raises(NotFound):
        await service.appointment_for_patient(db, actor, theirs.id)


# ── The doctor's day ─────────────────────────────────────────────────────
def doctor_actor(doctor) -> Actor:
    return Actor(user_id=doctor.user_id, role="doctor", is_verified_doctor=True)


async def test_the_doctors_day_lists_name_time_and_status_by_time(db, clinic):
    doctor, session, patient, _ = await world(db, clinic)
    other, _ = await other_patient(db, 3)
    await add_appointment(db, other, doctor, clinic, session, at(WED, "11:00"), status="cancelled")
    await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    rows = await service.doctor_day(db, doctor_actor(doctor), WED)
    assert [(r.patient_name, r.starts_at.strftime("%H:%M"), r.status) for r in rows] == [
        ("Pat Booking", "10:00", "booked"),
        ("Patient 3", "11:00", "cancelled"),
    ]


def test_the_doctors_day_has_only_name_time_and_status():
    # Booking is not consent: nothing else about the patient may be added here.
    assert set(DayRow.__dataclass_fields__) == {"starts_at", "patient_name", "status"}


async def test_the_doctors_day_excludes_other_days_and_other_doctors(db, clinic):
    doctor, session, patient, _ = await world(db, clinic)
    rival = await verified_doctor(db, clinic, email="rival@x.com", name="Dr Rival")
    rival_session = await add_session(db, rival, clinic)
    other, _ = await other_patient(db)
    await add_appointment(db, patient, doctor, clinic, session, at(date(2026, 10, 21), "10:00"))
    await add_appointment(db, other, rival, clinic, rival_session, at(WED, "10:00"))
    assert await service.doctor_day(db, doctor_actor(doctor), WED) == []


async def test_a_time_just_after_midnight_belongs_to_its_pakistan_day(db, clinic):
    # Review focus 2: 00:15 on Thursday the 15th is 19:15 UTC on the 14th.
    doctor, session, patient, _ = await world(
        db, clinic, weekday=THURSDAY, start="00:00", end="01:00"
    )
    thursday = date(2026, 10, 15)
    await add_appointment(db, patient, doctor, clinic, session, at(thursday, "00:15"))
    assert len(await service.doctor_day(db, doctor_actor(doctor), thursday)) == 1
    assert await service.doctor_day(db, doctor_actor(doctor), WED) == []


async def test_a_pending_doctor_or_a_patient_sees_no_appointments(db, clinic):
    _, _, _, actor = await world(db, clinic)
    user = await make_user(db, email="pending@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    with pytest.raises(NotFound):
        await service.doctor_day(db, Actor(user_id=user.id, role="doctor"), WED)
    with pytest.raises(NotFound):
        await service.doctor_day(db, actor, WED)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_booking_manage.py -q`
Expected: failures with `AttributeError: module 'app.booking.service' has no attribute 'cancel'`.

- [ ] **Step 3: Write the implementation**

In `backend/app/booking/service.py`, change the `from .schemas import (...)` block to also import `AppointmentRow` and `DayRow`:

```python
from .schemas import (
    ACTIVE_STATUSES,
    CHANNEL_ONLINE,
    DEFAULT_MINUTES,
    HELD_STATUSES,
    HORIZON_DAYS,
    LEAD_MINUTES,
    SEARCH_LIMIT,
    AppointmentRow,
    ClinicRef,
    DayOptions,
    DayRow,
    DoctorCard,
)
```

Append to `backend/app/booking/service.py`:

```python


# ── Cancel and reschedule ────────────────────────────────────────────────
async def _own_appointment(
    session: AsyncSession, patient_id: uuid.UUID, appointment_id: uuid.UUID
) -> Appointment:
    appointment = (
        await session.execute(
            select(Appointment).where(
                Appointment.id == appointment_id, Appointment.patient_id == patient_id
            )
        )
    ).scalar_one_or_none()
    if appointment is None:
        raise NotFound()
    return appointment


async def cancel(
    session: AsyncSession,
    actor: Actor,
    appointment_id: uuid.UUID,
    *,
    user_agent: str | None = None,
) -> None:
    """Cancel the caller's own `booked` appointment, any time before it starts.
    The row is kept (`cancelled`); the time and the cap are freed."""
    patient = await _patient(session, actor)
    appointment = await _own_appointment(session, patient.id, appointment_id)
    await schedule_service.lock_doctor(session, appointment.doctor_id)
    await session.refresh(appointment)
    if appointment.status != "booked" or slots.as_utc(appointment.slot_start) <= _now():
        raise BookingRefused("not_cancellable")
    appointment.status = "cancelled"
    await audit.write(
        session,
        action=audit.APPOINTMENT_CANCEL,
        actor_user_id=actor.user_id,
        actor_role=actor.role,
        subject_patient_id=patient.id,
        resource_type="appointment",
        ip_address=actor.ip_address,
        user_agent=user_agent,
        detail={
            "appointment_id": str(appointment.id),
            "doctor_id": str(appointment.doctor_id),
            "clinic_id": str(appointment.clinic_id),
        },
    )
    await session.commit()


async def reschedule(
    session: AsyncSession,
    actor: Actor,
    appointment_id: uuid.UUID,
    clinic_id: uuid.UUID,
    slot_start: datetime,
    *,
    user_agent: str | None = None,
) -> Appointment:
    """Move the caller's own appointment to `slot_start` with the same doctor
    (at any of their live hospitals): book the new time and cancel the old one
    in one transaction. A refused new time leaves the old booking untouched."""
    patient = await _patient(session, actor)
    old = await _own_appointment(session, patient.id, appointment_id)
    return await _place(
        session, actor, old.doctor_id, clinic_id, slot_start, replacing=old, user_agent=user_agent
    )


# ── Lists ────────────────────────────────────────────────────────────────
async def _patient_rows(session: AsyncSession, patient_id: uuid.UUID) -> list[AppointmentRow]:
    stmt = (
        select(Appointment, Doctor, Clinic)
        .join(Doctor, Doctor.id == Appointment.doctor_id)
        .join(Clinic, Clinic.id == Appointment.clinic_id)
        .where(Appointment.patient_id == patient_id)
        .order_by(Appointment.slot_start)
    )
    now = _now()
    rows: list[AppointmentRow] = []
    for appointment, doctor, clinic in (await session.execute(stmt)).all():
        start = slots.as_utc(appointment.slot_start)
        rows.append(
            AppointmentRow(
                id=appointment.id,
                doctor_id=doctor.id,
                doctor_name=doctor.full_name,
                specialty=doctor.specialty,
                clinic_id=clinic.id,
                clinic_name=clinic.name,
                starts_at=start.astimezone(dbtypes.PKT),
                status=appointment.status,
                upcoming=appointment.status in HELD_STATUSES and start >= now,
                can_change=appointment.status == "booked" and start > now,
            )
        )
    return rows


async def my_appointments(
    session: AsyncSession, actor: Actor
) -> tuple[list[AppointmentRow], list[AppointmentRow]]:
    """(upcoming, past) for the caller. Upcoming: booked or checked in and not
    yet started, soonest first. Past: everything else, newest first."""
    patient = await _patient(session, actor)
    rows = await _patient_rows(session, patient.id)
    upcoming = [r for r in rows if r.upcoming]
    past = sorted((r for r in rows if not r.upcoming), key=lambda r: r.starts_at, reverse=True)
    return upcoming, past


async def appointment_for_patient(
    session: AsyncSession, actor: Actor, appointment_id: uuid.UUID
) -> AppointmentRow:
    patient = await _patient(session, actor)
    for row in await _patient_rows(session, patient.id):
        if row.id == appointment_id:
            return row
    raise NotFound()


async def doctor_day(session: AsyncSession, actor: Actor, day: date) -> list[DayRow]:
    """A verified doctor's appointments on one Pakistan day, by time: name, time
    and status only (booking never grants record access). Anyone else, and a
    doctor who is not verified, gets `NotFound`."""
    if actor.role != UserRole.DOCTOR.value:
        raise NotFound()
    doctor = await schedule_service.my_doctor(session, actor)
    if doctor.verification_status != VerificationStatus.VERIFIED:
        raise NotFound()
    lo = slots.to_utc(day, time(0, 0))
    stmt = (
        select(Appointment.slot_start, Appointment.status, Patient.full_name)
        .join(Patient, Patient.id == Appointment.patient_id)
        .where(
            Appointment.doctor_id == doctor.id,
            Appointment.slot_start >= lo,
            Appointment.slot_start < lo + timedelta(days=1),
        )
        .order_by(Appointment.slot_start)
    )
    return [
        DayRow(starts_at=slots.to_local(start), patient_name=name, status=status)
        for start, status, name in (await session.execute(stmt)).all()
    ]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_booking_manage.py -q`
Expected: all pass (about 29).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check backend tests --fix && uv run ruff format backend tests
uv run pytest tests/test_booking_slots.py tests/test_booking_search.py tests/test_booking_book.py tests/test_booking_manage.py -q
git add backend/app/booking/service.py tests/test_booking_manage.py
git commit -m "feat(booking): cancel, reschedule and the appointment lists"
```

---

### Task 7: Patient pages: find a doctor and see their free times

**Files:**
- Create: `backend/app/booking/display.py`
- Create: `backend/app/web/booking.py`
- Create: `frontend/templates/booking/find.html`, `frontend/templates/booking/doctor.html`
- Modify: `backend/app/main.py` (include the router), `backend/app/web/router.py` (`_patient_nav`: one entry), `tests/booking_helpers.py` (a `login` helper), `backend/app/i18n/messages/en.json`, `ur.json`
- Test: `tests/test_booking_display.py`, `tests/test_booking_web_find.py`

**Interfaces:**
- Consumes: `service.search_doctors`, `service.specialty_options`, `service.get_doctor`, `service.list_options`, `service.appointment_for_patient` (Tasks 4 and 6); `schemas.DoctorCard`, `ClinicRef`, `DayOptions`, `AppointmentRow` (Task 1); `slots.format_local`, `slots.to_local` (Task 1); from `web/router.py`: `_area`, `_patient_nav`, `_render`, `login_redirect`.
- Produces (`display.py`): `STATUS_BADGE`, `status_label(status, locale) -> str`, `status_badge(status) -> str`, `hhmm(value: datetime) -> str` (Pakistan `HH:MM`). Produces (`web/booking.py`): `router`; `_guard(request, locale, actor)`; `_ctx(loc, title_key, *, current)`; `_parse_id(value) -> UUID | None`; `_moving_row(session, actor, doctor_id, raw) -> AppointmentRow | None`; `_render_find(...)`; `_render_doctor(request, loc, session, actor, doctor_id, *, clinic_raw="", moving_raw="", status_code=200, page_error=None)`; `_confirm_url(...)`. Tasks 8 to 10 extend this module and reuse these.
- Routes: `GET /{locale}/patient/doctors` (query `q`, `specialty`, `notice`) and `GET /{locale}/patient/doctors/{doctor_id}` (query `clinic`, `moving`).

- [ ] **Step 1: Add the `login` helper to the shared test helpers**

In `tests/booking_helpers.py`, change the import line `from tests.conftest import make_user` to:

```python
from tests.conftest import TEST_PASSWORD, make_user
```

and append at the end of the file:

```python


async def login(client, email: str) -> None:
    """Sign in through the real login endpoint; the client keeps the cookies."""
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": TEST_PASSWORD})
    assert r.status_code == 200, r.text
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_booking_display.py`:

```python
"""P5: the small display helpers (pure functions)."""

from __future__ import annotations

from datetime import datetime

import pytest
from app.booking import display

from tests.booking_helpers import WED, at


@pytest.mark.parametrize(
    ("status", "en", "ur"),
    [
        ("booked", "Confirmed", "کنفرم"),
        ("checked_in", "Checked in", "چیک اِن"),
        ("done", "Completed", "مکمل"),
        ("cancelled", "Cancelled", "منسوخ"),
    ],
)
def test_each_status_has_a_label_in_both_languages(status, en, ur):
    assert display.status_label(status, "en") == en
    assert display.status_label(status, "ur") == ur


def test_an_unknown_status_is_shown_as_stored_never_as_a_catalogue_key():
    assert display.status_label("something_new", "en") == "something_new"


def test_badges_by_status():
    assert display.status_badge("booked") == "badge--ok"
    assert display.status_badge("cancelled") == "badge--err"
    assert display.status_badge("done") == "badge--neutral"
    assert display.status_badge("something_new") == "badge--neutral"


def test_hhmm_is_pakistan_time():
    assert display.hhmm(at(WED, "00:15")) == "00:15"  # 19:15 UTC the day before
    assert display.hhmm(at(WED, "13:05")) == "13:05"


def test_hhmm_reads_a_naive_value_as_utc():
    assert display.hhmm(datetime(2026, 10, 14, 5, 0)) == "10:00"
```

Create `tests/test_booking_web_find.py`:

```python
"""P5 web: finding a doctor and seeing their free times."""

from __future__ import annotations

import uuid

import pytest
from app.db.models import UserRole

from tests.booking_helpers import (
    THURSDAY,
    TODAY,
    WED,
    add_appointment,
    add_session,
    at,
    freeze,
    login,
    make_patient,
    verified_doctor,
)
from tests.conftest import add_affiliation, doctor_of, make_user

FIND = "/en/patient/doctors"


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    freeze(monkeypatch)


@pytest.fixture
async def me(db):
    _, patient, _ = await make_patient(db)
    return patient


@pytest.fixture
async def patient_client(client, me):
    await login(client, "booking.pat@x.com")
    return client


# ── Guards ───────────────────────────────────────────────────────────────
async def test_anonymous_goes_to_login(client):
    r = await client.get(FIND)
    assert r.status_code == 303
    assert r.headers["location"] == "/en/login?next=/en/patient/doctors"


@pytest.mark.parametrize(
    ("role", "area"), [(UserRole.DOCTOR, "doctor"), (UserRole.CLINIC_ADMIN, "admin")]
)
async def test_other_roles_go_to_their_own_area(client, db, clinic, role, area):
    await make_user(db, email="other@x.com", role=role, clinic_id=clinic.id)
    await login(client, "other@x.com")
    for path in (FIND, f"{FIND}/{uuid.uuid4()}"):
        r = await client.get(path)
        assert r.status_code == 303
        assert r.headers["location"] == f"/en/{area}"


# ── Find ─────────────────────────────────────────────────────────────────
async def test_the_page_lists_verified_doctors(patient_client, db, clinic):
    doctor = await verified_doctor(db, clinic)
    r = await patient_client.get(FIND)
    assert r.status_code == 200
    assert "Dr Booking" in r.text and "Cardiology" in r.text and "Shifa International" in r.text
    assert f"/en/patient/doctors/{doctor.id}" in r.text
    assert "Verified" in r.text


async def test_a_pending_doctor_is_not_listed(patient_client, db, clinic):
    await make_user(db, email="pending@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    r = await patient_client.get(FIND)
    assert r.status_code == 200
    assert "No verified doctors match your search." in r.text


async def test_search_by_name_and_by_hospital(patient_client, db, clinic):
    await verified_doctor(db, clinic)
    assert "Dr Booking" in (await patient_client.get(f"{FIND}?q=book")).text
    assert "Dr Booking" in (await patient_client.get(f"{FIND}?q=shifa")).text
    nothing = await patient_client.get(f"{FIND}?q=zzz")
    assert "Dr Booking" not in nothing.text and "No verified doctors match" in nothing.text


async def test_specialty_chips_filter_and_an_unknown_specialty_is_ignored(
    patient_client, db, clinic
):
    await verified_doctor(db, clinic, email="a.dr@x.com", name="Dr Alpha", specialty="Cardiology")
    await verified_doctor(db, clinic, email="b.dr@x.com", name="Dr Beta", specialty="Dermatology")
    page = (await patient_client.get(FIND)).text
    assert "specialty=Cardiology" in page and "specialty=Dermatology" in page
    only = await patient_client.get(f"{FIND}?specialty=Dermatology")
    assert "Dr Beta" in only.text and "Dr Alpha" not in only.text
    ignored = await patient_client.get(f"{FIND}?specialty=Nonsense")
    assert "Dr Alpha" in ignored.text and "Dr Beta" in ignored.text


async def test_names_are_escaped(patient_client, db, clinic):
    await verified_doctor(db, clinic, name="<script>alert(1)</script>")
    r = await patient_client.get(FIND)
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text


async def test_a_very_long_search_is_cut_not_an_error(patient_client, db, clinic):
    await verified_doctor(db, clinic)
    r = await patient_client.get(f"{FIND}?q={'x' * 500}")
    assert r.status_code == 200
    assert f'value="{"x" * 80}"' in r.text and "x" * 81 not in r.text


async def test_an_unknown_notice_is_ignored_and_a_known_one_is_shown(patient_client, db, clinic):
    await verified_doctor(db, clinic)
    assert "cannot be booked" not in (await patient_client.get(f"{FIND}?notice=bogus")).text
    assert "cannot be booked" in (await patient_client.get(f"{FIND}?notice=unavailable")).text


async def test_the_urdu_page_is_right_to_left(patient_client, db, clinic):
    await verified_doctor(db, clinic)
    r = await patient_client.get("/ur/patient/doctors")
    assert r.status_code == 200
    assert 'dir="rtl"' in r.text and "ڈاکٹر تلاش کریں" in r.text
    assert 'href="/en/patient/doctors"' in r.text  # the language switch


async def test_the_patient_nav_has_find_a_doctor(patient_client):
    r = await patient_client.get("/en/patient")
    assert 'href="/en/patient/doctors"' in r.text


# ── A doctor's free times ────────────────────────────────────────────────
async def test_the_doctor_page_lists_only_free_times(patient_client, db, clinic):
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic)
    _, other, _ = await make_patient(db, email="p2@x.com", name="Patient 2")
    await add_appointment(db, other, doctor, clinic, session, at(WED, "09:00"))
    r = await patient_client.get(f"{FIND}/{doctor.id}")
    assert r.status_code == 200
    assert "at=2026-10-14T09:00" not in r.text  # taken
    assert "at=2026-10-14T09:15" in r.text and "at=2026-10-21T12:45" in r.text
    assert "Wednesday" in r.text and "2026-10-14" in r.text
    assert "at=2026-10-28" not in r.text  # beyond 14 days


async def test_each_hospital_has_its_own_times(patient_client, db, clinic, second_clinic):
    doctor = await verified_doctor(db, clinic)
    await add_affiliation(db, doctor.id, second_clinic.id)
    await add_session(db, doctor, clinic, weekday=2, start="09:00", end="10:00")
    await add_session(db, doctor, second_clinic, weekday=3, start="09:00", end="10:00")
    here = (await patient_client.get(f"{FIND}/{doctor.id}?clinic={clinic.id}")).text
    there = (await patient_client.get(f"{FIND}/{doctor.id}?clinic={second_clinic.id}")).text
    assert "at=2026-10-14T09:00" in here and "at=2026-10-15T09:00" not in here
    assert "at=2026-10-15T09:00" in there and "at=2026-10-14T09:00" not in there
    assert f"clinic={clinic.id}" in here and f"clinic={second_clinic.id}" in here


async def test_an_unknown_hospital_falls_back_to_the_first(patient_client, db, clinic):
    doctor = await verified_doctor(db, clinic)
    await add_session(db, doctor, clinic)
    r = await patient_client.get(f"{FIND}/{doctor.id}?clinic=not-a-uuid")
    assert r.status_code == 200 and "at=2026-10-14T09:00" in r.text


async def test_a_doctor_with_no_sessions_says_so(patient_client, db, clinic):
    doctor = await verified_doctor(db, clinic)
    r = await patient_client.get(f"{FIND}/{doctor.id}")
    assert r.status_code == 200 and "No free times at this hospital" in r.text


@pytest.mark.parametrize("target", ["not-a-uuid", str(uuid.uuid4())])
async def test_an_unknown_doctor_shows_the_find_page_with_a_404(patient_client, target):
    r = await patient_client.get(f"{FIND}/{target}")
    assert r.status_code == 404 and "find that" in r.text


async def test_a_pending_doctor_cannot_be_opened(patient_client, db, clinic):
    user = await make_user(db, email="pending@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    pending = await doctor_of(db, user)
    r = await patient_client.get(f"{FIND}/{pending.id}")
    assert r.status_code == 404


async def test_a_time_just_after_midnight_is_shown_under_its_pakistan_date(
    patient_client, db, clinic
):
    # Review focus 2: 00:15 on Thursday the 15th is 19:15 UTC on the 14th.
    doctor = await verified_doctor(db, clinic)
    await add_session(db, doctor, clinic, weekday=THURSDAY, start="00:00", end="01:00")
    r = await patient_client.get(f"{FIND}/{doctor.id}")
    assert "at=2026-10-15T00:15" in r.text
    assert "Thursday" in r.text and "2026-10-15" in r.text
    assert ">00:15<" in r.text


# ── The booking being moved ──────────────────────────────────────────────
async def test_a_booking_being_moved_is_named_and_carried_in_the_links(
    patient_client, db, clinic, me
):
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic)
    mine = await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    r = await patient_client.get(f"{FIND}/{doctor.id}?moving={mine.id}")
    assert "Moving your appointment on 2026-10-14 at 10:00" in r.text
    assert f"moving={mine.id}" in r.text


async def test_a_moving_id_that_is_not_the_callers_or_not_changeable_is_ignored(
    patient_client, db, clinic, me
):
    doctor = await verified_doctor(db, clinic)
    rival = await verified_doctor(db, clinic, email="rival@x.com", name="Dr Rival")
    session = await add_session(db, doctor, clinic)
    rival_session = await add_session(db, rival, clinic)
    _, other, _ = await make_patient(db, email="p2@x.com", name="Patient 2")
    foreign = await add_appointment(db, other, doctor, clinic, session, at(WED, "10:00"))
    started = await add_appointment(db, me, doctor, clinic, session, at(TODAY, "09:00"))
    elsewhere = await add_appointment(db, me, rival, clinic, rival_session, at(WED, "11:00"))
    for moving in (foreign.id, started.id, elsewhere.id, "junk"):
        r = await patient_client.get(f"{FIND}/{doctor.id}?moving={moving}")
        assert r.status_code == 200
        assert "Moving your appointment" not in r.text and "moving=" not in r.text
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_booking_display.py tests/test_booking_web_find.py -q`
Expected: collection errors (`cannot import name 'display'`) and 404s from the missing routes.

- [ ] **Step 4: Add the i18n keys**

Run the i18n helper from the File Structure section with these dicts:

```python
EN = {
    "nav.find_doctor": "Find a doctor",
    "booking.status.booked": "Confirmed",
    "booking.status.checked_in": "Checked in",
    "booking.status.done": "Completed",
    "booking.status.cancelled": "Cancelled",
    "booking.field.doctor": "Doctor",
    "booking.field.hospital": "Hospital",
    "booking.field.date": "Date",
    "booking.field.time": "Time",
    "booking.field.status": "Status",
    "booking.find.title": "Find a doctor",
    "booking.find.subtitle": "Search verified doctors by name or hospital, then pick a time.",
    "booking.find.search_label": "Doctor or hospital",
    "booking.find.search_action": "Search",
    "booking.find.clear": "Clear search",
    "booking.find.specialties": "Specialty",
    "booking.find.all": "All",
    "booking.find.verified": "Verified",
    "booking.find.results": "Doctors found: {count}",
    "booking.find.empty": "No verified doctors match your search.",
    "booking.find.view": "See times",
    "booking.notice.unavailable": "That doctor cannot be booked right now.",
    "booking.doctor.title": "Book an appointment",
    "booking.doctor.pick": "Free times for the next 14 days",
    "booking.doctor.no_times": "No free times at this hospital in the next 14 days.",
    "booking.doctor.back": "Back to doctors",
    "booking.doctor.moving": "Moving your appointment on {date} at {time}. Pick a new time.",
    "booking.doctor.keep": "Keep my current appointment",
}
UR = {
    "nav.find_doctor": "ڈاکٹر تلاش کریں",
    "booking.status.booked": "کنفرم",
    "booking.status.checked_in": "چیک اِن",
    "booking.status.done": "مکمل",
    "booking.status.cancelled": "منسوخ",
    "booking.field.doctor": "ڈاکٹر",
    "booking.field.hospital": "ہسپتال",
    "booking.field.date": "تاریخ",
    "booking.field.time": "وقت",
    "booking.field.status": "حالت",
    "booking.find.title": "ڈاکٹر تلاش کریں",
    "booking.find.subtitle": "تصدیق شدہ ڈاکٹروں کو نام یا ہسپتال سے تلاش کریں، پھر وقت منتخب کریں۔",
    "booking.find.search_label": "ڈاکٹر یا ہسپتال",
    "booking.find.search_action": "تلاش کریں",
    "booking.find.clear": "تلاش صاف کریں",
    "booking.find.specialties": "شعبہ",
    "booking.find.all": "سب",
    "booking.find.verified": "تصدیق شدہ",
    "booking.find.results": "ملنے والے ڈاکٹر: {count}",
    "booking.find.empty": "آپ کی تلاش سے کوئی تصدیق شدہ ڈاکٹر نہیں ملا۔",
    "booking.find.view": "اوقات دیکھیں",
    "booking.notice.unavailable": "اس ڈاکٹر کی بکنگ ابھی ممکن نہیں۔",
    "booking.doctor.title": "اپائنٹمنٹ بک کریں",
    "booking.doctor.pick": "اگلے 14 دنوں کے خالی اوقات",
    "booking.doctor.no_times": "اس ہسپتال میں اگلے 14 دنوں میں کوئی خالی وقت نہیں۔",
    "booking.doctor.back": "ڈاکٹروں کی فہرست پر واپس",
    "booking.doctor.moving": "آپ {date} کو {time} بجے کی اپائنٹمنٹ کا وقت بدل رہے ہیں۔ نیا وقت منتخب کریں۔",
    "booking.doctor.keep": "موجودہ اپائنٹمنٹ برقرار رکھیں",
}
```

Expected: prints `ok`.

- [ ] **Step 5: Write the display helpers**

Create `backend/app/booking/display.py`:

```python
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
```

- [ ] **Step 6: Write the web module**

Create `backend/app/web/booking.py`:

```python
"""Server-rendered pages for appointment booking (P5, FR15, FR16).

Every rule lives in `booking/service.py`; this module only turns forms into
service calls and results into pages, mirroring `web/documents.py`. Every action
works without JavaScript: a GET shows a page, a POST acts and redirects (PRG),
and a refused action shows the same page again with fresh times and the reason.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..booking import display, service, slots
from ..booking.schemas import AppointmentRow, ClinicRef
from ..db.models import UserRole
from ..deps import Actor, OptionalActorDep, SessionDep
from ..errors import NotFound
from ..i18n.catalogue import normalise_locale, translate
from .router import _area, _patient_nav, _render, login_redirect

router = APIRouter(tags=["web-booking"])

QUERY_MAX = 80  # the longest search text honoured
# `?notice=` values the find page understands (anything else is ignored).
_FIND_NOTICES = {"unavailable": "booking.notice.unavailable"}


# ── Guard and shared context ─────────────────────────────────────────────
def _guard(request: Request, locale: str, actor: Actor | None) -> Response | None:
    """The patient pages' redirects (as `web/documents.py`): signed out goes to
    sign-in and back here, another role goes to its own area, and a profile that
    is not finished goes to onboarding."""
    loc = normalise_locale(locale)
    if actor is None:
        return login_redirect(loc, request)
    if actor.role != UserRole.PATIENT.value:
        return RedirectResponse(f"/{loc}/{_area(actor.role)}", status_code=303)
    if not actor.onboarding_complete:
        return RedirectResponse(f"/{loc}/onboarding", status_code=303)
    return None


def _ctx(loc: str, title_key: str, *, current: str) -> dict[str, Any]:
    return {
        "crumb": translate("auth.role.patient", loc),
        "page_title": translate(title_key, loc),
        "nav_items": _patient_nav(loc, current=current),
        "weekday": lambda day: translate(f"weekday.{day.weekday()}", loc),
        "hhmm": display.hhmm,
        "slot_value": slots.format_local,
        "status_label": lambda status: display.status_label(status, loc),
        "status_badge": display.status_badge,
    }


def _parse_id(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


# ── Find a doctor ────────────────────────────────────────────────────────
def _find_url(loc: str, q: str, specialty: str) -> str:
    query = urlencode({k: v for k, v in (("q", q), ("specialty", specialty)) if v})
    return f"/{loc}/patient/doctors" + (f"?{query}" if query else "")


async def _render_find(
    request: Request,
    loc: str,
    session: SessionDep,
    *,
    status_code: int = 200,
    q: str = "",
    specialty: str = "",
    page_error: str | None = None,
    notice: str | None = None,
) -> Response:
    specialties = await service.specialty_options(session)
    chosen = specialty if specialty in specialties else ""
    cards = await service.search_doctors(session, query=q, specialty=chosen)
    return _render(
        request,
        "booking/find.html",
        loc,
        status_code=status_code,
        path_suffix="/patient/doctors",
        cards=cards,
        specialties=specialties,
        q=q,
        specialty=chosen,
        filters_active=bool(q or chosen),
        page_error=page_error,
        notice=notice,
        specialty_href=lambda value: _find_url(loc, q, value),
        doctor_href=lambda card: f"/{loc}/patient/doctors/{card.id}",
        **_ctx(loc, "booking.find.title", current="doctors"),
    )


async def _find_not_found(request: Request, loc: str, session: SessionDep) -> Response:
    return await _render_find(
        request, loc, session, status_code=404, page_error=translate("errors.not_found", loc)
    )


@router.get("/{locale}/patient/doctors", response_class=HTMLResponse)
async def find_page(
    request: Request,
    locale: str,
    session: SessionDep,
    actor: OptionalActorDep,
    q: str = "",
    specialty: str = "",
    notice: str = "",
) -> Response:
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    loc = normalise_locale(locale)
    key = _FIND_NOTICES.get(notice)
    return await _render_find(
        request,
        loc,
        session,
        q=q.strip()[:QUERY_MAX],
        specialty=specialty,
        notice=translate(key, loc) if key else None,
    )


# ── A doctor's free times ────────────────────────────────────────────────
async def _moving_row(
    session: SessionDep, actor: Actor, doctor_id: uuid.UUID, raw: str
) -> AppointmentRow | None:
    """The appointment being moved, only if `raw` names one of the caller's own
    that can still change and is with this doctor. Anything else is ignored, so
    the page never confirms that someone else's id exists."""
    parsed = _parse_id(raw) if raw else None
    if parsed is None:
        return None
    try:
        row = await service.appointment_for_patient(session, actor, parsed)
    except NotFound:
        return None
    return row if row.can_change and row.doctor_id == doctor_id else None


def _confirm_url(
    loc: str,
    doctor_id: uuid.UUID,
    clinic_id: uuid.UUID,
    start: datetime,
    moving: AppointmentRow | None,
) -> str:
    params = {"clinic": str(clinic_id), "at": slots.format_local(start)}
    if moving is not None:
        params["moving"] = str(moving.id)
    return f"/{loc}/patient/doctors/{doctor_id}/confirm?{urlencode(params, safe=':')}"


async def _render_doctor(
    request: Request,
    loc: str,
    session: SessionDep,
    actor: Actor,
    doctor_id: uuid.UUID,
    *,
    clinic_raw: str = "",
    moving_raw: str = "",
    status_code: int = 200,
    page_error: str | None = None,
) -> Response:
    try:
        doctor = await service.get_doctor(session, doctor_id)
    except NotFound:
        return await _find_not_found(request, loc, session)
    clinic = next((c for c in doctor.clinics if str(c.id) == clinic_raw), doctor.clinics[0])
    moving = await _moving_row(session, actor, doctor_id, moving_raw)
    days = await service.list_options(session, doctor_id, clinic.id)

    def clinic_href(other: ClinicRef) -> str:
        params = {"clinic": str(other.id)}
        if moving is not None:
            params["moving"] = str(moving.id)
        return f"/{loc}/patient/doctors/{doctor_id}?{urlencode(params)}"

    return _render(
        request,
        "booking/doctor.html",
        loc,
        status_code=status_code,
        path_suffix=f"/patient/doctors/{doctor_id}",
        doctor=doctor,
        clinic=clinic,
        days=days,
        moving=moving,
        moving_text=(
            translate(
                "booking.doctor.moving",
                loc,
                date=moving.starts_at.date().isoformat(),
                time=display.hhmm(moving.starts_at),
            )
            if moving is not None
            else ""
        ),
        page_error=page_error,
        clinic_href=clinic_href,
        time_href=lambda start: _confirm_url(loc, doctor_id, clinic.id, start, moving),
        **_ctx(loc, "booking.doctor.title", current="doctors"),
    )


@router.get("/{locale}/patient/doctors/{doctor_id}", response_class=HTMLResponse)
async def doctor_page(
    request: Request,
    locale: str,
    doctor_id: str,
    session: SessionDep,
    actor: OptionalActorDep,
    clinic: str = "",
    moving: str = "",
) -> Response:
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    parsed = _parse_id(doctor_id)
    if parsed is None:
        return await _find_not_found(request, loc, session)
    return await _render_doctor(
        request, loc, session, actor, parsed, clinic_raw=clinic, moving_raw=moving
    )
```

Create `frontend/templates/booking/find.html`:

```html
{% extends "base.html" %}
{#- Find a doctor (FR15, P5 spec 3.3). A GET search form and specialty links, so
    it works without JavaScript. Names are autoescaped; never use |safe here.

    Context (built in web/booking.py::_render_find):
      cards          list[DoctorCard] — verified doctors with a live hospital
      specialties    list[str]; specialty — the chosen one or ""
      q              the search text; filters_active — q or specialty is set
      page_error, notice   banner texts or None
      specialty_href(value), doctor_href(card) -#}

{% block title %}{{ t('booking.find.title') }} · {{ t('app.name') }}{% endblock %}

{% block body %}
<div class="shell">
  {% include "partials/topbar.html" %}

  <main class="content">
    {% if page_error %}
      <div class="banner banner--error" role="alert">
        <span aria-hidden="true">!</span><span>{{ page_error }}</span>
      </div>
    {% endif %}
    {% if notice %}
      <div class="banner banner--warn" role="status"><span>{{ notice }}</span></div>
    {% endif %}

    <div class="panel stack">
      <p class="lead">{{ t('booking.find.subtitle') }}</p>
      <form class="stack" method="get" action="/{{ locale }}/patient/doctors" role="search">
        <label class="field" for="f-q">
          <span class="field-label">{{ t('booking.find.search_label') }}</span>
          <input class="input" id="f-q" name="q" type="search" value="{{ q }}" maxlength="80">
        </label>
        {% if specialty %}<input type="hidden" name="specialty" value="{{ specialty }}">{% endif %}
        <div class="btn-row">
          <button class="btn btn--primary" type="submit">{{ t('booking.find.search_action') }}</button>
          {% if filters_active %}
            <a class="btn btn--secondary" href="/{{ locale }}/patient/doctors">{{ t('booking.find.clear') }}</a>
          {% endif %}
        </div>
      </form>

      {% if specialties %}
        <div>
          <div class="eyebrow">{{ t('booking.find.specialties') }}</div>
          <p style="display:flex;flex-wrap:wrap;gap:var(--sp-2);margin:var(--sp-2) 0 0">
            <a class="btn btn--sm {% if not specialty %}btn--primary{% else %}btn--secondary{% endif %}"
               href="{{ specialty_href('') }}">{{ t('booking.find.all') }}</a>
            {% for s in specialties %}
              <a class="btn btn--sm {% if s == specialty %}btn--primary{% else %}btn--secondary{% endif %}"
                 href="{{ specialty_href(s) }}">{{ s }}</a>
            {% endfor %}
          </p>
        </div>
      {% endif %}
    </div>

    <section class="panel stack" aria-labelledby="results-h">
      <h2 id="results-h" class="panel__label">{{ t('booking.find.results', count=cards|length) }}</h2>
      {% if cards %}
        <ul class="grant-list">
          {% for c in cards %}
            <li class="grant-card stack">
              <div class="entry-row__main">
                <div class="entry-row__title">{{ c.name }}</div>
                {% if c.specialty %}<div class="field-help">{{ c.specialty }}</div>{% endif %}
                <div class="mono">
                  {% for cl in c.clinics %}{{ cl.name }}{% if cl.city %} — {{ cl.city }}{% endif %}{% if not loop.last %} · {% endif %}{% endfor %}
                </div>
              </div>
              <span class="badge badge--ok">{{ t('booking.find.verified') }}</span>
              <div class="entry-row__actions">
                <a class="btn btn--primary btn--sm" href="{{ doctor_href(c) }}">{{ t('booking.find.view') }}</a>
              </div>
            </li>
          {% endfor %}
        </ul>
      {% else %}
        <p class="empty-state">{{ t('booking.find.empty') }}</p>
      {% endif %}
    </section>
  </main>
</div>
{% endblock %}
```

Create `frontend/templates/booking/doctor.html`:

```html
{% extends "base.html" %}
{#- A doctor's free times (FR15, P5 spec 3.3). A time is a plain link to the
    confirm step; nothing on this page books anything. Names are autoescaped.

    Context (built in web/booking.py::_render_doctor):
      doctor      DoctorCard; clinic — the ClinicRef whose times are shown
      days        list[DayOptions] — only days with at least one free time
      moving      AppointmentRow being moved, or None; moving_text its banner text
      page_error  banner text or None
      clinic_href(clinic), time_href(start), weekday(day), hhmm(start) -#}

{% block title %}{{ t('booking.doctor.title') }} · {{ t('app.name') }}{% endblock %}

{% block body %}
<div class="shell">
  {% include "partials/topbar.html" %}

  <main class="content">
    {% if page_error %}
      <div class="banner banner--error" role="alert">
        <span aria-hidden="true">!</span><span>{{ page_error }}</span>
      </div>
    {% endif %}
    {% if moving %}
      <div class="banner banner--warn" role="status">
        <span aria-hidden="true">!</span>
        <span>{{ moving_text }}
          <a href="/{{ locale }}/patient/appointments/{{ moving.id }}">{{ t('booking.doctor.keep') }}</a></span>
      </div>
    {% endif %}

    <div class="panel stack">
      <div class="entry-row__title">{{ doctor.name }}</div>
      {% if doctor.specialty %}<div class="field-help">{{ doctor.specialty }}</div>{% endif %}
      <p style="margin:0"><span class="badge badge--ok">{{ t('booking.find.verified') }}</span></p>
      <div>
        <div class="eyebrow">{{ t('booking.field.hospital') }}</div>
        <p style="display:flex;flex-wrap:wrap;gap:var(--sp-2);margin:var(--sp-2) 0 0">
          {% for c in doctor.clinics %}
            <a class="btn btn--sm {% if c.id == clinic.id %}btn--primary{% else %}btn--secondary{% endif %}"
               href="{{ clinic_href(c) }}"
               {% if c.id == clinic.id %}aria-current="true"{% endif %}>{{ c.name }}{% if c.city %} — {{ c.city }}{% endif %}</a>
          {% endfor %}
        </p>
      </div>
    </div>

    <div class="panel stack">
      <h2 class="panel__label">{{ t('booking.doctor.pick') }}</h2>
      {% if not days %}
        <p class="empty-state">{{ t('booking.doctor.no_times') }}</p>
      {% endif %}
    </div>

    {% for d in days %}
      <section class="panel stack" aria-labelledby="day-{{ d.day.isoformat() }}">
        <h3 id="day-{{ d.day.isoformat() }}" class="panel__label">{{ weekday(d.day) }} <bdi>{{ d.day.isoformat() }}</bdi></h3>
        <p style="display:flex;flex-wrap:wrap;gap:var(--sp-2);margin:0">
          {% for start in d.times %}
            <a class="btn btn--secondary btn--sm mono" dir="ltr" href="{{ time_href(start) }}">{{ hhmm(start) }}</a>
          {% endfor %}
        </p>
      </section>
    {% endfor %}

    <p><a class="btn btn--secondary" href="/{{ locale }}/patient/doctors">{{ t('booking.doctor.back') }}</a></p>
  </main>
</div>
{% endblock %}
```

- [ ] **Step 7: Wire the router and the nav**

In `backend/app/main.py`, next to the other web imports add `from app.web.booking import router as web_booking_router`, and add `app.include_router(web_booking_router)` right after `app.include_router(web_documents_router)`.

In `backend/app/web/router.py`, in `_patient_nav`, add this entry after the `documents` entry (inside the returned list):

```python
        {
            "href": f"/{loc}/patient/doctors",
            "label": translate("nav.find_doctor", loc),
            "current": current == "doctors",
        },
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/test_booking_display.py tests/test_booking_web_find.py -q`
Expected: all pass (about 30).

- [ ] **Step 9: Lint, run the nearby suites, and commit**

```bash
uv run ruff check backend tests --fix && uv run ruff format backend tests
uv run pytest tests/test_booking_*.py tests/test_web.py tests/test_passport_web.py tests/test_privacy_access_web.py -q
git add backend/app/booking/display.py backend/app/web/booking.py backend/app/main.py backend/app/web/router.py frontend/templates/booking backend/app/i18n/messages tests/booking_helpers.py tests/test_booking_display.py tests/test_booking_web_find.py
git commit -m "feat(booking): find a doctor and see their free times"
```

---

### Task 8: Patient pages: confirm a time, book it, and move a booking

**Files:**
- Modify: `backend/app/web/booking.py` (imports, then append)
- Create: `frontend/templates/booking/confirm.html`
- Modify: `backend/app/i18n/messages/en.json`, `ur.json`
- Test: `tests/test_booking_web_book.py`

**Interfaces:**
- Consumes: Task 7's `_guard`, `_ctx`, `_parse_id`, `_moving_row`, `_render_doctor`, `_find_not_found`; `service.book`, `service.reschedule`, `service.get_doctor`, `service.list_options`, `service.appointment_for_patient`; `deps.check_booking_write_rate`; `errors.BookingRefused` (exposes `.reason`, `.message_key`, `.http_status`), `errors.RateLimited`.
- Produces (`web/booking.py`): `_parse_slot(value) -> datetime | None`; `GET /{locale}/patient/doctors/{doctor_id}/confirm` (query `clinic`, `at`, `moving`); `POST /{locale}/patient/appointments` (form `doctor_id`, `clinic_id`, `slot`); `POST /{locale}/patient/appointments/{appointment_id}/reschedule` (form `clinic_id`, `slot`).
- Redirect targets that Task 9 builds: a successful booking goes to `/{loc}/patient/appointments/{id}?saved=booked`, a successful move to `/{loc}/patient/appointments/{new_id}?saved=rescheduled`, and an appointment that is not the caller's goes to `/{loc}/patient/appointments`. This task's tests assert those `Location` headers; they do not follow them.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_booking_web_book.py`:

```python
"""P5 web: the confirm step, booking, and moving a booking."""

from __future__ import annotations

import uuid

import pytest
from app.db.models import (
    Appointment,
    ScheduleException,
    ScheduleExceptionType,
    UserRole,
    VerificationStatus,
)
from app.settings import settings
from sqlalchemy import select

from tests.booking_helpers import (
    MONDAY,
    TODAY,
    WED,
    add_appointment,
    add_session,
    at,
    freeze,
    login,
    make_patient,
    verified_doctor,
)
from tests.conftest import make_user

FIND = "/en/patient/doctors"
BOOK = "/en/patient/appointments"


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    freeze(monkeypatch)


@pytest.fixture
async def me(db):
    _, patient, _ = await make_patient(db)
    return patient


@pytest.fixture
async def patient_client(client, me):
    await login(client, "booking.pat@x.com")
    return client


def confirm_url(doctor, clinic, when: str, moving=None) -> str:
    extra = f"&moving={moving.id}" if moving is not None else ""
    return f"{FIND}/{doctor.id}/confirm?clinic={clinic.id}&at={when}{extra}"


def book_form(doctor, clinic, when: str) -> dict:
    return {"doctor_id": str(doctor.id), "clinic_id": str(clinic.id), "slot": when}


async def appointments(db) -> list[Appointment]:
    result = await db.execute(select(Appointment).execution_options(populate_existing=True))
    return list(result.scalars().all())


async def world(db, clinic, **session_kw):
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic, **session_kw)
    return doctor, session


# ── Guards ───────────────────────────────────────────────────────────────
async def test_anonymous_is_sent_to_sign_in(client, db, clinic):
    doctor, _ = await world(db, clinic)
    r = await client.get(confirm_url(doctor, clinic, "2026-10-14T10:00"))
    assert r.status_code == 303 and r.headers["location"].startswith("/en/login?next=/en/patient/")
    r = await client.post(BOOK, data=book_form(doctor, clinic, "2026-10-14T10:00"))
    assert r.status_code == 303 and r.headers["location"] == "/en/login"


@pytest.mark.parametrize(
    ("role", "area"), [(UserRole.DOCTOR, "doctor"), (UserRole.CLINIC_ADMIN, "admin")]
)
async def test_other_roles_go_to_their_own_area(client, db, clinic, role, area):
    doctor, _ = await world(db, clinic)
    await make_user(db, email="other@x.com", role=role, clinic_id=clinic.id)
    await login(client, "other@x.com")
    calls = [
        ("GET", confirm_url(doctor, clinic, "2026-10-14T10:00")),
        ("POST", BOOK),
        ("POST", f"{BOOK}/{uuid.uuid4()}/reschedule"),
    ]
    for method, path in calls:
        r = await client.request(method, path)
        assert r.status_code == 303 and r.headers["location"] == f"/en/{area}"
    assert await appointments(db) == []


# ── The confirm step ─────────────────────────────────────────────────────
async def test_the_confirm_page_shows_the_choice_and_posts_it(patient_client, db, clinic):
    doctor, _ = await world(db, clinic)
    r = await patient_client.get(confirm_url(doctor, clinic, "2026-10-14T10:00"))
    assert r.status_code == 200
    for text in ("Dr Booking", "Cardiology", "Shifa International", "Wednesday", "2026-10-14"):
        assert text in r.text
    assert ">10:00<" in r.text
    assert 'action="/en/patient/appointments"' in r.text
    assert f'name="doctor_id" value="{doctor.id}"' in r.text
    assert f'name="clinic_id" value="{clinic.id}"' in r.text
    assert 'name="slot" value="2026-10-14T10:00"' in r.text
    assert "Confirm booking" in r.text
    assert await appointments(db) == []  # looking books nothing


async def test_the_confirm_page_uses_pakistan_time_after_midnight(patient_client, db, clinic):
    doctor, _ = await world(db, clinic, weekday=3, start="00:00", end="01:00")
    r = await patient_client.get(confirm_url(doctor, clinic, "2026-10-15T00:15"))
    assert r.status_code == 200 and "Thursday" in r.text and "2026-10-15" in r.text
    assert 'name="slot" value="2026-10-15T00:15"' in r.text


async def test_malformed_parameters_go_back_to_the_doctor_page(patient_client, db, clinic):
    doctor, _ = await world(db, clinic)
    for query in ("", "?clinic=x&at=2026-10-14T10:00", f"?clinic={clinic.id}&at=junk"):
        r = await patient_client.get(f"{FIND}/{doctor.id}/confirm{query}")
        assert r.status_code == 303 and r.headers["location"] == f"/en/patient/doctors/{doctor.id}"


async def test_an_unknown_doctor_or_foreign_hospital_is_handled(
    patient_client, db, clinic, second_clinic
):
    doctor, _ = await world(db, clinic)
    r = await patient_client.get(
        f"{FIND}/{uuid.uuid4()}/confirm?clinic={clinic.id}&at=2026-10-14T10:00"
    )
    assert r.status_code == 404
    r = await patient_client.get(confirm_url(doctor, second_clinic, "2026-10-14T10:00"))
    assert r.status_code == 303 and r.headers["location"] == f"/en/patient/doctors/{doctor.id}"


@pytest.mark.parametrize("when", ["2026-10-14T10:07", "2026-10-14T13:00", "2026-10-15T10:00"])
async def test_a_time_that_is_not_offered_shows_the_doctor_page_again(
    patient_client, db, clinic, when
):
    doctor, _ = await world(db, clinic)
    r = await patient_client.get(confirm_url(doctor, clinic, when))
    assert r.status_code == 409 and "That time is not available" in r.text
    assert "at=2026-10-14T09:00" in r.text  # fresh times to pick from


async def test_a_taken_time_is_not_confirmable(patient_client, db, clinic):
    doctor, session = await world(db, clinic)
    _, other, _ = await make_patient(db, email="p2@x.com", name="Patient 2")
    await add_appointment(db, other, doctor, clinic, session, at(WED, "10:00"))
    r = await patient_client.get(confirm_url(doctor, clinic, "2026-10-14T10:00"))
    assert r.status_code == 409


async def test_the_confirm_page_for_a_move_posts_to_reschedule(patient_client, db, clinic, me):
    doctor, session = await world(db, clinic)
    mine = await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    r = await patient_client.get(confirm_url(doctor, clinic, "2026-10-14T11:00", moving=mine))
    assert r.status_code == 200
    assert f'action="/en/patient/appointments/{mine.id}/reschedule"' in r.text
    assert "Confirm new time" in r.text
    assert "This replaces your appointment on 2026-10-14 at 10:00." in r.text


async def test_a_moving_id_that_is_not_the_callers_is_ignored_on_confirm(
    patient_client, db, clinic
):
    doctor, session = await world(db, clinic)
    _, other, _ = await make_patient(db, email="p2@x.com", name="Patient 2")
    theirs = await add_appointment(db, other, doctor, clinic, session, at(WED, "10:00"))
    r = await patient_client.get(confirm_url(doctor, clinic, "2026-10-14T11:00", moving=theirs))
    assert r.status_code == 200
    assert 'action="/en/patient/appointments"' in r.text and "reschedule" not in r.text


# ── Booking ──────────────────────────────────────────────────────────────
async def test_booking_saves_and_redirects_to_the_confirmation(patient_client, db, clinic, me):
    doctor, session = await world(db, clinic)
    r = await patient_client.post(BOOK, data=book_form(doctor, clinic, "2026-10-14T10:00"))
    [saved] = await appointments(db)
    assert r.status_code == 303
    assert r.headers["location"] == f"/en/patient/appointments/{saved.id}?saved=booked"
    assert saved.patient_id == me.id and saved.doctor_id == doctor.id
    assert saved.schedule_id == session.id and saved.status == "booked"
    assert saved.booking_channel == "online"


async def test_booking_a_time_just_after_midnight_in_pakistan(patient_client, db, clinic):
    doctor, _ = await world(db, clinic, weekday=3, start="00:00", end="01:00")
    r = await patient_client.post(BOOK, data=book_form(doctor, clinic, "2026-10-15T00:15"))
    [saved] = await appointments(db)
    assert r.status_code == 303
    assert saved.slot_start.replace(tzinfo=None).isoformat() == "2026-10-14T19:15:00"


async def test_a_taken_time_shows_the_page_again_with_fresh_times(patient_client, db, clinic):
    doctor, session = await world(db, clinic)
    _, other, _ = await make_patient(db, email="p2@x.com", name="Patient 2")
    await add_appointment(db, other, doctor, clinic, session, at(WED, "10:00"))
    r = await patient_client.post(BOOK, data=book_form(doctor, clinic, "2026-10-14T10:00"))
    assert r.status_code == 409 and "Someone just booked that time" in r.text
    assert "at=2026-10-14T10:00" not in r.text and "at=2026-10-14T10:15" in r.text
    assert len(await appointments(db)) == 1


async def test_a_full_session_is_explained(patient_client, db, clinic):
    doctor, session = await world(db, clinic, cap=1)
    _, other, _ = await make_patient(db, email="p2@x.com", name="Patient 2")
    await add_appointment(db, other, doctor, clinic, session, at(WED, "09:00"))
    r = await patient_client.post(BOOK, data=book_form(doctor, clinic, "2026-10-14T10:00"))
    assert r.status_code == 409 and "That session is full" in r.text


async def test_two_with_one_doctor_in_a_day_is_explained(patient_client, db, clinic, me):
    doctor, session = await world(db, clinic)
    await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    r = await patient_client.post(BOOK, data=book_form(doctor, clinic, "2026-10-14T11:00"))
    assert r.status_code == 409 and "with this doctor that day" in r.text
    assert len(await appointments(db)) == 1


async def test_an_overlap_with_another_doctor_is_explained(patient_client, db, clinic, me):
    doctor_a, _ = await world(db, clinic)
    doctor_b = await verified_doctor(db, clinic, email="b.dr@x.com", name="Dr B")
    session_b = await add_session(db, doctor_b, clinic)
    await add_appointment(db, me, doctor_b, clinic, session_b, at(WED, "10:00"))
    r = await patient_client.post(BOOK, data=book_form(doctor_a, clinic, "2026-10-14T10:00"))
    assert r.status_code == 409 and "You already have an appointment at that time" in r.text


async def test_a_time_too_soon_is_explained(patient_client, db, clinic):
    doctor, _ = await world(db, clinic, weekday=MONDAY, start="12:00", end="18:00")
    r = await patient_client.post(BOOK, data=book_form(doctor, clinic, f"{TODAY}T13:15"))
    assert r.status_code == 409 and "too soon" in r.text


async def test_a_time_removed_after_the_page_loaded_is_refused(patient_client, db, clinic):
    # Review focus 3, through the page.
    doctor, session = await world(db, clinic)
    page = await patient_client.get(f"{FIND}/{doctor.id}")
    assert "at=2026-10-14T10:00" in page.text
    db.add(
        ScheduleException(
            schedule_id=session.id, exception_date=WED, kind=ScheduleExceptionType.LEAVE
        )
    )
    await db.commit()
    r = await patient_client.post(BOOK, data=book_form(doctor, clinic, "2026-10-14T10:00"))
    assert r.status_code == 409 and "That time is not available" in r.text
    assert await appointments(db) == []


async def test_a_doctor_unverified_after_the_page_loaded_cannot_be_booked(
    patient_client, db, clinic
):
    # Review focus 5, through the page.
    doctor, _ = await world(db, clinic)
    doctor.verification_status = VerificationStatus.REJECTED
    doctor.verified_by = None
    doctor.verified_at = None
    await db.commit()
    r = await patient_client.post(BOOK, data=book_form(doctor, clinic, "2026-10-14T10:00"))
    assert r.status_code == 303 and r.headers["location"] == "/en/patient/doctors?notice=unavailable"
    assert await appointments(db) == []


async def test_cancel_then_rebook_the_same_time_through_the_page(patient_client, db, clinic, me):
    # Review focus 5, second half.
    doctor, session = await world(db, clinic)
    await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"), status="cancelled")
    r = await patient_client.post(BOOK, data=book_form(doctor, clinic, "2026-10-14T10:00"))
    assert r.status_code == 303 and "saved=booked" in r.headers["location"]


async def test_tampered_forms_do_not_book(patient_client, db, clinic):
    doctor, _ = await world(db, clinic)
    r = await patient_client.post(BOOK, data={"doctor_id": "junk", "clinic_id": "", "slot": ""})
    assert r.status_code == 404
    r = await patient_client.post(
        BOOK, data={"doctor_id": str(doctor.id), "clinic_id": "junk", "slot": "2026-10-14T10:00"}
    )
    assert r.status_code == 303 and r.headers["location"] == f"/en/patient/doctors/{doctor.id}"
    r = await patient_client.post(BOOK, data={"doctor_id": str(doctor.id)})
    assert r.status_code == 303
    assert await appointments(db) == []


async def test_the_write_rate_limit_applies(patient_client, db, clinic, monkeypatch):
    monkeypatch.setattr(settings, "booking_write_rate_limit_per_minute", 1)
    doctor, _ = await world(db, clinic)
    first = await patient_client.post(BOOK, data=book_form(doctor, clinic, "2026-10-14T10:00"))
    second = await patient_client.post(BOOK, data=book_form(doctor, clinic, "2026-10-14T10:15"))
    assert first.status_code == 303
    assert second.status_code == 429 and "Too many attempts" in second.text
    assert len(await appointments(db)) == 1


async def test_names_are_escaped_on_the_confirm_page(patient_client, db, clinic):
    doctor, _ = await world(db, clinic)
    doctor.full_name = "<b>Dr</b>"
    await db.commit()
    r = await patient_client.get(confirm_url(doctor, clinic, "2026-10-14T10:00"))
    assert "<b>Dr</b>" not in r.text and "&lt;b&gt;Dr&lt;/b&gt;" in r.text


# ── Moving a booking ─────────────────────────────────────────────────────
async def move(client, appointment, clinic, when: str):
    return await client.post(
        f"{BOOK}/{appointment.id}/reschedule", data={"clinic_id": str(clinic.id), "slot": when}
    )


async def test_moving_books_the_new_time_and_cancels_the_old(patient_client, db, clinic, me):
    doctor, session = await world(db, clinic)
    old = await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    r = await move(patient_client, old, clinic, "2026-10-21T11:00")
    rows = {a.id: a for a in await appointments(db)}
    [new] = [a for a in rows.values() if a.id != old.id]
    assert r.status_code == 303
    assert r.headers["location"] == f"/en/patient/appointments/{new.id}?saved=rescheduled"
    assert rows[old.id].status == "cancelled" and new.status == "booked"


async def test_moving_to_another_time_with_the_same_doctor_the_same_day(
    patient_client, db, clinic, me
):
    # Review focus 1, through the page.
    doctor, session = await world(db, clinic)
    old = await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    r = await move(patient_client, old, clinic, "2026-10-14T11:00")
    assert r.status_code == 303 and "saved=rescheduled" in r.headers["location"]


async def test_a_refused_move_leaves_the_old_booking_and_stays_in_move_mode(
    patient_client, db, clinic, me
):
    doctor, session = await world(db, clinic)
    _, other, _ = await make_patient(db, email="p2@x.com", name="Patient 2")
    old = await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    await add_appointment(db, other, doctor, clinic, session, at(WED, "11:00"))
    r = await move(patient_client, old, clinic, "2026-10-14T11:00")
    assert r.status_code == 409 and "Someone just booked that time" in r.text
    assert f"moving={old.id}" in r.text  # still moving that booking
    assert {a.status for a in await appointments(db)} == {"booked"} and len(await appointments(db)) == 2


async def test_moving_someone_elses_or_an_unknown_booking_goes_to_the_list(
    patient_client, db, clinic
):
    doctor, session = await world(db, clinic)
    _, other, _ = await make_patient(db, email="p2@x.com", name="Patient 2")
    theirs = await add_appointment(db, other, doctor, clinic, session, at(WED, "10:00"))
    r = await move(patient_client, theirs, clinic, "2026-10-14T11:00")
    assert r.status_code == 303 and r.headers["location"] == "/en/patient/appointments"
    r = await patient_client.post(
        f"{BOOK}/{uuid.uuid4()}/reschedule", data={"clinic_id": str(clinic.id), "slot": "x"}
    )
    assert r.status_code == 303 and r.headers["location"] == "/en/patient/appointments"
    r = await patient_client.post(f"{BOOK}/junk/reschedule", data={})
    assert r.status_code == 303 and r.headers["location"] == "/en/patient/appointments"
    assert {a.status for a in await appointments(db)} == {"booked"}


async def test_moving_a_started_booking_is_refused(patient_client, db, clinic, me):
    doctor, session = await world(db, clinic)
    started = await add_appointment(db, me, doctor, clinic, session, at(TODAY, "09:00"))
    r = await move(patient_client, started, clinic, "2026-10-14T11:00")
    assert r.status_code == 409 and "can't be changed any more" in r.text.replace("&#39;", "'")
    assert (await appointments(db))[0].status == "booked"


async def test_a_move_with_a_bad_time_goes_back_to_move_mode(patient_client, db, clinic, me):
    doctor, session = await world(db, clinic)
    old = await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    r = await move(patient_client, old, clinic, "junk")
    assert r.status_code == 303
    assert r.headers["location"] == f"/en/patient/doctors/{doctor.id}?moving={old.id}"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_booking_web_book.py -q`
Expected: failures: 404 or 405 for the missing confirm, book and reschedule routes.

- [ ] **Step 3: Add the i18n keys**

Run the i18n helper from the File Structure section with:

```python
EN = {
    "booking.confirm.title": "Confirm your appointment",
    "booking.confirm.title_move": "Confirm the new time",
    "booking.confirm.action": "Confirm booking",
    "booking.confirm.action_move": "Confirm new time",
    "booking.confirm.change": "Choose a different time",
    "booking.confirm.hint": "You can cancel any time before the appointment starts.",
    "booking.confirm.replaces": "This replaces your appointment on {date} at {time}.",
}
UR = {
    "booking.confirm.title": "اپنی اپائنٹمنٹ کی تصدیق کریں",
    "booking.confirm.title_move": "نئے وقت کی تصدیق کریں",
    "booking.confirm.action": "بکنگ کی تصدیق کریں",
    "booking.confirm.action_move": "نئے وقت کی تصدیق کریں",
    "booking.confirm.change": "کوئی اور وقت منتخب کریں",
    "booking.confirm.hint": "اپائنٹمنٹ شروع ہونے سے پہلے آپ کسی بھی وقت اسے منسوخ کر سکتے ہیں۔",
    "booking.confirm.replaces": "یہ {date} کو {time} بجے کی آپ کی اپائنٹمنٹ کی جگہ لے گی۔",
}
```

- [ ] **Step 4: Write the confirm template**

Create `frontend/templates/booking/confirm.html`:

```html
{% extends "base.html" %}
{#- Confirm the chosen time (P5 spec 3.3): doctor, hospital, date and time only.
    There is no token number; the real one is assigned at check-in, later.
    Names are autoescaped; never use |safe here.

    Context (built in web/booking.py::confirm_page):
      doctor      DoctorCard; clinic — the ClinicRef; day — a date (Pakistan)
      start       the chosen instant (UTC); moving — AppointmentRow or None
      replaces_text, action (the form's address), back_href
      hhmm(start), slot_value(start), weekday(day) -#}

{% block title %}{{ page_title }} · {{ t('app.name') }}{% endblock %}

{% block body %}
<div class="shell">
  {% include "partials/topbar.html" %}

  <main class="content">
    <div class="panel stack">
      <h2 class="panel__label">{{ page_title }}</h2>
      {% if moving %}<p class="field-help">{{ replaces_text }}</p>{% endif %}
      <dl class="stack" style="margin:0">
        <div>
          <dt class="eyebrow">{{ t('booking.field.doctor') }}</dt>
          <dd style="margin:0">{{ doctor.name }}{% if doctor.specialty %} · {{ doctor.specialty }}{% endif %}</dd>
        </div>
        <div>
          <dt class="eyebrow">{{ t('booking.field.hospital') }}</dt>
          <dd style="margin:0">{{ clinic.name }}{% if clinic.city %} — {{ clinic.city }}{% endif %}</dd>
        </div>
        <div>
          <dt class="eyebrow">{{ t('booking.field.date') }}</dt>
          <dd style="margin:0">{{ weekday(day) }} <bdi>{{ day.isoformat() }}</bdi></dd>
        </div>
        <div>
          <dt class="eyebrow">{{ t('booking.field.time') }}</dt>
          <dd style="margin:0" class="mono" dir="ltr">{{ hhmm(start) }}</dd>
        </div>
      </dl>

      <form class="stack" method="post" action="{{ action }}">
        <input type="hidden" name="doctor_id" value="{{ doctor.id }}">
        <input type="hidden" name="clinic_id" value="{{ clinic.id }}">
        <input type="hidden" name="slot" value="{{ slot_value(start) }}">
        <p class="field-help">{{ t('booking.confirm.hint') }}</p>
        <div class="btn-row">
          {% set label = t('booking.confirm.action_move') if moving else t('booking.confirm.action') %}
          <button class="btn btn--primary" type="submit" data-label="{{ label }}">{{ label }}</button>
          <a class="btn btn--secondary" href="{{ back_href }}">{{ t('booking.confirm.change') }}</a>
        </div>
      </form>
    </div>
  </main>
</div>
{% endblock %}
```

- [ ] **Step 5: Write the handlers**

In `backend/app/web/booking.py`, change the import block to add what this task needs:

```python
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..booking import display, service, slots
from ..booking.schemas import AppointmentRow, ClinicRef
from ..db.models import UserRole
from ..deps import Actor, OptionalActorDep, SessionDep, check_booking_write_rate
from ..errors import BookingRefused, NotFound, RateLimited
from ..i18n.catalogue import normalise_locale, translate
from .router import _area, _patient_nav, _render, login_redirect
```

(`datetime` and `uuid` stay imported from Task 7.) Append to the module:

```python


# ── Confirm ──────────────────────────────────────────────────────────────
def _parse_slot(value: str) -> datetime | None:
    """`YYYY-MM-DDTHH:MM` (Pakistan time) as an aware UTC instant, else None."""
    try:
        return slots.parse_local(value)
    except ValueError:
        return None


@router.get("/{locale}/patient/doctors/{doctor_id}/confirm", response_class=HTMLResponse)
async def confirm_page(
    request: Request,
    locale: str,
    doctor_id: str,
    session: SessionDep,
    actor: OptionalActorDep,
    clinic: str = "",
    at: str = "",
    moving: str = "",
) -> Response:
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    parsed = _parse_id(doctor_id)
    if parsed is None:
        return await _find_not_found(request, loc, session)
    doctor_url = f"/{loc}/patient/doctors/{parsed}"
    clinic_id, start = _parse_id(clinic), _parse_slot(at)
    if clinic_id is None or start is None:
        return RedirectResponse(doctor_url, status_code=303)
    try:
        doctor = await service.get_doctor(session, parsed)
    except NotFound:
        return await _find_not_found(request, loc, session)
    chosen = next((c for c in doctor.clinics if c.id == clinic_id), None)
    if chosen is None:
        return RedirectResponse(doctor_url, status_code=303)
    try:
        options = await service.list_options(session, parsed, clinic_id)
    except NotFound:  # the doctor changed between the two reads
        return await _find_not_found(request, loc, session)
    if start not in {moment for day in options for moment in day.times}:
        return await _render_doctor(
            request,
            loc,
            session,
            actor,
            parsed,
            clinic_raw=clinic,
            moving_raw=moving,
            status_code=409,
            page_error=translate("errors.booking_not_offered", loc),
        )
    moving_row = await _moving_row(session, actor, parsed, moving)
    back = {"clinic": str(clinic_id)}
    if moving_row is not None:
        back["moving"] = str(moving_row.id)
    local = slots.to_local(start)
    return _render(
        request,
        "booking/confirm.html",
        loc,
        path_suffix=f"/patient/doctors/{parsed}",
        doctor=doctor,
        clinic=chosen,
        day=local.date(),
        start=start,
        moving=moving_row,
        replaces_text=(
            translate(
                "booking.confirm.replaces",
                loc,
                date=moving_row.starts_at.date().isoformat(),
                time=display.hhmm(moving_row.starts_at),
            )
            if moving_row is not None
            else ""
        ),
        action=(
            f"/{loc}/patient/appointments/{moving_row.id}/reschedule"
            if moving_row is not None
            else f"/{loc}/patient/appointments"
        ),
        back_href=f"{doctor_url}?{urlencode(back)}",
        **_ctx(
            loc,
            "booking.confirm.title_move" if moving_row is not None else "booking.confirm.title",
            current="doctors",
        ),
    )


# ── Book ─────────────────────────────────────────────────────────────────
@router.post("/{locale}/patient/appointments")
async def book_submit(
    request: Request,
    locale: str,
    session: SessionDep,
    actor: OptionalActorDep,
    doctor_id: Annotated[str, Form()] = "",
    clinic_id: Annotated[str, Form()] = "",
    slot: Annotated[str, Form()] = "",
) -> Response:
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    doctor = _parse_id(doctor_id)
    if doctor is None:
        return await _find_not_found(request, loc, session)
    clinic, start = _parse_id(clinic_id), _parse_slot(slot)
    if clinic is None or start is None:
        return RedirectResponse(f"/{loc}/patient/doctors/{doctor}", status_code=303)

    async def again(code: int, message: str) -> Response:
        return await _render_doctor(
            request,
            loc,
            session,
            actor,
            doctor,
            clinic_raw=clinic_id,
            status_code=code,
            page_error=message,
        )

    try:
        await check_booking_write_rate(actor)
    except RateLimited:
        return await again(429, translate("errors.rate_limited", loc))
    try:
        appointment = await service.book(
            session, actor, doctor, clinic, start, user_agent=request.headers.get("user-agent")
        )
    except BookingRefused as exc:
        await session.rollback()  # let go of the doctor lock before drawing the page
        return await again(exc.http_status, translate(exc.message_key, loc))
    except NotFound:
        await session.rollback()
        return RedirectResponse(f"/{loc}/patient/doctors?notice=unavailable", status_code=303)
    return RedirectResponse(
        f"/{loc}/patient/appointments/{appointment.id}?saved=booked", status_code=303
    )


# ── Move a booking ───────────────────────────────────────────────────────
@router.post("/{locale}/patient/appointments/{appointment_id}/reschedule")
async def reschedule_submit(
    request: Request,
    locale: str,
    appointment_id: str,
    session: SessionDep,
    actor: OptionalActorDep,
    clinic_id: Annotated[str, Form()] = "",
    slot: Annotated[str, Form()] = "",
) -> Response:
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    list_url = f"/{loc}/patient/appointments"
    parsed = _parse_id(appointment_id)
    if parsed is None:
        return RedirectResponse(list_url, status_code=303)
    try:
        row = await service.appointment_for_patient(session, actor, parsed)
    except NotFound:  # not theirs and not there look the same
        return RedirectResponse(list_url, status_code=303)
    clinic, start = _parse_id(clinic_id), _parse_slot(slot)
    if clinic is None or start is None:
        return RedirectResponse(
            f"/{loc}/patient/doctors/{row.doctor_id}?moving={row.id}", status_code=303
        )

    async def again(code: int, message: str) -> Response:
        return await _render_doctor(
            request,
            loc,
            session,
            actor,
            row.doctor_id,
            clinic_raw=clinic_id,
            moving_raw=str(row.id),
            status_code=code,
            page_error=message,
        )

    try:
        await check_booking_write_rate(actor)
    except RateLimited:
        return await again(429, translate("errors.rate_limited", loc))
    try:
        moved = await service.reschedule(
            session,
            actor,
            parsed,
            clinic,
            start,
            user_agent=request.headers.get("user-agent"),
        )
    except BookingRefused as exc:
        await session.rollback()
        return await again(exc.http_status, translate(exc.message_key, loc))
    except NotFound:
        await session.rollback()
        return RedirectResponse(list_url, status_code=303)
    return RedirectResponse(f"{list_url}/{moved.id}?saved=rescheduled", status_code=303)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_booking_web_book.py tests/test_booking_web_find.py -q`
Expected: all pass (about 55).

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check backend tests --fix && uv run ruff format backend tests
uv run pytest tests/test_booking_*.py -q
git add backend/app/web/booking.py frontend/templates/booking/confirm.html backend/app/i18n/messages tests/test_booking_web_book.py
git commit -m "feat(booking): confirm, book and move a booking from the pages"
```

---

### Task 9: Patient pages: my appointments, the confirmation, cancel, and the reschedule entry

**Files:**
- Modify: `backend/app/web/booking.py` (append), `backend/app/web/router.py` (`_patient_nav`: one entry), `backend/app/i18n/messages/en.json`, `ur.json`
- Create: `frontend/templates/booking/list.html`, `frontend/templates/booking/appointment.html`, `frontend/templates/booking/_row.html`, `frontend/templates/booking/_actions.html`
- Test: `tests/test_booking_web_appointments.py`

**Interfaces:**
- Consumes: Task 7's `_guard`, `_ctx`, `_parse_id`; Task 8's routes (their redirect targets); `service.my_appointments`, `service.appointment_for_patient`, `service.cancel`; `deps.check_booking_write_rate`; `AppointmentRow` (`id, doctor_id, doctor_name, specialty, clinic_id, clinic_name, starts_at` (Pakistan time), `status, upcoming, can_change`).
- Produces (`web/booking.py`): `_SAVED` (the `?saved=` values), `_render_list(request, loc, session, actor, *, status_code=200, saved="", page_error=None)`; `GET /{locale}/patient/appointments`; `GET /{locale}/patient/appointments/{appointment_id}` (the confirmation); `POST /{locale}/patient/appointments/{appointment_id}/cancel`; `GET /{locale}/patient/appointments/{appointment_id}/reschedule` (redirects into the doctor page in move mode).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_booking_web_appointments.py`:

```python
"""P5 web: my appointments, the confirmation, cancel, and the reschedule entry."""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from app.db.models import Appointment, UserRole, VerificationStatus
from app.settings import settings
from sqlalchemy import select

from tests.booking_helpers import (
    THURSDAY,
    TODAY,
    WED,
    add_appointment,
    add_session,
    at,
    freeze,
    login,
    make_patient,
    verified_doctor,
)
from tests.conftest import make_user

FIND = "/en/patient/doctors"
LIST = "/en/patient/appointments"


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    freeze(monkeypatch)


@pytest.fixture
async def me(db):
    _, patient, _ = await make_patient(db)
    return patient


@pytest.fixture
async def patient_client(client, me):
    await login(client, "booking.pat@x.com")
    return client


async def world(db, clinic, **session_kw):
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic, **session_kw)
    return doctor, session


async def status_of(db, appointment) -> str:
    row = (
        await db.execute(
            select(Appointment)
            .where(Appointment.id == appointment.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return row.status


# ── Guards ───────────────────────────────────────────────────────────────
async def test_anonymous_is_sent_to_sign_in(client):
    r = await client.get(LIST)
    assert r.status_code == 303 and r.headers["location"] == "/en/login?next=/en/patient/appointments"
    r = await client.post(f"{LIST}/{uuid.uuid4()}/cancel")
    assert r.status_code == 303 and r.headers["location"] == "/en/login"


@pytest.mark.parametrize(
    ("role", "area"), [(UserRole.DOCTOR, "doctor"), (UserRole.CLINIC_ADMIN, "admin")]
)
async def test_other_roles_go_to_their_own_area(client, db, clinic, role, area):
    await make_user(db, email="other@x.com", role=role, clinic_id=clinic.id)
    await login(client, "other@x.com")
    some = uuid.uuid4()
    calls = [
        ("GET", LIST),
        ("GET", f"{LIST}/{some}"),
        ("POST", f"{LIST}/{some}/cancel"),
        ("GET", f"{LIST}/{some}/reschedule"),
    ]
    for method, path in calls:
        r = await client.request(method, path)
        assert r.status_code == 303 and r.headers["location"] == f"/en/{area}"


# ── The list ─────────────────────────────────────────────────────────────
async def test_the_list_splits_upcoming_and_past(patient_client, db, clinic, me):
    doctor, session = await world(db, clinic)
    await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    await add_appointment(db, me, doctor, clinic, session, at(TODAY, "09:00"))  # started
    await add_appointment(db, me, doctor, clinic, session, at(WED, "11:00"), status="cancelled")
    r = await patient_client.get(LIST)
    assert r.status_code == 200
    assert "Upcoming" in r.text and "Past" in r.text
    assert "Dr Booking" in r.text and "Shifa International" in r.text
    assert "2026-10-14 10:00" in r.text and "2026-10-12 09:00" in r.text
    assert "Confirmed" in r.text and "Cancelled" in r.text
    # Only the one upcoming row can be cancelled or moved.
    assert r.text.count("/cancel\"") == 1 and r.text.count("/reschedule\"") == 1


async def test_empty_states(patient_client):
    r = await patient_client.get(LIST)
    assert "You have no upcoming appointments." in r.text and "Nothing here yet." in r.text
    assert 'href="/en/patient/doctors"' in r.text


async def test_the_list_never_shows_another_patients_appointments(patient_client, db, clinic):
    doctor, session = await world(db, clinic)
    _, other, _ = await make_patient(db, email="p2@x.com", name="Patient 2")
    await add_appointment(db, other, doctor, clinic, session, at(WED, "10:00"))
    r = await patient_client.get(LIST)
    assert "2026-10-14 10:00" not in r.text and "Patient 2" not in r.text


async def test_the_list_survives_the_doctor_being_unverified_later(
    patient_client, db, clinic, me
):
    doctor, session = await world(db, clinic)
    await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    doctor.verification_status = VerificationStatus.REJECTED
    doctor.verified_by = None
    doctor.verified_at = None
    await db.commit()
    r = await patient_client.get(LIST)
    assert r.status_code == 200 and "Dr Booking" in r.text


async def test_names_are_escaped_in_the_list(patient_client, db, clinic, me):
    doctor, session = await world(db, clinic)
    doctor.full_name = "<i>Dr</i>"
    await db.commit()
    await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    r = await patient_client.get(LIST)
    assert "<i>Dr</i>" not in r.text and "&lt;i&gt;Dr&lt;/i&gt;" in r.text


async def test_the_urdu_list_is_right_to_left(patient_client, db, clinic, me):
    doctor, session = await world(db, clinic)
    await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    r = await patient_client.get("/ur/patient/appointments")
    assert r.status_code == 200
    assert 'dir="rtl"' in r.text and "میری اپائنٹمنٹس" in r.text and "کنفرم" in r.text


async def test_the_patient_nav_has_appointments(patient_client):
    r = await patient_client.get("/en/patient")
    assert 'href="/en/patient/appointments"' in r.text


async def test_a_saved_flag_shows_a_banner_and_an_unknown_one_does_not(patient_client):
    assert "Your appointment was cancelled." in (await patient_client.get(f"{LIST}?saved=cancelled")).text
    assert "was cancelled" not in (await patient_client.get(f"{LIST}?saved=bogus")).text


# ── The confirmation ─────────────────────────────────────────────────────
async def test_the_confirmation_shows_date_and_time_only(patient_client, db, clinic, me):
    doctor, session = await world(db, clinic)
    mine = await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    r = await patient_client.get(f"{LIST}/{mine.id}?saved=booked")
    assert r.status_code == 200
    for text in (
        "Your appointment is confirmed.",
        "Dr Booking",
        "Shifa International",
        "Wednesday",
        "2026-10-14",
        "Confirmed",
    ):
        assert text in r.text
    assert ">10:00<" in r.text
    assert "Token" not in r.text  # no token number is shown (the CSS file name is lowercase)
    assert f"/en/patient/appointments/{mine.id}/cancel" in r.text
    assert f"/en/patient/appointments/{mine.id}/reschedule" in r.text
    assert 'href="/en/patient/appointments"' in r.text


async def test_the_confirmation_for_a_time_after_midnight_shows_pakistan_time(
    patient_client, db, clinic, me
):
    doctor, session = await world(db, clinic, weekday=THURSDAY, start="00:00", end="01:00")
    mine = await add_appointment(db, me, doctor, clinic, session, at(date(2026, 10, 15), "00:15"))
    r = await patient_client.get(f"{LIST}/{mine.id}")
    assert "Thursday" in r.text and "2026-10-15" in r.text and ">00:15<" in r.text


async def test_a_past_or_cancelled_appointment_has_no_actions(patient_client, db, clinic, me):
    doctor, session = await world(db, clinic)
    cancelled = await add_appointment(
        db, me, doctor, clinic, session, at(WED, "10:00"), status="cancelled"
    )
    r = await patient_client.get(f"{LIST}/{cancelled.id}")
    assert r.status_code == 200 and "/cancel" not in r.text and "/reschedule" not in r.text


async def test_someone_elses_unknown_or_malformed_confirmation_goes_to_the_list(
    patient_client, db, clinic
):
    doctor, session = await world(db, clinic)
    _, other, _ = await make_patient(db, email="p2@x.com", name="Patient 2")
    theirs = await add_appointment(db, other, doctor, clinic, session, at(WED, "10:00"))
    for target in (theirs.id, uuid.uuid4(), "junk"):
        r = await patient_client.get(f"{LIST}/{target}")
        assert r.status_code == 303 and r.headers["location"] == LIST


# ── Cancel ───────────────────────────────────────────────────────────────
async def test_cancelling_redirects_to_the_list_with_a_notice(patient_client, db, clinic, me):
    doctor, session = await world(db, clinic)
    mine = await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    r = await patient_client.post(f"{LIST}/{mine.id}/cancel")
    assert r.status_code == 303 and r.headers["location"] == f"{LIST}?saved=cancelled"
    assert await status_of(db, mine) == "cancelled"
    page = await patient_client.get(r.headers["location"])
    assert "Your appointment was cancelled." in page.text


async def test_cancelling_frees_the_time_on_the_doctor_page(patient_client, db, clinic, me):
    doctor, session = await world(db, clinic)
    mine = await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    assert "at=2026-10-14T10:00" not in (await patient_client.get(f"{FIND}/{doctor.id}")).text
    await patient_client.post(f"{LIST}/{mine.id}/cancel")
    assert "at=2026-10-14T10:00" in (await patient_client.get(f"{FIND}/{doctor.id}")).text


async def test_someone_elses_or_unknown_cancel_changes_nothing(patient_client, db, clinic):
    doctor, session = await world(db, clinic)
    _, other, _ = await make_patient(db, email="p2@x.com", name="Patient 2")
    theirs = await add_appointment(db, other, doctor, clinic, session, at(WED, "10:00"))
    for target in (theirs.id, uuid.uuid4(), "junk"):
        r = await patient_client.post(f"{LIST}/{target}/cancel")
        assert r.status_code == 303 and r.headers["location"] == LIST
    assert await status_of(db, theirs) == "booked"


async def test_a_started_or_already_cancelled_appointment_cannot_be_cancelled(
    patient_client, db, clinic, me
):
    doctor, session = await world(db, clinic)
    started = await add_appointment(db, me, doctor, clinic, session, at(TODAY, "09:00"))
    gone = await add_appointment(
        db, me, doctor, clinic, session, at(WED, "10:00"), status="cancelled"
    )
    for target in (started, gone):
        r = await patient_client.post(f"{LIST}/{target.id}/cancel")
        assert r.status_code == 409
        assert "can't be changed any more" in r.text.replace("&#39;", "'")
    assert await status_of(db, started) == "booked"


async def test_the_cancel_write_rate_limit_applies(patient_client, db, clinic, me, monkeypatch):
    monkeypatch.setattr(settings, "booking_write_rate_limit_per_minute", 1)
    doctor, session = await world(db, clinic)
    first = await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    second = await add_appointment(db, me, doctor, clinic, session, at(WED, "10:15"))
    assert (await patient_client.post(f"{LIST}/{first.id}/cancel")).status_code == 303
    r = await patient_client.post(f"{LIST}/{second.id}/cancel")
    assert r.status_code == 429 and "Too many attempts" in r.text
    assert await status_of(db, second) == "booked"


# ── The reschedule entry ─────────────────────────────────────────────────
async def test_reschedule_opens_the_doctor_page_in_move_mode(patient_client, db, clinic, me):
    doctor, session = await world(db, clinic)
    mine = await add_appointment(db, me, doctor, clinic, session, at(WED, "10:00"))
    r = await patient_client.get(f"{LIST}/{mine.id}/reschedule")
    assert r.status_code == 303
    assert r.headers["location"] == (
        f"/en/patient/doctors/{doctor.id}?clinic={clinic.id}&moving={mine.id}"
    )
    page = await patient_client.get(r.headers["location"])
    assert "Moving your appointment on 2026-10-14 at 10:00" in page.text


async def test_reschedule_of_a_started_one_is_explained_and_of_a_foreign_one_goes_to_the_list(
    patient_client, db, clinic, me
):
    doctor, session = await world(db, clinic)
    _, other, _ = await make_patient(db, email="p2@x.com", name="Patient 2")
    started = await add_appointment(db, me, doctor, clinic, session, at(TODAY, "09:00"))
    theirs = await add_appointment(db, other, doctor, clinic, session, at(WED, "10:00"))
    r = await patient_client.get(f"{LIST}/{started.id}/reschedule")
    assert r.status_code == 409 and "can't be changed any more" in r.text.replace("&#39;", "'")
    for target in (theirs.id, uuid.uuid4(), "junk"):
        r = await patient_client.get(f"{LIST}/{target}/reschedule")
        assert r.status_code == 303 and r.headers["location"] == LIST


# ── The whole journey ────────────────────────────────────────────────────
async def test_find_book_see_move_and_cancel(patient_client, db, clinic):
    doctor, _ = await world(db, clinic)
    form = {"doctor_id": str(doctor.id), "clinic_id": str(clinic.id), "slot": "2026-10-14T10:00"}
    booked = await patient_client.post(LIST, data=form)
    confirmation = await patient_client.get(booked.headers["location"])
    assert "Your appointment is confirmed." in confirmation.text

    listing = await patient_client.get(LIST)
    assert "2026-10-14 10:00" in listing.text

    appointment_id = booked.headers["location"].split("/")[-1].split("?")[0]
    moved = await patient_client.post(
        f"{LIST}/{appointment_id}/reschedule",
        data={"clinic_id": str(clinic.id), "slot": "2026-10-21T11:00"},
    )
    assert moved.status_code == 303 and "saved=rescheduled" in moved.headers["location"]
    page = await patient_client.get(moved.headers["location"])
    assert "Your appointment has been moved." in page.text and "2026-10-21" in page.text

    new_id = moved.headers["location"].split("/")[-1].split("?")[0]
    cancelled = await patient_client.post(f"{LIST}/{new_id}/cancel")
    assert cancelled.headers["location"] == f"{LIST}?saved=cancelled"
    final = await patient_client.get(LIST)
    assert "You have no upcoming appointments." in final.text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_booking_web_appointments.py -q`
Expected: failures: 404 or 405 for the missing list, confirmation, cancel and reschedule routes.

- [ ] **Step 3: Add the i18n keys**

Run the i18n helper from the File Structure section with:

```python
EN = {
    "nav.appointments": "Appointments",
    "booking.list.title": "My appointments",
    "booking.list.upcoming": "Upcoming",
    "booking.list.past": "Past",
    "booking.list.empty_upcoming": "You have no upcoming appointments.",
    "booking.list.empty_past": "Nothing here yet.",
    "booking.action.reschedule": "Reschedule",
    "booking.action.cancel": "Cancel",
    "booking.action.confirm_cancel": "Yes, cancel it",
    "booking.cancel_hint": "This frees the time for someone else.",
    "booking.saved.booked": "Your appointment is confirmed.",
    "booking.saved.rescheduled": "Your appointment has been moved.",
    "booking.saved.cancelled": "Your appointment was cancelled.",
    "booking.appointment.title": "Your appointment",
    "booking.appointment.all": "See all my appointments",
}
UR = {
    "nav.appointments": "اپائنٹمنٹس",
    "booking.list.title": "میری اپائنٹمنٹس",
    "booking.list.upcoming": "آنے والی",
    "booking.list.past": "گزشتہ",
    "booking.list.empty_upcoming": "آپ کی کوئی آنے والی اپائنٹمنٹ نہیں۔",
    "booking.list.empty_past": "ابھی یہاں کچھ نہیں۔",
    "booking.action.reschedule": "وقت بدلیں",
    "booking.action.cancel": "منسوخ کریں",
    "booking.action.confirm_cancel": "جی ہاں، منسوخ کریں",
    "booking.cancel_hint": "اس سے یہ وقت کسی اور کے لیے خالی ہو جائے گا۔",
    "booking.saved.booked": "آپ کی اپائنٹمنٹ کنفرم ہو گئی ہے۔",
    "booking.saved.rescheduled": "آپ کی اپائنٹمنٹ کا وقت بدل دیا گیا ہے۔",
    "booking.saved.cancelled": "آپ کی اپائنٹمنٹ منسوخ کر دی گئی ہے۔",
    "booking.appointment.title": "آپ کی اپائنٹمنٹ",
    "booking.appointment.all": "میری تمام اپائنٹمنٹس دیکھیں",
}
```

- [ ] **Step 4: Write the templates**

Create `frontend/templates/booking/_actions.html`:

```html
{#- Reschedule and Cancel for one appointment `r` (an AppointmentRow). Shown only
    while `r.can_change`. Cancel is a two-step <details> confirmation, so it works
    without JavaScript. -#}
{% if r.can_change %}
  <div class="entry-row__actions">
    <a class="btn btn--secondary btn--sm" href="/{{ locale }}/patient/appointments/{{ r.id }}/reschedule">{{ t('booking.action.reschedule') }}</a>
    <details class="confirm">
      <summary class="btn btn--secondary btn--sm">{{ t('booking.action.cancel') }}</summary>
      <form method="post" action="/{{ locale }}/patient/appointments/{{ r.id }}/cancel">
        <p class="field-help">{{ t('booking.cancel_hint') }}</p>
        <button class="btn btn--danger btn--sm" type="submit"
                data-label="{{ t('booking.action.confirm_cancel') }}">{{ t('booking.action.confirm_cancel') }}</button>
      </form>
    </details>
  </div>
{% endif %}
```

Create `frontend/templates/booking/_row.html`:

```html
{#- One appointment in a list. Expects `r` (an AppointmentRow) from the loop. -#}
<li class="grant-card stack">
  <div class="entry-row__main">
    <div class="entry-row__title"><a href="/{{ locale }}/patient/appointments/{{ r.id }}">{{ r.doctor_name }}</a></div>
    <div class="field-help">{% if r.specialty %}{{ r.specialty }} · {% endif %}{{ r.clinic_name }}</div>
    <div class="mono" dir="ltr">{{ r.starts_at.date().isoformat() }} {{ hhmm(r.starts_at) }}</div>
  </div>
  <span class="badge {{ status_badge(r.status) }}">{{ status_label(r.status) }}</span>
  {% include "booking/_actions.html" %}
</li>
```

Create `frontend/templates/booking/list.html`:

```html
{% extends "base.html" %}
{#- My appointments (FR16, P5 spec 3.3). Every control works without JavaScript.
    Names are autoescaped; never use |safe here.

    Context (built in web/booking.py::_render_list):
      upcoming, past   list[AppointmentRow]
      saved            success banner text or None; page_error — error banner or None
      hhmm(), status_label(), status_badge() -#}

{% block title %}{{ t('booking.list.title') }} · {{ t('app.name') }}{% endblock %}

{% block body %}
<div class="shell">
  {% include "partials/topbar.html" %}

  <main class="content">
    {% if page_error %}
      <div class="banner banner--error" role="alert">
        <span aria-hidden="true">!</span><span>{{ page_error }}</span>
      </div>
    {% endif %}
    {% if saved %}
      <div class="banner banner--ok" role="status"><span>{{ saved }}</span></div>
    {% endif %}

    <section class="panel stack" aria-labelledby="upcoming-h">
      <h2 id="upcoming-h" class="panel__label">{{ t('booking.list.upcoming') }}</h2>
      {% if upcoming %}
        <ul class="grant-list">
          {% for r in upcoming %}{% include "booking/_row.html" %}{% endfor %}
        </ul>
      {% else %}
        <p class="empty-state">{{ t('booking.list.empty_upcoming') }}</p>
      {% endif %}
      <p><a class="btn btn--primary" href="/{{ locale }}/patient/doctors">{{ t('nav.find_doctor') }}</a></p>
    </section>

    <section class="panel stack" aria-labelledby="past-h">
      <h2 id="past-h" class="panel__label">{{ t('booking.list.past') }}</h2>
      {% if past %}
        <ul class="grant-list">
          {% for r in past %}{% include "booking/_row.html" %}{% endfor %}
        </ul>
      {% else %}
        <p class="empty-state">{{ t('booking.list.empty_past') }}</p>
      {% endif %}
    </section>
  </main>
</div>
{% endblock %}
```

Create `frontend/templates/booking/appointment.html`:

```html
{% extends "base.html" %}
{#- One appointment, and the confirmation after booking or moving (P5 spec 3.3):
    doctor, hospital, date, time and status only. No token number: the real one
    is assigned at check-in, later. Names are autoescaped.

    Context (built in web/booking.py::appointment_page):
      row     AppointmentRow; saved — success banner text or None
      weekday(day), hhmm(), status_label(), status_badge() -#}

{% block title %}{{ t('booking.appointment.title') }} · {{ t('app.name') }}{% endblock %}

{% block body %}
<div class="shell">
  {% include "partials/topbar.html" %}

  <main class="content">
    {% if saved %}
      <div class="banner banner--ok" role="status"><span>{{ saved }}</span></div>
    {% endif %}

    <div class="panel stack">
      <h2 class="panel__label">{{ t('booking.appointment.title') }}</h2>
      <dl class="stack" style="margin:0">
        <div>
          <dt class="eyebrow">{{ t('booking.field.doctor') }}</dt>
          <dd style="margin:0">{{ row.doctor_name }}{% if row.specialty %} · {{ row.specialty }}{% endif %}</dd>
        </div>
        <div>
          <dt class="eyebrow">{{ t('booking.field.hospital') }}</dt>
          <dd style="margin:0">{{ row.clinic_name }}</dd>
        </div>
        <div>
          <dt class="eyebrow">{{ t('booking.field.date') }}</dt>
          <dd style="margin:0">{{ weekday(row.starts_at) }} <bdi>{{ row.starts_at.date().isoformat() }}</bdi></dd>
        </div>
        <div>
          <dt class="eyebrow">{{ t('booking.field.time') }}</dt>
          <dd style="margin:0" class="mono" dir="ltr">{{ hhmm(row.starts_at) }}</dd>
        </div>
        <div>
          <dt class="eyebrow">{{ t('booking.field.status') }}</dt>
          <dd style="margin:0"><span class="badge {{ status_badge(row.status) }}">{{ status_label(row.status) }}</span></dd>
        </div>
      </dl>
      {% set r = row %}
      {% include "booking/_actions.html" %}
      <p><a class="btn btn--secondary" href="/{{ locale }}/patient/appointments">{{ t('booking.appointment.all') }}</a></p>
    </div>
  </main>
</div>
{% endblock %}
```

`weekday(...)` is called with a datetime here and a date elsewhere: both have `.weekday()`, so one helper serves both.

- [ ] **Step 5: Write the handlers**

Append to `backend/app/web/booking.py`:

```python


# ── My appointments ──────────────────────────────────────────────────────
# `?saved=` values the list and the confirmation understand (anything else is ignored).
_SAVED = {
    "booked": "booking.saved.booked",
    "rescheduled": "booking.saved.rescheduled",
    "cancelled": "booking.saved.cancelled",
}


async def _render_list(
    request: Request,
    loc: str,
    session: SessionDep,
    actor: Actor,
    *,
    status_code: int = 200,
    saved: str = "",
    page_error: str | None = None,
) -> Response:
    upcoming, past = await service.my_appointments(session, actor)
    key = _SAVED.get(saved)
    return _render(
        request,
        "booking/list.html",
        loc,
        status_code=status_code,
        path_suffix="/patient/appointments",
        upcoming=upcoming,
        past=past,
        saved=translate(key, loc) if key else None,
        page_error=page_error,
        **_ctx(loc, "booking.list.title", current="appointments"),
    )


@router.get("/{locale}/patient/appointments", response_class=HTMLResponse)
async def list_page(
    request: Request,
    locale: str,
    session: SessionDep,
    actor: OptionalActorDep,
    saved: str = "",
) -> Response:
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    return await _render_list(request, normalise_locale(locale), session, actor, saved=saved)


@router.get("/{locale}/patient/appointments/{appointment_id}", response_class=HTMLResponse)
async def appointment_page(
    request: Request,
    locale: str,
    appointment_id: str,
    session: SessionDep,
    actor: OptionalActorDep,
    saved: str = "",
) -> Response:
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    list_url = f"/{loc}/patient/appointments"
    parsed = _parse_id(appointment_id)
    if parsed is None:
        return RedirectResponse(list_url, status_code=303)
    try:
        row = await service.appointment_for_patient(session, actor, parsed)
    except NotFound:  # not theirs and not there look the same
        return RedirectResponse(list_url, status_code=303)
    key = _SAVED.get(saved)
    return _render(
        request,
        "booking/appointment.html",
        loc,
        path_suffix=f"/patient/appointments/{row.id}",
        row=row,
        saved=translate(key, loc) if key else None,
        **_ctx(loc, "booking.appointment.title", current="appointments"),
    )


@router.post("/{locale}/patient/appointments/{appointment_id}/cancel")
async def cancel_submit(
    request: Request,
    locale: str,
    appointment_id: str,
    session: SessionDep,
    actor: OptionalActorDep,
) -> Response:
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    list_url = f"/{loc}/patient/appointments"
    parsed = _parse_id(appointment_id)
    if parsed is None:
        return RedirectResponse(list_url, status_code=303)
    try:
        await check_booking_write_rate(actor)
    except RateLimited:
        return await _render_list(
            request,
            loc,
            session,
            actor,
            status_code=429,
            page_error=translate("errors.rate_limited", loc),
        )
    try:
        await service.cancel(session, actor, parsed, user_agent=request.headers.get("user-agent"))
    except BookingRefused as exc:
        await session.rollback()
        return await _render_list(
            request,
            loc,
            session,
            actor,
            status_code=exc.http_status,
            page_error=translate(exc.message_key, loc),
        )
    except NotFound:
        await session.rollback()
        return RedirectResponse(list_url, status_code=303)
    return RedirectResponse(f"{list_url}?saved=cancelled", status_code=303)


@router.get("/{locale}/patient/appointments/{appointment_id}/reschedule")
async def reschedule_page(
    request: Request,
    locale: str,
    appointment_id: str,
    session: SessionDep,
    actor: OptionalActorDep,
) -> Response:
    """The way into "move this booking": the doctor page, in move mode."""
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    list_url = f"/{loc}/patient/appointments"
    parsed = _parse_id(appointment_id)
    if parsed is None:
        return RedirectResponse(list_url, status_code=303)
    try:
        row = await service.appointment_for_patient(session, actor, parsed)
    except NotFound:
        return RedirectResponse(list_url, status_code=303)
    if not row.can_change:
        return await _render_list(
            request,
            loc,
            session,
            actor,
            status_code=409,
            page_error=translate("errors.booking_not_cancellable", loc),
        )
    params = urlencode({"clinic": str(row.clinic_id), "moving": str(row.id)})
    return RedirectResponse(f"/{loc}/patient/doctors/{row.doctor_id}?{params}", status_code=303)
```

In `backend/app/web/router.py`, in `_patient_nav`, add after the `doctors` entry:

```python
        {
            "href": f"/{loc}/patient/appointments",
            "label": translate("nav.appointments", loc),
            "current": current == "appointments",
        },
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_booking_web_appointments.py tests/test_booking_web_book.py tests/test_booking_web_find.py -q`
Expected: all pass (about 85).

- [ ] **Step 7: Lint, run the whole booking suite, and commit**

```bash
uv run ruff check backend tests --fix && uv run ruff format backend tests
uv run pytest tests/test_booking_*.py -q
git add backend/app/web/booking.py backend/app/web/router.py frontend/templates/booking backend/app/i18n/messages tests/test_booking_web_appointments.py
git commit -m "feat(booking): my appointments, the confirmation, cancel and the reschedule entry"
```

---

### Task 10: Doctor pages: the day's appointments, and the dashboard panel

**Files:**
- Modify: `backend/app/booking/display.py` (append), `backend/app/web/booking.py` (imports, append), `backend/app/web/router.py` (`_doctor_nav`, `_guarded`, two imports), `frontend/templates/shell.html`, `backend/app/i18n/messages/en.json`, `ur.json`
- Create: `frontend/templates/booking/doctor_day.html`, `frontend/templates/booking/_day_table.html`, `frontend/templates/doctor/_appointments_today.html`
- Test: `tests/test_booking_display.py` (append), `tests/test_booking_web_doctor.py`

**Interfaces:**
- Consumes: `service.doctor_day(session, actor, day) -> list[DayRow]` and `service.current_day()` (Task 6, Task 4); `schemas.DayRow(starts_at, patient_name, status)`; `display.hhmm`, `display.status_label`, `display.status_badge` (Task 7); from `web/router.py`: `_doctor_nav(loc, current=..., verified=True)`.
- Produces: `display.DayRowOut(time, name, status, badge, cancelled)` and `display.build_day_rows(rows, locale) -> list[DayRowOut]`; `GET /{locale}/doctor/appointments` (query `date`, `YYYY-MM-DD`); on the doctor home page, an "Appointments today" panel above "My patients" for a verified doctor only; a nav entry "Appointments" for a verified doctor.
- The doctor sees **name, time and status only**. Nothing else about the patient is read here, so nothing else can leak.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_booking_display.py` (add `from app.booking.schemas import DayRow`, `from app.db import types as dbtypes` and `DayRowOut` access through `display`):

```python
from app.booking.schemas import DayRow
from app.db import types as dbtypes


def test_build_day_rows_has_name_time_and_status_only():
    rows = [
        DayRow(
            starts_at=at(WED, "10:00").astimezone(dbtypes.PKT),
            patient_name="Ayesha Raza",
            status="booked",
        ),
        DayRow(
            starts_at=at(WED, "00:15").astimezone(dbtypes.PKT),
            patient_name="Bilal Khan",
            status="cancelled",
        ),
    ]
    out = display.build_day_rows(rows, "en")
    assert out == [
        display.DayRowOut(
            time="10:00", name="Ayesha Raza", status="Confirmed", badge="badge--ok", cancelled=False
        ),
        display.DayRowOut(
            time="00:15",
            name="Bilal Khan",
            status="Cancelled",
            badge="badge--err",
            cancelled=True,
        ),
    ]
    assert set(display.DayRowOut.__dataclass_fields__) == {
        "time",
        "name",
        "status",
        "badge",
        "cancelled",
    }


def test_build_day_rows_speaks_urdu_and_handles_nothing():
    row = DayRow(starts_at=at(WED, "10:00"), patient_name="X", status="done")
    assert display.build_day_rows([row], "ur")[0].status == "مکمل"
    assert display.build_day_rows([], "en") == []
```

Create `tests/test_booking_web_doctor.py`:

```python
"""P5 web: the doctor's appointments page and the dashboard panel.

The doctor sees name, time and status only; booking is not consent (FR4)."""

from __future__ import annotations

from datetime import date

import pytest
from app.db.models import UserRole
from app.i18n.catalogue import translate

from tests.booking_helpers import (
    MONDAY,
    THURSDAY,
    TODAY,
    WED,
    add_appointment,
    add_session,
    at,
    freeze,
    login,
    make_patient,
    verified_doctor,
)
from tests.conftest import make_user

PAGE = "/en/doctor/appointments"


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    freeze(monkeypatch)


@pytest.fixture
async def mine(db, clinic):
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic, weekday=MONDAY, start="09:00", end="18:00")
    return doctor, session


@pytest.fixture
async def doctor_client(client, mine):
    await login(client, "booking.dr@x.com")
    return client


# ── Guards ───────────────────────────────────────────────────────────────
async def test_anonymous_goes_to_login(client):
    r = await client.get(PAGE)
    assert r.status_code == 303
    assert r.headers["location"] == "/en/login?next=/en/doctor/appointments"


@pytest.mark.parametrize(
    ("role", "area"), [(UserRole.PATIENT, "patient"), (UserRole.CLINIC_ADMIN, "admin")]
)
async def test_other_roles_go_to_their_own_area(client, db, clinic, role, area):
    await make_user(db, email="other@x.com", role=role, clinic_id=clinic.id)
    await login(client, "other@x.com")
    r = await client.get(PAGE)
    assert r.status_code == 303 and r.headers["location"] == f"/en/{area}"


async def test_a_pending_doctor_is_sent_to_their_dashboard(client, db, clinic):
    await make_user(db, email="pending@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    await login(client, "pending@x.com")
    r = await client.get(PAGE)
    assert r.status_code == 303 and r.headers["location"] == "/en/doctor"


# ── The day's list ───────────────────────────────────────────────────────
async def test_todays_appointments_show_time_name_and_status(doctor_client, db, clinic, mine):
    doctor, session = mine
    _, patient, _ = await make_patient(db)
    await add_appointment(db, patient, doctor, clinic, session, at(TODAY, "14:00"))
    r = await doctor_client.get(PAGE)
    assert r.status_code == 200
    assert "Pat Booking" in r.text and ">14:00<" in r.text and "Confirmed" in r.text
    assert 'value="2026-10-12"' in r.text  # the date picker starts on today


async def test_only_name_time_and_status_reach_the_page(doctor_client, db, clinic, mine):
    doctor, session = mine
    _, patient, _ = await make_patient(db)
    patient.passport_no = "CN-SECRET-77"
    await db.commit()
    await add_appointment(db, patient, doctor, clinic, session, at(TODAY, "14:00"))
    r = await doctor_client.get(PAGE)
    assert "Pat Booking" in r.text and "CN-SECRET-77" not in r.text


async def test_a_cancelled_appointment_is_shown_as_cancelled(doctor_client, db, clinic, mine):
    doctor, session = mine
    _, patient, _ = await make_patient(db)
    await add_appointment(db, patient, doctor, clinic, session, at(TODAY, "14:00"), status="cancelled")
    r = await doctor_client.get(PAGE)
    assert "Pat Booking" in r.text and "Cancelled" in r.text


async def test_an_empty_day_says_so(doctor_client):
    assert "No appointments on this day." in (await doctor_client.get(PAGE)).text


async def test_the_date_picker_chooses_the_day(doctor_client, db, clinic, mine):
    doctor, session = mine
    _, patient, _ = await make_patient(db)
    await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    on_wed = await doctor_client.get(f"{PAGE}?date=2026-10-14")
    assert "Pat Booking" in on_wed.text and "Wednesday" in on_wed.text
    assert "Pat Booking" not in (await doctor_client.get(PAGE)).text  # today is Monday
    assert "Pat Booking" not in (await doctor_client.get(f"{PAGE}?date=2026-10-15")).text


@pytest.mark.parametrize(
    "raw", ["junk", "", "0001-01-01", "9999-12-31", "2028-01-01", "2026-13-40"]
)
async def test_a_bad_or_far_away_date_shows_today(doctor_client, raw):
    r = await doctor_client.get(f"{PAGE}?date={raw}")
    assert r.status_code == 200 and 'value="2026-10-12"' in r.text


async def test_the_day_links_step_by_one_day(doctor_client):
    r = await doctor_client.get(f"{PAGE}?date=2026-10-14")
    assert "date=2026-10-13" in r.text and "date=2026-10-15" in r.text


async def test_a_time_just_after_midnight_belongs_to_its_pakistan_day(
    doctor_client, db, clinic, mine
):
    # Review focus 2: 00:15 on Thursday the 15th is 19:15 UTC on the 14th.
    doctor, _ = mine
    thursday_session = await add_session(
        db, doctor, clinic, weekday=THURSDAY, start="00:00", end="01:00"
    )
    _, patient, _ = await make_patient(db)
    await add_appointment(
        db, patient, doctor, clinic, thursday_session, at(date(2026, 10, 15), "00:15")
    )
    on_thursday = await doctor_client.get(f"{PAGE}?date=2026-10-15")
    on_wednesday = await doctor_client.get(f"{PAGE}?date=2026-10-14")
    assert "Pat Booking" in on_thursday.text and ">00:15<" in on_thursday.text
    assert "Pat Booking" not in on_wednesday.text


async def test_another_doctors_patients_are_not_listed(doctor_client, db, clinic):
    rival = await verified_doctor(db, clinic, email="rival@x.com", name="Dr Rival")
    rival_session = await add_session(db, rival, clinic, weekday=MONDAY, start="09:00", end="18:00")
    _, patient, _ = await make_patient(db)
    await add_appointment(db, patient, rival, clinic, rival_session, at(TODAY, "14:00"))
    assert "Pat Booking" not in (await doctor_client.get(PAGE)).text


async def test_names_are_escaped(doctor_client, db, clinic, mine):
    doctor, session = mine
    _, patient, _ = await make_patient(db, name="<b>Pat</b>")
    await add_appointment(db, patient, doctor, clinic, session, at(TODAY, "14:00"))
    r = await doctor_client.get(PAGE)
    assert "<b>Pat</b>" not in r.text and "&lt;b&gt;Pat&lt;/b&gt;" in r.text


async def test_the_urdu_page_is_right_to_left(doctor_client):
    r = await doctor_client.get("/ur/doctor/appointments")
    assert r.status_code == 200 and 'dir="rtl"' in r.text and "اپائنٹمنٹس" in r.text
    assert 'href="/en/doctor/appointments?date=2026-10-12"' in r.text  # the language switch


# ── The nav and the dashboard panel ──────────────────────────────────────
async def test_a_verified_doctors_nav_has_appointments(doctor_client):
    r = await doctor_client.get("/en/doctor")
    assert 'href="/en/doctor/appointments"' in r.text


async def test_the_dashboard_panel_lists_today_above_my_patients(doctor_client, db, clinic, mine):
    doctor, session = mine
    _, patient, _ = await make_patient(db)
    await add_appointment(db, patient, doctor, clinic, session, at(TODAY, "14:00"))
    r = await doctor_client.get("/en/doctor")
    assert r.status_code == 200
    assert "Appointments today" in r.text and "Pat Booking" in r.text and ">14:00<" in r.text
    patients_title = translate("doctor_dashboard.patients.title", "en")
    assert r.text.index("Appointments today") < r.text.index(patients_title)


async def test_the_dashboard_panel_is_empty_with_no_appointments(doctor_client):
    r = await doctor_client.get("/en/doctor")
    assert "Appointments today" in r.text and "No appointments today." in r.text


async def test_the_dashboard_panel_only_shows_today(doctor_client, db, clinic, mine):
    doctor, session = mine
    _, patient, _ = await make_patient(db)
    await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    assert "Pat Booking" not in (await doctor_client.get("/en/doctor")).text


async def test_a_pending_doctor_sees_no_panel_and_no_nav_entry(client, db, clinic):
    await make_user(db, email="pending@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    await login(client, "pending@x.com")
    r = await client.get("/en/doctor")
    assert r.status_code == 200
    assert "Appointments today" not in r.text
    assert "/en/doctor/appointments" not in r.text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_booking_display.py tests/test_booking_web_doctor.py -q`
Expected: failures: `AttributeError: module 'app.booking.display' has no attribute 'build_day_rows'` and 404s for the missing page.

- [ ] **Step 3: Add the i18n keys**

Run the i18n helper from the File Structure section with:

```python
EN = {
    "booking.doctor_day.title": "Appointments",
    "booking.doctor_day.note": "Name, time and status only. Opening a record still needs the patient's permission.",
    "booking.doctor_day.show": "Show",
    "booking.doctor_day.prev": "Previous day",
    "booking.doctor_day.next": "Next day",
    "booking.doctor_day.today": "Today",
    "booking.doctor_day.empty": "No appointments on this day.",
    "booking.col.patient": "Patient",
    "booking.today.title": "Appointments today",
    "booking.today.empty": "No appointments today.",
    "booking.today.all": "See all appointments",
}
UR = {
    "booking.doctor_day.title": "اپائنٹمنٹس",
    "booking.doctor_day.note": "صرف نام، وقت اور حالت۔ ریکارڈ کھولنے کے لیے پھر بھی مریض کی اجازت درکار ہے۔",
    "booking.doctor_day.show": "دکھائیں",
    "booking.doctor_day.prev": "پچھلا دن",
    "booking.doctor_day.next": "اگلا دن",
    "booking.doctor_day.today": "آج",
    "booking.doctor_day.empty": "اس دن کوئی اپائنٹمنٹ نہیں۔",
    "booking.col.patient": "مریض",
    "booking.today.title": "آج کی اپائنٹمنٹس",
    "booking.today.empty": "آج کوئی اپائنٹمنٹ نہیں۔",
    "booking.today.all": "تمام اپائنٹمنٹس دیکھیں",
}
```

- [ ] **Step 4: Write the display rows**

In `backend/app/booking/display.py`, replace the import block with:

```python
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from ..i18n.catalogue import translate
from . import slots
from .schemas import DayRow
```

and append:

```python


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
```

- [ ] **Step 5: Write the templates**

Create `frontend/templates/booking/_day_table.html`:

```html
{#- A doctor's appointments for one day. Expects `day_rows` (list[DayRowOut]).
    Name, time and status only. Names are autoescaped; never use |safe here.
    The time is forced left-to-right on the Urdu page. -#}
<table class="patient-table">
  <thead>
    <tr>
      <th scope="col">{{ t('booking.field.time') }}</th>
      <th scope="col">{{ t('booking.col.patient') }}</th>
      <th scope="col">{{ t('booking.field.status') }}</th>
    </tr>
  </thead>
  <tbody>
    {% for row in day_rows %}
      <tr{% if row.cancelled %} style="color:var(--muted)"{% endif %}>
        <td dir="ltr" class="mono">{{ row.time }}</td>
        <td>{{ row.name }}</td>
        <td><span class="badge {{ row.badge }}">{{ row.status }}</span></td>
      </tr>
    {% endfor %}
  </tbody>
</table>
```

Create `frontend/templates/booking/doctor_day.html`:

```html
{% extends "base.html" %}
{#- A verified doctor's appointments for one day (P5 spec 3.3). Name, time and
    status only: a booking is not consent, so opening a record still needs the
    patient's own grant (FR4). Works without JavaScript: the date picker is a GET form.

    Context (built in web/booking.py::doctor_day_page):
      day, weekday_name   the Pakistan date shown and its weekday name
      rows                list[DayRowOut]
      prev_href, next_href, today_href -#}

{% block title %}{{ t('booking.doctor_day.title') }} · {{ t('app.name') }}{% endblock %}

{% block body %}
<div class="shell">
  {% include "partials/topbar.html" %}

  <main class="content">
    <div class="panel stack">
      <p class="lead">{{ t('booking.doctor_day.note') }}</p>
      <form class="stack" method="get" action="/{{ locale }}/doctor/appointments">
        <label class="field" for="f-date">
          <span class="field-label">{{ t('booking.field.date') }}</span>
          <input class="input" id="f-date" name="date" type="date" value="{{ day.isoformat() }}">
        </label>
        <div class="btn-row">
          <button class="btn btn--primary" type="submit">{{ t('booking.doctor_day.show') }}</button>
          <a class="btn btn--secondary btn--sm" href="{{ prev_href }}">{{ t('booking.doctor_day.prev') }}</a>
          <a class="btn btn--secondary btn--sm" href="{{ today_href }}">{{ t('booking.doctor_day.today') }}</a>
          <a class="btn btn--secondary btn--sm" href="{{ next_href }}">{{ t('booking.doctor_day.next') }}</a>
        </div>
      </form>
    </div>

    <div class="panel">
      <div class="panel__label">{{ weekday_name }} <bdi>{{ day.isoformat() }}</bdi></div>
      {% if rows %}
        {% set day_rows = rows %}
        {% include "booking/_day_table.html" %}
      {% else %}
        <p class="empty-state">{{ t('booking.doctor_day.empty') }}</p>
      {% endif %}
    </div>
  </main>
</div>
{% endblock %}
```

Create `frontend/templates/doctor/_appointments_today.html`:

```html
{#- P5 — a verified doctor's appointments today, above "My patients".
    Context: appointments_today  list[DayRowOut] (name, time and status only).
    Names are autoescaped; never use |safe here. -#}
<div class="panel" style="margin-top:var(--sp-5)">
  <div class="panel__label">{{ t('booking.today.title') }}</div>
  {% if appointments_today %}
    {% set day_rows = appointments_today %}
    {% include "booking/_day_table.html" %}
  {% else %}
    <p style="margin:0;font-size:13.5px;color:var(--muted)">{{ t('booking.today.empty') }}</p>
  {% endif %}
  <p style="margin:var(--sp-3) 0 0"><a href="/{{ locale }}/doctor/appointments">{{ t('booking.today.all') }}</a></p>
</div>
```

In `frontend/templates/shell.html`, replace

```html
    {% if patients is defined %}
      {% include "doctor/_patient_list.html" %}
    {% endif %}
```

with

```html
    {% if appointments_today is defined %}
      {% include "doctor/_appointments_today.html" %}
    {% endif %}

    {% if patients is defined %}
      {% include "doctor/_patient_list.html" %}
    {% endif %}
```

- [ ] **Step 6: Write the handler**

In `backend/app/web/booking.py`, change the imports: `from datetime import date, datetime, timedelta`, and `from fastapi import APIRouter, Form, Query, Request`. Append:

```python


# ── The doctor's day ─────────────────────────────────────────────────────
_DAY_SPAN = 366  # a date further than this from today shows today instead


def _doctor_guard(request: Request, locale: str, actor: Actor | None) -> Response | None:
    """Signed out goes to sign-in and back here, another role goes to its own area,
    and a doctor who is not verified goes to their dashboard (where the banner
    explains why): booking shows a doctor's patients, so it is for verified doctors."""
    loc = normalise_locale(locale)
    if actor is None:
        return login_redirect(loc, request)
    if actor.role != UserRole.DOCTOR.value:
        return RedirectResponse(f"/{loc}/{_area(actor.role)}", status_code=303)
    if not actor.onboarding_complete:
        return RedirectResponse(f"/{loc}/onboarding", status_code=303)
    if not actor.is_verified_doctor:
        return RedirectResponse(f"/{loc}/doctor", status_code=303)
    return None


def _pick_day(raw: str, today: date) -> date:
    """The day asked for; today when it is missing, malformed or more than a year away."""
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        return today
    return parsed if abs((parsed - today).days) <= _DAY_SPAN else today


@router.get("/{locale}/doctor/appointments", response_class=HTMLResponse)
async def doctor_day_page(
    request: Request,
    locale: str,
    session: SessionDep,
    actor: OptionalActorDep,
    day_raw: Annotated[str, Query(alias="date")] = "",
) -> Response:
    if (redirect := _doctor_guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    day = _pick_day(day_raw, service.current_day())
    try:
        found = await service.doctor_day(session, actor, day)
    except NotFound:
        return RedirectResponse(f"/{loc}/doctor", status_code=303)
    base = f"/{loc}/doctor/appointments"
    return _render(
        request,
        "booking/doctor_day.html",
        loc,
        path_suffix=f"/doctor/appointments?date={day.isoformat()}",
        crumb=translate("auth.role.doctor", loc),
        page_title=translate("booking.doctor_day.title", loc),
        nav_items=_doctor_nav(loc, current="appointments"),
        day=day,
        weekday_name=translate(f"weekday.{day.weekday()}", loc),
        rows=display.build_day_rows(found, loc),
        prev_href=f"{base}?date={(day - timedelta(days=1)).isoformat()}",
        next_href=f"{base}?date={(day + timedelta(days=1)).isoformat()}",
        today_href=base,
    )
```

and add `_doctor_nav` to the `from .router import ...` line: `from .router import _area, _doctor_nav, _patient_nav, _render, login_redirect`.

- [ ] **Step 7: Wire the nav and the dashboard panel in `web/router.py`**

Add two imports next to the other service imports:

```python
from ..booking import display as booking_display
from ..booking import service as booking_service
```

In `_doctor_nav`, replace

```python
    if verified:
        items.append(
            {
                "href": f"/{loc}/doctor/passport-lookup",
                "label": translate("nav.lookup", loc),
                "current": current == "lookup",
            }
        )
```

with

```python
    if verified:
        items.append(
            {
                "href": f"/{loc}/doctor/passport-lookup",
                "label": translate("nav.lookup", loc),
                "current": current == "lookup",
            }
        )
        items.append(
            {
                "href": f"/{loc}/doctor/appointments",
                "label": translate("nav.appointments", loc),
                "current": current == "appointments",
            }
        )
```

In `_guarded`, next to `patients = None` add `appointments_today = None`. In the `elif actor.is_verified_doctor:` branch, directly after its `await session.commit()` line (the one after the `DOCTOR_DASHBOARD_VIEW` audit write), add:

```python
        # P5: today's appointments, name, time and status only (booking is not consent).
        appointments_today = booking_display.build_day_rows(
            await booking_service.doctor_day(session, actor, booking_service.current_day()), loc
        )
```

and where `extra` is filled, add after the `patients` lines:

```python
    if appointments_today is not None:
        extra["appointments_today"] = appointments_today
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/test_booking_display.py tests/test_booking_web_doctor.py -q`
Expected: all pass (about 30).

- [ ] **Step 9: Lint, run the doctor and dashboard suites, and commit**

```bash
uv run ruff check backend tests --fix && uv run ruff format backend tests
uv run pytest tests/test_booking_*.py tests/test_doctor_dashboard.py tests/test_doctor_rows.py tests/test_schedule_web.py tests/test_verification.py -q
git add backend/app/booking/display.py backend/app/web/booking.py backend/app/web/router.py frontend/templates backend/app/i18n/messages tests/test_booking_display.py tests/test_booking_web_doctor.py
git commit -m "feat(booking): the doctor's day and the dashboard panel"
```

---

### Task 11: Quality gate, documentation, manual check, review, hand-off

**Files:**
- Create: `tests/test_booking_i18n.py`
- Modify: `CLAUDE.md`
- No application code changes unless a gate step finds a defect.

**Interfaces:**
- Consumes: everything from Tasks 1 to 10.
- Produces: a green branch, a documented feature, an offline SQL file for the schema owner, a review record, and a hand-off message. **Nothing is pushed, merged, or run against the shared Supabase project in this task without Zarwa's go-ahead.**

- [ ] **Step 1: Write the i18n completeness tests**

Create `tests/test_booking_i18n.py`:

```python
"""P5: every string the booking pages show exists in English and Urdu (FR28)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from app.i18n.catalogue import missing_keys

ROOT = Path(__file__).resolve().parents[1]
MESSAGES = ROOT / "backend" / "app" / "i18n" / "messages"
TEMPLATE_DIRS = [ROOT / "frontend" / "templates" / "booking"]
TEMPLATE_FILES = [ROOT / "frontend" / "templates" / "doctor" / "_appointments_today.html"]
PYTHON_FILES = [
    ROOT / "backend" / "app" / "web" / "booking.py",
    ROOT / "backend" / "app" / "booking" / "display.py",
]

IN_TEMPLATE = re.compile(r"""\bt\(\s*'([a-z0-9_.]+)'""")
IN_PYTHON = re.compile(r'"((?:booking|nav|errors)\.[a-z0-9_.]+)"')
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def catalogue(locale: str) -> dict[str, str]:
    return json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf8"))


def keys_used() -> set[str]:
    used: set[str] = set()
    templates = [p for d in TEMPLATE_DIRS for p in d.glob("*.html")] + TEMPLATE_FILES
    for path in templates:
        used |= set(IN_TEMPLATE.findall(path.read_text(encoding="utf8")))
    for path in PYTHON_FILES:
        used |= set(IN_PYTHON.findall(path.read_text(encoding="utf8")))
    # Looked up by building the key: `booking.status.<status>` and `weekday.<n>`.
    used |= {f"booking.status.{s}" for s in ("booked", "checked_in", "done", "cancelled")}
    used |= {f"weekday.{n}" for n in range(7)}
    return used


def test_the_scan_finds_the_booking_keys():
    used = keys_used()
    assert len(used) > 50
    assert {"booking.find.title", "booking.list.title", "nav.appointments"} <= used


def test_every_key_the_booking_pages_use_exists_in_both_languages():
    en, ur = catalogue("en"), catalogue("ur")
    missing = sorted(k for k in keys_used() if k not in en or k not in ur)
    assert not missing, missing


def test_no_key_is_missing_from_urdu_anywhere():
    assert missing_keys()["ur"] == []


@pytest.mark.parametrize("prefix", ["booking.", "errors.booking_", "nav.find_doctor", "nav.appointments"])
def test_placeholders_match_between_the_languages(prefix):
    en, ur = catalogue("en"), catalogue("ur")
    for key in (k for k in en if k.startswith(prefix)):
        assert set(PLACEHOLDER.findall(en[key])) == set(PLACEHOLDER.findall(ur[key])), key


def test_the_urdu_text_is_not_a_copy_of_the_english():
    en, ur = catalogue("en"), catalogue("ur")
    same = sorted(
        k for k in en if k.startswith(("booking.", "errors.booking_")) and en[k] == ur[k]
    )
    assert not same, same
```

- [ ] **Step 2: Run the new tests**

Run: `uv run pytest tests/test_booking_i18n.py -q`
Expected: all pass. If a key is reported missing, add it with the i18n helper (both languages) rather than weakening the test.

- [ ] **Step 3: Run the full gate**

```bash
uv run ruff check backend tests alembic
uv run ruff format --check backend tests alembic
uv run pytest -q
```

Expected: ruff clean; every test passes. One failure is already known and not ours (CLAUDE.md): `tests/test_schedule_schemas.py::test_times_must_be_whole_minutes_hh_mm[24:00]` fails on Python 3.14. Any other failure is ours: fix it (a nav or dashboard change can move an older page test) and re-run. Record the final pass count for the hand-off message.

Then check the migration chain offline (these commands connect to nothing):

```bash
export DATABASE_URL=postgresql+asyncpg://offline:offline@127.0.0.1:9/offline
uv run alembic heads
uv run alembic history | head -5
uv run alembic upgrade a7d3c89106f9:head --sql
```

Expected: `heads` prints exactly one head, `92b61c6e9db4`; the SQL is one `CREATE UNIQUE INDEX uq_appointment_active_slot ON appointment (schedule_id, slot_start) WHERE status <> 'cancelled'` plus the `alembic_version` update. Save that SQL to a scratch file outside the repo for the schema owner (Step 8).

- [ ] **Step 4: Document the feature in `CLAUDE.md`**

Make these additive edits:

1. In "Testing", after the `tests/test_schedule_*.py` line add:

```
uv run pytest tests/test_booking_*.py -v   # P5 appointment booking tests
```

2. In the "High-Level Structure" tree, after the `schedule/` line add:

```
    booking/                   # Appointment booking (P5, FR15/FR16): pure slot logic, service, display helpers — web pages only
```

and in the `web/` line, after `documents.py: document pages;` add ` booking.py: appointment booking pages;`.

3. In "Key Architectural Decisions", directly before `6. **Security Properties**`, add:

```
5b. **Appointment booking (P5, FR15 + FR16)**
   - `backend/app/booking/`: `slots.py` (pure: the grid, window membership, refusal reasons, overlap; no database, no clock), `service.py` (every rule: search, free times, book, cancel, reschedule, the patient's lists, the doctor's day), `display.py` (status labels, Pakistan `HH:MM`, the doctor's day rows), `schemas.py`. The pages are `web/booking.py`: patient `/{locale}/patient/doctors[/{id}[/confirm]]` and `/{locale}/patient/appointments[/{id}[/cancel|/reschedule]]`; doctor `/{locale}/doctor/appointments` and the "Appointments today" panel on the doctor home page. There is no JSON API yet.
   - No new table: it writes the existing `appointment` table (`booking_channel="online"`, always with `schedule_id`). One additive index, `uq_appointment_active_slot` (migration `92b61c6e9db4`: unique on `(schedule_id, slot_start)` where `status <> 'cancelled'`), is the database backstop. The app's own lock is the real guard: every book, cancel and reschedule runs `schedule.service.lock_doctor` first, re-reads what is taken, then writes. **ASK ME BEFORE RUNNING that migration on the shared project.**
   - Availability comes only from `get_effective_availability`. A time is `session start + n × slot_minutes` and the whole slot must fit; a session (one effective window on one date; an extra is counted apart from the weekly window) holds at most `max_tokens` active appointments; "active" is `booked`, `checked_in`, `done`. Bookable from 30 minutes ahead to 14 days ahead; cancel or move any time before it starts. All times are Pakistan time; `slot_start` is stored as an aware UTC instant, and SQLite hands back naive values (read them with `slots.as_utc`).
   - Only verified doctors with a live hospital are searchable or bookable (D7 decision 6). The doctor sees name, time and status only: booking is not consent (FR4). A patient cannot hold overlapping appointments or two with one doctor on one day. Reschedule is one transaction (book the new time, cancel the old); a refused move leaves the old booking. A foreign or unknown appointment id is one `NotFound` (the pages send it to the list). Audit rows (`appointment.book`, `.cancel`, `.reschedule`) hold ids only.
   - Spec: `docs/superpowers/specs/2026-10-12-appointment-booking-design.md`; plan: `docs/superpowers/plans/2026-10-12-appointment-booking-v1.md`.
```

4. In "Important Files to Understand First", after the last numbered item add:

```
N. **`backend/app/booking/slots.py`** and **`booking/service.py`** - Appointment booking (P5): the pure slot logic and the locked service on top of D7's availability. Read the P5 spec first
```

(use the next number in the list), and in "When Making Changes" add:

```
- **Booking changes (P5)**: Read `docs/superpowers/specs/2026-10-12-appointment-booking-design.md` first. Put grid and cap rules in `booking/slots.py` (pure) and every other rule in `booking/service.py`; read availability only through `schedule.service.get_effective_availability`; take `lock_doctor` before every write and re-read what is taken; never filter a patient query by anything but the caller's own `patient_id`; never show a doctor more than name, time and status; never hard-delete an appointment (cancel it).
```

- [ ] **Step 5: Commit the gate**

```bash
git add tests/test_booking_i18n.py CLAUDE.md
git commit -m "test(booking): i18n completeness; docs: describe appointment booking in CLAUDE.md"
```

- [ ] **Step 6: Manual check in the browser (no shared database)**

Start the local demo, which uses a throwaway SQLite file and a fake sign-in, and never touches the shared project: `uv run python backend/ops/scripts/local_demo.py` (serves `http://127.0.0.1:8010`; it prints the demo logins; do not edit that script on this branch). Use the demo doctor that has weekly hours and a demo patient. Check each item and note what you saw:

1. As the patient: **Find a doctor** lists the verified demo doctor with a "Verified" badge; searching by part of the name and by the hospital name finds them; a nonsense search shows the empty message.
2. Open the doctor: free times for the next 14 days appear grouped by weekday and date; with two hospitals the hospital buttons switch the times.
3. Pick a time: the confirm page shows doctor, hospital, date and time only (no token number). Confirm: the confirmation page says "Your appointment is confirmed." and shows status "Confirmed".
4. **Appointments** lists it under Upcoming. Open the doctor page again: that time is gone.
5. Reschedule: the doctor page says "Moving your appointment on ..."; pick another time on the same day, confirm; the old one shows under Past as "Cancelled", the new one under Upcoming.
6. Cancel (two-step "Yes, cancel it"): it moves to Past as "Cancelled" and the time is offered again.
7. In a second private window sign in as another patient and try to book the same time after the first patient's page was opened (open the confirm page first, book from the other window, then confirm): the page shows "Someone just booked that time" with fresh times.
8. As the doctor: the home page shows "Appointments today" above "My patients"; **Appointments** lists a chosen day with name, time and status only, and a cancelled booking as "Cancelled". No passport number, age or any record data appears.
9. Switch to Urdu on every page above: right-to-left layout, Urdu text, times and dates still readable left-to-right.
10. Narrow the window to phone width: no horizontal scrolling on the find, doctor, confirm and list pages.

If any item fails, write a test that reproduces it (Task 5, 6, 7, 8, 9 or 10 as fits), fix it, and re-run Step 3.

- [ ] **Step 7: Independent review of the whole branch**

Dispatch one fresh reviewer on the most capable model with: the spec, this plan, and `git diff dev...HEAD`. Ask for a read-only review focused on, in order: (1) any patient query not filtered by the caller's own `patient_id`, or anything that lets one patient see or change another's appointment; (2) the write path: lock before re-read, every refusal leaves nothing written, reschedule atomic, the index backstop actually reachable; (3) Pakistan-time and UTC edges (midnight, 30-minute lead, 14-day horizon, naive SQLite values); (4) the doctor seeing only name, time and status; (5) i18n and right-to-left; (6) test quality (tests that cannot fail, missing negative cases). Fix every confirmed finding with a test first, re-run Step 3, and commit each fix separately.

- [ ] **Step 8: Hand off to Zarwa. Stop here until she answers.**

Report: the final test count and ruff status, what the manual check showed, the review outcome, and these three decisions for her:

1. **Migration.** `92b61c6e9db4` adds one unique index on `appointment`. Per `CLAUDE.md` it is run only by the schema owner, never from a dev machine. Give her the offline SQL from Step 3, remind her the team must be told first, and ask that the owner confirm the `appointment` table is still empty (read-only) before applying. Nothing here runs it.
2. **Push and pull request.** Ask for her go-ahead to push `feature/appointment-booking` and open a PR into `dev` (never `main`). The PR description should say: what was built; no new table, one additive index; verified-doctors-only; doctor sees name, time and status only; and the known limits below. End it with the attribution line from the project settings.
3. **Next feature.** The patient history timeline (FR8, with the FR9 filters) is next, starting with its own brainstorm.

Known limits to state in the PR and the hand-off:

- Moving a booking inside a session that is already at its cap works in the service, but the page does not offer those times (it hides full days).
- Verification status is app-wide, not per hospital (an open product question from the doctor-verification work).
- Urdu wording has not been reviewed by a native speaker.
- No notifications, live queue, check-in, fees or token numbers (out of scope for v1).
- Teammates must run `alembic upgrade head` locally after the schema owner applies the migration.
