"""Medical history timeline — events, filters, API and access control (P3, FR8 / FR9).

Three layers, each tested where it lives:
  * `timeline/events.py` is pure (dicts in, events out): ordering, dates, provenance, filters,
    "nothing is invented" — tested with plain records and no database;
  * the service and JSON API: what each reader may see, unconfirmed uploads kept out, one 404
    for every refusal, one log row per section a doctor reads;
  * the guard that nothing but `timeline/sources.py` reads the visit / prescription tables.

All data is synthetic. The pages are in `test_timeline_web.py`.
"""

from __future__ import annotations

import ast
import json
import re
from datetime import date, timedelta
from pathlib import Path

import pytest
from app.db.models import (
    Allergy,
    Doctor,
    OcrExtraction,
    Patient,
    Profile,
    UserRole,
)
from app.db.types import utcnow, uuid7
from app.i18n.catalogue import missing_keys, translate
from app.timeline import events as ev
from app.timeline import service as tl_service
from sqlalchemy import update

from tests.conftest import make_user
from tests.timeline_support import (
    HBA1C,
    METFORMIN,
    access_rows,
    add_document,
    at,
    create_admin,
    create_doctor,
    create_patient,
    create_story,
    grant,
    login,
)

ROOT = Path(__file__).resolve().parent.parent
ME = "/api/v1/me/timeline"


def doctor_url(patient_id, suffix: str = "") -> str:
    return f"/api/v1/doctor/patients/{patient_id}/timeline{suffix}"


def error_body(r) -> dict:
    error = dict(r.json()["error"])
    error.pop("request_id")
    return error


# ── Fixtures ─────────────────────────────────────────────────────────────
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
async def other_patient(db) -> Patient:
    return await create_patient(db, "other@x.com")


@pytest.fixture
async def story(db, clinic, doctor, patient):
    return await create_story(db, clinic, doctor, patient)


# ═══ events.py — pure ════════════════════════════════════════════════════
def visit_rec(when, **kw):
    return {
        "id": uuid7(),
        "at": when,
        "doctor": "Sana Malik",
        "facility": "Shifa International, Islamabad",
        "complaint": None,
        "notes": None,
        "ai_summary": None,
        "diagnoses": [],
        **kw,
    }


def doc_rec(**kw):
    return {
        "origin": "document",
        "id": uuid7(),
        "doc_type": "lab",
        "date": None,
        "uploaded_at": at(2026, 10, 1),
        "authored_by_doctor": False,
        "facility": None,
        "doctor": None,
        "title": None,
        "notes": [],
        "rows": [],
        **kw,
    }


def test_events_are_ordered_by_the_date_they_happened_not_the_date_they_were_uploaded():
    older_upload_newer_event = doc_rec(date=date(2026, 8, 1), uploaded_at=at(2026, 8, 2))
    newer_upload_older_event = doc_rec(date=date(2026, 3, 1), uploaded_at=at(2026, 10, 1))
    events = ev.build_events(
        {"lab_reports": [newer_upload_older_event, older_upload_newer_event]}, loc="en", owner=True
    )
    assert [e.event_date for e in events] == [date(2026, 8, 1), date(2026, 3, 1)]


def test_a_visit_late_in_the_evening_utc_is_the_next_day_in_pakistan():
    # 21:00 UTC on the 10th is 02:00 on the 11th in Pakistan (UTC+5)
    [event] = ev.build_events(
        {"visits": [visit_rec(at(2026, 10, 10, 21, 0))]}, loc="en", owner=True
    )
    assert event.event_date == date(2026, 10, 11)
    assert event.date_basis == "clinical"


def test_two_visits_on_one_day_the_later_one_is_first():
    morning, evening = visit_rec(at(2026, 7, 1, 3, 0)), visit_rec(at(2026, 7, 1, 12, 0))
    events = ev.build_events({"visits": [morning, evening]}, loc="en", owner=True)
    assert [e.ref for e in events] == [f"encounter:{evening['id']}", f"encounter:{morning['id']}"]


def test_a_document_that_states_no_date_is_listed_last_and_says_so():
    dated = doc_rec(date=date(2020, 1, 1))
    undated_new = doc_rec(uploaded_at=at(2026, 10, 9))
    undated_old = doc_rec(uploaded_at=at(2026, 9, 1))
    events = ev.build_events(
        {"lab_reports": [undated_old, dated, undated_new]}, loc="en", owner=True
    )
    assert [e.ref for e in events] == [
        f"lab_document:{dated['id']}",
        f"lab_document:{undated_new['id']}",  # undated: the more recently uploaded first
        f"lab_document:{undated_old['id']}",
    ]
    assert [e.date_basis for e in events] == ["clinical", "unknown", "unknown"]
    assert events[1].event_date is None


