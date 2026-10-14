"""Turn a patient's records into timeline events — pure functions, no database (P3, FR8 / FR9).

The input is what the readers in `timeline/sources.py` and the consent gateway return: plain
dicts per section. The output is a list of `TimelineDetail`s, newest first, each carrying its
own detail blocks (the list endpoint drops them).

Rules that matter more than the layout:
  * An event is dated by when it HAPPENED — a visit's time, a prescription's issue date, the
    date a document itself states — never by when it was uploaded. A document with no date of
    its own is `unknown` and is listed after every dated event.
  * Nothing is invented. A value that is not in the record is not shown; a field is shown only
    when it has a value.
  * `provenance` says who vouches for an event. A program's reading of a paper counts only
    because the patient confirmed it, and says so.
  * An upload that is not confirmed yet is NOT an event: it is a `PendingItem` — what it is and
    where it stands, never a value read from it.
  * Two events are related only by a real link (a document's visit), never by looking alike.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from ..db import types as dbtypes
from ..documents.schemas import DB_TO_API_TYPE
from ..i18n.catalogue import translate
from .schemas import (
    CATEGORIES,
    Cell,
    DateBasis,
    DetailBlock,
    Fact,
    PendingItem,
    Provenance,
    Related,
    TimelineDetail,
    TimelineEvent,
)

_MONTHS = {
    "en": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
    "ur": (
        "جنوری",
        "فروری",
        "مارچ",
        "اپریل",
        "مئی",
        "جون",
        "جولائی",
        "اگست",
        "ستمبر",
        "اکتوبر",
        "نومبر",
        "دسمبر",
    ),
}
# A flag that means the value is outside what the report calls normal.
_OUT_OF_RANGE = frozenset({"high", "low", "abnormal", "critical"})
_SUMMARY_NAMES = 3  # medicine names shown on a card before "+N more"
_TITLE_REASON_MAX = 70


def human_date(value: date | None, loc: str) -> str:
    """ "11 Aug 2026" — the format the design uses. Pakistan dates, never shifted."""
    if value is None:
        return "—"
    months = _MONTHS.get(loc, _MONTHS["en"])
    return f"{value.day:02d} {months[value.month - 1]} {value.year}"


def _pkt(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)  # stored in UTC; a naive value is read as UTC
    return value.astimezone(dbtypes.PKT)


def _day(value: datetime | None) -> date | None:
    return dbtypes.pk_date(value) if value is not None else None


class _Ctx:
    def __init__(self, loc: str, owner: bool) -> None:
        self.loc = loc
        self.owner = owner

    def t(self, key: str, **params: Any) -> str:
        return translate(key, self.loc, **params)


def _dr(name: str | None) -> str | None:
    """ "Dr. X" — without doubling the prefix when the name already carries it."""
    if not name or not name.strip():
        return None
    name = name.strip()
    return name if name.lower().startswith(("dr.", "dr ")) else f"Dr. {name}"


def _clean(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _fact(ctx: _Ctx, key: str, value: Any) -> Fact | None:
    text = _clean(value)
    return Fact(label_key=key, label=ctx.t(key), value=text) if text else None


def _facts(ctx: _Ctx, heading_key: str, items: Iterable[tuple[str, Any]]) -> DetailBlock | None:
    facts = [f for key, value in items if (f := _fact(ctx, key, value)) is not None]
    if not facts:
        return None
    return DetailBlock(
        kind="facts", heading_key=heading_key, heading=ctx.t(heading_key), facts=facts
    )


def _text(ctx: _Ctx, heading_key: str, text: Any, *, ai: bool = False) -> DetailBlock | None:
    body = _clean(text)
    if not body:
        return None
    return DetailBlock(
        kind="text", heading_key=heading_key, heading=ctx.t(heading_key), text=body, ai_generated=ai
    )


def _table(
    ctx: _Ctx, heading_key: str, column_keys: Sequence[str], rows: list[list[Cell]]
) -> DetailBlock | None:
    if not rows:
        return None
    return DetailBlock(
        kind="table",
        heading_key=heading_key,
        heading=ctx.t(heading_key),
        columns=[ctx.t(key) for key in column_keys],
        rows=rows,
    )


def _blocks(*blocks: DetailBlock | None) -> list[DetailBlock]:
    return [b for b in blocks if b is not None]


def _event(
    ctx: _Ctx,
    *,
    kind: str,
    category: str,
    record_id: uuid.UUID,
    event_date: date | None,
    basis: DateBasis,
    title_key: str,
    title_params: Mapping[str, str] | None = None,
    summary: Iterable[str] = (),
    provenance: Provenance,
    blocks: list[DetailBlock],
    occurred_at: datetime | None = None,
    doctor: str | None = None,
    facility: str | None = None,
    document_id: uuid.UUID | None = None,
    uploaded_on: date | None = None,
    document_name: str | None = None,
) -> TimelineDetail:
    params = dict(title_params or {})
    if provenance == "ai_patient_confirmed" and ctx.owner:
        label_key = "timeline.prov.ai_you_confirmed"
    else:
        label_key = f"timeline.prov.{provenance}"
    return TimelineDetail(
        ref=f"{kind}:{record_id}",
        kind=kind,  # type: ignore[arg-type]
        category=category,  # type: ignore[arg-type]
        event_date=event_date,
        date_basis=basis,
        occurred_at=occurred_at,
        title_key=title_key,
        title_params=params,
        title=ctx.t(title_key, **params),
        summary=[line for line in summary if line],
        doctor=doctor,
        facility=facility,
        provenance=provenance,
        provenance_label=ctx.t(label_key),
        document_id=document_id if ctx.owner else None,  # a doctor is never given the file link
        uploaded_on=uploaded_on,
        document_name=document_name if ctx.owner else None,  # nor is a file name
        blocks=blocks,
    )


def _names_line(ctx: _Ctx, names: Sequence[str]) -> str:
    names = [n for n in names if n]
    if not names:
        return ""
    shown = ", ".join(names[:_SUMMARY_NAMES])
    extra = len(names) - _SUMMARY_NAMES
    return f"{shown} {ctx.t('timeline.more', n=extra)}" if extra > 0 else shown


def _by(by: str | None) -> Provenance:
    """A profile entry's author: the patient themselves, someone else (a clinician), or unknown
    (a row from before authorship was recorded)."""
    if by == "patient":
        return "patient_entered"
    if by == "clinician":
        return "clinician_recorded"
    return "source_unknown"


# ── Visits ───────────────────────────────────────────────────────────────
def _encounter(ctx: _Ctx, rec: Mapping[str, Any]) -> TimelineDetail:
    at: datetime = rec["at"]
    local = _pkt(at)
    reason = (
        _clean(rec.get("complaint")).splitlines()[0][:_TITLE_REASON_MAX]
        if rec.get("complaint")
        else ""
    )
    diagnoses = list(rec.get("diagnoses") or [])
    named = [_clean(d.get("description")) or _clean(d.get("code")) for d in diagnoses]
    doctor = _dr(rec.get("doctor"))
    facility = _clean(rec.get("facility")) or None
    diagnosis_rows = [
        [
            Cell(value=_clean(d.get("description")) or "—"),
            Cell(value=_clean(d.get("code")) or "—"),
            Cell(value=_clean(d.get("severity")) or "—"),
            Cell(value=ctx.t("timeline.yes") if d.get("chronic") else ctx.t("timeline.no")),
        ]
        for d in diagnoses
    ]
    return _event(
        ctx,
        kind="encounter",
        category="consultation",
        record_id=rec["id"],
        event_date=local.date(),
        basis="clinical",
        occurred_at=at,
        title_key="timeline.title.consultation_for" if reason else "timeline.title.consultation",
        title_params={"reason": reason} if reason else None,
        summary=[
            ctx.t("timeline.summary.diagnosis", text=_names_line(ctx, named)) if any(named) else ""
        ],
        provenance="doctor_confirmed",
        doctor=doctor,
        facility=facility,
        blocks=_blocks(
            _facts(
                ctx,
                "timeline.block.visit",
                [
                    ("timeline.fact.date", human_date(local.date(), ctx.loc)),
                    ("timeline.fact.time", local.strftime("%H:%M")),
                    ("timeline.fact.doctor", doctor),
                    ("timeline.fact.facility", facility),
                ],
            ),
            _text(ctx, "timeline.block.complaint", rec.get("complaint")),
            _table(
                ctx,
                "timeline.block.diagnoses",
                (
                    "timeline.col.diagnosis",
                    "timeline.col.icd10",
                    "timeline.col.severity",
                    "timeline.col.chronic",
                ),
                diagnosis_rows,
            ),
            _text(ctx, "timeline.block.notes", rec.get("notes")),
            _text(ctx, "timeline.block.ai_summary", rec.get("ai_summary"), ai=True),
        ),
    )


# ── Prescriptions ────────────────────────────────────────────────────────
def _medicine_label(medicine: str, dosage: str) -> str:
    return f"{medicine} {dosage}".strip() if dosage and dosage not in medicine else medicine


def _prescription(ctx: _Ctx, rec: Mapping[str, Any]) -> TimelineDetail:
    if rec.get("origin") == "document":
        return _rx_document(ctx, rec)
    items = list(rec.get("items") or [])
    issued: date = rec["issue_date"]
    doctor = _dr(rec.get("doctor"))
    facility = _clean(rec.get("facility")) or None
    patient_upload = rec.get("source") == "ocr_upload"
    # Documents this prescription was made from (linked by `ocr_extraction.item_id`): they are
    # its source, not a second event.
    sources = list(rec.get("documents") or [])
    source_name = _clean(sources[0].get("document_name")) if sources else ""
    source_uploaded = _day(sources[0].get("uploaded_at")) if sources else None
    rows = [
        [
            Cell(value=_clean(i.get("medicine")) or "—"),
            Cell(value=_clean(i.get("dosage")) or "—"),
            Cell(value=_clean(i.get("frequency")) or "—"),
            Cell(
                value=ctx.t("timeline.days", n=i["duration_days"])
                if i.get("duration_days")
                else "—"
            ),
            Cell(value=_clean(i.get("route")) or "—"),
            Cell(value=_clean(i.get("instructions")) or "—"),
        ]
        for i in items
    ]
    return _event(
        ctx,
        kind="prescription",
        category="prescription",
        record_id=rec["id"],
        event_date=issued,
        basis="clinical",
        title_key="timeline.title.prescription_issued",
        summary=[
            _names_line(
                ctx,
                [
                    _medicine_label(_clean(i.get("medicine")), _clean(i.get("strength")))
                    for i in items
                ],
            )
        ],
        provenance="ai_patient_confirmed" if patient_upload else "doctor_confirmed",
        doctor=doctor,
        facility=facility,
        document_id=sources[0]["id"] if sources else None,
        uploaded_on=source_uploaded,
        document_name=source_name or None,
        blocks=_blocks(
            _facts(
                ctx,
                "timeline.block.prescription",
                [
                    ("timeline.fact.date", human_date(issued, ctx.loc)),
                    ("timeline.fact.doctor", doctor),
                    ("timeline.fact.facility", facility),
                    ("timeline.fact.status", rec.get("status")),
                    (
                        "timeline.fact.source",
                        ctx.t("timeline.source.document") if sources else None,
                    ),
                    ("timeline.fact.file_name", source_name if ctx.owner else None),
                ],
            ),
            _table(
                ctx,
                "timeline.block.medicines",
                (
                    "timeline.col.medicine",
                    "timeline.col.dosage",
                    "timeline.col.frequency",
                    "timeline.col.duration",
                    "timeline.col.route",
                    "timeline.col.instructions",
                ),
                rows,
            ),
        ),
    )


def _doc_common(
    ctx: _Ctx, rec: Mapping[str, Any]
) -> tuple[date | None, DateBasis, date | None, Provenance]:
    stated: date | None = rec.get("date")
    uploaded_on = _day(rec.get("uploaded_at"))
    provenance: Provenance = (
        "doctor_confirmed" if rec.get("authored_by_doctor") else "ai_patient_confirmed"
    )
    return stated, ("clinical" if stated else "unknown"), uploaded_on, provenance


def _rx_document(ctx: _Ctx, rec: Mapping[str, Any]) -> TimelineDetail:
    stated, basis, uploaded_on, provenance = _doc_common(ctx, rec)
    rows = list(rec.get("rows") or [])
    doctor = _dr(rec.get("doctor"))
    facility = _clean(rec.get("facility")) or None
    table_rows = [
        [
            Cell(value=_clean(r.get("medicine")) or "—"),
            Cell(value=_clean(r.get("dosage")) or "—"),
            Cell(value=_clean(r.get("frequency")) or "—"),
            Cell(value=_clean(r.get("duration")) or "—"),
        ]
        for r in rows
    ]
    return _event(
        ctx,
        kind="rx_document",
        category="prescription",
        record_id=rec["id"],
        event_date=stated,
        basis=basis,
        title_key=(
            "timeline.title.prescription_issued"
            if rec.get("authored_by_doctor")
            else "timeline.title.prescription_uploaded"
        ),
        summary=[
            _names_line(
                ctx,
                [_medicine_label(_clean(r.get("medicine")), _clean(r.get("dosage"))) for r in rows],
            )
        ],
        provenance=provenance,
        doctor=doctor,
        facility=facility,
        document_id=rec["id"],
        uploaded_on=uploaded_on,
        document_name=_clean(rec.get("document_name")) or None,
        blocks=_blocks(
            _facts(
                ctx,
                "timeline.block.prescription",
                [
                    (
                        "timeline.fact.date",
                        human_date(stated, ctx.loc) if stated else ctx.t("timeline.date_unknown"),
                    ),
                    ("timeline.fact.doctor", doctor),
                    ("timeline.fact.facility", facility),
                    ("timeline.fact.uploaded_on", human_date(uploaded_on, ctx.loc)),
                    ("timeline.fact.file_name", rec.get("document_name") if ctx.owner else None),
                ],
            ),
            _table(
                ctx,
                "timeline.block.medicines",
                (
                    "timeline.col.medicine",
                    "timeline.col.dosage",
                    "timeline.col.frequency",
                    "timeline.col.duration",
                ),
                table_rows,
            ),
            _text(ctx, "timeline.block.advice", "\n".join(rec.get("notes") or [])),
        ),
    )


# ── Lab reports ──────────────────────────────────────────────────────────
def _flag_label(ctx: _Ctx, flag: str | None) -> str:
    return ctx.t(f"documents.flag.{flag}") if flag else ""


def _flag_note(ctx: _Ctx, row: Mapping[str, Any]) -> str | None:
    """A flag the report did not print is said to be worked out, never passed off as the lab's."""
    if not row.get("flag") or row.get("flag_source") != "calculated":
        return None
    return ctx.t(
        "documents.flag.check_hint" if row["flag"] == "check" else "documents.flag.calculated"
    )


