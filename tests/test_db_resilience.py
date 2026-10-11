"""When the database cannot be reached, and how many times it is asked.

Both come from one real incident on a dev machine with a slow route to a far-away pooler: a new
database connection hung for asyncpg's default 60 s and then the page answered with a bare JSON
500. Now a new connection gives up after `DATABASE_CONNECT_TIMEOUT_S`, the failure is a 503 with
a plain "try again" (a page for a browser, the usual envelope for the API) and a real bug is
still a 500. Each page also looked the patient's id up several times — each one a network round
trip — and now does it once per request.
"""

from __future__ import annotations

import errno
import socket
import sys
import uuid

import pytest
from app.db.models import UserRole
from app.db.session import engine_options, get_session
from app.deps import Actor
from app.documents import service as doc_service
from app.i18n.catalogue import translate
from app.settings import Settings
from app.web.unavailable import is_database_unavailable
from httpx import ASGITransport, AsyncClient
from markupsafe import escape
from sqlalchemy import event
from sqlalchemy.exc import DBAPIError, IntegrityError, InterfaceError, OperationalError

from tests.conftest import TEST_PASSWORD, make_user

# ── the engine's connect timeout ─────────────────────────────────────────────────────────────


def test_a_new_postgres_connection_gives_up_after_the_configured_time():
    direct = engine_options("postgresql+asyncpg://u:p@db.x.supabase.co:5432/postgres", 15)
    assert direct["connect_args"] == {"timeout": 15}
    assert direct["pool_size"] == 20 and direct["max_overflow"] == 10


def test_the_pooler_keeps_its_statement_cache_off_and_gets_the_timeout_too():
    for url in (
        "postgresql+asyncpg://u:p@aws-0-ap-southeast-2.pooler.supabase.com:5432/postgres",
        "postgresql+asyncpg://u:p@somewhere:6543/postgres",
    ):
        assert engine_options(url, 10)["connect_args"] == {"timeout": 10, "statement_cache_size": 0}


def test_sqlite_gets_no_postgres_options_and_a_silly_timeout_is_floored():
    assert engine_options("sqlite+aiosqlite:///:memory:", 15) == {}
    assert engine_options("postgresql+asyncpg://u:p@h:5432/db", 0)["connect_args"]["timeout"] == 1


def test_the_default_is_below_asyncpgs_sixty_seconds_but_not_so_short_that_a_slow_link_fails():
    """15 s failed to START the server on a slow route (the TLS handshake alone took 10 s on a
    bad moment); asyncpg's own 60 s turned a blip into a minute-long hang."""
    settings = Settings(_env_file=None)
    assert 20 <= settings.database_connect_timeout_s < 60
    assert settings.database_idle_ping_s > 0


# ── an unreachable database is a 503, not a bare 500 ─────────────────────────────────────────


