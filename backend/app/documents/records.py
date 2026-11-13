"""Records from a confirmed document — what Confirm & Save also writes to the typed clinical tables.

What the patient confirmed stays, untouched, in `ocr_extraction` (the audit source: the reading beside
the patient's correction). On top of that, the typed tables get a COPY of the confirmed values that fit
them, so the record is queryable and reaches the places that read those tables:

    prescription + prescription_item   <- the confirmed medicines (prescription / other document)
    diagnosis                          <- the confirmed diagnoses
    vital_sign                         <- the confirmed vital signs, the six the table has columns for
    lab_report + lab_result            <- a lab report and its plain-number results

Rules this module keeps (each is tested):

* **Nothing is invented.** A row says where it came from: `document_id` set, no visit, no doctor.
  A `prescription` is `source = 'ocr_upload'`. The `medicine` catalogue is shared reference data and is
  only READ: an item links to a catalogue entry when the written name matches EXACTLY ONE of them
  (never a guess between two strengths); otherwise it keeps the name as written in `medicine_text`.
* **Nothing is silently changed.** A value that does not fit its column, or has no column
  (`respiratory_rate`, `bmi`, `blood_sugar`; a result such as "Negative" in a numeric column), is left
  out of the typed table and stays in `ocr_extraction` — it is never truncated or guessed. A dosage or
  frequency longer than its column goes whole into the item's `instructions`.
* **Units.** A temperature is stored in °C and a weight in kg because the columns say so; an explicit
  `F` / `lb` is converted, and a bare number is accepted only where it can mean one thing (a human
  temperature is 30-45 °C or 90-110 °F, never both). A bare weight, whose unit cannot be known, is
  not stored.
* **Flags.** `lab_result.flag` takes only low / normal / high. It is stored ONLY when the report
  printed its own marker beside the result (and the result was not edited afterwards): a flag this
  program calculated is shown on the page, never written to a record, because a scan that dropped a
  decimal point makes a calculated flag confidently wrong.
* **All or nothing, and once.** `write` adds every row in the caller's transaction; the caller commits
  or rolls back. A document that already has records is left alone (`has_records`), so a retry can
  never double them. Deleting a document removes exactly these rows (`remove_for_document`).
* **Free text and values are clinical text**: nothing here logs them.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import ROUND_HALF_UP, Decimal
from time import monotonic

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import (
    DOC_TYPE_LAB,
    DOC_TYPE_OTHER,
    DOC_TYPE_RX,
    Diagnosis,
    LabReport,
    LabResult,
    Medicine,
    OcrExtraction,
    Prescription,
    PrescriptionItem,
    VitalSign,
)
from ..db.types import pk_date, uuid7
from . import groups, lab_flags
from .sections import DocumentSections

# ── Is the database ready for this? ──────────────────────────────────────
# Migration c8e5a41f9b27 adds the columns `write` / `remove_for_document` use. The shared database is
# migrated by its owner, not by the app, so this code can be running before that has happened. Until it
# has, nothing here touches the new columns and the app behaves exactly as it did before; the moment the
# migration is applied the copying switches on by itself (no restart: a "not ready" answer is only
# remembered for a few seconds, a "ready" one for the life of the process).
_NEEDED = (
    ("prescription", "document_id"),
    ("diagnosis", "document_id"),
    ("vital_sign", "document_id"),
    ("prescription_item", "medicine_text"),
)
_NOT_READY_FOR_S = 15.0
_probe_state: dict[str, float | bool] = {"ready": False, "asked_at": -1e9}


def reset_readiness() -> None:
    """Forget what was learned about the database (tests; a process that is told to re-check)."""
    _probe_state.update(ready=False, asked_at=-1e9)


async def is_ready(session: AsyncSession) -> bool:
    """Whether the migration's columns exist. One query on PostgreSQL, and only until the answer is
    yes."""
    if _probe_state["ready"]:
        return True
    if monotonic() - float(_probe_state["asked_at"]) < _NOT_READY_FOR_S:
        return False
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        wanted = " OR ".join(
            f"(table_name = '{table}' AND column_name = '{column}')" for table, column in _NEEDED
        )
        found = (
            await session.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    f"WHERE table_schema = ANY (current_schemas(false)) AND ({wanted})"
                )
            )
        ).scalar_one()
        ready = found == len(_NEEDED)
    else:  # SQLite (the tests)
        ready = True
        for table, column in _NEEDED:
            rows = (await session.execute(text(f"PRAGMA table_info({table})"))).all()
            ready = ready and any(row[1] == column for row in rows)
    _probe_state.update(ready=ready, asked_at=monotonic())
    return ready


SOURCE = "ocr_upload"  # prescription.source for a prescription read from an uploaded document
LAB_STATUS = "received"  # lab_report.status — the table's own default for a report received
ITEM_DOSAGE_MAX = ITEM_FREQUENCY_MAX = 50  # prescription_item.dosage / frequency columns
RESULT_UNIT_MAX = 30  # lab_result.unit
NAME_MAX = 255  # medicine_text, lab_result.test_name, lab_report.lab_name, diagnosis.description
_STORED_FLAGS = {lab_flags.HIGH, lab_flags.LOW, lab_flags.NORMAL}  # lab_result_flag_check


# ── Parsing: pure functions ──────────────────────────────────────────────
_PLAIN_NUMBER = re.compile(r"^\d{1,3}(?:,\d{3})+(?:\.\d+)?$|^\d+(?:\.\d+)?$")


def plain_decimal(text: str) -> Decimal | None:
    """A plain number — thousands separators allowed (`150,000`) — or `None`. `Positive`, `<0.5`,
    `12 mg` and `1,5` are not plain numbers."""
    cleaned = text.strip()
    if not cleaned or len(cleaned) > 24 or not _PLAIN_NUMBER.match(cleaned):
        return None
    return Decimal(cleaned.replace(",", ""))


_DURATION = re.compile(
    r"^(?:for\s+)?(\d{1,3})\s*(days?|d|weeks?|wks?|w|months?|mos?)\.?$", re.IGNORECASE
)
_DURATION_DAYS = {"d": 1, "w": 7, "m": 30}


def duration_days(text: str) -> int | None:
    """`5 days` -> 5, `2 weeks` -> 14, `1 month` -> 30. Only a number WITH a unit: a bare `5` could
    be anything, so it is not turned into days."""
    m = _DURATION.match(text.strip())
    if not m:
        return None
    days = int(m.group(1)) * _DURATION_DAYS[m.group(2).lower()[0]]
    return days if 0 < days <= 3650 else None


_BP = re.compile(r"^\s*(\d{2,3})\s*/\s*(\d{2,3})\s*(?:mm\s*hg|mmhg)?\s*$", re.IGNORECASE)


def blood_pressure(text: str) -> tuple[int, int] | None:
    """`120/80` -> (120, 80). The limits are wide on purpose (BP 280/160 must show); a pair the
    wrong way round or off the scale is not stored."""
    m = _BP.match(text)
    if not m:
        return None
    systolic, diastolic = int(m.group(1)), int(m.group(2))
    if 60 <= systolic <= 300 and 30 <= diastolic <= 200 and systolic > diastolic:
        return systolic, diastolic
    return None


_PULSE = re.compile(
    r"^\s*(\d{2,3})\s*(?:bpm|/\s*min|per\s*min(?:ute)?|beats/min)?\s*$", re.IGNORECASE
)


def pulse_bpm(text: str) -> int | None:
    m = _PULSE.match(text)
    return int(m.group(1)) if m and 20 <= int(m.group(1)) <= 300 else None


_SPO2 = re.compile(r"^\s*(\d{2,3})\s*%?\s*$")


def spo2_percent(text: str) -> int | None:
    m = _SPO2.match(text)
    return int(m.group(1)) if m and 50 <= int(m.group(1)) <= 100 else None


_TEMPERATURE = re.compile(
    r"^\s*(\d{2,3}(?:\.\d+)?)\s*(?:°|º|deg(?:rees?)?)?\s*([CF])?\s*$", re.IGNORECASE
)


def _one_place(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def temperature_c(text: str) -> Decimal | None:
    """Degrees Celsius. `37.2`, `37 C` -> as is; `98.6 F` -> 37.0; a bare number only where it can
    mean one thing (30-45 is °C, 90-110 is °F)."""
    m = _TEMPERATURE.match(text)
    if not m:
        return None
    value, unit = Decimal(m.group(1)), (m.group(2) or "").upper()
    if unit == "F" or (not unit and 90 <= value <= 110):
        value = (value - 32) * 5 / 9
    elif unit != "C" and not (30 <= value <= 45):
        return None
    value = _one_place(value)
    return value if 30 <= value <= 45 else None


_WEIGHT = re.compile(
    r"^\s*(\d{1,3}(?:\.\d+)?)\s*(kgs?|kilograms?|lbs?|pounds?)\.?\s*$", re.IGNORECASE
)


def weight_kg(text: str) -> Decimal | None:
    """Kilograms, from a weight that PRINTS its unit. A bare number is not stored: 70 could be
    kilograms or pounds, and the table cannot be told which."""
    m = _WEIGHT.match(text)
    if not m:
        return None
    value = Decimal(m.group(1))
    if m.group(2).lower().startswith(("lb", "pound")):
        value = value * Decimal("0.45359237")
    value = _one_place(value)
    return value if 2 <= value <= 400 else None


_HEIGHT_CM = re.compile(r"^\s*(\d{2,3}(?:\.\d+)?)\s*(cm|centimet(?:er|re)s?)?\s*$", re.IGNORECASE)
_HEIGHT_M = re.compile(r"^\s*(\d(?:\.\d+)?)\s*m(?:et(?:er|re)s?)?\s*$", re.IGNORECASE)
_HEIGHT_FT = re.compile(
    r"""^\s*(\d)\s*(?:'|ft|feet|foot)\s*(?:(\d{1,2})\s*(?:"|in|inch(?:es)?)?)?\s*$""", re.IGNORECASE
)


