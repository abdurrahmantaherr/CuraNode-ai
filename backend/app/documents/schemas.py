"""Request/response models for medical documents.

The API speaks in four friendly document types; the database stores the shared
schema's short `doc_type` codes. The mapping lives here and nowhere else.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from ..db.models import DOC_TYPE_LAB, DOC_TYPE_OTHER, DOC_TYPE_RX, DOC_TYPE_VACCINE

DocumentType = Literal["prescription", "lab_report", "vaccination", "other"]
DocumentStatusName = Literal[
    "uploaded", "processing", "awaiting_review", "finalized", "failed", "cancelled"
]

API_TO_DB_TYPE: dict[str, str] = {
    "prescription": DOC_TYPE_RX,
    "lab_report": DOC_TYPE_LAB,
    "vaccination": DOC_TYPE_VACCINE,
    "other": DOC_TYPE_OTHER,
}
DB_TO_API_TYPE = {v: k for k, v in API_TO_DB_TYPE.items()}
# `scan` is allowed by the shared schema but is not a type this feature offers.
DB_TO_API_TYPE.setdefault("scan", "other")


class FieldOut(BaseModel):
    name: str
    # What the OCR/extraction step proposed — never overwritten.
    extracted_value: str | None
    # What the patient changed it to (None = accepted as proposed, "" = cleared).
    corrected_value: str | None
    # The value that counts: the correction if there is one, else the proposal.
    value: str
    # A deterministic certainty CATEGORY from `normalization.py`
    # ("high"/"uncertain"/"ambiguous"/"missing"), not a calibrated model
    # probability. `None` for document types normalization doesn't run on
    # (lab, vaccination, other) or a row predating this column.
    confidence: float | None = None
    # "human_verified" once the patient has been through Confirm & Save for
    # this field (`reviewed_at` set), "ai_extracted" until then. Derived,
    # never stored separately.
    status: Literal["ai_extracted", "human_verified"] = "ai_extracted"


class RowOut(BaseModel):
    index: int
    fields: list[FieldOut]
    # A lab result's flag ("high" / "low" / "normal" / "abnormal" / "critical"), worked
    # out when the row is read and NEVER stored (see `lab_flags.py`): `flag_source` says
    # whether the report printed it ("printed") or it was compared with the stated
    # range ("calculated"). Both `None` for any other document type, or when there is
    # nothing trustworthy to say.
    flag: str | None = None
    flag_source: Literal["printed", "calculated"] | None = None


class GroupOut(BaseModel):
    """One TABLE of a document — its medicines, diagnosis, symptoms, vital signs,
    allergies, lab results or vaccine doses (see `groups.py`). `layout` is "table"
    (a row each) or "list" (a single row of labelled values, the vital signs)."""

    key: str
    field_names: list[str]
    layout: Literal["table", "list"]
    rows: list[RowOut]


class QualityOut(BaseModel):
    """How readable the uploaded photo or scan is (see `ocr.assess_quality`). `issues` holds
    any of "blurry", "faint", "low_resolution", "blank" — empty when the page looks fine.
    Advice for the patient, never a verdict on the document: nothing is blocked by it."""

    issues: list[str] = Field(default_factory=list)
    sharpness: float | None = None
    contrast: float | None = None


class SectionItem(BaseModel):
    key: str
    value: str
    # True only when a value WAS found but was too implausibly long for
    # this field to trust (see `sections._MAX_VALUE_LEN`) — `value` is
    # always "" in that case, never a guess. Additive: existing API
    # consumers reading only `key`/`value` are unaffected; a field that was
    # simply never present in the document still never appears at all.
    unreadable: bool = False


class SectionEntry(BaseModel):
    """One vaccine dose of a vaccination record: `values` holds only the columns
    the document actually showed (keys from `vaccines.ENTRY_KEYS`)."""

    values: dict[str, str]


class SectionTopic(BaseModel):
    """A headed passage of an "other" document ("Findings", "Procedure"...): `key` is one of
    `sections.TOPIC_KEYS`, `lines` the document's own words under that heading."""

    key: str
    lines: list[str]


class DocumentSectionsOut(BaseModel):
    """A document's OCR text organised for display — facility, patient,
    prescriber, the document's own dates, the clinical details (symptoms,
    diagnosis, vital signs), a vaccination record's doses, and any closing notes.
    Never a reviewable clinical field (those are `RowOut`s); a section with
    nothing found in it is simply empty, never padded."""

    facility: list[SectionItem] = Field(default_factory=list)
    patient: list[SectionItem] = Field(default_factory=list)
    provider: list[SectionItem] = Field(default_factory=list)
    report: list[SectionItem] = Field(default_factory=list)
    # Symptoms / complaint and diagnosis — the clinical reason for the document.
    clinical: list[SectionItem] = Field(default_factory=list)
    vitals: list[SectionItem] = Field(default_factory=list)
    # Vaccine doses, and which of `vaccines.ENTRY_KEYS` columns any dose uses.
    entries: list[SectionEntry] = Field(default_factory=list)
    entry_fields: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    topics: list[SectionTopic] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (
            self.facility
            or self.patient
            or self.provider
            or self.report
            or self.clinical
            or self.vitals
            or self.entries
            or self.notes
            or self.topics
        )