class _Down:
    """A session whose first use fails the way a dead network does."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def get(self, *_a, **_k):
        raise self.exc

    async def execute(self, *_a, **_k):
        raise self.exc

    async def commit(self) -> None:
        raise self.exc

    async def rollback(self) -> None:
        return None

    async def close(self) -> None:
        return None


def _outage(app, exc: BaseException) -> None:
    async def down():
        yield _Down(exc)

    app.dependency_overrides[get_session] = down


async def _signed_in(client, db, email: str = "pat@x.com") -> None:
    await make_user(db, email=email, role=UserRole.PATIENT)
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": TEST_PASSWORD})
    assert r.status_code == 200, r.text


_FAILURES = [
    pytest.param(TimeoutError(), id="connect-timeout"),
    pytest.param(ConnectionRefusedError(), id="refused"),
    pytest.param(ConnectionResetError(), id="reset"),
    pytest.param(socket.gaierror("no such host"), id="dns"),
    pytest.param(
        OperationalError("SELECT secret_column FROM t", {"ssn": "PII-VALUE"}, Exception("x")),
        id="operational",
    ),
    pytest.param(InterfaceError("SELECT 1", {}, Exception("x")), id="interface"),
    # a plain OSError that is still a network failure (not a TimeoutError / ConnectionError)
    pytest.param(OSError(errno.ENETUNREACH, "Network is unreachable"), id="network-unreachable"),
    pytest.param(OSError(errno.EHOSTUNREACH, "No route to host"), id="host-unreachable"),
    pytest.param(
        DBAPIError("SELECT 1", {}, Exception("x"), connection_invalidated=True),
        id="connection-invalidated",
    ),
]


@pytest.mark.parametrize("failure", _FAILURES)
async def test_the_api_answers_503_with_the_usual_envelope(app, client, db, failure):
    await _signed_in(client, db)
    _outage(app, failure)
    r = await client.get("/api/v1/documents")
    assert r.status_code == 503
    error = r.json()["error"]
    assert error["code"] == "SERVICE_UNAVAILABLE" and error["retryable"] is True
    assert error["message"] == translate("errors.service_unavailable", "en")
    assert r.headers["retry-after"] == "5" and r.headers["cache-control"] == "no-store"
    assert r.headers["x-request-id"] == error["request_id"]  # still correlated
    assert r.headers["x-content-type-options"] == "nosniff"  # the usual hardening still applies
    # nothing about the failure leaks: no SQL, no parameter values
    assert "secret_column" not in r.text and "PII-VALUE" not in r.text


@pytest.mark.parametrize("failure", _FAILURES)
async def test_a_browser_gets_a_page_not_json(app, client, db, failure):
    await _signed_in(client, db)
    _outage(app, failure)
    r = await client.get("/en/patient/documents")
    assert r.status_code == 503
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["retry-after"] == "5" and r.headers["cache-control"] == "no-store"
    page = r.text
    assert escape(translate("errors.unavailable.title", "en")) in page
    assert escape(translate("errors.service_unavailable", "en")) in page
    assert '"error"' not in page and "Traceback" not in page and "secret_column" not in page
    assert r.headers["x-request-id"] in page  # something to quote when asking for help
    # trying again is a link back to the very page that failed
    assert 'href="/en/patient/documents"' in page


async def test_the_try_again_link_keeps_the_query_and_cannot_be_used_to_inject_markup(
    app, client, db
):
    await _signed_in(client, db)
    _outage(app, TimeoutError())
    r = await client.get("/en/patient/documents?q=abc&status=failed")
    assert 'href="/en/patient/documents?q=abc&amp;status=failed"' in r.text
    hostile = await client.get('/en/patient/documents?q="><script>alert(1)</script>')
    assert hostile.status_code == 503
    assert "<script>alert(1)</script>" not in hostile.text


async def test_the_urdu_page_is_urdu_and_right_to_left(app, client, db):
    await _signed_in(client, db)
    _outage(app, TimeoutError())
    r = await client.get("/ur/patient/documents")
    assert r.status_code == 503
    assert 'dir="rtl"' in r.text and 'lang="ur"' in r.text
    assert escape(translate("errors.unavailable.title", "ur")) in r.text
    assert 'href="/ur/patient/documents"' in r.text


async def test_a_failed_form_submission_does_not_claim_to_be_safe_to_replay(app, client, db):
    """A GET can simply be reloaded. A POST might have been applied before the connection
    dropped, so the page says so and offers the way home, never a link that repeats it."""
    await _signed_in(client, db)
    _outage(app, TimeoutError())
    r = await client.post(f"/en/patient/documents/{uuid.uuid4()}/cancel", follow_redirects=False)
    assert r.status_code == 503
    assert escape(translate("errors.unavailable.unsure", "en")) in r.text
    assert 'href="/en/login"' in r.text
    assert "btn--primary" not in r.text  # no "try again" button for a write
    # ...and the way home must exist (it was `/en`, a 404, until a real click found out)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as visitor:
        assert (await visitor.get("/en/login")).status_code == 200


async def test_the_reference_on_the_page_is_in_the_log(app, client, db, monkeypatch):
    """The page shows a reference; the log line for the same failure carries it, so a report of
    "T1E09VA..." can be matched to what actually happened (the class of the failure, the path)."""
    from app.web import unavailable

    events: list[tuple[str, dict]] = []

    class Recorder:
        def warning(self, name, **fields):
            events.append((name, fields))

    monkeypatch.setattr(unavailable, "log", Recorder())
    await _signed_in(client, db)
    _outage(app, socket.gaierror(11001, "getaddrinfo failed"))
    r = await client.post(f"/en/patient/documents/{uuid.uuid4()}/cancel", follow_redirects=False)
    reference = r.headers["x-request-id"]
    assert reference in r.text
    assert events == [
        (
            "database_unavailable",
            {
                "error": "gaierror",
                "path": events[0][1]["path"],
                "method": "POST",
                "request_id": reference,
            },
        )
    ]


async def test_a_real_bug_is_still_a_500_not_hidden_as_an_outage(app, db, monkeypatch):
    async def boom(*_a, **_k):
        raise ValueError("a genuine bug")

    monkeypatch.setattr(doc_service, "count_finalized", boom)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="https://test") as c:
        await _signed_in(c, db)
        r = await c.get("/api/v1/documents")
    assert r.status_code == 500 and r.json()["error"]["code"] == "INTERNAL_ERROR"


@pytest.mark.skipif(sys.platform != "win32", reason="WinError codes exist only on Windows")
@pytest.mark.parametrize("winerror", [121, 10051, 10060, 1231])
def test_the_windows_errors_a_stalled_connection_really_raises_count_as_an_outage(winerror):
    """`OSError: [WinError 121] The semaphore timeout period has expired` is what a connect that
    stalls on Windows raises — a plain OSError, neither a TimeoutError nor a ConnectionError."""
    exc = OSError(0, "stalled", None, winerror)
    if winerror != 10060:  # Python maps WSAETIMEDOUT itself; the others stay a plain OSError
        assert type(exc) is OSError
    assert is_database_unavailable(exc)


@pytest.mark.parametrize(
    "bug",
    [
        pytest.param(ValueError("x"), id="value-error"),
        pytest.param(KeyError("x"), id="key-error"),
        pytest.param(FileNotFoundError(errno.ENOENT, "No such file"), id="file-not-found"),
        pytest.param(PermissionError(errno.EACCES, "denied"), id="permission"),
        pytest.param(IntegrityError("INSERT", {}, Exception("dup")), id="integrity"),
        pytest.param(DBAPIError("SELECT", {}, Exception("x")), id="plain-dbapi"),
    ],
)
async def test_what_is_not_an_outage_is_not_dressed_up_as_one(app, db, bug):
    assert not is_database_unavailable(bug)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="https://test") as c:
        await _signed_in(c, db)
        _outage(app, bug)
        r = await c.get("/api/v1/documents")
    assert r.status_code == 500 and r.json()["error"]["code"] == "INTERNAL_ERROR"


async def test_other_errors_keep_their_own_status(app, client, db):
    """The new handlers must not swallow the app's own errors."""
    r = await client.get("/api/v1/documents")  # not signed in
    assert r.status_code == 401
    await _signed_in(client, db)
    assert (await client.get(f"/api/v1/documents/{uuid.uuid4()}")).status_code == 404