def _lab_document(ctx: _Ctx, rec: Mapping[str, Any]) -> TimelineDetail:
    stated, basis, uploaded_on, provenance = _doc_common(ctx, rec)
    rows = list(rec.get("rows") or [])
    title = _clean(rec.get("title"))
    flagged = sum(1 for r in rows if r.get("flag") in _OUT_OF_RANGE)
    summary = ""
    if len(rows) == 1:
        summary = ctx.t(
            "timeline.summary.result_one_flagged" if flagged else "timeline.summary.result_one"
        )
    elif rows:
        summary = (
            ctx.t("timeline.summary.results_flagged", n=len(rows), flagged=flagged)
            if flagged
            else ctx.t("timeline.summary.results", n=len(rows))
        )
    facility = _clean(rec.get("facility")) or None
    doctor = _dr(rec.get("doctor"))
    table_rows = [
        [
            Cell(value=_clean(r.get("test_name")) or "—"),
            Cell(value=_clean(r.get("result")) or "—"),
            Cell(value=_clean(r.get("unit")) or "—"),
            Cell(value=_clean(r.get("reference_range")) or "—"),
            Cell(
                value=_flag_label(ctx, r.get("flag")), flag=r.get("flag"), note=_flag_note(ctx, r)
            ),
        ]
        for r in rows
    ]
    return _event(
        ctx,
        kind="lab_document",
        category="lab",
        record_id=rec["id"],
        event_date=stated,
        basis=basis,
        title_key="timeline.title.lab_report_titled" if title else "timeline.title.lab_report",
        title_params={"title": title[:_TITLE_REASON_MAX]} if title else None,
        summary=[summary],
        provenance=provenance,
        doctor=doctor,
        facility=facility,
        document_id=rec["id"],
        uploaded_on=uploaded_on,
        document_name=_clean(rec.get("document_name")) or None,
        blocks=_blocks(
            _facts(
                ctx,
                "timeline.block.lab_report",
                [
                    (
                        "timeline.fact.date",
                        human_date(stated, ctx.loc) if stated else ctx.t("timeline.date_unknown"),
                    ),
                    ("timeline.fact.laboratory", facility),
                    ("timeline.fact.doctor", doctor),
                    ("timeline.fact.uploaded_on", human_date(uploaded_on, ctx.loc)),
                    ("timeline.fact.file_name", rec.get("document_name") if ctx.owner else None),
                ],
            ),
            _table(
                ctx,
                "timeline.block.results",
                (
                    "timeline.col.test",
                    "timeline.col.result",
                    "timeline.col.unit",
                    "timeline.col.range",
                    "timeline.col.flag",
                ),
                table_rows,
            ),
            _text(ctx, "timeline.block.notes", "\n".join(rec.get("notes") or [])),
        ),
    )