def height_cm(text: str) -> Decimal | None:
    """Centimetres, from `170 cm`, `1.7 m`, `5'7"` / `5 ft 7 in`, or a bare number that can only be
    centimetres (100-250)."""
    value: Decimal | None = None
    if m := _HEIGHT_FT.match(text):
        inches = int(m.group(1)) * 12 + int(m.group(2) or 0)
        value = Decimal(inches) * Decimal("2.54")
    elif m := _HEIGHT_M.match(text):
        value = Decimal(m.group(1)) * 100
    elif m := _HEIGHT_CM.match(text):
        number = Decimal(m.group(1))
        if m.group(2) or 100 <= number <= 250:
            value = number
    if value is None:
        return None
    value = _one_place(value)
    return value if 40 <= value <= 250 else None


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().lower()


def fit_to_column(value: str, limit: int, label: str, spill: list[str]) -> str | None:
    """`value` if it fits its column; else nothing in the column and the whole value in `spill`
    (it ends up in the item's instructions) — a value is never cut to fit."""
    if not value:
        return None
    if len(value) <= limit:
        return value
    spill.append(f"{label}: {value}")
    return None


# ── What the writer is given ─────────────────────────────────────────────
@dataclass(frozen=True)
class DocumentFacts:
    """The few things about a confirmed document that records are written from — plain values,
    taken before anything can expire them (a rollback elsewhere in Confirm & Save expires every
    ORM object in the session)."""

    id: uuid.UUID
    patient_id: uuid.UUID
    doc_type: str
    document_date: date | None
    uploaded_at: datetime
    finalized_at: datetime | None
    ocr_text: str | None