def test_an_entry_with_no_event_date_sits_on_the_day_it_was_recorded_and_says_so():
    [allergy] = ev.build_events(
        {
            "allergies": [
                {
                    "id": uuid7(),
                    "substance": "Peanuts",
                    "reaction": None,
                    "severity": None,
                    "recorded_at": at(2026, 5, 1),
                    "by": "patient",
                }
            ]
        },
        loc="en",
        owner=True,
    )
    assert allergy.event_date == date(2026, 5, 1) and allergy.date_basis == "recorded"
    [condition] = ev.build_events(
        {
            "conditions": [
                {
                    "id": uuid7(),
                    "name": "Asthma",
                    "onset": date(2020, 3, 1),
                    "recorded_at": at(2026, 5, 1),
                    "by": "clinician",
                }
            ]
        },
        loc="en",
        owner=True,
    )
    assert condition.event_date == date(2020, 3, 1) and condition.date_basis == "clinical"


def test_an_incomplete_visit_shows_only_what_the_record_holds():
    [event] = ev.build_events(
        {"visits": [visit_rec(at(2026, 7, 1), doctor=None, facility=None)]}, loc="en", owner=True
    )
    assert event.title == translate("timeline.title.consultation", "en")
    assert event.summary == [] and event.doctor is None and event.facility is None
    # no complaint, no diagnoses, no notes, no AI summary: no such block, no empty table
    assert [b.heading_key for b in event.blocks] == ["timeline.block.visit"]
    labels = {f.label_key for f in event.blocks[0].facts}
    assert labels == {
        "timeline.fact.date",
        "timeline.fact.time",
    }  # no doctor / facility line invented


def test_a_visit_summary_names_the_diagnoses_but_never_the_ai_summary():
    [event] = ev.build_events(
        {
            "visits": [
                visit_rec(
                    at(2026, 7, 1),
                    complaint="Fever and cough",
                    ai_summary="Probably viral.",
                    diagnoses=[
                        {
                            "description": "Viral fever",
                            "code": "B34",
                            "severity": None,
                            "chronic": False,
                        }
                    ],
                )
            ]
        },
        loc="en",
        owner=True,
    )
    assert "Fever and cough" in event.title
    assert event.summary == ["Diagnosis: Viral fever"]
    assert "Probably viral." not in json.dumps(event.summary)
    [ai] = [b for b in event.blocks if b.ai_generated]
    assert ai.text == "Probably viral." and ai.heading_key == "timeline.block.ai_summary"


def test_provenance_says_who_vouches_for_each_event():
    records = {
        "visits": [visit_rec(at(2026, 7, 1))],
        "prescriptions": [doc_rec(doc_type="rx", date=date(2026, 7, 1), authored_by_doctor=True)],
        "lab_reports": [doc_rec(date=date(2026, 7, 2))],
        "allergies": [
            {
                "id": uuid7(),
                "substance": s,
                "reaction": None,
                "severity": None,
                "recorded_at": at(2026, 5, 1),
                "by": by,
            }
            for s, by in (("A", "patient"), ("B", "clinician"), ("C", None))
        ],
    }
    by_title = {e.title: e for e in ev.build_events(records, loc="en", owner=True)}
    assert by_title["Consultation"].provenance == "doctor_confirmed"
    assert by_title["Prescription issued"].provenance == "doctor_confirmed"  # a doctor wrote it
    assert (
        by_title["Lab report"].provenance == "ai_patient_confirmed"
    )  # read by a program, saved by the patient
    assert by_title["Allergy: A"].provenance == "patient_entered"
    assert by_title["Allergy: B"].provenance == "clinician_recorded"
    assert by_title["Allergy: C"].provenance == "source_unknown"
    # The wording differs for the patient and a doctor reading the same event
    assert by_title["Lab report"].provenance_label == translate(
        "timeline.prov.ai_you_confirmed", "en"
    )
    doctor_view = {e.title: e for e in ev.build_events(records, loc="en", owner=False)}
    assert doctor_view["Lab report"].provenance_label == translate(
        "timeline.prov.ai_patient_confirmed", "en"
    )


def test_a_doctor_view_never_carries_a_document_link():
    rec = doc_rec(date=date(2026, 7, 2))
    [owner] = ev.build_events({"lab_reports": [rec]}, loc="en", owner=True)
    [reader] = ev.build_events({"lab_reports": [rec]}, loc="en", owner=False)
    assert owner.document_id == rec["id"] and reader.document_id is None


