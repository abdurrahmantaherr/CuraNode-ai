"""Server-rendered pages and form handling (SPEC 5).

Role enforcement here is a second line only. The authoritative check is the
backend dependency in `deps.py`; these handlers exist so a browser gets a
redirect instead of a raw 403 page.

Locale is a path prefix (`/en/...`, `/ur/...`), so switching language is a
route change that preserves the page — FR28's "without losing the user's
place" with no client state to juggle.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select
from supabase_auth.errors import AuthApiError

from ..audit import writer as audit
from ..cache import cache, oauth_state_key
from ..db import types as dbtypes
from ..db.models import Clinic, Profile, UserRole
from ..deps import (
    Actor,
    OptionalActorDep,
    SessionDep,
    check_profile_write_rate,
    enforce_auth_rate_limit,
)
from ..errors import (
    AppError,
    DuplicateEntry,
    ListFull,
    NotEditable,
    NotFound,
    RateLimited,
    Unauthenticated,
    ValidationFailed,
    message_params,
)
from ..i18n.catalogue import direction, normalise_locale, translate
from ..identity import oauth, service
from ..identity.router import clear_session_cookies, set_session_cookies
from ..identity.schemas import (
    DoctorOnboardingRequest,
    DoctorRegisterRequest,
    LoginRequest,
    PatientOnboardingRequest,
    PatientRegisterRequest,
)
from ..paths import TEMPLATES_DIR
from ..profile import schemas as profile_schemas
from ..profile import service as profile_service
from ..settings import settings

OAUTH_STATE_COOKIE = "cn_oauth_state"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["web"])

ROLE_LABEL = {
    "patient": "auth.role.patient",
    "doctor": "auth.role.doctor",
    "admin": "admin.dashboard.title",
}


def _base_context(request: Request, locale: str) -> dict[str, Any]:
    loc = normalise_locale(locale)
    suffix = request.url.path[len(f"/{loc}") :] or "/"
    return {
        "request": request,
        "locale": loc,
        "direction": direction(loc),
        "other_locale": "ur" if loc == "en" else "en",
        "path_suffix": suffix,
        "t": lambda key, **kw: translate(key, loc, **kw),
        "oauth_enabled": settings.oauth_enabled,
    }


def _render(
    request: Request, template: str, locale: str, status_code: int = 200, **extra: Any
) -> HTMLResponse:
    ctx = _base_context(request, locale)
    ctx.update(extra)
    return templates.TemplateResponse(request, template, ctx, status_code=status_code)


async def _clinic_options(session: SessionDep) -> list[dict[str, str]]:
    rows = await session.execute(select(Clinic).order_by(Clinic.city, Clinic.name))
    return [{"value": str(c.id), "label": f"{c.name} — {c.city}"} for c in rows.scalars().all()]


def _initials(name: str) -> str:
    parts = [p for p in name.split() if p]
    return "".join(p[0].upper() for p in parts[:2]) or "?"


# ── Login ────────────────────────────────────────────────────────────────
@router.get("/{locale}/login", response_class=HTMLResponse)
async def login_page(request: Request, locale: str, actor: OptionalActorDep) -> Response:
    if actor is not None:
        return RedirectResponse(f"/{normalise_locale(locale)}/{_area(actor.role)}", status_code=303)
    return _render(request, "auth/login.html", locale, role="patient", values={}, errors={})


@router.post("/{locale}/login")
async def login_submit(
    request: Request,
    locale: str,
    session: SessionDep,
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "",
) -> Response:
    loc = normalise_locale(locale)
    selected = role if role in ("patient", "doctor") else ""

    def fail(message_key: str, code: int = 400, **params: object) -> Response:
        return _render(
            request,
            "auth/login.html",
            loc,
            status_code=code,
            # Keep the toggle where the user left it, so the error makes sense.
            role=selected or "patient",
            values={"email": email},
            errors={},
            form_error=translate(message_key, loc, **params),
        )

    try:
        await enforce_auth_rate_limit(request)
    except RateLimited:
        return fail("errors.rate_limited", 429)

    try:
        body = LoginRequest(email=email, password=password)
    except ValidationError:
        return fail("errors.unauthenticated", 401)

    try:
        out, pair = await service.login(
            session,
            body,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except Unauthenticated:
        return fail("errors.unauthenticated", 401)

    # Role check runs AFTER the password is verified, so it can only ever be
    # triggered by someone who already owns the account. It therefore reveals
    # nothing an attacker could not already see, and the session is discarded
    # rather than handed over (SPEC AC-23).
    if selected and out.role != selected:
        await service.discard_session(session, pair)
        return fail(
            "errors.role_mismatch",
            403,
            selected=translate(f"auth.role.{selected}", loc),
        )

    response = RedirectResponse(f"/{loc}/{_area(out.role)}", status_code=303)
    set_session_cookies(response, pair)
    return response


# ── Register ─────────────────────────────────────────────────────────────
@router.get("/{locale}/register", response_class=HTMLResponse)
async def register_page(
    request: Request, locale: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    if actor is not None:
        return RedirectResponse(f"/{normalise_locale(locale)}/{_area(actor.role)}", status_code=303)
    return _render(
        request,
        "auth/register.html",
        locale,
        role="patient",
        values={},
        errors={},
        clinics=await _clinic_options(session),
    )


@router.post("/{locale}/register")
async def register_submit(
    request: Request,
    locale: str,
    session: SessionDep,
    full_name: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    password_confirm: Annotated[str, Form()] = "",
    phone_e164: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "patient",
    pmdc_number: Annotated[str, Form()] = "",
    specialty: Annotated[str, Form()] = "",
    primary_clinic_id: Annotated[str, Form()] = "",
    consent: Annotated[str | None, Form()] = None,
) -> Response:
    loc = normalise_locale(locale)
    role = role if role in ("patient", "doctor") else "patient"
    values = {
        "full_name": full_name,
        "email": email,
        "phone_e164": phone_e164,
        "pmdc_number": pmdc_number,
        "specialty": specialty,
        "primary_clinic_id": primary_clinic_id,
        "consent": bool(consent),
    }

    async def rerender(
        errors: dict[str, str], form_error: str | None = None, code: int = 422
    ) -> Response:
        return _render(
            request,
            "auth/register.html",
            loc,
            status_code=code,
            role=role,
            values=values,
            errors=errors,
            clinics=await _clinic_options(session),
            form_error=form_error,
        )

    try:
        await enforce_auth_rate_limit(request)
    except RateLimited:
        return await rerender({}, translate("errors.rate_limited", loc), 429)

    # Presentation-layer checks that the API schema cannot express.
    errors: dict[str, str] = {}
    if not full_name.strip():
        errors["full_name"] = translate("errors.name_required", loc)
    if len(password) < 10:
        errors["password"] = translate("errors.password_short", loc)
    elif password != password_confirm:
        errors["password_confirm"] = translate("errors.password_mismatch", loc)
    if not consent:
        errors["consent"] = translate("errors.consent_required", loc)
    if errors:
        return await rerender(errors)

    payload: dict[str, Any] = {
        "full_name": full_name,
        "email": email,
        "password": password,
        "preferred_locale": loc,
        "phone_e164": phone_e164.strip() or None,
    }

    try:
        if role == "doctor":
            if not (pmdc_number.strip() and specialty.strip() and primary_clinic_id):
                return await rerender({}, translate("errors.doctor_fields_required", loc))
            body: Any = DoctorRegisterRequest(
                role="doctor",
                pmdc_number=pmdc_number.strip(),
                specialty=specialty.strip(),
                primary_clinic_id=uuid.UUID(primary_clinic_id),
                **payload,
            )
        else:
            body = PatientRegisterRequest(**payload)
    except (ValidationError, ValueError):
        return await rerender({"email": translate("errors.email_invalid", loc)})

    try:
        out, pair = await service.register(
            session,
            body,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except ValidationFailed:
        return await rerender({"primary_clinic_id": translate("errors.clinic_unknown", loc)})
    except AppError:
        return await rerender({}, translate("errors.internal", loc), 500)

    if pair is None:
        # Duplicate email. Do not confirm the address exists — send the user to
        # sign in instead (SPEC AC-24, BL-05).
        return RedirectResponse(f"/{loc}/login", status_code=303)

    response = RedirectResponse(f"/{loc}/{_area(out.role)}", status_code=303)
    set_session_cookies(response, pair)
    return response


@router.post("/{locale}/logout")
async def logout_submit(request: Request, locale: str, session: SessionDep) -> Response:
    await service.logout(session, request.cookies.get(settings.access_cookie_name))
    response = RedirectResponse(f"/{normalise_locale(locale)}/login", status_code=303)
    clear_session_cookies(response)
    return response


# ── OAuth (docs/oauth.md) ────────────────────────────────────────────────
def _set_oauth_state_cookie(response: Response, state: str) -> None:
    # SameSite=Lax, not Strict: this cookie must survive the return hop from
    # Google via Supabase, which is a cross-site navigation (plan §3a).
    response.set_cookie(
        OAUTH_STATE_COOKIE,
        state,
        max_age=settings.oauth_state_ttl_s,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def _clear_oauth_state_cookie(response: Response) -> None:
    response.delete_cookie(
        OAUTH_STATE_COOKIE,
        path="/",
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )


@router.post("/{locale}/auth/oauth/{provider}")
async def oauth_start(
    request: Request, locale: str, session: SessionDep, provider: str
) -> Response:
    # A POST, not a link — a prefetch, an <img>, or a stray crawler must not
    # be able to start a flow (plan §7).
    loc = normalise_locale(locale)

    try:
        await enforce_auth_rate_limit(request)
    except RateLimited:
        return _render(
            request,
            "auth/login.html",
            loc,
            status_code=429,
            role="patient",
            values={},
            errors={},
            form_error=translate("errors.rate_limited", loc),
        )

    if not oauth.is_enabled(provider):
        return _render(
            request,
            "auth/login.html",
            loc,
            status_code=400,
            role="patient",
            values={},
            errors={},
            form_error=translate("errors.oauth_unavailable", loc),
        )

    verifier, challenge = oauth.make_pkce_pair()
    state = oauth.new_state()
    pending = oauth.PendingAuth(
        verifier=verifier, locale=loc, next_path=None, created_at=oauth.utcnow()
    )
    await cache.set(oauth_state_key(state), pending, settings.oauth_state_ttl_s)

    await audit.write(
        session,
        action=audit.AUTH_OAUTH_START,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        detail={"provider": provider},
    )
    await session.commit()

    # Our CSRF-binding state rides inside `redirect_to`'s own query string —
    # NOT as a sibling `state=` param on the Supabase call itself. Supabase's
    # `/authorize` manages its own internal state for the provider round
    # trip; a caller-supplied `state` collides with that and comes back as
    # `bad_oauth_state` (see `oauth.authorize_url`'s docstring).
    redirect_to = f"{settings.public_base_url}/auth/callback?{urlencode({'state': state})}"
    url = oauth.authorize_url(provider, challenge=challenge, redirect_to=redirect_to)
    response = RedirectResponse(url, status_code=303)
    _set_oauth_state_cookie(response, state)
    return response


@router.get("/auth/callback")
async def oauth_callback(
    request: Request,
    session: SessionDep,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> Response:
    # Not locale-prefixed — the redirect URI registered with Supabase has to
    # be one fixed string; locale is recovered from the stored PendingAuth.

    def fail(loc: str) -> Response:
        response = _render(
            request,
            "auth/login.html",
            loc,
            status_code=400,
            role="patient",
            values={},
            errors={},
            form_error=translate("errors.oauth_failed", loc),
        )
        _clear_oauth_state_cookie(response)
        return response

    try:
        await enforce_auth_rate_limit(request)
    except RateLimited:
        return fail(settings.default_locale)

    cookie_state = request.cookies.get(OAUTH_STATE_COOKIE)
    pending: oauth.PendingAuth | None = None
    if state:
        pending = await cache.get(oauth_state_key(state))

    # State triple-check: query param, cookie, and cache entry must all
    # agree, and the cache entry is consumed here regardless of outcome —
    # this is the CSRF defence for the whole flow (plan §6).
    if state:
        await cache.delete(oauth_state_key(state))
    if not (code and state and cookie_state and pending and cookie_state == state):
        return fail(settings.default_locale)

    loc = pending.locale

    if error:
        return fail(loc)

    try:
        exchanged = await oauth.exchange_code(code, pending.verifier)
    except AuthApiError:
        return fail(loc)

    try:
        session_out, pair, onboarding_required = await service.login_with_oauth(
            session,
            exchanged,
            provider="google",
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except Unauthenticated:
        return fail(loc)

    target = f"/{loc}/onboarding" if onboarding_required else f"/{loc}/{_area(session_out.role)}"

    # Same-site interstitial (plan §3a): the hop that just landed here is a
    # cross-site navigation, so a Strict session cookie set on THIS response
    # can be dropped on the immediate next request. Bouncing through a
    # same-site page first means the request that follows is same-site.
    response = _render(request, "auth/oauth_complete.html", loc, next_path=target)
    set_session_cookies(response, pair)
    _clear_oauth_state_cookie(response)
    return response


# ── Onboarding (first-time OAuth users only) ─────────────────────────────
@router.get("/{locale}/onboarding", response_class=HTMLResponse)
async def onboarding_page(
    request: Request, locale: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    loc = normalise_locale(locale)
    if actor is None:
        return RedirectResponse(f"/{loc}/login", status_code=303)
    if actor.onboarding_complete:
        return RedirectResponse(f"/{loc}/{_area(actor.role)}", status_code=303)

    user = await session.get(Profile, actor.user_id)
    assert user is not None
    return _render(
        request,
        "auth/onboarding.html",
        loc,
        role="patient",
        values={"full_name": user.full_name or ""},
        errors={},
        clinics=await _clinic_options(session),
    )


@router.post("/{locale}/onboarding")
async def onboarding_submit(
    request: Request,
    locale: str,
    session: SessionDep,
    actor: OptionalActorDep,
    full_name: Annotated[str, Form()] = "",
    phone_e164: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "patient",
    pmdc_number: Annotated[str, Form()] = "",
    specialty: Annotated[str, Form()] = "",
    primary_clinic_id: Annotated[str, Form()] = "",
    consent: Annotated[str | None, Form()] = None,
) -> Response:
    loc = normalise_locale(locale)
    if actor is None:
        return RedirectResponse(f"/{loc}/login", status_code=303)
    if actor.onboarding_complete:
        return RedirectResponse(f"/{loc}/{_area(actor.role)}", status_code=303)

    role = role if role in ("patient", "doctor") else "patient"
    values = {
        "full_name": full_name,
        "phone_e164": phone_e164,
        "pmdc_number": pmdc_number,
        "specialty": specialty,
        "primary_clinic_id": primary_clinic_id,
        "consent": bool(consent),
    }

    async def rerender(
        errors: dict[str, str], form_error: str | None = None, code: int = 422
    ) -> Response:
        return _render(
            request,
            "auth/onboarding.html",
            loc,
            status_code=code,
            role=role,
            values=values,
            errors=errors,
            clinics=await _clinic_options(session),
            form_error=form_error,
        )

    try:
        await enforce_auth_rate_limit(request)
    except RateLimited:
        return await rerender({}, translate("errors.rate_limited", loc), 429)

    # Same presentation-layer checks as register_submit, minus the password
    # rules — an OAuth user has no password to set here.
    errors: dict[str, str] = {}
    if not full_name.strip():
        errors["full_name"] = translate("errors.name_required", loc)
    if not consent:
        errors["consent"] = translate("errors.consent_required", loc)
    if errors:
        return await rerender(errors)

    payload: dict[str, Any] = {
        "full_name": full_name,
        "phone_e164": phone_e164.strip() or None,
        "preferred_locale": loc,
    }

    try:
        if role == "doctor":
            if not (pmdc_number.strip() and specialty.strip() and primary_clinic_id):
                return await rerender({}, translate("errors.doctor_fields_required", loc))
            body: Any = DoctorOnboardingRequest(
                role="doctor",
                pmdc_number=pmdc_number.strip(),
                specialty=specialty.strip(),
                primary_clinic_id=uuid.UUID(primary_clinic_id),
                **payload,
            )
        else:
            body = PatientOnboardingRequest(**payload)
    except (ValidationError, ValueError):
        return await rerender({"full_name": translate("errors.name_required", loc)})

    user = await session.get(Profile, actor.user_id)
    assert user is not None

    try:
        user = await service.complete_onboarding(
            session,
            user,
            body,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except ValidationFailed:
        return await rerender({"primary_clinic_id": translate("errors.clinic_unknown", loc)})

    return RedirectResponse(f"/{loc}/{_area(user.role.value)}", status_code=303)


# ── Protected areas ──────────────────────────────────────────────────────
def _area(role: str) -> str:
    return {"patient": "patient", "doctor": "doctor", "admin": "admin"}[role]


async def _guarded(
    request: Request,
    locale: str,
    session: SessionDep,
    actor: OptionalActorDep,
    required_role: str,
) -> Response:
    loc = normalise_locale(locale)

    if actor is None:
        # Preserve the destination so the user returns after signing in.
        return RedirectResponse(f"/{loc}/login?next={request.url.path}", status_code=303)

    if actor.role != required_role:
        # Cross-role deep link: send them to their own area rather than a raw
        # 403 page (SPEC AC-16).
        return RedirectResponse(f"/{loc}/{_area(actor.role)}", status_code=303)

    if not actor.onboarding_complete:
        # OAuth-only state: signed in, but no Patient/Doctor/ClinicStaff row
        # exists yet. No dashboard exists to show until that's chosen.
        return RedirectResponse(f"/{loc}/onboarding", status_code=303)

    user = await session.get(Profile, actor.user_id)
    assert user is not None
    me = await service.build_me(session, user)

    pending_message = ""
    show_banner = False
    if actor.role == UserRole.DOCTOR.value and not actor.is_verified_doctor:
        show_banner = True
        clinic_name = ""
        if me.clinic_ids:
            clinic = await session.get(Clinic, me.clinic_ids[0])
            clinic_name = clinic.name if clinic else ""
        pending_message = translate("doctor.pending_body", loc, clinic=clinic_name)

    title_key = {
        "patient": "patient.dashboard.title",
        "doctor": "doctor.dashboard.title",
        "admin": "admin.dashboard.title",
    }[actor.role]

    is_patient = actor.role == UserRole.PATIENT.value
    return _render(
        request,
        "shell.html",
        loc,
        nav_items=_patient_nav(loc, current="dashboard") if is_patient else [],
        profile_href=f"/{loc}/patient/profile" if is_patient else None,
        crumb=translate(ROLE_LABEL[actor.role], loc),
        page_title=translate(title_key, loc),
        full_name=me.full_name,
        initials=_initials(me.full_name),
        role_label=translate(ROLE_LABEL[actor.role], loc),
        passport_no=me.passport_no,
        show_unverified_banner=show_banner,
        pending_message=pending_message,
    )


@router.get("/{locale}/patient", response_class=HTMLResponse)
async def patient_area(
    request: Request, locale: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    return await _guarded(request, locale, session, actor, "patient")


@router.get("/{locale}/doctor", response_class=HTMLResponse)
async def doctor_area(
    request: Request, locale: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    return await _guarded(request, locale, session, actor, "doctor")


@router.get("/{locale}/admin", response_class=HTMLResponse)
async def admin_area(
    request: Request, locale: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    return await _guarded(request, locale, session, actor, "admin")


# ── Patient profile (FR2, profile SPEC §4.7–4.8) ────────────────────────
def _patient_nav(loc: str, *, current: str) -> list[dict[str, Any]]:
    return [
        {
            "href": f"/{loc}/patient",
            "label": translate("nav.dashboard", loc),
            "current": current == "dashboard",
        },
        {
            "href": f"/{loc}/patient/profile",
            "label": translate("nav.profile", loc),
            "current": current == "profile",
        },
    ]


def _patient_page_guard(request: Request, locale: str, actor: Actor | None) -> Response | None:
    """The profile's AC-03 redirects, mirroring `_guarded()`. Raises nothing;
    returns the redirect, or None when the caller may proceed."""
    loc = normalise_locale(locale)
    if actor is None:
        return RedirectResponse(f"/{loc}/login?next=/{loc}/patient/profile", status_code=303)
    if actor.role != UserRole.PATIENT.value:
        return RedirectResponse(f"/{loc}/{_area(actor.role)}", status_code=303)
    if not actor.onboarding_complete:
        return RedirectResponse(f"/{loc}/onboarding", status_code=303)
    return None


# URL section -> everything the generic entry handlers need for that kind.
_ENTRY_KINDS: dict[str, dict[str, Any]] = {
    "allergies": {
        "kind": "allergy",
        "fields": ("substance", "reaction", "severity"),
        "create": profile_schemas.AllergyCreate,
        "update": profile_schemas.AllergyUpdate,
        "add_fn": profile_service.add_allergy,
        "update_fn": profile_service.update_allergy,
        "remove_fn": profile_service.remove_allergy,
    },
    "conditions": {
        "kind": "condition",
        "fields": ("name", "onset_date"),
        "create": profile_schemas.ConditionCreate,
        "update": profile_schemas.ConditionUpdate,
        "add_fn": profile_service.add_condition,
        "update_fn": profile_service.update_condition,
        "remove_fn": profile_service.remove_condition,
    },
    "medications": {
        "kind": "medication",
        "fields": ("name", "strength", "frequency", "started_on", "notes"),
        "create": profile_schemas.MedicationCreate,
        "update": profile_schemas.MedicationUpdate,
        "add_fn": profile_service.add_medication,
        "update_fn": profile_service.update_medication,
        "remove_fn": profile_service.remove_medication,
    },
}
_KIND_TO_SECTION = {spec["kind"]: section for section, spec in _ENTRY_KINDS.items()}

_BASIC_FIELDS = (
    "full_name",
    "date_of_birth",
    "gender",
    "blood_group",
    "phone_e164",
    "emergency_contact",
)

# BL-31 — the only `saved` values honoured; user input is never reflected.
_SAVED: dict[str, tuple[str, str]] = {
    "basics.updated": ("basics", "profile.saved.basics"),
    **{
        f"{spec['kind']}.{event}": (section, f"profile.saved.{event}")
        for section, spec in _ENTRY_KINDS.items()
        for event in ("added", "updated", "removed")
    },
}

_SEVERITY_BADGE = {
    "severe": "badge--err",
    "moderate": "badge--warn",
    "mild": "badge--ok",
    "unknown": "badge--neutral",
}


def _as_form_value(value: Any) -> str:
    """How a stored value appears in a form control (and in BL-14 compares)."""
    if value is None:
        return ""
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _choice_options(
    loc: str, values: tuple[str, ...], stored: str | None, *, label_prefix: str | None
) -> list[dict[str, str]]:
    options = [
        {"value": v, "label": translate(f"{label_prefix}.{v}", loc) if label_prefix else v}
        for v in values
    ]
    # A legacy stored value outside the allowed set is shown and kept selected,
    # so saving the form unchanged sends it back untouched (BL-14).
    if stored and stored not in values:
        options.append({"value": stored, "label": stored})
    return options


def _entry_values(kind: str, entry: Any) -> dict[str, str]:
    fields = _ENTRY_KINDS[_KIND_TO_SECTION[kind]]["fields"]
    return {f: _as_form_value(getattr(entry, f)) for f in fields}


def _entry_parts(loc: str) -> Any:
    """The muted secondary line under an entry's name, empty parts omitted."""

    def parts(kind: str, e: Any) -> list[str]:
        if kind == "allergy":
            out = [e.reaction]
        elif kind == "condition":
            onset = e.onset_date
            out = [f"{translate('field.onset_date', loc)} {onset}" if onset else None]
        else:
            since = e.started_on
            out = [
                e.strength,
                e.frequency,
                f"{translate('field.started_on', loc)} {since}" if since else None,
            ]
        return [p for p in out if p]

    return parts


