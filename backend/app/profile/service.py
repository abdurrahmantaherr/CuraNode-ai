"""Patient profile business logic (profile SPEC §4, BL-01…BL-36).

Scoping is the whole game here. The app connects to Postgres with BYPASSRLS,
so Supabase's row-level policies do NOT protect these queries: every entry
lookup is filtered by the caller's own `patient_id` in application code
(BL-02), and a foreign, nonexistent, or inactive id all surface as the same
`NotFound` (BL-03).

Entries are never hard-deleted. Allergies and medications are soft-removed
with `removed_at`; conditions with `status='resolved'`. Each successful
mutation writes exactly one audit row in its own transaction; reads, no-ops,
rejections, and repeat removals write none (BL-24…BL-27).

Nothing in this module logs a field value (BL-28).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel
from pydantic_core import PydanticCustomError
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import writer as audit
from ..db import types as dbtypes
from ..db.models import Allergy, ChronicCondition, Patient, PatientMedication, Profile
from ..deps import Actor
from ..errors import DuplicateEntry, ListFull, NotEditable, NotFound, ValidationFailed
from ..settings import settings
from .schemas import (
    AllergyCreate,
    AllergyOut,
    AllergyUpdate,
    ConditionCreate,
    ConditionOut,
    ConditionUpdate,
    MedicationCreate,
    MedicationOut,
    MedicationUpdate,
    PatientProfileOut,
    ProfileUpdateRequest,
    normalise_name,
)

ACTOR_ROLE = "patient"
RESOLVED = "resolved"

# BL-22 — unknown/legacy severities sort last.
_SEVERITY_RANK = {"severe": 0, "moderate": 1, "mild": 2, "unknown": 3}


# ── Pure helpers ─────────────────────────────────────────────────────────
def compute_age(dob: date | None, today: date) -> int | None:
    """Full years. A 29 February birthday counts as 1 March in common years."""
    if dob is None:
        return None
    try:
        birthday = dob.replace(year=today.year)
    except ValueError:  # 29 Feb in a non-leap year
        birthday = date(today.year, 3, 1)
    years = today.year - dob.year
    if today < birthday:
        years -= 1
    return max(years, 0)


def _ts_key(value: datetime | None) -> tuple[bool, float]:
    """Sort key for a timestamp: NULLs last, and naive (SQLite-loaded, UTC)
    vs. aware (freshly set in this session) values made comparable."""
    if value is None:
        return (True, 0.0)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (False, value.timestamp())


def _stored_key(value: str) -> str:
    """`normalise_name` for a value already in the database. Rows written by
    the wider product were never through `clean_text`, so one holding a
    control character must still compare rather than raise."""
    try:
        return normalise_name(value)
    except PydanticCustomError:
        return value.strip().casefold()


# ── Entry kinds ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class _Kind:
    """Everything that differs between allergies, conditions and medications,
    so add/update/remove are written once."""

    model: type[Any]
    resource_type: str
    name_field: str
    since_field: str | None
    clinical_fields: tuple[str, ...]
    add_action: str
    update_action: str
    remove_action: str
    to_out: Callable[[Any, uuid.UUID], BaseModel]

    def active(self) -> ColumnElement[bool]:
        if self.model is ChronicCondition:
            return ChronicCondition.status.is_distinct_from(RESOLVED)
        return self.model.removed_at.is_(None)

    def is_removed(self, entry: Any) -> bool:
        if self.model is ChronicCondition:
            return entry.status == RESOLVED
        return entry.removed_at is not None


def _allergy_out(a: Allergy, user_id: uuid.UUID) -> AllergyOut:
    return AllergyOut(
        id=a.id,
        substance=a.substance,
        reaction=a.reaction,
        severity=a.severity,
        recorded_at=a.recorded_at,
        updated_at=a.updated_at,
        editable=a.recorded_by == user_id,
    )


def _condition_out(c: ChronicCondition, user_id: uuid.UUID) -> ConditionOut:
    return ConditionOut(
        id=c.id,
        name=c.name,
        onset_date=c.onset_date,
        status=c.status,
        icd10_code=c.icd10_code,
        recorded_at=c.recorded_at,
        updated_at=c.updated_at,
        editable=c.recorded_by == user_id,
    )


def _medication_out(m: PatientMedication, user_id: uuid.UUID) -> MedicationOut:
    return MedicationOut(
        id=m.id,
        name=m.name,
        strength=m.strength,
        frequency=m.frequency,
        started_on=m.started_on,
        notes=m.notes,
        recorded_at=m.recorded_at,
        updated_at=m.updated_at,
        editable=m.recorded_by == user_id,
    )


ALLERGY = _Kind(
    model=Allergy,
    resource_type="allergy",
    name_field="substance",
    since_field=None,
    clinical_fields=("substance", "reaction", "severity"),
    add_action=audit.PROFILE_ALLERGY_ADD,
    update_action=audit.PROFILE_ALLERGY_UPDATE,
    remove_action=audit.PROFILE_ALLERGY_REMOVE,
    to_out=_allergy_out,
)
CONDITION = _Kind(
    model=ChronicCondition,
    resource_type="chronic_condition",
    name_field="name",
    since_field="onset_date",
    clinical_fields=("name", "onset_date", "status"),
    add_action=audit.PROFILE_CONDITION_ADD,
    update_action=audit.PROFILE_CONDITION_UPDATE,
    remove_action=audit.PROFILE_CONDITION_REMOVE,
    to_out=_condition_out,
)
# `notes` is deliberately absent: free text that may hold PII never enters
# audit values (BL-26).
MEDICATION = _Kind(
    model=PatientMedication,
    resource_type="patient_medication",
    name_field="name",
    since_field="started_on",
    clinical_fields=("name", "strength", "frequency", "started_on"),
    add_action=audit.PROFILE_MEDICATION_ADD,
    update_action=audit.PROFILE_MEDICATION_UPDATE,
    remove_action=audit.PROFILE_MEDICATION_REMOVE,
    to_out=_medication_out,
)


# ── Shared steps ─────────────────────────────────────────────────────────
async def _patient_for(session: AsyncSession, actor: Actor) -> Patient:
    patient = (
        await session.execute(select(Patient).where(Patient.user_id == actor.user_id))
    ).scalar_one_or_none()
    if patient is None:
        # OAuth onboarding incomplete — no Medical Passport yet.
        raise NotFound()
    return patient


async def _active_entries(session: AsyncSession, kind: _Kind, patient_id: uuid.UUID) -> list[Any]:
    rows = await session.execute(
        select(kind.model).where(kind.model.patient_id == patient_id, kind.active())
    )
    return list(rows.scalars().all())


async def _check_cap(session: AsyncSession, kind: _Kind, patient_id: uuid.UUID) -> None:
    count = (
        await session.execute(
            select(func.count())
            .select_from(kind.model)
            .where(kind.model.patient_id == patient_id, kind.active())
        )
    ).scalar_one()
    if count >= settings.profile_max_entries_per_list:
        raise ListFull(settings.profile_max_entries_per_list)


async def _check_duplicate(
    session: AsyncSession,
    kind: _Kind,
    patient_id: uuid.UUID,
    name: str,
    *,
    exclude_id: uuid.UUID | None = None,
) -> None:
    """BL-19 — active entries only, editable or not."""
    name_col = getattr(kind.model, kind.name_field)
    rows = await session.execute(
        select(kind.model.id, name_col).where(kind.model.patient_id == patient_id, kind.active())
    )
    key = normalise_name(name)
    for entry_id, existing in rows.all():
        if entry_id != exclude_id and _stored_key(existing) == key:
            raise DuplicateEntry({"field": kind.name_field})


def _check_since(kind: _Kind, patient: Patient, since: date | None) -> None:
    """BL-18 — a since-date may not precede the stored date of birth."""
    if kind.since_field is None or since is None or patient.date_of_birth is None:
        return
    if since < patient.date_of_birth:
        raise ValidationFailed({"fields": {kind.since_field: "errors.date_before_birth"}})


def _snapshot(kind: _Kind, entry: Any, fields: list[str] | tuple[str, ...]) -> dict[str, Any]:
    return {f: getattr(entry, f) for f in fields if f in kind.clinical_fields}


async def _audit(
    session: AsyncSession,
    actor: Actor,
    patient: Patient,
    *,
    action: str,
    resource_type: str,
    detail: dict[str, Any],
    user_agent: str | None,
) -> None:
    await audit.write(
        session,
        action=action,
        actor_user_id=actor.user_id,
        actor_role=ACTOR_ROLE,
        subject_patient_id=patient.id,
        resource_type=resource_type,
        ip_address=actor.ip_address,
        user_agent=user_agent,
        detail=detail,
    )


# ── Profile ──────────────────────────────────────────────────────────────
def _display_name(user: Profile, patient: Patient) -> str:
    return user.full_name if user.full_name else patient.full_name


async def get_profile(session: AsyncSession, actor: Actor) -> PatientProfileOut:
    patient = await _patient_for(session, actor)
    user = await session.get(Profile, actor.user_id)
    assert user is not None  # resolved by the auth dependency this request

    allergies = await _active_entries(session, ALLERGY, patient.id)
    conditions = await _active_entries(session, CONDITION, patient.id)
    medications = await _active_entries(session, MEDICATION, patient.id)

    # BL-22 ordering. `recorded_at` may be NULL only for legacy conditions.
    allergies.sort(
        key=lambda a: (
            _SEVERITY_RANK.get(a.severity or "", 4),
            a.substance.casefold(),
            _ts_key(a.recorded_at),
        )
    )
    conditions.sort(key=lambda c: (c.name.casefold(), _ts_key(c.recorded_at)))
    medications.sort(key=lambda m: (m.name.casefold(), _ts_key(m.recorded_at)))

    uid = actor.user_id
    return PatientProfileOut(
        passport_no=patient.passport_no,
        full_name=_display_name(user, patient),
        email=user.email,
        phone_e164=user.phone_e164,
        date_of_birth=patient.date_of_birth,
        age_years=compute_age(patient.date_of_birth, dbtypes.today_pk()),
        gender=patient.gender,
        blood_group=patient.blood_group,
        emergency_contact=patient.emergency_contact,
        allergies=[_allergy_out(a, uid) for a in allergies],
        conditions=[_condition_out(c, uid) for c in conditions],
        medications=[_medication_out(m, uid) for m in medications],
    )


async def update_profile(
    session: AsyncSession,
    actor: Actor,
    body: ProfileUpdateRequest,
    *,
    user_agent: str | None = None,
) -> PatientProfileOut:
    patient = await _patient_for(session, actor)
    user = await session.get(Profile, actor.user_id)
    assert user is not None

    stored: dict[str, Any] = {
        "full_name": _display_name(user, patient),
        "date_of_birth": patient.date_of_birth,
        "gender": patient.gender,
        "blood_group": patient.blood_group,
        "phone_e164": user.phone_e164,
        "emergency_contact": patient.emergency_contact,
    }
    changed = sorted(f for f in body.model_fields_set if getattr(body, f) != stored[f])
    if not changed:
        return await get_profile(session, actor)

    for f in changed:
        value = getattr(body, f)
        if f == "full_name":
            # BL-07 — both copies, one transaction.
            user.full_name = value
            patient.full_name = value
        elif f == "phone_e164":
            # BL-06 — the profile row only; Supabase Auth is never called.
            user.phone_e164 = value
        else:
            setattr(patient, f, value)

    # BL-25 — field names only; identity values stay out of the audit blob.
    await _audit(
        session,
        actor,
        patient,
        action=audit.PROFILE_UPDATE,
        resource_type="patient",
        detail={"resource_id": str(patient.id), "fields_changed": changed},
        user_agent=user_agent,
    )
    await session.commit()
    return await get_profile(session, actor)


# ── Entries (generic) ────────────────────────────────────────────────────
async def _add(
    session: AsyncSession,
    actor: Actor,
    kind: _Kind,
    body: BaseModel,
    *,
    user_agent: str | None,
) -> Any:
    patient = await _patient_for(session, actor)
    values = body.model_dump()
    if kind.since_field:
        _check_since(kind, patient, values.get(kind.since_field))
    await _check_cap(session, kind, patient.id)
    await _check_duplicate(session, kind, patient.id, values[kind.name_field])

    entry = kind.model(
        id=dbtypes.uuid7(),
        patient_id=patient.id,
        recorded_by=actor.user_id,
        recorded_at=dbtypes.utcnow(),
        **values,
    )
    if kind is CONDITION:
        entry.status = "active"
    session.add(entry)
    await session.flush()

    await _audit(
        session,
        actor,
        patient,
        action=kind.add_action,
        resource_type=kind.resource_type,
        detail={
            "resource_id": str(entry.id),
            "after": _snapshot(kind, entry, kind.clinical_fields),
        },
        user_agent=user_agent,
    )
    await session.commit()
    return kind.to_out(entry, actor.user_id)


async def _load_own(
    session: AsyncSession,
    kind: _Kind,
    patient: Patient,
    entry_id: uuid.UUID,
    *,
    active_only: bool,
) -> Any:
    stmt = select(kind.model).where(kind.model.id == entry_id, kind.model.patient_id == patient.id)
    if active_only:
        stmt = stmt.where(kind.active())
    entry = (await session.execute(stmt)).scalar_one_or_none()
    if entry is None:
        raise NotFound()
    return entry


async def _update(
    session: AsyncSession,
    actor: Actor,
    kind: _Kind,
    entry_id: uuid.UUID,
    body: BaseModel,
    *,
    user_agent: str | None,
) -> Any:
    patient = await _patient_for(session, actor)
    entry = await _load_own(session, kind, patient, entry_id, active_only=True)
    if entry.recorded_by != actor.user_id:
        raise NotEditable()

    changed = sorted(f for f in body.model_fields_set if getattr(body, f) != getattr(entry, f))
    if not changed:
        return kind.to_out(entry, actor.user_id)

    if kind.since_field and kind.since_field in changed:
        _check_since(kind, patient, getattr(body, kind.since_field))
    if kind.name_field in changed:
        await _check_duplicate(
            session, kind, patient.id, getattr(body, kind.name_field), exclude_id=entry.id
        )

    before = _snapshot(kind, entry, changed)
    for f in changed:
        setattr(entry, f, getattr(body, f))
    entry.updated_at = dbtypes.utcnow()
    after = _snapshot(kind, entry, changed)

    await _audit(
        session,
        actor,
        patient,
        action=kind.update_action,
        resource_type=kind.resource_type,
        detail={
            "resource_id": str(entry.id),
            "fields_changed": changed,
            "before": before,
            "after": after,
        },
        user_agent=user_agent,
    )
    await session.commit()
    return kind.to_out(entry, actor.user_id)


async def _remove(
    session: AsyncSession,
    actor: Actor,
    kind: _Kind,
    entry_id: uuid.UUID,
    *,
    user_agent: str | None,
) -> None:
    patient = await _patient_for(session, actor)
    entry = await _load_own(session, kind, patient, entry_id, active_only=False)
    if entry.recorded_by != actor.user_id:
        raise NotEditable()
    if kind.is_removed(entry):
        return  # idempotent — no second write, no second audit row

    before = _snapshot(kind, entry, kind.clinical_fields)
    now = dbtypes.utcnow()
    if kind is CONDITION:
        entry.status = RESOLVED
    else:
        entry.removed_at = now
    entry.updated_at = now

    await _audit(
        session,
        actor,
        patient,
        action=kind.remove_action,
        resource_type=kind.resource_type,
        detail={"resource_id": str(entry.id), "before": before},
        user_agent=user_agent,
    )
    await session.commit()


# ── Public API (profile SPEC §6.6) ───────────────────────────────────────
async def add_allergy(
    session: AsyncSession, actor: Actor, body: AllergyCreate, *, user_agent: str | None = None
) -> AllergyOut:
    return await _add(session, actor, ALLERGY, body, user_agent=user_agent)


async def update_allergy(
    session: AsyncSession,
    actor: Actor,
    allergy_id: uuid.UUID,
    body: AllergyUpdate,
    *,
    user_agent: str | None = None,
) -> AllergyOut:
    return await _update(session, actor, ALLERGY, allergy_id, body, user_agent=user_agent)


async def remove_allergy(
    session: AsyncSession, actor: Actor, allergy_id: uuid.UUID, *, user_agent: str | None = None
) -> None:
    await _remove(session, actor, ALLERGY, allergy_id, user_agent=user_agent)


async def add_condition(
    session: AsyncSession, actor: Actor, body: ConditionCreate, *, user_agent: str | None = None
) -> ConditionOut:
    return await _add(session, actor, CONDITION, body, user_agent=user_agent)


async def update_condition(
    session: AsyncSession,
    actor: Actor,
    condition_id: uuid.UUID,
    body: ConditionUpdate,
    *,
    user_agent: str | None = None,
) -> ConditionOut:
    return await _update(session, actor, CONDITION, condition_id, body, user_agent=user_agent)


async def remove_condition(
    session: AsyncSession, actor: Actor, condition_id: uuid.UUID, *, user_agent: str | None = None
) -> None:
    await _remove(session, actor, CONDITION, condition_id, user_agent=user_agent)


async def add_medication(
    session: AsyncSession, actor: Actor, body: MedicationCreate, *, user_agent: str | None = None
) -> MedicationOut:
    return await _add(session, actor, MEDICATION, body, user_agent=user_agent)


async def update_medication(
    session: AsyncSession,
    actor: Actor,
    medication_id: uuid.UUID,
    body: MedicationUpdate,
    *,
    user_agent: str | None = None,
) -> MedicationOut:
    return await _update(session, actor, MEDICATION, medication_id, body, user_agent=user_agent)


async def remove_medication(
    session: AsyncSession,
    actor: Actor,
    medication_id: uuid.UUID,
    *,
    user_agent: str | None = None,
) -> None:
    await _remove(session, actor, MEDICATION, medication_id, user_agent=user_agent)
