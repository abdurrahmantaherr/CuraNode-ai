"""Async engine and session factory.

The engine URL is the only place the database choice appears; swapping SQLite
for `postgresql+psycopg://` needs no other code change (TDD 1.1 / NFR6).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ..settings import settings

_is_sqlite = settings.database_url.startswith("sqlite")
# Supabase's transaction-mode pooler (pgbouncer, port 6543) does not support
# server-side prepared statements, which asyncpg uses by default.
_is_supabase_pooler = (
    ":6543" in settings.database_url or "pooler.supabase.com" in settings.database_url
)

engine = create_async_engine(
    settings.database_url,
    echo=False,
    future=True,
    # pool_size/max_overflow are Postgres-side knobs (TDD 10.2); SQLite's
    # driver does not accept them.
    **({} if _is_sqlite else {"pool_size": 20, "max_overflow": 10}),
    **({"connect_args": {"statement_cache_size": 0}} if _is_supabase_pooler else {}),
)

SessionFactory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session
