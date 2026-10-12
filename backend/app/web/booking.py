"""Server-rendered pages for appointment booking (P5, FR15, FR16).

Every rule lives in `booking/service.py`; this module only turns forms into
service calls and results into pages, mirroring `web/documents.py`. Every action
works without JavaScript: a GET shows a page, a POST acts and redirects (PRG),
and a refused action shows the same page again with fresh times and the reason.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..booking import display, service, slots
from ..booking.schemas import AppointmentRow, ClinicRef
from ..db.models import UserRole
from ..deps import Actor, OptionalActorDep, SessionDep
from ..errors import NotFound
from ..i18n.catalogue import normalise_locale, translate
from .router import _area, _patient_nav, _render, login_redirect

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
    days = await service.list_options(session, doctor_id, clinic.id)

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
