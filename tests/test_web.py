"""Server-rendered page tests (SPEC §5, §11 T16-T19).

Covers the browser-facing half: role landing, protected routes, form
validation and error rendering, and Urdu/RTL.
"""

from __future__ import annotations

import re

from app.db.models import Doctor, User, UserRole
from app.i18n.catalogue import missing_keys, translate
from app.settings import settings
from sqlalchemy import select

from tests.conftest import TEST_PASSWORD, make_user


def _form(**kw):
    body = {
        "full_name": "Ayesha Raza",
        "email": "web@x.com",
        "password": TEST_PASSWORD,
        "password_confirm": TEST_PASSWORD,
        "role": "patient",
        "consent": "on",
    }
    body.update(kw)
    return body


# ── T16 / AC-16 — role landing and cross-role redirect ──────────────────
async def test_t16_role_based_landing(client, db, clinic):
    await make_user(db, email="pat@x.com", role=UserRole.PATIENT)
    r = await client.post(
        "/en/login", data={"email": "pat@x.com", "password": TEST_PASSWORD}
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/en/patient"

    page = await client.get("/en/patient")
    assert page.status_code == 200

    # Deep-linking to another role's area redirects to the caller's own.
    cross = await client.get("/en/doctor")
    assert cross.status_code == 303
    assert cross.headers["location"] == "/en/patient"


async def test_t16_doctor_lands_on_doctor_area(client, db, clinic):
    await make_user(
        db, email="dr@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id
    )
    r = await client.post(
        "/en/login", data={"email": "dr@x.com", "password": TEST_PASSWORD}
    )
    assert r.headers["location"] == "/en/doctor"


# ── T17 / AC-17 — protected routes preserve the destination ─────────────
async def test_t17_protected_route_redirects_with_next(client):
    r = await client.get("/en/patient")
    assert r.status_code == 303
    assert r.headers["location"] == "/en/login?next=/en/patient"


async def test_t17_signed_in_user_skips_login_page(client, db):
    await make_user(db, email="skip@x.com", role=UserRole.PATIENT)
    await client.post(
        "/en/login", data={"email": "skip@x.com", "password": TEST_PASSWORD}
    )
    r = await client.get("/en/login")
    assert r.status_code == 303
    assert r.headers["location"] == "/en/patient"


# ── T18 / AC-18 — Urdu and RTL ──────────────────────────────────────────
async def test_t18_urdu_renders_rtl(client):
    en = await client.get("/en/login")
    ur = await client.get("/ur/login")
    assert en.status_code == ur.status_code == 200

    assert 'dir="ltr"' in en.text and 'lang="en"' in en.text
    assert 'dir="rtl"' in ur.text and 'lang="ur"' in ur.text

    # Urdu copy is actually rendered, not just the direction flipped.
    assert translate("auth.login.submit", "ur") in ur.text
    assert translate("auth.login.submit", "en") in en.text

    # The Nastaliq face loads only for Urdu (NFR1 — it is a heavy download).
    assert "Noto+Nastaliq+Urdu" in ur.text
    assert "Noto+Nastaliq+Urdu" not in en.text


async def test_t18_catalogue_is_complete():
    """A screen that has not been translated must not ship (FR28)."""
    assert missing_keys()["ur"] == []


async def test_t18_no_physical_direction_css():
    """TDD 2.3 rule 1 — logical properties only, so RTL needs no second sheet."""
    from pathlib import Path

    css = (
        Path("frontend/static/css/app.css").read_text(encoding="utf-8")
    )
    # Strip comments before scanning so prose about the rule is not a hit.
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)

    banned = [
        r"margin-left\s*:", r"margin-right\s*:",
        r"padding-left\s*:", r"padding-right\s*:",
        r"text-align\s*:\s*(left|right)",
        r"\bleft\s*:", r"\bright\s*:",
    ]
    for pattern in banned:
        hits = re.findall(pattern, css)
        assert not hits, f"physical direction property used: {pattern} -> {hits}"


# ── T19 / AC-19 — form states ───────────────────────────────────────────
async def test_t19_focus_ring_defined():
    """DESIGN.md sets outline:none with no replacement; this is the fix."""
    from pathlib import Path

    css = Path("frontend/static/css/app.css").read_text(encoding="utf-8")
    assert ":focus-visible" in css
    assert "outline: 2px solid var(--brand)" in css


async def test_t19_validation_errors_render_accessibly(client, db):
    r = await client.post(
        "/en/register", data=_form(password="short", password_confirm="short")
    )
    assert r.status_code == 422
    assert 'aria-invalid="true"' in r.text
    assert 'role="alert"' in r.text
    assert translate("errors.password_short", "en") in r.text

    # Nothing was created.
    assert (
        await db.execute(select(User).where(User.email == "web@x.com"))
    ).scalar_one_or_none() is None


