"""RECONSTRUCTED placeholder for an upstream revision (document / OCR work, ocr_job.ai_sections)

This file is NOT the original migration. It exists so this repo's revision
chain reaches the OCR codebase's real `e3a52c8d7f14`
(`e3a52c8d7f14_ocr_extraction_more_field_names.py`, supplied by the schema
owner on 2026-10-10), whose `down_revision` is `b7e24c19a053`, a revision whose
file is in no branch, commit or worktree of this repo.

What is known
-------------
* `b7e24c19a053` is the parent of `e3a52c8d7f14` (stated by that real file).
* Its parent is taken to be `35c4bc13fa37` (itself a reconstructed placeholder
  for the OCR codebase's first pair). Evidence: `UPDATE alembic_version` has run
  5 times since pg_stat_statements was reset on 2026-09-17 — once for this
  repo's `488dbf2b3915`, twice for the pair ending at `35c4bc13fa37`, and
  exactly twice after it: `b7e24c19a053`, then `e3a52c8d7f14`. The database was
  at `35c4bc13fa37` when checked on 2026-10-07/08, and there are no downgrade
  traces. Not proven by a file.
* The only DDL that ran after `35c4bc13fa37` and is NOT in the real
  `e3a52c8d7f14` (which only widens `ocr_extraction_field_name_check`) is,
  read-only from pg_stat_statements (first run 2026-10-08 14:14:08 UTC, once,
  as role `postgres`):

      ALTER TABLE ocr_job ADD COLUMN ai_sections JSONB

What is NOT known: anything else the original did, and its downgrade.

Why upgrade() does nothing
--------------------------
The change is already in the shared database, and this repo neither maps nor
creates `ocr_job`. Re-running it would fail where it exists and is meaningless
where the table does not.

Why downgrade() refuses
-----------------------
It belongs to another team's codebase and holds live (synthetic) data; its real
downgrade is unknown. Because Postgres DDL is transactional and Alembic runs a
downgrade in one transaction, this refusal also rolls back any downgrade steps
run before it in the same command.

If the real `b7e24c19a053` file turns up, replace this file with it.

Revision ID: b7e24c19a053
Revises: 35c4bc13fa37
Create Date: 2026-10-10 20:00:00.000000

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "b7e24c19a053"
down_revision: str | Sequence[str] | None = "35c4bc13fa37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # No-op by design: see the module docstring. The shared database already
    # has the change this revision stands for.
    pass


def downgrade() -> None:
    raise RuntimeError(
        "b7e24c19a053 is a reconstructed placeholder for the document/OCR codebase's "
        "migration. Downgrading past it from this repo would undo another team's schema "
        "change, and its real downgrade code is unknown. Refusing."
    )
