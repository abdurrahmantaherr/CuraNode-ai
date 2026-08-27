"""Audit writer (FR5, NFR17).

Audit writes are never conditional and never best-effort (SPEC BL-15). The row
is written inside the caller's transaction, so if the audit insert fails the
action it describes is rolled back with it.

`audit_log` is append-only: this module offers no update or delete, and no
other module may write to the table.
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
