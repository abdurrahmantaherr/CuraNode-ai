"""RECONSTRUCTED placeholder for an upstream revision (document / OCR work, round 2)

This file is NOT the original migration. It exists so this repo's revision
chain reaches the revision the shared Supabase database is actually at
(`e3a52c8d7f14`), so that this repo's own revisions can be chained after it.

What happened
-------------
The shared database is migrated by more than one codebase through the same
`alembic_version` table. After `35c4bc13fa37` (itself a reconstructed
placeholder, see that file) was written, the document-upload / OCR codebase
moved the database on to `e3a52c8d7f14` on 2026-10-08. Its file was never
added to this repo, and no file, commit, branch, stash or worktree here refers
to that id. Found read-only on 2026-10-10 (D7 step T0).

Evidence for the parent (`35c4bc13fa37`):

  * The database was at `35c4bc13fa37` when it was checked on 2026-10-07/08
    (that placeholder and P9's migration both record it).
  * `UPDATE alembic_version` has run 5 times since pg_stat_statements was
    reset on 2026-09-17: once for this repo's `488dbf2b3915`, twice for the
    pair `35c4bc13fa37` stands for, so twice after `35c4bc13fa37`.
  * No downgrade traces: no `DROP COLUMN`, and no `DELETE FROM` or
    `INSERT INTO alembic_version`.

What is NOT known:

  * The direct parent. Two version updates happened after `35c4bc13fa37`, so
    there may be one unknown revision between it and `e3a52c8d7f14`. This
    placeholder declares `down_revision = "35c4bc13fa37"` and stands in for
    one or two upstream revisions.
  * Which statement belonged to which revision, and their downgrade code.

DDL recovered from pg_stat_statements (first-run times from `stats_since`,
UTC; each ran once, as role `postgres`). Only these two statements ran after
`35c4bc13fa37` was reconstructed:

    -- 2026-10-08 14:14:08
    ALTER TABLE ocr_job ADD COLUMN ai_sections JSONB

    -- 2026-10-08 16:15:51 (the DROP's second recorded run; its first was 09-27)
    ALTER TABLE public.ocr_extraction DROP CONSTRAINT ocr_extraction_field_name_check
    ALTER TABLE public.ocr_extraction ADD CONSTRAINT ocr_extraction_field_name_check CHECK
        (field_name IN ('medicine', 'dosage', 'frequency', 'duration', 'test_name',
        'result', 'unit', 'reference_range', 'vaccine_name', 'dose', 'date_given',
        'batch_no', 'site', 'next_due', 'diagnosis', 'symptom', 'blood_pressure',
        'pulse', 'temperature', 'respiratory_rate', 'spo2', 'weight', 'height', 'bmi',
        'blood_sugar', 'allergy_substance', 'allergy_reaction', 'allergy_severity'))

(`ocr_extraction.row_index` and `idx_ocr_extraction_job` ran on 2026-09-27
and belong to `35c4bc13fa37`, not here.)

Why upgrade() does nothing
--------------------------
Both changes are already in the shared database, and this repo neither maps
nor creates `ocr_job` or `ocr_extraction`. Re-running the DDL would fail on a
database that has it and is meaningless on one that doesn't have those tables.

Why downgrade() refuses
-----------------------
Those changes belong to another team's codebase and hold live (synthetic)
data. Undoing them from this repo would be wrong and unrecoverable.

If the real upstream migration files turn up, replace this file with them
(keeping revision id e3a52c8d7f14 as the last of them) and drop this
placeholder.

Revision ID: e3a52c8d7f14
Revises: 35c4bc13fa37
Create Date: 2026-10-10 12:00:00.000000

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "e3a52c8d7f14"
down_revision: str | Sequence[str] | None = "35c4bc13fa37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # No-op by design: see the module docstring. The shared database already
    # has every change this revision stands for.
    pass


def downgrade() -> None:
    raise RuntimeError(
        "e3a52c8d7f14 is a reconstructed placeholder for the document/OCR codebase's "
        "migrations. Downgrading past it from this repo would undo another team's schema "
        "changes, and their real downgrade code is unknown. Refusing."
    )
