"""Medical timeline — database round trips (P3).

Every query is a network round trip, about a second on a far-away pooler (see CLAUDE.md,
"Reaching the database"), so how many statements a timeline load issues is part of its contract:
it is pinned here, and it must not grow with the amount of history. The data is the synthetic story
the other timeline tests use, plus a lot more saved documents.
"""

from __future__ import annotations

import uuid

import pytest
from app.deps import Actor
from app.timeline import events as ev
from app.timeline import service
from sqlalchemy import event

from tests.timeline_support import (
    HBA1C,
    METFORMIN,
    add_document,
    create_admin,
    create_doctor,
    create_patient,
    create_story,
    grant,
)

# What one load costs today (statements). A change that raises these should say why.
PATIENT_LOAD = (
    9  # documents+job, their cells, visits, prescriptions, lab link, 3 profile tables, pending
)
DOCTOR_LOAD = (
    17  # the same, minus the patient-only reads, plus the gateway's access checks and log rows
)
PATIENT_DETAIL = 7


def actor(user_id, role, *, verified=False, clinics=()):
    return Actor(
        user_id=user_id,
        role=role,
        clinic_ids=frozenset(clinics),
        is_verified_doctor=verified,
        locale="en",
        request_id="test",
        ip_address="127.0.0.1",
        onboarding_complete=True,
    )


@pytest.fixture
def counter(engine):
    """How many SQL statements run while it is open (transaction control is not counted)."""
    seen: list[str] = []

    def count(conn, cursor, statement, parameters, context, executemany):
        if not statement.lstrip().upper().startswith(("BEGIN", "COMMIT", "ROLLBACK", "PRAGMA")):
            seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", count)
    yield seen
    event.remove(engine.sync_engine, "before_cursor_execute", count)


async def build(db, clinic, extra_documents: int):
    admin = await create_admin(db, clinic)
    doctor = await create_doctor(db, clinic, admin)
    patient = await create_patient(db)
    story = await create_story(db, clinic, doctor, patient)
    for _ in range(extra_documents):
        await add_document(db, patient, doc_type="lab", uploaded_by=patient.user_id, rows=(HBA1C,))
        await add_document(
            db, patient, doc_type="rx", uploaded_by=patient.user_id, rows=(METFORMIN,)
        )
    await grant(
        db,
        patient,
        grantee_type="doctor",
        grantee_id=doctor.id,
        sections=[
            "visits",
            "prescriptions",
            "lab_reports",
            "allergies",
            "conditions",
            "medications",
        ],
    )
    return doctor, patient, story


async def statements(db, counter, who, patient_id) -> tuple[int, int]:
    counter.clear()
    out = await service.timeline(db, who, patient_id, ev.Filters(), loc="en")
    return len(counter), out.total


@pytest.mark.parametrize("extra_documents", [0, 5, 40])
async def test_a_timeline_load_costs_a_fixed_number_of_queries_however_much_history(
    db, clinic, counter, extra_documents
):
    doctor, patient, _ = await build(db, clinic, extra_documents)
    me = actor(patient.user_id, "patient")
    dr = actor(doctor.user_id, "doctor", verified=True, clinics=[clinic.id])
    mine, mine_events = await statements(db, counter, me, patient.id)
    theirs, their_events = await statements(db, counter, dr, patient.id)
    assert mine_events >= 8 + 2 * extra_documents and their_events >= 5 + 2 * extra_documents
    assert mine <= PATIENT_LOAD, f"a patient's timeline now costs {mine} statements"
    assert theirs <= DOCTOR_LOAD, f"a doctor's timeline now costs {theirs} statements"


async def test_filters_and_search_cost_nothing_extra(db, clinic, counter):
    _, patient, _ = await build(db, clinic, 5)
    me = actor(patient.user_id, "patient")
    plain, _ = await statements(db, counter, me, patient.id)
    counter.clear()
    await service.timeline(
        db, me, patient.id, ev.Filters(query="hba1c", category="lab"), loc="en", limit=5, offset=5
    )
    assert len(counter) == plain  # they run on what is already loaded, after the consent gateway


async def test_one_events_detail_page_stays_within_its_budget(db, clinic, counter):
    _, patient, story = await build(db, clinic, 10)
    me = actor(patient.user_id, "patient")
    counter.clear()
    await service.event_detail(db, me, patient.id, "encounter", story.visit.id, loc="en")
    assert len(counter) <= PATIENT_DETAIL


async def test_the_shared_read_is_dropped_so_the_next_load_sees_new_documents(db, clinic):
    """The documents are read once PER LOAD, never kept: a document saved between two loads on the
    same session is on the second one, and nothing is left behind on the session."""
    _, patient, _ = await build(db, clinic, 0)
    me = actor(patient.user_id, "patient")
    first = await service.timeline(db, me, patient.id, ev.Filters(), loc="en")
    assert not any(str(key).startswith("p3_") for key in db.info)
    added = await add_document(
        db, patient, doc_type="lab", uploaded_by=patient.user_id, rows=(HBA1C,)
    )
    second = await service.timeline(db, me, patient.id, ev.Filters(), loc="en")
    assert second.total == first.total + 1
    assert f"lab_document:{added.id}" in [i.ref for i in second.items]


async def test_a_failed_load_leaves_nothing_behind(db, clinic):
    _, patient, _ = await build(db, clinic, 0)
    me = actor(patient.user_id, "patient")
    with pytest.raises(Exception):  # noqa: B017 - any failure; the point is the cleanup
        await service.timeline(db, me, uuid.uuid4(), ev.Filters(), loc="en")
    assert not any(str(key).startswith("p3_") for key in db.info)
