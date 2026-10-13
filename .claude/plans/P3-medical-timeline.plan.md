# P3 — Medical History Timeline: implementation plan (FR8, FR9)

Branch: `feature/medical-history-timeline` (from `dev` @ `0027b80`). Nothing committed. This plan was
written after inspecting the PRD, TDD, ERD, the workflow diagram, the UI/UX prototype artifact and the
code, and **updates** the first implementation (kept, extended — never replaced).

## 1. What already existed before this iteration (uncommitted, from the first P3 pass)

| Area | State |
|---|---|
| Data | `encounter`, `diagnosis`, `medicine`, `prescription`, `prescription_item` mapped read-only; no migration |
| Backend | `timeline/{schemas,events,sources,service,router}.py`; patient and doctor JSON API; pages in `web/timeline.py` |
| Access | one path through `consent/gateway.py` (`purpose="timeline_view"`, one `access_log` row per section a doctor reads, identical 404 for every refusal) |
| Documents | only **finalized** documents are events; a patient's upload counts only rows with `reviewed_at`; awaiting-review uploads were a *count* in a banner |
| Filters | tabs All / Consultations / Labs / Prescriptions; From / To; hospital; doctor. **No search. No Vaccinations / Other Medical Documents tabs. No pending-document items.** |
| UI | date in a left column, pill tabs, kind tag on every card |
| Tests | 78 timeline tests; full suite 1895 passed, 1 pre-existing failure (`test_schedule_schemas[24:00]`, Python 3.14, also fails on a clean `origin/dev`) |

## 2. Findings that drive the design

* **ERD (32 entities) has no treatment, procedure or vaccination entity.** The only vaccination basis
  is `document.doc_type = 'vaccine'`; there is nothing for a "Treatments / Procedures" category to be
  built from. See conflict C1.
* **Stable links the schema offers** (verified read-only against `information_schema`, 2026-10-12):
  `lab_report.document_id` (NOT NULL → every lab report is backed by a document),
  `lab_report.encounter_id`, `document.encounter_id`, `ocr_extraction.item_id → prescription_item`.
  Nothing in this repo writes them (the doctor-side / P6 features will), but they define what
  "the same record" means, so duplicate prevention keys on them — never on names or dates.
* `document.doc_type` allows `rx, lab, vaccine, other, scan`; this app writes the first four.
* The OCR pipeline, confirmation flow and storage are **not touched**: the timeline reads around them.
* Figma: the Figma Dev Mode MCP server is not enabled in this environment and no Figma link exists in
  the repo or the brief, so no Figma comparison is possible. The reference used is the **UI/UX
  Prototype artifact** (`pt-timeline` screen): tabs, a rail with the date above each card, provenance
  label top-right of the card, "View details →".

## 3. Conflicts and decisions

* **C1 — Treatments / Procedures category.** Requested; unsupported by ERD/PRD/schema. Decision: **no
  tab, no fabricated category.** Proposed solutions (need the schema owner / product decision):
  (a) a `procedure` entity linked to `encounter`; or (b) a sub-classification of `other` documents.
  (b) was rejected for now: deriving it from OCR text would label unverified text as a procedure.
* **C2 — Consultation *documents*.** There is no consultation document type; consultations come from
  `encounter`. Decision: Consultations = `encounter` rows (doctor side). Documents never become
  consultations by guesswork.
* **C3 — Vaccination / other documents are the patient's alone** (no consent section covers them —
  `consent/schemas.SECTIONS` has no such section). Decision: unchanged; the two tabs are hidden for a
  doctor. Sharing them would need a new consent section (privacy spec change).
* **C4 — Allergies / conditions / medicines the patient lists** have no tab in the brief. Decision:
  category `profile`, reachable under **All** only (never under Other Medical Documents).
* **C5 — Hiding an event from a timeline** is not supported by the data model (no hidden flag, no
  audit table for it). Decision: not implemented; documented (§8).

## 4. Design

**Categories** (backend-validated, `type=` parameter): `consultation`, `lab`, `prescription`,
`vaccination`, `document` (Other Medical Documents), `profile` (All only). Mapping: `encounter` →
consultation; `prescription` rows, prescription documents, patient-listed medicines → prescription;
lab documents → lab; `vaccine` documents → vaccination; `other`/`scan` documents → document.

**Automatic integration of uploads (no manual step).** The timeline is *derived on read* from the
documents tables — no second table, no sync job, nothing to go stale.
* `finalized` → an event (clinical date = `document.document_date`, falling back to
  `lab_report.report_date` for a lab document; never the upload day).
* `uploaded` / `processing` / `awaiting_review` / `failed` → a **pending item** (owner only), in its
  own "Documents in progress" section with the existing status chip and a link to the review page.
  A pending item shows **only** type, file name, upload day and status — never extracted values.
  It is not an event: no clinical date, not matched by a date range, never counted as history.
* `cancelled` (deleted / discarded) → nothing. The file link disappears with it.

**Duplicate prevention (by stable id).**
1. A document referenced by a `lab_report` row **is** that lab event (the `lab_report` row never yields
   a second event); the row only contributes `encounter_id` and a fallback `report_date`.
