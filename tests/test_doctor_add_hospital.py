"""An existing doctor links themselves to more hospitals they work at.

Each self-added link is `pending`: the doctor can schedule there at once (D7),
but consent counts only `active` links, so a clinic grant at that hospital does
NOT reach them until the hospital confirms the link.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from app.consent import service as consent_service
from app.consent.schemas import SECTIONS
from app.db import types as dbtypes
from app.db.models import (
    AuditLog,
    Clinic,
    ConsentGrant,
    DoctorAffiliation,
    DoctorSchedule,
    Patient,
    UserRole,
)
from app.db.types import uuid7
from app.deps import Actor
from sqlalchemy import select

from tests.conftest import TEST_PASSWORD, add_affiliation, doctor_of, make_user

PAGE = "/en/doctor/schedule"
ADD = f"{PAGE}/hospitals"
TODAY = date(2026, 10, 12)


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    monkeypatch.setattr(dbtypes, "today_pk", lambda: TODAY)


@pytest.fixture
async def third_clinic(db) -> Clinic:
    c = Clinic(id=uuid7(), name="Lahore Care Hospital", city="Lahore", type="hospital")
    db.add(c)
    await db.commit()
    return c


async def login(client, email: str) -> None:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": TEST_PASSWORD})
    assert r.status_code == 200, r.text


@pytest.fixture
async def doctor_client(client, schedule_doctor):
    await login(client, "schedule.doctor@x.com")
    return client


async def links(db, user) -> dict[uuid.UUID, str]:
    doctor = await doctor_of(db, user)
    rows = await db.execute(
        select(DoctorAffiliation)
        .where(DoctorAffiliation.doctor_id == doctor.id)
        .execution_options(populate_existing=True)
    )
    return {a.clinic_id: a.status for a in rows.scalars()}


async def test_page_lists_hospitals_and_offers_the_others(doctor_client, third_clinic):
    body = (await doctor_client.get(PAGE)).text
    assert "Your hospitals" in body
    assert body.count("Linked</span>") == 2  # schedule_doctor: two active links
    assert f'name="clinic_ids" value="{third_clinic.id}"' in body
    assert "Lahore Care Hospital — Lahore" in body


async def test_adding_a_hospital_creates_a_pending_link_and_allows_scheduling(
    doctor_client, db, schedule_doctor, third_clinic
):
    r = await doctor_client.post(ADD, data={"clinic_ids": [str(third_clinic.id)]})
    assert (r.status_code, r.headers["location"]) == (
        303,
        "/en/doctor/schedule?saved=hospitals_added#hospitals",
    )
    assert (await links(db, schedule_doctor))[third_clinic.id] == "pending"

    body = (await doctor_client.get(PAGE)).text
    assert "Awaiting the hospital&#39;s confirmation" in body
    assert "You can add hours at 3 hospital(s)." in body

    r = await doctor_client.post(
        f"{PAGE}/slots",
        data={
            "clinic_id": str(third_clinic.id),
            "weekdays": ["4"],
            "start_time": "09:00",
            "end_time": "12:00",
            "slot_minutes": "15",
            "max_tokens": "",
        },
    )
    assert r.status_code == 303, r.text
    [slot] = (await db.execute(select(DoctorSchedule))).scalars().all()
    pending = (
        await db.execute(
            select(DoctorAffiliation).where(DoctorAffiliation.clinic_id == third_clinic.id)
        )
    ).scalar_one()
    assert slot.affiliation_id == pending.id


async def test_one_audit_row_with_clinic_ids_only(doctor_client, db, third_clinic):
    await doctor_client.post(ADD, data={"clinic_ids": [str(third_clinic.id)]})
    [row] = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "doctor.affiliation.add")))
        .scalars()
        .all()
    )
    assert row.actor_role == "doctor" and row.subject_patient_id is None
    assert str(third_clinic.id) in row.detail and '"status": "pending"' in row.detail


async def test_a_pending_link_never_reaches_a_clinic_grant(client, db, clinic, third_clinic):
    """The security property: self-linking to a hospital must not expose the
    patients who shared their record with that hospital."""
    admin = await make_user(
        db, email="admin@x.com", role=UserRole.CLINIC_ADMIN, clinic_id=clinic.id
    )
    verified = await make_user(
        db,
        email="verified@x.com",
        role=UserRole.DOCTOR,
        clinic_id=clinic.id,
        is_verified=True,
        verifier_id=admin.id,
    )
    patient_user = await make_user(db, email="pat@x.com", role=UserRole.PATIENT)
    patient = (
        await db.execute(select(Patient).where(Patient.user_id == patient_user.id))
    ).scalar_one()
    db.add(
        ConsentGrant(
            patient_id=patient.id,
            grantee_type="clinic",
            grantee_id=third_clinic.id,
            scope_sections=list(SECTIONS),
        )
    )
    await db.commit()
    actor = Actor(user_id=verified.id, role=UserRole.DOCTOR.value, is_verified_doctor=True)

    await login(client, "verified@x.com")
    r = await client.post(ADD, data={"clinic_ids": [str(third_clinic.id)]})
    assert r.status_code == 303
    assert await consent_service.list_patients_for_doctor(db, actor) == []

    # Once the hospital confirms (status active), the grant applies as usual.
    aff = (
        await db.execute(
            select(DoctorAffiliation).where(DoctorAffiliation.clinic_id == third_clinic.id)
        )
    ).scalar_one()
    aff.status = "active"
    await db.commit()
    assert [p.id for p in await consent_service.list_patients_for_doctor(db, actor)] == [patient.id]


async def test_already_linked_adds_nothing(doctor_client, db, schedule_doctor, clinic):
    before = await links(db, schedule_doctor)
    r = await doctor_client.post(ADD, data={"clinic_ids": [str(clinic.id)]})
    assert r.status_code == 422
    assert "You are already linked to the hospitals you chose." in r.text
    assert await links(db, schedule_doctor) == before


async def test_a_double_submit_creates_one_link(doctor_client, db, schedule_doctor, third_clinic):
    data = {"clinic_ids": [str(third_clinic.id), str(third_clinic.id)]}
    assert (await doctor_client.post(ADD, data=data)).status_code == 303
    assert (await doctor_client.post(ADD, data=data)).status_code == 422
    rows = (
        (
            await db.execute(
                select(DoctorAffiliation).where(DoctorAffiliation.clinic_id == third_clinic.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_an_ended_link_cannot_be_re_added(doctor_client, db, schedule_doctor, third_clinic):
    await add_affiliation(
        db, (await doctor_of(db, schedule_doctor)).id, third_clinic.id, "inactive"
    )
    r = await doctor_client.post(ADD, data={"clinic_ids": [str(third_clinic.id)]})
    assert r.status_code == 422
    assert "Only the hospital can restore it." in r.text
    assert (await links(db, schedule_doctor))[third_clinic.id] == "inactive"


@pytest.mark.parametrize("bad", ["unknown", "malformed", "inactive"])
async def test_unknown_malformed_or_closed_hospitals_are_refused(
    doctor_client, db, schedule_doctor, inactive_clinic, bad
):
    value = {"unknown": str(uuid.uuid4()), "malformed": "nope", "inactive": str(inactive_clinic.id)}
    before = await links(db, schedule_doctor)
    r = await doctor_client.post(ADD, data={"clinic_ids": [value[bad]]})
    assert r.status_code == 422
    assert "We couldn" not in r.text  # a form error, not a page error
    assert await links(db, schedule_doctor) == before


async def test_nothing_ticked_is_refused(doctor_client):
    r = await doctor_client.post(ADD, data={})
    assert r.status_code == 422
    assert "Choose at least one hospital." in r.text


async def test_at_most_ten_links(doctor_client, db, schedule_doctor):
    extra = [Clinic(id=uuid7(), name=f"Clinic {i:02d}", type="clinic") for i in range(9)]
    db.add_all(extra)
    await db.commit()
    r = await doctor_client.post(ADD, data={"clinic_ids": [str(c.id) for c in extra]})
    assert r.status_code == 422  # 2 linked + 9 new = 11
    assert "Choose at most 10 hospitals." in r.text


@pytest.mark.parametrize("role", [UserRole.PATIENT, UserRole.CLINIC_ADMIN])
async def test_other_roles_cannot_add_links(client, db, clinic, third_clinic, role):
    await make_user(db, email="other@x.com", role=role, clinic_id=clinic.id)
    await login(client, "other@x.com")
    r = await client.post(ADD, data={"clinic_ids": [str(third_clinic.id)]})
    assert r.status_code == 303
    assert (await db.execute(select(DoctorAffiliation))).scalars().all() == []
