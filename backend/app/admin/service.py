"""Clinic-admin self-sign-up (D-VERIFY v1).

Deliberately self-contained rather than extending `identity/service.py`:
that module is the carefully-reasoned, already-reviewed home of patient and
doctor registration, and `identity/schemas.py`'s own docstring is explicit
that no registration model exposes a verified/privileged state. Admin
sign-up is privileged by construction (it can approve doctors), so it gets
its own small module, its own gate (the invite code), and duplicates the
~20 lines of Supabase-registration boilerplate rather than widen the shared
module's blast radius.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import writer as audit
from ..db.models import AccountStatus, ClinicInviteCode, ClinicStaff, LocaleCode, Profile, UserRole
from ..db.types import uuid7
from ..errors import ValidationFailed
from ..identity import security
from ..identity.schemas import SessionOut, landing_route_for
from ..settings import settings
from ..verification.codes import verify_code
from .schemas import AdminRegisterRequest


async def _user_by_email(session: AsyncSession, email: str) -> Profile | None:
    return (
        await session.execute(select(Profile).where(Profile.email == email.strip().lower()))
    ).scalar_one_or_none()


def _session_out(user: Profile, gotrue_session: Any) -> tuple[SessionOut, security.TokenPair]:
    locale = user.preferred_locale.value
    expires_at_ts = gotrue_session.expires_at or (
        int(datetime.now(UTC).timestamp()) + gotrue_session.expires_in
    )
    out = SessionOut(
        user_id=user.id,
        role="admin",
        full_name=user.full_name,
        locale=locale,  # type: ignore[arg-type]
        landing_route=landing_route_for("admin", locale),
        access_expires_at=datetime.fromtimestamp(expires_at_ts, tz=UTC),
    )
    pair = security.TokenPair(
        access_token=gotrue_session.access_token,
        refresh_token=gotrue_session.refresh_token,
    )
    return out, pair


async def register_admin(
    session: AsyncSession,
    body: AdminRegisterRequest,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[SessionOut, security.TokenPair | None]:
    existing = await _user_by_email(session, body.email)
    if existing is not None:
        # Same enumeration guard as identity/service.py::register — identical
        # shape, no row, no cookies, Supabase never called.
        return (
            SessionOut(
                user_id=uuid7(),
                role="admin",
                full_name=body.full_name,
                locale=body.preferred_locale,
                landing_route=landing_route_for("admin", body.preferred_locale),
                access_expires_at=datetime.now(UTC)
                + timedelta(minutes=settings.access_token_minutes),
            ),
            None,
        )

    invite = (
        await session.execute(
            select(ClinicInviteCode).where(
                ClinicInviteCode.clinic_id == body.clinic_id, ClinicInviteCode.is_active.is_(True)
            )
        )
    ).scalar_one_or_none()
    if invite is None or not verify_code(body.invite_code, invite.code_hash):
        raise ValidationFailed({"fields": {"invite_code": "errors.invite_code_invalid"}})

    client = await security.get_supabase_client()
    created = await client.auth.admin.create_user(
        {"email": body.email, "password": body.password, "email_confirm": True}
    )
    user_id = uuid.UUID(created.user.id)

    user = await session.get(Profile, user_id)
    if user is None:
        user = Profile(id=user_id)
        session.add(user)
    user.email = body.email
    user.role = UserRole.CLINIC_ADMIN
    user.status = AccountStatus.ACTIVE
    user.preferred_locale = LocaleCode(body.preferred_locale)
    user.full_name = body.full_name
    user.is_synthetic = settings.environment != "pilot"
    await session.flush()

    session.add(ClinicStaff(id=uuid7(), user_id=user.id, clinic_id=body.clinic_id, role="admin"))

    await audit.write(
        session,
        action=audit.AUTH_REGISTER,
        actor_user_id=user.id,
        actor_role=user.role.value,
        ip_address=ip,
        user_agent=user_agent,
        detail={"role": "admin"},
    )

    auth_client = await security.new_auth_client()
    signed_in = await auth_client.auth.sign_in_with_password(
        {"email": body.email, "password": body.password}
    )
    out, pair = _session_out(user, signed_in.session)
    await session.commit()
    return out, pair