def _severity_label(loc: str) -> Any:
    def label(severity: str) -> str:
        if severity in profile_schemas.SEVERITIES:
            return translate(f"severity.{severity}", loc)
        return severity  # legacy value, shown raw

    return label


async def _render_profile(
    request: Request,
    loc: str,
    session: SessionDep,
    actor: Actor,
    *,
    status_code: int = 200,
    form_state: dict[str, Any] | None = None,
    saved: dict[str, str] | None = None,
    edit: dict[str, str] | None = None,
    page_error: str | None = None,
) -> Response:
    """Render the full page from the database, overlaying the one failed
    form's submitted values and errors (BL-30)."""
    try:
        profile = await profile_service.get_profile(session, actor)
    except NotFound:
        return RedirectResponse(f"/{loc}/onboarding", status_code=303)

    basics_values = {
        "full_name": profile.full_name,
        "date_of_birth": _as_form_value(profile.date_of_birth),
        "gender": _as_form_value(profile.gender),
        "blood_group": _as_form_value(profile.blood_group),
        "phone_e164": _as_form_value(profile.phone_e164),
        "emergency_contact": _as_form_value(profile.emergency_contact),
    }

    def form_failed(form_key: str) -> bool:
        return bool(form_state) and form_state.get("form") == form_key

    def severity_options_for(value: str | None) -> list[dict[str, str]]:
        return _choice_options(loc, profile_schemas.SEVERITIES, value, label_prefix="severity")

    return _render(
        request,
        "patient/profile.html",
        loc,
        status_code=status_code,
        # The language switch always targets the page, never a POST-only path.
        path_suffix="/patient/profile",
        crumb=translate("auth.role.patient", loc),
        page_title=translate("profile.title", loc),
        nav_items=_patient_nav(loc, current="profile"),
        profile=profile,
        initials=_initials(profile.full_name),
        today=dbtypes.today_pk().isoformat(),
        basics_values=basics_values,
        gender_options=_choice_options(
            loc, profile_schemas.GENDERS, profile.gender, label_prefix="gender"
        ),
        blood_group_options=_choice_options(
            loc, profile_schemas.BLOOD_GROUPS, profile.blood_group, label_prefix=None
        ),
        severity_options_for=severity_options_for,
        entry_values=_entry_values,
        entry_parts=_entry_parts(loc),
        severity_badge=lambda s: _SEVERITY_BADGE.get(s, "badge--neutral"),
        severity_label=_severity_label(loc),
        form_failed=form_failed,
        form_state=form_state,
        saved=saved,
        edit=edit,
        page_error=page_error,
    )


