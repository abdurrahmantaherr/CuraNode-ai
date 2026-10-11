"""doctor verification v1: doctor_verification, clinic_invite_code

Additive only, hand-written. Both tables are owned outright by this repo —
neither exists in the shared schema. `doctor_verification` is the history
behind the shared `doctor.verification_status` column (unchanged here);
`clinic_invite_code` gates clinic-admin self-sign-up.

Revision ID: a7d3c89106f9
Revises: 291c06a2b86f
Create Date: 2026-10-11 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7d3c89106f9"
down_revision: str | Sequence[str] | None = "291c06a2b86f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "doctor_verification",
        sa.Column("verification_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "doctor_id", sa.Uuid(), sa.ForeignKey("doctor.doctor_id"), nullable=False
        ),
        sa.Column(
            "decided_by", sa.Uuid(), sa.ForeignKey("user_profile.user_id"), nullable=False
        ),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(32), nullable=True),
        sa.Column("checked_number", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("checked_name", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("checked_licence", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "decided_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "idx_doctor_verification_doctor", "doctor_verification", ["doctor_id", "decided_at"]
    )

    op.create_table(
        "clinic_invite_code",
        sa.Column(
            "clinic_id", sa.Uuid(), sa.ForeignKey("clinic.clinic_id"), primary_key=True
        ),
        sa.Column("code_hash", sa.String(128), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("clinic_invite_code")
    op.drop_index("idx_doctor_verification_doctor", table_name="doctor_verification")
    op.drop_table("doctor_verification")
