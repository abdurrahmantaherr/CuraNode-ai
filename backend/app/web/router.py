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
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any
from urllib.parse import urlencode

import segno
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from pydantic import ValidationError
from pydantic_core import PydanticCustomError
from sqlalchemy import select
from supabase_auth.errors import AuthApiError

from ..admin import schemas as admin_schemas
from ..admin import service as admin_service
from ..audit import writer as audit
from ..cache import cache, oauth_state_key
from ..consent import access_log as consent_access_log
from ..consent import gateway as consent_gateway
from ..consent import schemas as consent_schemas
from ..consent import service as consent_service
from ..db import types as dbtypes
from ..db.models import Clinic, Profile, UserRole
from ..deps import (
    Actor,
    OptionalActorDep,
    SessionDep,
    check_consent_search_rate,
    check_consent_write_rate,
    check_profile_write_rate,
    check_schedule_write_rate,
    enforce_auth_rate_limit,
)
from ..doctor.rows import age_in_years, build_rows, gender_label
from ..errors import (
    AppError,
    DuplicateEntry,
    ListFull,
    NotEditable,
    NotFound,
    RateLimited,
    ScheduleConflict,
    Unauthenticated,
    ValidationFailed,
    message_params,
)
from ..i18n.catalogue import direction, normalise_locale, translate
from ..identity import oauth, service
from ..identity.router import clear_session_cookies, set_session_cookies
from ..identity.schemas import (
    MAX_CLINICS,
    DoctorOnboardingRequest,
    DoctorRegisterRequest,
    LoginRequest,
    PatientOnboardingRequest,
    PatientRegisterRequest,
)
from ..paths import TEMPLATES_DIR
from ..profile import schemas as profile_schemas
from ..profile import service as profile_service
from ..schedule import engine as schedule_engine
from ..schedule import schemas as schedule_schemas
from ..schedule import service as schedule_service
from ..settings import settings
from ..verification import service as verification_service

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


