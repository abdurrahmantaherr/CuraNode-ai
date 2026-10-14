"""Live status on the document pages: the contract the in-page poller relies on.

The poller itself (`frontend/static/js/doc-status.js`) is tested in `tests/js/doc_status.test.mjs`
(run below when Node is installed). What belongs HERE is everything that script trusts the
server for: the API answers it polls, the markup hooks it reads, the messages it announces, and
the failure pages whose text it shows. Nothing here needs a browser.
"""

# ruff: noqa: F811 -- fixtures imported from the document suite are then used as test arguments
from __future__ import annotations

import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from app.db.models import Document, OcrJob
from app.db.types import utcnow
from app.documents import ocr
from app.i18n.catalogue import missing_keys, translate
from sqlalchemy import update

# Reuse the document suite's harness (fake OCR, fake storage, a signed-in patient).
from tests.test_documents import (  # noqa: F401
    PNG,
    fake_ocr,
    no_real_gemini,
    patient_user,
    second_patient,
    upload,
    upload_and_process,
)

ROOT = Path(__file__).resolve().parent.parent
STATIC_JS = ROOT / "frontend" / "static" / "js"


async def make_processing(db, doc_id: str) -> None:
    await db.execute(
        update(Document).where(Document.id == uuid.UUID(doc_id)).values(status="processing")
    )
    db.add(
        OcrJob(
            id=uuid.uuid4(),
            document_id=uuid.UUID(doc_id),
            status="running",
            engine="tesseract",
            started_at=utcnow(),
        )
    )
    await db.commit()


# ── The API the poller asks ──────────────────────────────────────────────
async def test_the_status_endpoint_reports_the_real_state_at_every_step(
    client, db, fake_ocr, patient_user
):
    up = await upload(client)
    doc_id = up.json()["id"]
    assert (await client.get(f"/api/v1/documents/{doc_id}")).json()["status"] == "uploaded"

    await make_processing(db, doc_id)
    body = (await client.get(f"/api/v1/documents/{doc_id}")).json()
    assert body["status"] == "processing" and body["failure_code"] is None

    await db.execute(
        update(Document).where(Document.id == uuid.UUID(doc_id)).values(status="awaiting_review")
    )
    await db.commit()
    assert (await client.get(f"/api/v1/documents/{doc_id}")).json()["status"] == "awaiting_review"


async def test_a_processing_failure_is_reported_with_its_code_and_a_safe_retry(
    client, fake_ocr, patient_user
):
    fake_ocr.error = ocr.OCR_EMPTY
    up = await upload(client)
    doc_id = up.json()["id"]
    done = await client.post(f"/api/v1/documents/{doc_id}/process")
    assert done.status_code == 200

    body = (await client.get(f"/api/v1/documents/{doc_id}")).json()
    assert body["status"] == "failed" and body["failure_code"] == ocr.OCR_EMPTY
    assert body["can_retry"] is True  # the page may offer "Try again"

    # Retrying after the cause is gone creates no second document and reads once more.
    fake_ocr.error = None
    again = await client.post(f"/api/v1/documents/{doc_id}/process")
    assert again.json()["status"] == "awaiting_review"
    assert len((await client.get("/api/v1/documents/in-progress")).json()) == 1


async def test_a_retry_of_a_document_already_read_runs_no_second_read(
    client, fake_ocr, patient_user
):
    detail = await upload_and_process(client, fake_ocr)
    for _ in range(3):  # a double-click, a retry button pressed again
        again = await client.post(f"/api/v1/documents/{detail['id']}/process")
        assert again.status_code == 200 and again.json()["status"] == "awaiting_review"
    assert len(fake_ocr.calls) == 1


async def test_a_deleted_document_ends_the_poll_and_an_unknown_one_is_a_404(
    client, db, fake_ocr, patient_user
):
    up = await upload(client)
    doc_id = up.json()["id"]
    assert (await client.delete(f"/api/v1/documents/{doc_id}")).status_code in (200, 204)

    # A deleted document stays as an empty "cancelled" tombstone, so the poll gets a normal
    # answer whose status is no longer "processing": the page swaps to the discarded notice.
    assert (await client.get(f"/api/v1/documents/{doc_id}")).json()["status"] == "cancelled"
    # An id that does not exist (or is not the caller's) is a 404 — the poller stops and says so.
    unknown = await client.get(f"/api/v1/documents/{uuid.uuid4()}")
    assert unknown.status_code == 404


