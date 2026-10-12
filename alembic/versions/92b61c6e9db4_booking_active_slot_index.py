"""booking v1: one active appointment per schedule time

Additive only, hand-written. A partial unique index on the shared `appointment`
table, which the booking work (P5) owns: at most one non-cancelled appointment
per `(schedule_id, slot_start)`. The booking service already refuses a taken
time under a per-doctor lock; this makes the database refuse it too, so two
patients can never hold one time even if a race, a script or another tool
slips past the app. The table had no rows on the shared project when this was
written; re-check before applying (`SELECT count(*) FROM appointment`).

Revision ID: 92b61c6e9db4
Revises: a7d3c89106f9
Create Date: 2026-10-12 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "92b61c6e9db4"
down_revision: str | Sequence[str] | None = "a7d3c89106f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_appointment_active_slot",
        "appointment",
        ["schedule_id", "slot_start"],
        unique=True,
        postgresql_where=sa.text("status <> 'cancelled'"),
        sqlite_where=sa.text("status <> 'cancelled'"),
    )


def downgrade() -> None:
    op.drop_index("uq_appointment_active_slot", table_name="appointment")
