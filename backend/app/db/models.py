"""ORM models for the identity baseline (TDD 3.2, 3.3, 3.8).

These map onto tables that already exist in the shared Supabase project (a
larger CuraNode-AI schema this repo's auth slice is one piece of) — table and
column names follow that existing schema exactly, not this repo's original
naming. Python-facing attribute names are kept close to the original design
where the two didn't collide, so the rest of the codebase (schemas, service
logic) needed minimal churn.

`user_profile`/`clinic`/`patient`/`doctor`/`clinic_staff`/`doctor_affiliation`
are pre-existing tables owned by the shared schema — this module never
creates or drops them. Only `audit_log` is owned outright by this codebase,
plus a handful of additive columns on `user_profile`/`doctor` that this
feature needs and the shared schema didn't have yet (see the Alembic
migration for exactly what was added).
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

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

from .types import utcnow


class Base(DeclarativeBase):
    pass


# ── Enumerated types (TDD 3.2) ───────────────────────────────────────────
class UserRole(str, enum.Enum):
    PATIENT = "patient"
    DOCTOR = "doctor"
    # Value is "admin", not "clinic_admin" — the shared schema's
    # `user_profile_role_check` CHECK constraint only allows
    # patient/doctor/staff/admin. "staff" (a separate, lower-privileged
    # clinic role) has no corresponding dependency in this codebase yet.
    CLINIC_ADMIN = "admin"


class AccountStatus(str, enum.Enum):
    # `PENDING_VERIFICATION` is retained for future use but is unreachable via
    # self-registration — accounts are active on creation (SPEC BL-09).
    PENDING_VERIFICATION = "pending_verification"
    ACTIVE = "active"
    SUSPENDED = "suspended"


class LocaleCode(str, enum.Enum):
    EN = "en"
    UR = "ur"


class VerificationStatus(str, enum.Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"


def _ts(**kw: object) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), **kw)  # type: ignore[arg-type]


def _enum(enum_cls: type[enum.Enum], name: str) -> SAEnum:
    """Store the enum's *value* (not its Python name) and return members on load.

    `native_enum=False` keeps this portable: SQLite gets a plain VARCHAR,
    Postgres can be switched to a native type later without changing models.
    `create_constraint` defaults to False in SQLAlchemy 2.0, matching the
    existing shared-schema columns, which carry no CHECK constraint either.
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda e: [m.value for m in e],
    )