@dataclass
class RowData:
    """One confirmed row: its table, what the patient left in each field, the id of the stored cell
    of each field (so a medicine's cells can be linked to the item written from it), and which
    fields the patient corrected (a printed lab marker is trusted only beside a result nobody
    edited)."""

    index: int
    group: str
    values: dict[str, str]
    cell_ids: dict[str, uuid.UUID] = field(default_factory=dict)
    edited: frozenset[str] = frozenset()


@dataclass(frozen=True)
class RecordsOutcome:
    """What `write` stored — counts only, never a value."""

    prescriptions: int = 0
    prescription_items: int = 0
    diagnoses: int = 0
    vital_measurements: int = 0
    lab_reports: int = 0
    lab_results: int = 0
    lab_skipped: int = 0  # lab results with no plain number: they stay in `ocr_extraction`
    failed: bool = False
    # The database does not have migration c8e5a41f9b27 yet: nothing was attempted.
    unavailable: bool = False

    @property
    def stored(self) -> bool:
        return bool(
            self.prescription_items
            or self.diagnoses
            or self.vital_measurements
            or self.lab_reports
            or self.lab_results
        )


def lab_name_of(parsed: DocumentSections | None) -> str | None:
    """The laboratory's name as the document's letterhead gives it, if it does."""
    if parsed is None:
        return None
    named = {key: value for key, value, unreadable in parsed.facility if value and not unreadable}
    name = named.get("lab_name") or named.get("hospital_name")
    return name[:NAME_MAX] if name else None


