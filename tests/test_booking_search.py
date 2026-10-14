"""P5: who a patient can find, and the free times they can pick."""

from __future__ import annotations

from datetime import date, time

import pytest
from app.booking import service
from app.db import types as dbtypes
from app.db.models import ScheduleException, ScheduleExceptionType, UserRole, VerificationStatus
from app.errors import NotFound

from tests.booking_helpers import (
    MONDAY,
    THURSDAY,
    TODAY,
    WED,
    add_appointment,
    add_session,
    at,
    freeze,
    make_patient,
    verified_doctor,
)
from tests.conftest import add_affiliation, doctor_of, make_user


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    freeze(monkeypatch)


# ── Search ───────────────────────────────────────────────────────────────
async def test_a_verified_doctor_is_found_with_their_live_hospitals(db, clinic, second_clinic):
    doctor = await verified_doctor(db, clinic)
    await add_affiliation(db, doctor.id, second_clinic.id)
    cards = await service.search_doctors(db)
    assert [c.name for c in cards] == ["Dr Booking"]
    assert {cl.name for cl in cards[0].clinics} == {"Shifa International", "Maroof International"}


async def test_a_pending_doctor_is_not_found(db, clinic):
    await make_user(db, email="pending@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    assert await service.search_doctors(db) == []


async def test_a_rejected_doctor_is_not_found(db, clinic):
    doctor = await verified_doctor(db, clinic)
    doctor.verification_status = VerificationStatus.REJECTED
    doctor.verified_by = None
    doctor.verified_at = None
    await db.commit()
    assert await service.search_doctors(db) == []


async def test_a_doctor_with_no_live_hospital_is_not_found(db, inactive_clinic):
    await verified_doctor(db, inactive_clinic)
    assert await service.search_doctors(db) == []


async def test_an_inactive_affiliation_hides_only_that_hospital(db, clinic, second_clinic):
    doctor = await verified_doctor(db, clinic)
    await add_affiliation(db, doctor.id, second_clinic.id, status="inactive")
    cards = await service.search_doctors(db)
    assert [cl.name for cl in cards[0].clinics] == ["Shifa International"]


async def test_search_matches_the_doctor_name_ignoring_case(db, clinic):
    await verified_doctor(db, clinic)
    assert len(await service.search_doctors(db, query="BOOK")) == 1
    assert await service.search_doctors(db, query="zzz") == []


async def test_search_matches_a_hospital_name(db, clinic):
    await verified_doctor(db, clinic)
    assert len(await service.search_doctors(db, query="shifa")) == 1


async def test_the_specialty_filter_is_exact(db, clinic):
    await verified_doctor(db, clinic, email="a.dr@x.com", name="Dr A", specialty="Cardiology")
    await verified_doctor(db, clinic, email="b.dr@x.com", name="Dr B", specialty="Dermatology")
    cards = await service.search_doctors(db, specialty="Dermatology")
    assert [c.name for c in cards] == ["Dr B"]


async def test_specialty_options_are_distinct_and_sorted(db, clinic):
    await verified_doctor(db, clinic, email="a.dr@x.com", name="Dr A", specialty="Dermatology")
    await verified_doctor(db, clinic, email="b.dr@x.com", name="Dr B", specialty="Cardiology")
    await verified_doctor(db, clinic, email="c.dr@x.com", name="Dr C", specialty="Cardiology")
    assert await service.specialty_options(db) == ["Cardiology", "Dermatology"]


async def test_get_doctor_returns_a_visible_doctor_and_hides_the_rest(db, clinic):
    doctor = await verified_doctor(db, clinic)
    assert (await service.get_doctor(db, doctor.id)).name == "Dr Booking"
    pending = await make_user(db, email="p@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    with pytest.raises(NotFound):
        await service.get_doctor(db, (await doctor_of(db, pending)).id)


# ── The free times ───────────────────────────────────────────────────────
async def test_options_list_the_free_times_for_each_session_day(db, clinic):
    doctor = await verified_doctor(db, clinic)
    await add_session(db, doctor, clinic)  # Wednesdays 09:00-13:00
    days = await service.list_options(db, doctor.id, clinic.id)
    # Today is Monday the 12th, so the horizon ends on the 26th: Wednesday the
    # 28th is too far.
    assert [d.day for d in days] == [WED, date(2026, 10, 21)]
    assert len(days[0].times) == 16
    assert days[0].times[0] == at(WED, "09:00") and days[0].times[-1] == at(WED, "12:45")


async def test_taken_times_are_not_offered(db, clinic):
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic)
    _, patient, _ = await make_patient(db)
    await add_appointment(db, patient, doctor, clinic, session, at(WED, "09:00"))
    days = await service.list_options(db, doctor.id, clinic.id)
    assert len(days[0].times) == 15 and at(WED, "09:00") not in days[0].times


async def test_a_cancelled_booking_does_not_block_its_time(db, clinic):
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic)
    _, patient, _ = await make_patient(db)
    await add_appointment(
        db, patient, doctor, clinic, session, at(WED, "09:00"), status="cancelled"
    )
    assert len((await service.list_options(db, doctor.id, clinic.id))[0].times) == 16


