"""What an upload costs the database, and what happens when its connection dies underneath it.

From a real dev-server log on a slow route to the database (~1 s per round trip): an upload made
17 round trips, three of them reads of rows it had just written; a connection reset while a
request waited on a slow network call raised from the session cleanup AFTER the response had
gone out; and a failed storage upload was logged with no hint of why. Each is covered here.
"""

from __future__ import annotations

import socket

import pytest
from app.db import session as session_mod
from app.db.models import Document, UserRole
from app.db.session import get_session
from app.deps import Actor
from app.documents import ocr
from app.documents import service as doc_service
from app.documents import storage as doc_storage
from app.errors import FileUnavailable, NotFound, StorageUnavailable
from app.settings import settings
from sqlalchemy import event, select, update
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests import document_fixtures as fx
from tests.conftest import TEST_PASSWORD, make_user

RX_TEXT = (
    "Dr. Mehreen Iqbal\nDate: 11 Aug 2024\n"
    "1. Tab. Metformin 500mg  1-0-1  x 30 days\n2. Tab. Atorvastatin 20mg  0-0-1  x 30 days"
)
PNG = fx.image_bytes(fx.text_page())


class _Ocr:
    name = "tesseract"

    async def transcribe(self, data, mime):
        return RX_TEXT, None


@pytest.fixture(autouse=True)
def _fake_ocr_and_no_rate_limit(monkeypatch):
    monkeypatch.setattr(ocr, "get_engine", lambda layout="page": _Ocr())
    monkeypatch.setattr(settings, "document_write_rate_limit_per_minute", 100_000)