def _recorded_at(doc: DocumentFacts) -> datetime:
    """When the measurements were taken: the day the document states (at noon in Pakistan, so the
    Pakistan day is that day whatever zone it is read in), else when it was saved."""
    day = doc.document_date
    if day is None:
        return doc.finalized_at or doc.uploaded_at
    return datetime.combine(day, time(7, 0), tzinfo=UTC)  # 12:00 in Pakistan (UTC+5)


# ── Writing ──────────────────────────────────────────────────────────────
async def _catalogue_matches(session: AsyncSession, names: Iterable[str]) -> dict[str, uuid.UUID]:
    """Written name (normalised) -> the ONE catalogue medicine whose generic or brand name it
    equals. A name that matches several entries (one brand in two strengths) matches none."""
    keys = {_norm(n) for n in names if n.strip()}
    if not keys:
        return {}
    found = await session.execute(
        select(Medicine.id, Medicine.generic_name, Medicine.brand_name).where(
            or_(
                func.lower(Medicine.generic_name).in_(keys),
                func.lower(Medicine.brand_name).in_(keys),
            )
        )
    )
    hits: dict[str, set[uuid.UUID]] = defaultdict(set)
    for medicine_id, generic, brand in found.all():
        for label in (generic, brand):
            if label and _norm(label) in keys:
                hits[_norm(label)].add(medicine_id)
    return {key: next(iter(ids)) for key, ids in hits.items() if len(ids) == 1}


async def has_records(session: AsyncSession, document_id: uuid.UUID) -> bool:
    """Whether this document already has rows in the typed tables."""
    for model in (Prescription, Diagnosis, VitalSign, LabReport):
        if await session.scalar(select(model.id).where(model.document_id == document_id).limit(1)):
            return True
    return False


async def write(
    session: AsyncSession,
    *,
    doc: DocumentFacts,
    rows: list[RowData],
    parsed: DocumentSections | None,
) -> RecordsOutcome:
    """Add the typed rows for a confirmed document to the caller's transaction (no commit).
    Does nothing if the document already has records."""
    if doc.doc_type not in (DOC_TYPE_RX, DOC_TYPE_OTHER, DOC_TYPE_LAB):
        return RecordsOutcome()  # a vaccination record has no table of its own here
    if not await is_ready(session):
        return RecordsOutcome(unavailable=True)
    if await has_records(session, doc.id):
        return RecordsOutcome()

    by_group: dict[str, list[RowData]] = defaultdict(list)
    for row in rows:
        by_group[row.group].append(row)

    outcome = RecordsOutcome()
    if doc.doc_type in (DOC_TYPE_RX, DOC_TYPE_OTHER):
        items = await _write_prescription(session, doc, by_group.get(groups.MEDICINES.key, []))
        diagnoses = _write_diagnoses(session, doc, by_group.get(groups.DIAGNOSES.key, []))
        vitals = _write_vitals(session, doc, by_group.get(groups.VITALS.key, []))
        outcome = RecordsOutcome(
            prescriptions=1 if items else 0,
            prescription_items=items,
            diagnoses=diagnoses,
            vital_measurements=vitals,
        )
    else:  # a lab report
        written, skipped = await _write_lab(
            session, doc, by_group.get(groups.RESULTS.key, []), parsed
        )
        outcome = RecordsOutcome(lab_reports=1, lab_results=written, lab_skipped=skipped)
    await session.flush()
    return outcome


