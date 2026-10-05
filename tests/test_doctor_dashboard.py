"""D4 v1 — doctor patient dashboard: consent query (this file's first half)
and the page (second half).

Visibility is the whole feature, so most tests here are about who must NOT
appear.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

import pytest
from app.consent import service as consent_service
from app.db.models import (
    AuditLog,
    ConsentGrant,
    Doctor,
    Patient,
    Profile,
    UserRole,
    VerificationStatus,
)
from app.db.types import utcnow, uuid7
from app.deps import Actor
from app.errors import NotFound
from app.i18n.catalogue import missing_keys, translate
from markupsafe import escape
from sqlalchemy import select

from tests.conftest import TEST_PASSWORD, make_user


# ── Helpers ──────────────────────────────────────────────────────────────
async def patient_of(db, user: Profile) -> Patient:
    stmt = select(Patient).where(Patient.user_id == user.id)
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one()


async def doctor_of(db, user: Profile) -> Doctor:
    stmt = select(Doctor).where(Doctor.user_id == user.id)
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one()


async def make_patient(db, email: str, full_name: str, **fields) -> Patient:
    user = await make_user(db, email=email, role=UserRole.PATIENT)
    patient = await patient_of(db, user)
    patient.full_name = full_name
    for key, value in fields.items():
        setattr(patient, key, value)
    await db.commit()
    return patient


async def grant(db, patient: Patient, doctor: Doctor, *, revoked=False, expires_in=None):
    row = ConsentGrant(
        id=uuid7(),
        patient_id=patient.id,
        grantee_type="doctor",
        grantee_id=doctor.id,
        scope_sections=[],
        granted_at=utcnow(),
        revoked_at=utcnow() if revoked else None,
        expires_at=(utcnow() + expires_in) if expires_in is not None else None,
    )
    db.add(row)
    await db.commit()
    return row


def as_actor(doctor: Doctor, *, verified: bool = True) -> Actor:
    return Actor(user_id=doctor.user_id, role="doctor", is_verified_doctor=verified)


@pytest.fixture
async def admin(db, clinic) -> Profile:
    return await make_user(db, email="adm@x.com", role=UserRole.CLINIC_ADMIN, clinic_id=clinic.id)


@pytest.fixture
async def doctor(db, clinic, admin) -> Doctor:
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
async def other_doctor(db, clinic, admin) -> Doctor:
    user = await make_user(
        db,
        email="dr2@x.com",
        role=UserRole.DOCTOR,
        clinic_id=clinic.id,
        is_verified=True,
        verifier_id=admin.id,
    )
    return await doctor_of(db, user)


# ── list_patients_for_doctor ─────────────────────────────────────────────
async def test_lists_only_patients_who_granted_this_doctor(db, doctor, other_doctor):
    mine = await make_patient(db, "p1@x.com", "Mine")
    theirs = await make_patient(db, "p2@x.com", "Theirs")
    await make_patient(db, "p3@x.com", "Nobody")  # no grant at all
    await grant(db, mine, doctor)
    await grant(db, theirs, other_doctor)

    got = await consent_service.list_patients_for_doctor(db, as_actor(doctor))

    assert [p.full_name for p in got] == ["Mine"]


async def test_revoked_grant_is_not_listed(db, doctor):
    p = await make_patient(db, "p1@x.com", "Revoked")
    await grant(db, p, doctor, revoked=True)
    assert await consent_service.list_patients_for_doctor(db, as_actor(doctor)) == []


async def test_expired_grant_is_not_listed(db, doctor):
    p = await make_patient(db, "p1@x.com", "Expired")
    await grant(db, p, doctor, expires_in=timedelta(days=-1))
    assert await consent_service.list_patients_for_doctor(db, as_actor(doctor)) == []


async def test_unexpired_grant_is_listed(db, doctor):
    p = await make_patient(db, "p1@x.com", "Future")
    await grant(db, p, doctor, expires_in=timedelta(days=30))
    got = await consent_service.list_patients_for_doctor(db, as_actor(doctor))
    assert [x.full_name for x in got] == ["Future"]


async def test_sorted_by_name(db, doctor):
    for i, name in enumerate(["Zainab", "Ali", "Bilal"]):
        p = await make_patient(db, f"p{i}@x.com", name)
        await grant(db, p, doctor)
    got = await consent_service.list_patients_for_doctor(db, as_actor(doctor))
    assert [p.full_name for p in got] == ["Ali", "Bilal", "Zainab"]


async def test_two_active_grants_list_the_patient_once(db, doctor):
    # Review focus 3 — the database has no unique constraint behind BL-04.
    p = await make_patient(db, "p1@x.com", "Twice")
    await grant(db, p, doctor)
    await grant(db, p, doctor)
    got = await consent_service.list_patients_for_doctor(db, as_actor(doctor))
    assert [x.full_name for x in got] == ["Twice"]


async def test_unverified_doctor_gets_not_found(db, clinic):
    user = await make_user(db, email="new@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    pending = await doctor_of(db, user)
    with pytest.raises(NotFound):
        await consent_service.list_patients_for_doctor(db, as_actor(pending, verified=True))


async def test_non_doctor_gets_not_found(db):
    user = await make_user(db, email="pat@x.com", role=UserRole.PATIENT)
    actor = Actor(user_id=user.id, role="patient")
    with pytest.raises(NotFound):
        await consent_service.list_patients_for_doctor(db, actor)


# ── The page ─────────────────────────────────────────────────────────────
DASH_URL = "/en/doctor"


def in_html(key: str, locale: str = "en", **params) -> str:
    return str(escape(translate(key, locale, **params)))


async def login(client, email: str) -> None:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": TEST_PASSWORD})
    assert r.status_code == 200, r.text


async def dashboard_audit_rows(db) -> list[AuditLog]:
    rows = await db.execute(
        select(AuditLog)
        .where(AuditLog.action == "doctor.dashboard.view")
        .order_by(AuditLog.id)
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


async def test_verified_doctor_sees_consented_patients_with_the_four_fields(client, db, doctor):
    p = await make_patient(
        db, "p1@x.com", "Ayesha Raza", date_of_birth=date(1994, 3, 14), gender="female"
    )
    await grant(db, p, doctor)
    await login(client, "dr@x.com")

    r = await client.get(DASH_URL)

    assert r.status_code == 200
    assert "Ayesha Raza" in r.text
    assert p.passport_no in r.text
    assert ">Female<" in r.text  # the label, never the stored code
    assert "female" not in r.text
    assert in_html("doctor_dashboard.patients.title") in r.text
    assert f'href="/en/doctor/patient/{p.id}"' in r.text
    # No passport number in any URL (P4 AC-12).
    assert f"/{p.passport_no}" not in r.text


async def test_ungranted_revoked_and_expired_patients_are_absent(client, db, doctor, other_doctor):
    ok = await make_patient(db, "ok@x.com", "Visible Vera")
    gone = await make_patient(db, "gone@x.com", "Revoked Rana")
    old = await make_patient(db, "old@x.com", "Expired Esa")
    foreign = await make_patient(db, "for@x.com", "Foreign Fiza")
    await make_patient(db, "none@x.com", "Ungranted Uzma")
    await grant(db, ok, doctor)
    await grant(db, gone, doctor, revoked=True)
    await grant(db, old, doctor, expires_in=timedelta(days=-1))
    await grant(db, foreign, other_doctor)
    await login(client, "dr@x.com")

    html = (await client.get(DASH_URL)).text

    assert "Visible Vera" in html
    for name in ("Revoked Rana", "Expired Esa", "Foreign Fiza", "Ungranted Uzma"):
        assert name not in html


async def test_revoking_removes_the_patient_on_the_next_load(client, db, doctor):
    p = await make_patient(db, "p1@x.com", "Soon Gone")
    g = await grant(db, p, doctor)
    await login(client, "dr@x.com")
    assert "Soon Gone" in (await client.get(DASH_URL)).text

    g.revoked_at = utcnow()
    await db.commit()

    assert "Soon Gone" not in (await client.get(DASH_URL)).text


async def test_empty_state_when_nobody_has_granted_access(client, db, doctor):
    await login(client, "dr@x.com")
    r = await client.get(DASH_URL)
    assert r.status_code == 200
    assert in_html("doctor_dashboard.empty") in r.text
    assert "<table" not in r.text


async def test_missing_dob_and_gender_render_dashes(client, db, doctor):
    p = await make_patient(db, "p1@x.com", "No Details")
    await grant(db, p, doctor)
    await login(client, "dr@x.com")
    html = (await client.get(DASH_URL)).text
    assert html.count("—") >= 2


async def test_rows_are_ordered_by_name(client, db, doctor):
    for i, name in enumerate(["Zainab Z", "Ali A", "Bilal B"]):
        await grant(db, await make_patient(db, f"p{i}@x.com", name), doctor)
    await login(client, "dr@x.com")
    html = (await client.get(DASH_URL)).text
    assert html.index("Ali A") < html.index("Bilal B") < html.index("Zainab Z")


async def test_patient_names_are_escaped(client, db, doctor):
    p = await make_patient(db, "p1@x.com", "<script>alert(1)</script>")
    await grant(db, p, doctor)
    await login(client, "dr@x.com")
    html = (await client.get(DASH_URL)).text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


async def test_unverified_doctor_sees_the_banner_and_no_list(client, db, clinic):
    await make_user(db, email="new@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    await login(client, "new@x.com")
    r = await client.get(DASH_URL)
    assert r.status_code == 200
    assert in_html("doctor.pending_title") in r.text
    assert in_html("doctor_dashboard.patients.title") not in r.text
    assert "<table" not in r.text


async def test_verified_doctor_without_a_clinic_still_sees_the_list(client, db, admin):
    # Review focus 4 — the list must not depend on a clinic affiliation.
    user = await make_user(
        db, email="solo@x.com", role=UserRole.DOCTOR, is_verified=True, verifier_id=admin.id
    )
    solo = await doctor_of(db, user)
    await grant(db, await make_patient(db, "p1@x.com", "Solo Patient"), solo)
    await login(client, "solo@x.com")
    assert "Solo Patient" in (await client.get(DASH_URL)).text


async def test_doctor_unverified_after_login_loses_the_list(client, db, doctor):
    # Review focus 5 — status is read from the database on every request.
    await grant(db, await make_patient(db, "p1@x.com", "Was Visible"), doctor)
    await login(client, "dr@x.com")
    assert "Was Visible" in (await client.get(DASH_URL)).text

    row = await doctor_of(db, await db.get(Profile, doctor.user_id))
    row.verification_status = VerificationStatus.PENDING
    row.verified_by = None
    row.verified_at = None
    await db.commit()

    html = (await client.get(DASH_URL)).text
    assert "Was Visible" not in html


async def test_patient_and_logged_out_are_redirected(client, db):
    r = await client.get(DASH_URL)
    assert r.status_code == 303 and r.headers["location"].startswith("/en/login")

    await make_user(db, email="pat@x.com", role=UserRole.PATIENT)
    await login(client, "pat@x.com")
    r = await client.get(DASH_URL)
    assert r.status_code == 303 and r.headers["location"] == "/en/patient"


async def test_no_clinical_data_on_the_list(client, db, doctor):
    p = await make_patient(db, "p1@x.com", "Clinical Cara")
    await grant(db, p, doctor)
    await login(client, "dr@x.com")
    html = (await client.get(DASH_URL)).text.lower()
    for word in ("allerg", "chronic", "medication", "risk"):
        assert word not in html


async def test_one_audit_row_per_load_with_a_count_and_no_names(client, db, doctor):
    await grant(db, await make_patient(db, "p1@x.com", "Secret Name"), doctor)
    await grant(db, await make_patient(db, "p2@x.com", "Other Name"), doctor)
    await login(client, "dr@x.com")

    await client.get(DASH_URL)
    await client.get(DASH_URL)

    rows = await dashboard_audit_rows(db)
    assert len(rows) == 2
    assert rows[0].actor_user_id == doctor.user_id
    assert rows[0].actor_role == "doctor"
    detail = json.loads(rows[0].detail)
    assert detail == {"patient_count": 2}
    assert "Secret Name" not in rows[0].detail


async def test_urdu_page_renders_and_passport_stays_left_to_right(client, db, doctor):
    p = await make_patient(db, "p1@x.com", "اسد علی", gender="female")
    await grant(db, p, doctor)
    await login(client, "dr@x.com")
    html = (await client.get("/ur/doctor")).text
    assert in_html("doctor_dashboard.patients.title", "ur") in html
    assert "اسد علی" in html
    assert ">خاتون<" in html
    assert re.search(rf'dir="ltr"[^>]*>\s*{re.escape(p.passport_no)}', html)


def test_catalogue_has_the_new_keys_in_both_languages():
    assert missing_keys() == {"ur": []}
