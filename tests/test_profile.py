"""Patient profile test suite (FR2).

Test ids map 1:1 to `.claude/specs/patient_profile_spec.md` §11.2 — `test_tN_*`
covers AC-N. Runs on the usual harness: in-memory SQLite and the fake Supabase
Auth double, no network.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import re
import uuid
from datetime import date
from pathlib import Path

import pytest
from app.db.models import (
    AccountStatus,
    Allergy,
    AuditLog,
    Base,
    ChronicCondition,
    Patient,
    PatientMedication,
    Profile,
    UserRole,
)
from app.db.types import utcnow, uuid7
from app.i18n.catalogue import missing_keys, translate
from app.log_config import PLACEHOLDER, _redact
from app.profile.schemas import PatientProfileOut
from app.settings import settings
from markupsafe import escape
from sqlalchemy import select

from tests.conftest import TEST_PASSWORD, make_user

TODAY = date(2026, 9, 22)
SECTIONS = {"allergies": "allergy", "conditions": "condition", "medications": "medication"}
NAME_FIELD = {"allergies": "substance", "conditions": "name", "medications": "name"}
MODEL = {"allergies": Allergy, "conditions": ChronicCondition, "medications": PatientMedication}


# ── Helpers ──────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def freeze_today(monkeypatch):
    monkeypatch.setattr("app.db.types.today_pk", lambda: TODAY)
    return TODAY


async def login(client, email: str) -> None:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": TEST_PASSWORD})
    assert r.status_code == 200, r.text


# The app writes through its own session, so the test session re-reads with
# `populate_existing` instead of `expire_all()` — expiring would make any
# later attribute access lazy-load outside the async greenlet.
async def patient_of(db, user: Profile) -> Patient:
    stmt = select(Patient).where(Patient.user_id == user.id)
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one()


@pytest.fixture
async def patient_user(client, db) -> Profile:
    user = await make_user(db, email="pat@x.com", role=UserRole.PATIENT)
    await login(client, "pat@x.com")
    return user


@pytest.fixture
async def second_patient(db) -> Profile:
    return await make_user(db, email="other@x.com", role=UserRole.PATIENT)


async def add_allergy_row(
    db, patient_id, *, recorded_by, substance="Dust", severity=None, reaction=None, removed_at=None
) -> Allergy:
    row = Allergy(
        id=uuid7(),
        patient_id=patient_id,
        substance=substance,
        reaction=reaction,
        severity=severity,
        recorded_by=recorded_by,
        recorded_at=utcnow(),
        removed_at=removed_at,
    )
    db.add(row)
    await db.commit()
    return row


async def add_condition_row(
    db, patient_id, *, recorded_by, name="Asthma", status="active", icd10_code=None, risk=None
) -> ChronicCondition:
    row = ChronicCondition(
        id=uuid7(),
        patient_id=patient_id,
        name=name,
        status=status,
        icd10_code=icd10_code,
        risk_level=risk,
        recorded_by=recorded_by,
        recorded_at=utcnow(),
    )
    db.add(row)
    await db.commit()
    return row


async def add_medication_row(
    db, patient_id, *, recorded_by, name="Metformin", removed_at=None
) -> PatientMedication:
    row = PatientMedication(
        id=uuid7(),
        patient_id=patient_id,
        name=name,
        recorded_by=recorded_by,
        recorded_at=utcnow(),
        removed_at=removed_at,
    )
    db.add(row)
    await db.commit()
    return row


async def add_row(db, section: str, patient_id, *, recorded_by, name: str):
    if section == "allergies":
        return await add_allergy_row(db, patient_id, recorded_by=recorded_by, substance=name)
    if section == "conditions":
        return await add_condition_row(db, patient_id, recorded_by=recorded_by, name=name)
    return await add_medication_row(db, patient_id, recorded_by=recorded_by, name=name)


async def fresh(db, model, row_id):
    return await db.get(model, row_id, populate_existing=True)


async def audit_rows(db, prefix: str = "profile.") -> list[AuditLog]:
    rows = await db.execute(
        select(AuditLog)
        .where(AuditLog.action.like(f"{prefix}%"))
        .order_by(AuditLog.id)
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


def in_html(key: str, locale: str = "en", **params) -> str:
    """A translated message as it appears in autoescaped HTML (' -> &#39;)."""
    return str(escape(translate(key, locale, **params)))


def err_fields(r) -> dict[str, str]:
    return r.json()["error"]["details"]["fields"]


# ── T1 / AC-01 — view (web) ──────────────────────────────────────────────
async def test_t1_profile_page_renders(client, db, patient_user):
    patient = await patient_of(db, patient_user)
    patient.date_of_birth = date(1990, 1, 1)
    await db.commit()
    await add_allergy_row(db, patient.id, recorded_by=patient_user.id, substance="Penicillin")
    await add_allergy_row(
        db, patient.id, recorded_by=patient_user.id, substance="Gone-QX", removed_at=utcnow()
    )

    r = await client.get("/en/patient/profile")
    assert r.status_code == 200
    html = r.text
    assert patient.passport_no in html
    assert "pat@x.com" in html
    assert translate("profile.age_years", "en", years=36) in html
    assert "Penicillin" in html
    assert translate("profile.empty.conditions", "en") in html
    assert translate("profile.empty.medications", "en") in html
    assert 'value="1990-01-01"' in html
    assert "Gone-QX" not in html


# ── T2 / AC-02 — view (API) ──────────────────────────────────────────────
async def test_t2_profile_api_shape_and_order(client, db, patient_user):
    patient = await patient_of(db, patient_user)
    uid = patient_user.id
    for substance, severity in [
        ("Aspirin", "mild"),
        ("Bees", "severe"),
        ("Cats", None),
        ("Dust", "very much"),  # legacy value, no CHECK in the shared schema
    ]:
        await add_allergy_row(
            db, patient.id, recorded_by=uid, substance=substance, severity=severity
        )
    await add_condition_row(db, patient.id, recorded_by=uid, name="Asthma")
    await add_condition_row(db, patient.id, recorded_by=uid, name="Flu", status="resolved")
    await add_medication_row(db, patient.id, recorded_by=uid, name="Metformin")
    await add_medication_row(db, patient.id, recorded_by=uid, name="Old", removed_at=utcnow())

    r = await client.get("/api/v1/me/profile")
    assert r.status_code == 200
    body = PatientProfileOut.model_validate(r.json())
    assert [a.substance for a in body.allergies] == ["Bees", "Aspirin", "Cats", "Dust"]
    assert body.allergies[3].severity == "very much"
    assert [c.name for c in body.conditions] == ["Asthma"]
    assert [m.name for m in body.medications] == ["Metformin"]
    assert body.age_years is None

    patient = await patient_of(db, patient_user)
    patient.date_of_birth = date(1990, 9, 23)
    await db.commit()
    assert (await client.get("/api/v1/me/profile")).json()["age_years"] == 35


# ── T3 / AC-03 — access control ──────────────────────────────────────────
WRITES = [
    ("PATCH", "/api/v1/me/profile"),
    ("POST", "/api/v1/me/allergies"),
    ("PATCH", f"/api/v1/me/allergies/{uuid.uuid4()}"),
    ("DELETE", f"/api/v1/me/allergies/{uuid.uuid4()}"),
    ("POST", "/api/v1/me/conditions"),
    ("DELETE", f"/api/v1/me/medications/{uuid.uuid4()}"),
]


async def test_t3_access_control_anonymous(client):
    assert (await client.get("/api/v1/me/profile")).status_code == 401
    web = await client.get("/en/patient/profile")
    assert web.status_code == 303
    assert web.headers["location"] == "/en/login?next=/en/patient/profile"
    post = await client.post("/en/patient/profile/allergies", data={"substance": "Dust"})
    assert post.status_code == 303
    for method, url in WRITES:
        r = await client.request(method, url, json={})
        assert r.status_code == 401, (method, url)


async def test_t3_access_control_doctor_and_admin(client, db, clinic):
    await make_user(db, email="dr@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    await login(client, "dr@x.com")
    api = await client.get("/api/v1/me/profile")
    assert api.status_code == 403
    assert api.json()["error"]["code"] == "FORBIDDEN"
    web = await client.get("/en/patient/profile")
    assert web.status_code == 303 and web.headers["location"] == "/en/doctor"
    for method, url in WRITES:
        assert (await client.request(method, url, json={})).status_code == 403, (method, url)

    await make_user(db, email="adm@x.com", role=UserRole.CLINIC_ADMIN, clinic_id=clinic.id)
    await login(client, "adm@x.com")
    assert (await client.get("/api/v1/me/profile")).status_code == 403
    web = await client.get("/en/patient/profile")
    assert web.status_code == 303 and web.headers["location"] == "/en/admin"


async def test_t3_access_control_suspended_and_no_patient_row(client, db, patient_user):
    user = await fresh(db, Profile, patient_user.id)
    user.status = AccountStatus.SUSPENDED
    await db.commit()
    assert (await client.get("/api/v1/me/profile")).status_code == 401

    user = await fresh(db, Profile, patient_user.id)
    user.status = AccountStatus.ACTIVE
    await db.delete(await patient_of(db, patient_user))
    await db.commit()
    api = await client.get("/api/v1/me/profile")
    assert api.status_code == 404
    assert api.json()["error"]["code"] == "NOT_FOUND"
    web = await client.get("/en/patient/profile")
    assert web.status_code == 303 and web.headers["location"] == "/en/onboarding"


# ── T4 / AC-04 — update basics ───────────────────────────────────────────
async def test_t4_update_basics_api_and_web(client, db, patient_user):
    r = await client.patch("/api/v1/me/profile", json={"blood_group": "O+"})
    assert r.status_code == 200
    assert r.json()["blood_group"] == "O+"
    patient = await patient_of(db, patient_user)
    assert patient.blood_group == "O+"
    assert patient.full_name == "Test Person" and patient.gender is None

    r = await client.patch("/api/v1/me/profile", json={"full_name": "Sana Iqbal"})
    assert r.status_code == 200
    assert (await fresh(db, Profile, patient_user.id)).full_name == "Sana Iqbal"
    assert (await patient_of(db, patient_user)).full_name == "Sana Iqbal"

    form = {
        "full_name": "Sana Iqbal Khan",
        "date_of_birth": "1990-01-01",
        "gender": "female",
        "blood_group": "O+",
        "phone_e164": "+923001234567",
        "emergency_contact": "Ali 03001112223",
    }
    web = await client.post("/en/patient/profile", data=form)
    assert web.status_code == 303
    assert web.headers["location"] == "/en/patient/profile?saved=basics.updated#basics"
    page = await client.get("/en/patient/profile?saved=basics.updated")
    assert translate("profile.saved.basics", "en") in page.text
    shell = await client.get("/en/patient")
    assert "Sana Iqbal Khan" in shell.text
    patient = await patient_of(db, patient_user)
    assert patient.emergency_contact == "Ali 03001112223"
    assert (await fresh(db, Profile, patient_user.id)).phone_e164 == "+923001234567"


# ── T5 / AC-05 — clearing optional details ───────────────────────────────
async def test_t5_clear_optional_fields(client, db, patient_user):
    full = {
        "date_of_birth": "1990-01-01",
        "gender": "male",
        "blood_group": "B-",
        "phone_e164": "+923001234567",
        "emergency_contact": "Someone",
    }
    assert (await client.patch("/api/v1/me/profile", json=full)).status_code == 200
    cleared = await client.patch("/api/v1/me/profile", json=dict.fromkeys(full))
    assert cleared.status_code == 200
    patient = await patient_of(db, patient_user)
    assert (
        patient.date_of_birth,
        patient.gender,
        patient.blood_group,
        patient.emergency_contact,
    ) == (None, None, None, None)
    assert (await fresh(db, Profile, patient_user.id)).phone_e164 is None

    # Web: empty inputs clear too.
    assert (await client.patch("/api/v1/me/profile", json=full)).status_code == 200
    web = await client.post(
        "/en/patient/profile", data={"full_name": "Test Person", **dict.fromkeys(full, "")}
    )
    assert web.status_code == 303
    patient = await patient_of(db, patient_user)
    assert patient.gender is None and patient.date_of_birth is None

    for bad in (None, "  "):
        r = await client.patch("/api/v1/me/profile", json={"full_name": bad})
        assert r.status_code == 422
        assert err_fields(r)["full_name"] == "errors.name_required"
    assert (await fresh(db, Profile, patient_user.id)).full_name == "Test Person"


# ── T6 / AC-06 — basics validation ───────────────────────────────────────
@pytest.mark.parametrize(
    ("field", "value", "key"),
    [
        ("date_of_birth", "2999-01-01", "errors.date_in_future"),
        ("date_of_birth", "1899-12-31", "errors.date_too_early"),
        ("date_of_birth", "not-a-date", "errors.date_invalid"),
        ("gender", "M", "errors.choice_invalid"),
        ("blood_group", "o+", "errors.choice_invalid"),
        ("blood_group", "AB", "errors.choice_invalid"),
        ("phone_e164", "03001234567", "errors.phone_invalid"),
        ("full_name", "x" * 121, "errors.text_too_long"),
        ("full_name", "Bad\x07Name", "errors.text_invalid"),
    ],
)
async def test_t6_basics_validation_api(client, db, patient_user, field, value, key):
    r = await client.patch("/api/v1/me/profile", json={field: value})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"
    assert err_fields(r)[field] == key
    patient = await patient_of(db, patient_user)
    assert patient.date_of_birth is None and patient.gender is None
    assert patient.full_name == "Test Person"
    assert await audit_rows(db) == []


async def test_t6_basics_validation_web_and_legacy(client, db, patient_user):
    r = await client.post(
        "/en/patient/profile",
        data={"full_name": "Typed Name", "date_of_birth": "2999-01-01", "blood_group": "A+"},
    )
    assert r.status_code == 422
    assert 'value="Typed Name"' in r.text
    assert 'aria-invalid="true"' in r.text and 'role="alert"' in r.text
    assert in_html("errors.date_in_future") in r.text
    assert (await patient_of(db, patient_user)).blood_group is None

    # A legacy stored gender the patient did not touch never blocks a save.
    patient = await patient_of(db, patient_user)
    patient.gender = "Male"
    await db.commit()
    page = await client.get("/en/patient/profile")
    assert re.search(r'<option value="Male"\s+selected>', page.text)
    r = await client.post(
        "/en/patient/profile",
        data={"full_name": "Test Person", "gender": "Male", "phone_e164": "+923009990000"},
    )
    assert r.status_code == 303
    patient = await patient_of(db, patient_user)
    assert patient.gender == "Male"
    assert (await fresh(db, Profile, patient_user.id)).phone_e164 == "+923009990000"
    [row] = await audit_rows(db)
    assert json.loads(row.detail)["fields_changed"] == ["phone_e164"]


# ── T7 / AC-07 — protected fields ────────────────────────────────────────
@pytest.mark.parametrize(
    "extra",
    [
        {"email": "x@y.com"},
        {"passport_no": "CN-X"},
        {"role": "admin"},
        {"status": "suspended"},
        {"patient_id": str(uuid.uuid4())},
        {"cnic_hash": "abc"},
    ],
)
async def test_t7_protected_fields_rejected_api(client, db, patient_user, extra):
    assert (await client.patch("/api/v1/me/profile", json=extra)).status_code == 422


async def test_t7_protected_fields_ignored_web(client, db, patient_user, fake_supabase):
    before = await patient_of(db, patient_user)
    passport = before.passport_no
    calls_before = list(fake_supabase.calls)

    r = await client.post(
        "/en/patient/profile",
        data={
            "full_name": "New Name",
            "role": "admin",
            "passport_no": "X",
            "email": "a@b.c",
            "status": "suspended",
        },
    )
    assert r.status_code == 303
    user = await fresh(db, Profile, patient_user.id)
    assert (user.email, user.role, user.status) == (
        "pat@x.com",
        UserRole.PATIENT,
        AccountStatus.ACTIVE,
    )
    assert (await patient_of(db, patient_user)).passport_no == passport
    await client.patch("/api/v1/me/profile", json={"phone_e164": "+923001234567"})
    assert fake_supabase.calls == calls_before  # no Supabase Auth API call at all


# ── T8 / AC-08 — add allergy ─────────────────────────────────────────────
async def test_t8_add_allergy(client, db, patient_user):
    r = await client.post(
        "/api/v1/me/allergies",
        json={"substance": "  Penicillin ", "severity": "severe", "reaction": "Rash"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["substance"] == "Penicillin" and body["editable"] is True
    row = await fresh(db, Allergy, uuid.UUID(body["id"]))
    assert row.recorded_by == patient_user.id and row.removed_at is None

    web = await client.post("/en/patient/profile/allergies", data={"substance": "Latex"})
    assert web.status_code == 303
    assert web.headers["location"] == "/en/patient/profile?saved=allergy.added#allergies"


# ── T9 / AC-09 — edit allergy ────────────────────────────────────────────
async def test_t9_edit_allergy(client, db, patient_user):
    patient = await patient_of(db, patient_user)
    row = await add_allergy_row(
        db, patient.id, recorded_by=patient_user.id, substance="Dust", severity="mild"
    )

    r = await client.patch(f"/api/v1/me/allergies/{row.id}", json={"severity": "moderate"})
    assert r.status_code == 200
    got = await fresh(db, Allergy, row.id)
    assert got.severity == "moderate" and got.substance == "Dust" and got.updated_at is not None

    page = await client.get(f"/en/patient/profile?edit=allergy&id={row.id}")
    assert f'action="/en/patient/profile/allergies/{row.id}"' in page.text
    assert f'id="allergy-{row.id}-f-substance"' in page.text

    web = await client.post(
        f"/en/patient/profile/allergies/{row.id}",
        data={"substance": "House dust", "reaction": "", "severity": "moderate"},
    )
    assert web.status_code == 303
    assert web.headers["location"] == "/en/patient/profile?saved=allergy.updated#allergies"
    assert (await fresh(db, Allergy, row.id)).substance == "House dust"

    audits = len(await audit_rows(db))
    noop = await client.patch(f"/api/v1/me/allergies/{row.id}", json={})
    assert noop.status_code == 200
    assert len(await audit_rows(db)) == audits


# ── T10 / AC-10 — soft remove, idempotent ────────────────────────────────
async def test_t10_remove_allergy_soft_and_idempotent(client, db, patient_user):
    patient = await patient_of(db, patient_user)
    row = await add_allergy_row(db, patient.id, recorded_by=patient_user.id, substance="Dust-QX")
    page = await client.get("/en/patient/profile")
    assert '<details class="confirm">' in page.text

    assert (await client.delete(f"/api/v1/me/allergies/{row.id}")).status_code == 204
    got = await fresh(db, Allergy, row.id)
    assert got is not None and got.removed_at is not None
    assert (await client.get("/api/v1/me/profile")).json()["allergies"] == []
    assert "Dust-QX" not in (await client.get("/en/patient/profile")).text

    assert (await client.delete(f"/api/v1/me/allergies/{row.id}")).status_code == 204
    assert len(await audit_rows(db, "profile.allergy.remove")) == 1
    assert (await client.patch(f"/api/v1/me/allergies/{row.id}", json={})).status_code == 404

    other = await add_allergy_row(db, patient.id, recorded_by=patient_user.id, substance="Latex")
    web = await client.post(f"/en/patient/profile/allergies/{other.id}/remove")
    assert web.status_code == 303
    assert web.headers["location"] == "/en/patient/profile?saved=allergy.removed#allergies"


# ── T11 / AC-11 — chronic conditions ─────────────────────────────────────
async def test_t11_conditions_crud(client, db, patient_user):
    r = await client.post("/api/v1/me/conditions", json={"name": "Asthma"})
    assert r.status_code == 201 and r.json()["status"] == "active"
    cid = uuid.UUID(r.json()["id"])

    row = await fresh(db, ChronicCondition, cid)
    row.icd10_code, row.risk_level = "J45", "low"
    await db.commit()

    r = await client.patch(
        f"/api/v1/me/conditions/{cid}", json={"name": "Asthma (mild)", "onset_date": "2010-05-01"}
    )
    assert r.status_code == 200 and r.json()["onset_date"] == "2010-05-01"

    assert (await client.delete(f"/api/v1/me/conditions/{cid}")).status_code == 204
    row = await fresh(db, ChronicCondition, cid)
    assert row.status == "resolved"
    assert (row.icd10_code, row.risk_level) == ("J45", "low")
    assert (await client.delete(f"/api/v1/me/conditions/{cid}")).status_code == 204

    assert (
        await client.post("/api/v1/me/conditions", json={"name": "Asthma (mild)"})
    ).status_code == 201

    web = await client.post("/en/patient/profile/conditions", data={"name": "Migraine"})
    assert web.headers["location"] == "/en/patient/profile?saved=condition.added#conditions"
    [mig] = [
        c
        for c in (await client.get("/api/v1/me/profile")).json()["conditions"]
        if c["name"] == "Migraine"
    ]
    web = await client.post(
        f"/en/patient/profile/conditions/{mig['id']}", data={"name": "Migraine", "onset_date": ""}
    )
    assert web.headers["location"] == "/en/patient/profile?saved=condition.updated#conditions"
    web = await client.post(f"/en/patient/profile/conditions/{mig['id']}/remove")
    assert web.headers["location"] == "/en/patient/profile?saved=condition.removed#conditions"


# ── T12 / AC-12 — current medications ────────────────────────────────────
async def test_t12_medications_crud(client, db, patient_user):
    table = Base.metadata.tables["patient_medication"]
    assert set(table.c.keys()) == {
        "medication_id",
        "patient_id",
        "name",
        "strength",
        "frequency",
        "started_on",
        "notes",
        "recorded_by",
        "recorded_at",
        "updated_at",
        "removed_at",
    }

    r = await client.post(
        "/api/v1/me/medications",
        json={
            "name": "Metformin",
            "strength": "500 mg",
            "frequency": "Twice a day",
            "started_on": "2020-01-01",
            "notes": "With food",
        },
    )
    assert r.status_code == 201
    mid = uuid.UUID(r.json()["id"])
    r = await client.patch(f"/api/v1/me/medications/{mid}", json={"strength": "850 mg"})
    assert r.status_code == 200 and r.json()["strength"] == "850 mg"
    assert (await client.delete(f"/api/v1/me/medications/{mid}")).status_code == 204
    row = await fresh(db, PatientMedication, mid)
    assert row is not None and row.removed_at is not None

    web = await client.post(
        "/en/patient/profile/medications", data={"name": "Aspirin", "strength": "75 mg"}
    )
    assert web.headers["location"] == "/en/patient/profile?saved=medication.added#medications"
    [asp] = (await client.get("/api/v1/me/profile")).json()["medications"]
    web = await client.post(
        f"/en/patient/profile/medications/{asp['id']}",
        data={
            "name": "Aspirin",
            "strength": "81 mg",
            "frequency": "",
            "started_on": "",
            "notes": "",
        },
    )
    assert web.headers["location"] == "/en/patient/profile?saved=medication.updated#medications"
    web = await client.post(f"/en/patient/profile/medications/{asp['id']}/remove")
    assert web.headers["location"] == "/en/patient/profile?saved=medication.removed#medications"


# ── T13 / AC-13 — entry validation ───────────────────────────────────────
@pytest.mark.parametrize(
    ("url", "body", "field", "key"),
    [
        ("/api/v1/me/allergies", {}, "substance", "errors.text_required"),
        ("/api/v1/me/allergies", {"substance": "  "}, "substance", "errors.text_required"),
        ("/api/v1/me/allergies", {"substance": "X"}, "substance", "errors.text_required"),
        ("/api/v1/me/conditions", {"name": "x" * 121}, "name", "errors.text_too_long"),
        (
            "/api/v1/me/allergies",
            {"substance": "Dust", "reaction": "r" * 256},
            "reaction",
            "errors.text_too_long",
        ),
        (
            "/api/v1/me/medications",
            {"name": "Metformin", "strength": "s" * 51},
            "strength",
            "errors.text_too_long",
        ),
        (
            "/api/v1/me/medications",
            {"name": "Metformin", "notes": "n" * 501},
            "notes",
            "errors.text_too_long",
        ),
        (
            "/api/v1/me/allergies",
            {"substance": "Dust", "severity": "high"},
            "severity",
            "errors.choice_invalid",
        ),
        (
            "/api/v1/me/conditions",
            {"name": "Asthma", "onset_date": "2999-01-01"},
            "onset_date",
            "errors.date_in_future",
        ),
        (
            "/api/v1/me/medications",
            {"name": "Metformin", "started_on": "1899-01-01"},
            "started_on",
            "errors.date_too_early",
        ),
        (
            "/api/v1/me/medications",
            {"name": "Metformin", "notes": "bad\x00"},
            "notes",
            "errors.text_invalid",
        ),
    ],
)
async def test_t13_entry_validation(client, db, patient_user, url, body, field, key):
    r = await client.post(url, json=body)
    assert r.status_code == 422
    assert err_fields(r)[field] == key
    for model in (Allergy, ChronicCondition, PatientMedication):
        assert (await db.execute(select(model))).scalars().all() == []


async def test_t13_date_before_birth_and_null_name(client, db, patient_user):
    patient = await patient_of(db, patient_user)
    patient.date_of_birth = date(2000, 1, 1)
    await db.commit()
    r = await client.post(
        "/api/v1/me/medications", json={"name": "Metformin", "started_on": "1999-12-31"}
    )
    assert r.status_code == 422
    assert err_fields(r)["started_on"] == "errors.date_before_birth"
    assert (await db.execute(select(PatientMedication))).scalars().all() == []

    row = await add_allergy_row(db, patient.id, recorded_by=patient_user.id)
    r = await client.patch(f"/api/v1/me/allergies/{row.id}", json={"substance": None})
    assert r.status_code == 422
    assert err_fields(r)["substance"] == "errors.text_required"


# ── T14 / AC-14 — duplicates ─────────────────────────────────────────────
@pytest.mark.parametrize("section", list(SECTIONS))
async def test_t14_duplicates_rejected(client, db, patient_user, section):
    patient = await patient_of(db, patient_user)
    name = NAME_FIELD[section]
    original = await add_row(
        db, section, patient.id, recorded_by=patient_user.id, name="Penicillin"
    )

    r = await client.post(f"/api/v1/me/{section}", json={name: "penicillin "})
    assert r.status_code == 409 and r.json()["error"]["code"] == "DUPLICATE_ENTRY"

    other = await add_row(db, section, patient.id, recorded_by=patient_user.id, name="Other")
    r = await client.patch(f"/api/v1/me/{section}/{other.id}", json={name: "PENICILLIN"})
    assert r.status_code == 409

    web = await client.post(f"/en/patient/profile/{section}", data={name: "Penicillin"})
    assert web.status_code == 409
    assert f'id="{SECTIONS[section]}-new-e-{name}"' in web.text
    assert translate("errors.duplicate_entry", "en") in web.text

    assert (await client.delete(f"/api/v1/me/{section}/{original.id}")).status_code == 204
    assert (
        await client.post(f"/api/v1/me/{section}", json={name: "Penicillin"})
    ).status_code == 201

    # A non-editable (clinician) active entry counts too.
    await add_row(db, section, patient.id, recorded_by=None, name="Clinician-Item")
    r = await client.post(f"/api/v1/me/{section}", json={name: "clinician-item"})
    assert r.status_code == 409


# ── T15 / AC-15 — list cap ───────────────────────────────────────────────
async def test_t15_list_cap(client, db, patient_user, monkeypatch):
    monkeypatch.setattr(settings, "profile_max_entries_per_list", 3)
    ids = []
    for n in ("Aa", "Bb", "Cc"):
        r = await client.post("/api/v1/me/allergies", json={"substance": n})
        assert r.status_code == 201
        ids.append(r.json()["id"])
    r = await client.post("/api/v1/me/allergies", json={"substance": "Dd"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "LIST_FULL"
    assert "3" in r.json()["error"]["message"]

    web = await client.post("/en/patient/profile/allergies", data={"substance": "Dd"})
    assert web.status_code == 422
    assert translate("errors.list_full", "en", max=3) in web.text

    assert (await client.delete(f"/api/v1/me/allergies/{ids[0]}")).status_code == 204
    assert (await client.post("/api/v1/me/allergies", json={"substance": "Dd"})).status_code == 201


# ── T16 / AC-16 — clinician and legacy entries are read-only ─────────────
async def test_t16_clinician_and_legacy_entries_read_only(client, db, patient_user, clinic):
    doctor = await make_user(db, email="doc@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    patient = await patient_of(db, patient_user)
    legacy = await add_allergy_row(db, patient.id, recorded_by=None, substance="Legacy")
    clinician = await add_allergy_row(db, patient.id, recorded_by=doctor.id, substance="Clinical")

    body = (await client.get("/api/v1/me/profile")).json()
    assert [a["editable"] for a in body["allergies"]] == [False, False]

    page = (await client.get("/en/patient/profile")).text
    assert page.count(translate("profile.recorded_by_clinician", "en")) == 2
    for row in (legacy, clinician):
        assert f"/allergies/{row.id}/remove" not in page
        assert f"id={row.id}" not in page

        r = await client.patch(f"/api/v1/me/allergies/{row.id}", json={"severity": "mild"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "NOT_EDITABLE"
        r = await client.delete(f"/api/v1/me/allergies/{row.id}")
        assert r.status_code == 403
        got = await fresh(db, Allergy, row.id)
        assert got.severity is None and got.removed_at is None

    ignored = await client.get(f"/en/patient/profile?edit=allergy&id={legacy.id}")
    assert ignored.status_code == 200
    assert f'action="/en/patient/profile/allergies/{legacy.id}"' not in ignored.text

    web = await client.post(f"/en/patient/profile/allergies/{legacy.id}/remove")
    assert web.status_code == 403
    assert in_html("errors.not_editable") in web.text


# ── T17 / AC-17 — ownership isolation ────────────────────────────────────
@pytest.mark.parametrize("section", list(SECTIONS))
async def test_t17_ownership_isolation(client, db, patient_user, second_patient, section):
    other = await patient_of(db, second_patient)
    foreign = await add_row(db, section, other.id, recorded_by=second_patient.id, name="Theirs-QX")
    name = NAME_FIELD[section]

    def strip(r):
        body = r.json()
        body["error"].pop("request_id")
        return body

    for method, payload in (("PATCH", {name: "Mine"}), ("DELETE", None)):
        a = await client.request(method, f"/api/v1/me/{section}/{foreign.id}", json=payload)
        b = await client.request(method, f"/api/v1/me/{section}/{uuid.uuid4()}", json=payload)
        assert a.status_code == b.status_code == 404
        assert strip(a) == strip(b)

    got = await fresh(db, MODEL[section], foreign.id)
    assert getattr(got, NAME_FIELD[section]) == "Theirs-QX"
    assert "Theirs-QX" not in (await client.get("/api/v1/me/profile")).text

    web = await client.post(f"/en/patient/profile/{section}/{foreign.id}/remove")
    assert web.status_code == 404
    assert in_html("errors.not_found") in web.text

    assert (await client.delete(f"/api/v1/me/{section}/not-a-uuid")).status_code == 422
    assert (
        await client.post(f"/en/patient/profile/{section}/not-a-uuid/remove")
    ).status_code == 404


# ── T18 / AC-18 — audit trail ────────────────────────────────────────────
async def test_t18_audit_trail(client, db, patient_user):
    patient = await patient_of(db, patient_user)
    await client.patch("/api/v1/me/profile", json={"blood_group": "O+"})
    [row] = await audit_rows(db)
    assert row.action == "profile.update"
    assert row.actor_role == "patient" and row.actor_user_id == patient_user.id
    assert row.subject_patient_id == patient.id and row.resource_type == "patient"
    detail = json.loads(row.detail)
    assert detail == {"resource_id": str(patient.id), "fields_changed": ["blood_group"]}

    aid = (
        await client.post("/api/v1/me/allergies", json={"substance": "Dust", "severity": "mild"})
    ).json()["id"]
    await client.patch(f"/api/v1/me/allergies/{aid}", json={"severity": "severe"})
    await client.delete(f"/api/v1/me/allergies/{aid}")
    add, upd, rem = await audit_rows(db, "profile.allergy.")
    assert (add.action, upd.action, rem.action) == (
        "profile.allergy.add",
        "profile.allergy.update",
        "profile.allergy.remove",
    )
    assert all(r.resource_type == "allergy" for r in (add, upd, rem))
    assert json.loads(add.detail)["after"] == {
        "substance": "Dust",
        "reaction": None,
        "severity": "mild",
    }
    upd_d = json.loads(upd.detail)
    assert upd_d["fields_changed"] == ["severity"]
    assert (upd_d["before"], upd_d["after"]) == ({"severity": "mild"}, {"severity": "severe"})
    assert json.loads(rem.detail)["before"]["severity"] == "severe"

    mid = (
        await client.post("/api/v1/me/medications", json={"name": "Metformin", "notes": "a"})
    ).json()["id"]
    assert (
        "notes"
        not in json.loads((await audit_rows(db, "profile.medication.add"))[0].detail)["after"]
    )
    await client.patch(f"/api/v1/me/medications/{mid}", json={"notes": "b", "strength": "5 mg"})
    [med_upd] = await audit_rows(db, "profile.medication.update")
    d = json.loads(med_upd.detail)
    assert d["fields_changed"] == ["notes", "strength"]
    assert "notes" not in d["before"] and "notes" not in d["after"]

    count = len(await audit_rows(db))
    await client.patch("/api/v1/me/profile", json={"blood_group": "bad"})  # 422
    await client.post("/api/v1/me/allergies", json={"substance": "dust"})  # fine (removed)
    count += 1
    await client.post("/api/v1/me/allergies", json={"substance": "DUST"})  # 409
    await client.patch(f"/api/v1/me/allergies/{uuid.uuid4()}", json={})  # 404
    await client.patch("/api/v1/me/profile", json={"blood_group": "O+"})  # no-op
    assert len(await audit_rows(db)) == count


# ── T19 / AC-19 — no PHI in logs ─────────────────────────────────────────
async def test_t19_no_phi_in_logs(client, db, patient_user, caplog):
    import structlog.testing

    values = ["ZZ-SUBSTANCE-QX", "+923009998877", "ZZ-NOTES-QX", "1990-01-01", "ZZ-COND-QX"]
    with structlog.testing.capture_logs() as captured:
        await client.patch(
            "/api/v1/me/profile",
            json={"phone_e164": "+923009998877", "date_of_birth": "1990-01-01"},
        )
        await client.post("/en/patient/profile", data={"full_name": "Test Person"})
        aid = (
            await client.post("/api/v1/me/allergies", json={"substance": "ZZ-SUBSTANCE-QX"})
        ).json()["id"]
        await client.patch(f"/api/v1/me/allergies/{aid}", json={"reaction": "ZZ-NOTES-QX"})
        await client.delete(f"/api/v1/me/allergies/{aid}")
        await client.post("/api/v1/me/conditions", json={"name": "ZZ-COND-QX"})
        await client.post(
            "/api/v1/me/medications", json={"name": "Metformin", "notes": "ZZ-NOTES-QX"}
        )
    dumped = json.dumps(captured, default=str) + caplog.text
    for v in values:
        assert v not in dumped

    sample = {
        k: "secret"
        for k in (
            "date_of_birth",
            "emergency_contact",
            "blood_group",
            "gender",
            "substance",
            "reaction",
            "severity",
            "strength",
            "frequency",
            "started_on",
            "onset_date",
            "notes",
            "before",
            "after",
        )
    }
    assert set(_redact(sample).values()) == {PLACEHOLDER}


# ── T20 / AC-20 — write rate limit ───────────────────────────────────────
async def test_t20_write_rate_limit(client, db, patient_user, second_patient, monkeypatch):
    monkeypatch.setattr(settings, "profile_write_rate_limit_per_minute", 3)
    for _ in range(3):
        assert (
            await client.patch("/api/v1/me/profile", json={"blood_group": "O+"})
        ).status_code == 200
    r = await client.patch("/api/v1/me/profile", json={"blood_group": "O+"})
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "RATE_LIMITED"
    assert r.headers["retry-after"] == "60"

    web = await client.post("/en/patient/profile/allergies", data={"substance": "Dust"})
    assert web.status_code == 429
    assert translate("errors.rate_limited", "en") in web.text
    assert (await client.get("/api/v1/me/profile")).status_code == 200

    await login(client, "other@x.com")
    assert (await client.patch("/api/v1/me/profile", json={"blood_group": "A+"})).status_code == 200


# ── T21 / AC-21 — Urdu, RTL, language switch ─────────────────────────────
PROFILE_KEYS = [
    "nav.profile",
    "profile.title",
    "profile.subtitle",
    "profile.open_link",
    "profile.section.allergies",
    "profile.empty.medications",
    "profile.action.confirm_remove",
    "profile.saved.removed",
    "field.emergency_contact_help",
    "severity.unknown",
    "gender.prefer_not_to_say",
    "errors.not_found",
    "errors.not_editable",
    "errors.duplicate_entry",
    "errors.list_full",
    "errors.text_required",
    "errors.date_before_birth",
    "errors.phone_invalid",
    "errors.choice_invalid",
]


async def test_t21_urdu_rtl_and_switch(client, db, patient_user):
    ur = await client.get("/ur/patient/profile")
    assert ur.status_code == 200
    assert 'lang="ur"' in ur.text and 'dir="rtl"' in ur.text
    assert translate("profile.title", "ur") in ur.text
    assert translate("profile.section.allergies", "ur") in ur.text
    assert 'href="/en/patient/profile"' in ur.text

    en = await client.get("/en/patient/profile")
    assert 'href="/ur/patient/profile"' in en.text

    assert missing_keys()["ur"] == []
    en_catalogue = json.loads(Path("backend/app/i18n/messages/en.json").read_text(encoding="utf-8"))
    for key in PROFILE_KEYS:
        assert key in en_catalogue, key


# ── T22 / AC-22 — accessibility, design system, no-JS ────────────────────
async def test_t22_accessibility_design_and_no_js(client, db, patient_user):
    patient = await patient_of(db, patient_user)
    await add_allergy_row(db, patient.id, recorded_by=patient_user.id, substance="Dust")
    await add_medication_row(db, patient.id, recorded_by=patient_user.id)
    html = (await client.get("/en/patient/profile")).text

    ids = re.findall(r'\bid="([^"]+)"', html)
    assert len(ids) == len(set(ids)), "duplicate ids on the page"
    label_targets = set(re.findall(r'<label\b[^>]*\bfor="([^"]+)"', html))
    for tag in re.findall(r"<(?:input|select|textarea)\b[^>]*>", html):
        control_id = re.search(r'\bid="([^"]+)"', tag)
        assert control_id, tag
        assert control_id.group(1) in label_targets, tag

    forms = re.findall(r"<form\b[^>]*>", html)
    assert forms and all('method="post"' in f for f in forms)
    assert re.search(r'<script src="/static/js/profile.js" defer>', html)

    js = Path("frontend/static/js/profile.js").read_text(encoding="utf-8")
    assert "btn.disabled = true" in js and "aria-busy" in js

    css = Path("frontend/static/css/app.css").read_text(encoding="utf-8")
    assert ".entry-row" in css and ".badge--err" in css
    coarse = re.search(r"@media \(pointer: coarse\) \{(.*?)\n\}", css, flags=re.DOTALL)
    assert coarse and ".btn--sm" in coarse.group(1)


# ── T23 / AC-23 — output escaping ────────────────────────────────────────
async def test_t23_output_is_escaped(client, db, patient_user):
    script = "<script>alert(1)</script>"
    img = "<img src=x onerror=1>"
    aid = (await client.post("/api/v1/me/allergies", json={"substance": script})).json()["id"]
    await client.post("/api/v1/me/medications", json={"name": "Metformin", "notes": img})

    assert (await fresh(db, Allergy, uuid.UUID(aid))).substance == script
    api = (await client.get("/api/v1/me/profile")).json()
    assert api["allergies"][0]["substance"] == script

    html = (await client.get("/en/patient/profile")).text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert script not in html
    assert img not in html

    [med] = api["medications"]
    edit = (await client.get(f"/en/patient/profile?edit=medication&id={med['id']}")).text
    assert "&lt;img src=x onerror=1&gt;" in edit
    assert img not in edit


# ── T24 / AC-24 — entry point ────────────────────────────────────────────
async def test_t24_dashboard_links_to_profile(client, db, patient_user, clinic):
    dash = await client.get("/en/patient")
    assert 'href="/en/patient/profile"' in dash.text
    assert translate("nav.profile", "en") in dash.text
    assert translate("profile.open_link", "en") in dash.text

    page = await client.get("/en/patient/profile")
    assert 'href="/en/patient"' in page.text
    assert re.search(
        r'<a class="topnav__link" href="/en/patient/profile"\s+aria-current="page">', page.text
    )

    await make_user(
        db,
        email="vdoc@x.com",
        role=UserRole.DOCTOR,
        clinic_id=clinic.id,
        is_verified=True,
        verifier_id=patient_user.id,
    )
    await login(client, "vdoc@x.com")
    assert "/patient/profile" not in (await client.get("/en/doctor")).text

    await make_user(db, email="adm@x.com", role=UserRole.CLINIC_ADMIN, clinic_id=clinic.id)
    await login(client, "adm@x.com")
    assert "/patient/profile" not in (await client.get("/en/admin")).text


# ── T25 / AC-25 — migration is additive and protected ────────────────────
def test_t25_migration_is_additive():
    [path] = list(Path("alembic/versions").glob("*_patient_profile_*.py"))
    spec = importlib.util.spec_from_file_location("profile_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    assert module.down_revision == "6feacefae2d0"
    upgrade = inspect.getsource(module.upgrade)
    for banned in ("drop_", "alter_column", "rename"):
        assert banned not in upgrade, banned
    assert 'create_table(\n        "patient_medication"' in upgrade or (
        'create_table("patient_medication"' in upgrade
    )
    assert "ENABLE ROW LEVEL SECURITY" in upgrade
    assert 'dialect.name == "postgresql"' in upgrade

    downgrade = inspect.getsource(module.downgrade)
    assert 'drop_table("patient_medication")' in downgrade