def test_a_calculated_lab_flag_is_never_passed_off_as_the_labs_own():
    rows = [
        {
            "test_name": "HbA1c",
            "result": "7.2",
            "unit": "%",
            "reference_range": "4-5.6",
            "flag": "high",
            "flag_source": "calculated",
        },
        {
            "test_name": "Hb",
            "result": "9",
            "unit": "g/dL",
            "reference_range": "",
            "flag": "low",
            "flag_source": "printed",
        },
    ]
    [event] = ev.build_events(
        {"lab_reports": [doc_rec(date=date(2026, 7, 2), rows=rows)]}, loc="en", owner=True
    )
    [table] = [b for b in event.blocks if b.kind == "table"]
    calculated, printed = table.rows[0][4], table.rows[1][4]
    assert calculated.flag == "high" and calculated.note == translate(
        "documents.flag.calculated", "en"
    )
    assert printed.flag == "low" and printed.note is None
    assert event.summary == ["2 results · 2 outside the normal range"]


def make_filter_events():
    return ev.build_events(
        {
            "visits": [
                visit_rec(at(2026, 7, 28), facility="Shifa International, Islamabad"),
                visit_rec(
                    at(2026, 3, 1), doctor="Ali Raza", facility="Maroof International, Islamabad"
                ),
            ],
            "lab_reports": [
                doc_rec(date=date(2026, 6, 15), facility="Chughtai Lab, Lahore"),
                doc_rec(date=None),
            ],
            "prescriptions": [doc_rec(doc_type="rx", date=date(2026, 8, 11))],
        },
        loc="en",
        owner=True,
    )


def test_filters_combine_and_counts_ignore_only_the_tab():
    events = make_filter_events()
    f = ev.Filters

    def refs(flt):
        return [e for e in events if ev.matches(e, flt)]

    assert len(refs(f())) == 5
    assert {e.category for e in refs(f(category="lab"))} == {"lab"} and len(
        refs(f(category="lab"))
    ) == 2
    # a date range leaves out the event with no date
    in_summer = refs(f(date_from=date(2026, 6, 1), date_to=date(2026, 8, 31)))
    assert len(in_summer) == 3 and all(e.event_date for e in in_summer)
    assert (
        len(refs(f(date_from=date(2026, 7, 28), date_to=date(2026, 7, 28)))) == 1
    )  # both ends included
    assert len(refs(f(facility="shifa international, islamabad"))) == 1  # case-insensitive
    assert [e.doctor for e in refs(f(doctor="Dr. Ali Raza"))] == ["Dr. Ali Raza"]
    assert refs(f(category="lab", doctor="Dr. Ali Raza")) == []
    # tab counts: every filter but the tab
    assert ev.tab_counts(events, f()) == {
        "all": 5,
        "consultation": 2,
        "lab": 2,
        "prescription": 1,
        "vaccination": 0,
        "document": 0,
        "profile": 0,
    }
    assert ev.tab_counts(events, f(category="lab", date_from=date(2026, 6, 1))) == {
        "all": 3,
        "consultation": 1,
        "lab": 1,
        "prescription": 1,
        "vaccination": 0,
        "document": 0,
        "profile": 0,
    }
    assert ev.distinct(e.facility for e in events) == [
        "Chughtai Lab, Lahore",
        "Maroof International, Islamabad",
        "Shifa International, Islamabad",
    ]


def test_filters_are_parsed_strictly_and_a_bad_one_is_reported_not_guessed():
    flt, problems = tl_service.parse_filters(
        {"type": "lab", "date_from": "2026-06-01", "doctor": " Dr. A "}
    )
    assert (flt.category, flt.date_from, flt.doctor, problems) == (
        "lab",
        date(2026, 6, 1),
        "Dr. A",
        {},
    )
    assert tl_service.parse_filters({"type": "all"})[0].category is None
    flt, problems = tl_service.parse_filters(
        {"type": "xray", "date_from": "01/06/2026", "date_to": "2026-13-01"}
    )
    assert problems == {
        "type": "errors.timeline_type_invalid",
        "date_from": "errors.timeline_date_invalid",
        "date_to": "errors.timeline_date_invalid",
    }
    assert flt == ev.Filters()  # nothing applied that was not understood
    flt, problems = tl_service.parse_filters({"date_from": "2026-08-01", "date_to": "2026-07-01"})
    assert problems == {"date_to": "errors.timeline_range_invalid"} and not flt.narrowed_by_date


def test_dates_are_written_the_way_the_design_shows_them():
    assert ev.human_date(date(2026, 8, 11), "en") == "11 Aug 2026"
    assert ev.human_date(date(2026, 8, 1), "ur") == "01 اگست 2026"
    assert ev.human_date(None, "en") == "—"


# ═══ The JSON API: a patient's own timeline ═════════════════════════════
async def test_a_new_patients_timeline_is_empty_not_an_error(client, db, patient):
    await login(client, "pat@x.com")
    r = await client.get(ME)
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == [] and body["total"] == 0 and body["undated"] == 0
    assert body["counts"] == {
        "all": 0,
        "consultation": 0,
        "lab": 0,
        "prescription": 0,
        "vaccination": 0,
        "document": 0,
        "profile": 0,
    }
    assert body["pending_review"] == 0


