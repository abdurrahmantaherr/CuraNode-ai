"""A flaky route to the database: stale connections, blips while connecting, and starting up.

From one real dev machine (slow, far-away pooler; DNS that fails now and then; connections reset
by the other end). The first request after the server had sat idle failed with a 503 and the next
one worked, because the connection pooled at start-up had been dropped meanwhile; and the server
itself twice failed to START on a TLS handshake that timed out. Opening a connection is now
retried for failures that are gone a moment later, an idle connection is tested before it is
used, and start-up is patient.
"""

from __future__ import annotations

import asyncio
import errno
import socket

import pytest
from app import main as main_mod
from app.db import session as session_mod
from app.db.session import install_connection_guards
from app.db.transient import connect_with_retry, is_network_error, is_transient_connect_error
from asyncpg import exceptions as pg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# ── which failures are worth another try ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(socket.gaierror(11001, "getaddrinfo failed"), id="dns"),
        pytest.param(TimeoutError(), id="timeout"),
        pytest.param(ConnectionResetError(), id="reset"),
        pytest.param(ConnectionRefusedError(), id="refused"),
        pytest.param(OSError(errno.ENETUNREACH, "network unreachable"), id="unreachable"),
        pytest.param(pg.ConnectionDoesNotExistError("closed"), id="server-closed-it"),
        pytest.param(pg.CannotConnectNowError("starting up"), id="server-not-ready"),
    ],
)
def test_a_blip_is_worth_another_try(failure):
    assert is_transient_connect_error(failure)


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(
            pg.InvalidPasswordError("password authentication failed"), id="wrong-password"
        ),
        pytest.param(pg.InvalidCatalogNameError("database does not exist"), id="no-such-database"),
        pytest.param(pg.TooManyConnectionsError("too many"), id="too-many-connections"),
        pytest.param(ValueError("bug"), id="bug"),
        pytest.param(FileNotFoundError(errno.ENOENT, "no such file"), id="file"),
        pytest.param(PermissionError(errno.EACCES, "denied"), id="permission"),
    ],
)
def test_what_a_second_try_cannot_fix_is_not_retried(failure):
    """A wrong password retried is a lockout waiting to happen."""
    assert not is_transient_connect_error(failure)


def test_a_network_error_is_not_the_same_as_a_connection_error_from_the_server():
    assert is_network_error(TimeoutError()) and not is_network_error(
        pg.ConnectionDoesNotExistError("x")
    )


