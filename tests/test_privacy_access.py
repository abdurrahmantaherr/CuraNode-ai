"""Privacy & Access Control test suite (P9: FR4 per-section, FR5).

`test_acNN_*` covers acceptance criterion AC-NN of
`.claude/specs/privacy_access_spec.md`. Web-page criteria (AC-17 page,
AC-24, AC-28) live in `tests/test_privacy_access_web.py`.

Runs on the usual harness: in-memory SQLite and the fake Supabase Auth
double, no network. The migration test builds its own throwaway SQLite
database; nothing here touches the shared Supabase project.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.db.models import AccessLog, ConsentGrant, UserRole
from app.db.types import utcnow, uuid7
from sqlalchemy.exc import IntegrityError

from tests.conftest import make_user

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "dba968144fb4_consent_grant_sections_replaced_by.py"
ALL = ["basics", "allergies", "conditions", "medications", "prescriptions", "lab_reports", "visits"]


# ── S1: models and migration (AC-29) ─────────────────────────────────────
def _load_migration():
    spec = importlib.util.spec_from_file_location("p9_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _pre_p9_schema(conn) -> None:
    """The slice of the shared schema the migration touches, as it was before P9."""
    conn.exec_driver_sql("CREATE TABLE user_profile (user_id CHAR(32) PRIMARY KEY)")
    conn.exec_driver_sql("CREATE TABLE patient (patient_id CHAR(32) PRIMARY KEY)")
    conn.exec_driver_sql(
        "CREATE TABLE consent_grant ("
        " consent_id CHAR(32) PRIMARY KEY,"
        " patient_id CHAR(32) NOT NULL REFERENCES patient(patient_id),"
        " grantee_type VARCHAR(20) NOT NULL,"
        " grantee_id CHAR(32) NOT NULL,"
        " scope_sections JSON NOT NULL DEFAULT '[]',"
        " granted_at DATETIME NOT NULL,"
        " expires_at DATETIME,"
        " revoked_at DATETIME)"
    )
    conn.exec_driver_sql("INSERT INTO patient VALUES ('p1')")
    conn.exec_driver_sql(
        "INSERT INTO consent_grant (consent_id, patient_id, grantee_type, grantee_id,"
        " scope_sections, granted_at) VALUES"
        " ('g1', 'p1', 'doctor', 'd1', '[]', '2026-10-01'),"
        " ('g2', 'p1', 'doctor', 'd2', '[\"allergies\"]', '2026-10-01')"
    )


def _run(conn, fn) -> None:
    with Operations.context(MigrationContext.configure(conn)):
        fn()


def test_ac29_migration_upgrades_and_downgrades_on_sqlite():
    module = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        _pre_p9_schema(conn)
        _run(conn, module.upgrade)

        cols = {c["name"] for c in sa.inspect(conn).get_columns("consent_grant")}
        assert {"revoked_by", "replaced_by"} <= cols
        rows = dict(
            conn.exec_driver_sql("SELECT consent_id, scope_sections FROM consent_grant").all()
        )
        assert json.loads(rows["g1"]) == ALL  # legacy [] → every section
        assert json.loads(rows["g2"]) == ["allergies"]  # a real choice is left alone

        # A second unrevoked grant for the same grantee is refused …
        with pytest.raises(sa.exc.IntegrityError), conn.begin_nested():
            conn.exec_driver_sql(
                "INSERT INTO consent_grant (consent_id, patient_id, grantee_type,"
                " grantee_id, scope_sections, granted_at)"
                " VALUES ('g3', 'p1', 'doctor', 'd1', '[]', '2026-10-02')"
            )
        # … but one alongside a revoked grant is fine.
        conn.exec_driver_sql(
            "UPDATE consent_grant SET revoked_at = '2026-10-02' WHERE consent_id='g1'"
        )
        conn.exec_driver_sql(
            "INSERT INTO consent_grant (consent_id, patient_id, grantee_type, grantee_id,"
            " scope_sections, granted_at) VALUES ('g3', 'p1', 'doctor', 'd1', '[]', '2026-10-02')"
        )

        _run(conn, module.downgrade)
        cols = {c["name"] for c in sa.inspect(conn).get_columns("consent_grant")}
        assert "revoked_by" not in cols and "replaced_by" not in cols
        indexes = {i["name"] for i in sa.inspect(conn).get_indexes("consent_grant")}
        assert "uq_consent_grant_one_active" not in indexes


def test_ac29_migration_is_additive():
    """Static check: upgrade() only adds — no drop, rename or type change."""
    source = MIGRATION.read_text(encoding="utf-8")
    upgrade = source.split("def upgrade()")[1].split("def downgrade()")[0]
    for forbidden in ("drop_", "alter_column", "rename", "DROP ", "DELETE "):
        assert forbidden not in upgrade
    assert 'down_revision: str | Sequence[str] | None = "488dbf2b3915"' in source


async def test_ac29_one_unrevoked_grant_per_grantee_in_the_model(db):
    user = await make_user(db, email="p@x.com", role=UserRole.PATIENT)
    await db.refresh(user, ["patient"])
    grantee = uuid7()

    def grant(**kw) -> ConsentGrant:
        return ConsentGrant(
            id=uuid7(),
            patient_id=user.patient.id,
            grantee_type="doctor",
            grantee_id=grantee,
            scope_sections=["basics"],
            granted_at=utcnow(),
            **kw,
        )

    db.add(grant(revoked_at=utcnow()))
    db.add(grant())
    await db.commit()  # one revoked + one active is fine
    db.add(grant())
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_ac29_access_log_action_is_constrained(db):
    user = await make_user(db, email="p@x.com", role=UserRole.PATIENT)
    await db.refresh(user, ["patient"])
    db.add(
        AccessLog(
            patient_id=user.patient.id,
            resource_type="section.basics",
            resource_id=user.patient.id,
            action="delete",
        )
    )
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()
