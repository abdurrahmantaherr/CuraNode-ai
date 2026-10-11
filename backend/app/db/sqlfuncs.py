"""SQL functions the models need that are not portable between the databases this project
runs on (Postgres in production, SQLite in the tests)."""

from __future__ import annotations

from sqlalchemy import Date
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import FunctionElement


class pk_date(FunctionElement):
    """The calendar date IN PAKISTAN of a UTC timestamp column.

    Timestamps are stored in UTC (TDD 3.1) and shown in Pakistan time, so "the day it was
    uploaded" is a Pakistan day. `date(uploaded_at)` is the UTC day, which is the previous day for
    the five hours after midnight in Pakistan: a document uploaded at 1 am would be listed,
    and found by a date filter, under yesterday."""

    type = Date()
    inherit_cache = True
    name = "pk_date"


@compiles(pk_date)
def _pk_date_default(element: pk_date, compiler, **kw) -> str:  # type: ignore[no-untyped-def]
    return f"date({compiler.process(element.clauses, **kw)})"


@compiles(pk_date, "postgresql")
def _pk_date_postgresql(element: pk_date, compiler, **kw) -> str:  # type: ignore[no-untyped-def]
    return f"(({compiler.process(element.clauses, **kw)} AT TIME ZONE 'Asia/Karachi')::date)"


@compiles(pk_date, "sqlite")
def _pk_date_sqlite(element: pk_date, compiler, **kw) -> str:  # type: ignore[no-untyped-def]
    # Karachi is a fixed UTC+5 (no DST since 2009); SQLite has no time-zone database.
    return f"date({compiler.process(element.clauses, **kw)}, '+5 hours')"
