"""Server-rendered pages for the live queue (P6, FR19): the clinic admin's desk.

Every rule lives in `queueing/service.py`; this module only turns forms into
service calls and results into pages, mirroring `web/booking.py`. Every action
works without JavaScript: a POST acts and redirects, and a refused action shows
the same page again with the reason. The patient's and the doctor's views of the
queue are small additions to the booking pages (`web/booking.py`, `web/router.py`).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..deps import (
    Actor,
    OptionalActorDep,
    SessionDep,
    check_queue_lookup_rate,
    check_queue_write_rate,
)
from ..errors import NotFound, QueueRefused, RateLimited
from ..i18n.catalogue import normalise_locale, translate
from ..queueing import service
from ..queueing.schemas import PatientHit, SessionKey
from .router import _admin_nav, _admin_page_guard, _render

router = APIRouter(tags=["web-queue"])

# `?saved=` values the queue page understands (anything else is ignored).
_SAVED = {
    "checked_in": "queue.saved.checked_in",
    "sent_in": "queue.saved.sent_in",
    "seen": "queue.saved.seen",
    "put_back": "queue.saved.put_back",
    "moved": "queue.saved.moved",
    "walk_in": "queue.saved.walk_in",
}


def _guard(request: Request, locale: str, actor: Actor | None) -> Response | None:
    """The admin pages' redirects, plus: an admin with no clinic yet goes to their dashboard."""
    if (redirect := _admin_page_guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    if not actor.clinic_ids:
        return RedirectResponse(f"/{normalise_locale(locale)}/admin", status_code=303)
    return None


def _parse_id(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _agent(request: Request) -> str | None:
    return request.headers.get("user-agent")


async def _render_queue(
    request: Request,
    loc: str,
    session: SessionDep,
    actor: Actor,
    *,
    status_code: int = 200,
    saved: str = "",
    page_error: str | None = None,
    find: str = "",
    find_session: str = "",
) -> Response:
    views = await service.admin_sessions(session, actor)
    hits: list[PatientHit] = []
    find_error: str | None = None
    needle = find.strip()
    if needle and SessionKey.decode(find_session) is not None:
        try:
            await check_queue_lookup_rate(actor)
            hits = await service.find_patients(session, actor, needle, user_agent=_agent(request))
        except RateLimited:
            find_error = translate("errors.rate_limited", loc)
        except QueueRefused as exc:
            find_error = translate(exc.message_key, loc)
    key = _SAVED.get(saved)
    return _render(
        request,
        "queue/admin.html",
        loc,
        status_code=status_code,
        path_suffix="/admin/queue",
        crumb=translate("admin.dashboard.title", loc),
        page_title=translate("queue.title", loc),
        nav_items=_admin_nav(loc, current="queue"),
        views=views,
        hits=hits,
        find=needle,
        find_session=find_session,
        find_error=find_error,
        saved=translate(key, loc) if key else None,
        page_error=page_error,
    )


@router.get("/{locale}/admin/queue", response_class=HTMLResponse)
async def queue_page(
    request: Request, locale: str, session: SessionDep, actor: OptionalActorDep, saved: str = ""
) -> Response:
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    return await _render_queue(request, normalise_locale(locale), session, actor, saved=saved)


@router.post("/{locale}/admin/queue/find", response_class=HTMLResponse)
async def find_patient(
    request: Request,
    locale: str,
    session: SessionDep,
    actor: OptionalActorDep,
    find: Annotated[str, Form()] = "",
    session_key: Annotated[str, Form(alias="session")] = "",
) -> Response:
    """The walk-in search is a POST so what the desk types (a name, a passport number)
    never lands in a URL, a browser history or an access log. It draws the page directly:
    a search changes nothing, so there is nothing to redirect to."""
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    return await _render_queue(
        request,
        normalise_locale(locale),
        session,
        actor,
        find=find[:80],
        find_session=session_key,
    )


async def _run(
    request: Request,
    locale: str,
    session: SessionDep,
    actor: Actor,
    call: Callable[[], Awaitable[None]],
    saved: str,
) -> Response:
    """Rate-limit, run one queue action, and answer: redirect on success, the page
    again with the reason on a refusal, the plain page on an unknown id."""
    loc = normalise_locale(locale)
    try:
        await check_queue_write_rate(actor)
    except RateLimited:
        return await _render_queue(
            request,
            loc,
            session,
            actor,
            status_code=429,
            page_error=translate("errors.rate_limited", loc),
        )
    try:
        await call()
    except QueueRefused as exc:
        await session.rollback()  # let go of the doctor lock before drawing the page
        return await _render_queue(
            request,
            loc,
            session,
            actor,
            status_code=exc.http_status,
            page_error=translate(exc.message_key, loc),
        )
    except NotFound:
        await session.rollback()
        return RedirectResponse(f"/{loc}/admin/queue", status_code=303)
    return RedirectResponse(f"/{loc}/admin/queue?saved={saved}", status_code=303)


@router.post("/{locale}/admin/queue/check-in/{appointment_id}")
async def check_in(
    request: Request,
    locale: str,
    appointment_id: str,
    session: SessionDep,
    actor: OptionalActorDep,
) -> Response:
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    parsed = _parse_id(appointment_id)
    if parsed is None:
        return RedirectResponse(f"/{normalise_locale(locale)}/admin/queue", status_code=303)
    return await _run(
        request,
        locale,
        session,
        actor,
        lambda: service.check_in(session, actor, parsed, user_agent=_agent(request)),
        "checked_in",
    )


async def _entry_action(
    request: Request,
    locale: str,
    entry_id: str,
    session: SessionDep,
    actor: Actor | None,
    action: Callable[..., Awaitable[None]],
    saved: str,
) -> Response:
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    parsed = _parse_id(entry_id)
    if parsed is None:
        return RedirectResponse(f"/{normalise_locale(locale)}/admin/queue", status_code=303)
    return await _run(
        request,
        locale,
        session,
        actor,
        lambda: action(session, actor, parsed, user_agent=_agent(request)),
        saved,
    )


@router.post("/{locale}/admin/queue/{entry_id}/send-in")
async def send_in(
    request: Request, locale: str, entry_id: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    return await _entry_action(
        request, locale, entry_id, session, actor, service.send_in, "sent_in"
    )


@router.post("/{locale}/admin/queue/{entry_id}/seen")
async def seen(
    request: Request, locale: str, entry_id: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    return await _entry_action(request, locale, entry_id, session, actor, service.mark_seen, "seen")


@router.post("/{locale}/admin/queue/{entry_id}/put-back")
async def put_back(
    request: Request, locale: str, entry_id: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    return await _entry_action(
        request, locale, entry_id, session, actor, service.put_back, "put_back"
    )


@router.post("/{locale}/admin/queue/{entry_id}/move-up")
async def move_up(
    request: Request, locale: str, entry_id: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    return await _entry_action(request, locale, entry_id, session, actor, service.move_up, "moved")


@router.post("/{locale}/admin/queue/{entry_id}/make-next")
async def make_next(
    request: Request, locale: str, entry_id: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    return await _entry_action(
        request, locale, entry_id, session, actor, service.make_next, "moved"
    )


@router.post("/{locale}/admin/queue/walk-in")
async def walk_in(
    request: Request,
    locale: str,
    session: SessionDep,
    actor: OptionalActorDep,
    session_key: Annotated[str, Form(alias="session")] = "",
    patient_id: Annotated[str, Form()] = "",
) -> Response:
    if (redirect := _guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    key, patient = SessionKey.decode(session_key), _parse_id(patient_id)
    if key is None or patient is None:
        return RedirectResponse(f"/{normalise_locale(locale)}/admin/queue", status_code=303)
    return await _run(
        request,
        locale,
        session,
        actor,
        lambda: service.add_walk_in(session, actor, key, patient, user_agent=_agent(request)),
        "walk_in",
    )
