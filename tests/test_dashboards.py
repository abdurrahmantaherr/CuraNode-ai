# ruff: noqa: F811 -- fixtures imported from the document suite are then used as test arguments
"""Patient and doctor home pages (Priority 2): real data, one next step, honest gaps.

`web/dashboard.py` holds the pure choosers (tested directly, no database); the pages are tested
through the real routes. Nothing here is a fixed example: every count and row asserted below is
one the test itself created.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.booking import service as booking_service
from app.booking.schemas import AppointmentRow, DayRow
from app.db import types as dbtypes
from app.db.models import Document, Patient, UserRole
from app.documents.schemas import DocumentOut
from app.i18n.catalogue import missing_keys, translate
from app.queueing.schemas import Line, SessionView
from app.web import dashboard
from sqlalchemy import event, select, update
from sqlalchemy.exc import OperationalError

from tests.booking_helpers import (
    MONDAY,
    TODAY,
    add_appointment,
    add_session,
    at,
    freeze,
    make_patient,
    verified_doctor,
)
from tests.booking_helpers import login as booking_login
from tests.conftest import make_user
from tests.test_documents import (  # noqa: F401
    fake_ocr,
    no_real_gemini,
    patient_user,
    upload,
)

PKT = dbtypes.PKT


# ── Builders for the pure tests ──────────────────────────────────────────
def doc(status: str, name: str = "scan.png") -> DocumentOut:
    return DocumentOut(
        id=uuid.uuid4(),
        document_type="prescription",
        status=status,
        failure_code=None,
        original_filename=name,
        mime_type="image/png",
        size_bytes=10,
        uploaded_at=datetime(2026, 10, 12, 8, 0, tzinfo=UTC),
        document_date=None,
        finalized_at=None,
    )


def appt(minutes_ahead: int = 60, name: str = "Dr Booking") -> AppointmentRow:
    start = datetime(2026, 10, 12, 13, 0, tzinfo=PKT) + timedelta(minutes=minutes_ahead)
    return AppointmentRow(
        id=uuid.uuid4(),
        doctor_id=uuid.uuid4(),
        doctor_name=name,
        specialty="Cardiology",
        clinic_id=uuid.uuid4(),
        clinic_name="Shifa",
        starts_at=start,
        status="booked",
        upcoming=True,
        can_change=True,
    )


def step(documents, appointments=(), saved=0):
    return dashboard.patient_next_step(
        loc="en", documents=documents, appointments=appointments, saved_count=saved
    )


# ── Patient: which step comes first ──────────────────────────────────────
def test_a_document_only_the_patient_can_move_comes_before_everything_else():
    docs = [
        doc("processing", "a"),
        doc("failed", "b"),
        doc("awaiting_review", "c"),
        doc("uploaded", "d"),
    ]
    first = step(docs, [appt()])
    assert (first.kind, first.name) == ("doc_awaiting_review", "c")
    assert step([d for d in docs if d.status != "awaiting_review"]).kind == "doc_failed"
    assert step([doc("uploaded"), doc("processing")]).kind == "doc_uploaded"
    assert step([doc("processing")]).kind == "doc_processing"


def test_the_step_counts_the_documents_of_that_kind_and_links_to_the_first():
    docs = [doc("awaiting_review", "one"), doc("awaiting_review", "two")]
    s = step(docs)
    assert s.count == 2 and s.href == f"/en/patient/documents/{docs[0].id}"


def test_with_nothing_unfinished_the_next_appointment_is_the_step():
    row = appt(90)
    s = step([], [row])
    assert (s.kind, s.name, s.href) == (
        "appointment",
        "Dr Booking",
        f"/en/patient/appointments/{row.id}",
    )
    assert s.when == "2026-10-12 14:30"


def test_a_brand_new_patient_is_told_how_to_begin():
    assert step([], [], saved=0).kind == "first_use"
    assert step([], [], saved=4).kind == "all_clear"  # has records, nothing to do


def test_when_part_of_the_picture_is_missing_no_step_is_guessed():
    # documents unavailable, appointments fine and empty: cannot say "first use"
    assert (
        dashboard.patient_next_step(loc="en", documents=None, appointments=[], saved_count=0)
        is None
    )
    assert (
        dashboard.patient_next_step(loc="en", documents=[], appointments=None, saved_count=0)
        is None
    )
    assert (
        dashboard.patient_next_step(loc="en", documents=[], appointments=[], saved_count=None)
        is None
    )
    # ...but what IS known still counts
    assert (
        dashboard.patient_next_step(
            loc="en", documents=[doc("failed")], appointments=None, saved_count=None
        ).kind
        == "doc_failed"
    )


# ── Doctor: which task comes first ───────────────────────────────────────
NOW = datetime(2026, 10, 12, 13, 0, tzinfo=PKT)


def day_row(minutes: int, status: str = "booked", name: str = "Pat") -> DayRow:
    return DayRow(starts_at=NOW + timedelta(minutes=minutes), patient_name=name, status=status)


def view(*waiting: str) -> SessionView:
    lines = tuple(
        Line(uuid.uuid4(), uuid.uuid4(), n, "waiting", i + 1, NOW, False)
        for i, n in enumerate(waiting)
    )
    return SessionView(
        key=None, doctor_name="Dr", clinic_name="C", day=NOW.date(), start=NOW.time(), end=NOW.time(),
        slot_minutes=15, cap=10, used=len(lines), in_room=None, waiting=lines, expected=(), seen=(),
    )  # fmt: skip


def summary(day=(), queue=(), patients=0):
    return dashboard.doctor_next_step(
        loc="en", day=day, queue=queue, patient_count=patients, now=NOW
    )


def test_patients_waiting_come_first_then_the_next_appointment_ahead():
    s = summary([day_row(30)], [view("Ann", "Bob")], patients=3)
    assert (s.step.kind, s.step.name, s.step.count) == ("waiting", "Ann", 2)
    assert (s.waiting, s.appointments_today, s.patients) == (2, 1, 3)

    s = summary(
        [
            day_row(-30, "done", "Early"),
            day_row(45, "booked", "Soon"),
            day_row(90, "booked", "Later"),
        ]
    )
    assert (s.step.kind, s.step.name, s.step.when, s.step.count) == (
        "next_appointment",
        "Soon",
        "13:45",
        2,
    )


def test_cancelled_and_past_appointments_are_never_the_next_one():
    s = summary([day_row(30, "cancelled", "Gone"), day_row(-20, "booked", "Missed")])
    assert (
        s.step.kind == "day_done" and s.appointments_today == 1
    )  # the cancelled one is not counted
    assert summary([day_row(30, "cancelled")]).step.kind == "idle"


def test_a_quiet_day_points_to_the_schedule():
    s = summary()
    assert (s.step.kind, s.step.href, s.appointments_today, s.waiting) == (
        "idle",
        "/en/doctor/schedule",
        0,
        0,
    )


# ── The section guard ────────────────────────────────────────────────────
class FakeSession:
    def __init__(self):
        self.rolled_back = 0

    async def rollback(self):
        self.rolled_back += 1


async def test_an_unreachable_database_blanks_one_block_and_rolls_back():
    session = FakeSession()

    async def boom():
        raise OperationalError("select 1", {}, Exception("connection reset"))

    assert await dashboard.section(session, "x", boom) == (None, False)
    assert session.rolled_back == 1

    async def fine():
        return [1]

    assert await dashboard.section(session, "x", fine) == ([1], True)


async def test_a_real_bug_is_not_swallowed():
    async def bug():
        raise ValueError("a bug, not an outage")

    with pytest.raises(ValueError):
        await dashboard.section(FakeSession(), "x", bug)


# ── Patient page ─────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    freeze(monkeypatch)


async def patient_row(db, user) -> Patient:
    return (await db.execute(select(Patient).where(Patient.user_id == user.id))).scalar_one()


async def test_a_first_time_patient_is_welcomed_and_nothing_is_made_up(client, patient_user):
    page = (await client.get("/en/patient")).text
    assert "Welcome. Start by uploading" in page
    assert "Upload your first document" in page and 'href="/en/patient/documents/new"' in page
    assert "You have no upcoming appointments." in page
    assert "You haven&#39;t uploaded any documents yet." in page
    assert "This area arrives with a later feature" not in page
    assert "data-doc-list-poll" not in page  # nothing is being read: nothing to follow
    assert 'class="entry-row"' not in page and 'class="grant-card' not in page


async def test_an_unfinished_document_shows_its_real_status_and_what_it_waits_for(
    client, db, fake_ocr, patient_user
):
    up = (await upload(client, name="rx-june.png")).json()
    page = (await client.get("/en/patient")).text
    assert "rx-june.png" in page and "Uploaded, waiting to be read" in page
    assert f'href="/en/patient/documents/{up["id"]}"' in page
    assert "is uploaded but has not been read yet" in page  # the step

    await db.execute(
        update(Document).where(Document.id == uuid.UUID(up["id"])).values(status="processing")
    )
    await db.commit()
    page = (await client.get("/en/patient")).text
    assert "Being read. This updates by itself." in page
    # While one is being read the page follows it in place (documents.js, no reload).
    assert 'data-doc-list-poll="/api/v1/documents/in-progress?limit=100"' in page
    assert 'data-doc-status="processing"' in page and 'id="doc-live"' in page
    assert "/static/js/doc-status.js" in page and "/static/js/documents.js" in page

    await db.execute(
        update(Document).where(Document.id == uuid.UUID(up["id"])).values(status="awaiting_review")
    )
    await db.commit()
    page = (await client.get("/en/patient")).text
    assert "Needs your review before it is saved" in page and "Review document" in page
    assert "data-doc-list-poll" not in page  # nothing is being read any more


async def test_only_the_patients_own_documents_and_appointments_are_listed(
    client, db, clinic, fake_ocr, patient_user
):
    await upload(client, name="mine.png")
    other_user, other, _ = await make_patient(db, email="someone.else@x.com", name="Someone Else")
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic, weekday=MONDAY, start="09:00", end="18:00")
    await add_appointment(db, other, doctor, clinic, session, at(TODAY, "16:00"))
    foreign_doc = Document(
        id=uuid.uuid4(), patient_id=other.id, doc_type="rx", status="awaiting_review",
        original_filename="theirs.png", object_key="k", mime_type="image/png", size_bytes=1,
        uploaded_by=other_user.id,
    )  # fmt: skip
    db.add(foreign_doc)
    await db.commit()

    page = (await client.get("/en/patient")).text
    assert "mine.png" in page and "theirs.png" not in page
    assert "Dr Booking" not in page and "You have no upcoming appointments." in page


async def test_upcoming_appointments_are_real_ordered_and_capped(client, db, clinic, patient_user):
    patient = await patient_row(db, patient_user)
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic, weekday=MONDAY, start="09:00", end="22:00")
    for hhmm, status in [
        ("20:00", "booked"), ("15:00", "booked"), ("18:00", "booked"), ("19:00", "booked"),
        ("14:30", "cancelled"), ("11:00", "booked"),
    ]:  # fmt: skip
        await add_appointment(db, patient, doctor, clinic, session, at(TODAY, hhmm), status)

    page = (await client.get("/en/patient")).text
    times = re.findall(r'<div class="mono" dir="ltr">(2026-10-12 \d\d:\d\d)</div>', page)
    # 11:00 already started, 14:30 is cancelled: neither is upcoming. Three shown, soonest first.
    assert times == ["2026-10-12 15:00", "2026-10-12 18:00", "2026-10-12 19:00"]
    assert "You have more upcoming appointments." in page  # the 20:00 one
    assert "Your next appointment is with Dr Booking on 2026-10-12 15:00." in page
    assert 'href="/en/patient/appointments"' in page


async def test_a_block_that_cannot_load_says_so_and_the_rest_still_shows(
    client, db, fake_ocr, patient_user, monkeypatch
):
    await upload(client, name="still-here.png")

    async def down(*a, **k):
        raise OperationalError("select", {}, Exception("connection reset"))

    monkeypatch.setattr(booking_service, "next_appointments", down)
    r = await client.get("/en/patient")
    page = r.text
    assert r.status_code == 200
    assert "We couldn&#39;t load your appointments right now." in page
    assert "You have no upcoming appointments." not in page  # no empty state it cannot vouch for
    assert "still-here.png" in page  # the documents block is unaffected


async def test_when_nothing_can_be_loaded_no_first_use_claim_is_made(
    client, patient_user, monkeypatch
):
    async def down(*a, **k):
        raise OperationalError("select", {}, Exception("connection reset"))

    for name in ("next_appointments",):
        monkeypatch.setattr(booking_service, name, down)
    from app.documents import service as doc_service

    monkeypatch.setattr(doc_service, "list_in_progress", down)
    monkeypatch.setattr(doc_service, "count_finalized", down)
    page = (await client.get("/en/patient")).text
    assert "Welcome. Start by uploading" not in page
    assert page.count("banner--warn") >= 2 and "Try again" in page


async def test_the_patient_page_renders_in_urdu_right_to_left(client, patient_user):
    page = (await client.get("/ur/patient")).text
    assert 'dir="rtl"' in page
    assert translate("dashboard.next.first_use", "ur") in page
    assert translate("dashboard.appointments.title", "ur") in page


async def test_the_patient_home_costs_a_bounded_number_of_statements(client, patient_user, db):
    sync = db.sync_session.get_bind()
    seen: list[str] = []

    def spy(conn, cursor, statement, *a):
        seen.append(statement)

    await client.get("/en/patient")  # warm: first request also loads the actor
    event.listen(sync, "before_cursor_execute", spy)
    try:
        await client.get("/en/patient")
    finally:
        event.remove(sync, "before_cursor_execute", spy)
    # sign-in checks + profile + 3 dashboard blocks (appointments, documents, saved count).
    assert len(seen) <= 10, len(seen)
    assert sum("FROM appointment" in s for s in seen) == 1  # the history is not loaded


# ── Doctor page ──────────────────────────────────────────────────────────
@pytest.fixture
async def doctor_page(client, db, clinic):
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic, weekday=MONDAY, start="09:00", end="18:00")
    _, pat, _ = await make_patient(db)
    await booking_login(client, "booking.dr@x.com")
    return doctor, session, pat


async def test_a_verified_doctor_sees_the_next_appointment_and_real_counts(
    client, db, clinic, doctor_page
):
    doctor, session, pat = doctor_page
    await add_appointment(db, pat, doctor, clinic, session, at(TODAY, "15:00"))
    _, other, _ = await make_patient(db, email="second@x.com", name="Second Patient")
    await add_appointment(db, other, doctor, clinic, session, at(TODAY, "16:00"))
    await add_appointment(db, other, doctor, clinic, session, at(TODAY, "17:00"), "cancelled")

    page = (await client.get("/en/doctor")).text
    assert "Next appointment: Pat Booking at 15:00." in page
    assert "Appointments today: 2 · Waiting: 0 · Patients who shared records with you: 0" in page
    assert 'href="/en/doctor/appointments"' in page and "See today&#39;s appointments" in page
    assert "pending review" not in page.lower()  # nothing in the system produces such a count


async def test_a_quiet_day_has_an_empty_state_and_a_useful_link(client, doctor_page):
    page = (await client.get("/en/doctor")).text
    assert "No appointments today. You can review your schedule." in page
    assert "Open my schedule" in page and "No appointments today." in page


async def test_a_doctor_block_that_cannot_load_is_named_and_the_rest_shows(
    client, db, clinic, doctor_page, monkeypatch
):
    doctor, session, pat = doctor_page
    await add_appointment(db, pat, doctor, clinic, session, at(TODAY, "15:00"))

    async def down(*a, **k):
        raise OperationalError("select", {}, Exception("connection reset"))

    from app.queueing import service as queue_service

    monkeypatch.setattr(queue_service, "doctor_queue", down)
    r = await client.get("/en/doctor")
    page = r.text
    assert r.status_code == 200
    assert "Today&#39;s queue couldn&#39;t be loaded right now." in page
    assert "Pat Booking" in page  # today's appointments are still there
    assert "Appointments today:" not in page  # no summary built from half the data
    assert translate("queue.doctor.title", "en") not in page  # the queue block is left out...
    assert translate("queue.doctor.empty", "en") not in page  # ...never shown as an empty queue


async def test_a_pending_doctor_gets_a_real_next_step_and_no_patient_data(client, db, clinic):
    await make_user(db, email="pending.dr@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    await booking_login(client, "pending.dr@x.com")
    page = (await client.get("/en/doctor")).text
    assert "While your account is being verified you can set up your working hours." in page
    assert "This area arrives with a later feature" not in page
    assert 'href="/en/doctor/schedule"' in page
    assert "passport-lookup" not in page  # FR3: no patient-data affordance
    assert 'id="today-h"' not in page  # and no summary built from data they may not see


async def test_a_patient_never_sees_the_doctor_summary_and_the_reverse(
    client, db, clinic, patient_user
):
    page = (await client.get("/en/patient")).text
    assert "Patients who shared records" not in page
    r = await client.get("/en/doctor")
    assert r.status_code == 303 and r.headers["location"] == "/en/patient"


def test_every_dashboard_message_exists_in_both_languages():
    assert missing_keys() == {"ur": []}
    for key in ("dashboard.next.doc_awaiting_review", "dashboard.doctor.counts"):
        assert translate(key, "ur") != key


# ── Added in the final verification pass ─────────────────────────────────
async def test_completed_processing_and_failed_documents_are_each_shown_as_what_they_are(
    client, db, fake_ocr, patient_user
):
    ids = {}
    for name in ("done.png", "reading.png", "broken.png", "review.png"):
        ids[name] = (await upload(client, name=name)).json()["id"]
    for name, status in (
        ("reading.png", "processing"),
        ("broken.png", "failed"),
        ("review.png", "awaiting_review"),
        ("done.png", "finalized"),
    ):
        await db.execute(
            update(Document).where(Document.id == uuid.UUID(ids[name])).values(status=status)
        )
    await db.commit()

    page = (await client.get("/en/patient")).text
    rows = dict(
        re.findall(r'data-doc-id="[^"]+" data-doc-status="(\w+)"\s+data-doc-name="([^"]+)"', page)
    )
    assert {v: k for k, v in rows.items()} == {
        "reading.png": "processing",
        "broken.png": "failed",
        "review.png": "awaiting_review",
    }
    assert "done.png" not in rows.values()  # a saved record is not "unfinished": it is only counted
    assert "Saved documents: 1" in page
    assert "Couldn&#39;t be read. Open it to try again." in page
    # The review is the patient's own: nothing here claims a doctor is reviewing anything.
    assert "doctor review" not in page.lower() and "reviewed by a doctor" not in page.lower()
    assert "Review document" in page  # awaiting_review wins over failed and processing


async def test_the_document_block_failing_leaves_the_appointments_block_working(
    client, db, clinic, patient_user, monkeypatch
):
    from app.documents import service as doc_service

    patient = await patient_row(db, patient_user)
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic, weekday=MONDAY, start="09:00", end="22:00")
    await add_appointment(db, patient, doctor, clinic, session, at(TODAY, "15:00"))

    async def down(*a, **k):
        raise OperationalError("select", {}, Exception("connection reset"))

    monkeypatch.setattr(doc_service, "list_in_progress", down)
    r = await client.get("/en/patient")
    page = r.text
    assert r.status_code == 200
    assert "We couldn&#39;t load your documents right now." in page
    assert "You haven&#39;t uploaded any documents yet." not in page  # no empty claim
    assert "Dr Booking" in page and "2026-10-12 15:00" in page  # appointments unaffected
    assert "Welcome. Start by uploading" not in page  # not "first use" when half is unknown


async def test_a_document_with_no_file_name_and_an_appointment_without_a_specialty_still_render(
    client, db, clinic, patient_user
):
    patient = await patient_row(db, patient_user)
    db.add(
        Document(
            id=uuid.uuid4(),
            patient_id=patient.id,
            doc_type="lab",
            status="failed",
            original_filename=None,
            object_key="k",
            mime_type="image/png",
            size_bytes=1,
            uploaded_by=patient_user.id,
        )
    )
    doctor = await verified_doctor(db, clinic, specialty=None)
    session = await add_session(db, doctor, clinic, weekday=MONDAY, start="09:00", end="22:00")
    await add_appointment(db, patient, doctor, clinic, session, at(TODAY, "15:00"))
    r = await client.get("/en/patient")
    assert r.status_code == 200
    assert translate("documents.detail.title", "en") in r.text  # the fallback name, not "None"
    assert "None" not in r.text.split("<main")[1]
    assert "Dr Booking" in r.text


async def test_a_patient_cannot_see_another_patients_appointment_or_use_their_ids(
    client, db, clinic, patient_user
):
    _, other, _ = await make_patient(db, email="victim@x.com", name="Victim")
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic, weekday=MONDAY, start="09:00", end="22:00")
    theirs = await add_appointment(db, other, doctor, clinic, session, at(TODAY, "15:00"))
    page = (await client.get("/en/patient")).text
    assert str(theirs.id) not in page and "You have no upcoming appointments." in page
    # ...and their appointment page is the same not-found as one that does not exist.
    mine = await client.get(f"/en/patient/appointments/{theirs.id}", follow_redirects=False)
    unknown = await client.get(f"/en/patient/appointments/{uuid.uuid4()}", follow_redirects=False)
    assert (mine.status_code, mine.headers.get("location")) == (
        unknown.status_code,
        unknown.headers.get("location"),
    )


async def test_the_doctor_summary_counts_only_this_doctors_authorized_data(
    client, db, clinic, doctor_page
):
    from tests.timeline_support import grant

    doctor, session, pat = doctor_page
    await add_appointment(db, pat, doctor, clinic, session, at(TODAY, "15:00"))
    # Another doctor's appointment today is not this doctor's.
    rival = await verified_doctor(db, clinic, email="rival.dr@x.com", name="Dr Rival")
    rival_session = await add_session(db, rival, clinic, weekday=MONDAY, start="09:00", end="18:00")
    _, other, _ = await make_patient(db, email="rival.pat@x.com", name="Rival Patient")
    await add_appointment(db, other, rival, clinic, rival_session, at(TODAY, "14:00"))
    # One patient has shared records with this doctor; another has not.
    await grant(db, pat, grantee_type="doctor", grantee_id=doctor.id, sections=["basics"])

    page = (await client.get("/en/doctor")).text
    assert "Next appointment: Pat Booking at 15:00." in page
    assert "Appointments today: 1 · Waiting: 0 · Patients who shared records with you: 1" in page
    assert "Rival Patient" not in page and "Dr Rival" not in page


async def test_the_doctor_page_offers_no_document_review_workflow(client, doctor_page):
    page = (await client.get("/en/doctor")).text.lower()
    for claim in ("pending review", "documents to review", "awaiting your review", "review queue"):
        assert claim not in page


@pytest.mark.parametrize("first", ["app.web.dashboard", "app.web.router", "app.web.unavailable"])
def test_the_web_modules_import_in_any_order(first):
    """`dashboard` needs the outage classifier from `unavailable`, which imports the router, which
    imports `dashboard`: a cycle that only worked if something else had imported them first."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    backend = Path(__file__).resolve().parent.parent / "backend"
    env = {**os.environ, "ENVIRONMENT": "test", "PYTHONPATH": str(backend)}
    run = subprocess.run(
        [sys.executable, "-c", f"import {first}"],
        capture_output=True, text=True, env=env, timeout=120, check=False,
    )  # fmt: skip
    assert run.returncode == 0, run.stderr[-1500:]
