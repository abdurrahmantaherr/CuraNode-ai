"""Actor resolution and role gates (TDD 7.2).

Exactly four dependencies exist, and **no endpoint anywhere may perform an
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

    if user.role == UserRole.DOCTOR:
        doctor = (
            await session.execute(select(Doctor).where(Doctor.user_id == user.id))
        ).scalar_one_or_none()
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

    return Actor(
        user_id=user.id,
        role=user.role.value,
        clinic_ids=frozenset(clinic_ids),
        is_verified_doctor=is_verified_doctor,
        locale=user.preferred_locale.value,
        request_id=getattr(request.state, "request_id", "-"),
        ip_address=request.client.host if request.client else None,
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


ActorDep = Annotated[Actor, Depends(current_actor)]
OptionalActorDep = Annotated[Actor | None, Depends(optional_actor)]
PatientDep = Annotated[Actor, Depends(require_role("patient"))]
VerifiedDoctorDep = Annotated[Actor, Depends(require_verified_doctor)]
ClinicAdminDep = Annotated[Actor, Depends(require_role("admin"))]


# ── Rate limiting (TDD 7.7 — 5/min on auth endpoints) ────────────────────
async def enforce_auth_rate_limit(request: Request) -> None:
    identifier = request.client.host if request.client else "unknown"
    count = await cache.incr(ratelimit_key("auth", identifier), 60)
    if count > settings.auth_rate_limit_per_minute:
        raise RateLimited(retry_after_s=60)


AuthRateLimit = Depends(enforce_auth_rate_limit)
