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

Two forms of a section exist. The COMPACT form (`_READERS`) is what a doctor's patient page
lists. The RICH form (`_RICH_READERS`, `read_section(..., rich=True)`) carries what the medical
timeline (P3) needs — ids, dates, who recorded an entry; it is gated and logged exactly the
same way. `visits`, `prescriptions` and `lab_reports` are read by `timeline/sources.py` — the
only module besides this one that may select a patient's visits or prescriptions.

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
from ..timeline import sources as timeline_sources
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
    session: AsyncSession,
    actor: Actor,
    access: Access,
    section: str,
    *,
    purpose: str,
    rich: bool = False,
) -> Any:
    """One section's data, logged first for a non-owner. Does not commit. `rich` asks for the
    form the medical timeline builds on (same permission check, same one log row)."""
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
    reader = _RICH_READERS.get(section) if rich else None
    return await (reader or _READERS[section])(session, access.patient)


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


def _by(recorded_by: uuid.UUID | None, patient: Patient) -> str | None:
    """Who wrote a profile entry: the patient, someone else (a clinician), or nobody on record."""
    if recorded_by is None:
        return None
    return "patient" if recorded_by == patient.user_id else "clinician"


async def _visits(session: AsyncSession, patient: Patient) -> list[dict[str, Any]]:
    return timeline_sources.compact_visits(await timeline_sources.visits(session, patient))


async def _prescriptions(session: AsyncSession, patient: Patient) -> list[dict[str, Any]]:
    return timeline_sources.compact_prescriptions(
        await timeline_sources.prescriptions(session, patient)
    )


async def _lab_reports(session: AsyncSession, patient: Patient) -> list[dict[str, Any]]:
    return timeline_sources.compact_lab_reports(
        await timeline_sources.lab_reports(session, patient)
    )


# ── Rich readers (the timeline's form: ids, dates, authorship) ───────────
async def _allergies_rich(session: AsyncSession, patient: Patient) -> list[dict[str, Any]]:
    rows = await session.execute(
        select(Allergy)
        .where(Allergy.patient_id == patient.id, Allergy.removed_at.is_(None))
        .order_by(Allergy.recorded_at, Allergy.id)
    )
    return [
        {
            "id": a.id,
            "substance": a.substance,
            "reaction": a.reaction,
            "severity": a.severity,
            "recorded_at": a.recorded_at,
            "by": _by(a.recorded_by, patient),
        }
        for a in rows.scalars().all()
    ]


async def _conditions_rich(session: AsyncSession, patient: Patient) -> list[dict[str, Any]]:
    rows = await session.execute(
        select(ChronicCondition)
        .where(ChronicCondition.patient_id == patient.id, ChronicCondition.status != "resolved")
        .order_by(ChronicCondition.recorded_at, ChronicCondition.id)
    )
    return [
        {
            "id": c.id,
            "name": c.name,
            "onset": c.onset_date,
            "recorded_at": c.recorded_at,
            "by": _by(c.recorded_by, patient),
        }
        for c in rows.scalars().all()
    ]


async def _medications_rich(session: AsyncSession, patient: Patient) -> list[dict[str, Any]]:
    rows = await session.execute(
        select(PatientMedication)
        .where(PatientMedication.patient_id == patient.id, PatientMedication.removed_at.is_(None))
        .order_by(PatientMedication.recorded_at, PatientMedication.id)
    )
    return [
        {
            "id": m.id,
            "name": m.name,
            "strength": m.strength,
            "frequency": m.frequency,
            "started_on": m.started_on,
            "recorded_at": m.recorded_at,
            "by": _by(m.recorded_by, patient),
        }
        for m in rows.scalars().all()
    ]


_READERS = {
    "basics": _basics,
    "allergies": _allergies,
    "conditions": _conditions,
    "medications": _medications,
    "prescriptions": _prescriptions,
    "lab_reports": _lab_reports,
    "visits": _visits,
}
assert set(_READERS) == set(SECTIONS)

# What the medical timeline reads (P3). `basics` has no timeline form: it is not an event.
_RICH_READERS = {
    "allergies": _allergies_rich,
    "conditions": _conditions_rich,
    "medications": _medications_rich,
    "prescriptions": timeline_sources.prescriptions,
    "lab_reports": timeline_sources.lab_reports,
    "visits": timeline_sources.visits,
}
