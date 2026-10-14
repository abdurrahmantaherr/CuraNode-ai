"""The shapes of the medical timeline (P3 — FR8, FR9).

An event is one thing that happened to the patient's record, placed on the date it happened
(a visit's date, a prescription's issue date, a document's own date) — never on the day it was
uploaded. Every event says WHERE IT CAME FROM (`provenance`), because a timeline mixes things a
doctor wrote with things a patient typed and things a program read off a paper and the patient
then confirmed; nothing here is ever shown as more certain than that.

Text the reader sees (`title`, `summary`, block headings and labels) is already in the caller's
language; the `*_key` fields carry the catalogue key for a client that wants to translate itself.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

# What kind of record an event is — each one backed by a real record type or document
# classification, never invented:
#   consultation  a doctor's visit (`encounter`)
#   lab           a lab report document            prescription  a prescription, or a medicine
#   vaccination   a vaccination record document                  the patient lists
#   document      any other medical document       profile       an allergy / documented condition
# There is no treatment or procedure record in the data model (ERD), so there is no such category.
Category = Literal["consultation", "lab", "prescription", "vaccination", "document", "profile"]
CATEGORIES: tuple[str, ...] = (
    "consultation",
    "lab",
    "prescription",
    "vaccination",
    "document",
    "profile",
)
# The tabs shown, in order (All is the first, with no category). `profile` entries are under All only.
TAB_CATEGORIES: tuple[str, ...] = (
    "consultation",
    "lab",
    "prescription",
    "vaccination",
    "document",
)
# No consent section covers these two, so only the patient's own timeline ever holds them.
OWNER_ONLY_CATEGORIES: tuple[str, ...] = ("vaccination", "document")

# What the thing is, which decides where its details are read from:
#   encounter      a doctor visit (the shared `encounter` table)
#   prescription   a doctor's prescription (the shared `prescription` table)
#   rx_document    a saved prescription DOCUMENT (a patient's upload, or one a doctor wrote)
#   lab_document   a saved lab report document
#   document       a saved vaccination record or other document (the patient's own view only)
#   allergy / condition / medication   a profile entry
Kind = Literal[
    "encounter",
    "prescription",
    "rx_document",
    "lab_document",
    "document",
    "allergy",
    "condition",
    "medication",
]
KINDS: tuple[str, ...] = (
    "encounter",
    "prescription",
    "rx_document",
    "lab_document",
    "document",
    "allergy",
    "condition",
    "medication",
)

# Who vouches for it. `ai_patient_confirmed`: a program read it off a paper and the patient
# reviewed and saved it — real, but not a clinician's word. Anything still awaiting that review
# is not on the timeline at all.
Provenance = Literal[
    "doctor_confirmed",
    "patient_entered",
    "ai_patient_confirmed",
    "clinician_recorded",
    "source_unknown",
]

# `clinical`: the date the thing happened (or the document states). `recorded`: the day it was
# written into the record, because no event date exists (an allergy, a condition with no onset).
# `unknown`: no date at all — such an event is listed last, apart from the dated ones.
DateBasis = Literal["clinical", "recorded", "unknown"]


class Fact(BaseModel):
    label_key: str
    label: str
    value: str


class Cell(BaseModel):
    value: str
    # A lab result's flag (high / low / normal / abnormal / critical / check), for styling.
    flag: str | None = None
    # Said under a flag worked out by the program rather than printed on the report.
    note: str | None = None


class DetailBlock(BaseModel):
    """One part of an event's detail page: a list of facts, a table, or a paragraph."""

    kind: Literal["facts", "table", "text"]
    heading_key: str
    heading: str
    facts: list[Fact] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Cell]] = Field(default_factory=list)
    text: str | None = None
    # True for text a model wrote (a visit's AI summary): it is labelled as such, never merged
    # into what the doctor wrote.
    ai_generated: bool = False


class Related(BaseModel):
    """Another event this one is linked to by a real relationship (never by a similar name or
    date): a document that belongs to a visit, or the visit a document belongs to. Both events
    stay — they are different records — and each says how it relates to the other."""

    ref: str
    relation: Literal["part_of", "has_attachment"]
    title: str
    event_date: date | None = None


class TimelineEvent(BaseModel):
    ref: str  # "<kind>:<id>" — what the detail address is built from
    kind: Kind
    category: Category
    event_date: date | None
    date_basis: DateBasis
    occurred_at: datetime | None = None  # the time of day, when the source has one (a visit)
    title_key: str
    title_params: dict[str, str] = Field(default_factory=dict)
    title: str = ""
    summary: list[str] = Field(default_factory=list)
    doctor: str | None = None
    facility: str | None = None
    provenance: Provenance
    provenance_label: str = ""
    # The patient's own view only: the saved document this event comes from, so the page can
    # link to the existing document screen. Never given to a doctor.
    document_id: uuid.UUID | None = None
    uploaded_on: date | None = None
    # The patient's own view only: the name the saved file was uploaded under (searchable there).
    document_name: str | None = None
    related: list[Related] = Field(default_factory=list)


class TimelineDetail(TimelineEvent):
    blocks: list[DetailBlock] = Field(default_factory=list)


class PendingItem(BaseModel):
    """An upload that is not part of the history yet. It carries what the upload IS (type, name,
    when it was uploaded, where it stands) and nothing read from it: until the patient has
    reviewed and saved it, no extracted value is a clinical fact. Never given to a doctor, and
    never matched by a date range (it has no clinical date)."""

    id: uuid.UUID
    category: Category
    document_type: str  # prescription | lab_report | vaccination | other
    status: str  # uploaded | processing | awaiting_review | failed
    status_label: str
    name: str | None = None
    uploaded_on: date | None = None
    message: str | None = None  # why a failed one failed


class TimelineOut(BaseModel):
    items: list[TimelineEvent]
    total: int  # events matching every filter, before paging
    limit: int
    offset: int  # where this page starts (a web page past the end is moved back to the last one)
    # How many events each tab would show, with every filter EXCEPT the tab applied.
    counts: dict[str, int]
    # The values the facility / doctor filters can take, from the whole unfiltered timeline.
    facilities: list[str]
    doctors: list[str]
    undated: int  # events with no date at all (listed last)
    # The patient's own view only: uploads that are not history yet (being read, waiting for the
    # patient's review, failed), after the category and search filters. Empty for a doctor.
    pending: list[PendingItem] = Field(default_factory=list)
    # ... and how many uploads wait for the patient's review, before any filter. `None` for a doctor.
    pending_review: int | None = None
