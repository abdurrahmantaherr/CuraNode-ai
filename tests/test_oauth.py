"""OAuth (Google) sign-in tests — docs/oauth.md §11, T1-T14.

Each test that needs to walk the full redirect chain does it in three
requests against the same `client`, exactly the way a browser would:

  1. POST /en/auth/oauth/google   -> 303 to the (fake) provider authorize URL
  2. the "browser" completes sign-in at Google/Supabase (simulated by
     `fake_supabase.authorize(...)`, which mints a code bound to the PKCE
     challenge the start route sent)
  3. GET /auth/callback?code=...&state=...

httpx's `AsyncClient` stores cookies across requests on the same client
instance automatically, so the `cn_oauth_state` cookie set in step 1 rides
along into step 3 exactly as a real browser's would.
"""

from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlparse

from app.db.models import AccountStatus, Doctor, Patient, Profile, UserRole
from app.i18n.catalogue import missing_keys
from app.settings import settings as app_settings
from sqlalchemy import select

from tests.conftest import make_user


def _enable_oauth(monkeypatch) -> None:
    monkeypatch.setattr(app_settings, "oauth_enabled", True)
    monkeypatch.setattr(app_settings, "oauth_providers", "google")
    monkeypatch.setattr(app_settings, "supabase_anon_key", "test-anon-key")


async def _start(client) -> tuple[str, str]:
    """Runs step 1 and returns (state, code_challenge) parsed off the
    redirect Location. State is NOT a top-level query param on the Supabase
    authorize URL (it collides with GoTrue's own state handling) — it's
    embedded inside the `redirect_to` param's own query string instead."""
    r = await client.post("/en/auth/oauth/google")
    assert r.status_code == 303
    qs = parse_qs(urlparse(r.headers["location"]).query)
    assert "state" not in qs
    redirect_to_qs = parse_qs(urlparse(qs["redirect_to"][0]).query)
    assert "cn_oauth_state" in r.cookies
    return redirect_to_qs["state"][0], qs["code_challenge"][0]


async def _complete(client, fake_supabase, email, *, user_metadata=None, user_id=None):
    """Runs steps 1-3 for a fresh sign-in and returns the callback response."""
    state, challenge = await _start(client)
    code = fake_supabase.authorize(
        "google", email, challenge=challenge, user_metadata=user_metadata, user_id=user_id
    )
    return await client.get(f"/auth/callback?code={code}&state={state}")


# ── T1 ────────────────────────────────────────────────────────────────────
async def test_t1_start_redirects_to_authorize_with_state_cookie(
    client, fake_supabase, monkeypatch
):
    _enable_oauth(monkeypatch)
    state, challenge = await _start(client)
    assert state
    assert challenge


# ── T2 ────────────────────────────────────────────────────────────────────
async def test_t2_disabled_oauth_is_refused(client, fake_supabase, monkeypatch):
    # Explicit, not relied on as an ambient default — a developer's local
    # .env may itself set OAUTH_ENABLED=true, and pydantic-settings reads
    # that regardless of test context (conftest.py never overrides this var).
    monkeypatch.setattr(app_settings, "oauth_enabled", False)
    r = await client.post("/en/auth/oauth/google")
    assert r.status_code == 400
    assert "location" not in r.headers
    assert "cn_oauth_state" not in r.cookies


async def test_t2_unknown_provider_is_refused(client, fake_supabase, monkeypatch):
    _enable_oauth(monkeypatch)
    r = await client.post("/en/auth/oauth/facebook")
    assert r.status_code == 400
    assert "location" not in r.headers


# ── T3 ────────────────────────────────────────────────────────────────────
async def test_t3_callback_missing_state_is_rejected(client, fake_supabase, monkeypatch):
    _enable_oauth(monkeypatch)
    r = await client.get("/auth/callback?code=anything")
    assert r.status_code == 400
    assert "cn_access" not in r.cookies


async def test_t3_callback_state_not_matching_cookie_is_rejected(
    client, fake_supabase, monkeypatch
):
    _enable_oauth(monkeypatch)
    _state, challenge = await _start(client)
    code = fake_supabase.authorize("google", "x@example.com", challenge=challenge)
    r = await client.get(f"/auth/callback?code={code}&state=not-the-real-state")
    assert r.status_code == 400
    assert "cn_access" not in r.cookies


