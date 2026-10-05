"""Request and response models for Medical Passport consent (passport SPEC §8).

Validation failures raise `PydanticCustomError(<type>, "errors.<key>")`, as in
`profile/schemas.py`, so the error's `msg` is an existing i18n key.

Request models forbid unknown fields: the grantee is chosen by PMDC number or
clinic name only, never by a raw id, and `scope_sections`/`expires_at` are not
settable here (passport SPEC §9.3, §9.5).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from ..profile.schemas import _err, _optional_text

GranteeType = Literal["doctor", "clinic"]

PMDC_MAX = 32  # doctor.pmdc_number
CLINIC_NAME_MAX = 160  # clinic.name


# ── Requests ─────────────────────────────────────────────────────────────
class GrantCreate(BaseModel):
    """Exactly one of `pmdc_number` (BL-02) or `clinic_name` (BL-03)."""

    model_config = ConfigDict(extra="forbid")

    pmdc_number: str | None = None
    clinic_name: str | None = None

    @field_validator("pmdc_number", mode="before")
    @classmethod
    def _pmdc(cls, v: Any) -> str | None:
        return _optional_text(v, PMDC_MAX)

    @field_validator("clinic_name", mode="before")
    @classmethod
    def _clinic(cls, v: Any) -> str | None:
        return _optional_text(v, CLINIC_NAME_MAX)

    @model_validator(mode="after")
    def _exactly_one(self) -> GrantCreate:
        if (self.pmdc_number is None) == (self.clinic_name is None):
            raise _err("choice_invalid")
        return self


# ── Responses ────────────────────────────────────────────────────────────
class ConsentGrantOut(BaseModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    grantee_type: str  # raw stored value; DB-constrained to doctor/clinic
    grantee_id: uuid.UUID
    # Doctor full name or clinic name. `grantee_id` has no FK, so None if the
    # grantee row no longer exists.
    grantee_name: str | None
    scope_sections: list[str]  # always [] in this version — all-or-nothing
    granted_at: datetime
    expires_at: datetime | None  # always None in this version (BL-12)
    revoked_at: datetime | None
