"""Input models for the doctor schedule (D7 spec §5, §8.1).

As in the profile schemas, every validation failure raises
`PydanticCustomError(<type>, "errors.<key>")`, so an error's `msg` is exactly
an i18n key the web forms translate. Cross-field checks are attached to the
later field (`end_time` validates against `start_time` and `slot_minutes`),
which is why fields are declared in that order.

Inputs arrive from HTML forms, so values may be strings: "09:00" for a time,
"" for a blank optional number. Request models forbid unknown fields; in
particular a doctor id is never accepted from the request (spec §3).

What these models do NOT check: anything that needs the database or the
doctor's other windows (affiliation, overlaps, limits, anchors). That is the
schedule service's job, on top of the pure engine.
"""

from __future__ import annotations

import uuid
from datetime import date, time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator
from pydantic_core import PydanticCustomError

from ..db import types as dbtypes
from ..profile.schemas import clean_text

SLOT_MINUTES_CHOICES: tuple[int, ...] = tuple(range(5, 61, 5))  # R12
SLOT_MINUTES_DEFAULT = 15
MAX_TOKENS_MIN, MAX_TOKENS_MAX = 1, 200  # R13; the upper bound is app-only
LATEST_END = time(23, 59)  # §7: no slot crosses midnight
REASON_MAX = 200


def _err(kind: str, **ctx: Any) -> PydanticCustomError:
    return PydanticCustomError(kind, f"errors.{kind}", ctx or None)


# ── Helpers (module-level, unit-tested) ──────────────────────────────────
def minutes_between(start: time, end: time) -> int:
    return (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)


