"""records from confirmed documents: let visit-bound rows come from a document instead

Hand-written. When a patient confirms a document, what they confirmed is also written to the typed
clinical tables (prescription + prescription_item, diagnosis, vital_sign, lab_report + lab_result).
Three of those tables were built for a doctor's visit: `prescription` requires an `encounter_id` AND a
`doctor_id`, `diagnosis` and `vital_sign` require an `encounter_id`, and `prescription_item` requires a
`medicine_id` from the shared catalogue. A patient's upload has none of those, and inventing a visit,
a doctor or a catalogue entry for it would put things in front of doctors that never happened. So the
rule changes from "always a visit" to "a visit, OR the document it was read from":

* `prescription.encounter_id`, `prescription.doctor_id`, `diagnosis.encounter_id`,
  `vital_sign.encounter_id`, `prescription_item.medicine_id` become NULLable.
* `prescription`, `diagnosis` and `vital_sign` gain `document_id` (the document the row came from;
  NULL for a doctor's own rows). `prescription_item` gains `medicine_text` (the name as written on the
  paper, for a medicine with no catalogue entry — the catalogue is shared reference data and is not
  written from a patient's upload).
* CHECKs keep the tables honest: a row with no visit MUST name its document; an item must have a
  catalogue medicine or a written name; so nothing can become an anonymous orphan.
* One derived prescription and one derived vital-sign row per document (partial unique indexes), so
  a repeated or retried projection can never double a record.

Existing rows are untouched and every row that exists today satisfies the new CHECKs (each has a
visit), so this is a relaxing change: every writer that exists keeps working unchanged. The doctor
pages and the timeline only ever read visit-bound rows, and say so (they join through the visit).

The downgrade REFUSES to run while any document-derived row exists — it never deletes a patient's
records to make a constraint fit. Remove those deliberately first.

Check before applying: `SELECT count(*) FROM prescription` / `diagnosis` / `vital_sign` (names and
counts only). **ASK ME BEFORE RUNNING on the shared project** — the schema owner applies it, and
applies `92b61c6e9db4` (the booking index) first if it is not yet applied.

Revision ID: c8e5a41f9b27
Revises: 92b61c6e9db4
Create Date: 2026-10-15 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c8e5a41f9b27"
down_revision: str | Sequence[str] | None = "92b61c6e9db4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# table -> the CHECK that says "a visit, or the document it came from"
_VISIT_OR_DOCUMENT = {
    "prescription": (
        "ck_prescription_visit_or_document",
        "(encounter_id IS NOT NULL AND doctor_id IS NOT NULL) OR document_id IS NOT NULL",
    ),
    "diagnosis": (
        "ck_diagnosis_visit_or_document",
        "encounter_id IS NOT NULL OR document_id IS NOT NULL",
    ),
    "vital_sign": (
        "ck_vital_sign_visit_or_document",
        "encounter_id IS NOT NULL OR document_id IS NOT NULL",
    ),
}
_ITEM_CHECK = (
    "ck_prescription_item_medicine_or_text",
    "medicine_id IS NOT NULL OR medicine_text IS NOT NULL",
)


def _fk_name(table: str) -> str:
    return f"fk_{table}_document_id"


def _document_link(table: str) -> sa.Column:
    # Named, so a batch operation (SQLite, in the tests) and a later downgrade can find it.
    return sa.Column(
        "document_id",
        sa.Uuid(),
        sa.ForeignKey("document.document_id", ondelete="CASCADE", name=_fk_name(table)),
        nullable=True,
    )


def upgrade() -> None:
    # `batch_alter_table` lets SQLite (the tests) run this; on Postgres it emits plain ALTER TABLEs.
    # A row no longer needs a visit / doctor / catalogue entry — but must have the alternative.
    with op.batch_alter_table("prescription") as batch:
        batch.add_column(_document_link("prescription"))
        batch.alter_column("encounter_id", existing_type=sa.Uuid(), nullable=True)
        batch.alter_column("doctor_id", existing_type=sa.Uuid(), nullable=True)
        batch.create_check_constraint(*_VISIT_OR_DOCUMENT["prescription"])
    with op.batch_alter_table("diagnosis") as batch:
        batch.add_column(_document_link("diagnosis"))
        batch.alter_column("encounter_id", existing_type=sa.Uuid(), nullable=True)
        batch.create_check_constraint(*_VISIT_OR_DOCUMENT["diagnosis"])
    with op.batch_alter_table("vital_sign") as batch:
        batch.add_column(_document_link("vital_sign"))
        batch.alter_column("encounter_id", existing_type=sa.Uuid(), nullable=True)
        batch.create_check_constraint(*_VISIT_OR_DOCUMENT["vital_sign"])
    with op.batch_alter_table("prescription_item") as batch:
        batch.add_column(sa.Column("medicine_text", sa.String(255), nullable=True))
        batch.alter_column("medicine_id", existing_type=sa.Uuid(), nullable=True)
        batch.create_check_constraint(*_ITEM_CHECK)

    # Lookups by document, and one derived prescription / vital-sign row per document.
    op.create_index(
        "uq_prescription_document",
        "prescription",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("document_id IS NOT NULL"),
        sqlite_where=sa.text("document_id IS NOT NULL"),
    )
    op.create_index(
        "uq_vital_sign_document",
        "vital_sign",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("document_id IS NOT NULL"),
        sqlite_where=sa.text("document_id IS NOT NULL"),
    )
    op.create_index("idx_diagnosis_document_id", "diagnosis", ["document_id"])


def _refuse_while(table: str, condition: str, why: str) -> None:
    """Stop the downgrade — never delete a patient's records to make a constraint fit. On Postgres
    it is a DO block, so it also shows in the offline SQL (`alembic downgrade --sql`) and runs
    inside the transaction; on any other database (the tests' SQLite) it is a plain check."""
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DO $$ BEGIN "
            f"IF EXISTS (SELECT 1 FROM {table} WHERE {condition}) "
            f"THEN RAISE EXCEPTION '{why}'; END IF; END $$"
        )
        return
    found = op.get_bind().execute(sa.text(f"SELECT count(*) FROM {table} WHERE {condition}"))
    if found.scalar_one():
        raise RuntimeError(why)


