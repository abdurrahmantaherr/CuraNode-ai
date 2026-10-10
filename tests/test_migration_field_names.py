"""The `ocr_extraction.field_name` widening (revision e3a52c8d7f14): additive, a strict
superset, tied to the application's own list of field names, and reversible only
without destroying data. The suite's database is SQLite built from the models, so the
Postgres statements are checked by capturing what the migration would run."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from app.documents import groups

REVISION = "e3a52c8d7f14"
PREVIOUS = "b7e24c19a053"


def _migration():
    [path] = list(Path("alembic/versions").glob(f"{REVISION}_*.py"))
    spec = importlib.util.spec_from_file_location("field_names_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _run(module, direction: str, dialect: str, monkeypatch) -> list[str]:
    statements: list[str] = []
    bind = MagicMock()
    bind.dialect.name = dialect
    monkeypatch.setattr(module.op, "get_bind", lambda: bind)
    monkeypatch.setattr(module.op, "execute", lambda sql: statements.append(str(sql)))
    getattr(module, direction)()
    return statements


def test_it_extends_the_chain_without_branching():
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))
    assert scripts.get_heads() == [REVISION]
    assert scripts.get_revision(REVISION).down_revision == PREVIOUS


def test_the_field_names_in_the_database_are_exactly_the_ones_the_code_uses():
    """If a group gains a field, this fails until a migration allows it."""
    assert set(_migration().NEW_FIELDS) == set(groups.ALL_FIELDS)
    assert len(_migration().NEW_FIELDS) == len(set(_migration().NEW_FIELDS))


def test_it_only_widens_what_the_constraint_allowed():
    module = _migration()
    assert set(module.OLD_FIELDS) <= set(module.NEW_FIELDS)
    assert set(module.OLD_FIELDS) == {
        "medicine",
        "dosage",
        "frequency",
        "duration",
        "test_name",
        "result",
        "unit",
        "reference_range",
    }
    assert all(len(name) <= 20 for name in module.NEW_FIELDS)  # field_name is varchar(20)


def test_upgrade_swaps_one_check_constraint_and_does_nothing_else(monkeypatch):
    module = _migration()
    drop, add = _run(module, "upgrade", "postgresql", monkeypatch)
    assert (
        drop == "ALTER TABLE public.ocr_extraction DROP CONSTRAINT ocr_extraction_field_name_check"
    )
    assert add.startswith(
        "ALTER TABLE public.ocr_extraction ADD CONSTRAINT ocr_extraction_field_name_check CHECK"
    )
    allowed = set(re.findall(r"'([a-z0-9_]+)'", add))
    assert allowed == set(groups.ALL_FIELDS)
    for banned in ("DELETE", "UPDATE", "TRUNCATE", "DROP TABLE", "DROP COLUMN", "ADD COLUMN"):
        assert banned not in drop + add


def test_it_is_a_no_op_on_the_sqlite_test_database(monkeypatch):
    module = _migration()
    assert _run(module, "upgrade", "sqlite", monkeypatch) == []
    assert _run(module, "downgrade", "sqlite", monkeypatch) == []


def test_the_downgrade_refuses_to_delete_reviewed_data(monkeypatch):
    guard, drop, add = _run(_migration(), "downgrade", "postgresql", monkeypatch)
    assert "RAISE EXCEPTION" in guard and "NOT IN (" in guard
    assert "DELETE" not in guard + drop + add  # it stops; it never removes rows
    restored = set(re.findall(r"'([a-z0-9_]+)'", add))
    assert restored == set(_migration().OLD_FIELDS)


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_both_directions_are_defined(direction):
    assert callable(getattr(_migration(), direction))
