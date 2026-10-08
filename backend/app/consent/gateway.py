"""Consent gateway — the only door to a patient's clinical record (D2).

This is the only module that queries the doctor-owned clinical tables
(encounter, diagnosis, vital_sign, prescription, lab_*);
`tests/test_consent.py::test_no_direct_clinical_queries` enforces that.

Access is decided by `consent.service.patient_and_grant_for_doctor`, which
checks the grant live; every denial there is the same `NotFound`, and it
propagates unchanged. Every query below filters by `patient_id` in application
code — the app connects with BYPASSRLS, so the database will not catch a miss.

The read and its single `record.read` audit row share one transaction: if the
audit insert fails, nothing is returned and nothing is persisted.

The reads run one after another, not under `asyncio.gather`: they share one
`AsyncSession`, which does not support concurrent operations on a connection.
"""

from __future__ import annotations

import uuid
from collections import defaultdict

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
    Allergy,
    ChronicCondition,
    Diagnosis,
    Encounter,
    LabReport,
    LabResult,
    Medicine,
    PatientMedication,
    Prescription,
    PrescriptionItem,
    VitalSign,
)
from ..deps import Actor
from . import service

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
    try:
        allergies = (
            await session.scalars(
                select(Allergy)
                .where(Allergy.patient_id == pid, Allergy.removed_at.is_(None))
                .order_by(Allergy.recorded_at.desc())
            )
        ).all()
        conditions = (
            await session.scalars(
                select(ChronicCondition)
                .where(ChronicCondition.patient_id == pid, ChronicCondition.status != "resolved")
                .order_by(ChronicCondition.recorded_at.desc())
            )
        ).all()
        medications = (
            await session.scalars(
                select(PatientMedication)
                .where(PatientMedication.patient_id == pid, PatientMedication.removed_at.is_(None))
                .order_by(PatientMedication.recorded_at.desc())
            )
        ).all()
        encounters = await _encounters(session, pid)
        lab_reports = await _lab_reports(session, pid)

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


async def _encounters(session: AsyncSession, pid: uuid.UUID) -> list[EncounterOut]:
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

    # patient_id is applied here as well as the encounter scope.
    rx_rows = (
        await session.scalars(
            select(Prescription)
            .where(Prescription.patient_id == pid, Prescription.encounter_id.in_(enc_ids))
            .order_by(Prescription.issue_date.desc(), Prescription.created_at.desc())
        )
    ).all()
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


async def _lab_reports(session: AsyncSession, pid: uuid.UUID) -> list[LabReportOut]:
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
