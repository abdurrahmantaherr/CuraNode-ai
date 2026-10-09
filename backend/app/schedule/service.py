"""Doctor schedule business logic (D7 spec §5–§7).

Every rule about windows (overlap, availability, conflicts, the time-off
mapping, the anchor) lives in the pure `engine`; this module loads rows,
turns them into engine values, asks the engine, and turns its answers into
writes, audit rows and errors. It never re-implements the overlap test.

Scoping: the app connects with BYPASSRLS, so the shared RLS policies do not
protect these queries. Every query reaches `doctor_schedule` through
`doctor_affiliation` and filters by the caller's own `doctor_id`, which is
resolved from `actor.user_id` and never read from a request. A foreign,
nonexistent or removed id is one identical `NotFound`.

Every mutation starts by locking the caller's `doctor` row
(`SELECT … FOR UPDATE`, spec §7), then checks and writes in the same
transaction, so two concurrent writes by one doctor cannot both pass a check.
SQLite ignores the lock; tests assert the statement instead (AC-12).

Rows are never deleted: a slot is removed by `status = 'inactive'`, an
exception by `removed_at`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import writer as audit
from ..db import types as dbtypes
from ..db.models import (
    Appointment,
    Clinic,
    Doctor,
    DoctorAffiliation,
    DoctorSchedule,
    ScheduleException,
    ScheduleExceptionType,
)
from ..deps import Actor
from ..errors import ListFull, NotFound, ScheduleConflict, ValidationFailed
from . import engine
from .schemas import ExtraCreate, SlotCreate, SlotUpdate, TimeOffCreate

ACTOR_ROLE = "doctor"
LIVE = "active"  # doctor_schedule.status / doctor_affiliation.status / clinic.status
REMOVED = "inactive"  # what D7 writes when it removes a slot
MAX_RANGE_DAYS = 30  # R5 / TDD §5
MAX_SLOTS_PER_WEEKDAY = 10  # R7
UPCOMING_BOOKED = ("booked", "checked_in")  # R10
MAX_UPCOMING_EXCEPTIONS = 200  # R7: live rows dated today or later


# ── Loaded state ─────────────────────────────────────────────────────────
@dataclass
class Loaded:
    """One doctor's schedule as rows and as engine values, loaded once per
    request. Exceptions are the live ones dated today or later: past dates are
    never read or changed (R5)."""

    doctor: Doctor
    today: date
    affiliations: dict[uuid.UUID, DoctorAffiliation] = field(default_factory=dict)
    clinics: dict[uuid.UUID, Clinic] = field(default_factory=dict)
    slot_rows: dict[uuid.UUID, DoctorSchedule] = field(default_factory=dict)
    exception_rows: dict[uuid.UUID, ScheduleException] = field(default_factory=dict)

    def clinic_of(self, slot: DoctorSchedule) -> Clinic:
        return self.clinics[self.affiliations[slot.affiliation_id].clinic_id]

    def affiliation_live(self, affiliation: DoctorAffiliation) -> bool:
        return affiliation.status == LIVE and self.clinics[affiliation.clinic_id].status == LIVE

    def slot_value(self, row: DoctorSchedule) -> engine.Slot:
        affiliation = self.affiliations[row.affiliation_id]
        return engine.Slot(
            id=row.id,
            clinic_id=affiliation.clinic_id,
            weekday=row.weekday,
            start=row.start_time,
            end=row.end_time,
            max_tokens=row.max_tokens,
            active=row.status == LIVE,
            clinic_active=self.affiliation_live(affiliation),
        )

    @property
    def slots(self) -> list[engine.Slot]:
        return [self.slot_value(row) for row in self.slot_rows.values()]

    @property
    def exceptions(self) -> list[engine.DateException]:
        return [exception_value(row) for row in self.exception_rows.values()]


def exception_value(row: ScheduleException) -> engine.DateException:
    return engine.DateException(
        id=row.id,
        schedule_id=row.schedule_id,
        day=row.exception_date,
        kind=row.kind.value,
        start=row.start_time,
        end=row.end_time,
        max_tokens=row.max_tokens,
        removed=row.removed_at is not None,
    )


async def my_doctor(session: AsyncSession, actor: Actor) -> Doctor:
    doctor = (
        await session.execute(select(Doctor).where(Doctor.user_id == actor.user_id))
    ).scalar_one_or_none()
    if doctor is None:
        # OAuth onboarding incomplete: no doctor row yet.
        raise NotFound()
    return doctor


def lock_statement(doctor_id: uuid.UUID) -> Select:
    """The per-doctor write lock (spec §7). Public so AC-12 can assert it."""
    return select(Doctor.id).where(Doctor.id == doctor_id).with_for_update()


async def lock_doctor(session: AsyncSession, doctor_id: uuid.UUID) -> None:
    await session.execute(lock_statement(doctor_id))


async def load(session: AsyncSession, doctor: Doctor) -> Loaded:
    """The doctor's affiliations (any status), their clinics, every slot under
    them (any status), and the live exceptions dated today or later."""
    today = dbtypes.today_pk()
    loaded = Loaded(doctor=doctor, today=today)

    rows = await session.execute(
        select(DoctorAffiliation, Clinic)
        .join(Clinic, Clinic.id == DoctorAffiliation.clinic_id)
        .where(DoctorAffiliation.doctor_id == doctor.id)
    )
    for affiliation, clinic in rows.all():
        loaded.affiliations[affiliation.id] = affiliation
        loaded.clinics[clinic.id] = clinic

    slots = await session.execute(
        select(DoctorSchedule)
        .join(DoctorAffiliation, DoctorAffiliation.id == DoctorSchedule.affiliation_id)
        .where(DoctorAffiliation.doctor_id == doctor.id)
    )
    loaded.slot_rows = {row.id: row for row in slots.scalars().all()}

    exceptions = await session.execute(
        select(ScheduleException)
        .join(DoctorSchedule, DoctorSchedule.id == ScheduleException.schedule_id)
        .join(DoctorAffiliation, DoctorAffiliation.id == DoctorSchedule.affiliation_id)
        .where(
            DoctorAffiliation.doctor_id == doctor.id,
            ScheduleException.removed_at.is_(None),
            ScheduleException.exception_date >= today,
        )
    )
    loaded.exception_rows = {row.id: row for row in exceptions.scalars().all()}
    return loaded


# ── Errors ───────────────────────────────────────────────────────────────
def hhmm(value: time) -> str:
    return value.strftime("%H:%M")


def conflict_error(loaded: Loaded, conflict: engine.Conflict) -> ScheduleConflict:
    """Turn the engine's plain conflict into the 409 the caller sees.

    `day` is a weekday number (the web handler localises it, spec §8.1);
    `date` is set when the conflicting window is on a specific date."""
    clinic = loaded.clinics.get(conflict.clinic_id)
    weekday = conflict.weekday if conflict.weekday is not None else conflict.day.weekday()  # type: ignore[union-attr]
    affiliation_live = any(
        loaded.affiliation_live(a)
        for a in loaded.affiliations.values()
        if a.clinic_id == conflict.clinic_id
    )
    return ScheduleConflict(
        {
            "clinic": clinic.name if clinic else "",
            "day": weekday,
            "start": hhmm(conflict.start),
            "end": hhmm(conflict.end),
            "date": conflict.day.isoformat() if conflict.day else "",
            "clinic_inactive": "" if affiliation_live else "1",
            "conflict_id": str(conflict.exception_id or conflict.schedule_id),
        }
    )


def raise_first(loaded: Loaded, conflicts: list[engine.Conflict]) -> None:
    if conflicts:
        raise conflict_error(loaded, conflicts[0])


def invalid(field_name: str, key: str) -> ValidationFailed:
    return ValidationFailed({"fields": {field_name: key}})


# ── Reads ────────────────────────────────────────────────────────────────
async def find_conflicts(
    session: AsyncSession,
    doctor_id: uuid.UUID,
    weekday: int,
    start: time,
    end: time,
    exclude_slot_id: uuid.UUID | None = None,
) -> list[engine.Conflict]:
    """R1: the doctor's active slots on `weekday` that `[start, end)` overlaps,
    at any clinic. An empty list means no conflict."""
    doctor = await session.get(Doctor, doctor_id)
    if doctor is None:
        return []
    loaded = await load(session, doctor)
    return engine.slot_conflicts(weekday, start, end, loaded.slots, exclude_slot_id)


def check_range(today: date, date_from: date, date_to: date) -> None:
    """R5: never a past date, never more than 30 days, never backwards."""
    if date_from < today or date_to < date_from:
        raise invalid("date_from", "errors.schedule_range_invalid")
    if (date_to - date_from).days + 1 > MAX_RANGE_DAYS:
        raise invalid("date_to", "errors.schedule_range_invalid")


def availability(
    loaded: Loaded, date_from: date, date_to: date, clinic_id: uuid.UUID | None = None
) -> list[engine.EffectiveWindow]:
    """R3 over a range for already-loaded state, sorted by date then start."""
    windows: list[engine.EffectiveWindow] = []
    day = date_from
    while day <= date_to:
        windows.extend(
            w
            for w in engine.effective_windows(day, loaded.slots, loaded.exceptions)
            if clinic_id is None or w.clinic_id == clinic_id
        )
        day += timedelta(days=1)
    return windows


async def get_effective_availability(
    session: AsyncSession,
    doctor_id: uuid.UUID,
    date_from: date,
    date_to: date,
    clinic_id: uuid.UUID | None = None,
) -> list[engine.EffectiveWindow]:
    """The only way to read a doctor's availability (spec §6): weekly slots
    with exceptions applied, for today up to 30 days. Each window keeps its
    source and its effective cap; windows are never merged. A pure read: no
    audit, no lock. It does not care who is calling."""
    doctor = await session.get(Doctor, doctor_id)
    if doctor is None:
        raise NotFound()
    loaded = await load(session, doctor)
    check_range(loaded.today, date_from, date_to)
    return availability(loaded, date_from, date_to, clinic_id)


# ── Writes: shared helpers ───────────────────────────────────────────────
def resolve_affiliation(
    loaded: Loaded, clinic_id: uuid.UUID, field_name: str = "clinic_id"
) -> DoctorAffiliation:
    """R2: the doctor's active affiliation at an active clinic; the most
    recent `start_date` when there are several. `end_date` is not consulted,
    as nowhere else in the code does."""
    live = [
        a
        for a in loaded.affiliations.values()
        if a.clinic_id == clinic_id and loaded.affiliation_live(a)
    ]
    if not live:
        raise invalid(field_name, "errors.schedule_clinic_not_affiliated")
    return max(live, key=lambda a: a.start_date)


async def booked_slot_ids(session: AsyncSession, slot_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """R10: which of these slots have upcoming booked or checked-in
    appointments. Count-only on `schedule_id`; no patient column is read."""
    if not slot_ids:
        return set()
    rows = await session.execute(
        select(Appointment.schedule_id)
        .where(
            Appointment.schedule_id.in_(slot_ids),
            Appointment.slot_start >= dbtypes.utcnow(),
            Appointment.status.in_(UPCOMING_BOOKED),
        )
        .distinct()
    )
    return {sid for sid in rows.scalars().all() if sid is not None}


def check_slot_window(
    loaded: Loaded,
    weekday: int,
    start: time,
    end: time,
    slot_id: uuid.UUID | None = None,
) -> None:
    """R1 (other weekly slots) and R1b (future extras at any clinic)."""
    raise_first(loaded, engine.slot_conflicts(weekday, start, end, loaded.slots, slot_id))
    raise_first(
        loaded,
        engine.slot_extra_conflicts(
            weekday, start, end, loaded.slots, loaded.exceptions, loaded.today, slot_id
        ),
    )


def slot_detail(loaded: Loaded, row: DoctorSchedule) -> dict[str, Any]:
    return {
        "affiliation_id": str(row.affiliation_id),
        "clinic_id": str(loaded.affiliations[row.affiliation_id].clinic_id),
        "weekday": row.weekday,
        "start": hhmm(row.start_time),
        "end": hhmm(row.end_time),
        "slot_minutes": row.slot_minutes,
        "max_tokens": row.max_tokens,
    }


def exception_detail(loaded: Loaded, row: ScheduleException) -> dict[str, Any]:
    slot = loaded.slot_rows[row.schedule_id]
    return {
        "schedule_id": str(row.schedule_id),
        "clinic_id": str(loaded.affiliations[slot.affiliation_id].clinic_id),
        "date": row.exception_date.isoformat(),
        "type": row.kind.value,
        "start": hhmm(row.start_time) if row.start_time else None,
        "end": hhmm(row.end_time) if row.end_time else None,
        "max_tokens": row.max_tokens,
        # Free text may hold PII: only its length is ever recorded (spec §8.2).
        "reason_length": len(row.reason or ""),
    }


async def write_audit(
    session: AsyncSession,
    actor: Actor,
    *,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID,
    detail: dict[str, Any],
    user_agent: str | None,
) -> None:
    await audit.write(
        session,
        action=action,
        actor_user_id=actor.user_id,
        actor_role=ACTOR_ROLE,
        resource_type=resource_type,
        ip_address=actor.ip_address,
        user_agent=user_agent,
        detail={"resource_id": str(resource_id), **detail},
    )


async def begin_write(session: AsyncSession, actor: Actor) -> Loaded:
    """Resolve the caller's doctor, take the per-doctor lock, then load."""
    doctor = await my_doctor(session, actor)
    await lock_doctor(session, doctor.id)
    return await load(session, doctor)