# ── T4 ────────────────────────────────────────────────────────────────────
async def test_t4_replayed_state_is_rejected(client, db, fake_supabase, monkeypatch):
    _enable_oauth(monkeypatch)
    state, challenge = await _start(client)
    code = fake_supabase.authorize("google", "replay@example.com", challenge=challenge)

    r1 = await client.get(f"/auth/callback?code={code}&state={state}")
    assert r1.status_code == 200
    assert "cn_access" in r1.cookies

    # The success path clears the state cookie, so restore it manually here
    # to isolate "the cache entry was already consumed" (what this test is
    # actually about) from "the cookie is gone" as the rejection reason.
    client.cookies.set("cn_oauth_state", state)
    r2 = await client.get(f"/auth/callback?code={code}&state={state}")
    assert r2.status_code == 400
    # No second session was issued for the replay.
    assert r2.cookies.get("cn_access") in (None, r1.cookies.get("cn_access"))


# ── T5 ────────────────────────────────────────────────────────────────────
async def test_t5_brand_new_user_lands_on_onboarding(client, db, fake_supabase, monkeypatch):
    _enable_oauth(monkeypatch)
    r = await _complete(
        client, fake_supabase, "newuser@example.com", user_metadata={"full_name": "Newuser Test"}
    )
    assert r.status_code == 200
    assert "/en/onboarding" in r.text
    assert "cn_access" in r.cookies
    assert "cn_refresh" in r.cookies

    row = await db.execute(select(Profile).where(Profile.email == "newuser@example.com"))
    profile = row.scalar_one()
    assert profile.status == AccountStatus.ACTIVE
    assert profile.full_name == "Newuser Test"


