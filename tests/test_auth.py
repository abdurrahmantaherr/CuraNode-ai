"""Authentication test suite.

Test ids map 1:1 to SPEC_authentication.md §11. T2/T3/T4 are void — their
acceptance criteria were removed with OTP. T15's Argon2-parameter and T23's
raw-token-decoding assertions are void too — password hashing and access-
token issuance now belong to Supabase Auth, exercised here only through the
fake double in tests/fakes.py.
"""

from __future__ import annotations

import pytest
from app.db.models import AccountStatus, Doctor, Patient, Profile, UserRole, VerificationStatus
from app.db.types import utcnow

# These MUST be imported at module scope. `from __future__ import annotations`
# turns annotations into strings, and FastAPI resolves them against the
# module's globals — a function-local import is invisible to it, and the
# dependency silently degrades into a query parameter (422).
from app.deps import ClinicAdminDep, PatientDep, VerifiedDoctorDep
from app.settings import settings
from sqlalchemy import select

from tests.conftest import TEST_PASSWORD, make_user
from tests.fakes import fake_decode_token as decode_supabase_access_token


def _register_payload(email: str, **extra):
    body = {
        "full_name": "Ayesha Raza",
        "email": email,
        "password": TEST_PASSWORD,
        "role": "patient",
    }
    body.update(extra)
    return body


# ── T1 / AC-01 — patient registration ────────────────────────────────────
async def test_t1_patient_registration(client, db):
    r = await client.post("/api/v1/auth/register", json=_register_payload("a@b.com"))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["role"] == "patient"
    assert body["landing_route"] == "/en/patient"

    # Signed in immediately — both cookies present (no OTP step).
    assert settings.access_cookie_name in r.cookies
    assert settings.refresh_cookie_name in r.cookies

    user = (await db.execute(select(Profile).where(Profile.email == "a@b.com"))).scalar_one()
    assert user.role == UserRole.PATIENT
    assert user.status == AccountStatus.ACTIVE

    patient = (
        await db.execute(select(Patient).where(Patient.user_id == user.id))
    ).scalar_one_or_none()
    assert patient is not None
    assert patient.passport_no.startswith("CN-")
    assert len(patient.passport_no) == 12


# ── T5 / AC-05 — login success ───────────────────────────────────────────
async def test_t5_login_sets_cookies_and_resets_counters(client, db, clinic):
    await make_user(db, email="p@x.com", role=UserRole.PATIENT)

    r = await client.post(
        "/api/v1/auth/login", json={"email": "p@x.com", "password": TEST_PASSWORD}
    )
    assert r.status_code == 200
    assert r.json()["landing_route"] == "/en/patient"

    cookie_header = "".join(str(v) for v in r.headers.get_list("set-cookie"))
    assert "HttpOnly" in cookie_header
    assert "Secure" in cookie_header
    assert "SameSite=strict" in cookie_header.replace("SameSite=Strict", "SameSite=strict")

    user = (await db.execute(select(Profile).where(Profile.email == "p@x.com"))).scalar_one()
    await db.refresh(user)
    assert user.failed_logins == 0
    assert user.last_login_at is not None


# ── T6 / AC-06 — indistinguishable failures, including timing ────────────
async def test_t6_auth_failures_are_identical(client, db):
    await make_user(db, email="known@x.com", role=UserRole.PATIENT)
    await make_user(
        db,
        email="susp@x.com",
        role=UserRole.PATIENT,
        status=AccountStatus.SUSPENDED,
    )

    wrong = await client.post(
        "/api/v1/auth/login", json={"email": "known@x.com", "password": "Wrong123456"}
    )
    unknown = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@x.com", "password": "Wrong123456"}
    )
    suspended = await client.post(
        "/api/v1/auth/login", json={"email": "susp@x.com", "password": TEST_PASSWORD}
    )

    assert wrong.status_code == unknown.status_code == suspended.status_code == 401
    strip = lambda b: {**b["error"], "request_id": None}
    assert strip(wrong.json()) == strip(unknown.json()) == strip(suspended.json())


