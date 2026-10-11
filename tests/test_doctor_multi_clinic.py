"""A doctor picks every hospital/clinic they work at when creating their profile.

Each pick becomes one active `doctor_affiliation`, so the D7 schedule can span
all of them. The doctor still lands pending (FR3): choosing clinics grants no
verification and no patient access.
"""

from __future__ import annotations

import uuid

from app.db.models import Doctor, DoctorAffiliation, Profile, VerificationStatus
from app.i18n.catalogue import translate
from sqlalchemy import select

from tests.conftest import TEST_PASSWORD, make_user
from tests.test_oauth import _complete, _enable_oauth


def _form(email: str, **extra) -> dict:
    return {
        "full_name": "Multi Clinic",
        "email": email,
        "password": TEST_PASSWORD,
        "password_confirm": TEST_PASSWORD,
        "role": "doctor",
        "pmdc_number": f"MC-{uuid.uuid4().hex[:6]}",
        "specialty": "Cardiology",
        "consent": "on",
        **extra,
    }


async def _affiliated_clinics(db, email: str) -> tuple[Doctor, set[uuid.UUID]]:
    user = (await db.execute(select(Profile).where(Profile.email == email))).scalar_one()
    doctor = (await db.execute(select(Doctor).where(Doctor.user_id == user.id))).scalar_one()
    rows = await db.execute(
        select(DoctorAffiliation.clinic_id).where(
            DoctorAffiliation.doctor_id == doctor.id, DoctorAffiliation.status == "active"
        )
    )
    return doctor, set(rows.scalars().all())


async def test_register_page_lists_every_clinic_as_a_checkbox(client, clinic, second_clinic):
    body = (await client.get("/en/register")).text
    assert body.count('name="clinic_ids"') == 2
    assert clinic.name in body and second_clinic.name in body
    assert translate("field.clinics", "en") in body


async def test_web_register_with_two_hospitals_creates_two_affiliations(
    client, db, clinic, second_clinic
):
    r = await client.post(
        "/en/register",
        data=_form("two@x.com", clinic_ids=[str(clinic.id), str(second_clinic.id)]),
    )
    assert (r.status_code, r.headers["location"]) == (303, "/en/doctor")
    doctor, clinics = await _affiliated_clinics(db, "two@x.com")
    assert clinics == {clinic.id, second_clinic.id}
    assert doctor.verification_status == VerificationStatus.PENDING  # FR3 unchanged


async def test_the_schedule_page_then_offers_both_hospitals(client, db, clinic, second_clinic):
    await client.post(
        "/en/register",
        data=_form("sched@x.com", clinic_ids=[str(clinic.id), str(second_clinic.id)]),
    )
    body = (await client.get("/en/doctor/schedule")).text
    assert "You can add hours at 2 hospital(s)." in body
    assert clinic.name in body and second_clinic.name in body


async def test_web_register_needs_at_least_one_hospital(client, db):
    r = await client.post("/en/register", data=_form("none@x.com"))
    assert r.status_code == 422
    assert translate("errors.doctor_fields_required", "en") in r.text
    assert (
        await db.execute(select(Profile).where(Profile.email == "none@x.com"))
    ).scalar_one_or_none() is None


async def test_web_register_refuses_an_unknown_or_malformed_clinic(client, db, clinic):
    for bad in (str(uuid.uuid4()), "not-a-uuid"):
        r = await client.post(
            "/en/register", data=_form(f"bad{bad[:4]}@x.com", clinic_ids=[str(clinic.id), bad])
        )
        assert r.status_code == 422
        assert translate("errors.clinic_unknown", "en") in r.text
        # The good clinic stays ticked on the re-rendered form.
        assert f'value="{clinic.id}"\n                         checked' in r.text


async def test_web_register_ticked_clinics_survive_a_failed_submit(client, clinic, second_clinic):
    r = await client.post(
        "/en/register",
        data=_form(
            "short@x.com",
            password="short",
            password_confirm="short",
            clinic_ids=[str(second_clinic.id)],
        ),
    )
    assert r.status_code == 422
    assert f'value="{second_clinic.id}"\n                         checked' in r.text
    assert f'value="{clinic.id}"\n                         checked' not in r.text


async def test_api_register_accepts_clinic_ids_and_dedupes_primary(
    client, db, clinic, second_clinic
):
    payload = {
        "email": "api@x.com",
        "password": TEST_PASSWORD,
        "full_name": "Api Doctor",
        "role": "doctor",
        "pmdc_number": "API-1",
        "specialty": "Cardiology",
        "primary_clinic_id": str(clinic.id),
        "clinic_ids": [str(clinic.id), str(second_clinic.id)],  # primary repeated
    }
    r = await client.post("/api/v1/auth/register", json=payload)
    assert r.status_code == 201, r.text
    _, clinics = await _affiliated_clinics(db, "api@x.com")
    assert clinics == {clinic.id, second_clinic.id}


async def test_api_register_still_accepts_only_primary_clinic_id(client, db, clinic):
    payload = {
        "email": "legacy@x.com",
        "password": TEST_PASSWORD,
        "full_name": "Legacy Doctor",
        "role": "doctor",
        "pmdc_number": "API-2",
        "specialty": "Cardiology",
        "primary_clinic_id": str(clinic.id),
    }
    assert (await client.post("/api/v1/auth/register", json=payload)).status_code == 201
    _, clinics = await _affiliated_clinics(db, "legacy@x.com")
    assert clinics == {clinic.id}


async def test_api_register_rejects_no_clinic_and_too_many(client, clinic):
    base = {
        "password": TEST_PASSWORD,
        "full_name": "Api Doctor",
        "role": "doctor",
        "pmdc_number": "API-3",
        "specialty": "Cardiology",
    }
    r = await client.post("/api/v1/auth/register", json={**base, "email": "n@x.com"})
    assert r.status_code == 422
    eleven = [str(uuid.uuid4()) for _ in range(11)]
    r = await client.post(
        "/api/v1/auth/register", json={**base, "email": "m@x.com", "clinic_ids": eleven}
    )
    assert r.status_code == 422


async def test_oauth_onboarding_with_two_hospitals(
    client, db, clinic, second_clinic, fake_supabase, monkeypatch
):
    _enable_oauth(monkeypatch)
    await _complete(client, fake_supabase, "onboard-two@example.com")
    r = await client.post(
        "/en/onboarding",
        data={
            "full_name": "Onboard Two",
            "role": "doctor",
            "pmdc_number": "OAUTH-TWO",
            "specialty": "Cardiology",
            "clinic_ids": [str(clinic.id), str(second_clinic.id)],
            "consent": "on",
        },
    )
    assert (r.status_code, r.headers["location"]) == (303, "/en/doctor")
    _, clinics = await _affiliated_clinics(db, "onboard-two@example.com")
    assert clinics == {clinic.id, second_clinic.id}


async def test_a_patient_registration_ignores_ticked_clinics(client, db, clinic):
    data = _form("pat@x.com", role="patient", clinic_ids=[str(clinic.id)])
    r = await client.post("/en/register", data=data)
    assert r.status_code == 303
    user = (await db.execute(select(Profile).where(Profile.email == "pat@x.com"))).scalar_one()
    assert (
        await db.execute(select(Doctor).where(Doctor.user_id == user.id))
    ).scalar_one_or_none() is None


async def test_existing_single_clinic_doctors_are_unaffected(client, db, clinic):
    await make_user(db, email="old@x.com", role="doctor", clinic_id=clinic.id)
    _, clinics = await _affiliated_clinics(db, "old@x.com")
    assert clinics == {clinic.id}
