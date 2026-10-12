"""D7 T5: schedule input validation and the capacity calculation.

Pure tests: no database, no app. Each failure is checked by its i18n key
(`err["msg"]`), the same contract the web forms rely on. Covers the
validation side of AC-5, AC-22, AC-23 and AC-29.
"""

from __future__ import annotations

import uuid
from datetime import date, time, timedelta

import pytest
from app.db import types as dbtypes
from app.schedule.schemas import (
    MAX_TOKENS_MAX,
    REASON_MAX,
    SLOT_MINUTES_CHOICES,
    ExtraCreate,
    SlotCreate,
    SlotUpdate,
    TimeOffCreate,
    calc_max_tokens,
)
from pydantic import ValidationError

CLINIC = str(uuid.uuid4())
TODAY = date(2026, 10, 12)  # a Monday


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    monkeypatch.setattr(dbtypes, "today_pk", lambda: TODAY)


def slot(**overrides):
    return SlotCreate(**_slot_data(**overrides))


def errors_of(exc: pytest.ExceptionInfo[ValidationError]) -> dict[str, str]:
    return {str(e["loc"][0]) if e["loc"] else "__all__": e["msg"] for e in exc.value.errors()}


def rejects(model, field: str, key: str, **data) -> None:
    with pytest.raises(ValidationError) as exc:
        model(**data)
    assert errors_of(exc).get(field) == key, errors_of(exc)


# ── calc_max_tokens (R13) ────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("start", "end", "minutes", "expected"),
    [
        (time(9), time(13), 15, 16),  # the spec's example
        (time(9), time(9, 50), 15, 3),  # leftover minutes are not a turn
        (time(9), time(9, 15), 15, 1),  # exactly one turn
        (time(0), time(23, 59), 5, 200),  # 287 turns, capped at 200
        (time(8), time(20), 60, 12),
    ],
)
def test_calc_max_tokens(start, end, minutes, expected):
    assert calc_max_tokens(start, end, minutes) == expected


# ── Times (AC-5) ─────────────────────────────────────────────────────────
def test_a_valid_slot_parses():
    s = slot()
    assert (s.start_time, s.end_time) == (time(9), time(13))
    assert s.weekdays == [0]
    assert s.clinic_id == uuid.UUID(CLINIC)


@pytest.mark.parametrize("end", ["09:00", "08:59"])
def test_end_not_after_start_is_rejected(end):
    rejects(SlotCreate, "end_time", "errors.schedule_time_order", **_slot_data(end_time=end))


@pytest.mark.parametrize("value", ["09:00:30", "09:00:00", "9:00", "24:00", "noon", "", "09-00"])
def test_times_must_be_whole_minutes_hh_mm(value):
    rejects(
        SlotCreate, "start_time", "errors.schedule_time_invalid", **_slot_data(start_time=value)
    )


def test_a_time_object_with_seconds_is_rejected():
    rejects(
        SlotCreate,
        "start_time",
        "errors.schedule_time_invalid",
        **_slot_data(start_time=time(9, 0, 30)),
    )


def test_latest_end_is_2359():
    assert slot(start_time="22:00", end_time="23:59").end_time == time(23, 59)


# ── slot_minutes (AC-22) ─────────────────────────────────────────────────
def test_slot_minutes_defaults_to_15():
    assert slot().slot_minutes == 15
    assert slot(slot_minutes="").slot_minutes == 15


@pytest.mark.parametrize("value", SLOT_MINUTES_CHOICES)
def test_every_step_of_5_from_5_to_60_is_accepted(value):
    assert slot(start_time="08:00", end_time="20:00", slot_minutes=str(value)).slot_minutes == value


@pytest.mark.parametrize("value", ["0", "3", "7", "65", "-5", "15.0", "abc", "²", True])
def test_other_slot_minutes_are_rejected(value):
    rejects(
        SlotCreate,
        "slot_minutes",
        "errors.schedule_slot_minutes_invalid",
        **_slot_data(slot_minutes=value),
    )


def test_a_slot_shorter_than_one_turn_is_rejected():
    rejects(
        SlotCreate,
        "end_time",
        "errors.schedule_too_short",
        **_slot_data(start_time="09:00", end_time="09:10", slot_minutes="15"),
    )


# ── max_tokens (AC-23) ───────────────────────────────────────────────────
@pytest.mark.parametrize("value", ["1", "16", "200", 200])
def test_max_tokens_from_1_to_200_is_accepted(value):
    assert slot(max_tokens=value).max_tokens == int(value)


@pytest.mark.parametrize("value", ["0", "201", "-1", "1.5", "ten", "²", False])
def test_other_max_tokens_are_rejected(value):
    rejects(
        SlotCreate,
        "max_tokens",
        "errors.schedule_max_tokens_invalid",
        **_slot_data(max_tokens=value),
    )


def test_blank_max_tokens_resolves_to_calc():
    s = slot(max_tokens="")
    assert s.max_tokens is None
    assert s.resolved_max_tokens() == 16


def test_calc_prefill_is_capped_at_200():
    s = slot(start_time="00:00", end_time="23:59", slot_minutes="5", max_tokens="")
    assert s.resolved_max_tokens() == MAX_TOKENS_MAX


