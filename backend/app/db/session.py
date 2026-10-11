"""Async engine and session factory.

The engine URL is the only place the database choice appears; swapping SQLite
for `postgresql+psycopg://` needs no other code change (TDD 1.1 / NFR6).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import event
from sqlalchemy import exc as sa_exc
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.util import await_only

from ..log_config import get_logger
from ..settings import settings
from .transient import connect_with_retry

log = get_logger()

# Supabase's transaction-mode pooler (pgbouncer, port 6543) does not support
# server-side prepared statements, which asyncpg uses by default — `engine_options` turns the
# statement cache off for it.


def engine_options(url: str, connect_timeout_s: int) -> dict[str, Any]:
    """The Postgres-side engine knobs for `url` (none at all for SQLite, whose driver accepts
    none of them). Separate from `create_async_engine` so it can be tested without a server."""
    if url.startswith("sqlite"):
        return {}
    pooler = ":6543" in url or "pooler.supabase.com" in url
    connect_args: dict[str, Any] = {"timeout": max(1, int(connect_timeout_s))}
    if pooler:
        connect_args["statement_cache_size"] = 0
    return {
        # pool_size/max_overflow are Postgres-side knobs (TDD 10.2).
        "pool_size": 20,
        "max_overflow": 10,
        # Reuse the connection used most recently, not the one that has waited longest: with
        # one person clicking about, requests then keep one warm connection busy instead of
        # rotating through several and letting each sit idle long enough to be dropped.
        "pool_use_lifo": True,
        # Never keep a connection for ever: a pooler or router forgets old ones.
        "pool_recycle": 900,
        "connect_args": connect_args,
    }


def install_connection_guards(
    engine: AsyncEngine,
    *,
    idle_ping_s: float,
    connect_attempts: int = 3,
    connect_budget_s: float = 30.0,
    backoff_s: float = 0.5,
) -> None:
    """Two protections against a flaky route to the database, both found from a real dev log.

    1. A connection that has sat idle may have been dropped by the pooler or a router; the
       first request to use it then failed ("connection was closed in the middle of operation")
       and the NEXT one worked. So a connection idle for more than `idle_ping_s` is tested as it
       is checked out, and a dead one is replaced before the request sees it. A busy
       connection is not tested (a test is a network round trip). `0` switches this off.
    2. Opening a connection is tried again, a couple of times within `connect_budget_s`, when it
       failed the way a blip fails (a DNS lookup, a stalled handshake, a reset) — never for a
       wrong password or any other error that a second try cannot fix.
    """
    sync_engine = engine.sync_engine

    @event.listens_for(sync_engine, "do_connect")
    def _connect(dialect, conn_rec, cargs, cparams):  # type: ignore[no-untyped-def]
        def retried(attempt: int, error: BaseException) -> None:
            log.warning("database_connect_retry", attempt=attempt, error=type(error).__name__)

        return connect_with_retry(
            lambda: dialect.connect(*cargs, **cparams),
            attempts=connect_attempts,
            budget_s=connect_budget_s,
            backoff_s=backoff_s,
            pause=lambda seconds: await_only(asyncio.sleep(seconds)),
            on_retry=retried,
        )

    if idle_ping_s <= 0:
        return

    @event.listens_for(sync_engine, "checkin")
    def _stamp(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
        connection_record.info["idle_since"] = time.monotonic()

    @event.listens_for(sync_engine, "checkout")
    def _verify(dbapi_connection, connection_record, connection_proxy):  # type: ignore[no-untyped-def]
        since = connection_record.info.get("idle_since")
        if since is None or time.monotonic() - since < idle_ping_s:
            return  # new, or in use a moment ago
        try:
            alive = sync_engine.dialect.do_ping(dbapi_connection)
        except Exception:  # noqa: BLE001 - any failure to answer means it is gone
            alive = False
        if not alive:
            log.warning("stale_connection_replaced", idle_s=round(time.monotonic() - since))
            raise sa_exc.DisconnectionError("the pooled connection did not answer")  # new one next


engine = create_async_engine(
    settings.database_url,
    echo=False,
    future=True,
    **engine_options(settings.database_url, settings.database_connect_timeout_s),
)
if not settings.database_url.startswith("sqlite"):
    install_connection_guards(
        engine,
        idle_ping_s=settings.database_idle_ping_s,
        connect_budget_s=2.0 * settings.database_connect_timeout_s,
    )

SessionFactory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    """One session per request. Closing it rolls back whatever is still open, and when the
    connection died meanwhile (a request that waited on a slow network call holds its connection
    idle, and a pooler or router may reset it) that rollback raises — AFTER the response has
    gone out, as a traceback and a "response already started" error for something that harmed
    nobody. The dead connection is discarded by the pool either way, so the failure is logged
    (class only) and swallowed; an error from the request itself still propagates untouched."""
    session = SessionFactory()
    try:
        yield session
    finally:
        try:
            await asyncio.shield(session.close())
        except Exception as exc:  # noqa: BLE001 - cancellation is not an Exception, and passes
            log.warning("session_close_failed", error=type(exc).__name__)
