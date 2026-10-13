"""Live queue and check-in business logic (P6; FR17, FR19, FR24; spec 3.2).

A queue is one doctor session: one effective window on one date at one clinic,
the same unit booking uses for the cap. A queue entry belongs to a session
through its appointment (`schedule_id` and a `slot_start` inside the window).
Every action takes the doctor lock (`schedule_service.lock_doctor`) first, then
re-reads the queue, then writes; the database enforces neither unique tokens nor
the cap, so this lock is the guard.

Scoping: the app connects with BYPASSRLS, so every query is filtered by the
caller's own clinics (an admin) or own id (a doctor, a patient). A foreign or
unknown id is one identical `NotFound`.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..booking import service as booking_service
from ..booking import slots
from ..booking.schemas import ACTIVE_STATUSES, DEFAULT_MINUTES
from ..db.models import Appointment, Clinic, Doctor, Patient, QueueEntry, UserRole
from ..deps import Actor
from ..errors import NotFound
from . import order
from .schemas import (
    CHANNEL_WALK_IN,
    STATE_IN_ROOM,
    STATE_SERVED,
    STATE_WAITING,
    Line,
    QueueStatus,
    SessionKey,
    SessionView,
)


# ── Scope, time and sessions ─────────────────────────────────────────────
def _staff(actor: Actor) -> frozenset[uuid.UUID]:
    """The admin's own clinics. Anyone else, or an admin with none, is `NotFound`."""
    if actor.role != UserRole.CLINIC_ADMIN.value or not actor.clinic_ids:
        raise NotFound()
    return actor.clinic_ids


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    """The aware UTC start of a Pakistan day and of the next one."""
    lo = slots.to_utc(day, time(0, 0))
    return lo, lo + timedelta(days=1)


async def _session_entries(
    session: AsyncSession, schedule_id: uuid.UUID, lo: datetime, hi: datetime
) -> list[tuple[QueueEntry, Appointment]]:
    """The queue entries of one session: those whose appointment hangs off
    `schedule_id` and starts in `[lo, hi)`. Read fresh, never from the identity map."""
    rows = await session.execute(
        select(QueueEntry, Appointment)
        .join(Appointment, Appointment.id == QueueEntry.appointment_id)
        .where(
            Appointment.schedule_id == schedule_id,
            Appointment.slot_start >= lo,
            Appointment.slot_start < hi,
        )
        .execution_options(populate_existing=True)
    )
    return [(entry, appointment) for entry, appointment in rows.all()]


async def _window_of(session: AsyncSession, appointment: Appointment) -> slots.Window | None:
    """The session window an appointment belongs to. Only for today's appointments:
    D7 refuses to report a past day."""
    if appointment.schedule_id is None:
        return None
    start = slots.as_utc(appointment.slot_start)
    day = slots.to_local(start).date()
    windows = await booking_service.session_windows(
        session, appointment.doctor_id, appointment.clinic_id, day
    )
    return next(
        (
            w
            for w in windows
            if w.schedule_id == appointment.schedule_id and slots.in_window(w, start)
        ),
        None,
    )


async def _session_range(
    session: AsyncSession, appointment: Appointment
) -> tuple[datetime, datetime]:
    """The time range of the appointment's session, or its whole day if the
    session no longer exists."""
    window = await _window_of(session, appointment)
    if window is not None:
        return slots.window_bounds(window)
    return _day_bounds(slots.to_local(appointment.slot_start).date())


async def _peers(
    session: AsyncSession, appointment: Appointment
) -> list[tuple[QueueEntry, Appointment]]:
    """Every queue entry of the appointment's session (itself included)."""
    if appointment.schedule_id is None:
        return []
    lo, hi = await _session_range(session, appointment)
    return await _session_entries(session, appointment.schedule_id, lo, hi)


# ── The admin's and the doctor's views ───────────────────────────────────
def _line(appointment: Appointment, name: str, entry: QueueEntry | None) -> Line:
    return Line(
        appointment_id=appointment.id,
        entry_id=entry.id if entry is not None else None,
        patient_name=name,
        state=entry.state if entry is not None else "expected",
        token=entry.token_number if entry is not None else None,
        booked_at=slots.to_local(appointment.slot_start),
        walk_in=appointment.booking_channel == CHANNEL_WALK_IN,
    )