# ── Profile entries ──────────────────────────────────────────────────────
def _allergy(ctx: _Ctx, rec: Mapping[str, Any]) -> TimelineDetail:
    recorded = _day(rec["recorded_at"])
    return _event(
        ctx,
        kind="allergy",
        category="profile",
        record_id=rec["id"],
        event_date=recorded,
        basis="recorded",
        title_key="timeline.title.allergy",
        title_params={"name": _clean(rec.get("substance"))},
        summary=[
            " · ".join(x for x in (_clean(rec.get("severity")), _clean(rec.get("reaction"))) if x)
        ],
        provenance=_by(rec.get("by")),
        blocks=_blocks(
            _facts(
                ctx,
                "timeline.block.allergy",
                [
                    ("timeline.fact.substance", rec.get("substance")),
                    ("timeline.fact.severity", rec.get("severity")),
                    ("timeline.fact.reaction", rec.get("reaction")),
                    ("timeline.fact.recorded_on", human_date(recorded, ctx.loc)),
                ],
            )
        ),
    )


def _condition(ctx: _Ctx, rec: Mapping[str, Any]) -> TimelineDetail:
    onset: date | None = rec.get("onset")
    recorded = _day(rec["recorded_at"])
    summary = ctx.t("timeline.summary.since", date=human_date(onset, ctx.loc)) if onset else ""
    return _event(
        ctx,
        kind="condition",
        category="profile",
        record_id=rec["id"],
        event_date=onset or recorded,
        basis="clinical" if onset else "recorded",
        title_key="timeline.title.condition",
        title_params={"name": _clean(rec.get("name"))},
        summary=[summary],
        provenance=_by(rec.get("by")),
        blocks=_blocks(
            _facts(
                ctx,
                "timeline.block.condition",
                [
                    ("timeline.fact.condition", rec.get("name")),
                    ("timeline.fact.onset", human_date(onset, ctx.loc) if onset else None),
                    ("timeline.fact.recorded_on", human_date(recorded, ctx.loc)),
                ],
            )
        ),
    )


