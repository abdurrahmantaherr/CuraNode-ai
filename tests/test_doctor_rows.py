"""D4 v1 — display rows for the doctor dashboard (pure functions, no database)."""

from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace

from app.doctor.rows import DASH, PatientRow, age_in_years, build_rows

TODAY = date(2026, 10, 6)


def patient(**over):
    base = {
        "id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
        "full_name": "Ayesha Raza",
        "date_of_birth": date(1994, 3, 14),
        "gender": "female",
        "passport_no": "CN-AAAA-1111",
    }
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
            gender="Female",
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


def test_build_rows_translates_the_gender_code():
    # A stored code like "prefer_not_to_say" must never reach the screen.
    en = build_rows([patient(gender="prefer_not_to_say")], locale="en", today=TODAY)
    ur = build_rows([patient(gender="female")], locale="ur", today=TODAY)
    assert en[0].gender == "Prefer not to say"
    assert ur[0].gender == "خاتون"


def test_build_rows_whitespace_only_gender_is_a_dash():
    rows = build_rows([patient(gender="   ")], locale="en", today=TODAY)
    assert rows[0].gender == DASH


def test_build_rows_keeps_an_unknown_legacy_gender_as_stored():
    # The table is shared with the wider product and may hold legacy values.
    rows = build_rows([patient(gender="M")], locale="en", today=TODAY)
    assert rows[0].gender == "M"


def test_build_rows_keeps_input_order():
    a = patient(full_name="A", id=uuid.uuid4())
    b = patient(full_name="B", id=uuid.uuid4())
    assert [r.name for r in build_rows([a, b], locale="en", today=TODAY)] == ["A", "B"]