# NOTE (post-Supabase-migration): a `test_t6_timing_does_not_leak_existence`
# test previously asserted response-timing parity between unknown-email and
# wrong-password logins, guarded by a local `dummy_verify()` burning the same
# Argon2 cost either way. Password verification is now a network call to
# Supabase's GoTrue service, so this app no longer controls that timing —
# only the response *shape* (asserted above) remains a guarantee this layer
# can make.


# ── T7 / AC-07 — lockout ─────────────────────────────────────────────────
async def test_t7_lockout_after_ten_failures_and_not_extended(client, db, monkeypatch):
    # Lockout needs 10 attempts; the 5/min rate limit (tested separately by
    # T14) would otherwise short-circuit this at attempt 6.
    monkeypatch.setattr(settings, "auth_rate_limit_per_minute", 1000)
    await make_user(db, email="lock@x.com", role=UserRole.PATIENT)

    for _ in range(settings.max_failed_logins):
        await client.post(
            "/api/v1/auth/login", json={"email": "lock@x.com", "password": "Nope1234567"}
        )

    user = (await db.execute(select(Profile).where(Profile.email == "lock@x.com"))).scalar_one()
    await db.refresh(user)
    assert user.failed_logins >= settings.max_failed_logins
    locked_at = user.locked_until
    assert locked_at is not None

    # Correct password during the lock still fails, and must NOT extend it.
    r = await client.post(
        "/api/v1/auth/login", json={"email": "lock@x.com", "password": TEST_PASSWORD}
    )
    assert r.status_code == 401
    await db.refresh(user)
    assert user.locked_until == locked_at


