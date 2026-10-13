"""Medical history timeline — the pages (P3, FR8 / FR9).

The patient's page and detail page, the doctor's, the redirects, filters in the address, paging,
the empty / no-match / not-found states, and the link from the doctor's patient page. The events
themselves (ordering, provenance, access) are covered in `test_timeline.py`.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import pytest
from app.db.models import Allergy, Doctor, Patient, Profile, UserRole
from app.db.types import uuid7
from app.i18n.catalogue import translate
from markupsafe import escape

from tests.conftest import make_user
from tests.timeline_support import (
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

PAGE = "/en/patient/timeline"


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


def doctor_page(patient_id, suffix: str = "") -> str:
    return f"/en/doctor/patient/{patient_id}/timeline{suffix}"


def in_html(key: str, locale: str = "en", **params) -> str:
    return str(escape(translate(key, locale, **params)))


def cards(html: str) -> list[str]:
    """The titles of the events on a page, in the order shown."""
    return [
        re.sub(r"\s+", " ", t).strip()
        for t in re.findall(r'<h3 class="tl-card__title"[^>]*>(.*?)</h3>', html, flags=re.DOTALL)
    ]


def state(html: str) -> str | None:
    m = re.search(r'data-timeline-state="([a-z-]+)"', html)
    return m.group(1) if m else None


# ── The patient's page ───────────────────────────────────────────────────
async def test_the_patients_page_lists_events_newest_first_with_the_designs_parts(
    client, db, patient, story
):
    await login(client, "pat@x.com")
    r = await client.get(PAGE)
    assert r.status_code == 200
    assert cards(r.text) == [
        "Prescription uploaded &amp; digitised",
        "Consultation — Endocrinology follow-up",
        "Prescription issued",
        "Lab report",
        "Allergy: Peanuts",
        "Vaccination record",
        "Condition: Asthma",
        "Lab report",
    ]
    # dates as the design writes them; the undated one is apart and says so
    assert "11 Aug 2026" in r.text and "28 Jul 2026" in r.text and "15 Jun 2026" in r.text
    assert in_html("timeline.undated.title") in r.text
    # who vouches for each, and the details link
    assert in_html("timeline.prov.doctor_confirmed") in r.text
    assert in_html("timeline.prov.reviewed_confirmed") in r.text  # documents the patient confirmed
    assert in_html("timeline.prov.clinician_recorded") in r.text
    assert r.text.count(in_html("timeline.view_details")) == 8
    assert f'href="/en/patient/timeline/encounter/{story.visit.id}"' in r.text
    # the tabs, each with its count (Treatments / Procedures is not a tab: no such record exists)
    for key, count in (
        ("all", 8),
        ("consultation", 1),
        ("lab", 2),
        ("prescription", 2),
        ("vaccination", 1),
        ("document", 0),
    ):
        tab = re.search(
            rf'<a class="tl-tab"[^>]*>\s*{re.escape(in_html("timeline.tab." + key))}\s*<span class="tl-count">(\d+)</span>',
            r.text,
        )
        assert tab and int(tab.group(1)) == count, key
    assert 'aria-current="page">All' in re.sub(r"\s+", " ", r.text)


async def test_the_page_is_in_the_patients_navigation(client, db, patient):
    await login(client, "pat@x.com")
    html = re.sub(r"\s+", " ", (await client.get(PAGE)).text)
    assert (
        f'<a class="topnav__link" href="{PAGE}" aria-current="page">{in_html("nav.timeline")}</a>'
        in html
    )
    # ...and reachable from the other patient pages, not marked current there
    other = re.sub(r"\s+", " ", (await client.get("/en/patient/documents")).text)
    assert f'<a class="topnav__link" href="{PAGE}" >{in_html("nav.timeline")}</a>' in other


async def test_a_new_patient_sees_an_empty_state_with_the_way_forward(client, db, patient):
    await login(client, "pat@x.com")
    r = await client.get(PAGE)
    assert r.status_code == 200 and state(r.text) == "empty"
    assert in_html("timeline.empty.owner") in r.text
    assert 'href="/en/patient/documents/new"' in r.text
    assert cards(r.text) == []


async def test_a_tab_narrows_the_list_and_keeps_the_other_tabs_counts(client, db, patient, story):
    await login(client, "pat@x.com")
    r = await client.get(PAGE, params={"type": "lab"})
    assert cards(r.text) == ["Lab report", "Lab report"]
    assert re.search(r'aria-current="page">\s*' + re.escape(in_html("timeline.tab.lab")), r.text)
    assert 'name="type" value="lab"' in r.text  # the choice travels with the filter form
    consult = await client.get(PAGE, params={"type": "consultation"})
    assert cards(consult.text) == ["Consultation — Endocrinology follow-up"]


async def test_filters_live_in_the_address_and_come_back_filled_in(client, db, patient, story):
    await login(client, "pat@x.com")
    r = await client.get(
        PAGE,
        params={"date_from": "2026-06-01", "date_to": "2026-08-31", "doctor": "Dr. Sana Malik"},
    )
    assert cards(r.text) == ["Consultation — Endocrinology follow-up", "Prescription issued"]
    assert 'value="2026-06-01"' in r.text and 'value="2026-08-31"' in r.text
    assert in_html("timeline.filter.count", count=2) in r.text
    # a tab link keeps the other filters
    assert "date_from=2026-06-01" in re.search(r'href="([^"]*type=lab[^"]*)"', r.text).group(1)


async def test_no_match_says_so_and_offers_to_clear_the_filters(client, db, patient, story):
    await login(client, "pat@x.com")
    r = await client.get(PAGE, params={"date_from": "2030-01-01"})
    assert r.status_code == 200 and state(r.text) == "no-match"
    assert in_html("timeline.empty.filtered") in r.text and cards(r.text) == []
    assert f'href="{PAGE}"' in r.text


async def test_a_filter_that_is_not_understood_is_left_out_and_said_so(client, db, patient, story):
    await login(client, "pat@x.com")
    r = await client.get(PAGE, params={"type": "xray", "date_from": "01/06/2026"})
    assert r.status_code == 200
    assert len(cards(r.text)) == 8  # nothing was applied
    assert in_html("timeline.filter.ignored") in r.text
    assert in_html("errors.timeline_type_invalid") in r.text
    assert in_html("errors.timeline_date_invalid") in r.text


async def test_uploads_in_progress_are_listed_apart_and_are_not_history(client, db, patient, story):
    waiting = await add_document(
        db,
        patient,
        doc_type="rx",
        uploaded_by=patient.user_id,
        status="awaiting_review",
        rows=(),
        reviewed=False,
        document_date=date(2026, 9, 9),
    )
    await login(client, "pat@x.com")
    r = await client.get(PAGE)
    assert len(cards(r.text)) == 8
    assert 'data-timeline-state="pending"' in r.text
    assert in_html("timeline.pending.heading") in r.text
    assert f'href="/en/patient/documents/{waiting.id}"' in r.text  # to review it
    assert in_html("timeline.pending.review") in r.text
    assert in_html("documents.status.awaiting_review") in r.text


async def test_a_long_timeline_is_paged_and_a_page_past_the_end_is_the_last(client, db, patient):
    for day in range(30):
        db.add(
            Allergy(
                id=uuid7(),
                patient_id=patient.id,
                substance=f"Substance {day:02d}",
                recorded_at=at(2026, 1, 1) + timedelta(days=day),
                recorded_by=patient.user_id,
            )
        )
    await db.commit()
    await login(client, "pat@x.com")
    first = await client.get(PAGE)
    second = await client.get(PAGE, params={"page": "2"})
    beyond = await client.get(PAGE, params={"page": "99"})
    assert (len(cards(first.text)), len(cards(second.text))) == (25, 5)
    assert (
        cards(first.text)[0] == "Allergy: Substance 29"
        and cards(second.text)[-1] == "Allergy: Substance 00"
    )
    assert cards(beyond.text) == cards(second.text)
    assert in_html("timeline.pager.page", page=2, pages=2) in beyond.text
    assert 'rel="next"' in first.text and 'rel="prev"' in second.text
    assert cards((await client.get(PAGE, params={"page": "junk"})).text) == cards(first.text)


async def test_text_from_the_record_is_escaped(client, db, patient):
    db.add(
        Allergy(
            id=uuid7(),
            patient_id=patient.id,
            substance="<script>alert(1)</script>",
            recorded_at=at(2026, 5, 1),
            recorded_by=patient.user_id,
        )
    )
    await db.commit()
    await login(client, "pat@x.com")
    html = (await client.get(PAGE)).text
    assert (
        "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    )


async def test_the_page_follows_the_language_and_reads_right_to_left_in_urdu(
    client, db, patient, story
):
    await login(client, "pat@x.com")
    r = await client.get("/ur/patient/timeline")
    assert r.status_code == 200 and 'dir="rtl"' in r.text
    assert translate("timeline.title", "ur") in r.text
    assert "11 اگست 2026" in r.text
    assert 'href="/en/patient/timeline"' in r.text  # the language switch keeps the page


# ── Details ──────────────────────────────────────────────────────────────
async def test_view_details_shows_everything_the_record_holds_about_the_event(
    client, db, patient, story
):
    await login(client, "pat@x.com")
    r = await client.get(f"{PAGE}/encounter/{story.visit.id}")
    assert r.status_code == 200
    assert "Endocrinology follow-up" in r.text and "Review again in three months." in r.text
    assert "Type 2 diabetes mellitus" in r.text and "E11" in r.text
    assert "Dr. Sana Malik" in r.text and "Shifa International, Islamabad" in r.text
    # a model's summary is labelled, and kept apart from what the doctor wrote
    assert in_html("timeline.ai_generated") in r.text and "Stable on current treatment." in r.text
    assert in_html("timeline.prov_help.doctor_confirmed") in r.text
    assert f'href="{PAGE}"' in r.text  # back to the timeline


async def test_an_ai_read_document_says_the_patient_confirmed_it_and_links_to_the_file(
    client, db, patient, story
):
    await login(client, "pat@x.com")
    r = await client.get(f"{PAGE}/rx_document/{story.rx_doc.id}")
    assert r.status_code == 200
    assert in_html("timeline.prov_help.ai_patient_confirmed") in r.text
    # a prescription the patient reviewed and saved says exactly that — not "AI"
    assert in_html("timeline.prov.reviewed_confirmed") in r.text
    assert in_html("timeline.prov.ai_you_confirmed") not in r.text
    assert f'href="/en/patient/documents/{story.rx_doc.id}"' in r.text
    assert f'href="/en/patient/documents/{story.rx_doc.id}/file"' in r.text
    assert (
        "Metformin" in r.text
        and "twice daily" in r.text
        and "2026-09-01" not in r.text.split("<main")[0]
    )


async def test_a_lab_result_shows_its_flag_and_that_it_was_calculated(client, db, patient, story):
    await login(client, "pat@x.com")
    r = await client.get(f"{PAGE}/lab_document/{story.lab.id}")
    assert "HbA1c" in r.text and "7.2" in r.text and "4.0-5.6" in r.text
    assert in_html("documents.flag.high") in r.text
    assert in_html("documents.flag.calculated") in r.text  # never passed off as the lab's own


async def test_the_back_link_remembers_the_filters_and_the_page(client, db, patient, story):
    await login(client, "pat@x.com")
    r = await client.get(
        f"{PAGE}/lab_document/{story.lab.id}",
        params={"type": "lab", "date_from": "2026-01-01", "page": "1", "evil": "https://x.test"},
    )
    back = re.search(
        r'<a class="btn btn--secondary btn--sm" href="([^"]+)">'
        + re.escape(in_html("timeline.back")),
        r.text,
    ).group(1)
    assert back.startswith(PAGE + "?") and "type=lab" in back and "date_from=2026-01-01" in back
    assert "evil" not in back and "x.test" not in back


@pytest.mark.parametrize("which", ["missing", "bad_id", "wrong_kind"])
async def test_a_detail_that_does_not_exist_shows_the_timeline_with_a_message(
    client, db, patient, story, which
):
    await login(client, "pat@x.com")
    url = {
        "missing": f"{PAGE}/encounter/{uuid7()}",
        "bad_id": f"{PAGE}/encounter/not-an-id",
        "wrong_kind": f"{PAGE}/allergy/{story.visit.id}",
    }[which]
    r = await client.get(url)
    assert r.status_code == 404
    assert in_html("errors.not_found") in r.text and len(cards(r.text)) == 8


async def test_another_patients_event_is_not_found(client, db, patient, other_patient, story):
    theirs = await add_document(
        db,
        other_patient,
        doc_type="lab",
        uploaded_by=other_patient.user_id,
        rows=(),
        document_date=date(2026, 9, 9),
    )
    await login(client, "pat@x.com")
    r = await client.get(f"{PAGE}/lab_document/{theirs.id}")
    assert r.status_code == 404 and str(theirs.id) not in r.text


# ── Who may open what ────────────────────────────────────────────────────
async def test_a_signed_out_visitor_is_sent_to_sign_in_and_brought_back(client, db, patient, story):
    for url in (PAGE, f"{PAGE}/encounter/{story.visit.id}", doctor_page(patient.id)):
        r = await client.get(url, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].startswith("/en/login?next=")
        assert url.split("?")[0] in r.headers["location"].replace("%2F", "/")


async def test_a_page_for_the_other_role_sends_the_visitor_to_their_own_area(
    client, db, doctor, patient, admin, story
):
    await login(client, "dr@x.com")
    assert (await client.get(PAGE, follow_redirects=False)).headers["location"] == "/en/doctor"
    await login(client, "pat@x.com")
    assert (await client.get(doctor_page(patient.id), follow_redirects=False)).headers[
        "location"
    ] == "/en/patient"
    await login(client, "adm@x.com")
    assert (await client.get(PAGE, follow_redirects=False)).status_code == 303
    assert (await client.get(doctor_page(patient.id), follow_redirects=False)).status_code == 303


async def test_an_unverified_doctor_is_sent_to_their_dashboard(client, db, clinic, patient, story):
    await make_user(
        db, email="pending@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id, is_verified=False
    )
    await login(client, "pending@x.com")
    r = await client.get(doctor_page(patient.id), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/en/doctor"
    assert await access_rows(db) == []


# ── The doctor's page ────────────────────────────────────────────────────
async def test_a_doctor_sees_what_the_patient_shared_and_no_file_links(
    client, db, doctor, patient, story
):
    await grant(
        db,
        patient,
        grantee_type="doctor",
        grantee_id=doctor.id,
        sections=["visits", "lab_reports", "prescriptions"],
    )
    await login(client, "dr@x.com")
    r = await client.get(doctor_page(patient.id))
    assert r.status_code == 200
    assert cards(r.text) == [
        "Prescription uploaded &amp; digitised",
        "Consultation — Endocrinology follow-up",
        "Prescription issued",
        "Lab report",
        "Lab report",
    ]
    assert in_html("timeline.subtitle_doctor") in r.text
    assert (
        in_html("timeline.prov.ai_patient_confirmed") in r.text
    )  # the doctor is told, in the doctor's words
    assert in_html("timeline.prov.ai_you_confirmed") not in r.text
    assert f'href="/en/doctor/patient/{patient.id}/timeline/encounter/{story.visit.id}"' in r.text
    for private in (
        "Peanuts",
        "Asthma",
        "Vaccination",
        "/patient/documents",
        "/file",
        "Upload a document",
    ):
        assert private not in r.text
    assert 'data-timeline-state="pending"' not in r.text  # the patient's own uploads
    assert sorted(a.resource_type for a in await access_rows(db)) == [
        "section.lab_reports",
        "section.prescriptions",
        "section.visits",
    ]


async def test_a_doctors_detail_page(client, db, doctor, patient, story):
    await grant(db, patient, grantee_type="doctor", grantee_id=doctor.id, sections=["visits"])
    await login(client, "dr@x.com")
    r = await client.get(doctor_page(patient.id, f"/encounter/{story.visit.id}"))
    assert r.status_code == 200 and "Review again in three months." in r.text
    assert f'href="/en/doctor/patient/{patient.id}/timeline"' in r.text
    assert "/patient/documents" not in r.text
    [row] = await access_rows(db)
    assert row.resource_type == "section.visits"
    # an event in a section the patient did not share is not found — and is not a different page
    assert (
        await client.get(doctor_page(patient.id, f"/prescription/{story.rx.id}"))
    ).status_code == 404


@pytest.mark.parametrize("case", ["none", "missing", "garbage"])
async def test_a_doctor_who_may_not_see_gets_the_same_page_as_for_a_patient_who_does_not_exist(
    client, db, doctor, patient, story, case
):
    target = {"none": str(patient.id), "missing": str(uuid7()), "garbage": "not-an-id"}[case]
    await login(client, "dr@x.com")
    r = await client.get(f"/en/doctor/patient/{target}/timeline")
    d = await client.get(f"/en/doctor/patient/{target}/timeline/encounter/{story.visit.id}")
    assert (r.status_code, d.status_code) == (404, 404)
    assert in_html("lookup.not_found") in r.text and in_html("lookup.not_found") in d.text
    assert patient.passport_no not in r.text and "Endocrinology" not in r.text
    assert await access_rows(db) == []


async def test_the_doctors_patient_page_links_to_the_timeline_and_lists_visits_and_lab_reports(
    client, db, doctor, patient, story
):
    await grant(
        db, patient, grantee_type="doctor", grantee_id=doctor.id, sections=["visits", "lab_reports"]
    )
    await login(client, "dr@x.com")
    r = await client.get(f"/en/doctor/patient/{patient.id}")
    assert r.status_code == 200
    assert f'href="/en/doctor/patient/{patient.id}/timeline"' in r.text
    assert in_html("doctor_patient.timeline") in r.text
    # the sections that used to read "nothing recorded" now list what the record holds
    assert "Type 2 diabetes mellitus" in r.text and "Sana Malik" in r.text
    assert in_html("doctor_patient.on", date="2026-07-28") in r.text
    assert in_html("doctor_patient.on", date="2026-06-15") in r.text
    assert "Glucophage" not in r.text  # prescriptions were not shared


# ── The Medical Passport / QR workflow ───────────────────────────────────
async def test_scanning_the_passport_leads_a_doctor_to_the_timeline_through_consent(
    client, db, doctor, patient, story
):
    """The QR holds only the passport number. A scanner types it into the doctor's lookup; from
    there the doctor reaches the patient's page and the timeline — and only what was shared."""
    await grant(
        db, patient, grantee_type="doctor", grantee_id=doctor.id, sections=["visits", "lab_reports"]
    )
    await login(client, "dr@x.com")
    r = await client.post(
        "/en/doctor/passport-lookup",
        data={"passport_no": patient.passport_no},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == f"/en/doctor/patient/{patient.id}"
    page = await client.get(r.headers["location"])
    link = re.search(r'href="([^"]*/timeline)"', page.text).group(1)
    assert link == doctor_page(patient.id)
    timeline = await client.get(link)
    assert timeline.status_code == 200
    assert cards(timeline.text) == [
        "Consultation — Endocrinology follow-up",
        "Lab report",
        "Lab report",
    ]


async def test_a_scan_of_a_patient_who_shared_nothing_never_reaches_the_timeline(
    client, db, doctor, patient, story
):
    await login(client, "dr@x.com")
    r = await client.post("/en/doctor/passport-lookup", data={"passport_no": patient.passport_no})
    assert r.status_code == 404 and in_html("lookup.not_found") in r.text
    assert "timeline" not in r.text.split("<main", 1)[1]
    assert (await client.get(doctor_page(patient.id))).status_code == 404
    assert await access_rows(db) == []


# ── Cards ────────────────────────────────────────────────────────────────
async def test_an_undated_card_says_when_it_was_uploaded_without_repeating_the_heading(
    client, db, patient, story
):
    await login(client, "pat@x.com")
    html = (await client.get(PAGE)).text
    section = html.split(in_html("timeline.undated.title"), 1)[1]
    assert in_html("timeline.uploaded_on", date="05 Oct 2026") in section
    # the date column of an undated card holds the upload day, not the heading's words again
    columns = re.findall(r'<div class="tl-item__when">(.*?)</div>', section, flags=re.DOTALL)
    assert columns and all(in_html("timeline.date_unknown") not in c for c in columns)


async def test_a_single_lab_result_is_not_called_results(client, db, patient, story):
    await login(client, "pat@x.com")
    html = (await client.get(PAGE, params={"type": "lab"})).text
    assert in_html("timeline.summary.result_one_flagged") in html
    assert "1 results" not in html


# ── The label on a document the patient uploaded, reviewed and saved ─────
async def test_every_document_the_patient_uploaded_and_confirmed_reads_reviewed_and_confirmed(
    client, db, doctor, patient, story
):
    # an upload still waiting for review, and one that failed: neither can carry the label
    await add_document(
        db,
        patient,
        doc_type="rx",
        uploaded_by=patient.user_id,
        status="awaiting_review",
        rows=(),
        reviewed=False,
    )
    await add_document(
        db, patient, doc_type="rx", uploaded_by=patient.user_id, status="failed", rows=()
    )
    await login(client, "pat@x.com")
    page = (await client.get(PAGE)).text
    new, old = (
        in_html("timeline.prov.reviewed_confirmed"),
        in_html("timeline.prov.ai_you_confirmed"),
    )

    # every card of a document the patient uploaded, read and confirmed carries it — the
    # prescription, both lab reports and the vaccination record — and the old wording is nowhere
    cards = re.findall(r'<article class="tl-card".*?</article>', page, flags=re.DOTALL)
    confirmed = [c for c in cards if new in c]
    assert page.count('class="tl-prov tl-prov--confirmed"') == 4 == len(confirmed)
    assert old not in page and "AI-generated" not in page
    titles = [
        re.search(r'class="tl-card__title"[^>]*>(.*?)</h3>', c, re.DOTALL).group(1)
        for c in confirmed
    ]
    assert sorted(titles) == sorted(
        ["Prescription uploaded &amp; digitised", "Lab report", "Lab report", "Vaccination record"]
    )
    rx_card = next(c for c in confirmed if "Prescription uploaded" in c)
    assert "Metformin 500 mg" in rx_card and in_html("timeline.view_details") in rx_card
    # ...and nothing else moved: what a doctor, a clinician or the patient themself wrote keeps its
    # own label, and the counts and links stay
    for other in (
        "timeline.prov.doctor_confirmed",
        "timeline.prov.clinician_recorded",
        "timeline.prov.patient_entered",
    ):
        assert in_html(other) in page, other
    assert not any(new in c for c in cards if "Consultation" in c or "Prescription issued" in c)
    assert page.count(in_html("timeline.view_details")) == 8
    # the same pending / failed uploads are listed apart, with no confirmation label of their own
    pending = page.split('data-timeline-state="pending"', 1)[1].split("</section>", 1)[0]
    assert new not in pending and old not in pending


async def test_the_label_follows_the_existing_confirmation_state_in_the_api_too(
    client, db, doctor, patient, story
):
    """The API is unchanged: the event still says WHO vouches for it (`ai_patient_confirmed` — a
    program read it, the patient saved it); only the wording on the patient's own page changed."""
    await login(client, "pat@x.com")
    items = {i["ref"]: i for i in (await client.get("/api/v1/me/timeline")).json()["items"]}
    rx = items[f"rx_document:{story.rx_doc.id}"]
    assert rx["provenance"] == "ai_patient_confirmed"
    assert rx["provenance_label"] == translate("timeline.prov.ai_you_confirmed", "en")
    # a prescription a doctor wrote is a different state, with its own label
    assert items[f"prescription:{story.rx.id}"]["provenance"] == "doctor_confirmed"
    page = (await client.get(PAGE)).text
    doctors_card = next(
        c
        for c in re.findall(r'<article class="tl-card".*?</article>', page, flags=re.DOTALL)
        if "Prescription issued" in c
    )
    assert in_html("timeline.prov.doctor_confirmed") in doctors_card
    assert "tl-prov--confirmed" not in doctors_card


async def test_a_doctor_still_sees_that_it_was_read_by_a_program_and_confirmed_by_the_patient(
    client, db, doctor, patient, story
):
    """ "Reviewed & confirmed" is the patient's own word for their own action; to a doctor it would
    read as a clinician's check, so their wording is unchanged."""
    await grant(
        db, patient, grantee_type="doctor", grantee_id=doctor.id, sections=["prescriptions"]
    )
    await login(client, "dr@x.com")
    for url in (
        doctor_page(patient.id),
        doctor_page(patient.id, f"/rx_document/{story.rx_doc.id}"),
    ):
        text = (await client.get(url)).text
        assert in_html("timeline.prov.ai_patient_confirmed") in text, url
        assert in_html("timeline.prov.reviewed_confirmed") not in text, url
        assert "tl-prov--confirmed" not in text, url


async def test_the_detail_pages_of_a_lab_report_and_a_vaccination_record_say_it_too(
    client, db, patient, story
):
    await login(client, "pat@x.com")
    new, old = (
        in_html("timeline.prov.reviewed_confirmed"),
        in_html("timeline.prov.ai_you_confirmed"),
    )
    for url in (f"{PAGE}/lab_document/{story.lab.id}", f"{PAGE}/document/{story.vaccine.id}"):
        text = (await client.get(url)).text
        assert new in text and old not in text, url
        # who vouches for it is still said in full beside it: a program read it, the patient saved it
        assert in_html("timeline.prov_help.ai_patient_confirmed") in text, url