async def test_a_full_session_drops_that_day(db, clinic):
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic, cap=1)
    _, patient, _ = await make_patient(db)
    await add_appointment(db, patient, doctor, clinic, session, at(WED, "09:00"))
    days = await service.list_options(db, doctor.id, clinic.id)
    assert [d.day for d in days] == [date(2026, 10, 21)]


async def test_time_off_hides_the_whole_day(db, clinic):
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic)
    db.add(
        ScheduleException(
            schedule_id=session.id, exception_date=WED, kind=ScheduleExceptionType.LEAVE
        )
    )
    await db.commit()
    assert [d.day for d in await service.list_options(db, doctor.id, clinic.id)] == [
        date(2026, 10, 21)
    ]


async def test_partial_time_off_hides_only_those_times(db, clinic):
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic)
    db.add(
        ScheduleException(
            schedule_id=session.id,
            exception_date=WED,
            kind=ScheduleExceptionType.LEAVE,
            start_time=time(9, 0),
            end_time=time(11, 0),
        )
    )
    await db.commit()
    first_day = (await service.list_options(db, doctor.id, clinic.id))[0]
    assert first_day.times[0] == at(WED, "11:00") and len(first_day.times) == 8


async def test_extra_hours_add_a_session(db, clinic):
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic)
    friday = date(2026, 10, 16)
    db.add(
        ScheduleException(
            schedule_id=session.id,
            exception_date=friday,
            kind=ScheduleExceptionType.EXTRA,
            start_time=time(16, 0),
            end_time=time(17, 0),
            max_tokens=2,
        )
    )
    await db.commit()
    days = {d.day: d for d in await service.list_options(db, doctor.id, clinic.id)}
    assert list(days[friday].times) == [
        at(friday, "16:00"),
        at(friday, "16:15"),
        at(friday, "16:30"),
        at(friday, "16:45"),
    ]


async def test_todays_past_times_are_hidden_by_the_lead(db, clinic):
    # Now is Monday 13:00 in Pakistan. The morning session is over, so only the
    # later Mondays are offered; an afternoon session that starts after the
    # 30 minute lead brings today back.
    doctor = await verified_doctor(db, clinic)
    await add_session(db, doctor, clinic, weekday=MONDAY, start="09:00", end="13:00")
    days = await service.list_options(db, doctor.id, clinic.id)
    assert [d.day for d in days] == [date(2026, 10, 19), date(2026, 10, 26)]
    await add_session(db, doctor, clinic, weekday=MONDAY, start="14:00", end="18:00")
    today = (await service.list_options(db, doctor.id, clinic.id))[0]
    assert today.day == TODAY and today.times[0] == at(TODAY, "14:00")


async def test_the_last_day_is_included_and_the_next_is_not(db, clinic):
    doctor = await verified_doctor(db, clinic)
    await add_session(db, doctor, clinic, weekday=MONDAY, start="14:00", end="18:00")
    days = [d.day for d in await service.list_options(db, doctor.id, clinic.id)]
    # Today, then the 19th, then the 26th (today + 14 days); November the 2nd is out.
    assert days == [TODAY, date(2026, 10, 19), date(2026, 10, 26)]


