"""Async engine and session factory.

The engine URL is the only place the database choice appears; swapping SQLite
for `postgresql+psycopg://` needs no other code change (TDD 1.1 / NFR6).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ..settings import settings

_is_sqlite = settings.database_url.startswith("sqlite")

engine = create_async_engine(
    settings.database_url,
    echo=False,
    future=True,
    # pool_size/max_overflow are Postgres-side knobs (TDD 10.2); SQLite's
    # driver does not accept them.
    **({} if _is_sqlite else {"pool_size": 20, "max_overflow": 10}),
)

SessionFactory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session
