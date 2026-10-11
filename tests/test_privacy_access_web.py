"""Privacy & Access Control — server-rendered pages (P9 spec §4.10–4.12, §5).

`test_acNN_*` covers acceptance criterion AC-NN of
`.claude/specs/privacy_access_spec.md`: the doctor patient page (AC-17), the
patient access page (AC-24), no PHI in logs (AC-27) and the catalogue (AC-28).
"""

from __future__ import annotations

import json
import re
from datetime import timedelta

import pytest
from app.consent.schemas import SECTIONS
from app.db.models import (
    AccessLog,
    Allergy,
    ConsentGrant,
    Doctor,
    Patient,
    PatientMedication,
    Profile,
    UserRole,
)
from app.db.types import utcnow, uuid7
from app.i18n.catalogue import missing_keys, translate
from markupsafe import escape
from sqlalchemy import select

from tests.conftest import TEST_PASSWORD, make_user

ACCESS = "/en/patient/access"


# ── Helpers ──────────────────────────────────────────────────────────────
async def login(client, email: str) -> None:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": TEST_PASSWORD})
    assert r.status_code == 200, r.text


def in_html(key: str, locale: str = "en", **params) -> str:
    return str(escape(translate(key, locale, **params)))


async def patient_of(db, user: Profile) -> Patient:
    stmt = select(Patient).where(Patient.user_id == user.id)
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one()


async def doctor_of(db, user: Profile) -> Doctor:
    stmt = select(Doctor).where(Doctor.user_id == user.id)
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one()


async def access_rows(db) -> list[AccessLog]:
    rows = await db.execute(select(AccessLog).execution_options(populate_existing=True))
    return list(rows.scalars().all())


async def grant(db, patient, *, grantee_type, grantee_id, sections, **kw) -> ConsentGrant:
    row = ConsentGrant(
        id=uuid7(),
        patient_id=patient.id,
        grantee_type=grantee_type,
        grantee_id=grantee_id,
        scope_sections=list(sections),
        granted_at=kw.pop("granted_at", utcnow()),
        **kw,
    )
    db.add(row)
    await db.commit()
    return row


def section_states(html: str) -> dict[str, str]:
    """{section heading id: "not-shared" | "empty" | "data"} from the doctor page."""
    states = {}
    for key in SECTIONS:
        block = html.split(f'id="sec-{key}"', 1)[1].split("</section>", 1)[0]
        m = re.search(r'data-section-state="([a-z-]+)"', block)
        states[key] = m.group(1) if m else "data"
    return states


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
    d = await doctor_of(db, user)
    d.full_name, d.specialty = "Dr Sana Malik", "Pulmonology"
    await db.commit()
    return d


@pytest.fixture
async def patient(db) -> Patient:
    user = await make_user(db, email="pat@x.com", role=UserRole.PATIENT)
    p = await patient_of(db, user)
    p.blood_group = "B+"
    db.add(Allergy(patient_id=p.id, substance="Peanuts", severity="severe"))
    db.add(PatientMedication(patient_id=p.id, name="Montelukast", strength="10 mg"))
    await db.commit()
    return p


# ── AC-17 — the doctor patient page ──────────────────────────────────────
async def test_ac17_doctor_page_shows_shared_not_shared_and_nothing_recorded(
    client, db, doctor, patient
):
    await grant(
        db,
        patient,
        grantee_type="doctor",
        grantee_id=doctor.id,
        sections=["basics", "allergies", "conditions"],
    )
    await login(client, "dr@x.com")
    r = await client.get(f"/en/doctor/patient/{patient.id}")
    assert r.status_code == 200
    states = section_states(r.text)
    assert states["basics"] == "data" and states["allergies"] == "data"
    assert states["conditions"] == "empty"  # shared, but nothing recorded
    for key in ("medications", "prescriptions", "lab_reports", "visits"):
        assert states[key] == "not-shared"
    assert "Peanuts" in r.text and "B+" in r.text
    assert "Montelukast" not in r.text  # medications weren't shared
    assert r.text.count(in_html("doctor_patient.not_shared")) == 4

    rows = await access_rows(db)
    assert sorted(a.resource_type for a in rows) == [
        "section.allergies",
        "section.basics",
        "section.conditions",
    ]


