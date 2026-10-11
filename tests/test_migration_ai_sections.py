"""The `ocr_job.ai_sections` migration (revision b7e24c19a053): additive-only,
hand-written, and reversible. The suite's database is SQLite built from the
models (see `conftest.py`), so this tests the migration itself separately: its
place in the chain, what it is allowed to contain, and that it really adds and
removes exactly one nullable column on a table shaped like the shared one.
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from app.db.models import OcrJob

REVISION = "b7e24c19a053"
PREVIOUS = "35c4bc13fa37"


def _migration():
    [path] = list(Path("alembic/versions").glob(f"{REVISION}_*.py"))
    spec = importlib.util.spec_from_file_location("ai_sections_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_it_extends_the_chain_without_branching():
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))
    assert len(scripts.get_heads()) == 1  # one linear chain, never a branch
    assert scripts.get_revision(REVISION).down_revision == PREVIOUS
    # ...and it is still reachable from the head, i.e. nothing was inserted before it.
    assert REVISION in {r.revision for r in scripts.walk_revisions()}


def test_it_is_additive_only():
    module = _migration()
    upgrade = inspect.getsource(module.upgrade)
    assert "add_column" in upgrade
    for banned in (
        "drop_",
        "alter_column",
        "rename",
        "execute",
        "create_table",
        "DELETE",
        "UPDATE",
    ):
        assert banned not in upgrade, banned
    assert "nullable=True" in upgrade  # existing rows are untouched: NULL means "none stored"
    assert "server_default" not in upgrade  # no table rewrite, no backfill


def test_the_downgrade_is_the_exact_reverse():
    downgrade = inspect.getsource(_migration().downgrade)
    assert 'drop_column("ocr_job", "ai_sections")' in downgrade


def test_the_model_matches_the_migration():
    column = OcrJob.__table__.c.ai_sections
    assert column.nullable is True
    assert column.default is None and column.server_default is None


def test_it_adds_then_removes_one_nullable_column_and_leaves_existing_rows_alone():
    engine = sa.create_engine("sqlite://")
    module = _migration()
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE ocr_job (job_id TEXT PRIMARY KEY, status TEXT)"))
        conn.execute(sa.text("INSERT INTO ocr_job VALUES ('job-1', 'done')"))
        context = MigrationContext.configure(conn)

        with Operations.context(context):
            module.upgrade()
        columns = {c["name"]: c for c in sa.inspect(conn).get_columns("ocr_job")}
        assert set(columns) == {"job_id", "status", "ai_sections"}
        assert columns["ai_sections"]["nullable"] is True
        assert conn.execute(sa.text("SELECT status, ai_sections FROM ocr_job")).one() == (
            "done",
            None,
        )

        with Operations.context(context):
            module.downgrade()
        assert {c["name"] for c in sa.inspect(conn).get_columns("ocr_job")} == {"job_id", "status"}
        assert conn.execute(sa.text("SELECT status FROM ocr_job")).scalar_one() == "done"
