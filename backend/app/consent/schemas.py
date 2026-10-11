"""Request and response models for consent (passport SPEC §8; P9 spec §6.3).

Validation failures raise `PydanticCustomError(<type>, "errors.<key>")`, as in
`profile/schemas.py`, so the error's `msg` is an existing i18n key.

Request models forbid unknown fields. A grantee is named by exactly one of
`doctor_id`/`clinic_id` (from the grantee search, P9) or `pmdc_number`/
`clinic_name` (P4). Every grant lists at least one section (P9 BL-02).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from ..profile.schemas import _err, _optional_text

GranteeType = Literal["doctor", "clinic"]

# The record sections a grant can share, in canonical (display and storage)
# order. `prescriptions`, `lab_reports` and `visits` hold nothing until P2/P3.
SECTIONS: tuple[str, ...] = (
    "basics",
    "allergies",
    "conditions",
    "medications",
    "prescriptions",
    "lab_reports",
    "visits",
)

DURATIONS: dict[str, timedelta | None] = {
    "24h": timedelta(days=1),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "until_revoked": None,
}
DEFAULT_DURATION = "30d"

PMDC_MAX = 32  # doctor.pmdc_number
CLINIC_NAME_MAX = 160  # clinic.name
QUERY_MIN, QUERY_MAX = 2, 80


def normalise_sections(value: Any) -> list[str]:
    """At least one known key; duplicates dropped; canonical order (BL-02)."""
    if not isinstance(value, list) or not value:
        raise _err("sections_required")
    if any(not isinstance(v, str) or v not in SECTIONS for v in value):
        raise _err("section_invalid")
    chosen = set(value)
    return [s for s in SECTIONS if s in chosen]


def expiry_for(duration: str, granted_at: datetime) -> datetime | None:
    delta = DURATIONS[duration]
    return None if delta is None else granted_at + delta


# ── Requests ─────────────────────────────────────────────────────────────
class GrantCreate(BaseModel):
    """Exactly one grantee identifier, at least one section, and a duration."""

    model_config = ConfigDict(extra="forbid")

    doctor_id: uuid.UUID | None = None
    clinic_id: uuid.UUID | None = None
    pmdc_number: str | None = None
    clinic_name: str | None = None
    sections: list[str]
    duration: str = DEFAULT_DURATION

    @field_validator("pmdc_number", mode="before")
    @classmethod
    def _pmdc(cls, v: Any) -> str | None:
        return _optional_text(v, PMDC_MAX)

    @field_validator("clinic_name", mode="before")
    @classmethod
    def _clinic(cls, v: Any) -> str | None:
        return _optional_text(v, CLINIC_NAME_MAX)

    @field_validator("sections", mode="before")
    @classmethod
    def _sections(cls, v: Any) -> list[str]:
        return normalise_sections(v)

    @field_validator("duration", mode="before")
    @classmethod
    def _duration(cls, v: Any) -> str:
        if v not in DURATIONS:
            raise _err("duration_invalid")
        return v

    @model_validator(mode="after")
    def _exactly_one(self) -> GrantCreate:
        named = [self.doctor_id, self.clinic_id, self.pmdc_number, self.clinic_name]
        if sum(v is not None for v in named) != 1:
            raise _err("choice_invalid")
        return self


class SectionsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sections: list[str]

    @field_validator("sections", mode="before")
    @classmethod
    def _sections(cls, v: Any) -> list[str]:
        return normalise_sections(v)


def clean_query(value: Any) -> str:
    """Grantee search text: trimmed, 2–80 characters."""
    q = _optional_text(value if isinstance(value, str) else None, QUERY_MAX) or ""
    if len(q) < QUERY_MIN:
        raise _err("query_too_short", min=QUERY_MIN)
    return q


# ── Responses ────────────────────────────────────────────────────────────
class ConsentGrantOut(BaseModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    grantee_type: str  # raw stored value; DB-constrained to doctor/clinic
    grantee_id: uuid.UUID
    # Doctor full name or clinic name. `grantee_id` has no FK, so None if the
    # grantee row no longer exists.
    grantee_name: str | None
    grantee_detail: str | None = None  # doctor specialty / clinic city
    scope_sections: list[str]  # P4 name, kept for compatibility
    sections: list[str]  # same value; the name new clients use
    granted_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    replaced_by: uuid.UUID | None = None
    active: bool


class GranteeOut(BaseModel):
    type: GranteeType
    id: uuid.UUID
    name: str
    detail: str | None  # specialty / city


class AccessLogEntry(BaseModel):
    occurred_at: datetime
    section: str
    accessor_name: str | None
    accessor_specialty: str | None
    clinic_name: str | None


class AccessLogPage(BaseModel):
    items: list[AccessLogEntry]
    next_cursor: str | None