# ── T8 / AC-08 — verification is read live, not from the token ───────────
async def test_t8_unverified_doctor_blocked_then_allowed_without_relogin(client, db, clinic, app):
    @app.get("/api/v1/_test/clinical")
    async def _clinical(actor: VerifiedDoctorDep):  # type: ignore[no-untyped-def]
        return {"ok": True}

    doctor = await make_user(db, email="doc@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    await client.post("/api/v1/auth/login", json={"email": "doc@x.com", "password": TEST_PASSWORD})

    blocked = await client.get("/api/v1/_test/clinical")
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "FORBIDDEN"

    # Flip verification in the database — no new login.
    row = (await db.execute(select(Doctor).where(Doctor.user_id == doctor.id))).scalar_one()
    row.verification_status = VerificationStatus.VERIFIED
    row.verified_by = doctor.id
    row.verified_at = utcnow()
    await db.commit()

    allowed = await client.get("/api/v1/_test/clinical")
    assert allowed.status_code == 200


# ── T9 / T10 / AC-09, AC-10 — rotation and reuse detection ──────────────
async def test_t9_refresh_rotates(client, db):
    await make_user(db, email="rot@x.com", role=UserRole.PATIENT)
    await client.post("/api/v1/auth/login", json={"email": "rot@x.com", "password": TEST_PASSWORD})
    first = client.cookies.get(settings.refresh_cookie_name)

    r = await client.post("/api/v1/auth/refresh")
    assert r.status_code == 200
    second = client.cookies.get(settings.refresh_cookie_name)
    assert second and second != first


async def test_t10_refresh_reuse_revokes_family(client, db):
    await make_user(db, email="reuse@x.com", role=UserRole.PATIENT)
    await client.post(
        "/api/v1/auth/login", json={"email": "reuse@x.com", "password": TEST_PASSWORD}
    )
    stolen = client.cookies.get(settings.refresh_cookie_name)

    assert (await client.post("/api/v1/auth/refresh")).status_code == 200

    # Replay the consumed token.
    replay = await client.post(
        "/api/v1/auth/refresh", cookies={settings.refresh_cookie_name: stolen}
    )
    assert replay.status_code == 401

    # The whole family is dead — the rotated token no longer works either.
    assert (await client.post("/api/v1/auth/refresh")).status_code == 401


# ── T11 / AC-11 — logout is idempotent ───────────────────────────────────
async def test_t11_logout_idempotent(client, db):
    await make_user(db, email="out@x.com", role=UserRole.PATIENT)
    await client.post("/api/v1/auth/login", json={"email": "out@x.com", "password": TEST_PASSWORD})
    assert (await client.post("/api/v1/auth/logout")).status_code == 204
    assert (await client.post("/api/v1/auth/logout")).status_code == 204
    assert (await client.get("/api/v1/me")).status_code == 401


# ── T12 / AC-12 — identity projection ────────────────────────────────────
async def test_t12_me_projection(client, db, clinic):
    assert (await client.get("/api/v1/me")).status_code == 401

    await make_user(db, email="me@x.com", role=UserRole.PATIENT)
    await client.post("/api/v1/auth/login", json={"email": "me@x.com", "password": TEST_PASSWORD})
    body = (await client.get("/api/v1/me")).json()
    assert body["role"] == "patient"
    assert body["passport_no"] is not None
    assert body["clinic_ids"] == []
    assert body["phone_masked"] is None
    assert body["is_verified_doctor"] is False


# ── T13 / AC-13 — role matrix ────────────────────────────────────────────
@pytest.mark.parametrize(
    "role,allowed_path",
    [(UserRole.PATIENT, "patient"), (UserRole.DOCTOR, "doctor")],
)
async def test_t13_role_gates(client, db, clinic, app, role, allowed_path):
    @app.get("/api/v1/_test/patient-only")
    async def _p(actor: PatientDep):  # type: ignore[no-untyped-def]
        return {"ok": True}

    @app.get("/api/v1/_test/admin-only")
    async def _a(actor: ClinicAdminDep):  # type: ignore[no-untyped-def]
        return {"ok": True}

    await make_user(db, email=f"{allowed_path}@x.com", role=role, clinic_id=clinic.id)
    await client.post(
        "/api/v1/auth/login",
        json={"email": f"{allowed_path}@x.com", "password": TEST_PASSWORD},
    )

    patient_only = await client.get("/api/v1/_test/patient-only")
    admin_only = await client.get("/api/v1/_test/admin-only")

    assert patient_only.status_code == (200 if role == UserRole.PATIENT else 403)
    assert admin_only.status_code == 403
    # Authorisation failure is 403 here — never 404, which belongs to consent.
    assert admin_only.json()["error"]["code"] == "FORBIDDEN"


# ── T14 / AC-14 — rate limit ─────────────────────────────────────────────
async def test_t14_auth_rate_limit(client, db):
    await make_user(db, email="rl@x.com", role=UserRole.PATIENT)
    codes = []
    for _ in range(settings.auth_rate_limit_per_minute + 2):
        r = await client.post(
            "/api/v1/auth/login", json={"email": "rl@x.com", "password": "Wrong123456"}
        )
        codes.append(r.status_code)
    assert 429 in codes
    limited = [c for c in codes if c == 429]
    assert len(limited) >= 1


# ── T15 / AC-15 — log hygiene ─────────────────────────────────────────────
# The Argon2-parameter assertion this test used to make is void: password
# hashing now happens inside Supabase Auth, outside this codebase entirely.
def test_t15_redaction_covers_credentials():
    from app.log_config import redaction_processor

    out = redaction_processor(
        None,
        "",
        {
            "password": "secret",
            "email": "a@b.com",
            "phone_e164": "+923001234567",
            "full_name": "Ayesha",
            "token": "abc",
            "ctx": {"refresh_token": "r", "safe": "keep"},
        },
    )
    for key in ("password", "email", "phone_e164", "full_name", "token"):
        assert out[key] == "[redacted]"
    assert out["ctx"]["refresh_token"] == "[redacted]"
    assert out["ctx"]["safe"] == "keep"


# ── T20 / AC-20 — cookie attributes, no token leakage ───────────────────
async def test_t20_cookie_flags_and_no_body_leak(client, db):
    await make_user(db, email="ck@x.com", role=UserRole.PATIENT)
    r = await client.post(
        "/api/v1/auth/login", json={"email": "ck@x.com", "password": TEST_PASSWORD}
    )
    header = "; ".join(r.headers.get_list("set-cookie"))
    assert "HttpOnly" in header and "Secure" in header
    assert "Path=/" in header

    token = client.cookies.get(settings.access_cookie_name)
    assert token and token not in r.text  # never echoed in the body


# ── T21 / AC-21 — doctor registration cannot self-verify ────────────────
async def test_t21_doctor_registration_is_unverified(client, db, clinic):
    r = await client.post(
        "/api/v1/auth/register",
        json=_register_payload(
            "doc1@x.com",
            role="doctor",
            pmdc_number="41192",
            specialty="Cardiology",
            primary_clinic_id=str(clinic.id),
        ),
    )
    assert r.status_code == 201
    assert r.json()["role"] == "doctor"

    user = (await db.execute(select(Profile).where(Profile.email == "doc1@x.com"))).scalar_one()
    doctor = (await db.execute(select(Doctor).where(Doctor.user_id == user.id))).scalar_one()
    assert doctor.verification_status == VerificationStatus.PENDING
    assert doctor.verified_by is None and doctor.verified_at is None
    assert doctor.pmdc_number == "41192"
    assert (
        await db.execute(select(Patient).where(Patient.user_id == user.id))
    ).scalar_one_or_none() is None


async def test_t21_injected_is_verified_is_ignored(client, db, clinic):
    """G7 — the single most important assertion in the feature."""
    r = await client.post(
        "/api/v1/auth/register",
        json=_register_payload(
            "doc2@x.com",
            role="doctor",
            pmdc_number="41192",
            specialty="Cardiology",
            primary_clinic_id=str(clinic.id),
            is_verified=True,
            verified_by=str(clinic.id),
            status="active",
        ),
    )
    assert r.status_code == 201
    user = (await db.execute(select(Profile).where(Profile.email == "doc2@x.com"))).scalar_one()
    doctor = (await db.execute(select(Doctor).where(Doctor.user_id == user.id))).scalar_one()
    assert doctor.verification_status == VerificationStatus.PENDING, (
        "client injected is_verified into the row"
    )


async def test_t21_doctor_missing_fields_and_bad_clinic(client, db, clinic):
    missing = await client.post(
        "/api/v1/auth/register",
        json=_register_payload("doc3@x.com", role="doctor", specialty="Cardiology"),
    )
    assert missing.status_code == 422

    import uuid as _uuid

    unknown = await client.post(
        "/api/v1/auth/register",
        json=_register_payload(
            "doc4@x.com",
            role="doctor",
            pmdc_number="41192",
            specialty="Cardiology",
            primary_clinic_id=str(_uuid.uuid4()),
        ),
    )
    assert unknown.status_code == 422
    assert (
        await db.execute(select(Profile).where(Profile.email == "doc4@x.com"))
    ).scalar_one_or_none() is None


# ── T23 / AC-23 — login role toggle is not a server input ───────────────
async def test_t23_login_request_rejects_role_field(client, db):
    await make_user(db, email="tog@x.com", role=UserRole.PATIENT)
    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "tog@x.com", "password": TEST_PASSWORD, "role": "doctor"},
    )
    # The extra key is ignored; routing follows the account, not the toggle.
    assert r.status_code == 200
    assert r.json()["role"] == "patient"
    assert r.json()["landing_route"] == "/en/patient"

    claims = await decode_supabase_access_token(client.cookies.get(settings.access_cookie_name))
    user = (await db.execute(select(Profile).where(Profile.email == "tog@x.com"))).scalar_one()
    assert claims.user_id == user.id


def test_t23_access_token_claims_carry_no_role_or_verification():
    """AC-08 depends on this: verification/role must not be cacheable in a
    token — `AccessClaims` structurally cannot carry either, so `deps.py` has
    no choice but to re-read them from the database on every request (see
    T8, which proves that live-read behavior end to end)."""
    from dataclasses import fields

    from app.identity.security import AccessClaims

    assert {f.name for f in fields(AccessClaims)} == {"user_id"}


# ── T24 / AC-24 — duplicate email is not an oracle ──────────────────────
async def test_t24_duplicate_email_no_oracle(client, db):
    first = await client.post("/api/v1/auth/register", json=_register_payload("dup@x.com"))
    assert first.status_code == 201

    for variant in ("dup@x.com", "DUP@x.com", "  Dup@X.com  "):
        again = await client.post("/api/v1/auth/register", json=_register_payload(variant))
        assert again.status_code == 201, variant
        # Shape matches success, but no session is issued.
        assert set(again.json()) == set(first.json())
        assert settings.access_cookie_name not in again.cookies

    count = len(
        (await db.execute(select(Profile).where(Profile.email == "dup@x.com"))).scalars().all()
    )
    assert count == 1