async def _views(session: AsyncSession, pairs: list[tuple[Doctor, Clinic]]) -> list[SessionView]:
    """One `SessionView` per session today of each (doctor, hospital) pair."""
    day = booking_service.current_day()
    day_lo, day_hi = _day_bounds(day)
    views: list[SessionView] = []
    for doctor, clinic in pairs:
        windows = await booking_service.session_windows(session, doctor.id, clinic.id, day)
        if not windows:
            continue
        rows = (
            await session.execute(
                select(Appointment, Patient.full_name, QueueEntry)
                .join(Patient, Patient.id == Appointment.patient_id)
                .join(QueueEntry, QueueEntry.appointment_id == Appointment.id, isouter=True)
                .where(
                    Appointment.doctor_id == doctor.id,
                    Appointment.clinic_id == clinic.id,
                    Appointment.slot_start >= day_lo,
                    Appointment.slot_start < day_hi,
                    Appointment.status.in_(ACTIVE_STATUSES),
                )
                .order_by(Appointment.slot_start)
                .execution_options(populate_existing=True)
            )
        ).all()
        for window in windows:
            lo, hi = slots.window_bounds(window)
            mine = [
                _line(appointment, name, entry)
                for appointment, name, entry in rows
                if appointment.schedule_id == window.schedule_id
                and lo <= slots.as_utc(appointment.slot_start) < hi
            ]
            views.append(
                SessionView(
                    key=SessionKey(doctor.id, clinic.id, window.schedule_id, window.start),
                    doctor_name=doctor.full_name,
                    clinic_name=clinic.name,
                    day=day,
                    start=window.start,
                    end=window.end,
                    slot_minutes=window.slot_minutes,
                    cap=window.max_tokens,
                    used=len(mine),
                    in_room=next((ln for ln in mine if ln.state == STATE_IN_ROOM), None),
                    waiting=tuple(
                        sorted((ln for ln in mine if ln.state == STATE_WAITING), key=_token)
                    ),
                    expected=tuple(
                        sorted((ln for ln in mine if ln.state == "expected"), key=_booked)
                    ),
                    seen=tuple(sorted((ln for ln in mine if ln.state == STATE_SERVED), key=_token)),
                )
            )
    views.sort(key=lambda v: (v.doctor_name, v.clinic_name, v.start))
    return views


def _token(line: Line) -> int:
    return line.token or 0


def _booked(line: Line) -> datetime:
    return line.booked_at


async def admin_sessions(session: AsyncSession, actor: Actor) -> list[SessionView]:
    """Today's sessions of the verified doctors at the admin's own clinics."""
    clinics = _staff(actor)
    pairs = [(d, c) for d, c in await booking_service.visible_pairs(session) if c.id in clinics]
    return await _views(session, pairs)


async def doctor_queue(session: AsyncSession, actor: Actor) -> list[SessionView]:
    """A verified doctor's own sessions today. Anyone else is `NotFound`."""
    if actor.role != UserRole.DOCTOR.value or not actor.is_verified_doctor:
        raise NotFound()
    pairs = [
        (d, c)
        for d, c in await booking_service.visible_pairs(session)
        if d.user_id == actor.user_id
    ]
    return await _views(session, pairs)


# ── The patient's view ───────────────────────────────────────────────────
async def _own_patient_id(session: AsyncSession, actor: Actor) -> uuid.UUID:
    if actor.role != UserRole.PATIENT.value:
        raise NotFound()
    patient_id = (
        await session.execute(select(Patient.id).where(Patient.user_id == actor.user_id))
    ).scalar_one_or_none()
    if patient_id is None:
        raise NotFound()
    return patient_id


async def _status(
    session: AsyncSession, appointment: Appointment, entry: QueueEntry | None
) -> QueueStatus | None:
    """Where one appointment stands today, or None when there is nothing to show
    (cancelled, another day, or not in the queue and not booked)."""
    if (
        appointment.status == "cancelled"
        or slots.to_local(appointment.slot_start).date() != booking_service.current_day()
    ):
        return None
    window = await _window_of(session, appointment)
    minutes = window.slot_minutes if window is not None else DEFAULT_MINUTES
    peers = await _peers(session, appointment)
    waiting = [e for e, _ in peers if e.state == STATE_WAITING]
    in_room = any(e.state == STATE_IN_ROOM for e, _ in peers)
    if entry is None:
        if appointment.status != "booked":
            return None
        return QueueStatus("expected", None, None, len(waiting), True)
    if entry.state == STATE_SERVED:
        return QueueStatus("served", None, None, len(waiting), False)
    if entry.state == STATE_IN_ROOM:
        return QueueStatus("in_room", None, None, len(waiting), True)
    place = order.position([e.token_number for e in waiting], entry.token_number)
    wait = order.wait_minutes(place - 1, in_room, minutes)
    return QueueStatus("next" if place == 1 else "waiting", place, wait, len(waiting), True)


async def patient_status(
    session: AsyncSession, actor: Actor, appointment_id: uuid.UUID
) -> QueueStatus | None:
    """The caller's own place for one of their appointments. Someone else's, or
    an unknown id, is `NotFound`."""
    patient_id = await _own_patient_id(session, actor)
    row = (
        await session.execute(
            select(Appointment, QueueEntry)
            .join(QueueEntry, QueueEntry.appointment_id == Appointment.id, isouter=True)
            .where(Appointment.id == appointment_id, Appointment.patient_id == patient_id)
        )
    ).first()
    if row is None:
        raise NotFound()
    return await _status(session, row[0], row[1])


async def patient_badges(session: AsyncSession, actor: Actor) -> dict[uuid.UUID, QueueStatus]:
    """The status of each of the caller's appointments today that has one."""
    patient_id = await _own_patient_id(session, actor)
    day_lo, day_hi = _day_bounds(booking_service.current_day())
    rows = (
        await session.execute(
            select(Appointment, QueueEntry)
            .join(QueueEntry, QueueEntry.appointment_id == Appointment.id, isouter=True)
            .where(
                Appointment.patient_id == patient_id,
                Appointment.slot_start >= day_lo,
                Appointment.slot_start < day_hi,
                Appointment.status.in_(ACTIVE_STATUSES),
            )
        )
    ).all()
    badges: dict[uuid.UUID, QueueStatus] = {}
    for appointment, entry in rows:
        status = await _status(session, appointment, entry)
        if status is not None:
            badges[appointment.id] = status
    return badges