def downgrade() -> None:
    for table in ("prescription", "diagnosis", "vital_sign"):
        _refuse_while(
            table,
            "document_id IS NOT NULL",
            f"{table} holds rows written from confirmed documents; "
            "remove them deliberately before downgrading (this never deletes records)",
        )
    _refuse_while(
        "prescription_item",
        "medicine_id IS NULL",
        "prescription_item holds rows with no catalogue medicine; "
        "remove them deliberately before downgrading (this never deletes records)",
    )

    op.drop_index("idx_diagnosis_document_id", table_name="diagnosis")
    op.drop_index("uq_vital_sign_document", table_name="vital_sign")
    op.drop_index("uq_prescription_document", table_name="prescription")

    with op.batch_alter_table("prescription_item") as batch:
        batch.drop_constraint(_ITEM_CHECK[0], type_="check")
        batch.alter_column("medicine_id", existing_type=sa.Uuid(), nullable=False)
        batch.drop_column("medicine_text")
    with op.batch_alter_table("vital_sign") as batch:
        batch.drop_constraint(_VISIT_OR_DOCUMENT["vital_sign"][0], type_="check")
        batch.drop_constraint(_fk_name("vital_sign"), type_="foreignkey")
        batch.alter_column("encounter_id", existing_type=sa.Uuid(), nullable=False)
        batch.drop_column("document_id")
    with op.batch_alter_table("diagnosis") as batch:
        batch.drop_constraint(_VISIT_OR_DOCUMENT["diagnosis"][0], type_="check")
        batch.drop_constraint(_fk_name("diagnosis"), type_="foreignkey")
        batch.alter_column("encounter_id", existing_type=sa.Uuid(), nullable=False)
        batch.drop_column("document_id")
    with op.batch_alter_table("prescription") as batch:
        batch.drop_constraint(_VISIT_OR_DOCUMENT["prescription"][0], type_="check")
        batch.drop_constraint(_fk_name("prescription"), type_="foreignkey")
        batch.alter_column("doctor_id", existing_type=sa.Uuid(), nullable=False)
        batch.alter_column("encounter_id", existing_type=sa.Uuid(), nullable=False)
        batch.drop_column("document_id")
