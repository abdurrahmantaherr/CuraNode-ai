"""Consent gateway — the only door to a patient's clinical record (D2).

This is the only module that queries the doctor-owned clinical tables
(encounter, diagnosis, vital_sign, prescription, lab_*) — itself, and through the
timeline readers it calls (`timeline/sources.py`);
`tests/test_consent.py::test_no_direct_clinical_queries` enforces that.

Access is decided by `consent.service.patient_and_grant_for_doctor`, which
checks the grant live; every denial there is the same `NotFound`, and it
propagates unchanged. Every query below filters by `patient_id` in application
code — the app connects with BYPASSRLS, so the database will not catch a miss.

The read and its single `record.read` audit row share one transaction: if the
audit insert fails, nothing is returned and nothing is persisted.

The reads run one after another, not under `asyncio.gather`: they share one
`AsyncSession`, which does not support concurrent operations on a connection.

Profile sections (P9):

The consent gateway — the one door to another person's record (P9 spec §4.7).

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
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import writer as audit
from ..clinical.schemas import (
    AllergyOut,
    ConditionOut,
    DiagnosisOut,
    EncounterOut,
    LabReportOut,
    LabResultOut,
    MedicationOut,
    MedicineOut,
    PatientBasicOut,
    PatientRecordOut,
    PrescriptionItemOut,
    PrescriptionOut,
    VitalSignOut,
)
from ..db.models import (
    AccessLog,
    Allergy,
    ChronicCondition,
    ConsentGrant,
    Diagnosis,
    Encounter,
    LabReport,
    LabResult,
    Medicine,
    Patient,
    PatientMedication,
    Prescription,
    PrescriptionItem,
    UserRole,
    VitalSign,
)
from ..deps import Actor
from ..errors import ConsentDenied
from ..timeline import sources as timeline_sources
from . import service
from .schemas import SECTIONS
from .service import DOCTOR, _active, doctor_grant_filter, live_doctor

ACTOR_ROLE = "doctor"
RESOURCE_TYPE = "patient_record"
DEFAULT_PURPOSE = "patient_record_view"


async def load_patient_for_doctor(
    session: AsyncSession,
    actor: Actor,
    patient_id: uuid.UUID,
    *,
    purpose: str = DEFAULT_PURPOSE,
    user_agent: str | None = None,
) -> PatientRecordOut:
    patient, grant = await service.patient_and_grant_for_doctor(session, actor, patient_id)
    pid = patient.id
    # Only what the patient shared: the union of the active grants reaching this
    # doctor. An unshared section is never queried (it comes back as []).
    shared = (await allowed_sections(session, actor, pid, purpose=purpose)).sections
    try:
        allergies: list[Allergy] = []
        if "allergies" in shared:
            allergies = list(
                await session.scalars(
                    select(Allergy)
                    .where(Allergy.patient_id == pid, Allergy.removed_at.is_(None))
                    .order_by(Allergy.recorded_at.desc())
                )
            )
        conditions: list[ChronicCondition] = []
        if "conditions" in shared:
            conditions = list(
                await session.scalars(
                    select(ChronicCondition)
                    .where(
                        ChronicCondition.patient_id == pid, ChronicCondition.status != "resolved"
                    )
                    .order_by(ChronicCondition.recorded_at.desc())
                )
            )
        medications: list[PatientMedication] = []
        if "medications" in shared:
            medications = list(
                await session.scalars(
                    select(PatientMedication)
                    .where(
                        PatientMedication.patient_id == pid,
                        PatientMedication.removed_at.is_(None),
                    )
                    .order_by(PatientMedication.recorded_at.desc())
                )
            )
        # A visit lists the prescriptions written in it only if the patient shared the
        # "prescriptions" section too — sharing "visits" alone must not hand them over.
        encounters = (
            await _encounters(session, pid, with_prescriptions="prescriptions" in shared)
            if "visits" in shared
            else []
        )
        lab_reports = await _lab_report_records(session, pid) if "lab_reports" in shared else []

        record = PatientRecordOut(
            patient=PatientBasicOut.model_validate(patient),
            allergies=[AllergyOut.model_validate(a) for a in allergies],
            conditions=[ConditionOut.model_validate(c) for c in conditions],
            medications=[MedicationOut.model_validate(m) for m in medications],
            encounters=encounters,
            lab_reports=lab_reports,
        )
        await audit.write(
            session,
            action=audit.RECORD_READ,
            actor_user_id=actor.user_id,
            actor_role=ACTOR_ROLE,
            subject_patient_id=pid,
            resource_type=RESOURCE_TYPE,
            ip_address=actor.ip_address,
            user_agent=user_agent,
            detail={
                "patient_id": str(pid),
                "doctor_id": str(grant.grantee_id),
                "grant_id": str(grant.id),
                "purpose": purpose,
            },
        )
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    return record


ACTION_READ = "read"
RESOURCE_PREFIX = "section."


@dataclass(frozen=True)
class Access:
    patient: Patient
    sections: frozenset[str]
    # section -> the grant that allows it (a doctor grant preferred, BL-19)
    consent_for: dict[str, uuid.UUID]
    is_owner: bool
    # the most recent active grant reaching this doctor — the one the `record.read` audit
    # row names; None for the patient themself
    grant: ConsentGrant | None = None


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
    grants = list(rows.scalars().all())
    consent_for: dict[str, uuid.UUID] = {}
    # Doctor grants first, so they win the attribution (BL-19).
    for g in sorted(grants, key=lambda g: g.grantee_type != DOCTOR):
        for section in g.scope_sections or []:
            if section in SECTIONS:
                consent_for.setdefault(section, g.id)
    if not consent_for:
        raise ConsentDenied()
    relied_on = max(grants, key=lambda g: g.granted_at)
    return Access(patient, frozenset(consent_for), consent_for, is_owner=False, grant=relied_on)


async def _log_read(session: AsyncSession, actor: Actor, access: Access, section: str) -> None:
    """The one shared `access_log` row for a non-owner reading `section` (BL-18), flushed
    before any data leaves: a failed log write raises here and the caller returns nothing."""
    if access.is_owner:
        return
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
    await session.flush()


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
    await _log_read(session, actor, access, section)
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


@dataclass(frozen=True)
class DoctorPatientPage:
    """What the doctor's patient page (D2) shows. `sections[key] is None` means "not shared"
    (BL-17). `visits` and `lab_reports` hold D2's typed records (also `encounters` /
    `lab_reports` below, which is what the page lists); the other sections are the compact
    rows the page lists."""

    patient_id: uuid.UUID
    passport_no: str
    sections: dict[str, Any]
    encounters: list[EncounterOut]
    lab_reports: list[LabReportOut]


async def load_doctor_patient_page(
    session: AsyncSession,
    actor: Actor,
    patient_id: uuid.UUID,
    *,
    purpose: str = DEFAULT_PURPOSE,
    user_agent: str | None = None,
) -> DoctorPatientPage:
    """Everything the doctor's patient page needs, in ONE pass: one live access check, one
    `access_log` row per section the doctor may see (written before that section's data is
    read, BL-18), each shared section read once, one `record.read` audit row, one commit —
    or nothing at all if any step fails. An unshared section is never queried (BL-17).

    This replaces calling `read_sections` and then `load_patient_for_doctor`, which checked
    access three times and read the profile sections and the visits twice (each query is a
    network round trip, see CLAUDE.md "Reaching the database"). Both of those stay for the
    callers that want exactly one of the two forms.

    Only a doctor reaches this: the patient themself, or anyone without a live grant, is
    `ConsentDenied` — the one 404 of every refusal (D2)."""
    _require_purpose(purpose)
    try:
        access = await allowed_sections(session, actor, patient_id, purpose=purpose)
        if access.is_owner or access.grant is None:
            raise ConsentDenied()
        pid = access.patient.id
        sections: dict[str, Any] = {}
        encounters: list[EncounterOut] = []
        lab_reports: list[LabReportOut] = []
        for key in SECTIONS:
            if key not in access.sections:
                sections[key] = None  # never queried (BL-17)
                continue
            await _log_read(session, actor, access, key)
            if key == "visits":
                # A visit lists the prescriptions written in it only if the patient shared the
                # "prescriptions" section too.
                encounters = await _encounters(
                    session, pid, with_prescriptions="prescriptions" in access.sections
                )
                sections[key] = encounters
            elif key == "lab_reports":
                lab_reports = await _lab_report_records(session, pid)
                sections[key] = lab_reports
            else:
                sections[key] = await _READERS[key](session, access.patient)
        await audit.write(
            session,
            action=audit.RECORD_READ,
            actor_user_id=actor.user_id,
            actor_role=ACTOR_ROLE,
            subject_patient_id=pid,
            resource_type=RESOURCE_TYPE,
            ip_address=actor.ip_address,
            user_agent=user_agent,
            detail={
                "patient_id": str(pid),
                "doctor_id": str(access.grant.grantee_id),
                "grant_id": str(access.grant.id),
                "purpose": purpose,
            },
        )
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    return DoctorPatientPage(pid, access.patient.passport_no, sections, encounters, lab_reports)


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


async def _encounters(
    session: AsyncSession, pid: uuid.UUID, *, with_prescriptions: bool
) -> list[EncounterOut]:
    encs = (
        await session.scalars(
            select(Encounter)
            .where(Encounter.patient_id == pid)
            .order_by(Encounter.visit_datetime.desc())
        )
    ).all()
    if not encs:
        return []
    enc_ids = [e.id for e in encs]

    diagnoses: dict[uuid.UUID, list[DiagnosisOut]] = defaultdict(list)
    for d in await session.scalars(select(Diagnosis).where(Diagnosis.encounter_id.in_(enc_ids))):
        diagnoses[d.encounter_id].append(DiagnosisOut.model_validate(d))

    vitals: dict[uuid.UUID, list[VitalSignOut]] = defaultdict(list)
    for v in await session.scalars(
        select(VitalSign)
        .where(VitalSign.encounter_id.in_(enc_ids))
        .order_by(VitalSign.recorded_at.desc())
    ):
        vitals[v.encounter_id].append(VitalSignOut.model_validate(v))

    # patient_id is applied here as well as the encounter scope. An unshared section is
    # never queried (BL-17): without the prescriptions grant this select does not run.
    rx_rows = (
        (
            await session.scalars(
                select(Prescription)
                .where(Prescription.patient_id == pid, Prescription.encounter_id.in_(enc_ids))
                .order_by(Prescription.issue_date.desc(), Prescription.created_at.desc())
            )
        ).all()
        if with_prescriptions
        else []
    )
    items: dict[uuid.UUID, list[PrescriptionItemOut]] = defaultdict(list)
    if rx_rows:
        item_rows = await session.execute(
            select(PrescriptionItem, Medicine)
            .outerjoin(Medicine, Medicine.id == PrescriptionItem.medicine_id)
            .where(PrescriptionItem.prescription_id.in_([r.id for r in rx_rows]))
        )
        for item, med in item_rows.all():
            items[item.prescription_id].append(
                PrescriptionItemOut(
                    id=item.id,
                    medicine=MedicineOut.model_validate(med) if med is not None else None,
                    dosage=item.dosage,
                    frequency=item.frequency,
                    duration_days=item.duration_days,
                    route=item.route,
                    instructions=item.instructions,
                )
            )
    prescriptions: dict[uuid.UUID, list[PrescriptionOut]] = defaultdict(list)
    for r in rx_rows:
        prescriptions[r.encounter_id].append(
            PrescriptionOut(
                id=r.id,
                doctor_id=r.doctor_id,
                issue_date=r.issue_date,
                source=r.source,
                status=r.status,
                created_at=r.created_at,
                items=items[r.id],
            )
        )

    return [
        EncounterOut(
            id=e.id,
            doctor_id=e.doctor_id,
            clinic_id=e.clinic_id,
            visit_datetime=e.visit_datetime,
            chief_complaint=e.chief_complaint,
            clinical_notes=e.clinical_notes,
            ai_summary=e.ai_summary,
            what_changed_diff=e.what_changed_diff,
            diagnoses=diagnoses[e.id],
            vitals=vitals[e.id],
            prescriptions=prescriptions[e.id],
        )
        for e in encs
    ]


async def _lab_report_records(session: AsyncSession, pid: uuid.UUID) -> list[LabReportOut]:
    reports = (
        await session.scalars(
            select(LabReport)
            .where(LabReport.patient_id == pid)
            .order_by(LabReport.report_date.desc())
        )
    ).all()
    if not reports:
        return []
    results: dict[uuid.UUID, list[LabResultOut]] = defaultdict(list)
    for r in await session.scalars(
        select(LabResult).where(LabResult.report_id.in_([rep.id for rep in reports]))
    ):
        results[r.report_id].append(LabResultOut.model_validate(r))
    return [
        LabReportOut(
            id=rep.id,
            encounter_id=rep.encounter_id,
            document_id=rep.document_id,
            lab_name=rep.lab_name,
            report_date=rep.report_date,
            status=rep.status,
            results=results[rep.id],
        )
        for rep in reports
    ]


async def profile_changes_since(
    session: AsyncSession,
    patient_id: uuid.UUID,
    since: datetime,
    sections: frozenset[str],
) -> list[tuple[str, str]]:
    """The D2 "What changed" diff: allergies / conditions / medications recorded
    or removed after `since`, as ("kind", name) pairs. Only the given (shared)
    sections are queried. It lives here because only this module may read those
    tables (P9 BL-12); callers must pass sections they already hold consent for."""
    out: list[tuple[str, str]] = []

    if "allergies" in sections:
        allergies = (
            await session.scalars(select(Allergy).where(Allergy.patient_id == patient_id))
        ).all()
        for a in sorted(allergies, key=lambda x: x.recorded_at):
            if a.removed_at is None and a.recorded_at > since:
                out.append(("new_allergy", a.substance))
            elif a.removed_at is not None and a.removed_at > since and a.recorded_at <= since:
                out.append(("removed_allergy", a.substance))

    if "conditions" in sections:
        conditions = (
            await session.scalars(
                select(ChronicCondition).where(ChronicCondition.patient_id == patient_id)
            )
        ).all()
        for c in sorted(conditions, key=lambda x: x.recorded_at):
            if c.status != "resolved" and c.recorded_at > since:
                out.append(("new_condition", c.name))
            elif (
                c.status == "resolved"
                and c.updated_at is not None
                and c.updated_at > since
                and c.recorded_at <= since
            ):
                out.append(("resolved_condition", c.name))

    if "medications" in sections:
        meds = (
            await session.scalars(
                select(PatientMedication).where(PatientMedication.patient_id == patient_id)
            )
        ).all()
        for m in sorted(meds, key=lambda x: x.recorded_at):
            if m.removed_at is None and m.recorded_at > since:
                out.append(("new_medication", m.name))
            elif m.removed_at is not None and m.removed_at > since and m.recorded_at <= since:
                out.append(("removed_medication", m.name))

    return out
