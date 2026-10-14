"""Actor resolution and role gates (TDD 7.2).

Exactly five dependencies exist, and **no endpoint anywhere may perform an
ad-hoc role check**. Centralising it here is what makes the authorisation model
reviewable in one place instead of twenty.

`require_verified_doctor` queries the database on every request rather than
trusting a token claim, so an admin revoking verification takes effect on the
next call (SPEC AC-08, BL-03).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .cache import cache, ratelimit_key
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
from .errors import Forbidden, RateLimited, Unauthenticated
from .identity.security import InvalidToken, decode_supabase_access_token
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


def remember_patient_id(session: AsyncSession, user_id: uuid.UUID, patient_id: uuid.UUID) -> None:
    """Keep a patient's id for the rest of this request's session. The id is derived from the
    verified user id every request (never trusted from a client), and a session lives for one
    request, so this saves repeated identical lookups — each one is a network round trip to the
    database — without changing who may see what."""
    session.info[("patient_id", user_id)] = patient_id


def remembered_patient_id(session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID | None:
    return session.info.get(("patient_id", user_id))


async def _load_actor(request: Request, session: AsyncSession) -> Actor | None:
    token = request.cookies.get(settings.access_cookie_name)
    if not token:
        return None
    try:
        claims = await decode_supabase_access_token(token)
    except InvalidToken:
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
        if patient is not None:
            remember_patient_id(session, user.id, patient)
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
# Any doctor, verified or pending (D7). Only for features that expose no
# patient data — the doctor's own schedule. Everything clinical stays behind
# `VerifiedDoctorDep`.
_require_doctor = require_role("doctor")

ActorDep = Annotated[Actor, Depends(current_actor)]
OptionalActorDep = Annotated[Actor | None, Depends(optional_actor)]
PatientDep = Annotated[Actor, Depends(_require_patient)]
VerifiedDoctorDep = Annotated[Actor, Depends(require_verified_doctor)]
DoctorDep = Annotated[Actor, Depends(_require_doctor)]
ClinicAdminDep = Annotated[Actor, Depends(require_role("admin"))]


# ── Rate limiting (TDD 7.7 — 5/min on auth endpoints) ────────────────────
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


# ── Grantee search budget (P9) ───────────────────────────────────────────
async def check_consent_search_rate(actor: Actor) -> None:
    """Counts one grantee search. Shared by the API and the access page."""
    count = await cache.incr(ratelimit_key("consent_search", str(actor.user_id)), 60)
    if count > settings.consent_search_rate_limit_per_minute:
        raise RateLimited(retry_after_s=60)


async def enforce_consent_search_rate_limit(actor: PatientDep) -> None:
    """Depends on PatientDep so 401/403 surface before 429."""
    await check_consent_search_rate(actor)


ConsentSearchRateLimit = Depends(enforce_consent_search_rate_limit)


# ── Doctor schedule write budget (D7) ────────────────────────────────────
async def check_schedule_write_rate(actor: Actor) -> None:
    """Counts one schedule write attempt by a doctor. Called by every schedule
    POST handler (D7 has no JSON API, so there is no dependency wrapper)."""
    count = await cache.incr(ratelimit_key("schedule_write", str(actor.user_id)), 60)
    if count > settings.schedule_write_rate_limit_per_minute:
        raise RateLimited(retry_after_s=60)


# ── Medical-document write budget (FR10) ─────────────────────────────────
async def check_document_write_rate(actor: Actor) -> None:
    """Counts one document write attempt (upload, process, confirm, cancel).
    Shared by the API dependency below and the web handlers. Processing calls
    a paid OCR model, so this is also the cost guard."""
    count = await cache.incr(ratelimit_key("document_write", str(actor.user_id)), 60)
    if count > settings.document_write_rate_limit_per_minute:
        raise RateLimited(retry_after_s=60)


# ── Appointment-booking write budget (P5) ────────────────────────────────
async def check_booking_write_rate(actor: Actor) -> None:
    """Counts one booking write attempt (book, reschedule, cancel). Shared by every
    web handler, so they all draw from one per-patient window."""
    count = await cache.incr(ratelimit_key("booking_write", str(actor.user_id)), 60)
    if count > settings.booking_write_rate_limit_per_minute:
        raise RateLimited(retry_after_s=60)


# ── Live-queue budgets (P6) ──────────────────────────────────────────────
async def check_queue_write_rate(actor: Actor) -> None:
    """Counts one front-desk queue action (check in, send in, seen, move, walk-in)."""
    count = await cache.incr(ratelimit_key("queue_write", str(actor.user_id)), 60)
    if count > settings.queue_write_rate_limit_per_minute:
        raise RateLimited(retry_after_s=60)


async def check_queue_lookup_rate(actor: Actor) -> None:
    """Counts one walk-in patient search. Tighter than the write budget on purpose."""
    count = await cache.incr(ratelimit_key("queue_lookup", str(actor.user_id)), 60)
    if count > settings.queue_lookup_rate_limit_per_minute:
        raise RateLimited(retry_after_s=60)


async def enforce_document_write_rate_limit(actor: PatientDep) -> None:
    """Depends on PatientDep so 401/403 surface before 429."""
    await check_document_write_rate(actor)


DocumentWriteRateLimit = Depends(enforce_document_write_rate_limit)


async def enforce_doctor_document_write_rate_limit(actor: VerifiedDoctorDep) -> None:
    """Same budget/mechanism as `DocumentWriteRateLimit` (`check_document_write_rate`),
    just gated by `VerifiedDoctorDep` instead of `PatientDep` for the
    doctor-generated document endpoint."""
    await check_document_write_rate(actor)


DoctorDocumentWriteRateLimit = Depends(enforce_doctor_document_write_rate_limit)
