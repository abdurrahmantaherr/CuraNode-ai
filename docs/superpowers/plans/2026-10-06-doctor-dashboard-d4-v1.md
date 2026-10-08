# D4 v1 Doctor Patient Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A verified doctor's home page (`/{locale}/doctor`) lists the patients who currently grant them access, showing name, age, gender and passport number.

**Architecture:** One new query in Osama's consent service decides who is visible (reusing his `_active()` clause). A small pure module builds display rows (age, dashes, link). The existing `_guarded` doctor branch passes the rows to `shell.html`, which includes a new partial. No migration, no new table.

**Tech Stack:** FastAPI, SQLAlchemy 2 (async), Jinja2, pytest (`asyncio_mode = "auto"`, in-memory SQLite, `FakeSupabaseAuth`), ruff.

**Spec:** `docs/superpowers/specs/2026-10-05-doctor-dashboard-d4-v1-design.md`

## Global Constraints

- Branch: `feature/doctor-dashboard`, based on `feature/P4-medical-passport` until it merges into `dev`. Never push or open a PR without the user saying so.
- No database migration and no new table.
- Consent is decided only in `backend/app/consent/service.py`. The dashboard never queries `Patient` for visibility itself.
- A row shows only: name, age, gender, passport number. No grant date, no clinical data, no risk flag.
- Row link is `/{locale}/doctor/patient/{patient_id}`. Never put a passport number in a URL (P4 AC-12).
- No `|safe` on patient-supplied text. Names go through Jinja2 autoescaping.
- Every user-facing string is an i18n key present in both `en.json` and `ur.json` (`missing_keys()` must stay empty).
- Passport number renders `dir="ltr"`, also on the Urdu page.
- Keep edits to Osama's files (`consent/service.py`, `web/router.py`, `audit/writer.py`, `app.css`, both JSON files) minimal and additive, so his PR and this one merge cleanly.
- Style: ruff-clean (`uv run ruff check backend tests` and `uv run ruff format backend tests`). Run tests with `uv run pytest -q`.
- Commit messages end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

Inputs the spec implies but its tests do not obviously cover. Each has a test in the task named in brackets.

1. A patient with a blank-string gender (not `NULL`) should show "—", not an empty cell. [Task 1]
2. A date of birth in the future (bad data) should show "—", not a negative age. [Task 1]
3. A patient with two active grants to the same doctor (the database does not forbid it) must appear once. [Task 2]
4. A doctor who is verified but has no clinic affiliation still sees the list. [Task 3]
5. A doctor account whose verification is revoked after login loses the list on the next request, because the status is read from the database every time. [Task 3]

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/doctor/__init__.py` (new) | Package marker. |
| `backend/app/doctor/rows.py` (new) | Pure functions: `age_in_years`, `build_rows`; dataclass `PatientRow`. No database access. |
| `backend/app/consent/service.py` (modify, append one function) | `list_patients_for_doctor`: who may this doctor see. |
| `backend/app/audit/writer.py` (modify, one constant) | `DOCTOR_DASHBOARD_VIEW`. |
| `backend/app/web/router.py` (modify, doctor branch of `_guarded`) | Load rows, write the audit entry, pass `patients` to the template. |
| `frontend/templates/doctor/_patient_list.html` (new) | The table and the empty state. |
| `frontend/templates/shell.html` (modify) | Include the partial when `patients` is defined. |
| `frontend/static/css/app.css` (modify, append) | Minimal `.patient-table` styles. |
| `backend/app/i18n/messages/en.json`, `ur.json` (modify) | Six new keys each. |
| `tests/test_doctor_rows.py` (new) | Unit tests for `rows.py`. |
| `tests/test_doctor_dashboard.py` (new) | Service tests and page tests. |

Spec deviation, on purpose: the spec listed no helper module. `rows.py` is added so the age logic is unit-testable and so `web/router.py` (Osama's heavy file) gets the smallest possible edit.

---

### Task 1: Row builder (`age_in_years`, `build_rows`)

**Files:**
- Create: `backend/app/doctor/__init__.py`
- Create: `backend/app/doctor/rows.py`
- Test: `tests/test_doctor_rows.py`

**Interfaces:**
- Consumes: `app.db.models.Patient` fields `id`, `full_name`, `date_of_birth`, `gender`, `passport_no`.
- Produces:
  - `age_in_years(born: date | None, today: date) -> int | None`
  - `@dataclass(frozen=True) class PatientRow: name: str; age: str; gender: str; passport_no: str; href: str`
  - `build_rows(patients: Sequence[Patient], *, locale: str, today: date) -> list[PatientRow]`
  - constant `DASH = "—"`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_doctor_rows.py`:

