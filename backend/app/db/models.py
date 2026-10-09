"""ORM models for the identity baseline (TDD 3.2, 3.3, 3.8).

These map onto tables that already exist in the shared Supabase project (a
larger CuraNode-AI schema this repo's auth slice is one piece of) — table and
column names follow that existing schema exactly, not this repo's original
naming. Python-facing attribute names are kept close to the original design
where the two didn't collide, so the rest of the codebase (schemas, service
logic) needed minimal churn.

`user_profile`/`clinic`/`patient`/`doctor`/`clinic_staff`/`doctor_affiliation`
are pre-existing tables owned by the shared schema — this module never
creates or drops them; nor does it own the shared `allergy`/
`chronic_condition` tables, or `doctor_schedule`/`schedule_exception` (D7). Only `audit_log` and `patient_medication` are
owned outright by this codebase, plus a handful of additive columns on
`user_profile`/`doctor`/`allergy`/`chronic_condition` that the shared schema
didn't have yet (see the Alembic migrations for exactly what was added).
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime, time

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    Time,
    Uuid,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .types import utcnow, uuid7


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


class ScheduleExceptionType(str, enum.Enum):
    """`schedule_exception.type` (D7). `leave` and `emergency` both mean
    "unavailable"; `extra` adds hours."""

    LEAVE = "leave"
    EMERGENCY = "emergency"
    EXTRA = "extra"


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
    emergency_contact: Mapped[str | None] = mapped_column(String(255), nullable=True)
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


# ── Doctor schedule (D7) ─────────────────────────────────────────────────
class DoctorSchedule(Base):
    """doctor_schedule — one weekly slot of a doctor at a clinic (D7).

    Shared, pre-existing table (verified against the live schema 2026-10-10).
    A slot belongs to an *affiliation*, which gives both the doctor and the
    clinic. `slot_minutes` is the booked-appointment grid; `max_tokens` caps
    patients per session per date, booked plus walk-in (D7 R12–R14).

    `status` has no CHECK in the live table. `active` is live; D7 removes a
    slot by setting `inactive` and never deletes a row. Readers treat any value
    other than `active` as not live, since other codebases share the table.

    The first four CHECKs mirror the live ones; the last two were added by D7's
    migration (`291c06a2b86f`).
    """

    __tablename__ = "doctor_schedule"

    id: Mapped[uuid.UUID] = mapped_column("schedule_id", Uuid, primary_key=True, default=uuid7)
    affiliation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("doctor_affiliation.affiliation_id", ondelete="CASCADE"), nullable=False
    )
    weekday: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    slot_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")

    __table_args__ = (
        CheckConstraint("end_time > start_time", name="doctor_schedule_check"),
        CheckConstraint("weekday >= 0 AND weekday <= 6", name="doctor_schedule_weekday_check"),
        CheckConstraint("slot_minutes > 0", name="doctor_schedule_slot_minutes_check"),
        CheckConstraint("max_tokens >= 0", name="doctor_schedule_max_tokens_check"),
        CheckConstraint(
            "slot_minutes BETWEEN 5 AND 60 AND slot_minutes % 5 = 0",
            name="doctor_schedule_slot_minutes_grid",
        ),
        CheckConstraint("max_tokens >= 1", name="doctor_schedule_max_tokens_min"),
        Index("idx_doctor_schedule_affiliation_id", "affiliation_id"),
    )


class ScheduleException(Base):
    """schedule_exception — time off on, or extra hours anchored to, one slot
    on one date (D7).

    Shared, pre-existing table. D7's migration (`291c06a2b86f`) added
    `start_time`/`end_time` (time off for a window, or the extra's window;
    both NULL = the whole slot), `removed_at` (soft-remove; rows are never
    deleted) and `max_tokens` (an extra's own cap; NULL = inherit the anchor
    slot's cap, read live), plus their CHECKs and the date index.
    """

    __tablename__ = "schedule_exception"

    id: Mapped[uuid.UUID] = mapped_column("exception_id", Uuid, primary_key=True, default=uuid7)
    schedule_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("doctor_schedule.schedule_id", ondelete="CASCADE"), nullable=False
    )
    exception_date: Mapped[date] = mapped_column(nullable=False)
    kind: Mapped[ScheduleExceptionType] = mapped_column(
        "type", _enum(ScheduleExceptionType, "schedule_exception_type"), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _ts(nullable=False, default=utcnow)
    start_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    end_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    removed_at: Mapped[datetime | None] = _ts(nullable=True)
    max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "type IN ('leave', 'emergency', 'extra')", name="schedule_exception_type_check"
        ),
        CheckConstraint(
            "(start_time IS NULL) = (end_time IS NULL)", name="schedule_exception_times_pair"
        ),
        CheckConstraint(
            "start_time IS NULL OR end_time > start_time", name="schedule_exception_times_order"
        ),
        CheckConstraint(
            "type <> 'extra' OR (start_time IS NOT NULL AND end_time IS NOT NULL)",
            name="schedule_exception_extra_has_times",
        ),
        CheckConstraint(
            "max_tokens IS NULL OR max_tokens >= 1", name="schedule_exception_max_tokens_min"
        ),
        CheckConstraint(
            "max_tokens IS NULL OR type = 'extra'",
            name="schedule_exception_max_tokens_extra_only",
        ),
        Index("idx_schedule_exception_schedule_id", "schedule_id"),
        Index("idx_schedule_exception_date", "exception_date"),
    )


# ── Patient profile (FR2) ────────────────────────────────────────────────
class Allergy(Base):
    """A patient's known allergy.

    Shared, pre-existing table. This repo added `recorded_by`, `updated_at`
    and `removed_at` (patient-profile migration). `severity` is a plain string,
    not `_enum(...)`: the column has no CHECK and holds legacy values that
    `validate_strings=True` would refuse to load. `removed_at` is the
    soft-remove marker — rows are never deleted by this app.
    """

    __tablename__ = "allergy"

    id: Mapped[uuid.UUID] = mapped_column("allergy_id", Uuid, primary_key=True, default=uuid7)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("patient.patient_id", ondelete="CASCADE"), nullable=False
    )
    substance: Mapped[str] = mapped_column(String(255), nullable=False)
    reaction: Mapped[str | None] = mapped_column(String(255), nullable=True)
    severity: Mapped[str | None] = mapped_column(String(20), nullable=True)
    recorded_at: Mapped[datetime] = _ts(nullable=False, default=utcnow)
    recorded_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user_profile.user_id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime | None] = _ts(nullable=True)
    removed_at: Mapped[datetime | None] = _ts(nullable=True)


class ChronicCondition(Base):
    """A long-term condition.

    Shared, pre-existing table. This repo added `recorded_by`, `recorded_at`
    and `updated_at`. There is no `removed_at`: a condition is taken off the
    list by setting `status='resolved'`. `icd10_code` and `risk_level` belong
    to the wider product and are never written here.
    """

    __tablename__ = "chronic_condition"

    id: Mapped[uuid.UUID] = mapped_column("condition_id", Uuid, primary_key=True, default=uuid7)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("patient.patient_id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    icd10_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    onset_date: Mapped[date | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    risk_level: Mapped[str | None] = mapped_column(String(20), nullable=True)
    recorded_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user_profile.user_id", ondelete="SET NULL"), nullable=True
    )
    recorded_at: Mapped[datetime] = _ts(nullable=False, default=utcnow)
    updated_at: Mapped[datetime | None] = _ts(nullable=True)


class PatientMedication(Base):
    """A medicine the patient says they currently take.

    Owned outright by this repo (created by the patient-profile migration).
    Named to avoid confusion with the shared `medicine` catalogue and the
    doctor-owned `prescription_item`. Soft-removed via `removed_at`.
    """

    __tablename__ = "patient_medication"

    id: Mapped[uuid.UUID] = mapped_column("medication_id", Uuid, primary_key=True, default=uuid7)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("patient.patient_id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    strength: Mapped[str | None] = mapped_column(String(50), nullable=True)
    frequency: Mapped[str | None] = mapped_column(String(50), nullable=True)
    started_on: Mapped[date | None] = mapped_column(nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user_profile.user_id", ondelete="SET NULL"), nullable=True
    )
    recorded_at: Mapped[datetime] = _ts(nullable=False, default=utcnow)
    updated_at: Mapped[datetime | None] = _ts(nullable=True)
    removed_at: Mapped[datetime | None] = _ts(nullable=True)

    __table_args__ = (Index("idx_patient_medication_patient_id", "patient_id"),)


# ── Medical Passport consent (FR4) ──────────────────────────────────────
class ConsentGrant(Base):
    """consent_grant — FR4/NFR16/D2, per-section since P9.

    Pre-exists in the shared Supabase schema (verified against the live
    `information_schema` 2026-10-01 and again 2026-10-07). `grantee_id` is
    polymorphic (a `doctor_id` or a `clinic_id`, by `grantee_type`) and has no
    FK, so the consent service checks the grantee exists. `grantee_type` is
    DB-constrained to `'doctor'`/`'clinic'`.

    `scope_sections` lists the record sections the grant shares (P9, see
    `.claude/specs/privacy_access_spec.md`); `[]` means nothing is shared.
    Changing sections never edits a grant: the old one is revoked and points
    at its replacement through `replaced_by`. Rows are never deleted.

    `revoked_by` and `replaced_by` were added by this repo's P9 migration, as
    was `uq_consent_grant_one_active`: at most one unrevoked grant per
    patient/grantee, now enforced by the database as well as the service.
    """

    __tablename__ = "consent_grant"

    id: Mapped[uuid.UUID] = mapped_column("consent_id", Uuid, primary_key=True, default=uuid7)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("patient.patient_id", ondelete="CASCADE"), nullable=False
    )
    grantee_type: Mapped[str] = mapped_column(String(20), nullable=False)
    grantee_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    scope_sections: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=list
    )
    granted_at: Mapped[datetime] = _ts(nullable=False, default=utcnow)
    expires_at: Mapped[datetime | None] = _ts(nullable=True)
    revoked_at: Mapped[datetime | None] = _ts(nullable=True)
    # NULL with `revoked_at` set means the system closed a lapsed grant.
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user_profile.user_id", ondelete="SET NULL"), nullable=True
    )
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("consent_grant.consent_id"), nullable=True
    )

    __table_args__ = (
        Index(
            "uq_consent_grant_one_active",
            "patient_id",
            "grantee_type",
            "grantee_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
            sqlite_where=text("revoked_at IS NULL"),
        ),
    )


class AccessLog(Base):
    """access_log — FR5, NFR17. One row per read of a patient's record.

    Pre-exists in the shared Supabase schema (verified 2026-10-07); this repo
    adds no DDL to it. Written only by `consent/gateway.py`, one row per
    section a non-owner reads, in the same transaction as the read. Append-
    only: the application never updates or deletes a row.

    `resource_id` is NOT NULL in the live table; section reads have no record
    id of their own yet, so they store the patient's id.
    """

    __tablename__ = "access_log"

    id: Mapped[uuid.UUID] = mapped_column("log_id", Uuid, primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user_profile.user_id", ondelete="SET NULL"), nullable=True
    )
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("patient.patient_id", ondelete="SET NULL"), nullable=True
    )
    consent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("consent_grant.consent_id"), nullable=True
    )
    resource_type: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    action: Mapped[str] = mapped_column(String(10), nullable=False)
    is_break_glass: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ip_address: Mapped[str | None] = mapped_column(
        String(64).with_variant(INET(), "postgresql"), nullable=True
    )
    occurred_at: Mapped[datetime] = _ts(nullable=False, default=utcnow)

    __table_args__ = (
        CheckConstraint("action IN ('read', 'write', 'export')", name="access_log_action_check"),
        Index("idx_access_log_patient_id", "patient_id"),
        Index("idx_access_log_occurred_at", "occurred_at"),
    )


class AuditLog(Base):
    """FR5, NFR17 — append-only. Never updated or deleted by the application.

    Owned outright by this codebase. It holds auth events and grant changes;
    reads of a patient's record go to the shared `access_log` instead (see
    `AccessLog`), whose required `resource_id` and consent linkage don't fit
    generic events like login/logout/lockout.
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
