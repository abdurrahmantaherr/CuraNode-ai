"""The doctor's patient page (D2) — database round trips and the one-pass read.

Every query is a network round trip, about a second on a far-away pooler (see CLAUDE.md,
"Reaching the database"), so what one view of the page costs is part of its contract. It used to
check the doctor's access three times and read the profile sections and the visits twice: 42
statements with everything shared, 26 with two sections. `load_doctor_patient_page` does it in
one pass — 29 and 16 — with one live access check, one `access_log` row per shared section,
each shared section read once, one `record.read` audit row and one commit.
"""

from __future__ import annotations

import pytest
from app.audit import writer
from app.consent import gateway
from app.db.models import AuditLog, Doctor, Patient, Profile
from app.deps import Actor
from app.errors import ConsentDenied
from sqlalchemy import event, select

from tests.timeline_support import (
    access_rows,
    add_visit,
    at,
    create_admin,
    create_doctor,
    create_patient,
    create_story,
    grant,
    login,
)

ALL = ["basics", "allergies", "conditions", "medications", "prescriptions", "lab_reports", "visits"]

# What one view costs today (statements, transaction control not counted). A change that raises
# these should say why. The page's own sign-in checks and the previous-read lookup come first.
FULL = 29  # every section shared
PARTIAL = 16  # visits and lab reports only


@pytest.fixture
async def admin(db, clinic) -> Profile:
    return await create_admin(db, clinic)


@pytest.fixture
async def doctor(db, clinic, admin) -> Doctor:
    return await create_doctor(db, clinic, admin)


@pytest.fixture
async def patient(db) -> Patient:
    return await create_patient(db)


@pytest.fixture
async def story(db, clinic, doctor, patient):
    return await create_story(db, clinic, doctor, patient)


@pytest.fixture
def seen(engine):
    """The SQL statements that run while it is open (transaction control is not counted)."""
    rows: list[str] = []

    def count(conn, cursor, statement, parameters, context, executemany):
        if not statement.lstrip().upper().startswith(("BEGIN", "COMMIT", "ROLLBACK", "PRAGMA")):
            rows.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", count)
    yield rows
    event.remove(engine.sync_engine, "before_cursor_execute", count)


def reading(statements: list[str], table: str) -> int:
    """How many of the statements read `table`."""
    needle = f"FROM {table}"
    return sum(1 for s in statements if needle in s)


async def view(client, db, doctor, patient, seen, sections):
    await grant(db, patient, grantee_type="doctor", grantee_id=doctor.id, sections=sections)
    await login(client, "dr@x.com")
    seen.clear()
    r = await client.get(f"/en/doctor/patient/{patient.id}")
    assert r.status_code == 200
    return r


def actor_of(user_id, role, *, clinics=()) -> Actor:
    return Actor(
        user_id=user_id,
        role=role,
        clinic_ids=frozenset(clinics),
        is_verified_doctor=role == "doctor",
        locale="en",
        request_id="test",
        ip_address="127.0.0.1",
        onboarding_complete=True,
    )


# ── What a view costs ────────────────────────────────────────────────────
@pytest.mark.parametrize("extra_visits", [0, 6])
async def test_a_view_costs_a_fixed_number_of_statements_however_much_history(
    client, db, clinic, doctor, patient, story, seen, extra_visits
):
    for n in range(extra_visits):
        await add_visit(
            db,
            patient,
            doctor,
            clinic,
            when=at(2026, 1, 1 + n),
            complaint=f"Follow-up {n}",
            diagnoses=((f"Condition {n}", None),),
        )
    r = await view(client, db, doctor, patient, seen, ALL)
    assert "Type 2 diabetes mellitus" in r.text
    assert len(seen) <= FULL, f"the doctor's patient page now costs {len(seen)} statements"


async def test_a_partial_grant_costs_less(client, db, doctor, patient, story, seen):
    await view(client, db, doctor, patient, seen, ["visits", "lab_reports"])
    assert len(seen) <= PARTIAL, f"the doctor's patient page now costs {len(seen)} statements"


async def test_access_is_checked_once_and_each_shared_section_is_read_once(
    client, db, doctor, patient, story, seen
):
    await view(client, db, doctor, patient, seen, ALL)
    assert reading(seen, "consent_grant") == 1  # the live access check
    for table in ("allergy", "chronic_condition", "patient_medication"):
        assert reading(seen, table) == 1, table  # not once for the page and again for the record


# ── What the read leaves behind ──────────────────────────────────────────
async def test_each_shared_section_is_logged_once_and_the_view_is_audited_once(
    client, db, doctor, patient, story, seen
):
    await view(client, db, doctor, patient, seen, ALL)
    logged = sorted(row.resource_type for row in await access_rows(db))
    assert logged == sorted(f"section.{key}" for key in ALL)
    audited = (await db.execute(select(AuditLog).where(AuditLog.action == "record.read"))).all()
    assert len(audited) == 1


async def test_a_section_that_was_not_shared_is_not_queried_or_logged(
    client, db, doctor, patient, story, seen
):
    await view(client, db, doctor, patient, seen, ["visits"])
    for table in ("allergy", "chronic_condition", "patient_medication", "lab_report", "document"):
        assert reading(seen, table) == 0, table
    assert [row.resource_type for row in await access_rows(db)] == ["section.visits"]


# ── It is all or nothing, and only for a doctor with a live grant ────────
async def test_a_failed_audit_write_leaves_nothing_behind(db, clinic, doctor, patient, monkeypatch):
    await grant(db, patient, grantee_type="doctor", grantee_id=doctor.id, sections=ALL)

    async def boom(*args, **kwargs):
        raise RuntimeError("audit down")

    monkeypatch.setattr(writer, "write", boom)
    me = actor_of(doctor.user_id, "doctor", clinics=[clinic.id])
    with pytest.raises(RuntimeError):
        await gateway.load_doctor_patient_page(db, me, patient.id)
    assert await access_rows(db) == []  # no section was logged without its data going out


async def test_a_doctor_without_a_grant_and_the_patient_themself_get_the_same_refusal(
    db, clinic, doctor, patient
):
    # read the ids first: a refusal rolls the session back, which expires the loaded rows
    patient_id = patient.id
    as_doctor = actor_of(doctor.user_id, "doctor", clinics=[clinic.id])
    as_patient = actor_of(patient.user_id, "patient")
    for who in (as_doctor, as_patient):
        with pytest.raises(ConsentDenied):
            await gateway.load_doctor_patient_page(db, who, patient_id)
    assert await access_rows(db) == []