# ── the patient's id is looked up once per request ───────────────────────────────────────────


class _Statements:
    """Counts the statements one request sends to the database."""

    def __init__(self, engine) -> None:
        self.engine = engine.sync_engine
        self.sql: list[str] = []

    def _record(self, _conn, _cursor, statement, *_a, **_k) -> None:
        self.sql.append(" ".join(statement.split()))

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *_exc) -> None:
        event.remove(self.engine, "before_cursor_execute", self._record)

    @property
    def patient_lookups(self) -> int:
        return sum(
            1
            for s in self.sql
            if s.startswith("SELECT patient.patient_id FROM patient WHERE patient.user_id")
        )


@pytest.mark.parametrize(
    "path",
    [
        "/en/patient/documents",  # a count and a list of saved records, a count and a list of uploads
        "/api/v1/documents",
        "/api/v1/documents/in-progress",
        f"/api/v1/documents/{uuid.uuid4()}",  # an unknown id
    ],
)
async def test_the_patient_id_is_looked_up_once_per_request(client, db, engine, path):
    await _signed_in(client, db)
    with _Statements(engine) as seen:
        await client.get(path)
    assert seen.patient_lookups == 1, seen.sql


async def test_the_remembered_id_belongs_to_the_user_it_was_looked_up_for(db):
    """The shortcut is per user inside one session: asking for a second user's id must not
    return the first's, and a user with no patient record is refused every time."""
    first = await make_user(db, email="one@x.com", role=UserRole.PATIENT)
    second = await make_user(db, email="two@x.com", role=UserRole.PATIENT)
    doctor = await make_user(db, email="doc@x.com", role=UserRole.DOCTOR)
    a = await doc_service._patient_id(db, Actor(user_id=first.id, role="patient"))
    b = await doc_service._patient_id(db, Actor(user_id=second.id, role="patient"))
    assert a != b
    assert await doc_service._patient_id(db, Actor(user_id=first.id, role="patient")) == a
    for _ in range(2):  # a failed lookup is never remembered as a success
        with pytest.raises(Exception) as refused:
            await doc_service._patient_id(db, Actor(user_id=doctor.id, role="patient"))
        assert type(refused.value).__name__ == "NotFound"