def _medication(ctx: _Ctx, rec: Mapping[str, Any]) -> TimelineDetail:
    started: date | None = rec.get("started_on")
    recorded = _day(rec["recorded_at"])
    return _event(
        ctx,
        kind="medication",
        category="prescription",
        record_id=rec["id"],
        event_date=started or recorded,
        basis="clinical" if started else "recorded",
        title_key="timeline.title.medication",
        title_params={"name": _clean(rec.get("name"))},
        summary=[
            " · ".join(x for x in (_clean(rec.get("strength")), _clean(rec.get("frequency"))) if x)
        ],
        provenance=_by(rec.get("by")),
        blocks=_blocks(
            _facts(
                ctx,
                "timeline.block.medication",
                [
                    ("timeline.fact.medicine", rec.get("name")),
                    ("timeline.fact.strength", rec.get("strength")),
                    ("timeline.fact.frequency", rec.get("frequency")),
                    ("timeline.fact.started_on", human_date(started, ctx.loc) if started else None),
                    ("timeline.fact.recorded_on", human_date(recorded, ctx.loc)),
                ],
            )
        ),
    )


# ── Other saved documents (the patient's own view only) ──────────────────
def _other_document(ctx: _Ctx, rec: Mapping[str, Any]) -> TimelineDetail:
    stated, basis, uploaded_on, provenance = _doc_common(ctx, rec)
    rows = list(rec.get("rows") or [])
    facility = _clean(rec.get("facility")) or None
    doctor = _dr(rec.get("doctor"))
    title = _clean(rec.get("title"))
    vaccine = rec.get("doc_type") == "vaccine"
    if vaccine:
        names = [_clean(r.get("vaccine_name")) for r in rows]
        table = _table(
            ctx,
            "timeline.block.vaccines",
            (
                "timeline.col.vaccine",
                "timeline.col.dose",
                "timeline.col.date_given",
                "timeline.col.batch",
                "timeline.col.site",
                "timeline.col.next_due",
            ),
            [
                [
                    Cell(value=_clean(r.get(k)) or "—")
                    for k in ("vaccine_name", "dose", "date_given", "batch_no", "site", "next_due")
                ]
                for r in rows
            ],
        )
        title_key, params = "timeline.title.vaccination", None
    else:
        names = [_medicine_label(_clean(r.get("medicine")), _clean(r.get("dosage"))) for r in rows]
        table = _table(
            ctx,
            "timeline.block.medicines",
            (
                "timeline.col.medicine",
                "timeline.col.dosage",
                "timeline.col.frequency",
                "timeline.col.duration",
            ),
            [
                [
                    Cell(value=_clean(r.get(k)) or "—")
                    for k in ("medicine", "dosage", "frequency", "duration")
                ]
                for r in rows
            ],
        )
        title_key = "timeline.title.document_titled" if title else "timeline.title.document"
        params = {"title": title[:_TITLE_REASON_MAX]} if title else None
    return _event(
        ctx,
        kind="document",
        category="vaccination" if vaccine else "document",
        record_id=rec["id"],
        event_date=stated,
        basis=basis,
        title_key=title_key,
        title_params=params,
        summary=[_names_line(ctx, names) or (facility or "")],
        provenance=provenance,
        doctor=doctor,
        facility=facility,
        document_id=rec["id"],
        uploaded_on=uploaded_on,
        document_name=_clean(rec.get("document_name")) or None,
        blocks=_blocks(
            _facts(
                ctx,
                "timeline.block.document",
                [
                    (
                        "timeline.fact.date",
                        human_date(stated, ctx.loc) if stated else ctx.t("timeline.date_unknown"),
                    ),
                    ("timeline.fact.facility", facility),
                    ("timeline.fact.doctor", doctor),
                    ("timeline.fact.uploaded_on", human_date(uploaded_on, ctx.loc)),
                    ("timeline.fact.file_name", rec.get("document_name") if ctx.owner else None),
                ],
            ),
            table,
            _text(ctx, "timeline.block.notes", "\n".join(rec.get("notes") or [])),
        ),
    )


