"""Request and response models for the patient profile (profile SPEC §6.3).

Every validation failure raises `PydanticCustomError(<type>, "errors.<key>")`
so the error's `msg` is exactly an i18n key — the envelope and the web forms
both translate it. Length and choice checks therefore live in validators
rather than in `Field(max_length=...)` / `Literal`, whose built-in messages
are English prose, not keys.

Request models forbid unknown fields, which is what keeps the passport
number, email, role, and status out of reach of this feature (AC-07).
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError

from ..db import types as dbtypes

Gender = Literal["female", "male", "other", "prefer_not_to_say"]
BloodGroup = Literal["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"]
Severity = Literal["mild", "moderate", "severe", "unknown"]

GENDERS: tuple[str, ...] = ("female", "male", "other", "prefer_not_to_say")
BLOOD_GROUPS: tuple[str, ...] = ("A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-")
SEVERITIES: tuple[str, ...] = ("mild", "moderate", "severe", "unknown")

PHONE_PATTERN = r"^\+92[0-9]{10}$"
EARLIEST_DATE = date(1900, 1, 1)
NAME_MIN, NAME_MAX = 2, 120
REACTION_MAX = 255
EMERGENCY_CONTACT_MAX = 255
STRENGTH_MAX = FREQUENCY_MAX = 50
NOTES_MAX = 500

_PHONE_RE = re.compile(PHONE_PATTERN)
# Ordinary whitespace is collapsed; any *other* control character is refused.
_WHITESPACE_RUN = re.compile(r"[ \t\r\n]+")


def _err(kind: str, **ctx: Any) -> PydanticCustomError:
    return PydanticCustomError(kind, f"errors.{kind}", ctx or None)


# ── Helpers (module-level, unit-tested) ──────────────────────────────────
def clean_text(value: str | None) -> str | None:
    """Trim, collapse whitespace runs to one space, map "" to None, and reject
    control characters. Urdu and other scripts pass through untouched."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise _err("text_invalid")
    value = _WHITESPACE_RUN.sub(" ", value).strip()
    if any(unicodedata.category(c) == "Cc" for c in value):
        raise _err("text_invalid")
    return value or None


def normalise_name(value: str) -> str:
    """Duplicate-detection key (BL-19)."""
    return (clean_text(value) or "").casefold()


def check_past_date(value: date | None) -> date | None:
    if value is None:
        return None
    if value < EARLIEST_DATE:
        raise _err("date_too_early")
    if value > dbtypes.today_pk():
        raise _err("date_in_future")
    return value


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        raise _err("date_invalid")
    if isinstance(value, date):
        return check_past_date(value)
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value.strip())
        except ValueError:
            raise _err("date_invalid") from None
        return check_past_date(parsed)
    raise _err("date_invalid")


def _required_name(value: Any) -> str:
    cleaned = clean_text(value)
    if cleaned is None or len(cleaned) < NAME_MIN:
        raise _err("text_required")
    if len(cleaned) > NAME_MAX:
        raise _err("text_too_long", max=NAME_MAX)
    return cleaned


def _optional_text(value: Any, max_len: int) -> str | None:
    cleaned = clean_text(value)
    if cleaned is not None and len(cleaned) > max_len:
        raise _err("text_too_long", max=max_len)
    return cleaned


def _choice(value: Any, allowed: tuple[str, ...]) -> Any:
    if value is None or value in allowed:
        return value
    raise _err("choice_invalid")


# ── Basic details ────────────────────────────────────────────────────────
class ProfileUpdateRequest(BaseModel):
    """PATCH semantics: only fields in `model_fields_set` are applied.
    An explicit null clears an optional field; `full_name` cannot be null."""

    model_config = ConfigDict(extra="forbid")

    full_name: str | None = None
    date_of_birth: date | None = None
    gender: Gender | None = None
    blood_group: BloodGroup | None = None
    phone_e164: str | None = None
    emergency_contact: str | None = None

    @field_validator("full_name", mode="before")
    @classmethod
    def _full_name(cls, v: Any) -> str:
        cleaned = clean_text(v)
        if cleaned is None or len(cleaned) < NAME_MIN:
            raise _err("name_required")
        if len(cleaned) > NAME_MAX:
            raise _err("text_too_long", max=NAME_MAX)
        return cleaned

    @field_validator("date_of_birth", mode="before")
    @classmethod
    def _dob(cls, v: Any) -> date | None:
        return _parse_date(v)

    @field_validator("gender", mode="before")
    @classmethod
    def _gender(cls, v: Any) -> Any:
        return _choice(v, GENDERS)

    @field_validator("blood_group", mode="before")
    @classmethod
    def _blood_group(cls, v: Any) -> Any:
        return _choice(v, BLOOD_GROUPS)

    @field_validator("phone_e164", mode="before")
    @classmethod
    def _phone(cls, v: Any) -> str | None:
        if v is None:
            return None
        if not isinstance(v, str) or not _PHONE_RE.match(v.strip()):
            raise _err("phone_invalid")
        return v.strip()

    @field_validator("emergency_contact", mode="before")
    @classmethod
    def _emergency_contact(cls, v: Any) -> str | None:
        return _optional_text(v, EMERGENCY_CONTACT_MAX)