async def test_ac17_clinic_grant_reaches_affiliated_doctor_page(
    client, db, doctor, patient, clinic
):
    await grant(db, patient, grantee_type="clinic", grantee_id=clinic.id, sections=["medications"])
    await login(client, "dr@x.com")
    r = await client.get(f"/en/doctor/patient/{patient.id}")
    assert r.status_code == 200 and "Montelukast" in r.text and "Peanuts" not in r.text
    [row] = await access_rows(db)
    assert row.resource_type == "section.medications"


@pytest.mark.parametrize("case", ["none", "revoked", "expired", "empty", "missing"])
async def test_ac16_doctor_page_refusals_are_identical(client, db, doctor, patient, case):
    target = patient.id
    if case == "revoked":
        await grant(
            db,
            patient,
            grantee_type="doctor",
            grantee_id=doctor.id,
            sections=["basics"],
            revoked_at=utcnow(),
        )
    elif case == "expired":
        await grant(
            db,
            patient,
            grantee_type="doctor",
            grantee_id=doctor.id,
            sections=["basics"],
            expires_at=utcnow() - timedelta(minutes=1),
        )
    elif case == "empty":
        await grant(db, patient, grantee_type="doctor", grantee_id=doctor.id, sections=[])
    elif case == "missing":
        target = uuid7()
    await login(client, "dr@x.com")
    r = await client.get(f"/en/doctor/patient/{target}")
    assert r.status_code == 404
    assert in_html("lookup.not_found") in r.text
    assert patient.passport_no not in r.text
    assert await access_rows(db) == []


# ── AC-24 — the patient access page ──────────────────────────────────────
async def test_ac24_page_has_three_parts_and_dashboard_link(client, db, patient):
    await login(client, "pat@x.com")
    page = (await client.get(ACCESS)).text
    for key in ("access.section.who_can_see", "access.section.grant", "access.section.log"):
        assert in_html(key) in page
    assert in_html("access.log.empty") in page
    assert 'href="/en/patient/access"' in (await client.get("/en/patient")).text


async def test_ac24_search_then_grant_with_defaults(client, db, doctor, patient):
    await login(client, "pat@x.com")
    r = await client.get(ACCESS, params={"q": "sana"})
    assert r.status_code == 200
    assert "Dr Sana Malik" in r.text and "Pulmonology" in r.text
    assert f'value="doctor:{doctor.id}"' in r.text
    # BL-24: no section pre-ticked; 30 days pre-selected.
    form = r.text.split('action="/en/patient/access" novalidate', 1)[1]
    assert not re.search(r'name="sections" value="[a-z_]+"\s+checked', form)
    assert re.search(r'name="duration" value="30d"\s+checked', form)

    r = await client.post(
        ACCESS,
        data={
            "grantee": f"doctor:{doctor.id}",
            "sections": ["allergies", "basics"],
            "duration": "7d",
            "q": "sana",
        },
    )
    assert r.status_code == 303 and r.headers["location"].endswith("?saved=granted")
    [row] = (await db.execute(select(ConsentGrant))).scalars().all()
    assert row.scope_sections == ["basics", "allergies"]
    assert row.expires_at - row.granted_at == timedelta(days=7)


async def test_ac24_failed_grant_reshows_results_and_choices(client, db, doctor, patient):
    await login(client, "pat@x.com")
    r = await client.post(
        ACCESS, data={"grantee": f"doctor:{doctor.id}", "duration": "24h", "q": "sana"}
    )
    assert r.status_code == 422
    assert in_html("errors.sections_required") in r.text
    assert f'value="doctor:{doctor.id}"\n' in r.text or f'value="doctor:{doctor.id}" ' in r.text
    assert re.search(r'name="duration" value="24h"\s+checked', r.text)


async def test_ac24_search_errors_and_no_results(client, db, patient):
    await login(client, "pat@x.com")
    short = await client.get(ACCESS, params={"q": "a"})
    assert short.status_code == 422
    assert in_html("errors.query_too_short", min=2) in short.text
    none = await client.get(ACCESS, params={"q": "zzzz"})
    assert none.status_code == 200 and in_html("access.search.none") in none.text