# ── The whole timeline ───────────────────────────────────────────────────
_BUILDERS = {
    "visits": _encounter,
    "prescriptions": _prescription,
    "lab_reports": _lab_document,
    "allergies": _allergy,
    "conditions": _condition,
    "medications": _medication,
    "documents": _other_document,
}


def build_events(
    records: Mapping[str, Sequence[Mapping[str, Any]]], *, loc: str, owner: bool
) -> list[TimelineDetail]:
    """Every record of every section given, as events, newest first. A section that is missing
    from `records` (not shared with the reader) simply contributes nothing."""
    ctx = _Ctx(loc, owner)
    events: list[TimelineDetail] = []
    visit_of: dict[str, uuid.UUID] = {}  # a document event's ref -> the visit it belongs to
    for section, recs in records.items():
        if section not in _BUILDERS:
            continue
        for rec in recs:
            event = _BUILDERS[section](ctx, rec)
            events.append(event)
            if rec.get("encounter_id") and rec.get("origin") == "document":
                visit_of[event.ref] = rec["encounter_id"]
    events = sort_events(events)
    _link_visits(events, visit_of)
    return events


def _link_visits(events: list[TimelineDetail], visit_of: Mapping[str, uuid.UUID]) -> None:
    """Say, on both sides, that a document belongs to a visit — only when the link is a real one
    (`document.encounter_id` / `lab_report.encounter_id`) AND the reader can see both events. A
    document whose visit the reader cannot see is simply a document: nothing is hidden or merged,
    and nothing is guessed from similar names or dates."""
    by_ref = {e.ref: e for e in events}
    for event in events:  # newest first, so each visit lists its documents in that order
        encounter_id = visit_of.get(event.ref)
        visit = by_ref.get(f"encounter:{encounter_id}") if encounter_id else None
        if visit is None:
            continue
        event.related.append(
            Related(
                ref=visit.ref, relation="part_of", title=visit.title, event_date=visit.event_date
            )
        )
        visit.related.append(
            Related(
                ref=event.ref,
                relation="has_attachment",
                title=event.title,
                event_date=event.event_date,
            )
        )