# ── Allergies ────────────────────────────────────────────────────────────
class _AllergyFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("reaction", mode="before", check_fields=False)
    @classmethod
    def _reaction(cls, v: Any) -> str | None:
        return _optional_text(v, REACTION_MAX)

    @field_validator("severity", mode="before", check_fields=False)
    @classmethod
    def _severity(cls, v: Any) -> Any:
        return _choice(v, SEVERITIES)


class AllergyCreate(_AllergyFields):
    # `validate_default` makes a missing substance run the validator, so it
    # reports `errors.text_required` rather than pydantic's "Field required".
    substance: str = Field(default=None, validate_default=True)  # type: ignore[assignment]
    reaction: str | None = None
    severity: Severity | None = None

    @field_validator("substance", mode="before")
    @classmethod
    def _substance(cls, v: Any) -> str:
        return _required_name(v)


class AllergyUpdate(_AllergyFields):
    substance: str | None = None  # may not be set to null
    reaction: str | None = None
    severity: Severity | None = None

    @field_validator("substance", mode="before")
    @classmethod
    def _substance(cls, v: Any) -> str:
        return _required_name(v)


# ── Chronic conditions ───────────────────────────────────────────────────
class ConditionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default=None, validate_default=True)  # type: ignore[assignment]
    onset_date: date | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v: Any) -> str:
        return _required_name(v)

    @field_validator("onset_date", mode="before")
    @classmethod
    def _onset(cls, v: Any) -> date | None:
        return _parse_date(v)


class ConditionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None  # may not be set to null
    onset_date: date | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v: Any) -> str:
        return _required_name(v)

    @field_validator("onset_date", mode="before")
    @classmethod
    def _onset(cls, v: Any) -> date | None:
        return _parse_date(v)


# ── Current medications ──────────────────────────────────────────────────
class _MedicationFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("strength", mode="before", check_fields=False)
    @classmethod
    def _strength(cls, v: Any) -> str | None:
        return _optional_text(v, STRENGTH_MAX)

    @field_validator("frequency", mode="before", check_fields=False)
    @classmethod
    def _frequency(cls, v: Any) -> str | None:
        return _optional_text(v, FREQUENCY_MAX)

    @field_validator("started_on", mode="before", check_fields=False)
    @classmethod
    def _started_on(cls, v: Any) -> date | None:
        return _parse_date(v)

    @field_validator("notes", mode="before", check_fields=False)
    @classmethod
    def _notes(cls, v: Any) -> str | None:
        return _optional_text(v, NOTES_MAX)


class MedicationCreate(_MedicationFields):
    name: str = Field(default=None, validate_default=True)  # type: ignore[assignment]
    strength: str | None = None
    frequency: str | None = None
    started_on: date | None = None
    notes: str | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v: Any) -> str:
        return _required_name(v)


class MedicationUpdate(_MedicationFields):
    name: str | None = None  # may not be set to null
    strength: str | None = None
    frequency: str | None = None
    started_on: date | None = None
    notes: str | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v: Any) -> str:
        return _required_name(v)


# ── Responses ────────────────────────────────────────────────────────────
class AllergyOut(BaseModel):
    id: uuid.UUID
    substance: str
    reaction: str | None
    severity: str | None  # raw stored value — may be a legacy value outside `Severity`
    recorded_at: datetime
    updated_at: datetime | None
    editable: bool


class ConditionOut(BaseModel):
    id: uuid.UUID
    name: str
    onset_date: date | None
    status: str  # raw stored value; never "resolved" in responses
    icd10_code: str | None  # read-only, never written by this feature
    recorded_at: datetime | None
    updated_at: datetime | None
    editable: bool


class MedicationOut(BaseModel):
    id: uuid.UUID
    name: str
    strength: str | None
    frequency: str | None
    started_on: date | None
    notes: str | None
    recorded_at: datetime
    updated_at: datetime | None
    editable: bool


class PatientProfileOut(BaseModel):
    passport_no: str
    full_name: str
    email: str | None  # user_profile.email is nullable in the shared schema
    phone_e164: str | None  # unmasked — owner-only endpoint
    date_of_birth: date | None
    age_years: int | None
    gender: str | None  # raw stored value
    blood_group: str | None  # raw stored value
    emergency_contact: str | None
    allergies: list[AllergyOut]
    conditions: list[ConditionOut]
    medications: list[MedicationOut]
