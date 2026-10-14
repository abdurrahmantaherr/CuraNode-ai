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
from ..consent import gateway
from ..db.models import AuditLog

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
    session: AsyncSession,
    patient_id: uuid.UUID,
    since: datetime,
    sections: frozenset[str] = frozenset({"allergies", "conditions", "medications"}),
) -> list[Change]:
    """Delegates to the consent gateway, the only reader of those tables; pass
    only the sections the patient shared with this doctor."""
    return await gateway.profile_changes_since(session, patient_id, since, sections)
