# Shared RAG Foundation — Implementation Plan

**Status:** Planning draft — repository audit pending  
**Branch for these planning documents:** `feature/rag-foundation-docs`  
**Integration flow:** `feature/* → dev → main`  
**Scope:** Shared foundation first, then P6 and P7 adapters. This plan alone does not authorize code or database changes.

## 1. Guiding approach
Build one reusable RAG foundation and integrate two separate consumers: P6 Lab Report Management & AI Interpretation, then P7 Multilingual Routine Query Chatbot. Keep retrieval/generation primitives shared, while keeping lab-specific validation and chatbot intent routing in their respective adapters. Reuse existing CuraNode-AI components rather than creating parallel auth, OCR, provider, or audit implementations.

## 2. Proposed architecture

```text
Authenticated request
        |
Authorization / ownership / consent checks
        |
Feature adapter
  |                         |
  P6 lab adapter            P7 intent router
  |                         |
Authorized structured SQL   +-- approved knowledge question
for finalized lab values    |       -> Shared RAG service
  |                         |
  |                         +-- patient-specific question
  |                         |       -> authorized structured SQL
  |                         |       -> approved knowledge retrieval if needed
  |                         |
  |                         +-- appointments / queue / live operation
  |                                 -> existing live backend function
  |
Evidence/context assembly where applicable
        |
Approved LLM provider abstraction (only after privacy/provider review)
        |
Output validation, citations, safety handling
        |
Response + minimized audit event
```

Knowledge documents and patient/operational records are separate data paths. A vector index is not the system of record for patient results or live appointments.

## 3. Phase 0 — Repository and environment audit (first; no code changes)
1. Confirm local working tree, current branch, remotes, `dev` and `main` state, and existing uncommitted changes.
2. Map FastAPI routes, services, dependency injection, configuration, error handling, tests, and migration tooling.
3. Inspect migration history and actual database schema in a safe/read-only manner; confirm extensions, RLS, indexes, and migration head. Do not apply a migration to the shared database.
4. Trace authentication, patient/doctor/clinic authorization, consent grant/revocation, and audit logging.
5. Inspect existing AI provider adapters, secrets/configuration, prompts, timeouts, retries, error handling, and provider data terms.
6. Audit P6 OCR/extraction/review/finalization; identify extracted versus corrected/finalized values and existing flags/reference data.
7. Audit P7 routes, current intents, language handling, and existing provider/RAG logic.
8. Run existing tests and record the baseline; a pre-existing failure is not automatically a RAG regression.
9. Record findings and blockers in `exec.md`; update this plan if the repository differs from assumptions.

**Exit condition:** reviewed map of reusable components, security boundaries, data sources, and a safe development/testing setup.

## 4. Phase 1 — Finalize design decisions
Based on audit results, decide and record:
- Existing database/vector/keyword search capabilities and whether pgvector or schema changes are needed.
- Approved knowledge-source list, source owner, versioning, and update process.
- Embedding model selected from a benchmark using English, Urdu script, Roman Urdu, and mixed-language examples.
- Chunking and metadata schema based on actual source documents.
- Provider selection/reuse with privacy/data-handling approval.
- Service interface, API contracts, configuration, timeouts, and failure modes.
- Whether reranking is needed based on measured retrieval quality.
- Safe dev database/test fixtures and migration approval process.

Do not commit to pgvector, a new hosted provider, or a final embedding model solely because it appears as a candidate in the spec.

## 5. Phase 2 — Knowledge ingestion and indexing
1. Define a normalized source/document representation and metadata.
2. Implement ingestion for approved documents only.
3. Preserve source identity, title, section, language, version, effective date, and approval/active state.
4. Use structure-aware chunking and retain heading/provenance with each chunk.
5. Add deduplication, source retirement, and safe re-indexing/versioning.
6. Generate embeddings using one configured model/version. If E5 is selected, apply its documented query/passage prefixes consistently.
7. Build lexical and vector search using approved datastore capabilities.
8. Ensure index updates cannot silently expose retired or unapproved material.
9. Add tests for parsing, chunk boundaries, metadata, deduplication, indexing, and re-indexing.

Initial chunk ranges in `spec.md` are experimental starting points; tune using retrieval tests.

## 6. Phase 3 — Shared retrieval and answer service
1. Implement a shared service boundary for query normalization, retrieval scope, hybrid candidate retrieval, merge/deduplication, ranking, and context-budget control.
2. Require explicit scope and metadata filters; enforce private-data authorization outside vector search as well.
3. Return evidence objects with source/chunk IDs and metadata, not untraceable text.
4. Add relevance gating and configurable abstention/clarification behavior.
5. Integrate the approved existing LLM provider abstraction only after privacy review.
6. Constrain prompts to supplied evidence; treat documents and user content as untrusted.
7. Validate citations and key facts; preserve numbers, units, dates, and medicine names.
8. Add timeout, provider error, empty retrieval, conflicting evidence, and malformed output handling.
9. Add minimized, useful audit events.
10. Test components and full pipeline with synthetic/non-sensitive fixtures.