def own_live_slot(loaded: Loaded, slot_id: uuid.UUID) -> DoctorSchedule:
    row = loaded.slot_rows.get(slot_id)
    if row is None or row.status != LIVE:
        raise NotFound()  # foreign, nonexistent and removed look the same
    return row


# ── Writes: weekly slots ─────────────────────────────────────────────────
async def add_slots(
    session: AsyncSession, actor: Actor, body: SlotCreate, *, user_agent: str | None = None
) -> list[DoctorSchedule]:
    """One slot per chosen weekday, all or nothing (R9), each checked against
    R1, R1b, R2 and R7; one audit row per slot."""
    loaded = await begin_write(session, actor)
    affiliation = resolve_affiliation(loaded, body.clinic_id)
    max_tokens = body.resolved_max_tokens()

    for weekday in body.weekdays:
        active = sum(
            1 for r in loaded.slot_rows.values() if r.status == LIVE and r.weekday == weekday
        )
        if active >= MAX_SLOTS_PER_WEEKDAY:
            raise ListFull(MAX_SLOTS_PER_WEEKDAY)
        check_slot_window(loaded, weekday, body.start_time, body.end_time)

    created = [
        DoctorSchedule(
            id=dbtypes.uuid7(),
            affiliation_id=affiliation.id,
            weekday=weekday,
            start_time=body.start_time,
            end_time=body.end_time,
            slot_minutes=body.slot_minutes,
            max_tokens=max_tokens,
            status=LIVE,
        )
        for weekday in body.weekdays
    ]
    session.add_all(created)
    await session.flush()
    for row in created:
        loaded.slot_rows[row.id] = row
        await write_audit(
            session,
            actor,
            action=audit.SCHEDULE_SLOT_ADD,
            resource_type="doctor_schedule",
            resource_id=row.id,
            detail={"after": slot_detail(loaded, row)},
            user_agent=user_agent,
        )
    await session.commit()
    return created