async def test_options_are_per_hospital(db, clinic, second_clinic):
    doctor = await verified_doctor(db, clinic)
    await add_affiliation(db, doctor.id, second_clinic.id)
    await add_session(db, doctor, clinic, weekday=2, start="09:00", end="10:00")
    await add_session(db, doctor, second_clinic, weekday=3, start="09:00", end="10:00")
    here = await service.list_options(db, doctor.id, clinic.id)
    there = await service.list_options(db, doctor.id, second_clinic.id)
    assert {d.day.weekday() for d in here} == {2} and {d.day.weekday() for d in there} == {3}


async def test_a_hospital_the_doctor_is_not_at_is_not_found(db, clinic, second_clinic):
    doctor = await verified_doctor(db, clinic)
    with pytest.raises(NotFound):
        await service.list_options(db, doctor.id, second_clinic.id)


async def test_a_pending_doctor_has_no_options(db, clinic):
    user = await make_user(db, email="p@x.com", role=UserRole.DOCTOR, clinic_id=clinic.id)
    doctor = await doctor_of(db, user)
    await add_session(db, doctor, clinic)
    with pytest.raises(NotFound):
        await service.list_options(db, doctor.id, clinic.id)


async def test_a_time_after_midnight_in_pakistan_is_listed_under_its_pakistan_date(db, clinic):
    # Review focus 2: 00:15 on Thursday the 15th is 19:15 UTC on the 14th.
    doctor = await verified_doctor(db, clinic)
    await add_session(db, doctor, clinic, weekday=THURSDAY, start="00:00", end="01:00")
    days = await service.list_options(db, doctor.id, clinic.id)
    thursday = date(2026, 10, 15)
    assert days[0].day == thursday
    first = days[0].times[0]
    assert first == at(thursday, "00:00")
    assert first.date() == date(2026, 10, 14)  # the UTC date is the day before
    assert first.astimezone(dbtypes.PKT).date() == thursday


async def test_options_hide_times_the_doctor_already_holds_under_another_schedule_row(db, clinic):
    # Final review: see test_a_time_held_under_another_schedule_row_of_the_doctor_is_taken.
    doctor = await verified_doctor(db, clinic)
    wednesday = await add_session(db, doctor, clinic, minutes=20)
    monday = await add_session(db, doctor, clinic, weekday=MONDAY, start="09:00", end="13:00")
    _, patient, _ = await make_patient(db)
    await add_appointment(db, patient, doctor, clinic, wednesday, at(WED, "10:00"))
    db.add(
        ScheduleException(
            schedule_id=wednesday.id, exception_date=WED, kind=ScheduleExceptionType.LEAVE
        )
    )
    db.add(
        ScheduleException(
            schedule_id=monday.id,
            exception_date=WED,
            kind=ScheduleExceptionType.EXTRA,
            start_time=time(9, 0),
            end_time=time(13, 0),
        )
    )
    await db.commit()
    days = await service.list_options(db, doctor.id, clinic.id)
    times = [t for d in days if d.day == WED for t in d.times]
    assert at(WED, "10:00") not in times and at(WED, "10:15") not in times
    assert at(WED, "09:45") in times and at(WED, "10:30") in times


async def test_options_can_leave_out_the_booking_being_moved(db, clinic):
    doctor = await verified_doctor(db, clinic)
    session = await add_session(db, doctor, clinic, cap=1)
    _, patient, _ = await make_patient(db)
    mine = await add_appointment(db, patient, doctor, clinic, session, at(WED, "10:00"))
    full = await service.list_options(db, doctor.id, clinic.id)
    assert WED not in [d.day for d in full]  # the session is at its cap
    moving = await service.list_options(db, doctor.id, clinic.id, exclude=mine.id)
    wednesday = next(d for d in moving if d.day == WED)
    assert len(wednesday.times) == 16 and at(WED, "10:00") in wednesday.times
