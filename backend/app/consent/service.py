"""Consent business logic (passport SPEC §8; P9 privacy & access spec §4).

As in `profile/service.py`, scoping is done here, not by the database: the
app connects with BYPASSRLS, so every grant lookup is filtered by the caller's
own `patient_id`, and a foreign or nonexistent grant id surface as the same
`NotFound`.

A grant shares a list of record sections (P9 BL-02) for a chosen duration
(BL-07). At most one unrevoked grant exists per patient/grantee (BL-04,
backed by `uq_consent_grant_one_active`); granting again while one is active
returns it unchanged (BL-05), and a lapsed one is closed and linked to its
successor (BL-06). Changing sections revokes and replaces (BL-08). Grants are
never hard-deleted (BL-09). Each successful mutation writes exactly one audit
row in the same transaction (BL-11).

Nothing in this module logs a PMDC number, clinic name, passport number or
search text (passport AC-12, P9 AC-27).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import writer as audit
from ..db import types as dbtypes
from ..db.models import (
    Clinic,
    ConsentGrant,
    Doctor,
    DoctorAffiliation,
    Patient,
    VerificationStatus,
)
from ..deps import Actor
from ..errors import DuplicateEntry, NotFound
from ..profile.service import _patient_for
from .schemas import DEFAULT_DURATION, SECTIONS, ConsentGrantOut, GranteeOut, expiry_for

ACTOR_ROLE = "patient"
RESOURCE_TYPE = "consent_grant"
DOCTOR = "doctor"
CLINIC = "clinic"
SEARCH_LIMIT = 10  # per grantee type
LIKE_ESCAPE = "\\"

GranteeInfo = tuple[str, str | None]  # (name, detail)


# ── Shared steps ─────────────────────────────────────────────────────────
def _active() -> ColumnElement[bool]:
    """Not revoked and not expired."""
    return ConsentGrant.revoked_at.is_(None) & or_(
        ConsentGrant.expires_at.is_(None), ConsentGrant.expires_at > dbtypes.utcnow()
    )


def _aware(ts: datetime) -> datetime:
    """SQLite hands back naive datetimes; every stored timestamp is UTC."""
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def is_active(g: ConsentGrant, now: datetime | None = None) -> bool:
    now = now or dbtypes.utcnow()
    return g.revoked_at is None and (g.expires_at is None or _aware(g.expires_at) > now)


def canonical(sections: list[str]) -> list[str]:
    """Known keys only, de-duplicated, in `SECTIONS` order."""
    chosen = set(sections)
    return [s for s in SECTIONS if s in chosen]


async def _grantee_info(
    session: AsyncSession, grants: list[ConsentGrant]
) -> dict[tuple[str, uuid.UUID], GranteeInfo]:
    """One query per grantee type, however many grants there are."""
    info: dict[tuple[str, uuid.UUID], GranteeInfo] = {}
    doctor_ids = {g.grantee_id for g in grants if g.grantee_type == DOCTOR}
    clinic_ids = {g.grantee_id for g in grants if g.grantee_type == CLINIC}
    if doctor_ids:
        rows = await session.execute(
            select(Doctor.id, Doctor.full_name, Doctor.specialty).where(Doctor.id.in_(doctor_ids))
        )
        info.update(((DOCTOR, i), (n, d)) for i, n, d in rows.all())
    if clinic_ids:
        rows = await session.execute(
            select(Clinic.id, Clinic.name, Clinic.city).where(Clinic.id.in_(clinic_ids))
        )
        info.update(((CLINIC, i), (n, d)) for i, n, d in rows.all())
    return info


def _grant_out(g: ConsentGrant, info: dict[tuple[str, uuid.UUID], GranteeInfo]) -> ConsentGrantOut:
    name, detail = info.get((g.grantee_type, g.grantee_id), (None, None))
    sections = list(g.scope_sections or [])
    return ConsentGrantOut(
        id=g.id,
        patient_id=g.patient_id,
        grantee_type=g.grantee_type,
        grantee_id=g.grantee_id,
        grantee_name=name,
        grantee_detail=detail,
        scope_sections=sections,
        sections=sections,
        granted_at=g.granted_at,
        expires_at=g.expires_at,
        revoked_at=g.revoked_at,
        replaced_by=g.replaced_by,
        active=is_active(g),
    )


async def _to_out(session: AsyncSession, g: ConsentGrant) -> ConsentGrantOut:
    return _grant_out(g, await _grantee_info(session, [g]))


async def _verified_doctor(session: AsyncSession, condition: ColumnElement[bool]) -> Doctor:
    """BL-01 — nonexistent and unverified are the same `NotFound`, so a patient
    can't use the grant form to learn a doctor's verification state."""
    doctor = (await session.execute(select(Doctor).where(condition))).scalar_one_or_none()
    if doctor is None or doctor.verification_status != VerificationStatus.VERIFIED:
        raise NotFound()
    return doctor


async def _active_clinic(session: AsyncSession, condition: ColumnElement[bool]) -> Clinic:
    """BL-01 — exactly one active clinic. `clinic.name` is not unique in the
    shared schema; an ambiguous name is refused rather than guessed."""
    rows = await session.execute(
        select(Clinic).where(condition, Clinic.status == "active").limit(2)
    )
    clinics = list(rows.scalars().all())
    if len(clinics) != 1:
        raise NotFound()
    return clinics[0]


async def _resolve_grantee(
    session: AsyncSession,
    *,
    doctor_id: uuid.UUID | None,
    clinic_id: uuid.UUID | None,
    pmdc_number: str | None,
    clinic_name: str | None,
) -> tuple[str, uuid.UUID]:
    if doctor_id is not None:
        return DOCTOR, (await _verified_doctor(session, Doctor.id == doctor_id)).id
    if pmdc_number is not None:
        return DOCTOR, (await _verified_doctor(session, Doctor.pmdc_number == pmdc_number)).id
    if clinic_id is not None:
        return CLINIC, (await _active_clinic(session, Clinic.id == clinic_id)).id
    assert clinic_name is not None
    clinic = await _active_clinic(session, func.lower(Clinic.name) == clinic_name.lower())
    return CLINIC, clinic.id


async def _own_grant(
    session: AsyncSession, patient: Patient, grant_id: uuid.UUID, *, active_only: bool
) -> ConsentGrant:
    """BL-10 — foreign, missing and (when `active_only`) inactive are one `NotFound`."""
    stmt = select(ConsentGrant).where(
        ConsentGrant.id == grant_id, ConsentGrant.patient_id == patient.id
    )
    if active_only:
        stmt = stmt.where(_active())
    grant = (await session.execute(stmt)).scalar_one_or_none()
    if grant is None:
        raise NotFound()
    return grant


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


async def _flush_or_duplicate(session: AsyncSession) -> None:
    """A race past the service check meets the unique index: report it as the
    duplicate it is rather than a 500."""
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise DuplicateEntry({"field": "grantee"}) from None


# ── Patient side ─────────────────────────────────────────────────────────
async def grant_consent(
    session: AsyncSession,
    actor: Actor,
    *,
    sections: list[str],
    duration: str = DEFAULT_DURATION,
    doctor_id: uuid.UUID | None = None,
    clinic_id: uuid.UUID | None = None,
    pmdc_number: str | None = None,
    clinic_name: str | None = None,
    user_agent: str | None = None,
) -> tuple[ConsentGrantOut, bool]:
    """Returns `(grant, created)`. Exactly one grantee identifier and at least
    one section — `GrantCreate` enforces both for request bodies; a direct
    caller breaking them is a bug."""
    named = [doctor_id, clinic_id, pmdc_number, clinic_name]
    if sum(v is not None for v in named) != 1:
        raise ValueError("exactly one grantee identifier is required")
    sections = canonical(sections)
    if not sections:
        raise ValueError("at least one section is required")

    patient = await _patient_for(session, actor)
    grantee_type, grantee_id = await _resolve_grantee(
        session,
        doctor_id=doctor_id,
        clinic_id=clinic_id,
        pmdc_number=pmdc_number,
        clinic_name=clinic_name,
    )

    now = dbtypes.utcnow()
    rows = await session.execute(
        select(ConsentGrant).where(
            ConsentGrant.patient_id == patient.id,
            ConsentGrant.grantee_type == grantee_type,
            ConsentGrant.grantee_id == grantee_id,
            ConsentGrant.revoked_at.is_(None),
        )
    )
    unrevoked = list(rows.scalars().all())
    for g in unrevoked:
        if is_active(g, now):
            return await _to_out(session, g), False  # BL-05: no write, no audit

    # BL-06 — close lapsed grants first, so the unique index never sees two.
    for old in unrevoked:
        old.revoked_at = now
    if unrevoked:
        await session.flush()

    grant = ConsentGrant(
        id=dbtypes.uuid7(),
        patient_id=patient.id,
        grantee_type=grantee_type,
        grantee_id=grantee_id,
        scope_sections=sections,
        granted_at=now,
        expires_at=expiry_for(duration, now),
    )
    session.add(grant)
    await _flush_or_duplicate(session)
    for old in unrevoked:
        old.replaced_by = grant.id

    # Ids and section keys only — never the PMDC number or clinic name typed.
    await _audit(
        session,
        actor,
        patient,
        action=audit.CONSENT_GRANT,
        detail={
            "resource_id": str(grant.id),
            "grantee_type": grantee_type,
            "grantee_id": str(grantee_id),
            "sections": sections,
            "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
        },
        user_agent=user_agent,
    )
    await session.commit()
    return await _to_out(session, grant), True


async def change_sections(
    session: AsyncSession,
    actor: Actor,
    grant_id: uuid.UUID,
    *,
    sections: list[str],
    user_agent: str | None = None,
) -> ConsentGrantOut:
    """BL-08 — revoke and replace; identical sections are a no-op."""
    sections = canonical(sections)
    if not sections:
        raise ValueError("at least one section is required")
    patient = await _patient_for(session, actor)
    old = await _own_grant(session, patient, grant_id, active_only=True)
    before = list(old.scope_sections or [])
    if before == sections:
        return await _to_out(session, old)

    now = dbtypes.utcnow()
    old.revoked_at = now
    old.revoked_by = actor.user_id
    await session.flush()  # before the insert, for the unique index
    new = ConsentGrant(
        id=dbtypes.uuid7(),
        patient_id=patient.id,
        grantee_type=old.grantee_type,
        grantee_id=old.grantee_id,
        scope_sections=sections,
        granted_at=now,
        expires_at=old.expires_at,
    )
    session.add(new)
    await _flush_or_duplicate(session)
    old.replaced_by = new.id

    await _audit(
        session,
        actor,
        patient,
        action=audit.CONSENT_CHANGE,
        detail={
            "resource_id": str(new.id),
            "replaces": str(old.id),
            "grantee_type": old.grantee_type,
            "grantee_id": str(old.grantee_id),
            "sections_before": before,
            "sections_after": sections,
        },
        user_agent=user_agent,
    )
    await session.commit()
    return await _to_out(session, new)


async def list_grants(
    session: AsyncSession, actor: Actor, *, include_revoked: bool = False
) -> list[ConsentGrantOut]:
    """Active grants newest first; with `include_revoked`, then every revoked
    or expired grant, newest first."""
    patient = await _patient_for(session, actor)
    stmt = select(ConsentGrant).where(ConsentGrant.patient_id == patient.id)
    if not include_revoked:
        stmt = stmt.where(_active())
    rows = await session.execute(stmt.order_by(ConsentGrant.granted_at.desc()))
    grants = list(rows.scalars().all())
    now = dbtypes.utcnow()
    grants.sort(key=lambda g: not is_active(g, now))  # stable: keeps newest first
    info = await _grantee_info(session, grants)
    return [_grant_out(g, info) for g in grants]


async def revoke_grant(
    session: AsyncSession,
    actor: Actor,
    grant_id: uuid.UUID,
    *,
    user_agent: str | None = None,
) -> ConsentGrantOut:
    """BL-09 — soft revoke, recording who; a repeat revoke is a no-op."""
    patient = await _patient_for(session, actor)
    grant = await _own_grant(session, patient, grant_id, active_only=False)
    if grant.revoked_at is not None:
        return await _to_out(session, grant)  # idempotent — no second write or audit row

    grant.revoked_at = dbtypes.utcnow()
    grant.revoked_by = actor.user_id
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


def _like_pattern(q: str) -> str:
    """`%q%`, with LIKE's own wildcards in `q` matched literally."""
    for ch in (LIKE_ESCAPE, "%", "_"):
        q = q.replace(ch, LIKE_ESCAPE + ch)
    return f"%{q.lower()}%"


