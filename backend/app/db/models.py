"""ORM models for the identity baseline (TDD 3.2, 3.3, 3.8).

Only the tables authentication needs exist here. Clinical tables belong to
later features and are deliberately absent.

Email is stored **already normalised** (trimmed, lower-cased) by the service
layer, which is why a plain unique String replaces Postgres CITEXT without any
loss of the case-insensitivity guarantee (SPEC BL-11).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .types import utcnow, uuid7


class Base(DeclarativeBase):
    pass


# ── Enumerated types (TDD 3.2) ───────────────────────────────────────────
class UserRole(str, enum.Enum):
    PATIENT = "patient"
    DOCTOR = "doctor"
    CLINIC_ADMIN = "clinic_admin"


class AccountStatus(str, enum.Enum):
    # `PENDING_VERIFICATION` is retained for future use but is unreachable via
    # self-registration — accounts are active on creation (SPEC BL-09).
    PENDING_VERIFICATION = "pending_verification"
    ACTIVE = "active"
    SUSPENDED = "suspended"


class LocaleCode(str, enum.Enum):
    EN = "en"
    UR = "ur"


def _ts(**kw: object) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), **kw)  # type: ignore[arg-type]


def _enum(enum_cls: type[enum.Enum], name: str) -> SAEnum:
    """Store the enum's *value* (not its Python name) and return members on load.

    `native_enum=False` keeps this portable: SQLite gets a VARCHAR + CHECK,
    Postgres can be switched to a native type later without changing models.
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda e: [m.value for m in e],
    )


# ── Identity ─────────────────────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    phone_e164: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[UserRole] = mapped_column(_enum(UserRole, "user_role"), nullable=False)
    status: Mapped[AccountStatus] = mapped_column(
        _enum(AccountStatus, "account_status"),
        nullable=False,
        default=AccountStatus.ACTIVE,
    )
    preferred_locale: Mapped[LocaleCode] = mapped_column(
        _enum(LocaleCode, "locale_code"), nullable=False, default=LocaleCode.EN
    )
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    last_login_at: Mapped[datetime | None] = _ts(nullable=True)
    failed_logins: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = _ts(nullable=True)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _ts(nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = _ts(nullable=False, default=utcnow, onupdate=utcnow)

    patient: Mapped[Patient | None] = relationship(back_populates="user", uselist=False)
    doctor: Mapped[Doctor | None] = relationship(
        back_populates="user", uselist=False, foreign_keys="Doctor.user_id"
    )

    __table_args__ = (Index("idx_users_role_status", "role", "status"),)


class Clinic(Base):
    __tablename__ = "clinics"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    city: Mapped[str] = mapped_column(String(80), nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False, default="")
    timezone: Mapped[str] = mapped_column(String(40), nullable=False, default="Asia/Karachi")
    created_at: Mapped[datetime] = _ts(nullable=False, default=utcnow)


class Patient(Base):
    """FR1 — the Medical Passport. `passport_no` is issued once and never changes."""

    __tablename__ = "patients"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id"), primary_key=True
    )
    passport_no: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    date_of_birth: Mapped[datetime | None] = _ts(nullable=True)
    gender: Mapped[str | None] = mapped_column(String(20), nullable=True)
    blood_group: Mapped[str | None] = mapped_column(String(8), nullable=True)
    created_at: Mapped[datetime] = _ts(nullable=False, default=utcnow)

    user: Mapped[User] = relationship(back_populates="patient")


class Doctor(Base):
    """FR3 — a doctor may self-register but always lands unverified.

    `verified_has_verifier` makes verification impossible to fake by flipping a
    boolean: a verified doctor always names a responsible human.
    """

    __tablename__ = "doctors"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id"), primary_key=True
    )
    primary_clinic_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("clinics.id"), nullable=False
    )
    specialty: Mapped[str] = mapped_column(String(80), nullable=False)
    pmdc_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verified_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id"), nullable=True
    )
    verified_at: Mapped[datetime | None] = _ts(nullable=True)
    created_at: Mapped[datetime] = _ts(nullable=False, default=utcnow)

    user: Mapped[User] = relationship(back_populates="doctor", foreign_keys=[user_id])
    clinic: Mapped[Clinic] = relationship()

    __table_args__ = (
        CheckConstraint(
            "is_verified = 0 OR (verified_by IS NOT NULL AND verified_at IS NOT NULL)",
            name="verified_has_verifier",
        ),
    )


class ClinicStaff(Base):
    __tablename__ = "clinic_staff"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id"), primary_key=True
    )
    clinic_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("clinics.id"), primary_key=True
    )
    created_at: Mapped[datetime] = _ts(nullable=False, default=utcnow)


class AuditLog(Base):
    """FR5, NFR17 — append-only. Never updated or deleted by the application."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = _ts(nullable=False, default=utcnow)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    actor_role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_patient_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    resource_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    __table_args__ = (Index("idx_audit_actor", "actor_user_id", "occurred_at"),)