async def test_the_timeline_is_in_clinical_date_order_with_undated_last(client, db, patient, story):
    await login(client, "pat@x.com")
    body = (await client.get(ME)).json()
    assert [i["ref"] for i in body["items"]] == story.order
    assert body["total"] == 8 and body["undated"] == 1
    assert body["counts"] == {
        "all": 8,
        "consultation": 1,
        "lab": 2,
        "prescription": 2,
        "vaccination": 1,
        "document": 0,
        "profile": 2,
    }
    assert body["facilities"] == ["Shifa International, Islamabad"]
    assert body["doctors"] == ["Dr. Sana Malik"]
    # the list never carries the detail blocks
    assert all("blocks" not in item for item in body["items"])


async def test_each_event_has_a_title_a_provider_and_who_vouches_for_it(client, db, patient, story):
    await login(client, "pat@x.com")
    items = {i["ref"]: i for i in (await client.get(ME)).json()["items"]}
    visit = items[f"encounter:{story.visit.id}"]
    assert (
        visit["title"] == "Consultation — Endocrinology follow-up"
    )  # only the first line of the reason
    assert (visit["doctor"], visit["facility"]) == (
        "Dr. Sana Malik",
        "Shifa International, Islamabad",
    )
    assert (visit["provenance"], visit["event_date"]) == ("doctor_confirmed", "2026-07-28")
    assert visit["summary"] == ["Diagnosis: Type 2 diabetes mellitus"]
    upload = items[f"rx_document:{story.rx_doc.id}"]
    assert upload["title"] == "Prescription uploaded & digitised"
    assert upload["provenance"] == "ai_patient_confirmed"
    assert upload["summary"] == ["Metformin 500 mg"]
    assert upload["document_id"] == str(story.rx_doc.id) and upload["uploaded_on"] == "2026-09-01"
    allergy = items[f"allergy:{story.allergy.id}"]
    assert (allergy["provenance"], allergy["date_basis"]) == ("patient_entered", "recorded")
    assert items[f"condition:{story.condition.id}"]["provenance"] == "clinician_recorded"


async def test_the_filters_work_through_the_api(client, db, patient, story):
    await login(client, "pat@x.com")

    async def refs(**params):
        r = await client.get(ME, params=params)
        assert r.status_code == 200, r.text
        return r.json()

    labs = await refs(type="lab")
    assert [i["ref"] for i in labs["items"]] == [
        f"lab_document:{story.lab.id}",
        f"lab_document:{story.undated_lab.id}",
    ]
    assert labs["counts"]["all"] == 8  # the tabs keep counting the whole timeline
    ranged = await refs(date_from="2026-06-01", date_to="2026-08-31")
    assert [i["ref"] for i in ranged["items"]] == story.order[
        :4
    ]  # the undated lab is outside any range
    assert ranged["counts"] == {
        "all": 4,
        "consultation": 1,
        "lab": 1,
        "prescription": 2,
        "vaccination": 0,
        "document": 0,
        "profile": 0,
    }
    # the visit and the prescription written at it name the doctor; the uploaded one does not
    assert (await refs(doctor="dr. sana malik"))["total"] == 2
    assert (await refs(facility="Nowhere"))["items"] == []
    assert (await refs(type="consultation", date_from="2027-01-01"))["items"] == []


async def test_a_filter_that_is_not_understood_is_refused_naming_it(client, db, patient, story):
    await login(client, "pat@x.com")
    for params, field in (
        ({"type": "xray"}, "type"),
        ({"date_from": "yesterday"}, "date_from"),
        ({"date_to": "2026-02-30"}, "date_to"),
        ({"statuss": "x"}, "statuss"),  # a misspelt option is refused, not silently ignored
    ):
        r = await client.get(ME, params=params)
        assert r.status_code == 422, params
        assert field in r.json()["error"]["details"]["fields"]
    assert (await client.get(ME, params={"limit": 0})).status_code == 422
    assert (await client.get(ME, params={"offset": -1})).status_code == 422


async def test_the_timeline_pages_and_the_total_stays_whole(client, db, patient, story):
    await login(client, "pat@x.com")
    first = (await client.get(ME, params={"limit": 3})).json()
    second = (await client.get(ME, params={"limit": 3, "offset": 3})).json()
    past_the_end = (await client.get(ME, params={"limit": 3, "offset": 30})).json()
    assert [i["ref"] for i in first["items"] + second["items"]] == story.order[:6]
    assert first["total"] == second["total"] == past_the_end["total"] == 8
    assert past_the_end["items"] == []
    assert (first["limit"], first["offset"], second["offset"]) == (3, 0, 3)