async def search_grantees(session: AsyncSession, actor: Actor, q: str) -> list[GranteeOut]:
    """Verified doctors by name or specialty, active clinics by name (AC-11).

    Patients only (via `_patient_for`), so this is not an open directory. Never
    returns a PMDC number, a user id, or an unverified doctor.
    """
    await _patient_for(session, actor)
    pattern = _like_pattern(q)
    doctors = await session.execute(
        select(Doctor.id, Doctor.full_name, Doctor.specialty)
        .where(
            Doctor.verification_status == VerificationStatus.VERIFIED,
            or_(
                func.lower(Doctor.full_name).like(pattern, escape=LIKE_ESCAPE),
                func.lower(func.coalesce(Doctor.specialty, "")).like(pattern, escape=LIKE_ESCAPE),
            ),
        )
        .order_by(Doctor.full_name, Doctor.id)
        .limit(SEARCH_LIMIT)
    )
    clinics = await session.execute(
        select(Clinic.id, Clinic.name, Clinic.city)
        .where(Clinic.status == "active", func.lower(Clinic.name).like(pattern, escape=LIKE_ESCAPE))
        .order_by(Clinic.name, Clinic.id)
        .limit(SEARCH_LIMIT)
    )
    return [GranteeOut(type=DOCTOR, id=i, name=n, detail=d) for i, n, d in doctors.all()] + [
        GranteeOut(type=CLINIC, id=i, name=n, detail=d) for i, n, d in clinics.all()
    ]


