"""revoke TRUNCATE from the Supabase API roles on identity/clinical tables

Supabase's default grants give `anon` and `authenticated` every table
privilege, including TRUNCATE. Row-level security does NOT apply to TRUNCATE.
PostgREST never issues it, so this is not reachable through the Data API
today — but a role that must never be able to empty `user_profile` or
`audit_log` should not hold the privilege at all (audit L12).

Only TRUNCATE is revoked; SELECT/INSERT/UPDATE/DELETE stay exactly as the
shared schema's RLS model expects. Each REVOKE runs only if the role exists,
so the migration is a no-op on a plain (non-Supabase) Postgres.

Revision ID: b7d2e9c4a1f6
Revises: a3c1f0e7b2d4
Create Date: 2026-10-08 12:30:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d2e9c4a1f6"
down_revision: str | Sequence[str] | None = "a3c1f0e7b2d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES: tuple[str, ...] = (
    "user_profile",
    "clinic",
    "clinic_staff",
    "doctor",
    "doctor_affiliation",
    "patient",
    "consent_grant",
    "allergy",
    "chronic_condition",
    "patient_medication",
    "audit_log",
)
ROLES: tuple[str, ...] = ("anon", "authenticated")


def _statement(verb: str) -> str:
    preposition = "FROM" if verb == "REVOKE" else "TO"
    tables = ", ".join(f"public.{t}" for t in TABLES)
    checks = " ".join(
        f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
        f"EXECUTE '{verb} TRUNCATE ON {tables} {preposition} {role}'; END IF;"
        for role in ROLES
    )
    return f"DO $$ BEGIN {checks} END $$;"


def upgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return
    op.execute(_statement("REVOKE"))


def downgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return
    op.execute(_statement("GRANT"))