```python
"""D4 v1 — display rows for the doctor dashboard (pure functions, no database)."""

from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace

from app.doctor.rows import DASH, PatientRow, age_in_years, build_rows

TODAY = date(2026, 10, 6)


def patient(**over):
    base = dict(
        id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        full_name="Ayesha Raza",
        date_of_birth=date(1994, 3, 14),
        gender="female",
        passport_no="CN-AAAA-1111",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_age_after_birthday_this_year():
    assert age_in_years(date(1994, 3, 14), TODAY) == 32


def test_age_before_birthday_this_year():
    assert age_in_years(date(1994, 12, 1), TODAY) == 31


def test_age_on_birthday_counts_the_new_year():
    assert age_in_years(date(1994, 10, 6), TODAY) == 32


def test_age_of_a_leap_day_baby():
    assert age_in_years(date(2000, 2, 29), date(2026, 2, 28)) == 25
    assert age_in_years(date(2000, 2, 29), date(2026, 3, 1)) == 26


def test_age_missing_dob_is_none():
    assert age_in_years(None, TODAY) is None


def test_age_future_dob_is_none():
    # Review focus 2 — bad data must not produce a negative age.
    assert age_in_years(date(2030, 1, 1), TODAY) is None


def test_build_rows_fills_every_field():
    rows = build_rows([patient()], locale="en", today=TODAY)
    assert rows == [
        PatientRow(
            name="Ayesha Raza",
            age="32",
            gender="female",
            passport_no="CN-AAAA-1111",
            href="/en/doctor/patient/00000000-0000-0000-0000-000000000001",
        )
    ]


def test_build_rows_uses_the_locale_in_the_link():
    rows = build_rows([patient()], locale="ur", today=TODAY)
    assert rows[0].href.startswith("/ur/doctor/patient/")


def test_build_rows_shows_dash_for_missing_values():
    rows = build_rows([patient(date_of_birth=None, gender=None)], locale="en", today=TODAY)
    assert rows[0].age == DASH
    assert rows[0].gender == DASH


def test_build_rows_blank_gender_is_a_dash():
    # Review focus 1 — an empty string is not a value.
    rows = build_rows([patient(gender="")], locale="en", today=TODAY)
    assert rows[0].gender == DASH


def test_build_rows_keeps_input_order():
    a = patient(full_name="A", id=uuid.uuid4())
    b = patient(full_name="B", id=uuid.uuid4())
    assert [r.name for r in build_rows([a, b], locale="en", today=TODAY)] == ["A", "B"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_doctor_rows.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'app.doctor'`.

- [ ] **Step 3: Write the minimal implementation**

Create `backend/app/doctor/__init__.py`:

```python
"""Doctor-side pages (FR3, D4)."""
```

Create `backend/app/doctor/rows.py`:

```python
"""Display rows for the doctor dashboard (D4 v1).

Pure functions only: no database access, no consent decisions. Who a doctor may
see is decided in `consent/service.py`; this module only formats what that
returns.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from ..db.models import Patient

DASH = "—"


@dataclass(frozen=True)
class PatientRow:
    name: str
    age: str
    gender: str
    passport_no: str
    href: str


def age_in_years(born: date | None, today: date) -> int | None:
    """Whole years since `born`; None when unknown or in the future."""
    if born is None or born > today:
        return None
    years = today.year - born.year
    if (today.month, today.day) < (born.month, born.day):
        years -= 1
    return years


def build_rows(patients: Sequence[Patient], *, locale: str, today: date) -> list[PatientRow]:
    rows: list[PatientRow] = []
    for p in patients:
        age = age_in_years(p.date_of_birth, today)
        rows.append(
            PatientRow(
                name=p.full_name,
                age=str(age) if age is not None else DASH,
                gender=p.gender or DASH,
                passport_no=p.passport_no,
                # The id, not the passport number: paths reach the access log (P4 AC-12).
                href=f"/{locale}/doctor/patient/{p.id}",
            )
        )
    return rows
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_doctor_rows.py -q`
Expected: `10 passed`.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check backend tests && uv run ruff format backend tests
git add backend/app/doctor tests/test_doctor_rows.py
git commit -m "feat(D4): add doctor dashboard row builder"
```

---

### Task 2: Consent query `list_patients_for_doctor`

**Files:**
- Modify: `backend/app/consent/service.py` (append after `patient_for_doctor`, currently the last function)
- Test: `tests/test_doctor_dashboard.py` (create; the page tests are added in Task 3)

**Interfaces:**
- Consumes: `_active()`, `DOCTOR`, `Actor`, `NotFound`, `Doctor`, `Patient`, `ConsentGrant`, `VerificationStatus` (all already imported in that module).
- Produces: `async def list_patients_for_doctor(session: AsyncSession, actor: Actor) -> list[Patient]`. Sorted by `full_name` then `id`. Raises `NotFound` when the actor is not a verified doctor.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_doctor_dashboard.py`:

