"""Actor resolution and role gates (TDD 7.2).

Exactly four dependencies exist, and **no endpoint anywhere may perform an
ad-hoc role check**. Centralising it here is what makes the authorisation model
reviewable in one place instead of twenty.

`require_verified_doctor` queries the database on every request rather than
trusting a token claim, so an admin revoking verification takes effect on the
next call (SPEC AC-08, BL-03).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .cache import cache, ratelimit_key, revoked_session_key, tokens_revoked_before_key
from .db.models import (
    AccountStatus,
    ClinicStaff,
    Doctor,
    DoctorAffiliation,
    Patient,
    Profile,
    UserRole,
    VerificationStatus,
)
from .db.session import get_session
from .errors import CrossOriginRejected, Forbidden, RateLimited, Unauthenticated
from .identity.security import (
    CLOCK_SKEW_S,
    AccessClaims,
    InvalidToken,
    decode_supabase_access_token,
)
from .settings import settings

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@dataclass(frozen=True)
class Actor:
    """The resolved caller. Constructed once per request and passed explicitly.

    Never read from a global — an implicit current-user is how authorisation
    bugs hide.
    """

    user_id: uuid.UUID
    role: str
    clinic_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    is_verified_doctor: bool = False
    locale: str = "en"
    request_id: str = "-"
    ip_address: str | None = None
    onboarding_complete: bool = True


def access_token_max_age_s() -> int:
    return settings.access_token_minutes * 60 + CLOCK_SKEW_S


async def token_is_current(claims: AccessClaims) -> bool:
    """Local, DB-free checks on top of signature/expiry verification.

    1. Lifetime cap — an access token older than ACCESS_TOKEN_MINUTES is
       refused even if Supabase's own JWT expiry (a dashboard setting) is
       longer, so a copied token cannot outlive the session cookie.
    2. Logout revocation — `service.logout` records the logged-out session
       and an "issued before" epoch for the user; tokens they cover are dead
       immediately rather than at expiry.

    The markers live in the process cache, so on a multi-worker deployment
    they need the shared Redis-backed cache (same limitation as rate limits —
    see docs/auth_hardening.md). Refresh tokens are revoked server-side by
    Supabase regardless.
    """
    if claims.issued_at is None:
        return False
    now = int(time.time())
    if now - claims.issued_at > access_token_max_age_s():
        return False
    if claims.session_id and await cache.get(revoked_session_key(claims.session_id)):
        return False
    revoked_before = await cache.get(tokens_revoked_before_key(str(claims.user_id)))
    return not (revoked_before is not None and claims.issued_at < revoked_before)


async def revoke_access_tokens(claims: AccessClaims) -> None:
    """Kill this session's access tokens, and every older token of the user."""
    ttl = access_token_max_age_s() + 60
    if claims.session_id:
        await cache.set(revoked_session_key(claims.session_id), True, ttl)
    await cache.set(tokens_revoked_before_key(str(claims.user_id)), int(time.time()), ttl)


async def _load_actor(request: Request, session: AsyncSession) -> Actor | None:
    token = request.cookies.get(settings.access_cookie_name)
    if not token:
        return None
    try:
        claims = await decode_supabase_access_token(token)
    except InvalidToken:
        return None
    if not await token_is_current(claims):
        return None

    user = await session.get(Profile, claims.user_id)
    if user is None or user.status != AccountStatus.ACTIVE:
        return None

    clinic_ids: set[uuid.UUID] = set()
    is_verified_doctor = False
    # Default True (password-registered users always have a role record the
    # instant `register()` commits); only OAuth can leave this False.
    onboarding_complete = True

    if user.role == UserRole.PATIENT:
        patient = (
            await session.execute(select(Patient.id).where(Patient.user_id == user.id))
        ).scalar_one_or_none()
        onboarding_complete = patient is not None
    elif user.role == UserRole.DOCTOR:
        doctor = (
            await session.execute(select(Doctor).where(Doctor.user_id == user.id))
        ).scalar_one_or_none()
        onboarding_complete = doctor is not None
        if doctor is not None:
            # Live read — never from the token.
            is_verified_doctor = doctor.verification_status == VerificationStatus.VERIFIED
            aff_rows = await session.execute(
                select(DoctorAffiliation.clinic_id).where(
                    DoctorAffiliation.doctor_id == doctor.id,
                    DoctorAffiliation.status == "active",
                )
            )
            clinic_ids.update(aff_rows.scalars().all())
    elif user.role == UserRole.CLINIC_ADMIN:
        rows = await session.execute(
            select(ClinicStaff.clinic_id).where(ClinicStaff.user_id == user.id)
        )
        clinic_ids.update(rows.scalars().all())
        onboarding_complete = bool(clinic_ids)

    return Actor(
        user_id=user.id,
        role=user.role.value,
        clinic_ids=frozenset(clinic_ids),
        is_verified_doctor=is_verified_doctor,
        locale=user.preferred_locale.value,
        request_id=getattr(request.state, "request_id", "-"),
        ip_address=request.client.host if request.client else None,
        onboarding_complete=onboarding_complete,
    )


