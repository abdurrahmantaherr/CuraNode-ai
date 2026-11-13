"""What the signed-in home pages show (patient and doctor): real data, one "next step", honest gaps.

Everything here is built from rows the existing services already return — nothing is invented:
a count is a count of rows, a status is the stored status. Two kinds of code live here:

- PURE choosers (`patient_next_step`, `doctor_next_step`, the row builders): no database, no
  clock — the caller passes both. They decide which single action to put first.
- `section()`: runs one data load for a dashboard block and turns "the database could not be
  reached" — and only that (see `web.unavailable.is_database_unavailable`) — into "this block is
  unavailable", so one slow query cannot blank the whole page. Any other error is a bug and is
  raised as before.

No consent decisions are made here and no clinical data is read: a doctor's numbers come from the
same services as the doctor's existing panels (`booking.service.doctor_day`, `queueing.service`,
`consent.service.list_patients_for_doctor`), so they carry the same permissions.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..booking import display as booking_display
from ..booking.schemas import AppointmentRow, DayRow
from ..documents.schemas import DocumentOut
from ..i18n.catalogue import translate
from ..log_config import get_logger
from ..queueing.schemas import SessionView

log = get_logger()

# How many of each list the home page shows (one more is read to know whether there is more).
DOCS_SHOWN = 5
APPOINTMENTS_SHOWN = 3

# The order a patient's unfinished documents ask for attention in: what only THEY can move
# forward first, then what failed, then what the system is still doing.
_DOC_ATTENTION = ("awaiting_review", "failed", "uploaded", "processing")


async def section[T](
    session: AsyncSession, name: str, load: Callable[[], Awaitable[T]]
) -> tuple[T | None, bool]:
    """(value, True) — or (None, False) when the database could not be reached for this block."""
    # Imported here: `unavailable` imports the router, which imports this module.
    from .unavailable import is_database_unavailable

    try:
        return await load(), True
    except Exception as exc:
        if not is_database_unavailable(exc):
            raise
        log.warning("dashboard_section_unavailable", section=name, error=type(exc).__name__)
        await session.rollback()  # a failed statement leaves the transaction unusable
        return None, False


# ── Patient ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Step:
    """The one thing to do next. `kind` selects the wording; the rest fills it in."""

    kind: str
    href: str = ""
    name: str = ""  # a file name or a doctor's name
    when: str = ""  # a formatted date and time
    count: int = 0


def patient_next_step(
    *,
    loc: str,
    documents: Sequence[DocumentOut] | None,
    appointments: Sequence[AppointmentRow] | None,
    saved_count: int | None,
) -> Step | None:
    """The patient's most useful next action, or None when too little could be loaded to say.

    Documents first (only the patient can review or retry them), then the next appointment, then
    — a first-time patient with nothing at all — the two ways to begin."""
    base = f"/{loc}/patient"
    if documents:
        for status in _DOC_ATTENTION:
            for doc in documents:
                if doc.status == status:
                    return Step(
                        kind=f"doc_{status}",
                        href=f"{base}/documents/{doc.id}",
                        name=doc.original_filename or "",
                        count=sum(1 for d in documents if d.status == status),
                    )
    if appointments:
        first = appointments[0]
        return Step(
            kind="appointment",
            href=f"{base}/appointments/{first.id}",
            name=first.doctor_name,
            when=_when(first),
        )
    if documents is None or appointments is None or saved_count is None:
        return None  # part of the picture is missing: say nothing rather than guess
    if saved_count == 0:
        return Step(kind="first_use", href=f"{base}/documents/new")
    return Step(kind="all_clear", href=f"{base}/doctors")


def _when(row: AppointmentRow) -> str:
    return f"{row.starts_at.date().isoformat()} {booking_display.hhmm(row.starts_at)}"


def appointment_rows(rows: Sequence[AppointmentRow], loc: str) -> list[dict[str, Any]]:
    return [
        {
            "href": f"/{loc}/patient/appointments/{row.id}",
            "doctor": row.doctor_name,
            "where": (f"{row.specialty} · " if row.specialty else "") + row.clinic_name,
            "when": _when(row),
            "status": booking_display.status_label(row.status, loc),
            "badge": booking_display.status_badge(row.status),
        }
        for row in rows
    ]


def document_rows(docs: Sequence[DocumentOut], loc: str) -> list[dict[str, Any]]:
    """One line per unfinished document: its stored status and what it is waiting for."""
    return [
        {
            "id": str(doc.id),
            "href": f"/{loc}/patient/documents/{doc.id}",
            "name": doc.original_filename or translate("documents.detail.title", loc),
            "type": translate(f"documents.type.{doc.document_type}", loc),
            "status": doc.status,
            "hint": translate(f"dashboard.doc_hint.{doc.status}", loc),
        }
        for doc in docs
    ]


# ── Doctor ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class DoctorStep:
    kind: str  # waiting | next_appointment | day_done | idle
    href: str = ""
    name: str = ""
    when: str = ""
    count: int = 0


@dataclass(frozen=True)
class DoctorSummary:
    step: DoctorStep
    appointments_today: int  # not cancelled
    waiting: int
    patients: int


def doctor_next_step(
    *,
    loc: str,
    day: Sequence[DayRow],
    queue: Sequence[SessionView],
    patient_count: int,
    now: datetime,
) -> DoctorSummary:
    """A verified doctor's next task from what the page already loaded: patients waiting in the
    queue first, then the next appointment still ahead today, then a quiet day. `now` is Pakistan
    wall-clock, like `DayRow.starts_at`."""
    appointments = f"/{loc}/doctor/appointments"
    active = [row for row in day if row.status != "cancelled"]
    waiting = [line for view in queue for line in view.waiting]

    if waiting:
        step = DoctorStep("waiting", appointments, waiting[0].patient_name, count=len(waiting))
    else:
        ahead = [row for row in active if row.status == "booked" and row.starts_at >= now]
        if ahead:
            nxt = ahead[0]
            step = DoctorStep(
                "next_appointment",
                appointments,
                nxt.patient_name,
                when=booking_display.hhmm(nxt.starts_at),
                count=len(ahead),
            )
        elif active:
            step = DoctorStep("day_done", appointments, count=len(active))
        else:
            step = DoctorStep("idle", f"/{loc}/doctor/schedule")
    return DoctorSummary(
        step=step,
        appointments_today=len(active),
        waiting=len(waiting),
        patients=patient_count,
    )