```python
"""D4 v1 — doctor patient dashboard: consent query (this file's first half)
and the page (second half, added in the page task).

Visibility is the whole feature, so most tests here are about who must NOT
appear.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from app.consent import service as consent_service
from app.db.models import ConsentGrant, Doctor, Patient, Profile, UserRole
from app.db.types import utcnow, uuid7
from app.deps import Actor
from app.errors import NotFound
from sqlalchemy import select

from tests.conftest import make_user


# ── Helpers ──────────────────────────────────────────────────────────────
async def patient_of(db, user: Profile) -> Patient:
    stmt = select(Patient).where(Patient.user_id == user.id)
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one()


async def doctor_of(db, user: Profile) -> Doctor:
    stmt = select(Doctor).where(Doctor.user_id == user.id)
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one()


async def make_patient(db, email: str, full_name: str, **fields) -> Patient:
    user = await make_user(db, email=email, role=UserRole.PATIENT)
    patient = await patient_of(db, user)
    patient.full_name = full_name
    for key, value in fields.items():
        setattr(patient, key, value)
    await db.commit()
    return patient


async def grant(db, patient: Patient, doctor: Doctor, *, revoked=False, expires_in=None):
    row = ConsentGrant(
        id=uuid7(),
        patient_id=patient.id,
        grantee_type="doctor",
        grantee_id=doctor.id,
        scope_sections=[],
        granted_at=utcnow(),
        revoked_at=utcnow() if revoked else None,
        expires_at=(utcnow() + expires_in) if expires_in is not None else None,
    )
    db.add(row)
    await db.commit()
    return row


def as_actor(doctor: Doctor, *, verified: bool = True) -> Actor:
    return Actor(user_id=doctor.user_id, role="doctor", is_verified_doctor=verified)


@pytest.fixture
async def admin(db, clinic) -> Profile:
    return await make_user(db, email="adm@x.com", role=UserRole.CLINIC_ADMIN, clinic_id=clinic.id)


@pytest.fixture
async def doctor(db, clinic, admin) -> Doctor:
    user = await make_user(
        db,
        email="dr@x.com",
        role=UserRole.DOCTOR,
        clinic_id=clinic.id,
        is_verified=True,
        verifier_id=admin.id,
    )
    return await doctor_of(db, user)


@pytest.fixture
async def other_doctor(db, clinic, admin) -> Doctor:
    user = await make_user(
        db,
        email="dr2@x.com",
        role=UserRole.DOCTOR,
        clinic_id=clinic.id,
        is_verified=True,
        verifier_id=admin.id,
    )
    return await doctor_of(db, user)


# ── list_patients_for_doctor ─────────────────────────────────────────────
async def test_lists_only_patients_who_granted_this_doctor(db, doctor, other_doctor):
    mine = await make_patient(db, "p1@x.com", "Mine")
    theirs = await make_patient(db, "p2@x.com", "Theirs")
    await make_patient(db, "p3@x.com", "Nobody")  # no grant at all
    await grant(db, mine, doctor)
    await grant(db, theirs, other_doctor)

    got = await consent_service.list_patients_for_doctor(db, as_actor(doctor))

    assert [p.full_name for p in got] == ["Mine"]


async def test_revoked_grant_is_not_listed(db, doctor):
    p = await make_patient(db, "p1@x.com", "Revoked")
    await grant(db, p, doctor, revoked=True)
    assert await consent_service.list_patients_for_doctor(db, as_actor(doctor)) == []


async def test_expired_grant_is_not_listed(db, doctor):
    p = await make_patient(db, "p1@x.com", "Expired")
    await grant(db, p, doctor, expires_in=timedelta(days=-1))
    assert await consent_service.list_patients_for_doctor(db, as_actor(doctor)) == []


async def test_unexpired_grant_is_listed(db, doctor):
    p = await make_patient(db, "p1@x.com", "Future")
    await grant(db, p, doctor, expires_in=timedelta(days=30))
    got = await consent_service.list_patients_for_doctor(db, as_actor(doctor))
    assert [x.full_name for x in got] == ["Future"]


async def test_sorted_by_name(db, doctor):
    for i, name in enumerate(["Zainab", "Ali", "Bilal"]):
        p = await make_patient(db, f"p{i}@x.com", name)
        await grant(db, p, doctor)
    got = await consent_service.list_patients_for_doctor(db, as_actor(doctor))
    assert [p.full_name for p in got] == ["Ali", "Bilal", "Zainab"]


async def test_two_active_grants_list_the_patient_once(db, doctor):
    # Review focus 3 — the database has no unique constraint behind BL-04.
    p = await make_patient(db, "p1@x.com", "Twice")
    await grant(db, p, doctor)
    await grant(db, p, doctor)
    got = await consent_service.list_patients_for_doctor(db, as_actor(doctor))
    assert [x.full_name for x in got] == ["Twice"]


async def test_unverified_doctor_gets_not_found(db, clinic):
    user = await make_user(db, email="new@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    pending = await doctor_of(db, user)
    with pytest.raises(NotFound):
        await consent_service.list_patients_for_doctor(db, as_actor(pending, verified=True))


async def test_non_doctor_gets_not_found(db):
    user = await make_user(db, email="pat@x.com", role=UserRole.PATIENT)
    actor = Actor(user_id=user.id, role="patient")
    with pytest.raises(NotFound):
        await consent_service.list_patients_for_doctor(db, actor)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_doctor_dashboard.py -q`
