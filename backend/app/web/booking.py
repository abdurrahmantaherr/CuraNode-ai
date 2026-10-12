"""Server-rendered pages for appointment booking (P5, FR15, FR16).

Every rule lives in `booking/service.py`; this module only turns forms into
service calls and results into pages, mirroring `web/documents.py`. Every action
works without JavaScript: a GET shows a page, a POST acts and redirects (PRG),
and a refused action shows the same page again with fresh times and the reason.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..booking import display, service, slots
from ..booking.schemas import AppointmentRow, ClinicRef, DayOptions
from ..db.models import UserRole
from ..deps import Actor, OptionalActorDep, SessionDep, check_booking_write_rate
from ..errors import BookingRefused, NotFound, RateLimited
from ..i18n.catalogue import normalise_locale, translate
from .router import _area, _doctor_nav, _patient_nav, _render, login_redirect

router = APIRouter(tags=["web-booking"])

QUERY_MAX = 80  # the longest search text honoured
# `?notice=` values the find page understands (anything else is ignored).
_FIND_NOTICES = {"unavailable": "booking.notice.unavailable"}


# ── Guard and shared context ─────────────────────────────────────────────
def _guard(request: Request, locale: str, actor: Actor | None) -> Response | None:
    """The patient pages' redirects (as `web/documents.py`): signed out goes to
    sign-in and back here, another role goes to its own area, and a profile that
    is not finished goes to onboarding."""
    loc = normalise_locale(locale)
    if actor is None:
        return login_redirect(loc, request)
    if actor.role != UserRole.PATIENT.value:
        return RedirectResponse(f"/{loc}/{_area(actor.role)}", status_code=303)
    if not actor.onboarding_complete:
        return RedirectResponse(f"/{loc}/onboarding", status_code=303)
    return None


def _ctx(loc: str, title_key: str, *, current: str) -> dict[str, Any]:
    return {
        "crumb": translate("auth.role.patient", loc),
        "page_title": translate(title_key, loc),
        "nav_items": _patient_nav(loc, current=current),
        "weekday": lambda day: translate(f"weekday.{day.weekday()}", loc),
        "hhmm": display.hhmm,
        "slot_value": slots.format_local,
        "status_label": lambda status: display.status_label(status, loc),
        "status_badge": display.status_badge,
    }


def _parse_id(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


# ── Find a doctor ────────────────────────────────────────────────────────
def _find_url(loc: str, q: str, specialty: str) -> str:
    query = urlencode({k: v for k, v in (("q", q), ("specialty", specialty)) if v})
    return f"/{loc}/patient/doctors" + (f"?{query}" if query else "")


async def _render_find(
    request: Request,
    loc: str,
    session: SessionDep,
    *,
    status_code: int = 200,
    q: str = "",
    specialty: str = "",
    page_error: str | None = None,
    notice: str | None = None,
) -> Response:
    specialties = await service.specialty_options(session)
    chosen = specialty if specialty in specialties else ""
    cards = await service.search_doctors(session, query=q, specialty=chosen)
    return _render(
        request,
        "booking/find.html",
        loc,
        status_code=status_code,
        path_suffix="/patient/doctors",
        cards=cards,
        specialties=specialties,
        q=q,
        specialty=chosen,
        filters_active=bool(q or chosen),
        page_error=page_error,
        notice=notice,
        specialty_href=lambda value: _find_url(loc, q, value),
        doctor_href=lambda card: f"/{loc}/patient/doctors/{card.id}",
        **_ctx(loc, "booking.find.title", current="doctors"),
    )


async def _find_not_found(request: Request, loc: str, session: SessionDep) -> Response:
    return await _render_find(
        request, loc, session, status_code=404, page_error=translate("errors.not_found", loc)
    )


@router.get("/{locale}/patient/doctors", response_class=HTMLResponse)
async def find_page(
    request: Request,
    locale: str,
    session: SessionDep,
    actor: OptionalActorDep,
    q: str = "",
    specialty: str = "",
    notice: str = "",
) -> Response:
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    loc = normalise_locale(locale)
    key = _FIND_NOTICES.get(notice)
    return await _render_find(
        request,
        loc,
        session,
        q=q.strip()[:QUERY_MAX],
        specialty=specialty,
        notice=translate(key, loc) if key else None,
    )


# ── A doctor's free times ────────────────────────────────────────────────
async def _moving_row(
    session: SessionDep, actor: Actor, doctor_id: uuid.UUID, raw: str
) -> AppointmentRow | None:
    """The appointment being moved, only if `raw` names one of the caller's own
    that can still change and is with this doctor. Anything else is ignored, so
    the page never confirms that someone else's id exists."""
    parsed = _parse_id(raw) if raw else None
    if parsed is None:
        return None
    try:
        row = await service.appointment_for_patient(session, actor, parsed)
    except NotFound:
        return None
    return row if row.can_change and row.doctor_id == doctor_id else None