def _field_errors(exc: ValidationError, loc: str) -> dict[str, str]:
    errors: dict[str, str] = {}
    for err in exc.errors():
        field = str(err["loc"][0]) if err["loc"] else "__all__"
        errors.setdefault(field, translate(err["msg"], loc, **(err.get("ctx") or {})))
    return errors


async def _profile_failure(
    request: Request,
    loc: str,
    session: SessionDep,
    actor: Actor,
    exc: Exception,
    *,
    form: str,
    section: str,
    values: dict[str, Any],
    name_field: str | None = None,
) -> Response:
    """Map a failed web mutation onto the re-rendered page (SPEC §4.8)."""
    await session.rollback()

    def state(errors: dict[str, str], banner: str | None = None) -> dict[str, Any]:
        return {
            "form": form,
            "section": section,
            "values": values,
            "errors": errors,
            "banner": banner,
        }

    async def page(code: int, **kw: Any) -> Response:
        return await _render_profile(request, loc, session, actor, status_code=code, **kw)

    if isinstance(exc, ValidationError):
        return await page(422, form_state=state(_field_errors(exc, loc)))
    if isinstance(exc, ValidationFailed):
        fields = exc.details.get("fields", {})
        return await page(422, form_state=state({k: translate(v, loc) for k, v in fields.items()}))
    if isinstance(exc, DuplicateEntry):
        field = exc.details.get("field", name_field or "name")
        return await page(409, form_state=state({field: translate(exc.message_key, loc)}))
    if isinstance(exc, ListFull):
        banner = translate(exc.message_key, loc, **message_params(exc))
        return await page(422, form_state=state({}, banner))
    if isinstance(exc, NotEditable):
        return await page(403, form_state=state({}, translate(exc.message_key, loc)))
    if isinstance(exc, NotFound):
        return await page(404, page_error=translate("errors.not_found", loc))
    if isinstance(exc, RateLimited):
        return await page(429, page_error=translate("errors.rate_limited", loc))
    return await page(500, page_error=translate("errors.internal", loc))