def _chosen_clinics(raw: list[str]) -> list[uuid.UUID] | None:
    """The hospitals/clinics ticked on a doctor sign-up form, de-duplicated in
    order. None when any value is not a UUID (a tampered form)."""
    try:
        return list(dict.fromkeys(uuid.UUID(v.strip()) for v in raw if v.strip()))
    except ValueError:
        return None


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
    selected = role if role in ("patient", "doctor", "admin") else ""

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
    clinic_ids: Annotated[list[str] | None, Form()] = None,
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
        "clinic_ids": [str(c) for c in (clinic_ids or [])]
        + ([primary_clinic_id] if primary_clinic_id else []),
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
            chosen = _chosen_clinics(values["clinic_ids"])
            if chosen is None:
                return await rerender({"clinic_ids": translate("errors.clinic_unknown", loc)})
            if not (pmdc_number.strip() and specialty.strip() and chosen):
                return await rerender({}, translate("errors.doctor_fields_required", loc))
            if len(chosen) > MAX_CLINICS:
                return await rerender({"clinic_ids": translate("errors.too_many_clinics", loc)})
            body: Any = DoctorRegisterRequest(
                role="doctor",
                pmdc_number=pmdc_number.strip(),
                specialty=specialty.strip(),
                clinic_ids=chosen,
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
        return await rerender({"clinic_ids": translate("errors.clinic_unknown", loc)})
    except AppError:
        return await rerender({}, translate("errors.internal", loc), 500)

    if pair is None:
        # Duplicate email. Do not confirm the address exists — send the user to
        # sign in instead (SPEC AC-24, BL-05).
        return RedirectResponse(f"/{loc}/login", status_code=303)

    response = RedirectResponse(f"/{loc}/{_area(out.role)}", status_code=303)
    set_session_cookies(response, pair)
    return response


# ── Clinic-admin sign-up (D-VERIFY v1) ───────────────────────────────────
@router.get("/{locale}/admin/register", response_class=HTMLResponse)
async def admin_register_page(
    request: Request, locale: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    loc = normalise_locale(locale)
    if actor is not None:
        return RedirectResponse(f"/{loc}/{_area(actor.role)}", status_code=303)
    return _render(
        request,
        "auth/admin_register.html",
        loc,
        values={},
        errors={},
        form_error=None,
        clinics=await _clinic_options(session),
    )


@router.post("/{locale}/admin/register")
async def admin_register_submit(
    request: Request,
    locale: str,
    session: SessionDep,
    full_name: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    password_confirm: Annotated[str, Form()] = "",
    clinic_id: Annotated[str, Form()] = "",
    invite_code: Annotated[str, Form()] = "",
    consent: Annotated[str | None, Form()] = None,
) -> Response:
    loc = normalise_locale(locale)
    values = {
        "full_name": full_name,
        "email": email,
        "clinic_id": clinic_id,
        "invite_code": invite_code,
        "consent": bool(consent),
    }

    async def rerender(
        errors: dict[str, str], form_error: str | None = None, code: int = 422
    ) -> Response:
        return _render(
            request,
            "auth/admin_register.html",
            loc,
            status_code=code,
            values=values,
            errors=errors,
            clinics=await _clinic_options(session),
            form_error=form_error,
        )

    try:
        await enforce_auth_rate_limit(request)
    except RateLimited:
        return await rerender({}, translate("errors.rate_limited", loc), 429)

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

    try:
        body = admin_schemas.AdminRegisterRequest(
            full_name=full_name,
            email=email,
            password=password,
            clinic_id=uuid.UUID(clinic_id),
            invite_code=invite_code,
            preferred_locale=loc,
        )
    except (ValidationError, ValueError):
        return await rerender({"email": translate("errors.email_invalid", loc)})

    try:
        out, pair = await admin_service.register_admin(
            session,
            body,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except ValidationFailed as exc:
        fields = exc.details.get("fields", {})
        return await rerender({k: translate(v, loc) for k, v in fields.items()})
    except AppError:
        return await rerender({}, translate("errors.internal", loc), 500)

    if pair is None:
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
    clinic_ids: Annotated[list[str] | None, Form()] = None,
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
        "clinic_ids": [str(c) for c in (clinic_ids or [])]
        + ([primary_clinic_id] if primary_clinic_id else []),
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
            chosen = _chosen_clinics(values["clinic_ids"])
            if chosen is None:
                return await rerender({"clinic_ids": translate("errors.clinic_unknown", loc)})
            if not (pmdc_number.strip() and specialty.strip() and chosen):
                return await rerender({}, translate("errors.doctor_fields_required", loc))
            if len(chosen) > MAX_CLINICS:
                return await rerender({"clinic_ids": translate("errors.too_many_clinics", loc)})
            body: Any = DoctorOnboardingRequest(
                role="doctor",
                pmdc_number=pmdc_number.strip(),
                specialty=specialty.strip(),
                clinic_ids=chosen,
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
        return await rerender({"clinic_ids": translate("errors.clinic_unknown", loc)})

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
    rejected_message = ""
    show_rejected_banner = False
    if actor.role == UserRole.DOCTOR.value and not actor.is_verified_doctor:
        clinic_name = ""
        if me.clinic_ids:
            clinic = await session.get(Clinic, me.clinic_ids[0])
            clinic_name = clinic.name if clinic else ""
        status, reason = await verification_service.doctor_status_for_banner(session, actor)
        if status == "rejected":
            show_rejected_banner = True
            reason_text = (
                translate(f"verify.reason.{reason}", loc)
                if reason
                else translate("verify.reason.other", loc)
            )
            rejected_message = translate(
                "doctor.rejected_body", loc, reason=reason_text, clinic=clinic_name
            )
        else:
            show_banner = True
            pending_message = translate("doctor.pending_body", loc, clinic=clinic_name)

    title_key = {
        "patient": "patient.dashboard.title",
        "doctor": "doctor.dashboard.title",
        "admin": "admin.dashboard.title",
    }[actor.role]

    is_patient = actor.role == UserRole.PATIENT.value
    patients = None
    if is_patient:
        nav_items = _patient_nav(loc, current="dashboard")
        links = [
            {"href": f"/{loc}/patient/profile", "label": translate("profile.open_link", loc)},
            {"href": f"/{loc}/patient/passport", "label": translate("passport.open_link", loc)},
            {"href": f"/{loc}/patient/access", "label": translate("passport.manage_access", loc)},
        ]
    elif actor.is_verified_doctor:
        # FR3 — an unverified doctor gets no patient-data affordance at all.
        nav_items = _doctor_nav(loc, current="dashboard")
        links = [
            {"href": f"/{loc}/doctor/passport-lookup", "label": translate("lookup.open_link", loc)},
            {"href": f"/{loc}/doctor/schedule", "label": translate("schedule.open_link", loc)},
        ]
        # D4 — who is listed is decided in the consent service, live, on every load.
        granted = await consent_service.list_patients_for_doctor(session, actor)
        patients = build_rows(granted, locale=loc, today=dbtypes.utcnow().date())
        await audit.write(
            session,
            action=audit.DOCTOR_DASHBOARD_VIEW,
            actor_user_id=actor.user_id,
            actor_role=actor.role,
            resource_type="patient_list",
            ip_address=actor.ip_address,
            user_agent=request.headers.get("user-agent"),
            detail={"patient_count": len(patients)},
        )
        await session.commit()
    elif actor.role == UserRole.DOCTOR.value:
        # A pending doctor: no patient-data affordance at all (FR3), but the
        # schedule exposes none, so it is their one link (D7 decision 6).
        nav_items = _doctor_nav(loc, current="dashboard", verified=False)
        links = [{"href": f"/{loc}/doctor/schedule", "label": translate("schedule.open_link", loc)}]
    else:
        nav_items, links = [], []

    # Only a verified doctor's page defines `patients`; shell.html keys off that.
    extra: dict[str, Any] = {}
    if patients is not None:
        extra["patients"] = patients
    return _render(
        request,
        "shell.html",
        loc,
        nav_items=nav_items,
        links=links,
        crumb=translate(ROLE_LABEL[actor.role], loc),
        page_title=translate(title_key, loc),
        full_name=me.full_name,
        initials=_initials(me.full_name),
        role_label=translate(ROLE_LABEL[actor.role], loc),
        passport_no=me.passport_no,
        show_unverified_banner=show_banner,
        pending_message=pending_message,
        show_rejected_banner=show_rejected_banner,
        rejected_message=rejected_message,
        **extra,
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
        {
            "href": f"/{loc}/patient/passport",
            "label": translate("nav.passport", loc),
            "current": current == "passport",
        },
        {
            "href": f"/{loc}/patient/access",
            "label": translate("nav.access", loc),
            "current": current == "access",
        },
    ]


def _patient_page_guard(
    request: Request, locale: str, actor: Actor | None, page: str = "profile"
) -> Response | None:
    """The profile's AC-03 redirects, mirroring `_guarded()`. Raises nothing;
    returns the redirect, or None when the caller may proceed. `page` is the
    GET page to come back to after signing in (never a POST-only path)."""
    loc = normalise_locale(locale)
    if actor is None:
        return RedirectResponse(f"/{loc}/login?next=/{loc}/patient/{page}", status_code=303)
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


# ── Medical Passport (FR1, FR4 — passport SPEC §8) ──────────────────────
def _qr_svg(text: str) -> Markup:
    """BL-11 — the code encodes `text` alone. Inline SVG, so it renders with
    JavaScript disabled (AC-14). Always dark-on-light whatever the theme: an
    inverted code scans unreliably."""
    svg = segno.make(text, micro=False).svg_inline(
        scale=6, border=4, omitsize=True, svgclass="qr__svg", dark="black", light="white"
    )
    return Markup(svg)  # generated by segno from a server-issued value, no user markup


@router.get("/{locale}/patient/passport", response_class=HTMLResponse)
async def patient_passport_page(
    request: Request, locale: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    if (redirect := _patient_page_guard(request, locale, actor, "passport")) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    user = await session.get(Profile, actor.user_id)
    assert user is not None
    me = await service.build_me(session, user)
    if not me.passport_no:
        return RedirectResponse(f"/{loc}/onboarding", status_code=303)
    return _render(
        request,
        "patient/passport.html",
        loc,
        crumb=translate("auth.role.patient", loc),
        page_title=translate("passport.title", loc),
        nav_items=_patient_nav(loc, current="passport"),
        passport_no=me.passport_no,
        qr_svg=_qr_svg(me.passport_no),
    )


# BL-25 / profile BL-31 pattern — the only `saved` values honoured; user input
# is never reflected.
_ACCESS_SAVED = {
    "granted": "access.saved.granted",
    "exists": "access.saved.exists",
    "changed": "access.saved.changed",
    "revoked": "access.saved.revoked",
}
_DURATIONS = tuple(consent_schemas.DURATIONS)
_LATER_SECTIONS = frozenset({"prescriptions", "lab_reports", "visits"})  # filled by P2/P3


def _pkt(ts: datetime | None) -> str:
    """A stored UTC timestamp as Pakistan time, `YYYY-MM-DD HH:MM`."""
    if ts is None:
        return ""
    if ts.tzinfo is None:  # SQLite returns naive datetimes; every stored value is UTC
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(dbtypes.PKT).strftime("%Y-%m-%d %H:%M")


def _pkt_date(ts: datetime | None) -> str:
    return _pkt(ts)[:10]


def _parse_grantee(value: str) -> tuple[str, uuid.UUID] | None:
    """`doctor:<uuid>` / `clinic:<uuid>` from the search result radios."""
    kind, _, raw = value.partition(":")
    if kind not in ("doctor", "clinic"):
        return None
    parsed = _parse_entry_id(raw)
    return (kind, parsed) if parsed is not None else None


async def _render_access(
    request: Request,
    loc: str,
    session: SessionDep,
    actor: Actor,
    *,
    status_code: int = 200,
    q: str = "",
    q_error: str | None = None,
    results: list[consent_schemas.GranteeOut] | None = None,
    form_state: dict[str, Any] | None = None,
    saved: str | None = None,
    page_error: str | None = None,
) -> Response:
    try:
        every = await consent_service.list_grants(session, actor, include_revoked=True)
        log = await consent_access_log.list_access_log(session, actor)
    except NotFound:
        return RedirectResponse(f"/{loc}/onboarding", status_code=303)
    return _render(
        request,
        "patient/access.html",
        loc,
        status_code=status_code,
        # The language switch always targets the page, never a POST-only path.
        path_suffix="/patient/access",
        crumb=translate("auth.role.patient", loc),
        page_title=translate("access.title", loc),
        nav_items=_patient_nav(loc, current="access"),
        grants=[g for g in every if g.active],
        past=[g for g in every if not g.active],
        log=log.items,
        sections=consent_schemas.SECTIONS,
        later_sections=_LATER_SECTIONS,
        durations=_DURATIONS,
        q=q,
        q_error=q_error,
        results=results,
        form_state=form_state
        or {"values": {"sections": [], "duration": consent_schemas.DEFAULT_DURATION}, "errors": {}},
        saved=saved,
        page_error=page_error,
        pkt=_pkt,
        pkt_date=_pkt_date,
    )


async def _search(
    session: SessionDep, actor: Actor, loc: str, q: str, *, count: bool
) -> tuple[list[consent_schemas.GranteeOut] | None, str | None]:
    """Grantee results for `q`, or a field error. `count` draws on the search
    budget; a re-render after a failed grant re-shows results without it."""
    if not q:
        return None, None
    try:
        cleaned = consent_schemas.clean_query(q)
    except PydanticCustomError as exc:
        return None, translate(exc.message_template, loc, **(exc.context or {}))
    if count:
        await check_consent_search_rate(actor)
    return await consent_service.search_grantees(session, actor, cleaned), None


async def _access_failure(
    request: Request,
    loc: str,
    session: SessionDep,
    actor: Actor,
    exc: Exception,
    *,
    values: dict[str, Any],
    q: str = "",
) -> Response:
    """Map a failed grant (`values` set) or a failed change/revoke (`values`
    empty) onto the re-rendered page (spec §4.12)."""
    await session.rollback()
    results, _ = await _search(session, actor, loc, q, count=False) if values else (None, None)

    async def page(code: int, errors: dict[str, str] | None = None, **kw: Any) -> Response:
        state = {"values": values, "errors": errors or {}} if values else None
        return await _render_access(
            request,
            loc,
            session,
            actor,
            status_code=code,
            q=q,
            results=results,
            form_state=state,
            **kw,
        )

    if isinstance(exc, ValidationError):
        errors: dict[str, str] = {}
        for err in exc.errors():
            field = str(err["loc"][0]) if err["loc"] else "grantee"
            # The id fields come from the chosen radio, so they report on it.
            field = "grantee" if field in ("doctor_id", "clinic_id") else field
            msg = err["msg"] if str(err["msg"]).startswith("errors.") else "errors.choice_invalid"
            errors.setdefault(field, translate(msg, loc))
        if not values:
            return await page(422, page_error=next(iter(errors.values())))
        return await page(422, errors)
    if isinstance(exc, ValidationFailed):
        fields = exc.details.get("fields", {})
        return await page(422, {k: translate(v, loc) for k, v in fields.items()})
    if isinstance(exc, NotFound):
        if values:  # the chosen grantee isn't grantable — unknown ≡ unverified (BL-01)
            return await page(404, {"grantee": translate("errors.grantee_not_found", loc)})
        return await page(404, page_error=translate("errors.not_found", loc))
    if isinstance(exc, DuplicateEntry):
        return await page(409, {"grantee": translate("errors.already_granted", loc)})
    if isinstance(exc, RateLimited):
        return await page(429, page_error=translate("errors.rate_limited", loc))
    return await page(500, page_error=translate("errors.internal", loc))


@router.get("/{locale}/patient/access", response_class=HTMLResponse)
async def patient_access_page(
    request: Request,
    locale: str,
    session: SessionDep,
    actor: OptionalActorDep,
    saved: str | None = None,
    q: str = "",
) -> Response:
    if (redirect := _patient_page_guard(request, locale, actor, "access")) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    banner = translate(_ACCESS_SAVED[saved], loc) if saved in _ACCESS_SAVED else None
    q = q[:200]
    try:
        results, q_error = await _search(session, actor, loc, q, count=True)
    except RateLimited:
        return await _render_access(
            request,
            loc,
            session,
            actor,
            status_code=429,
            q=q,
            page_error=translate("errors.rate_limited", loc),
        )
    except NotFound:
        return RedirectResponse(f"/{loc}/onboarding", status_code=303)
    return await _render_access(
        request,
        loc,
        session,
        actor,
        status_code=422 if q_error else 200,
        q=q,
        q_error=q_error,
        results=results,
        saved=banner,
    )


@router.post("/{locale}/patient/access")
async def patient_access_grant_submit(
    request: Request, locale: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    if (redirect := _patient_page_guard(request, locale, actor, "access")) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    form = await request.form()
    values: dict[str, Any] = {
        "grantee": str(form.get("grantee", "")),
        "sections": [str(s) for s in form.getlist("sections")],
        "duration": str(form.get("duration", "")),
    }
    q = str(form.get("q", ""))[:200]

    try:
        await check_consent_write_rate(actor)
        grantee = _parse_grantee(values["grantee"])
        if grantee is None:
            raise ValidationFailed({"fields": {"grantee": "errors.choice_invalid"}})
        kind, grantee_id = grantee
        body = consent_schemas.GrantCreate.model_validate(
            {
                f"{kind}_id": grantee_id,
                "sections": values["sections"],
                "duration": values["duration"],
            }
        )
        _, created = await consent_service.grant_consent(
            session,
            actor,
            sections=body.sections,
            duration=body.duration,
            doctor_id=body.doctor_id,
            clinic_id=body.clinic_id,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception as exc:  # noqa: BLE001 — every outcome is mapped to the page
        return await _access_failure(request, loc, session, actor, exc, values=values, q=q)
    saved = "granted" if created else "exists"  # BL-05: an active grant is returned as is
    return RedirectResponse(f"/{loc}/patient/access?saved={saved}", status_code=303)


@router.post("/{locale}/patient/access/{grant_id}/sections")
async def patient_access_sections_submit(
    request: Request, locale: str, grant_id: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    if (redirect := _patient_page_guard(request, locale, actor, "access")) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    parsed_id = _parse_entry_id(grant_id)
    form = await request.form()

    try:
        if parsed_id is None:
            raise NotFound()  # a malformed id is NotFound on the web, not a raw 422
        await check_consent_write_rate(actor)
        body = consent_schemas.SectionsUpdate.model_validate(
            {"sections": [str(s) for s in form.getlist("sections")]}
        )
        await consent_service.change_sections(
            session,
            actor,
            parsed_id,
            sections=body.sections,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception as exc:  # noqa: BLE001 — every outcome is mapped to the page
        return await _access_failure(request, loc, session, actor, exc, values={})
    return RedirectResponse(f"/{loc}/patient/access?saved=changed", status_code=303)


@router.post("/{locale}/patient/access/{grant_id}/revoke")
async def patient_access_revoke_submit(
    request: Request, locale: str, grant_id: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    if (redirect := _patient_page_guard(request, locale, actor, "access")) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    parsed_id = _parse_entry_id(grant_id)

    try:
        if parsed_id is None:
            # A malformed id is NotFound on the web, not a raw 422.
            raise NotFound()
        await check_consent_write_rate(actor)
        await consent_service.revoke_grant(
            session, actor, parsed_id, user_agent=request.headers.get("user-agent")
        )
    except Exception as exc:  # noqa: BLE001 — every outcome is mapped to the page
        return await _access_failure(request, loc, session, actor, exc, values={})
    return RedirectResponse(f"/{loc}/patient/access?saved=revoked", status_code=303)


# ── Doctor passport lookup (D2, BL-07, BL-09, BL-10) ─────────────────────
def _doctor_nav(loc: str, *, current: str, verified: bool = True) -> list[dict[str, Any]]:
    """A pending doctor gets no passport lookup (FR3); every doctor gets the
    schedule (D7)."""
    items = [
        {
            "href": f"/{loc}/doctor",
            "label": translate("nav.dashboard", loc),
            "current": current == "dashboard",
        },
    ]
    if verified:
        items.append(
            {
                "href": f"/{loc}/doctor/passport-lookup",
                "label": translate("nav.lookup", loc),
                "current": current == "lookup",
            }
        )
    items.append(
        {
            "href": f"/{loc}/doctor/schedule",
            "label": translate("nav.schedule", loc),
            "current": current == "schedule",
        }
    )
    return items


def _doctor_page_guard(request: Request, locale: str, actor: Actor | None) -> Response | None:
    """Mirrors `_patient_page_guard`. An unverified doctor is sent to their
    dashboard, where the FR3 banner explains why — no patient-data page at all."""
    loc = normalise_locale(locale)
    if actor is None:
        return RedirectResponse(f"/{loc}/login?next=/{loc}/doctor/passport-lookup", status_code=303)
    if actor.role != UserRole.DOCTOR.value:
        return RedirectResponse(f"/{loc}/{_area(actor.role)}", status_code=303)
    if not actor.onboarding_complete:
        return RedirectResponse(f"/{loc}/onboarding", status_code=303)
    if not actor.is_verified_doctor:
        return RedirectResponse(f"/{loc}/doctor", status_code=303)
    return None


def _render_lookup(
    request: Request,
    loc: str,
    *,
    status_code: int = 200,
    value: str = "",
    error: str | None = None,
) -> Response:
    return _render(
        request,
        "doctor/lookup.html",
        loc,
        status_code=status_code,
        path_suffix="/doctor/passport-lookup",
        crumb=translate("auth.role.doctor", loc),
        page_title=translate("lookup.title", loc),
        nav_items=_doctor_nav(loc, current="lookup"),
        values={"passport_no": value},
        errors={"passport_no": error} if error else {},
    )


@router.get("/{locale}/doctor/passport-lookup", response_class=HTMLResponse)
async def doctor_lookup_page(request: Request, locale: str, actor: OptionalActorDep) -> Response:
    if (redirect := _doctor_page_guard(request, locale, actor)) is not None:
        return redirect
    return _render_lookup(request, normalise_locale(locale))


@router.post("/{locale}/doctor/passport-lookup")
async def doctor_lookup_submit(
    request: Request,
    locale: str,
    session: SessionDep,
    actor: OptionalActorDep,
    passport_no: Annotated[str, Form()] = "",
) -> Response:
    if (redirect := _doctor_page_guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    if not passport_no.strip():
        return _render_lookup(
            request, loc, status_code=422, error=translate("errors.text_required", loc)
        )
    try:
        patient = await consent_service.find_patient_for_doctor(session, actor, passport_no)
    except NotFound:
        # D2 — one response for "no such patient", "never granted", "revoked"
        # and "expired".
        return _render_lookup(
            request,
            loc,
            status_code=404,
            value=passport_no,
            error=translate("lookup.not_found", loc),
        )
    # The patient's id, not the passport number, goes in the URL: paths reach
    # the access log, and a passport number must never be logged (AC-12).
    return RedirectResponse(f"/{loc}/doctor/patient/{patient.id}", status_code=303)


@router.get("/{locale}/doctor/patient/{patient_id}", response_class=HTMLResponse)
async def doctor_patient_page(
    request: Request, locale: str, patient_id: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    if (redirect := _doctor_page_guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    parsed_id = _parse_entry_id(patient_id)
    try:
        if parsed_id is None:
            raise NotFound()
        # Through the consent gateway, re-checked on every view (BL-07): only the
        # shared sections are read, and each read is logged (P9 BL-17, BL-18).
        view = await consent_gateway.read_sections(
            session, actor, parsed_id, purpose="doctor.patient_view"
        )
    except NotFound:  # includes ConsentDenied — one D2 response for every refusal
        return _render_lookup(
            request, loc, status_code=404, error=translate("lookup.not_found", loc)
        )
    basics = view.sections["basics"]
    if basics is not None:
        age = age_in_years(basics["date_of_birth"], dbtypes.today_pk())
        basics = {
            **basics,
            "age": str(age) if age is not None else "—",
            "gender": gender_label(basics["gender"], loc),
        }
    return _render(
        request,
        "doctor/patient.html",
        loc,
        crumb=translate("auth.role.doctor", loc),
        page_title=translate("doctor_patient.title", loc),
        nav_items=_doctor_nav(loc, current="lookup"),
        passport_no=view.passport_no,
        sections=consent_schemas.SECTIONS,
        shared={**view.sections, "basics": basics},
    )


# ── Doctor schedule (D7) ─────────────────────────────────────────────────
# Any doctor, verified or pending (spec §3, decision 6). The role gate is the
# guard below plus `DoctorDep` semantics; never `VerifiedDoctorDep`.
_SCHEDULE_SAVED = (
    "slot_added",
    "slot_updated",
    "slot_removed",
    "time_off_added",
    "extra_added",
    "exception_removed",
    "hospitals_added",
)
_SLOT_FIELDS = ("clinic_id", "start_time", "end_time", "slot_minutes", "max_tokens")
_TIME_OFF_FIELDS = ("exception_date", "clinic_id", "start_time", "end_time", "emergency", "reason")
_EXTRA_FIELDS = ("exception_date", "clinic_id", "start_time", "end_time", "max_tokens", "reason")
_CLINIC_COLOURS = 4  # .clinic-c0 … .clinic-c3 in app.css, all design tokens


def _doctor_schedule_guard(request: Request, locale: str, actor: Actor | None) -> Response | None:
    """Like `_doctor_page_guard`, but an unverified doctor is let through: the
    schedule exposes no patient data (spec §3)."""
    loc = normalise_locale(locale)
    if actor is None:
        return RedirectResponse(f"/{loc}/login?next=/{loc}/doctor/schedule", status_code=303)
    if actor.role != UserRole.DOCTOR.value:
        return RedirectResponse(f"/{loc}/{_area(actor.role)}", status_code=303)
    if not actor.onboarding_complete:
        return RedirectResponse(f"/{loc}/onboarding", status_code=303)
    return None


def _form_values(form: Any, fields: tuple[str, ...]) -> dict[str, str]:
    """Only the named fields are read; a missing field is "" so the schema
    reports its own i18n key instead of pydantic's English."""
    return {f: str(form.get(f, "")) for f in fields}


def _week_start(raw: str | None, today: date) -> date:
    """The first of the seven days shown. By default the view starts TODAY and
    runs a week forward, so late in a calendar week it still shows the coming
    days instead of a row of past ones. `?week=` starts it on that date."""
    try:
        return date.fromisoformat(raw) if raw else today
    except ValueError:
        return today


def _weekday_label(loc: str, weekday: int) -> str:
    return translate(f"weekday.{weekday}", loc)


def _hhmm(value: Any) -> str:
    return value.strftime("%H:%M") if value else ""


def _conflict_message(loc: str, exc: ScheduleConflict) -> str:
    d = exc.details
    day = _weekday_label(loc, int(d.get("day", 0)))
    if d.get("date"):
        day = f"{day} {d['date']}"
    clinic = d.get("clinic", "")
    if d.get("clinic_inactive"):
        clinic = f"{clinic} ({translate('schedule.inactive_clinic', loc)})"
    return translate(exc.message_key, loc, day=day, start=d["start"], end=d["end"], clinic=clinic)


def _slot_prefill(values: dict[str, str]) -> dict[str, str]:
    """R13: a blank cap is shown as the calculated value after a failed submit."""
    if values.get("max_tokens", "").strip():
        return values
    try:
        start = schedule_schemas.parse_time(values.get("start_time", ""))
        end = schedule_schemas.parse_time(values.get("end_time", ""))
        minutes = schedule_schemas.parse_slot_minutes(values.get("slot_minutes", ""))
    except PydanticCustomError:
        return values
    if end <= start or schedule_schemas.minutes_between(start, end) < minutes:
        return values
    return {**values, "max_tokens": str(schedule_schemas.calc_max_tokens(start, end, minutes))}


async def _render_schedule(
    request: Request,
    loc: str,
    session: SessionDep,
    actor: Actor,
    *,
    status_code: int = 200,
    form_state: dict[str, Any] | None = None,
    saved: str | None = None,
    page_error: str | None = None,
    week: str | None = None,
    clinic: str | None = None,
    edit: str | None = None,
) -> Response:
    doctor = await schedule_service.my_doctor(session, actor)
    loaded = await schedule_service.load(session, doctor)
    today = loaded.today
    week_start = _week_start(week, today)
    week_end = week_start + timedelta(days=6)

    # Clinics: every affiliated clinic for names and colours; live ones for forms.
    clinics = sorted(loaded.clinics.values(), key=lambda c: c.name)
    colour = {c.id: f"clinic-c{i % _CLINIC_COLOURS}" for i, c in enumerate(clinics)}
    live_clinic_ids = {
        a.clinic_id for a in loaded.affiliations.values() if loaded.affiliation_live(a)
    }
    live_clinics = [c for c in clinics if c.id in live_clinic_ids]
    clinic_filter = next((c.id for c in clinics if str(c.id) == clinic), None)

    # Your hospitals: one line per clinic — linked (an active link at an active
    # clinic) or not (ended link, or a closed clinic).
    hospitals = [
        {
            "name": c.name,
            "colour": colour[c.id],
            "state": "linked" if c.id in live_clinic_ids else "ended",
        }
        for c in clinics
    ]
    # Every active clinic the doctor is not actively linked to; re-adding one
    # with an ended link reactivates it (identity.service.link_doctor_clinics).
    addable = (
        await session.execute(select(Clinic).where(Clinic.status == "active").order_by(Clinic.name))
    ).scalars()
    hospital_options = [
        {"value": str(c.id), "label": f"{c.name} — {c.city}" if c.city else c.name}
        for c in addable
        if c.id not in live_clinic_ids
    ]

    # The week view reads availability only through the service (spec §6).
    windows: list[schedule_engine.EffectiveWindow] = []
    if week_end >= today:
        windows = await schedule_service.get_effective_availability(
            session, doctor.id, max(week_start, today), week_end, clinic_filter
        )
    days = []
    for offset in range(7):
        day = week_start + timedelta(days=offset)
        blocks = [
            {
                "clinic": loaded.clinics[w.clinic_id].name,
                "colour": colour[w.clinic_id],
                "start": _hhmm(w.start),
                "end": _hhmm(w.end),
                "extra": w.is_extra,
                "capacity": translate(
                    "schedule.capacity_inherited" if w.inherited else "schedule.capacity",
                    loc,
                    count=w.max_tokens,
                    day=_weekday_label(loc, w.anchor_weekday),
                ),
            }
            for w in windows
            if w.day == day
        ]
        days.append(
            {
                "label": _weekday_label(loc, day.weekday()),
                "date": day.isoformat(),
                "past": day < today,
                "today": day == today,
                "blocks": blocks,
            }
        )

    live_slots = sorted(
        (r for r in loaded.slot_rows.values() if r.status == schedule_service.LIVE),
        key=lambda r: (r.weekday, r.start_time),
    )
    locked = await schedule_service.booked_slot_ids(session, [r.id for r in live_slots])
    slots = []
    for row in live_slots:
        slot_clinic = loaded.clinic_of(row)
        slots.append(
            {
                "id": str(row.id),
                "day": _weekday_label(loc, row.weekday),
                "start": _hhmm(row.start_time),
                "end": _hhmm(row.end_time),
                "clinic": slot_clinic.name,
                "colour": colour[slot_clinic.id],
                "inactive": not loaded.affiliation_live(loaded.affiliations[row.affiliation_id]),
                "locked": row.id in locked,
                "meta": translate(
                    "schedule.slot_meta", loc, minutes=row.slot_minutes, count=row.max_tokens
                ),
                "calc": schedule_schemas.calc_max_tokens(
                    row.start_time, row.end_time, row.slot_minutes
                ),
                "values": {
                    "clinic_id": str(slot_clinic.id),
                    "weekday": str(row.weekday),
                    "start_time": _hhmm(row.start_time),
                    "end_time": _hhmm(row.end_time),
                    "slot_minutes": str(row.slot_minutes),
                    "max_tokens": str(row.max_tokens),
                },
            }
        )

    upcoming = sorted(
        loaded.exception_rows.values(),
        key=lambda e: (e.exception_date, e.start_time or time.min),
    )
    exceptions = []
    for row in upcoming:
        anchor = loaded.slot_rows[row.schedule_id]
        anchor_clinic = loaded.clinic_of(anchor)
        is_extra = row.kind.value == schedule_engine.EXTRA
        whole = row.start_time is None
        entry = {
            "id": str(row.id),
            "date": row.exception_date.isoformat(),
            "day": _weekday_label(loc, row.exception_date.weekday()),
            "kind": translate(f"schedule.kind.{row.kind.value}", loc),
            "extra": is_extra,
            "window": translate("schedule.whole_slot", loc)
            if whole
            else f"{_hhmm(row.start_time)}–{_hhmm(row.end_time)}",
            "clinic": anchor_clinic.name,
            "colour": colour[anchor_clinic.id],
            "covers": None
            if is_extra
            else translate(
                "schedule.covers_slot",
                loc,
                day=_weekday_label(loc, anchor.weekday),
                start=_hhmm(anchor.start_time),
                end=_hhmm(anchor.end_time),
            ),
            "reason": row.reason,
            "capacity": None,
        }
        if is_extra:
            inherited = row.max_tokens is None
            entry["capacity"] = translate(
                "schedule.capacity_inherited" if inherited else "schedule.capacity",
                loc,
                count=anchor.max_tokens if inherited else row.max_tokens,
                day=_weekday_label(loc, anchor.weekday),
            )
        exceptions.append(entry)

    inherit_hints = []
    for c in live_clinics:
        anchor_value = schedule_engine.choose_anchor(c.id, loaded.slots)
        if anchor_value is not None:
            hint = translate(
                "schedule.extra_max_tokens_inherit",
                loc,
                count=anchor_value.max_tokens,
                day=_weekday_label(loc, anchor_value.weekday),
            )
            inherit_hints.append(f"{c.name}: {hint}")

    user = await session.get(Profile, actor.user_id)
    full_name = (user.full_name if user else None) or doctor.full_name
    week_param = f"&clinic={clinic_filter}" if clinic_filter else ""
    edit_id = edit if edit and any(s["id"] == edit and not s["locked"] for s in slots) else None

    return _render(
        request,
        "doctor/schedule.html",
        loc,
        status_code=status_code,
        nav_items=_doctor_nav(loc, current="schedule", verified=actor.is_verified_doctor),
        crumb=translate(ROLE_LABEL["doctor"], loc),
        page_title=translate("schedule.title", loc),
        full_name=full_name,
        initials=_initials(full_name),
        role_label=translate(ROLE_LABEL["doctor"], loc),
        today=today.isoformat(),
        week_label=translate("schedule.week_of", loc, date=week_start.isoformat()),
        prev_week=f"/{loc}/doctor/schedule?week={(week_start - timedelta(days=7)).isoformat()}{week_param}",
        next_week=f"/{loc}/doctor/schedule?week={(week_start + timedelta(days=7)).isoformat()}{week_param}",
        this_week=f"/{loc}/doctor/schedule?week={today.isoformat()}{week_param}",
        week_value=week_start.isoformat(),
        days=days,
        legend=[{"name": c.name, "colour": colour[c.id]} for c in clinics],
        clinic_filter=str(clinic_filter) if clinic_filter else "",
        filter_options=[{"value": str(c.id), "label": c.name} for c in clinics],
        clinic_options=[{"value": str(c.id), "label": c.name} for c in live_clinics],
        weekday_options=[{"value": str(i), "label": _weekday_label(loc, i)} for i in range(7)],
        minutes_options=[
            {"value": str(m), "label": translate("schedule.minutes_option", loc, n=m)}
            for m in schedule_schemas.SLOT_MINUTES_CHOICES
        ],
        slots=slots,
        exceptions=exceptions,
        inherit_hints=inherit_hints,
        can_add=bool(live_clinics),
        hospitals=hospitals,
        hospital_options=hospital_options,
        edit_id=edit_id,
        form_state=form_state,
        saved=translate(f"schedule.saved.{saved}", loc) if saved in _SCHEDULE_SAVED else None,
        page_error=page_error,
        max_tokens_max=schedule_schemas.MAX_TOKENS_MAX,
    )


async def _schedule_failure(
    request: Request,
    loc: str,
    session: SessionDep,
    actor: Actor,
    exc: Exception,
    *,
    form: str,
    values: dict[str, Any],
) -> Response:
    """Map a failed schedule write onto the re-rendered page."""
    await session.rollback()

    def state(errors: dict[str, str], banner: str | None = None) -> dict[str, Any]:
        if form.startswith("slot"):
            shown = _slot_prefill({k: v for k, v in values.items() if isinstance(v, str)})
            shown_values = {**values, **shown}
        else:
            shown_values = values
        return {"form": form, "values": shown_values, "errors": errors, "banner": banner}

    async def page(code: int, **kw: Any) -> Response:
        edit = (
            form.removeprefix("slot-") if form.startswith("slot-") and form != "slot-new" else None
        )
        return await _render_schedule(
            request, loc, session, actor, status_code=code, edit=edit, **kw
        )

    if isinstance(exc, ValidationError):
        return await page(422, form_state=state(_field_errors(exc, loc)))
    if isinstance(exc, ValidationFailed):
        fields = {k: translate(v, loc) for k, v in exc.details.get("fields", {}).items()}
        banner = fields.pop("__all__", None)
        return await page(422, form_state=state(fields, banner))
    if isinstance(exc, ScheduleConflict):
        return await page(409, form_state=state({}, _conflict_message(loc, exc)))
    if isinstance(exc, ListFull):
        banner = translate(exc.message_key, loc, **message_params(exc))
        return await page(422, form_state=state({}, banner))
    if isinstance(exc, NotFound):
        return await page(404, page_error=translate("errors.not_found", loc))
    if isinstance(exc, RateLimited):
        return await page(429, page_error=translate("errors.rate_limited", loc))
    return await page(500, page_error=translate("errors.internal", loc))


def _schedule_saved(loc: str, key: str, anchor: str) -> Response:
    return RedirectResponse(f"/{loc}/doctor/schedule?saved={key}#{anchor}", status_code=303)


@router.get("/{locale}/doctor/schedule", response_class=HTMLResponse)
async def doctor_schedule_page(
    request: Request,
    locale: str,
    session: SessionDep,
    actor: OptionalActorDep,
    week: str | None = None,
    clinic: str | None = None,
    saved: str | None = None,
    edit: str | None = None,
) -> Response:
    if (redirect := _doctor_schedule_guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    return await _render_schedule(
        request,
        normalise_locale(locale),
        session,
        actor,
        week=week,
        clinic=clinic,
        saved=saved,
        edit=edit,
    )


@router.post("/{locale}/doctor/schedule/slots")
async def doctor_schedule_add_slots(
    request: Request, locale: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    if (redirect := _doctor_schedule_guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    form = await request.form()
    values: dict[str, Any] = {
        **_form_values(form, _SLOT_FIELDS),
        "weekdays": [str(v) for v in form.getlist("weekdays")],
    }
    try:
        await check_schedule_write_rate(actor)
        body = schedule_schemas.SlotCreate.model_validate(values)
        await schedule_service.add_slots(
            session, actor, body, user_agent=request.headers.get("user-agent")
        )
    except Exception as exc:  # noqa: BLE001 — every outcome is mapped onto the page
        return await _schedule_failure(
            request, loc, session, actor, exc, form="slot-new", values=values
        )
    return _schedule_saved(loc, "slot_added", "slots")


@router.post("/{locale}/doctor/schedule/slots/{slot_id}")
async def doctor_schedule_edit_slot(
    request: Request, locale: str, slot_id: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    if (redirect := _doctor_schedule_guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    parsed = _parse_entry_id(slot_id)
    if parsed is None:  # a malformed id is NotFound on the web, not a raw 422
        return await _schedule_failure(
            request, loc, session, actor, NotFound(), form="slots", values={}
        )
    form = await request.form()
    values: dict[str, Any] = _form_values(form, (*_SLOT_FIELDS, "weekday"))
    try:
        await check_schedule_write_rate(actor)
        body = schedule_schemas.SlotUpdate.model_validate(values)
        await schedule_service.update_slot(
            session, actor, parsed, body, user_agent=request.headers.get("user-agent")
        )
    except Exception as exc:  # noqa: BLE001 — every outcome is mapped onto the page
        return await _schedule_failure(
            request, loc, session, actor, exc, form=f"slot-{parsed}", values=values
        )
    return _schedule_saved(loc, "slot_updated", "slots")


@router.post("/{locale}/doctor/schedule/slots/{slot_id}/remove")
async def doctor_schedule_remove_slot(
    request: Request, locale: str, slot_id: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    if (redirect := _doctor_schedule_guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    parsed = _parse_entry_id(slot_id)
    try:
        if parsed is None:
            raise NotFound()
        await check_schedule_write_rate(actor)
        await schedule_service.remove_slot(
            session, actor, parsed, user_agent=request.headers.get("user-agent")
        )
    except Exception as exc:  # noqa: BLE001 — every outcome is mapped onto the page
        return await _schedule_failure(request, loc, session, actor, exc, form="slots", values={})
    return _schedule_saved(loc, "slot_removed", "slots")


@router.post("/{locale}/doctor/schedule/time-off")
async def doctor_schedule_add_time_off(
    request: Request, locale: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    if (redirect := _doctor_schedule_guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    values: dict[str, Any] = _form_values(await request.form(), _TIME_OFF_FIELDS)
    try:
        await check_schedule_write_rate(actor)
        body = schedule_schemas.TimeOffCreate.model_validate(values)
        await schedule_service.add_time_off(
            session, actor, body, user_agent=request.headers.get("user-agent")
        )
    except Exception as exc:  # noqa: BLE001 — every outcome is mapped onto the page
        return await _schedule_failure(
            request, loc, session, actor, exc, form="time-off", values=values
        )
    return _schedule_saved(loc, "time_off_added", "exceptions")


@router.post("/{locale}/doctor/schedule/extra")
async def doctor_schedule_add_extra(
    request: Request, locale: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    if (redirect := _doctor_schedule_guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    values: dict[str, Any] = _form_values(await request.form(), _EXTRA_FIELDS)
    try:
        await check_schedule_write_rate(actor)
        body = schedule_schemas.ExtraCreate.model_validate(values)
        await schedule_service.add_extra(
            session, actor, body, user_agent=request.headers.get("user-agent")
        )
    except Exception as exc:  # noqa: BLE001 — every outcome is mapped onto the page
        return await _schedule_failure(
            request, loc, session, actor, exc, form="extra", values=values
        )
    return _schedule_saved(loc, "extra_added", "exceptions")


@router.post("/{locale}/doctor/schedule/hospitals")
async def doctor_schedule_add_hospitals(
    request: Request, locale: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    """A doctor links themselves to more hospitals they work at. Each link is
    pending: usable for the schedule, never for patient access (consent counts
    active links only)."""
    if (redirect := _doctor_schedule_guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    raw = [str(v) for v in (await request.form()).getlist("clinic_ids")]
    values: dict[str, Any] = {"clinic_ids": raw}
    try:
        await check_schedule_write_rate(actor)
        chosen = _chosen_clinics(raw)
        if chosen is None:
            raise ValidationFailed({"fields": {"clinic_ids": "errors.clinic_unknown"}})
        await service.add_doctor_clinics(
            session,
            actor.user_id,
            chosen,
            ip=actor.ip_address,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception as exc:  # noqa: BLE001 — every outcome is mapped onto the page
        return await _schedule_failure(
            request, loc, session, actor, exc, form="hospitals", values=values
        )
    return _schedule_saved(loc, "hospitals_added", "hospitals")


@router.post("/{locale}/doctor/schedule/exceptions/{exception_id}/remove")
async def doctor_schedule_remove_exception(
    request: Request, locale: str, exception_id: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    if (redirect := _doctor_schedule_guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    parsed = _parse_entry_id(exception_id)
    try:
        if parsed is None:
            raise NotFound()
        await check_schedule_write_rate(actor)
        await schedule_service.remove_exception(
            session, actor, parsed, user_agent=request.headers.get("user-agent")
        )
    except Exception as exc:  # noqa: BLE001 — every outcome is mapped onto the page
        return await _schedule_failure(
            request, loc, session, actor, exc, form="exceptions", values={}
        )
    return _schedule_saved(loc, "exception_removed", "exceptions")
