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
`chronic_condition` tables. Only `audit_log` and `patient_medication` are
owned outright by this codebase, plus a handful of additive columns on
`user_profile`/`doctor`/`allergy`/`chronic_condition` that the shared schema
didn't have yet (see the Alembic migrations for exactly what was added).
The medical-document feature reuses the shared `document` / `ocr_job` /
`ocr_extraction` tables the same way (additive columns only).
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
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


# ── Medical documents (FR10–FR13) ────────────────────────────────────────
class DocumentStatus(str, enum.Enum):
    """The one lifecycle every document type shares.

    Only `FINALIZED` is a medical record. Everything before it is a temporary,
    reviewable upload that no screen lists as a record.
    """

    UPLOADED = "uploaded"  # stored, not yet read
    PROCESSING = "processing"  # OCR / extraction running
    AWAITING_REVIEW = "awaiting_review"  # extracted; the patient has not confirmed
    FINALIZED = "finalized"  # patient clicked Confirm & Save
    FAILED = "failed"  # OCR / extraction failed; retry or cancel
    CANCELLED = "cancelled"  # patient discarded it


IN_PROGRESS_STATUSES = (
    DocumentStatus.UPLOADED,
    DocumentStatus.PROCESSING,
    DocumentStatus.AWAITING_REVIEW,
    DocumentStatus.FAILED,
)

# `document.doc_type` values. The shared schema allowed rx / lab / scan / other;
# this feature's migration adds `vaccine` (the column is varchar(10), so the
# short form is deliberate).
DOC_TYPE_RX = "rx"
DOC_TYPE_LAB = "lab"
DOC_TYPE_VACCINE = "vaccine"
DOC_TYPE_OTHER = "other"
DOC_TYPES = (DOC_TYPE_RX, DOC_TYPE_LAB, DOC_TYPE_VACCINE, DOC_TYPE_OTHER)

# `document.ocr_mode`. Only printed documents are read now (Tesseract); the
# column and its CHECK constraint belong to the shared schema and still allow
# 'handwritten', which this app no longer writes or offers.
OCR_MODE_PRINTED = "printed"


class Document(Base):
    """One uploaded medical document — the single table for all four types.

    Shared, pre-existing table. This repo added `original_filename`, `status`,
    `failure_code`, `ocr_text`, `document_date`, `finalized_at`, `ocr_mode`. `status`
    defaults to `finalized` at the database level only so rows that predate
    the column read as records; this app always writes it explicitly.
    `object_key` points into the private `medical-documents` bucket; the file
    is never moved or copied, finalizing only changes `status`.
    `encounter_id` exists in the shared table but is never written here.
    """

    __tablename__ = "document"

    id: Mapped[uuid.UUID] = mapped_column("document_id", Uuid, primary_key=True, default=uuid7)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("patient.patient_id", ondelete="CASCADE"), nullable=False
    )
    doc_type: Mapped[str] = mapped_column(String(10), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("user_profile.user_id"), nullable=False
    )
    uploaded_at: Mapped[datetime] = _ts(nullable=False, default=utcnow)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=DocumentStatus.UPLOADED.value
    )
    failure_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    ocr_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    document_date: Mapped[date | None] = mapped_column(nullable=True)
    finalized_at: Mapped[datetime | None] = _ts(nullable=True)
    ocr_mode: Mapped[str] = mapped_column(String(12), nullable=False, default=OCR_MODE_PRINTED)

    __table_args__ = (
        CheckConstraint(
            "doc_type IN ('rx', 'lab', 'scan', 'other', 'vaccine')",
            name="document_doc_type_check",
        ),
        CheckConstraint(
            "status IN ('uploaded', 'processing', 'awaiting_review', "
            "'finalized', 'failed', 'cancelled')",
            name="document_status_check",
        ),
        CheckConstraint("ocr_mode IN ('printed', 'handwritten')", name="document_ocr_mode_check"),
        Index("idx_document_patient_status", "patient_id", "status"),
    )


class OcrJob(Base):
    """One OCR run per document (`document_id` is UNIQUE in the shared schema,
    so a retry resets this row rather than adding another)."""

    __tablename__ = "ocr_job"

    id: Mapped[uuid.UUID] = mapped_column("job_id", Uuid, primary_key=True, default=uuid7)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("document.document_id", ondelete="CASCADE"), unique=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    engine: Mapped[str] = mapped_column(String(20), nullable=False)
    overall_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = _ts(nullable=True)
    finished_at: Mapped[datetime | None] = _ts(nullable=True)
    # This repo's additive column: the document's SECTIONS (facility, patient,
    # doctor, dates, advice) as read by the AI layer and validated against the
    # OCR text (`documents/ai_sections.py`). NULL = none stored (AI off, failed,
    # or a document that predates this column) — the saved-record page then
    # shows the deterministic sections exactly as before. Reset on every retry.
    ai_sections: Mapped[dict | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )


class OcrExtraction(Base):
    """One proposed field value, and what the patient made of it.

    `extracted_value` is what the engine proposed and is never overwritten;
    `corrected_value` is the patient's reviewed value (NULL = accepted as
    proposed, '' = cleared). `row_index` groups the fields of one medicine or
    one lab result — this repo's additive column. The shared `item_id` link to
    `prescription_item` is left untouched (a patient upload has no encounter or
    prescribing doctor, both NOT NULL on `prescription`).
    """

    __tablename__ = "ocr_extraction"

    id: Mapped[uuid.UUID] = mapped_column("extraction_id", Uuid, primary_key=True, default=uuid7)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("ocr_job.job_id", ondelete="CASCADE"), nullable=False
    )
    field_name: Mapped[str] = mapped_column(String(20), nullable=False)
    extracted_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    corrected_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user_profile.user_id"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = _ts(nullable=True)
    row_index: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (Index("idx_ocr_extraction_job", "job_id"),)


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
