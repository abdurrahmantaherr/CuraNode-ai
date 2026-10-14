"""Assemble a patient's medical timeline (P3, FR8 / FR9).

ONE code path serves the patient and a doctor the patient has shared with, and it is the
consent gateway's: `allowed_sections` decides what the reader may see (everything for the
patient; for a doctor only the sections an active grant names — anything else is the same
404, never "empty"), and every section a doctor reads is written to `access_log` before any
data leaves (purpose `timeline_view`). The timeline never selects another patient's rows
itself.

What reaches the page:
  * only SAVED documents are events. An upload that is being read, waits for the patient's
    review, or failed is listed for the patient as a `PendingItem` — what it is and where it
    stands, never a value read from it (an upload is not a record until the patient confirms it);
  * a doctor never receives the patient's vaccination records or other documents (no consent
    section covers them) and never receives a link to a stored file.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..consent import gateway
from ..db.models import Patient
from ..deps import Actor, remembered_patient_id
from ..errors import NotFound, ValidationFailed
from . import events as ev
from . import sources
from .schemas import CATEGORIES, KINDS, PendingItem, TimelineDetail, TimelineOut

PURPOSE = "timeline_view"

# The consent sections that carry timeline events, in the order they are read.
TIMELINE_SECTIONS: tuple[str, ...] = (
    "visits",
    "prescriptions",
    "lab_reports",
    "allergies",
    "conditions",
    "medications",
)
# Which section holds each kind of event. A saved vaccination record or other document
# (`document`) is in no section: it is the patient's own view only.
SECTION_OF_KIND: dict[str, str] = {
    "encounter": "visits",
    "prescription": "prescriptions",
    "rx_document": "prescriptions",
    "lab_document": "lab_reports",
    "allergy": "allergies",
    "condition": "conditions",
    "medication": "medications",
}
# The sections an event's detail page reads: its own, plus the ones holding the records it can be
# related to (a visit and the documents attached to it). Only those the reader may see are read —
# and a doctor's read of each is logged like any other.
RELATED_SECTIONS: dict[str, tuple[str, ...]] = {
    "encounter": ("visits", "prescriptions", "lab_reports"),
    "rx_document": ("prescriptions", "visits"),
    "lab_document": ("lab_reports", "visits"),
    "document": ("visits",),
}
DEFAULT_LIMIT = 100
MAX_LIMIT = 200


# ── Filters ──────────────────────────────────────────────────────────────
def parse_filters(values: Mapping[str, str | None]) -> tuple[ev.Filters, dict[str, str]]:
    """The tab / search / date range / facility / doctor filters from raw query values, and —
    never guessed at — the message key for each one that was not understood. An unknown value
    is reported, not silently turned into "everything"."""
    problems: dict[str, str] = {}

    def one(name: str) -> str | None:
        value = (values.get(name) or "").strip()
        return value or None

    category = one("type")
    if category == "all":
        category = None
    if category is not None and category not in CATEGORIES:
        problems["type"] = "errors.timeline_type_invalid"
        category = None

    query = ev.clean_query(values.get("q"))
    if len(query) > ev.QUERY_MAX:
        problems["q"] = "errors.timeline_query_too_long"
        query = ""

    dates: dict[str, date | None] = {}
    for name in ("date_from", "date_to"):
        raw = one(name)
        dates[name] = None
        if raw is not None:
            try:
                dates[name] = date.fromisoformat(raw)
            except ValueError:
                problems[name] = "errors.timeline_date_invalid"
    if (
        dates["date_from"]
        and dates["date_to"]
        and dates["date_from"] > dates["date_to"]
        and not problems
    ):
        problems["date_to"] = "errors.timeline_range_invalid"
        dates["date_from"] = dates["date_to"] = None

    return (
        ev.Filters(
            category=category,
            date_from=dates["date_from"],
            date_to=dates["date_to"],
            facility=one("facility"),
            doctor=one("doctor"),
            query=query,
        ),
        problems,
    )


# ── Loading ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Loaded:
    access: gateway.Access
    events: list[TimelineDetail]
    pending: list[PendingItem]  # the patient's own view only


async def own_patient_id(session: AsyncSession, actor: Actor) -> uuid.UUID:
    """The signed-in patient's own patient id (from their verified user id, never from a request)."""
    known = remembered_patient_id(session, actor.user_id)
    if known is not None:
        return known
    found = await session.scalar(select(Patient.id).where(Patient.user_id == actor.user_id))
    if found is None:
        raise NotFound()
    return found


