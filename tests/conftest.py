"""Test fixtures.

Each test gets a fresh in-memory database and a cleared cache, so lockout
counters and rate limits from one test cannot leak into the next.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("COOKIE_SECURE", "true")

import pytest
from app.cache import InMemoryCache
from app.db import session as session_mod
from app.db.models import (
    AccountStatus,
    Base,
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
from app.db.session import get_session
from app.db.types import utcnow, uuid7
from app.identity import security as identity_security
from app.identity.security import generate_passport_no
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.fakes import FakeSupabaseAuth, fake_decode_token

TEST_PASSWORD = "CorrectHorseBattery1"


@pytest.fixture
async def engine():
    # StaticPool keeps one connection so :memory: survives across sessions.
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def sessionmaker_(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    c = InMemoryCache()
    monkeypatch.setattr("app.cache.cache", c)
    monkeypatch.setattr("app.identity.service.cache", c)
    monkeypatch.setattr("app.deps.cache", c)
    return c


@pytest.fixture(autouse=True)
def fake_supabase(monkeypatch):
    """Every test gets its own isolated Supabase Auth double — no network,
    no shared state between tests, and no real JWKS fetch."""
    backend = FakeSupabaseAuth()

    async def _get_client():
        return backend

    monkeypatch.setattr(identity_security, "get_supabase_client", _get_client)
    # Production splits admin vs. auth-flow clients to avoid the real SDK's
    # stateful-session hazard; the fake has no such hazard, so both resolve
    # to the same in-memory backend.
    monkeypatch.setattr(identity_security, "new_auth_client", _get_client)
    monkeypatch.setattr(identity_security, "decode_supabase_access_token", fake_decode_token)
    # deps.py imports the name directly, so it needs its own patch target.
    monkeypatch.setattr("app.deps.decode_supabase_access_token", fake_decode_token)
    return backend


@pytest.fixture
async def db(sessionmaker_) -> AsyncIterator:
    async with sessionmaker_() as s:
        yield s


@pytest.fixture
async def app(sessionmaker_, engine, monkeypatch):
    monkeypatch.setattr(session_mod, "engine", engine)
    monkeypatch.setattr(session_mod, "SessionFactory", sessionmaker_)

    from app.main import app as fastapi_app

    async def _override() -> AsyncIterator:
        async with sessionmaker_() as s:
            yield s

    fastapi_app.dependency_overrides[get_session] = _override
    yield fastapi_app
    fastapi_app.dependency_overrides.clear()


@pytest.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    # https:// is required, not cosmetic: cookies are issued with `Secure`, and
    # a client will refuse to store or resend them over plain http.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        yield c


@pytest.fixture
async def clinic(db) -> Clinic:
    c = Clinic(
        id=uuid7(),
        name="Shifa International",
        city="Islamabad",
        address="F-8",
        type="hospital",
    )
    db.add(c)
    await db.commit()
    return c


async def make_user(
    db,
    *,
    email: str,
    role: UserRole,
    password: str = TEST_PASSWORD,
    status: AccountStatus = AccountStatus.ACTIVE,
    clinic_id=None,
    is_verified: bool = False,
    verifier_id=None,
) -> Profile:
    """Creates a `Profile` row directly and registers matching credentials
    with the fake Supabase backend the active test is using, so the account
    can log in through the real `/api/v1/auth/login` flow."""
    user_id = uuid7()
    normalised_email = email.strip().lower()
    client = await identity_security.get_supabase_client()
    client.register(str(user_id), normalised_email, password)

    user = Profile(
        id=user_id,
        email=normalised_email,
        role=role,
        status=status,
        preferred_locale=LocaleCode.EN,
        full_name="Test Person",
        is_synthetic=True,
    )
    db.add(user)
    await db.flush()

    if role == UserRole.PATIENT:
        db.add(
            Patient(
                id=uuid7(),
                user_id=user.id,
                full_name=user.full_name,
                passport_no=generate_passport_no(),
            )
        )
    elif role == UserRole.DOCTOR:
        doctor = Doctor(
            id=uuid7(),
            user_id=user.id,
            full_name=user.full_name,
            specialty="Cardiology",
            pmdc_number=secrets.token_hex(6),
            verification_status=(
                VerificationStatus.VERIFIED if is_verified else VerificationStatus.PENDING
            ),
            verified_by=verifier_id if is_verified else None,
            verified_at=utcnow() if is_verified else None,
        )
        db.add(doctor)
        if clinic_id is not None:
            db.add(
                DoctorAffiliation(
                    id=uuid7(),
                    doctor_id=doctor.id,
                    clinic_id=clinic_id,
                    start_date=utcnow().date(),
                    status="active",
                )
            )
    elif role == UserRole.CLINIC_ADMIN and clinic_id is not None:
        db.add(ClinicStaff(id=uuid7(), user_id=user.id, clinic_id=clinic_id, role="admin"))

    await db.commit()
    return user
