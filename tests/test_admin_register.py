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


import pytest
from app.admin import service as admin_service
from app.admin.schemas import AdminRegisterRequest
from app.db.models import ClinicInviteCode, ClinicStaff
from app.errors import ValidationFailed
from app.verification.codes import hash_code
from sqlalchemy import select


async def set_code(db, clinic_id, code: str) -> None:
    # Upsert: clinic_id is the primary key (one row per clinic, Task 1), so
    # rotating a code updates the existing row rather than inserting a second.
    row = await db.get(ClinicInviteCode, clinic_id)
    if row is None:
        db.add(ClinicInviteCode(clinic_id=clinic_id, code_hash=hash_code(code), is_active=True))
    else:
        row.code_hash = hash_code(code)
        row.is_active = True
    await db.commit()


async def test_register_admin_with_the_right_code_creates_a_clinic_admin(db, clinic):
    await set_code(db, clinic.id, "RIGHT-CODE-0001")
    body = AdminRegisterRequest(
        full_name="Front Desk Lead",
        email="fd@x.com",
        password="CorrectHorseBattery1",
        clinic_id=clinic.id,
        invite_code="RIGHT-CODE-0001",
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
        full_name="Someone",
        email="fd2@x.com",
        password="CorrectHorseBattery1",
        clinic_id=clinic.id,
        invite_code="WRONG-CODE-0001",
    )
    with pytest.raises(ValidationFailed):
        await admin_service.register_admin(db, body)


async def test_register_admin_with_no_code_set_for_the_clinic_is_refused(db, clinic):
    body = AdminRegisterRequest(
        full_name="Someone",
        email="fd3@x.com",
        password="CorrectHorseBattery1",
        clinic_id=clinic.id,
        invite_code="ANYTHING-0001",
    )
    with pytest.raises(ValidationFailed):
        await admin_service.register_admin(db, body)


async def test_rotated_code_stops_the_old_one_working(db, clinic):
    # Review focus 3 — rotation must actually invalidate the old code.
    await set_code(db, clinic.id, "OLD-CODE-0001")
    await set_code(db, clinic.id, "NEW-CODE-0001")  # same clinic_id — updates the one row

    body_old = AdminRegisterRequest(
        full_name="Someone",
        email="old@x.com",
        password="CorrectHorseBattery1",
        clinic_id=clinic.id,
        invite_code="OLD-CODE-0001",
    )
    with pytest.raises(ValidationFailed):
        await admin_service.register_admin(db, body_old)

    body_new = AdminRegisterRequest(
        full_name="Someone",
        email="new@x.com",
        password="CorrectHorseBattery1",
        clinic_id=clinic.id,
        invite_code="NEW-CODE-0001",
    )
    _out, pair = await admin_service.register_admin(db, body_new)
    assert pair is not None


async def test_duplicate_email_returns_no_cookies_and_creates_nothing(db, clinic):
    await set_code(db, clinic.id, "RIGHT-CODE-0001")
    from tests.conftest import make_user

    existing = await make_user(
        db, email="taken@x.com", role=UserRole.CLINIC_ADMIN, clinic_id=clinic.id
    )
    body = AdminRegisterRequest(
        full_name="Impostor",
        email="taken@x.com",
        password="CorrectHorseBattery1",
        clinic_id=clinic.id,
        invite_code="RIGHT-CODE-0001",
    )
    out, pair = await admin_service.register_admin(db, body)
    assert pair is None
    assert out.user_id != existing.id  # a fresh, throwaway id — never leaks the real one

    count = (
        (await db.execute(select(ClinicStaff).where(ClinicStaff.user_id != existing.id)))
        .scalars()
        .all()
    )
    assert count == []  # nothing new was created


async def test_admin_register_page_renders(client, db, clinic):
    r = await client.get("/en/admin/register")
    assert r.status_code == 200
    assert in_html("admin.register.title") in r.text
    assert str(clinic.id) in r.text  # clinic appears in the dropdown


async def test_admin_register_submit_with_right_code_signs_in(client, db, clinic):
    from app.db.models import ClinicInviteCode
    from app.verification.codes import hash_code

    db.add(
        ClinicInviteCode(
            clinic_id=clinic.id, code_hash=hash_code("SIGNUP-CODE-0001"), is_active=True
        )
    )
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


def test_catalogue_has_every_new_key_in_both_languages():
    assert missing_keys() == {"ur": []}


async def test_wrong_code_gives_the_same_refusal_whether_or_not_the_email_exists(db, clinic):
    # Important finding 1 — checking email before the invite code lets anyone
    # learn whether an email has an account, for free (no valid code needed).
    await set_code(db, clinic.id, "RIGHT-CODE-0001")
    existing = await make_user(
        db, email="taken@x.com", role=UserRole.CLINIC_ADMIN, clinic_id=clinic.id
    )

    body_existing = AdminRegisterRequest(
        full_name="Someone",
        email="taken@x.com",
        password="CorrectHorseBattery1",
        clinic_id=clinic.id,
        invite_code="WRONG-CODE-0001",
    )
    with pytest.raises(ValidationFailed):
        await admin_service.register_admin(db, body_existing)

    body_new = AdminRegisterRequest(
        full_name="Someone",
        email="never-seen@x.com",
        password="CorrectHorseBattery1",
        clinic_id=clinic.id,
        invite_code="WRONG-CODE-0001",
    )
    with pytest.raises(ValidationFailed):
        await admin_service.register_admin(db, body_new)

    # Neither attempt created a second account for the existing email.
    count = (
        (await db.execute(select(ClinicStaff).where(ClinicStaff.user_id == existing.id)))
        .scalars()
        .all()
    )
    assert len(count) == 1
