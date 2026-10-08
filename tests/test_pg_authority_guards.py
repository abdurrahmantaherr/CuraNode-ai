"""Postgres-level regression tests for migration a3c1f0e7b2d4 (audit C1, H2).

The default suite runs on SQLite, which has no roles, no RLS and no triggers,
so it cannot exercise the Supabase Data API door at all. These tests do, on a
real Postgres:

* a throwaway database is created and dropped per test;
* a Supabase-shaped stub is installed — `anon`/`authenticated`/`service_role`
  roles, `auth.uid()`/`auth.role()` reading `request.jwt.claims` the way
  PostgREST sets it, `auth.users`, the production `is_admin()`, the
  production `handle_auth_user_update` sync trigger, and the production RLS
  policies for the guarded tables (copied from the live catalog);
* the migration's own SQL constants are executed — not a re-typed copy.

They are skipped unless `CURANODE_PG_TEST_URL` points at a LOCAL Postgres
superuser connection (e.g. `postgresql+asyncpg://postgres@127.0.0.1:5432/postgres`).
A non-local host is refused outright so this can never touch the shared
Supabase project. See docs/auth_hardening.md for how to run them.
"""

from __future__ import annotations

import importlib.util
import json
import os
import secrets
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

PG_URL = os.environ.get("CURANODE_PG_TEST_URL", "")

pytestmark = pytest.mark.skipif(
    not PG_URL, reason="CURANODE_PG_TEST_URL not set — Postgres-level guard tests skipped"
)

_MIGRATION = next(Path(__file__).resolve().parents[1].glob("alembic/versions/a3c1f0e7b2d4_*.py"))


