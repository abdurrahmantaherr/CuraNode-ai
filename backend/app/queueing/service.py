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

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import writer as audit
from ..booking import service as booking_service
from ..booking import slots
from ..booking.schemas import ACTIVE_STATUSES, DEFAULT_MINUTES
from ..db.models import Appointment, Clinic, Doctor, Patient, QueueEntry, UserRole
from ..deps import Actor
from ..errors import NotFound, QueueRefused
from ..schedule import service as schedule_service
from . import order
from .display import mask_passport
from .schemas import (
    CHANNEL_WALK_IN,
    SEARCH_LIMIT,
    SEARCH_MIN,
    STATE_IN_ROOM,
    STATE_SERVED,
    STATE_WAITING,
    Line,
    PatientHit,
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


# ── Actions (the clinic admin only) ──────────────────────────────────────
def _new_entry(appointment_id: uuid.UUID, token: int) -> QueueEntry:
    """A waiting entry. If the schema owner's answer showed another NOT NULL column
    with no database default (Task 1, Step 1), set it here from the appointment."""
    return QueueEntry(appointment_id=appointment_id, token_number=token, state=STATE_WAITING)


async def _audit(
    session: AsyncSession,
    actor: Actor,
    action: str,
    appointment: Appointment,
    *,
    entry: QueueEntry | None = None,
    extra: dict[str, str] | None = None,
    user_agent: str | None = None,
) -> None:
    """One audit row, ids only: never a name, a passport number or search text."""
    detail = {
        "appointment_id": str(appointment.id),
        "doctor_id": str(appointment.doctor_id),
        "clinic_id": str(appointment.clinic_id),
    }
    if entry is not None:
        detail["entry_id"] = str(entry.id)
    if extra:
        detail.update(extra)
    await audit.write(
        session,
        action=action,
        actor_user_id=actor.user_id,
        actor_role=actor.role,
        subject_patient_id=appointment.patient_id,
        resource_type="queue_entry",
        ip_address=actor.ip_address,
        user_agent=user_agent,
        detail=detail,
    )


async def _locked_entry(
    session: AsyncSession, actor: Actor, entry_id: uuid.UUID
) -> tuple[QueueEntry, Appointment]:
    """The admin's own entry, found, then the doctor locked, then re-read. Only
    today's entries can be acted on."""
    clinics = _staff(actor)
    row = (
        await session.execute(
            select(QueueEntry, Appointment)
            .join(Appointment, Appointment.id == QueueEntry.appointment_id)
            .where(QueueEntry.id == entry_id, Appointment.clinic_id.in_(clinics))
        )
    ).first()
    if row is None:
        raise NotFound()
    entry, appointment = row
    await schedule_service.lock_doctor(session, appointment.doctor_id)
    await session.refresh(entry)
    await session.refresh(appointment)
    if slots.to_local(appointment.slot_start).date() != booking_service.current_day():
        raise QueueRefused("not_today")
    return entry, appointment


async def check_in(
    session: AsyncSession,
    actor: Actor,
    appointment_id: uuid.UUID,
    *,
    user_agent: str | None = None,
) -> None:
    """Check a booked patient in: they join their session's queue at the back.
    Checking in twice does nothing."""
    clinics = _staff(actor)
    appointment = (
        await session.execute(
            select(Appointment).where(
                Appointment.id == appointment_id, Appointment.clinic_id.in_(clinics)
            )
        )
    ).scalar_one_or_none()
    if appointment is None:
        raise NotFound()
    await schedule_service.lock_doctor(session, appointment.doctor_id)
    await session.refresh(appointment)
    existing = (
        await session.execute(select(QueueEntry).where(QueueEntry.appointment_id == appointment.id))
    ).scalar_one_or_none()
    if existing is not None:
        return
    if appointment.status != "booked":
        raise QueueRefused("not_booked")
    if slots.to_local(appointment.slot_start).date() != booking_service.current_day():
        raise QueueRefused("not_today")
    if not await booking_service.is_bookable(session, appointment.doctor_id, appointment.clinic_id):
        raise QueueRefused("doctor_unavailable")
    window = await _window_of(session, appointment)
    if window is None or booking_service.now() >= slots.window_bounds(window)[1]:
        raise QueueRefused("session_over")
    lo, hi = slots.window_bounds(window)
    entries = await _session_entries(session, window.schedule_id, lo, hi)
    entry = _new_entry(appointment.id, order.next_token(e.token_number for e, _ in entries))
    session.add(entry)
    appointment.status = "checked_in"
    await session.flush()
    await _audit(
        session, actor, audit.QUEUE_CHECK_IN, appointment, entry=entry, user_agent=user_agent
    )
    await session.commit()


async def send_in(
    session: AsyncSession, actor: Actor, entry_id: uuid.UUID, *, user_agent: str | None = None
) -> None:
    """Send a waiting patient into the room. Refused while someone is already there."""
    entry, appointment = await _locked_entry(session, actor, entry_id)
    if entry.state != STATE_WAITING:
        raise QueueRefused("not_waiting")
    peers = await _peers(session, appointment)
    if any(other.state == STATE_IN_ROOM for other, _ in peers):
        raise QueueRefused("room_occupied")
    entry.state = STATE_IN_ROOM
    await _audit(
        session, actor, audit.QUEUE_SEND_IN, appointment, entry=entry, user_agent=user_agent
    )
    await session.commit()


async def mark_seen(
    session: AsyncSession, actor: Actor, entry_id: uuid.UUID, *, user_agent: str | None = None
) -> None:
    """The patient in the room has been seen: the visit is finished (final)."""
    entry, appointment = await _locked_entry(session, actor, entry_id)
    if entry.state != STATE_IN_ROOM:
        raise QueueRefused("not_in_room")
    entry.state = STATE_SERVED
    appointment.status = "done"
    await _audit(session, actor, audit.QUEUE_SEEN, appointment, entry=entry, user_agent=user_agent)
    await session.commit()


async def put_back(
    session: AsyncSession, actor: Actor, entry_id: uuid.UUID, *, user_agent: str | None = None
) -> None:
    """Undo a mistaken "send in": back to waiting, keeping their token (their place)."""
    entry, appointment = await _locked_entry(session, actor, entry_id)
    if entry.state != STATE_IN_ROOM:
        raise QueueRefused("not_in_room")
    entry.state = STATE_WAITING
    await _audit(
        session, actor, audit.QUEUE_PUT_BACK, appointment, entry=entry, user_agent=user_agent
    )
    await session.commit()


async def _move(
    session: AsyncSession, actor: Actor, entry_id: uuid.UUID, how: str, user_agent: str | None
) -> None:
    entry, appointment = await _locked_entry(session, actor, entry_id)
    if entry.state != STATE_WAITING:
        raise QueueRefused("not_waiting")
    peers = await _peers(session, appointment)
    waiting = sorted(
        (e for e, _ in peers if e.state == STATE_WAITING),
        key=lambda e: (e.token_number, str(e.id)),
    )
    ids = [e.id for e in waiting]
    new_ids = order.moved_up(ids, entry.id) if how == "up" else order.made_next(ids, entry.id)
    if new_ids == ids:
        return
    new_tokens = order.reassign([e.token_number for e in waiting], new_ids)
    for other in waiting:
        other.token_number = new_tokens[other.id]
    await _audit(
        session,
        actor,
        audit.QUEUE_MOVE,
        appointment,
        entry=entry,
        extra={"how": how},
        user_agent=user_agent,
    )
    await session.commit()


async def move_up(
    session: AsyncSession, actor: Actor, entry_id: uuid.UUID, *, user_agent: str | None = None
) -> None:
    """Swap a waiting patient with the one directly ahead."""
    await _move(session, actor, entry_id, "up", user_agent)


async def make_next(
    session: AsyncSession, actor: Actor, entry_id: uuid.UUID, *, user_agent: str | None = None
) -> None:
    """Put a waiting patient first among the waiting."""
    await _move(session, actor, entry_id, "next", user_agent)


# ── Walk-ins ─────────────────────────────────────────────────────────────
async def find_patients(
    session: AsyncSession, actor: Actor, text: str, *, user_agent: str | None = None
) -> list[PatientHit]:
    """Find a registered patient at the front desk by name (part of it, any case) or
    by an exact full passport number. Results show the passport number masked.
    The audit row records only how many were found, never what was typed."""
    _staff(actor)
    needle = text.strip()[:80]
    if len(needle) < SEARCH_MIN:
        raise QueueRefused("search_too_short")
    patients = (
        (
            await session.execute(
                select(Patient)
                .where(
                    or_(
                        func.upper(Patient.passport_no) == needle.upper(),
                        Patient.full_name.icontains(needle, autoescape=True),
                    )
                )
                .order_by(Patient.full_name, Patient.id)
                .limit(SEARCH_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    await audit.write(
        session,
        action=audit.QUEUE_FIND_PATIENT,
        actor_user_id=actor.user_id,
        actor_role=actor.role,
        resource_type="patient_search",
        ip_address=actor.ip_address,
        user_agent=user_agent,
        detail={"results": len(patients)},
    )
    await session.commit()
    return [PatientHit(p.id, p.full_name, mask_passport(p.passport_no)) for p in patients]


async def add_walk_in(
    session: AsyncSession,
    actor: Actor,
    key: SessionKey,
    patient_id: uuid.UUID,
    *,
    user_agent: str | None = None,
) -> None:
    """Add a registered patient with no booking to a session's queue, at the back.
    They count toward the session cap but never block a bookable time."""
    clinics = _staff(actor)
    if key.clinic_id not in clinics:
        raise NotFound()
    await schedule_service.lock_doctor(session, key.doctor_id)
    if not await booking_service.is_bookable(session, key.doctor_id, key.clinic_id):
        raise QueueRefused("doctor_unavailable")
    today = booking_service.current_day()
    windows = await booking_service.session_windows(session, key.doctor_id, key.clinic_id, today)
    window = next(
        (w for w in windows if w.schedule_id == key.schedule_id and w.start == key.start), None
    )
    if window is None:
        raise NotFound()
    lo, hi = slots.window_bounds(window)
    now = booking_service.now()
    if now >= hi:
        raise QueueRefused("session_over")
    patient = await session.get(Patient, patient_id)
    if patient is None:
        raise NotFound()

    day_lo, day_hi = _day_bounds(today)
    holds = (
        await session.execute(
            select(func.count())
            .select_from(Appointment)
            .where(
                Appointment.patient_id == patient.id,
                Appointment.doctor_id == key.doctor_id,
                Appointment.status.in_(("booked", "checked_in")),
                Appointment.slot_start >= day_lo,
                Appointment.slot_start < day_hi,
            )
        )
    ).scalar_one()
    if holds:
        raise QueueRefused("has_booking_today")
    queued = (
        await session.execute(
            select(func.count())
            .select_from(QueueEntry)
            .join(Appointment, Appointment.id == QueueEntry.appointment_id)
            .where(
                Appointment.patient_id == patient.id,
                QueueEntry.state.in_((STATE_WAITING, STATE_IN_ROOM)),
            )
        )
    ).scalar_one()
    if queued:
        raise QueueRefused("already_queued")

    taken = await booking_service.active_starts(session, window.schedule_id, lo, hi)
    if len(taken) >= window.max_tokens:
        raise QueueRefused("session_full")
    start = max(now, lo + timedelta(microseconds=1))
    held = set(taken)
    while start in held:
        start += timedelta(microseconds=1)
    appointment = Appointment(
        patient_id=patient.id,
        doctor_id=key.doctor_id,
        clinic_id=key.clinic_id,
        schedule_id=window.schedule_id,
        slot_start=start,
        status="checked_in",
        booking_channel=CHANNEL_WALK_IN,
    )
    session.add(appointment)
    try:
        await session.flush()
    except IntegrityError:  # the index caught a clash the checks did not see
        await session.rollback()
        raise QueueRefused("try_again") from None
    entries = await _session_entries(session, window.schedule_id, lo, hi)
    entry = _new_entry(appointment.id, order.next_token(e.token_number for e, _ in entries))
    session.add(entry)
    await session.flush()
    await _audit(
        session, actor, audit.QUEUE_WALK_IN, appointment, entry=entry, user_agent=user_agent
    )
    await session.commit()
