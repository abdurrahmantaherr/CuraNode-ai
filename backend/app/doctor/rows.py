"""Display rows for the doctor dashboard (D4 v1).

Pure functions only: no database access, no consent decisions. Who a doctor may
see is decided in `consent/service.py`; this module only formats what that
returns.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from ..db.models import Patient

DASH = "—"


@dataclass(frozen=True)
class PatientRow:
    name: str
    age: str
    gender: str
    passport_no: str
    href: str


def age_in_years(born: date | None, today: date) -> int | None:
    """Whole years since `born`; None when unknown or in the future."""
    if born is None or born > today:
        return None
    years = today.year - born.year
    if (today.month, today.day) < (born.month, born.day):
        years -= 1
    return years


def build_rows(patients: Sequence[Patient], *, locale: str, today: date) -> list[PatientRow]:
    rows: list[PatientRow] = []
    for p in patients:
        age = age_in_years(p.date_of_birth, today)
        rows.append(
            PatientRow(
                name=p.full_name,
                age=str(age) if age is not None else DASH,
                gender=p.gender or DASH,
                passport_no=p.passport_no,
                # The id, not the passport number: paths reach the access log (P4 AC-12).
                href=f"/{locale}/doctor/patient/{p.id}",
            )
        )
    return rows