def _without_own_time(days: list[DayOptions], moving: AppointmentRow | None) -> list[DayOptions]:
    """In move mode the booking's own time is not a new time: leave it out of the offer."""
    if moving is None:
        return days
    own = slots.as_utc(moving.starts_at)
    kept = [DayOptions(day=d.day, times=tuple(t for t in d.times if t != own)) for d in days]
    return [d for d in kept if d.times]


def _confirm_url(
    loc: str,
    doctor_id: uuid.UUID,
    clinic_id: uuid.UUID,
    start: datetime,
    moving: AppointmentRow | None,
) -> str:
    params = {"clinic": str(clinic_id), "at": slots.format_local(start)}
    if moving is not None:
        params["moving"] = str(moving.id)
    return f"/{loc}/patient/doctors/{doctor_id}/confirm?{urlencode(params, safe=':')}"


async def _render_doctor(
    request: Request,
    loc: str,
    session: SessionDep,
    actor: Actor,
    doctor_id: uuid.UUID,
    *,
    clinic_raw: str = "",
    moving_raw: str = "",
    status_code: int = 200,
    page_error: str | None = None,
) -> Response:
    try:
        doctor = await service.get_doctor(session, doctor_id)
    except NotFound:
        return await _find_not_found(request, loc, session)
    clinic = next((c for c in doctor.clinics if str(c.id) == clinic_raw), doctor.clinics[0])
    moving = await _moving_row(session, actor, doctor_id, moving_raw)
    days = _without_own_time(
        await service.list_options(
            session, doctor_id, clinic.id, exclude=moving.id if moving is not None else None
        ),
        moving,
    )

    def clinic_href(other: ClinicRef) -> str:
        params = {"clinic": str(other.id)}
        if moving is not None:
            params["moving"] = str(moving.id)
        return f"/{loc}/patient/doctors/{doctor_id}?{urlencode(params)}"

    return _render(
        request,
        "booking/doctor.html",
        loc,
        status_code=status_code,
        path_suffix=f"/patient/doctors/{doctor_id}",
        doctor=doctor,
        clinic=clinic,
        days=days,
        moving=moving,
        moving_text=(
            translate(
                "booking.doctor.moving",
                loc,
                date=moving.starts_at.date().isoformat(),
                time=display.hhmm(moving.starts_at),
            )
            if moving is not None
            else ""
        ),
        page_error=page_error,
        clinic_href=clinic_href,
        time_href=lambda start: _confirm_url(loc, doctor_id, clinic.id, start, moving),
        **_ctx(loc, "booking.doctor.title", current="doctors"),
    )


@router.get("/{locale}/patient/doctors/{doctor_id}", response_class=HTMLResponse)
async def doctor_page(
    request: Request,
    locale: str,
    doctor_id: str,
    session: SessionDep,
    actor: OptionalActorDep,
    clinic: str = "",
    moving: str = "",
) -> Response:
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    parsed = _parse_id(doctor_id)
    if parsed is None:
        return await _find_not_found(request, loc, session)
    return await _render_doctor(
        request, loc, session, actor, parsed, clinic_raw=clinic, moving_raw=moving
    )


# ── Confirm ──────────────────────────────────────────────────────────────
def _parse_slot(value: str) -> datetime | None:
    """`YYYY-MM-DDTHH:MM` (Pakistan time) as an aware UTC instant, else None."""
    try:
        return slots.parse_local(value)
    except ValueError:
        return None


