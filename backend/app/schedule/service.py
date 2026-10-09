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

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import types as dbtypes
from ..db.models import (
    Clinic,
    Doctor,
    DoctorAffiliation,
    DoctorSchedule,
    ScheduleException,
)
from ..deps import Actor
from ..errors import NotFound, ScheduleConflict, ValidationFailed
from . import engine

ACTOR_ROLE = "doctor"
LIVE = "active"  # doctor_schedule.status / doctor_affiliation.status / clinic.status
REMOVED = "inactive"  # what D7 writes when it removes a slot
MAX_RANGE_DAYS = 30  # R5 / TDD §5


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
