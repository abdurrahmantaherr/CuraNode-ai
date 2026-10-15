"""Where the timeline's visits, prescriptions and lab reports come from (P3).

Section readers for `consent/gateway.py`: each takes ONE `Patient` and reads only that
patient's rows — they never decide who may call them. Whoever may read a patient's `visits`,
`prescriptions` or `lab_reports` is decided by the gateway (a grant for that section, or the
patient themselves); nothing else in the codebase may select from these tables
(`tests/test_privacy_access.py::test_ac21_*` scans for it, exactly as for the profile tables).

  visits         the doctor's `encounter` rows, each with its diagnoses
  prescriptions  the doctor's `prescription` rows with their items, AND every saved prescription
                 document (a patient's upload they confirmed, or one a doctor wrote)
  lab_reports    every saved lab-report document

The tables are empty until the doctor-side features that write them exist; the saved documents
are what fills a timeline today. A document counts only once it is saved (an upload still being
reviewed is not a record), and only the values the patient confirmed are read from it
(`documents.service.finalized_documents`).

Records are plain dicts (UUIDs, dates and datetimes as they come), consumed by `events.py`.

One real thing, one event. The schema's own links decide what is "the same record" — never a
name or a date that look alike:
  * a document a `lab_report` row points at IS that lab event (the row adds only its visit and a
    fallback date; it never yields a second event);
  * a document whose cells became `prescription_item`s (`ocr_extraction.item_id`) is the SOURCE of
    that prescription: the prescription is the event, the document is listed on it;
  * `document.encounter_id` / `lab_report.encounter_id` link a document to a visit — both stay,
    and `events.py` shows the relationship.
None of these links is written by this application today, so they matter once the doctor-side and
lab features write them.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import types as dbtypes
from ..db.models import (
    DOC_TYPE_LAB,
    DOC_TYPE_OTHER,
    DOC_TYPE_RX,
    DOC_TYPE_VACCINE,
    Clinic,
    Diagnosis,
    Doctor,
    Encounter,
    LabReport,
    Medicine,
    Patient,
    Prescription,
    PrescriptionItem,
)
from ..documents import service as doc_service

_COMPACT_NAMES = 3

# One timeline load reads the patient's saved documents ONCE and hands each reader its own
# types, instead of every reader running the same three queries again. The held copy lives
# only inside `preloaded_documents` (one request, one reader) and is dropped on the way out.
_PRELOAD_KEY = "p3_saved_documents"


def document_types_for(sections: Iterable[str], *, owner: bool) -> tuple[str, ...]:
    """The document types the sections a reader may see are made of: prescription and lab
    documents come with their consent sections; vaccination records and other documents belong
    to the patient alone."""
    held = set(sections)
    types: list[str] = []
    if "prescriptions" in held:
        types.append(DOC_TYPE_RX)
    if "lab_reports" in held:
        types.append(DOC_TYPE_LAB)
    if owner:
        types += [DOC_TYPE_VACCINE, DOC_TYPE_OTHER]
    return tuple(types)


@asynccontextmanager
async def preloaded_documents(
    session: AsyncSession, patient: Patient, doc_types: tuple[str, ...]
) -> AsyncIterator[None]:
    """Read the saved documents of these types in one go; the readers below use that copy while
    the block runs. Nothing is returned to a caller before the readers (and, for a doctor,
    their access-log rows) have run, so reading ahead exposes nothing early; an exception
    leaves with the copy dropped."""
    saved = await doc_service.finalized_documents(session, patient, doc_types) if doc_types else []
    session.info[_PRELOAD_KEY] = (patient.id, frozenset(doc_types), saved)
    try:
        yield
    finally:
        session.info.pop(_PRELOAD_KEY, None)


async def _saved_documents(
    session: AsyncSession, patient: Patient, doc_types: tuple[str, ...]
) -> list[doc_service.FinalizedDocument]:
    held = session.info.get(_PRELOAD_KEY)
    if held is not None and held[0] == patient.id and set(doc_types) <= held[1]:
        return [fd for fd in held[2] if fd.doc.doc_type in doc_types]  # same newest-first order
    return await doc_service.finalized_documents(session, patient, doc_types)


def _facility(name: str | None, city: str | None) -> str | None:
    if not name:
        return None
    return f"{name}, {city}" if city else name


def _pick(
    items: list[tuple[str, str, bool]], *keys: str, not_in: frozenset[str] = frozenset()
) -> str | None:
    """The first non-empty, readable value among these keys of a parsed section, leaving out any
    value (compared without regard to case) in `not_in`."""
    for key in keys:
        for k, value, unreadable in items:
            if k == key and value and not unreadable and value.casefold() not in not_in:
                return value
    return None


def _document_record(
    fd: doc_service.FinalizedDocument, report: LabReport | None = None
) -> dict[str, Any]:
    doc, parsed = fd.doc, fd.parsed
    lab = doc.doc_type == DOC_TYPE_LAB
    # The reader takes a document's first line for its letterhead when nothing better is found,
    # so a page that opens with a medicine or a test can name that as its "hospital". A name the
    # patient confirmed as one of the document's OWN values cannot be where it was issued.
    own_values = frozenset(
        field.value.casefold() for row in fd.rows for field in row.fields if field.value
    )
    return {
        "origin": "document",
        "id": doc.id,
        "doc_type": doc.doc_type,
        # The date the document states; for a lab report whose own date was not read, the date its
        # structured `lab_report` row carries — never the day it was uploaded.
        "date": doc.document_date or (report.report_date if report is not None else None),
        "uploaded_at": doc.uploaded_at,
        "document_name": doc.original_filename,
        "encounter_id": doc.encounter_id or (report.encounter_id if report is not None else None),
        "authored_by_doctor": fd.source == doc_service.SOURCE_DOCTOR_GENERATED,
        "facility": _pick(
            parsed.facility if parsed else [],
            *(("lab_name", "hospital_name") if lab else ("hospital_name", "lab_name")),
            not_in=own_values,
        ),
        "doctor": _pick(parsed.provider if parsed else [], "doctor_name"),
        "title": _pick(parsed.report if parsed else [], "document_title"),
        "notes": list(parsed.notes) if parsed else [],
        "rows": [
            {
                **{field.name: field.value for field in row.fields},
                "flag": row.flag,
                "flag_source": row.flag_source,
            }
            for row in fd.rows
        ],
    }


# ── visits ───────────────────────────────────────────────────────────────
async def visits(session: AsyncSession, patient: Patient) -> list[dict[str, Any]]:
    # One query: each visit with its diagnoses (a visit with none still comes back, once).
    rows = (
        await session.execute(
            select(Encounter, Doctor.full_name, Clinic.name, Clinic.city, Diagnosis)
            .join(Doctor, Doctor.id == Encounter.doctor_id)
            .join(Clinic, Clinic.id == Encounter.clinic_id)
            .outerjoin(Diagnosis, Diagnosis.encounter_id == Encounter.id)
            .where(Encounter.patient_id == patient.id)
            .order_by(Encounter.visit_datetime.desc(), Encounter.id, Diagnosis.id)
        )
    ).all()
    visits_by_id: dict[uuid.UUID, dict[str, Any]] = {}
    for e, doctor_name, clinic_name, city, d in rows:
        visit = visits_by_id.setdefault(
            e.id,
            {
                "id": e.id,
                "at": e.visit_datetime,
                "doctor": doctor_name,
                "facility": _facility(clinic_name, city),
                "complaint": e.chief_complaint,
                "notes": e.clinical_notes,
                "ai_summary": e.ai_summary,
                "diagnoses": [],
            },
        )
        if d is not None:
            visit["diagnoses"].append(
                {
                    "code": d.icd10_code,
                    "description": d.description,
                    "severity": d.severity,
                    "chronic": d.is_chronic,
                }
            )
    return list(visits_by_id.values())


# ── prescriptions ────────────────────────────────────────────────────────
async def _prescription_rows(session: AsyncSession, patient: Patient) -> list[dict[str, Any]]:
    # One query: each prescription with its medicines (one with none still comes back, once).
    rows = (
        await session.execute(
            select(
                Prescription, Doctor.full_name, Clinic.name, Clinic.city, PrescriptionItem, Medicine
            )
            .join(Doctor, Doctor.id == Prescription.doctor_id)
            .join(Encounter, Encounter.id == Prescription.encounter_id)
            .join(Clinic, Clinic.id == Encounter.clinic_id)
            .outerjoin(PrescriptionItem, PrescriptionItem.prescription_id == Prescription.id)
            .outerjoin(Medicine, Medicine.id == PrescriptionItem.medicine_id)
            # A prescription written from a CONFIRMED DOCUMENT (`document_id` set, no visit) is a copy
            # of what that document already shows: the document stays the event, never listed twice.
            .where(Prescription.patient_id == patient.id, Prescription.document_id.is_(None))
            .order_by(Prescription.issue_date.desc(), Prescription.id, PrescriptionItem.id)
        )
    ).all()
    found: dict[uuid.UUID, dict[str, Any]] = {}
    for p, doctor_name, clinic_name, city, item, medicine in rows:
        record = found.setdefault(
            p.id,
            {
                "origin": "table",
                "id": p.id,
                "issue_date": p.issue_date,
                "source": p.source,
                "status": p.status,
                "doctor": doctor_name,
                "facility": _facility(clinic_name, city),
                "items": [],
            },
        )
        if item is not None and medicine is not None:
            record["items"].append(
                {
                    "medicine": medicine.brand_name or medicine.generic_name,
                    "strength": medicine.strength,
                    "dosage": item.dosage,
                    "frequency": item.frequency,
                    "duration_days": item.duration_days,
                    "route": item.route,
                    "instructions": item.instructions,
                }
            )
    return list(found.values())


async def _source_prescriptions(
    session: AsyncSession, saved: list[doc_service.FinalizedDocument]
) -> dict[uuid.UUID, set[uuid.UUID]]:
    """document id -> the prescriptions its cells became (`ocr_extraction.item_id` ->
    `prescription_item.prescription_id`). Empty unless another feature has linked them."""
    item_ids = {item for fd in saved for item in fd.item_ids}
    if not item_ids:
        return {}
    owner_of = dict(
        (
            await session.execute(
                select(PrescriptionItem.id, PrescriptionItem.prescription_id).where(
                    PrescriptionItem.id.in_(item_ids)
                )
            )
        ).all()
    )
    return {
        fd.doc.id: {owner_of[i] for i in fd.item_ids if i in owner_of}
        for fd in saved
        if fd.item_ids
    }


async def prescriptions(session: AsyncSession, patient: Patient) -> list[dict[str, Any]]:
    saved = await _saved_documents(session, patient, (DOC_TYPE_RX,))
    structured = await _prescription_rows(session, patient)
    known = {p["id"]: p for p in structured}
    source_of = await _source_prescriptions(session, saved)
    standalone: list[dict[str, Any]] = []
    for fd in saved:
        record = _document_record(fd)
        # Only a prescription of THIS patient's own list takes the document in: a link that
        # points anywhere else leaves the document as the event it is.
        merged_into = [known[pid] for pid in source_of.get(fd.doc.id, ()) if pid in known]
        if not merged_into:
            standalone.append(record)
            continue
        for prescription in merged_into:
            prescription.setdefault("documents", []).append(record)
    return structured + standalone


# ── lab reports ──────────────────────────────────────────────────────────
async def _lab_report_rows(
    session: AsyncSession, patient: Patient, doc_ids: list[uuid.UUID]
) -> dict[uuid.UUID, LabReport]:
    """document id -> its `lab_report` row, for this patient only."""
    if not doc_ids:
        return {}
    rows = await session.execute(
        select(LabReport).where(
            LabReport.patient_id == patient.id, LabReport.document_id.in_(doc_ids)
        )
    )
    return {r.document_id: r for r in rows.scalars().all()}


async def lab_reports(session: AsyncSession, patient: Patient) -> list[dict[str, Any]]:
    saved = await _saved_documents(session, patient, (DOC_TYPE_LAB,))
    reports = await _lab_report_rows(session, patient, [fd.doc.id for fd in saved])
    return [_document_record(fd, reports.get(fd.doc.id)) for fd in saved]


# ── the patient's own view only ──────────────────────────────────────────
async def other_documents(session: AsyncSession, patient: Patient) -> list[dict[str, Any]]:
    """Saved vaccination records and other documents. No consent section covers them, so only
    the patient is ever given these (`timeline/service.py` calls this for the owner alone)."""
    saved = await _saved_documents(session, patient, (DOC_TYPE_VACCINE, DOC_TYPE_OTHER))
    return [_document_record(fd) for fd in saved]


async def pending_documents(session: AsyncSession, patient: Patient) -> list[dict[str, Any]]:
    """Uploads that are not records yet (being read, waiting for the patient's review, or
    failed). Only what the upload itself is — never anything read from it. The patient's own
    view only."""
    return [
        {
            "id": d.id,
            "doc_type": d.doc_type,
            "status": d.status,
            "failure_code": d.failure_code,
            "name": d.name,
            "uploaded_at": d.uploaded_at,
        }
        for d in await doc_service.in_progress_documents(session, patient)
    ]


# ── the compact form a doctor's patient page lists ───────────────────────
# Data only, no words to translate: a name, a detail line, and a date.
def _names(items: list[str]) -> str:
    items = [i for i in items if i]
    shown = ", ".join(items[:_COMPACT_NAMES])
    extra = len(items) - _COMPACT_NAMES
    return f"{shown} +{extra}" if extra > 0 else shown


def compact_visits(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in records:
        named = [d.get("description") or d.get("code") or "" for d in r["diagnoses"]]
        first_line = (r.get("complaint") or "").strip().splitlines()[:1]
        out.append(
            {
                "name": _names(named) or (first_line[0] if first_line else r.get("doctor") or "—"),
                "detail": " · ".join(x for x in (r.get("doctor"), r.get("facility")) if x),
                "since": dbtypes.pk_date(r["at"]) if r.get("at") else None,
            }
        )
    return out


def compact_prescriptions(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in records:
        if r["origin"] == "document":
            names = [x.get("medicine") or "" for x in r["rows"]]
            when = r.get("date")
        else:
            names = [x.get("medicine") or "" for x in r["items"]]
            when = r.get("issue_date")
        out.append(
            {
                "name": _names(names) or "—",
                "detail": " · ".join(x for x in (r.get("doctor"), r.get("facility")) if x),
                "since": when,
            }
        )
    return out


def compact_lab_reports(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": _names([x.get("test_name") or "" for x in r["rows"]]) or r.get("title") or "—",
            "detail": " · ".join(x for x in (r.get("facility"), r.get("doctor")) if x),
            "since": r.get("date"),
        }
        for r in records
    ]