Expected: 8 failures, `AttributeError: module 'app.consent.service' has no attribute 'list_patients_for_doctor'`.

- [ ] **Step 3: Write the implementation**

Append to the end of `backend/app/consent/service.py`:

```python


async def list_patients_for_doctor(session: AsyncSession, actor: Actor) -> list[Patient]:
    """D4 — every patient with an active grant naming this verified doctor, by name.

    Same rules as `_granted_patient`: the doctor and the grant are checked live
    on every call (BL-07), and anything but a verified doctor is `NotFound`.
    Two active grants for one patient (nothing in the database forbids it) still
    yield one row.
    """
    doctor = (
        await session.execute(select(Doctor).where(Doctor.user_id == actor.user_id))
    ).scalar_one_or_none()
    if doctor is None or doctor.verification_status != VerificationStatus.VERIFIED:
        raise NotFound()
    rows = await session.execute(
        select(Patient)
        .join(ConsentGrant, ConsentGrant.patient_id == Patient.id)
        .where(
            ConsentGrant.grantee_type == DOCTOR,
            ConsentGrant.grantee_id == doctor.id,
            _active(),
        )
        .distinct()
        .order_by(Patient.full_name, Patient.id)
    )
    return list(rows.scalars().all())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_doctor_dashboard.py -q`
Expected: `8 passed`.

- [ ] **Step 5: Run the consent suite for regressions, lint, commit**

```bash
uv run pytest tests/test_consent.py tests/test_passport_web.py -q
uv run ruff check backend tests && uv run ruff format backend tests
git add backend/app/consent/service.py tests/test_doctor_dashboard.py
git commit -m "feat(D4): list a verified doctor's consented patients"
```

Expected: the two existing suites still pass (50 tests).

---

### Task 3: Dashboard page (route, template, i18n, audit)

**Files:**
- Modify: `backend/app/audit/writer.py` (one constant)
- Modify: `backend/app/web/router.py` (imports and the doctor branch of `_guarded`)
- Create: `frontend/templates/doctor/_patient_list.html`
- Modify: `frontend/templates/shell.html`
- Modify: `frontend/static/css/app.css` (append)
- Modify: `backend/app/i18n/messages/en.json`, `backend/app/i18n/messages/ur.json`
- Test: `tests/test_doctor_dashboard.py` (append)

