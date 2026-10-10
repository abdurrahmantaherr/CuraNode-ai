"""The reviewable TABLES of a document — one pipeline, several groups of rows.

A medicine list was once the only thing a patient reviewed. A real prescription also
carries a diagnosis, symptoms, vital signs and allergies; a lab report its results; a
vaccination card its doses. Each is a GROUP of rows in the same shared
`ocr_extraction` table — a row being the fields that share one `row_index`, a group
being the field names that belong together. The field names of one group never
appear in another, so a row's group is always recoverable from its field names alone.

This module is a leaf (it imports nothing from the pipeline) and is the single
source of truth for which field names exist: the migration that widens
`ocr_extraction.field_name`'s CHECK is tested against `ALL_FIELDS`, so the code and
the database cannot drift apart.

Nothing here knows how a value is READ from a document — that is `extractors.py`
(medicines, lab results) and `proposals.py` (everything else).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..db.models import DOC_TYPE_LAB, DOC_TYPE_OTHER, DOC_TYPE_RX, DOC_TYPE_VACCINE

# Every table owns a block of `row_index` values, so a client that adds a medicine at
# "the next index" can never land on a row of another table. A document type has only ONE
# table in the first block (its primary one), so those never collide with each other.
INDEX_BLOCK = 1000

TABLE = "table"  # many rows, a column per field, one removable row each
LIST = "list"  # ONE row, every field shown as a labelled input (vital signs)


@dataclass(frozen=True)
class GroupSpec:
    key: str
    fields: tuple[str, ...]
    # The field that must be non-empty for a row to count as a real entry. `None`
    # when no single field defines the row (a vital-signs row may have any of its).
    required: str | None
    layout: str = TABLE
    # Whether the extractor module reads this group (medicines, lab results);
    # every other group is proposed from the document's sections (`proposals.py`).
    from_extractor: bool = False
    max_rows: int = 40
    # The first `row_index` of this table's block (see INDEX_BLOCK).
    index_base: int = 0


MEDICINES = GroupSpec(
    "medicines",
    ("medicine", "dosage", "frequency", "duration"),
    required="medicine",
    from_extractor=True,
    max_rows=100,
)
RESULTS = GroupSpec(
    "results",
    ("test_name", "result", "unit", "reference_range"),
    required="test_name",
    from_extractor=True,
    max_rows=100,
)
VACCINES = GroupSpec(
    "vaccines",
    ("vaccine_name", "dose", "date_given", "batch_no", "site", "next_due"),
    required="vaccine_name",
)
DIAGNOSES = GroupSpec(
    "diagnoses", ("diagnosis",), required="diagnosis", max_rows=10, index_base=1 * INDEX_BLOCK
)
SYMPTOMS = GroupSpec(
    "symptoms", ("symptom",), required="symptom", max_rows=15, index_base=2 * INDEX_BLOCK
)
VITALS = GroupSpec(
    "vitals",
    (
        "blood_pressure",
        "pulse",
        "temperature",
        "respiratory_rate",
        "spo2",
        "weight",
        "height",
        "bmi",
        "blood_sugar",
    ),
    required=None,
    layout=LIST,
    max_rows=1,
    index_base=3 * INDEX_BLOCK,
)
ALLERGIES = GroupSpec(
    "allergies",
    ("allergy_substance", "allergy_reaction", "allergy_severity"),
    required="allergy_substance",
    max_rows=10,
    index_base=4 * INDEX_BLOCK,
)

# Per document type, in the order they are shown and in which `row_index` values are
# handed out. The FIRST group of a type is its primary one (what `DocumentDetailOut.rows`
# and `field_names` have always meant). An "other" medical document is read like a
# prescription: medicines (there may be none), diagnosis, symptoms, vital signs, allergies.
GROUPS: dict[str, tuple[GroupSpec, ...]] = {
    DOC_TYPE_RX: (MEDICINES, DIAGNOSES, SYMPTOMS, VITALS, ALLERGIES),
    DOC_TYPE_LAB: (RESULTS,),
    DOC_TYPE_VACCINE: (VACCINES,),
    DOC_TYPE_OTHER: (MEDICINES, DIAGNOSES, SYMPTOMS, VITALS, ALLERGIES),
}
PRIMARY: dict[str, GroupSpec | None] = {
    DOC_TYPE_RX: MEDICINES,
    DOC_TYPE_LAB: RESULTS,
    DOC_TYPE_VACCINE: VACCINES,
    DOC_TYPE_OTHER: MEDICINES,
}

# The fewest rows a CONFIRMED document must keep in a table, by document type: a prescription
# without a medicine, or a lab report without a result, is not a record. Everywhere else 0 — an
# "other" document or a vaccination card with nothing readable can still be saved.
MIN_ROWS: dict[tuple[str, str], int] = {
    (DOC_TYPE_RX, MEDICINES.key): 1,
    (DOC_TYPE_LAB, RESULTS.key): 1,
}

ALL_GROUPS: tuple[GroupSpec, ...] = (
    MEDICINES,
    RESULTS,
    VACCINES,
    DIAGNOSES,
    SYMPTOMS,
    VITALS,
    ALLERGIES,
)
BY_KEY: dict[str, GroupSpec] = {g.key: g for g in ALL_GROUPS}
ALL_FIELDS: frozenset[str] = frozenset(f for g in ALL_GROUPS for f in g.fields)
_GROUP_OF_FIELD: dict[str, GroupSpec] = {f: g for g in ALL_GROUPS for f in g.fields}

# A rule worth stating once: no field name may belong to two groups.
assert len(_GROUP_OF_FIELD) == sum(len(g.fields) for g in ALL_GROUPS)
# `ocr_extraction.field_name` is varchar(20).
assert all(len(f) <= 20 for f in ALL_FIELDS)


def group_of(field_names: set[str] | frozenset[str]) -> GroupSpec | None:
    """The group whose fields these are — `None` if any name is unknown or the names
    span more than one group."""
    groups = {_GROUP_OF_FIELD.get(name) for name in field_names}
    if not field_names or None in groups or len(groups) != 1:
        return None
    return next(iter(groups))


def row_in_block(spec: GroupSpec, index: int) -> bool:
    """Whether `index` lies in `spec`'s own block of row indexes."""
    return spec.index_base <= index < spec.index_base + INDEX_BLOCK


def groups_for(doc_type: str) -> tuple[GroupSpec, ...]:
    return GROUPS[doc_type]


def fields_for(doc_type: str) -> frozenset[str]:
    return frozenset(f for g in GROUPS[doc_type] for f in g.fields)


def min_rows(doc_type: str, spec: GroupSpec) -> int:
    """How many rows a confirmed `doc_type` document must keep in `spec`'s table."""
    return MIN_ROWS.get((doc_type, spec.key), 0)
