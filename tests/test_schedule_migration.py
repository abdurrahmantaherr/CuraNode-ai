"""D7 T1: the hand-written migration 291c06a2b86f, exercised on SQLite only.

The shared tables are rebuilt as they are live (spec §0.1) — the slice of the
schema the migration touches — then the migration runs up and down through
Alembic's operations API. It is never run against the shared database here.
Covers AC-27, AC-29 (database part) and AC-30.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "291c06a2b86f_d7_schedule_exception_windows_and_caps.py"

NEW_EXCEPTION_COLUMNS = {"start_time", "end_time", "removed_at", "max_tokens"}
NEW_CHECKS = {
    "schedule_exception": {
        "schedule_exception_times_pair",
        "schedule_exception_times_order",
        "schedule_exception_extra_has_times",
        "schedule_exception_max_tokens_min",
        "schedule_exception_max_tokens_extra_only",
    },
    "doctor_schedule": {"doctor_schedule_slot_minutes_grid", "doctor_schedule_max_tokens_min"},
}


def _load():
    spec = importlib.util.spec_from_file_location("d7_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _run(conn, fn) -> None:
    with Operations.context(MigrationContext.configure(conn)):
        fn()


def _pre_d7_schema(conn) -> None:
    """`doctor_schedule` and `schedule_exception` as they are live, before D7."""
    conn.exec_driver_sql("CREATE TABLE doctor_affiliation (affiliation_id CHAR(32) PRIMARY KEY)")
    conn.exec_driver_sql(
        "CREATE TABLE doctor_schedule ("
        " schedule_id CHAR(32) PRIMARY KEY,"
        " affiliation_id CHAR(32) NOT NULL REFERENCES doctor_affiliation(affiliation_id),"
        " weekday SMALLINT NOT NULL,"
        " start_time TIME NOT NULL,"
        " end_time TIME NOT NULL,"
        " slot_minutes INTEGER NOT NULL,"
        " max_tokens INTEGER NOT NULL,"
        " status VARCHAR(20) NOT NULL DEFAULT 'active',"
        " CONSTRAINT doctor_schedule_check CHECK (end_time > start_time),"
        " CONSTRAINT doctor_schedule_weekday_check CHECK (weekday >= 0 AND weekday <= 6),"
        " CONSTRAINT doctor_schedule_slot_minutes_check CHECK (slot_minutes > 0),"
        " CONSTRAINT doctor_schedule_max_tokens_check CHECK (max_tokens >= 0))"
    )
    conn.exec_driver_sql(
        "CREATE TABLE schedule_exception ("
        " exception_id CHAR(32) PRIMARY KEY,"
        " schedule_id CHAR(32) NOT NULL REFERENCES doctor_schedule(schedule_id),"
        " exception_date DATE NOT NULL,"
        " type VARCHAR(20) NOT NULL,"
        " reason TEXT,"
        " created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
        " CONSTRAINT schedule_exception_type_check"
        "   CHECK (type IN ('leave', 'emergency', 'extra')))"
    )
    conn.exec_driver_sql("INSERT INTO doctor_affiliation VALUES ('a1')")
    conn.exec_driver_sql(
        "INSERT INTO doctor_schedule VALUES ('s1', 'a1', 0, '09:00', '13:00', 15, 16, 'active')"
    )
    # An existing whole-slot leave row: it must survive the migration untouched.
    conn.exec_driver_sql(
        "INSERT INTO schedule_exception (exception_id, schedule_id, exception_date, type)"
        " VALUES ('e0', 's1', '2026-10-12', 'leave')"
    )


def _slot(conn, sid: str, slot_minutes: int, max_tokens: int) -> None:
    conn.exec_driver_sql(
        "INSERT INTO doctor_schedule VALUES"
        f" ('{sid}', 'a1', 1, '09:00', '13:00', {slot_minutes}, {max_tokens}, 'active')"
    )


def _exception(conn, eid: str, kind: str, start=None, end=None, max_tokens=None) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO schedule_exception"
            " (exception_id, schedule_id, exception_date, type, start_time, end_time, max_tokens)"
            " VALUES (:eid, 's1', '2026-10-12', :kind, :start, :end, :cap)"
        ),
        {"eid": eid, "kind": kind, "start": start, "end": end, "cap": max_tokens},
    )


def _refused(conn, fn, *args) -> None:
    with pytest.raises(sa.exc.IntegrityError), conn.begin_nested():
        fn(conn, *args)


def _checks(conn, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(conn).get_check_constraints(table)}


@pytest.fixture
def migrated():
    module = _load()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        _pre_d7_schema(conn)
        _run(conn, module.upgrade)
        yield conn, module


def test_upgrade_adds_columns_checks_and_index(migrated):
    conn, _ = migrated
    inspector = sa.inspect(conn)
    columns = {c["name"] for c in inspector.get_columns("schedule_exception")}
    assert NEW_EXCEPTION_COLUMNS <= columns
    for table, names in NEW_CHECKS.items():
        assert names <= _checks(conn, table)
    indexes = {i["name"] for i in inspector.get_indexes("schedule_exception")}
    assert "idx_schedule_exception_date" in indexes
    # Existing rows and the live CHECKs survive.
    assert conn.exec_driver_sql("SELECT count(*) FROM schedule_exception").scalar() == 1
    assert "doctor_schedule_max_tokens_check" in _checks(conn, "doctor_schedule")


@pytest.mark.parametrize("value", [7, 0, 65])
def test_ac27_slot_minutes_off_the_grid_is_refused_by_the_database(migrated, value):
    conn, _ = migrated
    _refused(conn, _slot, f"bad{value}", value, 10)


def test_ac27_slot_minutes_on_the_grid_is_accepted(migrated):
    conn, _ = migrated
    for value in (5, 15, 60):
        _slot(conn, f"ok{value}", value, 10)


def test_ac30_max_tokens_zero_is_refused_by_the_database(migrated):
    conn, _ = migrated
    _refused(conn, _slot, "zero", 15, 0)
    _slot(conn, "one", 15, 1)


def test_ac29_exception_checks(migrated):
    conn, _ = migrated
    _exception(conn, "x1", "extra", "13:00", "15:00", None)  # inherit
    _exception(conn, "x2", "extra", "15:00", "17:00", 5)  # own cap
    _exception(conn, "l1", "leave", "10:00", "11:00")  # window time off
    _refused(conn, _exception, "b1", "extra", "13:00", "15:00", 0)  # cap < 1
    _refused(conn, _exception, "b2", "leave", None, None, 3)  # cap on time off
    _refused(conn, _exception, "b3", "extra")  # extra without times
    _refused(conn, _exception, "b4", "leave", "10:00", None)  # one time only
    _refused(conn, _exception, "b5", "leave", "11:00", "10:00")  # end before start


def test_downgrade_removes_everything_it_added(migrated):
    conn, module = migrated
    _run(conn, module.downgrade)
    inspector = sa.inspect(conn)
    columns = {c["name"] for c in inspector.get_columns("schedule_exception")}
    assert not (NEW_EXCEPTION_COLUMNS & columns)
    for table, names in NEW_CHECKS.items():
        assert not (names & _checks(conn, table))
    indexes = {i["name"] for i in inspector.get_indexes("schedule_exception")}
    assert "idx_schedule_exception_date" not in indexes
    assert "doctor_schedule_max_tokens_check" in _checks(conn, "doctor_schedule")
    assert conn.exec_driver_sql("SELECT count(*) FROM schedule_exception").scalar() == 1


def test_migration_is_additive_and_marked_ask_me_before_running():
    source = MIGRATION.read_text(encoding="utf-8")
    upgrade = source.split("def upgrade()")[1].split("def downgrade()")[0]
    for forbidden in ("drop_", "alter_column", "rename", "DROP ", "DELETE ", "UPDATE "):
        assert forbidden not in upgrade
    assert 'down_revision: str | Sequence[str] | None = "dba968144fb4"' in source
    assert source.count("ASK ME BEFORE RUNNING") >= 2
    assert "slot_minutes BETWEEN 5 AND 60" in source.split('"""')[1]  # the pre-check SQL