def calc_max_tokens(start: time, end: time, slot_minutes: int) -> int:
    """R13's pre-fill: whole patient turns in the session, capped at 200.

    Assumes a validated slot (end > start, duration ≥ `slot_minutes`), so the
    result is at least 1."""
    return min(minutes_between(start, end) // slot_minutes, MAX_TOKENS_MAX)


def parse_time(value: Any) -> time:
    """A whole-minute local time (R8). "HH:MM" from a form, or a `time`.
    Seconds, fractions and time zones are refused."""
    if isinstance(value, time):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if len(text) != 5 or text[2] != ":":  # exactly "HH:MM"
            raise _err("schedule_time_invalid")
        try:
            parsed = time.fromisoformat(text)
        except ValueError:
            raise _err("schedule_time_invalid") from None
    else:
        raise _err("schedule_time_invalid")
    if parsed.second or parsed.microsecond or parsed.tzinfo is not None:
        raise _err("schedule_time_invalid")
    if parsed > LATEST_END:
        raise _err("schedule_time_invalid")
    return parsed


def _optional_time(value: Any) -> time | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return parse_time(value)


def _check_end(start: time | None, end: time | None) -> None:
    if start is not None and end is not None and end <= start:
        raise _err("schedule_time_order")


def parse_slot_minutes(value: Any) -> int:
    if value is None or (isinstance(value, str) and not value.strip()):
        return SLOT_MINUTES_DEFAULT
    number = _whole_number(value)
    if number not in SLOT_MINUTES_CHOICES:
        raise _err("schedule_slot_minutes_invalid")
    return number


def parse_max_tokens(value: Any) -> int | None:
    """Blank means "not given" (None): for a slot the service then stores
    `calc_max_tokens`; for an extra it means "inherit the anchor's cap"."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    number = _whole_number(value, kind="schedule_max_tokens_invalid")
    if not MAX_TOKENS_MIN <= number <= MAX_TOKENS_MAX:
        raise _err("schedule_max_tokens_invalid")
    return number


def _whole_number(value: Any, kind: str = "schedule_slot_minutes_invalid") -> int:
    if isinstance(value, bool):
        raise _err(kind)
    if isinstance(value, int):
        return value
    text = value.strip() if isinstance(value, str) else ""
    # ASCII digits only: `isdigit()` alone accepts "²", which `int()` refuses.
    if text.isascii() and text.isdigit():
        return int(text)
    raise _err(kind)


def parse_weekday(value: Any) -> int:
    number = _whole_number(value, kind="choice_invalid")
    if not 0 <= number <= 6:
        raise _err("choice_invalid")
    return number


def _parse_uuid(value: Any) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value.strip())
        except ValueError:
            raise _err("choice_invalid") from None
    raise _err("choice_invalid")


def _future_date(value: Any) -> date:
    """R5: an exception's date is today (PKT) or later."""
    if isinstance(value, date) and not hasattr(value, "hour"):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = date.fromisoformat(value.strip())
        except ValueError:
            raise _err("date_invalid") from None
    else:
        raise _err("date_invalid")
    if parsed < dbtypes.today_pk():
        raise _err("schedule_past_date")
    return parsed


def _reason(value: Any) -> str | None:
    cleaned = clean_text(value)
    if cleaned is not None and len(cleaned) > REASON_MAX:
        raise _err("text_too_long", max=REASON_MAX)
    return cleaned


def _end_after_start_and_long_enough(end: time, info: ValidationInfo) -> time:
    start = info.data.get("start_time")
    _check_end(start, end)
    slot_minutes = info.data.get("slot_minutes")
    if start is not None and slot_minutes and minutes_between(start, end) < slot_minutes:
        raise _err("schedule_too_short")
    return end


# ── Weekly slots ─────────────────────────────────────────────────────────
class _SlotFields(BaseModel):
    """Shared by add and edit. Field order matters: `end_time`'s validator
    reads `start_time` and `slot_minutes`."""

    model_config = ConfigDict(extra="forbid")

    clinic_id: uuid.UUID
    slot_minutes: int = SLOT_MINUTES_DEFAULT
    start_time: time
    end_time: time
    max_tokens: int | None = None

    @field_validator("clinic_id", mode="before")
    @classmethod
    def _clinic(cls, v: Any) -> uuid.UUID:
        return _parse_uuid(v)

    @field_validator("slot_minutes", mode="before")
    @classmethod
    def _slot_minutes(cls, v: Any) -> int:
        return parse_slot_minutes(v)

    @field_validator("start_time", mode="before")
    @classmethod
    def _start(cls, v: Any) -> time:
        return parse_time(v)

    @field_validator("end_time", mode="before")
    @classmethod
    def _end_parse(cls, v: Any) -> time:
        return parse_time(v)

    @field_validator("end_time")
    @classmethod
    def _end_rules(cls, v: time, info: ValidationInfo) -> time:
        return _end_after_start_and_long_enough(v, info)

    @field_validator("max_tokens", mode="before")
    @classmethod
    def _max_tokens(cls, v: Any) -> int | None:
        return parse_max_tokens(v)

    def resolved_max_tokens(self) -> int:
        """What the service stores: the doctor's value, or `calc` when blank (R13)."""
        if self.max_tokens is not None:
            return self.max_tokens
        return calc_max_tokens(self.start_time, self.end_time, self.slot_minutes)


class SlotCreate(_SlotFields):
    """Add one slot per chosen weekday (R9: all or nothing)."""

    weekdays: list[int] = Field(default=None, validate_default=True)  # type: ignore[assignment]

    @field_validator("weekdays", mode="before")
    @classmethod
    def _weekdays(cls, v: Any) -> list[int]:
        if v is None or v == "" or v == []:
            raise _err("schedule_weekday_required")
        values = v if isinstance(v, list | tuple) else [v]
        return sorted({parse_weekday(item) for item in values})


class SlotUpdate(_SlotFields):
    """Edit one slot. A blank `max_tokens` resets it to `calc` (R13)."""

    weekday: int

    @field_validator("weekday", mode="before")
    @classmethod
    def _weekday(cls, v: Any) -> int:
        return parse_weekday(v)


# ── Exceptions ───────────────────────────────────────────────────────────
class TimeOffCreate(BaseModel):
    """Time off on one date: a whole day or a window; one clinic, or all
    clinics when `clinic_id` is None (§4.2)."""

    model_config = ConfigDict(extra="forbid")

    exception_date: date
    clinic_id: uuid.UUID | None = None
    start_time: time | None = None
    end_time: time | None = None
    emergency: bool = False
    reason: str | None = None

    @field_validator("exception_date", mode="before")
    @classmethod
    def _date(cls, v: Any) -> date:
        return _future_date(v)

    @field_validator("clinic_id", mode="before")
    @classmethod
    def _clinic(cls, v: Any) -> uuid.UUID | None:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        return _parse_uuid(v)

    @field_validator("start_time", "end_time", mode="before")
    @classmethod
    def _times(cls, v: Any) -> time | None:
        return _optional_time(v)

    @field_validator("end_time")
    @classmethod
    def _window(cls, v: time | None, info: ValidationInfo) -> time | None:
        start = info.data.get("start_time")
        if (start is None) != (v is None) and "start_time" in info.data:
            raise _err("schedule_time_invalid")  # both or neither
        _check_end(start, v)
        return v

    @field_validator("emergency", mode="before")
    @classmethod
    def _emergency(cls, v: Any) -> bool:
        # An HTML checkbox sends "on" when ticked and nothing otherwise.
        return v in (True, "on", "true", "1")

    @field_validator("reason", mode="before")
    @classmethod
    def _reason(cls, v: Any) -> str | None:
        return _reason(v)


class ExtraCreate(BaseModel):
    """Extra hours at one clinic on one date. A blank `max_tokens` stores NULL:
    the extra inherits its anchor slot's cap, read live (R13, decision 18)."""

    model_config = ConfigDict(extra="forbid")

    exception_date: date
    clinic_id: uuid.UUID
    start_time: time
    end_time: time
    max_tokens: int | None = None
    reason: str | None = None

    @field_validator("exception_date", mode="before")
    @classmethod
    def _date(cls, v: Any) -> date:
        return _future_date(v)

    @field_validator("clinic_id", mode="before")
    @classmethod
    def _clinic(cls, v: Any) -> uuid.UUID:
        return _parse_uuid(v)

    @field_validator("start_time", "end_time", mode="before")
    @classmethod
    def _times(cls, v: Any) -> time:
        return parse_time(v)

    @field_validator("end_time")
    @classmethod
    def _order(cls, v: time, info: ValidationInfo) -> time:
        _check_end(info.data.get("start_time"), v)
        return v

    @field_validator("max_tokens", mode="before")
    @classmethod
    def _max_tokens(cls, v: Any) -> int | None:
        return parse_max_tokens(v)

    @field_validator("reason", mode="before")
    @classmethod
    def _reason(cls, v: Any) -> str | None:
        return _reason(v)
