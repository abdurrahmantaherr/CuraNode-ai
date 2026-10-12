"""Appointment booking business logic (P5, FR15, FR16; spec 3.2).

Every rule lives here: who is visible, which times are free, who may book,
cancel or move what. Every write locks the doctor first (D7's per-doctor
`SELECT ... FOR UPDATE`), re-reads what is taken and the session's count, then
writes; the partial unique index on `appointment` is the backstop. The pure
grid and cap logic is in `slots.py`.

Scoping: the app connects with BYPASSRLS, so every patient query filters by the
caller's own `patient_id`, resolved from `actor.user_id` and never read from a
request. A foreign or nonexistent appointment is one identical `NotFound`.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import types as dbtypes
from ..db.models import (
    Appointment,
    Clinic,
    Doctor,
    DoctorAffiliation,
    DoctorSchedule,
    VerificationStatus,
)
from ..errors import NotFound
from ..schedule import service as schedule_service
from . import slots
from .schemas import (
    ACTIVE_STATUSES,
    HORIZON_DAYS,
    LEAD_MINUTES,
    SEARCH_LIMIT,
    ClinicRef,
    DayOptions,
    DoctorCard,
)

LIVE = "active"  # doctor_affiliation.status / clinic.status


def _now() -> datetime:
    """The current instant, UTC. Tests replace this one function to freeze the clock."""
    return dbtypes.utcnow()


def current_day() -> date:
    """Today in Pakistan."""
    return _now().astimezone(dbtypes.PKT).date()


# ── Who is visible ───────────────────────────────────────────────────────
async def _visible_doctors(
    session: AsyncSession, doctor_id: uuid.UUID | None = None
) -> dict[uuid.UUID, tuple[Doctor, dict[uuid.UUID, Clinic]]]:
    """Verified doctors and their live hospitals: the only doctors a patient may
    see or book (D7 decision 6). A doctor with no live hospital is absent."""
    stmt = (
        select(Doctor, Clinic)
        .join(DoctorAffiliation, DoctorAffiliation.doctor_id == Doctor.id)
        .join(Clinic, Clinic.id == DoctorAffiliation.clinic_id)
        .where(
            Doctor.verification_status == VerificationStatus.VERIFIED,
            DoctorAffiliation.status == LIVE,
            Clinic.status == LIVE,
        )
        .order_by(Doctor.full_name, Doctor.id)
    )
    if doctor_id is not None:
        stmt = stmt.where(Doctor.id == doctor_id)
    found: dict[uuid.UUID, tuple[Doctor, dict[uuid.UUID, Clinic]]] = {}
    for doctor, clinic in (await session.execute(stmt)).all():
        found.setdefault(doctor.id, (doctor, {}))[1][clinic.id] = clinic
    return found


def _card(doctor: Doctor, clinics: dict[uuid.UUID, Clinic]) -> DoctorCard:
    return DoctorCard(
        id=doctor.id,
        name=doctor.full_name,
        specialty=doctor.specialty,
        clinics=tuple(
            ClinicRef(id=c.id, name=c.name, city=c.city)
            for c in sorted(clinics.values(), key=lambda c: c.name)
        ),
    )


async def search_doctors(
    session: AsyncSession, *, query: str = "", specialty: str = ""
) -> list[DoctorCard]:
    """Visible doctors whose name or any hospital name contains `query`
    (case-insensitive), optionally of one exact `specialty`, by name."""
    cards = [_card(d, c) for d, c in (await _visible_doctors(session)).values()]
    if specialty:
        cards = [c for c in cards if c.specialty == specialty]
    needle = query.strip().casefold()
    if needle:
        cards = [
            c
            for c in cards
            if needle in c.name.casefold() or any(needle in cl.name.casefold() for cl in c.clinics)
        ]
    return cards[:SEARCH_LIMIT]


async def specialty_options(session: AsyncSession) -> list[str]:
    visible = (await _visible_doctors(session)).values()
    return sorted({d.specialty for d, _ in visible if d.specialty})


async def get_doctor(session: AsyncSession, doctor_id: uuid.UUID) -> DoctorCard:
    found = await _visible_doctors(session, doctor_id)
    if doctor_id not in found:
        raise NotFound()
    doctor, clinics = found[doctor_id]
    return _card(doctor, clinics)


# ── Sessions and what is already booked ──────────────────────────────────
async def _windows(
    session: AsyncSession,
    doctor_id: uuid.UUID,
    clinic_id: uuid.UUID,
    date_from: date,
    date_to: date,
) -> list[slots.Window]:
    """D7's effective session windows at one hospital, with the doctor's minutes
    per patient attached. This is the only way availability is read (D7 §6)."""
    found = await schedule_service.get_effective_availability(
        session, doctor_id, date_from, date_to, clinic_id
    )
    if not found:
        return []
    rows = await session.execute(
        select(DoctorSchedule.id, DoctorSchedule.slot_minutes).where(
            DoctorSchedule.id.in_({w.schedule_id for w in found})
        )
    )
    minutes = {schedule_id: slot_minutes for schedule_id, slot_minutes in rows.all()}
    return [
        slots.Window(
            schedule_id=w.schedule_id,
            clinic_id=w.clinic_id,
            day=w.day,
            start=w.start,
            end=w.end,
            slot_minutes=minutes[w.schedule_id],
            max_tokens=w.max_tokens,
        )
        for w in found
    ]


async def _active_starts(
    session: AsyncSession,
    schedule_ids: set[uuid.UUID],
    lo: datetime,
    hi: datetime,
    *,
    exclude: uuid.UUID | None = None,
) -> dict[uuid.UUID, list[datetime]]:
    """The starts (aware UTC) of the active appointments of these schedules in
    `[lo, hi)`, grouped by schedule. `exclude` leaves one appointment out (the
    one a reschedule is replacing)."""
    stmt = select(Appointment.schedule_id, Appointment.slot_start).where(
        Appointment.schedule_id.in_(schedule_ids),
        Appointment.status.in_(ACTIVE_STATUSES),
        Appointment.slot_start >= lo,
        Appointment.slot_start < hi,
    )
    if exclude is not None:
        stmt = stmt.where(Appointment.id != exclude)
    grouped: dict[uuid.UUID, list[datetime]] = {sid: [] for sid in schedule_ids}
    for schedule_id, start in (await session.execute(stmt)).all():
        grouped[schedule_id].append(slots.as_utc(start))
    return grouped


async def list_options(
    session: AsyncSession, doctor_id: uuid.UUID, clinic_id: uuid.UUID
) -> list[DayOptions]:
    """The free times at one hospital for the next 14 days, only days that have
    any. Read-only: it books nothing and takes no lock."""
    found = await _visible_doctors(session, doctor_id)
    if doctor_id not in found or clinic_id not in found[doctor_id][1]:
        raise NotFound()
    earliest = _now() + timedelta(minutes=LEAD_MINUTES)
    today = current_day()
    last_day = today + timedelta(days=HORIZON_DAYS)
    windows = await _windows(session, doctor_id, clinic_id, today, last_day)
    if not windows:
        return []
    bounds = [slots.window_bounds(w) for w in windows]
    taken = await _active_starts(
        session,
        {w.schedule_id for w in windows},
        min(lo for lo, _ in bounds),
        max(hi for _, hi in bounds),
    )
    by_day: dict[date, list[datetime]] = {}
    for window, (lo, hi) in zip(windows, bounds, strict=True):
        booked = [s for s in taken[window.schedule_id] if lo <= s < hi]
        times = slots.bookable_times(window, booked, earliest=earliest, last_day=last_day)
        if times:
            by_day.setdefault(window.day, []).extend(times)
    return [DayOptions(day=d, times=tuple(sorted(ts))) for d, ts in sorted(by_day.items())]
