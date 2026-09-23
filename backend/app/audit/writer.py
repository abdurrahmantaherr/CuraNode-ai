"""Audit writer (FR5, NFR17).

Audit writes are never conditional and never best-effort (SPEC BL-15). The row
is written inside the caller's transaction, so if the audit insert fails the
action it describes is rolled back with it.

`audit_log` is append-only: this module offers no update or delete, and no
other module may write to the table.

`detail` never carries credentials or identity PII. The one deliberate
exception is the patient profile's *clinical entry* actions
(`profile.allergy.*`, `profile.condition.*`, `profile.medication.*`): their
`before`/`after` hold the clinical fields (substance, reaction, severity;
condition name, onset date, status; medicine name, strength, frequency,
start date) so an in-place edit never silently loses history (PRD D1).
Medication `notes` is free text that may contain PII and is therefore never
copied into `before`/`after` — it appears only by name in `fields_changed`.
`profile.update` (basic details) records field names only, never values.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import AuditLog

# Actions emitted by authentication (SPEC 7).
AUTH_REGISTER = "auth.register"
AUTH_LOGIN = "auth.login"
AUTH_LOGOUT = "auth.logout"
AUTH_LOCKOUT = "auth.lockout"
AUTH_REFRESH_REUSE = "auth.refresh_reuse_detected"
AUTH_OAUTH_START = "auth.oauth_start"
AUTH_OAUTH_LOGIN = "auth.oauth_login"
AUTH_OAUTH_ONBOARDED = "auth.oauth_onboarded"

# Actions emitted by the patient profile (FR2).
PROFILE_UPDATE = "profile.update"
PROFILE_ALLERGY_ADD = "profile.allergy.add"
PROFILE_ALLERGY_UPDATE = "profile.allergy.update"
PROFILE_ALLERGY_REMOVE = "profile.allergy.remove"
PROFILE_CONDITION_ADD = "profile.condition.add"
PROFILE_CONDITION_UPDATE = "profile.condition.update"
PROFILE_CONDITION_REMOVE = "profile.condition.remove"
PROFILE_MEDICATION_ADD = "profile.medication.add"
PROFILE_MEDICATION_UPDATE = "profile.medication.update"
PROFILE_MEDICATION_REMOVE = "profile.medication.remove"


async def write(
    session: AsyncSession,
    *,
    action: str,
    actor_user_id: uuid.UUID | None = None,
    actor_role: str | None = None,
    subject_patient_id: uuid.UUID | None = None,
    resource_type: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditLog(
            action=action,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            subject_patient_id=subject_patient_id,
            resource_type=resource_type,
            ip_address=ip_address,
            user_agent=user_agent,
            # Never store credentials or PII in the detail blob.
            detail=json.dumps(detail or {}, default=str),
        )
    )
    await session.flush()