async def current_actor(request: Request, session: SessionDep) -> Actor:
    actor = await _load_actor(request, session)
    if actor is None:
        raise Unauthenticated()
    request.state.actor = actor
    return actor


async def optional_actor(request: Request, session: SessionDep) -> Actor | None:
    """For pages that render differently when signed in but do not require it."""
    actor = await _load_actor(request, session)
    request.state.actor = actor
    return actor


def require_role(*roles: str) -> Callable[..., Awaitable[Actor]]:
    async def dependency(actor: Annotated[Actor, Depends(current_actor)]) -> Actor:
        if actor.role not in roles:
            raise Forbidden({"required_roles": list(roles)})
        return actor

    return dependency


async def require_verified_doctor(
    actor: Annotated[Actor, Depends(current_actor)],
) -> Actor:
    """FR3 — an unverified doctor reaches no patient data at all.

    There is no partial access, no read-only mode, and no preview.
    """
    if actor.role != UserRole.DOCTOR.value:
        raise Forbidden({"required_roles": ["doctor"]})
    if not actor.is_verified_doctor:
        raise Forbidden({"reason": "doctor_unverified"})
    return actor


# Hoisted to one module-level callable so FastAPI's per-request dependency
# cache resolves it once even when a route and its rate-limit dependency both
# ask for a patient.
_require_patient = require_role("patient")

ActorDep = Annotated[Actor, Depends(current_actor)]
OptionalActorDep = Annotated[Actor | None, Depends(optional_actor)]
PatientDep = Annotated[Actor, Depends(_require_patient)]
VerifiedDoctorDep = Annotated[Actor, Depends(require_verified_doctor)]
ClinicAdminDep = Annotated[Actor, Depends(require_role("admin"))]


# ── Same-origin check for credential forms (login CSRF, audit M6) ───────
def _origin_of(url: str) -> str | None:
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


def is_same_origin(request: Request) -> bool:
    """True unless the browser says this request came from another origin.

    Session cookies are SameSite=Strict, which already keeps authenticated
    state changes same-site — but login and registration are *unauthenticated*
    POSTs, and the response sets a fresh session cookie, so a hostile page
    could sign a victim into the attacker's account. Browsers send `Origin`
    on every POST (falling back to `Referer`); when either is present it must
    be this site. When both are absent the request did not come from a
    browser form, so there is no victim session to forge.
    """
    allowed = {o for o in (_origin_of(settings.public_base_url),) if o}
    host = request.headers.get("host")
    if host:
        allowed.add(f"{request.url.scheme}://{host}".lower())

    origin = request.headers.get("origin")
    if origin is not None:
        return origin.lower() in allowed  # includes the opaque "null" origin → refused
    referer = request.headers.get("referer")
    if referer is not None:
        return _origin_of(referer) in allowed
    return True


async def require_same_origin(request: Request) -> None:
    if not is_same_origin(request):
        raise CrossOriginRejected()


SameOrigin = Depends(require_same_origin)


# ── Rate limiting (TDD 7.7 — 5/min on auth endpoints) ────────────────────
# Per-process, keyed on the TCP peer address. Production limitations (no Redis
# in this project yet): counters are not shared across workers and reset on
# restart, and behind a reverse proxy every client shares the proxy's address
# unless uvicorn is started with --proxy-headers/--forwarded-allow-ips for
# that proxy. See docs/auth_hardening.md. Account lockout does NOT depend on
# this — it is enforced atomically in the database (identity/service.py).
async def enforce_auth_rate_limit(request: Request) -> None:
    identifier = request.client.host if request.client else "unknown"
    count = await cache.incr(ratelimit_key("auth", identifier), 60)
    if count > settings.auth_rate_limit_per_minute:
        raise RateLimited(retry_after_s=60)


AuthRateLimit = Depends(enforce_auth_rate_limit)


# ── Patient-profile write budget (FR2, profile AC-20) ────────────────────
async def check_profile_write_rate(actor: Actor) -> None:
    """Counts one write attempt. Shared by the API dependency below and the
    web handlers, so both doors draw from the same per-patient window."""
    count = await cache.incr(ratelimit_key("profile_write", str(actor.user_id)), 60)
    if count > settings.profile_write_rate_limit_per_minute:
        raise RateLimited(retry_after_s=60)


async def enforce_profile_write_rate_limit(actor: PatientDep) -> None:
    """Depends on PatientDep so 401/403 surface before 429."""
    await check_profile_write_rate(actor)


ProfileWriteRateLimit = Depends(enforce_profile_write_rate_limit)


# ── Consent write budget (FR4) ───────────────────────────────────────────
async def check_consent_write_rate(actor: Actor) -> None:
    """Counts one grant/revoke attempt. Shared by the API dependency below and
    any web handler, so both doors draw from the same per-patient window."""
    count = await cache.incr(ratelimit_key("consent_write", str(actor.user_id)), 60)
    if count > settings.consent_write_rate_limit_per_minute:
        raise RateLimited(retry_after_s=60)


async def enforce_consent_write_rate_limit(actor: PatientDep) -> None:
    """Depends on PatientDep so 401/403 surface before 429."""
    await check_consent_write_rate(actor)


ConsentWriteRateLimit = Depends(enforce_consent_write_rate_limit)
