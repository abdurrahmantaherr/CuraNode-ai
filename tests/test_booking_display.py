"""P5: the small display helpers (pure functions)."""

from __future__ import annotations

from datetime import datetime

import pytest
from app.booking import display
from app.booking.schemas import DayRow
from app.db import types as dbtypes

from tests.booking_helpers import WED, at


@pytest.mark.parametrize(
    ("status", "en", "ur"),
    [
        ("booked", "Confirmed", "کنفرم"),
        ("checked_in", "Checked in", "چیک اِن"),
        ("done", "Completed", "مکمل"),
        ("cancelled", "Cancelled", "منسوخ"),
    ],
)
def test_each_status_has_a_label_in_both_languages(status, en, ur):
    assert display.status_label(status, "en") == en
    assert display.status_label(status, "ur") == ur


def test_an_unknown_status_is_shown_as_stored_never_as_a_catalogue_key():
    assert display.status_label("something_new", "en") == "something_new"


def test_badges_by_status():
    assert display.status_badge("booked") == "badge--ok"
    assert display.status_badge("cancelled") == "badge--err"
    assert display.status_badge("done") == "badge--neutral"
    assert display.status_badge("something_new") == "badge--neutral"


def test_hhmm_is_pakistan_time():
    assert display.hhmm(at(WED, "00:15")) == "00:15"  # 19:15 UTC the day before
    assert display.hhmm(at(WED, "13:05")) == "13:05"


def test_hhmm_reads_a_naive_value_as_utc():
    naive = datetime(2026, 10, 14, 5, 0)  # noqa: DTZ001 - SQLite hands back naive stamps
    assert display.hhmm(naive) == "10:00"


def test_build_day_rows_has_name_time_and_status_only():
    rows = [
        DayRow(
            starts_at=at(WED, "10:00").astimezone(dbtypes.PKT),
            patient_name="Ayesha Raza",
            status="booked",
        ),
        DayRow(
            starts_at=at(WED, "00:15").astimezone(dbtypes.PKT),
            patient_name="Bilal Khan",
            status="cancelled",
        ),
    ]
    out = display.build_day_rows(rows, "en")
    assert out == [
        display.DayRowOut(
            time="10:00", name="Ayesha Raza", status="Confirmed", badge="badge--ok", cancelled=False
        ),
        display.DayRowOut(
            time="00:15",
            name="Bilal Khan",
            status="Cancelled",
            badge="badge--err",
            cancelled=True,
        ),
    ]
    assert set(display.DayRowOut.__dataclass_fields__) == {
        "time",
        "name",
        "status",
        "badge",
        "cancelled",
    }


def test_build_day_rows_speaks_urdu_and_handles_nothing():
    row = DayRow(starts_at=at(WED, "10:00"), patient_name="X", status="done")
    assert display.build_day_rows([row], "ur")[0].status == "مکمل"
    assert display.build_day_rows([], "en") == []