async def update_slot(
    session: AsyncSession,
    actor: Actor,
    slot_id: uuid.UUID,
    body: SlotUpdate,
    *,
    user_agent: str | None = None,
) -> DoctorSchedule:
    """Edit a live slot for all future weeks (R6). A slot at an inactive
    clinic can be removed, not edited (R2); one with upcoming bookings cannot
    change at all (R10)."""
    loaded = await begin_write(session, actor)
    row = own_live_slot(loaded, slot_id)
    if not loaded.affiliation_live(loaded.affiliations[row.affiliation_id]):
        raise invalid("clinic_id", "errors.schedule_clinic_not_affiliated")
    affiliation = resolve_affiliation(loaded, body.clinic_id)
    if row.id in await booked_slot_ids(session, [row.id]):
        raise invalid("__all__", "errors.schedule_has_appointments")
    check_slot_window(loaded, body.weekday, body.start_time, body.end_time, row.id)

    before = slot_detail(loaded, row)
    row.affiliation_id = affiliation.id
    row.weekday = body.weekday
    row.start_time = body.start_time
    row.end_time = body.end_time
    row.slot_minutes = body.slot_minutes
    row.max_tokens = body.resolved_max_tokens()
    await session.flush()
    await write_audit(
        session,
        actor,
        action=audit.SCHEDULE_SLOT_UPDATE,
        resource_type="doctor_schedule",
        resource_id=row.id,
        detail={"before": before, "after": slot_detail(loaded, row)},
        user_agent=user_agent,
    )
    await session.commit()
    return row


