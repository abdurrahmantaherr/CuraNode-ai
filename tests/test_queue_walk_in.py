"""P6: finding a patient at the front desk, and walk-ins."""

from __future__ import annotations

import json
import uuid
from datetime import date

import pytest
from app.booking import service as booking_service
from app.booking import slots
from app.db.models import Appointment, AuditLog, VerificationStatus
from app.deps import Actor
from app.errors import NotFound, QueueRefused
from app.queueing import service
from app.queueing.display import mask_passport
from app.queueing.schemas import SessionKey
from sqlalchemy import select

from tests.booking_helpers import TODAY, add_appointment, at, freeze, make_patient
from tests.queue_helpers import add_entry, booked_today, entry_of, make_admin, todays_session


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    freeze(monkeypatch)


async def person(db, n: int, name: str | None = None):
    _, patient, actor = await make_patient(db, email=f"w{n}@x.com", name=name or f"Patient {n}")
    return patient, actor


async def world(db, clinic, **kw):
    doctor, schedule = await todays_session(db, clinic, **kw)
    _, admin = await make_admin(db, clinic)
    key = SessionKey(doctor.id, clinic.id, schedule.id, schedule.start_time)
    return doctor, schedule, admin, key


async def reason_of(call) -> str:
    with pytest.raises(QueueRefused) as exc:
        await call
    return exc.value.reason


async def appointment_of(db, patient) -> Appointment:
    return (
        await db.execute(
            select(Appointment)
            .where(Appointment.patient_id == patient.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


# ── Masking ──────────────────────────────────────────────────────────────
def test_a_passport_number_shows_only_its_last_four_characters():
    assert mask_passport("CN-ZWSD-6GH8") == "••-••••-6GH8"


def test_a_very_short_value_is_hidden_completely():
    assert mask_passport("AB-12") == "••-••"


# ── The search ───────────────────────────────────────────────────────────
async def test_a_name_search_ignores_case_and_matches_part_of_a_name(db, clinic):
    _, _, admin, _ = await world(db, clinic)
    patient, _ = await person(db, 1, "Ayesha Raza")
    await person(db, 2, "Bilal Khan")
    hits = await service.find_patients(db, admin, "  YESHA ")
    assert [(h.patient_id, h.name) for h in hits] == [(patient.id, "Ayesha Raza")]


async def test_an_exact_passport_number_matches_in_any_case(db, clinic):
    _, _, admin, _ = await world(db, clinic)
    patient, _ = await person(db, 1)
    hits = await service.find_patients(db, admin, patient.passport_no.lower())
    assert [h.patient_id for h in hits] == [patient.id]


async def test_part_of_a_passport_number_never_matches(db, clinic):
    # Review focus 5: no enumerating patients by guessing numbers.
    _, _, admin, _ = await world(db, clinic)
    patient, _ = await person(db, 1, "Zed Person")
    assert await service.find_patients(db, admin, patient.passport_no[:-1]) == []
    assert await service.find_patients(db, admin, patient.passport_no[3:]) == []


async def test_results_never_carry_the_full_passport_number(db, clinic):
    _, _, admin, _ = await world(db, clinic)
    patient, _ = await person(db, 1, "Ayesha Raza")
    [hit] = await service.find_patients(db, admin, "Ayesha")
    assert (
        hit.passport == mask_passport(patient.passport_no) and hit.passport != patient.passport_no
    )
    assert patient.passport_no[-4:] in hit.passport


async def test_a_short_search_is_refused_and_a_long_result_list_is_cut(db, clinic):
    _, _, admin, _ = await world(db, clinic)
    assert await reason_of(service.find_patients(db, admin, " ab ")) == "search_too_short"
    for n in range(12):
        await person(db, n, f"Zainab {n:02d}")
    hits = await service.find_patients(db, admin, "Zainab")
    assert len(hits) == 8 and [h.name for h in hits] == [f"Zainab {n:02d}" for n in range(8)]


async def test_a_percent_sign_is_not_a_wildcard(db, clinic):
    _, _, admin, _ = await world(db, clinic)
    await person(db, 1, "Ayesha Raza")
    assert await service.find_patients(db, admin, "%%%") == []


async def test_only_an_admin_can_search(db, clinic):
    _, _, _, _ = await world(db, clinic)
    await person(db, 1)
    with pytest.raises(NotFound):
        await service.find_patients(db, Actor(user_id=uuid.uuid4(), role="patient"), "Patient")


async def test_a_search_logs_only_how_many_it_found(db, clinic):
    _, _, admin, _ = await world(db, clinic)
    await person(db, 1, "Ayesha Raza")
    await service.find_patients(db, admin, "Ayesha")
    [row] = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "queue.find_patient")))
        .scalars()
        .all()
    )
    assert json.loads(row.detail) == {"results": 1}
    assert "Ayesha" not in (row.detail or "") and row.subject_patient_id is None