**Interfaces:**
- Consumes: `consent_service.list_patients_for_doctor`, `build_rows`, `audit.write`.
- Produces: audit action `audit.DOCTOR_DASHBOARD_VIEW = "doctor.dashboard.view"`; template variable `patients: list[PatientRow]` (defined only for verified doctors); i18n keys `doctor_dashboard.patients.title`, `doctor_dashboard.col.name`, `doctor_dashboard.col.age`, `doctor_dashboard.col.gender`, `doctor_dashboard.col.passport`, `doctor_dashboard.empty`.

- [ ] **Step 1: Write the failing page tests**

Append to `tests/test_doctor_dashboard.py` (add the new imports at the top of the file with the others):

```python
# --- add to the import block at the top ---
import json
import re
from datetime import date

from app.db.models import AuditLog
from app.i18n.catalogue import missing_keys, translate
from markupsafe import escape

from tests.conftest import TEST_PASSWORD
```

```python
# ── The page ─────────────────────────────────────────────────────────────
DASH_URL = "/en/doctor"


def in_html(key: str, locale: str = "en", **params) -> str:
    return str(escape(translate(key, locale, **params)))


async def login(client, email: str) -> None:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": TEST_PASSWORD})
    assert r.status_code == 200, r.text


async def dashboard_audit_rows(db) -> list[AuditLog]:
    rows = await db.execute(
        select(AuditLog)
        .where(AuditLog.action == "doctor.dashboard.view")
        .order_by(AuditLog.id)
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


async def test_verified_doctor_sees_consented_patients_with_the_four_fields(client, db, doctor):
    p = await make_patient(
        db, "p1@x.com", "Ayesha Raza", date_of_birth=date(1994, 3, 14), gender="female"
    )
    await grant(db, p, doctor)
    await login(client, "dr@x.com")

    r = await client.get(DASH_URL)

    assert r.status_code == 200
    assert "Ayesha Raza" in r.text
    assert p.passport_no in r.text
    assert "female" in r.text
    assert in_html("doctor_dashboard.patients.title") in r.text
    assert f'href="/en/doctor/patient/{p.id}"' in r.text
    # No passport number in any URL (P4 AC-12).
    assert f"/{p.passport_no}" not in r.text


async def test_ungranted_revoked_and_expired_patients_are_absent(client, db, doctor, other_doctor):
    ok = await make_patient(db, "ok@x.com", "Visible Vera")
    gone = await make_patient(db, "gone@x.com", "Revoked Rana")
    old = await make_patient(db, "old@x.com", "Expired Esa")
    foreign = await make_patient(db, "for@x.com", "Foreign Fiza")
    await make_patient(db, "none@x.com", "Ungranted Uzma")
    await grant(db, ok, doctor)
    await grant(db, gone, doctor, revoked=True)
    await grant(db, old, doctor, expires_in=timedelta(days=-1))
    await grant(db, foreign, other_doctor)
    await login(client, "dr@x.com")

    html = (await client.get(DASH_URL)).text

    assert "Visible Vera" in html
    for name in ("Revoked Rana", "Expired Esa", "Foreign Fiza", "Ungranted Uzma"):
        assert name not in html


async def test_revoking_removes_the_patient_on_the_next_load(client, db, doctor):
    p = await make_patient(db, "p1@x.com", "Soon Gone")
    g = await grant(db, p, doctor)
    await login(client, "dr@x.com")
    assert "Soon Gone" in (await client.get(DASH_URL)).text

    g.revoked_at = utcnow()
    await db.commit()

    assert "Soon Gone" not in (await client.get(DASH_URL)).text


async def test_empty_state_when_nobody_has_granted_access(client, db, doctor):
    await login(client, "dr@x.com")
    r = await client.get(DASH_URL)
    assert r.status_code == 200
    assert in_html("doctor_dashboard.empty") in r.text
    assert "<table" not in r.text


async def test_missing_dob_and_gender_render_dashes(client, db, doctor):
    p = await make_patient(db, "p1@x.com", "No Details")
    await grant(db, p, doctor)
    await login(client, "dr@x.com")
    html = (await client.get(DASH_URL)).text
    assert html.count("—") >= 2


async def test_rows_are_ordered_by_name(client, db, doctor):
    for i, name in enumerate(["Zainab Z", "Ali A", "Bilal B"]):
        await grant(db, await make_patient(db, f"p{i}@x.com", name), doctor)
    await login(client, "dr@x.com")
    html = (await client.get(DASH_URL)).text
    assert html.index("Ali A") < html.index("Bilal B") < html.index("Zainab Z")


async def test_patient_names_are_escaped(client, db, doctor):
    p = await make_patient(db, "p1@x.com", "<script>alert(1)</script>")
    await grant(db, p, doctor)
    await login(client, "dr@x.com")
    html = (await client.get(DASH_URL)).text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


async def test_unverified_doctor_sees_the_banner_and_no_list(client, db, clinic):
    await make_user(db, email="new@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    await login(client, "new@x.com")
    r = await client.get(DASH_URL)
    assert r.status_code == 200
    assert in_html("doctor.pending_title") in r.text
    assert in_html("doctor_dashboard.patients.title") not in r.text
    assert "<table" not in r.text


async def test_verified_doctor_without_a_clinic_still_sees_the_list(client, db, admin):
    # Review focus 4 — the list must not depend on a clinic affiliation.
    user = await make_user(
        db, email="solo@x.com", role=UserRole.DOCTOR, is_verified=True, verifier_id=admin.id
    )
    solo = await doctor_of(db, user)
    await grant(db, await make_patient(db, "p1@x.com", "Solo Patient"), solo)
    await login(client, "solo@x.com")
    assert "Solo Patient" in (await client.get(DASH_URL)).text


async def test_doctor_unverified_after_login_loses_the_list(client, db, doctor):
    # Review focus 5 — status is read from the database on every request.
    await grant(db, await make_patient(db, "p1@x.com", "Was Visible"), doctor)
    await login(client, "dr@x.com")
    assert "Was Visible" in (await client.get(DASH_URL)).text

    from app.db.models import VerificationStatus

    row = await doctor_of(db, await db.get(Profile, doctor.user_id))
    row.verification_status = VerificationStatus.PENDING
    row.verified_by = None
    row.verified_at = None
    await db.commit()

    html = (await client.get(DASH_URL)).text
    assert "Was Visible" not in html


async def test_patient_and_logged_out_are_redirected(client, db):
    r = await client.get(DASH_URL)
    assert r.status_code == 303 and r.headers["location"].startswith("/en/login")

    await make_user(db, email="pat@x.com", role=UserRole.PATIENT)
    await login(client, "pat@x.com")
    r = await client.get(DASH_URL)
    assert r.status_code == 303 and r.headers["location"] == "/en/patient"


async def test_no_clinical_data_on_the_list(client, db, doctor):
    p = await make_patient(db, "p1@x.com", "Clinical Cara")
    await grant(db, p, doctor)
    await login(client, "dr@x.com")
    html = (await client.get(DASH_URL)).text.lower()
    for word in ("allerg", "chronic", "medication", "risk"):
        assert word not in html


async def test_one_audit_row_per_load_with_a_count_and_no_names(client, db, doctor):
    await grant(db, await make_patient(db, "p1@x.com", "Secret Name"), doctor)
    await grant(db, await make_patient(db, "p2@x.com", "Other Name"), doctor)
    await login(client, "dr@x.com")

    await client.get(DASH_URL)
    await client.get(DASH_URL)

    rows = await dashboard_audit_rows(db)
    assert len(rows) == 2
    assert rows[0].actor_user_id == doctor.user_id
    assert rows[0].actor_role == "doctor"
    detail = json.loads(rows[0].detail)
    assert detail == {"patient_count": 2}
    assert "Secret Name" not in rows[0].detail


async def test_urdu_page_renders_and_passport_stays_left_to_right(client, db, doctor):
    p = await make_patient(db, "p1@x.com", "اسد علی")
    await grant(db, p, doctor)
    await login(client, "dr@x.com")
    html = (await client.get("/ur/doctor")).text
    assert in_html("doctor_dashboard.patients.title", "ur") in html
    assert "اسد علی" in html
    assert re.search(rf'dir="ltr"[^>]*>\s*{re.escape(p.passport_no)}', html)


def test_catalogue_has_the_new_keys_in_both_languages():
    assert missing_keys() == {"ur": []}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_doctor_dashboard.py -q`