async def remove_slot(
    session: AsyncSession, actor: Actor, slot_id: uuid.UUID, *, user_agent: str | None = None
) -> None:
    """Soft-remove a slot (`status = 'inactive'`) and, with it, its live
    exceptions dated today or later (R11), so no extra is left anchored to a
    dead slot. Blocked while upcoming bookings exist (R10).

    R11 asks for R4b before those removals; it cannot fire here, because the
    time a removed leave would free belongs to the slot being removed."""
    loaded = await begin_write(session, actor)
    row = own_live_slot(loaded, slot_id)
    if row.id in await booked_slot_ids(session, [row.id]):
        raise invalid("__all__", "errors.schedule_has_appointments")

    now = dbtypes.utcnow()
    row.status = REMOVED
    dependents = [e for e in loaded.exception_rows.values() if e.schedule_id == row.id]
    for exception in dependents:
        exception.removed_at = now
    await session.flush()

    await write_audit(
        session,
        actor,
        action=audit.SCHEDULE_SLOT_REMOVE,
        resource_type="doctor_schedule",
        resource_id=row.id,
        detail={"before": slot_detail(loaded, row)},
        user_agent=user_agent,
    )
    for exception in dependents:
        await write_audit(
            session,
            actor,
            action=audit.SCHEDULE_EXCEPTION_REMOVE,
            resource_type="schedule_exception",
            resource_id=exception.id,
            detail={**exception_detail(loaded, exception), "with_slot": True},
            user_agent=user_agent,
        )
    await session.commit()


