"""Medical timeline pages (P3 — FR8, FR9).

  /{locale}/patient/timeline                                       the patient's own timeline
  /{locale}/patient/timeline/{kind}/{id}                           one event, with its details
  /{locale}/doctor/patient/{patient_id}/timeline                   a patient's timeline, for a verified doctor
  /{locale}/doctor/patient/{patient_id}/timeline/{kind}/{id}

Both audiences call `timeline/service.py`, which reads through the consent gateway, so a doctor
sees only the sections the patient shared, each read is logged, and every refusal is the same
"not found" page as an unknown patient (D2). Every control on these pages is a plain link or GET
form: they work without JavaScript, and the filters live in the address so a view can be
bookmarked or shared with the same person.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..db.models import UserRole
from ..deps import Actor, OptionalActorDep, SessionDep
from ..errors import ConsentDenied, NotFound
from ..i18n.catalogue import normalise_locale, translate
from ..timeline import events as ev
from ..timeline import service
from ..timeline.schemas import OWNER_ONLY_CATEGORIES, TAB_CATEGORIES
from .router import _area, _doctor_nav, _patient_nav, _render, _render_lookup, login_redirect

router = APIRouter(tags=["web-timeline"])

PAGE_SIZE = 25
# The tabs are `schemas.TAB_CATEGORIES` after "All"; allergies and documented conditions
# (category `profile`) are reached under All only.
_FILTER_KEYS = ("type", "q", "date_from", "date_to", "facility", "doctor")
# Problems shown beside the field they are about; any other is listed in a banner.
_INLINE_ERRORS = ("q", "date_from", "date_to")
# What carries over to a detail page's "back" link: the filters and the page, nothing else.
_BACK_KEYS = (*_FILTER_KEYS, "page")
# How each provenance is drawn (the prototype's `prov`: a doctor's ✓, the AI's ✦, the patient's own).
_PROVENANCE_CLASS = {
    "doctor_confirmed": "doctor",
    "clinician_recorded": "doctor",
    "ai_patient_confirmed": "ai",
    "patient_entered": "user",
    "source_unknown": "user",
}


# ── Guards ───────────────────────────────────────────────────────────────
def _patient_guard(request: Request, locale: str, actor: Actor | None) -> Response | None:
    loc = normalise_locale(locale)
    if actor is None:
        return login_redirect(loc, request)
    if actor.role != UserRole.PATIENT.value:
        return RedirectResponse(f"/{loc}/{_area(actor.role)}", status_code=303)
    if not actor.onboarding_complete:
        return RedirectResponse(f"/{loc}/onboarding", status_code=303)
    return None


def _doctor_guard(request: Request, locale: str, actor: Actor | None) -> Response | None:
    """Patient data: a verified doctor only. A pending doctor is sent to their dashboard, where
    the FR3 banner explains why."""
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


# ── Whose timeline, and where its pages live ─────────────────────────────
@dataclass(frozen=True)
class _Scope:
    loc: str
    owner: bool  # the patient themselves (vs. a doctor the patient shared with)
    patient_id: uuid.UUID
    base: str  # the timeline page's address, without a query
    suffix: str  # the same address without the locale prefix (the language switch)
    record_href: str  # where "back to the record" goes


def _scope_for_patient(loc: str, patient_id: uuid.UUID) -> _Scope:
    return _Scope(
        loc,
        True,
        patient_id,
        f"/{loc}/patient/timeline",
        "/patient/timeline",
        f"/{loc}/patient",
    )


def _scope_for_doctor(loc: str, patient_id: uuid.UUID) -> _Scope:
    path = f"/doctor/patient/{patient_id}"
    return _Scope(
        loc, False, patient_id, f"/{loc}{path}/timeline", f"{path}/timeline", f"/{loc}{path}"
    )


def _parse_id(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _url(base: str, values: Mapping[str, str | None]) -> str:
    query = urlencode({key: value for key, value in values.items() if value})
    return f"{base}?{query}" if query else base


def _page_number(raw: str | None) -> int:
    return int(raw) if raw and raw.isdigit() and len(raw) < 9 and int(raw) > 0 else 1


def _context(scope: _Scope) -> dict[str, Any]:
    loc = scope.loc
    return {
        "owner": scope.owner,
        "crumb": translate("auth.role.patient" if scope.owner else "auth.role.doctor", loc),
        "page_title": translate("timeline.title", loc),
        "nav_items": (
            _patient_nav(loc, current="timeline")
            if scope.owner
            else _doctor_nav(loc, current="lookup")
        ),
        "path_suffix": scope.suffix,
        "timeline_href": scope.base,
        "record_href": scope.record_href,
        "fmt_date": lambda value: ev.human_date(value, loc),
        "prov_class": lambda value: _PROVENANCE_CLASS.get(value, "user"),
        "kind_label": lambda kind: translate(f"timeline.kind.{kind}", loc),
    }


def _not_found_for_doctor(request: Request, loc: str) -> Response:
    """Identical to an unknown patient on the lookup page (D2): never says which refusal it was."""
    return _render_lookup(request, loc, status_code=404, error=translate("lookup.not_found", loc))


# ── The timeline ─────────────────────────────────────────────────────────
async def _render_list(
    request: Request,
    scope: _Scope,
    session: SessionDep,
    actor: Actor,
    query: Mapping[str, str],
    *,
    status_code: int = 200,
    page_error: str | None = None,
) -> Response:
    loc = scope.loc
    filters, problems = service.parse_filters({key: query.get(key) for key in _FILTER_KEYS})
    page = _page_number(query.get("page"))
    data = await service.timeline(
        session,
        actor,
        scope.patient_id,
        filters,
        loc=loc,
        limit=PAGE_SIZE,
        offset=(page - 1) * PAGE_SIZE,
        clamp=True,
    )
    page = data.offset // PAGE_SIZE + 1
    pages = max(1, -(-data.total // PAGE_SIZE))
    # The filters as they are APPLIED (a value that was not understood is not echoed back).
    values = {
        "type": filters.category or "",
        "q": filters.query,
        "date_from": filters.date_from.isoformat() if filters.date_from else "",
        "date_to": filters.date_to.isoformat() if filters.date_to else "",
        "facility": filters.facility or "",
        "doctor": filters.doctor or "",
    }
    if problems.get("date_to") == "errors.timeline_range_invalid":
        # Both dates were readable but reversed: nothing was applied, but show what was typed.
        values["date_from"] = (query.get("date_from") or "").strip()
        values["date_to"] = (query.get("date_to") or "").strip()
    # Extra filters (everything but the tab) narrow the list; the tab alone does not count as one.
    narrowed = any(values[key] for key in _FILTER_KEYS if key != "type")
    # The search sits in the form's visible row; the rest wait in the collapsed section.
    more_filters = (
        any(values[key] for key in ("date_from", "date_to", "facility", "doctor"))
        or "date_from" in problems
        or "date_to" in problems  # the section opens so the message beside the date is seen
    )

    def detail_href(ref: str) -> str:
        return _url(
            f"{scope.base}/{ref.replace(':', '/')}",
            {**{k: v for k, v in values.items() if v}, "page": str(page) if page > 1 else ""},
        )

    return _render(
        request,
        "timeline/list.html",
        loc,
        status_code=status_code,
        data=data,
        events=[e for e in data.items if e.event_date is not None],
        undated=[e for e in data.items if e.event_date is None],
        values=values,
        selected=values["type"],
        narrowed=narrowed,
        any_filter=bool(values["type"]) or narrowed,
        more_filters=more_filters,
        pending=data.pending,
        pending_href=lambda document_id: f"/{loc}/patient/documents/{document_id}",
        errors={k: translate(v, loc) for k, v in problems.items() if k in _INLINE_ERRORS},
        ignored=[translate(v, loc) for k, v in problems.items() if k not in _INLINE_ERRORS],
        facility_options=[{"value": name, "label": name} for name in data.facilities],
        doctor_options=[{"value": name, "label": name} for name in data.doctors],
        tabs=[
            ("", "all"),
            *((c, c) for c in TAB_CATEGORIES if scope.owner or c not in OWNER_ONLY_CATEGORIES),
        ],
        tab_count=lambda key: data.counts.get(key or "all", 0),
        tab_href=lambda category: _url(scope.base, {**values, "type": category, "page": ""}),
        reset_href=scope.base,
        page=page,
        pages=pages,
        page_href=lambda n: _url(scope.base, {**values, "page": str(n) if n > 1 else ""}),
        detail_href=detail_href,
        page_error=page_error,
        **_context(scope),
    )


@router.get("/{locale}/patient/timeline", response_class=HTMLResponse)
async def patient_timeline_page(
    request: Request, locale: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    if (redirect := _patient_guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    scope = _scope_for_patient(
        normalise_locale(locale), await service.own_patient_id(session, actor)
    )
    return await _render_list(request, scope, session, actor, request.query_params)


@router.get("/{locale}/doctor/patient/{patient_id}/timeline", response_class=HTMLResponse)
async def doctor_timeline_page(
    request: Request, locale: str, patient_id: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    if (redirect := _doctor_guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    parsed = _parse_id(patient_id)
    if parsed is None:
        return _not_found_for_doctor(request, loc)
    try:
        return await _render_list(
            request, _scope_for_doctor(loc, parsed), session, actor, request.query_params
        )
    except NotFound:  # includes ConsentDenied — one answer for every refusal
        return _not_found_for_doctor(request, loc)


# ── One event ────────────────────────────────────────────────────────────
async def _render_event(
    request: Request,
    scope: _Scope,
    session: SessionDep,
    actor: Actor,
    kind: str,
    event_id: str,
) -> Response:
    loc = scope.loc
    query = request.query_params
    parsed = _parse_id(event_id)
    try:
        if parsed is None:
            raise NotFound()
        event = await service.event_detail(session, actor, scope.patient_id, kind, parsed, loc=loc)
    except ConsentDenied:
        # Only a doctor can be refused (no access to this patient at all).
        return _not_found_for_doctor(request, loc)
    except NotFound:
        try:
            return await _render_list(
                request,
                scope,
                session,
                actor,
                {},
                status_code=404,
                page_error=translate("errors.not_found", loc),
            )
        except NotFound:  # the doctor's access ended meanwhile
            return _not_found_for_doctor(request, loc)
    back = {key: query.get(key, "") for key in _BACK_KEYS}
    return _render(
        request,
        "timeline/detail.html",
        loc,
        event=event,
        back_href=_url(scope.base, back),
        related_href=lambda ref: f"{scope.base}/{ref.replace(':', '/')}",
        **{**_context(scope), "path_suffix": f"{scope.suffix}/{kind}/{event_id}"},
    )


@router.get("/{locale}/patient/timeline/{kind}/{event_id}", response_class=HTMLResponse)
async def patient_event_page(
    request: Request,
    locale: str,
    kind: str,
    event_id: str,
    session: SessionDep,
    actor: OptionalActorDep,
) -> Response:
    if (redirect := _patient_guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    scope = _scope_for_patient(
        normalise_locale(locale), await service.own_patient_id(session, actor)
    )
    return await _render_event(request, scope, session, actor, kind, event_id)


@router.get(
    "/{locale}/doctor/patient/{patient_id}/timeline/{kind}/{event_id}", response_class=HTMLResponse
)
async def doctor_event_page(
    request: Request,
    locale: str,
    patient_id: str,
    kind: str,
    event_id: str,
    session: SessionDep,
    actor: OptionalActorDep,
) -> Response:
    if (redirect := _doctor_guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    parsed = _parse_id(patient_id)
    if parsed is None:
        return _not_found_for_doctor(request, loc)
    return await _render_event(
        request, _scope_for_doctor(loc, parsed), session, actor, kind, event_id
    )