# ── T6 ────────────────────────────────────────────────────────────────────
async def test_t6_onboarding_as_patient_creates_patient_row(client, db, fake_supabase, monkeypatch):
    _enable_oauth(monkeypatch)
    await _complete(client, fake_supabase, "patient-onboard@example.com")

    r = await client.post(
        "/en/onboarding",
        data={"full_name": "Patient Onboard", "role": "patient", "consent": "on"},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/en/patient"

    profile_row = await db.execute(
        select(Profile).where(Profile.email == "patient-onboard@example.com")
    )
    profile = profile_row.scalar_one()
    assert profile.role == UserRole.PATIENT

    patient_row = await db.execute(select(Patient).where(Patient.user_id == profile.id))
    patient = patient_row.scalar_one()
    assert patient.passport_no

    page = await client.get("/en/patient")
    assert page.status_code == 200


# ── T7 ────────────────────────────────────────────────────────────────────
async def test_t7_onboarding_as_doctor_creates_pending_doctor(
    client, db, clinic, fake_supabase, monkeypatch
):
    _enable_oauth(monkeypatch)
    await _complete(client, fake_supabase, "doctor-onboard@example.com")

    r = await client.post(
        "/en/onboarding",
        data={
            "full_name": "Doctor Onboard",
            "role": "doctor",
            "pmdc_number": "OAUTH-PMDC-1",
            "specialty": "Cardiology",
            "primary_clinic_id": str(clinic.id),
            "consent": "on",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/en/doctor"

    profile_row = await db.execute(
        select(Profile).where(Profile.email == "doctor-onboard@example.com")
    )
    profile = profile_row.scalar_one()
    assert profile.role == UserRole.DOCTOR

    doctor_row = await db.execute(select(Doctor).where(Doctor.user_id == profile.id))
    doctor = doctor_row.scalar_one()
    assert doctor.verification_status.value == "pending"

    page = await client.get("/en/doctor")
    assert page.status_code == 200
    assert "pending" in page.text.lower() or "verification" in page.text.lower()


# ── T8 ────────────────────────────────────────────────────────────────────
async def test_t8_returning_complete_user_skips_onboarding(client, db, fake_supabase, monkeypatch):
    _enable_oauth(monkeypatch)
    await _complete(client, fake_supabase, "returning@example.com")
    await client.post(
        "/en/onboarding",
        data={"full_name": "Returning User", "role": "patient", "consent": "on"},
    )
    await client.post("/en/logout")

    r = await _complete(client, fake_supabase, "returning@example.com")
    assert r.status_code == 200
    assert "/en/patient" in r.text
    assert "/en/onboarding" not in r.text


# ── T9 ────────────────────────────────────────────────────────────────────
async def test_t9_suspended_profile_gets_generic_failure(client, db, fake_supabase, monkeypatch):
    _enable_oauth(monkeypatch)
    user = await make_user(
        db,
        email="suspended-oauth@example.com",
        role=UserRole.PATIENT,
        status=AccountStatus.SUSPENDED,
    )

    r = await _complete(client, fake_supabase, "suspended-oauth@example.com", user_id=str(user.id))
    assert r.status_code == 400
    assert "cn_access" not in r.cookies
    assert len(fake_supabase.revoked_access_tokens) == 1


# ── T10 ───────────────────────────────────────────────────────────────────
async def test_t10_email_collision_with_different_identity_is_refused(
    client, db, fake_supabase, monkeypatch
):
    _enable_oauth(monkeypatch)
    await make_user(db, email="collision@example.com", role=UserRole.PATIENT)

    # A *different* Supabase auth identity (different user_id) sharing the
    # same email — the real-world shape of an unlinked OAuth identity.
    r = await _complete(client, fake_supabase, "collision@example.com", user_id=str(uuid.uuid4()))
    assert r.status_code == 400
    assert "cn_access" not in r.cookies
    assert len(fake_supabase.revoked_access_tokens) == 1


# ── T11 ───────────────────────────────────────────────────────────────────
async def test_t11_deep_link_mid_onboarding_bounces_to_onboarding(
    client, db, fake_supabase, monkeypatch
):
    _enable_oauth(monkeypatch)
    await _complete(client, fake_supabase, "midonboard@example.com")

    r = await client.get("/en/patient")
    assert r.status_code == 303
    assert r.headers["location"] == "/en/onboarding"


# ── T12 ───────────────────────────────────────────────────────────────────
def test_t12_catalogue_still_complete():
    assert missing_keys() == {"ur": []}


# ── T13 ───────────────────────────────────────────────────────────────────
async def test_t13_state_cookie_attributes(client, fake_supabase, monkeypatch):
    _enable_oauth(monkeypatch)
    r = await client.post("/en/auth/oauth/google")
    raw = r.headers.get_list("set-cookie")
    state_cookie = next(c for c in raw if c.startswith("cn_oauth_state="))
    assert "HttpOnly" in state_cookie
    assert "SameSite=lax" in state_cookie or "SameSite=Lax" in state_cookie
    assert "Max-Age=" in state_cookie


# ── Additional edge cases (docs/oauth.md §12) ────────────────────────────
async def test_provider_cancellation_is_a_generic_failure(client, fake_supabase, monkeypatch):
    _enable_oauth(monkeypatch)
    state, _challenge = await _start(client)
    r = await client.get(f"/auth/callback?state={state}&error=access_denied")
    assert r.status_code == 400
    assert "cn_access" not in r.cookies


async def test_unknown_state_is_rejected(client, fake_supabase, monkeypatch):
    _enable_oauth(monkeypatch)
    r = await client.get("/auth/callback?code=whatever&state=never-issued")
    assert r.status_code == 400


async def test_bad_verifier_is_rejected(client, fake_supabase, monkeypatch):
    """A code minted for one PKCE challenge cannot be redeemed by another
    pending authorization's verifier — exercises the exchange itself."""
    _enable_oauth(monkeypatch)
    state, _real_challenge = await _start(client)
    # Mint a code bound to an unrelated challenge — as if an attacker
    # captured someone else's code and tried it against this state.
    code = fake_supabase.authorize("google", "attacker@example.com", challenge="not-our-challenge")
    r = await client.get(f"/auth/callback?code={code}&state={state}")
    assert r.status_code == 400
    assert "cn_access" not in r.cookies


async def test_disabled_oauth_leaves_password_flow_untouched(client, db):
    """Whatever `oauth_enabled` happens to be must not affect the password
    paths at all — the whole point of the kill switch (docs/oauth.md §13)."""
    from tests.conftest import TEST_PASSWORD

    await make_user(db, email="password-only@example.com", role=UserRole.PATIENT)
    r = await client.post(
        "/en/login", data={"email": "password-only@example.com", "password": TEST_PASSWORD}
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/en/patient"
