# Live Queue, Check-in and Walk-ins v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A clinic admin checks booked patients in and adds walk-ins; everyone queues in arrival order per doctor session; the admin sends patients in, marks them seen and moves urgent cases forward; the patient sees their live position and estimated wait; the doctor sees their queue.

**Architecture:** A new `queueing` package holds pure ordering logic (`order.py`) and a service (`service.py`) that works on the existing shared `queue_entry` table and the booking `appointment` rows, under the existing per-doctor lock. Pages live in their own router module `web/queue.py`. No new table and no migration.

**Tech Stack:** FastAPI, SQLAlchemy 2 (async), Jinja2, pytest (`asyncio_mode = "auto"`, in-memory SQLite, `FakeSupabaseAuth`), ruff.

**Spec:** `docs/superpowers/specs/2026-10-13-live-queue-checkin-design.md`

## Amendments to the spec decided while planning

These refine the spec; Task 10 updates the spec text to match.

1. **`not_booked` refusal.** Checking in a cancelled or done appointment is refused with its own reason (`not_booked`, "That appointment can't be checked in") instead of the misleading "not for today". Order of checks: already queued (no-op), `not_booked`, `not_today`, `doctor_unavailable`, `session_over`.
2. **Walk-in time.** `slot_start = max(arrival, session start + 1 microsecond)`, then the next free microsecond if that instant is held. The `+1 µs` keeps a walk-in off the session-start grid time, which it would otherwise take from a booked patient.
3. **No arrival time for booked patients.** The repo does not know whether `queue_entry` has a timestamp column, so lists show the **booked time** for booked patients and the **arrival time** for walk-ins (a walk-in's `slot_start` is its arrival).

## Global Constraints

- Branch `feature/live-queue`, stacked on `feature/appointment-booking` (booking is not in `dev` yet). Never push, merge, or run any database command against the shared Supabase project. No migration is needed.
- **Task 1 needs a real answer from Zarwa** (the schema owner's read-only check of `queue_entry`). It is the only reason to stop in this plan.
- Only a **clinic admin** (`UserRole.CLINIC_ADMIN`, value `"admin"`) acts on the queue, only for doctors affiliated with their own `actor.clinic_ids`. The doctor and the patient only view. The admin sees names, times and queue state only, never medical records.
- A queue is one doctor **session** (one effective window on one date at one clinic): `schedule_id` plus the window's time range, exactly as booking counts a session. Only **verified** doctors at a live hospital have queues.
- Order = token number among `waiting` entries. Position = waiting entries with a smaller token + 1. Estimated wait = (waiting ahead + 1 if someone is in the room) × the session's `slot_minutes`. The token itself is never shown.
- One patient in the room per session. A move permutes the existing token numbers among the waiting entries (tokens stay unique). Every action takes `schedule_service.lock_doctor` first, then re-reads, then writes.
- Statuses: appointment `booked → checked_in → done`; queue state `waiting → in_room → served`. A walk-in is an `appointment` with `booking_channel = "walk_in"`, status `checked_in`, the session's `schedule_id`, and a `queue_entry`.
- Walk-ins count toward the session cap but never block bookable times: booking's `_doctor_held` ignores `walk_in` appointments.
- Passport numbers are **masked** in the walk-in search (last 4 letters or digits visible; an exact full number matches). Search needs at least 3 characters and returns at most 8 results. Audit rows hold ids only; a lookup logs only the result count.
- A foreign or unknown id is one identical `NotFound` (the page goes back to the queue). Pakistan time (`dbtypes.PKT`); "today" is the Pakistan date.
- Every user-facing string is an i18n key present in both `en.json` and `ur.json`. Every action works without JavaScript (post, then redirect). Add no CSS: reuse existing classes.
- Style: ruff-clean (`uv run ruff check backend tests` and `uv run ruff format backend tests`; do **not** run the formatter over `alembic/`, it rewrites a teammate's merged migration). Ruff 0.16 enables the datetime-zone rules: a naive `datetime(...)` or `strptime` needs `# noqa: DTZ00x - reason`. Run tests with `uv run pytest -q`.
- Commit messages end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- **Run `git commit && task-done` as one chained command** so a failed lint or commit cannot record a completion line.

## Review Focus

Inputs the spec implies but its numbered tests do not obviously cover. Each has a test in the task named.

1. **Two staff act at the same moment** (two check-ins, or two "send in" clicks): tokens stay unique and increasing, the second click is refused as `not_waiting`, and the doctor lock is taken **before** the queue is read. [Tasks 5, 6]
2. **A walk-in is off the booking grid**: it counts toward the cap, never hides a bookable time (booking must still offer 12:00 and 12:15 when a walk-in holds 12:00:00.000001), and two walk-ins in the same instant both get a place. [Task 6]
3. **Pakistan midnight and the edges of "today"**: an appointment at 00:10 Pakistan time (the previous day in UTC) is today's; tomorrow's cannot be checked in; check-in is refused once the session has ended. [Task 5]
4. **Patients who are not in the queue**: a booked patient who never arrives never enters it; a cancelled or done appointment cannot be checked in; an appointment whose doctor is no longer verified cannot be checked in. [Task 5]
5. **Scoping and leaks**: an admin of clinic A cannot see or act on clinic B's appointments or entries; the patient card never shows another patient's name; the walk-in search never returns a full passport number and a partial number never matches. [Tasks 4, 5, 6, 7, 8]

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/queueing/__init__.py` (new) | Package marker. |
| `backend/app/queueing/schemas.py` (new) | Constants, the refusal reasons, `SessionKey`, and the plain result types. |
| `backend/app/queueing/order.py` (new) | **Pure logic, no database, no clock.** Next token, position, wait, move up, make next, token reassignment. |
| `backend/app/queueing/display.py` (new) | Pure display helpers: `mask_passport`. |
| `backend/app/queueing/service.py` (new) | Every action and view. Owns every rule. |
| `backend/app/web/queue.py` (new) | The admin queue page and its actions. |
| `backend/app/db/models.py` (modify) | The `QueueEntry` model (no migration). |
| `backend/app/booking/service.py` (modify) | Small public wrappers, and `_doctor_held` ignoring walk-ins. |
| `backend/app/errors.py`, `settings.py`, `deps.py`, `audit/writer.py` (modify) | `QueueRefused`; two rate limits; audit constants. |
| `backend/app/web/booking.py`, `web/router.py` (modify) | The patient card and badge; the doctor's queue; the admin nav entry. |
| `frontend/templates/queue/*.html`, `frontend/static/js/queue.js` (new) | The admin page, the patient card, the doctor's queue partial, the refresh script. |
| `frontend/templates/booking/{appointment,list,_row,doctor_day}.html`, `shell.html` (modify) | Hooks for the card, the badge and the doctor's queue. |
| `backend/app/i18n/messages/en.json`, `ur.json` (modify) | New keys per task. |
| `tests/fixtures/queue_entry_columns.json` (new) | The schema owner's confirmed `queue_entry` columns. |
| `tests/queue_helpers.py` (new) | Shared setup: an admin, today's session, queue entries. |
| `tests/test_queue_*.py` (new) | One file per task. |

**i18n helper used by several tasks.** Save the snippet below as a scratch script (outside the repo) and run it with a JSON file `{"en": {...}, "ur": {...}}`. It refuses to overwrite an existing key and keeps each file's line endings:

```bash
uv run python - KEYS.json <<'EOF'
import json, pathlib, sys

keys = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf8"))
for loc in ("en", "ur"):
    pairs = keys[loc]
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

### Task 1: The `queue_entry` model, the constants and the schema owner's answer

**Files:**
- Create: `backend/app/queueing/__init__.py`, `backend/app/queueing/schemas.py`
- Modify: `backend/app/db/models.py` (a `QueueEntry` class after `Appointment`)
- Create: `tests/fixtures/queue_entry_columns.json`
- Test: `tests/test_queue_model.py`, `tests/test_queue_schemas.py`

**Interfaces:**
- Produces (`models.py`): `QueueEntry` with `id`, `appointment_id` (unique), `token_number`, `state`, plus any other column the schema owner confirms.
- Produces (`schemas.py`): `STATE_WAITING = "waiting"`, `STATE_IN_ROOM = "in_room"`, `STATE_SERVED = "served"`, `CHANNEL_WALK_IN = "walk_in"`, `SEARCH_MIN = 3`, `SEARCH_LIMIT = 8`, `REFUSAL_REASONS` (tuple of 12 strings); frozen dataclasses `SessionKey(doctor_id, clinic_id, schedule_id, start: time)` with `encode() -> str` and `decode(value) -> SessionKey | None`; `Line(appointment_id, entry_id, patient_name, state, token, booked_at, walk_in)`; `SessionView(key, doctor_name, clinic_name, day, start, end, slot_minutes, cap, used, in_room, waiting, expected, seen)`; `PatientHit(patient_id, name, passport)`; `QueueStatus(state, position, wait_minutes, waiting_total, live)`.

- [ ] **Step 1: Get the schema owner's answer (the one stop in this plan)**

Ask Zarwa for the schema owner's output of the first two queries in spec section 4 (the columns of `queue_entry`, and its constraints). Also ask whether any teammate's code already writes `queue_entry`, and for the `booking_channel` query result (so `walk_in` does not clash with a name in use). **Do not continue until you have the columns answer.** If she cannot get it yet, tell her what is blocked and wait.

Save the columns as `tests/fixtures/queue_entry_columns.json`, one object per column, in table order, in exactly this shape (these three keys only):

```json
[
  {"name": "<column name>", "nullable": false, "default": null}
]
```

The plan assumes the table has at least these columns: a UUID primary key, `appointment_id` (NOT NULL, UNIQUE, references `appointment`), `token_number` (integer, NOT NULL) and `state` (NOT NULL, CHECK `waiting`/`in_room`/`served`). Whatever the answer shows rules:
- The model's primary-key column name below (`queue_entry_id`) is a guess. Use the confirmed name.
- A column the plan does not mention is added to the model. If it is NOT NULL with no database default, give it a value in `_new_entry` (Task 5, Step 3); if the answer shows a column the app cannot fill from an appointment, stop and ask Zarwa.
- If `appointment_id` is not unique, or `token_number`/`state` are missing, stop: the fallback is Approach 2 in the spec (a migration with the schema owner's approval).

Record in the ledger: `Task 1: schema owner's answer received <date>; columns: <list>`.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_queue_model.py`:

```python
"""P6: the QueueEntry model matches the shared queue_entry table."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.db.models import QueueEntry
from sqlalchemy.exc import IntegrityError

from tests.booking_helpers import (
    TODAY,
    add_appointment,
    add_session,
    at,
    make_patient,
    verified_doctor,
)

CONFIRMED = Path(__file__).parent / "fixtures" / "queue_entry_columns.json"


def test_the_model_has_exactly_the_columns_the_schema_owner_confirmed():
    confirmed = json.loads(CONFIRMED.read_text(encoding="utf8"))
    table = QueueEntry.__table__
    assert {c["name"] for c in confirmed} == {col.name for col in table.columns}
    for c in confirmed:
        assert table.columns[c["name"]].nullable == c["nullable"], c["name"]


def test_the_state_check_allows_the_three_live_states():
    checks = [str(c.sqltext) for c in QueueEntry.__table__.constraints if hasattr(c, "sqltext")]
    assert any("'waiting'" in c and "'in_room'" in c and "'served'" in c for c in checks)


async def appointment_for(db, clinic, email="q1@x.com"):
    doctor = await verified_doctor(db, clinic, email=email)
    schedule = await add_session(db, doctor, clinic, weekday=0, start="12:00", end="18:00")
    _, patient, _ = await make_patient(db, email=f"pat.{email}")
    return await add_appointment(db, patient, doctor, clinic, schedule, at(TODAY, "14:00"))


async def test_an_entry_can_be_saved_and_read_back(db, clinic):
    appointment = await appointment_for(db, clinic)
    db.add(QueueEntry(appointment_id=appointment.id, token_number=1, state="waiting"))
    await db.commit()
    saved = (await db.execute(QueueEntry.__table__.select())).one()
    assert saved.token_number == 1 and saved.state == "waiting"


async def test_one_entry_per_appointment(db, clinic):
    appointment = await appointment_for(db, clinic)
    db.add(QueueEntry(appointment_id=appointment.id, token_number=1, state="waiting"))
    await db.commit()
    db.add(QueueEntry(appointment_id=appointment.id, token_number=2, state="waiting"))
    with pytest.raises(IntegrityError):
        await db.commit()


async def test_only_the_three_states_are_allowed(db, clinic):
    appointment = await appointment_for(db, clinic)
    db.add(QueueEntry(appointment_id=appointment.id, token_number=1, state="called"))
    with pytest.raises(IntegrityError):
        await db.commit()
```

Create `tests/test_queue_schemas.py`:

```python
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
    ["", "junk", "a:b:c:d", "a:b:c", f"{uuid.uuid4()}:{uuid.uuid4()}:{uuid.uuid4()}:2561",
     f"{uuid.uuid4()}:{uuid.uuid4()}:{uuid.uuid4()}:9"],
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
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_queue_model.py tests/test_queue_schemas.py -q`
Expected: collection errors, `ImportError: cannot import name 'QueueEntry'` and `ModuleNotFoundError: No module named 'app.queueing'`.

- [ ] **Step 4: Write the code**

Create `backend/app/queueing/__init__.py`:

```python
"""Live queue, check-in and walk-ins (P6, FR17, FR19, FR24)."""
```

Create `backend/app/queueing/schemas.py`:

```python
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
        return (
            f"{self.doctor_id}:{self.clinic_id}:{self.schedule_id}:{self.start.strftime('%H%M')}"
        )

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
```

In `backend/app/db/models.py`, directly after the `Appointment` class (before the `# ── Patient profile (FR2)` comment), add (use the primary-key column name confirmed in Step 1 in place of `queue_entry_id`, and add any further confirmed column):

```python
class QueueEntry(Base):
    """queue_entry — shared, pre-existing; one row per checked-in appointment.

    Mapped for the live queue (P6). `appointment_id` is UNIQUE; `token_number`
    has no uniqueness or bound in the database, so the queue service keeps tokens
    unique under the per-doctor lock. `state` follows the live CHECK. The columns
    are the ones the schema owner confirmed (tests/fixtures/queue_entry_columns.json).
    """

    __tablename__ = "queue_entry"

    id: Mapped[uuid.UUID] = mapped_column("queue_entry_id", Uuid, primary_key=True, default=uuid7)
    appointment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("appointment.appointment_id"), unique=True, nullable=False
    )
    token_number: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="waiting")

    __table_args__ = (
        CheckConstraint("state IN ('waiting', 'in_room', 'served')", name="queue_entry_state_check"),
    )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_queue_model.py tests/test_queue_schemas.py -q`
Expected: all pass (about 15). If `test_the_model_has_exactly_the_columns...` fails, the model and the confirmed answer disagree: fix the model, never the fixture.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check backend tests --fix && uv run ruff format backend tests
git add backend/app/queueing backend/app/db/models.py tests/fixtures/queue_entry_columns.json tests/test_queue_model.py tests/test_queue_schemas.py
git commit -m "feat(queue): map queue_entry and add the queue constants"
```

---

### Task 2: The pure ordering rules

**Files:**
- Create: `backend/app/queueing/order.py`
- Test: `tests/test_queue_order.py`

**Interfaces:**
- Produces (`order.py`): `next_token(tokens: Iterable[int]) -> int`; `position(waiting_tokens: Sequence[int], token: int) -> int`; `wait_minutes(ahead: int, in_room: bool, minutes: int) -> int`; `moved_up(order: Sequence[K], who: K) -> list[K]`; `made_next(order: Sequence[K], who: K) -> list[K]`; `reassign(tokens: Sequence[int], new_order: Sequence[K]) -> dict[K, int]` (`K` is any hashable id).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_queue_order.py`:

```python
"""P6: the pure queue-ordering rules. No database, no clock."""

from __future__ import annotations

import pytest
from app.queueing import order


def test_the_first_token_is_one():
    assert order.next_token([]) == 1


def test_the_next_token_is_one_past_the_highest_ever_used():
    assert order.next_token([1, 2, 5]) == 6
    assert order.next_token(iter([3])) == 4  # any iterable


def test_position_is_the_number_ahead_plus_one():
    assert order.position([4, 7, 9], 4) == 1
    assert order.position([4, 7, 9], 7) == 2
    assert order.position([4, 7, 9], 9) == 3


def test_position_ignores_the_order_the_tokens_are_listed_in():
    assert order.position([9, 4, 7], 7) == 2


@pytest.mark.parametrize(
    ("ahead", "in_room", "minutes", "expected"),
    [
        (0, False, 15, 0),  # you're next, nobody in the room
        (0, True, 15, 15),  # next, but someone is being seen
        (2, False, 15, 30),
        (2, True, 15, 45),
        (3, True, 30, 120),
    ],
)
def test_the_wait_is_people_ahead_plus_the_room_times_minutes(ahead, in_room, minutes, expected):
    assert order.wait_minutes(ahead, in_room, minutes) == expected


def test_move_up_swaps_with_the_one_ahead():
    assert order.moved_up(["a", "b", "c"], "c") == ["a", "c", "b"]
    assert order.moved_up(["a", "b", "c"], "b") == ["b", "a", "c"]


def test_move_up_of_the_first_changes_nothing():
    assert order.moved_up(["a", "b", "c"], "a") == ["a", "b", "c"]


def test_make_next_puts_them_first_and_keeps_the_rest_in_order():
    assert order.made_next(["a", "b", "c", "d"], "c") == ["c", "a", "b", "d"]
    assert order.made_next(["a", "b"], "a") == ["a", "b"]


def test_inputs_are_not_changed():
    original = ["a", "b", "c"]
    order.moved_up(original, "c")
    order.made_next(original, "c")
    assert original == ["a", "b", "c"]


def test_reassign_gives_the_sorted_tokens_to_the_new_order():
    # a had 2, b had 5, c had 9; c is made next
    new = order.reassign([2, 5, 9], ["c", "a", "b"])
    assert new == {"c": 2, "a": 5, "b": 9}


def test_reassign_keeps_the_set_of_tokens_unique():
    new = order.reassign([7, 3, 5], ["x", "y", "z"])
    assert sorted(new.values()) == [3, 5, 7] and len(set(new.values())) == 3


def test_reassign_needs_one_token_per_person():
    with pytest.raises(ValueError):
        order.reassign([1, 2], ["a", "b", "c"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_queue_order.py -q`
Expected: collection error, `ImportError: cannot import name 'order'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/queueing/order.py`:

```python
"""Pure queue-ordering rules (spec 3.2): no database, no clock.

The queue runs in arrival order, which is token order: a check-in or a walk-in
takes the next unused token of its session. A patient's token is internal; the
screens show their position. Moving someone forward only permutes the token
numbers the waiting patients already hold, so tokens stay unique.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable, Sequence
from typing import TypeVar

K = TypeVar("K", bound=Hashable)


def next_token(tokens: Iterable[int]) -> int:
    """One past the highest token ever used in the session (a seen patient's
    token is never reused)."""
    return max(tokens, default=0) + 1


def position(waiting_tokens: Sequence[int], token: int) -> int:
    """Place in line: the waiting patients with a smaller token, plus one."""
    return sum(1 for other in waiting_tokens if other < token) + 1


def wait_minutes(ahead: int, in_room: bool, minutes: int) -> int:
    """The estimate: everyone ahead takes a turn, and so does the patient in the room."""
    return (ahead + (1 if in_room else 0)) * minutes


def moved_up(order: Sequence[K], who: K) -> list[K]:
    """`who` swaps places with the person directly ahead (the first stays first)."""
    items = list(order)
    index = items.index(who)
    if index > 0:
        items[index - 1], items[index] = items[index], items[index - 1]
    return items


def made_next(order: Sequence[K], who: K) -> list[K]:
    """`who` goes to the front; everyone else keeps their relative order."""
    return [who, *(item for item in order if item != who)]


def reassign(tokens: Sequence[int], new_order: Sequence[K]) -> dict[K, int]:
    """Give the sorted `tokens` to `new_order`, one each, smallest first."""
    if len(tokens) != len(new_order):
        raise ValueError("one token per person")
    return dict(zip(new_order, sorted(tokens), strict=True))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_queue_order.py -q`
Expected: all pass (about 17).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check backend tests --fix && uv run ruff format backend tests
git add backend/app/queueing/order.py tests/test_queue_order.py
git commit -m "feat(queue): add the pure queue-ordering rules"
```

---

### Task 3: Plumbing: the refusal error, rate limits, audit constants, error messages

**Files:**
- Modify: `backend/app/errors.py` (after `BookingRefused`), `backend/app/settings.py`, `backend/app/deps.py`, `backend/app/audit/writer.py`, `backend/app/i18n/messages/en.json`, `ur.json`
- Test: `tests/test_queue_plumbing.py`

**Interfaces:**
- Consumes: `schemas.REFUSAL_REASONS` (Task 1).
- Produces: `QueueRefused(reason: str)` (409; `message_key = f"errors.queue_{reason}"`; `.reason`); `settings.queue_write_rate_limit_per_minute` (default 120) and `settings.queue_lookup_rate_limit_per_minute` (default 30); `check_queue_write_rate(actor)` and `check_queue_lookup_rate(actor)` (raise `RateLimited(retry_after_s=60)`); `audit.QUEUE_CHECK_IN`, `QUEUE_SEND_IN`, `QUEUE_SEEN`, `QUEUE_PUT_BACK`, `QUEUE_MOVE`, `QUEUE_WALK_IN`, `QUEUE_FIND_PATIENT`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_queue_plumbing.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_queue_plumbing.py -q`
Expected: collection error, `ImportError: cannot import name 'check_queue_lookup_rate'`.

- [ ] **Step 3: Write the code**

In `backend/app/errors.py`, directly after the `BookingRefused` class:

```python
class QueueRefused(AppError):
    """A queue action the rules refuse (P6, spec 3.2): the appointment is not for
    today or cannot be checked in, the session has ended, the doctor is not
    available, someone is already in the room, the session is full, the patient
    already has a booking today or is already queued, the patient is no longer
    waiting or not in the room, the search is too short, or a walk-in's time was
    just taken. `reason` picks the message (`errors.queue_<reason>`). A 409: the
    page is shown again with the reason."""

    code = "QUEUE_REFUSED"
    http_status = status.HTTP_409_CONFLICT
    message_key = "errors.queue_not_today"
    retryable = False

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.message_key = f"errors.queue_{reason}"
        super().__init__({"reason": reason})
```

In `backend/app/settings.py`, directly after `booking_write_rate_limit_per_minute: int = 20`:

```python

    # ── Live queue (P6) ──────────────────────────────────────────────────
    # The front desk acts often, so the write budget is generous; the walk-in
    # lookup is tighter because it is the only way to browse patient names.
    queue_write_rate_limit_per_minute: int = 120
    queue_lookup_rate_limit_per_minute: int = 30
```

In `backend/app/deps.py`, directly after `check_booking_write_rate`:

```python


# ── Live-queue budgets (P6) ──────────────────────────────────────────────
async def check_queue_write_rate(actor: Actor) -> None:
    """Counts one front-desk queue action (check in, send in, seen, move, walk-in)."""
    count = await cache.incr(ratelimit_key("queue_write", str(actor.user_id)), 60)
    if count > settings.queue_write_rate_limit_per_minute:
        raise RateLimited(retry_after_s=60)


async def check_queue_lookup_rate(actor: Actor) -> None:
    """Counts one walk-in patient search. Tighter than the write budget on purpose."""
    count = await cache.incr(ratelimit_key("queue_lookup", str(actor.user_id)), 60)
    if count > settings.queue_lookup_rate_limit_per_minute:
        raise RateLimited(retry_after_s=60)
```

In `backend/app/audit/writer.py`, directly after `APPOINTMENT_RESCHEDULE = "appointment.reschedule"`:

```python

# Emitted by the live queue (P6). `detail` holds ids only: never a name, a passport
# number or the text typed into a search; a lookup logs only how many results it found.
QUEUE_CHECK_IN = "queue.check_in"
QUEUE_SEND_IN = "queue.send_in"
QUEUE_SEEN = "queue.seen"
QUEUE_PUT_BACK = "queue.put_back"
QUEUE_MOVE = "queue.move"
QUEUE_WALK_IN = "queue.walk_in"
QUEUE_FIND_PATIENT = "queue.find_patient"
```

Add the twelve refusal messages with the i18n helper, using this keys file:

```json
{
  "en": {
    "errors.queue_not_today": "That appointment isn't for today.",
    "errors.queue_not_booked": "That appointment can't be checked in.",
    "errors.queue_session_over": "That session has ended.",
    "errors.queue_doctor_unavailable": "That doctor is not available.",
    "errors.queue_room_occupied": "Someone is already in the room. Mark them seen first.",
    "errors.queue_session_full": "That session is full.",
    "errors.queue_has_booking_today": "This patient has an appointment with this doctor today. Check them in instead.",
    "errors.queue_already_queued": "This patient is already in a queue.",
    "errors.queue_not_waiting": "That patient is no longer waiting.",
    "errors.queue_not_in_room": "That patient is not in the room.",
    "errors.queue_search_too_short": "Type at least 3 characters.",
    "errors.queue_try_again": "That time was just taken. Please try again."
  },
  "ur": {
    "errors.queue_not_today": "یہ اپائنٹمنٹ آج کی نہیں ہے۔",
    "errors.queue_not_booked": "اس اپائنٹمنٹ کا چیک اِن نہیں ہو سکتا۔",
    "errors.queue_session_over": "یہ سیشن ختم ہو چکا ہے۔",
    "errors.queue_doctor_unavailable": "یہ ڈاکٹر دستیاب نہیں ہیں۔",
    "errors.queue_room_occupied": "کمرے میں پہلے سے کوئی مریض ہے۔ پہلے انہیں دیکھا ہوا نشان زد کریں۔",
    "errors.queue_session_full": "یہ سیشن بھر چکا ہے۔",
    "errors.queue_has_booking_today": "اس مریض کی آج اس ڈاکٹر کے ساتھ اپائنٹمنٹ ہے۔ اس کے بجائے انہیں چیک اِن کریں۔",
    "errors.queue_already_queued": "یہ مریض پہلے سے قطار میں ہیں۔",
    "errors.queue_not_waiting": "یہ مریض اب انتظار میں نہیں ہیں۔",
    "errors.queue_not_in_room": "یہ مریض کمرے میں نہیں ہیں۔",
    "errors.queue_search_too_short": "کم از کم 3 حروف لکھیں۔",
    "errors.queue_try_again": "یہ وقت ابھی لیا جا چکا ہے۔ براہ کرم دوبارہ کوشش کریں۔"
  }
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_queue_plumbing.py -q`
Expected: all pass (12 parametrized + 6).

- [ ] **Step 5: Run the full suite, lint, commit**

```bash
uv run pytest -q
uv run ruff check backend tests --fix && uv run ruff format backend tests
git add backend/app/errors.py backend/app/settings.py backend/app/deps.py backend/app/audit/writer.py backend/app/i18n tests/test_queue_plumbing.py
git commit -m "feat(queue): add the refusal error, desk rate limits and audit actions"
```

---

### Task 4: Service: the views (admin, doctor, patient)

**Files:**
- Modify: `backend/app/booking/service.py` (append five small public wrappers)
- Create: `backend/app/queueing/service.py`, `tests/queue_helpers.py`
- Test: `tests/test_booking_public_helpers.py`, `tests/test_queue_views.py`

**Interfaces:**
- Consumes: `schemas.*` (Task 1), `order.*` (Task 2), `booking.slots`, D7's `get_effective_availability` through the new wrappers.
- Produces (`booking/service.py`): `now() -> datetime` (the booking clock); `async session_windows(session, doctor_id, clinic_id, day) -> list[slots.Window]`; `async is_bookable(session, doctor_id, clinic_id) -> bool`; `async visible_pairs(session) -> list[tuple[Doctor, Clinic]]`; `async active_starts(session, schedule_id, lo, hi) -> list[datetime]`.
- Produces (`queueing/service.py`): `_staff(actor)`, `_day_bounds(day)`, `_session_entries(session, schedule_id, lo, hi)`, `_window_of(session, appointment)`, `_session_range(session, appointment)`, `_peers(session, appointment)`; `async admin_sessions(session, actor) -> list[SessionView]`; `async doctor_queue(session, actor) -> list[SessionView]`; `async patient_status(session, actor, appointment_id) -> QueueStatus | None` (`NotFound` unless the caller's own); `async patient_badges(session, actor) -> dict[UUID, QueueStatus]`. Tasks 5 to 9 reuse the private helpers.
- Produces (`tests/queue_helpers.py`): `make_admin(db, clinic, *, email) -> (Profile, Actor)`; `todays_session(db, clinic, *, cap, minutes, email, name) -> (Doctor, DoctorSchedule)` (Monday 12:00 to 18:00; the frozen "now" is Monday 13:00); `booked_today(db, clinic, doctor, schedule, patient, hhmm="14:00", status="booked") -> Appointment`; `add_entry(db, appointment, token, state="waiting") -> QueueEntry`; `entry_of(db, appointment) -> QueueEntry | None`.

- [ ] **Step 1: Write the test helpers and the failing tests**

Create `tests/queue_helpers.py`:

```python
"""Shared setup for the queue tests (P6): an admin, today's session, queue entries.
Time comes from `tests.booking_helpers.freeze`: "now" is Monday 13:00 in Pakistan."""

from __future__ import annotations

from app.db.models import Appointment, Profile, QueueEntry, UserRole
from app.deps import Actor
from sqlalchemy import select

from tests.booking_helpers import MONDAY, TODAY, add_appointment, add_session, at, verified_doctor
from tests.conftest import make_user


async def make_admin(db, clinic, *, email="queue.admin@x.com") -> tuple[Profile, Actor]:
    user = await make_user(db, email=email, role=UserRole.CLINIC_ADMIN, clinic_id=clinic.id)
    actor = Actor(
        user_id=user.id,
        role=UserRole.CLINIC_ADMIN.value,
        clinic_ids=frozenset({clinic.id}),
    )
    return user, actor


async def todays_session(db, clinic, *, cap=16, minutes=15, email="queue.dr@x.com", name="Dr Queue"):
    """A verified doctor with a Monday 12:00-18:00 session (today, when time is frozen)."""
    doctor = await verified_doctor(db, clinic, email=email, name=name)
    schedule = await add_session(
        db, doctor, clinic, weekday=MONDAY, start="12:00", end="18:00", minutes=minutes, cap=cap
    )
    return doctor, schedule


async def booked_today(
    db, clinic, doctor, schedule, patient, hhmm="14:00", status="booked"
) -> Appointment:
    return await add_appointment(
        db, patient, doctor, clinic, schedule, at(TODAY, hhmm), status=status
    )


async def add_entry(db, appointment, token, state="waiting") -> QueueEntry:
    """A queue entry for `appointment`, with the appointment status that goes with its state."""
    entry = QueueEntry(appointment_id=appointment.id, token_number=token, state=state)
    appointment.status = "done" if state == "served" else "checked_in"
    db.add(entry)
    await db.commit()
    return entry


async def entry_of(db, appointment) -> QueueEntry | None:
    return (
        await db.execute(
            select(QueueEntry)
            .where(QueueEntry.appointment_id == appointment.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
```

Create `tests/test_booking_public_helpers.py`:

```python
"""P6: the small public wrappers the queue uses from the booking service."""

from __future__ import annotations

import pytest
from app.booking import service
from app.db.models import UserRole, VerificationStatus

from tests.booking_helpers import (
    NOW,
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
from tests.queue_helpers import todays_session


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    freeze(monkeypatch)


def test_now_follows_the_frozen_booking_clock():
    assert service.now() == NOW


async def test_is_bookable_only_for_a_verified_doctor_at_a_live_hospital(db, clinic, second_clinic):
    doctor = await verified_doctor(db, clinic)
    assert await service.is_bookable(db, doctor.id, clinic.id) is True
    assert await service.is_bookable(db, doctor.id, second_clinic.id) is False
    doctor.verification_status = VerificationStatus.REJECTED
    doctor.verified_by = None
    doctor.verified_at = None
    await db.commit()
    assert await service.is_bookable(db, doctor.id, clinic.id) is False


async def test_a_pending_doctor_is_not_bookable(db, clinic):
    await make_user(db, email="p@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    assert await service.visible_pairs(db) == []


async def test_visible_pairs_lists_each_live_hospital_by_name(db, clinic, second_clinic):
    doctor = await verified_doctor(db, clinic)
    await add_affiliation(db, doctor.id, second_clinic.id)
    pairs = await service.visible_pairs(db)
    assert [(d.id, c.name) for d, c in pairs] == [
        (doctor.id, "Maroof International"),
        (doctor.id, "Shifa International"),
    ]


async def test_session_windows_returns_the_days_windows(db, clinic):
    doctor, schedule = await todays_session(db, clinic)
    [window] = await service.session_windows(db, doctor.id, clinic.id, TODAY)
    assert window.schedule_id == schedule.id and window.slot_minutes == 15
    assert (window.start.hour, window.end.hour) == (12, 18)


async def test_active_starts_leaves_out_cancelled(db, clinic):
    doctor, schedule = await todays_session(db, clinic)
    _, patient, _ = await make_patient(db)
    live = await add_appointment(db, patient, doctor, clinic, schedule, at(TODAY, "14:00"))
    await add_appointment(db, patient, doctor, clinic, schedule, at(TODAY, "14:15"), status="cancelled")
    lo, hi = at(TODAY, "12:00"), at(TODAY, "18:00")
    starts = await service.active_starts(db, schedule.id, lo, hi)
    assert starts == [at(TODAY, "14:00")] and live.status == "booked"
    assert await service.active_starts(db, schedule.id, at(WED, "12:00"), at(WED, "18:00")) == []
```

Create `tests/test_queue_views.py`:

```python
"""P6: what the admin, the doctor and the patient see of a queue."""

from __future__ import annotations

import uuid
from datetime import date, time

import pytest
from app.deps import Actor
from app.errors import NotFound
from app.queueing import service
from app.queueing.schemas import SessionKey

from tests.booking_helpers import (
    add_appointment,
    add_session,
    at,
    freeze,
    make_patient,
    verified_doctor,
)
from tests.queue_helpers import add_entry, booked_today, make_admin, todays_session


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    freeze(monkeypatch)


async def person(db, n: int):
    """(patient row, actor) for patient number n."""
    _, patient, actor = await make_patient(db, email=f"v{n}@x.com", name=f"Patient {n}")
    return patient, actor


async def world(db, clinic):
    doctor, schedule = await todays_session(db, clinic)
    _, admin = await make_admin(db, clinic)
    return doctor, schedule, admin


def doctor_actor(doctor) -> Actor:
    return Actor(user_id=doctor.user_id, role="doctor", is_verified_doctor=True)


# ── The admin's view ─────────────────────────────────────────────────────
async def test_the_admin_sees_each_patient_in_the_right_group(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    (p1, _), (p2, _), (p3, _), (p4, _) = [await person(db, n) for n in (1, 2, 3, 4)]
    await booked_today(db, clinic, doctor, schedule, p1, "14:00")  # expected
    a2 = await booked_today(db, clinic, doctor, schedule, p2, "14:15")
    await add_entry(db, a2, 1, "waiting")
    a3 = await booked_today(db, clinic, doctor, schedule, p3, "14:30")
    await add_entry(db, a3, 2, "in_room")
    a4 = await booked_today(db, clinic, doctor, schedule, p4, "14:45")
    await add_entry(db, a4, 3, "served")
    [view] = await service.admin_sessions(db, admin)
    assert (view.doctor_name, view.clinic_name) == ("Dr Queue", "Shifa International")
    assert (view.start, view.end, view.slot_minutes, view.cap, view.used) == (
        time(12, 0),
        time(18, 0),
        15,
        16,
        4,
    )
    assert view.key == SessionKey(doctor.id, clinic.id, schedule.id, time(12, 0))
    assert [line.patient_name for line in view.expected] == ["Patient 1"]
    assert [line.patient_name for line in view.waiting] == ["Patient 2"]
    assert view.in_room is not None and view.in_room.patient_name == "Patient 3"
    assert [line.patient_name for line in view.seen] == ["Patient 4"]


async def test_the_waiting_list_follows_tokens_not_booked_times(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    (late, _), (early, _) = await person(db, 1), await person(db, 2)
    a_late = await booked_today(db, clinic, doctor, schedule, late, "15:00")
    a_early = await booked_today(db, clinic, doctor, schedule, early, "14:00")
    await add_entry(db, a_late, 1)  # arrived first, though booked later
    await add_entry(db, a_early, 2)
    [view] = await service.admin_sessions(db, admin)
    assert [line.patient_name for line in view.waiting] == ["Patient 1", "Patient 2"]
    assert [line.token for line in view.waiting] == [1, 2]


async def test_a_walk_in_is_flagged_and_counts_toward_the_places_used(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    (booked, _), (walker, _) = await person(db, 1), await person(db, 2)
    await booked_today(db, clinic, doctor, schedule, booked, "14:00")
    arrival = await booked_today(db, clinic, doctor, schedule, walker, "13:07")
    arrival.booking_channel = "walk_in"
    await db.commit()
    await add_entry(db, arrival, 1)
    [view] = await service.admin_sessions(db, admin)
    assert view.used == 2
    assert [(line.patient_name, line.walk_in) for line in view.waiting] == [("Patient 2", True)]
    assert [line.walk_in for line in view.expected] == [False]


async def test_cancelled_appointments_and_other_days_are_not_listed(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    (p1, _), (p2, _), (p3, _) = [await person(db, n) for n in (1, 2, 3)]
    await booked_today(db, clinic, doctor, schedule, p1, "14:00", status="cancelled")
    await add_appointment(
        db, p2, doctor, clinic, schedule, at(date(2026, 10, 19), "14:00")
    )  # next Monday
    await booked_today(db, clinic, doctor, schedule, p3, "14:15")
    [view] = await service.admin_sessions(db, admin)
    assert [line.patient_name for line in view.expected] == ["Patient 3"] and view.used == 1


async def test_the_admin_sees_only_their_own_clinics(db, clinic, second_clinic):
    await todays_session(db, clinic)
    await todays_session(db, second_clinic, email="other.dr@x.com", name="Dr Other")
    _, admin = await make_admin(db, clinic)
    views = await service.admin_sessions(db, admin)
    assert [(v.doctor_name, v.clinic_name) for v in views] == [("Dr Queue", "Shifa International")]


async def test_a_doctor_with_no_session_today_has_no_panel(db, clinic):
    _, _, admin = await world(db, clinic)
    quiet = await verified_doctor(db, clinic, email="quiet.dr@x.com", name="Dr Quiet")
    await add_session(db, quiet, clinic, weekday=2)  # Wednesdays only
    assert [v.doctor_name for v in await service.admin_sessions(db, admin)] == ["Dr Queue"]


async def test_only_an_admin_with_a_clinic_can_see_the_admin_view(db, clinic):
    await world(db, clinic)
    with pytest.raises(NotFound):
        await service.admin_sessions(db, Actor(user_id=uuid.uuid4(), role="patient"))
    with pytest.raises(NotFound):
        await service.admin_sessions(db, Actor(user_id=uuid.uuid4(), role="admin"))  # no clinics


# ── The doctor's view ────────────────────────────────────────────────────
async def test_the_doctor_sees_only_their_own_queue(db, clinic):
    doctor, schedule, _ = await world(db, clinic)
    rival, rival_schedule = await todays_session(db, clinic, email="r.dr@x.com", name="Dr Rival")
    (mine, _), (theirs, _) = await person(db, 1), await person(db, 2)
    a = await booked_today(db, clinic, doctor, schedule, mine, "14:00")
    await add_entry(db, a, 1)
    await booked_today(db, clinic, rival, rival_schedule, theirs, "14:15")
    [view] = await service.doctor_queue(db, doctor_actor(doctor))
    assert view.doctor_name == "Dr Queue"
    assert [line.patient_name for line in view.waiting] == ["Patient 1"]
    assert view.expected == ()


async def test_only_a_verified_doctor_can_see_a_doctors_queue(db, clinic):
    doctor, _, admin = await world(db, clinic)
    with pytest.raises(NotFound):
        await service.doctor_queue(db, admin)
    with pytest.raises(NotFound):
        await service.doctor_queue(
            db, Actor(user_id=doctor.user_id, role="doctor", is_verified_doctor=False)
        )


# ── The patient's view ───────────────────────────────────────────────────
async def test_a_booked_patient_who_has_not_arrived_is_expected(db, clinic):
    doctor, schedule, _ = await world(db, clinic)
    patient, actor = await person(db, 1)
    appointment = await booked_today(db, clinic, doctor, schedule, patient)
    status = await service.patient_status(db, actor, appointment.id)
    assert (status.state, status.position, status.wait_minutes, status.live) == (
        "expected",
        None,
        None,
        True,
    )


async def test_the_first_in_line_with_an_empty_room_is_next_with_no_wait(db, clinic):
    doctor, schedule, _ = await world(db, clinic)
    patient, actor = await person(db, 1)
    appointment = await booked_today(db, clinic, doctor, schedule, patient)
    await add_entry(db, appointment, 1)
    status = await service.patient_status(db, actor, appointment.id)
    assert (status.state, status.position, status.wait_minutes) == ("next", 1, 0)


async def test_position_and_wait_count_those_ahead_and_the_room(db, clinic):
    doctor, schedule, _ = await world(db, clinic)
    people = [await person(db, n) for n in (1, 2, 3, 4)]
    appointments = [
        await booked_today(db, clinic, doctor, schedule, p, f"14:{n * 15:02d}")
        for n, (p, _) in enumerate(people)
    ]
    await add_entry(db, appointments[0], 1, "in_room")
    await add_entry(db, appointments[1], 2)
    await add_entry(db, appointments[2], 3)
    await add_entry(db, appointments[3], 4)
    status = await service.patient_status(db, people[3][1], appointments[3].id)
    # two waiting ahead plus the one in the room, 15 minutes each
    assert (status.state, status.position, status.wait_minutes, status.waiting_total) == (
        "waiting",
        3,
        45,
        3,
    )


async def test_in_the_room_and_seen(db, clinic):
    doctor, schedule, _ = await world(db, clinic)
    (p1, actor1), (p2, actor2) = await person(db, 1), await person(db, 2)
    a1 = await booked_today(db, clinic, doctor, schedule, p1, "14:00")
    a2 = await booked_today(db, clinic, doctor, schedule, p2, "14:15")
    await add_entry(db, a1, 1, "in_room")
    await add_entry(db, a2, 2, "served")
    inside = await service.patient_status(db, actor1, a1.id)
    done = await service.patient_status(db, actor2, a2.id)
    assert (inside.state, inside.live) == ("in_room", True)
    assert (done.state, done.live) == ("served", False)


async def test_no_status_for_cancelled_or_other_days(db, clinic):
    doctor, schedule, _ = await world(db, clinic)
    patient, actor = await person(db, 1)
    cancelled = await booked_today(db, clinic, doctor, schedule, patient, "14:00", status="cancelled")
    nextweek = await add_appointment(
        db, patient, doctor, clinic, schedule, at(date(2026, 10, 19), "14:00")
    )
    assert await service.patient_status(db, actor, cancelled.id) is None
    assert await service.patient_status(db, actor, nextweek.id) is None


async def test_someone_elses_or_unknown_appointment_is_not_found(db, clinic):
    doctor, schedule, _ = await world(db, clinic)
    (p1, _), (_, actor2) = await person(db, 1), await person(db, 2)
    appointment = await booked_today(db, clinic, doctor, schedule, p1)
    with pytest.raises(NotFound):
        await service.patient_status(db, actor2, appointment.id)
    with pytest.raises(NotFound):
        await service.patient_status(db, actor2, uuid.uuid4())


async def test_only_a_patient_has_a_status(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    patient, _ = await person(db, 1)
    appointment = await booked_today(db, clinic, doctor, schedule, patient)
    with pytest.raises(NotFound):
        await service.patient_status(db, admin, appointment.id)


async def test_badges_cover_only_todays_live_appointments(db, clinic):
    doctor, schedule, _ = await world(db, clinic)
    patient, actor = await person(db, 1)
    a1 = await booked_today(db, clinic, doctor, schedule, patient, "14:00")
    await add_entry(db, a1, 1)
    await add_appointment(db, patient, doctor, clinic, schedule, at(date(2026, 10, 19), "14:00"))
    await booked_today(db, clinic, doctor, schedule, patient, "14:15", status="cancelled")
    badges = await service.patient_badges(db, actor)
    assert list(badges) == [a1.id] and badges[a1.id].state == "next"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_booking_public_helpers.py tests/test_queue_views.py -q`
Expected: failures: `AttributeError: module 'app.booking.service' has no attribute 'now'` and `ModuleNotFoundError: No module named 'app.queueing.service'`.

- [ ] **Step 3: Write the implementation**

Append to `backend/app/booking/service.py` (after the last function, `doctor_day`):

```python


# ── Small public wrappers for the live queue (P6) ────────────────────────
def now() -> datetime:
    """The current instant, UTC. This is the booking clock: freezing `_now` freezes it."""
    return _now()


async def session_windows(
    session: AsyncSession, doctor_id: uuid.UUID, clinic_id: uuid.UUID, day: date
) -> list[slots.Window]:
    """The doctor's session windows at one hospital on one day (D7 availability).
    `day` must be today or later: D7 refuses a past day."""
    return await _windows(session, doctor_id, clinic_id, day, day)


async def is_bookable(session: AsyncSession, doctor_id: uuid.UUID, clinic_id: uuid.UUID) -> bool:
    """True for a verified doctor at one of their live hospitals."""
    found = await _visible_doctors(session, doctor_id)
    return doctor_id in found and clinic_id in found[doctor_id][1]


async def visible_pairs(session: AsyncSession) -> list[tuple[Doctor, Clinic]]:
    """Every verified doctor with each of their live hospitals, as (doctor, hospital)
    pairs, by doctor name then hospital name."""
    return [
        (doctor, clinic)
        for doctor, clinics in (await _visible_doctors(session)).values()
        for clinic in sorted(clinics.values(), key=lambda c: c.name)
    ]


async def active_starts(
    session: AsyncSession, schedule_id: uuid.UUID, lo: datetime, hi: datetime
) -> list[datetime]:
    """The starts (aware UTC) of the active appointments of one schedule in `[lo, hi)`."""
    return (await _active_starts(session, {schedule_id}, lo, hi))[schedule_id]
```

Create `backend/app/queueing/service.py`:

```python
"""Live queue and check-in business logic (P6; FR17, FR19, FR24; spec 3.2).

A queue is one doctor session: one effective window on one date at one clinic,
the same unit booking uses for the cap. A queue entry belongs to a session
through its appointment (`schedule_id` and a `slot_start` inside the window).
Every action takes the doctor lock (`schedule_service.lock_doctor`) first, then
re-reads the queue, then writes; the database enforces neither unique tokens nor
the cap, so this lock is the guard.

Scoping: the app connects with BYPASSRLS, so every query is filtered by the
caller's own clinics (an admin) or own id (a doctor, a patient). A foreign or
unknown id is one identical `NotFound`.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..booking import service as booking_service
from ..booking import slots
from ..booking.schemas import ACTIVE_STATUSES, DEFAULT_MINUTES
from ..db.models import Appointment, Clinic, Doctor, Patient, QueueEntry, UserRole
from ..deps import Actor
from ..errors import NotFound
from . import order
from .schemas import (
    CHANNEL_WALK_IN,
    STATE_IN_ROOM,
    STATE_SERVED,
    STATE_WAITING,
    Line,
    QueueStatus,
    SessionKey,
    SessionView,
)


# ── Scope, time and sessions ─────────────────────────────────────────────
def _staff(actor: Actor) -> frozenset[uuid.UUID]:
    """The admin's own clinics. Anyone else, or an admin with none, is `NotFound`."""
    if actor.role != UserRole.CLINIC_ADMIN.value or not actor.clinic_ids:
        raise NotFound()
    return actor.clinic_ids


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    """The aware UTC start of a Pakistan day and of the next one."""
    lo = slots.to_utc(day, time(0, 0))
    return lo, lo + timedelta(days=1)


async def _session_entries(
    session: AsyncSession, schedule_id: uuid.UUID, lo: datetime, hi: datetime
) -> list[tuple[QueueEntry, Appointment]]:
    """The queue entries of one session: those whose appointment hangs off
    `schedule_id` and starts in `[lo, hi)`. Read fresh, never from the identity map."""
    rows = await session.execute(
        select(QueueEntry, Appointment)
        .join(Appointment, Appointment.id == QueueEntry.appointment_id)
        .where(
            Appointment.schedule_id == schedule_id,
            Appointment.slot_start >= lo,
            Appointment.slot_start < hi,
        )
        .execution_options(populate_existing=True)
    )
    return [(entry, appointment) for entry, appointment in rows.all()]


async def _window_of(session: AsyncSession, appointment: Appointment) -> slots.Window | None:
    """The session window an appointment belongs to. Only for today's appointments:
    D7 refuses to report a past day."""
    if appointment.schedule_id is None:
        return None
    start = slots.as_utc(appointment.slot_start)
    day = slots.to_local(start).date()
    windows = await booking_service.session_windows(
        session, appointment.doctor_id, appointment.clinic_id, day
    )
    return next(
        (w for w in windows if w.schedule_id == appointment.schedule_id and slots.in_window(w, start)),
        None,
    )


async def _session_range(
    session: AsyncSession, appointment: Appointment
) -> tuple[datetime, datetime]:
    """The time range of the appointment's session, or its whole day if the
    session no longer exists."""
    window = await _window_of(session, appointment)
    if window is not None:
        return slots.window_bounds(window)
    return _day_bounds(slots.to_local(appointment.slot_start).date())


async def _peers(
    session: AsyncSession, appointment: Appointment
) -> list[tuple[QueueEntry, Appointment]]:
    """Every queue entry of the appointment's session (itself included)."""
    if appointment.schedule_id is None:
        return []
    lo, hi = await _session_range(session, appointment)
    return await _session_entries(session, appointment.schedule_id, lo, hi)


# ── The admin's and the doctor's views ───────────────────────────────────
def _line(appointment: Appointment, name: str, entry: QueueEntry | None) -> Line:
    return Line(
        appointment_id=appointment.id,
        entry_id=entry.id if entry is not None else None,
        patient_name=name,
        state=entry.state if entry is not None else "expected",
        token=entry.token_number if entry is not None else None,
        booked_at=slots.to_local(appointment.slot_start),
        walk_in=appointment.booking_channel == CHANNEL_WALK_IN,
    )


async def _views(session: AsyncSession, pairs: list[tuple[Doctor, Clinic]]) -> list[SessionView]:
    """One `SessionView` per session today of each (doctor, hospital) pair."""
    day = booking_service.current_day()
    day_lo, day_hi = _day_bounds(day)
    views: list[SessionView] = []
    for doctor, clinic in pairs:
        windows = await booking_service.session_windows(session, doctor.id, clinic.id, day)
        if not windows:
            continue
        rows = (
            await session.execute(
                select(Appointment, Patient.full_name, QueueEntry)
                .join(Patient, Patient.id == Appointment.patient_id)
                .join(QueueEntry, QueueEntry.appointment_id == Appointment.id, isouter=True)
                .where(
                    Appointment.doctor_id == doctor.id,
                    Appointment.clinic_id == clinic.id,
                    Appointment.slot_start >= day_lo,
                    Appointment.slot_start < day_hi,
                    Appointment.status.in_(ACTIVE_STATUSES),
                )
                .order_by(Appointment.slot_start)
                .execution_options(populate_existing=True)
            )
        ).all()
        for window in windows:
            lo, hi = slots.window_bounds(window)
            mine = [
                _line(appointment, name, entry)
                for appointment, name, entry in rows
                if appointment.schedule_id == window.schedule_id
                and lo <= slots.as_utc(appointment.slot_start) < hi
            ]
            views.append(
                SessionView(
                    key=SessionKey(doctor.id, clinic.id, window.schedule_id, window.start),
                    doctor_name=doctor.full_name,
                    clinic_name=clinic.name,
                    day=day,
                    start=window.start,
                    end=window.end,
                    slot_minutes=window.slot_minutes,
                    cap=window.max_tokens,
                    used=len(mine),
                    in_room=next((ln for ln in mine if ln.state == STATE_IN_ROOM), None),
                    waiting=tuple(
                        sorted((ln for ln in mine if ln.state == STATE_WAITING), key=_token)
                    ),
                    expected=tuple(
                        sorted((ln for ln in mine if ln.state == "expected"), key=_booked)
                    ),
                    seen=tuple(sorted((ln for ln in mine if ln.state == STATE_SERVED), key=_token)),
                )
            )
    views.sort(key=lambda v: (v.doctor_name, v.clinic_name, v.start))
    return views


def _token(line: Line) -> int:
    return line.token or 0


def _booked(line: Line) -> datetime:
    return line.booked_at


async def admin_sessions(session: AsyncSession, actor: Actor) -> list[SessionView]:
    """Today's sessions of the verified doctors at the admin's own clinics."""
    clinics = _staff(actor)
    pairs = [(d, c) for d, c in await booking_service.visible_pairs(session) if c.id in clinics]
    return await _views(session, pairs)


async def doctor_queue(session: AsyncSession, actor: Actor) -> list[SessionView]:
    """A verified doctor's own sessions today. Anyone else is `NotFound`."""
    if actor.role != UserRole.DOCTOR.value or not actor.is_verified_doctor:
        raise NotFound()
    pairs = [
        (d, c) for d, c in await booking_service.visible_pairs(session) if d.user_id == actor.user_id
    ]
    return await _views(session, pairs)


# ── The patient's view ───────────────────────────────────────────────────
async def _own_patient_id(session: AsyncSession, actor: Actor) -> uuid.UUID:
    if actor.role != UserRole.PATIENT.value:
        raise NotFound()
    patient_id = (
        await session.execute(select(Patient.id).where(Patient.user_id == actor.user_id))
    ).scalar_one_or_none()
    if patient_id is None:
        raise NotFound()
    return patient_id


async def _status(
    session: AsyncSession, appointment: Appointment, entry: QueueEntry | None
) -> QueueStatus | None:
    """Where one appointment stands today, or None when there is nothing to show
    (cancelled, another day, or not in the queue and not booked)."""
    if (
        appointment.status == "cancelled"
        or slots.to_local(appointment.slot_start).date() != booking_service.current_day()
    ):
        return None
    window = await _window_of(session, appointment)
    minutes = window.slot_minutes if window is not None else DEFAULT_MINUTES
    peers = await _peers(session, appointment)
    waiting = [e for e, _ in peers if e.state == STATE_WAITING]
    in_room = any(e.state == STATE_IN_ROOM for e, _ in peers)
    if entry is None:
        if appointment.status != "booked":
            return None
        return QueueStatus("expected", None, None, len(waiting), True)
    if entry.state == STATE_SERVED:
        return QueueStatus("served", None, None, len(waiting), False)
    if entry.state == STATE_IN_ROOM:
        return QueueStatus("in_room", None, None, len(waiting), True)
    place = order.position([e.token_number for e in waiting], entry.token_number)
    wait = order.wait_minutes(place - 1, in_room, minutes)
    return QueueStatus("next" if place == 1 else "waiting", place, wait, len(waiting), True)


async def patient_status(
    session: AsyncSession, actor: Actor, appointment_id: uuid.UUID
) -> QueueStatus | None:
    """The caller's own place for one of their appointments. Someone else's, or
    an unknown id, is `NotFound`."""
    patient_id = await _own_patient_id(session, actor)
    row = (
        await session.execute(
            select(Appointment, QueueEntry)
            .join(QueueEntry, QueueEntry.appointment_id == Appointment.id, isouter=True)
            .where(Appointment.id == appointment_id, Appointment.patient_id == patient_id)
        )
    ).first()
    if row is None:
        raise NotFound()
    return await _status(session, row[0], row[1])


async def patient_badges(session: AsyncSession, actor: Actor) -> dict[uuid.UUID, QueueStatus]:
    """The status of each of the caller's appointments today that has one."""
    patient_id = await _own_patient_id(session, actor)
    day_lo, day_hi = _day_bounds(booking_service.current_day())
    rows = (
        await session.execute(
            select(Appointment, QueueEntry)
            .join(QueueEntry, QueueEntry.appointment_id == Appointment.id, isouter=True)
            .where(
                Appointment.patient_id == patient_id,
                Appointment.slot_start >= day_lo,
                Appointment.slot_start < day_hi,
                Appointment.status.in_(ACTIVE_STATUSES),
            )
        )
    ).all()
    badges: dict[uuid.UUID, QueueStatus] = {}
    for appointment, entry in rows:
        status = await _status(session, appointment, entry)
        if status is not None:
            badges[appointment.id] = status
    return badges
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_booking_public_helpers.py tests/test_queue_views.py -q`
Expected: all pass (about 26).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check backend tests --fix && uv run ruff format backend tests
uv run pytest tests/test_booking_*.py tests/test_queue_*.py -q
git add backend/app/booking/service.py backend/app/queueing/service.py tests/queue_helpers.py tests/test_booking_public_helpers.py tests/test_queue_views.py
git commit -m "feat(queue): the admin, doctor and patient views of a queue"
```

---

### Task 5: Service: check in, send in, mark seen, put back, move

**Files:**
- Modify: `backend/app/queueing/service.py` (imports, then append)
- Test: `tests/test_queue_actions.py`

**Interfaces:**
- Consumes: Task 4's `_staff`, `_day_bounds`, `_session_entries`, `_window_of`, `_session_range`, `_peers`; `order.*`; `booking_service.is_bookable`, `.now`, `.current_day`; `schedule_service.lock_doctor`; `QueueRefused`; `audit.QUEUE_*`.
- Produces: `_new_entry(appointment_id, token) -> QueueEntry`; `_audit(...)`; `_locked_entry(session, actor, entry_id) -> (QueueEntry, Appointment)`; `async check_in(session, actor, appointment_id, *, user_agent=None) -> None`; `async send_in(session, actor, entry_id, *, user_agent=None)`; `async mark_seen(...)`; `async put_back(...)`; `async move_up(...)`; `async make_next(...)` (same signature as `send_in`). Each raises `QueueRefused(reason)` or `NotFound`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_queue_actions.py`:

```python
"""P6: check in, send in, mark seen, put back and move forward."""

from __future__ import annotations

import json
import uuid
from datetime import date

import pytest
from app.db.models import Appointment, AuditLog, VerificationStatus
from app.deps import Actor
from app.errors import NotFound, QueueRefused
from app.queueing import service
from sqlalchemy import select

from tests.booking_helpers import (
    MONDAY,
    TODAY,
    add_appointment,
    add_session,
    at,
    freeze,
    make_patient,
    verified_doctor,
)
from tests.queue_helpers import add_entry, booked_today, entry_of, make_admin, todays_session


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    freeze(monkeypatch)


async def person(db, n: int):
    _, patient, _ = await make_patient(db, email=f"a{n}@x.com", name=f"Patient {n}")
    return patient


async def world(db, clinic):
    doctor, schedule = await todays_session(db, clinic)
    _, admin = await make_admin(db, clinic)
    return doctor, schedule, admin


async def reason_of(call) -> str:
    with pytest.raises(QueueRefused) as exc:
        await call
    return exc.value.reason


async def status_of(db, appointment) -> str:
    row = (
        await db.execute(
            select(Appointment)
            .where(Appointment.id == appointment.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return row.status


async def three_waiting(db, clinic, doctor, schedule):
    """Three patients waiting with tokens 1, 2, 3."""
    appointments = []
    for n in (1, 2, 3):
        appointment = await booked_today(
            db, clinic, doctor, schedule, await person(db, n), f"14:{n * 15:02d}"
        )
        await add_entry(db, appointment, n)
        appointments.append(appointment)
    return appointments


async def tokens(db, appointments) -> list[int | None]:
    return [(await entry_of(db, a)).token_number for a in appointments]


# ── Check in ─────────────────────────────────────────────────────────────
async def test_checking_in_queues_the_patient_and_marks_the_appointment(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    appointment = await booked_today(db, clinic, doctor, schedule, await person(db, 1))
    await service.check_in(db, admin, appointment.id)
    entry = await entry_of(db, appointment)
    assert (entry.state, entry.token_number) == ("waiting", 1)
    assert await status_of(db, appointment) == "checked_in"


async def test_tokens_follow_arrival_order_and_are_never_reused(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    first = await booked_today(db, clinic, doctor, schedule, await person(db, 1), "15:00")
    second = await booked_today(db, clinic, doctor, schedule, await person(db, 2), "14:00")
    third = await booked_today(db, clinic, doctor, schedule, await person(db, 3), "14:30")
    await service.check_in(db, admin, first.id)
    await service.check_in(db, admin, second.id)
    first_entry = await entry_of(db, first)
    await service.send_in(db, admin, first_entry.id)
    await service.mark_seen(db, admin, first_entry.id)  # token 1 is now served
    await service.check_in(db, admin, third.id)
    assert await tokens(db, [first, second, third]) == [1, 2, 3]  # 3, not a reused 1


async def test_checking_in_twice_does_nothing(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    appointment = await booked_today(db, clinic, doctor, schedule, await person(db, 1))
    await service.check_in(db, admin, appointment.id)
    await service.check_in(db, admin, appointment.id)
    entries = (await db.execute(select(AuditLog).where(AuditLog.action == "queue.check_in"))).all()
    assert (await entry_of(db, appointment)).token_number == 1 and len(entries) == 1


async def test_check_in_locks_the_doctor_before_it_reads_the_queue(db, clinic, monkeypatch):
    # Review focus 1: two staff acting at once must not read the same highest token.
    doctor, schedule, admin = await world(db, clinic)
    appointment = await booked_today(db, clinic, doctor, schedule, await person(db, 1))
    calls: list[str] = []
    real_lock = service.schedule_service.lock_doctor
    real_read = service._session_entries

    async def spy_lock(session, doctor_id):
        calls.append("lock")
        await real_lock(session, doctor_id)

    async def spy_read(*args, **kwargs):
        calls.append("read")
        return await real_read(*args, **kwargs)

    monkeypatch.setattr(service.schedule_service, "lock_doctor", spy_lock)
    monkeypatch.setattr(service, "_session_entries", spy_read)
    await service.check_in(db, admin, appointment.id)
    assert calls.index("lock") < calls.index("read")


async def test_a_cancelled_or_done_appointment_cannot_be_checked_in(db, clinic):
    # Review focus 4.
    doctor, schedule, admin = await world(db, clinic)
    cancelled = await booked_today(
        db, clinic, doctor, schedule, await person(db, 1), status="cancelled"
    )
    done = await booked_today(db, clinic, doctor, schedule, await person(db, 2), status="done")
    assert await reason_of(service.check_in(db, admin, cancelled.id)) == "not_booked"
    assert await reason_of(service.check_in(db, admin, done.id)) == "not_booked"
    assert await entry_of(db, cancelled) is None


async def test_tomorrows_appointment_is_not_for_today(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    tomorrow = await add_appointment(
        db, await person(db, 1), doctor, clinic, schedule, at(date(2026, 10, 13), "14:00")
    )
    assert await reason_of(service.check_in(db, admin, tomorrow.id)) == "not_today"


async def test_an_appointment_just_after_midnight_in_pakistan_is_today(db, clinic, monkeypatch):
    # Review focus 3: 00:10 Pakistan time is the previous day in UTC.
    freeze(monkeypatch, now=at(TODAY, "00:20"))
    doctor = await verified_doctor(db, clinic, email="night.dr@x.com", name="Dr Night")
    schedule = await add_session(db, doctor, clinic, weekday=MONDAY, start="00:00", end="01:00")
    _, admin = await make_admin(db, clinic)
    appointment = await add_appointment(
        db, await person(db, 1), doctor, clinic, schedule, at(TODAY, "00:10")
    )
    await service.check_in(db, admin, appointment.id)
    assert (await entry_of(db, appointment)).state == "waiting"


async def test_check_in_is_refused_once_the_session_has_ended(db, clinic, monkeypatch):
    doctor, schedule, admin = await world(db, clinic)
    appointment = await booked_today(db, clinic, doctor, schedule, await person(db, 1))
    freeze(monkeypatch, now=at(TODAY, "18:30"))
    assert await reason_of(service.check_in(db, admin, appointment.id)) == "session_over"


async def test_early_arrival_before_the_session_starts_is_fine(db, clinic, monkeypatch):
    doctor, schedule, admin = await world(db, clinic)
    appointment = await booked_today(db, clinic, doctor, schedule, await person(db, 1))
    freeze(monkeypatch, now=at(TODAY, "11:30"))
    await service.check_in(db, admin, appointment.id)
    assert (await entry_of(db, appointment)).state == "waiting"


async def test_a_doctor_who_is_no_longer_verified_cannot_receive_check_ins(db, clinic):
    # Review focus 4.
    doctor, schedule, admin = await world(db, clinic)
    appointment = await booked_today(db, clinic, doctor, schedule, await person(db, 1))
    doctor.verification_status = VerificationStatus.REJECTED
    doctor.verified_by = None
    doctor.verified_at = None
    await db.commit()
    assert await reason_of(service.check_in(db, admin, appointment.id)) == "doctor_unavailable"


async def test_an_appointment_with_no_schedule_has_no_session(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    appointment = await booked_today(db, clinic, doctor, schedule, await person(db, 1))
    appointment.schedule_id = None
    await db.commit()
    assert await reason_of(service.check_in(db, admin, appointment.id)) == "session_over"


async def test_another_clinics_admin_and_other_roles_get_not_found(db, clinic, second_clinic):
    # Review focus 5.
    doctor, schedule, _ = await world(db, clinic)
    appointment = await booked_today(db, clinic, doctor, schedule, await person(db, 1))
    _, outsider = await make_admin(db, second_clinic, email="out.admin@x.com")
    with pytest.raises(NotFound):
        await service.check_in(db, outsider, appointment.id)
    with pytest.raises(NotFound):
        await service.check_in(db, Actor(user_id=uuid.uuid4(), role="patient"), appointment.id)
    with pytest.raises(NotFound):
        await service.check_in(db, outsider, uuid.uuid4())
    assert await entry_of(db, appointment) is None


async def test_a_check_in_writes_one_audit_row_with_ids_only(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    appointment = await booked_today(db, clinic, doctor, schedule, await person(db, 1))
    await service.check_in(db, admin, appointment.id)
    rows = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "queue.check_in")))
        .scalars()
        .all()
    )
    entry = await entry_of(db, appointment)
    assert len(rows) == 1
    assert json.loads(rows[0].detail) == {
        "appointment_id": str(appointment.id),
        "doctor_id": str(doctor.id),
        "clinic_id": str(clinic.id),
        "entry_id": str(entry.id),
    }
    assert rows[0].actor_role == "admin"


# ── Send in, mark seen, put back ─────────────────────────────────────────
async def test_send_in_moves_a_waiting_patient_into_the_room(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    [first, *_] = await three_waiting(db, clinic, doctor, schedule)
    await service.send_in(db, admin, (await entry_of(db, first)).id)
    assert (await entry_of(db, first)).state == "in_room"


async def test_any_waiting_patient_can_be_sent_in_not_only_the_first(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    [_, second, _] = await three_waiting(db, clinic, doctor, schedule)
    await service.send_in(db, admin, (await entry_of(db, second)).id)
    assert (await entry_of(db, second)).state == "in_room"


async def test_only_one_patient_can_be_in_the_room(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    [first, second, _] = await three_waiting(db, clinic, doctor, schedule)
    await service.send_in(db, admin, (await entry_of(db, first)).id)
    reason = await reason_of(service.send_in(db, admin, (await entry_of(db, second)).id))
    assert reason == "room_occupied" and (await entry_of(db, second)).state == "waiting"


async def test_a_second_click_on_the_same_patient_is_not_waiting_any_more(db, clinic):
    # Review focus 1: two staff click "send in" for the same patient.
    doctor, schedule, admin = await world(db, clinic)
    [first, *_] = await three_waiting(db, clinic, doctor, schedule)
    entry_id = (await entry_of(db, first)).id
    await service.send_in(db, admin, entry_id)
    assert await reason_of(service.send_in(db, admin, entry_id)) == "not_waiting"


async def test_mark_seen_finishes_the_visit(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    [first, *_] = await three_waiting(db, clinic, doctor, schedule)
    entry_id = (await entry_of(db, first)).id
    await service.send_in(db, admin, entry_id)
    await service.mark_seen(db, admin, entry_id)
    assert (await entry_of(db, first)).state == "served"
    assert await status_of(db, first) == "done"


async def test_the_room_is_free_again_after_mark_seen(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    [first, second, _] = await three_waiting(db, clinic, doctor, schedule)
    first_id, second_id = (await entry_of(db, first)).id, (await entry_of(db, second)).id
    await service.send_in(db, admin, first_id)
    await service.mark_seen(db, admin, first_id)
    await service.send_in(db, admin, second_id)
    assert (await entry_of(db, second)).state == "in_room"


async def test_only_someone_in_the_room_can_be_marked_seen_or_put_back(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    [first, *_] = await three_waiting(db, clinic, doctor, schedule)
    entry_id = (await entry_of(db, first)).id
    assert await reason_of(service.mark_seen(db, admin, entry_id)) == "not_in_room"
    assert await reason_of(service.put_back(db, admin, entry_id)) == "not_in_room"
    await service.send_in(db, admin, entry_id)
    await service.mark_seen(db, admin, entry_id)
    assert await reason_of(service.mark_seen(db, admin, entry_id)) == "not_in_room"


async def test_put_back_keeps_the_place_and_the_appointment_status(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    [first, second, _] = await three_waiting(db, clinic, doctor, schedule)
    entry_id = (await entry_of(db, first)).id
    await service.send_in(db, admin, entry_id)
    await service.put_back(db, admin, entry_id)
    entry = await entry_of(db, first)
    assert (entry.state, entry.token_number) == ("waiting", 1)
    assert await status_of(db, first) == "checked_in"
    assert (await entry_of(db, second)).token_number == 2


async def test_entries_of_another_clinic_or_another_day_cannot_be_acted_on(
    db, clinic, second_clinic
):
    doctor, schedule, admin = await world(db, clinic)
    [first, *_] = await three_waiting(db, clinic, doctor, schedule)
    _, outsider = await make_admin(db, second_clinic, email="out.admin@x.com")
    with pytest.raises(NotFound):
        await service.send_in(db, outsider, (await entry_of(db, first)).id)
    old = await add_appointment(
        db, await person(db, 9), doctor, clinic, schedule, at(date(2026, 10, 5), "14:00")
    )
    stale = await add_entry(db, old, 1)
    assert await reason_of(service.send_in(db, admin, stale.id)) == "not_today"


async def test_queue_actions_write_audit_rows_with_ids_only(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    [first, *_] = await three_waiting(db, clinic, doctor, schedule)
    entry_id = (await entry_of(db, first)).id
    await service.send_in(db, admin, entry_id)
    await service.put_back(db, admin, entry_id)
    await service.send_in(db, admin, entry_id)
    await service.mark_seen(db, admin, entry_id)
    actions = [
        row.action
        for row in (await db.execute(select(AuditLog).order_by(AuditLog.created_at))).scalars()
        if row.action.startswith("queue.")
    ]
    assert sorted(actions) == ["queue.put_back", "queue.seen", "queue.send_in", "queue.send_in"]
    for row in (await db.execute(select(AuditLog))).scalars():
        if row.action.startswith("queue."):
            assert set(json.loads(row.detail)) == {
                "appointment_id",
                "doctor_id",
                "clinic_id",
                "entry_id",
            }


# ── Move forward ─────────────────────────────────────────────────────────
async def test_move_up_swaps_with_the_patient_ahead(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    appointments = await three_waiting(db, clinic, doctor, schedule)
    await service.move_up(db, admin, (await entry_of(db, appointments[2])).id)
    assert await tokens(db, appointments) == [1, 3, 2]


async def test_moving_up_the_first_in_line_changes_nothing_and_writes_nothing(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    appointments = await three_waiting(db, clinic, doctor, schedule)
    await service.move_up(db, admin, (await entry_of(db, appointments[0])).id)
    assert await tokens(db, appointments) == [1, 2, 3]
    rows = (await db.execute(select(AuditLog).where(AuditLog.action == "queue.move"))).all()
    assert rows == []


async def test_make_next_puts_the_patient_first(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    appointments = await three_waiting(db, clinic, doctor, schedule)
    await service.make_next(db, admin, (await entry_of(db, appointments[2])).id)
    assert await tokens(db, appointments) == [2, 3, 1]


async def test_a_move_keeps_tokens_unique_and_leaves_the_room_and_the_seen_alone(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    waiting = await three_waiting(db, clinic, doctor, schedule)
    in_room = await booked_today(db, clinic, doctor, schedule, await person(db, 7), "15:30")
    seen = await booked_today(db, clinic, doctor, schedule, await person(db, 8), "15:45")
    await add_entry(db, in_room, 4, "in_room")
    await add_entry(db, seen, 5, "served")
    await service.make_next(db, admin, (await entry_of(db, waiting[2])).id)
    assert await tokens(db, waiting) == [2, 3, 1]
    assert (await entry_of(db, in_room)).token_number == 4
    assert (await entry_of(db, seen)).token_number == 5
    everyone = await tokens(db, [*waiting, in_room, seen])
    assert len(set(everyone)) == 5


async def test_only_a_waiting_patient_can_be_moved(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    [first, *_] = await three_waiting(db, clinic, doctor, schedule)
    entry_id = (await entry_of(db, first)).id
    await service.send_in(db, admin, entry_id)
    assert await reason_of(service.move_up(db, admin, entry_id)) == "not_waiting"
    assert await reason_of(service.make_next(db, admin, entry_id)) == "not_waiting"


async def test_a_move_in_one_session_does_not_touch_another(db, clinic):
    doctor, schedule, admin = await world(db, clinic)
    other, other_schedule = await todays_session(db, clinic, email="o.dr@x.com", name="Dr Other")
    mine = await three_waiting(db, clinic, doctor, schedule)
    theirs = await booked_today(db, clinic, other, other_schedule, await person(db, 9), "14:00")
    await add_entry(db, theirs, 1)
    await service.make_next(db, admin, (await entry_of(db, mine[2])).id)
    assert (await entry_of(db, theirs)).token_number == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_queue_actions.py -q`
Expected: failures: `AttributeError: module 'app.queueing.service' has no attribute 'check_in'`.

- [ ] **Step 3: Write the implementation**

In `backend/app/queueing/service.py`, replace the import block (from `import uuid` down to the end of the `from .schemas import (...)` block) with:

```python
import uuid
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import writer as audit
from ..booking import service as booking_service
from ..booking import slots
from ..booking.schemas import ACTIVE_STATUSES, DEFAULT_MINUTES
from ..db.models import Appointment, Clinic, Doctor, Patient, QueueEntry, UserRole
from ..deps import Actor
from ..errors import NotFound, QueueRefused
from ..schedule import service as schedule_service
from . import order
from .schemas import (
    CHANNEL_WALK_IN,
    STATE_IN_ROOM,
    STATE_SERVED,
    STATE_WAITING,
    Line,
    QueueStatus,
    SessionKey,
    SessionView,
)
```

Append to `backend/app/queueing/service.py`:

```python


# ── Actions (the clinic admin only) ──────────────────────────────────────
def _new_entry(appointment_id: uuid.UUID, token: int) -> QueueEntry:
    """A waiting entry. If the schema owner's answer showed another NOT NULL column
    with no database default (Task 1, Step 1), set it here from the appointment."""
    return QueueEntry(appointment_id=appointment_id, token_number=token, state=STATE_WAITING)


async def _audit(
    session: AsyncSession,
    actor: Actor,
    action: str,
    appointment: Appointment,
    *,
    entry: QueueEntry | None = None,
    extra: dict[str, str] | None = None,
    user_agent: str | None = None,
) -> None:
    """One audit row, ids only: never a name, a passport number or search text."""
    detail = {
        "appointment_id": str(appointment.id),
        "doctor_id": str(appointment.doctor_id),
        "clinic_id": str(appointment.clinic_id),
    }
    if entry is not None:
        detail["entry_id"] = str(entry.id)
    if extra:
        detail.update(extra)
    await audit.write(
        session,
        action=action,
        actor_user_id=actor.user_id,
        actor_role=actor.role,
        subject_patient_id=appointment.patient_id,
        resource_type="queue_entry",
        ip_address=actor.ip_address,
        user_agent=user_agent,
        detail=detail,
    )


async def _locked_entry(
    session: AsyncSession, actor: Actor, entry_id: uuid.UUID
) -> tuple[QueueEntry, Appointment]:
    """The admin's own entry, found, then the doctor locked, then re-read. Only
    today's entries can be acted on."""
    clinics = _staff(actor)
    row = (
        await session.execute(
            select(QueueEntry, Appointment)
            .join(Appointment, Appointment.id == QueueEntry.appointment_id)
            .where(QueueEntry.id == entry_id, Appointment.clinic_id.in_(clinics))
        )
    ).first()
    if row is None:
        raise NotFound()
    entry, appointment = row
    await schedule_service.lock_doctor(session, appointment.doctor_id)
    await session.refresh(entry)
    await session.refresh(appointment)
    if slots.to_local(appointment.slot_start).date() != booking_service.current_day():
        raise QueueRefused("not_today")
    return entry, appointment


async def check_in(
    session: AsyncSession,
    actor: Actor,
    appointment_id: uuid.UUID,
    *,
    user_agent: str | None = None,
) -> None:
    """Check a booked patient in: they join their session's queue at the back.
    Checking in twice does nothing."""
    clinics = _staff(actor)
    appointment = (
        await session.execute(
            select(Appointment).where(
                Appointment.id == appointment_id, Appointment.clinic_id.in_(clinics)
            )
        )
    ).scalar_one_or_none()
    if appointment is None:
        raise NotFound()
    await schedule_service.lock_doctor(session, appointment.doctor_id)
    await session.refresh(appointment)
    existing = (
        await session.execute(select(QueueEntry).where(QueueEntry.appointment_id == appointment.id))
    ).scalar_one_or_none()
    if existing is not None:
        return
    if appointment.status != "booked":
        raise QueueRefused("not_booked")
    if slots.to_local(appointment.slot_start).date() != booking_service.current_day():
        raise QueueRefused("not_today")
    if not await booking_service.is_bookable(session, appointment.doctor_id, appointment.clinic_id):
        raise QueueRefused("doctor_unavailable")
    window = await _window_of(session, appointment)
    if window is None or booking_service.now() >= slots.window_bounds(window)[1]:
        raise QueueRefused("session_over")
    lo, hi = slots.window_bounds(window)
    entries = await _session_entries(session, window.schedule_id, lo, hi)
    entry = _new_entry(appointment.id, order.next_token(e.token_number for e, _ in entries))
    session.add(entry)
    appointment.status = "checked_in"
    await session.flush()
    await _audit(session, actor, audit.QUEUE_CHECK_IN, appointment, entry=entry, user_agent=user_agent)
    await session.commit()


async def send_in(
    session: AsyncSession, actor: Actor, entry_id: uuid.UUID, *, user_agent: str | None = None
) -> None:
    """Send a waiting patient into the room. Refused while someone is already there."""
    entry, appointment = await _locked_entry(session, actor, entry_id)
    if entry.state != STATE_WAITING:
        raise QueueRefused("not_waiting")
    peers = await _peers(session, appointment)
    if any(other.state == STATE_IN_ROOM for other, _ in peers):
        raise QueueRefused("room_occupied")
    entry.state = STATE_IN_ROOM
    await _audit(session, actor, audit.QUEUE_SEND_IN, appointment, entry=entry, user_agent=user_agent)
    await session.commit()


async def mark_seen(
    session: AsyncSession, actor: Actor, entry_id: uuid.UUID, *, user_agent: str | None = None
) -> None:
    """The patient in the room has been seen: the visit is finished (final)."""
    entry, appointment = await _locked_entry(session, actor, entry_id)
    if entry.state != STATE_IN_ROOM:
        raise QueueRefused("not_in_room")
    entry.state = STATE_SERVED
    appointment.status = "done"
    await _audit(session, actor, audit.QUEUE_SEEN, appointment, entry=entry, user_agent=user_agent)
    await session.commit()


async def put_back(
    session: AsyncSession, actor: Actor, entry_id: uuid.UUID, *, user_agent: str | None = None
) -> None:
    """Undo a mistaken "send in": back to waiting, keeping their token (their place)."""
    entry, appointment = await _locked_entry(session, actor, entry_id)
    if entry.state != STATE_IN_ROOM:
        raise QueueRefused("not_in_room")
    entry.state = STATE_WAITING
    await _audit(session, actor, audit.QUEUE_PUT_BACK, appointment, entry=entry, user_agent=user_agent)
    await session.commit()


async def _move(
    session: AsyncSession, actor: Actor, entry_id: uuid.UUID, how: str, user_agent: str | None
) -> None:
    entry, appointment = await _locked_entry(session, actor, entry_id)
    if entry.state != STATE_WAITING:
        raise QueueRefused("not_waiting")
    peers = await _peers(session, appointment)
    waiting = sorted(
        (e for e, _ in peers if e.state == STATE_WAITING),
        key=lambda e: (e.token_number, str(e.id)),
    )
    ids = [e.id for e in waiting]
    new_ids = order.moved_up(ids, entry.id) if how == "up" else order.made_next(ids, entry.id)
    if new_ids == ids:
        return
    new_tokens = order.reassign([e.token_number for e in waiting], new_ids)
    for other in waiting:
        other.token_number = new_tokens[other.id]
    await _audit(
        session,
        actor,
        audit.QUEUE_MOVE,
        appointment,
        entry=entry,
        extra={"how": how},
        user_agent=user_agent,
    )
    await session.commit()


async def move_up(
    session: AsyncSession, actor: Actor, entry_id: uuid.UUID, *, user_agent: str | None = None
) -> None:
    """Swap a waiting patient with the one directly ahead."""
    await _move(session, actor, entry_id, "up", user_agent)


async def make_next(
    session: AsyncSession, actor: Actor, entry_id: uuid.UUID, *, user_agent: str | None = None
) -> None:
    """Put a waiting patient first among the waiting."""
    await _move(session, actor, entry_id, "next", user_agent)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_queue_actions.py -q`
Expected: all pass (about 30).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check backend tests --fix && uv run ruff format backend tests
uv run pytest tests/test_queue_*.py tests/test_booking_*.py -q
git add backend/app/queueing/service.py tests/test_queue_actions.py
git commit -m "feat(queue): check in, send in, mark seen, put back and move forward"
```

---

### Task 6: Service: the walk-in search, walk-ins, and booking ignoring walk-ins

**Files:**
- Create: `backend/app/queueing/display.py`
- Modify: `backend/app/queueing/service.py` (imports, then append), `backend/app/booking/service.py` (`_doctor_held`)
- Test: `tests/test_queue_walk_in.py`

**Interfaces:**
- Consumes: Task 5's `_audit`, `_new_entry`, `_session_entries`, `_day_bounds`, `_staff`; `booking_service.is_bookable`, `.session_windows`, `.active_starts`, `.now`, `.current_day`; `SessionKey`; `PatientHit`; `SEARCH_MIN`, `SEARCH_LIMIT`, `CHANNEL_WALK_IN`.
- Produces: `display.mask_passport(value) -> str`; `async find_patients(session, actor, text, *, user_agent=None) -> list[PatientHit]`; `async add_walk_in(session, actor, key: SessionKey, patient_id, *, user_agent=None) -> None`. Booking: `_doctor_held` ignores `walk_in` appointments (a NULL channel still counts).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_queue_walk_in.py`:

```python
"""P6: finding a patient at the front desk, and walk-ins."""

from __future__ import annotations

import json
import uuid

import pytest
from app.booking import service as booking_service
from app.booking import slots
from app.db.models import Appointment, AuditLog, VerificationStatus
from app.deps import Actor
from app.errors import NotFound, QueueRefused
from app.queueing import service
from app.queueing.display import mask_passport
from app.queueing.schemas import SessionKey
from sqlalchemy import select

from tests.booking_helpers import TODAY, at, freeze, make_patient
from tests.queue_helpers import add_entry, booked_today, entry_of, make_admin, todays_session


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    freeze(monkeypatch)


async def person(db, n: int, name: str | None = None):
    _, patient, actor = await make_patient(db, email=f"w{n}@x.com", name=name or f"Patient {n}")
    return patient, actor


async def world(db, clinic, **kw):
    doctor, schedule = await todays_session(db, clinic, **kw)
    _, admin = await make_admin(db, clinic)
    key = SessionKey(doctor.id, clinic.id, schedule.id, schedule.start_time)
    return doctor, schedule, admin, key


async def reason_of(call) -> str:
    with pytest.raises(QueueRefused) as exc:
        await call
    return exc.value.reason


async def appointment_of(db, patient) -> Appointment:
    return (
        await db.execute(
            select(Appointment)
            .where(Appointment.patient_id == patient.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


# ── Masking ──────────────────────────────────────────────────────────────
def test_a_passport_number_shows_only_its_last_four_characters():
    assert mask_passport("CN-ZWSD-6GH8") == "••-••••-6GH8"


def test_a_very_short_value_is_hidden_completely():
    assert mask_passport("AB-12") == "••-••"


# ── The search ───────────────────────────────────────────────────────────
async def test_a_name_search_ignores_case_and_matches_part_of_a_name(db, clinic):
    _, _, admin, _ = await world(db, clinic)
    patient, _ = await person(db, 1, "Ayesha Raza")
    await person(db, 2, "Bilal Khan")
    hits = await service.find_patients(db, admin, "  YESHA ")
    assert [(h.patient_id, h.name) for h in hits] == [(patient.id, "Ayesha Raza")]


async def test_an_exact_passport_number_matches_in_any_case(db, clinic):
    _, _, admin, _ = await world(db, clinic)
    patient, _ = await person(db, 1)
    hits = await service.find_patients(db, admin, patient.passport_no.lower())
    assert [h.patient_id for h in hits] == [patient.id]


async def test_part_of_a_passport_number_never_matches(db, clinic):
    # Review focus 5: no enumerating patients by guessing numbers.
    _, _, admin, _ = await world(db, clinic)
    patient, _ = await person(db, 1, "Zed Person")
    assert await service.find_patients(db, admin, patient.passport_no[:-1]) == []
    assert await service.find_patients(db, admin, patient.passport_no[3:]) == []


async def test_results_never_carry_the_full_passport_number(db, clinic):
    _, _, admin, _ = await world(db, clinic)
    patient, _ = await person(db, 1, "Ayesha Raza")
    [hit] = await service.find_patients(db, admin, "Ayesha")
    assert hit.passport == mask_passport(patient.passport_no) and hit.passport != patient.passport_no
    assert patient.passport_no[-4:] in hit.passport


async def test_a_short_search_is_refused_and_a_long_result_list_is_cut(db, clinic):
    _, _, admin, _ = await world(db, clinic)
    assert await reason_of(service.find_patients(db, admin, " ab ")) == "search_too_short"
    for n in range(12):
        await person(db, n, f"Zainab {n:02d}")
    hits = await service.find_patients(db, admin, "Zainab")
    assert len(hits) == 8 and [h.name for h in hits] == [f"Zainab {n:02d}" for n in range(8)]


async def test_a_percent_sign_is_not_a_wildcard(db, clinic):
    _, _, admin, _ = await world(db, clinic)
    await person(db, 1, "Ayesha Raza")
    assert await service.find_patients(db, admin, "%%%") == []


async def test_only_an_admin_can_search(db, clinic):
    _, _, _, _ = await world(db, clinic)
    await person(db, 1)
    with pytest.raises(NotFound):
        await service.find_patients(db, Actor(user_id=uuid.uuid4(), role="patient"), "Patient")


async def test_a_search_logs_only_how_many_it_found(db, clinic):
    _, _, admin, _ = await world(db, clinic)
    await person(db, 1, "Ayesha Raza")
    await service.find_patients(db, admin, "Ayesha")
    [row] = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "queue.find_patient")))
        .scalars()
        .all()
    )
    assert json.loads(row.detail) == {"results": 1}
    assert "Ayesha" not in (row.detail or "") and row.subject_patient_id is None


# ── Walk-ins ─────────────────────────────────────────────────────────────
async def test_a_walk_in_gets_an_appointment_and_a_place_at_the_back(db, clinic):
    doctor, schedule, admin, key = await world(db, clinic)
    waiting, _ = await person(db, 1)
    await add_entry(db, await booked_today(db, clinic, doctor, schedule, waiting), 1)
    walker, _ = await person(db, 2)
    await service.add_walk_in(db, admin, key, walker.id)
    appointment = await appointment_of(db, walker)
    entry = await entry_of(db, appointment)
    assert (appointment.booking_channel, appointment.status) == ("walk_in", "checked_in")
    assert appointment.schedule_id == schedule.id and appointment.clinic_id == clinic.id
    assert (entry.state, entry.token_number) == ("waiting", 2)


async def test_a_walks_in_time_is_the_arrival_time(db, clinic, monkeypatch):
    doctor, schedule, admin, key = await world(db, clinic)
    freeze(monkeypatch, now=at(TODAY, "13:07"))
    walker, _ = await person(db, 1)
    await service.add_walk_in(db, admin, key, walker.id)
    appointment = await appointment_of(db, walker)
    assert slots.as_utc(appointment.slot_start) == at(TODAY, "13:07")


async def test_a_walk_in_is_never_placed_before_the_session_starts(db, clinic, monkeypatch):
    doctor, schedule, admin, key = await world(db, clinic)
    freeze(monkeypatch, now=at(TODAY, "11:30"))
    walker, _ = await person(db, 1)
    await service.add_walk_in(db, admin, key, walker.id)
    appointment = await appointment_of(db, walker)
    start = slots.as_utc(appointment.slot_start)
    # one microsecond after the session opens: inside the window, off the booking grid
    assert (start - at(TODAY, "12:00")).total_seconds() == pytest.approx(0.000001)


async def test_two_walk_ins_in_the_same_instant_both_get_a_place(db, clinic):
    # Review focus 2.
    doctor, schedule, admin, key = await world(db, clinic)
    first, _ = await person(db, 1)
    second, _ = await person(db, 2)
    await service.add_walk_in(db, admin, key, first.id)
    await service.add_walk_in(db, admin, key, second.id)
    one, two = await appointment_of(db, first), await appointment_of(db, second)
    assert one.slot_start != two.slot_start
    assert [(await entry_of(db, one)).token_number, (await entry_of(db, two)).token_number] == [1, 2]


async def test_walk_ins_count_toward_the_cap(db, clinic):
    doctor, schedule, admin, key = await world(db, clinic, cap=2)
    booked, _ = await person(db, 1)
    await booked_today(db, clinic, doctor, schedule, booked, "14:00")
    one, _ = await person(db, 2)
    two, _ = await person(db, 3)
    await service.add_walk_in(db, admin, key, one.id)
    assert await reason_of(service.add_walk_in(db, admin, key, two.id)) == "session_full"
    assert await entry_of(db, await appointment_of(db, one)) is not None


async def test_a_patient_with_a_booking_today_is_sent_to_check_in(db, clinic):
    doctor, schedule, admin, key = await world(db, clinic)
    patient, _ = await person(db, 1)
    await booked_today(db, clinic, doctor, schedule, patient, "14:00")
    assert await reason_of(service.add_walk_in(db, admin, key, patient.id)) == "has_booking_today"


async def test_a_patient_already_in_a_queue_cannot_join_another(db, clinic):
    doctor, schedule, admin, key = await world(db, clinic)
    other, other_schedule = await todays_session(db, clinic, email="o.dr@x.com", name="Dr Other")
    patient, _ = await person(db, 1)
    await add_entry(db, await booked_today(db, clinic, other, other_schedule, patient), 1)
    assert await reason_of(service.add_walk_in(db, admin, key, patient.id)) == "already_queued"


async def test_a_patient_who_was_seen_earlier_can_come_back_as_a_walk_in(db, clinic):
    doctor, schedule, admin, key = await world(db, clinic)
    patient, _ = await person(db, 1)
    seen = await booked_today(db, clinic, doctor, schedule, patient, "12:30")
    await add_entry(db, seen, 1, "served")
    await service.add_walk_in(db, admin, key, patient.id)
    entries = (await db.execute(select(Appointment).where(Appointment.patient_id == patient.id))).all()
    assert len(entries) == 2


async def test_no_walk_in_after_the_session_has_ended(db, clinic, monkeypatch):
    doctor, schedule, admin, key = await world(db, clinic)
    patient, _ = await person(db, 1)
    freeze(monkeypatch, now=at(TODAY, "18:30"))
    assert await reason_of(service.add_walk_in(db, admin, key, patient.id)) == "session_over"


async def test_a_doctor_who_is_not_verified_cannot_take_walk_ins(db, clinic):
    doctor, schedule, admin, key = await world(db, clinic)
    patient, _ = await person(db, 1)
    doctor.verification_status = VerificationStatus.REJECTED
    doctor.verified_by = None
    doctor.verified_at = None
    await db.commit()
    assert await reason_of(service.add_walk_in(db, admin, key, patient.id)) == "doctor_unavailable"


async def test_scoping_and_unknown_things_are_not_found(db, clinic, second_clinic):
    doctor, schedule, admin, key = await world(db, clinic)
    patient, _ = await person(db, 1)
    _, outsider = await make_admin(db, second_clinic, email="out.admin@x.com")
    with pytest.raises(NotFound):
        await service.add_walk_in(db, outsider, key, patient.id)
    with pytest.raises(NotFound):
        await service.add_walk_in(db, admin, key, uuid.uuid4())
    with pytest.raises(NotFound):
        await service.add_walk_in(
            db, admin, SessionKey(doctor.id, clinic.id, uuid.uuid4(), key.start), patient.id
        )
    with pytest.raises(NotFound):
        await service.add_walk_in(db, Actor(user_id=uuid.uuid4(), role="patient"), key, patient.id)


async def test_a_walk_in_locks_the_doctor_first_and_writes_an_audit_row(db, clinic, monkeypatch):
    doctor, schedule, admin, key = await world(db, clinic)
    patient, _ = await person(db, 1)
    calls: list[uuid.UUID] = []
    real = service.schedule_service.lock_doctor

    async def spy(session, doctor_id):
        calls.append(doctor_id)
        await real(session, doctor_id)

    monkeypatch.setattr(service.schedule_service, "lock_doctor", spy)
    await service.add_walk_in(db, admin, key, patient.id)
    assert calls == [doctor.id]
    [row] = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "queue.walk_in")))
        .scalars()
        .all()
    )
    assert set(json.loads(row.detail)) == {"appointment_id", "doctor_id", "clinic_id", "entry_id"}


# ── Walk-ins and the booking rules ───────────────────────────────────────
async def test_a_walk_in_never_hides_a_bookable_time(db, clinic, monkeypatch):
    # Review focus 2: it holds 12:00:00.000001, which overlaps the 12:00 and 12:15
    # turns on paper, but it is not a booked turn and must not block them.
    doctor, schedule, admin, key = await world(db, clinic)
    freeze(monkeypatch, now=at(TODAY, "11:30"))
    walker, _ = await person(db, 1)
    await service.add_walk_in(db, admin, key, walker.id)
    options = await booking_service.list_options(db, doctor.id, clinic.id)
    today = next(d for d in options if d.day == TODAY)
    assert at(TODAY, "12:00") in today.times and at(TODAY, "12:15") in today.times
    _, _, actor = await make_patient(db, email="online@x.com", name="Online")
    appointment = await booking_service.book(db, actor, doctor.id, clinic.id, at(TODAY, "12:15"))
    assert appointment.status == "booked"


async def test_the_doctor_held_check_counts_every_channel_but_walk_in(db, clinic):
    doctor, schedule, _, _ = await world(db, clinic)
    online, legacy, walker = [(await person(db, n))[0] for n in (1, 2, 3)]
    await booked_today(db, clinic, doctor, schedule, online, "14:00")  # channel "online"
    old = await booked_today(db, clinic, doctor, schedule, legacy, "14:15")
    old.booking_channel = None  # a legacy booking with no channel still blocks
    arrival = await booked_today(db, clinic, doctor, schedule, walker, "14:30")
    arrival.booking_channel = "walk_in"
    await db.commit()
    held = await booking_service._doctor_held(db, doctor.id, at(TODAY, "12:00"), at(TODAY, "18:00"))
    assert sorted(start for start, _ in held) == [at(TODAY, "14:00"), at(TODAY, "14:15")]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_queue_walk_in.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'app.queueing.display'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/queueing/display.py`:

```python
"""Pure display helpers for the queue (P6): no database."""

from __future__ import annotations


def mask_passport(value: str) -> str:
    """Hide every letter or digit but the last four: `CN-ZWSD-6GH8` becomes
    `••-••••-6GH8`. A value with four or fewer is hidden completely."""
    positions = [i for i, ch in enumerate(value) if ch.isalnum()]
    hidden = set(positions[:-4]) if len(positions) > 4 else set(positions)
    return "".join("•" if i in hidden else ch for i, ch in enumerate(value))
```

In `backend/app/queueing/service.py`, replace the import block with (adds `func`, `or_`, `IntegrityError`, `display`, `PatientHit`, `SEARCH_*`):

```python
import uuid
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import writer as audit
from ..booking import service as booking_service
from ..booking import slots
from ..booking.schemas import ACTIVE_STATUSES, DEFAULT_MINUTES
from ..db.models import Appointment, Clinic, Doctor, Patient, QueueEntry, UserRole
from ..deps import Actor
from ..errors import NotFound, QueueRefused
from ..schedule import service as schedule_service
from . import order
from .display import mask_passport
from .schemas import (
    CHANNEL_WALK_IN,
    SEARCH_LIMIT,
    SEARCH_MIN,
    STATE_IN_ROOM,
    STATE_SERVED,
    STATE_WAITING,
    Line,
    PatientHit,
    QueueStatus,
    SessionKey,
    SessionView,
)
```

Append to `backend/app/queueing/service.py`:

```python


# ── Walk-ins ─────────────────────────────────────────────────────────────
async def find_patients(
    session: AsyncSession, actor: Actor, text: str, *, user_agent: str | None = None
) -> list[PatientHit]:
    """Find a registered patient at the front desk by name (part of it, any case) or
    by an exact full passport number. Results show the passport number masked.
    The audit row records only how many were found, never what was typed."""
    _staff(actor)
    needle = text.strip()[:80]
    if len(needle) < SEARCH_MIN:
        raise QueueRefused("search_too_short")
    patients = (
        (
            await session.execute(
                select(Patient)
                .where(
                    or_(
                        func.upper(Patient.passport_no) == needle.upper(),
                        Patient.full_name.icontains(needle, autoescape=True),
                    )
                )
                .order_by(Patient.full_name, Patient.id)
                .limit(SEARCH_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    await audit.write(
        session,
        action=audit.QUEUE_FIND_PATIENT,
        actor_user_id=actor.user_id,
        actor_role=actor.role,
        resource_type="patient_search",
        ip_address=actor.ip_address,
        user_agent=user_agent,
        detail={"results": len(patients)},
    )
    await session.commit()
    return [PatientHit(p.id, p.full_name, mask_passport(p.passport_no)) for p in patients]


async def add_walk_in(
    session: AsyncSession,
    actor: Actor,
    key: SessionKey,
    patient_id: uuid.UUID,
    *,
    user_agent: str | None = None,
) -> None:
    """Add a registered patient with no booking to a session's queue, at the back.
    They count toward the session cap but never block a bookable time."""
    clinics = _staff(actor)
    if key.clinic_id not in clinics:
        raise NotFound()
    await schedule_service.lock_doctor(session, key.doctor_id)
    if not await booking_service.is_bookable(session, key.doctor_id, key.clinic_id):
        raise QueueRefused("doctor_unavailable")
    today = booking_service.current_day()
    windows = await booking_service.session_windows(session, key.doctor_id, key.clinic_id, today)
    window = next(
        (w for w in windows if w.schedule_id == key.schedule_id and w.start == key.start), None
    )
    if window is None:
        raise NotFound()
    lo, hi = slots.window_bounds(window)
    now = booking_service.now()
    if now >= hi:
        raise QueueRefused("session_over")
    patient = await session.get(Patient, patient_id)
    if patient is None:
        raise NotFound()

    day_lo, day_hi = _day_bounds(today)
    holds = (
        await session.execute(
            select(func.count())
            .select_from(Appointment)
            .where(
                Appointment.patient_id == patient.id,
                Appointment.doctor_id == key.doctor_id,
                Appointment.status.in_(("booked", "checked_in")),
                Appointment.slot_start >= day_lo,
                Appointment.slot_start < day_hi,
            )
        )
    ).scalar_one()
    if holds:
        raise QueueRefused("has_booking_today")
    queued = (
        await session.execute(
            select(func.count())
            .select_from(QueueEntry)
            .join(Appointment, Appointment.id == QueueEntry.appointment_id)
            .where(
                Appointment.patient_id == patient.id,
                QueueEntry.state.in_((STATE_WAITING, STATE_IN_ROOM)),
            )
        )
    ).scalar_one()
    if queued:
        raise QueueRefused("already_queued")

    taken = await booking_service.active_starts(session, window.schedule_id, lo, hi)
    if len(taken) >= window.max_tokens:
        raise QueueRefused("session_full")
    start = max(now, lo + timedelta(microseconds=1))
    held = set(taken)
    while start in held:
        start += timedelta(microseconds=1)
    appointment = Appointment(
        patient_id=patient.id,
        doctor_id=key.doctor_id,
        clinic_id=key.clinic_id,
        schedule_id=window.schedule_id,
        slot_start=start,
        status="checked_in",
        booking_channel=CHANNEL_WALK_IN,
    )
    session.add(appointment)
    try:
        await session.flush()
    except IntegrityError:  # the index caught a clash the checks did not see
        await session.rollback()
        raise QueueRefused("try_again") from None
    entries = await _session_entries(session, window.schedule_id, lo, hi)
    entry = _new_entry(appointment.id, order.next_token(e.token_number for e, _ in entries))
    session.add(entry)
    await session.flush()
    await _audit(session, actor, audit.QUEUE_WALK_IN, appointment, entry=entry, user_agent=user_agent)
    await session.commit()
```

In `backend/app/booking/service.py`, add `or_` to the sqlalchemy import (`from sqlalchemy import or_, select`), add `from ..queueing.schemas import CHANNEL_WALK_IN` to the imports, and in `_doctor_held` replace the `.where(...)` of `stmt` with:

```python
        .where(
            Appointment.doctor_id == doctor_id,
            Appointment.status.in_(ACTIVE_STATUSES),
            Appointment.slot_start >= lo,
            Appointment.slot_start < hi,
            # a walk-in counts toward the cap but is not a booked turn: it never
            # blocks a bookable time (a NULL channel is a legacy booking and does)
            or_(Appointment.booking_channel.is_(None), Appointment.booking_channel != CHANNEL_WALK_IN),
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_queue_walk_in.py -q`
Expected: all pass (about 25).

- [ ] **Step 5: Run the whole suite, lint, commit**

```bash
uv run pytest -q
uv run ruff check backend tests --fix && uv run ruff format backend tests
git add backend/app/queueing/display.py backend/app/queueing/service.py backend/app/booking/service.py tests/test_queue_walk_in.py
git commit -m "feat(queue): find a patient and add walk-ins; booking ignores walk-ins"
```

---

### Task 7: The clinic admin's queue page

**Files:**
- Create: `backend/app/web/queue.py`, `frontend/templates/queue/admin.html`
- Modify: `backend/app/main.py` (include the router), `backend/app/web/router.py` (`_admin_nav`: one entry), `backend/app/i18n/messages/en.json`, `ur.json`
- Test: `tests/test_queue_web_admin.py`

**Interfaces:**
- Consumes: everything in `queueing/service.py` (Tasks 4 to 6); from `web/router.py`: `_admin_nav`, `_admin_page_guard`, `_render`; `deps.check_queue_write_rate`, `check_queue_lookup_rate`; `QueueRefused`, `RateLimited`, `NotFound`; `SessionKey`.
- Produces (`web/queue.py`): `router`; routes `GET /{locale}/admin/queue` (query `saved`, `find`, `session`); `POST /{locale}/admin/queue/check-in/{appointment_id}`; `POST /{locale}/admin/queue/{entry_id}/send-in`, `/seen`, `/put-back`, `/move-up`, `/make-next`; `POST /{locale}/admin/queue/walk-in` (form `session`, `patient_id`). Every action redirects to `/{loc}/admin/queue?saved=<flag>`; a refusal re-renders the page (409) with the reason; an unknown or foreign id redirects to the plain queue page.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_queue_web_admin.py`:

```python
"""P6 web: the clinic admin's queue page and its actions."""

from __future__ import annotations

import uuid

import pytest
from app.db.models import Appointment, Patient, UserRole
from app.queueing.schemas import SessionKey
from app.settings import settings
from sqlalchemy import select

from tests.booking_helpers import freeze, login, make_patient
from tests.conftest import make_user
from tests.queue_helpers import add_entry, booked_today, entry_of, make_admin, todays_session

PAGE = "/en/admin/queue"


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    freeze(monkeypatch)


@pytest.fixture
async def desk(client, db, clinic):
    """The admin signed in, with a doctor who has a session today."""
    doctor, schedule = await todays_session(db, clinic)
    await make_admin(db, clinic)
    await login(client, "queue.admin@x.com")
    return doctor, schedule


async def patient(db, n: int, name: str | None = None):
    _, row, _ = await make_patient(db, email=f"d{n}@x.com", name=name or f"Patient {n}")
    return row


async def in_queue(db, clinic, doctor, schedule, n: int, token: int, state="waiting", name=None):
    """A booked patient number n, checked in with `token` in `state`."""
    appointment = await booked_today(
        db, clinic, doctor, schedule, await patient(db, n, name), f"14:{n * 5:02d}"
    )
    await add_entry(db, appointment, token, state)
    return appointment


def key_of(doctor, clinic, schedule) -> str:
    return SessionKey(doctor.id, clinic.id, schedule.id, schedule.start_time).encode()


def text_of(response) -> str:
    return response.text.replace("&#39;", "'")


# ── Guards ───────────────────────────────────────────────────────────────
async def test_anonymous_goes_to_login(client):
    r = await client.get(PAGE)
    assert r.status_code == 303 and r.headers["location"] == "/en/login?next=/en/admin/queue"


@pytest.mark.parametrize(
    ("role", "area"), [(UserRole.PATIENT, "patient"), (UserRole.DOCTOR, "doctor")]
)
async def test_other_roles_go_to_their_own_area(client, db, clinic, role, area):
    await make_user(db, email="other@x.com", role=role, clinic_id=clinic.id)
    await login(client, "other@x.com")
    some = uuid.uuid4()
    calls = [
        ("GET", PAGE),
        ("POST", f"{PAGE}/check-in/{some}"),
        ("POST", f"{PAGE}/{some}/send-in"),
        ("POST", f"{PAGE}/{some}/seen"),
        ("POST", f"{PAGE}/walk-in"),
    ]
    for method, path in calls:
        r = await client.request(method, path)
        assert r.status_code == 303 and r.headers["location"] == f"/en/{area}"


async def test_an_admin_with_no_clinic_goes_to_their_dashboard(client, db):
    await make_user(db, email="lonely@x.com", role=UserRole.CLINIC_ADMIN)
    await login(client, "lonely@x.com")
    r = await client.get(PAGE)
    assert r.status_code == 303 and r.headers["location"] == "/en/admin"


# ── The page ─────────────────────────────────────────────────────────────
async def test_the_page_lists_a_session_and_each_patient_in_the_right_group(client, db, clinic, desk):
    doctor, schedule = desk
    await in_queue(db, clinic, doctor, schedule, 1, 1, "in_room")
    await in_queue(db, clinic, doctor, schedule, 2, 2, "waiting")
    await in_queue(db, clinic, doctor, schedule, 3, 3, "served")
    await booked_today(db, clinic, doctor, schedule, await patient(db, 4), "15:00")
    r = await client.get(PAGE)
    assert r.status_code == 200
    for text in ("Dr Queue", "Shifa International", "12:00-18:00", "Places used: 4 of 16"):
        assert text in r.text
    for name in ("Patient 1", "Patient 2", "Patient 3", "Patient 4"):
        assert name in r.text
    for button in ("Mark seen", "Put back", "Send in", "Move up", "Make next", "Check in"):
        assert button in r.text


async def test_the_page_has_a_queue_item_in_the_admin_menu(client, desk):
    assert 'href="/en/admin/queue"' in (await client.get(PAGE)).text
    assert 'href="/en/admin/queue"' in (await client.get("/en/admin")).text


async def test_an_admin_with_no_sessions_today_is_told_so(client, db, clinic):
    await make_admin(db, clinic)
    await login(client, "queue.admin@x.com")
    r = await client.get(PAGE)
    assert r.status_code == 200 and "No doctor sessions today" in r.text


async def test_no_passport_number_is_on_the_page(client, db, clinic, desk):
    doctor, schedule = desk
    appointment = await in_queue(db, clinic, doctor, schedule, 1, 1)
    queued = await db.get(Patient, appointment.patient_id)
    r = await client.get(PAGE)
    assert "Patient 1" in r.text and queued.passport_no not in r.text


async def test_names_are_escaped(client, db, clinic, desk):
    doctor, schedule = desk
    await in_queue(db, clinic, doctor, schedule, 1, 1, name="<b>Pat</b>")
    r = await client.get(PAGE)
    assert "<b>Pat</b>" not in r.text and "&lt;b&gt;Pat&lt;/b&gt;" in r.text


async def test_the_urdu_page_is_right_to_left(client, db, clinic, desk):
    doctor, schedule = desk
    await in_queue(db, clinic, doctor, schedule, 1, 1)
    r = await client.get("/ur/admin/queue")
    assert r.status_code == 200 and 'dir="rtl"' in r.text and "قطار" in r.text
    assert 'href="/en/admin/queue"' in r.text  # the language switch


async def test_a_saved_flag_shows_a_banner_and_an_unknown_one_does_not(client, desk):
    assert "Checked in." in (await client.get(f"{PAGE}?saved=checked_in")).text
    assert "Checked in." not in (await client.get(f"{PAGE}?saved=bogus")).text


# ── Actions ──────────────────────────────────────────────────────────────
async def test_checking_in_redirects_with_a_notice_and_queues_the_patient(client, db, clinic, desk):
    doctor, schedule = desk
    appointment = await booked_today(db, clinic, doctor, schedule, await patient(db, 1))
    r = await client.post(f"{PAGE}/check-in/{appointment.id}")
    assert r.status_code == 303 and r.headers["location"] == f"{PAGE}?saved=checked_in"
    assert (await entry_of(db, appointment)).state == "waiting"


async def test_a_refused_check_in_shows_the_reason(client, db, clinic, desk):
    doctor, schedule = desk
    cancelled = await booked_today(
        db, clinic, doctor, schedule, await patient(db, 1), status="cancelled"
    )
    r = await client.post(f"{PAGE}/check-in/{cancelled.id}")
    assert r.status_code == 409 and "That appointment can't be checked in." in text_of(r)
    assert await entry_of(db, cancelled) is None


async def test_unknown_malformed_and_foreign_ids_go_back_to_the_queue(
    client, db, clinic, second_clinic, desk
):
    doctor, schedule = desk
    other, other_schedule = await todays_session(
        db, second_clinic, email="o.dr@x.com", name="Dr Other"
    )
    foreign = await booked_today(db, second_clinic, other, other_schedule, await patient(db, 1))
    for path in (
        f"{PAGE}/check-in/{uuid.uuid4()}",
        f"{PAGE}/check-in/junk",
        f"{PAGE}/check-in/{foreign.id}",
        f"{PAGE}/{uuid.uuid4()}/send-in",
        f"{PAGE}/junk/seen",
    ):
        r = await client.post(path)
        assert r.status_code == 303 and r.headers["location"] == PAGE
    assert await entry_of(db, foreign) is None


async def test_the_whole_visit_through_the_buttons(client, db, clinic, desk):
    doctor, schedule = desk
    first = await in_queue(db, clinic, doctor, schedule, 1, 1)
    second = await in_queue(db, clinic, doctor, schedule, 2, 2)
    one, two = (await entry_of(db, first)).id, (await entry_of(db, second)).id

    r = await client.post(f"{PAGE}/{one}/send-in")
    assert r.headers["location"] == f"{PAGE}?saved=sent_in"
    r = await client.post(f"{PAGE}/{two}/send-in")  # the room is taken
    assert r.status_code == 409 and "Someone is already in the room" in r.text
    r = await client.post(f"{PAGE}/{one}/put-back")
    assert r.headers["location"] == f"{PAGE}?saved=put_back"
    r = await client.post(f"{PAGE}/{two}/make-next")
    assert r.headers["location"] == f"{PAGE}?saved=moved"
    assert (await entry_of(db, second)).token_number == 1
    r = await client.post(f"{PAGE}/{one}/move-up")
    assert r.headers["location"] == f"{PAGE}?saved=moved"
    assert (await entry_of(db, first)).token_number == 1  # swapped back
    r = await client.post(f"{PAGE}/{one}/send-in")
    r = await client.post(f"{PAGE}/{one}/seen")
    assert r.headers["location"] == f"{PAGE}?saved=seen"
    assert (await entry_of(db, first)).state == "served"


async def test_another_clinics_admin_cannot_act_on_the_queue(client, db, clinic, second_clinic, desk):
    doctor, schedule = desk
    waiting = await in_queue(db, clinic, doctor, schedule, 1, 1)
    await make_admin(db, second_clinic, email="out.admin@x.com")
    client.cookies.clear()
    await login(client, "out.admin@x.com")
    r = await client.post(f"{PAGE}/{(await entry_of(db, waiting)).id}/send-in")
    assert r.status_code == 303 and r.headers["location"] == PAGE
    assert (await entry_of(db, waiting)).state == "waiting"
    assert "Patient 1" not in (await client.get(PAGE)).text


async def test_the_desk_write_rate_limit_applies(client, db, clinic, desk, monkeypatch):
    monkeypatch.setattr(settings, "queue_write_rate_limit_per_minute", 1)
    doctor, schedule = desk
    first = await booked_today(db, clinic, doctor, schedule, await patient(db, 1), "14:00")
    second = await booked_today(db, clinic, doctor, schedule, await patient(db, 2), "14:15")
    assert (await client.post(f"{PAGE}/check-in/{first.id}")).status_code == 303
    r = await client.post(f"{PAGE}/check-in/{second.id}")
    assert r.status_code == 429 and "Too many attempts" in r.text
    assert await entry_of(db, second) is None


# ── Walk-ins ─────────────────────────────────────────────────────────────
async def test_a_name_search_shows_masked_results_for_that_session(client, db, clinic, desk):
    doctor, schedule = desk
    found = await patient(db, 1, "Ayesha Raza")
    key = key_of(doctor, clinic, schedule)
    r = await client.get(PAGE, params={"find": "ayesha", "session": key})
    assert r.status_code == 200 and "Ayesha Raza" in r.text and "Add to queue" in r.text
    assert found.passport_no not in r.text and found.passport_no[-4:] in r.text
    assert "••" in r.text


async def test_an_exact_passport_number_finds_the_patient(client, db, clinic, desk):
    doctor, schedule = desk
    found = await patient(db, 1, "Ayesha Raza")
    key = key_of(doctor, clinic, schedule)
    r = await client.get(PAGE, params={"find": found.passport_no, "session": key})
    assert "Ayesha Raza" in r.text and "••" in r.text
    # the number appears once, in the search box where it was typed, never in a result
    assert r.text.count(found.passport_no) == 1


async def test_a_short_or_empty_search_says_so(client, db, clinic, desk):
    doctor, schedule = desk
    key = key_of(doctor, clinic, schedule)
    r = await client.get(PAGE, params={"find": "ab", "session": key})
    assert "Type at least 3 characters." in r.text
    r = await client.get(PAGE, params={"find": "zzzzzz", "session": key})
    assert "No patient found." in r.text


async def test_the_lookup_rate_limit_shows_a_message_not_an_error(client, db, clinic, desk, monkeypatch):
    monkeypatch.setattr(settings, "queue_lookup_rate_limit_per_minute", 1)
    doctor, schedule = desk
    await patient(db, 1, "Ayesha Raza")
    key = key_of(doctor, clinic, schedule)
    assert "Ayesha Raza" in (await client.get(PAGE, params={"find": "ayesha", "session": key})).text
    r = await client.get(PAGE, params={"find": "ayesha", "session": key})
    assert r.status_code == 200 and "Too many attempts" in r.text


async def test_adding_a_walk_in_queues_them_and_shows_them_in_the_list(client, db, clinic, desk):
    doctor, schedule = desk
    walker = await patient(db, 1, "Ayesha Raza")
    key = key_of(doctor, clinic, schedule)
    r = await client.post(f"{PAGE}/walk-in", data={"session": key, "patient_id": str(walker.id)})
    assert r.status_code == 303 and r.headers["location"] == f"{PAGE}?saved=walk_in"
    appointment = (
        await db.execute(select(Appointment).where(Appointment.patient_id == walker.id))
    ).scalar_one()
    assert appointment.booking_channel == "walk_in"
    page = await client.get(r.headers["location"])
    assert "Ayesha Raza" in page.text and "Walk-in" in page.text
    assert "Walk-in added to the queue." in page.text


async def test_a_walk_in_for_a_full_session_shows_the_reason(client, db, clinic):
    doctor, schedule = await todays_session(db, clinic, cap=1)
    await make_admin(db, clinic)
    await login(client, "queue.admin@x.com")
    await booked_today(db, clinic, doctor, schedule, await patient(db, 1), "14:00")
    walker = await patient(db, 2)
    key = key_of(doctor, clinic, schedule)
    r = await client.post(f"{PAGE}/walk-in", data={"session": key, "patient_id": str(walker.id)})
    assert r.status_code == 409 and "That session is full." in r.text


async def test_a_tampered_walk_in_form_creates_nothing(client, db, clinic, desk):
    doctor, schedule = desk
    walker = await patient(db, 1)
    key = key_of(doctor, clinic, schedule)
    for data in (
        {"session": "junk", "patient_id": str(walker.id)},
        {"session": key, "patient_id": "junk"},
        {"session": key},
        {},
    ):
        r = await client.post(f"{PAGE}/walk-in", data=data)
        assert r.status_code == 303 and r.headers["location"] == PAGE
    assert (await db.execute(select(Appointment))).all() == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_queue_web_admin.py -q`
Expected: failures: 404 for the missing routes.

- [ ] **Step 3: Add the i18n keys**

Run the i18n helper with this keys file:

```json
{
  "en": {
    "nav.queue": "Queue",
    "queue.title": "Queue",
    "queue.admin.empty": "No doctor sessions today at your clinic.",
    "queue.places_used": "Places used: {used} of {cap}",
    "queue.in_room": "In the room",
    "queue.room_empty": "Nobody is in the room.",
    "queue.waiting": "Waiting",
    "queue.waiting_empty": "Nobody is waiting.",
    "queue.expected": "Expected",
    "queue.expected_empty": "Nobody else is expected.",
    "queue.seen_today": "Seen today ({count})",
    "queue.badge.walk_in": "Walk-in",
    "queue.booked_at": "Booked {time}",
    "queue.arrived_at": "Arrived {time}",
    "queue.action.check_in": "Check in",
    "queue.action.send_in": "Send in",
    "queue.action.seen": "Mark seen",
    "queue.action.put_back": "Put back",
    "queue.action.move_up": "Move up",
    "queue.action.make_next": "Make next",
    "queue.walk_in.title": "Add a walk-in",
    "queue.walk_in.hint": "Search by passport number or name (at least 3 characters).",
    "queue.walk_in.search_label": "Passport number or name",
    "queue.walk_in.search_action": "Search",
    "queue.walk_in.add": "Add to queue",
    "queue.walk_in.none": "No patient found.",
    "queue.saved.checked_in": "Checked in.",
    "queue.saved.sent_in": "Sent in.",
    "queue.saved.seen": "Marked as seen.",
    "queue.saved.put_back": "Put back in the queue.",
    "queue.saved.moved": "Queue order updated.",
    "queue.saved.walk_in": "Walk-in added to the queue."
  },
  "ur": {
    "nav.queue": "قطار",
    "queue.title": "قطار",
    "queue.admin.empty": "آج آپ کے کلینک میں کسی ڈاکٹر کا سیشن نہیں۔",
    "queue.places_used": "استعمال شدہ جگہیں: {cap} میں سے {used}",
    "queue.in_room": "کمرے میں",
    "queue.room_empty": "کمرے میں کوئی نہیں۔",
    "queue.waiting": "انتظار میں",
    "queue.waiting_empty": "کوئی انتظار میں نہیں۔",
    "queue.expected": "متوقع",
    "queue.expected_empty": "کسی اور کی توقع نہیں۔",
    "queue.seen_today": "آج دیکھے گئے ({count})",
    "queue.badge.walk_in": "بغیر اپائنٹمنٹ",
    "queue.booked_at": "اپائنٹمنٹ {time} بجے",
    "queue.arrived_at": "آمد {time} بجے",
    "queue.action.check_in": "چیک اِن",
    "queue.action.send_in": "اندر بھیجیں",
    "queue.action.seen": "دیکھا ہوا",
    "queue.action.put_back": "واپس انتظار میں",
    "queue.action.move_up": "اوپر کریں",
    "queue.action.make_next": "اگلا کریں",
    "queue.walk_in.title": "بغیر اپائنٹمنٹ مریض شامل کریں",
    "queue.walk_in.hint": "پاسپورٹ نمبر یا نام سے تلاش کریں (کم از کم 3 حروف)۔",
    "queue.walk_in.search_label": "پاسپورٹ نمبر یا نام",
    "queue.walk_in.search_action": "تلاش کریں",
    "queue.walk_in.add": "قطار میں شامل کریں",
    "queue.walk_in.none": "کوئی مریض نہیں ملا۔",
    "queue.saved.checked_in": "چیک اِن ہو گیا۔",
    "queue.saved.sent_in": "اندر بھیج دیا گیا۔",
    "queue.saved.seen": "دیکھا ہوا نشان زد ہو گیا۔",
    "queue.saved.put_back": "واپس قطار میں رکھ دیا گیا۔",
    "queue.saved.moved": "قطار کی ترتیب بدل دی گئی۔",
    "queue.saved.walk_in": "مریض کو قطار میں شامل کر دیا گیا۔"
  }
}
```

- [ ] **Step 4: Write the page module**

Create `backend/app/web/queue.py`:

```python
"""Server-rendered pages for the live queue (P6, FR19): the clinic admin's desk.

Every rule lives in `queueing/service.py`; this module only turns forms into
service calls and results into pages, mirroring `web/booking.py`. Every action
works without JavaScript: a POST acts and redirects, and a refused action shows
the same page again with the reason. The patient's and the doctor's views of the
queue are small additions to the booking pages (`web/booking.py`, `web/router.py`).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..deps import (
    Actor,
    OptionalActorDep,
    SessionDep,
    check_queue_lookup_rate,
    check_queue_write_rate,
)
from ..errors import NotFound, QueueRefused, RateLimited
from ..i18n.catalogue import normalise_locale, translate
from ..queueing import service
from ..queueing.schemas import PatientHit, SessionKey
from .router import _admin_nav, _admin_page_guard, _render

router = APIRouter(tags=["web-queue"])

# `?saved=` values the queue page understands (anything else is ignored).
_SAVED = {
    "checked_in": "queue.saved.checked_in",
    "sent_in": "queue.saved.sent_in",
    "seen": "queue.saved.seen",
    "put_back": "queue.saved.put_back",
    "moved": "queue.saved.moved",
    "walk_in": "queue.saved.walk_in",
}


def _guard(request: Request, locale: str, actor: Actor | None) -> Response | None:
    """The admin pages' redirects, plus: an admin with no clinic yet goes to their dashboard."""
    if (redirect := _admin_page_guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    if not actor.clinic_ids:
        return RedirectResponse(f"/{normalise_locale(locale)}/admin", status_code=303)
    return None


def _parse_id(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _agent(request: Request) -> str | None:
    return request.headers.get("user-agent")


async def _render_queue(
    request: Request,
    loc: str,
    session: SessionDep,
    actor: Actor,
    *,
    status_code: int = 200,
    saved: str = "",
    page_error: str | None = None,
    find: str = "",
    find_session: str = "",
) -> Response:
    views = await service.admin_sessions(session, actor)
    hits: list[PatientHit] = []
    find_error: str | None = None
    needle = find.strip()
    if needle and SessionKey.decode(find_session) is not None:
        try:
            await check_queue_lookup_rate(actor)
            hits = await service.find_patients(session, actor, needle, user_agent=_agent(request))
        except RateLimited:
            find_error = translate("errors.rate_limited", loc)
        except QueueRefused as exc:
            find_error = translate(exc.message_key, loc)
    key = _SAVED.get(saved)
    return _render(
        request,
        "queue/admin.html",
        loc,
        status_code=status_code,
        path_suffix="/admin/queue",
        crumb=translate("admin.dashboard.title", loc),
        page_title=translate("queue.title", loc),
        nav_items=_admin_nav(loc, current="queue"),
        views=views,
        hits=hits,
        find=needle,
        find_session=find_session,
        find_error=find_error,
        saved=translate(key, loc) if key else None,
        page_error=page_error,
    )


@router.get("/{locale}/admin/queue", response_class=HTMLResponse)
async def queue_page(
    request: Request,
    locale: str,
    session: SessionDep,
    actor: OptionalActorDep,
    saved: str = "",
    find: str = "",
    find_session: Annotated[str, Query(alias="session")] = "",
) -> Response:
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    return await _render_queue(
        request,
        normalise_locale(locale),
        session,
        actor,
        saved=saved,
        find=find[:80],
        find_session=find_session,
    )


async def _run(
    request: Request,
    locale: str,
    session: SessionDep,
    actor: Actor,
    call: Callable[[], Awaitable[None]],
    saved: str,
) -> Response:
    """Rate-limit, run one queue action, and answer: redirect on success, the page
    again with the reason on a refusal, the plain page on an unknown id."""
    loc = normalise_locale(locale)
    try:
        await check_queue_write_rate(actor)
    except RateLimited:
        return await _render_queue(
            request,
            loc,
            session,
            actor,
            status_code=429,
            page_error=translate("errors.rate_limited", loc),
        )
    try:
        await call()
    except QueueRefused as exc:
        await session.rollback()  # let go of the doctor lock before drawing the page
        return await _render_queue(
            request,
            loc,
            session,
            actor,
            status_code=exc.http_status,
            page_error=translate(exc.message_key, loc),
        )
    except NotFound:
        await session.rollback()
        return RedirectResponse(f"/{loc}/admin/queue", status_code=303)
    return RedirectResponse(f"/{loc}/admin/queue?saved={saved}", status_code=303)


@router.post("/{locale}/admin/queue/check-in/{appointment_id}")
async def check_in(
    request: Request,
    locale: str,
    appointment_id: str,
    session: SessionDep,
    actor: OptionalActorDep,
) -> Response:
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    parsed = _parse_id(appointment_id)
    if parsed is None:
        return RedirectResponse(f"/{normalise_locale(locale)}/admin/queue", status_code=303)
    return await _run(
        request,
        locale,
        session,
        actor,
        lambda: service.check_in(session, actor, parsed, user_agent=_agent(request)),
        "checked_in",
    )


async def _entry_action(
    request: Request,
    locale: str,
    entry_id: str,
    session: SessionDep,
    actor: Actor | None,
    action: Callable[..., Awaitable[None]],
    saved: str,
) -> Response:
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    parsed = _parse_id(entry_id)
    if parsed is None:
        return RedirectResponse(f"/{normalise_locale(locale)}/admin/queue", status_code=303)
    return await _run(
        request,
        locale,
        session,
        actor,
        lambda: action(session, actor, parsed, user_agent=_agent(request)),
        saved,
    )


@router.post("/{locale}/admin/queue/{entry_id}/send-in")
async def send_in(
    request: Request, locale: str, entry_id: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    return await _entry_action(request, locale, entry_id, session, actor, service.send_in, "sent_in")


@router.post("/{locale}/admin/queue/{entry_id}/seen")
async def seen(
    request: Request, locale: str, entry_id: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    return await _entry_action(request, locale, entry_id, session, actor, service.mark_seen, "seen")


@router.post("/{locale}/admin/queue/{entry_id}/put-back")
async def put_back(
    request: Request, locale: str, entry_id: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    return await _entry_action(
        request, locale, entry_id, session, actor, service.put_back, "put_back"
    )


@router.post("/{locale}/admin/queue/{entry_id}/move-up")
async def move_up(
    request: Request, locale: str, entry_id: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    return await _entry_action(request, locale, entry_id, session, actor, service.move_up, "moved")


@router.post("/{locale}/admin/queue/{entry_id}/make-next")
async def make_next(
    request: Request, locale: str, entry_id: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    return await _entry_action(
        request, locale, entry_id, session, actor, service.make_next, "moved"
    )


@router.post("/{locale}/admin/queue/walk-in")
async def walk_in(
    request: Request,
    locale: str,
    session: SessionDep,
    actor: OptionalActorDep,
    session_key: Annotated[str, Form(alias="session")] = "",
    patient_id: Annotated[str, Form()] = "",
) -> Response:
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    key, patient = SessionKey.decode(session_key), _parse_id(patient_id)
    if key is None or patient is None:
        return RedirectResponse(f"/{normalise_locale(locale)}/admin/queue", status_code=303)
    return await _run(
        request,
        locale,
        session,
        actor,
        lambda: service.add_walk_in(session, actor, key, patient, user_agent=_agent(request)),
        "walk_in",
    )
```

Create `frontend/templates/queue/admin.html`:

```html
{% extends "base.html" %}
{#- The clinic admin's queue desk (FR19, P6 spec 3.3). Every control is a form
    (post, then redirect), so it works without JavaScript. Names are autoescaped;
    never use |safe here. The page does not refresh itself: the desk is what
    changes it.

    Context (built in web/queue.py::_render_queue):
      views        list[SessionView] — today's sessions at the admin's clinics
      hits         list[PatientHit] — walk-in search results (passport masked)
      find, find_session, find_error — the search text, the session it was for, its error
      saved, page_error — banner texts or None -#}

{% block title %}{{ t('queue.title') }} · {{ t('app.name') }}{% endblock %}

{% macro act(path, label, style='secondary') %}
  <form method="post" action="/{{ locale }}/admin/queue/{{ path }}" style="display:inline">
    <button class="btn btn--{{ style }} btn--sm" type="submit" data-label="{{ label }}">{{ label }}</button>
  </form>
{% endmacro %}

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

    {% if not views %}
      <div class="panel"><p class="empty-state">{{ t('queue.admin.empty') }}</p></div>
    {% endif %}

    {% for v in views %}
      {% set key = v.key.encode() %}
      <section class="panel stack" aria-labelledby="s-{{ loop.index }}">
        <h2 id="s-{{ loop.index }}" class="panel__label">
          {{ v.doctor_name }} · {{ v.clinic_name }} ·
          <bdi dir="ltr">{{ v.start.strftime('%H:%M') }}-{{ v.end.strftime('%H:%M') }}</bdi>
        </h2>
        <p class="field-help">{{ t('queue.places_used', used=v.used, cap=v.cap) }}</p>

        <div>
          <div class="eyebrow">{{ t('queue.in_room') }}</div>
          {% if v.in_room %}
            <div class="entry-row">
              <div class="entry-row__main">
                <div class="entry-row__title">{{ v.in_room.patient_name }}
                  {% if v.in_room.walk_in %}<span class="badge badge--neutral">{{ t('queue.badge.walk_in') }}</span>{% endif %}
                </div>
              </div>
              <div class="entry-row__actions">
                {{ act(v.in_room.entry_id ~ '/seen', t('queue.action.seen'), 'primary') }}
                {{ act(v.in_room.entry_id ~ '/put-back', t('queue.action.put_back')) }}
              </div>
            </div>
          {% else %}
            <p class="field-help">{{ t('queue.room_empty') }}</p>
          {% endif %}
        </div>

        <div>
          <div class="eyebrow">{{ t('queue.waiting') }}</div>
          {% if v.waiting %}
            <ol class="entry-list">
              {% for line in v.waiting %}
                <li class="entry-row">
                  <div class="entry-row__main">
                    <div class="entry-row__title">#{{ loop.index }} {{ line.patient_name }}
                      {% if line.walk_in %}<span class="badge badge--neutral">{{ t('queue.badge.walk_in') }}</span>{% endif %}
                    </div>
                    <div class="mono" dir="ltr">
                      {% if line.walk_in %}{{ t('queue.arrived_at', time=line.booked_at.strftime('%H:%M')) }}{% else %}{{ t('queue.booked_at', time=line.booked_at.strftime('%H:%M')) }}{% endif %}
                    </div>
                  </div>
                  <div class="entry-row__actions">
                    {{ act(line.entry_id ~ '/send-in', t('queue.action.send_in'), 'primary') }}
                    {{ act(line.entry_id ~ '/move-up', t('queue.action.move_up')) }}
                    {{ act(line.entry_id ~ '/make-next', t('queue.action.make_next')) }}
                  </div>
                </li>
              {% endfor %}
            </ol>
          {% else %}
            <p class="field-help">{{ t('queue.waiting_empty') }}</p>
          {% endif %}
        </div>

        <div>
          <div class="eyebrow">{{ t('queue.expected') }}</div>
          {% if v.expected %}
            <ul class="entry-list">
              {% for line in v.expected %}
                <li class="entry-row">
                  <div class="entry-row__main">
                    <div class="entry-row__title">{{ line.patient_name }}</div>
                    <div class="mono" dir="ltr">{{ t('queue.booked_at', time=line.booked_at.strftime('%H:%M')) }}</div>
                  </div>
                  <div class="entry-row__actions">
                    {{ act('check-in/' ~ line.appointment_id, t('queue.action.check_in'), 'primary') }}
                  </div>
                </li>
              {% endfor %}
            </ul>
          {% else %}
            <p class="field-help">{{ t('queue.expected_empty') }}</p>
          {% endif %}
        </div>

        {% if v.seen %}
          <details>
            <summary class="eyebrow">{{ t('queue.seen_today', count=v.seen|length) }}</summary>
            <ul class="entry-list">
              {% for line in v.seen %}<li>{{ line.patient_name }}</li>{% endfor %}
            </ul>
          </details>
        {% endif %}

        <div id="walkin-{{ loop.index }}" class="stack">
          <div class="eyebrow">{{ t('queue.walk_in.title') }}</div>
          <p class="field-help">{{ t('queue.walk_in.hint') }}</p>
          <form class="stack" method="get" action="/{{ locale }}/admin/queue#walkin-{{ loop.index }}" role="search">
            <input type="hidden" name="session" value="{{ key }}">
            <label class="field" for="find-{{ loop.index }}">
              <span class="field-label">{{ t('queue.walk_in.search_label') }}</span>
              <input class="input" id="find-{{ loop.index }}" name="find" type="search" maxlength="80"
                     value="{% if find_session == key %}{{ find }}{% endif %}">
            </label>
            <div><button class="btn btn--secondary btn--sm" type="submit">{{ t('queue.walk_in.search_action') }}</button></div>
          </form>
          {% if find_session == key %}
            {% if find_error %}
              <span class="field-error" role="alert"><span aria-hidden="true">!</span>{{ find_error }}</span>
            {% elif hits %}
              <ul class="grant-list">
                {% for hit in hits %}
                  <li class="grant-card stack">
                    <div class="entry-row__main">
                      <div class="entry-row__title">{{ hit.name }}</div>
                      <div class="mono" dir="ltr">{{ hit.passport }}</div>
                    </div>
                    <form method="post" action="/{{ locale }}/admin/queue/walk-in">
                      <input type="hidden" name="session" value="{{ key }}">
                      <input type="hidden" name="patient_id" value="{{ hit.patient_id }}">
                      <button class="btn btn--primary btn--sm" type="submit" data-label="{{ t('queue.walk_in.add') }}">{{ t('queue.walk_in.add') }}</button>
                    </form>
                  </li>
                {% endfor %}
              </ul>
            {% elif find %}
              <p class="empty-state">{{ t('queue.walk_in.none') }}</p>
            {% endif %}
          {% endif %}
        </div>
      </section>
    {% endfor %}
  </main>
</div>
{% endblock %}
```

- [ ] **Step 5: Wire the router and the admin menu**

In `backend/app/main.py`, next to the other web imports add `from app.web.queue import router as web_queue_router`, and add `app.include_router(web_queue_router)` right after `app.include_router(web_booking_router)`.

In `backend/app/web/router.py`, replace `_admin_nav` with:

```python
def _admin_nav(loc: str, *, current: str) -> list[dict[str, Any]]:
    items = [
        {
            "href": f"/{loc}/admin?tab={t}",
            "label": translate(f"verify.tab.{t}", loc),
            "current": current == t,
        }
        for t in _ADMIN_TABS
    ]
    items.append(
        {
            "href": f"/{loc}/admin/queue",
            "label": translate("nav.queue", loc),
            "current": current == "queue",
        }
    )
    return items
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_queue_web_admin.py -q`
Expected: all pass (about 30). If the admin-with-no-clinic test cannot sign in, create that admin the way `tests/test_admin_register.py` does and keep the assertion.

- [ ] **Step 7: Lint, run the nearby suites, and commit**

```bash
uv run ruff check backend tests --fix && uv run ruff format backend tests
uv run pytest tests/test_queue_*.py tests/test_verification.py tests/test_admin_register.py tests/test_web.py -q
git add backend/app/web/queue.py backend/app/web/router.py backend/app/main.py frontend/templates/queue backend/app/i18n tests/test_queue_web_admin.py
git commit -m "feat(queue): the clinic admin's queue page"
```

---

### Task 8: The patient's queue card and badge

**Files:**
- Create: `frontend/templates/queue/_card.html`, `frontend/static/js/queue.js`
- Modify: `backend/app/web/booking.py` (`appointment_page`, `_render_list`), `frontend/templates/booking/appointment.html`, `frontend/templates/booking/_row.html`, `backend/app/i18n/messages/en.json`, `ur.json`
- Test: `tests/test_queue_web_patient.py`

**Interfaces:**
- Consumes: `queueing.service.patient_status`, `patient_badges` (Task 4); `QueueStatus`; `booking.display.hhmm`; `booking.service.now`.
- Produces: the queue card on `GET /{locale}/patient/appointments/{id}` (context `queue_status`, `updated_at`); a badge per today's queued row on the appointments list (context `queue_badges`: `dict[UUID, QueueStatus]`); `queue.js` reloads a page that carries `data-autorefresh`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_queue_web_patient.py`:

```python
"""P6 web: the patient's queue card and the badge on the appointments list."""

from __future__ import annotations

from datetime import date

import pytest
from tests.booking_helpers import add_appointment, at, freeze, login, make_patient
from tests.queue_helpers import add_entry, booked_today, make_admin, todays_session

LIST = "/en/patient/appointments"


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    freeze(monkeypatch)


async def person(db, n: int, name: str | None = None):
    _, patient, actor = await make_patient(db, email=f"c{n}@x.com", name=name or f"Patient {n}")
    return patient, actor


@pytest.fixture
async def room(db, clinic):
    doctor, schedule = await todays_session(db, clinic)
    await make_admin(db, clinic)
    return doctor, schedule


async def sign_in(client, n: int):
    await login(client, f"c{n}@x.com")


def card(response) -> str:
    return response.text.replace("&#39;", "'")


async def test_a_booked_patient_who_has_not_arrived_is_told_to_tell_the_desk(client, db, clinic, room):
    doctor, schedule = room
    patient, _ = await person(db, 1)
    appointment = await booked_today(db, clinic, doctor, schedule, patient)
    await sign_in(client, 1)
    r = await client.get(f"{LIST}/{appointment.id}")
    assert "Your place in the queue" in r.text
    assert "Not checked in yet. Tell the front desk when you arrive." in r.text
    assert 'data-autorefresh="15000"' in r.text and "/static/js/queue.js" in r.text


async def test_the_card_shows_position_and_wait_without_other_names(client, db, clinic, room):
    doctor, schedule = room
    people = [await person(db, n) for n in (1, 2, 3, 4)]
    appointments = [
        await booked_today(db, clinic, doctor, schedule, p, f"14:{n * 15:02d}")
        for n, (p, _) in enumerate(people)
    ]
    await add_entry(db, appointments[0], 1, "in_room")
    for token, appointment in enumerate(appointments[1:], start=2):
        await add_entry(db, appointment, token)
    await sign_in(client, 4)
    r = await client.get(f"{LIST}/{appointments[3].id}")
    assert "You are #3 in line, about 45 minutes." in r.text
    assert "People waiting: 3" in r.text
    for other in ("Patient 1", "Patient 2", "Patient 3"):
        assert other not in r.text
    assert "Last updated 13:00" in r.text and "Refresh" in r.text


async def test_the_next_patient_is_told_to_be_ready(client, db, clinic, room):
    doctor, schedule = room
    patient, _ = await person(db, 1)
    appointment = await booked_today(db, clinic, doctor, schedule, patient)
    await add_entry(db, appointment, 1)
    await sign_in(client, 1)
    assert "You're next. Please be ready." in card(await client.get(f"{LIST}/{appointment.id}"))


async def test_in_the_room_and_seen(client, db, clinic, room):
    doctor, schedule = room
    (p1, _), (p2, _) = await person(db, 1), await person(db, 2)
    a1 = await booked_today(db, clinic, doctor, schedule, p1, "14:00")
    a2 = await booked_today(db, clinic, doctor, schedule, p2, "14:15")
    await add_entry(db, a1, 1, "in_room")
    await add_entry(db, a2, 2, "served")
    await sign_in(client, 1)
    inside = await client.get(f"{LIST}/{a1.id}")
    assert "It's your turn. Please go in." in card(inside) and "data-autorefresh" in inside.text
    client.cookies.clear()
    await sign_in(client, 2)
    done = await client.get(f"{LIST}/{a2.id}")
    assert "Seen. Thank you." in done.text
    assert "data-autorefresh" not in done.text  # nothing left to wait for


async def test_there_is_no_card_for_another_day_or_a_cancelled_booking(client, db, clinic, room):
    doctor, schedule = room
    patient, _ = await person(db, 1)
    later = await add_appointment(
        db, patient, doctor, clinic, schedule, at(date(2026, 10, 19), "14:00")
    )
    gone = await booked_today(db, clinic, doctor, schedule, patient, "14:15", status="cancelled")
    await sign_in(client, 1)
    for appointment in (later, gone):
        r = await client.get(f"{LIST}/{appointment.id}")
        assert r.status_code == 200 and "Your place in the queue" not in r.text


async def test_the_card_is_in_urdu_too(client, db, clinic, room):
    doctor, schedule = room
    patient, _ = await person(db, 1)
    appointment = await booked_today(db, clinic, doctor, schedule, patient)
    await add_entry(db, appointment, 1)
    await sign_in(client, 1)
    r = await client.get(f"/ur/patient/appointments/{appointment.id}")
    assert r.status_code == 200 and 'dir="rtl"' in r.text and "قطار" in r.text
    assert "Your place in the queue" not in r.text


async def test_someone_elses_appointment_still_goes_to_the_list(client, db, clinic, room):
    doctor, schedule = room
    (p1, _), _ = await person(db, 1), await person(db, 2)
    appointment = await booked_today(db, clinic, doctor, schedule, p1)
    await sign_in(client, 2)
    r = await client.get(f"{LIST}/{appointment.id}")
    assert r.status_code == 303 and r.headers["location"] == LIST


async def test_the_list_shows_a_badge_for_todays_queued_rows_only(client, db, clinic, room):
    doctor, schedule = room
    patient, _ = await person(db, 1)
    a1 = await booked_today(db, clinic, doctor, schedule, patient, "14:00")
    await add_entry(db, a1, 1)
    await add_appointment(db, patient, doctor, clinic, schedule, at(date(2026, 10, 19), "14:00"))
    await sign_in(client, 1)
    r = await client.get(LIST)
    assert r.text.count("In queue #1") == 1


async def test_the_badge_for_someone_in_the_room(client, db, clinic, room):
    doctor, schedule = room
    patient, _ = await person(db, 1)
    appointment = await booked_today(db, clinic, doctor, schedule, patient)
    await add_entry(db, appointment, 1, "in_room")
    await sign_in(client, 1)
    assert "In the room" in (await client.get(LIST)).text

```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_queue_web_patient.py -q`
Expected: failures: the card text and badge are missing.

- [ ] **Step 3: Add the i18n keys**

Run the i18n helper with this keys file:

```json
{
  "en": {
    "queue.card.title": "Your place in the queue",
    "queue.card.expected": "Not checked in yet. Tell the front desk when you arrive.",
    "queue.card.waiting": "You are #{position} in line, about {minutes} minutes.",
    "queue.card.next": "You're next. Please be ready.",
    "queue.card.in_room": "It's your turn. Please go in.",
    "queue.card.served": "Seen. Thank you.",
    "queue.card.waiting_total": "People waiting: {count}",
    "queue.card.updated": "Last updated {time}",
    "queue.card.refresh": "Refresh",
    "queue.badge.waiting": "In queue #{position}",
    "queue.badge.in_room": "In the room"
  },
  "ur": {
    "queue.card.title": "قطار میں آپ کی جگہ",
    "queue.card.expected": "ابھی چیک اِن نہیں ہوا۔ پہنچنے پر فرنٹ ڈیسک کو بتائیں۔",
    "queue.card.waiting": "آپ قطار میں {position} نمبر پر ہیں، تقریباً {minutes} منٹ۔",
    "queue.card.next": "اگلی باری آپ کی ہے۔ براہ کرم تیار رہیں۔",
    "queue.card.in_room": "آپ کی باری ہے۔ براہ کرم اندر جائیں۔",
    "queue.card.served": "آپ کو دیکھ لیا گیا۔ شکریہ۔",
    "queue.card.waiting_total": "انتظار میں لوگ: {count}",
    "queue.card.updated": "آخری اپ ڈیٹ {time}",
    "queue.card.refresh": "ریفریش",
    "queue.badge.waiting": "قطار میں نمبر {position}",
    "queue.badge.in_room": "کمرے میں"
  }
}
```

- [ ] **Step 4: Write the card, the script and the hooks**

Create `frontend/templates/queue/_card.html`:

```html
{#- The patient's place in today's queue (FR17, P6 spec 3.3). Expects `queue_status`
    (a QueueStatus), `updated_at` (HH:MM) and `row` (the AppointmentRow). It never
    shows another patient's name. Works without JavaScript: the Refresh link
    reloads the page, and queue.js does it every 15 seconds while `live`. -#}
<div class="panel stack" aria-live="polite">
  <h2 class="panel__label">{{ t('queue.card.title') }}</h2>
  {% if queue_status.state == 'expected' %}
    <p class="lead">{{ t('queue.card.expected') }}</p>
  {% elif queue_status.state == 'waiting' %}
    <p class="lead">{{ t('queue.card.waiting', position=queue_status.position, minutes=queue_status.wait_minutes) }}</p>
  {% elif queue_status.state == 'next' %}
    <p class="lead">{{ t('queue.card.next') }}</p>
  {% elif queue_status.state == 'in_room' %}
    <p class="lead">{{ t('queue.card.in_room') }}</p>
  {% else %}
    <p class="lead">{{ t('queue.card.served') }}</p>
  {% endif %}
  {% if queue_status.live %}
    <p class="field-help">
      {% if queue_status.state in ('waiting', 'next') %}{{ t('queue.card.waiting_total', count=queue_status.waiting_total) }} · {% endif %}{{ t('queue.card.updated', time=updated_at) }}
    </p>
    <p><a class="btn btn--secondary btn--sm" href="/{{ locale }}/patient/appointments/{{ row.id }}">{{ t('queue.card.refresh') }}</a></p>
  {% endif %}
</div>
```

Create `frontend/static/js/queue.js`:

```js
/* Live queue (P6): a page that carries data-autorefresh reloads itself, so the
   patient's place stays current. Without JavaScript the Refresh link does the same. */
(function () {
  "use strict";
  var auto = document.querySelector("[data-autorefresh]");
  if (!auto) return;
  var ms = parseInt(auto.getAttribute("data-autorefresh"), 10) || 15000;
  window.setTimeout(function () {
    window.location.reload();
  }, ms);
})();
```

In `frontend/templates/booking/appointment.html` make three edits:

1. Replace `  <main class="content">` with:

```html
  <main class="content"{% if queue_status and queue_status.live %} data-autorefresh="15000"{% endif %}>
```

2. Replace

```html
    {% endif %}

    <div class="panel stack">
      <h2 class="panel__label">{{ t('booking.appointment.title') }}</h2>
```

with

```html
    {% endif %}

    {% if queue_status %}
      {% include "queue/_card.html" %}
    {% endif %}

    <div class="panel stack">
      <h2 class="panel__label">{{ t('booking.appointment.title') }}</h2>
```

3. Replace the last lines `  </main>\n</div>\n{% endblock %}` with:

```html
  </main>
</div>
{% if queue_status and queue_status.live %}<script src="/static/js/queue.js" defer></script>{% endif %}
{% endblock %}
```

In `frontend/templates/booking/_row.html`, directly after the status badge line (`<span class="badge {{ status_badge(r.status) }}">...</span>`), add:

```html
  {% if queue_badges and queue_badges.get(r.id) %}
    {% set qb = queue_badges.get(r.id) %}
    {% if qb.state in ('waiting', 'next') %}
      <span class="badge badge--warn">{{ t('queue.badge.waiting', position=qb.position) }}</span>
    {% elif qb.state == 'in_room' %}
      <span class="badge badge--ok">{{ t('queue.badge.in_room') }}</span>
    {% endif %}
  {% endif %}
```

In `backend/app/web/booking.py`: add `from ..queueing import service as queue_service` to the imports; in `_render_list`, after `upcoming, past = await service.my_appointments(session, actor)` add `badges = await queue_service.patient_badges(session, actor)` and pass `queue_badges=badges,` into `_render(...)`; in `appointment_page`, after the `row` has been loaded (after the `except NotFound` block) add:

```python
    queue_status = await queue_service.patient_status(session, actor, row.id)
```

and pass these two into the `_render(...)` call there: `queue_status=queue_status,` and `updated_at=display.hhmm(service.now()),`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_queue_web_patient.py tests/test_booking_web_appointments.py -q`
Expected: all pass (about 35).

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check backend tests --fix && uv run ruff format backend tests
uv run pytest tests/test_queue_*.py tests/test_booking_*.py -q
git add backend/app/web/booking.py frontend/templates/queue/_card.html frontend/templates/booking frontend/static/js/queue.js backend/app/i18n tests/test_queue_web_patient.py
git commit -m "feat(queue): the patient's live place in the queue"
```

---

### Task 9: The doctor's view of their queue

**Files:**
- Create: `frontend/templates/queue/_doctor_queue.html`
- Modify: `backend/app/web/booking.py` (`doctor_day_page`), `backend/app/web/router.py` (`_guarded`, one import), `frontend/templates/booking/doctor_day.html`, `frontend/templates/shell.html`, `backend/app/i18n/messages/en.json`, `ur.json`
- Test: `tests/test_queue_web_doctor.py`

**Interfaces:**
- Consumes: `queueing.service.doctor_queue` (Task 4); `SessionView`, `Line`.
- Produces: a "Queue today" panel on the doctor's home page (context `queue_today`) and at the top of `/{locale}/doctor/appointments` when the chosen day is today (context `queue_views`). View only: names, walk-in flag and times.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_queue_web_doctor.py`:

```python
"""P6 web: the doctor's view-only queue."""

from __future__ import annotations

import pytest
from app.db.models import UserRole

from tests.booking_helpers import add_session, freeze, login, make_patient, verified_doctor
from tests.conftest import make_user
from tests.queue_helpers import add_entry, booked_today, todays_session


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    freeze(monkeypatch)


@pytest.fixture
async def mine(client, db, clinic):
    doctor, schedule = await todays_session(db, clinic, email="queue.dr@x.com")
    await login(client, "queue.dr@x.com")
    return doctor, schedule


async def person(db, n: int, name: str | None = None):
    _, patient, _ = await make_patient(db, email=f"q{n}@x.com", name=name or f"Patient {n}")
    return patient


async def queue_of(db, clinic, doctor, schedule):
    """One patient in the room, two waiting (the second a walk-in), one expected."""
    room = await booked_today(db, clinic, doctor, schedule, await person(db, 1), "14:00")
    first = await booked_today(db, clinic, doctor, schedule, await person(db, 2), "14:15")
    walker = await booked_today(db, clinic, doctor, schedule, await person(db, 3), "13:07")
    walker.booking_channel = "walk_in"
    await db.commit()
    await booked_today(db, clinic, doctor, schedule, await person(db, 4), "15:30")
    await add_entry(db, room, 1, "in_room")
    await add_entry(db, first, 2)
    await add_entry(db, walker, 3)


async def test_the_dashboard_shows_the_queue_in_order(client, db, clinic, mine):
    doctor, schedule = mine
    await queue_of(db, clinic, doctor, schedule)
    r = await client.get("/en/doctor")
    assert r.status_code == 200 and "Queue today" in r.text
    text = r.text
    assert text.index("Patient 2") < text.index("Patient 3")  # waiting, in order
    assert "Patient 1" in text and "Patient 4" in text and "Walk-in" in text
    assert "Shifa International" in text and "12:00-18:00" in text


async def test_a_pending_doctor_gets_no_queue_panel(client, db, clinic):
    await make_user(db, email="free.dr@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    await login(client, "free.dr@x.com")
    r = await client.get("/en/doctor")
    assert r.status_code == 200 and "Queue today" not in r.text


async def test_a_verified_doctor_with_no_session_is_told_so(client, db, clinic):
    doctor = await verified_doctor(db, clinic, email="wed.dr@x.com", name="Dr Wed")
    await add_session(db, doctor, clinic, weekday=2)  # Wednesdays only
    await login(client, "wed.dr@x.com")
    r = await client.get("/en/doctor")
    assert "Queue today" in r.text and "No sessions today." in r.text


async def test_the_appointments_page_shows_the_queue_for_today_only(client, db, clinic, mine):
    doctor, schedule = mine
    await queue_of(db, clinic, doctor, schedule)
    today = await client.get("/en/doctor/appointments")
    assert "Queue today" in today.text and "Patient 3" in today.text
    other_day = await client.get("/en/doctor/appointments?date=2026-10-14")
    assert "Queue today" not in other_day.text


async def test_another_doctors_patients_are_not_in_my_queue(client, db, clinic, mine):
    doctor, schedule = mine
    rival, rival_schedule = await todays_session(db, clinic, email="r.dr@x.com", name="Dr Rival")
    entry = await booked_today(db, clinic, rival, rival_schedule, await person(db, 9, "Secret Person"))
    await add_entry(db, entry, 1)
    assert "Secret Person" not in (await client.get("/en/doctor")).text
    assert "Secret Person" not in (await client.get("/en/doctor/appointments")).text


async def test_no_buttons_and_no_passport_numbers_for_the_doctor(client, db, clinic, mine):
    doctor, schedule = mine
    await queue_of(db, clinic, doctor, schedule)
    patient = await person(db, 1)
    page = (await client.get("/en/doctor")).text
    for word in ("Send in", "Mark seen", "Check in", "Move up", "Make next", "Put back"):
        assert word not in page
    assert patient.passport_no not in page


async def test_names_are_escaped_in_the_doctors_queue(client, db, clinic, mine):
    doctor, schedule = mine
    entry = await booked_today(db, clinic, doctor, schedule, await person(db, 1, "<i>Pat</i>"))
    await add_entry(db, entry, 1)
    page = (await client.get("/en/doctor")).text
    assert "<i>Pat</i>" not in page and "&lt;i&gt;Pat&lt;/i&gt;" in page


async def test_the_urdu_queue_is_right_to_left(client, db, clinic, mine):
    doctor, schedule = mine
    await queue_of(db, clinic, doctor, schedule)
    r = await client.get("/ur/doctor")
    assert r.status_code == 200 and 'dir="rtl"' in r.text and "آج کی قطار" in r.text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_queue_web_doctor.py -q`
Expected: failures: "Queue today" is missing.

- [ ] **Step 3: Add the i18n keys**

Run the i18n helper with this keys file:

```json
{
  "en": {
    "queue.doctor.title": "Queue today",
    "queue.doctor.empty": "No sessions today."
  },
  "ur": {
    "queue.doctor.title": "آج کی قطار",
    "queue.doctor.empty": "آج کوئی سیشن نہیں۔"
  }
}
```

- [ ] **Step 4: Write the partial and the hooks**

Create `frontend/templates/queue/_doctor_queue.html`:

```html
{#- A verified doctor's queue today, view only (FR24, P6 spec 3.3): who is in the
    room, the waiting patients in order, and those still expected. Names and times
    only. Expects `queue_views` (list[SessionView]). Names are autoescaped. -#}
<div class="panel stack" style="margin-top:var(--sp-5)">
  <div class="panel__label">{{ t('queue.doctor.title') }}</div>
  {% if queue_views %}
    {% for v in queue_views %}
      <section class="stack">
        <div class="eyebrow">
          {{ v.clinic_name }} · <bdi dir="ltr">{{ v.start.strftime('%H:%M') }}-{{ v.end.strftime('%H:%M') }}</bdi>
        </div>
        <p class="field-help">
          {{ t('queue.in_room') }}:
          {% if v.in_room %}{{ v.in_room.patient_name }}{% else %}{{ t('queue.room_empty') }}{% endif %}
        </p>
        {% if v.waiting %}
          <ol>
            {% for line in v.waiting %}
              <li>{{ line.patient_name }}
                {% if line.walk_in %}<span class="badge badge--neutral">{{ t('queue.badge.walk_in') }}</span>{% endif %}
              </li>
            {% endfor %}
          </ol>
        {% else %}
          <p class="field-help">{{ t('queue.waiting_empty') }}</p>
        {% endif %}
        {% if v.expected %}
          <div class="eyebrow">{{ t('queue.expected') }}</div>
          <ul>
            {% for line in v.expected %}
              <li>{{ line.patient_name }} · <span class="mono" dir="ltr">{{ line.booked_at.strftime('%H:%M') }}</span></li>
            {% endfor %}
          </ul>
        {% endif %}
      </section>
    {% endfor %}
  {% else %}
    <p class="empty-state">{{ t('queue.doctor.empty') }}</p>
  {% endif %}
</div>
```

In `frontend/templates/booking/doctor_day.html`, replace

```html
    <div class="panel">
      <div class="panel__label">{{ weekday_name }} <bdi>{{ day.isoformat() }}</bdi></div>
```

with

```html
    {% if queue_views is defined %}
      {% include "queue/_doctor_queue.html" %}
    {% endif %}

    <div class="panel">
      <div class="panel__label">{{ weekday_name }} <bdi>{{ day.isoformat() }}</bdi></div>
```

In `frontend/templates/shell.html`, replace

```html
    {% if patients is defined %}
      {% include "doctor/_patient_list.html" %}
    {% endif %}
```

with

```html
    {% if queue_today is defined %}
      {% set queue_views = queue_today %}
      {% include "queue/_doctor_queue.html" %}
    {% endif %}

    {% if patients is defined %}
      {% include "doctor/_patient_list.html" %}
    {% endif %}
```

In `backend/app/web/booking.py`, in `doctor_day_page`, after `found` has been loaded (after the `except NotFound` block) add:

```python
    queue_views = await queue_service.doctor_queue(session, actor) if day == service.current_day() else None
```

and pass `**({"queue_views": queue_views} if queue_views is not None else {}),` into the `_render(...)` call (so the partial is only included on today's page, and `queue_views is defined` is false on other days).

In `backend/app/web/router.py`: add `from ..queueing import service as queue_service` next to the other service imports; in `_guarded`, next to `appointments_today = None` add `queue_today = None`; directly after the `appointments_today = booking_display.build_day_rows(...)` statement inside the `elif actor.is_verified_doctor:` branch add:

```python
        # P6: the doctor's queue today, view only.
        queue_today = await queue_service.doctor_queue(session, actor)
```

and where `extra` is filled, after the `appointments_today` lines, add:

```python
    if queue_today is not None:
        extra["queue_today"] = queue_today
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_queue_web_doctor.py -q`
Expected: all pass (about 9).

- [ ] **Step 6: Lint, run the doctor and dashboard suites, and commit**

```bash
uv run ruff check backend tests --fix && uv run ruff format backend tests
uv run pytest tests/test_queue_*.py tests/test_booking_*.py tests/test_doctor_dashboard.py tests/test_schedule_web.py tests/test_verification.py -q
git add backend/app/web/booking.py backend/app/web/router.py frontend/templates backend/app/i18n tests/test_queue_web_doctor.py
git commit -m "feat(queue): the doctor's view-only queue"
```

---

### Task 10: Quality gate, documentation, manual check, review, hand-off

**Files:**
- Create: `tests/test_queue_i18n.py`
- Modify: `CLAUDE.md`, `docs/superpowers/specs/2026-10-13-live-queue-checkin-design.md` (the three amendments at the top of this plan)
- No application code changes unless a gate step finds a defect.

**Interfaces:**
- Consumes: everything from Tasks 1 to 9.
- Produces: a green branch, a documented feature, a spec that matches what was built, a review record and a hand-off message. **Nothing is pushed or merged in this task without Zarwa's go-ahead, and no database command is run against the shared project.**

- [ ] **Step 1: Write the i18n completeness tests**

Create `tests/test_queue_i18n.py`:

```python
"""P6: every string the queue pages show exists in English and Urdu (FR28)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from app.i18n.catalogue import missing_keys
from app.queueing.schemas import REFUSAL_REASONS

ROOT = Path(__file__).resolve().parents[1]
MESSAGES = ROOT / "backend" / "app" / "i18n" / "messages"
TEMPLATES = ROOT / "frontend" / "templates"
TEMPLATE_FILES = [*sorted((TEMPLATES / "queue").glob("*.html")), TEMPLATES / "booking" / "_row.html"]
PYTHON_FILES = [ROOT / "backend" / "app" / "web" / "queue.py"]

IN_TEMPLATE = re.compile(r"""\bt\(\s*'([a-z0-9_.]+)'""")
IN_PYTHON = re.compile(r'"((?:queue|nav|errors)\.[a-z0-9_.]+)"')
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def catalogue(locale: str) -> dict[str, str]:
    return json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf8"))


def keys_used() -> set[str]:
    used: set[str] = set()
    for path in TEMPLATE_FILES:
        used |= set(IN_TEMPLATE.findall(path.read_text(encoding="utf8")))
    for path in PYTHON_FILES:
        used |= set(IN_PYTHON.findall(path.read_text(encoding="utf8")))
    # Looked up by building the key: `errors.queue_<reason>` (QueueRefused).
    used |= {f"errors.queue_{reason}" for reason in REFUSAL_REASONS}
    return used


def test_the_scan_finds_the_queue_keys():
    used = keys_used()
    assert len(used) > 50
    assert {"queue.title", "queue.card.waiting", "queue.doctor.title", "nav.queue"} <= used


def test_every_key_the_queue_pages_use_exists_in_both_languages():
    en, ur = catalogue("en"), catalogue("ur")
    missing = sorted(k for k in keys_used() if k not in en or k not in ur)
    assert not missing, missing


def test_no_key_is_missing_from_urdu_anywhere():
    assert missing_keys()["ur"] == []


@pytest.mark.parametrize("prefix", ["queue.", "errors.queue_", "nav.queue"])
def test_placeholders_match_between_the_languages(prefix):
    en, ur = catalogue("en"), catalogue("ur")
    for key in (k for k in en if k.startswith(prefix)):
        assert set(PLACEHOLDER.findall(en[key])) == set(PLACEHOLDER.findall(ur[key])), key


def test_the_urdu_text_is_not_a_copy_of_the_english():
    en, ur = catalogue("en"), catalogue("ur")
    same = sorted(k for k in en if k.startswith(("queue.", "errors.queue_")) and en[k] == ur[k])
    assert not same, same
```

- [ ] **Step 2: Run the new tests**

Run: `uv run pytest tests/test_queue_i18n.py -q`
Expected: all pass. If a key is reported missing, add it with the i18n helper (both languages) rather than weakening the test.

- [ ] **Step 3: Run the full gate**

```bash
uv run ruff check backend tests alembic/versions/92b61c6e9db4_booking_active_slot_index.py
uv run ruff format --check backend tests
uv run pytest -q
```

Expected: ruff clean on those paths (never format `alembic/` as a whole); every test passes. Any failure is ours: an older page test can move when the admin menu or the doctor home page gains an item. Fix it and re-run. Record the final pass count for the hand-off message.

- [ ] **Step 4: Make the spec match what was built**

In `docs/superpowers/specs/2026-10-13-live-queue-checkin-design.md`, read the file and make these three edits with the same meaning as the amendments at the top of this plan:
1. Section 3.2, **Check in**: add that a cancelled or done appointment is refused with its own message ("That appointment can't be checked in"), and in section 3.5 replace the "already checked in" message with "that appointment can't be checked in" (a second check-in does nothing, so it never raises an error).
2. Section 3.2, **Walk-in**: `slot_start` is the arrival time but never earlier than **one microsecond after** the session's start (so it never takes the start's grid time); if that exact instant is held, the next free microsecond.
3. Section 3.3, the admin's **Waiting** line: each waiting patient shows the booked time (a walk-in shows its arrival time), not an arrival time for everyone.

- [ ] **Step 5: Document the feature in `CLAUDE.md`**

Make these additive edits (keep the file's CRLF line endings):

1. In "Testing", after the `tests/test_booking_*.py` line add:

```
uv run pytest tests/test_queue_*.py -v     # P6 live queue / check-in / walk-in tests
```

2. In the "High-Level Structure" tree, after the `booking/` line add:

```
    queueing/                  # Live queue, check-in and walk-ins (P6, FR17/FR19/FR24): pure ordering rules, service — web pages only
```

and in the `web/` line, after `booking.py: appointment booking pages;` add ` queue.py: the clinic admin's queue desk;`.

3. In "Key Architectural Decisions", directly before `6. **Security Properties**`, add:

```
5c. **Live queue, check-in and walk-ins (P6, FR17 + FR19 + FR24)**
   - `backend/app/queueing/`: `order.py` (pure: next token, position, wait, move up, make next), `service.py` (every rule and view), `display.py` (`mask_passport`), `schemas.py`. Pages: `web/queue.py` (the clinic admin's desk, `/{locale}/admin/queue`); the patient's queue card is on `/{locale}/patient/appointments/{id}` and a badge on the list (both in `web/booking.py`); the doctor's view-only "Queue today" is on the doctor home page and on `/{locale}/doctor/appointments` (today only). There is no JSON API.
   - No new table and no migration: it maps the shared `queue_entry` table (`QueueEntry`: `appointment_id` UNIQUE, `token_number`, `state` waiting / in_room / served; the database enforces neither unique tokens nor the cap) and writes `appointment` (`booked → checked_in → done`). The columns are pinned by `tests/fixtures/queue_entry_columns.json`, the schema owner's confirmed answer.
   - A queue is one doctor **session** (one effective window on one date at one clinic: `schedule_id` plus the window's time range, as booking counts a session). Every action takes `lock_doctor` first, then re-reads, then writes. Order is arrival order = token order; position = waiting patients with a smaller token + 1; estimated wait = (waiting ahead + 1 if someone is in the room) × the session's `slot_minutes`. Move up / make next permute the existing tokens among the waiting entries, so tokens stay unique.
   - Only the clinic admin acts, only for doctors at their own `actor.clinic_ids`; the doctor and the patient only view. One patient in the room per session. A walk-in is an `appointment` with `booking_channel='walk_in'`, status `checked_in`, the session's `schedule_id`, and `slot_start` = arrival (never earlier than the session start + 1 µs); it counts toward the cap, but `booking.service._doctor_held` ignores it so it never blocks a bookable time. Walk-ins are registered patients only: the search matches a name or an exact passport number and shows the passport masked. **Walk-ins with no account are a REQUIRED follow-up** (it needs a change to the shared patient tables, so the schema owner's approval).
   - v1 is the basic version: no Redis, no instant push, a 15-second page refresh. The scale-up list is in the spec, section 7.
   - Spec: `docs/superpowers/specs/2026-10-13-live-queue-checkin-design.md`; plan: `docs/superpowers/plans/2026-10-13-live-queue-checkin-v1.md`.
```

4. In "Important Files to Understand First", after item 18 add:

```
19. **`backend/app/queueing/order.py`** and **`queueing/service.py`** - Live queue (P6): the pure ordering rules and the locked service on top of booking's sessions and the shared `queue_entry` table. Read the P6 spec first
```

and in "When Making Changes" add:

```
- **Queue changes (P6)**: Read `docs/superpowers/specs/2026-10-13-live-queue-checkin-design.md` first. Put ordering rules in `queueing/order.py` (pure) and every other rule in `queueing/service.py`; take `lock_doctor` before every write and re-read the queue; only the clinic admin acts, scoped to their own `clinic_ids`; keep passport numbers masked and never log search text; never show a patient another patient's name; never hard-delete a queue entry or an appointment.
```

- [ ] **Step 6: Commit the gate**

```bash
git add tests/test_queue_i18n.py CLAUDE.md docs/superpowers/specs/2026-10-13-live-queue-checkin-design.md
git commit -m "test(queue): i18n completeness; docs: describe the live queue, match the spec"
```

- [ ] **Step 7: Manual check in the browser (no shared database)**

Start the local demo, which uses a throwaway SQLite file and a fake sign-in and never touches the shared project: `uv run python backend/ops/scripts/local_demo.py` (serves `http://127.0.0.1:8010`; it prints the demo logins; do not edit that script on this branch). The queue needs a session **today**: if `dr.bilal@example.com` has none today, sign in as them and add an extra session for today on the schedule page. Use the printed clinic admin of that doctor's clinic. Check each item and note what you saw:

1. **Admin Queue page:** the admin menu has "Queue"; today's session shows with "Places used".
2. **Check in:** as a patient, book a time today at least 30 minutes ahead; as the admin, the patient shows under "Expected"; *Check in* moves them to "Waiting" with a success banner.
3. **Patient card:** the patient's appointment page shows "You are #N in line..." (or "You're next"), "last updated" and a Refresh link, and refreshes itself about every 15 seconds.
4. **Walk-in:** search by part of a name: the passport shows masked (last four visible), the full number is nowhere; an exact full passport number finds the patient; two characters give "Type at least 3 characters"; *Add to queue* puts them in "Waiting" with a Walk-in badge.
5. **Order and room:** with two waiting, *Make next* and *Move up* change the order; *Send in* moves one into the room; a second *Send in* is refused with "Someone is already in the room"; *Put back* returns them to their place; *Mark seen* finishes the visit and the patient's card says "Seen. Thank you."
6. **Doctor:** the doctor home page and today's Appointments page show "Queue today" (view only, no buttons); another date's page does not.
7. **Booking is not blocked:** a walk-in does not remove any time from the doctor's bookable list.
8. **Urdu and phone width:** the admin page, the patient card and the doctor panel in Urdu are right-to-left with Urdu text; at 375 px there is no horizontal scrolling on the admin page and the patient card.

If any item fails, write a test that reproduces it (in the task that owns the code), fix it, and re-run Step 3. Stop the demo server when done.

- [ ] **Step 8: Independent review of the whole branch**

Dispatch one fresh reviewer on the most capable model that is available (if Fable needs credits, use Opus) with: the spec, this plan, the ledger's `Ruling:` lines, the Review Focus section verbatim, and `git diff <booking head>..HEAD` (the queue branch is stacked on booking, so review only the queue commits: base = the last `feature/appointment-booking` commit). Ask for a read-only review focused on, in order: (1) any query not scoped to the admin's own clinics, or anything letting one clinic's admin, a doctor or a patient see or change what is not theirs, or a patient see another patient's name; (2) the write path: lock before re-read, tokens unique under two staff acting at once, every refusal leaves nothing written, the cap with walk-ins, the doctor-unverified and session-ended edges; (3) Pakistan time and UTC (midnight, "today", the session window); (4) the walk-in search: masking, minimum length, result limit, no enumeration, audit rows holding only a count; (5) booking regression: a walk-in must never block a bookable time and the old booking tests still hold; (6) i18n and right-to-left; (7) test quality (tests that cannot fail, missing negative cases). Fix every confirmed Critical or Important finding with a test that fails first, re-run Step 3, and commit each fix separately.

- [ ] **Step 9: Hand off to Zarwa. Stop here until she answers.**

Report: the final test count and ruff status, what the manual check showed, the review outcome, the rulings made and any deferred minors, and these decisions for her:

1. **The schema owner's `queue_entry` answers.** Task 1 recorded them; confirm they came from the schema owner and that no teammate's code writes `queue_entry`. No migration is needed unless Task 1 forced the fallback.
2. **Pull-request order.** `feature/live-queue` is stacked on `feature/appointment-booking`: open booking's PR into `dev` first; the queue PR follows once booking is in `dev` (rebase the queue branch onto `dev` first). Never push, or open a PR, without her go-ahead.
3. **Next:** the required follow-up (walk-ins with no account), then the "make it scalable" list, then the patient history timeline (its decisions are saved in memory).

Update `C:\Users\Mg\Desktop\claude\curanode-implemented-features.md` (the live-queue row: built, unpushed, stacked on booking) and the memory note `curanode-queue-checkin-design.md` (status: built and reviewed, pending her tests and the PRs).

Known limits to state in the PR and the hand-off:
- The estimated wait is simple (people ahead × minutes per patient), not a prediction.
- The in-memory rate limiter is single-process; there is no Redis, no instant push and no admin auto-refresh (the scale-up list).
- No undo for "seen" or for "check in".
- Walk-ins need an existing account (the required follow-up).
- Urdu wording was written by Claude and has not been reviewed by a native speaker.
