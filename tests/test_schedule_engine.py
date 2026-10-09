"""D7 T6a: the pure schedule engine.

No database and no app: plain values in, plain values out. `test_u*` are
spec §10.1's U1–U10 at engine level (the service-level versions come with
T8). The rest pin the overlap definition, R3 precedence, R1/R1b/R4b, the
§4.2 time-off mapping, the anchor choice and live cap inheritance.
"""

from __future__ import annotations

import ast
import uuid
from dataclasses import replace
from datetime import date, time, timedelta
from pathlib import Path

import pytest
from app.schedule import engine
from app.schedule.engine import (
    EMERGENCY,
    EXTRA,
    LEAVE,
    DateException,
    Slot,
    choose_anchor,
    effective_windows,
    extra_conflicts,
    overlaps,
    slot_conflicts,
    slot_extra_conflicts,
    subtract,
    time_off_removal_conflicts,
    time_off_rows,
)

CLINIC_A = uuid.UUID("00000000-0000-7000-8000-00000000000a")
CLINIC_B = uuid.UUID("00000000-0000-7000-8000-00000000000b")
MONDAY, WEDNESDAY = 0, 2
D = date(2026, 10, 12)  # a Monday: "d" in spec §10.1
TODAY = D - timedelta(days=2)


def t(hhmm: str) -> time:
    return time.fromisoformat(hhmm)


def make_slot(clinic=CLINIC_A, weekday=MONDAY, start="09:00", end="13:00", cap=16, **kw) -> Slot:
    return Slot(
        id=uuid.uuid4(),
        clinic_id=clinic,
        weekday=weekday,
        start=t(start),
        end=t(end),
        max_tokens=cap,
        **kw,
    )


def leave(slot: Slot, day=D, start=None, end=None, kind=LEAVE, **kw) -> DateException:
    return DateException(
        id=uuid.uuid4(),
        schedule_id=slot.id,
        day=day,
        kind=kind,
        start=t(start) if start else None,
        end=t(end) if end else None,
        **kw,
    )


def extra(anchor: Slot, start: str, end: str, day=D, cap=None, **kw) -> DateException:
    return DateException(
        id=uuid.uuid4(),
        schedule_id=anchor.id,
        day=day,
        kind=EXTRA,
        start=t(start),
        end=t(end),
        max_tokens=cap,
        **kw,
    )


def spans(windows) -> list[tuple[str, str]]:
    return [(w.start.strftime("%H:%M"), w.end.strftime("%H:%M")) for w in windows]


# Spec §10.1 fixture: S = Monday 09:00–13:00 at A; B has a Wednesday slot so
# an extra at B has something to anchor to.
@pytest.fixture
def s() -> Slot:
    return make_slot()


@pytest.fixture
def b_slot() -> Slot:
    return make_slot(clinic=CLINIC_B, weekday=WEDNESDAY, start="10:00", end="12:00", cap=8)


# ── Purity ───────────────────────────────────────────────────────────────
def test_engine_imports_nothing_from_the_app():
    tree = ast.parse(Path(engine.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import"
            assert not (node.module or "").startswith("app"), node.module
        elif isinstance(node, ast.Import):
            assert not any(a.name.startswith("app") for a in node.names)


# ── The overlap definition ───────────────────────────────────────────────
@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (("09:00", "13:00"), ("12:00", "14:00"), True),  # partial
        (("09:00", "13:00"), ("10:00", "11:00"), True),  # contained
        (("10:00", "11:00"), ("09:00", "13:00"), True),  # containing
        (("09:00", "13:00"), ("09:00", "13:00"), True),  # identical
        (("09:00", "13:00"), ("13:00", "15:00"), False),  # back-to-back after
        (("09:00", "13:00"), ("07:00", "09:00"), False),  # back-to-back before
        (("09:00", "10:00"), ("11:00", "12:00"), False),  # apart
    ],
)
def test_overlaps_is_half_open(a, b, expected):
    assert overlaps(t(a[0]), t(a[1]), t(b[0]), t(b[1])) is expected
    assert overlaps(t(b[0]), t(b[1]), t(a[0]), t(a[1])) is expected  # symmetric