async def test_t19_password_mismatch_and_consent(client, db):
    mismatch = await client.post(
        "/en/register", data=_form(password_confirm="DifferentPass123")
    )
    assert translate("errors.password_mismatch", "en") in mismatch.text

    no_consent = _form()
    del no_consent["consent"]
    r = await client.post("/en/register", data=no_consent)
    assert translate("errors.consent_required", "en") in r.text


async def test_t19_submitting_state_present():
    """The submit button must disable itself so a double-click submits once."""
    from pathlib import Path

    js = Path("frontend/static/js/auth.js").read_text(encoding="utf-8")
    assert "btn.disabled = true" in js
    assert 'aria-busy' in js


# ── Registration through the browser form ───────────────────────────────
async def test_web_patient_registration_signs_in(client, db):
    r = await client.post("/en/register", data=_form())
    assert r.status_code == 303
    assert r.headers["location"] == "/en/patient"
    assert settings.access_cookie_name in client.cookies

    page = await client.get("/en/patient")
    assert page.status_code == 200
    assert "CN-" in page.text  # passport number is shown


async def test_web_doctor_registration_shows_pending_banner(client, db, clinic):
    r = await client.post(
        "/en/register",
        data=_form(
            email="webdoc@x.com",
            role="doctor",
            pmdc_number="41192",
            specialty="Cardiology",
            primary_clinic_id=str(clinic.id),
        ),
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/en/doctor"

    page = await client.get("/en/doctor")
    assert page.status_code == 200
    # FR3 — non-dismissible awaiting-verification state.
    assert translate("doctor.pending_title", "en") in page.text
    assert clinic.name in page.text

    user = (
        await db.execute(select(User).where(User.email == "webdoc@x.com"))
    ).scalar_one()
    assert (await db.get(Doctor, user.id)).is_verified is False


async def test_web_doctor_missing_fields_rerenders(client, db, clinic):
    r = await client.post(
        "/en/register", data=_form(email="d9@x.com", role="doctor")
    )
    assert r.status_code == 422
    assert translate("errors.doctor_fields_required", "en") in r.text


async def test_web_duplicate_email_does_not_confirm_existence(client, db):
    await make_user(db, email="taken@x.com", role=UserRole.PATIENT)
    r = await client.post("/en/register", data=_form(email="taken@x.com"))
    # Sent to sign in, with no hint that the address is registered.
    assert r.status_code == 303
    assert r.headers["location"] == "/en/login"
    assert settings.access_cookie_name not in client.cookies


def _fields_from_form(html: str) -> dict[str, str]:
    """Serialise the rendered form the way a browser would.

    Earlier tests hand-wrote `role=doctor`, which passed while the template
    shipped no role input at all — the toggle was decorative and the server
    had nothing to enforce. Deriving the payload from the HTML is what catches
    that, but only if radios and checkboxes obey `checked` like a real form.
    """
    fields: dict[str, str] = {}
    for tag in re.findall(r"<input\b[^>]*>", html):
        name = re.search(r'\bname="([^"]+)"', tag)
        if not name:
            continue
        kind = (re.search(r'\btype="([^"]+)"', tag) or [None, "text"])[1]
        value = re.search(r'\bvalue="([^"]*)"', tag)
        if kind in ("radio", "checkbox"):
            # Unchecked controls are simply not submitted.
            if "checked" not in tag:
                continue
            fields[name.group(1)] = value.group(1) if value else "on"
        else:
            fields[name.group(1)] = value.group(1) if value else ""
    return fields


async def test_login_form_actually_submits_the_selected_role(client, db):
    """Regression: the toggle must be wired to a real form field."""
    page = await client.get("/en/login")
    fields = _fields_from_form(page.text)
    assert "role" in fields, "login form ships no role input — toggle is decorative"
    assert fields["role"] == "patient"  # default selection

    # The Urdu page must carry it too.
    ur = await client.get("/ur/login")
    assert "role" in _fields_from_form(ur.text)


async def test_login_via_browser_derived_payload_enforces_role(client, db, clinic):
    """End-to-end: read the form, flip the toggle, submit exactly what a
    browser would send."""
    await make_user(db, email="browser@x.com", role=UserRole.PATIENT)

    page = await client.get("/en/login")
    payload = _fields_from_form(page.text)
    payload.update({"email": "browser@x.com", "password": TEST_PASSWORD})

    # Toggle set to Doctor, exactly as auth.js would.
    payload["role"] = "doctor"
    rejected = await client.post("/en/login", data=payload)
    assert rejected.status_code == 403
    assert settings.access_cookie_name not in client.cookies

    # Toggle back to Patient.
    payload["role"] = "patient"
    ok = await client.post("/en/login", data=payload)
    assert ok.status_code == 303
    assert ok.headers["location"] == "/en/patient"


async def test_login_page_keeps_toggle_after_rejection(client, db, clinic):
    """The rejected page must come back with Doctor still selected, or the
    error reads as nonsense."""
    await make_user(db, email="keep@x.com", role=UserRole.PATIENT)
    r = await client.post(
        "/en/login",
        data={"email": "keep@x.com", "password": TEST_PASSWORD, "role": "doctor"},
    )
    assert r.status_code == 403
    # The Doctor radio comes back checked, so the error reads coherently.
    assert _fields_from_form(r.text)["role"] == "doctor"
    assert re.search(r'value="doctor"[^>]*checked|checked[^>]*value="doctor"', r.text)


async def test_web_login_role_mismatch_is_rejected(client, db, clinic):
    """AC-23 — selecting the wrong role is refused, and grants no session.

    The check is post-authentication, so it can only be reached by someone who
    already knows the password.
    """
    await make_user(db, email="tg@x.com", role=UserRole.PATIENT)
    r = await client.post(
        "/en/login",
        data={"email": "tg@x.com", "password": TEST_PASSWORD, "role": "doctor"},
    )
    assert r.status_code == 403
    assert translate("errors.role_mismatch", "en").split("{")[0] in r.text
    # No session was handed over.
    assert settings.access_cookie_name not in client.cookies
    assert (await client.get("/en/patient")).status_code == 303


async def test_web_login_role_mismatch_both_directions(client, db, clinic):
    await make_user(
        db, email="realdoc@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id
    )
    r = await client.post(
        "/en/login",
        data={"email": "realdoc@x.com", "password": TEST_PASSWORD, "role": "patient"},
    )
    assert r.status_code == 403
    assert settings.access_cookie_name not in client.cookies


async def test_web_login_matching_role_succeeds(client, db, clinic):
    await make_user(db, email="ok@x.com", role=UserRole.PATIENT)
    r = await client.post(
        "/en/login",
        data={"email": "ok@x.com", "password": TEST_PASSWORD, "role": "patient"},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/en/patient"
    assert settings.access_cookie_name in client.cookies


async def test_web_login_role_mismatch_needs_correct_password(client, db):
    """The mismatch message must never be an unauthenticated oracle."""
    await make_user(db, email="oracle@x.com", role=UserRole.PATIENT)
    r = await client.post(
        "/en/login",
        data={"email": "oracle@x.com", "password": "WrongPassword1", "role": "doctor"},
    )
    # Generic 401 — indistinguishable from any other credential failure.
    assert r.status_code == 401
    assert translate("errors.unauthenticated", "en") in r.text
    assert translate("errors.role_mismatch", "en").split("{")[0] not in r.text


async def test_web_logout_clears_session(client, db):
    await make_user(db, email="lo@x.com", role=UserRole.PATIENT)
    await client.post("/en/login", data={"email": "lo@x.com", "password": TEST_PASSWORD})
    r = await client.post("/en/logout")
    assert r.status_code == 303
    assert r.headers["location"] == "/en/login"

    after = await client.get("/en/patient")
    assert after.status_code == 303  # back to login


async def test_web_invalid_credentials_show_banner(client, db):
    await make_user(db, email="bad@x.com", role=UserRole.PATIENT)
    r = await client.post(
        "/en/login", data={"email": "bad@x.com", "password": "WrongPassword1"}
    )
    assert r.status_code == 401
    assert translate("errors.unauthenticated", "en") in r.text
    assert "banner--error" in r.text


# ── Design-system conformance ───────────────────────────────────────────
async def test_design_tokens_match_design_doc():
    from pathlib import Path

    css = Path("frontend/static/css/tokens.css").read_text(encoding="utf-8")
    for token, value in [
        ("--brand", "#0F5C56"),
        ("--ai", "#6B5FD9"),
        ("--ok", "#2F8F5B"),
        ("--warn", "#C68A1F"),
        ("--err", "#C0432E"),
        ("--ink", "#1A2422"),
        ("--surface", "#FFFFFF"),
        ("--bg", "#F6F4F0"),
    ]:
        assert f"{token}: {value};" in css, f"{token} does not match DESIGN.md"
    # Dark theme must redefine the palette, not patch components.
    assert '[data-theme="dark"]' in css
    assert "#3E9A91" in css  # dark brand


async def test_no_raw_hex_in_app_css():
    """DESIGN.md §8 rule 1 — colour comes only from tokens."""
    from pathlib import Path

    css = Path("frontend/static/css/app.css").read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    hexes = set(re.findall(r"#[0-9a-fA-F]{3,8}\b", css))
    # #fff on a brand fill is literal in DESIGN.md itself (white on --brand).
    assert hexes <= {"#fff"}, f"raw hex outside tokens.css: {hexes}"
