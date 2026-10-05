"""Medical Passport page tests (FR1, FR4 — server-rendered half).

`tests/test_consent.py` covers the JSON API; this file covers the pages, the
same split as `test_auth.py` / `test_web.py`. `test_tN_*` covers item N of the
page test plan below. Its numbering is NOT the spec's AC numbering; where a
test also proves a spec criterion, the comment says which.

     8 passport page shows passport_no + QR     12 lookup without a grant → 404
     9 logged out → login, destination kept    13 Urdu renders RTL, catalogue complete
    10 lookup page for a verified doctor        14 QR is inline SVG, no JavaScript
    11 lookup with a grant → patient page; passport_no not in the URL

Also covered: the access page's grant/revoke forms, and the doctor-side
spec criteria AC-08…AC-12 (no grant / revoked / immediate revoke / no
passport number in logs).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import segno
from app.db.models import AuditLog, ConsentGrant, Doctor, Patient, Profile, UserRole
from app.db.types import utcnow, uuid7
from app.i18n.catalogue import missing_keys, translate
from markupsafe import escape
from sqlalchemy import select

from tests.conftest import TEST_PASSWORD, make_user

LOOKUP = "/en/doctor/passport-lookup"


# ── Helpers ──────────────────────────────────────────────────────────────
async def login(client, email: str) -> None:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": TEST_PASSWORD})
    assert r.status_code == 200, r.text


def in_html(key: str, locale: str = "en", **params) -> str:
    """A translated message as it appears in autoescaped HTML (' -> &#39;)."""
    return str(escape(translate(key, locale, **params)))


# The app writes through its own session, so the test session re-reads with
# `populate_existing` instead of `expire_all()`.
async def patient_of(db, user: Profile) -> Patient:
    stmt = select(Patient).where(Patient.user_id == user.id)
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one()


async def doctor_of(db, user: Profile) -> Doctor:
    stmt = select(Doctor).where(Doctor.user_id == user.id)
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one()


async def audit_rows(db, prefix: str = "consent.") -> list[AuditLog]:
    rows = await db.execute(
        select(AuditLog)
        .where(AuditLog.action.like(f"{prefix}%"))
        .order_by(AuditLog.id)
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


def field_errors(html: str) -> list[str]:
    found = re.findall(
        r'class="field-error"[^>]*>\s*<span aria-hidden="true">!</span>([^<]*)', html
    )
    return [e.strip() for e in found]


def without(html: str, *values: str) -> str:
    """Page HTML with the echoed form value removed, for D2 comparisons."""
    for v in values:
        html = html.replace(v, "")
    return html


@pytest.fixture
async def admin(db, clinic) -> Profile:
    return await make_user(db, email="adm@x.com", role=UserRole.CLINIC_ADMIN, clinic_id=clinic.id)


@pytest.fixture
async def verified_doctor(db, clinic, admin) -> Doctor:
    user = await make_user(
        db,
        email="dr@x.com",
        role=UserRole.DOCTOR,
        clinic_id=clinic.id,
        is_verified=True,
        verifier_id=admin.id,
    )
    return await doctor_of(db, user)


@pytest.fixture
async def patient_user(db) -> Profile:
    return await make_user(db, email="pat@x.com", role=UserRole.PATIENT)


async def grant_to(db, patient: Patient, doctor: Doctor, *, revoked: bool = False) -> ConsentGrant:
    row = ConsentGrant(
        id=uuid7(),
        patient_id=patient.id,
        grantee_type="doctor",
        grantee_id=doctor.id,
        scope_sections=[],
        granted_at=utcnow(),
        revoked_at=utcnow() if revoked else None,
    )
    db.add(row)
    await db.commit()
    return row


# ── T8 — passport page (spec AC-01) ──────────────────────────────────────
async def test_t8_passport_page_renders_number_and_qr(client, db, patient_user):
    patient = await patient_of(db, patient_user)
    await login(client, "pat@x.com")
    r = await client.get("/en/patient/passport")
    assert r.status_code == 200
    assert patient.passport_no in r.text
    assert in_html("passport.title") in r.text
    assert in_html("passport.qr_help") in r.text
    assert 'class="qr__svg"' in r.text
    assert 'href="/en/patient/access"' in r.text  # link to access management
    # Reachable from the dashboard and the top nav.
    dash = await client.get("/en/patient")
    assert 'href="/en/patient/passport"' in dash.text
    assert 'href="/en/patient/access"' in dash.text
    assert 'aria-current="page"' in r.text and in_html("nav.passport") in r.text


async def test_t8_qr_encodes_passport_no_alone(client, db, patient_user):
    """BL-11 — the code's modules are exactly those of a code for the bare
    passport number: no name, no URL, nothing else."""
    patient = await patient_of(db, patient_user)
    await login(client, "pat@x.com")
    html = (await client.get("/en/patient/passport")).text
    page_svg = re.search(r"<svg\b.*?</svg>", html, flags=re.DOTALL).group(0)
    # The modules (the dark path) must be those of a code for the bare number.
    expected = segno.make(patient.passport_no, micro=False).svg_inline(scale=6, border=4)
    modules = re.search(r' d="(M4[^"]+)"', expected).group(1)
    assert f'd="{modules}"' in page_svg
    assert page_svg.count("<path") == 2  # light background + modules, nothing else


# ── T9 — logged out ──────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("url", "target"),
    [
        ("/en/patient/passport", "/en/patient/passport"),
        ("/en/patient/access", "/en/patient/access"),
        (LOOKUP, LOOKUP),
        (f"/en/doctor/patient/{uuid7()}", LOOKUP),
    ],
)
async def test_t9_logged_out_redirects_to_login(client, url, target):
    r = await client.get(url)
    assert r.status_code == 303
    assert r.headers["location"] == f"/en/login?next={target}"


async def test_t9_logged_out_posts_redirect_to_login(client):
    for url in ("/en/patient/access", f"/en/patient/access/{uuid7()}/revoke", LOOKUP):
        r = await client.post(url, data={"grantee_type": "doctor", "grantee": "x"})
        assert r.status_code == 303 and r.headers["location"].startswith("/en/login"), url


async def test_t9_wrong_role_goes_to_own_area(client, db, patient_user, verified_doctor):
    await login(client, "pat@x.com")
    assert (await client.get(LOOKUP)).headers["location"] == "/en/patient"
    await login(client, "dr@x.com")
    for url in ("/en/patient/passport", "/en/patient/access"):
        assert (await client.get(url)).headers["location"] == "/en/doctor"


# ── T10 — lookup page ────────────────────────────────────────────────────
async def test_t10_lookup_page_for_verified_doctor(client, db, verified_doctor):
    await login(client, "dr@x.com")
    r = await client.get(LOOKUP)
    assert r.status_code == 200
    assert in_html("lookup.title") in r.text
    assert 'name="passport_no"' in r.text
    assert f'action="{LOOKUP}"' in r.text
    assert f'href="{LOOKUP}"' in (await client.get("/en/doctor")).text


async def test_t10_unverified_doctor_gets_no_lookup(client, db, clinic):
    await make_user(db, email="pending@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    await login(client, "pending@x.com")
    assert (await client.get(LOOKUP)).headers["location"] == "/en/doctor"
    post = await client.post(LOOKUP, data={"passport_no": "CN-AAAA-AAAA"})
    assert post.headers["location"] == "/en/doctor"
    # FR3 — no patient-data affordance anywhere behind the banner.
    assert "passport-lookup" not in (await client.get("/en/doctor")).text


# ── T11 — lookup with a grant (spec AC-10, AC-11, AC-12) ─────────────────
async def test_t11_lookup_with_grant_redirects_without_passport_no(
    client, db, patient_user, verified_doctor
):
    patient = await patient_of(db, patient_user)
    await grant_to(db, patient, verified_doctor)
    await login(client, "dr@x.com")

    # Surrounding whitespace and lower case are forgiven.
    r = await client.post(LOOKUP, data={"passport_no": f"  {patient.passport_no.lower()} "})
    assert r.status_code == 303
    location = r.headers["location"]
    assert location == f"/en/doctor/patient/{patient.id}"
    assert patient.passport_no not in location
    assert patient.passport_no.lower() not in location

    page = await client.get(location)
    assert page.status_code == 200
    assert patient.passport_no in page.text
    assert in_html("doctor_patient.access_confirmed") in page.text


async def test_t11_revoke_takes_effect_on_next_request(client, db, patient_user, verified_doctor):
    """Spec AC-11 / BL-07 — no delay, no re-login."""
    patient = await patient_of(db, patient_user)
    grant = await grant_to(db, patient, verified_doctor)
    await login(client, "dr@x.com")
    location = (await client.post(LOOKUP, data={"passport_no": patient.passport_no})).headers[
        "location"
    ]
    assert (await client.get(location)).status_code == 200

    grant = await db.get(ConsentGrant, grant.id, populate_existing=True)
    grant.revoked_at = utcnow()
    await db.commit()

    after = await client.get(location)
    assert after.status_code == 404
    assert in_html("lookup.not_found") in after.text
    assert patient.passport_no not in after.text


async def test_t11_no_passport_no_in_logs(client, db, patient_user, verified_doctor, caplog):
    """Spec AC-12."""
    import structlog.testing

    patient = await patient_of(db, patient_user)
    doctor = verified_doctor
    await login(client, "pat@x.com")
    with structlog.testing.capture_logs() as captured:
        await client.get("/en/patient/passport")
        await client.post(
            "/en/patient/access", data={"grantee_type": "doctor", "grantee": doctor.pmdc_number}
        )
        await login(client, "dr@x.com")
        location = (await client.post(LOOKUP, data={"passport_no": patient.passport_no})).headers[
            "location"
        ]
        await client.get(location)
        await client.post(LOOKUP, data={"passport_no": "CN-ZZZZ-ZZZZ"})
    dumped = json.dumps(captured, default=str) + caplog.text
    for v in (patient.passport_no, doctor.pmdc_number, "CN-ZZZZ-ZZZZ"):
        assert v not in dumped


# ── T12 — lookup without a grant (spec AC-08, AC-09) ─────────────────────
async def test_t12_lookup_without_grant_is_404(client, db, patient_user, verified_doctor):
    patient = await patient_of(db, patient_user)
    await login(client, "dr@x.com")
    r = await client.post(LOOKUP, data={"passport_no": patient.passport_no})
    assert r.status_code == 404
    assert field_errors(r.text) == [in_html("lookup.not_found")]
    # Direct navigation to the patient page fails the same way.
    direct = await client.get(f"/en/doctor/patient/{patient.id}")
    assert direct.status_code == 404
    assert patient.passport_no not in direct.text


async def test_t12_no_grant_revoked_and_unknown_are_indistinguishable(
    client, db, clinic, admin, verified_doctor
):
    """D2 / BL-10 — apart from echoing the typed value back into the input,
    the pages are byte-identical."""
    never = await patient_of(db, await make_user(db, email="a@x.com", role=UserRole.PATIENT))
    revoked = await patient_of(db, await make_user(db, email="b@x.com", role=UserRole.PATIENT))
    await grant_to(db, revoked, verified_doctor, revoked=True)
    # A grant to a different doctor doesn't count either.
    other = await doctor_of(
        db,
        await make_user(
            db,
            email="dr2@x.com",
            role=UserRole.DOCTOR,
            clinic_id=clinic.id,
            is_verified=True,
            verifier_id=admin.id,
        ),
    )
    elsewhere = await patient_of(db, await make_user(db, email="c@x.com", role=UserRole.PATIENT))
    await grant_to(db, elsewhere, other)
    await login(client, "dr@x.com")

    pages = {}
    for label, pno in (
        ("never", never.passport_no),
        ("revoked", revoked.passport_no),
        ("other doctor", elsewhere.passport_no),
        ("unknown", "CN-ZZZZ-ZZZZ"),
    ):
        r = await client.post(LOOKUP, data={"passport_no": pno})
        assert r.status_code == 404, label
        pages[label] = without(r.text, pno)
    assert len(set(pages.values())) == 1

    direct = {
        p.id: await client.get(f"/en/doctor/patient/{p.id}") for p in (never, revoked, elsewhere)
    }
    direct[uuid7()] = await client.get(f"/en/doctor/patient/{uuid7()}")
    direct["bad"] = await client.get("/en/doctor/patient/not-a-uuid")
    assert {r.status_code for r in direct.values()} == {404}
    assert len({r.text for r in direct.values()}) == 1


async def test_t12_blank_lookup_is_rejected(client, db, verified_doctor):
    await login(client, "dr@x.com")
    r = await client.post(LOOKUP, data={"passport_no": "   "})
    assert r.status_code == 422
    assert field_errors(r.text) == [in_html("errors.text_required")]


# ── T13 — Urdu (spec AC-13) ──────────────────────────────────────────────
async def test_t13_urdu_pages_render_rtl(client, db, patient_user, verified_doctor):
    await login(client, "pat@x.com")
    for url, key in (
        ("/ur/patient/passport", "passport.title"),
        ("/ur/patient/access", "access.title"),
    ):
        ur = await client.get(url)
        assert ur.status_code == 200
        assert 'dir="rtl"' in ur.text and 'lang="ur"' in ur.text
        assert in_html(key, "ur") in ur.text
        # The language switch keeps the user on the same page.
        assert f'href="/en{url[3:]}"' in ur.text
    en = await client.get("/en/patient/passport")
    assert 'dir="ltr"' in en.text and 'href="/ur/patient/passport"' in en.text

    await login(client, "dr@x.com")
    ur = await client.get("/ur/doctor/passport-lookup")
    assert 'dir="rtl"' in ur.text and in_html("lookup.title", "ur") in ur.text


async def test_t13_catalogue_is_complete():
    assert missing_keys()["ur"] == []
    en = json.loads(Path("backend/app/i18n/messages/en.json").read_text(encoding="utf-8"))
    for prefix in ("passport.", "access.", "lookup.", "doctor_patient."):
        assert any(k.startswith(prefix) for k in en), prefix


# ── T14 — QR without JavaScript (spec AC-14) ─────────────────────────────
async def test_t14_qr_is_inline_svg_without_script(client, db, patient_user):
    await login(client, "pat@x.com")
    html = (await client.get("/en/patient/passport")).text
    assert "<script" not in html.lower()
    assert re.search(r"<svg\b[^>]*class=\"qr__svg\"", html)
    assert "<img" not in html  # inline, not a separate request
    assert 'role="img"' in html and in_html("passport.qr_title") in html


# ── Access page forms ────────────────────────────────────────────────────
async def test_access_page_grant_and_revoke(client, db, patient_user, verified_doctor, clinic):
    await login(client, "pat@x.com")
    empty = await client.get("/en/patient/access")
    assert empty.status_code == 200 and in_html("access.empty") in empty.text

    r = await client.post(
        "/en/patient/access",
        data={"grantee_type": "doctor", "grantee": verified_doctor.pmdc_number},
    )
    assert r.status_code == 303 and r.headers["location"] == "/en/patient/access?saved=granted"
    await client.post("/en/patient/access", data={"grantee_type": "clinic", "grantee": clinic.name})

    page = (await client.get("/en/patient/access?saved=granted")).text
    assert in_html("access.saved.granted") in page
    assert verified_doctor.full_name in page and clinic.name in page
    ids = re.findall(r'action="/en/patient/access/([0-9a-f-]{36})/revoke"', page)
    assert len(ids) == 2

    r = await client.post(f"/en/patient/access/{ids[0]}/revoke")
    assert r.status_code == 303 and r.headers["location"] == "/en/patient/access?saved=revoked"
    # Idempotent: the same form submitted twice is not an error.
    assert (await client.post(f"/en/patient/access/{ids[0]}/revoke")).status_code == 303
    page = (await client.get("/en/patient/access")).text
    assert ids[0] not in page and ids[1] in page
    assert [a.action for a in await audit_rows(db)] == [
        "consent.grant",
        "consent.grant",
        "consent.revoke",
    ]

    # Unknown `saved` values are never reflected.
    assert "<b>" not in (await client.get("/en/patient/access?saved=<b>")).text


@pytest.mark.parametrize(
    ("data", "status", "key"),
    [
        ({"grantee_type": "doctor", "grantee": "NO-SUCH"}, 404, "errors.grantee_doctor_not_found"),
        ({"grantee_type": "clinic", "grantee": "No Such"}, 404, "errors.grantee_clinic_not_found"),
        ({"grantee_type": "doctor", "grantee": "  "}, 422, "errors.text_required"),
        ({"grantee_type": "nurse", "grantee": "x"}, 422, "errors.choice_invalid"),
    ],
)
async def test_access_page_grant_errors(client, db, patient_user, data, status, key):
    await login(client, "pat@x.com")
    r = await client.post("/en/patient/access", data=data)
    assert r.status_code == status
    assert field_errors(r.text) == [in_html(key)]
    assert await audit_rows(db) == []


async def test_access_page_unverified_doctor_reads_as_unknown(client, db, patient_user, clinic):
    user = await make_user(db, email="pending@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    pending = await doctor_of(db, user)
    await login(client, "pat@x.com")
    unverified = await client.post(
        "/en/patient/access", data={"grantee_type": "doctor", "grantee": pending.pmdc_number}
    )
    unknown = await client.post(
        "/en/patient/access", data={"grantee_type": "doctor", "grantee": "NO-SUCH"}
    )
    assert unverified.status_code == unknown.status_code == 404
    assert without(unverified.text, pending.pmdc_number) == without(unknown.text, "NO-SUCH")


async def test_access_page_duplicate_and_foreign_revoke(client, db, patient_user, verified_doctor):
    other = await patient_of(db, await make_user(db, email="o@x.com", role=UserRole.PATIENT))
    foreign = await grant_to(db, other, verified_doctor)
    await login(client, "pat@x.com")
    form = {"grantee_type": "doctor", "grantee": verified_doctor.pmdc_number}
    assert (await client.post("/en/patient/access", data=form)).status_code == 303
    dup = await client.post("/en/patient/access", data=form)
    assert dup.status_code == 409
    assert field_errors(dup.text) == [in_html("errors.already_granted")]

    r = await client.post(f"/en/patient/access/{foreign.id}/revoke")
    assert r.status_code == 404 and in_html("errors.not_found") in r.text
    assert (await db.get(ConsentGrant, foreign.id, populate_existing=True)).revoked_at is None
    assert (await client.post("/en/patient/access/not-a-uuid/revoke")).status_code == 404
