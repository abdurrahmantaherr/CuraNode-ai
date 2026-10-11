"""The proposed rows of a document's OTHER tables.

`extractors.py` reads the medicines and the lab results. Everything else a patient
reviews — diagnosis, symptoms, vital signs, allergies, vaccine doses — is read from
the document's sections (`sections.py`: deterministic, with the optional AI reading
merged over it) and turned into rows here, ready to be stored as `ocr_extraction`
proposals. Pure functions, no database.

A proposal is never trusted: it is shown to the patient in an editable table and only
Confirm & Save makes it part of their record. What this module never does is invent —
a diagnosis, a symptom or an allergy appears only if the document wrote it.
"""

from __future__ import annotations

import re

from . import groups, sections, vitals

_LIST_SPLIT = re.compile(r"\s*[;|]\s*|\n+")
_SYMPTOM_SPLIT = re.compile(r"\s*[,;|]\s*|\n+")
_MAX_ITEM_LEN = 120


def _items(value: str, splitter: re.Pattern[str], limit: int) -> list[str]:
    seen: list[str] = []
    for part in splitter.split(value):
        item = part.strip(" \t.-")
        if item and item.casefold() not in {s.casefold() for s in seen}:
            seen.append(item[:_MAX_ITEM_LEN])
        if len(seen) >= limit:
            break
    return seen


def _report_value(parsed: sections.DocumentSections, key: str) -> str:
    return next((v for k, v, unreadable in parsed.report if k == key and not unreadable), "")


def _vitals_row(parsed: sections.DocumentSections) -> dict[str, str]:
    found = {k: v for k, v, unreadable in parsed.vitals if not unreadable}
    return {name: found.get(name, "") for name in vitals.VITAL_KEYS}


def extra_rows(
    doc_type: str,
    text: str,
    ai_reading: object = None,
    *,
    skip: frozenset[str] = frozenset(),
) -> list[tuple[groups.GroupSpec, list[dict[str, str]]]]:
    """`(group, rows)` for each of the document type's groups that is NOT read by
    `extractors.py` (or whose key is in `skip`), in display order. A table with
    nothing found comes back with no rows, so it is simply absent from the
    proposals — the patient can still add a row — while the single vital-signs row is
    always there (its fields empty when nothing was found), so every vital can be
    filled in."""
    parsed = sections.apply_ai_sections(sections.structure_document(text, doc_type), ai_reading)
    out: list[tuple[groups.GroupSpec, list[dict[str, str]]]] = []
    for spec in groups.groups_for(doc_type):
        if spec.from_extractor or spec.key in skip:
            continue
        rows: list[dict[str, str]]
        if spec is groups.VACCINES:
            rows = [dict(entry) for entry in parsed.entries]
        elif spec is groups.DIAGNOSES:
            rows = [
                {"diagnosis": item}
                for item in _items(_report_value(parsed, "diagnosis"), _LIST_SPLIT, spec.max_rows)
            ]
        elif spec is groups.SYMPTOMS:
            rows = [
                {"symptom": item}
                for item in _items(
                    _report_value(parsed, "complaint"), _SYMPTOM_SPLIT, spec.max_rows
                )
            ]
        elif spec is groups.VITALS:
            rows = [_vitals_row(parsed)]
        elif spec is groups.ALLERGIES:
            rows = [dict(entry) for entry in parsed.allergies]
        else:  # pragma: no cover - every non-extractor group is handled above
            rows = []
        out.append((spec, rows[: spec.max_rows]))
    return out