def _load_migration():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("mig_a3c1f0e7b2d4", _MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STUB = [
    # Roles PostgREST switches into. NOLOGIN — they are only ever SET ROLE'd to.
    """DO $$ BEGIN
         IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN CREATE ROLE anon NOLOGIN; END IF;
         IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN CREATE ROLE authenticated NOLOGIN; END IF;
         IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN CREATE ROLE service_role NOLOGIN BYPASSRLS; END IF;
         IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cn_auth_admin_sim') THEN CREATE ROLE cn_auth_admin_sim NOLOGIN; END IF;
       END $$;""",
    # Supabase's service_role bypasses RLS; roles are cluster-wide, so set it
    # even when a previous run already created the role.
    "ALTER ROLE service_role BYPASSRLS",
    "CREATE SCHEMA auth",
    "CREATE TABLE auth.users (id uuid PRIMARY KEY, email varchar, phone varchar)",
    """CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS $$
         SELECT nullif(current_setting('request.jwt.claims', true)::jsonb ->> 'sub', '')::uuid $$""",
    """CREATE FUNCTION auth.role() RETURNS text LANGUAGE sql STABLE AS $$
         SELECT nullif(current_setting('request.jwt.claims', true)::jsonb ->> 'role', '') $$""",
    """CREATE TABLE public.user_profile (
         user_id uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
         email varchar(320), phone varchar(20),
         role varchar NOT NULL CHECK (role IN ('patient','doctor','staff','admin')),
         status varchar NOT NULL DEFAULT 'active', locale varchar NOT NULL DEFAULT 'en',
         full_name varchar(120), failed_logins int NOT NULL DEFAULT 0,
         locked_until timestamptz, is_synthetic boolean NOT NULL DEFAULT true)""",
    """CREATE TABLE public.doctor (
         doctor_id uuid PRIMARY KEY,
         user_id uuid NOT NULL UNIQUE REFERENCES public.user_profile(user_id) ON DELETE CASCADE,
         full_name varchar NOT NULL, specialty varchar,
         pmdc_number varchar NOT NULL UNIQUE,
         verification_status varchar NOT NULL DEFAULT 'pending',
         verified_by uuid REFERENCES public.user_profile(user_id), verified_at timestamptz,
         CONSTRAINT verified_has_verifier CHECK (verification_status <> 'verified'
           OR (verified_by IS NOT NULL AND verified_at IS NOT NULL)))""",
    """CREATE TABLE public.patient (
         patient_id uuid PRIMARY KEY,
         user_id uuid NOT NULL UNIQUE REFERENCES public.user_profile(user_id) ON DELETE CASCADE,
         passport_uid varchar(16) NOT NULL UNIQUE, full_name varchar NOT NULL)""",
    """CREATE TABLE public.consent_grant (
         consent_id uuid PRIMARY KEY,
         patient_id uuid NOT NULL REFERENCES public.patient(patient_id) ON DELETE CASCADE,
         grantee_type varchar NOT NULL, grantee_id uuid NOT NULL,
         revoked_at timestamptz, expires_at timestamptz)""",
    # Production definitions, verbatim from the live catalog.
    """CREATE FUNCTION public.is_admin() RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
         SET search_path TO 'public' AS $$
         SELECT EXISTS (SELECT 1 FROM public.user_profile WHERE user_id = auth.uid() AND role = 'admin') $$""",
    """CREATE FUNCTION public.handle_auth_user_update() RETURNS trigger LANGUAGE plpgsql
         SECURITY DEFINER SET search_path TO 'public' AS $$
         BEGIN UPDATE public.user_profile SET email = NEW.email, phone = NEW.phone
               WHERE user_id = NEW.id; RETURN NEW; END; $$""",
    """CREATE TRIGGER on_auth_user_updated AFTER UPDATE ON auth.users
         FOR EACH ROW EXECUTE FUNCTION public.handle_auth_user_update()""",
    "ALTER TABLE public.user_profile ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE public.doctor ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE public.patient ENABLE ROW LEVEL SECURITY",
    """CREATE POLICY user_profile_select_own_or_admin ON public.user_profile FOR SELECT
         USING ((user_id = auth.uid()) OR is_admin())""",
    """CREATE POLICY user_profile_update_own_or_admin ON public.user_profile FOR UPDATE
         USING ((user_id = auth.uid()) OR is_admin()) WITH CHECK ((user_id = auth.uid()) OR is_admin())""",
    "CREATE POLICY doctor_select_all_authenticated ON public.doctor FOR SELECT TO authenticated USING (true)",
    "CREATE POLICY doctor_insert_self ON public.doctor FOR INSERT WITH CHECK (user_id = auth.uid())",
    """CREATE POLICY doctor_update_own_or_admin ON public.doctor FOR UPDATE
         USING ((user_id = auth.uid()) OR is_admin()) WITH CHECK ((user_id = auth.uid()) OR is_admin())""",
    "CREATE POLICY patient_select ON public.patient FOR SELECT USING ((user_id = auth.uid()) OR is_admin())",
    """CREATE POLICY patient_update_own_or_admin ON public.patient FOR UPDATE
         USING ((user_id = auth.uid()) OR is_admin()) WITH CHECK ((user_id = auth.uid()) OR is_admin())""",
    # Supabase's default grants — table-wide, which is exactly the problem.
    "GRANT USAGE ON SCHEMA public, auth TO anon, authenticated, service_role, cn_auth_admin_sim",
    "GRANT ALL ON ALL TABLES IN SCHEMA public TO anon, authenticated, service_role",
    "GRANT SELECT, UPDATE ON auth.users TO cn_auth_admin_sim",
]

ADMIN = uuid.UUID(int=1)
DOC = uuid.UUID(int=2)
PAT = uuid.UUID(int=3)
DOCTOR_ROW = uuid.UUID(int=20)
PATIENT_ROW = uuid.UUID(int=30)


@pytest.fixture
async def pg_url():
    parts = urlsplit(PG_URL)
    if parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        pytest.fail("CURANODE_PG_TEST_URL must point at a local Postgres — refusing to run")
    db_name = f"cn_guard_test_{secrets.token_hex(4)}"
    admin = create_async_engine(PG_URL, isolation_level="AUTOCOMMIT")
    async with admin.connect() as c:
        await c.execute(text(f'CREATE DATABASE "{db_name}"'))
    url = parts._replace(path=f"/{db_name}").geturl()
    try:
        yield url
    finally:
        async with admin.connect() as c:
            await c.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        await admin.dispose()


@pytest.fixture
async def engine(pg_url):
    eng = create_async_engine(pg_url)
    mig = _load_migration()
    async with eng.begin() as c:
        for stmt in STUB:
            await c.execute(text(stmt))
        for stmt in mig.UPGRADE_STATEMENTS:
            await c.exec_driver_sql(stmt)
    yield eng
    await eng.dispose()


@pytest.fixture
async def conn(engine):
    """Fresh seed per test inside a transaction that is always rolled back."""
    async with engine.connect() as c:
        trans = await c.begin()
        for uid, email, role in (
            (ADMIN, "admin@x.com", "admin"),
            (DOC, "doc@x.com", "doctor"),
            (PAT, "pat@x.com", "patient"),
        ):
            await c.execute(
                text("INSERT INTO auth.users (id, email) VALUES (:i, :e)"), {"i": uid, "e": email}
            )
            await c.execute(
                text("INSERT INTO public.user_profile (user_id, email, role) VALUES (:i, :e, :r)"),
                {"i": uid, "e": email, "r": role},
            )
        await c.execute(
            text(
                "INSERT INTO public.doctor (doctor_id, user_id, full_name, pmdc_number) "
                "VALUES (:d, :u, 'Dr Test', '41192')"
            ),
            {"d": DOCTOR_ROW, "u": DOC},
        )
        await c.execute(
            text(
                "INSERT INTO public.patient (patient_id, user_id, passport_uid, full_name) "
                "VALUES (:p, :u, 'CN-AAAA-BBBB', 'Pat')"
            ),
            {"p": PATIENT_ROW, "u": PAT},
        )
        try:
            yield c
        finally:
            await trans.rollback()


async def _as(c: AsyncConnection, role: str, sub: uuid.UUID | None = None) -> None:
    """Become a PostgREST request: SET LOCAL ROLE + the JWT claims GUC."""
    claims = {"role": role, **({"sub": str(sub)} if sub else {})}
    await c.execute(text(f"SET LOCAL ROLE {role}"))
    await c.execute(
        text("SELECT set_config('request.jwt.claims', :c, true)"), {"c": json.dumps(claims)}
    )


async def _reset(c: AsyncConnection) -> None:
    await c.execute(text("RESET ROLE"))
    await c.execute(text("SELECT set_config('request.jwt.claims', '', true)"))


async def _denied(c: AsyncConnection, sql: str, params: dict | None = None) -> None:
    """Runs `sql` in a savepoint and asserts the guard refused it (42501)."""
    nested = await c.begin_nested()
    with pytest.raises(DBAPIError) as exc:
        await c.execute(text(sql), params or {})
    await nested.rollback()
    assert "42501" in str(getattr(exc.value.orig, "sqlstate", "")) or "42501" in repr(
        exc.value.orig
    ), exc.value


async def _scalar(c: AsyncConnection, sql: str, params: dict | None = None):  # type: ignore[no-untyped-def]
    return (await c.execute(text(sql), params or {})).scalar_one()


# ── C1 — doctor authority columns ────────────────────────────────────────
@pytest.mark.parametrize(
    "assignment",
    [
        "verification_status = 'verified', verified_by = user_id, verified_at = now()",
        "verified_by = user_id",
        "verified_at = now()",
        "pmdc_number = '99999'",
        f"user_id = '{PAT}'",
    ],
)
async def test_doctor_cannot_change_authority_columns(conn, assignment):
    await _as(conn, "authenticated", DOC)
    await _denied(conn, f"UPDATE public.doctor SET {assignment} WHERE user_id = '{DOC}'")
    await _reset(conn)
    assert (
        await _scalar(conn, f"SELECT verification_status FROM public.doctor WHERE user_id='{DOC}'")
        == "pending"
    )


async def test_doctor_can_still_edit_ordinary_columns(conn):
    await _as(conn, "authenticated", DOC)
    await conn.execute(
        text(f"UPDATE public.doctor SET specialty = 'Cardiology' WHERE user_id = '{DOC}'")
    )
    await _reset(conn)
    assert (
        await _scalar(conn, f"SELECT specialty FROM public.doctor WHERE user_id='{DOC}'")
        == "Cardiology"
    )


async def test_self_insert_of_a_preverified_doctor_is_refused(conn):
    other = uuid.uuid4()
    await conn.execute(text("INSERT INTO auth.users (id) VALUES (:i)"), {"i": other})
    await conn.execute(
        text(
            "INSERT INTO public.user_profile (user_id, email, role) VALUES (:i, 'o@x.com', 'doctor')"
        ),
        {"i": other},
    )
    await _as(conn, "authenticated", other)
    await _denied(
        conn,
        "INSERT INTO public.doctor (doctor_id, user_id, full_name, pmdc_number, "
        "verification_status, verified_by, verified_at) "
        "VALUES (gen_random_uuid(), :u, 'X', '55555', 'verified', :u, now())",
        {"u": other},
    )
    # A PENDING self-insert (the registration shape) is still allowed.
    await conn.execute(
        text(
            "INSERT INTO public.doctor (doctor_id, user_id, full_name, pmdc_number) "
            "VALUES (gen_random_uuid(), :u, 'X', '55555')"
        ),
        {"u": other},
    )


async def test_admin_can_still_verify_a_doctor(conn):
    await _as(conn, "authenticated", ADMIN)
    await conn.execute(
        text(
            "UPDATE public.doctor SET verification_status='verified', verified_by=:a, "
            "verified_at=now() WHERE user_id = :d"
        ),
        {"a": ADMIN, "d": DOC},
    )
    await _reset(conn)
    assert (
        await _scalar(conn, f"SELECT verification_status FROM public.doctor WHERE user_id='{DOC}'")
        == "verified"
    )


@pytest.mark.parametrize("role", ["service_role", None])
async def test_service_role_and_app_connection_are_privileged(conn, role):
    # None = this app's own `postgres` connection (BYPASSRLS, no JWT).
    if role:
        await _as(conn, role)
    await conn.execute(
        text(
            "UPDATE public.doctor SET verification_status='verified', verified_by=:a, "
            "verified_at=now(), pmdc_number='77777' WHERE user_id = :d"
        ),
        {"a": ADMIN, "d": DOC},
    )
    await _reset(conn)
    assert (
        await _scalar(conn, f"SELECT pmdc_number FROM public.doctor WHERE user_id='{DOC}'")
        == "77777"
    )


# ── H2 — user_profile authority columns ──────────────────────────────────
@pytest.mark.parametrize(
    "assignment",
    [
        "email = 'victim@x.com'",
        "is_synthetic = false",
        "failed_logins = 5",
        "locked_until = now() + interval '1 day'",
    ],
)
async def test_user_cannot_change_profile_authority_columns(conn, assignment):
    await _as(conn, "authenticated", PAT)
    await _denied(conn, f"UPDATE public.user_profile SET {assignment} WHERE user_id = '{PAT}'")


async def test_user_can_still_edit_ordinary_profile_columns(conn):
    await _as(conn, "authenticated", PAT)
    await conn.execute(
        text(
            f"UPDATE public.user_profile SET full_name='Ayesha', phone='+923001234567' WHERE user_id='{PAT}'"
        )
    )
    await _reset(conn)
    assert (
        await _scalar(conn, f"SELECT full_name FROM public.user_profile WHERE user_id='{PAT}'")
        == "Ayesha"
    )


async def test_auth_email_sync_trigger_still_works(conn):
    # GoTrue updates auth.users as a non-superuser; the SECURITY DEFINER sync
    # trigger must still be able to write user_profile.email.
    await conn.execute(text("SET LOCAL ROLE cn_auth_admin_sim"))
    await conn.execute(text(f"UPDATE auth.users SET email = 'new@x.com' WHERE id = '{PAT}'"))
    await _reset(conn)
    assert (
        await _scalar(conn, f"SELECT email FROM public.user_profile WHERE user_id='{PAT}'")
        == "new@x.com"
    )


# ── H2 — patient authority columns ───────────────────────────────────────
@pytest.mark.parametrize("assignment", ["passport_uid = 'CN-ZZZZ-ZZZZ'", f"user_id = '{DOC}'"])
async def test_patient_cannot_change_passport_or_owner(conn, assignment):
    await _as(conn, "authenticated", PAT)
    await _denied(conn, f"UPDATE public.patient SET {assignment} WHERE user_id = '{PAT}'")


async def test_patient_can_still_edit_ordinary_columns(conn):
    await _as(conn, "authenticated", PAT)
    await conn.execute(text(f"UPDATE public.patient SET full_name='Pat B' WHERE user_id='{PAT}'"))


# ── Integrity indexes ────────────────────────────────────────────────────
async def test_email_uniqueness_is_case_insensitive(conn):
    other = uuid.uuid4()
    await conn.execute(text("INSERT INTO auth.users (id) VALUES (:i)"), {"i": other})
    nested = await conn.begin_nested()
    with pytest.raises(DBAPIError, match="uq_user_profile_email_lower"):
        await conn.execute(
            text(
                "INSERT INTO public.user_profile (user_id, email, role) VALUES (:i, 'PAT@X.com', 'patient')"
            ),
            {"i": other},
        )
    await nested.rollback()


async def test_downgrade_runs_cleanly(conn):
    mig = _load_migration()
    nested = await conn.begin_nested()
    for stmt in mig.DOWNGRADE_STATEMENTS:
        await conn.exec_driver_sql(stmt)
    await nested.rollback()


# ── L12 — migration b7d2e9c4a1f6 (TRUNCATE revoked from API roles) ───────
async def test_truncate_is_revoked_from_api_roles(conn):
    spec = importlib.util.spec_from_file_location(
        "mig_b7d2e9c4a1f6",
        next(Path(__file__).resolve().parents[1].glob("alembic/versions/b7d2e9c4a1f6_*.py")),
    )
    assert spec and spec.loader
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    for table in mig.TABLES:  # the stub only models some of them
        await conn.execute(text(f"CREATE TABLE IF NOT EXISTS public.{table} (id int)"))
    await conn.execute(text("GRANT ALL ON ALL TABLES IN SCHEMA public TO anon, authenticated"))

    def can_truncate(role: str) -> str:
        return f"SELECT has_table_privilege('{role}', 'public.user_profile', 'TRUNCATE')"

    assert await _scalar(conn, can_truncate("authenticated")) is True
    await conn.exec_driver_sql(mig._statement("REVOKE"))
    for role in mig.ROLES:
        assert await _scalar(conn, can_truncate(role)) is False
    # Everything RLS governs is untouched.
    assert await _scalar(
        conn, "SELECT has_table_privilege('authenticated', 'public.user_profile', 'UPDATE')"
    )
    await conn.exec_driver_sql(mig._statement("GRANT"))  # downgrade restores it
    assert await _scalar(conn, can_truncate("authenticated")) is True
