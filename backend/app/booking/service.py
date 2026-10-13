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
from datetime import date, datetime, time, timedelta

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import writer as audit
from ..db import types as dbtypes
from ..db.models import (
    Appointment,
    Clinic,
    Doctor,
    DoctorAffiliation,
    DoctorSchedule,
    Patient,
    UserRole,
    VerificationStatus,
)
from ..deps import Actor
from ..errors import BookingRefused, NotFound
from ..queueing.schemas import CHANNEL_WALK_IN
from ..schedule import service as schedule_service
from . import slots
from .schemas import (
    ACTIVE_STATUSES,
    CHANNEL_ONLINE,
    DEFAULT_MINUTES,
    HELD_STATUSES,
    HORIZON_DAYS,
    LEAD_MINUTES,
    SEARCH_LIMIT,
    AppointmentRow,
    ClinicRef,
    DayOptions,
    DayRow,
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


# The longest turn D7 allows (R12), so an appointment that starts this long before a
# time can still overlap it.
_LOOKBACK = timedelta(minutes=60)


async def _doctor_held(
    session: AsyncSession,
    doctor_id: uuid.UUID,
    lo: datetime,
    hi: datetime,
    *,
    exclude: uuid.UUID | None = None,
) -> list[tuple[datetime, int]]:
    """(start, minutes) of the doctor's active appointments starting in `[lo, hi)`,
    whatever schedule row they hang off. Taken and full are counted per session
    (`_active_starts`), but the doctor still sees one patient at a time: D7 R10's
    "time off plus extra hours" moves a day onto another row (an extra hangs off the
    doctor's earliest slot), and what was booked before must stay taken."""
    stmt = (
        select(Appointment.slot_start, DoctorSchedule.slot_minutes)
        .join(DoctorSchedule, DoctorSchedule.id == Appointment.schedule_id, isouter=True)
        .where(
            Appointment.doctor_id == doctor_id,
            Appointment.status.in_(ACTIVE_STATUSES),
            Appointment.slot_start >= lo,
            Appointment.slot_start < hi,
            # a walk-in counts toward the cap but is not a booked turn: it never blocks a
            # bookable time (a NULL channel is a legacy booking, and does)
            or_(
                Appointment.booking_channel.is_(None),
                Appointment.booking_channel != CHANNEL_WALK_IN,
            ),
        )
    )
    if exclude is not None:
        stmt = stmt.where(Appointment.id != exclude)
    return [
        (slots.as_utc(start), minutes or DEFAULT_MINUTES)
        for start, minutes in (await session.execute(stmt)).all()
    ]


def _clashes(start: datetime, minutes: int, held: list[tuple[datetime, int]]) -> bool:
    return any(slots.overlap(start, minutes, other, other_minutes) for other, other_minutes in held)


async def list_options(
    session: AsyncSession,
    doctor_id: uuid.UUID,
    clinic_id: uuid.UUID,
    *,
    exclude: uuid.UUID | None = None,
) -> list[DayOptions]:
    """The free times at one hospital for the next 14 days, only days that have
    any. Read-only: it books nothing and takes no lock. `exclude` leaves one
    appointment out of what is taken and of the cap: the booking being moved."""
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
        exclude=exclude,
    )
    held = await _doctor_held(
        session,
        doctor_id,
        min(lo for lo, _ in bounds) - _LOOKBACK,
        max(hi for _, hi in bounds),
        exclude=exclude,
    )
    by_day: dict[date, list[datetime]] = {}
    for window, (lo, hi) in zip(windows, bounds, strict=True):
        booked = [s for s in taken[window.schedule_id] if lo <= s < hi]
        times = slots.bookable_times(window, booked, earliest=earliest, last_day=last_day)
        times = [t for t in times if not _clashes(t, window.slot_minutes, held)]
        if times:
            by_day.setdefault(window.day, []).extend(times)
    return [DayOptions(day=d, times=tuple(sorted(ts))) for d, ts in sorted(by_day.items())]


# ── Booking ──────────────────────────────────────────────────────────────
async def _patient(session: AsyncSession, actor: Actor) -> Patient:
    """The caller's own patient row. Anyone but a patient is `NotFound`."""
    if actor.role != UserRole.PATIENT.value:
        raise NotFound()
    patient = (
        await session.execute(select(Patient).where(Patient.user_id == actor.user_id))
    ).scalar_one_or_none()
    if patient is None:
        raise NotFound()
    return patient


