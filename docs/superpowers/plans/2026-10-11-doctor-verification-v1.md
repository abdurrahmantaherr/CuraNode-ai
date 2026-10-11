# Doctor Verification v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A clinic administrator can sign in, see the doctors waiting at their own clinic, check PMDC's Doctor Search by hand, and approve, reject or revoke a doctor's verification — with every decision recorded. This also fixes a pre-existing bug: no admin account can sign in today.

**Architecture:** Two new, repo-owned database tables (`doctor_verification`, `clinic_invite_code`) hold history the shared `doctor` table has no room for. A new `verification` module is the single place that decides a doctor's status, mirroring `consent/service.py`'s shape. A new `admin` module handles clinic-admin self-sign-up, gated by a per-clinic invite code, without touching the sensitive `identity` module. The admin's verification page is a dedicated route (like `/patient/access`), not routed through the shared `_guarded` dashboard helper.

**Tech Stack:** FastAPI, SQLAlchemy 2 (async), Jinja2, Alembic (hand-written, additive only), pytest (`asyncio_mode = "auto"`, in-memory SQLite, `FakeSupabaseAuth`), ruff.

**Spec:** `docs/superpowers/specs/2026-10-11-doctor-verification-v1-design.md`

## Global Constraints

- Branch: new branch from `dev` (e.g. `feature/doctor-verification`). Never push, merge, or run the migration against the shared Supabase project without the user's go-ahead — she already messaged the team about the schema change.
- Migration is additive only, hand-written (never `alembic revision --autogenerate`), matching `alembic/versions/6feacefae2d0_*.py` and `alembic/versions/488dbf2b3915_*.py`.
- `doctor_verification` rows are never updated or deleted (same rule as `audit_log`).
- Approve requires all three checklist ticks, checked server-side, not just by disabling the button.
- A doctor at another clinic must be `NotFound`, never a 403 that reveals they exist (same shape as `consent/service.py`'s doctor/patient lookups).
- The admin's private `note` is never shown to the doctor and never written into `audit_log.detail` — only the fixed `reason` code is.
- Every user-facing string is an i18n key present in both `en.json` and `ur.json` (`missing_keys()` must stay empty).
- PMDC numbers and invite codes render `dir="ltr"`, including on the Urdu page.
- Style: ruff-clean (`uv run ruff check backend tests` and `uv run ruff format backend tests`). Run tests with `uv run pytest -q`.
- Commit messages end with `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`.

## Review Focus

1. A doctor with an active affiliation to **two** clinics, only one of which is the admin's, must still be visible and actionable to that admin — the clinic filter must not over- or under-match. [Task 3]
2. An admin who signs up, then is later removed from `clinic_staff` (no active affiliation), should get `NotFound` from every verification action, not a 403 that reveals the clinic. [Task 3]
3. A second invite-code sign-up attempt for the same clinic with the *old* code must fail after the code is rotated — a rotated code must actually stop working, not just add a new one. [Task 5]
4. Submitting the approve form with JavaScript disabled and one checkbox left unticked must be refused by the server, not silently accepted because the browser already hid the button. [Task 3, exercised again in Task 8]
5. A doctor who was rejected, then later approved, must see the pending/rejected banner disappear and the D4 dashboard work — the stale banner must not survive the approval. [Task 7]

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/db/models.py` (modify) | Add `VerificationDecision` enum, `DoctorVerification`, `ClinicInviteCode`. |
| `alembic/versions/a7d3c89106f9_doctor_verification_and_invite_codes.py` (new) | The two new tables. Additive only. |
| `backend/app/verification/__init__.py`, `codes.py` (new) | Invite-code hashing: `hash_code`, `verify_code`. Pure, no DB. |
| `backend/app/verification/schemas.py` (new) | `REASONS` tuple. |
| `backend/app/verification/service.py` (new) | `list_doctors_for_clinic`, `approve`, `reject`, `revoke`, `latest_decisions_for`, `doctor_status_for_banner`. |
| `backend/app/admin/__init__.py`, `schemas.py` (new) | `AdminRegisterRequest`. |
| `backend/app/admin/service.py` (new) | `register_admin` — self-contained, does not import `identity/service.py` internals. |
| `backend/ops/scripts/set_clinic_invite_code.py` (new) | One-off script: set/rotate a clinic's invite code, prints it once. |
| `backend/app/audit/writer.py` (modify) | Three new action constants. |
| `backend/app/web/router.py` (modify) | Login tile fix; new `/admin/register` routes; replace the `admin_area` stub with the dedicated verification dashboard routes; extend `_guarded`'s doctor banner branch. |
| `frontend/templates/auth/login.html` (modify) | Third `role-tile` for admin. |
| `frontend/templates/auth/admin_register.html` (new) | Admin sign-up form. |
| `frontend/templates/admin/dashboard.html` (new) | The three tabs, doctor rows, approve/reject/revoke forms. |
| `frontend/templates/shell.html` (modify) | New red banner branch for a rejected/revoked doctor. |
| `backend/app/i18n/messages/en.json`, `ur.json` (modify) | New keys throughout. |
| `tests/test_verification.py` (new) | Service tests, banner test, admin dashboard page tests. |
| `tests/test_admin_register.py` (new) | Admin registration, login fix, i18n completeness. |

Spec deviation, on purpose: the spec's §3.2 touch-points table named three partials
(`_pending_list.html`/`_verified_list.html`/`_rejected_list.html`) included from
`shell.html`, the same way D4's `doctor/_patient_list.html` is. Once the detailed
behaviour was worked out, the admin page needed per-row POST forms (approve/reject/
revoke), a checklist, and tabs addressed by URL — none of which fit `shell.html`'s
"one always-shown panel" shape. Task 8 instead gives it a dedicated route and a single
`admin/dashboard.html`, the same pattern already used for `/patient/access` (its own
guard, its own PRG redirects, `tab` as a query param rather than three separate pages).
The requirement — three tabs, approve/reject/revoke, the checklist, a PMDC link — is
unchanged; only the file split and the integration mechanism differ from the spec's
mechanical sketch.

---

### Task 1: Database models and migration

**Files:**
- Modify: `backend/app/db/models.py` (append after the `Doctor`/`DoctorAffiliation`/`ClinicStaff` block, before `# ── Patient profile (FR2)`)
- Create: `alembic/versions/a7d3c89106f9_doctor_verification_and_invite_codes.py`
- Test: `tests/test_db_models_verification.py`

**Interfaces:**
- Produces: `VerificationDecision(str, enum.Enum)` with members `APPROVE="approve"`, `REJECT="reject"`, `REVOKE="revoke"`; `DoctorVerification` (columns: `id`, `doctor_id`, `decided_by`, `decision`, `reason`, `checked_number`, `checked_name`, `checked_licence`, `note`, `decided_at`); `ClinicInviteCode` (columns: `clinic_id` [PK], `code_hash`, `is_active`, `created_at`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_db_models_verification.py`:

```python
"""D-VERIFY — the two new tables exist and round-trip (model layer only;
business rules belong to `verification/service.py`)."""

from __future__ import annotations

import pytest
from app.db.models import (
    ClinicInviteCode,
    Doctor,
    DoctorVerification,
    UserRole,
    VerificationDecision,
)
from app.db.types import utcnow, uuid7
from sqlalchemy import select

from tests.conftest import make_user


async def test_doctor_verification_round_trips(db, clinic):
    user = await make_user(db, email="dr@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    doctor = (await db.execute(select(Doctor).where(Doctor.user_id == user.id))).scalar_one()
    admin = await make_user(db, email="adm@x.com", role=UserRole.CLINIC_ADMIN, clinic_id=clinic.id)

    row = DoctorVerification(
        id=uuid7(),
        doctor_id=doctor.id,
        decided_by=admin.id,
        decision=VerificationDecision.APPROVE,
        reason=None,
        checked_number=True,
        checked_name=True,
        checked_licence=True,
        note="Checked on PMDC, all clear.",
        decided_at=utcnow(),
    )
    db.add(row)
    await db.commit()

    got = (
        await db.execute(select(DoctorVerification).where(DoctorVerification.doctor_id == doctor.id))
    ).scalar_one()
    assert got.decision == VerificationDecision.APPROVE
    assert got.checked_number is True
    assert got.note == "Checked on PMDC, all clear."


async def test_clinic_invite_code_round_trips(db, clinic):
    db.add(ClinicInviteCode(clinic_id=clinic.id, code_hash="salt$hash", is_active=True))
    await db.commit()

    got = (
        await db.execute(select(ClinicInviteCode).where(ClinicInviteCode.clinic_id == clinic.id))
    ).scalar_one()
    assert got.code_hash == "salt$hash"
    assert got.is_active is True


async def test_clinic_invite_code_clinic_id_is_the_primary_key(db, clinic):
    # One code per clinic — a second insert for the same clinic must fail,
    # not silently create a second active code.
    db.add(ClinicInviteCode(clinic_id=clinic.id, code_hash="first", is_active=True))
    await db.commit()
    db.add(ClinicInviteCode(clinic_id=clinic.id, code_hash="second", is_active=True))
    with pytest.raises(Exception):  # IntegrityError on Postgres, sqlite3.IntegrityError-wrapped here
        await db.commit()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_db_models_verification.py -q`
Expected: collection error, `ImportError: cannot import name 'DoctorVerification' from 'app.db.models'`.

- [ ] **Step 3: Add the models**

In `backend/app/db/models.py`, insert directly after the `ClinicStaff` class (before the `# ── Patient profile (FR2)` comment):

```python
# ── Doctor verification (D-VERIFY v1) ────────────────────────────────────
class VerificationDecision(str, enum.Enum):
    APPROVE = "approve"
    REJECT = "reject"
    REVOKE = "revoke"


class DoctorVerification(Base):
    """One row per clinic-admin decision about a doctor. Owned outright by
    this repo, append-only — never updated or deleted, same rule as
    `AuditLog`. `doctor.verification_status` stays the fast current-state
    read; this table is the history behind it.
    """

    __tablename__ = "doctor_verification"

    id: Mapped[uuid.UUID] = mapped_column("verification_id", Uuid, primary_key=True, default=uuid7)
    doctor_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("doctor.doctor_id"), nullable=False
    )
    decided_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("user_profile.user_id"), nullable=False
    )
    decision: Mapped[VerificationDecision] = mapped_column(
        _enum(VerificationDecision, "verification_decision"), nullable=False
    )
    # A fixed code from verification/schemas.py::REASONS — never free text.
    # NULL for `approve`.
    reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    checked_number: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    checked_name: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    checked_licence: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Admin-only free text. Never shown to the doctor, never in audit_log.detail.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = _ts(nullable=False, default=utcnow)

    __table_args__ = (Index("idx_doctor_verification_doctor", "doctor_id", "decided_at"),)


class ClinicInviteCode(Base):
    """One active code per clinic, gating clinic-admin self-sign-up
    (`/admin/register`). `code_hash` only — the plain code is shown once,
    by `ops/scripts/set_clinic_invite_code.py`, and never stored.
    """

    __tablename__ = "clinic_invite_code"

    clinic_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("clinic.clinic_id"), primary_key=True
    )
    code_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _ts(nullable=False, default=utcnow)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_db_models_verification.py -q`
Expected: `3 passed`.

- [ ] **Step 5: Write the migration**

Create `alembic/versions/a7d3c89106f9_doctor_verification_and_invite_codes.py`:

```python
"""doctor verification v1: doctor_verification, clinic_invite_code

Additive only, hand-written. Both tables are owned outright by this repo —
neither exists in the shared schema. `doctor_verification` is the history
behind the shared `doctor.verification_status` column (unchanged here);
`clinic_invite_code` gates clinic-admin self-sign-up.

Revision ID: a7d3c89106f9
Revises: 488dbf2b3915
Create Date: 2026-10-11 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7d3c89106f9"
down_revision: str | Sequence[str] | None = "488dbf2b3915"
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
```

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check backend tests && uv run ruff format backend tests
git add backend/app/db/models.py alembic/versions/a7d3c89106f9_doctor_verification_and_invite_codes.py tests/test_db_models_verification.py
git commit -m "feat(verify): add doctor_verification and clinic_invite_code tables"
```

Do **not** run `uv run alembic upgrade head` against the shared Supabase project in this task — that happens once in Task 9, after the user confirms the team is ready.

---

### Task 2: Invite-code hashing

**Files:**
- Create: `backend/app/verification/__init__.py`
- Create: `backend/app/verification/codes.py`
- Test: `tests/test_verification_codes.py`

**Interfaces:**
- Produces: `generate_code() -> str` (a human-typeable code, e.g. `8` groups of letters/digits); `hash_code(code: str) -> str` (returns `"<salt_hex>$<hash_hex>"`); `verify_code(code: str, stored: str) -> bool`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_verification_codes.py`:

```python
"""D-VERIFY — invite-code hashing. Pure functions, no database."""

from __future__ import annotations

from app.verification.codes import generate_code, hash_code, verify_code


def test_generate_code_is_reasonably_long_and_uppercase_alnum():
    code = generate_code()
    assert len(code) >= 10
    assert code == code.upper()
    assert code.isalnum()


def test_generate_code_is_not_constant():
    assert generate_code() != generate_code()


def test_hash_then_verify_the_same_code_succeeds():
    code = "CLINIC-TEST-0001"
    stored = hash_code(code)
    assert verify_code(code, stored) is True


def test_verify_rejects_a_wrong_code():
    stored = hash_code("CLINIC-TEST-0001")
    assert verify_code("CLINIC-TEST-0002", stored) is False


def test_verify_rejects_a_blank_code():
    stored = hash_code("CLINIC-TEST-0001")
    assert verify_code("", stored) is False


def test_hash_code_never_stores_the_plain_code():
    code = "CLINIC-TEST-0001"
    stored = hash_code(code)
    assert code not in stored


def test_two_hashes_of_the_same_code_differ():
    # Salted — two rotations of the same literal code must not look identical.
    code = "CLINIC-TEST-0001"
    assert hash_code(code) != hash_code(code)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_verification_codes.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'app.verification'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/verification/__init__.py`:

```python
"""Clinic-admin doctor verification (D-VERIFY v1)."""
```

Create `backend/app/verification/codes.py`:

```python
"""Clinic invite-code generation and hashing.

Plain-text codes are never stored (`clinic_invite_code.code_hash` only) — the
same reasoning as a password, except this app has no password hasher of its
own (Supabase owns those). PBKDF2-HMAC-SHA256 with a random salt is a small,
dependency-free, adequate choice for a code an admin types once at sign-up.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I — easy to read aloud
_ITERATIONS = 200_000


def generate_code() -> str:
    """A 16-character code in 4 dash-separated groups, e.g. `8K2N-QX7P-4VWR-9ZTC`."""
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(16))
    return "-".join(raw[i : i + 4] for i in range(0, 16, 4))


def hash_code(code: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", code.encode("utf-8"), bytes.fromhex(salt), _ITERATIONS)
    return f"{salt}${digest.hex()}"


def verify_code(code: str, stored: str) -> bool:
    if not code or "$" not in stored:
        return False
    salt, _, expected_hex = stored.partition("$")
    try:
        salt_bytes = bytes.fromhex(salt)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", code.encode("utf-8"), salt_bytes, _ITERATIONS)
    return hmac.compare_digest(digest.hex(), expected_hex)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_verification_codes.py -q`
Expected: `6 passed`.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check backend tests && uv run ruff format backend tests
git add backend/app/verification tests/test_verification_codes.py
git commit -m "feat(verify): add invite-code generation and hashing"
```

---

### Task 3: Verification service

**Files:**
- Create: `backend/app/verification/schemas.py`
- Modify: `backend/app/verification/service.py` (new file in this task)
- Modify: `backend/app/audit/writer.py` (append three constants)
- Test: `tests/test_verification.py` (create; later tasks append to it)

**Interfaces:**
- Consumes: `DoctorVerification`, `VerificationDecision`, `ClinicInviteCode` (Task 1); `Actor` (`deps.py`, unmodified — uses `actor.role`, `actor.user_id`, `actor.clinic_ids`, `actor.ip_address`); `NotFound`, `ValidationFailed` (`errors.py`); `audit.write` (`audit/writer.py`).
- Produces:
  - `REASONS: tuple[str, ...]` = `("number_not_found", "name_mismatch", "licence_invalid", "not_at_clinic", "other")`
  - `async def list_doctors_for_clinic(session: AsyncSession, actor: Actor, tab: str) -> list[Doctor]`
  - `async def approve(session, actor, doctor_id: uuid.UUID, *, checked_number: bool, checked_name: bool, checked_licence: bool, note: str | None, user_agent: str | None = None) -> None`
  - `async def reject(session, actor, doctor_id, *, reason: str, note: str | None, user_agent: str | None = None) -> None`
  - `async def revoke(session, actor, doctor_id, *, reason: str, note: str | None, user_agent: str | None = None) -> None`
  - `async def latest_decisions_for(session, doctor_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, DoctorVerification]`
  - `async def doctor_status_for_banner(session, actor: Actor) -> tuple[str, str | None]` — `(verification_status.value, latest_reason_or_None)`, used by Task 7.

- [ ] **Step 1: Write the failing tests**

Create `backend/app/verification/schemas.py`:

```python
"""Request-shape constants for the verification module."""

from __future__ import annotations

REASONS: tuple[str, ...] = (
    "number_not_found",
    "name_mismatch",
    "licence_invalid",
    "not_at_clinic",
    "other",
)
```

Create `tests/test_verification.py`:

```python
"""D-VERIFY — clinic-admin doctor verification: the service layer.

Visibility and authority are the whole feature, so most tests are about who
must NOT see or act on a doctor.
"""

from __future__ import annotations

import pytest
from app.db.models import Clinic, Doctor, DoctorAffiliation, DoctorVerification, UserRole, VerificationStatus
from app.db.types import utcnow, uuid7
from app.deps import Actor
from app.errors import NotFound, ValidationFailed
from app.verification import service as verification_service
from sqlalchemy import select

from tests.conftest import make_user


async def doctor_of(db, user) -> Doctor:
    stmt = select(Doctor).where(Doctor.user_id == user.id)
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one()


def as_admin(user, clinic_ids) -> Actor:
    return Actor(user_id=user.id, role="admin", clinic_ids=frozenset(clinic_ids))


@pytest.fixture
async def other_clinic(db):
    c = Clinic(id=uuid7(), name="Maroof International", city="Islamabad", type="hospital")
    db.add(c)
    await db.commit()
    return c


@pytest.fixture
async def admin(db, clinic) -> Actor:
    user = await make_user(db, email="adm@x.com", role=UserRole.CLINIC_ADMIN, clinic_id=clinic.id)
    return as_admin(user, [clinic.id])


@pytest.fixture
async def pending_doctor(db, clinic) -> Doctor:
    user = await make_user(db, email="dr@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    return await doctor_of(db, user)


# ── list_doctors_for_clinic ──────────────────────────────────────────────
async def test_lists_pending_doctors_at_the_admins_clinic(db, admin, pending_doctor):
    got = await verification_service.list_doctors_for_clinic(db, admin, "pending")
    assert [d.id for d in got] == [pending_doctor.id]


async def test_does_not_list_a_doctor_at_another_clinic(db, admin, other_clinic):
    user = await make_user(db, email="dr2@x.com", role=UserRole.DOCTOR, clinic_id=other_clinic.id)
    await doctor_of(db, user)
    got = await verification_service.list_doctors_for_clinic(db, admin, "pending")
    assert got == []


async def test_a_doctor_affiliated_with_two_clinics_is_still_visible_to_one_admin(
    db, admin, other_clinic
):
    # Review focus 1 — affiliated with both; only one admin's clinic.
    user = await make_user(db, email="dr2@x.com", role=UserRole.DOCTOR, clinic_id=other_clinic.id)
    doctor = await doctor_of(db, user)
    db.add(
        DoctorAffiliation(
            id=uuid7(),
            doctor_id=doctor.id,
            clinic_id=next(iter(admin.clinic_ids)),
            start_date=utcnow().date(),
            status="active",
        )
    )
    await db.commit()
    got = await verification_service.list_doctors_for_clinic(db, admin, "pending")
    assert [d.id for d in got] == [doctor.id]


async def test_non_admin_actor_gets_not_found(db, pending_doctor):
    actor = Actor(user_id=pending_doctor.user_id, role="doctor")
    with pytest.raises(NotFound):
        await verification_service.list_doctors_for_clinic(db, actor, "pending")


async def test_admin_with_no_active_affiliation_gets_not_found_on_actions(
    db, clinic, pending_doctor
):
    # Review focus 2 — an admin removed from clinic_staff (no clinic_ids).
    user = await make_user(db, email="exadmin@x.com", role=UserRole.CLINIC_ADMIN)
    actor = as_admin(user, [])
    with pytest.raises(NotFound):
        await verification_service.approve(
            db, actor, pending_doctor.id,
            checked_number=True, checked_name=True, checked_licence=True, note=None,
        )


async def test_unknown_tab_is_validation_failed(db, admin):
    with pytest.raises(ValidationFailed):
        await verification_service.list_doctors_for_clinic(db, admin, "bogus")


# ── approve ───────────────────────────────────────────────────────────────
async def test_approve_requires_all_three_ticks(db, admin, pending_doctor):
    # Review focus 4 — a missing tick is refused server-side.
    with pytest.raises(ValidationFailed):
        await verification_service.approve(
            db, admin, pending_doctor.id,
            checked_number=True, checked_name=True, checked_licence=False, note=None,
        )


async def test_approve_sets_verified_and_writes_history(db, admin, pending_doctor):
    await verification_service.approve(
        db, admin, pending_doctor.id,
        checked_number=True, checked_name=True, checked_licence=True, note="looks right",
    )
    row = (
        await db.execute(select(Doctor).where(Doctor.id == pending_doctor.id).execution_options(populate_existing=True))
    ).scalar_one()
    assert row.verification_status == VerificationStatus.VERIFIED
    assert row.verified_by == admin.user_id
    assert row.verified_at is not None

    history = (
        await db.execute(select(DoctorVerification).where(DoctorVerification.doctor_id == pending_doctor.id))
    ).scalar_one()
    assert history.decision.value == "approve"
    assert history.note == "looks right"


# ── reject ────────────────────────────────────────────────────────────────
async def test_reject_sets_rejected_with_reason(db, admin, pending_doctor):
    await verification_service.reject(
        db, admin, pending_doctor.id, reason="name_mismatch", note=None
    )
    row = (
        await db.execute(select(Doctor).where(Doctor.id == pending_doctor.id).execution_options(populate_existing=True))
    ).scalar_one()
    assert row.verification_status == VerificationStatus.REJECTED
    history = (
        await db.execute(select(DoctorVerification).where(DoctorVerification.doctor_id == pending_doctor.id))
    ).scalar_one()
    assert history.reason == "name_mismatch"


async def test_reject_other_without_a_note_is_refused(db, admin, pending_doctor):
    with pytest.raises(ValidationFailed):
        await verification_service.reject(db, admin, pending_doctor.id, reason="other", note=None)


async def test_reject_other_with_a_note_succeeds(db, admin, pending_doctor):
    await verification_service.reject(
        db, admin, pending_doctor.id, reason="other", note="PMDC site was down, re-check later"
    )
    row = (
        await db.execute(select(Doctor).where(Doctor.id == pending_doctor.id).execution_options(populate_existing=True))
    ).scalar_one()
    assert row.verification_status == VerificationStatus.REJECTED


async def test_unknown_reason_is_refused(db, admin, pending_doctor):
    with pytest.raises(ValidationFailed):
        await verification_service.reject(db, admin, pending_doctor.id, reason="bogus", note=None)


async def test_rejected_then_approved_moves_to_verified_and_keeps_history(db, admin, pending_doctor):
    await verification_service.reject(db, admin, pending_doctor.id, reason="name_mismatch", note=None)
    await verification_service.approve(
        db, admin, pending_doctor.id,
        checked_number=True, checked_name=True, checked_licence=True, note=None,
    )
    row = (
        await db.execute(select(Doctor).where(Doctor.id == pending_doctor.id).execution_options(populate_existing=True))
    ).scalar_one()
    assert row.verification_status == VerificationStatus.VERIFIED
    history = (
        await db.execute(
            select(DoctorVerification)
            .where(DoctorVerification.doctor_id == pending_doctor.id)
            .order_by(DoctorVerification.decided_at)
        )
    ).scalars().all()
    assert [h.decision.value for h in history] == ["reject", "approve"]


# ── revoke ────────────────────────────────────────────────────────────────
async def test_revoke_a_verified_doctor_sets_rejected_and_clears_verifier(db, admin, pending_doctor):
    await verification_service.approve(
        db, admin, pending_doctor.id,
        checked_number=True, checked_name=True, checked_licence=True, note=None,
    )
    await verification_service.revoke(db, admin, pending_doctor.id, reason="licence_invalid", note=None)
    row = (
        await db.execute(select(Doctor).where(Doctor.id == pending_doctor.id).execution_options(populate_existing=True))
    ).scalar_one()
    assert row.verification_status == VerificationStatus.REJECTED
    assert row.verified_by is None
    assert row.verified_at is None


# ── audit ─────────────────────────────────────────────────────────────────
async def test_each_decision_writes_one_audit_row_with_no_note_text(db, admin, pending_doctor):
    from app.db.models import AuditLog
    import json

    await verification_service.reject(
        db, admin, pending_doctor.id, reason="other", note="this doctor's number is fake, I checked twice"
    )
    rows = (
        await db.execute(select(AuditLog).where(AuditLog.action == "verification.reject"))
    ).scalars().all()
    assert len(rows) == 1
    detail = json.loads(rows[0].detail)
    assert detail == {"doctor_id": str(pending_doctor.id), "reason": "other"}
    assert "fake" not in rows[0].detail


# ── latest_decisions_for / doctor_status_for_banner ──────────────────────
async def test_latest_decisions_for_returns_the_newest_row_per_doctor(db, admin, pending_doctor):
    await verification_service.reject(db, admin, pending_doctor.id, reason="name_mismatch", note=None)
    await verification_service.approve(
        db, admin, pending_doctor.id,
        checked_number=True, checked_name=True, checked_licence=True, note=None,
    )
    latest = await verification_service.latest_decisions_for(db, [pending_doctor.id])
    assert latest[pending_doctor.id].decision.value == "approve"


async def test_latest_decisions_for_empty_list_returns_empty_dict(db):
    assert await verification_service.latest_decisions_for(db, []) == {}


async def test_doctor_status_for_banner_pending(db, pending_doctor):
    actor = Actor(user_id=pending_doctor.user_id, role="doctor")
    status, reason = await verification_service.doctor_status_for_banner(db, actor)
    assert status == "pending"
    assert reason is None


async def test_doctor_status_for_banner_rejected_carries_the_reason(db, admin, pending_doctor):
    await verification_service.reject(db, admin, pending_doctor.id, reason="licence_invalid", note=None)
    actor = Actor(user_id=pending_doctor.user_id, role="doctor")
    status, reason = await verification_service.doctor_status_for_banner(db, actor)
    assert status == "rejected"
    assert reason == "licence_invalid"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_verification.py -q`
Expected: `ModuleNotFoundError: No module named 'app.verification.service'` (and `AttributeError` for `audit.VERIFICATION_REJECT` etc. once the module exists — fix both in Step 3).

- [ ] **Step 3: Add the audit constants, then write the service**

Append to `backend/app/audit/writer.py`, directly after `DOCTOR_DASHBOARD_VIEW = "doctor.dashboard.view"`:

```python

# Emitted by clinic-admin doctor verification (D-VERIFY v1). `detail` never
# carries the admin's free-text note — only the doctor id and reason code.
VERIFICATION_APPROVE = "verification.approve"
VERIFICATION_REJECT = "verification.reject"
VERIFICATION_REVOKE = "verification.revoke"
```

Create `backend/app/verification/service.py`:

```python
"""Clinic-admin doctor verification business logic (D-VERIFY v1).

Mirrors `consent/service.py`'s shape: every call checks the actor's role and
clinic affiliation itself, live, never trusting a cached flag — the same
"who may see whom" boundary the dashboard (D4) and consent module already
draw. A doctor outside the admin's clinic is `NotFound`, the identical shape
as any other foreign/nonexistent lookup elsewhere in this app, so an admin
can't learn that a doctor exists at a clinic that isn't theirs.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import writer as audit
from ..db import types as dbtypes
from ..db.models import (
    Doctor,
    DoctorAffiliation,
    DoctorVerification,
    UserRole,
    VerificationDecision,
    VerificationStatus,
)
from ..deps import Actor
from ..errors import NotFound, ValidationFailed
from .schemas import REASONS

_TAB_STATUS = {
    "pending": VerificationStatus.PENDING,
    "verified": VerificationStatus.VERIFIED,
    "rejected": VerificationStatus.REJECTED,
}


def _require_admin(actor: Actor) -> None:
    if actor.role != UserRole.CLINIC_ADMIN.value:
        raise NotFound()


async def _doctor_in_clinic(session: AsyncSession, actor: Actor, doctor_id: uuid.UUID) -> Doctor:
    """Live, every call — an admin dropped from `clinic_staff` loses access
    to this doctor on their very next action (review focus 2)."""
    doctor = (
        await session.execute(
            select(Doctor)
            .join(DoctorAffiliation, DoctorAffiliation.doctor_id == Doctor.id)
            .where(
                Doctor.id == doctor_id,
                DoctorAffiliation.clinic_id.in_(actor.clinic_ids),
                DoctorAffiliation.status == "active",
            )
        )
    ).scalar_one_or_none()
    if doctor is None:
        raise NotFound()
    return doctor


async def _record(
    session: AsyncSession,
    actor: Actor,
    doctor: Doctor,
    *,
    decision: VerificationDecision,
    reason: str | None,
    checked_number: bool,
    checked_name: bool,
    checked_licence: bool,
    note: str | None,
    user_agent: str | None,
) -> None:
    session.add(
        DoctorVerification(
            id=dbtypes.uuid7(),
            doctor_id=doctor.id,
            decided_by=actor.user_id,
            decision=decision,
            reason=reason,
            checked_number=checked_number,
            checked_name=checked_name,
            checked_licence=checked_licence,
            note=(note or None),
            decided_at=dbtypes.utcnow(),
        )
    )
    action = {
        VerificationDecision.APPROVE: audit.VERIFICATION_APPROVE,
        VerificationDecision.REJECT: audit.VERIFICATION_REJECT,
        VerificationDecision.REVOKE: audit.VERIFICATION_REVOKE,
    }[decision]
    detail: dict[str, Any] = {"doctor_id": str(doctor.id)}
    if reason is not None:
        detail["reason"] = reason
    await audit.write(
        session,
        action=action,
        actor_user_id=actor.user_id,
        actor_role=actor.role,
        resource_type="doctor_verification",
        ip_address=actor.ip_address,
        user_agent=user_agent,
        detail=detail,
    )


async def list_doctors_for_clinic(session: AsyncSession, actor: Actor, tab: str) -> list[Doctor]:
    _require_admin(actor)
    status = _TAB_STATUS.get(tab)
    if status is None:
        raise ValidationFailed({"fields": {"tab": "errors.choice_invalid"}})
    rows = await session.execute(
        select(Doctor)
        .join(DoctorAffiliation, DoctorAffiliation.doctor_id == Doctor.id)
        .where(
            DoctorAffiliation.clinic_id.in_(actor.clinic_ids),
            DoctorAffiliation.status == "active",
            Doctor.verification_status == status,
        )
        .distinct()
        .order_by(Doctor.full_name, Doctor.id)
    )
    return list(rows.scalars().all())


async def approve(
    session: AsyncSession,
    actor: Actor,
    doctor_id: uuid.UUID,
    *,
    checked_number: bool,
    checked_name: bool,
    checked_licence: bool,
    note: str | None,
    user_agent: str | None = None,
) -> None:
    _require_admin(actor)
    if not (checked_number and checked_name and checked_licence):
        raise ValidationFailed({"fields": {"checklist": "errors.checklist_required"}})
    doctor = await _doctor_in_clinic(session, actor, doctor_id)
    doctor.verification_status = VerificationStatus.VERIFIED
    doctor.verified_by = actor.user_id
    doctor.verified_at = dbtypes.utcnow()
    await _record(
        session, actor, doctor,
        decision=VerificationDecision.APPROVE, reason=None,
        checked_number=True, checked_name=True, checked_licence=True,
        note=note, user_agent=user_agent,
    )
    await session.commit()


async def _reject_or_revoke(
    session: AsyncSession,
    actor: Actor,
    doctor_id: uuid.UUID,
    *,
    decision: VerificationDecision,
    reason: str,
    note: str | None,
    user_agent: str | None,
) -> None:
    _require_admin(actor)
    if reason not in REASONS:
        raise ValidationFailed({"fields": {"reason": "errors.choice_invalid"}})
    if reason == "other" and not (note or "").strip():
        raise ValidationFailed({"fields": {"note": "errors.text_required"}})
    doctor = await _doctor_in_clinic(session, actor, doctor_id)
    doctor.verification_status = VerificationStatus.REJECTED
    doctor.verified_by = None
    doctor.verified_at = None
    await _record(
        session, actor, doctor,
        decision=decision, reason=reason,
        checked_number=False, checked_name=False, checked_licence=False,
        note=note, user_agent=user_agent,
    )
    await session.commit()


async def reject(
    session: AsyncSession, actor: Actor, doctor_id: uuid.UUID, *,
    reason: str, note: str | None, user_agent: str | None = None,
) -> None:
    await _reject_or_revoke(
        session, actor, doctor_id,
        decision=VerificationDecision.REJECT, reason=reason, note=note, user_agent=user_agent,
    )


async def revoke(
    session: AsyncSession, actor: Actor, doctor_id: uuid.UUID, *,
    reason: str, note: str | None, user_agent: str | None = None,
) -> None:
    await _reject_or_revoke(
        session, actor, doctor_id,
        decision=VerificationDecision.REVOKE, reason=reason, note=note, user_agent=user_agent,
    )


async def latest_decisions_for(
    session: AsyncSession, doctor_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, DoctorVerification]:
    if not doctor_ids:
        return {}
    rows = await session.execute(
        select(DoctorVerification)
        .where(DoctorVerification.doctor_id.in_(doctor_ids))
        .order_by(DoctorVerification.decided_at)
    )
    latest: dict[uuid.UUID, DoctorVerification] = {}
    for row in rows.scalars().all():
        latest[row.doctor_id] = row  # ascending order — last write wins
    return latest


async def doctor_status_for_banner(session: AsyncSession, actor: Actor) -> tuple[str, str | None]:
    """For the doctor's own dashboard banner (Task 7). Read live, like
    everything else in this module — never cached on `Actor`."""
    doctor = (
        await session.execute(select(Doctor).where(Doctor.user_id == actor.user_id))
    ).scalar_one_or_none()
    if doctor is None:
        return (VerificationStatus.PENDING.value, None)
    reason = None
    if doctor.verification_status == VerificationStatus.REJECTED:
        latest = await latest_decisions_for(session, [doctor.id])
        row = latest.get(doctor.id)
        reason = row.reason if row else None
    return (doctor.verification_status.value, reason)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_verification.py -q`
Expected: `20 passed`.

- [ ] **Step 5: Run the full suite for regressions, lint, commit**

```bash
uv run pytest -q
uv run ruff check backend tests && uv run ruff format backend tests
git add backend/app/verification backend/app/audit/writer.py tests/test_verification.py
git commit -m "feat(verify): add the verification service (approve/reject/revoke)"
```

Expected: the whole suite passes (242 existing + 20 new = 262).

---

### Task 4: Login page — admin tile and role-check fix

**Files:**
- Modify: `frontend/templates/auth/login.html`
- Modify: `backend/app/web/router.py` (`login_submit`'s `selected` line)
- Modify: `backend/app/i18n/messages/en.json`, `ur.json`
- Test: `tests/test_admin_register.py` (create; later tasks append to it)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: nothing other tasks consume directly, but Task 6/8 rely on an admin being able to sign in at all.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_admin_register.py`:

```python
"""D-VERIFY — clinic-admin sign-up and sign-in."""

from __future__ import annotations

from app.db.models import UserRole
from app.i18n.catalogue import missing_keys, translate
from markupsafe import escape

from tests.conftest import TEST_PASSWORD, make_user


def in_html(key: str, locale: str = "en", **params) -> str:
    return str(escape(translate(key, locale, **params)))


async def login(client, email: str, *, role: str = "") -> object:
    payload = {"email": email, "password": TEST_PASSWORD}
    if role:
        payload["role"] = role
    return await client.post("/en/login", data=payload)


async def test_admin_login_page_offers_a_third_tile(client):
    r = await client.get("/en/login")
    assert 'value="admin"' in r.text
    assert in_html("auth.role.admin") in r.text


async def test_admin_can_sign_in_selecting_the_admin_tile(client, db, clinic):
    await make_user(db, email="adm@x.com", role=UserRole.CLINIC_ADMIN, clinic_id=clinic.id)
    r = await login(client, "adm@x.com", role="admin")
    assert r.status_code == 303
    assert r.headers["location"] == "/en/admin"


async def test_admin_selecting_doctor_tile_is_a_role_mismatch(client, db, clinic):
    await make_user(db, email="adm@x.com", role=UserRole.CLINIC_ADMIN, clinic_id=clinic.id)
    r = await login(client, "adm@x.com", role="doctor")
    assert r.status_code == 403
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_admin_register.py -q`
Expected: `test_admin_login_page_offers_a_third_tile` FAILs (`value="admin"` not in page); `test_admin_can_sign_in_selecting_the_admin_tile` FAILs with a redirect somewhere other than `/en/admin` (today's `selected` logic drops `role="admin"` to `""`, which happens to pass the role check and *does* already land on `/en/admin` — read the actual failure; if this one already passes, that is fine and expected, it proves only the tile is missing, not the check). `test_admin_selecting_doctor_tile_is_a_role_mismatch` should already pass today.

- [ ] **Step 3: Fix the template and the role check**

In `backend/app/web/router.py`, find `login_submit`'s line:

```python
    selected = role if role in ("patient", "doctor") else ""
```

Replace with:

```python
    selected = role if role in ("patient", "doctor", "admin") else ""
```

In `frontend/templates/auth/login.html`, replace:

```html
          {% for r in ['patient', 'doctor'] %}
```

with:

```html
          {% for r in ['patient', 'doctor', 'admin'] %}
```

And replace the subtitle paragraph:

```html
      <p class="auth-sub" id="login-sub">
        {{ t('auth.login.subtitle_patient') if role == 'patient' else t('auth.login.subtitle_doctor') }}
      </p>
```

with:

```html
      <p class="auth-sub" id="login-sub">
        {{ t('auth.login.subtitle_' ~ (role if role in ['patient', 'doctor', 'admin'] else 'patient')) }}
      </p>
```

Add to `backend/app/i18n/messages/en.json` (as the new last entry, with a comma added to the previous last entry):

```json
  "auth.role.admin": "Clinic admin",
  "auth.login.subtitle_admin": "Sign in to verify doctors at your clinic."
```

Add to `backend/app/i18n/messages/ur.json`:

```json
  "auth.role.admin": "کلینک ایڈمن",
  "auth.login.subtitle_admin": "اپنے کلینک کے ڈاکٹروں کی تصدیق کے لیے سائن ان کریں۔"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_admin_register.py -q`
Expected: `3 passed`.

- [ ] **Step 5: Run the web suite for regressions, lint, commit**

```bash
uv run pytest tests/test_web.py tests/test_admin_register.py -q
uv run ruff check backend tests && uv run ruff format backend tests
git add frontend/templates/auth/login.html backend/app/web/router.py backend/app/i18n tests/test_admin_register.py
git commit -m "fix(auth): let a clinic admin sign in (login page had no admin tile)"
```

---

### Task 5: Admin registration service and invite-code script

**Files:**
- Create: `backend/app/admin/__init__.py`
- Create: `backend/app/admin/schemas.py`
- Create: `backend/app/admin/service.py`
- Create: `backend/ops/scripts/set_clinic_invite_code.py`
- Test: `tests/test_admin_register.py` (append)

**Interfaces:**
- Consumes: `ClinicInviteCode` (Task 1), `hash_code`/`verify_code` (Task 2), `Profile`/`ClinicStaff`/`UserRole`/`AccountStatus`/`LocaleCode` (`db/models.py`), `landing_route_for`/`PASSWORD_MIN`/`PASSWORD_MAX`/`Locale` (`identity/schemas.py`, read-only import), `SessionOut`/`security.TokenPair` (`identity/schemas.py`, `identity/security.py`), `audit.write`, `audit.AUTH_REGISTER`.
- Produces: `AdminRegisterRequest(full_name, email, password, clinic_id, invite_code, preferred_locale)`; `async def register_admin(session, body, *, ip=None, user_agent=None) -> tuple[SessionOut, security.TokenPair | None]` (the `None` pair means duplicate-email, mirroring `identity/service.py::register`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_admin_register.py`:

```python
import uuid

import pytest
from app.admin import service as admin_service
from app.admin.schemas import AdminRegisterRequest
from app.db.models import ClinicInviteCode, ClinicStaff, UserRole
from app.errors import ValidationFailed
from app.verification.codes import hash_code
from sqlalchemy import select


async def set_code(db, clinic_id, code: str) -> None:
    db.add(ClinicInviteCode(clinic_id=clinic_id, code_hash=hash_code(code), is_active=True))
    await db.commit()


async def test_register_admin_with_the_right_code_creates_a_clinic_admin(db, clinic):
    await set_code(db, clinic.id, "RIGHT-CODE-0001")
    body = AdminRegisterRequest(
        full_name="Front Desk Lead", email="fd@x.com", password="CorrectHorseBattery1",
        clinic_id=clinic.id, invite_code="RIGHT-CODE-0001",
    )
    out, pair = await admin_service.register_admin(db, body)
    assert pair is not None
    assert out.role == "admin"

    staff = (
        await db.execute(select(ClinicStaff).where(ClinicStaff.user_id == out.user_id))
    ).scalar_one()
    assert staff.clinic_id == clinic.id


async def test_register_admin_with_the_wrong_code_is_refused(db, clinic):
    await set_code(db, clinic.id, "RIGHT-CODE-0001")
    body = AdminRegisterRequest(
        full_name="Someone", email="fd2@x.com", password="CorrectHorseBattery1",
        clinic_id=clinic.id, invite_code="WRONG-CODE-0001",
    )
    with pytest.raises(ValidationFailed):
        await admin_service.register_admin(db, body)


async def test_register_admin_with_no_code_set_for_the_clinic_is_refused(db, clinic):
    body = AdminRegisterRequest(
        full_name="Someone", email="fd3@x.com", password="CorrectHorseBattery1",
        clinic_id=clinic.id, invite_code="ANYTHING-0001",
    )
    with pytest.raises(ValidationFailed):
        await admin_service.register_admin(db, body)


async def test_rotated_code_stops_the_old_one_working(db, clinic):
    # Review focus 3 — rotation must actually invalidate the old code.
    await set_code(db, clinic.id, "OLD-CODE-0001")
    await set_code(db, clinic.id, "NEW-CODE-0001")  # same clinic_id — updates the one row

    body_old = AdminRegisterRequest(
        full_name="Someone", email="old@x.com", password="CorrectHorseBattery1",
        clinic_id=clinic.id, invite_code="OLD-CODE-0001",
    )
    with pytest.raises(ValidationFailed):
        await admin_service.register_admin(db, body_old)

    body_new = AdminRegisterRequest(
        full_name="Someone", email="new@x.com", password="CorrectHorseBattery1",
        clinic_id=clinic.id, invite_code="NEW-CODE-0001",
    )
    out, pair = await admin_service.register_admin(db, body_new)
    assert pair is not None


async def test_duplicate_email_returns_no_cookies_and_creates_nothing(db, clinic):
    await set_code(db, clinic.id, "RIGHT-CODE-0001")
    from tests.conftest import make_user

    existing = await make_user(db, email="taken@x.com", role=UserRole.CLINIC_ADMIN, clinic_id=clinic.id)
    body = AdminRegisterRequest(
        full_name="Impostor", email="taken@x.com", password="CorrectHorseBattery1",
        clinic_id=clinic.id, invite_code="RIGHT-CODE-0001",
    )
    out, pair = await admin_service.register_admin(db, body)
    assert pair is None
    assert out.user_id != existing.id  # a fresh, throwaway id — never leaks the real one

    count = (
        await db.execute(select(ClinicStaff).where(ClinicStaff.user_id != existing.id))
    ).scalars().all()
    assert count == []  # nothing new was created
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_admin_register.py -q -k register_admin`
Expected: `ModuleNotFoundError: No module named 'app.admin'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/admin/__init__.py`:

```python
"""Clinic-admin self-sign-up (D-VERIFY v1)."""
```

Create `backend/app/admin/schemas.py`:

```python
"""Clinic-admin registration request shape.

Reuses the password bounds and locale type from `identity/schemas.py`
read-only — this module never imports or modifies anything in `identity`.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, EmailStr, Field, field_validator

from ..identity.schemas import PASSWORD_MAX, PASSWORD_MIN, Locale


class AdminRegisterRequest(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=PASSWORD_MIN, max_length=PASSWORD_MAX)
    clinic_id: uuid.UUID
    invite_code: str = Field(min_length=6, max_length=64)
    preferred_locale: Locale = "en"

    @field_validator("email", mode="after")
    @classmethod
    def _norm(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("full_name", mode="after")
    @classmethod
    def _trim(cls, v: str) -> str:
        return v.strip()
```

Create `backend/app/admin/service.py`:

```python
"""Clinic-admin self-sign-up (D-VERIFY v1).

Deliberately self-contained rather than extending `identity/service.py`:
that module is the carefully-reasoned, already-reviewed home of patient and
doctor registration, and `identity/schemas.py`'s own docstring is explicit
that no registration model exposes a verified/privileged state. Admin
sign-up is privileged by construction (it can approve doctors), so it gets
its own small module, its own gate (the invite code), and duplicates the
~20 lines of Supabase-registration boilerplate rather than widen the shared
module's blast radius.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import writer as audit
from ..db.models import AccountStatus, ClinicInviteCode, ClinicStaff, LocaleCode, Profile, UserRole
from ..db.types import uuid7
from ..errors import ValidationFailed
from ..identity import security
from ..identity.schemas import SessionOut, landing_route_for
from ..settings import settings
from ..verification.codes import verify_code
from .schemas import AdminRegisterRequest


async def _user_by_email(session: AsyncSession, email: str) -> Profile | None:
    return (
        await session.execute(select(Profile).where(Profile.email == email.strip().lower()))
    ).scalar_one_or_none()


def _session_out(user: Profile, gotrue_session: Any) -> tuple[SessionOut, security.TokenPair]:
    locale = user.preferred_locale.value
    expires_at_ts = gotrue_session.expires_at or (
        int(datetime.now(UTC).timestamp()) + gotrue_session.expires_in
    )
    out = SessionOut(
        user_id=user.id,
        role="admin",
        full_name=user.full_name,
        locale=locale,  # type: ignore[arg-type]
        landing_route=landing_route_for("admin", locale),
        access_expires_at=datetime.fromtimestamp(expires_at_ts, tz=UTC),
    )
    pair = security.TokenPair(
        access_token=gotrue_session.access_token,
        refresh_token=gotrue_session.refresh_token,
    )
    return out, pair


async def register_admin(
    session: AsyncSession,
    body: AdminRegisterRequest,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[SessionOut, security.TokenPair | None]:
    existing = await _user_by_email(session, body.email)
    if existing is not None:
        # Same enumeration guard as identity/service.py::register — identical
        # shape, no row, no cookies, Supabase never called.
        return (
            SessionOut(
                user_id=uuid7(),
                role="admin",
                full_name=body.full_name,
                locale=body.preferred_locale,
                landing_route=landing_route_for("admin", body.preferred_locale),
                access_expires_at=datetime.now(UTC) + timedelta(minutes=settings.access_token_minutes),
            ),
            None,
        )

    invite = (
        await session.execute(
            select(ClinicInviteCode).where(
                ClinicInviteCode.clinic_id == body.clinic_id, ClinicInviteCode.is_active.is_(True)
            )
        )
    ).scalar_one_or_none()
    if invite is None or not verify_code(body.invite_code, invite.code_hash):
        raise ValidationFailed({"fields": {"invite_code": "errors.invite_code_invalid"}})

    client = await security.get_supabase_client()
    created = await client.auth.admin.create_user(
        {"email": body.email, "password": body.password, "email_confirm": True}
    )
    user_id = uuid.UUID(created.user.id)

    user = await session.get(Profile, user_id)
    if user is None:
        user = Profile(id=user_id)
        session.add(user)
    user.email = body.email
    user.role = UserRole.CLINIC_ADMIN
    user.status = AccountStatus.ACTIVE
    user.preferred_locale = LocaleCode(body.preferred_locale)
    user.full_name = body.full_name
    user.is_synthetic = settings.environment != "pilot"
    await session.flush()

    session.add(ClinicStaff(id=uuid7(), user_id=user.id, clinic_id=body.clinic_id, role="admin"))

    await audit.write(
        session,
        action=audit.AUTH_REGISTER,
        actor_user_id=user.id,
        actor_role=user.role.value,
        ip_address=ip,
        user_agent=user_agent,
        detail={"role": "admin"},
    )

    auth_client = await security.new_auth_client()
    signed_in = await auth_client.auth.sign_in_with_password(
        {"email": body.email, "password": body.password}
    )
    out, pair = _session_out(user, signed_in.session)
    await session.commit()
    return out, pair
```

Create `backend/ops/scripts/set_clinic_invite_code.py`:

```python
"""Set or rotate a clinic's admin sign-up invite code.

The plain code is printed ONCE and never stored — only its hash is. Run from
the repo root:

    uv run backend/ops/scripts/set_clinic_invite_code.py "Shifa International"

Tell the team before running this against the shared Supabase project: it
either creates or overwrites that clinic's one invite-code row, so an old
code already given out stops working the moment this runs.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.db.models import Clinic, ClinicInviteCode  # noqa: E402
from app.db.session import SessionFactory  # noqa: E402
from app.verification.codes import generate_code, hash_code  # noqa: E402
from sqlalchemy import func, select  # noqa: E402


async def main(clinic_name: str) -> None:
    async with SessionFactory() as session:
        clinic = (
            await session.execute(
                select(Clinic).where(func.lower(Clinic.name) == clinic_name.lower())
            )
        ).scalar_one_or_none()
        if clinic is None:
            print(f"No clinic named {clinic_name!r}. Nothing changed.")
            return

        code = generate_code()
        row = await session.get(ClinicInviteCode, clinic.id)
        if row is None:
            session.add(ClinicInviteCode(clinic_id=clinic.id, code_hash=hash_code(code), is_active=True))
        else:
            row.code_hash = hash_code(code)
            row.is_active = True
        await session.commit()

    print(f"Invite code for {clinic_name}: {code}")
    print("This is shown once. Give it to that clinic's admin now — it cannot be read back later.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: uv run backend/ops/scripts/set_clinic_invite_code.py \"<clinic name>\"")
        raise SystemExit(1)
    asyncio.run(main(sys.argv[1]))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_admin_register.py -q -k register_admin`
Expected: `5 passed`.

- [ ] **Step 5: Lint, commit**

```bash
uv run ruff check backend tests && uv run ruff format backend tests
git add backend/app/admin backend/ops/scripts/set_clinic_invite_code.py tests/test_admin_register.py
git commit -m "feat(verify): add clinic-admin self-sign-up gated by an invite code"
```

---

### Task 6: Admin sign-up page

**Files:**
- Create: `frontend/templates/auth/admin_register.html`
- Modify: `backend/app/web/router.py` (new routes; new imports)
- Modify: `backend/app/i18n/messages/en.json`, `ur.json`
- Test: `tests/test_admin_register.py` (append)

**Interfaces:**
- Consumes: `admin_service.register_admin` (Task 5), `_clinic_options` (existing helper in `web/router.py`), `set_session_cookies` (existing import), `enforce_auth_rate_limit`.
- Produces: `GET /{locale}/admin/register`, `POST /{locale}/admin/register`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_admin_register.py`:

```python
async def test_admin_register_page_renders(client, db, clinic):
    r = await client.get("/en/admin/register")
    assert r.status_code == 200
    assert in_html("admin.register.title") in r.text
    assert str(clinic.id) in r.text  # clinic appears in the dropdown


async def test_admin_register_submit_with_right_code_signs_in(client, db, clinic):
    from app.verification.codes import hash_code
    from app.db.models import ClinicInviteCode

    db.add(ClinicInviteCode(clinic_id=clinic.id, code_hash=hash_code("SIGNUP-CODE-0001"), is_active=True))
    await db.commit()

    r = await client.post(
        "/en/admin/register",
        data={
            "full_name": "Front Desk Lead",
            "email": "fd@x.com",
            "password": "CorrectHorseBattery1",
            "password_confirm": "CorrectHorseBattery1",
            "clinic_id": str(clinic.id),
            "invite_code": "SIGNUP-CODE-0001",
            "consent": "on",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/en/admin"
    assert "cn_access" in r.cookies or len(r.cookies) > 0


async def test_admin_register_submit_with_wrong_code_shows_an_error(client, db, clinic):
    r = await client.post(
        "/en/admin/register",
        data={
            "full_name": "Someone",
            "email": "x@x.com",
            "password": "CorrectHorseBattery1",
            "password_confirm": "CorrectHorseBattery1",
            "clinic_id": str(clinic.id),
            "invite_code": "WRONG-CODE-0001",
            "consent": "on",
        },
    )
    assert r.status_code == 422
    assert "cn_access" not in r.cookies
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_admin_register.py -q -k admin_register_page or admin_register_submit`
Expected: 404s — the route doesn't exist yet.

- [ ] **Step 3: Write the template and routes**

Create `frontend/templates/auth/admin_register.html`:

```html
{% extends "base.html" %}
{% import "partials/form_field.html" as f %}
{#- D-VERIFY — clinic-admin self-sign-up. Deliberately not linked from the
    public register page: the invite code is the gate, not discoverability.
    Context (web/router.py::admin_register_page/_submit): values, errors,
    form_error, clinics. -#}
{% block title %}{{ t('admin.register.title') }} · {{ t('app.name') }}{% endblock %}

{% block body %}
<main class="auth-page">
  <div class="auth-shell">
    <div class="auth-head">
      <div class="brandmark" aria-hidden="true">C</div>
      <h1 class="auth-title">{{ t('admin.register.title') }}</h1>
      <p class="auth-sub">{{ t('admin.register.subtitle') }}</p>
    </div>

    <form class="card" method="post" action="/{{ locale }}/admin/register" novalidate>
      {{ f.form_banner(form_error) }}

      {{ f.field('full_name', t('field.full_name'), value=values.get('full_name', ''),
                 errors=errors, required=True, autocomplete='name') }}
      {{ f.field('email', t('field.email'), type='email', value=values.get('email', ''),
                 errors=errors, required=True, autocomplete='email') }}

      <div class="grid-2">
        {{ f.field('password', t('field.password'), type='password',
                   errors=errors, required=True, autocomplete='new-password') }}
        {{ f.field('password_confirm', t('field.confirm_password'), type='password',
                   errors=errors, required=True, autocomplete='new-password') }}
      </div>

      {{ f.select('clinic_id', t('field.clinic'), clinics,
                  value=values.get('clinic_id', ''), errors=errors,
                  placeholder=t('field.clinic_placeholder')) }}
      {{ f.field('invite_code', t('field.invite_code'), value=values.get('invite_code', ''),
                 errors=errors, required=True, dir='ltr', help=t('field.invite_code_help')) }}

      <label class="checkline">
        <input type="checkbox" name="consent" {% if values.get('consent') %}checked{% endif %}>
        {{ t('auth.register.consent') }}
      </label>
      {% if errors.get('consent') %}
        <span class="field-error" role="alert"><span aria-hidden="true">!</span>{{ errors.get('consent') }}</span>
      {% endif %}

      {{ f.submit(t('admin.register.submit')) }}

      <div class="centre-note">
        {{ t('auth.register.already') }}
        <a href="/{{ locale }}/login">{{ t('auth.register.sign_in') }}</a>
      </div>
    </form>

    <p class="mono-note">{{ t('app.encrypted_note') }}</p>
  </div>
</main>
<script src="/static/js/auth.js" defer></script>
{% endblock %}
```

In `backend/app/web/router.py`, add to the imports:

```python
from ..admin import schemas as admin_schemas
from ..admin import service as admin_service
```

Add these two routes directly after the existing `register_submit` function (before `@router.post("/{locale}/logout")`):

```python
# ── Clinic-admin sign-up (D-VERIFY v1) ───────────────────────────────────
@router.get("/{locale}/admin/register", response_class=HTMLResponse)
async def admin_register_page(
    request: Request, locale: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    loc = normalise_locale(locale)
    if actor is not None:
        return RedirectResponse(f"/{loc}/{_area(actor.role)}", status_code=303)
    return _render(
        request, "auth/admin_register.html", loc,
        values={}, errors={}, form_error=None, clinics=await _clinic_options(session),
    )


@router.post("/{locale}/admin/register")
async def admin_register_submit(
    request: Request,
    locale: str,
    session: SessionDep,
    full_name: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    password_confirm: Annotated[str, Form()] = "",
    clinic_id: Annotated[str, Form()] = "",
    invite_code: Annotated[str, Form()] = "",
    consent: Annotated[str | None, Form()] = None,
) -> Response:
    loc = normalise_locale(locale)
    values = {
        "full_name": full_name, "email": email,
        "clinic_id": clinic_id, "invite_code": invite_code, "consent": bool(consent),
    }

    async def rerender(errors: dict[str, str], form_error: str | None = None, code: int = 422) -> Response:
        return _render(
            request, "auth/admin_register.html", loc, status_code=code,
            values=values, errors=errors, clinics=await _clinic_options(session), form_error=form_error,
        )

    try:
        await enforce_auth_rate_limit(request)
    except RateLimited:
        return await rerender({}, translate("errors.rate_limited", loc), 429)

    errors: dict[str, str] = {}
    if not full_name.strip():
        errors["full_name"] = translate("errors.name_required", loc)
    if len(password) < 10:
        errors["password"] = translate("errors.password_short", loc)
    elif password != password_confirm:
        errors["password_confirm"] = translate("errors.password_mismatch", loc)
    if not consent:
        errors["consent"] = translate("errors.consent_required", loc)
    if errors:
        return await rerender(errors)

    try:
        body = admin_schemas.AdminRegisterRequest(
            full_name=full_name, email=email, password=password,
            clinic_id=uuid.UUID(clinic_id), invite_code=invite_code, preferred_locale=loc,
        )
    except (ValidationError, ValueError):
        return await rerender({"email": translate("errors.email_invalid", loc)})

    try:
        out, pair = await admin_service.register_admin(
            session, body,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except ValidationFailed as exc:
        fields = exc.details.get("fields", {})
        return await rerender({k: translate(v, loc) for k, v in fields.items()})
    except AppError:
        return await rerender({}, translate("errors.internal", loc), 500)

    if pair is None:
        return RedirectResponse(f"/{loc}/login", status_code=303)

    response = RedirectResponse(f"/{loc}/{_area(out.role)}", status_code=303)
    set_session_cookies(response, pair)
    return response
```

Add to `backend/app/i18n/messages/en.json`:

```json
  "admin.register.title": "Clinic admin sign-up",
  "admin.register.subtitle": "You'll need the invite code for your clinic.",
  "admin.register.submit": "Create admin account",
  "field.invite_code": "Invite code",
  "field.invite_code_help": "Given to your clinic by the CuraNode team.",
  "errors.invite_code_invalid": "That invite code doesn't match this clinic."
```

Add to `backend/app/i18n/messages/ur.json`:

```json
  "admin.register.title": "کلینک ایڈمن سائن اپ",
  "admin.register.subtitle": "آپ کو اپنے کلینک کا انوائٹ کوڈ درکار ہوگا۔",
  "admin.register.submit": "ایڈمن اکاؤنٹ بنائیں",
  "field.invite_code": "انوائٹ کوڈ",
  "field.invite_code_help": "یہ کیورا نوڈ ٹیم کی طرف سے آپ کے کلینک کو دیا جاتا ہے۔",
  "errors.invite_code_invalid": "یہ انوائٹ کوڈ اس کلینک سے میل نہیں کھاتا۔"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_admin_register.py -q`
Expected: `11 passed` (3 from Task 4 + 5 from Task 5 + 3 from this task).

- [ ] **Step 5: Run the web suite for regressions, lint, commit**

```bash
uv run pytest tests/test_web.py tests/test_admin_register.py -q
uv run ruff check backend tests && uv run ruff format backend tests
git add frontend/templates/auth/admin_register.html backend/app/web/router.py backend/app/i18n tests/test_admin_register.py
git commit -m "feat(verify): add the clinic-admin sign-up page"
```

---

### Task 7: Doctor-side rejected/revoked banner

**Files:**
- Modify: `backend/app/web/router.py` (`_guarded`'s doctor branch)
- Modify: `frontend/templates/shell.html`
- Modify: `backend/app/i18n/messages/en.json`, `ur.json`
- Test: `tests/test_verification.py` (append)

**Interfaces:**
- Consumes: `verification_service.doctor_status_for_banner` (Task 3).
- Produces: nothing other tasks consume.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_verification.py`:

```python
async def login(client, email: str):
    from tests.conftest import TEST_PASSWORD

    r = await client.post("/api/v1/auth/login", json={"email": email, "password": TEST_PASSWORD})
    assert r.status_code == 200, r.text


async def test_rejected_doctor_sees_the_red_banner_and_no_dashboard_list(client, db, clinic, admin):
    user = await make_user(db, email="dr@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    doctor = await doctor_of(db, user)
    await verification_service.reject(db, admin, doctor.id, reason="name_mismatch", note=None)
    await login(client, "dr@x.com")

    r = await client.get("/en/doctor")
    assert r.status_code == 200
    assert "<table" not in r.text  # no patient list, same FR3 rule as pending


async def test_approving_a_rejected_doctor_removes_the_banner(client, db, clinic, admin):
    # Review focus 5 — the stale banner must not survive the approval.
    user = await make_user(db, email="dr@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    doctor = await doctor_of(db, user)
    await verification_service.reject(db, admin, doctor.id, reason="name_mismatch", note=None)
    await login(client, "dr@x.com")
    assert "<table" not in (await client.get("/en/doctor")).text

    await verification_service.approve(
        db, admin, doctor.id,
        checked_number=True, checked_name=True, checked_licence=True, note=None,
    )

    html = (await client.get("/en/doctor")).text
    assert "My patients" in html or "<table" in html  # the real dashboard renders again
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_verification.py -q -k rejected_doctor_sees or approving_a_rejected`
Expected: both FAIL — today a rejected doctor is treated exactly like a pending one (the yellow "awaiting verification" banner), and `doctor_status_for_banner` is never called from `_guarded`, so nothing distinguishes the two yet. Read the actual output to confirm it fails for that reason, not a typo.

- [ ] **Step 3: Wire the banner**

In `backend/app/web/router.py`, add to the imports:

```python
from ..verification import service as verification_service
```

In `_guarded`, replace:

```python
    pending_message = ""
    show_banner = False
    if actor.role == UserRole.DOCTOR.value and not actor.is_verified_doctor:
        show_banner = True
        clinic_name = ""
        if me.clinic_ids:
            clinic = await session.get(Clinic, me.clinic_ids[0])
            clinic_name = clinic.name if clinic else ""
        pending_message = translate("doctor.pending_body", loc, clinic=clinic_name)
```

with:

```python
    pending_message = ""
    show_banner = False
    rejected_message = ""
    show_rejected_banner = False
    if actor.role == UserRole.DOCTOR.value and not actor.is_verified_doctor:
        clinic_name = ""
        if me.clinic_ids:
            clinic = await session.get(Clinic, me.clinic_ids[0])
            clinic_name = clinic.name if clinic else ""
        status, reason = await verification_service.doctor_status_for_banner(session, actor)
        if status == "rejected":
            show_rejected_banner = True
            reason_text = translate(f"verify.reason.{reason}", loc) if reason else translate("verify.reason.other", loc)
            rejected_message = translate("doctor.rejected_body", loc, reason=reason_text, clinic=clinic_name)
        else:
            show_banner = True
            pending_message = translate("doctor.pending_body", loc, clinic=clinic_name)
```

And in the final `_render(...)` call at the end of `_guarded`, add two kwargs next to `show_unverified_banner`/`pending_message`:

```python
        show_unverified_banner=show_banner,
        pending_message=pending_message,
        show_rejected_banner=show_rejected_banner,
        rejected_message=rejected_message,
```

In `frontend/templates/shell.html`, directly after the existing pending-banner `{% endif %}` (before the `<div class="panel">`), add:

```html
    {% if show_rejected_banner %}
      <div class="banner banner--error" role="alert">
        <span aria-hidden="true">!</span>
        <div>
          <div class="banner__title">{{ t('doctor.rejected_title') }}</div>
          <div class="banner__body">{{ rejected_message }}</div>
        </div>
      </div>
    {% endif %}
```

Add to `backend/app/i18n/messages/en.json`:

```json
  "doctor.rejected_title": "Verification not approved",
  "doctor.rejected_body": "Your verification was not approved: {reason}. Please contact {clinic}.",
  "verify.reason.number_not_found": "PMDC number not found",
  "verify.reason.name_mismatch": "Name doesn't match",
  "verify.reason.licence_invalid": "Licence invalid or suspended",
  "verify.reason.not_at_clinic": "Not at this clinic",
  "verify.reason.other": "Other"
```

Add to `backend/app/i18n/messages/ur.json`:

```json
  "doctor.rejected_title": "تصدیق منظور نہیں ہوئی",
  "doctor.rejected_body": "آپ کی تصدیق منظور نہیں ہوئی: {reason}۔ براہ کرم {clinic} سے رابطہ کریں۔",
  "verify.reason.number_not_found": "پی ایم ڈی سی نمبر نہیں ملا",
  "verify.reason.name_mismatch": "نام میل نہیں کھاتا",
  "verify.reason.licence_invalid": "لائسنس غلط یا معطل ہے",
  "verify.reason.not_at_clinic": "اس کلینک میں نہیں",
  "verify.reason.other": "دیگر"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_verification.py -q -k rejected_doctor_sees or approving_a_rejected`
Expected: `2 passed`.

- [ ] **Step 5: Run the doctor dashboard suite for regressions, lint, commit**

```bash
uv run pytest tests/test_verification.py tests/test_doctor_dashboard.py -q
uv run ruff check backend tests && uv run ruff format backend tests
git add backend/app/web/router.py frontend/templates/shell.html backend/app/i18n tests/test_verification.py
git commit -m "feat(verify): show a red banner to a rejected or revoked doctor"
```

---

### Task 8: Admin verification dashboard

**Files:**
- Create: `frontend/templates/admin/dashboard.html`
- Modify: `backend/app/web/router.py` (replace the `admin_area` stub; add 3 POST routes)
- Modify: `backend/app/i18n/messages/en.json`, `ur.json`
- Test: `tests/test_verification.py` (append)

**Interfaces:**
- Consumes: `verification_service.list_doctors_for_clinic`, `.approve`, `.reject`, `.revoke`, `.latest_decisions_for`, `REASONS` (Tasks 3).
- Produces: `GET /{locale}/admin?tab=&saved=`, `POST /{locale}/admin/doctors/{doctor_id}/approve`, `/reject`, `/revoke`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_verification.py`:

```python
ADMIN_URL = "/en/admin"


async def test_admin_dashboard_requires_sign_in(client):
    r = await client.get(ADMIN_URL)
    assert r.status_code == 303 and r.headers["location"].startswith("/en/login")


async def test_admin_dashboard_pending_tab_lists_only_this_clinics_pending_doctors(
    client, db, clinic, other_clinic
):
    await make_user(db, email="adm@x.com", role=UserRole.CLINIC_ADMIN, clinic_id=clinic.id)
    mine = await make_user(db, email="dr1@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    await doctor_of(db, mine)
    theirs = await make_user(db, email="dr2@x.com", role=UserRole.DOCTOR, clinic_id=other_clinic.id)
    await doctor_of(db, theirs)
    await login(client, "adm@x.com")

    html = (await client.get(ADMIN_URL)).text
    assert "dr1@x.com" not in html  # emails never shown
    assert in_html("verify.tab.pending") in html


async def test_admin_can_approve_a_pending_doctor_with_the_checklist(client, db, clinic):
    await make_user(db, email="adm@x.com", role=UserRole.CLINIC_ADMIN, clinic_id=clinic.id)
    user = await make_user(db, email="dr@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    doctor = await doctor_of(db, user)
    await login(client, "adm@x.com")

    r = await client.post(
        f"/en/admin/doctors/{doctor.id}/approve",
        data={"checked_number": "on", "checked_name": "on", "checked_licence": "on"},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/en/admin?tab=verified&saved=approved"


async def test_admin_approve_with_a_missing_tick_is_refused(client, db, clinic):
    await make_user(db, email="adm@x.com", role=UserRole.CLINIC_ADMIN, clinic_id=clinic.id)
    user = await make_user(db, email="dr@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    doctor = await doctor_of(db, user)
    await login(client, "adm@x.com")

    r = await client.post(
        f"/en/admin/doctors/{doctor.id}/approve",
        data={"checked_number": "on", "checked_name": "on"},  # licence missing
    )
    assert r.status_code == 422
    assert in_html("errors.checklist_required") in r.text


async def test_admin_can_reject_with_a_reason(client, db, clinic):
    await make_user(db, email="adm@x.com", role=UserRole.CLINIC_ADMIN, clinic_id=clinic.id)
    user = await make_user(db, email="dr@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    doctor = await doctor_of(db, user)
    await login(client, "adm@x.com")

    r = await client.post(
        f"/en/admin/doctors/{doctor.id}/reject",
        data={"reason": "name_mismatch"},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/en/admin?tab=pending&saved=rejected"


async def test_admin_can_revoke_a_verified_doctor(client, db, clinic):
    await make_user(db, email="adm@x.com", role=UserRole.CLINIC_ADMIN, clinic_id=clinic.id)
    user = await make_user(db, email="dr@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    doctor = await doctor_of(db, user)
    await login(client, "adm@x.com")
    await client.post(
        f"/en/admin/doctors/{doctor.id}/approve",
        data={"checked_number": "on", "checked_name": "on", "checked_licence": "on"},
    )

    r = await client.post(
        f"/en/admin/doctors/{doctor.id}/revoke",
        data={"reason": "licence_invalid", "source_tab": "verified"},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/en/admin?tab=verified&saved=revoked"


async def test_admin_cannot_act_on_another_clinics_doctor(client, db, clinic, other_clinic):
    await make_user(db, email="adm@x.com", role=UserRole.CLINIC_ADMIN, clinic_id=clinic.id)
    user = await make_user(db, email="dr@x.com", role=UserRole.DOCTOR, clinic_id=other_clinic.id)
    doctor = await doctor_of(db, user)
    await login(client, "adm@x.com")

    r = await client.post(
        f"/en/admin/doctors/{doctor.id}/approve",
        data={"checked_number": "on", "checked_name": "on", "checked_licence": "on"},
    )
    assert r.status_code == 404


async def test_a_patient_or_doctor_cannot_reach_the_admin_dashboard(client, db):
    await make_user(db, email="pat@x.com", role=UserRole.PATIENT)
    await login(client, "pat@x.com")
    r = await client.get(ADMIN_URL)
    assert r.status_code == 303 and r.headers["location"] == "/en/patient"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_verification.py -q -k admin_dashboard or admin_can or admin_approve or admin_cannot or a_patient_or_doctor`
Expected: the sign-in-required test passes already (today's `_guarded` stub already redirects); every other test 404s or gets the empty placeholder page, since the routes and template don't exist yet.

- [ ] **Step 3: Write the template and routes**

Create `frontend/templates/admin/dashboard.html`:

```html
{% extends "base.html" %}
{#- D-VERIFY — clinic-admin doctor verification. Every action works without
    JavaScript: each row's form posts and redirects (PRG), same pattern as
    patient/access.html's revoke.

    Context (web/router.py::_render_admin):
      tab        "pending" | "verified" | "rejected"
      doctors    list[Doctor] at this admin's clinic, for this tab
      decisions  dict[doctor_id -> DoctorVerification] — latest, for verified/rejected
      reasons    REASONS tuple
      saved      success banner text, or None
      page_error page-level error banner text, or None -#}
{% import "partials/form_field.html" as f %}
{% block title %}{{ t('admin.dashboard.title') }} · {{ t('app.name') }}{% endblock %}

{% block body %}
<div class="shell">
  {% include "partials/topbar.html" %}

  <main class="content">
    {% if page_error %}
      <div class="banner banner--error" role="alert">
        <span aria-hidden="true">!</span><span>{{ page_error }}</span>
      </div>
    {% endif %}
    {% if saved %}
      <div class="banner banner--ok" role="status"><span>{{ saved }}</span></div>
    {% endif %}

    <section class="panel stack">
      {% if not doctors %}
        <p class="empty-state">{{ t('verify.empty.' ~ tab) }}</p>
      {% else %}
        <ul class="entry-list">
          {% for d in doctors %}
            <li class="entry-row">
              <div class="entry-row__main">
                <div class="entry-row__title">{{ d.full_name }}</div>
                <div class="mono" dir="ltr">{{ d.pmdc_number }}</div>
                <div class="mono-note">{{ d.specialty or '' }}</div>
                {% set dec = decisions.get(d.id) %}
                {% if tab == 'rejected' and dec %}
                  <div class="field-help">
                    {{ t('verify.last_decision', reason=t('verify.reason.' ~ (dec.reason or 'other'))) }}
                  </div>
                {% endif %}
              </div>

              <div class="entry-row__actions">
                <a class="btn btn--secondary btn--sm" href="https://pmdc.pk/" target="_blank" rel="noopener">
                  {{ t('verify.action.check_pmdc') }}
                </a>

                {% if tab in ('pending', 'rejected') %}
                  <details class="confirm">
                    <summary class="btn btn--primary btn--sm">{{ t('verify.action.approve') }}</summary>
                    <form method="post" action="/{{ locale }}/admin/doctors/{{ d.id }}/approve">
                      <input type="hidden" name="source_tab" value="{{ tab }}">
                      <label class="checkline">
                        <input type="checkbox" name="checked_number">
                        {{ t('verify.check.number') }}
                      </label>
                      <label class="checkline">
                        <input type="checkbox" name="checked_name">
                        {{ t('verify.check.name') }}
                      </label>
                      <label class="checkline">
                        <input type="checkbox" name="checked_licence">
                        {{ t('verify.check.licence') }}
                      </label>
                      {{ f.textarea('note', t('field.note'), rows=2, placeholder=t('field.note_placeholder'),
                                    id_prefix=d.id|string ~ '-approve-') }}
                      <button class="btn btn--primary btn--sm" type="submit"
                              data-label="{{ t('verify.action.confirm_approve') }}">{{ t('verify.action.confirm_approve') }}</button>
                    </form>
                  </details>
                {% endif %}

                {% if tab == 'pending' %}
                  <details class="confirm">
                    <summary class="btn btn--danger btn--sm">{{ t('verify.action.reject') }}</summary>
                    <form method="post" action="/{{ locale }}/admin/doctors/{{ d.id }}/reject">
                      <input type="hidden" name="source_tab" value="{{ tab }}">
                      <label class="field">
                        <span class="field-label">{{ t('field.reason') }}</span>
                        <select class="select" name="reason">
                          {% for r in reasons %}
                            <option value="{{ r }}">{{ t('verify.reason.' ~ r) }}</option>
                          {% endfor %}
                        </select>
                      </label>
                      {{ f.textarea('note', t('field.note'), rows=2, placeholder=t('field.note_placeholder'),
                                    id_prefix=d.id|string ~ '-reject-') }}
                      <button class="btn btn--danger btn--sm" type="submit"
                              data-label="{{ t('verify.action.confirm_reject') }}">{{ t('verify.action.confirm_reject') }}</button>
                    </form>
                  </details>
                {% endif %}

                {% if tab == 'verified' %}
                  <details class="confirm">
                    <summary class="btn btn--danger btn--sm">{{ t('verify.action.revoke') }}</summary>
                    <form method="post" action="/{{ locale }}/admin/doctors/{{ d.id }}/revoke">
                      <input type="hidden" name="source_tab" value="{{ tab }}">
                      <label class="field">
                        <span class="field-label">{{ t('field.reason') }}</span>
                        <select class="select" name="reason">
                          {% for r in reasons %}
                            <option value="{{ r }}">{{ t('verify.reason.' ~ r) }}</option>
                          {% endfor %}
                        </select>
                      </label>
                      {{ f.textarea('note', t('field.note'), rows=2, placeholder=t('field.note_placeholder'),
                                    id_prefix=d.id|string ~ '-revoke-') }}
                      <button class="btn btn--danger btn--sm" type="submit"
                              data-label="{{ t('verify.action.confirm_revoke') }}">{{ t('verify.action.confirm_revoke') }}</button>
                    </form>
                  </details>
                {% endif %}
              </div>
            </li>
          {% endfor %}
        </ul>
      {% endif %}
    </section>
  </main>
</div>
{% endblock %}
```

In `backend/app/web/router.py`, add to the imports:

```python
from ..verification.schemas import REASONS
```

(`verification_service` was already imported in Task 7.)

Replace the existing admin route:

```python
@router.get("/{locale}/admin", response_class=HTMLResponse)
async def admin_area(
    request: Request, locale: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    return await _guarded(request, locale, session, actor, "admin")
```

with:

```python
# ── Clinic-admin verification dashboard (D-VERIFY v1) ────────────────────
_ADMIN_TABS = ("pending", "verified", "rejected")
_ADMIN_SAVED = {
    "approved": "verify.saved.approved",
    "rejected": "verify.saved.rejected",
    "revoked": "verify.saved.revoked",
}


def _admin_nav(loc: str, *, current: str) -> list[dict[str, Any]]:
    return [
        {
            "href": f"/{loc}/admin?tab={t}",
            "label": translate(f"verify.tab.{t}", loc),
            "current": current == t,
        }
        for t in _ADMIN_TABS
    ]


def _admin_page_guard(request: Request, locale: str, actor: Actor | None) -> Response | None:
    loc = normalise_locale(locale)
    if actor is None:
        return RedirectResponse(f"/{loc}/login?next={request.url.path}", status_code=303)
    if actor.role != UserRole.CLINIC_ADMIN.value:
        return RedirectResponse(f"/{loc}/{_area(actor.role)}", status_code=303)
    return None


async def _render_admin(
    request: Request, loc: str, session: SessionDep, actor: Actor, *,
    tab: str, status_code: int = 200, saved: str | None = None, page_error: str | None = None,
) -> Response:
    if tab not in _ADMIN_TABS:
        tab = "pending"
    doctors = await verification_service.list_doctors_for_clinic(session, actor, tab)
    decisions = await verification_service.latest_decisions_for(session, [d.id for d in doctors])
    return _render(
        request, "admin/dashboard.html", loc, status_code=status_code,
        path_suffix="/admin",
        crumb=translate("admin.dashboard.title", loc),
        page_title=translate("admin.dashboard.title", loc),
        nav_items=_admin_nav(loc, current=tab),
        tab=tab,
        doctors=doctors,
        decisions=decisions,
        reasons=REASONS,
        saved=saved,
        page_error=page_error,
    )


async def _admin_failure(
    request: Request, loc: str, session: SessionDep, actor: Actor, exc: Exception, *, tab: str,
) -> Response:
    await session.rollback()
    if isinstance(exc, ValidationFailed):
        fields = exc.details.get("fields", {})
        msg = translate(next(iter(fields.values())), loc) if fields else translate("errors.validation_failed", loc)
        return await _render_admin(request, loc, session, actor, tab=tab, status_code=422, page_error=msg)
    if isinstance(exc, NotFound):
        return await _render_admin(request, loc, session, actor, tab=tab, status_code=404, page_error=translate("errors.not_found", loc))
    if isinstance(exc, RateLimited):
        return await _render_admin(request, loc, session, actor, tab=tab, status_code=429, page_error=translate("errors.rate_limited", loc))
    return await _render_admin(request, loc, session, actor, tab=tab, status_code=500, page_error=translate("errors.internal", loc))


@router.get("/{locale}/admin", response_class=HTMLResponse)
async def admin_area(
    request: Request, locale: str, session: SessionDep, actor: OptionalActorDep,
    tab: str = "pending", saved: str | None = None,
) -> Response:
    if (redirect := _admin_page_guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    banner = translate(_ADMIN_SAVED[saved], loc) if saved in _ADMIN_SAVED else None
    return await _render_admin(request, loc, session, actor, tab=tab, saved=banner)


@router.post("/{locale}/admin/doctors/{doctor_id}/approve")
async def admin_approve_submit(
    request: Request, locale: str, doctor_id: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    if (redirect := _admin_page_guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    form = await request.form()
    source_tab = str(form.get("source_tab", "pending"))
    try:
        parsed_id = _parse_entry_id(doctor_id)
        if parsed_id is None:
            raise NotFound()
        await verification_service.approve(
            session, actor, parsed_id,
            checked_number=form.get("checked_number") is not None,
            checked_name=form.get("checked_name") is not None,
            checked_licence=form.get("checked_licence") is not None,
            note=str(form.get("note", "")).strip() or None,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception as exc:  # noqa: BLE001 — every outcome is mapped to the page
        return await _admin_failure(request, loc, session, actor, exc, tab=source_tab)
    return RedirectResponse(f"/{loc}/admin?tab=verified&saved=approved", status_code=303)


@router.post("/{locale}/admin/doctors/{doctor_id}/reject")
async def admin_reject_submit(
    request: Request, locale: str, doctor_id: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    if (redirect := _admin_page_guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    form = await request.form()
    source_tab = str(form.get("source_tab", "pending"))
    try:
        parsed_id = _parse_entry_id(doctor_id)
        if parsed_id is None:
            raise NotFound()
        await verification_service.reject(
            session, actor, parsed_id,
            reason=str(form.get("reason", "")),
            note=str(form.get("note", "")).strip() or None,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception as exc:  # noqa: BLE001
        return await _admin_failure(request, loc, session, actor, exc, tab=source_tab)
    return RedirectResponse(f"/{loc}/admin?tab=pending&saved=rejected", status_code=303)


@router.post("/{locale}/admin/doctors/{doctor_id}/revoke")
async def admin_revoke_submit(
    request: Request, locale: str, doctor_id: str, session: SessionDep, actor: OptionalActorDep
) -> Response:
    if (redirect := _admin_page_guard(request, locale, actor)) is not None:
        return redirect
    assert actor is not None
    loc = normalise_locale(locale)
    form = await request.form()
    source_tab = str(form.get("source_tab", "verified"))
    try:
        parsed_id = _parse_entry_id(doctor_id)
        if parsed_id is None:
            raise NotFound()
        await verification_service.revoke(
            session, actor, parsed_id,
            reason=str(form.get("reason", "")),
            note=str(form.get("note", "")).strip() or None,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception as exc:  # noqa: BLE001
        return await _admin_failure(request, loc, session, actor, exc, tab=source_tab)
    return RedirectResponse(f"/{loc}/admin?tab=verified&saved=revoked", status_code=303)
```

Add to `backend/app/i18n/messages/en.json`:

```json
  "verify.tab.pending": "Pending",
  "verify.tab.verified": "Verified",
  "verify.tab.rejected": "Rejected",
  "verify.empty.pending": "No doctors are waiting for verification.",
  "verify.empty.verified": "No verified doctors yet.",
  "verify.empty.rejected": "No rejected or revoked doctors.",
  "verify.action.check_pmdc": "Check on PMDC",
  "verify.action.approve": "Approve",
  "verify.action.confirm_approve": "Confirm approval",
  "verify.action.reject": "Reject",
  "verify.action.confirm_reject": "Confirm rejection",
  "verify.action.revoke": "Revoke",
  "verify.action.confirm_revoke": "Confirm revoke",
  "verify.check.number": "The PMDC number exists on PMDC's Doctor Search.",
  "verify.check.name": "The name matches.",
  "verify.check.licence": "The licence is valid, with no suspension.",
  "verify.last_decision": "Rejected: {reason}",
  "verify.saved.approved": "Doctor approved.",
  "verify.saved.rejected": "Doctor rejected.",
  "verify.saved.revoked": "Doctor's verification revoked.",
  "field.reason": "Reason",
  "field.note": "Note (optional)",
  "field.note_placeholder": "Only other clinic admins see this.",
  "errors.checklist_required": "Tick all three boxes before approving."
```

Add to `backend/app/i18n/messages/ur.json`:

```json
  "verify.tab.pending": "زیرِ التوا",
  "verify.tab.verified": "تصدیق شدہ",
  "verify.tab.rejected": "مسترد",
  "verify.empty.pending": "کوئی ڈاکٹر تصدیق کا منتظر نہیں ہے۔",
  "verify.empty.verified": "ابھی تک کوئی ڈاکٹر تصدیق شدہ نہیں ہے۔",
  "verify.empty.rejected": "کوئی مسترد یا منسوخ شدہ ڈاکٹر نہیں ہے۔",
  "verify.action.check_pmdc": "پی ایم ڈی سی پر چیک کریں",
  "verify.action.approve": "منظور کریں",
  "verify.action.confirm_approve": "منظوری کی تصدیق کریں",
  "verify.action.reject": "مسترد کریں",
  "verify.action.confirm_reject": "مسترد کرنے کی تصدیق کریں",
  "verify.action.revoke": "منسوخ کریں",
  "verify.action.confirm_revoke": "منسوخی کی تصدیق کریں",
  "verify.check.number": "پی ایم ڈی سی نمبر پی ایم ڈی سی ڈاکٹر سرچ پر موجود ہے۔",
  "verify.check.name": "نام میل کھاتا ہے۔",
  "verify.check.licence": "لائسنس درست ہے، کوئی معطلی نہیں۔",
  "verify.last_decision": "مسترد: {reason}",
  "verify.saved.approved": "ڈاکٹر منظور ہو گیا۔",
  "verify.saved.rejected": "ڈاکٹر مسترد کر دیا گیا۔",
  "verify.saved.revoked": "ڈاکٹر کی تصدیق منسوخ کر دی گئی۔",
  "field.reason": "وجہ",
  "field.note": "نوٹ (اختیاری)",
  "field.note_placeholder": "یہ صرف دوسرے کلینک ایڈمن دیکھ سکتے ہیں۔",
  "errors.checklist_required": "منظور کرنے سے پہلے تینوں خانوں پر نشان لگائیں۔"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_verification.py -q`
Expected: `31 passed` (20 from Task 3 + 2 from Task 7 + 9 from this task).

- [ ] **Step 5: Run the full suite for regressions, lint, commit**

```bash
uv run pytest -q
uv run ruff check backend tests && uv run ruff format backend tests
git add frontend/templates/admin/dashboard.html backend/app/web/router.py backend/app/i18n tests/test_verification.py
git commit -m "feat(verify): add the clinic-admin verification dashboard"
```

Expected: the whole suite is green. Running total: 262 (end of Task 3) + 3 (Task 4) + 5 (Task 5) + 3 (Task 6) + 2 (Task 7) + 9 (Task 8) = **284**, plus the 3 model tests from Task 1 and 6 from Task 2, so the real full-suite number will be printed by the test run — read it, don't assume it.

---

### Task 9: Quality gate and i18n completeness

**Files:**
- Test: `tests/test_admin_register.py` (append one test)

**Interfaces:** none new.

- [ ] **Step 1: Write the i18n completeness test**

Append to `tests/test_admin_register.py`:

```python
def test_catalogue_has_every_new_key_in_both_languages():
    assert missing_keys() == {"ur": []}
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_admin_register.py -q -k catalogue`
Expected: `1 passed`. If it fails, the failure message lists exactly which key is missing from `ur.json` — add it there (translate faithfully; do not leave an English placeholder) and rerun.

- [ ] **Step 3: Full suite and lint**

```bash
uv run pytest -q
uv run ruff check backend tests && uv run ruff format backend tests
```

Expected: all green, no formatting changes needed.

- [ ] **Step 4: Manual check in the browser**

With `uv run backend/app/main.py` running against the shared Supabase project (only after the user confirms the team is ready — see Global Constraints):

1. Run `uv run backend/ops/scripts/set_clinic_invite_code.py "Shifa International"` (or whichever clinic exists) and note the printed code.
2. Sign up at `/en/admin/register` with that code. Confirm you land on `/en/admin` with an empty "Pending" tab if no doctors are waiting.
3. Register a new doctor at that clinic. Confirm they appear in the admin's Pending tab.
4. Approve them with all three ticks. Confirm: the Verified tab shows them; signing in as that doctor shows the D4 patient dashboard, not a banner.
5. Register a second doctor. Reject them with a reason. Confirm: they appear in Rejected with that reason; signing in as them shows the new red banner with that reason.
6. From the Rejected tab, approve that same doctor. Confirm the red banner is gone on their next page load.
7. From the Verified tab, revoke the first doctor with a reason. Confirm their dashboard reverts to the red banner on their next load.
8. Repeat steps 2-7's page loads once in Urdu (`/ur/...`) — confirm RTL layout, translated labels, and that the PMDC number and invite code stay left-to-right.

If real credentials for the shared project aren't available in this session, say so explicitly; the automated suite is then the only proof, same as the D4 plan's equivalent step.

- [ ] **Step 5: Apply the migration — ask first**

Do **not** run `uv run alembic upgrade head` against the shared Supabase project without the user explicitly saying the team is ready (Global Constraints). When she confirms, run it, then verify only the two new tables were added:

```bash
uv run alembic upgrade head
uv run alembic current
```

- [ ] **Step 6: Review and hand off**

Run the `security-review` skill on the branch, then `/code-review`. Fix real findings, each with a test that fails before the fix and passes after.

Do not push or open a PR without the user's go-ahead. When ready, follow the same tiered-merge flow as D4: PR into `dev`, reviewed by a teammate (AbdurRahman is the natural reviewer here — this touches `clinic_staff`/the shared schema), merged into `dev` only, with `main` promoted later and separately.