async def test_ac24_save_sections_and_past_grants(client, db, doctor, patient):
    g = await grant(db, patient, grantee_type="doctor", grantee_id=doctor.id, sections=["basics"])
    await login(client, "pat@x.com")
    page = (await client.get(ACCESS)).text
    assert re.search(rf'id="g-{g.id}-basics" name="sections" value="basics"\s+checked', page)

    r = await client.post(
        f"/en/patient/access/{g.id}/sections", data={"sections": ["basics", "medications"]}
    )
    assert r.status_code == 303 and r.headers["location"].endswith("?saved=changed")
    page = (await client.get(f"{ACCESS}?saved=changed")).text
    assert in_html("access.saved.changed") in page
    assert in_html("access.section.past") in page and in_html("access.replaced") in page

    empty = await client.post(f"/en/patient/access/{uuid7()}/sections", data={})
    assert empty.status_code == 422  # validated before the lookup
    foreign = await client.post(
        f"/en/patient/access/{uuid7()}/sections", data={"sections": "basics"}
    )
    assert foreign.status_code == 404 and in_html("errors.not_found") in foreign.text


async def test_ac24_who_looked_lists_reads(client, db, doctor, patient, clinic):
    await grant(db, patient, grantee_type="clinic", grantee_id=clinic.id, sections=["allergies"])
    await login(client, "dr@x.com")
    assert (await client.get(f"/en/doctor/patient/{patient.id}")).status_code == 200
    client.cookies.clear()
    await login(client, "pat@x.com")
    page = (await client.get(ACCESS)).text
    log = page.split('id="log-h"', 1)[1]
    assert "Dr Sana Malik" in log and clinic.name in log
    assert in_html("section.allergies") in log


async def test_ac24_urdu_page_is_rtl(client, db, doctor, patient):
    await grant(db, patient, grantee_type="doctor", grantee_id=doctor.id, sections=["basics"])
    await login(client, "pat@x.com")
    ur = await client.get("/ur/patient/access", params={"q": "sana"})
    assert ur.status_code == 200
    assert 'dir="rtl"' in ur.text and 'lang="ur"' in ur.text
    assert in_html("access.title", "ur") in ur.text
    assert in_html("section.basics", "ur") in ur.text
    assert in_html("access.duration.30d", "ur") in ur.text


async def test_ac24_search_on_the_page_is_rate_limited(client, db, patient, monkeypatch):
    from app.settings import settings

    monkeypatch.setattr(settings, "consent_search_rate_limit_per_minute", 2)
    await login(client, "pat@x.com")
    for _ in range(2):
        assert (await client.get(ACCESS, params={"q": "zz"})).status_code == 200
    r = await client.get(ACCESS, params={"q": "zz"})
    assert r.status_code == 429 and in_html("errors.rate_limited") in r.text


# ── AC-27 — no PHI or identifiers in logs ────────────────────────────────
async def test_ac27_no_pii_in_logs(client, db, doctor, patient, clinic, caplog):
    import structlog.testing

    await login(client, "pat@x.com")
    with structlog.testing.capture_logs() as captured:
        await client.get(ACCESS, params={"q": "Sana Malik"})
        await client.post(
            ACCESS,
            data={"grantee": f"doctor:{doctor.id}", "sections": ["allergies"], "q": "Sana Malik"},
        )
        await client.get("/api/v1/me/consents/grantees", params={"q": "Shifa"})
        client.cookies.clear()
        await login(client, "dr@x.com")
        await client.get(f"/en/doctor/patient/{patient.id}")
    dumped = json.dumps(captured, default=str) + caplog.text
    for v in ("Sana Malik", "Shifa", "Peanuts", doctor.pmdc_number, patient.passport_no):
        assert v not in dumped


# ── AC-28 — catalogue ────────────────────────────────────────────────────
def test_ac28_catalogue_complete():
    assert missing_keys()["ur"] == []
    for key in [f"section.{s}" for s in SECTIONS] + [
        "access.section.who_can_see",
        "access.section.log",
        "doctor_patient.not_shared",
        "doctor_patient.nothing_recorded",
        "errors.sections_required",
        "errors.cursor_invalid",
    ]:
        assert translate(key, "en") != key and translate(key, "ur") != key, key