Expected: the 8 service tests pass; the page tests fail (list not rendered, `doctor.dashboard.view` rows missing, translation keys missing).

- [ ] **Step 3: Add the audit constant**

In `backend/app/audit/writer.py`, directly after `RECORD_READ = "record.read"`:

```python

# Emitted by the doctor dashboard (D4). `detail` holds a patient count only.
DOCTOR_DASHBOARD_VIEW = "doctor.dashboard.view"
```

- [ ] **Step 4: Add the i18n keys**

In `backend/app/i18n/messages/en.json`, replace the last entry
`"doctor_patient.back": "Find another patient"` with:

```json
  "doctor_patient.back": "Find another patient",
  "doctor_dashboard.patients.title": "My patients",
  "doctor_dashboard.col.name": "Name",
  "doctor_dashboard.col.age": "Age",
  "doctor_dashboard.col.gender": "Gender",
  "doctor_dashboard.col.passport": "Passport number",
  "doctor_dashboard.empty": "No patients have shared their record with you yet."
```

In `backend/app/i18n/messages/ur.json`, replace the last entry
`"doctor_patient.back": "کوئی اور مریض تلاش کریں"` with:

```json
  "doctor_patient.back": "کوئی اور مریض تلاش کریں",
  "doctor_dashboard.patients.title": "میرے مریض",
  "doctor_dashboard.col.name": "نام",
  "doctor_dashboard.col.age": "عمر",
  "doctor_dashboard.col.gender": "جنس",
  "doctor_dashboard.col.passport": "پاسپورٹ نمبر",
  "doctor_dashboard.empty": "ابھی تک کسی مریض نے اپنا ریکارڈ آپ کے ساتھ شیئر نہیں کیا۔"
```