@router.get("/{locale}/patient/doctors/{doctor_id}/confirm", response_class=HTMLResponse)
async def confirm_page(
    request: Request,
    locale: str,
    doctor_id: str,
    session: SessionDep,
    actor: OptionalActorDep,
    clinic: str = "",
    at: str = "",
    moving: str = "",
) -> Response:
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    parsed = _parse_id(doctor_id)
    if parsed is None:
        return await _find_not_found(request, loc, session)
    doctor_url = f"/{loc}/patient/doctors/{parsed}"
    clinic_id, start = _parse_id(clinic), _parse_slot(at)
    if clinic_id is None or start is None:
        return RedirectResponse(doctor_url, status_code=303)
    try:
        doctor = await service.get_doctor(session, parsed)
    except NotFound:
        return await _find_not_found(request, loc, session)
    chosen = next((c for c in doctor.clinics if c.id == clinic_id), None)
    if chosen is None:
        return RedirectResponse(doctor_url, status_code=303)
    moving_row = await _moving_row(session, actor, parsed, moving)
    try:
        options = _without_own_time(
            await service.list_options(
                session,
                parsed,
                clinic_id,
                exclude=moving_row.id if moving_row is not None else None,
            ),
            moving_row,
        )
    except NotFound:  # the doctor changed between the two reads
        return await _find_not_found(request, loc, session)
    if start not in {moment for day in options for moment in day.times}:
        return await _render_doctor(
            request,
            loc,
            session,
            actor,
            parsed,
            clinic_raw=clinic,
            moving_raw=moving,
            status_code=409,
            page_error=translate("errors.booking_not_offered", loc),
        )
    back = {"clinic": str(clinic_id)}
    if moving_row is not None:
        back["moving"] = str(moving_row.id)
    local = slots.to_local(start)
    return _render(
        request,
        "booking/confirm.html",
        loc,
        path_suffix=f"/patient/doctors/{parsed}",
        doctor=doctor,
        clinic=chosen,
        day=local.date(),
        start=start,
        moving=moving_row,
        replaces_text=(
            translate(
                "booking.confirm.replaces",
                loc,
                date=moving_row.starts_at.date().isoformat(),
                time=display.hhmm(moving_row.starts_at),
            )
            if moving_row is not None
            else ""
        ),
        action=(
            f"/{loc}/patient/appointments/{moving_row.id}/reschedule"
            if moving_row is not None
            else f"/{loc}/patient/appointments"
        ),
        back_href=f"{doctor_url}?{urlencode(back)}",
        **_ctx(
            loc,
            "booking.confirm.title_move" if moving_row is not None else "booking.confirm.title",
            current="doctors",
        ),
    )


# ── Book ─────────────────────────────────────────────────────────────────
@router.post("/{locale}/patient/appointments")
async def book_submit(
    request: Request,
    locale: str,
    session: SessionDep,
    actor: OptionalActorDep,
    doctor_id: Annotated[str, Form()] = "",
    clinic_id: Annotated[str, Form()] = "",
    slot: Annotated[str, Form()] = "",
) -> Response:
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    doctor = _parse_id(doctor_id)
    if doctor is None:
        return await _find_not_found(request, loc, session)
    clinic, start = _parse_id(clinic_id), _parse_slot(slot)
    if clinic is None or start is None:
        return RedirectResponse(f"/{loc}/patient/doctors/{doctor}", status_code=303)

    async def again(code: int, message: str) -> Response:
        return await _render_doctor(
            request,
            loc,
            session,
            actor,
            doctor,
            clinic_raw=clinic_id,
            status_code=code,
            page_error=message,
        )

    try:
        await check_booking_write_rate(actor)
    except RateLimited:
        return await again(429, translate("errors.rate_limited", loc))
    try:
        appointment = await service.book(
            session, actor, doctor, clinic, start, user_agent=request.headers.get("user-agent")
        )
    except BookingRefused as exc:
        await session.rollback()  # let go of the doctor lock before drawing the page
        return await again(exc.http_status, translate(exc.message_key, loc))
    except NotFound:
        await session.rollback()
        return RedirectResponse(f"/{loc}/patient/doctors?notice=unavailable", status_code=303)
    return RedirectResponse(
        f"/{loc}/patient/appointments/{appointment.id}?saved=booked", status_code=303
    )