class _Log:
    """Records what a module logs, instead of printing it."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def warning(self, event_name, **fields) -> None:
        self.events.append((event_name, fields))

    def info(self, *_a, **_k) -> None:
        return None

    def error(self, *_a, **_k) -> None:
        return None


class _Trips:
    """Counts the network round trips a block of code costs: every statement and every commit."""

    def __init__(self, engine) -> None:
        self.engine = engine.sync_engine
        self.sql: list[str] = []
        self.commits = 0

    def _statement(self, _conn, _cursor, statement, *_a, **_k) -> None:
        self.sql.append(" ".join(statement.split()))

    def _commit(self, _conn) -> None:
        self.commits += 1

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._statement)
        event.listen(self.engine, "commit", self._commit)
        return self

    def __exit__(self, *_exc) -> None:
        event.remove(self.engine, "before_cursor_execute", self._statement)
        event.remove(self.engine, "commit", self._commit)

    @property
    def total(self) -> int:
        return len(self.sql) + self.commits


async def _signed_in(client, db, email: str = "pat@x.com"):
    user = await make_user(db, email=email, role=UserRole.PATIENT)
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": TEST_PASSWORD})
    assert r.status_code == 200, r.text
    return user


# ── an upload does not read back what it has just written ───────────────────────────────────


async def test_a_web_upload_costs_fourteen_round_trips_or_fewer(client, db, engine):
    """It was 17: the document re-read right after being inserted, re-read after the claim
    commit, and re-read once more at the end — by a handler that ignores the result."""
    await _signed_in(client, db)
    with _Trips(engine) as trips:
        r = await client.post(
            "/en/patient/documents",
            data={"document_type": "prescription"},
            files={"file": ("rx.png", PNG, "image/png")},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert not [s for s in trips.sql if s.startswith("SELECT document.")], trips.sql
    assert trips.total <= 14, (trips.total, trips.sql)


async def test_the_page_after_a_fast_upload_shows_the_document_as_read(client, db):
    await _signed_in(client, db)
    r = await client.post(
        "/en/patient/documents",
        data={"document_type": "prescription"},
        files={"file": ("rx.png", PNG, "image/png")},
        follow_redirects=False,
    )
    page = await client.get(r.headers["location"])
    assert page.status_code == 200
    assert "Metformin" in page.text and "Atorvastatin" in page.text  # the read happened, in order


async def test_the_object_handed_back_matches_the_row_it_stands_for(db, sessionmaker_):
    user = await make_user(db, email="pat@x.com", role=UserRole.PATIENT)
    actor = Actor(user_id=user.id, role="patient")
    doc = await doc_service.create_upload(
        db,
        actor,
        document_type="prescription",
        filename="rx.png",
        declared_mime="image/png",
        data=PNG,
    )
    out = await doc_service.process(db, actor, doc.id, data=PNG, known=doc)
    async with sessionmaker_() as other:
        row = (await other.execute(select(Document).where(Document.id == doc.id))).scalar_one()
    assert out.status == row.status == "awaiting_review"
    for column in ("failure_code", "ocr_text", "document_date", "ocr_mode"):
        assert getattr(out, column) == getattr(row, column), column
    assert "Metformin" in row.ocr_text


async def test_a_handed_over_document_the_session_has_expired_is_read_not_used(db):
    """The pipeline runs on a session that keeps loaded values at commit (`SessionFactory`).
    If one that does not were ever used, the shortcut must step aside and read the row."""
    user = await make_user(db, email="pat@x.com", role=UserRole.PATIENT)
    actor = Actor(user_id=user.id, role="patient")
    doc = await doc_service.create_upload(
        db,
        actor,
        document_type="prescription",
        filename="rx.png",
        declared_mime="image/png",
        data=PNG,
    )
    doc_id = sa_inspect(doc).identity[0]
    db.expire(doc)
    got = await doc_service._owned_or_known(db, actor, doc_id, doc)  # no MissingGreenlet
    assert got.id == doc_id and got.status == "uploaded"


async def test_bringing_a_document_up_to_date_patches_it_in_memory_without_a_read(db, engine):
    user = await make_user(db, email="pat@x.com", role=UserRole.PATIENT)
    actor = Actor(user_id=user.id, role="patient")
    doc = await doc_service.create_upload(
        db,
        actor,
        document_type="prescription",
        filename="rx.png",
        declared_mime="image/png",
        data=PNG,
    )
    with _Trips(engine) as trips:
        await doc_service._bring_up_to_date(db, doc, status="processing", failure_code=None)
    assert doc.status == "processing" and doc.failure_code is None
    assert trips.total == 0  # no round trip
    assert not sa_inspect(doc).modified  # and not queued as a change to write back


async def test_bringing_an_expired_document_up_to_date_reads_it_back_instead(db):
    user = await make_user(db, email="pat@x.com", role=UserRole.PATIENT)
    actor = Actor(user_id=user.id, role="patient")
    doc = await doc_service.create_upload(
        db,
        actor,
        document_type="prescription",
        filename="rx.png",
        declared_mime="image/png",
        data=PNG,
    )
    doc_id = sa_inspect(doc).identity[0]
    await db.execute(
        update(Document)
        .where(Document.id == doc_id)
        .values(status="failed", failure_code="ocr_failed")
        .execution_options(synchronize_session=False)
    )
    db.expire(doc)
    await doc_service._bring_up_to_date(db, doc, status="what-we-assumed")
    assert doc.status == "failed" and doc.failure_code == "ocr_failed"  # the row, not the guess


async def test_a_document_handed_over_is_never_trusted_for_someone_else(db):
    """`known` saves a read; it must not become a way round the ownership check."""
    owner = await make_user(db, email="own@x.com", role=UserRole.PATIENT)
    other = await make_user(db, email="oth@x.com", role=UserRole.PATIENT)
    owner_actor = Actor(user_id=owner.id, role="patient")
    other_actor = Actor(user_id=other.id, role="patient")
    doc = await doc_service.create_upload(
        db,
        owner_actor,
        document_type="prescription",
        filename="rx.png",
        declared_mime="image/png",
        data=PNG,
    )
    with pytest.raises(NotFound):
        await doc_service.process(db, other_actor, doc.id, data=PNG, known=doc)
    row = (await db.execute(select(Document).where(Document.id == doc.id))).scalar_one()
    assert row.status == "uploaded"  # nothing was read for the wrong person


async def test_a_document_handed_over_for_a_different_id_is_ignored(db):
    user = await make_user(db, email="pat@x.com", role=UserRole.PATIENT)
    actor = Actor(user_id=user.id, role="patient")
    first = await doc_service.create_upload(
        db,
        actor,
        document_type="prescription",
        filename="a.png",
        declared_mime="image/png",
        data=PNG,
    )
    second = await doc_service.create_upload(
        db,
        actor,
        document_type="prescription",
        filename="b.png",
        declared_mime="image/png",
        data=PNG,
    )
    out = await doc_service.process(db, actor, second.id, data=PNG, known=first)
    assert out.id == second.id and out.status == "awaiting_review"
    assert (await db.get(Document, first.id)).status == "uploaded"


# ── a storage failure says why (and only that) ──────────────────────────────────────────────


class _StorageApiError(Exception):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def test_a_storage_failure_is_described_by_class_cause_and_status_only():
    err = ConnectionError("could not reach patients/PRIVATE-ID/secret-name.png")
    err.__cause__ = socket.gaierror(11001, "getaddrinfo failed for secret-host.example")
    info = doc_storage.describe_failure(err)
    assert info == {"error": "ConnectionError", "cause": "gaierror"}  # a DNS failure, told apart
    assert "PRIVATE" not in str(info) and "secret" not in str(info)

    refused = _StorageApiError("bucket not found: medical-documents/abc", 404)
    assert doc_storage.describe_failure(refused) == {"error": "_StorageApiError", "status": 404}

    plain = RuntimeError("boom")
    assert doc_storage.describe_failure(plain) == {"error": "RuntimeError"}


def test_describing_a_failure_cannot_loop_on_a_cause_cycle():
    a, b = RuntimeError("a"), ValueError("b")
    a.__cause__, b.__cause__ = b, a
    assert doc_storage.describe_failure(a)["error"] == "RuntimeError"


async def test_each_storage_failure_logs_its_reason(db, fake_supabase, monkeypatch):
    user = await make_user(db, email="pat@x.com", role=UserRole.PATIENT)
    actor = Actor(user_id=user.id, role="patient")
    seen = _Log()
    monkeypatch.setattr(doc_service, "log", seen)

    fake_supabase.storage.fail_put = True  # the upload cannot be stored
    with pytest.raises(StorageUnavailable):
        await doc_service.create_upload(
            db,
            actor,
            document_type="prescription",
            filename="rx.png",
            declared_mime="image/png",
            data=PNG,
        )
    fake_supabase.storage.fail_put = False

    doc = await doc_service.create_upload(
        db,
        actor,
        document_type="prescription",
        filename="rx.png",
        declared_mime="image/png",
        data=PNG,
    )
    fake_supabase.storage.fail_get = True  # ...and the original cannot be fetched back
    await doc_service.process(db, actor, doc.id)  # becomes a retryable `failed`, not an error
    with pytest.raises(FileUnavailable):
        await doc_service.get_file(db, actor, doc.id)

    assert ("document_storage_put_failed", {"error": "RuntimeError"}) in seen.events
    assert seen.events.count(("document_storage_get_failed", {"error": "RuntimeError"})) == 2


async def test_a_failed_cleanup_delete_logs_its_reason_too(monkeypatch):
    seen = _Log()
    monkeypatch.setattr(doc_storage, "log", seen)

    async def unreachable():
        raise ConnectionError("no route to host")

    monkeypatch.setattr(doc_storage, "_bucket", unreachable)
    await doc_storage.delete("patients/x/y.png")  # best-effort: never raises
    assert seen.events == [("document_storage_delete_failed", {"error": "ConnectionError"})]


# ── a dead connection at cleanup is logged, not raised after the response ───────────────────


def _dead_connection() -> DBAPIError:
    return DBAPIError(
        "ROLLBACK",
        {},
        Exception("connection was closed in the middle of operation"),
        connection_invalidated=True,
    )


async def test_closing_a_session_on_a_dead_connection_is_logged_not_raised(monkeypatch):
    class Dead:
        async def close(self) -> None:
            raise _dead_connection()

    seen = _Log()
    monkeypatch.setattr(session_mod, "SessionFactory", lambda: Dead())
    monkeypatch.setattr(session_mod, "log", seen)

    generator = get_session()
    await generator.__anext__()
    with pytest.raises(StopAsyncIteration):  # finished normally
        await generator.__anext__()
    assert seen.events == [("session_close_failed", {"error": "DBAPIError"})]


async def test_an_error_from_the_request_itself_is_not_hidden_by_a_failing_close(monkeypatch):
    class Dead:
        async def close(self) -> None:
            raise _dead_connection()

    monkeypatch.setattr(session_mod, "SessionFactory", lambda: Dead())
    monkeypatch.setattr(session_mod, "log", _Log())
    generator = get_session()
    await generator.__anext__()
    with pytest.raises(ValueError, match="the request's own bug"):
        await generator.athrow(ValueError("the request's own bug"))


async def test_a_response_is_not_followed_by_a_crash_when_the_connection_dies_at_cleanup(
    app, client, db, engine, monkeypatch
):
    """The real log: 'Caught handled exception, but response already started'. The page was
    served; only the cleanup afterwards failed, on a connection the network had reset."""
    await _signed_in(client, db)

    class DiesOnClose(AsyncSession):
        async def close(self) -> None:
            await super().close()
            raise _dead_connection()

    app.dependency_overrides.pop(get_session, None)  # use the real dependency
    monkeypatch.setattr(
        session_mod,
        "SessionFactory",
        async_sessionmaker(engine, class_=DiesOnClose, expire_on_commit=False),
    )
    monkeypatch.setattr(session_mod, "log", _Log())
    r = await client.get("/api/v1/documents")  # would re-raise here if the cleanup error escaped
    assert r.status_code == 200 and r.json() == []