# ── Doctor side (D2, BL-07, BL-09, BL-10; P9 BL-14) ──────────────────────
# A grant reaches a doctor when it names them, or names a clinic where they
# hold an active affiliation (P9). Every failure — not a verified doctor, no
# such patient, never granted, revoked, expired — is the same `NotFound`, so a
# lookup reveals nothing about whether a patient exists.
async def live_doctor(
    session: AsyncSession, actor: Actor
) -> tuple[Doctor, frozenset[uuid.UUID]] | None:
    """The caller's doctor row and active clinic ids, read now — never from
    the token or a cache (BL-07) — or None unless they are a verified doctor."""
    doctor = (
        await session.execute(select(Doctor).where(Doctor.user_id == actor.user_id))
    ).scalar_one_or_none()
    if doctor is None or doctor.verification_status != VerificationStatus.VERIFIED:
        return None
    rows = await session.execute(
        select(DoctorAffiliation.clinic_id).where(
            DoctorAffiliation.doctor_id == doctor.id, DoctorAffiliation.status == "active"
        )
    )
    return doctor, frozenset(rows.scalars().all())


def doctor_grant_filter(
    doctor_id: uuid.UUID, clinic_ids: frozenset[uuid.UUID]
) -> ColumnElement[bool]:
    """Grants naming this doctor, or a clinic they are actively affiliated with."""
    names_doctor = (ConsentGrant.grantee_type == DOCTOR) & (ConsentGrant.grantee_id == doctor_id)
    if not clinic_ids:
        return names_doctor
    names_clinic = (ConsentGrant.grantee_type == CLINIC) & ConsentGrant.grantee_id.in_(clinic_ids)
    return or_(names_doctor, names_clinic)