# ── Move a booking ───────────────────────────────────────────────────────
@router.post("/{locale}/patient/appointments/{appointment_id}/reschedule")
async def reschedule_submit(
    request: Request,
    locale: str,
    appointment_id: str,
    session: SessionDep,
    actor: OptionalActorDep,
    clinic_id: Annotated[str, Form()] = "",
    slot: Annotated[str, Form()] = "",
) -> Response:
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    list_url = f"/{loc}/patient/appointments"
    parsed = _parse_id(appointment_id)
    if parsed is None:
        return RedirectResponse(list_url, status_code=303)
    try:
        row = await service.appointment_for_patient(session, actor, parsed)
    except NotFound:  # not theirs and not there look the same
        return RedirectResponse(list_url, status_code=303)
    clinic, start = _parse_id(clinic_id), _parse_slot(slot)
    if clinic is None or start is None:
        return RedirectResponse(
            f"/{loc}/patient/doctors/{row.doctor_id}?moving={row.id}", status_code=303
        )

    async def again(code: int, message: str) -> Response:
        return await _render_doctor(
            request,
            loc,
            session,
            actor,
            row.doctor_id,
            clinic_raw=clinic_id,
            moving_raw=str(row.id),
            status_code=code,
            page_error=message,
        )

    try:
        await check_booking_write_rate(actor)
    except RateLimited:
        return await again(429, translate("errors.rate_limited", loc))
    try:
        moved = await service.reschedule(
            session,
            actor,
            parsed,
            clinic,
            start,
            user_agent=request.headers.get("user-agent"),
        )
    except BookingRefused as exc:
        await session.rollback()
        return await again(exc.http_status, translate(exc.message_key, loc))
    except NotFound:
        await session.rollback()
        return RedirectResponse(list_url, status_code=303)
    return RedirectResponse(f"{list_url}/{moved.id}?saved=rescheduled", status_code=303)


# ── My appointments ──────────────────────────────────────────────────────
# `?saved=` values the list and the confirmation understand (anything else is ignored).
_SAVED = {
    "booked": "booking.saved.booked",
    "rescheduled": "booking.saved.rescheduled",
    "cancelled": "booking.saved.cancelled",
}


async def _render_list(
    request: Request,
    loc: str,
    session: SessionDep,
    actor: Actor,
    *,
    status_code: int = 200,
    saved: str = "",
    page_error: str | None = None,
) -> Response:
    upcoming, past = await service.my_appointments(session, actor)
    key = _SAVED.get(saved)
    return _render(
        request,
        "booking/list.html",
        loc,
        status_code=status_code,
        path_suffix="/patient/appointments",
        upcoming=upcoming,
        past=past,
        saved=translate(key, loc) if key else None,
        page_error=page_error,
        **_ctx(loc, "booking.list.title", current="appointments"),
    )


@router.get("/{locale}/patient/appointments", response_class=HTMLResponse)
async def list_page(
    request: Request,
    locale: str,
    session: SessionDep,
    actor: OptionalActorDep,
    saved: str = "",
) -> Response:
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    return await _render_list(request, normalise_locale(locale), session, actor, saved=saved)


@router.get("/{locale}/patient/appointments/{appointment_id}", response_class=HTMLResponse)
async def appointment_page(
    request: Request,
    locale: str,
    appointment_id: str,
    session: SessionDep,
    actor: OptionalActorDep,
    saved: str = "",
) -> Response:
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    list_url = f"/{loc}/patient/appointments"
    parsed = _parse_id(appointment_id)
    if parsed is None:
        return RedirectResponse(list_url, status_code=303)
    try:
        row = await service.appointment_for_patient(session, actor, parsed)
    except NotFound:  # not theirs and not there look the same
        return RedirectResponse(list_url, status_code=303)
    key = _SAVED.get(saved)
    return _render(
        request,
        "booking/appointment.html",
        loc,
        path_suffix=f"/patient/appointments/{row.id}",
        row=row,
        saved=translate(key, loc) if key else None,
        **_ctx(loc, "booking.appointment.title", current="appointments"),
    )


