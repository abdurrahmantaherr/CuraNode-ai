"""Clinic-admin doctor verification business logic (D-VERIFY v1).

Mirrors `consent/service.py`'s shape: every call checks the actor's role and
clinic affiliation itself, live, never trusting a cached flag — the same
"who may see whom" boundary the dashboard (D4) and consent module already
draw. A doctor outside the admin's clinic is `NotFound`, the identical shape
as any other foreign/nonexistent lookup elsewhere in this app, so an admin
can't learn that a doctor exists at a clinic that isn't theirs.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import writer as audit
from ..db import types as dbtypes
from ..db.models import (
    Doctor,
    DoctorAffiliation,
    DoctorVerification,
    UserRole,
    VerificationDecision,
    VerificationStatus,
)
from ..deps import Actor
from ..errors import NotFound, ValidationFailed
from .schemas import REASONS

_TAB_STATUS = {
    "pending": VerificationStatus.PENDING,
    "verified": VerificationStatus.VERIFIED,
    "rejected": VerificationStatus.REJECTED,
}


def _require_admin(actor: Actor) -> None:
    if actor.role != UserRole.CLINIC_ADMIN.value:
        raise NotFound()


async def _doctor_in_clinic(session: AsyncSession, actor: Actor, doctor_id: uuid.UUID) -> Doctor:
    """Live, every call — an admin dropped from `clinic_staff` loses access
    to this doctor on their very next action (review focus 2)."""
    doctor = (
        await session.execute(
            select(Doctor)
            .join(DoctorAffiliation, DoctorAffiliation.doctor_id == Doctor.id)
            .where(
                Doctor.id == doctor_id,
                DoctorAffiliation.clinic_id.in_(actor.clinic_ids),
                DoctorAffiliation.status == "active",
            )
        )
    ).scalar_one_or_none()
    if doctor is None:
        raise NotFound()
    return doctor


async def _record(
    session: AsyncSession,
    actor: Actor,
    doctor: Doctor,
    *,
    decision: VerificationDecision,
    reason: str | None,
    checked_number: bool,
    checked_name: bool,
    checked_licence: bool,
    note: str | None,
    user_agent: str | None,
) -> None:
    session.add(
        DoctorVerification(
            id=dbtypes.uuid7(),
            doctor_id=doctor.id,
            decided_by=actor.user_id,
            decision=decision,
            reason=reason,
            checked_number=checked_number,
            checked_name=checked_name,
            checked_licence=checked_licence,
            note=(note or None),
            decided_at=dbtypes.utcnow(),
        )
    )
    action = {
        VerificationDecision.APPROVE: audit.VERIFICATION_APPROVE,
        VerificationDecision.REJECT: audit.VERIFICATION_REJECT,
        VerificationDecision.REVOKE: audit.VERIFICATION_REVOKE,
    }[decision]
    detail: dict[str, Any] = {"doctor_id": str(doctor.id)}
    if reason is not None:
        detail["reason"] = reason
    await audit.write(
        session,
        action=action,
        actor_user_id=actor.user_id,
        actor_role=actor.role,
        resource_type="doctor_verification",
        ip_address=actor.ip_address,
        user_agent=user_agent,
        detail=detail,
    )


async def list_doctors_for_clinic(session: AsyncSession, actor: Actor, tab: str) -> list[Doctor]:
    _require_admin(actor)
    status = _TAB_STATUS.get(tab)
    if status is None:
        raise ValidationFailed({"fields": {"tab": "errors.choice_invalid"}})
    rows = await session.execute(
        select(Doctor)
        .join(DoctorAffiliation, DoctorAffiliation.doctor_id == Doctor.id)
        .where(
            DoctorAffiliation.clinic_id.in_(actor.clinic_ids),
            DoctorAffiliation.status == "active",
            Doctor.verification_status == status,
        )
        .distinct()
        .order_by(Doctor.full_name, Doctor.id)
    )
    return list(rows.scalars().all())


async def approve(
    session: AsyncSession,
    actor: Actor,
    doctor_id: uuid.UUID,
    *,
    checked_number: bool,
    checked_name: bool,
    checked_licence: bool,
    note: str | None,
    user_agent: str | None = None,
) -> None:
    _require_admin(actor)
    if not (checked_number and checked_name and checked_licence):
        raise ValidationFailed({"fields": {"checklist": "errors.checklist_required"}})
    doctor = await _doctor_in_clinic(session, actor, doctor_id)
    doctor.verification_status = VerificationStatus.VERIFIED
    doctor.verified_by = actor.user_id
    doctor.verified_at = dbtypes.utcnow()
    await _record(
        session,
        actor,
        doctor,
        decision=VerificationDecision.APPROVE,
        reason=None,
        checked_number=True,
        checked_name=True,
        checked_licence=True,
        note=note,
        user_agent=user_agent,
    )
    await session.commit()


async def _reject_or_revoke(
    session: AsyncSession,
    actor: Actor,
    doctor_id: uuid.UUID,
    *,
    decision: VerificationDecision,
    reason: str,
    note: str | None,
    user_agent: str | None,
) -> None:
    _require_admin(actor)
    if reason not in REASONS:
        raise ValidationFailed({"fields": {"reason": "errors.choice_invalid"}})
    if reason == "other" and not (note or "").strip():
        raise ValidationFailed({"fields": {"note": "errors.text_required"}})
    doctor = await _doctor_in_clinic(session, actor, doctor_id)
    doctor.verification_status = VerificationStatus.REJECTED
    doctor.verified_by = None
    doctor.verified_at = None
    await _record(
        session,
        actor,
        doctor,
        decision=decision,
        reason=reason,
        checked_number=False,
        checked_name=False,
        checked_licence=False,
        note=note,
        user_agent=user_agent,
    )
    await session.commit()


async def reject(
    session: AsyncSession,
    actor: Actor,
    doctor_id: uuid.UUID,
    *,
    reason: str,
    note: str | None,
    user_agent: str | None = None,
) -> None:
    await _reject_or_revoke(
        session,
        actor,
        doctor_id,
        decision=VerificationDecision.REJECT,
        reason=reason,
        note=note,
        user_agent=user_agent,
    )


async def revoke(
    session: AsyncSession,
    actor: Actor,
    doctor_id: uuid.UUID,
    *,
    reason: str,
    note: str | None,
    user_agent: str | None = None,
) -> None:
    await _reject_or_revoke(
        session,
        actor,
        doctor_id,
        decision=VerificationDecision.REVOKE,
        reason=reason,
        note=note,
        user_agent=user_agent,
    )


async def latest_decisions_for(
    session: AsyncSession, doctor_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, DoctorVerification]:
    if not doctor_ids:
        return {}
    rows = await session.execute(
        select(DoctorVerification)
        .where(DoctorVerification.doctor_id.in_(doctor_ids))
        .order_by(DoctorVerification.decided_at)
    )
    latest: dict[uuid.UUID, DoctorVerification] = {}
    for row in rows.scalars().all():
        latest[row.doctor_id] = row  # ascending order — last write wins
    return latest


async def doctor_status_for_banner(session: AsyncSession, actor: Actor) -> tuple[str, str | None]:
    """For the doctor's own dashboard banner (Task 7). Read live, like
    everything else in this module — never cached on `Actor`."""
    doctor = (
        await session.execute(select(Doctor).where(Doctor.user_id == actor.user_id))
    ).scalar_one_or_none()
    if doctor is None:
        return (VerificationStatus.PENDING.value, None)
    reason = None
    if doctor.verification_status == VerificationStatus.REJECTED:
        latest = await latest_decisions_for(session, [doctor.id])
        row = latest.get(doctor.id)
        reason = row.reason if row else None
    return (doctor.verification_status.value, reason)