2. A document whose `ocr_extraction.item_id` points at a `prescription_item` is the source of that
   `prescription`: one event (the structured prescription), the document is listed as its
   attachment, not as a second event.
3. `document.encounter_id` / `lab_report.encounter_id` link the document to a consultation: both stay
   (different things), each shows the relationship ("Part of consultation on …" / "Attached
   documents") when the reader can see both. Nothing is merged by name or date similarity.

**Search** (`q`): case-insensitive, whitespace-normalised, every term must match; over the title,
summary, doctor, facility and **all detail content** the reader is allowed to see (medicines, tests and
results, diagnoses, notes, advice) plus — for the owner only — the document file name. Runs on the
events the reader is already authorised to receive, so unauthorised data is never loaded to be
filtered. A doctor's search cannot reach file names (not exposed to doctors).

**Dates**: inclusive both ends, Pakistan calendar day (UTC+5, no DST); `From > To` is a 422 on the API
and a visible message on the page (nothing applied); an event with no clinical date is excluded by any
date range, never given a date.

**Deletion / updates / status changes**: events are read live, so a deleted document disappears,
a confirmed one appears, an edited date moves, and a corrected value shows — with no write path in the
timeline. Deleting a document never touches `encounter`, `diagnosis`, `prescription`, `lab_report` or
profile rows.

## 5. Steps and status

- [x] 1. Inspect repo, Git state, docs, ERD, prototype, live schema (read-only)
- [x] 2. This plan
- [x] 3. Models: `Document.encounter_id`, `OcrExtraction.item_id` (read-only mappings), `LabReport`
- [x] 4. Documents service: `in_progress_documents`, `item_ids` on `FinalizedDocument` (read helpers only;
      136 lines added, none removed — upload / OCR / review / confirm / delete untouched)
- [x] 5. Timeline sources: lab_report link, prescription-source link, pending uploads
- [x] 6. Events: categories, search, relations, pending items
- [x] 7. Service / API / pages: `q`, new categories, pending section, validation, detail reads related sections
- [x] 8. UI per prototype; i18n (en + ur, parity enforced); loading state (`timeline.js`)
- [x] 9. Tests: `test_timeline.py` (46), `test_timeline_web.py` (32), `test_timeline_documents.py` (54)
- [x] 10. Full suite, lint, format, PostgreSQL 16 check, visual check, docs

## 5b. Verification (2026-10-12)

| Check | Result |
|---|---|
| Full suite | 1949 passed, 3 skipped, 1 failed |
| The failure | `test_schedule_schemas.py::test_times_must_be_whole_minutes_hh_mm[24:00]` — fails identically on a clean export of `HEAD` (none of this work); Python 3.14 parses `24:00` as midnight. Not P3. |
| `ruff check` / `ruff format --check` | clean |
| `alembic heads` | one head, `291c06a2b86f` (no migration) |
| PostgreSQL 16 (throwaway) | links, pending, search, categories, date range, doctor view: same results as SQLite |
| Prototype comparison | measured computed styles of the prototype's `pt-timeline` screen vs the page: typography, tab underline, rail, dot, border and the 640px card width equal; card radius (token 14px vs 12px), shadow token and the "View details" colour differ on purpose. **Not pixel-perfect and not claimed to be.** Figma: not reachable (Dev Mode MCP server not enabled, no file link). |

## 5c. Iteration 3 — round trips and the delete wording (2026-10-12)

Measured first (`SELECT` / `INSERT` statements per load, in-memory SQLite, 28 events): patient **18**, doctor **23**,
with the same three document queries repeated for prescriptions, labs and other documents.

- Saved documents are read once per load, with their OCR job (`sources.preloaded_documents`), and handed to each
  reader by type; a visit + its diagnoses and a prescription + its medicines are one joined query each.
- Result: patient **9**, doctor **17**, independent of the number of documents (`tests/test_timeline_roundtrips.py`).
- The consent gateway (3 access queries + 1 `access_log` insert per section for a doctor) is not touched.
- Not done, on purpose: a SQL view / DB-side paging (needs a migration; only if a real-database timing asks), and a
  stored timeline table (second source of truth).
- Deleting a document: behaviour unchanged (its event and links go; no clinical record is touched); the
  confirmation now says so, and that profile entries it created stay.

## 6. Out of scope (per the brief)

Doctor-side scanned-prescription upload; any change to OCR internals, extraction or the confirm flow;
treatments/procedures (C1); hiding events (C5); AI lab interpretation (`ai_interpretation`, P6).

## 7. Risks

* The three link columns are unwritten today, so their handling is covered by seeded-row tests only.
* Search is evaluated in Python over the authorised events (the events come from several tables);
  fine for the NFR2 target (100 records) — revisit with SQL-side search if a timeline grows into the
  thousands.

## 8. Record management (what the timeline does and does not do)

| Action | Where | Timeline's role |
|---|---|---|
| Delete an original uploaded document | Documents page (patient's own uploads only, with confirmation; audited `document.delete`) | the event disappears; clinical records untouched |
| Correct clinical information | review step before saving (patient), profile page (patient's own entries); doctor-side records belong to the doctor | none — read-only |
| Hide / remove an event from a timeline | **not supported** (no schema for it, no audit trail) | not offered |
