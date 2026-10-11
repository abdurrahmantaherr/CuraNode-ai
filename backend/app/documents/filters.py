"""Search and filters for a patient's document list — one parser for the JSON API and the pages.

What can be searched is only what the `document` row itself holds: the original file name, the
document type, the lifecycle status, and a date. (The facility, doctor and patient names a
document carries are read from its OCR text each time a page is built — see `sections.py` — and
are deliberately not searchable, since that would mean reading every document's text on every
search.)

    q            part of the file name — or of a document type's name ("lab" finds lab reports)
    type         prescription | lab_report | vaccination | other
    status       uploaded | processing | awaiting_review | failed | finalized
    date_from    the document's own date, or the day it was uploaded when it has none ...
    date_to      ... between these two days, inclusive

Filters combine (all of them must hold). A value that is not valid is REPORTED, never guessed at:
the API answers 422 with the offending fields; a page shows what was ignored and lists the rest.
Scoping to the signed-in patient is NOT done here — the caller's own `patient_id` is added by
`service.py` to every query, exactly as for every other read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import Select, func, or_

from ..db.models import Document
from ..db.sqlfuncs import pk_date
from ..i18n.catalogue import translate
from .schemas import API_TO_DB_TYPE

QUERY_MAX = 100
# A document type is matched by name only for a search of at least this many characters; one or
# two letters would match every type.
TYPE_NAME_MIN = 3
# The statuses a patient can filter on. `cancelled` is a discarded upload: it is never listed.
STATUSES = ("uploaded", "processing", "awaiting_review", "failed", "finalized")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class DocumentFilters:
    query: str = ""
    document_type: str | None = None  # a `document.doc_type` value
    status: str | None = None
    date_from: date | None = None
    date_to: date | None = None

    @property
    def active(self) -> bool:
        return bool(
            self.query or self.document_type or self.status or self.date_from or self.date_to
        )

    def only(self, *, status_in: tuple[str, ...]) -> DocumentFilters | None:
        """These filters for a list that can only hold documents with a status in `status_in`:
        `None` when the status filter rules every one of them out (a search for "failed" has
        nothing to say about saved records), otherwise the filters unchanged."""
        if self.status is not None and self.status not in status_in:
            return None
        return self


def _clean_query(raw: str | None) -> str:
    return _SPACES.sub(" ", _CONTROL.sub(" ", raw or "")).strip()[:QUERY_MAX]


def _parse_date(raw: str | None) -> tuple[date | None, bool]:
    """`(date, ok)`: blank is fine (no filter); anything but a real `YYYY-MM-DD` is not."""
    value = (raw or "").strip()
    if not value:
        return None, True
    if not _ISO_DATE.match(value):
        return None, False
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None, False
    return (parsed, True) if parsed.year >= 1900 else (None, False)


def parse(
    *,
    query: str | None = None,
    document_type: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> tuple[DocumentFilters, dict[str, str]]:
    """The valid part of what was asked for, and `{field: message key}` for the rest."""
    problems: dict[str, str] = {}

    db_type: str | None = None
    if document_type:
        db_type = API_TO_DB_TYPE.get(document_type)
        if db_type is None:
            problems["type"] = "documents.filter.invalid_type"

    chosen_status: str | None = None
    if status:
        if status in STATUSES:
            chosen_status = status
        else:
            problems["status"] = "documents.filter.invalid_status"

    start, start_ok = _parse_date(date_from)
    end, end_ok = _parse_date(date_to)
    if not start_ok:
        problems["date_from"] = "documents.filter.invalid_date"
    if not end_ok:
        problems["date_to"] = "documents.filter.invalid_date"
    if start and end and start > end:
        problems["date_to"] = "documents.filter.range_invalid"
        start = end = None

    return (
        DocumentFilters(
            query=_clean_query(query),
            document_type=db_type,
            status=chosen_status,
            date_from=start,
            date_to=end,
        ),
        problems,
    )


def type_matches(query: str) -> frozenset[str]:
    """The `doc_type` values whose NAME contains `query`, in either language."""
    needle = query.casefold()
    if len(needle) < TYPE_NAME_MIN:
        return frozenset()
    return frozenset(
        db_type
        for api_type, db_type in API_TO_DB_TYPE.items()
        for locale in ("en", "ur")
        if needle in translate(f"documents.type.{api_type}", locale).casefold()
    )


def _like_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def apply(stmt: Select, filters: DocumentFilters) -> Select:  # type: ignore[type-arg]
    """`stmt` (a SELECT over `Document`) narrowed by every filter that is set."""
    if filters.document_type:
        stmt = stmt.where(Document.doc_type == filters.document_type)
    if filters.status:
        stmt = stmt.where(Document.status == filters.status)
    if filters.query:
        clauses = [Document.original_filename.ilike(_like_pattern(filters.query), escape="\\")]
        if types := type_matches(filters.query):
            clauses.append(Document.doc_type.in_(sorted(types)))
        stmt = stmt.where(or_(*clauses))
    # The date a patient thinks of as "the document's": the one written on it, else the day
    # it was uploaded IN PAKISTAN (an unconfirmed upload has a proposed date or none yet).
    effective = func.coalesce(Document.document_date, pk_date(Document.uploaded_at))
    if filters.date_from:
        stmt = stmt.where(effective >= filters.date_from)
    if filters.date_to:
        stmt = stmt.where(effective <= filters.date_to)
    return stmt
