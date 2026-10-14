"""P5: the hand-written migration 92b61c6e9db4, exercised on SQLite only. It is
never run against the shared database here."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "92b61c6e9db4_booking_active_slot_index.py"
INDEX = "uq_appointment_active_slot"


def _load():
    spec = importlib.util.spec_from_file_location("booking_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _engine():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE appointment (appointment_id VARCHAR(32) PRIMARY KEY, "
                "schedule_id VARCHAR(32), slot_start DATETIME NOT NULL, status VARCHAR(20) NOT NULL)"
            )
        )
    return engine


def _run(engine, fn_name: str) -> None:
    module = _load()
    with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
        getattr(module, fn_name)()


def test_revision_chain():
    module = _load()
    assert module.revision == "92b61c6e9db4"
    assert module.down_revision == "a7d3c89106f9"


def test_upgrade_creates_a_unique_partial_index():
    engine = _engine()
    _run(engine, "upgrade")
    indexes = {i["name"]: i for i in sa.inspect(engine).get_indexes("appointment")}
    assert indexes[INDEX]["unique"]
    assert indexes[INDEX]["column_names"] == ["schedule_id", "slot_start"]


def insert(conn, key: str, status: str) -> None:
    conn.execute(
        sa.text("INSERT INTO appointment VALUES (:id, 's1', '2026-10-14 05:00:00', :status)"),
        {"id": key, "status": status},
    )


def test_the_index_refuses_two_active_rows_and_ignores_cancelled_ones():
    engine = _engine()
    _run(engine, "upgrade")
    with engine.begin() as conn:
        insert(conn, "a", "cancelled")
        insert(conn, "b", "cancelled")
        insert(conn, "c", "booked")
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        insert(conn, "d", "booked")


def test_downgrade_drops_the_index():
    engine = _engine()
    _run(engine, "upgrade")
    _run(engine, "downgrade")
    assert INDEX not in {i["name"] for i in sa.inspect(engine).get_indexes("appointment")}
