# D7: Doctor Multi-Hospital Schedule Management

Status: DRAFT v9. Reconciled with the code and with the **live shared schema**
(read-only `information_schema`/`pg_catalog` inspection, 2026-10-10). v4 and v5
record the schema owner's decisions of 2026-10-10 (§11.1, decisions 1–20).
v8 corrects the migration-chain facts after a read-only investigation
(2026-10-10). Ready for planning; nothing is open (§11.3).
Branch: feature/D7-multi-hospital-schedule
Depends on: the identity baseline (FR3: `Doctor`, `DoctorAffiliation`, `Clinic`
in `backend/app/db/models.py`, mapped onto the shared schema). There is no "D1"
feature in this repo. In the PRD, D1 is the decision "contradictory records are
never reconciled", which D7 does not touch.
Needed by later: patient booking and live queue (P5, FR15–FR17; the shared
`appointment` and `queue_entry` tables already exist), later D4 versions, and
the clinic-admin side of FR18. None is built here. (D4 v1, the verified
doctor's consented-patient list, already exists.)

Amends: TDD §3.7 and the "Schedules" row of TDD §7.2. Where this spec and the
TDD disagree, this spec wins for D7 (see §12). The live schema wins over both.

## 0. Reconciliation

### 0.1 Live schema findings (2026-10-10)

| Finding | Consequence for D7 |
|---|---|
| `doctor_schedule` **already exists** in the shared schema (0 rows): `schedule_id` PK, `affiliation_id` NOT NULL FK → `doctor_affiliation` ON DELETE CASCADE, `weekday int2` CHECK 0–6, `start_time`/`end_time time` CHECK end > start, `slot_minutes int4 NOT NULL` CHECK > 0, `max_tokens int4 NOT NULL` CHECK ≥ 0, `status varchar NOT NULL DEFAULT 'active'` (no CHECK). No `created_at`/`updated_at`. | D7 maps onto it. **No new slot table.** A slot belongs to an *affiliation*, not to a (doctor, clinic) pair. The doctor sets `slot_minutes` (R12) and `max_tokens` (R13). The D7 migration adds a `slot_minutes` CHECK (§4.4). |
| `max_tokens` usage (2026-10-10): no table or column comments on `doctor_schedule`, `schedule_exception`, `appointment` or `queue_entry`; no function, trigger or view references `max_tokens`, `token_number` or `doctor_schedule`; no repo doc mentions it. Its only relative is `queue_entry.token_number int4 NOT NULL` (one row per appointment, `UNIQUE (appointment_id)`, no uniqueness on the token, no upper bound). | Nothing enforces or reads `max_tokens` today. Its meaning was decided by the schema owner: R13, R14. |
| Booking-side constraints (checked 2026-10-10 against decision 11.1 #8): `appointment.slot_start timestamptz NOT NULL`; `appointment.schedule_id` **nullable**; `appointment.booking_channel varchar` nullable with **no CHECK**; `appointment.status` CHECK IN (`booked`,`checked_in`,`done`,`cancelled`); **no unique index** on `(schedule_id, slot_start)` or `(doctor_id, slot_start)` (TDD's `uq_appt_slot` was never created); `queue_entry.appointment_id NOT NULL UNIQUE`; `queue_entry.token_number NOT NULL`, no uniqueness, no bound; `queue_entry.state` CHECK IN (`waiting`,`in_room`,`served`); no comments or triggers on either table. | **No contradiction** with "`max_tokens` caps booked plus walk-in patients per session; `slot_minutes` is only the booked-appointment grid". Gaps the booking work must close are listed in R14. |
| Is 0 a meaningful `max_tokens` value anywhere? (checked 2026-10-10 for decision 15) Live schema: no column of `appointment` or `queue_entry` refers to `max_tokens`; no default of 0 and no CHECK mentioning it; no comment, function, trigger or view references `max_tokens`; `doctor_schedule` has 0 rows. Repo: `seed_synthetic.py` and `local_demo.py` create no schedules; the only `max_tokens` in the repo outside this spec is the LLM-provider parameter in TDD §10 (unrelated). | **No use of 0 as a sentinel found**, so `max_tokens >= 1` becomes a database CHECK (decision 15). Not visible from here: other codebases' source; the pre-check before applying (§4.4) covers any rows they write in the meantime. |
| `schedule_exception` **already exists** (0 rows): `exception_id` PK, `schedule_id` NOT NULL FK → `doctor_schedule` ON DELETE CASCADE, `exception_date date`, `type varchar` CHECK IN (`leave`,`emergency`,`extra`), `reason text`, `created_at timestamptz DEFAULT now()`. No times, no clinic or doctor column, no removal marker. | D7 maps onto it. An exception belongs to **one weekly slot**, so the v2 design of all-clinic rows (NULL clinic) and free-standing extras cannot be stored as is. See §4.2 and R3. The schema owner approved adding `start_time`, `end_time` and `removed_at` (decision 2). |
| `appointment.schedule_id` → `doctor_schedule` (nullable FK, **no cascade**); `appointment.status` IN (`booked`,`checked_in`,`done`,`cancelled`); 0 rows. | Slots must never be hard-deleted: once P5 books against one, a delete would fail. D7 also refuses to edit or remove a slot with upcoming live appointments (R10). |
| Both schedule tables already have RLS on, with `select_all_authenticated` and owner/admin write policies built on `my_doctor_id()`/`is_admin()`. | No RLS work in D7. The app connects with BYPASSRLS, so scoping stays in application code. |
| PK defaults are `gen_random_uuid()`. | D7 still supplies uuid7 from the app, like every other table. |
| No `btree_gist` extension; extensions are pgcrypto, uuid-ossp, vector, pg_stat_statements, supabase_vault. | Confirms the exclusion-constraint option is out (§7). |
| `doctor_affiliation`: 9 rows, all `status='active'`, all `end_date IS NULL`. **No doctor has more than one affiliation.** `clinic`: 3 rows, all `active`. | Live data cannot show multi-clinic behaviour, so D7 must seed it (Q1). |
| `doctor` has a `bio` column the model doesn't map; `clinic_staff.role` IN (`receptionist`,`admin`). | Irrelevant to D7; noted only. |
| `alembic_version` holds **exactly one row, `e3a52c8d7f14`**. No file, commit, branch (local or remote), stash, worktree or unreachable git object references it. Statement history (`pg_stat_statements`, reset 2026-09-17, first-run times via `stats_since`) shows exactly two DDL statements after the `35c4bc13fa37` placeholder was reconstructed: **2026-10-08 14:14 UTC `ALTER TABLE ocr_job ADD COLUMN ai_sections JSONB`** and **2026-10-08 16:15 UTC `ocr_extraction_field_name_check` widened from 8 to 28 values** (vaccine, vitals, diagnosis/symptom, allergy fields). (`ocr_extraction.row_index` and `idx_ocr_extraction_job` ran on 09-27 and belong to `35c4bc13fa37`, as its placeholder already records. An earlier draft of this spec misattributed them.) `UPDATE alembic_version` has run 5 times since the reset: 1 for `488dbf2b3915`, 2 for the `35c4bc13fa37` pair, so **2 after `35c4bc13fa37`**. That is consistent with one or two upstream revisions, which cannot be told apart. P9's `dba968144fb4` is **not applied**: `consent_grant` has no `revoked_by`/`replaced_by`, and all 5 rows still have `scope_sections = []`. The auth-hardening revisions `a3c1f0e7b2d4`/`b7d2e9c4a1f6` (unmerged, on `origin/feature/auth-security-hardening`) are not applied either: no `cn_*` functions, and TRUNCATE is still granted. | CLAUDE.md's "shared DB is at `35c4bc13fa37`" is stale; it is deliberately left unedited for now (decision 1). The chain reconciliation is prerequisite task T0 (§4.4). The placeholder's parent is `35c4bc13fa37`: ancestry is well evidenced, but a **direct** parent cannot be proven, so the placeholder stands in for one or two unknown revisions. |

### 0.2 Mismatches between v1 of this spec and the code

| # | v1 said | The code actually does | Fixed in |
|---|---|---|---|
| 1 | Depends on "D1 (doctor profile…)" | No D1 feature; doctor, affiliation and clinic come from the identity baseline (FR3) | header |
| 2 | D4 dashboard "not built" | D4 v1 is merged | header |
| 3 | `/en/doctor` "shows a placeholder card" | Only for an unverified doctor. A verified doctor sees the D4 list (`shell.html`, `patients is defined`) | §9 |
| 4 | "Alembic (or whatever D1 used)" | Alembic, hand-written, additive-only. Repo head is `dba968144fb4` (parent `35c4bc13fa37`); CLAUDE.md's parent note is stale | §4.4 |
| 5 | New tables `weekly_slot` / `schedule_exception` with `doctor_id`, `clinic_id` | Shared `doctor_schedule` / `schedule_exception` already exist, keyed by `affiliation_id` / `schedule_id` | §4 |
| 6 | `is_active` bool | `doctor_schedule.status` already exists (default `active`); exceptions need an additive `removed_at` (the repo convention, as on `allergy`) | §4, Q3 |
| 7 | `kind` enum `unavailable`/`extra` | Live column is `type` with values `leave`/`emergency`/`extra` | §4.2 |
| 8 | "Asia/Karachi" | Fixed +05:00 (`db/types.PKT`, `today_pk()`), no `ZoneInfo` | R8 |
| 9 | "Active affiliation" undefined | `DoctorAffiliation.status == "active"` everywhere; `end_date` never consulted; `Clinic.status == "active"` filtered in consent | R2 |
| 10 | "Every endpoint checks role = doctor" | Role checks only through `deps.py` dependencies; none admits a pending doctor | §3 |
| 11 | Foreign record → "forbidden/not-found" | One identical `NotFound` for foreign, nonexistent, removed or malformed ids | §3, §8 |
| 12 | Patients/staff "get forbidden" | Web pages redirect a wrong role (303) to the caller's own area; 403 is API-only | AC-11 |
| 13 | `ValidationFailed` names the conflicting slot | `ValidationFailed` carries per-field keys with no parameters; parameterised messages use scalar `details` (`message_params`) | §8.1 |
| 14 | `SCHEDULE_SLOT_CREATED` … | Audit values are dotted lower-case (`profile.allergy.add`) | §8.2 |
| 15 | `update_exception` with no route or audit action | Resolved: exceptions are not editable in v1 | §6 |
| 16 | "History is not rewritten" with no validity columns | Resolved: availability is never computed for past dates | R5 |
| 17 | Exclusion constraint as an option | No `btree_gist`; SQLite tests; cannot express cross-table rules | §7 |
| 18 | (missing) rate limit | Profile and consent writes have per-user budgets | §8 |
| 19 | (missing) i18n detail | Flat keys; `missing_keys` test enforces Urdu parity; no weekday keys yet | §9 |
| 20 | (missing gap) | A slot could double-book a future extra at another clinic; removing a leave could re-expose a slot under an extra | R1b, R4b |

## 1. Goal

A doctor who works at several clinics defines when they are available at each
one, and the system guarantees they are never scheduled at two places at the
same time. The result is one trustworthy "effective availability" source that
other features can read later.

## 2. Scope

In scope
- Recurring weekly slots per doctor per affiliation (clinic)
- Date exceptions: time off (whole slot or a window) and extra hours
- Conflict detection across all of the doctor's clinics
- Effective availability function (weekly slots with exceptions applied)
- Doctor schedule page: combined week view across clinics, plus add/edit/remove
- English and Urdu UI, following the existing i18n pattern
- Audit log entries for every schedule change

Out of scope (do not build, do not stub UI for)
- Patient booking, queue, appointments. (The doctor sets `slot_minutes` and
  `max_tokens` per slot; nothing in D7 books against either. R14 records the
  rules the booking work must follow.)
- Notifications to patients
- Clinic admin or receptionist editing a doctor's schedule (admin side of FR18)
- Travel buffer between clinics (plain no-overlap rule)
- Slots that cross midnight (see §7)
- A JSON API (Q4)
- Editing an exception (remove and re-add instead)

## 3. Roles and permissions

| Actor | Can do |
|---|---|
| Doctor, verified or pending (own record only) | Add, edit, remove own slots; add, remove own exceptions; view own week |
| Doctor (another doctor's slot/exception id) | Nothing. `NotFound` (404), identical to a nonexistent id |
| Patient, clinic admin | Nothing in D7. Web: 303 redirect to their own area |
| Signed in, onboarding incomplete (OAuth) | Redirect to `/{locale}/onboarding` |
| Not signed in | Redirect to `/{locale}/login?next=…` |

- **Decided (decision 6):** a pending doctor manages their schedule exactly
  like a verified one (no patient data is exposed). Patient-facing visibility
  of unverified doctors is a P5 concern.
- Role gating: add a fifth dependency, `DoctorDep = require_role("doctor")`,
  to `deps.py`. Every schedule service entry point and web handler uses
  `DoctorDep` (directly, or through the web guard `_doctor_schedule_guard`,
  which sits beside `_doctor_page_guard` and does **not** redirect an
  unverified doctor). `VerifiedDoctorDep` is never used for schedule routes.
  No ad-hoc role checks. CLAUDE.md's dependency count changes from four to
  five in the same commit that adds `DoctorDep` (plan task T3).
- Ownership: `doctor_id` is never read from the request. The service resolves
  the `Doctor` from `actor.user_id`. Every query on `doctor_schedule` or
  `schedule_exception` reaches it through `doctor_affiliation` and filters
  `doctor_affiliation.doctor_id = <me>` in application code. BYPASSRLS means the
  database will not catch a missed scope, and the shared RLS policies do not
  apply to this app.

## 4. Data model: map onto the shared tables

Reuse `DoctorAffiliation` as the doctor-to-clinic link. **No new tables.** Both
tables below are pre-existing and shared (owned by the wider product), so the
models' docstrings say so, as `Allergy` does. The models mirror the live CHECKs
so the SQLite test schema enforces the same rules.

### 4.1 doctor_schedule → `DoctorSchedule`
| Column (Python attr) | Type | D7 usage |
|---|---|---|
| schedule_id (`id`) | uuid PK | uuid7 from the app |
| affiliation_id | uuid FK `doctor_affiliation` NOT NULL | the doctor's affiliation at the clinic; gives both doctor and clinic |
| weekday | int2, CHECK 0–6 | 0 = Monday … 6 = Sunday (`date.weekday()`) |
| start_time / end_time | time, CHECK end > start | PKT local, whole minutes, end ≤ 23:59 |
| slot_minutes | int4 NOT NULL, > 0; **+ D7 CHECK 5–60, multiple of 5** | set by the doctor in the slot form: 5–60 in steps of 5, default 15. The time grid for booked appointments only (R12, R14) |
| max_tokens | int4 NOT NULL, ≥ 0; **+ D7 CHECK ≥ 1** | entered by the doctor: integer 1–200 (200 is app-only), pre-filled with min(floor(duration ÷ `slot_minutes`), 200). Cap on patients per session per date, booked plus walk-in (R13, R14) |
| status | varchar NOT NULL default `active` | `active` = live. Removal sets `inactive`. Readers treat **anything other than `active`** as not live, since other codebases may write other values |

Two CHECKs added on this table by the D7 migration (§4.4), both mirrored in the
model so SQLite tests enforce them, and both validated by the app first so the
doctor sees a form error rather than a database error:
- `doctor_schedule_slot_minutes_grid` = `slot_minutes BETWEEN 5 AND 60 AND
  slot_minutes % 5 = 0` (decision 9).
- `doctor_schedule_max_tokens_min` = `max_tokens >= 1` (decision 15). Before
  adding it, the repo, seed scripts, and the live `appointment`/`queue_entry`
  definitions were checked for any use of 0 as a meaningful value (e.g.
  "unlimited" or "closed"). None was found (§0.1), so it is a database CHECK,
  not app-only. The live `doctor_schedule_max_tokens_check (≥ 0)` stays; the
  new one is stricter and sits beside it.

The upper limit of 200 is **app-only** (decision 14); no CHECK. Timestamps come
from `audit_log`.

### 4.2 schedule_exception → `ScheduleException`
| Column (Python attr) | Type | D7 usage |
|---|---|---|
| exception_id (`id`) | uuid PK | uuid7 |
| schedule_id | uuid FK `doctor_schedule` NOT NULL | the slot the exception applies to (time off) or is anchored to (extra) |
| exception_date | date | PKT local date |
| type (`kind`) | varchar CHECK IN (`leave`,`emergency`,`extra`) | `_enum(ScheduleExceptionType)` mapped to column `type`. D7 writes `leave` (or `emergency` if the doctor ticks it) for time off, and `extra` for added hours. Both `leave` and `emergency` mean "unavailable" when read |
| reason | text, nullable | max 200 chars (schema-validated) |
| created_at | timestamptz default now() | set by the app (`utcnow`) |
| **start_time / end_time** | **time, nullable, NEW** | time off: both NULL = the whole slot, set = a window inside it. Extra: always set |
| **removed_at** | **timestamptz, nullable, NEW** | soft-remove marker; rows are never deleted |
| **max_tokens** | **int4, nullable, NEW** (decision 12) | extras only: this extra session's own cap, 1–200 (200 app-only). NULL = inherit the anchor slot's `max_tokens`. Always NULL on `leave`/`emergency` rows |

**Approved by the schema owner (decisions 2 and 12):** the four new columns,
plus these CHECKs (named, additive): `schedule_exception_times_pair` (both NULL
or both set), `schedule_exception_times_order` (end > start when set),
`schedule_exception_extra_has_times` (`type <> 'extra'` or times set),
`schedule_exception_max_tokens_min` (`max_tokens IS NULL OR max_tokens >= 1`),
`schedule_exception_max_tokens_extra_only` (`max_tokens IS NULL OR type =
'extra'`). New index `idx_schedule_exception_date` on `(exception_date)`.

How the doctor's intents map to rows, created by the service in one
transaction:

| Doctor enters | Rows written |
|---|---|
| Time off, whole day, all clinics | one `leave` row (times NULL) per live slot on that weekday |
| Time off, whole day, one clinic | one row per live slot at that clinic on that weekday |
| Time off, a window (all clinics or one) | one row per live slot it intersects, times = the intersection |
| Extra hours at clinic C, start–end, optional cap | one `extra` row anchored to a live slot at the doctor's active affiliation with C (the earliest by weekday/start), with its own times, and `max_tokens` = the cap entered or NULL (inherit) |

Consequences, accepted for v1 and shown in the UI:
- **Known limitation (accepted, decision 5):** a day off (time off) covers
  only the slots that exist when it is entered. A slot added later on that
  weekday, or a slot edited to new times, is not covered by an earlier day
  off. D7 does not backfill or re-materialise. The UI states this next to the
  time-off form (`schedule.time_off_note`, §9.1), and the exceptions list
  shows which slot each row covers so the doctor can add time off again.
- Time off that hits no live slot is rejected (`errors.schedule_nothing_to_block`).
- Extra hours need at least one live weekly slot at that clinic (any weekday)
  to anchor to, otherwise `errors.schedule_extra_needs_slot`. This matches the
  shared design, where an exception always hangs off a schedule.

### 4.3 What this drops from TDD §3.7
`valid_from`/`valid_to` (R5 makes them unnecessary) and `UNIQUE (doctor_id,
on_date)`. TDD's `doctor_schedules`/`schedule_exceptions` names and
`doctors(user_id)` key never existed; the live tables are named and keyed as in
§4.1–4.2.

### 4.4 Migration

**Prerequisite task T0, migration chain (decision 1).** Phase A is repo-only
and done before any D7 migration work. **Every commit must be green**
(decision 19). Phase B is the shared-DB rollout, which is never part of
implementing D7.

*Phase A (repo only; no DB connection):*

> **Update 2026-10-10 (real file supplied).** The schema owner supplied the OCR
> codebase's real `e3a52c8d7f14_ocr_extraction_more_field_names.py`. It only
> widens `ocr_extraction_field_name_check`, and its `down_revision` is
> `b7e24c19a053`, a revision with no file anywhere in this repo or its remotes.
> So the placeholder for `e3a52c8d7f14` was replaced by the real file
> (unchanged), and `b7e24c19a053` is now the reconstructed placeholder
> (`ocr_job.ai_sections`; parent `35c4bc13fa37`, consistent with exactly two
> version updates after it). Chain: `… 35c4bc13fa37 → b7e24c19a053 →
> e3a52c8d7f14 → dba968144fb4 (P9) → 291c06a2b86f (D7)`. Phase B is unchanged:
> the shared DB is at `e3a52c8d7f14`, so B2/B3 start after it. A downgrade below
> `e3a52c8d7f14` would run the real file's own downgrade, then stop at the
> `b7e24c19a053` placeholder (one transaction, so nothing is left half-done).


Commit structure (decision 19): adding the placeholder alone would give the
chain two heads (it and P9 would both be children of `35c4bc13fa37`), and
`test_ac29` asserts a single head. So the placeholder, P9's re-parent, and the
`test_ac29` edits land together as **one atomic, green commit (A1, which
absorbs A2)**. A3 then only adds the new placeholder's own test.

1. **A1, placeholder + re-parent + chain test (one commit).** Add `e3a52c8d7f14_reconstructed_ocr_placeholder.py`:
   `revision = "e3a52c8d7f14"`, `down_revision = "35c4bc13fa37"`, in the style
   of `35c4bc13fa37_reconstructed_document_ocr_placeholder.py`. `upgrade()` is
   a no-op (the shared DB already has the changes; this repo maps none of
   these tables). `downgrade()` raises `RuntimeError` (another team's schema;
   real downgrade unknown). The docstring records:
   - that it is not the original file;
   - the two statements it stands for, with their first-run times:
     2026-10-08 14:14 UTC `ALTER TABLE ocr_job ADD COLUMN ai_sections JSONB`,
     and 2026-10-08 16:15 UTC `ocr_extraction_field_name_check` dropped and
     re-added with 28 values (list them);
   - the evidence for the parent (the DB was at `35c4bc13fa37` when it was
     checked on 2026-10-07/08; no downgrade traces; 5 version updates since
     the reset, 2 of them after `35c4bc13fa37`);
   - that the direct parent is unproven and the file may stand for two
     upstream revisions; replace it if the OCR team supplies the real files.
   In the **same commit**:
   - re-parent P9: in `dba968144fb4`, set `down_revision = "e3a52c8d7f14"` and
     update the docstring's `Revises:` line and "Parent" paragraph;
   - update `tests/test_privacy_access.py`:
     `test_ac29_revision_chain_is_single_and_unbroken` hard-codes the head and
     chain, so change it to the new chain (head `dba968144fb4`; D7's revision
     once that exists). `test_ac29_migration_is_additive` asserts P9's
     `down_revision … = "35c4bc13fa37"`, so change it to `"e3a52c8d7f14"`.
2. **A2, re-parent P9.** Absorbed into A1 (decision 19); no separate commit.
3. **A3, the new placeholder's test.** Add only the no-op-upgrade /
   refusing-downgrade test for `e3a52c8d7f14`, cloned from
   `test_ac29_placeholder_upgrade_is_noop_and_downgrade_refuses`.
4. **A4, auth-hardening.** Before `feature/auth-security-hardening` merges,
   re-parent its `a3c1f0e7b2d4` from `488dbf2b3915` onto the head at that
   time (`b7d2e9c4a1f6` follows it unchanged). Otherwise the chain gets a
   second head. At the same time, settle the conflict between its docstring
   (which declines a unique active-grant index) and P9's
   `uq_consent_grant_one_active`. This is a task on that branch, tracked
   here, and does not block D7.
5. **A5, verify** (AC-26): exactly one `alembic heads`; `alembic history` is
   linear; `alembic upgrade e3a52c8d7f14:head --sql` (offline, no DB) emits
   the P9 and D7 SQL, which is reviewed by hand. **No `upgrade head` on an
   empty SQLite DB:** `6feacefae2d0` alters shared tables that do not exist
   there. Tests keep building SQLite from the models.
6. CLAUDE.md is **not** edited in T0; plan task T-CLAUDE (§13) does it.

Resulting chain: `… 488dbf2b3915 → 35c4bc13fa37 → e3a52c8d7f14 →
dba968144fb4 (P9) → <D7>` (then `a3c1f0e7b2d4 → b7d2e9c4a1f6` when
auth-hardening merges).

*Phase B (shared DB). Every step is **ASK ME BEFORE RUNNING** and
**schema-owner-only**, never from a dev machine, never by an agent:*
- **B0, backup.** Take a Supabase backup or point-in-time-recovery marker.
  Export `consent_id, scope_sections` for every `consent_grant` row (5 today)
  so the P9 rewrite can be undone. Ask the OCR team not to run migrations
  until B3 is done, because they share `alembic_version`.
- **B1, read-only pre-checks.** `alembic_version` is still one row,
  `e3a52c8d7f14`. No new DDL in `pg_stat_statements` since 2026-10-08 16:15
  UTC. 0 unrevoked duplicate grants per (patient, grantee type, grantee id).
  0 `doctor_schedule` rows violating the D7 CHECKs (`slot_minutes` grid,
  `max_tokens >= 1`). If any check fails, stop.
- **B2, apply P9 only:** `alembic upgrade dba968144fb4`. The placeholders'
  `upgrade()` never runs here (the DB is already past them). Postgres DDL is
  transactional, so a failure rolls back. **Data change called out:** P9
  rewrites every `consent_grant` row with `scope_sections = []` to all seven
  sections. That is **all 5 live rows today** (3 unrevoked), and
  `downgrade()` does not revert it. It also adds `revoked_by`/`replaced_by`,
  their two FKs, and `uq_consent_grant_one_active` (not `CONCURRENTLY`; the
  table is tiny). Any other codebase that inserts a grant while an
  unrevoked one exists will now get a unique-violation error. Verify the
  columns, FKs, index and the 5 rewritten rows.
- **B3, apply D7:** `alembic upgrade <D7>`, carrying the two `doctor_schedule`
  CHECKs and the `schedule_exception` columns. Verify, then tell the OCR team
  the chain is free.

*Rollback (also ASK ME BEFORE RUNNING, schema-owner-only):*
- **D7:** `alembic downgrade dba968144fb4` drops what D7 added. Only before D7
  is in use: afterwards it loses the time-off windows and extra caps, and
  soft-removed exceptions would look live to other readers. Fix forward
  instead.
- **P9:** `alembic downgrade e3a52c8d7f14` drops the index, FKs and columns
  (losing `revoked_by`/`replaced_by` values). `scope_sections` is restored
  from the B0 export if needed.
- **Never below `e3a52c8d7f14`:** both placeholders refuse to downgrade.
- **Last resort:** restore the B0 backup. This affects every team on the DB.

**D7 revision.** One hand-written, additive revision, `down_revision =
"dba968144fb4"`:
- `schedule_exception`: `ADD COLUMN start_time time`, `end_time time`,
  `removed_at timestamptz`, `max_tokens int4` (nullable), the five CHECKs, and
  the index (approved, decisions 2 and 12). All new; no existing row can
  violate them (new columns start NULL).
- `doctor_schedule`: `ADD CONSTRAINT doctor_schedule_slot_minutes_grid CHECK
  (slot_minutes BETWEEN 5 AND 60 AND slot_minutes % 5 = 0)` (decision 9).
  The revision's docstring and this line carry the marker
  **`ASK ME BEFORE RUNNING` on the shared DB.** Immediately before it is
  applied there, run a read-only check that no existing row violates it
  (`SELECT count(*) FROM doctor_schedule WHERE NOT (…)`; 0 rows exist today).
  If any row does, stop and ask; never "fix" other codebases' rows.
- `doctor_schedule`: `ADD CONSTRAINT doctor_schedule_max_tokens_min CHECK
  (max_tokens >= 1)` (decision 15), also **`ASK ME BEFORE RUNNING` on the
  shared DB**, with the same read-only pre-check (`… WHERE max_tokens < 1`)
  and the same stop-and-ask rule.

`downgrade()` drops exactly what `upgrade()` added. ORM changes: add
`DoctorSchedule` (with both new CHECKs) and `ScheduleException` (with its
new column and CHECKs) to
`db/models.py`.

**Applying to the shared DB** (P9, then D7) is T0 Phase B above: schema-owner-only,
every step ASK ME BEFORE RUNNING, never part of implementing D7.
Until then, D7 runs on SQLite only (tests, `local_demo.py`).

## 5. Business rules

"Live slot" = `doctor_schedule.status = 'active'` **and** its affiliation has
`status = 'active'` **and** that clinic has `status = 'active'`. "Live
exception" = `removed_at IS NULL`.

R1. Overlap rule. For the same doctor (all affiliations), two slots with
`status = 'active'` on the same weekday overlap if `start_a < end_b AND start_b
< end_a`. Overlap is rejected whether at the same or different clinics.
Back-to-back is allowed. Slots under an inactive affiliation or clinic **still
count** (so reactivation can never create a hidden overlap); the error marks
the clinic inactive.

R1b. A new or edited slot is also rejected if it overlaps a live `extra` at
**any** clinic, including the same clinic and including the slot's own
extras, on any date ≥ today that falls on that weekday. This applies unless
that date's live time off already removes the overlapping part of the slot
(the overlap test uses the slot's *effective* window on that date, as in R4).
Back-to-back is allowed. (Widened from "different clinic" by decision 17, so a
later slot add or edit cannot create the overlap R4 forbids.)

R2. Affiliation rule. A slot can only be added to, or moved to, an affiliation
with `status = 'active'` at a clinic with `status = 'active'` (`end_date` is
not consulted, matching the rest of the code). If the doctor has several
active affiliation rows for one clinic, use the most recent `start_date`.
Moving a slot to another clinic changes its `affiliation_id`. If an
affiliation becomes inactive, its slots are kept, excluded from availability,
and shown with an "inactive clinic" badge. They can be removed, not edited.

R3. Effective availability for a date:
1. Take the live slots for that weekday.
2. For each slot, subtract its live `leave`/`emergency` exceptions on that date:
   the whole slot (times NULL) or the window.
3. Add every live `extra` on that date whose anchor slot's affiliation and
   clinic are active; its clinic is the anchor's clinic.
4. Do **not** merge windows (decision 17). After R1, R1b, R4 and R4b, no two
   windows overlap, so only back-to-back windows can touch. They stay separate
   because each is its own session with its own cap (R13, R14). Each output
   window carries its source: `schedule_id`, `exception_id` (extras only,
   else None), and its effective cap (R13). The week view may draw
   back-to-back blocks of one clinic as adjacent; that is presentation only.

R4. An `extra` window must not overlap, on that date, **any** other effective
window (R3 steps 1–3) at **any** clinic: other clinics' windows, other
extras, and windows at the **same** clinic, **including its own anchor slot's
window** when the extra falls on the anchor's weekday (decision 17). The
overlap definition is R1's (`start_a < end_b AND start_b < end_a`), so
back-to-back is allowed: for example, an anchor slot 09:00–13:00 plus an extra
13:00–15:00 on the same date. A window already removed by live time off on
that date is not "effective", so an extra may fill it. Rejected with
`ScheduleConflict` naming the conflicting window.

R4b. Removing a `leave`/`emergency` row is rejected if the slot window it frees
would overlap a live `extra` at **any** clinic on that date, including an extra
anchored to that same slot. (Widened by decision 17, for the same reason as
R1b.)

R5. Past dates. "Today" is `db/types.today_pk()`. Exceptions with
`exception_date < today` cannot be added or removed. Slot edits apply from
today. `get_effective_availability` rejects `date_from < today`
(`ValidationFailed`); the history of who was where belongs to P5's
appointments. A range is capped at 30 days (TDD §5).

R6. Editing a weekly slot changes it for all future weeks. A one-day change is
an exception.

R7. Limits: 10 slots with `status = 'active'` per doctor per weekday; 200 live
exception rows with `exception_date >= today` per doctor (rows, so one "all
clinics" entry can use several). Raised as `ListFull(max)`.

R8. All times are PKT (fixed UTC+05:00, `db/types.PKT`). No DST, no conversion.
Times are whole minutes.

R9. A multi-weekday slot add, and a time-off entry that writes several rows,
are all-or-nothing in one transaction, with one audit row per row written.

R10. A slot with `appointment` rows where `slot_start >= now()` and `status IN
('booked','checked_in')` cannot be edited or removed (count-only query; no
patient data is read). This includes an edit that changes only
`slot_minutes` or `max_tokens`. The table is empty today; this guard is for
P5. **Known v1 limitation (decision 10):** while upcoming bookings exist, the
doctor cannot change that slot at all, not even to raise capacity or extend
hours. The workaround is time off plus extra hours for specific dates, or
waiting until the bookings have passed. Relaxing this needs P5's
rebooking/notification rules.

R11. Removing a slot also soft-removes its live exceptions with
`exception_date >= today` in the same transaction (each audited), so no extra
is left anchored to a dead slot. R4b is checked for those removals first.

R12. Minutes per patient (decision 3). Every slot add and edit takes
`slot_minutes` from the form: an integer in {5, 10, …, 60}, default 15. Any
other value is rejected (`errors.schedule_slot_minutes_invalid`). The slot's
duration must be at least `slot_minutes` (`errors.schedule_too_short`). The
duration does not have to be a multiple; leftover minutes are just not a
whole patient turn. When several weekdays are added at once, all get the same
value. Editing it is an ordinary slot edit (audited, R10 applies). After the
D7 migration, the database CHECK makes values outside the grid impossible.

R13. `max_tokens`, the patients per session (decisions 7 and 8). **Decided.**
- Entered by the doctor in the slot form as an integer from 1 to 200
  (`errors.schedule_max_tokens_invalid`). The lower bound is also a database
  CHECK (§4.1); 200 is app-only (decision 14). 0 is never written.
- Pre-filled with the calculated value `calc = floor((end − start) in minutes
  ÷ slot_minutes)`, which is ≥ 1 because R12 requires duration ≥
  `slot_minutes`. Example: 09:00–13:00 at 15 minutes → 16. `calc` is capped at
  200 for the pre-fill (e.g. 00:00–23:59 at 5 minutes → 287 → 200). The doctor
  may enter any value 1–200, higher or lower than `calc` (walk-ins can push it
  above the booked grid).
- Pre-fill mechanics (no JavaScript required): on the add form, a blank field
  means "use `calc`", and the server stores `calc`. A small progressive
  enhancement (`schedule.js`) fills the field live as start, end or minutes
  change. After a failed submit, the form re-renders with the value entered,
  or with `calc` if it was blank. The edit form shows the **stored** value and
  a hint "Calculated: {count}" (`schedule.max_tokens_calc`). Changing times or
  minutes on edit does **not** silently rewrite a stored `max_tokens`; only a
  blank field resets it to `calc`.
- With several weekdays at once (R9), all slots get the same value.
- Extras (decision 12): the add-extra form has an **optional** "patients in
  this extra session" field, 1–200. Blank stores NULL, meaning "inherit the
  anchor slot's `max_tokens`". The effective cap of an extra on its date is
  `exception.max_tokens` if set, otherwise the anchor's current
  `max_tokens`. **Decided (decision 18):** inheritance is live, not a
  snapshot. It is resolved whenever it is read (availability, the UI, and
  later booking), so a later change to the anchor's cap also changes the cap
  of extras that inherit it. To freeze a value, the doctor enters it. Not
  pre-filled; the help text shows the current inherited value
  (`schedule.extra_max_tokens_inherit`).
- The UI shows each extra's **effective cap** beside it (decision 18), in the
  upcoming-exceptions list and on its block in the week view: "Up to {count}
  patients" (`schedule.capacity`) when set on the extra, or "Up to {count}
  patients (from your {day} hours)" (`schedule.capacity_inherited`) when
  inherited. It is computed by the service, not the template.

R14. **Rule the booking work (P5) must follow** (decision 8). D7 stores these
values; it does not enforce them, because there is no booking yet.
- `max_tokens` is the cap on patients in one session (one `doctor_schedule`
  row on one date), counting booked **and** walk-in patients together.
  `slot_minutes` is **only** the time grid for booked appointments
  (`slot_start` = session start + n × `slot_minutes`). It does not limit
  walk-ins.
- Live schema check (§0.1): nothing contradicts this. Gaps the booking work
  must close:
  1. `queue_entry.appointment_id` is NOT NULL, so a walk-in that gets a token
     must also have an `appointment` row. `slot_start` is NOT NULL, so a
     walk-in needs a defined value (e.g. arrival time, which is off the grid).
     The grid rule applies to booked appointments only.
  2. `booking_channel` has no CHECK, so P5 must define its values (e.g.
     booked vs walk-in) and how it tells them apart.
  3. `appointment.schedule_id` is nullable. Every appointment that takes a
     token **must** set it, or it escapes the cap.
  4. Which appointment statuses count toward the cap is **owned by the
     booking work** (decision 13); D7 does not decide it.
  5. Nothing enforces the cap, or one booking per grid time, in the database:
     no unique index on `(schedule_id, slot_start)` and no uniqueness or
     bound on `queue_entry.token_number`. P5 enforces both in the service
     under a lock (as D7 does, §7), or adds constraints with the schema
     owner's OK.
  6. Extra hours (§4.2) are anchored to a weekly slot, so an appointment in an
     extra session carries the anchor's `schedule_id`. P5 counts a session
     per (`schedule_id`, date, session window): the anchor's own window on
     its weekday, and each extra's window on its date. Each window has its
     own cap: the extra's `max_tokens`, or the anchor's when NULL (decision
     12, live per decision 18). An appointment belongs to the extra if
     `slot_start` falls inside the extra's window. R4 guarantees that **no**
     two effective windows overlap, at any clinic, so every `slot_start`
     falls in at most one session. At a back-to-back boundary, the start
     instant belongs to the later window (half-open `[start, end)`).

## 6. Core service (the source of truth)

All logic lives in `backend/app/schedule/service.py`, with
`schedule/schemas.py` for input validation (like `profile/`). Web handlers in
`web/router.py` stay thin.

- `find_conflicts(session, doctor_id, weekday, start, end, exclude_slot_id=None)` → overlapping slots (R1)
- `add_slots(session, actor, *, clinic_id, weekdays, start, end, slot_minutes, max_tokens | None)`: R1, R1b, R2, R7, R9, R12, R13, audit
- `update_slot(session, actor, slot_id, *, clinic_id, weekday, start, end, slot_minutes, max_tokens | None)`: R1, R1b, R2, R10, R12, R13, audit
  (`max_tokens=None` means "use calc", R13)
- `remove_slot(session, actor, slot_id)`: R10, R11, `status='inactive'`, audit
- `add_time_off(session, actor, *, date, clinic_id | None, start | None, end | None, emergency, reason)`: §4.2 mapping, R2, R5, R7, R9, audit
- `add_extra(session, actor, *, date, clinic_id, start, end, max_tokens | None, reason)`: anchor, R2, R4, R5, R7, R13, audit
- `remove_exception(session, actor, exception_id)`: R4b, R5, sets `removed_at`, audit
- `get_effective_availability(session, doctor_id, date_from, date_to, clinic_id=None)`
  → sorted windows `(date, clinic_id, start_time, end_time, schedule_id,
  exception_id | None, effective_max_tokens)`, unmerged (R3.4). A pure read:
  no audit, no lock

A foreign, nonexistent or removed id raises the same `NotFound`. An owned
exception with a past date raises `ValidationFailed` (`errors.schedule_past_date`).

`get_effective_availability` is the only function the week view, and later
booking, may use to read availability. It has no UI or patient dependency, and
it also reads rows that other codebases wrote (any `status`/`type` they use is
handled by the "anything other than `active`" and "`leave` or `emergency`"
rules).

## 7. Edge cases, decisions and known limitations

- **Known limitation (v1):** a day off covers only the slots that exist when it
  is entered (§4.2, decision 5).
- **Known limitation (v1):** a slot with upcoming bookings cannot be edited or
  removed at all (R10, decision 10).

- Midnight-crossing slots: not allowed. Enter 22:00–23:59 plus 00:00–02:00 on
  the next weekday. Give a helpful error when end ≤ start. 23:59 is the latest end.
- Duplicate slot: rejected by R1.
- Overlapping time-off rows on one slot and date: allowed (they union).
- Doctor with no active affiliation: the page renders; the add forms are
  replaced by an explanation.
- `ON DELETE CASCADE` from `doctor_affiliation` → `doctor_schedule` →
  `schedule_exception`: if the wider product hard-deletes an affiliation, its
  slots vanish (or the delete fails once appointments reference them). That is
  outside D7's control; it is noted for the schema owner.
- **Concurrency, decided: row lock.** Every mutation starts with `SELECT … FROM
  doctor WHERE doctor_id = :me FOR UPDATE`, then checks and writes in the same
  transaction. This serialises one doctor's schedule writes, covers the
  cross-table rules (R1b, R4, R4b, R11), and needs no extension (`btree_gist`
  is absent) and no DDL. SQLite ignores `FOR UPDATE`; see AC-12.

## 8. Routes

Server-rendered only (Q4). The pattern follows the profile and access pages:
POST to the collection to add, POST to the item path to edit, POST to
`…/remove` to remove. Success → 303 back with a saved banner; failure →
re-render with values and inline errors.

| Method | Path | Purpose |
|---|---|---|
| GET | /{locale}/doctor/schedule | Week view (`?clinic=<id>`, `?week=<YYYY-MM-DD>` Monday, default this week) |
| POST | /{locale}/doctor/schedule/slots | Add weekly slot(s) |
| POST | /{locale}/doctor/schedule/slots/{slot_id} | Edit slot |
| POST | /{locale}/doctor/schedule/slots/{slot_id}/remove | Remove slot |
| POST | /{locale}/doctor/schedule/time-off | Add time off |
| POST | /{locale}/doctor/schedule/extra | Add extra hours |
| POST | /{locale}/doctor/schedule/exceptions/{exception_id}/remove | Remove an exception row |

- A malformed id → `NotFound`.
- Write budget: `check_schedule_write_rate` in `deps.py`, with
  `settings.schedule_write_rate_limit_per_minute = 30`.
- No CSRF token; `SameSite=Strict` session cookies, as elsewhere.

### 8.1 Errors
- Field errors: `ValidationFailed({"fields": {field: key}})`. New keys:
  `errors.schedule_time_order`, `errors.schedule_time_invalid`,
  `errors.schedule_weekday_required`, `errors.schedule_past_date`,
  `errors.schedule_clinic_not_affiliated`, `errors.schedule_nothing_to_block`,
  `errors.schedule_extra_needs_slot`, `errors.schedule_range_invalid`,
  `errors.schedule_has_appointments`, `errors.schedule_slot_minutes_invalid`,
  `errors.schedule_too_short`, `errors.schedule_max_tokens_invalid`. Reused: `errors.choice_invalid`,
  `errors.text_too_long`, `errors.date_invalid`.
- Conflicts (R1, R1b, R4, R4b): a new `ScheduleConflict(AppError)` in `errors.py`
  (`SCHEDULE_CONFLICT`, 409, `errors.schedule_overlap`). Its scalar `details`
  (`clinic`, `day`, `start`, `end`, `conflict_id`) fill the message, e.g. "This
  overlaps your {day} {start}–{end} slot at {clinic}." The service puts the
  weekday number in `day`; the web handler localises it with `weekday.N`.
- Limits: `ListFull(max)`.

### 8.2 Audit
`audit.write(...)` in the same transaction. New constants in `audit/writer.py`:

| Constant | Value |
|---|---|
| SCHEDULE_SLOT_ADD | `schedule.slot.add` |
| SCHEDULE_SLOT_UPDATE | `schedule.slot.update` |
| SCHEDULE_SLOT_REMOVE | `schedule.slot.remove` |
| SCHEDULE_EXCEPTION_ADD | `schedule.exception.add` |
| SCHEDULE_EXCEPTION_REMOVE | `schedule.exception.remove` |

`subject_patient_id = NULL`; `resource_type = "doctor_schedule"` /
`"schedule_exception"`. `detail` holds the row id, `affiliation_id`,
`clinic_id`, weekday or date, type, times, `max_tokens` (slots, and extras,
where NULL means inherit), and for slots `slot_minutes`; for updates, `before`/`after`.
`reason` is never copied, only `reason_length`.

## 9. UI

Entry points (decision 6: the Schedule link appears on **both** doctor dashboards)
- Verified dashboard (`_guarded`, the D4 patient-list page): add
  `{href: /{loc}/doctor/schedule, label: schedule.open_link}` to `links`,
  beside the passport-lookup link.
- Pending dashboard (`_guarded`, the unverified-banner + `dashboard.placeholder`
  page): today `links = []`; it becomes `[schedule link]` only. `shell.html`
  already renders `links` under the placeholder text, so no template change is
  needed for it to show. It must be the only link: no passport lookup (FR3).
- Nav: add a `schedule` item. Verified: dashboard, lookup, schedule. Pending:
  dashboard, schedule (pending doctors currently get no nav at all, so this
  adds one).

Schedule page (`frontend/templates/doctor/schedule.html`)
- Week view: seven days from `get_effective_availability`, starting **today**
  by default (changed 2026-10-11: a Mon–Sun calendar week showed only past
  days late in the week); `?week=<date>` starts it on that date, and
  Previous/Next move seven days. Days before today are greyed. Blocks are coloured per clinic with design-system tokens, plus a
  legend. Each block shows its session's effective cap; an extra's block is
  marked as extra hours, and as inherited when its cap is.
- Weekly slot list, with inactive-clinic badges, edit and remove.
- Upcoming exceptions list (date ≥ today), each row showing date, clinic, slot
  and window, with remove. Extras also show their effective cap, marked when
  inherited (R13, decision 18).
- Clinic filter (All / one active clinic).
- Add slot: clinic (active affiliations only; a note says hours go only at
  linked clinics and another hospital's admin must add the doctor there), weekday checkboxes, start, end,
  minutes per patient (a `<select>` of 5, 10, …, 60, default 15), and
  patients per session (`<input type="number" min="1">`, pre-filled per R13,
  with the help text `schedule.max_tokens_help`). The edit form shows the same
  fields with the stored values plus the "Calculated: {count}" hint. When a
  slot has upcoming bookings, its edit/remove controls are replaced by
  `schedule.locked_by_bookings` (R10).
- Add time off: date, clinic or all, optional window, "emergency" checkbox,
  reason, with the known-limitation note (`schedule.time_off_note`) shown
  above the submit button.
- Add extra hours: date, clinic, start, end, optional patients in this
  session (`<input type="number" min="1" max="200">`, blank = inherit; help
  text `schedule.extra_max_tokens_inherit`), reason.
- Inline conflict errors; an empty state; works without JavaScript; mobile
  width; English and Urdu, with `dir` from `i18n.catalogue.direction`.

i18n: new flat keys in `en.json` and `ur.json` (parity is enforced by test):
`nav.schedule`, `schedule.*`, `weekday.0`…`weekday.6`, and the
`errors.schedule_*` keys.

### 9.1 UI text list (decision-relevant keys; full list in the plan)

**Urdu column: needs native review** (decision 11). This does **not** block
the merge. The strings ship as written, and a native speaker reviews them in a
follow-up. Every key must still exist in `ur.json` (the parity test does
block).

| Key | English | Urdu |
|---|---|---|
| `nav.schedule` | Schedule | شیڈول |
| `schedule.open_link` | Manage schedule | شیڈول کا انتظام کریں |
| `schedule.title` | My schedule | میرا شیڈول |
| `schedule.slot_minutes` | Minutes per patient | فی مریض منٹ |
| `schedule.slot_minutes_help` | Used later for booking. Choose 5 to 60 minutes. | یہ بعد میں بکنگ کے لیے استعمال ہوگا۔ 5 سے 60 منٹ منتخب کریں۔ |
| `schedule.max_tokens` | Patients per session | فی سیشن مریض |
| `schedule.max_tokens_help` | Most patients you will see in this session, booked and walk-in together. | اس سیشن میں زیادہ سے زیادہ مریض، بکنگ اور بغیر بکنگ دونوں ملا کر۔ |
| `schedule.max_tokens_calc` | Calculated: {count} | حساب کے مطابق: {count} |
| `schedule.capacity` | Up to {count} patients | زیادہ سے زیادہ {count} مریض |
| `schedule.capacity_inherited` | Up to {count} patients (from your {day} hours) | زیادہ سے زیادہ {count} مریض (آپ کے {day} کے اوقات سے) |
| `schedule.extra_badge` | Extra hours | اضافی اوقات |
| `schedule.locked_by_bookings` | This slot has upcoming bookings, so it can't be changed yet. | اس سلاٹ میں آنے والی بکنگز ہیں، اس لیے ابھی اسے تبدیل نہیں کیا جا سکتا۔ |
| **`schedule.time_off_note`** | **Time off applies only to the hours already on your schedule for that day. If you add or change hours for that day later, add the time off again.** | **چھٹی صرف اُن اوقات پر لاگو ہوتی ہے جو اُس دن کے لیے آپ کے شیڈول میں پہلے سے موجود ہیں۔ اگر آپ بعد میں اُس دن کے اوقات شامل کریں یا بدلیں تو چھٹی دوبارہ درج کریں۔** |
| `schedule.inactive_clinic` | Inactive clinic | غیر فعال کلینک |
| `schedule.empty` | You have no weekly hours yet. | آپ کے ابھی کوئی ہفتہ وار اوقات نہیں ہیں۔ |
| `weekday.0` … `weekday.6` | Monday … Sunday | پیر، منگل، بدھ، جمعرات، جمعہ، ہفتہ، اتوار |
| `errors.schedule_slot_minutes_invalid` | Choose 5 to 60 minutes, in steps of 5. | 5 سے 60 منٹ کے درمیان، 5 کے وقفے سے منتخب کریں۔ |
| `errors.schedule_max_tokens_invalid` | Enter a whole number from 1 to 200. | 1 سے 200 تک پورا عدد درج کریں۔ |
| `schedule.extra_max_tokens` | Patients in this extra session (optional) | اس اضافی سیشن میں مریض (اختیاری) |
| `schedule.extra_max_tokens_inherit` | Leave blank to use {count}, the limit of your regular {day} hours at this clinic. | خالی چھوڑ دیں تو اس کلینک میں آپ کے {day} کے باقاعدہ اوقات کی حد، {count}، استعمال ہوگی۔ |
| `errors.schedule_too_short` | These hours are shorter than the minutes per patient. | یہ اوقات فی مریض منٹ سے کم ہیں۔ |
| `errors.schedule_overlap` | This overlaps your {day} {start}–{end} hours at {clinic}. | یہ {clinic} میں آپ کے {day} {start}–{end} کے اوقات سے ٹکراتا ہے۔ |

## 10. Acceptance criteria

AC-1  A doctor (verified or pending) can add a slot for a clinic where they hold an active affiliation; it is stored on that affiliation and appears in the week view.
AC-2  A slot overlapping another active slot of the same doctor (any clinic, including an inactive one) is rejected with a conflict naming the clinic, day and times.
AC-3  Back-to-back slots are accepted.
AC-4  A slot or extra for a clinic without an active affiliation, or at an inactive clinic, is rejected.
AC-5  end ≤ start, end after 23:59, and times with seconds are rejected.
AC-6  Time off (whole day or window, one clinic or all) removes that date's availability without changing the weekly slots, and writes one row per affected slot.
AC-7  Extra hours add availability on that date; they are rejected if they overlap any other effective window that day (any clinic, including the same clinic and the anchor slot; see AC-32), or if no slot exists at that clinic to anchor to.
AC-8  get_effective_availability is correct for today up to 30 days: sorted, windows unmerged and each carrying its source and effective cap, removed, inactive and other-status rows excluded, `emergency` treated as `leave`. A past start or longer range is rejected.
AC-9  Exceptions with a past date cannot be added or removed.
AC-10 Another doctor's slot or exception id behaves exactly like a nonexistent id on every item route.
AC-11 A patient or clinic admin is redirected (303) to their own area; anonymous → login; incomplete onboarding → onboarding. No schedule data is rendered.
AC-12 Overlapping adds cannot both succeed: the doctor-row lock is taken before checking (asserted on the emitted SQL), and a sequential double submit is rejected. A truly concurrent test runs only on Postgres and is skipped on SQLite.
AC-13 Every write produces exactly one audit row per row changed, in the same transaction, without reason text.
AC-14 Urdu and English render, `dir="rtl"` in Urdu, and no key is missing in Urdu.
AC-15 A slot added or edited so that it overlaps a future extra at any clinic on that weekday, including the same clinic, is rejected (R1b).
AC-16 Removing time off that would expose a slot under an extra at any clinic, including an extra anchored to that slot, is rejected (R4b).
AC-17 Multi-weekday and multi-row writes are all-or-nothing.
AC-18 The 11th active slot on a weekday and the 201st upcoming exception row are rejected with the list-full message.
AC-19 A slot with upcoming booked or checked-in appointments cannot be edited or removed (R10).
AC-20 Removing a slot soft-removes its future exceptions (R11), and no row is ever hard-deleted.
AC-21 Unit tests cover the overlap function, the precedence rules and effective availability; integration tests cover every route.
AC-22 `slot_minutes` defaults to 15; only 5–60 in steps of 5 is accepted; a slot shorter than its `slot_minutes` is rejected.
AC-23 `max_tokens` accepts integers 1–200 and rejects 0, 201, negatives and non-integers; the pre-fill is min(calc, 200). A blank field on add stores floor(duration ÷ slot_minutes). On edit, a stored value is kept when times or minutes change, unless the field is blanked.
AC-24 The Schedule link appears on the verified dashboard (beside the lookup link) and on the pending-doctor placeholder page (as its only link). A pending doctor reaches every schedule route, and no schedule route uses `VerifiedDoctorDep`.
AC-25 The time-off form shows `schedule.time_off_note` in English and Urdu. (Urdu wording review is not an acceptance gate.)
AC-26 (T0) `alembic heads` returns exactly one head; `alembic history` is linear: `… 35c4bc13fa37 → e3a52c8d7f14` (placeholder) `→ dba968144fb4 → <D7>`; `alembic upgrade e3a52c8d7f14:head --sql` (offline) produces the P9 and D7 SQL, which is reviewed by hand; `test_ac29` asserts the new head and chain and that both placeholders are no-op on upgrade and refuse to downgrade. No `upgrade head` is run on an empty SQLite DB.
AC-27 The D7 migration adds `doctor_schedule_slot_minutes_grid`. On SQLite, a row with `slot_minutes` 7, 0 or 65 is rejected by the database. The revision carries the "ASK ME BEFORE RUNNING" marker.
AC-28 Editing only `slot_minutes` or `max_tokens` on a slot with upcoming booked or checked-in appointments is rejected (R10), and the page shows `schedule.locked_by_bookings`.
AC-29 An extra accepts an optional `max_tokens` of 1–200. Blank stores NULL (inherit). `max_tokens` on a `leave`/`emergency` row, or a value < 1, is rejected by the database (SQLite) as well as by the app.
AC-30 The D7 migration adds `doctor_schedule_max_tokens_min`; on SQLite a slot with `max_tokens = 0` is rejected by the database. The revision carries the "ASK ME BEFORE RUNNING" marker for both `doctor_schedule` CHECKs.
AC-31 (T-CLAUDE) After T0, CLAUDE.md contains no hardcoded Alembic revision id for the shared DB, and it states the one-head rule, the no-dev-machine-migrations rule, and five role dependencies.
AC-32 An extra that overlaps another effective window at the **same** clinic on its date, including its own anchor slot's window, is rejected with a conflict naming that window; a back-to-back extra (its start = the anchor's end, or its end = the anchor's start) is accepted; an extra filling a window freed by live time off at the same clinic is accepted (R4).
AC-33 An extra with a blank cap shows the anchor's current cap, marked inherited, in the exceptions list and the week view. After the anchor's `max_tokens` is changed, the extra shows the new value with no change to the extra's row (`max_tokens` stays NULL). An extra with its own cap shows that cap, unmarked.

### 10.1 Required unit test cases for R4 (same-clinic extras, decision 17)

Service-level tests (`tests/test_schedule_service.py`, pure service calls on
SQLite). Fixture: doctor D with affiliations at clinics A and B. Weekly slot
S = Monday 09:00–13:00 at A. Date d = the next Monday ≥ today.

| # | Setup | Action | Expected |
|---|---|---|---|
| U1 | — | extra at A on d, 12:00–14:00 (anchored to S) | `ScheduleConflict` naming S 09:00–13:00 at A |
| U2 | — | extra at A on d, 13:00–15:00 | accepted (back-to-back after) |
| U3 | — | extra at A on d, 07:00–09:00 | accepted (back-to-back before) |
| U4 | — | extra at A on d, 10:00–11:00 (inside S) | `ScheduleConflict` |
| U5 | live time off on S, d, 11:00–13:00 | extra at A on d, 11:00–13:00 | accepted (fills the freed window) |
| U6 | U5 done | remove that time off | rejected by R4b (would expose S under the extra) |
| U7 | extra at A on d, 13:00–15:00 | second extra at A on d, 14:00–16:00 | `ScheduleConflict` naming the first extra |
| U8 | extra at A on d, 13:00–15:00 | edit S to Monday 09:00–14:00 | rejected by R1b |
| U9 | extra at A on d, 13:00–15:00 | `get_effective_availability(d, d)` | two windows, 09:00–13:00 (S) and 13:00–15:00 (extra), unmerged, each with its own cap |
| U10 | extra at B on d, 12:00–14:00 | (different clinic, overlaps S) | `ScheduleConflict` (unchanged R4 behaviour) |

## 11. Open questions and decisions

### 11.1 Schema owner's decisions (2026-10-10; rounds: 1–6, 7–11, 12–16, 17–18, 19–20)

| # | Decision | Where it lands |
|---|---|---|
| 1 | The shared DB is at `e3a52c8d7f14`, which has no file here. Create a placeholder for it, then chain P9 and D7 after it. Do not edit CLAUDE.md yet. | Prerequisite task T0 (§4.4), AC-26 |
| 2 | Approved: add `start_time`, `end_time`, `removed_at` to `schedule_exception`, plus the three CHECKs and `idx_schedule_exception_date`. | §4.2, §4.4 |
| 3 | `slot_minutes` is set by the doctor in the slot form: default 15, 5–60 in steps of 5. | R12, §4.1, §9, AC-22 |
| 4 | `max_tokens` must not be 0. Inspect its usage and propose a safe default. | §0.1 findings; superseded by 7 and 8 |
| 5 | The day-off limitation is accepted for v1 and must be shown in the UI. | §4.2, §7, §9.1 `schedule.time_off_note`, AC-25 |
| 6 | Pending doctors may manage their schedule, using the new `DoctorDep`; the Schedule link appears on the verified dashboard and on the pending placeholder page. | §3, §9, AC-24 |
| 7 | `max_tokens` is a doctor-entered integer ≥ 1, pre-filled with the calculated value. | R13, §4.1, §9, AC-23 |
| 8 | `max_tokens` caps patients per session (booked plus walk-in); `slot_minutes` is only the booked-appointment grid. A rule the booking work must follow. Live constraints checked: no contradiction. | R14, §0.1 |
| 9 | Add a CHECK on `doctor_schedule.slot_minutes` (5–60, multiples of 5) in the D7 migration, **ASK ME BEFORE RUNNING** on the shared DB. | §4.1, §4.4, AC-27 |
| 10 | Keep blocking edits and removal while upcoming bookings exist; record it as a known v1 limitation. | R10, §7, AC-28 |
| 11 | Urdu strings in §9.1 are marked "needs native review"; this does not block the merge. | §9.1, AC-25 |
| 12 | Add an optional `max_tokens` to `schedule_exception` (CHECK ≥ 1 when set; blank = inherit the anchor slot's cap), plus an optional form field. | §4.2, §4.4, R13, R14.6, §9, AC-29 |
| 13 | Which appointment statuses count toward the cap stays with the booking work. | R14.4 |
| 14 | App-level upper limit for `max_tokens` is 200 (slots and extras). | R13, §4.1, §9.1, AC-23 |
| 15 | Add CHECK `max_tokens >= 1` on `doctor_schedule`, ASK ME BEFORE RUNNING, after checking for any meaningful use of 0. Checked: none found, so it is a database CHECK. | §0.1, §4.1, §4.4, AC-30 |
| 16 | After T0, update CLAUDE.md (plan task, not an edit now). | §13 T-CLAUDE, AC-31 |
| 17 | An extra must not overlap any other effective window at the same clinic on that date, including its own anchor slot (R1's definition; back-to-back allowed). The "which cap in the overlap" question no longer applies. | R4; consequences in R1b, R3.4, R4b, R14.6; AC-7, AC-15, AC-16, AC-32; §10.1 |
| 18 | A blank `max_tokens` on an extra inherits the anchor's cap and follows later changes to it. The UI shows the effective cap beside the extra. | R13, §9, §9.1, AC-33 |
| 19 | Every commit must be green. The `test_ac29` edits fold into the chain change; A3 only adds the new placeholder's test. Because a placeholder alone would create two heads, A1 and A2 land as one atomic commit. | §4.4 T0, §13 |
| 20 | Commit the spec and plan first in a docs-only commit with explicit paths, excluding the staged OAuth files. `local_demo.py` is not committed until the owner has reviewed it. | Plan §2, T14 |

### 11.2 Earlier questions, answered

1. **Does any test or seed data have a second clinic and a second affiliation
   for one doctor?** No. Tests have one `clinic` fixture, and `make_user` adds
   at most one affiliation. `seed_synthetic.py` has three clinics, but each
   doctor has one affiliation. The live DB has 9 affiliations and no doctor
   holds two. `local_demo.py` (untracked) has Dr Bilal at one clinic. D7 adds
   a second-clinic fixture and an affiliation helper (active and inactive),
   gives the verified seed doctor a second affiliation, and adds one to Bilal
   in the demo.
2. **Alembic or create_all?** Alembic, hand-written and additive. `create_all`
   is used only for SQLite tests and the local demo. D7 needs one small
   migration, after prerequisite task T0 (§4.4).
3. **Delete or soft-disable?** Never delete. A slot uses the existing
   `doctor_schedule.status` (`active` → `inactive`). An exception uses the new
   `removed_at` (decision 2).
4. **Is a JSON API needed now?** No. A JSON router can be added for P5 on top
   of `schedule/service.py`.

### 11.3 Still open

Nothing. Decisions 17 and 18 closed the last two items. Items deliberately left
to the booking work (P5) are recorded in R14, not here.

## 12. Amendments to TDD (to record in docs/TDD.md when D7 lands)

- §3.7: replace `doctor_schedules`/`schedule_exceptions` with the live
  `doctor_schedule` (per affiliation; `slot_minutes` 5–60 in steps of 5, set
  by the doctor, with a CHECK; `max_tokens` entered by the doctor (≥ 1), the
  per-session cap on booked plus walk-in patients; `status`)
  and `schedule_exception` (per slot, `type` leave/emergency/extra, plus D7's
  `start_time`/`end_time`/`removed_at` and the extras-only `max_tokens`, NULL =
  inherit). `doctor_schedule.max_tokens` is ≥ 1 (CHECK), at most 200 (app).
- §7.2 "Schedules": a doctor (verified or unverified) has RW on their own
  schedule. Clinic-admin RW (FR18) and patient R (FR15) remain planned.

## 13. Plan notes (ordered tasks for the implementation plan)

Each task is its own commit. Nothing here runs against the shared DB.

- **T0, migration chain** (§4.4, decision 1). Every commit green (decision
  19). Phase A: A1 is one commit with the placeholder `e3a52c8d7f14` (parent
  `35c4bc13fa37`; `ai_sections` and the widened `field_name_check`), P9's
  re-parent, and the `test_ac29` edits (hard-coded head and chain, P9
  `down_revision` assertion); A2 is absorbed into A1; A3 adds only the new
  placeholder's test; A4 re-parent auth-hardening's `a3c1f0e7b2d4` before that branch
  merges; A5 verify (AC-26). Phase B (B0 backup and consent export, B1
  pre-checks, B2 P9 with its consent-row rewrite, B3 D7, and rollback) is
  **ASK ME BEFORE RUNNING, schema-owner-only**, and is not a plan task.
- **T-CLAUDE, update CLAUDE.md** (decision 16). Runs immediately after T0,
  before any D7 feature code. Changes:
  1. Replace the hardcoded shared-DB revision in the "Migration chain warning"
     with an instruction to run `uv run alembic heads` (and `alembic current`
     only where a DB is configured) instead of trusting any id written in the
     doc. Describe the chain history (two reconstructed upstream placeholders,
     `35c4bc13fa37` and `e3a52c8d7f14`) without naming a "current" revision.
  2. Add the rule: **migrations are never run against the shared DB from a
     dev machine.** `upgrade`/`stamp`/`downgrade` against it are done only by
     the schema owner, and any revision marked "ASK ME BEFORE RUNNING" needs
     their explicit go-ahead.
  3. Add the rule: **the chain must have exactly one head.** Every new
     revision parents on the output of `alembic heads`; a second head is a
     bug to fix before merging. Upstream revisions found in the shared DB get
     a reconstructed placeholder, as T0 does.
  4. The dependency count in "Authentication & Session Management" stays at
     four here. It changes to **five** (`ActorDep`, `PatientDep`,
     `VerifiedDoctorDep`, `DoctorDep`, `ClinicAdminDep`) in the commit that
     adds `DoctorDep` (plan task T3), so CLAUDE.md never runs ahead of the
     code.
  5. Remove the stale "P9's `dba968144fb4` is parented on `488dbf2b3915`"
     statement.
  Verified by AC-31.
- **T1, models and migration**: `DoctorSchedule`, `ScheduleException` (with
  all new columns and CHECKs mirrored); the D7 revision with the "ASK ME
  BEFORE RUNNING" markers (§4.4); AC-27, AC-29, AC-30.
- **T2, `DoctorDep`** in `deps.py` and the web guard (§3).
- **T3, service** (in the plan: T6a, a pure engine that owns the single
  overlap definition and returns conflicts as plain data, then T6b and later,
  the database-backed service that raises `ScheduleConflict`): overlap and conflicts, slots, time off, extras (with the
  optional cap and live inheritance), effective availability (unmerged, with
  effective caps), locking, audit (§5–§7); unit cases U1–U10 (§10.1).
- **T4, web**: routes, templates, `schedule.js` pre-fill enhancement,
  dashboard links and nav, i18n keys (Urdu marked "needs native review").
- **T5, fixtures and seed**: second-clinic fixture and affiliation helper;
  second affiliation for the verified seed doctor; Bilal's second
  affiliation in `local_demo.py`.
- **T6, docs**: the TDD §3.7/§7.2 amendments (§12).
- **Not a task: applying anything to the shared DB.** That is T0 Phase B
  (§4.4): schema-owner-only, every step ASK ME BEFORE RUNNING.
