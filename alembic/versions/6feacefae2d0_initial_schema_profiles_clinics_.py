"""reconcile auth schema with the shared CuraNode-AI Supabase project

This project's Supabase database already has a larger, pre-existing schema
(`user_profile`, `clinic`, `patient`, `doctor`, `clinic_staff`,
`doctor_affiliation`, ...) owned by the wider CuraNode-AI product — this
migration does NOT create those tables. It only:

1. Adds columns this auth feature needs that the shared schema didn't have
   yet (lockout tracking, the synthetic-data flag, full_name, and a named
   verifier for doctor verification).
2. Creates `audit_log`, which is owned outright by this codebase (the
   shared schema's `access_log` is shaped for clinical record-access/
   consent auditing and doesn't fit generic auth events).

Revision ID: 6feacefae2d0
Revises:
Create Date: 2026-08-23 15:24:03.752254

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6feacefae2d0"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user_profile", sa.Column("full_name", sa.String(120), nullable=True))
    op.add_column(
        "user_profile",
        sa.Column("failed_logins", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "user_profile", sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "user_profile", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "user_profile",
        sa.Column("is_synthetic", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index("idx_user_profile_role_status", "user_profile", ["role", "status"])

    op.add_column(
        "doctor", sa.Column("verified_by", sa.Uuid(), sa.ForeignKey("user_profile.user_id"))
    )
    op.create_check_constraint(
        "verified_has_verifier",
        "doctor",
        "verification_status <> 'verified' "
        "OR (verified_by IS NOT NULL AND verified_at IS NOT NULL)",
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("actor_role", sa.String(16), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("subject_patient_id", sa.Uuid(), nullable=True),
        sa.Column("resource_type", sa.String(40), nullable=True),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=False, server_default="{}"),
    )
    op.create_index("idx_audit_actor", "audit_log", ["actor_user_id", "occurred_at"])


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_constraint("verified_has_verifier", "doctor", type_="check")
    op.drop_column("doctor", "verified_by")
    op.drop_index("idx_user_profile_role_status", table_name="user_profile")
    op.drop_column("user_profile", "is_synthetic")
    op.drop_column("user_profile", "last_login_at")
    op.drop_column("user_profile", "locked_until")
    op.drop_column("user_profile", "failed_logins")
    op.drop_column("user_profile", "full_name")
