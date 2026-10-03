"""Medical Passport consent business logic (passport SPEC §8, BL-01…BL-12).

As in `profile/service.py`, scoping is done here, not by the database: the
app connects with BYPASSRLS, so every grant lookup is filtered by the caller's
own `patient_id`, and a foreign or nonexistent grant id surface as the same
`NotFound`.

`consent_grant` has no unique constraint behind BL-04 (the live
`idx_consent_grant_active_lookup` is a plain partial index), so "one active
grant per patient/grantee" is enforced here. Grants are never hard-deleted
(BL-08); revoking sets `revoked_at`, and a repeat revoke writes nothing
(BL-06). Each successful mutation writes exactly one audit row in the same
transaction.

Nothing in this module logs a PMDC number, clinic name, or passport number
(AC-12).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import writer as audit
from ..db import types as dbtypes
from ..db.models import Clinic, ConsentGrant, Doctor, Patient, VerificationStatus
from ..deps import Actor
from ..errors import DuplicateEntry, NotFound
from ..profile.service import _patient_for
from .schemas import ConsentGrantOut

ACTOR_ROLE = "patient"
RESOURCE_TYPE = "consent_grant"
DOCTOR = "doctor"
CLINIC = "clinic"


# ── Shared steps ─────────────────────────────────────────────────────────
def _active() -> ColumnElement[bool]:
    """BL-05/BL-12 — not revoked and, once `expires_at` is populated, not expired."""
    return ConsentGrant.revoked_at.is_(None) & or_(
        ConsentGrant.expires_at.is_(None), ConsentGrant.expires_at > dbtypes.utcnow()
    )


async def _grantee_names(
    session: AsyncSession, grants: list[ConsentGrant]
) -> dict[tuple[str, uuid.UUID], str]:
    """One query per grantee type, however many grants there are."""
    names: dict[tuple[str, uuid.UUID], str] = {}
    doctor_ids = {g.grantee_id for g in grants if g.grantee_type == DOCTOR}
    clinic_ids = {g.grantee_id for g in grants if g.grantee_type == CLINIC}
    if doctor_ids:
        rows = await session.execute(
            select(Doctor.id, Doctor.full_name).where(Doctor.id.in_(doctor_ids))
        )
        names.update(((DOCTOR, i), n) for i, n in rows.all())
    if clinic_ids:
        rows = await session.execute(
            select(Clinic.id, Clinic.name).where(Clinic.id.in_(clinic_ids))
        )
        names.update(((CLINIC, i), n) for i, n in rows.all())
    return names


def _grant_out(g: ConsentGrant, names: dict[tuple[str, uuid.UUID], str]) -> ConsentGrantOut:
    return ConsentGrantOut(
        id=g.id,
        patient_id=g.patient_id,
        grantee_type=g.grantee_type,
        grantee_id=g.grantee_id,
        grantee_name=names.get((g.grantee_type, g.grantee_id)),
        scope_sections=list(g.scope_sections or []),
        granted_at=g.granted_at,
        expires_at=g.expires_at,
        revoked_at=g.revoked_at,
    )


async def _to_out(session: AsyncSession, g: ConsentGrant) -> ConsentGrantOut:
    return _grant_out(g, await _grantee_names(session, [g]))


async def _resolve_doctor(session: AsyncSession, pmdc_number: str) -> Doctor:
    """BL-02 — nonexistent and unverified are the same `NotFound`, so a patient
    can't use this form to learn a PMDC number's verification state."""
    doctor = (
        await session.execute(select(Doctor).where(Doctor.pmdc_number == pmdc_number))
    ).scalar_one_or_none()
    if doctor is None or doctor.verification_status != VerificationStatus.VERIFIED:
        raise NotFound()
    return doctor


async def _resolve_clinic(session: AsyncSession, clinic_name: str) -> Clinic:
    """BL-03 — case-insensitive exact name match. `clinic.name` is not unique
    in the shared schema; an ambiguous name is refused rather than guessed."""
    rows = await session.execute(
        select(Clinic).where(func.lower(Clinic.name) == clinic_name.lower()).limit(2)
    )
    clinics = list(rows.scalars().all())
    if len(clinics) != 1:
        raise NotFound()
    return clinics[0]


async def _check_no_active(
    session: AsyncSession, patient_id: uuid.UUID, grantee_type: str, grantee_id: uuid.UUID
) -> None:
    """BL-04 — enforced here; the database has no unique constraint for it."""
    existing = (
        await session.execute(
            select(ConsentGrant.id)
            .where(
                ConsentGrant.patient_id == patient_id,
                ConsentGrant.grantee_type == grantee_type,
                ConsentGrant.grantee_id == grantee_id,
                _active(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise DuplicateEntry({"field": "grantee"})


async def _audit(
    session: AsyncSession,
    actor: Actor,
    patient: Patient,
    *,
    action: str,
    detail: dict[str, Any],
    user_agent: str | None,
) -> None:
    await audit.write(
        session,
        action=action,
        actor_user_id=actor.user_id,
        actor_role=ACTOR_ROLE,
        subject_patient_id=patient.id,
        resource_type=RESOURCE_TYPE,
        ip_address=actor.ip_address,
        user_agent=user_agent,
        detail=detail,
    )


# ── Public API ───────────────────────────────────────────────────────────
async def grant_consent(
    session: AsyncSession,
    actor: Actor,
    *,
    pmdc_number: str | None = None,
    clinic_name: str | None = None,
    user_agent: str | None = None,
) -> ConsentGrantOut:
    """Exactly one of `pmdc_number`/`clinic_name` — `GrantCreate` enforces
    that for request bodies; a direct caller passing both or neither is a bug."""
    if (pmdc_number is None) == (clinic_name is None):
        raise ValueError("exactly one of pmdc_number or clinic_name is required")

    patient = await _patient_for(session, actor)
    if pmdc_number is not None:
        doctor = await _resolve_doctor(session, pmdc_number)
        grantee_type, grantee_id = DOCTOR, doctor.id
    else:
        assert clinic_name is not None
        clinic = await _resolve_clinic(session, clinic_name)
        grantee_type, grantee_id = CLINIC, clinic.id

    await _check_no_active(session, patient.id, grantee_type, grantee_id)

    grant = ConsentGrant(
        id=dbtypes.uuid7(),
        patient_id=patient.id,
        grantee_type=grantee_type,
        grantee_id=grantee_id,
        scope_sections=[],  # all-or-nothing (passport SPEC §9.5)
        granted_at=dbtypes.utcnow(),
        expires_at=None,  # "until revoked" only (BL-12)
    )
    session.add(grant)
    await session.flush()

    # Ids only — never the PMDC number or clinic name the patient typed.
    await _audit(
        session,
        actor,
        patient,
        action=audit.CONSENT_GRANT,
        detail={
            "resource_id": str(grant.id),
            "grantee_type": grantee_type,
            "grantee_id": str(grantee_id),
        },
        user_agent=user_agent,
    )
    await session.commit()
    return await _to_out(session, grant)


async def list_grants(session: AsyncSession, actor: Actor) -> list[ConsentGrantOut]:
    """BL-05 — the caller's own active grants, newest first."""
    patient = await _patient_for(session, actor)
    rows = await session.execute(
        select(ConsentGrant)
        .where(ConsentGrant.patient_id == patient.id, _active())
        .order_by(ConsentGrant.granted_at.desc())
    )
    grants = list(rows.scalars().all())
    names = await _grantee_names(session, grants)
    return [_grant_out(g, names) for g in grants]


async def revoke_grant(
    session: AsyncSession,
    actor: Actor,
    grant_id: uuid.UUID,
    *,
    user_agent: str | None = None,
) -> ConsentGrantOut:
    """BL-06/BL-08 — soft revoke; a repeat revoke is a no-op."""
    patient = await _patient_for(session, actor)
    grant = (
        await session.execute(
            select(ConsentGrant).where(
                ConsentGrant.id == grant_id, ConsentGrant.patient_id == patient.id
            )
        )
    ).scalar_one_or_none()
    if grant is None:
        raise NotFound()
    if grant.revoked_at is not None:
        return await _to_out(session, grant)  # idempotent — no second write or audit row

    grant.revoked_at = dbtypes.utcnow()
    await _audit(
        session,
        actor,
        patient,
        action=audit.CONSENT_REVOKE,
        detail={
            "resource_id": str(grant.id),
            "grantee_type": grant.grantee_type,
            "grantee_id": str(grant.grantee_id),
        },
        user_agent=user_agent,
    )
    await session.commit()
    return await _to_out(session, grant)
