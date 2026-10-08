"""Regression tests for the authentication security audit fixes.

Grouped by audit finding id. The database-level guards for C1/H2 live in
tests/test_pg_authority_guards.py (they need a real Postgres); everything
here runs on the default SQLite + fake-Supabase setup.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import timedelta

import jwt
import pytest
from app.audit import writer as audit
from app.db.models import AccountStatus, AuditLog, Doctor, Profile, UserRole
from app.db.types import utcnow
from app.errors import NotFound, Unauthenticated
from app.i18n.catalogue import translate
from app.identity import service
from app.identity.schemas import LoginRequest, PatientRegisterRequest
from app.settings import settings
from httpx import ASGITransport, AsyncClient
from markupsafe import escape
from sqlalchemy import event, select, update
from sqlalchemy.exc import IntegrityError
from supabase_auth.errors import AuthApiError

from tests.conftest import TEST_PASSWORD, make_user
from tests.fakes import TEST_JWT_SECRET
from tests.test_oauth import _complete, _enable_oauth

pytestmark = pytest.mark.usefixtures("no_rate_limit")


@pytest.fixture
def no_rate_limit(monkeypatch):
    # These tests count attempts precisely; the 5/min per-IP limit (T14) is
    # orthogonal and would short-circuit them.
    monkeypatch.setattr(settings, "auth_rate_limit_per_minute", 10_000)


def _reg(email: str, **extra):
    body = {"full_name": "Ayesha Raza", "email": email, "password": TEST_PASSWORD}
    body.update(extra)
    return body


async def _profile(db, email: str) -> Profile | None:
    row = await db.execute(
        select(Profile).where(Profile.email == email).execution_options(populate_existing=True)
    )
    return row.scalar_one_or_none()


async def _login(client, email: str, password: str = TEST_PASSWORD, **kw):
    return await client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}, **kw
    )


async def _me_with(client, token: str):
    """GET /api/v1/me presenting exactly this access token, as a replaying
    attacker would (explicit Cookie header — not the client's jar)."""
    return await client.get(
        "/api/v1/me", headers={"cookie": f"{settings.access_cookie_name}={token}"}
    )


async def _audit_actions(db) -> list[str]:
    rows = await db.execute(select(AuditLog.action).execution_options(populate_existing=True))
    return list(rows.scalars().all())


# ══ H1 — OAuth pre-account-takeover (no email verification) ════════════
async def test_h1_google_linked_onto_password_account_is_refused(
    client, db, fake_supabase, monkeypatch
):
    """The audit scenario: an attacker registers victim@gmail.com with their
    own password (nothing proves they own it); the victim later signs in with
    Google and Supabase links the Google identity onto that account. The
    victim must not be signed into it — and gets told to use the password
    sign-in instead of a generic error."""
    _enable_oauth(monkeypatch)
    await client.post("/api/v1/auth/register", json=_reg("victim@gmail.com"))
    squatted = await _profile(db, "victim@gmail.com")

    r = await _complete(client, fake_supabase, "victim@gmail.com", user_id=str(squatted.id))
    assert r.status_code == 400
    assert translate("errors.oauth_email_conflict", "en") in r.text
    assert settings.access_cookie_name not in r.cookies
    assert "admin.sign_out" in fake_supabase.calls  # Supabase's fresh session revoked
    assert audit.AUTH_OAUTH_LINK_REFUSED in await _audit_actions(db)
    # Nothing about the account changed; it is still the password owner's.
    assert (await _profile(db, "victim@gmail.com")).status == AccountStatus.ACTIVE


async def test_h1_refused_even_for_a_genuine_password_owner(client, db, fake_supabase, monkeypatch):
    """The app cannot tell a squatter's password account from the real
    owner's, so ANY password-registered account is refused via Google."""
    _enable_oauth(monkeypatch)
    user = await make_user(db, email="real@gmail.com", role=UserRole.PATIENT)
    r = await _complete(client, fake_supabase, "real@gmail.com", user_id=str(user.id))
    assert r.status_code == 400
    assert settings.access_cookie_name not in r.cookies
    # ...and the password sign-in keeps working.
    assert (await _login(client, "real@gmail.com")).status_code == 200


async def test_h1_google_only_account_still_signs_in(client, db, fake_supabase, monkeypatch):
    _enable_oauth(monkeypatch)
    first = await _complete(client, fake_supabase, "googler@gmail.com")
    assert first.status_code == 200
    assert settings.access_cookie_name in first.cookies


async def test_h1_missing_identity_list_fails_closed(client, fake_supabase, monkeypatch):
    _enable_oauth(monkeypatch)
    monkeypatch.setattr(service.security, "identity_providers", lambda _user: set())
    r = await _complete(client, fake_supabase, "noids@gmail.com")
    assert r.status_code == 400
    assert settings.access_cookie_name not in r.cookies


async def test_h1_password_registration_for_a_google_account_is_refused(
    client, db, fake_supabase, monkeypatch
):
    """The reverse order: Google first, then a password registration for the
    same address — refused as a duplicate, so no password identity is ever
    attached to the Google owner's account."""
    _enable_oauth(monkeypatch)
    assert (await _complete(client, fake_supabase, "owner@gmail.com")).status_code == 200
    r = await client.post("/api/v1/auth/register", json=_reg("Owner@Gmail.com"))
    assert r.status_code == 409
    assert "admin.create_user" not in fake_supabase.calls


# ══ H3 / M4 — lockout ═══════════════════════════════════════════════════
async def _fail(client, email: str, times: int) -> None:
    for _ in range(times):
        await _login(client, email, "WrongPassword99")


async def test_h3_nine_failures_do_not_lock(client, db):
    await make_user(db, email="nine@x.com", role=UserRole.PATIENT)
    await _fail(client, "nine@x.com", settings.max_failed_logins - 1)
    user = await _profile(db, "nine@x.com")
    assert user.failed_logins == settings.max_failed_logins - 1
    assert user.locked_until is None
    assert (await _login(client, "nine@x.com")).status_code == 200


async def test_h3_tenth_failure_locks_for_fifteen_minutes(client, db, fake_supabase):
    await make_user(db, email="ten@x.com", role=UserRole.PATIENT)
    await _fail(client, "ten@x.com", settings.max_failed_logins)
    user = await _profile(db, "ten@x.com")
    assert user.failed_logins == settings.max_failed_logins
    remaining = user.locked_until.replace(tzinfo=None) - utcnow().replace(tzinfo=None)
    assert timedelta(minutes=14) < remaining <= timedelta(minutes=settings.lockout_minutes)
    assert audit.AUTH_LOCKOUT in await _audit_actions(db)

    # While locked, even the right password fails — and is never sent to
    # Supabase (only the decoy address is).
    checks_before = fake_supabase.sign_in_emails.count("ten@x.com")
    locked = await _login(client, "ten@x.com")
    assert locked.status_code == 401
    assert fake_supabase.sign_in_emails.count("ten@x.com") == checks_before
    assert fake_supabase.sign_in_emails[-1].endswith("@login-decoy.invalid")


async def test_h3_expired_lock_does_not_relock_on_next_single_failure(client, db):
    """The audit's permanent-lockout DoS: after expiry, one wrong guess used
    to re-lock immediately because the counter never reset."""
    user = await make_user(db, email="exp@x.com", role=UserRole.PATIENT)
    await db.execute(
        update(Profile)
        .where(Profile.id == user.id)
        .values(
            failed_logins=settings.max_failed_logins, locked_until=utcnow() - timedelta(seconds=1)
        )
    )
    await db.commit()

    await _fail(client, "exp@x.com", 1)
    after = await _profile(db, "exp@x.com")
    assert after.failed_logins == 1
    assert after.locked_until is None
    assert (await _login(client, "exp@x.com")).status_code == 200


async def test_h3_legacy_row_at_threshold_without_lock_is_released(client, db):
    user = await make_user(db, email="legacy@x.com", role=UserRole.PATIENT)
    await db.execute(
        update(Profile).where(Profile.id == user.id).values(failed_logins=17, locked_until=None)
    )
    await db.commit()
    assert (await _login(client, "legacy@x.com")).status_code == 200


async def test_h3_success_resets_counters(client, db):
    await make_user(db, email="reset@x.com", role=UserRole.PATIENT)
    await _fail(client, "reset@x.com", 3)
    assert (await _login(client, "reset@x.com")).status_code == 200
    user = await _profile(db, "reset@x.com")
    assert user.failed_logins == 0 and user.locked_until is None


async def test_m4_concurrent_failures_cannot_exceed_threshold(db, sessionmaker_, fake_supabase):
    """Old code: read-modify-write in Python, so parallel guesses overwrote
    each other's increments. Now each guess atomically reserves a slot."""
    await make_user(db, email="race@x.com", role=UserRole.PATIENT)
    body = LoginRequest(email="race@x.com", password="WrongPassword99")

    async def attempt() -> None:
        async with sessionmaker_() as s:
            with pytest.raises(Unauthenticated):
                await service.login(s, body)

    await asyncio.gather(*(attempt() for _ in range(25)))

    real_checks = fake_supabase.sign_in_emails.count("race@x.com")
    assert real_checks == settings.max_failed_logins
    user = await _profile(db, "race@x.com")
    assert user.failed_logins == settings.max_failed_logins
    assert user.locked_until is not None


# ══ M2 — registration atomicity ════════════════════════════════════════
async def test_m2_duplicate_pmdc_is_refused_before_supabase(client, db, clinic, fake_supabase):
    await make_user(db, email="first@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    taken = (await db.execute(select(Doctor.pmdc_number))).scalar_one()
    r = await client.post(
        "/api/v1/auth/register",
        json=_reg(
            "second@x.com",
            role="doctor",
            pmdc_number=taken,
            specialty="Cardiology",
            primary_clinic_id=str(clinic.id),
        ),
    )
    assert r.status_code == 422
    assert r.json()["error"]["details"]["fields"] == {"pmdc_number": "errors.pmdc_taken"}
    assert "admin.create_user" not in fake_supabase.calls


async def test_m2_password_rejected_by_supabase_policy(client, db, fake_supabase):
    fake_supabase.fail_next(
        "admin.create_user", AuthApiError("Password is known to be weak", 422, "weak_password")
    )
    r = await client.post("/api/v1/auth/register", json=_reg("weak@x.com"))
    assert r.status_code == 422
    assert r.json()["error"]["details"]["fields"] == {"password": "errors.password_rejected"}
    assert await _profile(db, "weak@x.com") is None


async def test_m2_failure_after_create_user_is_rolled_back(client, db, fake_supabase, monkeypatch):
    async def audit_down(*_a, **_kw):
        raise RuntimeError("audit store unreachable at 10.0.0.7")

    monkeypatch.setattr(service.audit, "write", audit_down)
    r = await client.post("/api/v1/auth/register", json=_reg("half@x.com"))
    assert r.status_code == 503
    error = r.json()["error"]
    assert error["code"] == "REGISTRATION_FAILED"
    assert "10.0.0.7" not in r.text  # no internal detail leaks
    # The Supabase user this request created is deleted again; nothing is left.
    assert len(fake_supabase.deleted_user_ids) == 1
    assert "half@x.com" not in fake_supabase.users
    assert await _profile(db, "half@x.com") is None


async def test_m2_database_failure_after_create_user_is_rolled_back(
    client, db, fake_supabase, monkeypatch
):
    async def boom(*_a, **_kw):
        raise IntegrityError("INSERT INTO patient", {}, Exception("passport collision"))

    monkeypatch.setattr(service, "provision_role_records", boom)
    r = await client.post("/api/v1/auth/register", json=_reg("dbfail@x.com"))
    assert r.status_code == 503
    assert "passport" not in r.text and "INSERT" not in r.text
    assert len(fake_supabase.deleted_user_ids) == 1
    assert await _profile(db, "dbfail@x.com") is None


async def test_m2_lost_race_never_deletes_the_winner(client, db, fake_supabase):
    """Supabase already holds the address (another request created it after
    our profile check): Supabase's unique-email constraint answers
    `email_exists`, the loser gets the duplicate 409, and the winner's
    account must NOT be deleted."""
    fake_supabase.register(str(uuid.uuid4()), "race@x.com", "SomeoneElse123")
    r = await client.post("/api/v1/auth/register", json=_reg("race@x.com"))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "EMAIL_ALREADY_REGISTERED"
    assert fake_supabase.deleted_user_ids == []
    assert fake_supabase.users["race@x.com"]["password"] == "SomeoneElse123"


@pytest.mark.parametrize(
    "emails", [("twice@x.com", "twice@x.com"), ("twice@x.com", "TWICE@X.com ")]
)
async def test_m2_concurrent_duplicate_registrations(client, db, fake_supabase, emails):
    results = await asyncio.gather(
        *(client.post("/api/v1/auth/register", json=_reg(e)) for e in emails)
    )
    assert sorted(r.status_code for r in results) == [201, 409]
    loser = next(r for r in results if r.status_code == 409)
    assert loser.json()["error"]["code"] == "EMAIL_ALREADY_REGISTERED"
    rows = await db.execute(select(Profile).where(Profile.email == "twice@x.com"))
    assert len(rows.scalars().all()) == 1
    assert list(fake_supabase.users) == ["twice@x.com"]
    assert fake_supabase.deleted_user_ids == []


async def test_m2_web_registration_failure_is_generic(client, fake_supabase):
    fake_supabase.fail_next(
        "admin.create_user", AuthApiError("SMTP 550 mailbox unavailable", 500, "unexpected_failure")
    )
    r = await client.post(
        "/en/register",
        data={
            "full_name": "Ayesha Raza",
            "email": "webfail@x.com",
            "password": TEST_PASSWORD,
            "password_confirm": TEST_PASSWORD,
            "role": "patient",
            "consent": "on",
        },
    )
    assert r.status_code == 503
    assert str(escape(translate("errors.registration_failed", "en"))) in r.text
    assert "SMTP" not in r.text


async def test_m2_email_uniqueness_is_case_insensitive_in_the_database(db):
    a = Profile(id=uuid.uuid4(), email="case@x.com", role=UserRole.PATIENT, full_name="A")
    b = Profile(id=uuid.uuid4(), email="CASE@x.com", role=UserRole.PATIENT, full_name="B")
    db.add_all([a, b])
    with pytest.raises(IntegrityError):
        await db.commit()


# ══ M1 — email enumeration ═════════════════════════════════════════════
async def test_m1_unknown_and_locked_logins_still_call_supabase(client, db, fake_supabase):
    await make_user(db, email="known@x.com", role=UserRole.PATIENT)

    before = len(fake_supabase.sign_in_emails)
    unknown = await _login(client, "nobody@x.com", "WrongPassword99")
    assert len(fake_supabase.sign_in_emails) == before + 1  # one decoy round trip

    before = len(fake_supabase.sign_in_emails)
    wrong = await _login(client, "known@x.com", "WrongPassword99")
    assert len(fake_supabase.sign_in_emails) == before + 1

    def strip(r):
        return {**r.json()["error"], "request_id": None}

    assert unknown.status_code == wrong.status_code == 401
    assert strip(unknown) == strip(wrong)


async def test_m1_locked_failure_is_identical_to_wrong_password(client, db):
    await make_user(db, email="lk@x.com", role=UserRole.PATIENT)
    await _fail(client, "lk@x.com", settings.max_failed_logins)
    locked = await _login(client, "lk@x.com")
    unknown = await _login(client, "ghost@x.com")
    assert locked.status_code == unknown.status_code == 401
    assert {**locked.json()["error"], "request_id": None} == {
        **unknown.json()["error"],
        "request_id": None,
    }


# ══ M3 — session invalidation ══════════════════════════════════════════
def _forge_access_token(user_id: uuid.UUID, *, iat: int, session_id: str = "other-device") -> str:
    return jwt.encode(
        {
            "sub": str(user_id),
            "aud": "authenticated",
            "iat": iat,
            "exp": iat + 3600,
            "session_id": session_id,
        },
        TEST_JWT_SECRET,
        algorithm="HS256",
    )


async def test_m3_access_token_replayed_after_logout_is_rejected(client, db):
    await make_user(db, email="replay@x.com", role=UserRole.PATIENT)
    await _login(client, "replay@x.com")
    stolen = client.cookies.get(settings.access_cookie_name)
    assert (await client.get("/api/v1/me")).status_code == 200

    assert (await client.post("/api/v1/auth/logout")).status_code == 204
    replay = await _me_with(client, stolen)
    assert replay.status_code == 401


async def test_m3_logout_also_kills_older_tokens_on_other_devices(client, db):
    user = await make_user(db, email="multi@x.com", role=UserRole.PATIENT)
    other_device = _forge_access_token(user.id, iat=int(time.time()) - 60)
    assert (await _me_with(client, other_device)).status_code == 200

    await _login(client, "multi@x.com")
    await client.post("/api/v1/auth/logout")
    after = await _me_with(client, other_device)
    assert after.status_code == 401


async def test_m3_token_older_than_access_lifetime_is_rejected(client, db):
    user = await make_user(db, email="old@x.com", role=UserRole.PATIENT)
    too_old = int(time.time()) - settings.access_token_minutes * 60 - 120
    token = _forge_access_token(user.id, iat=too_old)  # exp still in the future
    r = await _me_with(client, token)
    assert r.status_code == 401


async def test_m3_new_login_after_logout_works(client, db):
    await make_user(db, email="again@x.com", role=UserRole.PATIENT)
    await _login(client, "again@x.com")
    await client.post("/api/v1/auth/logout")
    assert (await _login(client, "again@x.com")).status_code == 200
    assert (await client.get("/api/v1/me")).status_code == 200


# ══ M5 — cache headers ════════════════════════════════════════════════
async def test_m5_authenticated_medical_pages_are_no_store(client, db):
    await make_user(db, email="cache@x.com", role=UserRole.PATIENT)
    await client.post("/en/login", data={"email": "cache@x.com", "password": TEST_PASSWORD})
    for path in ("/en/patient", "/en/patient/profile", "/en/patient/passport", "/api/v1/me"):
        r = await client.get(path)
        assert r.status_code == 200, path
        assert r.headers["cache-control"] == "no-store", path

    static = await client.get("/static/css/app.css")
    assert static.status_code == 200
    assert "no-store" not in static.headers.get("cache-control", "")


# ══ M6 — login / registration CSRF ═════════════════════════════════════
EVIL = {"origin": "https://evil.example"}


async def test_m6_same_origin_login_succeeds(client, db):
    await make_user(db, email="so@x.com", role=UserRole.PATIENT)
    r = await client.post(
        "/en/login",
        data={"email": "so@x.com", "password": TEST_PASSWORD},
        headers={"origin": "https://test", "referer": "https://test/en/login"},
    )
    assert r.status_code == 303
    assert settings.access_cookie_name in client.cookies


async def test_m6_cross_origin_login_is_rejected(client, db):
    await make_user(db, email="csrf@x.com", role=UserRole.PATIENT)
    web = await client.post(
        "/en/login", data={"email": "csrf@x.com", "password": TEST_PASSWORD}, headers=EVIL
    )
    assert web.status_code == 403
    assert translate("errors.cross_origin", "en") in web.text
    api = await _login(client, "csrf@x.com", headers=EVIL)
    assert api.status_code == 403
    assert api.json()["error"]["code"] == "CROSS_ORIGIN_REJECTED"
    assert settings.access_cookie_name not in client.cookies


@pytest.mark.parametrize("headers", [{"origin": "null"}, {"referer": "https://evil.example/page"}])
async def test_m6_opaque_origin_and_foreign_referer_are_rejected(client, db, headers):
    await make_user(db, email="opq@x.com", role=UserRole.PATIENT)
    r = await _login(client, "opq@x.com", headers=headers)
    assert r.status_code == 403


async def test_m6_cross_origin_registration_is_rejected(client, db, fake_supabase):
    web = await client.post(
        "/en/register",
        data={
            "full_name": "Ayesha Raza",
            "email": "xreg@x.com",
            "password": TEST_PASSWORD,
            "password_confirm": TEST_PASSWORD,
            "consent": "on",
        },
        headers=EVIL,
    )
    api = await client.post("/api/v1/auth/register", json=_reg("xreg2@x.com"), headers=EVIL)
    assert web.status_code == api.status_code == 403
    assert "admin.create_user" not in fake_supabase.calls


async def test_m6_oauth_callback_still_works_after_cross_site_hop(
    client, db, fake_supabase, monkeypatch
):
    """The callback is a cross-site navigation (Google → Supabase → us) and
    must not be subject to the same-origin rule."""
    _enable_oauth(monkeypatch)
    client.headers["referer"] = "https://accounts.google.com/"
    try:
        r = await _complete(client, fake_supabase, "oauth.csrf@gmail.com")
    finally:
        del client.headers["referer"]
    assert r.status_code == 200
    assert settings.access_cookie_name in r.cookies


# ══ L-items — input hygiene, headers, logging, consent race ═══════════
@pytest.mark.parametrize("name", ["   ", "\n\n", "Ay\x00esha", "Ali\u202eReza"])
async def test_l1_blank_or_control_char_names_are_rejected(client, db, name):
    r = await client.post("/api/v1/auth/register", json=_reg("name@x.com", full_name=name))
    assert r.status_code == 422
    assert await _profile(db, "name@x.com") is None


async def test_l2_whitespace_only_password_is_rejected(client):
    with pytest.raises(ValueError, match="errors.password_blank"):
        PatientRegisterRequest(full_name="Ayesha", email="a@x.com", password=" " * 12)
    r = await client.post("/api/v1/auth/register", json=_reg("ws@x.com", password=" " * 12))
    assert r.status_code == 422


async def test_l1_web_form_reports_the_right_field(client):
    r = await client.post(
        "/en/register",
        data={
            "full_name": "Ay\u202eesha",
            "email": "f@x.com",
            "password": TEST_PASSWORD,
            "password_confirm": TEST_PASSWORD,
            "consent": "on",
        },
    )
    assert r.status_code == 422
    assert translate("errors.text_invalid", "en") in r.text
    assert translate("errors.email_invalid", "en") not in r.text


async def test_l_security_headers(client, monkeypatch):
    r = await client.get("/en/login")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "strict-transport-security" not in r.headers  # not outside pilot
    monkeypatch.setattr(settings, "environment", "pilot")
    assert "max-age" in (await client.get("/en/login")).headers["strict-transport-security"]


@pytest.mark.parametrize(
    "supplied,echoed",
    [("abc-123_X.y", True), ("a" * 65, False), ("bad id\r\nX-Injected: 1", False)],
)
async def test_l_request_id_is_validated(client, supplied, echoed):
    try:
        r = await client.get("/healthz", headers={"x-request-id": supplied})
    except Exception:  # noqa: BLE001 — httpx itself refuses CR/LF in a header
        assert not echoed
        return
    assert (r.headers["x-request-id"] == supplied) is echoed


async def test_l_unhandled_errors_are_logged_without_detail(app):
    from structlog.testing import capture_logs

    @app.get("/_test/boom")
    async def _boom():  # type: ignore[no-untyped-def]
        raise RuntimeError("leaky@x.com CN-AAAA-BBBB")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="https://test") as c:
        with capture_logs() as logs:
            r = await c.get("/_test/boom")
    assert r.status_code == 500
    assert "leaky" not in r.text
    entry = next(e for e in logs if e["event"] == "unhandled_error")
    assert entry["error_type"] == "RuntimeError"
    assert "leaky" not in repr(logs)


async def test_l9_consent_grant_locks_the_patient_row(client, db, sessionmaker_, engine, clinic):
    """Concurrent grants are serialised by a FOR UPDATE on the patient row
    (a unique index can't express "unrevoked AND unexpired"). SQLite drops
    FOR UPDATE, so this asserts the statement is issued."""
    from app.consent import service as consent_service
    from app.deps import Actor

    patient_user = await make_user(db, email="lockgrant@x.com", role=UserRole.PATIENT)
    locking: list[str] = []

    def spy(_conn, clauseelement, *_a, **_kw):  # type: ignore[no-untyped-def]
        if getattr(clauseelement, "_for_update_arg", None) is not None:
            locking.append(str(clauseelement))

    event.listen(engine.sync_engine, "before_execute", spy)
    try:
        async with sessionmaker_() as s:
            with pytest.raises(NotFound):
                await consent_service.grant_consent(
                    s,
                    Actor(user_id=patient_user.id, role="patient"),
                    clinic_name="No Such Clinic",
                )
    finally:
        event.remove(engine.sync_engine, "before_execute", spy)
    assert any("patient" in stmt for stmt in locking)
