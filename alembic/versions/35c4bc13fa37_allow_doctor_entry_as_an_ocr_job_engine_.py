"""allow 'doctor_entry' as an ocr_job engine value

Additive only, hand-written, same pattern as `c3a91d7e5f20`'s own widening of
this exact constraint (which added `trocr`).

A doctor-authored structured prescription (FR: doctor-generated documents)
still needs an `ocr_job` row — `ocr_extraction.job_id` is a NOT NULL foreign
key to it, and that is how a doctor-entered prescription's fields become
reachable through the existing document/detail system alongside a patient
upload's. But no OCR engine ever runs on it: the doctor's structured input
goes straight to normalization. `engine = 'tesseract'` would be a false
record of what happened; `'doctor_entry'` says so honestly. Nothing
previously allowed is disallowed now.

Revision ID: 35c4bc13fa37
Revises: c3a91d7e5f20
Create Date: 2026-09-29 20:59:38.564366

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "35c4bc13fa37"
down_revision: str | Sequence[str] | None = "c3a91d7e5f20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # SQLite (tests) has no CHECK on this column at all — the test DB is
    # built straight from the SQLAlchemy models, so there is nothing to
    # widen there; only the real, Postgres-hosted constraint needs it.
    if _is_postgres():
        op.execute("ALTER TABLE public.ocr_job DROP CONSTRAINT ocr_job_engine_check")
        op.execute(
            "ALTER TABLE public.ocr_job ADD CONSTRAINT ocr_job_engine_check "
            "CHECK (engine IN ('vision_llm', 'gcv', 'tesseract', 'trocr', 'doctor_entry'))"
        )


def downgrade() -> None:
    """WARNING: fails if any `doctor_entry` ocr_job row still exists —
    remove those first."""
    if _is_postgres():
        op.execute("ALTER TABLE public.ocr_job DROP CONSTRAINT ocr_job_engine_check")
        op.execute(
            "ALTER TABLE public.ocr_job ADD CONSTRAINT ocr_job_engine_check "
            "CHECK (engine IN ('vision_llm', 'gcv', 'tesseract', 'trocr'))"
        )
