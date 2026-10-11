# Shared RAG Foundation — Specification

**Status:** Draft — requirements captured; repository audit and technical decisions pending  
**Scope:** Shared Retrieval-Augmented Generation (RAG) services for CuraNode-AI, initially supporting P6 Lab Report Management & AI Interpretation and P7 Multilingual Routine Query Chatbot.

## 1. Purpose
Provide a reusable, secure, evidence-grounded retrieval and generation foundation so P6 and P7 can use approved health information without building separate RAG pipelines. This specification describes required behavior, not a claim that the functionality already exists.

## 2. Goals
- Retrieve relevant information from a curated, approved knowledge collection.
- Support English, Urdu script, and Roman Urdu, with quality measured separately for each.
- Return evidence-linked answers and abstain or ask for clarification when evidence is insufficient.
- Reuse existing authentication, authorization, consent, audit, provider, and OCR capabilities where appropriate after repository audit.
- Keep approved knowledge separate from private patient records and live operational data.
- Give P6 and P7 a common interface while allowing feature-specific safety and workflow rules.

## 3. Out of scope
- Diagnosing a condition or independently prescribing, starting, stopping, or changing treatment.
- Replacing clinicians, laboratory review, or emergency services.
- Embedding all patient records into a global/shared vector index.
- Replacing the existing OCR pipeline with a second OCR implementation.
- Treating retrieved text as executable instructions or as an authorization source.
- Applying database migrations or changing the live/shared Supabase schema before review and approval.
- Claiming clinical safety from retrieval benchmarks alone.

## 4. Consumers
- **P6 adapter:** explains finalized, authorized lab results using approved reference material; it must not independently diagnose.
- **P7 adapter:** answers routine multilingual questions using approved knowledge, authorized structured patient data when permitted, and live backend functions for operational tasks.
- **Knowledge maintainer/administrator:** approves sources, controls publication and retirement, and maintains metadata. The responsible role and approval workflow must be confirmed.

## 5. Functional requirements

### 5.1 Approved knowledge lifecycle
- Ingest only approved sources and record source identity, title, publisher/owner, language, version, publication/effective date where available, approval status, and ingestion timestamp.
- Normalize documents without silently changing clinical meaning.
- Split content using document structure and meaning-aware boundaries; preserve headings and provenance.
- Deduplicate content and support source updates, retirement, and re-indexing.
- Store the embedding model/version used for each index generation. Do not mix incompatible embedding spaces.
- Keep original language content and verified translations linked to the same source when available. Machine translations must not be represented as clinically verified unless reviewed.

### 5.2 Retrieval and answer generation
- Provide a shared retrieval interface with metadata filters and access scope.
- Support hybrid semantic/vector and lexical/keyword retrieval if the audit confirms a suitable implementation.
- Merge and deduplicate candidates, rank evidence, enforce a configurable context budget, and retain source/chunk identifiers.
- Generate answers only from supplied evidence and explicitly authorized data.
- Cite sources for substantive health-information claims in a consistent, inspectable format.
- If evidence is missing, conflicting, stale, out of scope, or below a validated relevance threshold, state the limitation, ask a clarifying question, or abstain.
- Treat retrieved documents and user inputs as untrusted; ignore instructions embedded in documents that attempt to override policy, access controls, or tool restrictions.
- Validate output for unsupported claims, source references, language, and preservation of clinically important numbers, units, dates, and medicine names.

### 5.3 Separate data paths
- Approved general medical knowledge: knowledge retrieval.
- Patient-specific values, history, allergies, medications, and other protected records: authorized structured queries or a separately approved patient-scoped retrieval design.
- Appointments, queue positions, bookings, and other changing operational facts: authenticated live backend functions, not stale knowledge chunks.
- Unfinalized OCR output: not eligible as a confirmed lab result.

### 5.4 Multilingual behavior
- Accept English, Urdu script, Roman Urdu, and reasonable mixed-language questions.
- Preserve original evidence and source language metadata.
- Evaluate retrieval and response quality independently for each supported language.
- Preserve numbers, dates, units, abbreviations, medicine names, and reference ranges.
- Ask for clarification or abstain when transliterated meaning is uncertain.
- Do not assume translated answers are clinically equivalent without evaluation.

### 5.5 Integration contracts
- Expose a stable internal service interface so P6 and P7 reuse ingestion, retrieval, context assembly, generation, citations, and validation.
- Keep P6-specific lab rules and P7-specific intent routing outside generic retrieval logic.
- Reuse existing provider abstractions if suitable; do not hard-code a new provider before audit.
- Provider calls must have explicit timeout, error, and safe-fallback behavior.

