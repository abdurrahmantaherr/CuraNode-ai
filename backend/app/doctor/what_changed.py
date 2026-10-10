"""The doctor patient page's "What changed" panel (D2).

A deterministic diff, no LLM: the anchor is this doctor's previous `record.read`
audit row for this patient, and the changes are the patient's allergies,
conditions and medications recorded or removed since then. Call `last_read_at`
*before* the gateway read, which writes this request's own `record.read` row.

Every query filters by `patient_id` in application code (BYPASSRLS). The result
holds ("kind", "name") pairs only; `kind` is a message-key suffix, e.g.
`new_allergy`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import writer as audit
from ..db.models import Allergy, AuditLog, ChronicCondition, PatientMedication

Change = tuple[str, str]


async def last_read_at(
    session: AsyncSession, doctor_user_id: uuid.UUID, patient_id: uuid.UUID
) -> datetime | None:
    """When this doctor last read this patient's record, or None."""
    return await session.scalar(
        select(func.max(AuditLog.occurred_at)).where(
            AuditLog.action == audit.RECORD_READ,
            AuditLog.actor_user_id == doctor_user_id,
            AuditLog.subject_patient_id == patient_id,
        )
    )


async def changes_since(
    session: AsyncSession, patient_id: uuid.UUID, since: datetime
) -> list[Change]:
    out: list[Change] = []

    allergies = (
        await session.scalars(select(Allergy).where(Allergy.patient_id == patient_id))
    ).all()
    for a in sorted(allergies, key=lambda x: x.recorded_at):
        if a.removed_at is None and a.recorded_at > since:
            out.append(("new_allergy", a.substance))
        elif a.removed_at is not None and a.removed_at > since and a.recorded_at <= since:
            out.append(("removed_allergy", a.substance))

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