@pytest.mark.parametrize(
    ("cuts", "expected"),
    [
        ([], [("09:00", "13:00")]),
        ([("11:00", "13:00")], [("09:00", "11:00")]),
        ([("10:00", "11:00")], [("09:00", "10:00"), ("11:00", "13:00")]),
        ([("08:00", "14:00")], []),
        ([("13:00", "14:00")], [("09:00", "13:00")]),  # touching cut removes nothing
        ([("10:30", "11:30"), ("10:00", "11:00")], [("09:00", "10:00"), ("11:30", "13:00")]),
    ],
)
def test_subtract(cuts, expected):
    pieces = subtract(t("09:00"), t("13:00"), [(t(a), t(b)) for a, b in cuts])
    assert [(a.strftime("%H:%M"), b.strftime("%H:%M")) for a, b in pieces] == expected


# ── R3: effective availability ───────────────────────────────────────────
def test_a_slot_gives_its_window_on_its_weekday_only(s):
    assert spans(effective_windows(D, [s], [])) == [("09:00", "13:00")]
    assert effective_windows(D + timedelta(days=1), [s], []) == []


def test_whole_day_time_off_removes_the_slot(s):
    assert effective_windows(D, [s], [leave(s)]) == []


def test_window_time_off_splits_the_slot(s):
    windows = effective_windows(D, [s], [leave(s, start="10:00", end="11:00")])
    assert spans(windows) == [("09:00", "10:00"), ("11:00", "13:00")]
    assert {w.schedule_id for w in windows} == {s.id}


def test_emergency_is_treated_as_leave(s):
    assert effective_windows(D, [s], [leave(s, kind=EMERGENCY)]) == []


def test_removed_exceptions_are_ignored(s):
    assert spans(effective_windows(D, [s], [leave(s, removed=True)])) == [("09:00", "13:00")]


def test_time_off_on_another_date_does_not_apply(s):
    other = leave(s, day=D + timedelta(days=7))
    assert spans(effective_windows(D, [s], [other])) == [("09:00", "13:00")]


@pytest.mark.parametrize("flags", [{"active": False}, {"clinic_active": False}])
def test_only_live_slots_give_availability(flags):
    assert effective_windows(D, [make_slot(**flags)], []) == []


def test_precedence_slot_minus_time_off_plus_extra(s, b_slot):
    windows = effective_windows(
        D,
        [s, b_slot],
        [leave(s, start="11:00", end="13:00"), extra(b_slot, "14:00", "16:00")],
    )
    assert spans(windows) == [("09:00", "11:00"), ("14:00", "16:00")]
    assert [w.clinic_id for w in windows] == [CLINIC_A, CLINIC_B]
    assert [w.is_extra for w in windows] == [False, True]


def test_an_extra_on_a_dead_anchor_gives_nothing(s):
    dead = replace(s, clinic_active=False)
    assert effective_windows(D, [dead], [extra(dead, "14:00", "16:00")]) == []


def test_windows_are_sorted_by_start(s, b_slot):
    late = make_slot(start="15:00", end="17:00")
    windows = effective_windows(D, [late, s, b_slot], [extra(b_slot, "13:00", "15:00")])
    assert spans(windows) == [("09:00", "13:00"), ("13:00", "15:00"), ("15:00", "17:00")]


# ── R13 / decision 18: the effective cap ─────────────────────────────────
def test_an_extra_with_its_own_cap_uses_it(s):
    [_, w] = effective_windows(D, [s], [extra(s, "13:00", "15:00", cap=5)])
    assert (w.max_tokens, w.inherited) == (5, False)


def test_a_blank_cap_inherits_the_anchor_cap_live(s):
    blank = extra(s, "13:00", "15:00")
    [_, before] = effective_windows(D, [s], [blank])
    assert (before.max_tokens, before.inherited, before.anchor_weekday) == (16, True, MONDAY)

    raised = replace(s, max_tokens=30)  # the anchor's cap changes later
    [_, after] = effective_windows(D, [raised], [blank])
    assert (after.max_tokens, after.inherited) == (30, True)
    assert blank.max_tokens is None  # the extra's own row is untouched


