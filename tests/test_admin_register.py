"""D-VERIFY — clinic-admin sign-up and sign-in."""

from __future__ import annotations

from app.db.models import UserRole
from app.i18n.catalogue import translate
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