_KIND_ORDER = {
    kind: i
    for i, kind in enumerate(
        (
            "encounter",
            "prescription",
            "rx_document",
            "lab_document",
            "condition",
            "allergy",
            "medication",
            "document",
        )
    )
}


def _seconds(value: datetime | None) -> int:
    if value is None:
        return 0
    local = _pkt(value)
    return local.hour * 3600 + local.minute * 60 + local.second


def sort_key(event: TimelineEvent) -> tuple[Any, ...]:
    """Dated events newest first (a later visit on the same day above an earlier one, then by
    kind, then by id so the order is total). Undated events last, by the day they were uploaded —
    an ordering of last resort for a thing whose own date is not known."""
    if event.event_date is None:
        fallback = event.uploaded_on or date.min
        return (1, -fallback.toordinal(), 0, _KIND_ORDER.get(event.kind, 99), event.ref)
    return (
        0,
        -event.event_date.toordinal(),
        -_seconds(event.occurred_at),
        _KIND_ORDER.get(event.kind, 99),
        event.ref,
    )


def sort_events(events: list[TimelineDetail]) -> list[TimelineDetail]:
    return sorted(events, key=sort_key)


# ── Filters (FR9) ────────────────────────────────────────────────────────
QUERY_MAX = 100
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SPACES = re.compile(r"\s+")
# Facts that hold a date: the date filter is for those, so a search for "aug" does not match them.
_DATE_FACTS = frozenset(
    {
        "timeline.fact.date",
        "timeline.fact.time",
        "timeline.fact.uploaded_on",
        "timeline.fact.recorded_on",
        "timeline.fact.onset",
        "timeline.fact.started_on",
    }
)