def test_an_entered_max_tokens_wins_over_calc():
    assert slot(max_tokens="40").resolved_max_tokens() == 40


# ── Weekdays and clinic ──────────────────────────────────────────────────
def test_weekdays_are_deduplicated_and_sorted():
    assert slot(weekdays=["4", "0", "4", "2"]).weekdays == [0, 2, 4]


@pytest.mark.parametrize("value", [[], "", None])
def test_at_least_one_weekday_is_required(value):
    rejects(
        SlotCreate, "weekdays", "errors.schedule_weekday_required", **_slot_data(weekdays=value)
    )


@pytest.mark.parametrize("value", [["7"], ["-1"], ["mon"]])
def test_weekday_out_of_range_is_rejected(value):
    rejects(SlotCreate, "weekdays", "errors.choice_invalid", **_slot_data(weekdays=value))


def test_a_malformed_clinic_id_is_rejected():
    rejects(SlotCreate, "clinic_id", "errors.choice_invalid", **_slot_data(clinic_id="nope"))


def test_doctor_id_cannot_be_sent():
    with pytest.raises(ValidationError):
        SlotCreate(**_slot_data(doctor_id=str(uuid.uuid4())))


def test_slot_update_takes_one_weekday():
    u = SlotUpdate(clinic_id=CLINIC, weekday="6", start_time="10:00", end_time="12:00")
    assert u.weekday == 6
    assert u.resolved_max_tokens() == 8


# ── Time off ─────────────────────────────────────────────────────────────
def test_whole_day_time_off_for_all_clinics():
    t = TimeOffCreate(exception_date=TODAY.isoformat(), clinic_id="", start_time="", end_time="")
    assert (t.clinic_id, t.start_time, t.end_time, t.emergency) == (None, None, None, False)


def test_time_off_window_and_emergency_checkbox():
    t = TimeOffCreate(
        exception_date=TODAY.isoformat(),
        clinic_id=CLINIC,
        start_time="11:00",
        end_time="13:00",
        emergency="on",
    )
    assert (t.start_time, t.end_time, t.emergency) == (time(11), time(13), True)


@pytest.mark.parametrize(("start", "end"), [("11:00", ""), ("", "13:00")])
def test_time_off_window_needs_both_times(start, end):
    rejects(
        TimeOffCreate,
        "end_time",
        "errors.schedule_time_invalid",
        exception_date=TODAY.isoformat(),
        start_time=start,
        end_time=end,
    )


def test_time_off_window_order():
    rejects(
        TimeOffCreate,
        "end_time",
        "errors.schedule_time_order",
        exception_date=TODAY.isoformat(),
        start_time="13:00",
        end_time="11:00",
    )


def test_past_dates_are_rejected_today_is_allowed():
    yesterday = (TODAY - timedelta(days=1)).isoformat()
    rejects(TimeOffCreate, "exception_date", "errors.schedule_past_date", exception_date=yesterday)
    assert TimeOffCreate(exception_date=TODAY.isoformat()).exception_date == TODAY


def test_a_malformed_date_is_rejected():
    rejects(TimeOffCreate, "exception_date", "errors.date_invalid", exception_date="12/10/2026")


def test_reason_is_trimmed_and_limited_to_200():
    ok = TimeOffCreate(exception_date=TODAY.isoformat(), reason="  Conference   day ")
    assert ok.reason == "Conference day"
    rejects(
        TimeOffCreate,
        "reason",
        "errors.text_too_long",
        exception_date=TODAY.isoformat(),
        reason="x" * (REASON_MAX + 1),
    )


# ── Extra hours (AC-29, app part) ────────────────────────────────────────
def extra_data(**overrides):
    data = {
        "exception_date": TODAY.isoformat(),
        "clinic_id": CLINIC,
        "start_time": "13:00",
        "end_time": "15:00",
    }
    data.update(overrides)
    return data


def test_extra_blank_cap_means_inherit():
    assert ExtraCreate(**extra_data(max_tokens="")).max_tokens is None


def test_extra_cap_from_1_to_200():
    assert ExtraCreate(**extra_data(max_tokens="12")).max_tokens == 12
    for bad in ("0", "201"):
        rejects(
            ExtraCreate,
            "max_tokens",
            "errors.schedule_max_tokens_invalid",
            **extra_data(max_tokens=bad),
        )


def test_extra_needs_a_clinic_and_both_times():
    rejects(ExtraCreate, "clinic_id", "errors.choice_invalid", **extra_data(clinic_id=""))
    rejects(ExtraCreate, "end_time", "errors.schedule_time_invalid", **extra_data(end_time=""))
    rejects(ExtraCreate, "end_time", "errors.schedule_time_order", **extra_data(end_time="12:00"))


def test_extra_in_the_past_is_rejected():
    yesterday = (TODAY - timedelta(days=1)).isoformat()
    rejects(
        ExtraCreate,
        "exception_date",
        "errors.schedule_past_date",
        **extra_data(exception_date=yesterday),
    )


# ── helpers ──────────────────────────────────────────────────────────────
def _slot_data(**overrides):
    data = {
        "clinic_id": CLINIC,
        "weekdays": ["0"],
        "start_time": "09:00",
        "end_time": "13:00",
    }
    data.update(overrides)
    return data
