"""Request and response models (SPEC 6.2).

Two absences are deliberate and security-relevant:

* `LoginRequest` has no `role` field. The Login screen's Patient/Doctor toggle
  is presentational; letting the client send a role would let it influence
  authorisation (SPEC AC-23, BL-19).
* No registration model exposes `is_verified`, `verified_by`, `verified_at`, or
  `status`. A self-registered doctor is always unverified (SPEC BL-01, G7).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

Role = Literal["patient", "doctor", "clinic_admin"]
Locale = Literal["en", "ur"]

PASSWORD_MIN = 10
PASSWORD_MAX = 128


def _normalise_email(value: str) -> str:
    """Trim and lower-case so casing can never create a second account.

    This is what makes Postgres CITEXT unnecessary (SPEC BL-11).
    """
    return value.strip().lower()


class _RegisterBase(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=PASSWORD_MIN, max_length=PASSWORD_MAX)
    phone_e164: str | None = Field(default=None, pattern=r"^\+92[0-9]{10}$")
    preferred_locale: Locale = "en"

    @field_validator("email", mode="after")
    @classmethod
    def _norm(cls, v: str) -> str:
        return _normalise_email(v)

    @field_validator("full_name", mode="after")
    @classmethod
    def _trim(cls, v: str) -> str:
        return v.strip()


class PatientRegisterRequest(_RegisterBase):
    role: Literal["patient"] = "patient"


class DoctorRegisterRequest(_RegisterBase):
    """FR3 — creates the account only. Verification is a clinic admin's act."""

    role: Literal["doctor"]
    pmdc_number: str = Field(min_length=3, max_length=32)
    specialty: str = Field(min_length=2, max_length=80)
    primary_clinic_id: uuid.UUID


RegisterRequest = Annotated[
    PatientRegisterRequest | DoctorRegisterRequest,
    Field(discriminator="role"),
]


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=PASSWORD_MAX)

    @field_validator("email", mode="after")
    @classmethod
    def _norm(cls, v: str) -> str:
        return _normalise_email(v)


class SessionOut(BaseModel):
    """Returned by register, login, and refresh. Tokens live in cookies only."""

    user_id: uuid.UUID
    role: Role
    full_name: str
    locale: Locale
    landing_route: str
    access_expires_at: datetime


class MeOut(BaseModel):
    user_id: uuid.UUID
    role: Role
    full_name: str
    email: EmailStr
    phone_masked: str | None
    locale: Locale
    status: Literal["pending_verification", "active", "suspended"]
    is_verified_doctor: bool
    clinic_ids: list[uuid.UUID]
    passport_no: str | None
    last_login_at: datetime | None


class ClinicOut(BaseModel):
    """Feeds the doctor-registration clinic selector (SPEC BL-21).

    Intentionally minimal — no address, no phone. A public endpoint should
    expose only what the selector needs.
    """

    id: uuid.UUID
    name: str
    city: str


def mask_phone(phone: str | None) -> str | None:
    if not phone or len(phone) < 4:
        return None
    return f"{phone[:6]} ••• ••{phone[-2:]}"


def landing_route_for(role: str, locale: str = "en") -> str:
    return {
        "patient": f"/{locale}/patient",
        "doctor": f"/{locale}/doctor",
        "clinic_admin": f"/{locale}/admin",
    }[role]