async def _write_prescription(
    session: AsyncSession, doc: DocumentFacts, rows: list[RowData]
) -> int:
    rows = [r for r in rows if r.values.get("medicine", "").strip()]
    if not rows:
        return 0
    matches = await _catalogue_matches(session, (r.values["medicine"] for r in rows))
    prescription = Prescription(
        id=uuid7(),
        patient_id=doc.patient_id,
        encounter_id=None,
        doctor_id=None,
        document_id=doc.id,
        issue_date=doc.document_date or pk_date(doc.uploaded_at),
        source=SOURCE,
        status="active",
    )
    session.add(prescription)
    await session.flush()  # the prescription exists before its items do
    made: list[tuple[RowData, PrescriptionItem]] = []
    for row in rows:
        name = row.values["medicine"].strip()
        spill: list[str] = []
        days_text = row.values.get("duration", "").strip()
        days = duration_days(days_text) if days_text else None
        if days_text and days is None:
            spill.append(f"Duration: {days_text}")
        item = PrescriptionItem(
            id=uuid7(),
            prescription_id=prescription.id,
            medicine_id=matches.get(_norm(name)),
            medicine_text=name[:NAME_MAX],
            dosage=fit_to_column(
                row.values.get("dosage", "").strip(), ITEM_DOSAGE_MAX, "Dosage", spill
            ),
            frequency=fit_to_column(
                row.values.get("frequency", "").strip(), ITEM_FREQUENCY_MAX, "Frequency", spill
            ),
            duration_days=days,
            instructions="; ".join(spill) or None,
        )
        session.add(item)
        made.append((row, item))
    await session.flush()  # the items exist before the cells that point at them
    for row, item in made:
        if row.cell_ids:
            await session.execute(
                update(OcrExtraction)
                .where(OcrExtraction.id.in_(list(row.cell_ids.values())))
                .values(item_id=item.id)
            )
    return len(made)


def _write_diagnoses(session: AsyncSession, doc: DocumentFacts, rows: list[RowData]) -> int:
    count = 0
    for row in rows:
        text = row.values.get("diagnosis", "").strip()
        if not text:
            continue
        session.add(
            Diagnosis(
                id=uuid7(),
                encounter_id=None,
                document_id=doc.id,
                description=text[:NAME_MAX],
                is_chronic=False,
            )
        )
        count += 1
    return count


def _write_vitals(session: AsyncSession, doc: DocumentFacts, rows: list[RowData]) -> int:
    """One `vital_sign` row from the single vital-signs row, carrying only the measurements that
    have a column AND read cleanly. Returns how many measurements were stored."""
    if not rows:
        return 0
    values = rows[0].values
    fields: dict[str, object] = {}
    if bp := blood_pressure(values.get("blood_pressure", "")):
        fields["bp_systolic"], fields["bp_diastolic"] = bp
    if (pulse := pulse_bpm(values.get("pulse", ""))) is not None:
        fields["pulse_bpm"] = pulse
    if (temperature := temperature_c(values.get("temperature", ""))) is not None:
        fields["temperature_c"] = temperature
    if (oxygen := spo2_percent(values.get("spo2", ""))) is not None:
        fields["spo2"] = oxygen
    if (weight := weight_kg(values.get("weight", ""))) is not None:
        fields["weight_kg"] = weight
    if (height := height_cm(values.get("height", ""))) is not None:
        fields["height_cm"] = height
    if not fields:
        return 0
    session.add(
        VitalSign(
            id=uuid7(),
            encounter_id=None,
            document_id=doc.id,
            recorded_at=_recorded_at(doc),
            **fields,
        )
    )
    # blood pressure is one measurement in two columns
    return len({"bp" if k.startswith("bp_") else k for k in fields})


