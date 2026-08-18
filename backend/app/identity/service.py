"""Registration, login, and session lifecycle (SPEC 4.1, 4.3, 4.4).

The invariants this module exists to hold:

* Self-registration may create a doctor *account*, never doctor *access*.
  `is_verified` is written as False here and is never read from the request.
* Registration must not become an email-enumeration oracle: a duplicate returns
  the same shape as a success, creates nothing, and issues no cookies.
* Every authentication failure is externally identical, in body and in timing.
* Refresh tokens are single-use; reuse revokes the whole family.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import writer as audit
from ..cache import cache, lockout_key, refresh_family_key, refresh_key
from ..db.models import (
    AccountStatus,
    Clinic,
    ClinicStaff,
    Doctor,
    LocaleCode,
    Patient,
    User,
    UserRole,
)
from ..db.types import uuid7
from ..errors import Unauthenticated, ValidationFailed
from ..settings import settings
from . import security
from .schemas import (
    DoctorRegisterRequest,
    LoginRequest,
    MeOut,
    SessionOut,
    landing_route_for,
    mask_phone,
)

PASSPORT_MAX_ATTEMPTS = 5


# ── Helpers ──────────────────────────────────────────────────────────────
async def _user_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def _unique_passport_no(session: AsyncSession) -> str:
    for _ in range(PASSPORT_MAX_ATTEMPTS):
        candidate = security.generate_passport_no()
        existing = await session.execute(
            select(Patient.passport_no).where(Patient.passport_no == candidate)
        )
        if existing.scalar_one_or_none() is None:
            return candidate
    # Five collisions against a 30^8 space means something is badly wrong;
    # surface it rather than looping forever.
    raise RuntimeError("Could not allocate a unique passport number")


async def _issue_tokens(user: User) -> tuple[SessionOut, security.TokenPair]:
    role = user.role.value
    locale = user.preferred_locale.value
    access, expires = security.issue_access_token(user.id, role, locale)
    raw, digest, family = security.new_refresh_token()

    ttl = settings.refresh_cookie_max_age
    await cache.set(
        refresh_key(digest),
        {"user_id": str(user.id), "family_id": family, "consumed": False},
        ttl,
    )
    await cache.add_to_set(refresh_family_key(family), digest, ttl)

    out = SessionOut(
        user_id=user.id,
        role=role,  # type: ignore[arg-type]
        full_name=user.full_name,
        locale=locale,  # type: ignore[arg-type]
        landing_route=landing_route_for(role, locale),
        access_expires_at=expires,
    )
    pair = security.TokenPair(
        access_token=access,
        access_expires_at=expires,
        refresh_token=raw,
        refresh_hash=digest,
        family_id=family,
    )
    return out, pair


# ── Registration (SPEC 4.1) ──────────────────────────────────────────────
async def register(
    session: AsyncSession,
    body: Any,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[SessionOut, security.TokenPair | None]:
    """Create a patient or doctor account and sign the user in.

    Returns `(SessionOut, None)` on the duplicate-email path so the router can
    emit an identical body with no cookies (SPEC AC-24).
    """
    is_doctor = isinstance(body, DoctorRegisterRequest)

    if is_doctor:
        clinic = await session.get(Clinic, body.primary_clinic_id)
        if clinic is None:
            raise ValidationFailed({"fields": {"primary_clinic_id": "errors.clinic_unknown"}})

    existing = await _user_by_email(session, body.email)
    if existing is not None:
        # Enumeration guard. Shape matches success exactly; no row, no cookies.
        return (
            SessionOut(
                user_id=uuid7(),
                role="patient",
                full_name=body.full_name,
                locale=body.preferred_locale,
                landing_route=landing_route_for("patient", body.preferred_locale),
                access_expires_at=datetime.now(UTC)
                + timedelta(minutes=settings.access_token_minutes),
            ),
            None,
        )

    user = User(
        id=uuid7(),
        email=body.email,
        phone_e164=body.phone_e164,
        password_hash=security.hash_password(body.password),
        role=UserRole.DOCTOR if is_doctor else UserRole.PATIENT,
        # No verification channel exists, so the account is usable immediately.
        status=AccountStatus.ACTIVE,
        preferred_locale=LocaleCode(body.preferred_locale),
        full_name=body.full_name,
        is_synthetic=settings.environment != "pilot",
    )
    session.add(user)
    await session.flush()

    if is_doctor:
        session.add(
            Doctor(
                user_id=user.id,
                primary_clinic_id=body.primary_clinic_id,
                specialty=body.specialty,
                pmdc_number=body.pmdc_number,
                # Never sourced from the request. This is the line that keeps
                # self-registration from becoming self-authorisation.
                is_verified=False,
                verified_by=None,
                verified_at=None,
            )
        )
    else:
        session.add(
            Patient(user_id=user.id, passport_no=await _unique_passport_no(session))
        )

    await audit.write(
        session,
        action=audit.AUTH_REGISTER,
        actor_user_id=user.id,
        actor_role=user.role.value,
        ip_address=ip,
        user_agent=user_agent,
        detail={"role": user.role.value},
    )

    out, pair = await _issue_tokens(user)
    await session.commit()
    return out, pair


# ── Login (SPEC 4.4) ─────────────────────────────────────────────────────
async def login(
    session: AsyncSession,
    body: LoginRequest,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[SessionOut, security.TokenPair]:
    user = await _user_by_email(session, body.email)

    if user is None:
        # Burn equivalent time so an unknown email is indistinguishable.
        security.dummy_verify()
        raise Unauthenticated()

    now = datetime.now(UTC)
    locked_until = user.locked_until
    if locked_until is not None and locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=UTC)

    if locked_until is not None and locked_until > now:
        # Do NOT extend the lock — otherwise an attacker could keep a real user
        # locked out indefinitely (SPEC BL-10).
        security.dummy_verify()
        raise Unauthenticated()

    if not security.verify_password(body.password, user.password_hash):
        user.failed_logins += 1
        if user.failed_logins >= settings.max_failed_logins:
            user.locked_until = now + timedelta(minutes=settings.lockout_minutes)
            await cache.set(
                lockout_key(str(user.id)), True, settings.lockout_minutes * 60
            )
            await audit.write(
                session,
                action=audit.AUTH_LOCKOUT,
                actor_user_id=user.id,
                actor_role=user.role.value,
                ip_address=ip,
                detail={"failed_logins": user.failed_logins},
            )
        await session.commit()
        raise Unauthenticated()

    if user.status != AccountStatus.ACTIVE:
        # Suspended is the only reachable non-active state now that OTP is
        # gone, and it must stay indistinguishable from a wrong password.
        raise Unauthenticated()

    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    await cache.delete(lockout_key(str(user.id)))

    await audit.write(
        session,
        action=audit.AUTH_LOGIN,
        actor_user_id=user.id,
        actor_role=user.role.value,
        ip_address=ip,
        user_agent=user_agent,
    )

    out, pair = await _issue_tokens(user)
    await session.commit()
    return out, pair


# ── Refresh rotation (SPEC 4.3) ──────────────────────────────────────────
async def rotate_refresh(
    session: AsyncSession, presented: str | None
) -> tuple[SessionOut, security.TokenPair]:
    if not presented:
        raise Unauthenticated()

    digest = security.hash_refresh_token(presented)
    record = await cache.get(refresh_key(digest))
    if record is None:
        raise Unauthenticated()

    if record.get("consumed"):
        # Replay of a spent token is treated as theft: revoke the family.
        await _revoke_family(session, record.get("family_id"), reason="reuse_detected")
        await session.commit()
        raise Unauthenticated()

    user = await session.get(User, uuid.UUID(record["user_id"]))
    if user is None or user.status != AccountStatus.ACTIVE:
        raise Unauthenticated()

    record["consumed"] = True
    await cache.set(refresh_key(digest), record, settings.refresh_cookie_max_age)

    role = user.role.value
    locale = user.preferred_locale.value
    access, expires = security.issue_access_token(user.id, role, locale)
    raw, new_digest, _ = security.new_refresh_token(record["family_id"])

    ttl = settings.refresh_cookie_max_age
    await cache.set(
        refresh_key(new_digest),
        {"user_id": str(user.id), "family_id": record["family_id"], "consumed": False},
        ttl,
    )
    await cache.add_to_set(refresh_family_key(record["family_id"]), new_digest, ttl)

    out = SessionOut(
        user_id=user.id,
        role=role,  # type: ignore[arg-type]
        full_name=user.full_name,
        locale=locale,  # type: ignore[arg-type]
        landing_route=landing_route_for(role, locale),
        access_expires_at=expires,
    )
    pair = security.TokenPair(
        access_token=access,
        access_expires_at=expires,
        refresh_token=raw,
        refresh_hash=new_digest,
        family_id=record["family_id"],
    )
    return out, pair


async def _revoke_family(
    session: AsyncSession, family_id: str | None, *, reason: str
) -> None:
    if not family_id:
        return
    members = await cache.members(refresh_family_key(family_id))
    if members:
        await cache.delete(*[refresh_key(m) for m in members])
    await cache.delete(refresh_family_key(family_id))
    if reason == "reuse_detected":
        await audit.write(
            session, action=audit.AUTH_REFRESH_REUSE, detail={"family_id": family_id}
        )


async def discard_session(
    session: AsyncSession, pair: security.TokenPair | None
) -> None:
    """Throw away a session that was issued but must not be handed to the user.

    Used when authentication succeeded but a post-authentication check failed,
    so the freshly-minted refresh token does not sit in the cache for 14 days
    as a usable credential nobody holds.
    """
    if pair is None:
        return
    await cache.delete(refresh_key(pair.refresh_hash))
    await _revoke_family(session, pair.family_id, reason="discarded")


async def logout(session: AsyncSession, presented: str | None) -> None:
    """Idempotent — a second call is not an error (SPEC AC-11)."""
    if not presented:
        return
    digest = security.hash_refresh_token(presented)
    record = await cache.get(refresh_key(digest))
    if record is None:
        return
    await _revoke_family(session, record.get("family_id"), reason="logout")
    await audit.write(
        session,
        action=audit.AUTH_LOGOUT,
        actor_user_id=uuid.UUID(record["user_id"]),
    )
    await session.commit()


# ── Identity projection ──────────────────────────────────────────────────
async def build_me(session: AsyncSession, user: User) -> MeOut:
    passport_no: str | None = None
    is_verified_doctor = False
    clinic_ids: list[uuid.UUID] = []

    if user.role == UserRole.PATIENT:
        patient = await session.get(Patient, user.id)
        passport_no = patient.passport_no if patient else None
    elif user.role == UserRole.DOCTOR:
        doctor = await session.get(Doctor, user.id)
        if doctor is not None:
            is_verified_doctor = doctor.is_verified
            clinic_ids = [doctor.primary_clinic_id]
    else:
        rows = await session.execute(
            select(ClinicStaff.clinic_id).where(ClinicStaff.user_id == user.id)
        )
        clinic_ids = list(rows.scalars().all())

    return MeOut(
        user_id=user.id,
        role=user.role.value,  # type: ignore[arg-type]
        full_name=user.full_name,
        email=user.email,
        phone_masked=mask_phone(user.phone_e164),
        locale=user.preferred_locale.value,  # type: ignore[arg-type]
        status=user.status.value,  # type: ignore[arg-type]
        is_verified_doctor=is_verified_doctor,
        clinic_ids=clinic_ids,
        passport_no=passport_no,
        last_login_at=user.last_login_at,
    )