async def _check_patient_rules(
    session: AsyncSession,
    patient_id: uuid.UUID,
    doctor_id: uuid.UUID,
    start: datetime,
    minutes: int,
    *,
    ignore: uuid.UUID | None,
) -> None:
    """The patient cannot hold two overlapping appointments (any doctor) or two
    with one doctor on one Pakistan day. `ignore` leaves out the appointment a
    reschedule is replacing."""
    day_start = slots.to_utc(slots.to_local(start).date(), time(0, 0))
    stmt = (
        select(Appointment.doctor_id, Appointment.slot_start, DoctorSchedule.slot_minutes)
        .join(DoctorSchedule, DoctorSchedule.id == Appointment.schedule_id, isouter=True)
        .where(
            Appointment.patient_id == patient_id,
            Appointment.status.in_(HELD_STATUSES),
            Appointment.slot_start >= day_start - timedelta(hours=1),
            Appointment.slot_start < day_start + timedelta(days=1, hours=1),
        )
    )
    if ignore is not None:
        stmt = stmt.where(Appointment.id != ignore)
    held = [
        (held_doctor, slots.as_utc(held_start), held_minutes or DEFAULT_MINUTES)
        for held_doctor, held_start, held_minutes in (await session.execute(stmt)).all()
    ]
    for _, held_start, held_minutes in held:
        if slots.overlap(start, minutes, held_start, held_minutes):
            raise BookingRefused("overlap")
    for held_doctor, held_start, _ in held:
        if held_doctor == doctor_id and slots.same_local_day(start, held_start):
            raise BookingRefused("same_day")


async def _place(
    session: AsyncSession,
    actor: Actor,
    doctor_id: uuid.UUID,
    clinic_id: uuid.UUID,
    slot_start: datetime,
    *,
    replacing: Appointment | None,
    user_agent: str | None,
) -> Appointment:
    """Book `slot_start`; when `replacing` is given, cancel that appointment in
    the same transaction (a reschedule). Lock the doctor, re-read what is taken,
    check every rule, then write. Any refusal leaves everything untouched."""
    patient = await _patient(session, actor)
    await schedule_service.lock_doctor(session, doctor_id)

    found = await _visible_doctors(session, doctor_id)
    if doctor_id not in found or clinic_id not in found[doctor_id][1]:
        raise NotFound()

    now = _now()
    if replacing is not None:
        await session.refresh(replacing)
        if replacing.status != "booked" or slots.as_utc(replacing.slot_start) <= now:
            raise BookingRefused("not_cancellable")

    earliest = now + timedelta(minutes=LEAD_MINUTES)
    last_day = current_day() + timedelta(days=HORIZON_DAYS)
    start = slots.as_utc(slot_start)
    if start < earliest:
        raise BookingRefused("too_soon")
    day = slots.to_local(start).date()
    if day > last_day:
        raise BookingRefused("too_far")

    windows = await _windows(session, doctor_id, clinic_id, day, day)
    window = next((w for w in windows if slots.in_window(w, start)), None)
    if window is None:
        raise BookingRefused("not_offered")

    ignore = replacing.id if replacing is not None else None
    lo, hi = slots.window_bounds(window)
    booked = (await _active_starts(session, {window.schedule_id}, lo, hi, exclude=ignore))[
        window.schedule_id
    ]
    reason = slots.refusal(window, booked, start, earliest=earliest, last_day=last_day)
    if reason is not None:
        raise BookingRefused(reason)
    held = await _doctor_held(
        session,
        doctor_id,
        start - _LOOKBACK,
        start + timedelta(minutes=window.slot_minutes),
        exclude=ignore,
    )
    if _clashes(start, window.slot_minutes, held):
        raise BookingRefused("taken")
    await _check_patient_rules(
        session, patient.id, doctor_id, start, window.slot_minutes, ignore=ignore
    )

    if replacing is not None:
        replacing.status = "cancelled"
        await session.flush()  # frees its time before the new row is inserted
    appointment = Appointment(
        patient_id=patient.id,
        doctor_id=doctor_id,
        clinic_id=clinic_id,
        schedule_id=window.schedule_id,
        slot_start=start,
        status="booked",
        booking_channel=CHANNEL_ONLINE,
    )
    session.add(appointment)
    try:
        await session.flush()
    except IntegrityError:
        # The partial unique index: a concurrent booking the checks did not see.
        await session.rollback()
        raise BookingRefused("taken") from None

    detail = {
        "appointment_id": str(appointment.id),
        "doctor_id": str(doctor_id),
        "clinic_id": str(clinic_id),
    }
    action = audit.APPOINTMENT_BOOK
    if replacing is not None:
        detail["replaces"] = str(replacing.id)
        action = audit.APPOINTMENT_RESCHEDULE
    await audit.write(
        session,
        action=action,
        actor_user_id=actor.user_id,
        actor_role=actor.role,
        subject_patient_id=patient.id,
        resource_type="appointment",
        ip_address=actor.ip_address,
        user_agent=user_agent,
        detail=detail,
    )
    await session.commit()
    return appointment