def _stored_flag(ocr_text: str | None, row: RowData) -> str | None:
    """The report's OWN marker (low / normal / high), or nothing. Never a calculated one."""
    if not ocr_text or "result" in row.edited:
        return None
    marker = lab_flags.printed_flag(
        ocr_text,
        row.values.get("test_name", ""),
        row.values.get("result", ""),
        row.values.get("unit", ""),
    )
    return marker if marker in _STORED_FLAGS else None


async def _write_lab(
    session: AsyncSession,
    doc: DocumentFacts,
    rows: list[RowData],
    parsed: DocumentSections | None,
) -> tuple[int, int]:
    report = LabReport(
        id=uuid7(),
        patient_id=doc.patient_id,
        encounter_id=None,
        document_id=doc.id,
        lab_name=lab_name_of(parsed),
        report_date=doc.document_date,
        status=LAB_STATUS,
    )
    session.add(report)
    await session.flush()  # the report exists before its results do
    written = skipped = 0
    for row in rows:
        name = row.values.get("test_name", "").strip()
        if not name:
            continue
        value = plain_decimal(row.values.get("result", ""))
        if value is None:
            skipped += 1  # "Negative", "<0.5", ...: the numeric column cannot hold it
            continue
        low, high = lab_flags.reference_limits(row.values.get("reference_range", ""))
        unit = row.values.get("unit", "").strip()
        session.add(
            LabResult(
                id=uuid7(),
                report_id=report.id,
                test_name=name[:NAME_MAX],
                value=value,
                unit=unit if 0 < len(unit) <= RESULT_UNIT_MAX else None,
                ref_range_low=Decimal(str(low)) if low is not None else None,
                ref_range_high=Decimal(str(high)) if high is not None else None,
                flag=_stored_flag(doc.ocr_text, row),
            )
        )
        written += 1
    return written, skipped


# ── Removing ─────────────────────────────────────────────────────────────
async def remove_for_document(session: AsyncSession, document_id: uuid.UUID) -> None:
    """Delete every row `write` made for this document, in the caller's transaction. Rows are
    removed child first (results, items), and the OCR cells that pointed at an item are released
    before it goes. Records that are not this document's — a doctor's, a visit's — are never
    selected: every statement is keyed on `document_id`. Only ever called for a document the patient
    uploaded themself (the only kind they may delete), so a `lab_report` keyed on it is theirs.
    On a database without the migration no such row can exist, so there is nothing to remove."""
    if not await is_ready(session):
        return
    prescription_ids = select(Prescription.id).where(Prescription.document_id == document_id)
    item_ids = select(PrescriptionItem.id).where(
        PrescriptionItem.prescription_id.in_(prescription_ids)
    )
    await session.execute(
        update(OcrExtraction).where(OcrExtraction.item_id.in_(item_ids)).values(item_id=None)
    )
    await session.execute(
        delete(PrescriptionItem).where(PrescriptionItem.prescription_id.in_(prescription_ids))
    )
    await session.execute(delete(Prescription).where(Prescription.document_id == document_id))
    report_ids = select(LabReport.id).where(LabReport.document_id == document_id)
    await session.execute(delete(LabResult).where(LabResult.report_id.in_(report_ids)))
    await session.execute(delete(LabReport).where(LabReport.document_id == document_id))
    await session.execute(delete(Diagnosis).where(Diagnosis.document_id == document_id))
    await session.execute(delete(VitalSign).where(VitalSign.document_id == document_id))
