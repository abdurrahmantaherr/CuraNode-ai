"""D-VERIFY — clinic-admin doctor verification: the service layer.

Visibility and authority are the whole feature, so most tests are about who
must NOT see or act on a doctor.
"""

from __future__ import annotations

import pytest
from app.db.models import (
    Clinic,
    Doctor,
    DoctorAffiliation,
    DoctorVerification,
    UserRole,
    VerificationStatus,
)
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
            db,
            actor,
            pending_doctor.id,
            checked_number=True,
            checked_name=True,
            checked_licence=True,
            note=None,
        )


async def test_unknown_tab_is_validation_failed(db, admin):
    with pytest.raises(ValidationFailed):
        await verification_service.list_doctors_for_clinic(db, admin, "bogus")


# ── approve ───────────────────────────────────────────────────────────────
async def test_approve_requires_all_three_ticks(db, admin, pending_doctor):
    # Review focus 4 — a missing tick is refused server-side.
    with pytest.raises(ValidationFailed):
        await verification_service.approve(
            db,
            admin,
            pending_doctor.id,
            checked_number=True,
            checked_name=True,
            checked_licence=False,
            note=None,
        )


async def test_approve_sets_verified_and_writes_history(db, admin, pending_doctor):
    await verification_service.approve(
        db,
        admin,
        pending_doctor.id,
        checked_number=True,
        checked_name=True,
        checked_licence=True,
        note="looks right",
    )
    row = (
        await db.execute(
            select(Doctor)
            .where(Doctor.id == pending_doctor.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.verification_status == VerificationStatus.VERIFIED
    assert row.verified_by == admin.user_id
    assert row.verified_at is not None

    history = (
        await db.execute(
            select(DoctorVerification).where(DoctorVerification.doctor_id == pending_doctor.id)
        )
    ).scalar_one()
    assert history.decision.value == "approve"
    assert history.note == "looks right"


# ── reject ────────────────────────────────────────────────────────────────
async def test_reject_sets_rejected_with_reason(db, admin, pending_doctor):
    await verification_service.reject(
        db, admin, pending_doctor.id, reason="name_mismatch", note=None
    )
    row = (
        await db.execute(
            select(Doctor)
            .where(Doctor.id == pending_doctor.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.verification_status == VerificationStatus.REJECTED
    history = (
        await db.execute(
            select(DoctorVerification).where(DoctorVerification.doctor_id == pending_doctor.id)
        )
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
        await db.execute(
            select(Doctor)
            .where(Doctor.id == pending_doctor.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.verification_status == VerificationStatus.REJECTED


async def test_unknown_reason_is_refused(db, admin, pending_doctor):
    with pytest.raises(ValidationFailed):
        await verification_service.reject(db, admin, pending_doctor.id, reason="bogus", note=None)


async def test_rejected_then_approved_moves_to_verified_and_keeps_history(
    db, admin, pending_doctor
):
    await verification_service.reject(
        db, admin, pending_doctor.id, reason="name_mismatch", note=None
    )
    await verification_service.approve(
        db,
        admin,
        pending_doctor.id,
        checked_number=True,
        checked_name=True,
        checked_licence=True,
        note=None,
    )
    row = (
        await db.execute(
            select(Doctor)
            .where(Doctor.id == pending_doctor.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.verification_status == VerificationStatus.VERIFIED
    history = (
        (
            await db.execute(
                select(DoctorVerification)
                .where(DoctorVerification.doctor_id == pending_doctor.id)
                .order_by(DoctorVerification.decided_at)
            )
        )
        .scalars()
        .all()
    )
    assert [h.decision.value for h in history] == ["reject", "approve"]


# ── revoke ────────────────────────────────────────────────────────────────
async def test_revoke_a_verified_doctor_sets_rejected_and_clears_verifier(
    db, admin, pending_doctor
):
    await verification_service.approve(
        db,
        admin,
        pending_doctor.id,
        checked_number=True,
        checked_name=True,
        checked_licence=True,
        note=None,
    )
    await verification_service.revoke(
        db, admin, pending_doctor.id, reason="licence_invalid", note=None
    )
    row = (
        await db.execute(
            select(Doctor)
            .where(Doctor.id == pending_doctor.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.verification_status == VerificationStatus.REJECTED
    assert row.verified_by is None
    assert row.verified_at is None


# ── audit ─────────────────────────────────────────────────────────────────
async def test_each_decision_writes_one_audit_row_with_no_note_text(db, admin, pending_doctor):
    import json

    from app.db.models import AuditLog

    await verification_service.reject(
        db,
        admin,
        pending_doctor.id,
        reason="other",
        note="this doctor's number is fake, I checked twice",
    )
    rows = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "verification.reject")))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    detail = json.loads(rows[0].detail)
    assert detail == {"doctor_id": str(pending_doctor.id), "reason": "other"}
    assert "fake" not in rows[0].detail


# ── latest_decisions_for / doctor_status_for_banner ──────────────────────
async def test_latest_decisions_for_returns_the_newest_row_per_doctor(db, admin, pending_doctor):
    await verification_service.reject(
        db, admin, pending_doctor.id, reason="name_mismatch", note=None
    )
    await verification_service.approve(
        db,
        admin,
        pending_doctor.id,
        checked_number=True,
        checked_name=True,
        checked_licence=True,
        note=None,
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
    await verification_service.reject(
        db, admin, pending_doctor.id, reason="licence_invalid", note=None
    )
    actor = Actor(user_id=pending_doctor.user_id, role="doctor")
    status, reason = await verification_service.doctor_status_for_banner(db, actor)
    assert status == "rejected"
    assert reason == "licence_invalid"


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
    # The actual distinguishing behaviour: the red "not approved" banner, not
    # the yellow "awaiting verification" one a pending doctor sees.
    assert "Verification not approved" in r.text
    assert "Awaiting verification" not in r.text


async def test_approving_a_rejected_doctor_removes_the_banner(client, db, clinic, admin):
    # Review focus 5 — the stale banner must not survive the approval.
    user = await make_user(db, email="dr@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    doctor = await doctor_of(db, user)
    await verification_service.reject(db, admin, doctor.id, reason="name_mismatch", note=None)
    await login(client, "dr@x.com")
    rejected_html = (await client.get("/en/doctor")).text
    assert "<table" not in rejected_html
    assert "Verification not approved" in rejected_html

    await verification_service.approve(
        db,
        admin,
        doctor.id,
        checked_number=True,
        checked_name=True,
        checked_licence=True,
        note=None,
    )

    html = (await client.get("/en/doctor")).text
    assert "Verification not approved" not in html
    assert "My patients" in html or "<table" in html  # the real dashboard renders again