def clean_query(raw: str | None) -> str:
    """A search text as typed, made safe to compare: control characters become spaces and runs of
    whitespace one space. (Its length is checked by the caller: `QUERY_MAX`.)"""
    return _SPACES.sub(" ", _CONTROL.sub(" ", raw or "")).strip()


@dataclass(frozen=True)
class Filters:
    category: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    facility: str | None = None
    doctor: str | None = None
    query: str = ""

    @property
    def narrowed_by_date(self) -> bool:
        return self.date_from is not None or self.date_to is not None

    @property
    def terms(self) -> tuple[str, ...]:
        """The search words, compared without regard to case. EVERY one must be found."""
        return tuple(self.query.casefold().split()) if self.query else ()


def _same(a: str | None, b: str | None) -> bool:
    return (a or "").strip().casefold() == (b or "").strip().casefold()


def search_text(event: TimelineEvent) -> str:
    """Everything the reader of this event can read in it, lower-cased: its title, summary, doctor,
    facility, file name (the patient's own view only — a doctor's event never carries one) and the
    CONTENT of its details (medicines, tests and results, diagnoses, notes). Headings, column names
    and dates are left out: a search for "medicines" must not match every prescription."""
    parts: list[str | None] = [
        event.title,
        *event.summary,
        event.doctor,
        event.facility,
        event.document_name,
    ]
    for block in getattr(event, "blocks", []):
        parts.append(block.text)
        parts.extend(f.value for f in block.facts if f.label_key not in _DATE_FACTS)
        parts.extend(cell.value for row in block.rows for cell in row)
    return " ".join(p for p in parts if p).casefold()


