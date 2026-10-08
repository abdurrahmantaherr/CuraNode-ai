"""RECONSTRUCTED placeholder for an upstream revision (document / OCR work)

This file is NOT the original migration. It exists so this repo's revision
chain reaches the revision the shared Supabase database is actually at, and
`alembic current` / `alembic upgrade` work from here again.

What happened
-------------
The shared database is migrated by more than one codebase through the same
`alembic_version` table. Between about 2026-09-24 and 2026-09-29, the
document-upload / OCR codebase applied two revisions on top of this repo's
`488dbf2b3915`, ending at `35c4bc13fa37`. Their files were never added to this
repo. Found and reconstructed, read-only, on 2026-10-07/08 (P9 step 0).

  * The id of the FIRST upstream revision (between 488dbf2b3915 and
    35c4bc13fa37) is unknown: Alembic stores only the current id. This
    placeholder therefore declares `down_revision = "488dbf2b3915"` and stands
    in for both upstream revisions.
  * Which statement belonged to which of the two revisions is unknown.
  * Their downgrade code is unknown.

DDL recovered from the database's statement statistics (pg_stat_statements,
reset 2026-09-17; every statement ran once unless noted, as role `postgres`):

    ALTER TABLE document ADD COLUMN original_filename VARCHAR(255)
    ALTER TABLE document ADD COLUMN status VARCHAR(20) DEFAULT 'finalized' NOT NULL
    ALTER TABLE document ADD COLUMN failure_code VARCHAR(40)
    ALTER TABLE document ADD COLUMN ocr_text TEXT
    ALTER TABLE document ADD COLUMN document_date DATE
    ALTER TABLE document ADD COLUMN finalized_at TIMESTAMP WITH TIME ZONE
    ALTER TABLE document ADD COLUMN ocr_mode VARCHAR(12) DEFAULT 'printed' NOT NULL
    ALTER TABLE public.document ADD CONSTRAINT document_status_check CHECK (status IN
        ('uploaded', 'processing', 'awaiting_review', 'finalized', 'failed', 'cancelled'))
    ALTER TABLE public.document ADD CONSTRAINT document_ocr_mode_check CHECK (ocr_mode IN
        ('printed', 'handwritten'))
    ALTER TABLE public.document DROP CONSTRAINT document_doc_type_check
    ALTER TABLE public.document ADD CONSTRAINT document_doc_type_check CHECK (doc_type IN
        ('rx', 'lab', 'scan', 'other', 'vaccine'))
    CREATE INDEX idx_document_patient_status ON document (patient_id, status)
    ALTER TABLE public.ocr_job DROP CONSTRAINT ocr_job_engine_check            -- ran twice
    ALTER TABLE public.ocr_job ADD CONSTRAINT ocr_job_engine_check CHECK (engine IN
        ('vision_llm', 'gcv', 'tesseract', 'trocr'))
    ALTER TABLE public.ocr_job ADD CONSTRAINT ocr_job_engine_check CHECK (engine IN
        ('vision_llm', 'gcv', 'tesseract', 'trocr', 'doctor_entry'))
    ALTER TABLE ocr_extraction ADD COLUMN row_index INTEGER
    ALTER TABLE public.ocr_extraction DROP CONSTRAINT ocr_extraction_field_name_check
    ALTER TABLE public.ocr_extraction ADD CONSTRAINT ocr_extraction_field_name_check CHECK
        (field_name IN ('medicine', 'dosage', 'frequency', 'duration', 'test_name',
        'result', 'unit', 'reference_range'))
    CREATE INDEX idx_ocr_extraction_job ON ocr_extraction (job_id)

Data dates: the first `document` rows using the new columns are from
2026-09-27; the first `ocr_job` with engine 'doctor_entry' is from 2026-09-29.

Why upgrade() does nothing
--------------------------
Every change above is already in the shared database, and this repo neither
maps nor creates `document`, `ocr_job` or `ocr_extraction`. Re-running the
DDL would fail on a database that has it and is meaningless on one that
doesn't have those tables at all.

Why downgrade() refuses
-----------------------
Those changes belong to another team's codebase and hold live (synthetic)
data. Undoing them from this repo would be wrong and unrecoverable.

If the real upstream migration files turn up, replace this file with them
(keeping revision id 35c4bc13fa37 as the head of their pair) and drop this
placeholder.

Revision ID: 35c4bc13fa37
Revises: 488dbf2b3915
Create Date: 2026-10-08 12:00:00.000000

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "35c4bc13fa37"
down_revision: str | Sequence[str] | None = "488dbf2b3915"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # No-op by design: see the module docstring. The shared database already
    # has every change this revision stands for.
    pass


def downgrade() -> None:
    raise RuntimeError(
        "35c4bc13fa37 is a reconstructed placeholder for the document/OCR codebase's "
        "migrations. Downgrading past it from this repo would undo another team's schema "
        "changes, and their real downgrade code is unknown. Refusing."
    )