class DocumentOut(BaseModel):
    id: uuid.UUID
    document_type: DocumentType
    status: DocumentStatusName
    # Set when status == "failed" (e.g. "ocr_unavailable", "ocr_empty").
    failure_code: str | None
    original_filename: str | None
    mime_type: str | None
    size_bytes: int | None
    uploaded_at: datetime
    document_date: date | None
    finalized_at: datetime | None
    # What the signed-in owner may do with this document right now. Decided by the server
    # from its status and provenance (never by a client), so a page can show a control
    # only when the action would be accepted. Additive: nothing here is stored.
    can_retry: bool = False  # a failed read can be tried again
    can_delete: bool = False  # see `service.can_delete`
    can_download: bool = False  # the original is still held


class DocumentDetailOut(DocumentOut):
    """A document with its extracted rows.

    For a finalized document this is the finalized medical record — the one
    structure the document screens and (later) the Medical Timeline both read.
    `rows` then holds only the rows the patient kept.
    """

    # The raw OCR text (omitted from list responses). Always preserved
    # unchanged — `sections` below is only a better presentation of it.
    ocr_text: str | None = None
    # The field names this document type extracts, in display order.
    field_names: list[str]
    rows: list[RowOut]
    # Every table of the document, in display order (`rows` / `field_names` above are
    # its PRIMARY table — the medicines, the lab results, the vaccine doses). Empty in
    # list responses.
    groups: list[GroupOut] = Field(default_factory=list)
    # OCR text organised into named sections, shown while a document awaits
    # review and on its saved record (omitted from list responses).
    sections: DocumentSectionsOut | None = None
    # "patient_upload" (the patient's own scan/photo, reviewed by them) or
    # "doctor_generated" (authored directly by a verified doctor, never
    # patient-reviewed). Derived from `Document.uploaded_by` — no separate
    # provenance column; see `service._document_source`.
    source: Literal["patient_upload", "doctor_generated"] = "patient_upload"
    # Set while a document is awaiting review or failed: whether the photo is clear
    # enough to read well. `None` = not judged (a saved record, or a file that could
    # not be measured).
    quality: QualityOut | None = None


class ReviewedRow(BaseModel):
    """One row as the patient left it. Fields left out are treated as
    unchanged; a row left out entirely is treated as removed."""

    index: int = Field(ge=0, le=10_000)
    fields: dict[str, str | None] = Field(default_factory=dict)


class ConfirmRequest(BaseModel):
    """Confirm & Save. `document_date` omitted keeps the proposed date; `null`
    clears it."""

    rows: list[ReviewedRow] = Field(default_factory=list)
    document_date: date | None = None
    # The tables (`groups.py` keys) this request REVIEWS. For these, a row left out is a row
    # the patient removed. A table not named is left exactly as proposed (never marked
    # reviewed), so a client that only knows the medicines cannot erase the rest. Omitted
    # means just the document's primary table.
    groups: list[str] | None = None
    # `row_index` of medicine rows the patient ticked "add to my current medicines" on. Only a
    # kept row of the medicines table counts; an index that is anything else is ignored.
    current_medicines: list[Annotated[int, Field(ge=0, le=10_000)]] = Field(
        default_factory=list, max_length=100
    )


class DoctorMedicineIn(BaseModel):
    """One medicine as a verified doctor enters it directly — already
    structured, never OCR'd. Only `medicine` is required; a genuinely
    unspecified dosage/frequency/duration stays empty, exactly like a
    patient-upload row the extractor couldn't read a value for."""

    medicine: str = Field(min_length=1, max_length=255)
    dosage: str = Field(default="", max_length=255)
    frequency: str = Field(default="", max_length=255)
    duration: str = Field(default="", max_length=255)


class CreateDoctorPrescriptionRequest(BaseModel):
    """A verified doctor's structured prescription for one of their patients.

    No `doctor_id` field: the authoring doctor is always the authenticated
    caller (`VerifiedDoctorDep`), never a client-supplied value — the same
    rule that already keeps `patient_id` out of the patient's OWN upload
    requests, applied to whichever identity the endpoint must not let a
    caller spoof.
    """

    patient_id: uuid.UUID
    medicines: list[DoctorMedicineIn] = Field(min_length=1)
    notes: str = Field(default="", max_length=2000)