async def test_an_upload_is_not_history_until_the_patient_confirms_it(client, db, patient, story):
    waiting = await add_document(
        db,
        patient,
        doc_type="rx",
        uploaded_by=patient.user_id,
        rows=(METFORMIN,),
        status="awaiting_review",
        document_date=date(2026, 9, 9),
        reviewed=False,
    )
    await add_document(
        db, patient, doc_type="lab", uploaded_by=patient.user_id, status="failed", rows=()
    )
    await add_document(
        db, patient, doc_type="lab", uploaded_by=patient.user_id, status="cancelled", rows=()
    )
    await login(client, "pat@x.com")
    body = (await client.get(ME)).json()
    assert [i["ref"] for i in body["items"]] == story.order  # nothing new
    assert all(str(waiting.id) not in i["ref"] for i in body["items"])
    assert body["pending_review"] == 1
    # ...it is listed as what it is — a pending item, not an event — and nothing read from it
    by_status = {p["status"]: p for p in body["pending"]}
    assert set(by_status) == {"awaiting_review", "failed"}  # the discarded one is nowhere
    item = by_status["awaiting_review"]
    assert (item["id"], item["category"]) == (str(waiting.id), "prescription")
    assert item["document_type"] == "prescription" and item["name"] == "scan.pdf"
    assert by_status["failed"]["category"] == "lab" and by_status["failed"]["message"]
    assert "Metformin" not in json.dumps(body["pending"])  # what was read is never shown here


async def test_only_the_values_the_patient_confirmed_are_read_from_an_upload(client, db, patient):
    """A table the review step never covered stays a proposal — it must not read as history."""
    doc = await add_document(
        db,
        patient,
        doc_type="rx",
        uploaded_by=patient.user_id,
        rows=(METFORMIN, {**METFORMIN, "medicine": "Unreviewed"}),
        document_date=date(2026, 8, 1),
    )
    # the second row (index 1) was never reviewed
    await db.execute(
        update(OcrExtraction).where(OcrExtraction.row_index == 1).values(reviewed_at=None)
    )
    await db.commit()
    await login(client, "pat@x.com")
    [item] = (await client.get(ME)).json()["items"]
    assert item["ref"] == f"rx_document:{doc.id}" and item["summary"] == ["Metformin 500 mg"]


async def test_a_medicine_name_is_never_shown_as_the_hospital(client, db, patient):
    """A page that opens straight with a medicine has no letterhead; the extractor's fallback
    reads that first line as a hospital name, and the timeline must not repeat it."""
    await add_document(
        db,
        patient,
        doc_type="rx",
        uploaded_by=patient.user_id,
        rows=(METFORMIN,),
        document_date=date(2026, 8, 1),
        ocr_text="Metformin 500 mg twice daily",
    )
    await login(client, "pat@x.com")
    body = (await client.get(ME)).json()
    [item] = body["items"]
    assert item["facility"] is None and body["facilities"] == []


async def test_a_patient_sees_only_their_own_record(
    client, db, patient, other_patient, doctor, clinic, story
):
    theirs = await add_document(
        db,
        other_patient,
        doc_type="lab",
        uploaded_by=other_patient.user_id,
        rows=(HBA1C,),
        document_date=date(2026, 9, 9),
    )
    db.add(
        Allergy(
            id=uuid7(),
            patient_id=other_patient.id,
            substance="Latex",
            recorded_by=other_patient.user_id,
        )
    )
    await db.commit()
    await login(client, "pat@x.com")
    body = (await client.get(ME)).json()
    assert [i["ref"] for i in body["items"]] == story.order
    assert "Latex" not in json.dumps(body)
    # ...and cannot reach one of the other patient's events by id either
    r = await client.get(f"{ME}/lab_document/{theirs.id}")
    assert r.status_code == 404
    assert (
        await client.get(f"{ME}/lab_document/{uuid7()}")
    ).status_code == 404  # identical for a missing id


