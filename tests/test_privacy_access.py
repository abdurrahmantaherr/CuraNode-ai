"""Privacy & Access Control test suite (P9: FR4 per-section, FR5).

`test_acNN_*` covers acceptance criterion AC-NN of
`.claude/specs/privacy_access_spec.md`. Web-page criteria (AC-17 page,
AC-24, AC-28) live in `tests/test_privacy_access_web.py`.

Runs on the usual harness: in-memory SQLite and the fake Supabase Auth
double, no network. The migration test builds its own throwaway SQLite
database; nothing here touches the shared Supabase project.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.consent import gateway
from app.consent import service as consent_service
from app.consent.schemas import SECTIONS
from app.db.models import (
    AccessLog,
    Allergy,
    AuditLog,
    ChronicCondition,
    Clinic,
    ConsentGrant,
    Doctor,
    DoctorAffiliation,
    Patient,
    PatientMedication,
    Profile,
    UserRole,
)
from app.db.types import utcnow, uuid7
from app.deps import Actor
from app.errors import ConsentDenied, NotFound
from app.settings import settings
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from tests.conftest import TEST_PASSWORD, make_user

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "dba968144fb4_consent_grant_sections_replaced_by.py"
ALL = ["basics", "allergies", "conditions", "medications", "prescriptions", "lab_reports", "visits"]


# ── S1: models and migration (AC-29) ─────────────────────────────────────
def _load_migration():
    spec = importlib.util.spec_from_file_location("p9_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _pre_p9_schema(conn) -> None:
    """The slice of the shared schema the migration touches, as it was before P9."""
    conn.exec_driver_sql("CREATE TABLE user_profile (user_id CHAR(32) PRIMARY KEY)")
    conn.exec_driver_sql("CREATE TABLE patient (patient_id CHAR(32) PRIMARY KEY)")
    conn.exec_driver_sql(
        "CREATE TABLE consent_grant ("
        " consent_id CHAR(32) PRIMARY KEY,"
        " patient_id CHAR(32) NOT NULL REFERENCES patient(patient_id),"
        " grantee_type VARCHAR(20) NOT NULL,"
        " grantee_id CHAR(32) NOT NULL,"
        " scope_sections JSON NOT NULL DEFAULT '[]',"
        " granted_at DATETIME NOT NULL,"
        " expires_at DATETIME,"
        " revoked_at DATETIME)"
    )
    conn.exec_driver_sql("INSERT INTO patient VALUES ('p1')")
    conn.exec_driver_sql(
        "INSERT INTO consent_grant (consent_id, patient_id, grantee_type, grantee_id,"
        " scope_sections, granted_at) VALUES"
        " ('g1', 'p1', 'doctor', 'd1', '[]', '2026-10-01'),"
        " ('g2', 'p1', 'doctor', 'd2', '[\"allergies\"]', '2026-10-01')"
    )


def _run(conn, fn) -> None:
    with Operations.context(MigrationContext.configure(conn)):
        fn()


def test_ac29_migration_upgrades_and_downgrades_on_sqlite():
    module = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        _pre_p9_schema(conn)
        _run(conn, module.upgrade)

        cols = {c["name"] for c in sa.inspect(conn).get_columns("consent_grant")}
        assert {"revoked_by", "replaced_by"} <= cols
        rows = dict(
            conn.exec_driver_sql("SELECT consent_id, scope_sections FROM consent_grant").all()
        )
        assert json.loads(rows["g1"]) == ALL  # legacy [] → every section
        assert json.loads(rows["g2"]) == ["allergies"]  # a real choice is left alone

        # A second unrevoked grant for the same grantee is refused …
        with pytest.raises(sa.exc.IntegrityError), conn.begin_nested():
            conn.exec_driver_sql(
                "INSERT INTO consent_grant (consent_id, patient_id, grantee_type,"
                " grantee_id, scope_sections, granted_at)"
                " VALUES ('g3', 'p1', 'doctor', 'd1', '[]', '2026-10-02')"
            )
        # … but one alongside a revoked grant is fine.
        conn.exec_driver_sql(
            "UPDATE consent_grant SET revoked_at = '2026-10-02' WHERE consent_id='g1'"
        )
        conn.exec_driver_sql(
            "INSERT INTO consent_grant (consent_id, patient_id, grantee_type, grantee_id,"
            " scope_sections, granted_at) VALUES ('g3', 'p1', 'doctor', 'd1', '[]', '2026-10-02')"
        )

        _run(conn, module.downgrade)
        cols = {c["name"] for c in sa.inspect(conn).get_columns("consent_grant")}
        assert "revoked_by" not in cols and "replaced_by" not in cols
        indexes = {i["name"] for i in sa.inspect(conn).get_indexes("consent_grant")}
        assert "uq_consent_grant_one_active" not in indexes


def test_ac29_migration_is_additive():
    """Static check: upgrade() only adds — no drop, rename or type change."""
    source = MIGRATION.read_text(encoding="utf-8")
    upgrade = source.split("def upgrade()")[1].split("def downgrade()")[0]
    for forbidden in ("drop_", "alter_column", "rename", "DROP ", "DELETE "):
        assert forbidden not in upgrade
    assert 'down_revision: str | Sequence[str] | None = "e3a52c8d7f14"' in source


def test_ac29_revision_chain_is_single_and_unbroken():
    """One head, and the chain runs through both reconstructed upstream revisions."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["dba968144fb4"]
    chain = [rev.revision for rev in script.walk_revisions("base", "heads")]
    assert chain == [
        "dba968144fb4",
        "e3a52c8d7f14",
        "35c4bc13fa37",
        "488dbf2b3915",
        "6feacefae2d0",
    ]


