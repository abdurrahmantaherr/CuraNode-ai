"""Medical Passport consent test suite (FR4).

`test_tN_*` covers item N of the consent test plan below, which is the
patient-side consent-management slice of `.claude/specs/medical_passport_spec.md`.
Its numbering is NOT the spec's AC numbering — the spec's doctor-side lookup
(AC-08…AC-11), passport page / QR code (AC-01, AC-14) and catalogue (AC-13)
criteria have no implementation yet and are not covered here.

     1 passport_no on the profile        8 doctor/admin → 403
     2 grant by PMDC → 201               9 duplicate active grant → 409
     3 grant by clinic name → 201       10 unknown PMDC → 404
     4 list active grants → 200         11 unverified PMDC ≡ unknown PMDC
     5 revoke → 204                     12 foreign grant id ≡ missing id
     6 repeat revoke → 204, no-op       13 malformed grant id → 422
     7 anonymous → 401                  14 11th write in a minute → 429

Runs on the usual harness: in-memory SQLite and the fake Supabase Auth
double, no network.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest
from app.db.models import AuditLog, ConsentGrant, Doctor, Patient, Profile, UserRole
from app.db.types import utcnow, uuid7
from app.settings import settings
from sqlalchemy import select

from tests.conftest import TEST_PASSWORD, make_user

URL = "/api/v1/me/consents"
ENDPOINTS = [("GET", URL), ("POST", URL), ("DELETE", f"{URL}/{uuid.uuid4()}")]
WRITES = [("POST", URL), ("DELETE", f"{URL}/{uuid.uuid4()}")]


# ── Helpers ──────────────────────────────────────────────────────────────
async def login(client, email: str) -> None:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": TEST_PASSWORD})
    assert r.status_code == 200, r.text


# The app writes through its own session, so the test session re-reads with
# `populate_existing` instead of `expire_all()` — expiring would make any
# later attribute access lazy-load outside the async greenlet.
async def patient_of(db, user: Profile) -> Patient:
    stmt = select(Patient).where(Patient.user_id == user.id)
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one()


async def doctor_of(db, user: Profile) -> Doctor:
    stmt = select(Doctor).where(Doctor.user_id == user.id)
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one()


async def fresh(db, model, row_id):
    return await db.get(model, row_id, populate_existing=True)


async def audit_rows(db, prefix: str = "consent.") -> list[AuditLog]:
    rows = await db.execute(
        select(AuditLog)
        .where(AuditLog.action.like(f"{prefix}%"))
        .order_by(AuditLog.id)
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


def body_without_request_id(r) -> dict:
    error = dict(r.json()["error"])
    error.pop("request_id")
    return error


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
async def unverified_doctor(db, clinic) -> Doctor:
    user = await make_user(db, email="pending@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    return await doctor_of(db, user)


@pytest.fixture
async def patient_user(client, db) -> Profile:
    user = await make_user(db, email="pat@x.com", role=UserRole.PATIENT)
    await login(client, "pat@x.com")
    return user


@pytest.fixture
async def second_patient(db) -> Profile:
    return await make_user(db, email="other@x.com", role=UserRole.PATIENT)


async def grant_row(
    db, patient_id, *, grantee_type, grantee_id, granted_at=None, expires_at=None, revoked_at=None
) -> ConsentGrant:
    row = ConsentGrant(
        id=uuid7(),
        patient_id=patient_id,
        grantee_type=grantee_type,
        grantee_id=grantee_id,
        scope_sections=[],
        granted_at=granted_at or utcnow(),
        expires_at=expires_at,
        revoked_at=revoked_at,
    )
    db.add(row)
    await db.commit()
    return row


# ── T1 — passport number on the profile ──────────────────────────────────
async def test_t1_profile_shows_passport_no(client, db, patient_user):
    patient = await patient_of(db, patient_user)
    r = await client.get("/api/v1/me/profile")
    assert r.status_code == 200
    assert r.json()["passport_no"] == patient.passport_no


# ── T2 — grant by PMDC number ────────────────────────────────────────────
async def test_t2_grant_by_pmdc(client, db, patient_user, verified_doctor):
    patient = await patient_of(db, patient_user)
    r = await client.post(URL, json={"pmdc_number": f"  {verified_doctor.pmdc_number} "})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["grantee_type"] == "doctor"
    assert body["grantee_id"] == str(verified_doctor.id)
    assert body["grantee_name"] == verified_doctor.full_name
    assert body["patient_id"] == str(patient.id)
    assert body["scope_sections"] == []
    assert body["expires_at"] is None and body["revoked_at"] is None

    row = await fresh(db, ConsentGrant, uuid.UUID(body["id"]))
    assert row.patient_id == patient.id and row.revoked_at is None

    [audit] = await audit_rows(db)
    assert audit.action == "consent.grant"
    assert audit.actor_user_id == patient_user.id
    assert audit.subject_patient_id == patient.id
    assert audit.resource_type == "consent_grant"
    detail = json.loads(audit.detail)
    assert detail == {
        "resource_id": body["id"],
        "grantee_type": "doctor",
        "grantee_id": str(verified_doctor.id),
    }


async def test_t2_no_grant_detail_in_logs(
    client, db, patient_user, verified_doctor, clinic, caplog
):
    import structlog.testing

    patient = await patient_of(db, patient_user)
    with structlog.testing.capture_logs() as captured:
        gid = (await client.post(URL, json={"pmdc_number": verified_doctor.pmdc_number})).json()[
            "id"
        ]
        await client.post(URL, json={"clinic_name": clinic.name})
        await client.get(URL)
        await client.delete(f"{URL}/{gid}")
        await client.post(URL, json={"pmdc_number": "ZZ-UNKNOWN-PMDC"})
    dumped = json.dumps(captured, default=str) + caplog.text
    for v in (verified_doctor.pmdc_number, clinic.name, patient.passport_no, "ZZ-UNKNOWN-PMDC"):
        assert v not in dumped
    # Nor in the audit trail — ids only.
    for row in await audit_rows(db):
        assert verified_doctor.pmdc_number not in row.detail
        assert clinic.name not in row.detail


# ── T3 — grant by clinic name ────────────────────────────────────────────
async def test_t3_grant_by_clinic_name(client, db, patient_user, clinic):
    r = await client.post(URL, json={"clinic_name": clinic.name.upper()})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["grantee_type"] == "clinic"
    assert body["grantee_id"] == str(clinic.id)
    assert body["grantee_name"] == clinic.name
    assert len(await audit_rows(db, "consent.grant")) == 1

    missing = await client.post(URL, json={"clinic_name": "No Such Clinic"})
    assert missing.status_code == 404
    assert len(await audit_rows(db)) == 1


@pytest.mark.parametrize(
    "body",
    [{}, {"pmdc_number": "x", "clinic_name": "y"}, {"pmdc_number": "   "}, {"grantee_id": "x"}],
)
async def test_t3_grant_body_needs_exactly_one_grantee(client, db, patient_user, body):
    r = await client.post(URL, json=body)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"
    assert await audit_rows(db) == []


# ── T4 — list active grants ──────────────────────────────────────────────
async def test_t4_list_active_grants(
    client, db, patient_user, second_patient, verified_doctor, clinic
):
    patient = await patient_of(db, patient_user)
    other = await patient_of(db, second_patient)
    now = utcnow()
    older = await grant_row(
        db,
        patient.id,
        grantee_type="clinic",
        grantee_id=clinic.id,
        granted_at=now - timedelta(days=2),
    )
    newer = await grant_row(
        db,
        patient.id,
        grantee_type="doctor",
        grantee_id=verified_doctor.id,
        granted_at=now - timedelta(days=1),
    )
    # Excluded: revoked, expired, and another patient's grant.
    await grant_row(
        db,
        patient.id,
        grantee_type="doctor",
        grantee_id=verified_doctor.id,
        revoked_at=now - timedelta(hours=1),
    )
    await grant_row(
        db,
        patient.id,
        grantee_type="clinic",
        grantee_id=clinic.id,
        expires_at=now - timedelta(hours=1),
    )
    await grant_row(db, other.id, grantee_type="clinic", grantee_id=clinic.id)

    r = await client.get(URL)
    assert r.status_code == 200
    body = r.json()
    assert [g["id"] for g in body] == [str(newer.id), str(older.id)]
    assert [g["grantee_name"] for g in body] == [verified_doctor.full_name, clinic.name]
    assert await audit_rows(db) == []  # reads write nothing


async def test_t4_list_empty(client, db, patient_user):
    r = await client.get(URL)
    assert r.status_code == 200 and r.json() == []


# ── T5 — revoke ──────────────────────────────────────────────────────────
async def test_t5_revoke_grant(client, db, patient_user, verified_doctor):
    gid = (await client.post(URL, json={"pmdc_number": verified_doctor.pmdc_number})).json()["id"]
    r = await client.delete(f"{URL}/{gid}")
    assert r.status_code == 204
    assert r.content == b""

    row = await fresh(db, ConsentGrant, uuid.UUID(gid))
    assert row is not None  # never hard-deleted
    assert row.revoked_at is not None
    assert (await client.get(URL)).json() == []

    grant, revoke = await audit_rows(db)
    assert grant.action == "consent.grant" and revoke.action == "consent.revoke"
    assert json.loads(revoke.detail)["resource_id"] == gid

    # Re-granting after a revoke creates a new row; it doesn't resurrect the old one.
    again = await client.post(URL, json={"pmdc_number": verified_doctor.pmdc_number})
    assert again.status_code == 201
    assert again.json()["id"] != gid
    assert (await fresh(db, ConsentGrant, uuid.UUID(gid))).revoked_at is not None


# ── T6 — repeat revoke is a no-op ────────────────────────────────────────
async def test_t6_revoke_twice_is_idempotent(client, db, patient_user, verified_doctor):
    gid = (await client.post(URL, json={"pmdc_number": verified_doctor.pmdc_number})).json()["id"]
    assert (await client.delete(f"{URL}/{gid}")).status_code == 204
    first = (await fresh(db, ConsentGrant, uuid.UUID(gid))).revoked_at

    assert (await client.delete(f"{URL}/{gid}")).status_code == 204
    assert (await fresh(db, ConsentGrant, uuid.UUID(gid))).revoked_at == first
    assert len(await audit_rows(db, "consent.revoke")) == 1


# ── T7 — anonymous ───────────────────────────────────────────────────────
async def test_t7_anonymous_gets_401(client):
    for method, url in ENDPOINTS:
        r = await client.request(method, url, json={"clinic_name": "x"})
        assert r.status_code == 401, (method, url)
        assert r.json()["error"]["code"] == "UNAUTHENTICATED"


# ── T8 — doctor / admin role ─────────────────────────────────────────────
@pytest.mark.parametrize("email", ["dr@x.com", "pending@x.com", "adm@x.com"])
async def test_t8_non_patient_gets_403(
    client, db, verified_doctor, unverified_doctor, admin, email
):
    await login(client, email)
    for method, url in ENDPOINTS:
        r = await client.request(method, url, json={"clinic_name": "x"})
        assert r.status_code == 403, (method, url)
        # A role mismatch, not the D2 case — so FORBIDDEN, never NOT_FOUND.
        assert r.json()["error"]["code"] == "FORBIDDEN"
    assert await audit_rows(db) == []


# ── T9 — one active grant per grantee ────────────────────────────────────
async def test_t9_duplicate_active_grant(client, db, patient_user, verified_doctor, clinic):
    for body in ({"pmdc_number": verified_doctor.pmdc_number}, {"clinic_name": clinic.name}):
        assert (await client.post(URL, json=body)).status_code == 201
        dup = await client.post(URL, json=body)
        assert dup.status_code == 409
        assert dup.json()["error"]["code"] == "DUPLICATE_ENTRY"

    rows = await db.execute(select(ConsentGrant).execution_options(populate_existing=True))
    assert len(rows.scalars().all()) == 2
    assert len(await audit_rows(db)) == 2


async def test_t9_expired_grant_does_not_block(client, db, patient_user, verified_doctor):
    patient = await patient_of(db, patient_user)
    await grant_row(
        db,
        patient.id,
        grantee_type="doctor",
        grantee_id=verified_doctor.id,
        expires_at=utcnow() - timedelta(minutes=1),
    )
    r = await client.post(URL, json={"pmdc_number": verified_doctor.pmdc_number})
    assert r.status_code == 201


# ── T10 — unknown PMDC ───────────────────────────────────────────────────
async def test_t10_unknown_pmdc_is_404(client, db, patient_user):
    r = await client.post(URL, json={"pmdc_number": "NO-SUCH-PMDC"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"
    assert await audit_rows(db) == []


# ── T11 — unverified PMDC ≡ unknown PMDC ─────────────────────────────────
async def test_t11_unverified_pmdc_indistinguishable(client, db, patient_user, unverified_doctor):
    unverified = await client.post(URL, json={"pmdc_number": unverified_doctor.pmdc_number})
    unknown = await client.post(URL, json={"pmdc_number": "NO-SUCH-PMDC"})
    assert unverified.status_code == unknown.status_code == 404
    assert body_without_request_id(unverified) == body_without_request_id(unknown)
    assert await audit_rows(db) == []
    rows = await db.execute(select(ConsentGrant).execution_options(populate_existing=True))
    assert rows.scalars().all() == []


# ── T12 — foreign grant id ≡ missing id ──────────────────────────────────
@pytest.mark.parametrize("revoked", [False, True])
async def test_t12_foreign_grant_indistinguishable(
    client, db, patient_user, second_patient, clinic, revoked
):
    other = await patient_of(db, second_patient)
    foreign = await grant_row(
        db,
        other.id,
        grantee_type="clinic",
        grantee_id=clinic.id,
        revoked_at=utcnow() if revoked else None,
    )
    # Re-read so `before` is in the same (SQLite-naive) form as the later read.
    before = (await fresh(db, ConsentGrant, foreign.id)).revoked_at

    r = await client.delete(f"{URL}/{foreign.id}")
    missing = await client.delete(f"{URL}/{uuid7()}")
    assert r.status_code == missing.status_code == 404
    assert body_without_request_id(r) == body_without_request_id(missing)

    assert (await fresh(db, ConsentGrant, foreign.id)).revoked_at == before
    assert await audit_rows(db) == []

    # The owner still sees their active grant.
    await login(client, "other@x.com")
    listed = [g["id"] for g in (await client.get(URL)).json()]
    assert listed == ([] if revoked else [str(foreign.id)])


# ── T13 — malformed grant id ─────────────────────────────────────────────
async def test_t13_malformed_grant_id_is_422(client, db, patient_user):
    r = await client.delete(f"{URL}/not-a-uuid")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"
    assert await audit_rows(db) == []


# ── T14 — write rate limit ───────────────────────────────────────────────
async def test_t14_write_rate_limit(client, db, patient_user, second_patient, verified_doctor):
    assert settings.consent_write_rate_limit_per_minute == 10
    gid = (await client.post(URL, json={"pmdc_number": verified_doctor.pmdc_number})).json()["id"]
    for _ in range(9):
        assert (await client.delete(f"{URL}/{gid}")).status_code == 204
    r = await client.post(URL, json={"pmdc_number": verified_doctor.pmdc_number})
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "RATE_LIMITED"
    assert r.headers["retry-after"] == "60"
    assert (await client.delete(f"{URL}/{gid}")).status_code == 429

    # Reads aren't budgeted, the profile has its own budget, and the window is per patient.
    assert (await client.get(URL)).status_code == 200
    assert (await client.patch("/api/v1/me/profile", json={"blood_group": "O+"})).status_code == 200
    await login(client, "other@x.com")
    assert (
        await client.post(URL, json={"pmdc_number": verified_doctor.pmdc_number})
    ).status_code == 201


# ── Rule 1: the consent gateway is the only reader of clinical tables ───
def test_no_direct_clinical_queries():
    import ast
    from pathlib import Path

    guarded = {"Encounter", "Diagnosis", "Prescription", "LabReport", "LabResult"}
    backend = Path(__file__).resolve().parent.parent / "backend"
    offenders = []
    for path in backend.rglob("*.py"):
        if path.name == "gateway.py" and path.parent.name == "consent":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "select"
            ):
                for sub in ast.walk(node):
                    name = sub.id if isinstance(sub, ast.Name) else getattr(sub, "attr", None)
                    if name in guarded:
                        offenders.append(f"{path}:{node.lineno} select({name})")
    assert not offenders, "clinical tables queried outside consent/gateway.py: " + "; ".join(
        offenders
    )


# ── Consent gateway (D2) ────────────────────────────────────────────────
async def _gateway_setup(db, clinic, verified_doctor):
    from app.db.models import Encounter

    user = await make_user(db, email="gwpat@x.com", role=UserRole.PATIENT)
    patient = await patient_of(db, user)
    await grant_row(db, patient.id, grantee_type="doctor", grantee_id=verified_doctor.id)
    for days in (5, 1):
        db.add(
            Encounter(
                id=uuid7(),
                patient_id=patient.id,
                doctor_id=verified_doctor.id,
                clinic_id=clinic.id,
                visit_datetime=utcnow() - timedelta(days=days),
            )
        )
    await db.commit()
    from app.deps import Actor

    return patient, Actor(user_id=verified_doctor.user_id, role="doctor", is_verified_doctor=True)


async def test_gateway_returns_record_and_one_audit_row(db, clinic, verified_doctor):
    from app.consent import gateway

    patient, actor = await _gateway_setup(db, clinic, verified_doctor)
    record = await gateway.load_patient_for_doctor(db, actor, patient.id)
    assert record.patient.id == patient.id
    dates = [e.visit_datetime for e in record.encounters]
    assert len(dates) == 2 and dates == sorted(dates, reverse=True)
    rows = await audit_rows(db, "record.")
    assert len(rows) == 1 and rows[0].action == "record.read"
    detail = json.loads(rows[0].detail)
    assert set(detail) == {"patient_id", "doctor_id", "grant_id", "purpose"}
    assert detail["doctor_id"] == str(verified_doctor.id)


async def test_gateway_denial_is_not_found_and_writes_nothing(db, clinic, verified_doctor):
    from app.consent import gateway
    from app.errors import NotFound

    _patient, actor = await _gateway_setup(db, clinic, verified_doctor)
    with pytest.raises(NotFound):
        await gateway.load_patient_for_doctor(db, actor, uuid7())
    stranger = await make_user(db, email="nogrant@x.com", role=UserRole.PATIENT)
    with pytest.raises(NotFound):
        await gateway.load_patient_for_doctor(db, actor, (await patient_of(db, stranger)).id)
    assert await audit_rows(db, "record.") == []


async def test_gateway_audit_failure_rolls_back(db, clinic, verified_doctor, monkeypatch):
    from app.audit import writer
    from app.consent import gateway

    patient, actor = await _gateway_setup(db, clinic, verified_doctor)

    async def boom(*a, **k):
        raise RuntimeError("audit down")

    monkeypatch.setattr(writer, "write", boom)
    with pytest.raises(RuntimeError):
        await gateway.load_patient_for_doctor(db, actor, patient.id)
    assert await audit_rows(db, "record.") == []
