"""Pure unit tests for the patient-profile helpers and request models
(profile SPEC §11.3) — support T2, T6 and T13."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from app.db import types as dbtypes
from app.profile.schemas import (
    AllergyCreate,
    AllergyUpdate,
    ConditionCreate,
    ConditionUpdate,
    MedicationCreate,
    MedicationUpdate,
    ProfileUpdateRequest,
    check_past_date,
    clean_text,
    normalise_name,
)
from app.profile.service import compute_age
from pydantic import ValidationError
from pydantic_core import PydanticCustomError


def test_clean_text_trims_and_collapses():
    assert clean_text("  Penicillin  ") == "Penicillin"
    assert clean_text("a   b") == "a b"
    assert clean_text("a\t\n b") == "a b"
    assert clean_text("  ") is None
    assert clean_text("") is None
    assert clean_text(None) is None


@pytest.mark.parametrize("bad", ["a\x00b", "a\x1bb", "\x07"])
def test_clean_text_rejects_control_characters(bad):
    with pytest.raises(PydanticCustomError) as exc:
        clean_text(bad)
    assert exc.value.message() == "errors.text_invalid"


def test_clean_text_keeps_urdu():
    assert clean_text("پینسلین") == "پینسلین"


def test_normalise_name():
    assert normalise_name("  PeNiCiLLiN ") == "penicillin"


@pytest.mark.parametrize(
    ("dob", "today", "age"),
    [
        (date(1990, 1, 1), date(2026, 9, 22), 36),
        (date(1990, 9, 23), date(2026, 9, 22), 35),
        (date(2000, 2, 29), date(2025, 2, 28), 24),
        (date(2000, 2, 29), date(2025, 3, 1), 25),
        (None, date(2026, 9, 22), None),
    ],
)
def test_compute_age(dob, today, age):
    assert compute_age(dob, today) == age


def test_check_past_date_bounds():
    today = dbtypes.today_pk()
    assert check_past_date(date(1900, 1, 1)) == date(1900, 1, 1)
    assert check_past_date(today) == today
    assert check_past_date(None) is None
    with pytest.raises(PydanticCustomError) as future:
        check_past_date(today + timedelta(days=1))
    assert future.value.message() == "errors.date_in_future"
    with pytest.raises(PydanticCustomError) as early:
        check_past_date(date(1899, 12, 31))
    assert early.value.message() == "errors.date_too_early"


@pytest.mark.parametrize(
    ("model", "valid"),
    [
        (ProfileUpdateRequest, {}),
        (AllergyCreate, {"substance": "Dust"}),
        (AllergyUpdate, {}),
        (ConditionCreate, {"name": "Asthma"}),
        (ConditionUpdate, {}),
        (MedicationCreate, {"name": "Metformin"}),
        (MedicationUpdate, {}),
    ],
)
def test_request_models_forbid_unknown_fields(model, valid):
    model.model_validate(valid)
    with pytest.raises(ValidationError):
        model.model_validate({**valid, "patient_id": "x"})


def test_error_messages_are_i18n_keys():
    with pytest.raises(ValidationError) as exc:
        AllergyCreate.model_validate({"reaction": "x" * 256, "severity": "high"})
    msgs = {e["loc"][0]: e["msg"] for e in exc.value.errors()}
    assert msgs == {
        "substance": "errors.text_required",
        "reaction": "errors.text_too_long",
        "severity": "errors.choice_invalid",
    }
