"""Clinic-admin registration request shape.

Reuses the password bounds and locale type from `identity/schemas.py`
read-only — this module never imports or modifies anything in `identity`.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, EmailStr, Field, field_validator

from ..identity.schemas import PASSWORD_MAX, PASSWORD_MIN, Locale


class AdminRegisterRequest(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=PASSWORD_MIN, max_length=PASSWORD_MAX)
    clinic_id: uuid.UUID
    invite_code: str = Field(min_length=6, max_length=64)
    preferred_locale: Locale = "en"

    @field_validator("email", mode="after")
    @classmethod
    def _norm(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("full_name", mode="after")
    @classmethod
    def _trim(cls, v: str) -> str:
        return v.strip()