async def test_another_patients_document_cannot_be_followed(
    client, app, db, fake_ocr, patient_user, second_patient
):
    from httpx import ASGITransport, AsyncClient

    from tests.test_documents import login

    doc_id = (await upload(client)).json()["id"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as other:
        await login(other, "other@x.com")
        foreign = await other.get(f"/api/v1/documents/{doc_id}")
        unknown = await other.get(f"/api/v1/documents/{uuid.uuid4()}")
    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json()["error"]["code"] == unknown.json()["error"]["code"]


async def test_a_signed_out_poll_is_a_401_not_a_login_page(client, app, patient_user):
    up = await upload(client)
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as anon:
        r = await anon.get(f"/api/v1/documents/{up.json()['id']}")
    assert r.status_code == 401  # the poller stops on this and tells the person to sign in


async def test_several_documents_being_read_at_once_each_report_their_own_status(
    client, db, fake_ocr, patient_user
):
    ids = [(await upload(client, name=f"scan-{n}.png")).json()["id"] for n in range(3)]
    await make_processing(db, ids[0])
    await make_processing(db, ids[1])
    listed = {
        d["id"]: d["status"] for d in (await client.get("/api/v1/documents/in-progress")).json()
    }
    assert listed[ids[0]] == listed[ids[1]] == "processing"
    assert listed[ids[2]] == "uploaded"


# ── The pages' hooks ─────────────────────────────────────────────────────
async def test_the_detail_page_polls_only_while_the_document_is_being_read(
    client, db, fake_ocr, patient_user
):
    up = await upload(client)
    doc_id = up.json()["id"]
    page = (await client.get(f"/en/patient/documents/{doc_id}")).text
    assert "data-doc-poll" not in page  # uploaded: waiting for the person, nothing to follow

    await make_processing(db, doc_id)
    page = (await client.get(f"/en/patient/documents/{doc_id}")).text
    assert f'data-doc-poll="/api/v1/documents/{doc_id}"' in page
    assert "data-autorefresh" not in page  # the full-page reload loop is gone
    assert "/static/js/doc-status.js" in page and "/static/js/documents.js" in page

    await db.execute(
        update(Document).where(Document.id == uuid.UUID(doc_id)).values(status="awaiting_review")
    )
    await db.commit()
    assert "data-doc-poll" not in (await client.get(f"/en/patient/documents/{doc_id}")).text


async def test_the_processing_panel_is_not_a_live_region_so_checks_are_not_read_out(
    client, db, fake_ocr, patient_user
):
    up = await upload(client)
    doc_id = up.json()["id"]
    await make_processing(db, doc_id)
    page = (await client.get(f"/en/patient/documents/{doc_id}")).text

    panel = re.search(r'<div class="panel state-block"([^>]*)>', page)
    assert panel and "aria-live" not in panel.group(1) and "role=" not in panel.group(1)
    # One persistent, polite, atomic live region outside <main>, written to only on a change.
    region = re.search(r'<div id="doc-live"([^>]*)>', page).group(1)
    assert (
        'role="status"' in region
        and 'aria-live="polite"' in region
        and 'aria-atomic="true"' in region
    )
    assert page.index('id="doc-live"') < page.index('<main class="content"')
    # The "last checked" line and the trouble notice are plain text with a real action.
    assert "data-doc-checked" in page and "data-doc-problem" in page
    assert "data-doc-check-now" in page


async def test_the_page_carries_every_message_the_script_announces_in_the_right_language(
    client, db, fake_ocr, patient_user
):
    up = await upload(client)
    doc_id = up.json()["id"]
    await make_processing(db, doc_id)
    en = (await client.get(f"/en/patient/documents/{doc_id}")).text
    ur = (await client.get(f"/ur/patient/documents/{doc_id}")).text
    for attr in (
        "temporary",
        "recovered",
        "slow",
        "signed-out",
        "gone",
        "changed",
        "done",
        "swap-failed",
        "checked",
    ):
        assert f"data-msg-{attr}=" in en and f"data-msg-{attr}=" in ur
    assert 'data-label-awaiting_review="Needs your review"' in en
    assert (
        f'data-label-awaiting_review="{translate("documents.status.awaiting_review", "ur")}"' in ur
    )
    assert 'dir="rtl"' in ur


async def test_the_slow_read_offers_a_retry_the_server_still_decides(
    client, db, fake_ocr, patient_user
):
    up = await upload(client)
    doc_id = up.json()["id"]
    await make_processing(db, doc_id)
    page = (await client.get(f"/en/patient/documents/{doc_id}")).text
    slow = re.search(r"<div data-doc-slow hidden>.*?</form>", page, re.DOTALL).group(0)
    assert f'action="/en/patient/documents/{doc_id}/process"' in slow  # the same safe endpoint

    # Not stale yet: the server refuses a second read, and the page just shows the state.
    r = await client.post(f"/en/patient/documents/{doc_id}/process", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith(doc_id)
    assert fake_ocr.calls == []


async def test_the_list_follows_documents_in_place_and_keeps_the_filter_form_out_of_the_swap(
    client, db, fake_ocr, patient_user
):
    first = (await upload(client, name="scan-a.png")).json()["id"]
    await make_processing(db, first)
    page = (await client.get("/en/patient/documents?q=scan")).text

    assert 'data-doc-list-poll="/api/v1/documents/in-progress?limit=100"' in page
    assert "data-autorefresh" not in page
    row = re.search(
        r'<li class="entry-row" data-doc-id="([^"]+)" data-doc-status="processing"', page
    )
    assert row and row.group(1) == first
    # Only the two result blocks are swapped; the search form sits before both of them.
    form_at = page.index('class="panel stack filter-form"')
    assert (
        form_at
        < page.index('data-live-region="in-progress"')
        < page.index('data-live-region="records"')
    )
    form_html = page[form_at : page.index("</form>", form_at)]
    assert "data-live-region" not in form_html and 'value="scan"' in form_html


async def test_the_list_does_not_poll_when_nothing_is_being_read(client, fake_ocr, patient_user):
    await upload_and_process(client, fake_ocr)
    page = (await client.get("/en/patient/documents")).text
    assert "data-doc-list-poll" not in page and "data-autorefresh" not in page
    assert 'data-live-region="in-progress"' in page  # the blocks exist, ready for a later swap


# ── Upload failures the page explains without losing the form ────────────
async def test_upload_page_has_a_recovery_notice_and_the_messages_for_it(client, patient_user):
    page = (await client.get("/en/patient/documents/new")).text
    notice = re.search(
        r'<div class="banner banner--error" id="upload-problem"([^>]*)>', page
    ).group(1)
    assert 'role="alert"' in notice and "hidden" in notice  # announced when shown, words + icon
    for attr in ("failed", "network", "unconfirmed", "uploading"):
        assert f"data-msg-{attr}=" in page
    assert 'id="upload-problem-list"' in page  # the way to check for an already-saved copy
    assert 'id="doc-live"' in page


@pytest.mark.parametrize(
    ("kwargs", "expected_id"),
    [
        ({"data": {"document_type": "prescription"}}, "e-file"),  # no file chosen
        (
            {
                "data": {"document_type": "prescription"},
                "files": {"file": ("notes.txt", b"hello", "text/plain")},
            },
            "e-file",
        ),
    ],
)
async def test_a_refused_upload_answers_with_the_page_and_a_visible_file_error(
    client, patient_user, kwargs, expected_id
):
    """`documents.js` reads this page's `#e-file` / `#e-file-text` to show the reason next to the
    file the person already chose — so they must be there, and visible, on a refusal."""
    r = await client.post("/en/patient/documents", **kwargs)
    assert r.status_code in (415, 422) and not r.is_redirect
    box = re.search(r'<span class="field-error" id="e-file"([^>]*)>', r.text)
    assert box and "hidden" not in box.group(1)
    assert re.search(r'id="e-file-text">[^<]+<', r.text)


async def test_a_refused_upload_creates_no_document(client, patient_user):
    await client.post(
        "/en/patient/documents",
        data={"document_type": "prescription"},
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert (await client.get("/api/v1/documents/in-progress")).json() == []


async def test_a_successful_upload_answers_with_a_redirect_to_the_documents_own_page(
    client, fake_ocr, patient_user
):
    r = await client.post(
        "/en/patient/documents",
        data={"document_type": "prescription"},
        files={"file": ("rx.png", PNG, "image/png")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert re.fullmatch(r"/en/patient/documents/[0-9a-f-]{36}", r.headers["location"])


async def test_a_dropped_connection_can_be_checked_against_the_server(
    client, fake_ocr, patient_user
):
    """After a network failure the script looks for a document with the same name and size that
    was just created, instead of asking for a second upload. That lookup is this exact request."""
    up = (await upload(client, name="lab.png")).json()
    found = [
        d
        for d in (await client.get("/api/v1/documents/in-progress?limit=50")).json()
        if d["original_filename"] == "lab.png" and d["size_bytes"] == len(PNG)
    ]
    assert [d["id"] for d in found] == [up["id"]] and found[0]["uploaded_at"]


# ── Catalogue and static files ───────────────────────────────────────────
def test_every_live_status_message_exists_in_both_languages_with_its_placeholders():
    assert missing_keys() == {"ur": []}
    for key, tokens in {
        "documents.live.changed": ("{name}", "{status}"),
        "documents.live.done": ("{status}",),
        "documents.live.checked": ("{time}",),
    }.items():
        for locale in ("en", "ur"):
            text = translate(key, locale)
            assert text != key and all(token in text for token in tokens), (key, locale)


def test_no_completion_estimate_is_promised_on_the_waiting_screens():
    for key in ("documents.upload.processing_hint", "documents.processing.hint"):
        for locale in ("en", "ur"):
            assert not re.search(r"\d", translate(key, locale)), (key, locale)


def test_the_documents_script_no_longer_reloads_the_page_on_a_timer():
    source = (STATIC_JS / "documents.js").read_text(encoding="utf-8")
    assert "data-autorefresh" not in source
    assert not re.search(r"setTimeout\(\s*function\s*\(\)\s*\{\s*window\.location\.reload", source)


def test_the_queue_page_keeps_its_own_refresh_untouched():
    assert "data-autorefresh" in (STATIC_JS / "queue.js").read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_the_poller_unit_tests_pass_under_node():
    run = subprocess.run(
        ["node", "--test", str(ROOT / "tests" / "js" / "doc_status.test.mjs")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert run.returncode == 0, run.stdout[-2000:] + run.stderr[-2000:]