# ── R1: weekly slots ─────────────────────────────────────────────────────
def test_r1_overlap_at_any_clinic_is_a_conflict(s):
    [c] = slot_conflicts(MONDAY, t("12:00"), t("14:00"), [replace(s, clinic_id=CLINIC_B)])
    assert (c.schedule_id, c.weekday, c.start, c.end) == (s.id, MONDAY, t("09:00"), t("13:00"))


def test_r1_back_to_back_and_other_weekdays_are_fine(s):
    assert slot_conflicts(MONDAY, t("13:00"), t("15:00"), [s]) == []
    assert slot_conflicts(WEDNESDAY, t("09:00"), t("13:00"), [s]) == []


def test_r1_counts_an_active_slot_at_an_inactive_clinic(s):
    assert slot_conflicts(MONDAY, t("10:00"), t("11:00"), [replace(s, clinic_active=False)])


def test_r1_ignores_a_removed_slot_and_the_slot_being_edited(s):
    assert slot_conflicts(MONDAY, t("10:00"), t("11:00"), [replace(s, active=False)]) == []
    assert slot_conflicts(MONDAY, t("10:00"), t("11:00"), [s], exclude_slot_id=s.id) == []


# ── R1b: slots vs future extras ──────────────────────────────────────────
def test_r1b_a_new_slot_over_a_future_extra_at_another_clinic(b_slot):
    e = extra(b_slot, "09:00", "10:00")
    [c] = slot_extra_conflicts(MONDAY, t("09:30"), t("12:00"), [b_slot], [e], from_day=TODAY)
    assert (c.exception_id, c.day, c.clinic_id) == (e.id, D, CLINIC_B)


def test_r1b_ignores_past_extras_other_weekdays_and_back_to_back(b_slot):
    past = extra(b_slot, "09:00", "10:00", day=TODAY - timedelta(days=5))  # a Monday
    assert past.day.weekday() == MONDAY
    assert slot_extra_conflicts(MONDAY, t("09:00"), t("12:00"), [b_slot], [past], TODAY) == []
    e = extra(b_slot, "13:00", "15:00")
    assert slot_extra_conflicts(MONDAY, t("09:00"), t("13:00"), [b_slot], [e], TODAY) == []
    assert slot_extra_conflicts(WEDNESDAY, t("12:00"), t("14:00"), [b_slot], [e], TODAY) == []


def test_r1b_time_already_taken_off_does_not_count(s):
    # S takes 11:00–13:00 off on d and an extra fills it; widening S to 14:00
    # still clashes only if the extra overlaps S's *effective* window.
    off = leave(s, start="11:00", end="13:00")
    fill = extra(s, "11:00", "13:00")
    assert slot_extra_conflicts(MONDAY, t("09:00"), t("13:00"), [s], [off, fill], TODAY, s.id) == []


# ── R4b ──────────────────────────────────────────────────────────────────
def test_r4b_only_live_time_off_rows_are_checked(s):
    e = extra(s, "13:00", "15:00")
    assert time_off_removal_conflicts(e.id, [s], [e]) == []  # not time off
    assert time_off_removal_conflicts(uuid.uuid4(), [s], [e]) == []  # unknown


def test_r4b_another_clinics_extra_blocks_the_removal(s, b_slot):
    off = leave(s, start="11:00", end="13:00")
    other = extra(b_slot, "11:00", "12:00")
    [c] = time_off_removal_conflicts(off.id, [s, b_slot], [off, other])
    assert (c.exception_id, c.clinic_id) == (other.id, CLINIC_B)


# ── §4.2: time-off rows ──────────────────────────────────────────────────
def test_whole_day_all_clinics_writes_one_row_per_slot_that_day(s, b_slot):
    a2 = make_slot(clinic=CLINIC_B, start="15:00", end="17:00")
    rows = time_off_rows(D, [s, b_slot, a2])
    assert [(r.schedule_id, r.start, r.end) for r in rows] == [
        (s.id, None, None),
        (a2.id, None, None),
    ]


def test_whole_day_one_clinic_writes_only_that_clinics_rows(s):
    a2 = make_slot(clinic=CLINIC_B, start="15:00", end="17:00")
    assert [r.schedule_id for r in time_off_rows(D, [s, a2], clinic_id=CLINIC_B)] == [a2.id]