def matches(event: TimelineEvent, f: Filters, *, ignore_category: bool = False) -> bool:
    if not ignore_category and f.category and event.category != f.category:
        return False
    if f.narrowed_by_date:
        # An event with no date cannot be said to be inside a range, so a date filter leaves it out.
        if event.event_date is None:
            return False
        if f.date_from and event.event_date < f.date_from:
            return False
        if f.date_to and event.event_date > f.date_to:
            return False
    if f.facility and not _same(event.facility, f.facility):
        return False
    if f.doctor and not _same(event.doctor, f.doctor):
        return False
    if f.terms:
        text = search_text(event)
        return all(term in text for term in f.terms)
    return True


def tab_counts(events: Sequence[TimelineEvent], f: Filters) -> dict[str, int]:
    """How many events each tab would show given every filter but the tab itself."""
    counts = {"all": 0, **{c: 0 for c in CATEGORIES}}
    for event in events:
        if matches(event, f, ignore_category=True):
            counts["all"] += 1
            counts[event.category] += 1
    return counts


# ── Uploads that are not history yet ─────────────────────────────────────
_PENDING_CATEGORY = {
    "rx": "prescription",
    "lab": "lab",
    "vaccine": "vaccination",
    "other": "document",
}


def build_pending(records: Sequence[Mapping[str, Any]], *, loc: str) -> list[PendingItem]:
    """The patient's uploads that are being read, wait for their review, or failed — as what they
    are (type, name, upload day, status) and nothing read from them."""
    items = []
    for rec in records:
        status = rec["status"]
        message = None
        if status == "failed":
            code = rec.get("failure_code")
            text = translate(f"errors.{code}", loc) if code else ""
            message = (
                text if text and text != f"errors.{code}" else translate("errors.ocr_failed", loc)
            )
        items.append(
            PendingItem(
                id=rec["id"],
                category=_PENDING_CATEGORY.get(rec["doc_type"], "document"),  # type: ignore[arg-type]
                document_type=DB_TO_API_TYPE.get(rec["doc_type"], "other"),
                status=status,
                status_label=translate(f"documents.status.{status}", loc),
                name=_clean(rec.get("name")) or None,
                uploaded_on=_day(rec.get("uploaded_at")),
                message=message,
            )
        )
    return items


def matches_pending(item: PendingItem, f: Filters) -> bool:
    """An upload has no clinical date, facility or doctor yet, so any of those filters leaves it
    out (it could only be there by being falsely assigned one); the tab and the search apply."""
    if f.narrowed_by_date or f.facility or f.doctor:
        return False
    if f.category and item.category != f.category:
        return False
    if f.terms:
        text = " ".join(x for x in (item.name, item.document_type, item.status_label) if x)
        return all(term in text.casefold() for term in f.terms)
    return True


def distinct(values: Iterable[str | None]) -> list[str]:
    seen: dict[str, str] = {}
    for value in values:
        if value and value.strip():
            seen.setdefault(value.strip().casefold(), value.strip())
    return sorted(seen.values(), key=str.casefold)


def strip_blocks(event: TimelineDetail) -> TimelineEvent:
    return TimelineEvent(**event.model_dump(exclude={"blocks"}))