async def test_the_detail_of_an_event(client, db, patient, story):
    await login(client, "pat@x.com")

    async def detail(kind, ident):
        r = await client.get(f"{ME}/{kind}/{ident}")
        assert r.status_code == 200, r.text
        return r.json()

    visit = await detail("encounter", story.visit.id)
    blocks = {b["heading_key"]: b for b in visit["blocks"]}
    assert blocks["timeline.block.complaint"]["text"] == "Endocrinology follow-up\nSecond line"
    assert blocks["timeline.block.notes"]["text"] == "Review again in three months."
    assert blocks["timeline.block.ai_summary"]["ai_generated"] is True  # labelled, kept apart
    assert blocks["timeline.block.notes"]["ai_generated"] is False
    [diagnosis] = blocks["timeline.block.diagnoses"]["rows"]
    assert [c["value"] for c in diagnosis] == ["Type 2 diabetes mellitus", "E11", "—", "No"]

    rx = await detail("prescription", story.rx.id)
    [row] = next(b for b in rx["blocks"] if b["kind"] == "table")["rows"]
    assert [c["value"] for c in row][:4] == ["Glucophage", "1 tablet", "twice daily", "30 days"]

    lab = await detail("lab_document", story.lab.id)
    [result] = next(b for b in lab["blocks"] if b["kind"] == "table")["rows"]
    assert result[0]["value"] == "HbA1c" and result[1]["value"] == "7.2"
    assert lab["document_id"] == str(story.lab.id)  # the patient can open the saved document

    vaccine = await detail("document", story.vaccine.id)
    assert vaccine["title"] == "Vaccination record"
    assert "Hepatitis B" in json.dumps(vaccine["blocks"])


@pytest.mark.parametrize("which", ["wrong_kind", "unknown_kind", "missing"])
async def test_a_detail_that_does_not_exist_is_one_404(client, db, patient, story, which):
    await login(client, "pat@x.com")
    url = {
        "wrong_kind": f"{ME}/allergy/{story.visit.id}",  # a real id under the wrong kind
        "unknown_kind": f"{ME}/not_a_kind/{story.visit.id}",
        "missing": f"{ME}/encounter/{uuid7()}",
    }[which]
    r = await client.get(url)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


async def test_the_roles_are_kept_apart(client, db, patient, doctor, admin, story):
    # not signed in
    assert (await client.get(ME)).status_code == 401
    assert (await client.get(doctor_url(patient.id))).status_code == 401
    # a doctor has no "own" timeline; a patient has no doctor view
    await login(client, "dr@x.com")
    assert (await client.get(ME)).status_code == 403
    await login(client, "pat@x.com")
    assert (await client.get(doctor_url(patient.id))).status_code == 403
    # a clinic admin never reaches clinical history (NFR16)
    await login(client, "adm@x.com")
    assert (await client.get(ME)).status_code == 403
    assert (await client.get(doctor_url(patient.id))).status_code == 403


async def test_an_unverified_doctor_reaches_no_patient_data(client, db, clinic, patient, story):
    await make_user(
        db, email="pending@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id, is_verified=False
    )
    await login(client, "pending@x.com")
    r = await client.get(doctor_url(patient.id))
    assert r.status_code == 403
    assert await access_rows(db) == []


# ═══ The JSON API: a doctor's view ══════════════════════════════════════
async def test_a_doctor_sees_only_the_sections_the_patient_shared(
    client, db, doctor, patient, story
):
    await grant(
        db, patient, grantee_type="doctor", grantee_id=doctor.id, sections=["visits", "lab_reports"]
    )
    await login(client, "dr@x.com")
    r = await client.get(doctor_url(patient.id))
    assert r.status_code == 200
    body = r.json()
    assert [i["ref"] for i in body["items"]] == [
        f"encounter:{story.visit.id}",
        f"lab_document:{story.lab.id}",
        f"lab_document:{story.undated_lab.id}",
    ]
    assert body["counts"] == {
        "all": 3,
        "consultation": 1,
        "lab": 2,
        "prescription": 0,
        "vaccination": 0,
        "document": 0,
        "profile": 0,
    }
    assert body["pending_review"] is None  # that is the patient's own notice
    text = json.dumps(body)
    for withheld in ("Peanuts", "Asthma", "Glucophage", "Metformin", "Hepatitis"):
        assert withheld not in text
    # every section read is logged, once, before the data left
    assert sorted(a.resource_type for a in await access_rows(db)) == [
        "section.lab_reports",
        "section.visits",
    ]


async def test_a_doctor_is_never_given_a_link_to_the_stored_file(
    client, db, doctor, patient, story
):
    await grant(
        db,
        patient,
        grantee_type="doctor",
        grantee_id=doctor.id,
        sections=["lab_reports", "prescriptions"],
    )
    await login(client, "dr@x.com")
    body = (await client.get(doctor_url(patient.id))).json()
    assert body["items"] and all(i["document_id"] is None for i in body["items"])
    detail = (await client.get(doctor_url(patient.id, f"/lab_document/{story.lab.id}"))).json()
    assert detail["document_id"] is None
    assert detail["provenance_label"] == translate("timeline.prov.ai_patient_confirmed", "en")


