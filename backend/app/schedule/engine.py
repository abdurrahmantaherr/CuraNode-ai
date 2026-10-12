"""Pure schedule engine for D7: overlap, effective availability, conflict rules.

This module imports nothing from the app — standard library only. Callers
pass plain frozen values in and get plain values out:

  * Conflicts are returned as data (`Conflict`), never raised. The schedule
    service turns them into `ScheduleConflict`.
  * Liveness that needs the database (is the affiliation active? is the
    clinic active?) is decided by the caller and passed in as
    `Slot.clinic_active`. Removed exceptions may be passed in with
    `removed=True`; the engine ignores them.

`overlaps` is THE overlap definition for the whole feature (spec R1): windows
are half-open `[start, end)`, so back-to-back windows (one ends exactly when
the other starts) do not overlap. Nothing else may re-implement it.

Rules implemented here (spec §5): R1 (`slot_conflicts`), R1b
(`slot_extra_conflicts`), R3 (`effective_windows`), R4 (`extra_conflicts`),
R4b (`time_off_removal_conflicts`), the time-off mapping of §4.2
(`time_off_rows`), the extra anchor (`choose_anchor`), and the live cap
inheritance of R13 / decision 18.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, time

LEAVE = "leave"
EMERGENCY = "emergency"
EXTRA = "extra"
UNAVAILABLE_KINDS = frozenset({LEAVE, EMERGENCY})  # both mean "unavailable" (spec §4.2)


# ── Values ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Slot:
    """A weekly slot (`doctor_schedule` row) as the engine sees it.

    `active` is `status == 'active'`. `clinic_active` is "the slot's
    affiliation and its clinic are both active", decided by the caller. A slot
    is *live* (it produces availability) only when both are true; R1 still
    counts an `active` slot at an inactive clinic.
    """

    id: uuid.UUID
    clinic_id: uuid.UUID
    weekday: int  # 0 = Monday … 6 = Sunday
    start: time
    end: time
    max_tokens: int
    active: bool = True
    clinic_active: bool = True

    @property
    def live(self) -> bool:
        return self.active and self.clinic_active


@dataclass(frozen=True)
class DateException:
    """A `schedule_exception` row: time off (`leave`/`emergency`) on its slot,
    or extra hours (`extra`) anchored to its slot. Time off with no times
    covers the whole slot; an extra always has times. `max_tokens` is only
    meaningful on an extra (None = inherit the anchor's cap)."""

    id: uuid.UUID
    schedule_id: uuid.UUID
    day: date
    kind: str
    start: time | None = None
    end: time | None = None
    max_tokens: int | None = None
    removed: bool = False


@dataclass(frozen=True)
class EffectiveWindow:
    """One bookable session window on one date (R3). Never merged with a
    neighbour: each window is its own session with its own cap (R14)."""

    day: date
    clinic_id: uuid.UUID
    start: time
    end: time
    schedule_id: uuid.UUID
    exception_id: uuid.UUID | None  # set for extra hours only
    max_tokens: int
    inherited: bool  # an extra whose cap comes from its anchor slot, read live
    anchor_weekday: int  # the source slot's weekday (for "from your {day} hours")

    @property
    def is_extra(self) -> bool:
        return self.exception_id is not None


@dataclass(frozen=True)
class Conflict:
    """The window a proposed change would overlap, as plain data.

    `weekday` is set when the conflicting window is a weekly slot, `day` when
    it is a window on a specific date (an extra, or an effective window)."""

    clinic_id: uuid.UUID
    start: time
    end: time
    schedule_id: uuid.UUID
    exception_id: uuid.UUID | None
    weekday: int | None = None
    day: date | None = None


@dataclass(frozen=True)
class TimeOffRow:
    """One row the service writes for a time-off entry (spec §4.2)."""

    schedule_id: uuid.UUID
    start: time | None
    end: time | None


# ── The overlap definition (R1) ──────────────────────────────────────────
def overlaps(a_start: time, a_end: time, b_start: time, b_end: time) -> bool:
    """Half-open windows overlap iff each starts before the other ends.
    Back-to-back (`a_end == b_start`) is NOT an overlap."""
    return a_start < b_end and b_start < a_end


def subtract(start: time, end: time, cuts: Iterable[tuple[time, time]]) -> list[tuple[time, time]]:
    """`[start, end)` minus every cut, as sorted, non-empty pieces."""
    pieces = [(start, end)]
    for cut_start, cut_end in sorted(cuts):
        next_pieces: list[tuple[time, time]] = []
        for piece_start, piece_end in pieces:
            if not overlaps(piece_start, piece_end, cut_start, cut_end):
                next_pieces.append((piece_start, piece_end))
                continue
            if piece_start < cut_start:
                next_pieces.append((piece_start, cut_start))
            if cut_end < piece_end:
                next_pieces.append((cut_end, piece_end))
        pieces = next_pieces
    return pieces


# ── Effective availability (R3) ──────────────────────────────────────────
def _live_exceptions(exceptions: Iterable[DateException], day: date) -> list[DateException]:
    return [e for e in exceptions if e.day == day and not e.removed]


def effective_windows(
    day: date, slots: Sequence[Slot], exceptions: Iterable[DateException]
) -> list[EffectiveWindow]:
    """R3 for one date: live slots on that weekday, minus their time off,
    plus live extras whose anchor slot is live. Sorted by start, then clinic;
    never merged."""
    by_id = {s.id: s for s in slots}
    todays = _live_exceptions(exceptions, day)
    windows: list[EffectiveWindow] = []

    for slot in slots:
        if not slot.live or slot.weekday != day.weekday():
            continue
        cuts = [
            (e.start or slot.start, e.end or slot.end)
            for e in todays
            if e.schedule_id == slot.id and e.kind in UNAVAILABLE_KINDS
        ]
        for start, end in subtract(slot.start, slot.end, cuts):
            windows.append(
                EffectiveWindow(
                    day=day,
                    clinic_id=slot.clinic_id,
                    start=start,
                    end=end,
                    schedule_id=slot.id,
                    exception_id=None,
                    max_tokens=slot.max_tokens,
                    inherited=False,
                    anchor_weekday=slot.weekday,
                )
            )

    for extra in todays:
        anchor = by_id.get(extra.schedule_id)
        if extra.kind != EXTRA or anchor is None or not anchor.live:
            continue
        if extra.start is None or extra.end is None:
            continue  # malformed; the database CHECK forbids it
        windows.append(
            EffectiveWindow(
                day=day,
                clinic_id=anchor.clinic_id,
                start=extra.start,
                end=extra.end,
                schedule_id=anchor.id,
                exception_id=extra.id,
                max_tokens=extra.max_tokens if extra.max_tokens is not None else anchor.max_tokens,
                inherited=extra.max_tokens is None,
                anchor_weekday=anchor.weekday,
            )
        )

    return sorted(windows, key=lambda w: (w.start, str(w.clinic_id), w.end))


def _window_conflict(w: EffectiveWindow) -> Conflict:
    return Conflict(
        clinic_id=w.clinic_id,
        start=w.start,
        end=w.end,
        schedule_id=w.schedule_id,
        exception_id=w.exception_id,
        weekday=None if w.is_extra else w.anchor_weekday,
        day=w.day,
    )


# ── R1: weekly slots never overlap ───────────────────────────────────────
def slot_conflicts(
    weekday: int,
    start: time,
    end: time,
    slots: Sequence[Slot],
    exclude_slot_id: uuid.UUID | None = None,
) -> list[Conflict]:
    """Every `active` slot on that weekday that `[start, end)` overlaps, at any
    clinic — including a clinic whose affiliation is now inactive, so that
    reactivating it can never expose a hidden overlap."""
    return [
        Conflict(
            clinic_id=s.clinic_id,
            start=s.start,
            end=s.end,
            schedule_id=s.id,
            exception_id=None,
            weekday=s.weekday,
        )
        for s in slots
        if s.active
        and s.id != exclude_slot_id
        and s.weekday == weekday
        and overlaps(start, end, s.start, s.end)
    ]


# ── R1b: a new or edited slot vs future extras ───────────────────────────
def slot_extra_conflicts(
    weekday: int,
    start: time,
    end: time,
    slots: Sequence[Slot],
    exceptions: Iterable[DateException],
    from_day: date,
    slot_id: uuid.UUID | None = None,
) -> list[Conflict]:
    """Live extras on any date ≥ `from_day` falling on `weekday` that the
    proposed slot window would overlap, at any clinic (decision 17).

    The slot's *effective* window on each date is used: when editing slot
    `slot_id`, its own live time off on that date is subtracted first, so an
    extra that fills time already taken off is not a conflict."""
    by_id = {s.id: s for s in slots}
    exceptions = list(exceptions)
    conflicts: list[Conflict] = []
    for extra in exceptions:
        if extra.kind != EXTRA or extra.removed or extra.day < from_day:
            continue
        if extra.day.weekday() != weekday or extra.start is None or extra.end is None:
            continue
        anchor = by_id.get(extra.schedule_id)
        if anchor is None or not anchor.live:
            continue
        cuts = [
            (e.start or start, e.end or end)
            for e in exceptions
            if slot_id is not None
            and e.schedule_id == slot_id
            and e.day == extra.day
            and e.kind in UNAVAILABLE_KINDS
            and not e.removed
        ]
        if any(
            overlaps(piece_start, piece_end, extra.start, extra.end)
            for piece_start, piece_end in subtract(start, end, cuts)
        ):
            conflicts.append(
                Conflict(
                    clinic_id=anchor.clinic_id,
                    start=extra.start,
                    end=extra.end,
                    schedule_id=anchor.id,
                    exception_id=extra.id,
                    day=extra.day,
                )
            )
    return sorted(conflicts, key=lambda c: (c.day or date.min, c.start))


# ── R4: an extra vs every effective window on its date ───────────────────
def extra_conflicts(
    day: date,
    start: time,
    end: time,
    slots: Sequence[Slot],
    exceptions: Iterable[DateException],
) -> list[Conflict]:
    """Every effective window on `day` (any clinic, the same clinic, the
    anchor slot itself, other extras) that `[start, end)` overlaps. Time
    already taken off is not effective, so an extra may fill it."""
    return [
        _window_conflict(w)
        for w in effective_windows(day, slots, exceptions)
        if overlaps(start, end, w.start, w.end)
    ]


# ── R4b: removing time off must not expose a slot under an extra ─────────
def time_off_removal_conflicts(
    exception_id: uuid.UUID,
    slots: Sequence[Slot],
    exceptions: Iterable[DateException],
) -> list[Conflict]:
    """The extras that would overlap the slot window freed by removing the
    time-off row `exception_id` (any clinic, including extras anchored to that
    same slot). Empty if the row is not live time off."""
    exceptions = list(exceptions)
    target = next((e for e in exceptions if e.id == exception_id), None)
    if target is None or target.removed or target.kind not in UNAVAILABLE_KINDS:
        return []
    after = effective_windows(target.day, slots, [e for e in exceptions if e.id != exception_id])
    freed_slot_pieces = [w for w in after if w.schedule_id == target.schedule_id and not w.is_extra]
    return [
        _window_conflict(extra)
        for extra in after
        if extra.is_extra
        and any(overlaps(extra.start, extra.end, w.start, w.end) for w in freed_slot_pieces)
    ]


# ── Time off mapping (spec §4.2) ─────────────────────────────────────────
def time_off_rows(
    day: date,
    slots: Sequence[Slot],
    clinic_id: uuid.UUID | None = None,
    start: time | None = None,
    end: time | None = None,
) -> list[TimeOffRow]:
    """The rows one time-off entry writes: one per live slot on that weekday
    (at `clinic_id`, or at every clinic when None). A whole-day entry stores
    no times (the whole slot); a window stores its intersection with each slot
    it touches. An empty result means there was nothing to block."""
    rows: list[TimeOffRow] = []
    for slot in sorted(slots, key=lambda s: (s.start, str(s.clinic_id))):
        if not slot.live or slot.weekday != day.weekday():
            continue
        if clinic_id is not None and slot.clinic_id != clinic_id:
            continue
        if start is None or end is None:
            rows.append(TimeOffRow(schedule_id=slot.id, start=None, end=None))
        elif overlaps(start, end, slot.start, slot.end):
            rows.append(
                TimeOffRow(
                    schedule_id=slot.id, start=max(start, slot.start), end=min(end, slot.end)
                )
            )
    return rows


# ── Extra hours anchor ───────────────────────────────────────────────────
def choose_anchor(clinic_id: uuid.UUID, slots: Sequence[Slot]) -> Slot | None:
    """The slot an extra at `clinic_id` hangs off: the earliest live slot
    there, by weekday then start. None means there is nothing to anchor to."""
    candidates = [s for s in slots if s.live and s.clinic_id == clinic_id]
    return min(candidates, key=lambda s: (s.weekday, s.start), default=None)