# ── Writes: exceptions ───────────────────────────────────────────────────
def check_exception_budget(loaded: Loaded, adding: int) -> None:
    """R7: at most 200 live exception rows dated today or later. Rows, not
    entries: one "all clinics" time-off entry can use several."""
    if len(loaded.exception_rows) + adding > MAX_UPCOMING_EXCEPTIONS:
        raise ListFull(MAX_UPCOMING_EXCEPTIONS)


async def _audit_exception_add(
    session: AsyncSession,
    actor: Actor,
    loaded: Loaded,
    row: ScheduleException,
    user_agent: str | None,
) -> None:
    await write_audit(
        session,
        actor,
        action=audit.SCHEDULE_EXCEPTION_ADD,
        resource_type="schedule_exception",
        resource_id=row.id,
        detail=exception_detail(loaded, row),
        user_agent=user_agent,
    )


async def add_time_off(
    session: AsyncSession, actor: Actor, body: TimeOffCreate, *, user_agent: str | None = None
) -> list[ScheduleException]:
    """Time off on one date (spec §4.2): one `leave` (or `emergency`) row per
    live slot it touches, all or nothing (R9). A whole-day entry stores no
    times; a window stores its intersection with each slot. It covers only the
    slots that exist now (known v1 limitation, decision 5)."""
    loaded = await begin_write(session, actor)
    if body.clinic_id is not None:
        resolve_affiliation(loaded, body.clinic_id)
    planned = engine.time_off_rows(
        body.exception_date, loaded.slots, body.clinic_id, body.start_time, body.end_time
    )
    if not planned:
        raise invalid("__all__", "errors.schedule_nothing_to_block")
    check_exception_budget(loaded, len(planned))

    kind = ScheduleExceptionType.EMERGENCY if body.emergency else ScheduleExceptionType.LEAVE
    created = [
        ScheduleException(
            id=dbtypes.uuid7(),
            schedule_id=item.schedule_id,
            exception_date=body.exception_date,
            kind=kind,
            start_time=item.start,
            end_time=item.end,
            reason=body.reason,
            created_at=dbtypes.utcnow(),
        )
        for item in planned
    ]
    session.add_all(created)
    await session.flush()
    for row in created:
        await _audit_exception_add(session, actor, loaded, row, user_agent)
    await session.commit()
    return created


