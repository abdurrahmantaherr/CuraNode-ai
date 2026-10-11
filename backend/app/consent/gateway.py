"""The consent gateway — the one door to another person's record (P9 spec §4.7).

TDD §1.4 Rule 1 / §4.3, realised over record *sections*: every read of a
patient's profile sections by anyone but that patient goes through here, and
nowhere else may select from `allergy`, `chronic_condition` or
`patient_medication` except `profile/service.py`, which reads only the
caller's own rows (BL-12; enforced by a test that scans the code).

`allowed_sections()` decides what an actor may see:
  1. the patient themself            → every section, nothing logged (BL-13)
  2. a verified doctor with active grants naming them, or naming a clinic
     where they hold an active affiliation → the union of those grants'
     sections (BL-14). Verification and affiliations are read live.
  3. anyone else, or an empty union  → `ConsentDenied` (BL-15, BL-16)

`ConsentDenied` is a `NotFound`: denied, revoked, expired, unverified and
"no such patient" all look identical to the caller (D2).

`read_section()` refuses a section outside that set, and for a non-owner
writes one `access_log` row before returning anything, in the same
transaction (BL-18). If that write fails the caller gets an exception, never
data. `purpose` is mandatory on every call (BL-20); the shared `access_log`
has no column for it, so it is part of the call contract only.

Section contents are clinical PHI: nothing here logs them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import (
    AccessLog,
    Allergy,
    ChronicCondition,
    ConsentGrant,
    Patient,
    PatientMedication,
    UserRole,
)
from ..deps import Actor
from ..errors import ConsentDenied
from .schemas import SECTIONS
from .service import DOCTOR, _active, doctor_grant_filter, live_doctor

ACTION_READ = "read"
RESOURCE_PREFIX = "section."


@dataclass(frozen=True)
class Access:
    patient: Patient
    sections: frozenset[str]
    # section -> the grant that allows it (a doctor grant preferred, BL-19)
    consent_for: dict[str, uuid.UUID]
    is_owner: bool


@dataclass(frozen=True)
class PassportView:
    """What a reader may see. `sections[key] is None` means "not shared"
    (BL-17); an allowed section with nothing in it is `[]`."""

    patient_id: uuid.UUID
    passport_no: str
    sections: dict[str, Any]


def _require_purpose(purpose: str) -> None:
    if not isinstance(purpose, str) or not purpose.strip():
        raise ValueError("purpose is required for every read through the consent gateway")


async def allowed_sections(
    session: AsyncSession, actor: Actor, patient_id: uuid.UUID, *, purpose: str
) -> Access:
    _require_purpose(purpose)
    patient = await session.get(Patient, patient_id)
    if patient is None:
        raise ConsentDenied()

    if actor.role == UserRole.PATIENT.value and patient.user_id == actor.user_id:
        return Access(patient, frozenset(SECTIONS), {}, is_owner=True)
    if actor.role != UserRole.DOCTOR.value:
        raise ConsentDenied()  # clinic admins, staff, other patients (NFR16)

    found = await live_doctor(session, actor)
    if found is None:
        raise ConsentDenied()
    doctor, clinic_ids = found

    rows = await session.execute(
        select(ConsentGrant).where(
            ConsentGrant.patient_id == patient.id,
            _active(),
            doctor_grant_filter(doctor.id, clinic_ids),
        )
    )
    consent_for: dict[str, uuid.UUID] = {}
    # Doctor grants first, so they win the attribution (BL-19).
    for g in sorted(rows.scalars().all(), key=lambda g: g.grantee_type != DOCTOR):
        for section in g.scope_sections or []:
            if section in SECTIONS:
                consent_for.setdefault(section, g.id)
    if not consent_for:
        raise ConsentDenied()
    return Access(patient, frozenset(consent_for), consent_for, is_owner=False)


async def read_section(
    session: AsyncSession, actor: Actor, access: Access, section: str, *, purpose: str
) -> Any:
    """One section's data, logged first for a non-owner. Does not commit."""
    _require_purpose(purpose)
    if section not in access.sections:
        raise ConsentDenied()
    if not access.is_owner:
        session.add(
            AccessLog(
                user_id=actor.user_id,
                patient_id=access.patient.id,
                consent_id=access.consent_for[section],
                resource_type=RESOURCE_PREFIX + section,
                resource_id=access.patient.id,
                action=ACTION_READ,
                is_break_glass=False,
                ip_address=actor.ip_address,
            )
        )
        await session.flush()  # a failed log write raises here, before any data leaves
    return await _READERS[section](session, access.patient)


async def read_sections(
    session: AsyncSession, actor: Actor, patient_id: uuid.UUID, *, purpose: str
) -> PassportView:
    """Every section the actor may see, each logged, committed together.
    On any failure nothing is committed and nothing is returned."""
    try:
        access = await allowed_sections(session, actor, patient_id, purpose=purpose)
        sections: dict[str, Any] = {}
        for key in SECTIONS:
            if key in access.sections:
                sections[key] = await read_section(session, actor, access, key, purpose=purpose)
            else:
                sections[key] = None  # never queried (BL-17)
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    return PassportView(access.patient.id, access.patient.passport_no, sections)


# ── Section readers (always scoped to `patient.id`) ──────────────────────
async def _basics(session: AsyncSession, patient: Patient) -> dict[str, Any]:
    return {
        "full_name": patient.full_name,
        "date_of_birth": patient.date_of_birth,
        "gender": patient.gender,
        "blood_group": patient.blood_group,
        "emergency_contact": patient.emergency_contact,
    }


async def _allergies(session: AsyncSession, patient: Patient) -> list[dict[str, Any]]:
    rows = await session.execute(
        select(Allergy)
        .where(Allergy.patient_id == patient.id, Allergy.removed_at.is_(None))
        .order_by(Allergy.recorded_at, Allergy.id)
    )
    return [
        {"substance": a.substance, "reaction": a.reaction, "severity": a.severity}
        for a in rows.scalars().all()
    ]


async def _conditions(session: AsyncSession, patient: Patient) -> list[dict[str, Any]]:
    rows = await session.execute(
        select(ChronicCondition)
        .where(ChronicCondition.patient_id == patient.id, ChronicCondition.status != "resolved")
        .order_by(ChronicCondition.recorded_at, ChronicCondition.id)
    )
    return [{"name": c.name, "since": c.onset_date} for c in rows.scalars().all()]


async def _medications(session: AsyncSession, patient: Patient) -> list[dict[str, Any]]:
    rows = await session.execute(
        select(PatientMedication)
        .where(PatientMedication.patient_id == patient.id, PatientMedication.removed_at.is_(None))
        .order_by(PatientMedication.recorded_at, PatientMedication.id)
    )
    return [
        {"name": m.name, "strength": m.strength, "frequency": m.frequency, "since": m.started_on}
        for m in rows.scalars().all()
    ]


async def _not_built_yet(session: AsyncSession, patient: Patient) -> list[dict[str, Any]]:
    """Prescriptions, lab reports and visits arrive with P2/P3; until then an
    allowed section is simply empty (shown as "Nothing recorded")."""
    return []


_READERS = {
    "basics": _basics,
    "allergies": _allergies,
    "conditions": _conditions,
    "medications": _medications,
    "prescriptions": _not_built_yet,
    "lab_reports": _not_built_yet,
    "visits": _not_built_yet,
}
assert set(_READERS) == set(SECTIONS)