async def book(
    session: AsyncSession,
    actor: Actor,
    doctor_id: uuid.UUID,
    clinic_id: uuid.UUID,
    slot_start: datetime,
    *,
    user_agent: str | None = None,
) -> Appointment:
    """Book `slot_start` with a verified doctor at one of their live hospitals.
    Raises `NotFound` (not a patient, doctor or hospital not bookable) or
    `BookingRefused(reason)`."""
    return await _place(
        session, actor, doctor_id, clinic_id, slot_start, replacing=None, user_agent=user_agent
    )


# ── Cancel and reschedule ────────────────────────────────────────────────
async def _own_appointment(
    session: AsyncSession, patient_id: uuid.UUID, appointment_id: uuid.UUID
) -> Appointment:
    appointment = (
        await session.execute(
            select(Appointment).where(
                Appointment.id == appointment_id, Appointment.patient_id == patient_id
            )
        )
    ).scalar_one_or_none()
    if appointment is None:
        raise NotFound()
    return appointment


async def cancel(
    session: AsyncSession,
    actor: Actor,
    appointment_id: uuid.UUID,
    *,
    user_agent: str | None = None,
) -> None:
    """Cancel the caller's own `booked` appointment, any time before it starts.
    The row is kept (`cancelled`); the time and the cap are freed."""
    patient = await _patient(session, actor)
    appointment = await _own_appointment(session, patient.id, appointment_id)
    await schedule_service.lock_doctor(session, appointment.doctor_id)
    await session.refresh(appointment)
    if appointment.status != "booked" or slots.as_utc(appointment.slot_start) <= _now():
        raise BookingRefused("not_cancellable")
    appointment.status = "cancelled"
    await audit.write(
        session,
        action=audit.APPOINTMENT_CANCEL,
        actor_user_id=actor.user_id,
        actor_role=actor.role,
        subject_patient_id=patient.id,
        resource_type="appointment",
        ip_address=actor.ip_address,
        user_agent=user_agent,
        detail={
            "appointment_id": str(appointment.id),
            "doctor_id": str(appointment.doctor_id),
            "clinic_id": str(appointment.clinic_id),
        },
    )
    await session.commit()


async def reschedule(
    session: AsyncSession,
    actor: Actor,
    appointment_id: uuid.UUID,
    clinic_id: uuid.UUID,
    slot_start: datetime,
    *,
    user_agent: str | None = None,
) -> Appointment:
    """Move the caller's own appointment to `slot_start` with the same doctor
    (at any of their live hospitals): book the new time and cancel the old one
    in one transaction. A refused new time leaves the old booking untouched."""
    patient = await _patient(session, actor)
    old = await _own_appointment(session, patient.id, appointment_id)
    return await _place(
        session, actor, old.doctor_id, clinic_id, slot_start, replacing=old, user_agent=user_agent
    )