If either file has more entries after `doctor_patient.back`, put the new keys after the final entry instead (add the comma there). Verify both files still parse:

Run: `uv run python -c "import json;[json.load(open(f'backend/app/i18n/messages/{l}.json',encoding='utf8')) for l in ('en','ur')];print('ok')"`
Expected: `ok`

- [ ] **Step 5: Create the partial**

Create `frontend/templates/doctor/_patient_list.html`:

```html
{#- D4 v1 — the doctor's consented patients.
    Context: patients  list[PatientRow] (name, age, gender, passport_no, href)
    Names are autoescaped; never use |safe here. No clinical data belongs on
    this list. The passport number is forced left-to-right on the Urdu page. -#}
<div class="panel" style="margin-top:var(--sp-5)">
  <div class="panel__label">{{ t('doctor_dashboard.patients.title') }}</div>
  {% if patients %}
    <table class="patient-table">
      <thead>
        <tr>
          <th scope="col">{{ t('doctor_dashboard.col.name') }}</th>
          <th scope="col">{{ t('doctor_dashboard.col.age') }}</th>
          <th scope="col">{{ t('doctor_dashboard.col.gender') }}</th>
          <th scope="col">{{ t('doctor_dashboard.col.passport') }}</th>
        </tr>
      </thead>
      <tbody>
        {% for row in patients %}
          <tr>
            <td><a href="{{ row.href }}">{{ row.name }}</a></td>
            <td>{{ row.age }}</td>
            <td>{{ row.gender }}</td>
            <td dir="ltr" class="mono">{{ row.passport_no }}</td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
  {% else %}
    <p style="margin:0;font-size:13.5px;color:var(--muted)">{{ t('doctor_dashboard.empty') }}</p>
  {% endif %}
</div>
```

- [ ] **Step 6: Include it from `shell.html`**

In `frontend/templates/shell.html`, directly after the closing `</div>` of the first `<div class="panel">` (the line before `</main>`), add:

```html
    {% if patients is defined %}
      {% include "doctor/_patient_list.html" %}
    {% endif %}
```

- [ ] **Step 7: Add the table styles**

Append to the end of `frontend/static/css/app.css`:

```css

/* D4 — doctor's patient list. */
.patient-table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
.patient-table th,
.patient-table td { text-align: start; padding: var(--sp-3) var(--sp-4); border-block-end: 1px solid var(--border, #e3e3e3); }
.patient-table th { font-size: 11.5px; font-weight: 600; color: var(--muted); }
.patient-table tbody tr:last-child td { border-block-end: 0; }
```

- [ ] **Step 8: Wire the route**

In `backend/app/web/router.py`:

1. Add to the imports (next to the existing `from ..audit import writer as audit` if present, otherwise add it; and next to `consent_service`):

```python
from datetime import date

from ..audit import writer as audit
from ..doctor.rows import build_rows
```

(Skip any line that already exists. `consent_service` is already imported by the lookup page.)

2. In `_guarded`, replace the `elif actor.is_verified_doctor:` block and the code through the `return _render(...)` so it reads:

```python
    patients = None
    if is_patient:
        nav_items = _patient_nav(loc, current="dashboard")
        links = [
            {"href": f"/{loc}/patient/profile", "label": translate("profile.open_link", loc)},
            {"href": f"/{loc}/patient/passport", "label": translate("passport.open_link", loc)},
            {"href": f"/{loc}/patient/access", "label": translate("passport.manage_access", loc)},
        ]
    elif actor.is_verified_doctor:
        # FR3 — an unverified doctor gets no patient-data affordance at all.
        nav_items = _doctor_nav(loc, current="dashboard")
        links = [
            {"href": f"/{loc}/doctor/passport-lookup", "label": translate("lookup.open_link", loc)}
        ]
        # D4 — who is listed is decided in the consent service, live, on every load.
        granted = await consent_service.list_patients_for_doctor(session, actor)
        patients = build_rows(granted, locale=loc, today=date.today())
        await audit.write(
            session,
            action=audit.DOCTOR_DASHBOARD_VIEW,
            actor_user_id=actor.user_id,
            actor_role=actor.role,
            resource_type="patient_list",
            ip_address=actor.ip_address,
            user_agent=request.headers.get("user-agent"),
            detail={"patient_count": len(patients)},
        )
        await session.commit()
    else:
        nav_items, links = [], []

    extra: dict[str, Any] = {}
    if patients is not None:
        extra["patients"] = patients
    return _render(
        request,
        "shell.html",
        loc,
        nav_items=nav_items,
        links=links,
        crumb=translate(ROLE_LABEL[actor.role], loc),
        page_title=translate(title_key, loc),
        full_name=me.full_name,
        initials=_initials(me.full_name),
        role_label=translate(ROLE_LABEL[actor.role], loc),
        passport_no=me.passport_no,
        show_unverified_banner=show_banner,
        pending_message=pending_message,
        **extra,
    )
```

The `patients` variable is only passed for verified doctors, so `shell.html`'s `{% if patients is defined %}` keeps patient and admin dashboards unchanged. (`list_patients_for_doctor` cannot raise `NotFound` here: `actor.is_verified_doctor` was just read from the same database. If a race ever makes it raise, the global handler returns the standard error envelope, which is acceptable.)

- [ ] **Step 9: Run the tests to verify they pass**

Run: `uv run pytest tests/test_doctor_dashboard.py -q`
Expected: all pass (8 service + 15 page tests).

If `test_doctor_unverified_after_login_loses_the_list` fails because `_load_actor` was cached, look at `deps.py::_load_actor` before changing anything: the spec requires the status to come from the database on every request.

- [ ] **Step 10: Run the full suite, lint, commit**

```bash
uv run pytest -q
uv run ruff check backend tests && uv run ruff format backend tests
git add backend frontend tests
git commit -m "feat(D4): show a verified doctor's consented patients on the dashboard"
```

Expected: the whole suite is green (155 existing tests plus this plan's tests, plus Osama's 50 already counted on the P4 branch).

---

### Task 4: Quality gate and hand-off

**Files:** none changed unless a check fails.

- [ ] **Step 1: Manual check in the browser**

Start the app (`uv run backend/app/main.py`, needs `.env`). In the shared Supabase project this needs a verified doctor, plus two patients with only one consent grant to that doctor. Do not create accounts in the shared project without asking the team. Check the English page, `/ur/doctor`, and the revoke flow (patient revokes, doctor reloads, the row is gone). If real credentials are not available, say so; the automated tests are then the only proof.

- [ ] **Step 2: Review the diff against the spec**

Run: `git diff origin/feature/P4-medical-passport...HEAD --stat`
Expected: only the files listed in File Structure, plus the two docs. Anything else is out of scope.

- [ ] **Step 3: Security and code review**

Run the `security-review` skill on the branch, then `/code-review`. Fix real findings, add a test for each fix.

- [ ] **Step 4: Rebase check**

Run: `git fetch origin && git log --oneline origin/dev -1 && git merge-base --is-ancestor origin/feature/P4-medical-passport origin/dev && echo merged`
If it prints `merged`, rebase onto `origin/dev`: `git rebase --onto origin/dev origin/feature/P4-medical-passport` and rerun `uv run pytest -q`. If not merged yet, stop here.

- [ ] **Step 5: Ask before publishing**

Do not push. Tell the user the branch is ready and ask whether to push and open a PR into `dev` (the PR body should name Osama's `consent/service.py` addition so he can review it).