# ── the retry itself ─────────────────────────────────────────────────────────────────────────


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.pauses: list[float] = []

    def __call__(self) -> float:
        return self.now

    def pause(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.now += seconds


def _flaky(failures: list[BaseException], result: str = "connected"):
    calls = {"n": 0}

    def connect() -> str:
        calls["n"] += 1
        if calls["n"] <= len(failures):
            raise failures[calls["n"] - 1]
        return result

    return connect, calls


def test_a_blip_is_ridden_out():
    clock = _Clock()
    connect, calls = _flaky([socket.gaierror(11001, "x"), TimeoutError()])
    seen: list[tuple[int, str]] = []
    got = connect_with_retry(
        connect,
        attempts=3,
        budget_s=30,
        backoff_s=0.5,
        pause=clock.pause,
        clock=clock,
        on_retry=lambda n, e: seen.append((n, type(e).__name__)),
    )
    assert got == "connected" and calls["n"] == 3
    assert clock.pauses == [0.5, 1.0]  # a little longer each time
    assert seen == [(1, "gaierror"), (2, "TimeoutError")]


def test_it_gives_up_after_the_attempts_and_raises_the_last_error():
    clock = _Clock()
    connect, calls = _flaky([TimeoutError("first"), TimeoutError("second"), TimeoutError("third")])
    with pytest.raises(TimeoutError, match="third"):
        connect_with_retry(
            connect, attempts=3, budget_s=30, backoff_s=0.1, pause=clock.pause, clock=clock
        )
    assert calls["n"] == 3


def test_it_never_retries_what_a_second_try_cannot_fix():
    clock = _Clock()
    connect, calls = _flaky([pg.InvalidPasswordError("nope")])
    with pytest.raises(pg.InvalidPasswordError):
        connect_with_retry(
            connect, attempts=3, budget_s=30, backoff_s=0.1, pause=clock.pause, clock=clock
        )
    assert calls["n"] == 1 and clock.pauses == []


def test_it_stops_when_the_time_budget_is_spent():
    """An attempt that timed out has already spent most of the budget: a second is worth it, a
    third that would run past the budget is not."""
    clock = _Clock()
    calls = {"n": 0}

    def slow_failure() -> str:
        calls["n"] += 1
        clock.now += 16  # each attempt stalls for 16 s, then times out
        raise TimeoutError

    with pytest.raises(TimeoutError):
        connect_with_retry(
            slow_failure, attempts=5, budget_s=30, backoff_s=0.5, pause=clock.pause, clock=clock
        )
    assert calls["n"] == 2  # 16 + 0.5 + 16 = 32.5: already past 30, so no third


# ── guards on a real engine (SQLite file: it has a real connection pool) ─────────────────────


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def warning(self, name, **fields) -> None:
        self.events.append((name, fields))


@pytest.fixture
def seen_log(monkeypatch) -> _Log:
    seen = _Log()
    monkeypatch.setattr(session_mod, "log", seen)
    return seen


@pytest.fixture
async def guarded(tmp_path):
    """A pooled engine with the guards installed and a very short idle limit."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'guards.db'}")
    install_connection_guards(engine, idle_ping_s=0.05, connect_budget_s=10, backoff_s=0.0)
    yield engine
    await engine.dispose()


async def _which_connection(engine) -> int:
    """Run a statement and say which pooled connection ran it."""
    async with engine.connect() as conn:
        raw = await conn.get_raw_connection()
        await conn.execute(text("select 1"))
        return id(raw.dbapi_connection)


def _count_pings(engine, *, answer=True, raises: BaseException | None = None) -> list[int]:
    pings: list[int] = []
    real = engine.sync_engine.dialect.do_ping

    def ping(dbapi_connection) -> bool:
        pings.append(1)
        if raises is not None:
            raise raises
        return real(dbapi_connection) if answer else False

    engine.sync_engine.dialect.do_ping = ping
    return pings


async def test_a_connection_idle_too_long_is_tested_and_kept_if_it_answers(guarded):
    first = await _which_connection(guarded)
    pings = _count_pings(guarded)
    await asyncio.sleep(0.1)
    assert await _which_connection(guarded) == first  # alive: the same one is used
    assert len(pings) == 1


async def test_a_connection_idle_too_long_that_does_not_answer_is_replaced(guarded, seen_log):
    first = await _which_connection(guarded)
    pings = _count_pings(guarded, answer=False)  # the pooler dropped it meanwhile
    await asyncio.sleep(0.1)
    second = await _which_connection(guarded)  # the request still succeeds, on a fresh one
    assert second != first and len(pings) == 1
    assert [e for e, _ in seen_log.events] == ["stale_connection_replaced"]


async def test_a_ping_that_raises_counts_as_a_dead_connection(guarded):
    first = await _which_connection(guarded)
    _count_pings(guarded, raises=ConnectionResetError())
    await asyncio.sleep(0.1)
    assert await _which_connection(guarded) != first


async def test_a_connection_used_a_moment_ago_is_not_tested(tmp_path):
    """A test is a network round trip: back-to-back requests must not pay it."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'busy.db'}")
    install_connection_guards(engine, idle_ping_s=60, connect_budget_s=10, backoff_s=0.0)
    try:
        await _which_connection(engine)
        pings = _count_pings(engine)
        await _which_connection(engine)
        await _which_connection(engine)
        assert pings == []
    finally:
        await engine.dispose()


async def test_the_idle_test_can_be_switched_off(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'off.db'}")
    install_connection_guards(engine, idle_ping_s=0, connect_budget_s=10, backoff_s=0.0)
    try:
        await _which_connection(engine)
        pings = _count_pings(engine)
        await asyncio.sleep(0.1)
        await _which_connection(engine)
        assert pings == []
    finally:
        await engine.dispose()