# ── Lists ────────────────────────────────────────────────────────────────
async def _patient_rows(session: AsyncSession, patient_id: uuid.UUID) -> list[AppointmentRow]:
    stmt = (
        select(Appointment, Doctor, Clinic)
        .join(Doctor, Doctor.id == Appointment.doctor_id)
        .join(Clinic, Clinic.id == Appointment.clinic_id)
        .where(Appointment.patient_id == patient_id)
        .order_by(Appointment.slot_start)
    )
    now = _now()
    rows: list[AppointmentRow] = []
    for appointment, doctor, clinic in (await session.execute(stmt)).all():
        start = slots.as_utc(appointment.slot_start)
        rows.append(
            AppointmentRow(
                id=appointment.id,
                doctor_id=doctor.id,
                doctor_name=doctor.full_name,
                specialty=doctor.specialty,
                clinic_id=clinic.id,
                clinic_name=clinic.name,
                starts_at=start.astimezone(dbtypes.PKT),
                status=appointment.status,
                upcoming=appointment.status in HELD_STATUSES and start >= now,
                can_change=appointment.status == "booked" and start > now,
            )
        )
    return rows


async def my_appointments(
    session: AsyncSession, actor: Actor
) -> tuple[list[AppointmentRow], list[AppointmentRow]]:
    """(upcoming, past) for the caller. Upcoming: booked or checked in and not
    yet started, soonest first. Past: everything else, newest first."""
    patient = await _patient(session, actor)
    rows = await _patient_rows(session, patient.id)
    upcoming = [r for r in rows if r.upcoming]
    past = sorted((r for r in rows if not r.upcoming), key=lambda r: r.starts_at, reverse=True)
    return upcoming, past


async def appointment_for_patient(
    session: AsyncSession, actor: Actor, appointment_id: uuid.UUID
) -> AppointmentRow:
    patient = await _patient(session, actor)
    for row in await _patient_rows(session, patient.id):
        if row.id == appointment_id:
            return row
    raise NotFound()


async def doctor_day(session: AsyncSession, actor: Actor, day: date) -> list[DayRow]:
    """A verified doctor's appointments on one Pakistan day, by time: name, time
    and status only (booking never grants record access). Anyone else, and a
    doctor who is not verified, gets `NotFound`."""
    if actor.role != UserRole.DOCTOR.value:
        raise NotFound()
    doctor = await schedule_service.my_doctor(session, actor)
    if doctor.verification_status != VerificationStatus.VERIFIED:
        raise NotFound()
    lo = slots.to_utc(day, time(0, 0))
    stmt = (
        select(Appointment.slot_start, Appointment.status, Patient.full_name)
        .join(Patient, Patient.id == Appointment.patient_id)
        .where(
            Appointment.doctor_id == doctor.id,
            Appointment.slot_start >= lo,
            Appointment.slot_start < lo + timedelta(days=1),
        )
        .order_by(Appointment.slot_start)
    )
    return [
        DayRow(starts_at=slots.to_local(start), patient_name=name, status=status)
        for start, status, name in (await session.execute(stmt)).all()
    ]


# ── Small public wrappers for the live queue (P6) ────────────────────────
def now() -> datetime:
    """The current instant, UTC. This is the booking clock: freezing `_now` freezes it."""
    return _now()


async def session_windows(
    session: AsyncSession, doctor_id: uuid.UUID, clinic_id: uuid.UUID, day: date
) -> list[slots.Window]:
    """The doctor's session windows at one hospital on one day (D7 availability).
    `day` must be today or later: D7 refuses a past day."""
    return await _windows(session, doctor_id, clinic_id, day, day)


async def is_bookable(session: AsyncSession, doctor_id: uuid.UUID, clinic_id: uuid.UUID) -> bool:
    """True for a verified doctor at one of their live hospitals."""
    found = await _visible_doctors(session, doctor_id)
    return doctor_id in found and clinic_id in found[doctor_id][1]


async def visible_pairs(session: AsyncSession) -> list[tuple[Doctor, Clinic]]:
    """Every verified doctor with each of their live hospitals, as (doctor, hospital)
    pairs, by doctor name then hospital name."""
    return [
        (doctor, clinic)
        for doctor, clinics in (await _visible_doctors(session)).values()
        for clinic in sorted(clinics.values(), key=lambda c: c.name)
    ]


async def active_starts(
    session: AsyncSession, schedule_id: uuid.UUID, lo: datetime, hi: datetime
) -> list[datetime]:
    """The starts (aware UTC) of the active appointments of one schedule in `[lo, hi)`."""
    return (await _active_starts(session, {schedule_id}, lo, hi))[schedule_id]