@router.post("/{locale}/patient/appointments/{appointment_id}/cancel")
async def cancel_submit(
    request: Request,
    locale: str,
    appointment_id: str,
    session: SessionDep,
    actor: OptionalActorDep,
) -> Response:
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    list_url = f"/{loc}/patient/appointments"
    parsed = _parse_id(appointment_id)
    if parsed is None:
        return RedirectResponse(list_url, status_code=303)
    try:
        await check_booking_write_rate(actor)
    except RateLimited:
        return await _render_list(
            request,
            loc,
            session,
            actor,
            status_code=429,
            page_error=translate("errors.rate_limited", loc),
        )
    try:
        await service.cancel(session, actor, parsed, user_agent=request.headers.get("user-agent"))
    except BookingRefused as exc:
        await session.rollback()
        return await _render_list(
            request,
            loc,
            session,
            actor,
            status_code=exc.http_status,
            page_error=translate(exc.message_key, loc),
        )
    except NotFound:
        await session.rollback()
        return RedirectResponse(list_url, status_code=303)
    return RedirectResponse(f"{list_url}?saved=cancelled", status_code=303)


@router.get("/{locale}/patient/appointments/{appointment_id}/reschedule")
async def reschedule_page(
    request: Request,
    locale: str,
    appointment_id: str,
    session: SessionDep,
    actor: OptionalActorDep,
) -> Response:
    """The way into "move this booking": the doctor page, in move mode."""
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    list_url = f"/{loc}/patient/appointments"
    parsed = _parse_id(appointment_id)
    if parsed is None:
        return RedirectResponse(list_url, status_code=303)
    try:
        row = await service.appointment_for_patient(session, actor, parsed)
    except NotFound:
        return RedirectResponse(list_url, status_code=303)
    if not row.can_change:
        return await _render_list(
            request,
            loc,
            session,
            actor,
            status_code=409,
            page_error=translate("errors.booking_not_cancellable", loc),
        )
    params = urlencode({"clinic": str(row.clinic_id), "moving": str(row.id)})
    return RedirectResponse(f"/{loc}/patient/doctors/{row.doctor_id}?{params}", status_code=303)


# ── The doctor's day ─────────────────────────────────────────────────────
_DAY_SPAN = 366  # a date further than this from today shows today instead


def _doctor_guard(request: Request, locale: str, actor: Actor | None) -> Response | None:
    """Signed out goes to sign-in and back here, another role goes to its own area,
    and a doctor who is not verified goes to their dashboard (where the banner
    explains why): booking shows a doctor's patients, so it is for verified doctors."""
    loc = normalise_locale(locale)
    if actor is None:
        return login_redirect(loc, request)
    if actor.role != UserRole.DOCTOR.value:
        return RedirectResponse(f"/{loc}/{_area(actor.role)}", status_code=303)
    if not actor.onboarding_complete:
        return RedirectResponse(f"/{loc}/onboarding", status_code=303)
    if not actor.is_verified_doctor:
        return RedirectResponse(f"/{loc}/doctor", status_code=303)
    return None


def _pick_day(raw: str, today: date) -> date:
    """The day asked for; today when it is missing, malformed or more than a year away."""
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        return today
    return parsed if abs((parsed - today).days) <= _DAY_SPAN else today


@router.get("/{locale}/doctor/appointments", response_class=HTMLResponse)
async def doctor_day_page(
    request: Request,
    locale: str,
    session: SessionDep,
    actor: OptionalActorDep,
    day_raw: Annotated[str, Query(alias="date")] = "",
) -> Response:
    if (redirect := _doctor_guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    day = _pick_day(day_raw, service.current_day())
    try:
        found = await service.doctor_day(session, actor, day)
    except NotFound:
        return RedirectResponse(f"/{loc}/doctor", status_code=303)
    base = f"/{loc}/doctor/appointments"
    return _render(
        request,
        "booking/doctor_day.html",
        loc,
        path_suffix=f"/doctor/appointments?date={day.isoformat()}",
        crumb=translate("auth.role.doctor", loc),
        page_title=translate("booking.doctor_day.title", loc),
        nav_items=_doctor_nav(loc, current="appointments"),
        day=day,
        weekday_name=translate(f"weekday.{day.weekday()}", loc),
        rows=display.build_day_rows(found, loc),
        prev_href=f"{base}?date={(day - timedelta(days=1)).isoformat()}",
        next_href=f"{base}?date={(day + timedelta(days=1)).isoformat()}",
        today_href=base,
    )