## 6. Security, privacy, and safety
- Authenticate and authorize the requester before accessing private data or performing patient-, doctor-, or clinic-scoped retrieval.
- Enforce patient ownership, role permissions, clinic boundaries, and applicable consent/revocation rules before data reaches retrieval or a model.
- Never rely on vector search filters alone as the security boundary; enforce authorization in trusted backend code and database policies as applicable.
- Do not send identifiable or sensitive health data to a hosted model until data minimization, provider terms, retention, regional/privacy requirements, and project approval are confirmed. Use approved non-sensitive knowledge or synthetic data for early tests.
- Do not expose one patient's records, chunks, citations, or metadata to another user.
- Log only necessary audit metadata (for example request ID, source IDs, model/provider, and validation outcome); minimize sensitive prompt/response retention.
- Treat retrieved text as untrusted and defend against prompt injection, data exfiltration instructions, and tool misuse.
- Preserve safety messaging and escalation policy once approved by the project/clinical advisor. Emergency guidance must be explicitly reviewed.
- Fail closed on authorization uncertainty.

## 7. Initial design hypotheses — not final decisions
These are starting points for experiments and must be validated against the repository, data, and evaluation set:
- Database/vector store: investigate existing PostgreSQL/Supabase capabilities and pgvector plus full-text search before choosing or migrating.
- Embedding candidates: `intfloat/multilingual-e5-base` and `BAAI/bge-m3`; benchmark English, Urdu script, Roman Urdu, and mixed-language queries.
- Initial knowledge article chunks: approximately 300–500 tokens with about 40–60 token overlap only when a section must be split.
- Initial lab-explanation chunks: approximately 200–400 tokens per test explanation.
- Initial medicine-information chunks: approximately 200–350 tokens per coherent medicine section.
- Initial FAQ chunks: approximately 100–250 tokens per question-answer or tightly related group.
- Initial retrieval experiment: up to 10 vector and 10 keyword candidates, merge/deduplicate, then use around 3–6 evidence passages subject to relevance and context budget.
- A reranker is optional and should be added only if evaluation demonstrates a meaningful benefit.
- A hosted LLM may be used only after provider/privacy approval; local embeddings do not make hosted generation private by themselves.

## 8. Content-specific handling
- **General medical articles:** split by headings and paragraphs; preserve source, section, language, version, and effective date.
- **Lab explanations/reference information:** keep test name and synonyms, units, purpose, limitations, and applicability notes together where possible. Do not assume one universal reference range.
- **Patient lab results:** treat as structured, access-controlled records. Keep test name, value, unit, reference range, flag, source report, and finalized/review status associated. Never place these in a global knowledge index.
- **Patient history and personal records:** use authorized structured queries by default. Any future semantic index must be explicitly designed, authorized, and isolated by patient/tenant scope.
- **Medicine information:** preserve exact ingredient, formulation, source, warnings, and section. Retrieval must not become authorization to prescribe.
- **FAQs:** keep each answer with its question and provenance; route live operations to backend functions.
- **Policies and consent text:** preserve policy version, jurisdiction, and effective date. Retrieval never grants access or replaces backend enforcement.

## 9. Acceptance criteria
Before production, the team must:
- Verify the repository architecture and select an implementation compatible with it.
- Demonstrate source traceability for substantive generated health-information claims on the evaluation set.
- Test unsupported, ambiguous, conflicting, and absent-evidence questions, including safe abstention.
- Test English, Urdu script, Roman Urdu, and mixed-language cases separately.
- Verify preservation of lab values, units, dates, medicine names, and reference ranges in synthetic cases.
- Pass negative authorization tests, including cross-patient and cross-clinic access attempts, with zero protected-data exposure in the tested cases.
- Test prompt-injection attempts in user questions and retrieved documents.
- Document latency, provider failures, and fallback behavior.
- Receive required project/clinical review for health-related response policies.

Proposed initial engineering targets (not achieved results or proof of clinical safety):
- Recall@5 at least 90% on a curated retrieval test set.
- Substantive claims have supported source citations in 100% of test cases where citations are required.
- Cross-patient exposure: 0 in security tests.
- Unauthorized access attempts denied: 100% in the defined test suite.
- Lab value/unit fidelity: 100% in synthetic test cases.
- No-evidence abstention behavior: at least 95% on the defined test set.

Report dataset size, test composition, per-language performance, and limitations alongside metrics. These targets do not establish clinical safety.

## 10. Open questions after repository audit
- Exact module locations, service boundaries, and existing test conventions.
- Actual current database schema, migration head, extensions, RLS policies, and safe development database.
- Whether a vector extension or existing search index is available.
- Which provider adapter is appropriate and what its data-handling terms permit.
- Final embedding model, chunk boundaries, retrieval/ranking settings, and any reranker.
- Knowledge-source approval owner, update cadence, and retirement policy.
- Clinical review requirements, emergency escalation language, and disclaimer policy.
- Exact P6 finalized-result representation and P7 intent/function-routing interfaces.

## 11. Change control
This is a draft specification. Record confirmed decisions and evidence in `exec.md`; update this file when requirements change. Do not treat hypotheses in Section 7 as approved implementation requirements until reviewed.