async def add_extra(
    session: AsyncSession, actor: Actor, body: ExtraCreate, *, user_agent: str | None = None
) -> ScheduleException:
    """Extra hours at one clinic on one date, anchored to the earliest live
    slot there, with an optional cap of its own (NULL = inherit the anchor's,
    read live). Rejected if it overlaps any effective window that date, at
    any clinic, the anchor included (R4, decision 17)."""
    loaded = await begin_write(session, actor)
    resolve_affiliation(loaded, body.clinic_id)
    anchor = engine.choose_anchor(body.clinic_id, loaded.slots)
    if anchor is None:
        raise invalid("clinic_id", "errors.schedule_extra_needs_slot")
    raise_first(
        loaded,
        engine.extra_conflicts(
            body.exception_date, body.start_time, body.end_time, loaded.slots, loaded.exceptions
        ),
    )
    check_exception_budget(loaded, 1)

    row = ScheduleException(
        id=dbtypes.uuid7(),
        schedule_id=anchor.id,
        exception_date=body.exception_date,
        kind=ScheduleExceptionType.EXTRA,
        start_time=body.start_time,
        end_time=body.end_time,
        max_tokens=body.max_tokens,
        reason=body.reason,
        created_at=dbtypes.utcnow(),
    )
    session.add(row)
    await session.flush()
    await _audit_exception_add(session, actor, loaded, row, user_agent)
    await session.commit()
    return row


async def remove_exception(
    session: AsyncSession,
    actor: Actor,
    exception_id: uuid.UUID,
    *,
    user_agent: str | None = None,
) -> None:
    """Soft-remove one exception row. A past one cannot be removed (R5);
    removing time off that would expose a slot under extra hours at any
    clinic is rejected (R4b)."""
    loaded = await begin_write(session, actor)
    row = (
        await session.execute(
            select(ScheduleException)
            .join(DoctorSchedule, DoctorSchedule.id == ScheduleException.schedule_id)
            .join(DoctorAffiliation, DoctorAffiliation.id == DoctorSchedule.affiliation_id)
            .where(
                ScheduleException.id == exception_id,
                DoctorAffiliation.doctor_id == loaded.doctor.id,
                ScheduleException.removed_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound()  # foreign, nonexistent and removed look the same
    if row.exception_date < loaded.today:
        raise invalid("__all__", "errors.schedule_past_date")
    if row.kind is not ScheduleExceptionType.EXTRA:
        raise_first(
            loaded, engine.time_off_removal_conflicts(row.id, loaded.slots, loaded.exceptions)
        )

    row.removed_at = dbtypes.utcnow()
    await session.flush()
    await write_audit(
        session,
        actor,
        action=audit.SCHEDULE_EXCEPTION_REMOVE,
        resource_type="schedule_exception",
        resource_id=row.id,
        detail=exception_detail(loaded, row),
        user_agent=user_agent,
    )
    await session.commit()