async def test_a_vaccination_record_and_other_documents_are_the_patients_alone(
    client, db, doctor, patient, story
):
    """No consent section covers them, so not even a full grant reaches them."""
    await grant(
        db,
        patient,
        grantee_type="doctor",
        grantee_id=doctor.id,
        sections=[
            "basics",
            "allergies",
            "conditions",
            "medications",
            "prescriptions",
            "lab_reports",
            "visits",
        ],
    )
    other = await add_document(
        db,
        patient,
        doc_type="other",
        uploaded_by=patient.user_id,
        rows=(METFORMIN,),
        document_date=date(2026, 4, 1),
        ocr_text="Discharge summary",
    )
    await login(client, "dr@x.com")
    body = (await client.get(doctor_url(patient.id))).json()
    refs = [i["ref"] for i in body["items"]]
    assert f"document:{story.vaccine.id}" not in refs and f"document:{other.id}" not in refs
    assert len(refs) == 7  # everything but the vaccination record
    r = await client.get(doctor_url(patient.id, f"/document/{story.vaccine.id}"))
    assert r.status_code == 404
    await login(client, "pat@x.com")  # the patient does see them
    mine = [i["ref"] for i in (await client.get(ME)).json()["items"]]
    assert f"document:{story.vaccine.id}" in mine and f"document:{other.id}" in mine


@pytest.mark.parametrize("case", ["none", "revoked", "expired", "empty", "missing"])
async def test_every_refusal_is_the_same_404_and_logs_nothing(
    client, db, doctor, patient, story, case
):
    target = patient.id
    if case == "revoked":
        await grant(
            db,
            patient,
            grantee_type="doctor",
            grantee_id=doctor.id,
            sections=["visits"],
            revoked_at=utcnow(),
        )
    elif case == "expired":
        await grant(
            db,
            patient,
            grantee_type="doctor",
            grantee_id=doctor.id,
            sections=["visits"],
            expires_at=utcnow() - timedelta(minutes=1),
        )
    elif case == "empty":
        await grant(db, patient, grantee_type="doctor", grantee_id=doctor.id, sections=[])
    elif case == "missing":
        target = uuid7()
    await login(client, "dr@x.com")
    r = await client.get(doctor_url(target))
    d = await client.get(doctor_url(target, f"/encounter/{story.visit.id}"))
    assert (r.status_code, d.status_code) == (404, 404)
    assert error_body(r) == error_body(d)
    assert r.json()["error"]["code"] == "NOT_FOUND"
    assert await access_rows(db) == []


async def test_revoking_takes_effect_on_the_next_request(client, db, doctor, patient, story):
    g = await grant(db, patient, grantee_type="doctor", grantee_id=doctor.id, sections=["visits"])
    await login(client, "dr@x.com")
    assert (await client.get(doctor_url(patient.id))).status_code == 200
    g.revoked_at = utcnow()
    await db.commit()
    assert (await client.get(doctor_url(patient.id))).status_code == 404


async def test_a_clinic_grant_reaches_a_doctor_affiliated_with_that_clinic(
    client, db, clinic, doctor, patient, story
):
    await grant(
        db, patient, grantee_type="clinic", grantee_id=clinic.id, sections=["prescriptions"]
    )
    await login(client, "dr@x.com")
    body = (await client.get(doctor_url(patient.id))).json()
    assert {i["ref"] for i in body["items"]} == {
        f"prescription:{story.rx.id}",
        f"rx_document:{story.rx_doc.id}",
    }
    assert {a.resource_type for a in await access_rows(db)} == {"section.prescriptions"}


async def test_a_doctors_detail_reads_only_the_sections_it_needs_and_logs_each_once(
    client, db, doctor, patient, story
):
    """A visit's page also looks at the sections its documents live in — only those the doctor was
    given, each logged once — so it can say which documents belong to the visit."""
    await grant(
        db, patient, grantee_type="doctor", grantee_id=doctor.id, sections=["visits", "lab_reports"]
    )
    await login(client, "dr@x.com")
    r = await client.get(doctor_url(patient.id, f"/encounter/{story.visit.id}"))
    assert r.status_code == 200
    assert r.json()["blocks"]
    rows = await access_rows(db)
    assert sorted(a.resource_type for a in rows) == ["section.lab_reports", "section.visits"]
    assert {(a.patient_id, a.user_id) for a in rows} == {(patient.id, doctor.user_id)}
    # an event that stands alone reads only its own section
    r = await client.get(doctor_url(patient.id, f"/lab_document/{story.lab.id}"))
    assert r.status_code == 200
    # an event in a section that was not shared is the same 404 as one that does not exist
    no_section = await client.get(doctor_url(patient.id, f"/prescription/{story.rx.id}"))
    no_event = await client.get(doctor_url(patient.id, f"/encounter/{uuid7()}"))
    assert (no_section.status_code, no_event.status_code) == (404, 404)
    assert error_body(no_section) == error_body(no_event)
    # nothing outside the grant was ever read, however many pages were opened
    assert {a.resource_type for a in await access_rows(db)} == {
        "section.lab_reports",
        "section.visits",
    }


