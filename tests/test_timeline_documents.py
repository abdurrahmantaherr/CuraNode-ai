"""Medical timeline — documents, filters and duplicate prevention (P3 iteration 2).

Part A drives the REAL endpoints (upload -> read -> review -> confirm -> delete) with a fake OCR
engine and the fake storage: the timeline is derived from the same rows, so nothing here is
mocked on the timeline side. Part B seeds the stable links the shared schema offers (a lab report
row, a document's visit, the prescription a document became). Parts C-F cover search, categories,
date ranges and who may see what.

All data is synthetic.
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from app.db.models import (
    Document,
    Encounter,
    LabReport,
    OcrExtraction,
    OcrJob,
    Patient,
    Prescription,
    PrescriptionItem,
    Profile,
)
from app.db.types import uuid7
from app.documents import ocr
from sqlalchemy import select, update

from tests.timeline_support import (
    HBA1C,
    METFORMIN,
    access_rows,
    add_document,
    add_prescription,
    add_visit,
    at,
    create_admin,
    create_doctor,
    create_patient,
    create_story,
    grant,
    login,
)

ME = "/api/v1/me/timeline"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

# Synthetic OCR texts, one per document type, each with a date the document itself states.
RX_TEXT = """Dr. Mehreen Iqbal
Date: 11 Aug 2024
1. Tab. Metformin 500mg  1-0-1  x 30 days
2. Cap Atorvastatin 10 mg once daily for 30 days
Allergies: Penicillin (rash)"""
LAB_TEXT = """Chughtai Lab   Report Date: 28/07/2024
HbA1c 7.2 % 4.0 - 5.6
Fasting Glucose 142 mg/dL 70-100"""
VACCINE_TEXT = """Vaccination Certificate
Patient Name: Baby Zainab
Vaccine: Pentavalent
Dose: 2
Date of Vaccination: 14-06-2026
Batch No: P9921"""
OTHER_TEXT = """City Hospital
Discharge Summary
Date of Discharge: 06-10-2026
Diagnosis: Appendicitis
Advice: Avoid heavy lifting for 4 weeks."""


# ── Harness ──────────────────────────────────────────────────────────────
class FakeOcr:
    """A controllable engine behind `ocr.get_engine` — the pipeline itself is the real one."""

    name = "tesseract"

    def __init__(self) -> None:
        self.text = RX_TEXT
        self.error: str | None = None

    async def transcribe(self, data: bytes, mime_type: str) -> tuple[str, None]:
        if self.error:
            raise ocr.OcrError(self.error)
        return self.text, None


@pytest.fixture
def fake_ocr(monkeypatch) -> FakeOcr:
    engine = FakeOcr()
    monkeypatch.setattr(ocr, "get_engine", lambda layout="page": engine)
    return engine


@pytest.fixture
async def admin(db, clinic) -> Profile:
    return await create_admin(db, clinic)


@pytest.fixture
async def doctor(db, clinic, admin):
    return await create_doctor(db, clinic, admin)


@pytest.fixture
async def patient(db) -> Patient:
    return await create_patient(db)


@pytest.fixture
async def other_patient(db) -> Patient:
    return await create_patient(db, "other@x.com")


@pytest.fixture
async def story(db, clinic, doctor, patient):
    return await create_story(db, clinic, doctor, patient)


async def upload(client, api_type: str, name: str = "scan.png") -> dict:
    r = await client.post(
        "/api/v1/documents",
        data={"document_type": api_type},
        files={"file": (name, PNG, "image/png")},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def read(client, doc_id: str) -> dict:
    r = await client.post(f"/api/v1/documents/{doc_id}/process")
    assert r.status_code == 200, r.text
    return r.json()


def confirm_body(detail: dict, **extra) -> dict:
    rows = [
        {"index": row["index"], "fields": {f["name"]: f["value"] for f in row["fields"]}}
        for row in detail["rows"]
    ]
    return {"rows": rows, **extra}


async def timeline(client, **params) -> dict:
    r = await client.get(ME, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def refs(body: dict) -> list[str]:
    return [i["ref"] for i in body["items"]]


# ═══ A. An upload reaches the timeline by itself, through the real workflow ═══
@pytest.mark.parametrize(
    ("api_type", "text", "kind", "category"),
    [
        ("prescription", RX_TEXT, "rx_document", "prescription"),
        ("lab_report", LAB_TEXT, "lab_document", "lab"),
        ("vaccination", VACCINE_TEXT, "document", "vaccination"),
        ("other", OTHER_TEXT, "document", "document"),
    ],
)
async def test_an_upload_is_pending_then_an_event_in_its_own_category_once_confirmed(
    client, db, patient, fake_ocr, api_type, text, kind, category
):
    fake_ocr.text = text
    await login(client, "pat@x.com")
    doc = await upload(client, api_type, name=f"{api_type}-scan.png")

    # 1. just uploaded: a pending item, no event
    body = await timeline(client)
    assert body["items"] == [] and body["total"] == 0
    [pending] = body["pending"]
    assert (pending["id"], pending["status"], pending["category"]) == (
        doc["id"],
        "uploaded",
        category,
    )
    assert pending["name"] == f"{api_type}-scan.png"

    # 2. read, waiting for the patient: still pending — and nothing read from it is shown
    detail = await read(client, doc["id"])
    assert detail["status"] == "awaiting_review"
    body = await timeline(client)
    assert body["items"] == [] and [p["status"] for p in body["pending"]] == ["awaiting_review"]
    assert body["pending_review"] == 1
    shown = json.dumps(body)
    for proposal in (
        "Metformin",
        "Atorvastatin",
        "Pentavalent",
        "Appendicitis",
        "HbA1c",
        "Penicillin",
    ):
        assert proposal not in shown

    # 3. confirmed: an event, in its category, dated by the document's own date
    saved = await client.post(f"/api/v1/documents/{doc['id']}/confirm", json=confirm_body(detail))
    assert saved.status_code == 200, saved.text
    body = await timeline(client)
    assert body["pending"] == [] and body["pending_review"] == 0
    [item] = body["items"]
    assert (item["kind"], item["category"]) == (kind, category)
    assert item["document_id"] == doc["id"]
    stated = saved.json()["document_date"]
    assert item["event_date"] == stated  # the date the document states...
    assert item["date_basis"] == "clinical" and item["provenance"] == "ai_patient_confirmed"
    uploaded_today = (await client.get(f"/api/v1/documents/{doc['id']}")).json()["uploaded_at"][:10]
    if stated is not None:
        assert stated != uploaded_today  # ...never the day it was uploaded
    assert body["counts"][category] == 1


async def test_a_document_that_states_no_date_stays_on_the_timeline_undated(
    client, db, patient, fake_ocr
):
    fake_ocr.text = "Metformin 500 mg twice daily for 30 days"  # no date anywhere
    await login(client, "pat@x.com")
    doc = await upload(client, "prescription")
    detail = await read(client, doc["id"])
    assert detail["document_date"] is None
    await client.post(f"/api/v1/documents/{doc['id']}/confirm", json=confirm_body(detail))
    body = await timeline(client)
    [item] = body["items"]
    assert item["event_date"] is None and item["date_basis"] == "unknown"
    assert item["uploaded_on"] is not None  # shown as the upload day, never as the clinical date
    assert body["undated"] == 1
    # a date range cannot claim it
    assert (await timeline(client, date_from="2000-01-01", date_to="2099-12-31"))["items"] == []


async def test_unconfirmed_ocr_output_never_becomes_verified_clinical_information(
    client, db, patient, fake_ocr
):
    """Reading a prescription proposes an allergy; until the patient confirms, it is nowhere —
    not on the timeline, not on their profile."""
    fake_ocr.text = RX_TEXT
    await login(client, "pat@x.com")
    doc = await upload(client, "prescription")
    detail = await read(client, doc["id"])
    assert any(g["key"] == "allergies" and g["rows"] for g in detail["groups"])  # it was proposed
    body = await timeline(client)
    assert not any(i["category"] == "profile" for i in body["items"])
    assert (await client.get("/api/v1/me/profile")).json()["allergies"] == []
    # confirmed the way the existing flow requires: only then does the profile gain it
    await client.post(
        f"/api/v1/documents/{doc['id']}/confirm",
        json={
            **confirm_body(detail),
            "groups": ["medicines", "allergies"],
            "rows": [
                {"index": r["index"], "fields": {f["name"]: f["value"] for f in r["fields"]}}
                for g in detail["groups"]
                if g["key"] in ("medicines", "allergies")
                for r in g["rows"]
            ],
        },
    )
    profile = (await client.get("/api/v1/me/profile")).json()
    assert [a["substance"] for a in profile["allergies"]] == ["Penicillin"]
    assert any(i["kind"] == "allergy" for i in (await timeline(client))["items"])


async def test_a_failed_read_is_a_pending_item_with_its_reason_and_can_be_retried(
    client, db, patient, fake_ocr
):
    fake_ocr.error = "ocr_unavailable"
    await login(client, "pat@x.com")
    doc = await upload(client, "lab_report")
    assert (await client.post(f"/api/v1/documents/{doc['id']}/process")).status_code == 200
    body = await timeline(client)
    [pending] = body["pending"]
    assert pending["status"] == "failed" and pending["message"]
    assert body["items"] == [] and body["pending_review"] == 0
    # the existing retry path still works, and then the document waits for review
    fake_ocr.error = None
    fake_ocr.text = LAB_TEXT
    assert (await read(client, doc["id"]))["status"] == "awaiting_review"
    assert [p["status"] for p in (await timeline(client))["pending"]] == ["awaiting_review"]


async def test_confirming_twice_or_reading_twice_never_duplicates_the_event(
    client, db, patient, fake_ocr
):
    await login(client, "pat@x.com")
    doc = await upload(client, "prescription")
    detail = await read(client, doc["id"])
    await read(client, doc["id"])  # a second read of the same upload changes nothing
    first = await client.post(f"/api/v1/documents/{doc['id']}/confirm", json=confirm_body(detail))
    again = await client.post(f"/api/v1/documents/{doc['id']}/confirm", json=confirm_body(detail))
    assert (first.status_code, again.status_code) == (200, 200)
    body = await timeline(client)
    assert len(body["items"]) == 1 and body["counts"]["all"] == 1 and body["pending"] == []


async def test_discarding_an_unconfirmed_upload_removes_it_everywhere(
    client, db, patient, fake_ocr
):
    await login(client, "pat@x.com")
    doc = await upload(client, "prescription")
    await read(client, doc["id"])
    assert (await client.post(f"/api/v1/documents/{doc['id']}/cancel")).status_code == 200
    body = await timeline(client)
    assert body["items"] == [] and body["pending"] == [] and body["pending_review"] == 0


async def test_an_edit_made_while_reviewing_is_what_the_timeline_shows(
    client, db, patient, fake_ocr
):
    await login(client, "pat@x.com")
    doc = await upload(client, "prescription")
    detail = await read(client, doc["id"])
    body = confirm_body(detail, document_date="2024-08-15")
    body["rows"][0]["fields"]["medicine"] = "Glucophage"  # corrected by the patient
    assert (
        await client.post(f"/api/v1/documents/{doc['id']}/confirm", json=body)
    ).status_code == 200
    [item] = (await timeline(client))["items"]
    assert item["event_date"] == "2024-08-15"
    assert "Glucophage" in item["summary"][0] and "Metformin" not in item["summary"][0]
    assert refs(await timeline(client, q="glucophage")) == [item["ref"]]  # searchable as corrected
    assert (await timeline(client, q="metformin"))["items"] == []


async def test_deleting_a_saved_document_updates_the_timeline_but_deletes_no_clinical_record(
    client, db, patient, story, fake_ocr
):
    await login(client, "pat@x.com")
    doc = await upload(client, "prescription")
    detail = await read(client, doc["id"])
    await client.post(f"/api/v1/documents/{doc['id']}/confirm", json=confirm_body(detail))
    before = await timeline(client)
    assert before["total"] == 9 and f"rx_document:{doc['id']}" in refs(before)

    gone = await client.delete(f"/api/v1/documents/{doc['id']}", params={"confirm": "true"})
    assert gone.status_code == 200, gone.text

    after = await timeline(client)
    assert refs(after) == story.order  # exactly the records that were not the document
    assert after["pending"] == []
    # no stale link: the event, its detail page and the file are gone
    assert (await client.get(f"{ME}/rx_document/{doc['id']}")).status_code == 404
    assert (await client.get(f"/api/v1/documents/{doc['id']}/file")).status_code == 404
    assert str(doc["id"]) not in json.dumps(after)
    # the separate clinical records are untouched
    assert len((await db.execute(select(Encounter))).scalars().all()) == 1
    assert len((await db.execute(select(Prescription))).scalars().all()) == 1
    tomb = (
        await db.execute(
            select(Document)
            .where(Document.id == __import__("uuid").UUID(doc["id"]))
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert tomb.status == "cancelled" and tomb.original_filename is None


# ═══ B. Duplicate prevention: the schema's own links, never look-alike names ═══
async def link(db, doc, **values):
    await db.execute(update(Document).where(Document.id == doc.id).values(**values))
    await db.commit()


async def test_a_document_a_lab_report_row_points_at_is_that_one_lab_event(
    client, db, patient, doctor, clinic
):
    visit = await add_visit(
        db, patient, doctor, clinic, when=at(2026, 6, 15, 8, 0), complaint="Labs"
    )
    doc = await add_document(
        db, patient, doc_type="lab", uploaded_by=patient.user_id, rows=(HBA1C,), document_date=None
    )
    db.add(
        LabReport(
            id=uuid7(),
            patient_id=patient.id,
            encounter_id=visit.id,
            document_id=doc.id,
            lab_name="Chughtai Lab",
            report_date=date(2026, 6, 14),
        )
    )
    await db.commit()
    await login(client, "pat@x.com")
    body = await timeline(client, type="lab")
    [lab] = body["items"]  # ONE lab event — the lab_report row did not add a second
    assert lab["ref"] == f"lab_document:{doc.id}"
    # the document stated no date: the structured report's own date is used, still never the upload day
    assert (lab["event_date"], lab["date_basis"]) == ("2026-06-14", "clinical")
    # and it knows which visit it belongs to
    assert [r["ref"] for r in lab["related"]] == [f"encounter:{visit.id}"]
    assert lab["related"][0]["relation"] == "part_of"


async def test_a_document_attached_to_a_visit_and_the_visit_point_at_each_other(
    client, db, patient, doctor, clinic, story
):
    doc = story.lab
    await link(db, doc, encounter_id=story.visit.id)
    await login(client, "pat@x.com")
    body = await timeline(client)
    assert refs(body) == story.order and body["total"] == 8  # nothing merged, nothing added
    by_ref = {i["ref"]: i for i in body["items"]}
    assert [r["ref"] for r in by_ref[f"lab_document:{doc.id}"]["related"]] == [
        f"encounter:{story.visit.id}"
    ]
    visit_related = by_ref[f"encounter:{story.visit.id}"]["related"]
    assert [(r["ref"], r["relation"]) for r in visit_related] == [
        (f"lab_document:{doc.id}", "has_attachment")
    ]
    # the detail page says so, with a link both ways
    page = (await client.get(f"/en/patient/timeline/encounter/{story.visit.id}")).text
    assert 'data-timeline-state="related"' in page
    assert f'href="/en/patient/timeline/lab_document/{doc.id}"' in page


async def test_the_link_is_only_shown_to_a_reader_who_can_see_both_records(
    client, db, doctor, patient, story
):
    await link(db, story.lab, encounter_id=story.visit.id)
    await grant(db, patient, grantee_type="doctor", grantee_id=doctor.id, sections=["lab_reports"])
    await login(client, "dr@x.com")
    r = await client.get(f"/api/v1/doctor/patients/{patient.id}/timeline")
    body = r.json()
    assert all(i["related"] == [] for i in body["items"])  # the visit was not shared: not hinted at
    assert "encounter" not in json.dumps(body)


async def test_a_document_that_became_a_prescription_is_listed_once(
    client, db, patient, doctor, clinic
):
    visit = await add_visit(db, patient, doctor, clinic, when=at(2026, 7, 28, 9, 0))
    rx = await add_prescription(
        db,
        patient,
        doctor,
        visit,
        issued=date(2026, 7, 28),
        items=(("Glucophage", "500 mg", "1 tablet", "twice daily", 30),),
        source="ocr_upload",
    )
    doc = await add_document(
        db,
        patient,
        doc_type="rx",
        uploaded_by=patient.user_id,
        rows=(METFORMIN,),
        document_date=date(2026, 7, 28),
        name="rx-from-clinic.pdf",
    )
    prescription_item = (await db.execute(select(PrescriptionItem))).scalars().first()
    job = (await db.execute(select(OcrJob).where(OcrJob.document_id == doc.id))).scalar_one()
    await db.execute(
        update(OcrExtraction)
        .where(OcrExtraction.job_id == job.id)
        .values(item_id=prescription_item.id)
    )
    await db.commit()

    await login(client, "pat@x.com")
    body = await timeline(client)
    assert refs(body) == [
        f"encounter:{visit.id}",
        f"prescription:{rx.id}",
    ]  # one prescription, no 2nd
    [prescription] = [i for i in body["items"] if i["kind"] == "prescription"]
    assert prescription["document_id"] == str(doc.id)  # the document it came from is linked
    detail = (await client.get(f"{ME}/prescription/{rx.id}")).json()
    facts = {f["label_key"]: f["value"] for b in detail["blocks"] for f in b["facts"]}
    assert facts["timeline.fact.file_name"] == "rx-from-clinic.pdf"
    assert "timeline.fact.source" in facts

    # a doctor is given the prescription but neither the document nor its name
    await grant(
        db, patient, grantee_type="doctor", grantee_id=doctor.id, sections=["prescriptions"]
    )
    await login(client, "dr@x.com")
    shared = (await client.get(f"/api/v1/doctor/patients/{patient.id}/timeline")).json()
    [seen] = shared["items"]
    assert seen["ref"] == f"prescription:{rx.id}" and seen["document_id"] is None
    assert "rx-from-clinic" not in json.dumps(shared)

    # deleting the source document leaves the prescription (a separate clinical record)
    await login(client, "pat@x.com")
    assert (
        await client.delete(f"/api/v1/documents/{doc.id}", params={"confirm": "true"})
    ).status_code == 200
    after = await timeline(client)
    assert refs(after) == [f"encounter:{visit.id}", f"prescription:{rx.id}"]
    assert after["items"][1]["document_id"] is None  # no link left to a deleted file
    assert len((await db.execute(select(Prescription))).scalars().all()) == 1


async def test_a_link_to_someone_elses_prescription_hides_nothing(
    client, db, patient, other_patient, doctor, clinic
):
    visit = await add_visit(db, other_patient, doctor, clinic, when=at(2026, 7, 28))
    foreign = await add_prescription(
        db,
        other_patient,
        doctor,
        visit,
        issued=date(2026, 7, 28),
        items=(("Glucophage", None, None, None, None),),
    )
    doc = await add_document(
        db,
        patient,
        doc_type="rx",
        uploaded_by=patient.user_id,
        rows=(METFORMIN,),
        document_date=date(2026, 8, 1),
    )
    their_item = (
        await db.execute(
            select(PrescriptionItem).where(PrescriptionItem.prescription_id == foreign.id)
        )
    ).scalar_one()
    job = (await db.execute(select(OcrJob).where(OcrJob.document_id == doc.id))).scalar_one()
    await db.execute(
        update(OcrExtraction).where(OcrExtraction.job_id == job.id).values(item_id=their_item.id)
    )
    await db.commit()
    await login(client, "pat@x.com")
    body = await timeline(client)
    assert refs(body) == [
        f"rx_document:{doc.id}"
    ]  # the document stays; the foreign record is never read


# ═══ C. Search ═══════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("glucophage", ["prescription"]),  # a medicine on a doctor's prescription
        ("metformin", ["rx_document"]),  # a medicine on a saved document
        ("diabetes", ["encounter"]),  # a diagnosis
        ("E11", ["encounter"]),  # a diagnosis code
        ("hba1c", ["lab_document", "lab_document"]),  # a lab test
        ("7.2", ["lab_document", "lab_document"]),  # a lab result
        ("sana malik", ["encounter", "prescription"]),  # a doctor's name (both words, any order)
        ("MALIK SANA", ["encounter", "prescription"]),  # ... without regard to case or order
        ("shifa", ["encounter", "prescription"]),  # a hospital
        ("endocrinology", ["encounter"]),  # consultation details
        ("three months", ["encounter"]),  # the visit notes
        ("stable on current", ["encounter"]),  # the model-written visit summary (labelled as such)
        ("peanuts", ["allergy"]),  # a profile entry
        ("hepatitis", ["document"]),  # a vaccination record
        (
            "  glucophage \t\n ",
            ["prescription"],
        ),  # whitespace and control characters are normalised
    ],
)
async def test_search_finds_what_the_record_actually_holds(
    client, db, patient, story, query, expected
):
    await login(client, "pat@x.com")
    body = await timeline(client, q=query)
    assert sorted(i["kind"] for i in body["items"]) == sorted(expected), query


async def test_search_misses_what_is_not_in_the_record_and_never_matches_headings_or_dates(
    client, db, patient, story
):
    await login(client, "pat@x.com")
    for query in ("nonexistent-medicine", "medicines", "reference range", "aug", "11 aug"):
        body = await timeline(client, q=query)
        assert body["items"] == [], query  # a heading, a column name or a date is not content
    # a search with no match says so on the page
    page = (await client.get("/en/patient/timeline", params={"q": "nonexistent-medicine"})).text
    assert 'data-timeline-state="no-match"' in page


async def test_the_owner_can_search_a_documents_file_name_but_a_doctor_cannot(
    client, db, patient, doctor, story
):
    await add_document(
        db,
        patient,
        doc_type="lab",
        uploaded_by=patient.user_id,
        rows=(HBA1C,),
        document_date=date(2026, 9, 9),
        name="blood-report-oct.pdf",
    )
    await login(client, "pat@x.com")
    [hit] = (await timeline(client, q="blood-report-oct"))["items"]
    assert hit["document_name"] == "blood-report-oct.pdf"
    await grant(db, patient, grantee_type="doctor", grantee_id=doctor.id, sections=["lab_reports"])
    await login(client, "dr@x.com")
    r = await client.get(
        f"/api/v1/doctor/patients/{patient.id}/timeline", params={"q": "blood-report-oct"}
    )
    assert r.status_code == 200 and r.json()["items"] == []  # not even a "not found" oracle
    listing = (await client.get(f"/api/v1/doctor/patients/{patient.id}/timeline")).json()
    assert "blood-report" not in json.dumps(listing) and "scan.pdf" not in json.dumps(listing)


async def test_a_search_never_reaches_a_section_the_doctor_was_not_given(
    client, db, doctor, patient, story
):
    await grant(db, patient, grantee_type="doctor", grantee_id=doctor.id, sections=["lab_reports"])
    await login(client, "dr@x.com")
    for query in ("peanuts", "glucophage", "diabetes", "asthma", "hepatitis"):
        r = await client.get(f"/api/v1/doctor/patients/{patient.id}/timeline", params={"q": query})
        assert r.status_code == 200 and r.json()["items"] == [], query
    # only the lab section was read, however much was searched
    assert {a.resource_type for a in await access_rows(db)} == {"section.lab_reports"}


async def test_search_combines_with_the_tab_and_the_date_range(client, db, patient, story):
    await login(client, "pat@x.com")
    both = await timeline(client, q="hba1c")
    assert len(both["items"]) == 2  # the dated lab and the undated one
    assert refs(await timeline(client, q="hba1c", date_from="2026-06-01")) == [
        f"lab_document:{story.lab.id}"  # a date range leaves the undated one out
    ]
    assert (await timeline(client, q="hba1c", type="prescription"))["items"] == []
    assert (await timeline(client, q="hba1c", date_from="2026-07-01"))["items"] == []
    counts = (await timeline(client, q="hba1c", type="prescription"))["counts"]
    assert (
        counts["lab"] == 2 and counts["all"] == 2
    )  # the tabs count the search, not the tab itself


async def test_search_follows_uploads_in_progress_by_name_and_tab_but_not_a_date_range(
    client, db, patient, story
):
    waiting = await add_document(
        db,
        patient,
        doc_type="lab",
        uploaded_by=patient.user_id,
        status="awaiting_review",
        rows=(HBA1C,),
        reviewed=False,
        name="chughtai-sep.pdf",
    )
    await login(client, "pat@x.com")
    assert [p["id"] for p in (await timeline(client, q="chughtai"))["pending"]] == [str(waiting.id)]
    assert [p["id"] for p in (await timeline(client, type="lab"))["pending"]] == [str(waiting.id)]
    assert (await timeline(client, type="prescription"))["pending"] == []
    assert (await timeline(client, q="hba1c"))[
        "pending"
    ] == []  # extracted values are never searched
    assert (await timeline(client, date_from="2020-01-01"))["pending"] == []  # no clinical date


async def test_the_search_text_is_validated_and_safe(client, db, patient, story):
    await login(client, "pat@x.com")
    r = await client.get(ME, params={"q": "x" * 101})
    assert r.status_code == 422 and "q" in r.json()["error"]["details"]["fields"]
    assert (await client.get(ME, params={"q": "x" * 100})).status_code == 200
    # characters that mean something to a database or a page are just characters
    for hostile in ("100%", "a_b", "'; drop table document;--", "<script>alert(1)</script>", "\\"):
        assert (await client.get(ME, params={"q": hostile})).status_code == 200
    page = (
        await client.get("/en/patient/timeline", params={"q": "<script>alert(1)</script>"})
    ).text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    # on the page, a search that is too long is reported beside the box and not applied
    long_page = (await client.get("/en/patient/timeline", params={"q": "x" * 101})).text
    assert (
        "at most 100 characters" in long_page
        and len([1 for _ in long_page.split('class="tl-card"')[1:]]) == 8
    )


# ═══ D. Categories ═══════════════════════════════════════════════════════
async def test_every_record_is_in_the_category_its_type_belongs_to(client, db, patient, story):
    await login(client, "pat@x.com")
    category_of = {i["ref"]: i["category"] for i in (await timeline(client))["items"]}
    assert category_of == {
        f"encounter:{story.visit.id}": "consultation",
        f"prescription:{story.rx.id}": "prescription",
        f"rx_document:{story.rx_doc.id}": "prescription",
        f"lab_document:{story.lab.id}": "lab",
        f"lab_document:{story.undated_lab.id}": "lab",
        f"document:{story.vaccine.id}": "vaccination",  # a vaccination record, not "other"
        f"allergy:{story.allergy.id}": "profile",  # under All only
        f"condition:{story.condition.id}": "profile",
    }
    other = await add_document(
        db,
        patient,
        doc_type="other",
        uploaded_by=patient.user_id,
        rows=(),
        document_date=date(2026, 4, 1),
    )
    assert refs(await timeline(client, type="document")) == [f"document:{other.id}"]
    assert refs(await timeline(client, type="vaccination")) == [f"document:{story.vaccine.id}"]
    assert len((await timeline(client, type="profile"))["items"]) == 2


@pytest.mark.parametrize(
    "category", ["treatment", "procedure", "treatments", "other", "xray", "ALL"]
)
async def test_a_category_that_does_not_exist_is_refused(client, db, patient, story, category):
    """There is no treatment or procedure record in the data model, so there is no such category."""
    await login(client, "pat@x.com")
    r = await client.get(ME, params={"type": category})
    assert r.status_code == 422 and "type" in r.json()["error"]["details"]["fields"]


async def test_a_doctors_timeline_has_no_vaccination_or_other_document_tabs(
    client, db, doctor, patient, story
):
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
    await login(client, "dr@x.com")
    page = (await client.get(f"/en/doctor/patient/{patient.id}/timeline")).text
    assert 'class="tl-tab"' in page
    assert page.count('class="tl-tab"') == 4  # All, Consultations, Labs, Prescriptions
    assert "Vaccinations" not in page and "Other Medical Documents" not in page
    r = await client.get(
        f"/api/v1/doctor/patients/{patient.id}/timeline", params={"type": "vaccination"}
    )
    assert r.status_code == 200 and r.json()["items"] == []


async def test_the_patients_page_has_every_supported_tab_and_no_unsupported_one(
    client, db, patient, story
):
    await login(client, "pat@x.com")
    page = (await client.get("/en/patient/timeline")).text
    assert page.count('class="tl-tab"') == 6
    for label in (
        "All",
        "Consultations",
        "Labs",
        "Prescriptions",
        "Vaccinations",
        "Other Medical Documents",
    ):
        assert label in page
    assert "Treatments" not in page and "Procedures" not in page


# ═══ E. Date range ═══════════════════════════════════════════════════════
async def test_the_date_range_is_inclusive_and_uses_pakistan_calendar_days(
    client, db, patient, doctor, clinic
):
    # 21:00 UTC on the 10th is 02:00 on the 11th in Pakistan (UTC+5): that is its clinical day
    late = await add_visit(db, patient, doctor, clinic, when=at(2026, 10, 10, 21, 0))
    early = await add_visit(
        db, patient, doctor, clinic, when=at(2026, 10, 10, 18, 59)
    )  # 23:59 on the 10th
    await login(client, "pat@x.com")
    on_11 = await timeline(client, date_from="2026-10-11", date_to="2026-10-11")
    on_10 = await timeline(client, date_from="2026-10-10", date_to="2026-10-10")
    assert refs(on_11) == [f"encounter:{late.id}"]
    assert refs(on_10) == [f"encounter:{early.id}"]
    assert on_11["items"][0]["event_date"] == "2026-10-11"
    both = await timeline(client, date_from="2026-10-10", date_to="2026-10-11")
    assert len(both["items"]) == 2
    # one end alone is a half-open range
    assert len((await timeline(client, date_from="2026-10-11"))["items"]) == 1
    assert len((await timeline(client, date_to="2026-10-10"))["items"]) == 1


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"date_from": "2026-10-12", "date_to": "2026-10-11"}, "date_to"),  # From after To
        ({"date_from": "12/10/2026"}, "date_from"),
        ({"date_to": "2026-02-30"}, "date_to"),
        ({"date_from": "2026-1-5"}, "date_from"),
    ],
)
async def test_an_invalid_range_is_refused_on_the_api_and_explained_on_the_page(
    client, db, patient, story, params, field
):
    await login(client, "pat@x.com")
    r = await client.get(ME, params=params)
    assert r.status_code == 422 and field in r.json()["error"]["details"]["fields"]
    page = await client.get("/en/patient/timeline", params=params)
    assert page.status_code == 200
    assert 'class="field-error"' in page.text  # said beside the date, not applied
    assert page.text.count('class="tl-card"') == 8  # nothing was filtered by a range nobody meant
    assert '<details class="tl-filters" open>' in page.text  # opened so the message is seen


# ═══ F. Who may see what ═════════════════════════════════════════════════
async def test_a_patient_cannot_steer_the_timeline_to_another_patient(
    client, db, patient, other_patient, story
):
    await add_document(
        db,
        other_patient,
        doc_type="lab",
        uploaded_by=other_patient.user_id,
        rows=(HBA1C,),
        status="awaiting_review",
        reviewed=False,
        name="theirs.pdf",
    )
    await login(client, "pat@x.com")
    for params in ({"patient_id": str(other_patient.id)}, {"user_id": "x"}, {"owner": "1"}):
        r = await client.get(ME, params=params)
        assert r.status_code == 422, params  # unknown parameters are refused, not ignored
    body = await timeline(client)
    assert body["pending"] == [] and "theirs" not in json.dumps(body)


async def test_uploads_in_progress_are_the_patients_alone(client, db, doctor, patient, story):
    await add_document(
        db,
        patient,
        doc_type="rx",
        uploaded_by=patient.user_id,
        status="awaiting_review",
        rows=(METFORMIN,),
        reviewed=False,
        name="pending-rx.pdf",
    )
    await grant(
        db,
        patient,
        grantee_type="doctor",
        grantee_id=doctor.id,
        sections=["visits", "prescriptions", "lab_reports"],
    )
    await login(client, "dr@x.com")
    body = (await client.get(f"/api/v1/doctor/patients/{patient.id}/timeline")).json()
    assert body["pending"] == [] and body["pending_review"] is None
    assert "pending-rx" not in json.dumps(body)
    page = (await client.get(f"/en/doctor/patient/{patient.id}/timeline")).text
    assert 'data-timeline-state="pending"' not in page


async def test_the_existing_confirmation_rule_is_unchanged(client, db, patient, fake_ocr):
    """Reading never finalizes: only the patient's Confirm & Save does (the timeline only watches)."""
    await login(client, "pat@x.com")
    doc = await upload(client, "prescription")
    detail = await read(client, doc["id"])
    stored = (
        await db.execute(select(Document).where(Document.id == __import__("uuid").UUID(doc["id"])))
    ).scalar_one()
    assert stored.status == "awaiting_review" and stored.finalized_at is None
    assert detail["can_delete"] is True
    for _ in range(3):  # looking at the timeline changes nothing about the document
        await timeline(client)
    await db.refresh(stored)
    assert stored.status == "awaiting_review"


async def test_the_delete_confirmation_says_what_happens_to_the_timeline_entry(
    client, db, patient, story
):
    await login(client, "pat@x.com")
    en = (await client.get(f"/en/patient/documents/{story.rx_doc.id}/delete")).text
    assert "medical timeline" in en and "stays in your profile and on your timeline" in en
    ur = (await client.get(f"/ur/patient/documents/{story.rx_doc.id}/delete")).text
    assert "ٹائم لائن" in ur and 'dir="rtl"' in ur
    # an upload that is not saved yet leaves the "documents in progress" instead
    waiting = await add_document(
        db,
        patient,
        doc_type="rx",
        uploaded_by=patient.user_id,
        status="awaiting_review",
        rows=(),
        reviewed=False,
    )
    unsaved = (await client.get(f"/en/patient/documents/{waiting.id}/delete")).text
    assert "documents in progress on your medical timeline" in unsaved