def test_ac29_placeholder_upgrade_is_noop_and_downgrade_refuses():
    spec = importlib.util.spec_from_file_location(
        "placeholder",
        ROOT / "alembic" / "versions" / "35c4bc13fa37_reconstructed_document_ocr_placeholder.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        _run(conn, module.upgrade)  # touches nothing
        assert sa.inspect(conn).get_table_names() == []
        with pytest.raises(RuntimeError, match="placeholder"):
            _run(conn, module.downgrade)


def test_ac29_e3a52c8d7f14_placeholder_upgrade_is_noop_and_downgrade_refuses():
    spec = importlib.util.spec_from_file_location(
        "placeholder_e3a52c8d7f14",
        ROOT / "alembic" / "versions" / "e3a52c8d7f14_reconstructed_ocr_placeholder.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert module.down_revision == "35c4bc13fa37"
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        _run(conn, module.upgrade)  # touches nothing
        assert sa.inspect(conn).get_table_names() == []
        with pytest.raises(RuntimeError, match="placeholder"):
            _run(conn, module.downgrade)


async def test_ac29_one_unrevoked_grant_per_grantee_in_the_model(db):
    user = await make_user(db, email="p@x.com", role=UserRole.PATIENT)
    await db.refresh(user, ["patient"])
    grantee = uuid7()

    def grant(**kw) -> ConsentGrant:
        return ConsentGrant(
            id=uuid7(),
            patient_id=user.patient.id,
            grantee_type="doctor",
            grantee_id=grantee,
            scope_sections=["basics"],
            granted_at=utcnow(),
            **kw,
        )

    db.add(grant(revoked_at=utcnow()))
    db.add(grant())
    await db.commit()  # one revoked + one active is fine
    db.add(grant())
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_ac29_access_log_action_is_constrained(db):
    user = await make_user(db, email="p@x.com", role=UserRole.PATIENT)
    await db.refresh(user, ["patient"])
    db.add(
        AccessLog(
            patient_id=user.patient.id,
            resource_type="section.basics",
            resource_id=user.patient.id,
            action="delete",
        )
    )
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


# ── Shared fixtures and helpers (S3 onward) ──────────────────────────────
URL = "/api/v1/me/consents"
SEARCH = f"{URL}/grantees"


async def login(client, email: str) -> None:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": TEST_PASSWORD})
    assert r.status_code == 200, r.text


async def patient_of(db, user: Profile) -> Patient:
    stmt = select(Patient).where(Patient.user_id == user.id)
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one()


async def doctor_of(db, user: Profile) -> Doctor:
    stmt = select(Doctor).where(Doctor.user_id == user.id)
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one()


async def grants(db) -> list[ConsentGrant]:
    rows = await db.execute(
        select(ConsentGrant)
        .order_by(ConsentGrant.granted_at, ConsentGrant.id)
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


async def audit_rows(db, prefix: str = "consent.") -> list[AuditLog]:
    rows = await db.execute(
        select(AuditLog)
        .where(AuditLog.action.like(f"{prefix}%"))
        .order_by(AuditLog.id)
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


def error_body(r) -> dict:
    error = dict(r.json()["error"])
    error.pop("request_id")
    return error


@pytest.fixture
async def admin(db, clinic) -> Profile:
    return await make_user(db, email="adm@x.com", role=UserRole.CLINIC_ADMIN, clinic_id=clinic.id)


@pytest.fixture
async def verified_doctor(db, admin) -> Doctor:
    # No clinic affiliation: only a grant naming this doctor reaches them.
    user = await make_user(
        db, email="dr@x.com", role=UserRole.DOCTOR, is_verified=True, verifier_id=admin.id
    )
    doctor = await doctor_of(db, user)
    doctor.full_name, doctor.specialty = "Dr Ayesha Khan", "Cardiology"
    await db.commit()
    return doctor


@pytest.fixture
async def clinic_doctor(db, clinic, admin) -> Doctor:
    """Verified, with an active affiliation at `clinic`."""
    user = await make_user(
        db,
        email="cd@x.com",
        role=UserRole.DOCTOR,
        clinic_id=clinic.id,
        is_verified=True,
        verifier_id=admin.id,
    )
    doctor = await doctor_of(db, user)
    doctor.full_name, doctor.specialty = "Dr Bilal Ahmed", "Dermatology"
    await db.commit()
    return doctor


@pytest.fixture
async def unverified_doctor(db, clinic) -> Doctor:
    user = await make_user(db, email="pending@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    doctor = await doctor_of(db, user)
    doctor.full_name = "Dr Ayesha Pending"
    await db.commit()
    return doctor


@pytest.fixture
async def patient_user(client, db) -> Profile:
    user = await make_user(db, email="pat@x.com", role=UserRole.PATIENT)
    await login(client, "pat@x.com")
    return user


@pytest.fixture
async def second_patient(db) -> Profile:
    return await make_user(db, email="other@x.com", role=UserRole.PATIENT)


async def grant_row(
    db,
    patient_id,
    *,
    grantee_type: str,
    grantee_id,
    sections=("basics",),
    granted_at=None,
    expires_at=None,
    revoked_at=None,
) -> ConsentGrant:
    row = ConsentGrant(
        id=uuid7(),
        patient_id=patient_id,
        grantee_type=grantee_type,
        grantee_id=grantee_id,
        scope_sections=list(sections),
        granted_at=granted_at or utcnow(),
        expires_at=expires_at,
        revoked_at=revoked_at,
    )
    db.add(row)
    await db.commit()
    return row


# ── S3: grants (AC-01…AC-12) ─────────────────────────────────────────────
async def test_ac01_grant_with_sections_canonical_order(client, db, patient_user, verified_doctor):
    body = {
        "doctor_id": str(verified_doctor.id),
        "sections": ["medications", "basics", "medications"],
    }
    r = await client.post(URL, json=body)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["sections"] == ["basics", "medications"]
    assert out["grantee_name"] == "Dr Ayesha Khan"
    assert out["grantee_detail"] == "Cardiology"
    [row] = await grants(db)
    assert row.scope_sections == ["basics", "medications"]


@pytest.mark.parametrize(
    ("sections", "key"),
    [([], "errors.sections_required"), (["x-ray"], "errors.section_invalid"), (None, None)],
)
async def test_ac02_sections_required_and_validated(
    client, db, patient_user, verified_doctor, sections, key
):
    body: dict = {"doctor_id": str(verified_doctor.id)}
    if sections is not None:
        body["sections"] = sections
    r = await client.post(URL, json=body)
    assert r.status_code == 422
    fields = r.json()["error"]["details"]["fields"]
    assert "sections" in fields
    if key:
        assert fields["sections"] == key
    assert await grants(db) == [] and await audit_rows(db) == []


@pytest.mark.parametrize(
    ("duration", "days"), [("24h", 1), ("7d", 7), ("30d", 30), ("until_revoked", None), (None, 30)]
)
async def test_ac03_duration_sets_expiry(client, db, patient_user, verified_doctor, duration, days):
    body: dict = {"doctor_id": str(verified_doctor.id), "sections": ["basics"]}
    if duration is not None:
        body["duration"] = duration
    assert (await client.post(URL, json=body)).status_code == 201
    [row] = await grants(db)
    if days is None:
        assert row.expires_at is None
    else:
        assert row.expires_at - row.granted_at == timedelta(days=days)


async def test_ac03_invalid_duration(client, db, patient_user, verified_doctor):
    r = await client.post(
        URL, json={"doctor_id": str(verified_doctor.id), "sections": ["basics"], "duration": "1y"}
    )
    assert r.status_code == 422
    assert r.json()["error"]["details"]["fields"]["duration"] == "errors.duration_invalid"


async def test_ac04_grant_by_each_identifier(client, db, patient_user, verified_doctor, clinic):
    sec = {"sections": ["basics"]}
    r1 = await client.post(URL, json={**sec, "doctor_id": str(verified_doctor.id)})
    r2 = await client.post(URL, json={**sec, "clinic_id": str(clinic.id)})
    assert (r1.status_code, r2.status_code) == (201, 201)
    assert r2.json()["grantee_type"] == "clinic" and r2.json()["grantee_detail"] == clinic.city
    # P4 identifiers name the same grantees, so they return the existing grants.
    r3 = await client.post(URL, json={**sec, "pmdc_number": verified_doctor.pmdc_number})
    r4 = await client.post(URL, json={**sec, "clinic_name": clinic.name})
    assert (r3.status_code, r4.status_code) == (200, 200)
    assert len(await grants(db)) == 2


async def test_ac04_unknown_and_unverified_identical_404(
    client, db, patient_user, unverified_doctor, clinic
):
    closed = Clinic(id=uuid7(), name="Closed Clinic", type="clinic", status="inactive")
    db.add(closed)
    await db.commit()
    sec = {"sections": ["basics"]}
    bodies = [
        {**sec, "doctor_id": str(unverified_doctor.id)},
        {**sec, "doctor_id": str(uuid7())},
        {**sec, "clinic_id": str(closed.id)},
        {**sec, "clinic_id": str(uuid7())},
        {**sec, "pmdc_number": unverified_doctor.pmdc_number},
    ]
    responses = [await client.post(URL, json=b) for b in bodies]
    assert {r.status_code for r in responses} == {404}
    assert len({json.dumps(error_body(r), sort_keys=True) for r in responses}) == 1
    assert await grants(db) == []


@pytest.mark.parametrize(
    "ids", [{}, {"doctor_id": "D", "clinic_id": "C"}, {"doctor_id": "D", "pmdc_number": "x"}]
)
async def test_ac04_exactly_one_identifier(client, db, patient_user, verified_doctor, clinic, ids):
    swap = {"D": str(verified_doctor.id), "C": str(clinic.id)}
    body = {k: swap.get(v, v) for k, v in ids.items()} | {"sections": ["basics"]}
    r = await client.post(URL, json=body)
    assert r.status_code == 422 and await grants(db) == []


async def test_ac05_duplicate_returns_existing_without_writes(
    client, db, patient_user, verified_doctor
):
    body = {"doctor_id": str(verified_doctor.id), "sections": ["basics"]}
    first = await client.post(URL, json=body)
    again = await client.post(URL, json={**body, "sections": ["visits"], "duration": "24h"})
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["sections"] == ["basics"]
    assert len(await grants(db)) == 1 and len(await audit_rows(db)) == 1


async def test_ac06_lapsed_grant_closed_and_linked(client, db, patient_user, verified_doctor):
    patient = await patient_of(db, patient_user)
    lapsed = await grant_row(
        db,
        patient.id,
        grantee_type="doctor",
        grantee_id=verified_doctor.id,
        expires_at=utcnow() - timedelta(minutes=5),
    )
    r = await client.post(URL, json={"doctor_id": str(verified_doctor.id), "sections": ["visits"]})
    assert r.status_code == 201
    old = await db.get(ConsentGrant, lapsed.id, populate_existing=True)
    assert old.revoked_at is not None and old.revoked_by is None  # closed by the system
    assert str(old.replaced_by) == r.json()["id"]


async def test_ac07_change_sections_revokes_and_links(client, db, patient_user, verified_doctor):
    created = await client.post(
        URL, json={"doctor_id": str(verified_doctor.id), "sections": ["basics"], "duration": "7d"}
    )
    gid = created.json()["id"]
    r = await client.put(f"{URL}/{gid}/sections", json={"sections": ["allergies", "basics"]})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["id"] != gid and new["sections"] == ["basics", "allergies"]

    old = await db.get(ConsentGrant, uuid.UUID(gid), populate_existing=True)
    replacement = await db.get(ConsentGrant, uuid.UUID(new["id"]), populate_existing=True)
    assert replacement.expires_at == old.expires_at  # same end date
    assert old.revoked_at is not None
    assert old.revoked_by == patient_user.id
    assert str(old.replaced_by) == new["id"]
    [change] = await audit_rows(db, "consent.change")
    detail = json.loads(change.detail)
    assert detail["replaces"] == gid and detail["resource_id"] == new["id"]
    assert detail["sections_before"] == ["basics"]
    assert detail["sections_after"] == ["basics", "allergies"]


async def test_ac07_same_sections_is_noop(client, db, patient_user, verified_doctor):
    body = {"doctor_id": str(verified_doctor.id), "sections": ["basics", "visits"]}
    gid = (await client.post(URL, json=body)).json()["id"]
    r = await client.put(f"{URL}/{gid}/sections", json={"sections": ["visits", "basics"]})
    assert r.status_code == 200 and r.json()["id"] == gid
    assert len(await grants(db)) == 1 and len(await audit_rows(db)) == 1


async def test_ac07_change_needs_sections(client, db, patient_user, verified_doctor):
    body = {"doctor_id": str(verified_doctor.id), "sections": ["basics"]}
    gid = (await client.post(URL, json=body)).json()["id"]
    r = await client.put(f"{URL}/{gid}/sections", json={"sections": []})
    assert r.status_code == 422


async def test_ac08_foreign_missing_inactive_identical_404(
    client, db, patient_user, second_patient, verified_doctor, clinic
):
    me = await patient_of(db, patient_user)
    other = await patient_of(db, second_patient)
    foreign = await grant_row(db, other.id, grantee_type="doctor", grantee_id=verified_doctor.id)
    revoked = await grant_row(
        db, me.id, grantee_type="doctor", grantee_id=verified_doctor.id, revoked_at=utcnow()
    )
    expired = await grant_row(
        db,
        me.id,
        grantee_type="clinic",
        grantee_id=clinic.id,
        expires_at=utcnow() - timedelta(minutes=1),
    )
    body = {"sections": ["allergies"]}
    responses = [
        await client.put(f"{URL}/{gid}/sections", json=body)
        for gid in (foreign.id, uuid7(), revoked.id, expired.id)
    ]
    responses.append(await client.delete(f"{URL}/{foreign.id}"))
    responses.append(await client.delete(f"{URL}/{uuid7()}"))
    assert {r.status_code for r in responses} == {404}
    assert len({json.dumps(error_body(r), sort_keys=True) for r in responses}) == 1
    assert (await db.get(ConsentGrant, foreign.id, populate_existing=True)).revoked_at is None


async def test_ac09_revoke_sets_revoked_by_and_is_idempotent(
    client, db, patient_user, verified_doctor
):
    body = {"doctor_id": str(verified_doctor.id), "sections": ["basics"]}
    gid = (await client.post(URL, json=body)).json()["id"]
    assert (await client.delete(f"{URL}/{gid}")).status_code == 204
    row = await db.get(ConsentGrant, uuid.UUID(gid), populate_existing=True)
    first_revoke = row.revoked_at
    assert first_revoke is not None and row.revoked_by == patient_user.id
    assert (await client.delete(f"{URL}/{gid}")).status_code == 204
    row = await db.get(ConsentGrant, uuid.UUID(gid), populate_existing=True)
    assert row.revoked_at == first_revoke
    assert len(await audit_rows(db, "consent.revoke")) == 1


async def test_ac10_list_active_and_include_revoked(
    client, db, patient_user, verified_doctor, clinic_doctor, clinic
):
    me = await patient_of(db, patient_user)
    now = utcnow()
    old_active = await grant_row(
        db, me.id, grantee_type="clinic", grantee_id=clinic.id, granted_at=now - timedelta(days=3)
    )
    new_active = await grant_row(
        db,
        me.id,
        grantee_type="doctor",
        grantee_id=verified_doctor.id,
        granted_at=now - timedelta(days=1),
    )
    revoked = await grant_row(
        db,
        me.id,
        grantee_type="doctor",
        grantee_id=clinic_doctor.id,
        granted_at=now - timedelta(hours=1),
        revoked_at=now,
    )
    active = (await client.get(URL)).json()
    assert [g["id"] for g in active] == [str(new_active.id), str(old_active.id)]
    assert all(g["active"] for g in active)

    every = (await client.get(URL, params={"include_revoked": "true"})).json()
    assert [g["id"] for g in every] == [str(new_active.id), str(old_active.id), str(revoked.id)]
    assert every[-1]["active"] is False and every[-1]["revoked_at"] is not None


async def test_ac11_search_verified_doctors_and_clinics_only(
    client, db, patient_user, verified_doctor, clinic_doctor, unverified_doctor, clinic
):
    db.add(Clinic(id=uuid7(), name="Ayesha Closed Clinic", type="clinic", status="inactive"))
    db.add(Clinic(id=uuid7(), name="Ayesha Care Clinic", type="clinic", city="Lahore"))
    await db.commit()

    r = await client.get(SEARCH, params={"q": "ayesha"})
    assert r.status_code == 200
    found = [(g["type"], g["name"], g["detail"]) for g in r.json()]
    assert found == [
        ("doctor", "Dr Ayesha Khan", "Cardiology"),
        ("clinic", "Ayesha Care Clinic", "Lahore"),
    ]
    assert set(r.json()[0]) == {"type", "id", "name", "detail"}  # no PMDC, no user id

    by_specialty = (await client.get(SEARCH, params={"q": "DERMA"})).json()
    assert [g["name"] for g in by_specialty] == ["Dr Bilal Ahmed"]


@pytest.mark.parametrize("q", ["", " ", "a"])
async def test_ac11_short_query_422(client, db, patient_user, q):
    r = await client.get(SEARCH, params={"q": q})
    assert r.status_code == 422
    assert r.json()["error"]["details"]["fields"]["q"] == "errors.query_too_short"


async def test_ac11_wildcards_are_literal(client, db, patient_user, verified_doctor):
    assert (await client.get(SEARCH, params={"q": "%%"})).json() == []
    assert (await client.get(SEARCH, params={"q": "__"})).json() == []


async def test_ac12_one_audit_row_per_change_no_names(client, db, patient_user, clinic):
    body = {"clinic_name": clinic.name, "sections": ["basics"]}
    gid = (await client.post(URL, json=body)).json()["id"]
    new = (await client.put(f"{URL}/{gid}/sections", json={"sections": ["visits"]})).json()["id"]
    await client.delete(f"{URL}/{new}")
    await client.get(URL)
    await client.get(SEARCH, params={"q": "shifa"})
    rows = await audit_rows(db)
    assert [r.action for r in rows] == ["consent.grant", "consent.change", "consent.revoke"]
    for row in rows:
        assert clinic.name not in row.detail and "shifa" not in row.detail.lower()


# ── Access matrix and rate limits (AC-25, AC-26) ─────────────────────────
ME_ENDPOINTS = [
    ("GET", SEARCH + "?q=ab"),
    ("GET", URL),
    ("POST", URL),
    ("PUT", f"{URL}/{uuid.uuid4()}/sections"),
    ("DELETE", f"{URL}/{uuid.uuid4()}"),
]


@pytest.mark.parametrize(("method", "url"), ME_ENDPOINTS)
async def test_ac25_anonymous_gets_401(client, method, url):
    r = await client.request(method, url, json={"sections": ["basics"]})
    assert r.status_code == 401


@pytest.mark.parametrize(("method", "url"), ME_ENDPOINTS)
async def test_ac25_doctor_and_admin_get_403(client, db, verified_doctor, admin, method, url):
    for email in ("dr@x.com", "adm@x.com"):
        await login(client, email)
        r = await client.request(method, url, json={"sections": ["basics"]})
        assert r.status_code == 403, (email, url)
        client.cookies.clear()


async def test_ac26_search_rate_limit(client, db, patient_user):
    for _ in range(settings.consent_search_rate_limit_per_minute):
        assert (await client.get(SEARCH, params={"q": "zz"})).status_code == 200
    r = await client.get(SEARCH, params={"q": "zz"})
    assert r.status_code == 429 and r.headers["retry-after"] == "60"


async def test_ac26_writes_share_one_budget(client, db, patient_user, verified_doctor):
    body = {"doctor_id": str(verified_doctor.id), "sections": ["basics"]}
    gid = (await client.post(URL, json=body)).json()["id"]
    for i in range(settings.consent_write_rate_limit_per_minute - 1):
        sections = ["basics"] if i % 2 else ["visits"]
        r = await client.put(f"{URL}/{gid}/sections", json={"sections": sections})
        assert r.status_code == 200
        gid = r.json()["id"]
    r = await client.delete(f"{URL}/{gid}")
    assert r.status_code == 429 and r.headers["retry-after"] == "60"


# ── S4: gateway (AC-13…AC-20, AC-23) ─────────────────────────────────────
PURPOSE = "test.read"


def as_actor(user_id, role: str) -> Actor:
    return Actor(user_id=user_id, role=role, ip_address="203.0.113.7")


async def seed_record(db, patient: Patient) -> None:
    patient.blood_group, patient.gender = "O+", "female"
    db.add(Allergy(patient_id=patient.id, substance="Penicillin", severity="severe"))
    db.add(Allergy(patient_id=patient.id, substance="Latex", removed_at=utcnow()))
    db.add(ChronicCondition(patient_id=patient.id, name="Asthma"))
    db.add(ChronicCondition(patient_id=patient.id, name="Old flu", status="resolved"))
    db.add(PatientMedication(patient_id=patient.id, name="Salbutamol", frequency="as needed"))
    await db.commit()


async def access_rows(db) -> list[AccessLog]:
    rows = await db.execute(
        select(AccessLog).order_by(AccessLog.occurred_at).execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


@pytest.fixture
async def record(db) -> tuple[Profile, Patient]:
    user = await make_user(db, email="rec@x.com", role=UserRole.PATIENT)
    patient = await patient_of(db, user)
    await seed_record(db, patient)
    return user, patient


async def test_ac13_owner_gets_all_sections_and_no_log_rows(db, record):
    user, patient = record
    view = await gateway.read_sections(
        db, as_actor(user.id, "patient"), patient.id, purpose=PURPOSE
    )
    assert set(view.sections) == set(SECTIONS)
    assert all(v is not None for v in view.sections.values())
    assert await access_rows(db) == []


async def test_ac14_doctor_grant_sections_only(db, record, verified_doctor):
    _, patient = record
    grant = await grant_row(
        db,
        patient.id,
        grantee_type="doctor",
        grantee_id=verified_doctor.id,
        sections=["allergies", "medications"],
    )
    actor = as_actor(verified_doctor.user_id, "doctor")
    access = await gateway.allowed_sections(db, actor, patient.id, purpose=PURPOSE)
    assert access.sections == {"allergies", "medications"} and not access.is_owner
    assert set(access.consent_for.values()) == {grant.id}

    view = await gateway.read_sections(db, actor, patient.id, purpose=PURPOSE)
    assert view.sections["allergies"] == [
        {"substance": "Penicillin", "reaction": None, "severity": "severe"}
    ]  # the removed allergy is not shown
    assert [m["name"] for m in view.sections["medications"]] == ["Salbutamol"]
    assert view.sections["basics"] is None and view.sections["conditions"] is None


async def test_ac15_clinic_grant_via_active_affiliation_and_union(
    db, record, clinic, clinic_doctor
):
    _, patient = record
    clinic_grant = await grant_row(
        db, patient.id, grantee_type="clinic", grantee_id=clinic.id, sections=["basics"]
    )
    actor = as_actor(clinic_doctor.user_id, "doctor")
    access = await gateway.allowed_sections(db, actor, patient.id, purpose=PURPOSE)
    assert access.sections == {"basics"}

    own_grant = await grant_row(
        db,
        patient.id,
        grantee_type="doctor",
        grantee_id=clinic_doctor.id,
        sections=["basics", "conditions"],
    )
    access = await gateway.allowed_sections(db, actor, patient.id, purpose=PURPOSE)
    assert access.sections == {"basics", "conditions"}
    # The doctor's own grant wins the attribution where both allow a section.
    assert access.consent_for == {"basics": own_grant.id, "conditions": own_grant.id}
    assert clinic_grant.id not in access.consent_for.values()


async def test_ac15_ended_affiliation_denied(db, record, clinic, clinic_doctor):
    _, patient = record
    await grant_row(db, patient.id, grantee_type="clinic", grantee_id=clinic.id)
    aff = (
        await db.execute(
            select(DoctorAffiliation).where(DoctorAffiliation.doctor_id == clinic_doctor.id)
        )
    ).scalar_one()
    aff.status = "ended"
    await db.commit()
    with pytest.raises(ConsentDenied):
        await gateway.allowed_sections(
            db, as_actor(clinic_doctor.user_id, "doctor"), patient.id, purpose=PURPOSE
        )


@pytest.mark.parametrize(
    "case",
    ["unverified", "admin", "other_patient", "revoked", "expired", "empty", "missing", "no_grant"],
)
async def test_ac16_denied_cases_raise_consent_denied(
    db, record, verified_doctor, unverified_doctor, clinic, admin, second_patient, case
):
    _, patient = record
    target = patient.id
    actor = as_actor(verified_doctor.user_id, "doctor")
    if case == "unverified":
        await grant_row(db, patient.id, grantee_type="doctor", grantee_id=unverified_doctor.id)
        actor = as_actor(unverified_doctor.user_id, "doctor")
    elif case == "admin":
        await grant_row(db, patient.id, grantee_type="clinic", grantee_id=clinic.id)
        actor = as_actor(admin.id, "admin")
    elif case == "other_patient":
        actor = as_actor(second_patient.id, "patient")
    elif case == "revoked":
        await grant_row(
            db,
            patient.id,
            grantee_type="doctor",
            grantee_id=verified_doctor.id,
            revoked_at=utcnow(),
        )
    elif case == "expired":
        await grant_row(
            db,
            patient.id,
            grantee_type="doctor",
            grantee_id=verified_doctor.id,
            expires_at=utcnow() - timedelta(seconds=1),
        )
    elif case == "empty":
        await grant_row(
            db, patient.id, grantee_type="doctor", grantee_id=verified_doctor.id, sections=[]
        )
    elif case == "missing":
        target = uuid7()
    with pytest.raises(ConsentDenied) as exc:
        await gateway.read_sections(db, actor, target, purpose=PURPOSE)
    assert isinstance(exc.value, NotFound) and exc.value.code == "NOT_FOUND"
    assert await access_rows(db) == []


async def test_ac17_unshared_sections_are_never_queried(db, record, verified_doctor, monkeypatch):
    _, patient = record
    await grant_row(
        db, patient.id, grantee_type="doctor", grantee_id=verified_doctor.id, sections=["basics"]
    )
    called: list[str] = []
    for key, reader in list(gateway._READERS.items()):

        async def spy(session, p, _key=key, _reader=reader):
            called.append(_key)
            return await _reader(session, p)

        monkeypatch.setitem(gateway._READERS, key, spy)
    view = await gateway.read_sections(
        db, as_actor(verified_doctor.user_id, "doctor"), patient.id, purpose=PURPOSE
    )
    assert called == ["basics"]
    assert view.sections["basics"]["blood_group"] == "O+"


async def test_ac17_section_outside_access_is_refused(db, record, verified_doctor):
    _, patient = record
    await grant_row(
        db, patient.id, grantee_type="doctor", grantee_id=verified_doctor.id, sections=["basics"]
    )
    actor = as_actor(verified_doctor.user_id, "doctor")
    access = await gateway.allowed_sections(db, actor, patient.id, purpose=PURPOSE)
    with pytest.raises(ConsentDenied):
        await gateway.read_section(db, actor, access, "allergies", purpose=PURPOSE)


async def test_ac18_one_access_log_row_per_section_read(db, record, clinic, clinic_doctor):
    _, patient = record
    grant = await grant_row(
        db,
        patient.id,
        grantee_type="clinic",
        grantee_id=clinic.id,
        sections=["allergies", "visits"],
    )
    await gateway.read_sections(
        db, as_actor(clinic_doctor.user_id, "doctor"), patient.id, purpose=PURPOSE
    )
    rows = await access_rows(db)
    assert sorted(r.resource_type for r in rows) == ["section.allergies", "section.visits"]
    for r in rows:
        assert r.action == "read" and r.is_break_glass is False
        assert r.user_id == clinic_doctor.user_id
        assert r.patient_id == patient.id and r.resource_id == patient.id
        assert r.consent_id == grant.id
        assert r.ip_address == "203.0.113.7"


async def test_ac19_log_failure_returns_no_data(db, record, verified_doctor, monkeypatch):
    _, patient = record
    await grant_row(
        db, patient.id, grantee_type="doctor", grantee_id=verified_doctor.id, sections=["basics"]
    )
    # An action outside the CHECK constraint makes the real INSERT fail.
    monkeypatch.setattr(gateway, "ACTION_READ", "peek")
    with pytest.raises(IntegrityError):
        await gateway.read_sections(
            db, as_actor(verified_doctor.user_id, "doctor"), patient.id, purpose=PURPOSE
        )
    assert await access_rows(db) == []


@pytest.mark.parametrize("purpose", ["", "   "])
async def test_ac20_purpose_required(db, record, purpose):
    user, patient = record
    with pytest.raises(ValueError):
        await gateway.allowed_sections(
            db, as_actor(user.id, "patient"), patient.id, purpose=purpose
        )


async def test_ac23_lookup_and_dashboard_honour_clinic_grants(db, record, clinic, clinic_doctor):
    _, patient = record
    actor = as_actor(clinic_doctor.user_id, "doctor")
    with pytest.raises(NotFound):
        await consent_service.patient_for_doctor(db, actor, patient.id)

    await grant_row(db, patient.id, grantee_type="clinic", grantee_id=clinic.id)
    await grant_row(db, patient.id, grantee_type="doctor", grantee_id=clinic_doctor.id)
    assert (await consent_service.patient_for_doctor(db, actor, patient.id)).id == patient.id
    found = await consent_service.find_patient_for_doctor(db, actor, patient.passport_no)
    assert found.id == patient.id
    listed = await consent_service.list_patients_for_doctor(db, actor)
    assert [p.id for p in listed] == [patient.id]  # two grants, one row


# ── S5: one door (AC-21) ─────────────────────────────────────────────────
APP = ROOT / "backend" / "app"
PROFILE_TABLE_MODELS = {"Allergy", "ChronicCondition", "PatientMedication"}
# The only modules allowed to touch the profile-table models: their mapping,
# the owner's own service (always scoped to the caller), and the gateway.
ALLOWED = {
    APP / "db" / "models.py",
    APP / "profile" / "service.py",
    APP / "consent" / "gateway.py",
}


def profile_model_uses(source: str) -> set[str]:
    """Every exact name of a profile-table model a module imports or uses."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and node.id in PROFILE_TABLE_MODELS:
            found.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in PROFILE_TABLE_MODELS:
            found.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            found.update(a.name for a in node.names if a.name in PROFILE_TABLE_MODELS)
    return found


def test_ac21_scanner_catches_a_direct_read():
    leaky = "from app.db.models import Allergy\nselect(models.ChronicCondition)\n"
    assert profile_model_uses(leaky) == {"Allergy", "ChronicCondition"}
    assert profile_model_uses("x = AllergyCreate") == set()  # schema names are fine


def test_ac21_no_direct_profile_table_reads_outside_gateway():
    offenders = {
        str(path.relative_to(ROOT)): sorted(uses)
        for path in APP.rglob("*.py")
        if path not in ALLOWED and (uses := profile_model_uses(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}, (
        "Read another patient's profile sections through consent/gateway.py "
        f"(P9 BL-12), not directly: {offenders}"
    )


# ── S7: access log (AC-22) ───────────────────────────────────────────────
LOG_URL = "/api/v1/me/access-log"


async def log_row(db, patient_id, *, user_id, section="basics", consent_id=None, at=None):
    row = AccessLog(
        user_id=user_id,
        patient_id=patient_id,
        consent_id=consent_id,
        resource_type=f"section.{section}",
        resource_id=patient_id,
        action="read",
        occurred_at=at or utcnow(),
    )
    db.add(row)
    await db.commit()
    return row


async def test_ac22_access_log_lists_reads_with_names_and_clinic(
    client, db, patient_user, clinic, clinic_doctor
):
    me = await patient_of(db, patient_user)
    await grant_row(
        db,
        me.id,
        grantee_type="clinic",
        grantee_id=clinic.id,
        sections=["allergies", "basics"],
    )
    await gateway.read_sections(
        db, as_actor(clinic_doctor.user_id, "doctor"), me.id, purpose=PURPOSE
    )
    r = await client.get(LOG_URL)
    assert r.status_code == 200
    body = r.json()
    assert body["next_cursor"] is None
    assert sorted(i["section"] for i in body["items"]) == ["allergies", "basics"]
    for item in body["items"]:
        assert item["accessor_name"] == "Dr Bilal Ahmed"
        assert item["accessor_specialty"] == "Dermatology"
        assert item["clinic_name"] == clinic.name


async def test_ac22_own_rows_only_newest_first_paged(
    client, db, patient_user, second_patient, verified_doctor
):
    me = await patient_of(db, patient_user)
    other = await patient_of(db, second_patient)
    base = utcnow() - timedelta(hours=1)
    for i in range(5):
        await log_row(db, me.id, user_id=verified_doctor.user_id, at=base + timedelta(minutes=i))
    await log_row(db, other.id, user_id=verified_doctor.user_id)  # someone else's record
    await log_row(db, me.id, user_id=patient_user.id)  # the patient's own read

    first = (await client.get(LOG_URL, params={"limit": 2})).json()
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = (await client.get(LOG_URL, params={"limit": 2, "cursor": first["next_cursor"]})).json()
    third = (await client.get(LOG_URL, params={"limit": 2, "cursor": second["next_cursor"]})).json()
    assert third["next_cursor"] is None
    times = [i["occurred_at"] for p in (first, second, third) for i in p["items"]]
    assert len(times) == 5 and times == sorted(times, reverse=True)
    assert all(i["accessor_name"] == "Dr Ayesha Khan" for i in first["items"])
    assert all(i["clinic_name"] is None for i in first["items"])  # a doctor grant


@pytest.mark.parametrize("cursor", ["!!!", "bm90LWEtY3Vyc29y"])
async def test_ac22_bad_cursor_422(client, db, patient_user, cursor):
    r = await client.get(LOG_URL, params={"cursor": cursor})
    assert r.status_code == 422
    assert r.json()["error"]["details"]["fields"]["cursor"] == "errors.cursor_invalid"


async def test_ac22_limit_bounds(client, db, patient_user):
    assert (await client.get(LOG_URL, params={"limit": 0})).status_code == 422
    assert (await client.get(LOG_URL, params={"limit": 101})).status_code == 422


@pytest.mark.parametrize("email", ["dr@x.com", "adm@x.com"])
async def test_ac25_access_log_is_patient_only(client, db, verified_doctor, admin, email):
    assert (await client.get(LOG_URL)).status_code == 401
    await login(client, email)
    assert (await client.get(LOG_URL)).status_code == 403