async def test_one_doctor_cannot_ride_another_doctors_grant(
    client, db, clinic, doctor, patient, story
):
    """A grant names a doctor; a second, verified doctor with no grant gets nothing."""
    await grant(db, patient, grantee_type="doctor", grantee_id=doctor.id, sections=["visits"])
    await make_user(
        db, email="dr2@x.com", role=UserRole.DOCTOR, is_verified=True, verifier_id=doctor.user_id
    )
    await login(client, "dr2@x.com")
    assert (await client.get(doctor_url(patient.id))).status_code == 404
    assert await access_rows(db) == []


# ═══ The one door (the AC-21 guard, for the tables this feature added) ═══
TIMELINE_MODELS = {
    "Encounter",
    "Diagnosis",
    "Prescription",
    "PrescriptionItem",
    "Medicine",
    "LabReport",
}
APP = ROOT / "backend" / "app"
# Their mapping, and the readers the consent gateway calls. Nothing else may select them.
ALLOWED = {APP / "db" / "models.py", APP / "timeline" / "sources.py"}


def model_uses(source: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and node.id in TIMELINE_MODELS:
            found.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in TIMELINE_MODELS:
            found.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            found.update(a.name for a in node.names if a.name in TIMELINE_MODELS)
    return found


def test_the_guard_catches_a_direct_read():
    assert model_uses("from app.db.models import Encounter\nselect(models.Diagnosis)") == {
        "Encounter",
        "Diagnosis",
    }
    assert model_uses("x = EncounterOut") == set()


def test_no_module_but_the_timeline_readers_selects_visits_or_prescriptions():
    offenders = {
        str(path.relative_to(ROOT)): sorted(uses)
        for path in APP.rglob("*.py")
        if path not in ALLOWED and (uses := model_uses(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}, (
        f"Read a patient's visits and prescriptions through the consent gateway: {offenders}"
    )


def test_the_timeline_reads_patient_documents_only_through_the_documents_service():
    """`sources.py` has no query of its own on `document` — it asks the documents service, so
    the "an upload is never a record" rule lives in one place."""
    source = (APP / "timeline" / "sources.py").read_text(encoding="utf-8")
    names = {n.id for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Name)}
    assert not names & {"Document", "OcrJob", "OcrExtraction"}


# ═══ The catalogue ═══════════════════════════════════════════════════════
def test_every_timeline_message_exists_in_both_languages():
    assert missing_keys() == {"ur": []}
    used: set[str] = set()
    for path in [
        *(APP / "timeline").glob("*.py"),
        APP / "web" / "timeline.py",
        *(ROOT / "frontend" / "templates" / "timeline").glob("*.html"),
    ]:
        used.update(
            re.findall(
                r"""['"]((?:timeline\.|errors\.timeline_)[a-z0-9_.]*[a-z0-9_])['"]""",
                path.read_text(encoding="utf-8"),
            )
        )
    en = json.loads((APP / "i18n" / "messages" / "en.json").read_text(encoding="utf-8"))
    assert used and sorted(k for k in used if k not in en) == []
    # keys built from a value (the tab, the kind, the provenance) have an entry for every value
    for prefix, values in (
        ("timeline.tab.", ("all", "consultation", "lab", "prescription")),
        ("timeline.kind.", ev_kinds()),
        (
            "timeline.prov.",
            (
                "doctor_confirmed",
                "clinician_recorded",
                "ai_patient_confirmed",
                "ai_you_confirmed",
                "patient_entered",
                "source_unknown",
            ),
        ),
        (
            "timeline.prov_help.",
            (
                "doctor_confirmed",
                "clinician_recorded",
                "ai_patient_confirmed",
                "patient_entered",
                "source_unknown",
            ),
        ),
    ):
        assert [prefix + v for v in values if prefix + v not in en] == []


def ev_kinds() -> tuple[str, ...]:
    from app.timeline.schemas import KINDS

    return KINDS


def test_one_result_is_singular_and_none_is_not_mentioned():
    one = {
        "test_name": "Hb",
        "result": "13",
        "unit": "g/dL",
        "reference_range": "12-15",
        "flag": None,
    }
    flagged = {**one, "flag": "low"}
    texts = {}
    for label, rows in (("one", [one]), ("flagged", [flagged]), ("two", [one, one]), ("none", [])):
        [event] = ev.build_events(
            {"lab_reports": [doc_rec(date=date(2026, 7, 2), rows=rows)]}, loc="en", owner=True
        )
        texts[label] = event.summary
    assert texts == {
        "one": ["1 result"],
        "flagged": ["1 result · outside the normal range"],
        "two": ["2 results"],
        "none": [],  # a report with no results reads nothing — it is not made to say "0 results"
    }
