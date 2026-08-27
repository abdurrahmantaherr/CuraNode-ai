"""Registration, login, and session lifecycle (SPEC 4.1, 4.3, 4.4).

Password verification and refresh-token rotation are delegated to Supabase
Auth — admin operations via `security.get_supabase_client()`, sign-in/refresh
via a fresh `security.new_auth_client()` per call (see that module for why
the two must not share a client instance). The invariants that remain the
app's responsibility:

* Self-registration may create a doctor *account*, never doctor *access*.
  `is_verified` is written as False here and is never read from the request.
* Registration must not become an email-enumeration oracle: a duplicate
  returns the same shape as a success, creates nothing in Supabase or the
  local DB, and issues no cookies.
* Login lockout (10 failures / 15 minutes) is tracked locally against
  `profiles.failed_logins`/`locked_until` — a locked account is refused
  before Supabase is ever called, and a lock is never extended by further
  attempts (SPEC BL-10). NOTE: because password verification now crosses the
  network to Supabase, the exact timing-indistinguishability Supabase's own
  GoTrue service provides is outside this app's control; only the response
  *shape* (identical `Unauthenticated`) is guaranteed here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from supabase_auth.errors import AuthApiError

from ..audit import writer as audit
from ..cache import cache, lockout_key
from ..db.models import (
    AccountStatus,
    Clinic,
    ClinicStaff,
    Doctor,
    DoctorAffiliation,
    LocaleCode,
    Patient,
    Profile,
    UserRole,
    VerificationStatus,
)
from ..db.types import uuid7
from ..errors import Unauthenticated, ValidationFailed
from ..settings import settings
from . import oauth as oauth_mod
from . import security
from .schemas import (
    DoctorOnboardingRequest,
    DoctorRegisterRequest,
    LoginRequest,
    MeOut,
    SessionOut,
    landing_route_for,
    mask_phone,
)

PASSPORT_MAX_ATTEMPTS = 5


# ── Helpers ──────────────────────────────────────────────────────────────
async def _user_by_email(session: AsyncSession, email: str) -> Profile | None:
    result = await session.execute(select(Profile).where(Profile.email == email))
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


async def provision_role_records(
    session: AsyncSession,
    user: Profile,
    *,
    role: UserRole,
    full_name: str,
    pmdc_number: str | None = None,
    specialty: str | None = None,
    primary_clinic_id: uuid.UUID | None = None,
) -> None:
    """Creates the `Patient` or `Doctor` (+ `DoctorAffiliation`) row for a
    user who has none yet. Shared by password registration and OAuth
    onboarding — the two ways a `user_profile` row can end up choosing a role.
    """
    if role == UserRole.DOCTOR:
        doctor = Doctor(
            id=uuid7(),
            user_id=user.id,
            full_name=full_name,
            specialty=specialty,
            pmdc_number=pmdc_number,
            # Never sourced from the request. This is the line that keeps
            # self-registration from becoming self-authorisation.
            verification_status=VerificationStatus.PENDING,
            verified_by=None,
            verified_at=None,
        )
        session.add(doctor)
        session.add(
            DoctorAffiliation(
                id=uuid7(),
                doctor_id=doctor.id,
                clinic_id=primary_clinic_id,
                start_date=datetime.now(UTC).date(),
                status="active",
            )
        )
    else:
        session.add(
            Patient(
                id=uuid7(),
                user_id=user.id,
                full_name=full_name,
                passport_no=await _unique_passport_no(session),
            )
        )


def _session_out(user: Profile, gotrue_session: Any) -> tuple[SessionOut, security.TokenPair]:
    role = user.role.value
    locale = user.preferred_locale.value
    expires_at_ts = gotrue_session.expires_at or (
        int(datetime.now(UTC).timestamp()) + gotrue_session.expires_in
    )
    expires_at = datetime.fromtimestamp(expires_at_ts, tz=UTC)
    out = SessionOut(
        user_id=user.id,
        role=role,  # type: ignore[arg-type]
        full_name=user.full_name,
        locale=locale,  # type: ignore[arg-type]
        landing_route=landing_route_for(role, locale),
        access_expires_at=expires_at,
    )
    pair = security.TokenPair(
        access_token=gotrue_session.access_token,
        refresh_token=gotrue_session.refresh_token,
    )
    return out, pair


async def _sign_out(access_token: str | None) -> None:
    """Best-effort session revocation. Never raises — logout must stay idempotent."""
    if not access_token:
        return
    client = await security.get_supabase_client()
    try:
        await client.auth.admin.sign_out(access_token, scope="global")
    except AuthApiError:
        pass


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
        # Enumeration guard. Shape matches success exactly; no row, no cookies,
        # and Supabase is never called.
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

    client = await security.get_supabase_client()
    created = await client.auth.admin.create_user(
        {
            "email": body.email,
            "password": body.password,
            # No verification channel exists, so the account is usable
            # immediately — matches AccountStatus.ACTIVE below.
            "email_confirm": True,
        }
    )
    user_id = uuid.UUID(created.user.id)

    # A DB trigger (`on_auth_user_created`) already inserted a default
    # `user_profile` row (role='patient', locale='en', status='active') the
    # instant the Supabase auth user was created — by the time this admin
    # call returns, that trigger has already committed. Update it rather
    # than inserting again, or role/verification-relevant fields silently
    # revert to the trigger's defaults.
    user = await session.get(Profile, user_id)
    if user is None:
        # Defensive fallback only — should be unreachable while the trigger
        # exists, but this must not become an account created ex nihilo.
        user = Profile(id=user_id)
        session.add(user)

    user.email = body.email
    user.phone_e164 = body.phone_e164
    user.role = UserRole.DOCTOR if is_doctor else UserRole.PATIENT
    user.status = AccountStatus.ACTIVE
    user.preferred_locale = LocaleCode(body.preferred_locale)
    user.full_name = body.full_name
    user.is_synthetic = settings.environment != "pilot"
    await session.flush()

    await provision_role_records(
        session,
        user,
        role=user.role,
        full_name=body.full_name,
        pmdc_number=body.pmdc_number if is_doctor else None,
        specialty=body.specialty if is_doctor else None,
        primary_clinic_id=body.primary_clinic_id if is_doctor else None,
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

    auth_client = await security.new_auth_client()
    signed_in = await auth_client.auth.sign_in_with_password(
        {"email": body.email, "password": body.password}
    )
    out, pair = _session_out(user, signed_in.session)
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
        raise Unauthenticated()

    now = datetime.now(UTC)
    locked_until = user.locked_until
    if locked_until is not None and locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=UTC)

    if locked_until is not None and locked_until > now:
        # Do NOT extend the lock — otherwise an attacker could keep a real user
        # locked out indefinitely (SPEC BL-10). Supabase is never called while
        # locked.
        raise Unauthenticated()

    auth_client = await security.new_auth_client()
    try:
        signed_in = await auth_client.auth.sign_in_with_password(
            {"email": body.email, "password": body.password}
        )
    except AuthApiError:
        user.failed_logins += 1
        if user.failed_logins >= settings.max_failed_logins:
            user.locked_until = now + timedelta(minutes=settings.lockout_minutes)
            await cache.set(lockout_key(str(user.id)), True, settings.lockout_minutes * 60)
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
        # Suspended must stay indistinguishable from a wrong password. The
        # session Supabase just issued is valid, so it must be revoked rather
        # than handed to the caller.
        await _sign_out(signed_in.session.access_token)
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

    out, pair = _session_out(user, signed_in.session)
    await session.commit()
    return out, pair


# ── OAuth login (docs/oauth.md §8b) ──────────────────────────────────────
async def login_with_oauth(
    session: AsyncSession,
    exchanged: Any,
    *,
    provider: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[SessionOut, security.TokenPair, bool]:
    """Signs in (or first-signs-in) a user Supabase just authenticated via
    OAuth. Returns `(SessionOut, TokenPair, onboarding_required)`.

    `exchanged` is the SDK response from `oauth.exchange_code` — same shape as
    `sign_in_with_password`'s: `.user.id`, `.user.email`, `.user.user_metadata`,
    `.session`.
    """
    user_id = uuid.UUID(exchanged.user.id)
    fields = oauth_mod.profile_fields(exchanged.user)

    # The `on_auth_user_created` trigger has already inserted a default
    # `user_profile` row by the time this returns — same assumption as
    # `register()`. The `if user is None` branch further down is defensive
    # only.
    user = await session.get(Profile, user_id)

    # An unverified provider email must never be able to take over an
    # existing password account — refuse and revoke the session Supabase just
    # issued rather than silently merging the two identities. This MUST run
    # before the fallback `Profile` below is created: staging that row first
    # would autoflush it into this very SELECT and make an account collide
    # with itself.
    existing = await _user_by_email(session, fields["email"])
    if existing is not None and existing.id != user_id:
        await _sign_out(exchanged.session.access_token)
        raise Unauthenticated()

    if user is None:
        # Defensive fallback only (should be unreachable while the trigger
        # exists) — `email` is NOT NULL, so it must be set before this is
        # ever flushed. Same ordering constraint `register()` has.
        user = Profile(
            id=user_id,
            role=UserRole.PATIENT,
            email=fields["email"],
            status=AccountStatus.ACTIVE,
        )
        session.add(user)

    if user.status != AccountStatus.ACTIVE:
        await _sign_out(exchanged.session.access_token)
        raise Unauthenticated()

    now = datetime.now(UTC)
    locked_until = user.locked_until
    if locked_until is not None and locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=UTC)
    if locked_until is not None and locked_until > now:
        # Honour an existing lock, but OAuth never contributes to
        # `failed_logins` — there is no password here to brute-force, so
        # incrementing it would only hand an attacker a way to lock a victim
        # out.
        await _sign_out(exchanged.session.access_token)
        raise Unauthenticated()

    user.email = fields["email"]
    if not user.full_name:
        user.full_name = fields["full_name"]
    user.is_synthetic = settings.environment != "pilot"

    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    await cache.delete(lockout_key(str(user.id)))
    await session.flush()

    onboarding_required = await _onboarding_required(session, user)

    await audit.write(
        session,
        action=audit.AUTH_OAUTH_LOGIN,
        actor_user_id=user.id,
        actor_role=user.role.value,
        ip_address=ip,
        user_agent=user_agent,
        detail={"provider": provider},
    )

    out, pair = _session_out(user, exchanged.session)
    await session.commit()
    return out, pair, onboarding_required


async def _onboarding_required(session: AsyncSession, user: Profile) -> bool:
    if user.role == UserRole.PATIENT:
        row = await session.execute(select(Patient.id).where(Patient.user_id == user.id))
    elif user.role == UserRole.DOCTOR:
        row = await session.execute(select(Doctor.id).where(Doctor.user_id == user.id))
    else:
        row = await session.execute(select(ClinicStaff.id).where(ClinicStaff.user_id == user.id))
    return row.scalar_one_or_none() is None


async def complete_onboarding(
    session: AsyncSession,
    user: Profile,
    body: Any,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> Profile:
    """Finishes provisioning a user who signed in via OAuth with no role yet.

    Returns the updated `Profile` — the caller already holds a valid session
    (cookies were set at OAuth login time), so this has nothing to re-issue;
    it only needs to hand back the chosen role for the post-onboarding
    redirect.

    Idempotent against a double-submit: if a role record already exists this
    is a no-op rather than provisioning a second one.
    """
    is_doctor = isinstance(body, DoctorOnboardingRequest)

    if is_doctor:
        clinic = await session.get(Clinic, body.primary_clinic_id)
        if clinic is None:
            raise ValidationFailed({"fields": {"primary_clinic_id": "errors.clinic_unknown"}})

    if not await _onboarding_required(session, user):
        return user

    role = UserRole.DOCTOR if is_doctor else UserRole.PATIENT
    user.role = role
    user.full_name = body.full_name
    user.phone_e164 = body.phone_e164
    user.preferred_locale = LocaleCode(body.preferred_locale)
    await session.flush()

    await provision_role_records(
        session,
        user,
        role=role,
        full_name=body.full_name,
        pmdc_number=body.pmdc_number if is_doctor else None,
        specialty=body.specialty if is_doctor else None,
        primary_clinic_id=body.primary_clinic_id if is_doctor else None,
    )

    await audit.write(
        session,
        action=audit.AUTH_OAUTH_ONBOARDED,
        actor_user_id=user.id,
        actor_role=user.role.value,
        ip_address=ip,
        user_agent=user_agent,
        detail={"role": role.value},
    )

    await session.commit()
    return user


# ── Refresh rotation (SPEC 4.3) ──────────────────────────────────────────
async def rotate_refresh(
    session: AsyncSession, presented: str | None
) -> tuple[SessionOut, security.TokenPair]:
    if not presented:
        raise Unauthenticated()

    auth_client = await security.new_auth_client()
    try:
        refreshed = await auth_client.auth.refresh_session(presented)
    except AuthApiError:
        # Supabase already revokes the whole session on reuse of a spent
        # refresh token, so family-wide revocation is implicit here.
        raise Unauthenticated()

    user = await session.get(Profile, uuid.UUID(refreshed.user.id))
    if user is None or user.status != AccountStatus.ACTIVE:
        await _sign_out(refreshed.session.access_token)
        raise Unauthenticated()

    out, pair = _session_out(user, refreshed.session)
    return out, pair


async def discard_session(session: AsyncSession, pair: security.TokenPair | None) -> None:
    """Throw away a session that was issued but must not be handed to the user.

    Used when authentication succeeded but a post-authentication check failed,
    so the freshly-minted session is not left usable by nobody.
    """
    if pair is None:
        return
    await _sign_out(pair.access_token)


async def logout(session: AsyncSession, access_token: str | None) -> None:
    """Idempotent — a second call is not an error (SPEC AC-11)."""
    if not access_token:
        return
    try:
        claims = await security.decode_supabase_access_token(access_token)
    except security.InvalidToken:
        return

    await _sign_out(access_token)
    await audit.write(session, action=audit.AUTH_LOGOUT, actor_user_id=claims.user_id)
    await session.commit()


# ── Identity projection ──────────────────────────────────────────────────
async def build_me(session: AsyncSession, user: Profile) -> MeOut:
    passport_no: str | None = None
    is_verified_doctor = False
    clinic_ids: list[uuid.UUID] = []

    if user.role == UserRole.PATIENT:
        patient = (
            await session.execute(select(Patient).where(Patient.user_id == user.id))
        ).scalar_one_or_none()
        passport_no = patient.passport_no if patient else None
    elif user.role == UserRole.DOCTOR:
        doctor = (
            await session.execute(select(Doctor).where(Doctor.user_id == user.id))
        ).scalar_one_or_none()
        if doctor is not None:
            is_verified_doctor = doctor.verification_status == VerificationStatus.VERIFIED
            aff_rows = await session.execute(
                select(DoctorAffiliation.clinic_id).where(
                    DoctorAffiliation.doctor_id == doctor.id,
                    DoctorAffiliation.status == "active",
                )
            )
            clinic_ids = list(aff_rows.scalars().all())
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