async def _granted_patient_and_grant(
    session: AsyncSession, actor: Actor, condition: ColumnElement[bool]
) -> tuple[Patient, ConsentGrant]:
    found = await live_doctor(session, actor)
    if found is None:
        raise NotFound()
    doctor, clinic_ids = found
    # Checked live against the database on every call — never cached (BL-07).
    row = (
        await session.execute(
            select(Patient, ConsentGrant)
            .join(ConsentGrant, ConsentGrant.patient_id == Patient.id)
            .where(condition, doctor_grant_filter(doctor.id, clinic_ids), _active())
            .order_by(ConsentGrant.granted_at.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        raise NotFound()
    return row[0], row[1]


async def _granted_patient(
    session: AsyncSession, actor: Actor, condition: ColumnElement[bool]
) -> Patient:
    return (await _granted_patient_and_grant(session, actor, condition))[0]


async def patient_and_grant_for_doctor(
    session: AsyncSession, actor: Actor, patient_id: uuid.UUID
) -> tuple[Patient, ConsentGrant]:
    """Same live check as `patient_for_doctor`, also returning the grant relied on
    (the clinical gateway records its id in the `record.read` audit row)."""
    return await _granted_patient_and_grant(session, actor, Patient.id == patient_id)


async def find_patient_for_doctor(session: AsyncSession, actor: Actor, passport_no: str) -> Patient:
    """Look a patient up by passport number — only if a grant reaches this doctor."""
    return await _granted_patient(
        session, actor, Patient.passport_no == passport_no.strip().upper()
    )


async def patient_for_doctor(session: AsyncSession, actor: Actor, patient_id: uuid.UUID) -> Patient:
    """Re-check access by patient id on every page view (BL-07)."""
    return await _granted_patient(session, actor, Patient.id == patient_id)


async def list_patients_for_doctor(session: AsyncSession, actor: Actor) -> list[Patient]:
    """D4 — every patient with an active grant reaching this verified doctor, by
    name, once each (a doctor grant and a clinic grant still give one row).

    Same rules as `_granted_patient`: checked live on every call (BL-07), and
    anything but a verified doctor is `NotFound`.
    """
    found = await live_doctor(session, actor)
    if found is None:
        raise NotFound()
    doctor, clinic_ids = found
    rows = await session.execute(
        select(Patient)
        .join(ConsentGrant, ConsentGrant.patient_id == Patient.id)
        .where(doctor_grant_filter(doctor.id, clinic_ids), _active())
        .distinct()
        .order_by(Patient.full_name, Patient.id)
    )
    return list(rows.scalars().all())
