"""D7 T4: the multi-clinic fixtures behave as the schedule tests assume."""

from __future__ import annotations

from datetime import date

from app.db import types as dbtypes
from app.db.models import DoctorAffiliation
from sqlalchemy import select

from tests.conftest import add_affiliation, doctor_of, next_weekday


async def test_schedule_doctor_holds_two_active_affiliations(
    db, schedule_doctor, clinic, second_clinic
):
    doctor = await doctor_of(db, schedule_doctor)
    clinics = set(
        (
            await db.execute(
                select(DoctorAffiliation.clinic_id).where(
                    DoctorAffiliation.doctor_id == doctor.id, DoctorAffiliation.status == "active"
                )
            )
        ).scalars()
    )
    assert clinics == {clinic.id, second_clinic.id}


async def test_add_affiliation_can_be_inactive(db, schedule_doctor, inactive_clinic):
    doctor = await doctor_of(db, schedule_doctor)
    aff = await add_affiliation(db, doctor.id, inactive_clinic.id, status="inactive")
    assert (aff.status, inactive_clinic.status) == ("inactive", "inactive")


def test_next_weekday(monkeypatch):
    monkeypatch.setattr(dbtypes, "today_pk", lambda: date(2026, 10, 14))  # a Wednesday
    assert next_weekday(2) == date(2026, 10, 14)  # today counts
    assert next_weekday(0) == date(2026, 10, 19)
    assert next_weekday(6, start=date(2026, 10, 12)) == date(2026, 10, 18)
