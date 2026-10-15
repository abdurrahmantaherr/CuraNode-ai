"""The `records from confirmed documents` migration (revision c8e5a41f9b27).

The suite's database is SQLite built from the models, so the migration is tested on its own: its
place in the chain, what it may contain, and that on tables shaped like the shared ones it really
lets a row come from a document instead of a visit — and refuses a row that has neither — and that
it reverses cleanly, but never by deleting a patient's records.
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from app.db.models import Diagnosis, Prescription, PrescriptionItem, VitalSign

REVISION = "c8e5a41f9b27"
PREVIOUS = "92b61c6e9db4"


def _migration():
    [path] = list(Path("alembic/versions").glob(f"{REVISION}_*.py"))
    spec = importlib.util.spec_from_file_location("records_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_it_extends_the_chain_without_branching():
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))
    assert scripts.get_heads() == [REVISION]
    assert scripts.get_revision(REVISION).down_revision == PREVIOUS


def test_it_only_relaxes_and_adds_and_never_deletes_data():
    upgrade = inspect.getsource(_migration().upgrade)
    for banned in ("drop_table", "drop_column", "DELETE", "UPDATE", "TRUNCATE", "execute"):
        assert banned not in upgrade, banned
    downgrade = inspect.getsource(_migration().downgrade)
    assert "DELETE" not in downgrade and "TRUNCATE" not in downgrade


def test_the_models_match_the_migration():
    assert Prescription.__table__.c.encounter_id.nullable is True
    assert Prescription.__table__.c.doctor_id.nullable is True
    assert Diagnosis.__table__.c.encounter_id.nullable is True
    assert VitalSign.__table__.c.encounter_id.nullable is True
    assert PrescriptionItem.__table__.c.medicine_id.nullable is True
    for table in (Prescription, Diagnosis, VitalSign):
        assert table.__table__.c.document_id.nullable is True
    assert PrescriptionItem.__table__.c.medicine_text.nullable is True


# The tables as the shared database has them TODAY (before this revision): a visit is required.
_BEFORE = [
    "CREATE TABLE document (document_id CHAR(32) PRIMARY KEY)",
    "CREATE TABLE encounter (encounter_id CHAR(32) PRIMARY KEY)",
    "CREATE TABLE doctor (doctor_id CHAR(32) PRIMARY KEY)",
    "CREATE TABLE medicine (medicine_id CHAR(32) PRIMARY KEY)",
    """CREATE TABLE prescription (
        prescription_id CHAR(32) PRIMARY KEY,
        encounter_id CHAR(32) NOT NULL REFERENCES encounter (encounter_id),
        patient_id CHAR(32) NOT NULL,
        doctor_id CHAR(32) NOT NULL REFERENCES doctor (doctor_id),
        source VARCHAR(20) NOT NULL)""",
    """CREATE TABLE prescription_item (
        item_id CHAR(32) PRIMARY KEY,
        prescription_id CHAR(32) NOT NULL REFERENCES prescription (prescription_id),
        medicine_id CHAR(32) NOT NULL REFERENCES medicine (medicine_id))""",
    """CREATE TABLE diagnosis (
        diagnosis_id CHAR(32) PRIMARY KEY,
        encounter_id CHAR(32) NOT NULL REFERENCES encounter (encounter_id))""",
    """CREATE TABLE vital_sign (
        vital_id CHAR(32) PRIMARY KEY,
        encounter_id CHAR(32) NOT NULL UNIQUE REFERENCES encounter (encounter_id))""",
    "INSERT INTO document VALUES ('d1')",
    "INSERT INTO encounter VALUES ('e1')",
    "INSERT INTO doctor VALUES ('dr1')",
    "INSERT INTO medicine VALUES ('m1')",
    "INSERT INTO prescription VALUES ('p1', 'e1', 'pat', 'dr1', 'digital')",
    "INSERT INTO prescription_item VALUES ('i1', 'p1', 'm1')",
    "INSERT INTO diagnosis VALUES ('x1', 'e1')",
    "INSERT INTO vital_sign VALUES ('v1', 'e1')",
]


def _upgraded(conn):
    for statement in _BEFORE:
        conn.execute(sa.text(statement))
    context = MigrationContext.configure(conn)
    with Operations.context(context):
        _migration().upgrade()
    return context


def _columns(conn, table):
    return {c["name"]: c for c in sa.inspect(conn).get_columns(table)}


def test_a_row_may_come_from_a_document_instead_of_a_visit():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        _upgraded(conn)
        assert _columns(conn, "prescription")["encounter_id"]["nullable"] is True
        assert _columns(conn, "prescription")["doctor_id"]["nullable"] is True
        assert _columns(conn, "prescription_item")["medicine_id"]["nullable"] is True
        assert "document_id" in _columns(conn, "prescription")
        assert "medicine_text" in _columns(conn, "prescription_item")

        # a prescription, an item, a diagnosis and a vital-sign row, all from document d1
        conn.execute(
            sa.text(
                "INSERT INTO prescription (prescription_id, patient_id, source, document_id)"
                " VALUES ('p2', 'pat', 'ocr_upload', 'd1')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO prescription_item (item_id, prescription_id, medicine_text)"
                " VALUES ('i2', 'p2', 'Metformin')"
            )
        )
        conn.execute(
            sa.text("INSERT INTO diagnosis (diagnosis_id, document_id) VALUES ('x2', 'd1')")
        )
        conn.execute(sa.text("INSERT INTO vital_sign (vital_id, document_id) VALUES ('v2', 'd1')"))

        # the rows that were there are untouched
        assert (
            conn.execute(
                sa.text("SELECT encounter_id FROM prescription WHERE prescription_id='p1'")
            ).scalar_one()
            == "e1"
        )


@pytest.mark.parametrize(
    "orphan",
    [
        (
            "INSERT INTO prescription (prescription_id, patient_id, source)"
            " VALUES ('p9', 'pat', 'ocr_upload')"
        ),
        "INSERT INTO diagnosis (diagnosis_id) VALUES ('x9')",
        "INSERT INTO vital_sign (vital_id) VALUES ('v9')",
        "INSERT INTO prescription_item (item_id, prescription_id) VALUES ('i9', 'p1')",
        # a doctor's row with a visit but no doctor, and no document, is not allowed either
        (
            "INSERT INTO prescription (prescription_id, patient_id, source, encounter_id)"
            " VALUES ('p8', 'pat', 'digital', 'e1')"
        ),
    ],
)
def test_a_row_with_neither_a_visit_nor_a_document_is_refused(orphan):
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        _upgraded(conn)
        with pytest.raises(sa.exc.IntegrityError), conn.begin_nested():
            conn.execute(sa.text(orphan))


def test_there_is_at_most_one_prescription_and_one_vital_sign_row_per_document():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        _upgraded(conn)
        conn.execute(
            sa.text(
                "INSERT INTO prescription (prescription_id, patient_id, source, document_id)"
                " VALUES ('p2', 'pat', 'ocr_upload', 'd1')"
            )
        )
        conn.execute(sa.text("INSERT INTO vital_sign (vital_id, document_id) VALUES ('v2', 'd1')"))
        with pytest.raises(sa.exc.IntegrityError), conn.begin_nested():
            conn.execute(
                sa.text(
                    "INSERT INTO prescription (prescription_id, patient_id, source, document_id)"
                    " VALUES ('p3', 'pat', 'ocr_upload', 'd1')"
                )
            )
        with pytest.raises(sa.exc.IntegrityError), conn.begin_nested():
            conn.execute(
                sa.text("INSERT INTO vital_sign (vital_id, document_id) VALUES ('v3', 'd1')")
            )
        # a document may have several diagnoses
        conn.execute(
            sa.text("INSERT INTO diagnosis (diagnosis_id, document_id) VALUES ('x2', 'd1')")
        )
        conn.execute(
            sa.text("INSERT INTO diagnosis (diagnosis_id, document_id) VALUES ('x3', 'd1')")
        )


def test_the_downgrade_restores_the_old_rules_when_nothing_came_from_a_document():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        context = _upgraded(conn)
        with Operations.context(context):
            _migration().downgrade()
        assert "document_id" not in _columns(conn, "prescription")
        assert "medicine_text" not in _columns(conn, "prescription_item")
        assert _columns(conn, "prescription")["encounter_id"]["nullable"] is False
        assert _columns(conn, "prescription_item")["medicine_id"]["nullable"] is False
        # the doctor's own rows survived the round trip
        assert conn.execute(sa.text("SELECT count(*) FROM prescription")).scalar_one() == 1
        assert conn.execute(sa.text("SELECT count(*) FROM vital_sign")).scalar_one() == 1


def test_the_downgrade_refuses_while_a_record_came_from_a_document_and_deletes_nothing():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        context = _upgraded(conn)
        conn.execute(
            sa.text("INSERT INTO diagnosis (diagnosis_id, document_id) VALUES ('x2', 'd1')")
        )
        with (
            Operations.context(context),
            pytest.raises(RuntimeError, match="written from confirmed"),
        ):
            _migration().downgrade()
        assert conn.execute(sa.text("SELECT count(*) FROM diagnosis")).scalar_one() == 2
