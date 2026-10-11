"""The patient's view of who read their record (FR5; P9 spec §4.9).

Reads the shared `access_log`, which only `consent/gateway.py` writes. The
patient sees rows about their own record by anyone but themself, newest first
(BL-21). Rows are never changed or deleted (BL-22, NFR17).

Paging is keyset-based on `(occurred_at, log_id)`: the cursor is an opaque
urlsafe-base64 token, so a page boundary never shifts when new reads arrive.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import AccessLog, Clinic, ConsentGrant, Doctor
from ..deps import Actor
from ..errors import ValidationFailed
from ..profile.service import _patient_for
from .gateway import RESOURCE_PREFIX
from .schemas import AccessLogEntry, AccessLogPage
from .service import CLINIC

DEFAULT_LIMIT, MAX_LIMIT = 50, 100


def encode_cursor(occurred_at: datetime, log_id: uuid.UUID) -> str:
    raw = f"{occurred_at.isoformat()}|{log_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        at, _, log_id = base64.urlsafe_b64decode(padded.encode()).decode().partition("|")
        return datetime.fromisoformat(at), uuid.UUID(log_id)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise ValidationFailed({"fields": {"cursor": "errors.cursor_invalid"}}) from None


async def list_access_log(
    session: AsyncSession,
    actor: Actor,
    *,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> AccessLogPage:
    limit = max(1, min(limit, MAX_LIMIT))
    patient = await _patient_for(session, actor)
    stmt = select(AccessLog).where(
        AccessLog.patient_id == patient.id,
        or_(AccessLog.user_id.is_(None), AccessLog.user_id != actor.user_id),
    )
    if cursor:
        at, log_id = decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                AccessLog.occurred_at < at,
                and_(AccessLog.occurred_at == at, AccessLog.id < log_id),
            )
        )
    rows = list(
        (
            await session.execute(
                stmt.order_by(AccessLog.occurred_at.desc(), AccessLog.id.desc()).limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    page, more = rows[:limit], len(rows) > limit

    # Names in one query per kind, however many rows.
    user_ids = {r.user_id for r in page if r.user_id is not None}
    doctors: dict[uuid.UUID, tuple[str, str | None]] = {}
    if user_ids:
        found = await session.execute(
            select(Doctor.user_id, Doctor.full_name, Doctor.specialty).where(
                Doctor.user_id.in_(user_ids)
            )
        )
        doctors = {u: (n, s) for u, n, s in found.all()}
    consent_ids = {r.consent_id for r in page if r.consent_id is not None}
    clinic_of: dict[uuid.UUID, str] = {}
    if consent_ids:
        found = await session.execute(
            select(ConsentGrant.id, Clinic.name)
            .join(Clinic, Clinic.id == ConsentGrant.grantee_id)
            .where(ConsentGrant.id.in_(consent_ids), ConsentGrant.grantee_type == CLINIC)
        )
        clinic_of = dict(found.tuples().all())

    items = []
    for r in page:
        name, specialty = doctors.get(r.user_id, (None, None)) if r.user_id else (None, None)
        section = r.resource_type.removeprefix(RESOURCE_PREFIX)
        items.append(
            AccessLogEntry(
                occurred_at=r.occurred_at,
                section=section,
                accessor_name=name,
                accessor_specialty=specialty,
                clinic_name=clinic_of.get(r.consent_id) if r.consent_id else None,
            )
        )
    next_cursor = encode_cursor(page[-1].occurred_at, page[-1].id) if more else None
    return AccessLogPage(items=items, next_cursor=next_cursor)