def _submitted(form: Any, fields: tuple[str, ...]) -> dict[str, str]:
    """Only the named fields are read; anything else posted is ignored (BL-05)."""
    return {f: str(form[f]) for f in fields if f in form}


def _to_payload(submitted: dict[str, str], stored: dict[str, str] | None) -> dict[str, Any]:
    """Empty -> None, and (BL-14) drop fields whose value equals what's stored,
    so an untouched legacy value never blocks a save."""
    payload: dict[str, Any] = {}
    for field, raw in submitted.items():
        if stored is not None and raw.strip() == stored.get(field, ""):
            continue
        payload[field] = raw if raw.strip() else None
    return payload


def _parse_entry_id(entry_id: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(entry_id)
    except ValueError:
        return None


def _saved_redirect(loc: str, key: str, section: str) -> Response:
    return RedirectResponse(f"/{loc}/patient/profile?saved={key}#{section}", status_code=303)


@router.get("/{locale}/patient/profile", response_class=HTMLResponse)
async def patient_profile_page(
    request: Request,
    locale: str,
    session: SessionDep,
    actor: OptionalActorDep,
    saved: str | None = None,
    edit: str | None = None,
    id: str | None = None,
) -> Response:
    if (redirect := _patient_page_guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)

    saved_banner = None
    if saved in _SAVED:
        section, key = _SAVED[saved]
        saved_banner = {"section": section, "message": translate(key, loc)}

    # BL-32 — honoured only for an own, active, editable entry: the template
    # matches the id against the rendered editable rows, so anything else is
    # silently ignored (no error, no existence oracle).
    edit_state = None
    parsed_id = _parse_entry_id(id) if id else None
    if edit in _KIND_TO_SECTION and parsed_id is not None:
        edit_state = {"kind": edit, "id": str(parsed_id)}

    return await _render_profile(request, loc, session, actor, saved=saved_banner, edit=edit_state)


@router.post("/{locale}/patient/profile")
async def patient_profile_basics_submit(
    request: Request, locale: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    if (redirect := _patient_page_guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    submitted = _submitted(await request.form(), _BASIC_FIELDS)

    try:
        await check_profile_write_rate(actor)
        current = await profile_service.get_profile(session, actor)
        stored = {f: _as_form_value(getattr(current, f)) for f in _BASIC_FIELDS}
        body = profile_schemas.ProfileUpdateRequest.model_validate(_to_payload(submitted, stored))
        await profile_service.update_profile(
            session, actor, body, user_agent=request.headers.get("user-agent")
        )
    except Exception as exc:  # noqa: BLE001 — every outcome is mapped per SPEC §4.8
        return await _profile_failure(
            request, loc, session, actor, exc, form="basics", section="basics", values=submitted
        )
    return _saved_redirect(loc, "basics.updated", "basics")


@router.post("/{locale}/patient/profile/{section}")
async def patient_entry_add_submit(
    request: Request, locale: str, section: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    if (redirect := _patient_page_guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    spec = _ENTRY_KINDS.get(section)
    if spec is None:
        return await _profile_failure(
            request, loc, session, actor, NotFound(), form="", section="", values={}
        )
    kind = spec["kind"]
    submitted = _submitted(await request.form(), spec["fields"])

    try:
        await check_profile_write_rate(actor)
        body = spec["create"].model_validate(_to_payload(submitted, None))
        await spec["add_fn"](session, actor, body, user_agent=request.headers.get("user-agent"))
    except Exception as exc:  # noqa: BLE001 — every outcome is mapped per SPEC §4.8
        return await _profile_failure(
            request,
            loc,
            session,
            actor,
            exc,
            form=f"{kind}-new",
            section=section,
            values=submitted,
            name_field=spec["fields"][0],
        )
    return _saved_redirect(loc, f"{kind}.added", section)


@router.post("/{locale}/patient/profile/{section}/{entry_id}")
async def patient_entry_edit_submit(
    request: Request,
    locale: str,
    section: str,
    entry_id: str,
    session: SessionDep,
    actor: OptionalActorDep,
) -> Response:
    if (redirect := _patient_page_guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    spec = _ENTRY_KINDS.get(section)
    parsed_id = _parse_entry_id(entry_id)
    if spec is None or parsed_id is None:
        # A malformed id is NotFound on the web, not a raw 422 (SPEC §4.7).
        return await _profile_failure(
            request, loc, session, actor, NotFound(), form="", section="", values={}
        )
    kind = spec["kind"]
    submitted = _submitted(await request.form(), spec["fields"])

    try:
        await check_profile_write_rate(actor)
        current = await profile_service.get_profile(session, actor)
        own = next((e for e in getattr(current, section) if e.id == parsed_id and e.editable), None)
        stored = _entry_values(kind, own) if own is not None else None
        body = spec["update"].model_validate(_to_payload(submitted, stored))
        await spec["update_fn"](
            session, actor, parsed_id, body, user_agent=request.headers.get("user-agent")
        )
    except Exception as exc:  # noqa: BLE001 — every outcome is mapped per SPEC §4.8
        return await _profile_failure(
            request,
            loc,
            session,
            actor,
            exc,
            form=f"{kind}-{parsed_id}",
            section=section,
            values=submitted,
            name_field=spec["fields"][0],
        )
    return _saved_redirect(loc, f"{kind}.updated", section)


@router.post("/{locale}/patient/profile/{section}/{entry_id}/remove")
async def patient_entry_remove_submit(
    request: Request,
    locale: str,
    section: str,
    entry_id: str,
    session: SessionDep,
    actor: OptionalActorDep,
) -> Response:
    if (redirect := _patient_page_guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    spec = _ENTRY_KINDS.get(section)
    parsed_id = _parse_entry_id(entry_id)
    if spec is None or parsed_id is None:
        return await _profile_failure(
            request, loc, session, actor, NotFound(), form="", section="", values={}
        )
    kind = spec["kind"]

    try:
        await check_profile_write_rate(actor)
        await spec["remove_fn"](
            session, actor, parsed_id, user_agent=request.headers.get("user-agent")
        )
    except Exception as exc:  # noqa: BLE001 — every outcome is mapped per SPEC §4.8
        return await _profile_failure(
            request, loc, session, actor, exc, form="", section=section, values={}
        )
    return _saved_redirect(loc, f"{kind}.removed", section)