async def load(session: AsyncSession, actor: Actor, patient_id: uuid.UUID, *, loc: str) -> Loaded:
    """Every event the reader may see, newest first. Raises `ConsentDenied` (a 404) when the
    reader may see nothing at all. Commits once, so a doctor's reads and their log rows land
    together — or neither does."""
    try:
        access = await gateway.allowed_sections(session, actor, patient_id, purpose=PURPOSE)
        records: dict[str, Any] = {}
        # The saved documents are read once for the whole load, not once per section.
        types = sources.document_types_for(access.sections, owner=access.is_owner)
        async with sources.preloaded_documents(session, access.patient, types):
            for section in TIMELINE_SECTIONS:
                if section in access.sections:
                    records[section] = await gateway.read_section(
                        session, actor, access, section, purpose=PURPOSE, rich=True
                    )
            if access.is_owner:
                records["documents"] = await sources.other_documents(session, access.patient)
        pending: list[dict[str, Any]] = []
        if access.is_owner:
            pending = await sources.pending_documents(session, access.patient)
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    return Loaded(
        access,
        ev.build_events(records, loc=loc, owner=access.is_owner),
        pending=ev.build_pending(pending, loc=loc),
    )


async def timeline(
    session: AsyncSession,
    actor: Actor,
    patient_id: uuid.UUID,
    filters: ev.Filters,
    *,
    loc: str,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    clamp: bool = False,
) -> TimelineOut:
    """One page of the timeline. `clamp` (the web pages) moves an offset past the end back to
    the last page; the API leaves it and answers an empty page, as any list endpoint does."""
    loaded = await load(session, actor, patient_id, loc=loc)
    everything = loaded.events
    chosen = [e for e in everything if ev.matches(e, filters)]
    if clamp and chosen and offset >= len(chosen):
        offset = ((len(chosen) - 1) // limit) * limit
    return TimelineOut(
        items=[ev.strip_blocks(e) for e in chosen[offset : offset + limit]],
        total=len(chosen),
        limit=limit,
        offset=offset,
        counts=ev.tab_counts(everything, filters),
        facilities=ev.distinct(e.facility for e in everything),
        doctors=ev.distinct(e.doctor for e in everything),
        undated=sum(1 for e in chosen if e.event_date is None),
        pending=[p for p in loaded.pending if ev.matches_pending(p, filters)],
        pending_review=(
            sum(1 for p in loaded.pending if p.status == "awaiting_review")
            if loaded.access.is_owner
            else None
        ),
    )


async def event_detail(
    session: AsyncSession,
    actor: Actor,
    patient_id: uuid.UUID,
    kind: str,
    event_id: uuid.UUID,
    *,
    loc: str,
) -> TimelineDetail:
    """One event with its details. An event in a section that was not shared is the same 404 as one
    that does not exist. Besides its own section, only the sections holding records it can be
    related to are read (`RELATED_SECTIONS`), and only if the reader may see them — so the
    relationship shown here is exactly the one the list shows."""
    if kind not in KINDS:
        raise NotFound()
    try:
        access = await gateway.allowed_sections(session, actor, patient_id, purpose=PURPOSE)
        records: dict[str, Any] = {}
        if kind == "document":
            if not access.is_owner:
                raise NotFound()  # no consent section covers it: not part of a doctor's view
        elif SECTION_OF_KIND[kind] not in access.sections:
            raise NotFound()  # not shared: the same answer as an event that does not exist
        own = () if kind == "document" else (SECTION_OF_KIND[kind],)
        to_read = [
            section
            for section in dict.fromkeys((*own, *RELATED_SECTIONS.get(kind, ())))
            if section in access.sections
        ]
        # vaccination records and other documents are the patient's own: they can belong to a visit
        with_documents = access.is_owner and kind in ("document", "encounter")
        types = sources.document_types_for(to_read, owner=with_documents)
        async with sources.preloaded_documents(session, access.patient, types):
            for section in to_read:
                records[section] = await gateway.read_section(
                    session, actor, access, section, purpose=PURPOSE, rich=True
                )
            if with_documents:
                records["documents"] = await sources.other_documents(session, access.patient)
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    wanted = f"{kind}:{event_id}"
    for event in ev.build_events(records, loc=loc, owner=access.is_owner):
        if event.ref == wanted:
            return event
    raise NotFound()


def require_valid(problems: dict[str, str]) -> None:
    """For the JSON API: a filter that was not understood is a 422 naming it."""
    if problems:
        raise ValidationFailed({"fields": problems})