# ── Walk-ins ─────────────────────────────────────────────────────────────
async def test_a_walk_in_gets_an_appointment_and_a_place_at_the_back(db, clinic):
    doctor, schedule, admin, key = await world(db, clinic)
    waiting, _ = await person(db, 1)
    await add_entry(db, await booked_today(db, clinic, doctor, schedule, waiting), 1)
    walker, _ = await person(db, 2)
    await service.add_walk_in(db, admin, key, walker.id)
    appointment = await appointment_of(db, walker)
    entry = await entry_of(db, appointment)
    assert (appointment.booking_channel, appointment.status) == ("walk_in", "checked_in")
    assert appointment.schedule_id == schedule.id and appointment.clinic_id == clinic.id
    assert (entry.state, entry.token_number) == ("waiting", 2)


async def test_a_walks_in_time_is_the_arrival_time(db, clinic, monkeypatch):
    _doctor, _schedule, admin, key = await world(db, clinic)
    freeze(monkeypatch, now=at(TODAY, "13:07"))
    walker, _ = await person(db, 1)
    await service.add_walk_in(db, admin, key, walker.id)
    appointment = await appointment_of(db, walker)
    assert slots.as_utc(appointment.slot_start) == at(TODAY, "13:07")


async def test_a_walk_in_is_never_placed_before_the_session_starts(db, clinic, monkeypatch):
    _doctor, _schedule, admin, key = await world(db, clinic)
    freeze(monkeypatch, now=at(TODAY, "11:30"))
    walker, _ = await person(db, 1)
    await service.add_walk_in(db, admin, key, walker.id)
    appointment = await appointment_of(db, walker)
    start = slots.as_utc(appointment.slot_start)
    # one microsecond after the session opens: inside the window, off the booking grid
    assert (start - at(TODAY, "12:00")).total_seconds() == pytest.approx(0.000001)


async def test_two_walk_ins_in_the_same_instant_both_get_a_place(db, clinic):
    # Review focus 2.
    _doctor, _schedule, admin, key = await world(db, clinic)
    first, _ = await person(db, 1)
    second, _ = await person(db, 2)
    await service.add_walk_in(db, admin, key, first.id)
    await service.add_walk_in(db, admin, key, second.id)
    one, two = await appointment_of(db, first), await appointment_of(db, second)
    assert one.slot_start != two.slot_start
    assert [(await entry_of(db, one)).token_number, (await entry_of(db, two)).token_number] == [
        1,
        2,
    ]


async def test_walk_ins_count_toward_the_cap(db, clinic):
    doctor, schedule, admin, key = await world(db, clinic, cap=2)
    booked, _ = await person(db, 1)
    await booked_today(db, clinic, doctor, schedule, booked, "14:00")
    one, _ = await person(db, 2)
    two, _ = await person(db, 3)
    await service.add_walk_in(db, admin, key, one.id)
    assert await reason_of(service.add_walk_in(db, admin, key, two.id)) == "session_full"
    assert await entry_of(db, await appointment_of(db, one)) is not None


async def test_a_patient_with_a_booking_today_is_sent_to_check_in(db, clinic):
    doctor, schedule, admin, key = await world(db, clinic)
    patient, _ = await person(db, 1)
    await booked_today(db, clinic, doctor, schedule, patient, "14:00")
    assert await reason_of(service.add_walk_in(db, admin, key, patient.id)) == "has_booking_today"


async def test_a_patient_already_in_a_queue_cannot_join_another(db, clinic):
    _doctor, _schedule, admin, key = await world(db, clinic)
    other, other_schedule = await todays_session(db, clinic, email="o.dr@x.com", name="Dr Other")
    patient, _ = await person(db, 1)
    await add_entry(db, await booked_today(db, clinic, other, other_schedule, patient), 1)
    assert await reason_of(service.add_walk_in(db, admin, key, patient.id)) == "already_queued"


async def test_a_patient_who_was_seen_earlier_can_come_back_as_a_walk_in(db, clinic):
    doctor, schedule, admin, key = await world(db, clinic)
    patient, _ = await person(db, 1)
    seen = await booked_today(db, clinic, doctor, schedule, patient, "12:30")
    await add_entry(db, seen, 1, "served")
    await service.add_walk_in(db, admin, key, patient.id)
    entries = (
        await db.execute(select(Appointment).where(Appointment.patient_id == patient.id))
    ).all()
    assert len(entries) == 2


async def test_no_walk_in_after_the_session_has_ended(db, clinic, monkeypatch):
    _doctor, _schedule, admin, key = await world(db, clinic)
    patient, _ = await person(db, 1)
    freeze(monkeypatch, now=at(TODAY, "18:30"))
    assert await reason_of(service.add_walk_in(db, admin, key, patient.id)) == "session_over"


async def test_a_doctor_who_is_not_verified_cannot_take_walk_ins(db, clinic):
    doctor, _schedule, admin, key = await world(db, clinic)
    patient, _ = await person(db, 1)
    doctor.verification_status = VerificationStatus.REJECTED
    doctor.verified_by = None
    doctor.verified_at = None
    await db.commit()
    assert await reason_of(service.add_walk_in(db, admin, key, patient.id)) == "doctor_unavailable"