def test_a_window_writes_the_intersection_with_each_slot(s):
    a2 = make_slot(clinic=CLINIC_B, start="15:00", end="17:00")
    rows = time_off_rows(D, [s, a2], start=t("12:00"), end=t("16:00"))
    assert [(r.schedule_id, r.start, r.end) for r in rows] == [
        (s.id, t("12:00"), t("13:00")),
        (a2.id, t("15:00"), t("16:00")),
    ]


def test_time_off_that_hits_nothing_writes_no_rows(s):
    assert time_off_rows(D, [s], start=t("13:00"), end=t("15:00")) == []  # back-to-back
    assert time_off_rows(D + timedelta(days=1), [s]) == []  # no slot that weekday
    assert time_off_rows(D, [replace(s, clinic_active=False)]) == []  # not live


# ── The anchor ───────────────────────────────────────────────────────────
def test_the_anchor_is_the_earliest_live_slot_at_the_clinic(s):
    later_same_day = make_slot(start="14:00", end="16:00")
    sunday = make_slot(weekday=6, start="07:00", end="08:00")
    dead_earlier = make_slot(weekday=MONDAY, start="06:00", end="07:00", clinic_active=False)
    assert choose_anchor(CLINIC_A, [sunday, later_same_day, s, dead_earlier]) == s


def test_no_live_slot_at_the_clinic_means_no_anchor(s):
    assert choose_anchor(CLINIC_B, [s]) is None


# ── Spec §10.1: U1–U10 at engine level ───────────────────────────────────
def test_u1_extra_overlapping_its_anchor_is_a_conflict(s):
    [c] = extra_conflicts(D, t("12:00"), t("14:00"), [s], [])
    assert (c.schedule_id, c.exception_id, c.clinic_id) == (s.id, None, CLINIC_A)
    assert (c.start, c.end) == (t("09:00"), t("13:00"))


def test_u2_back_to_back_after_the_anchor_is_accepted(s):
    assert extra_conflicts(D, t("13:00"), t("15:00"), [s], []) == []


def test_u3_back_to_back_before_the_anchor_is_accepted(s):
    assert extra_conflicts(D, t("07:00"), t("09:00"), [s], []) == []


def test_u4_extra_inside_the_anchor_is_a_conflict(s):
    [c] = extra_conflicts(D, t("10:00"), t("11:00"), [s], [])
    assert c.schedule_id == s.id


def test_u5_extra_may_fill_a_window_freed_by_time_off(s):
    off = leave(s, start="11:00", end="13:00")
    assert extra_conflicts(D, t("11:00"), t("13:00"), [s], [off]) == []


def test_u6_removing_that_time_off_is_rejected(s):
    off = leave(s, start="11:00", end="13:00")
    fill = extra(s, "11:00", "13:00")
    [c] = time_off_removal_conflicts(off.id, [s], [off, fill])
    assert (c.exception_id, c.start, c.end) == (fill.id, t("11:00"), t("13:00"))


def test_u7_a_second_extra_overlapping_the_first_is_a_conflict(s):
    first = extra(s, "13:00", "15:00")
    conflicts = extra_conflicts(D, t("14:00"), t("16:00"), [s], [first])
    assert [c.exception_id for c in conflicts] == [first.id]


def test_u8_editing_the_anchor_onto_its_extra_is_rejected(s):
    e = extra(s, "13:00", "15:00")
    [c] = slot_extra_conflicts(MONDAY, t("09:00"), t("14:00"), [s], [e], TODAY, slot_id=s.id)
    assert (c.exception_id, c.day) == (e.id, D)


def test_u9_availability_keeps_the_anchor_and_extra_separate(s):
    windows = effective_windows(D, [s], [extra(s, "13:00", "15:00", cap=4)])
    assert spans(windows) == [("09:00", "13:00"), ("13:00", "15:00")]
    assert [(w.schedule_id, w.is_extra, w.max_tokens) for w in windows] == [
        (s.id, False, 16),
        (s.id, True, 4),
    ]


def test_u10_extra_at_another_clinic_overlapping_s_is_a_conflict(s, b_slot):
    # The extra at B (anchored to B's Wednesday slot) is checked against S on d.
    [c] = extra_conflicts(D, t("12:00"), t("14:00"), [s, b_slot], [])
    assert (c.schedule_id, c.clinic_id) == (s.id, CLINIC_A)