def _make_connect_fail(engine, failures: list[BaseException]) -> dict[str, int]:
    calls = {"n": 0}
    real = engine.sync_engine.dialect.connect

    def connect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= len(failures):
            raise failures[calls["n"] - 1]
        return real(*args, **kwargs)

    engine.sync_engine.dialect.connect = connect
    return calls


async def test_opening_a_connection_rides_out_a_dns_blip_and_a_stall(guarded, seen_log):
    calls = _make_connect_fail(
        guarded, [socket.gaierror(11001, "getaddrinfo failed"), TimeoutError()]
    )
    async with guarded.connect() as conn:
        assert (await conn.execute(text("select 1"))).scalar_one() == 1
    assert calls["n"] == 3
    assert [(e, f["error"]) for e, f in seen_log.events] == [
        ("database_connect_retry", "gaierror"),
        ("database_connect_retry", "TimeoutError"),
    ]


async def test_a_wrong_password_is_raised_at_once_not_retried(guarded):
    calls = _make_connect_fail(guarded, [pg.InvalidPasswordError("password authentication failed")])
    with pytest.raises(pg.InvalidPasswordError):
        async with guarded.connect():
            pass
    assert calls["n"] == 1


async def test_a_database_that_stays_unreachable_still_fails_after_the_attempts(guarded):
    calls = _make_connect_fail(guarded, [socket.gaierror(11001, "x")] * 10)
    with pytest.raises(socket.gaierror):
        async with guarded.connect():
            pass
    assert calls["n"] == 3  # the attempts, not ten


# ── start-up ─────────────────────────────────────────────────────────────────────────────────


class _Result:
    def __init__(self, row) -> None:
        self.row = row

    def scalar_one_or_none(self):
        return self.row


class _StartupSessions:
    """Stands in for `SessionFactory`: the first `fail_first` uses cannot connect."""

    def __init__(self, fail_first: int, *, error: BaseException | None = None, row=None) -> None:
        self.uses = 0
        self.fail_first = fail_first
        self.error = error if error is not None else TimeoutError()
        self.row = row

    def __call__(self):
        return self

    async def __aenter__(self):
        self.uses += 1
        if self.uses <= self.fail_first:
            raise self.error
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def execute(self, *_a, **_k):
        return _Result(self.row)


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(main_mod, "STARTUP_DB_PAUSE_S", 0)
    monkeypatch.setattr(main_mod, "log", _Log())


async def test_the_server_starts_even_if_the_first_connections_stall(monkeypatch):
    sessions = _StartupSessions(fail_first=2)  # it used to exit on the first one
    monkeypatch.setattr(main_mod, "SessionFactory", sessions)
    await main_mod._startup_checks()
    assert sessions.uses == 3


async def test_after_the_attempts_it_still_refuses_to_start_but_says_why(monkeypatch):
    sessions = _StartupSessions(fail_first=99)
    monkeypatch.setattr(main_mod, "SessionFactory", sessions)
    with pytest.raises(RuntimeError, match="Could not reach the database") as raised:
        await main_mod._startup_checks()
    assert sessions.uses == main_mod.STARTUP_DB_ATTEMPTS
    assert isinstance(raised.value.__cause__, TimeoutError)


async def test_real_data_outside_the_pilot_still_stops_the_server_and_is_not_retried(monkeypatch):
    sessions = _StartupSessions(fail_first=0, row=object())  # a non-synthetic profile exists
    monkeypatch.setattr(main_mod, "SessionFactory", sessions)
    with pytest.raises(RuntimeError, match="Non-synthetic"):
        await main_mod._startup_checks()
    assert sessions.uses == 1


async def test_an_error_that_is_not_an_outage_is_not_retried_at_start_up(monkeypatch):
    sessions = _StartupSessions(fail_first=99, error=ValueError("a bug"))
    monkeypatch.setattr(main_mod, "SessionFactory", sessions)
    with pytest.raises(ValueError):
        await main_mod._startup_checks()
    assert sessions.uses == 1


async def test_the_pilot_environment_runs_no_check(monkeypatch):
    sessions = _StartupSessions(fail_first=99)
    monkeypatch.setattr(main_mod, "SessionFactory", sessions)
    monkeypatch.setattr(main_mod.settings, "environment", "pilot")
    await main_mod._startup_checks()
    assert sessions.uses == 0