Initial experiment only: up to 10 vector and 10 keyword candidates, merged/deduplicated, then roughly 3–6 passages if relevant and within the context budget. Tune on evaluation data. Do not copy an arbitrary distance threshold from a tutorial/demo.

## 7. Phase 4 — Evaluation and security gate
Create a versioned test set containing:
- Questions with known relevant passages and expected source IDs.
- English, Urdu script, Roman Urdu, and mixed-language queries.
- Synonyms, abbreviations, spelling variation, transliteration, and typos.
- No-answer, ambiguous, outdated, and conflicting-source questions.
- Synthetic lab cases with missing units, unknown ranges, corrected values, and conflicting values.
- Prompt injection in user prompts and retrieved content.
- Unauthorized patient/clinic access and consent-revocation scenarios.
- Provider timeout and service-unavailable scenarios.

Measure Recall@k, citation support, abstention, per-language quality, latency, key-value fidelity, and authorization outcomes. Record dataset size and limitations. Thresholds in `spec.md` are targets to evaluate, not current results or evidence of clinical safety.

## 8. Phase 5 — Integrate P6 (separate feature branch)
1. Start from the shared foundation merged into `dev`, or update the P6 branch from the agreed integration base.
2. Reuse the existing OCR pipeline; do not create another OCR system.
3. Confirm that only reviewed/finalized lab values can be used as confirmed values.
4. Fetch patient values through authorized structured queries; do not retrieve them from a global vector index.
5. Retrieve approved explanations/reference material for the relevant test, including source and applicability limitations.
6. Generate a bounded explanation that does not diagnose or prescribe.
7. Verify values, units, reference ranges, and citations against underlying records.
8. If data is unconfirmed, missing, contradictory, or lacks necessary unit/range context, state the limitation and follow the designed review/escalation flow.
9. Add tests for corrected-vs-extracted values, finalized status, authorization, citations, number/unit fidelity, and abstention.

## 9. Phase 6 — Integrate P7 (separate feature branch)
1. Start from the shared foundation merged into `dev`, or update the P7 branch from the agreed integration base.
2. Route general health-knowledge questions to shared approved-source RAG.
3. Route patient-specific questions through authorization and structured data access, using RAG only for approved explanatory knowledge as needed.
4. Route appointments, queue status, and other live operational questions to existing backend functions.
5. For medication-start/change requests, do not produce prescriptions; follow the approved safety and clinician-escalation policy.
6. Support English, Urdu script, Roman Urdu, and mixed language; preserve clinical facts and ask for clarification when needed.
7. Test intent routing, multilingual queries, private-data access, live data, prompt injection, no-evidence abstention, and provider failures.

## 10. Phase 7 — Integration, review, and release
1. Merge feature work into `dev` through pull requests and required checks.
2. Run regression tests for authentication, consent, patient data, P6, P7, and shared RAG.
3. Verify migrations against the approved development database before any shared database change. Obtain explicit approval before applying shared schema migrations.
4. Review provider privacy, logging, source approval, clinical safety wording, and failure behavior with project/advisor stakeholders.
5. Document limitations, configuration, source maintenance, re-indexing, and rollback.
6. Merge `dev` into `main` only after required review and acceptance criteria are met.

## 11. Branching proposal
- `feature/rag-foundation-docs`: this planning-document change only.
- `feature/rag-foundation`: shared ingestion/retrieval/generation implementation, created after audit and design review.
- `feature/p6-lab-interpretation`: P6 adapter and workflow integration.
- `feature/p7-chatbot`: P7 adapter and intent routing.
- `dev`: integration and regression testing.
- `main`: stable, reviewed code.

P6 and P7 should not independently implement duplicate retrieval pipelines. Coordinate feature branches through PRs to avoid divergent assumptions.

## 12. Definition of done
- Requirements in `spec.md` are implemented or explicitly deferred with reasons.
- Automated tests cover core logic, data separation, authorization, citations, multilingual behavior, and failure modes.
- Evaluation results are reproducible and reported per language and scenario.
- No known cross-patient exposure exists in the defined security test suite.
- No shared database migration has been applied without approval.
- `exec.md` records actual commands/results, decisions, commits/PRs, unresolved issues, and deviations from this plan.