async def test_scoping_and_unknown_things_are_not_found(db, clinic, second_clinic):
    doctor, _schedule, admin, key = await world(db, clinic)
    patient, _ = await person(db, 1)
    _, outsider = await make_admin(db, second_clinic, email="out.admin@x.com")
    with pytest.raises(NotFound):
        await service.add_walk_in(db, outsider, key, patient.id)
    with pytest.raises(NotFound):
        await service.add_walk_in(db, admin, key, uuid.uuid4())
    with pytest.raises(NotFound):
        await service.add_walk_in(
            db, admin, SessionKey(doctor.id, clinic.id, uuid.uuid4(), key.start), patient.id
        )
    with pytest.raises(NotFound):
        await service.add_walk_in(db, Actor(user_id=uuid.uuid4(), role="patient"), key, patient.id)


async def test_a_walk_in_locks_the_doctor_first_and_writes_an_audit_row(db, clinic, monkeypatch):
    doctor, _schedule, admin, key = await world(db, clinic)
    patient, _ = await person(db, 1)
    calls: list[uuid.UUID] = []
    real = service.schedule_service.lock_doctor

    async def spy(session, doctor_id):
        calls.append(doctor_id)
        await real(session, doctor_id)

    monkeypatch.setattr(service.schedule_service, "lock_doctor", spy)
    await service.add_walk_in(db, admin, key, patient.id)
    assert calls == [doctor.id]
    [row] = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "queue.walk_in")))
        .scalars()
        .all()
    )
    assert set(json.loads(row.detail)) == {"appointment_id", "doctor_id", "clinic_id", "entry_id"}


# ── Walk-ins and the booking rules ───────────────────────────────────────
async def test_a_walk_in_never_hides_a_bookable_time(db, clinic, monkeypatch):
    # Review focus 2: it holds 12:00:00.000001, which overlaps the 12:00 and 12:15
    # turns on paper, but it is not a booked turn and must not block them.
    doctor, _schedule, admin, key = await world(db, clinic)
    freeze(monkeypatch, now=at(TODAY, "11:30"))
    walker, _ = await person(db, 1)
    await service.add_walk_in(db, admin, key, walker.id)
    options = await booking_service.list_options(db, doctor.id, clinic.id)
    today = next(d for d in options if d.day == TODAY)
    assert at(TODAY, "12:00") in today.times and at(TODAY, "12:15") in today.times
    _, _, actor = await make_patient(db, email="online@x.com", name="Online")
    appointment = await booking_service.book(db, actor, doctor.id, clinic.id, at(TODAY, "12:15"))
    assert appointment.status == "booked"


async def test_the_doctor_held_check_counts_every_channel_but_walk_in(db, clinic):
    doctor, schedule, _, _ = await world(db, clinic)
    online, legacy, walker = [(await person(db, n))[0] for n in (1, 2, 3)]
    await booked_today(db, clinic, doctor, schedule, online, "14:00")  # channel "online"
    old = await booked_today(db, clinic, doctor, schedule, legacy, "14:15")
    old.booking_channel = None  # a legacy booking with no channel still blocks
    arrival = await booked_today(db, clinic, doctor, schedule, walker, "14:30")
    arrival.booking_channel = "walk_in"
    await db.commit()
    held = await booking_service._doctor_held(db, doctor.id, at(TODAY, "12:00"), at(TODAY, "18:00"))
    assert sorted(start for start, _ in held) == [at(TODAY, "14:00"), at(TODAY, "14:15")]


async def walk_ins_of(db, patient) -> list[Appointment]:
    return list(
        (
            await db.execute(
                select(Appointment).where(
                    Appointment.patient_id == patient.id, Appointment.booking_channel == "walk_in"
                )
            )
        )
        .scalars()
        .all()
    )


async def test_a_leftover_entry_from_an_earlier_day_does_not_block_a_walk_in(db, clinic):
    # Final review: a patient who left last week without being seen still had a
    # `waiting` entry, and "already in a queue" then refused them for ever, at every clinic.
    _, _, admin, key = await world(db, clinic)
    other, other_schedule = await todays_session(db, clinic, email="o.dr@x.com", name="Dr Other")
    patient, _ = await person(db, 1)
    last_monday = await add_appointment(
        db, patient, other, clinic, other_schedule, at(date(2026, 10, 5), "14:00")
    )
    await add_entry(db, last_monday, 1)  # checked in, never seen, never cleared
    await service.add_walk_in(db, admin, key, patient.id)
    assert len(await walk_ins_of(db, patient)) == 1


async def test_an_entry_from_just_before_midnight_in_pakistan_is_not_todays(db, clinic):
    # 23:30 on Sunday is still Sunday in Pakistan (18:30 UTC), so it is not today's queue.
    doctor, schedule, admin, key = await world(db, clinic)
    patient, _ = await person(db, 1)
    late = await add_appointment(
        db, patient, doctor, clinic, schedule, at(date(2026, 10, 11), "23:30")
    )
    await add_entry(db, late, 1)
    await service.add_walk_in(db, admin, key, patient.id)
    assert len(await walk_ins_of(db, patient)) == 1