# ── Identity ─────────────────────────────────────────────────────────────
class Profile(Base):
    """App-owned identity data, keyed 1:1 to Supabase's `auth.users.id`.

    Maps onto the pre-existing `user_profile` table. Credentials live
    entirely in Supabase Auth — this table never stores a password hash.
    `id` is always supplied explicitly from the Supabase auth user, never
    generated locally.
    """

    __tablename__ = "user_profile"

    id: Mapped[uuid.UUID] = mapped_column("user_id", Uuid, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    phone_e164: Mapped[str | None] = mapped_column("phone", String(20), nullable=True)
    role: Mapped[UserRole] = mapped_column(_enum(UserRole, "user_role"), nullable=False)
    status: Mapped[AccountStatus] = mapped_column(
        _enum(AccountStatus, "account_status"),
        nullable=False,
        default=AccountStatus.ACTIVE,
    )
    preferred_locale: Mapped[LocaleCode] = mapped_column(
        "locale", _enum(LocaleCode, "locale_code"), nullable=False, default=LocaleCode.EN
    )
    # Nullable at the DB level (the pre-existing schema had no such column at
    # all until this feature added it, and one row predates it) — always
    # supplied by this app's registration path regardless.
    full_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    last_login_at: Mapped[datetime | None] = _ts(nullable=True)
    failed_logins: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = _ts(nullable=True)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _ts(nullable=False, default=utcnow)

    patient: Mapped[Patient | None] = relationship(back_populates="user", uselist=False)
    doctor: Mapped[Doctor | None] = relationship(
        back_populates="user", uselist=False, foreign_keys="Doctor.user_id"
    )

    __table_args__ = (Index("idx_user_profile_role_status", "role", "status"),)


class Clinic(Base):
    __tablename__ = "clinic"

    id: Mapped[uuid.UUID] = mapped_column("clinic_id", Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    # Facility type (e.g. "hospital", "clinic") — required by the shared schema.
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    city: Mapped[str | None] = mapped_column(String(80), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_at: Mapped[datetime] = _ts(nullable=False, default=utcnow)


class Patient(Base):
    """FR1 — the Medical Passport. `passport_no` is issued once and never changes."""

    __tablename__ = "patient"

    id: Mapped[uuid.UUID] = mapped_column("patient_id", Uuid, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("user_profile.user_id"), unique=True, nullable=False
    )
    passport_no: Mapped[str] = mapped_column(
        "passport_uid", String(16), unique=True, nullable=False
    )
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    date_of_birth: Mapped[date | None] = mapped_column(nullable=True)
    gender: Mapped[str | None] = mapped_column(String(20), nullable=True)
    blood_group: Mapped[str | None] = mapped_column(String(8), nullable=True)
    created_at: Mapped[datetime] = _ts(nullable=False, default=utcnow)

    user: Mapped[Profile] = relationship(back_populates="patient")


class Doctor(Base):
    """FR3 — a doctor may self-register but always lands unverified.

    `verified_has_verifier` makes verification impossible to fake by flipping
    a status: a verified doctor always names a responsible human.
    """

    __tablename__ = "doctor"

    id: Mapped[uuid.UUID] = mapped_column("doctor_id", Uuid, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("user_profile.user_id"), unique=True, nullable=False
    )
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    specialty: Mapped[str | None] = mapped_column(String(80), nullable=True)
    pmdc_number: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    verification_status: Mapped[VerificationStatus] = mapped_column(
        _enum(VerificationStatus, "verification_status"),
        nullable=False,
        default=VerificationStatus.PENDING,
    )
    verified_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user_profile.user_id"), nullable=True
    )
    verified_at: Mapped[datetime | None] = _ts(nullable=True)

    user: Mapped[Profile] = relationship(back_populates="doctor", foreign_keys=[user_id])

    __table_args__ = (
        CheckConstraint(
            "verification_status <> 'verified' "
            "OR (verified_by IS NOT NULL AND verified_at IS NOT NULL)",
            name="verified_has_verifier",
        ),
    )


class DoctorAffiliation(Base):
    """A doctor's clinic membership (many-to-many, date-ranged).

    Replaces this feature's original single `primary_clinic_id` column — the
    shared schema models doctor↔clinic as a relationship in its own right, so
    a doctor can move between or hold multiple affiliations over time.
    Registration creates exactly one open-ended, active row here.
    """

    __tablename__ = "doctor_affiliation"

    id: Mapped[uuid.UUID] = mapped_column("affiliation_id", Uuid, primary_key=True)
    doctor_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("doctor.doctor_id"), nullable=False
    )
    clinic_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("clinic.clinic_id"), nullable=False
    )
    start_date: Mapped[date] = mapped_column(nullable=False)
    end_date: Mapped[date | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")


class ClinicStaff(Base):
    __tablename__ = "clinic_staff"

    id: Mapped[uuid.UUID] = mapped_column("staff_id", Uuid, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("user_profile.user_id"), nullable=False
    )
    clinic_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("clinic.clinic_id"), nullable=False
    )
    # This staff member's function at the clinic (e.g. "admin", "front_desk")
    # — distinct from the account-level `Profile.role`.
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    joined_at: Mapped[datetime] = mapped_column(
        "joined_at", DateTime(timezone=True), nullable=False, default=utcnow
    )


class AuditLog(Base):
    """FR5, NFR17 — append-only. Never updated or deleted by the application.

    Owned outright by this codebase — the shared schema's `access_log` table
    is shaped for clinical record-access/consent auditing (a required
    `resource_id`, break-glass flag, consent linkage) and doesn't fit generic
    auth events like login/logout/lockout, so this stays a separate table
    rather than being force-fit into that one.
    """

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
