-- ============================================================================================
-- CuraNode-AI: apply two schema revisions to the Supabase database, in order.
--
--   92b61c6e9db4  booking: one active appointment per schedule time (an index)
--   c8e5a41f9b27  records from confirmed documents: a prescription / diagnosis / vital-sign row
--                 may come from an uploaded document instead of a doctor's visit
--
-- This changes the STRUCTURE only. It never edits or deletes a row. Generated from the real Alembic
-- migrations (alembic upgrade a7d3c89106f9:head --sql), so it is exactly what Alembic would run.
-- The whole script is one transaction (BEGIN ... COMMIT): if anything fails, nothing is changed.
-- To regenerate it after a change:  DATABASE_URL=postgresql+asyncpg://offline:offline@127.0.0.1:9/offline
--   uv run alembic upgrade a7d3c89106f9:head --sql      (it connects to nothing)
--
-- STEP 1  Run the three checks below FIRST (they only read).
-- STEP 2  If the version is a7d3c89106f9 and the counts look right, run the rest as ONE script.
-- STEP 3  Run the first check again: the version must now be c8e5a41f9b27.
-- ============================================================================================

-- ---- STEP 1 / 3: checks (read-only) --------------------------------------------------------
-- Which revision is the database at?   Expect exactly: a7d3c89106f9   (afterwards: c8e5a41f9b27)
--   SELECT version_num FROM alembic_version;
--
-- How many rows are in the tables the second revision changes?  (any number is fine: nothing is
-- touched; it only tells you what is there.)
--   SELECT 'prescription' AS tbl, count(*) FROM prescription
--   UNION ALL SELECT 'prescription_item', count(*) FROM prescription_item
--   UNION ALL SELECT 'diagnosis', count(*) FROM diagnosis
--   UNION ALL SELECT 'vital_sign', count(*) FROM vital_sign;
--
-- Does any time slot hold two active appointments?  Expect NO ROWS (the first revision adds a
-- unique index that would refuse that; if rows come back, fix them before running the script).
--   SELECT schedule_id, slot_start, count(*) FROM appointment
--   WHERE status <> 'cancelled' GROUP BY 1, 2 HAVING count(*) > 1;

-- ---- STEP 2: the migration -----------------------------------------------------------------
BEGIN;
-- Running upgrade a7d3c89106f9 -> 92b61c6e9db4
CREATE UNIQUE INDEX uq_appointment_active_slot ON appointment (schedule_id, slot_start) WHERE status <> 'cancelled';
UPDATE alembic_version SET version_num='92b61c6e9db4' WHERE alembic_version.version_num = 'a7d3c89106f9';
-- Running upgrade 92b61c6e9db4 -> c8e5a41f9b27
ALTER TABLE prescription ADD COLUMN document_id UUID;
ALTER TABLE prescription ADD CONSTRAINT fk_prescription_document_id FOREIGN KEY(document_id) REFERENCES document (document_id) ON DELETE CASCADE;
ALTER TABLE prescription ALTER COLUMN encounter_id DROP NOT NULL;
ALTER TABLE prescription ALTER COLUMN doctor_id DROP NOT NULL;
ALTER TABLE prescription ADD CONSTRAINT ck_prescription_visit_or_document CHECK ((encounter_id IS NOT NULL AND doctor_id IS NOT NULL) OR document_id IS NOT NULL);
ALTER TABLE diagnosis ADD COLUMN document_id UUID;
ALTER TABLE diagnosis ADD CONSTRAINT fk_diagnosis_document_id FOREIGN KEY(document_id) REFERENCES document (document_id) ON DELETE CASCADE;
ALTER TABLE diagnosis ALTER COLUMN encounter_id DROP NOT NULL;
ALTER TABLE diagnosis ADD CONSTRAINT ck_diagnosis_visit_or_document CHECK (encounter_id IS NOT NULL OR document_id IS NOT NULL);
ALTER TABLE vital_sign ADD COLUMN document_id UUID;
ALTER TABLE vital_sign ADD CONSTRAINT fk_vital_sign_document_id FOREIGN KEY(document_id) REFERENCES document (document_id) ON DELETE CASCADE;
ALTER TABLE vital_sign ALTER COLUMN encounter_id DROP NOT NULL;
ALTER TABLE vital_sign ADD CONSTRAINT ck_vital_sign_visit_or_document CHECK (encounter_id IS NOT NULL OR document_id IS NOT NULL);
ALTER TABLE prescription_item ADD COLUMN medicine_text VARCHAR(255);
ALTER TABLE prescription_item ALTER COLUMN medicine_id DROP NOT NULL;
ALTER TABLE prescription_item ADD CONSTRAINT ck_prescription_item_medicine_or_text CHECK (medicine_id IS NOT NULL OR medicine_text IS NOT NULL);
CREATE UNIQUE INDEX uq_prescription_document ON prescription (document_id) WHERE document_id IS NOT NULL;
CREATE UNIQUE INDEX uq_vital_sign_document ON vital_sign (document_id) WHERE document_id IS NOT NULL;
CREATE INDEX idx_diagnosis_document_id ON diagnosis (document_id);
UPDATE alembic_version SET version_num='c8e5a41f9b27' WHERE alembic_version.version_num = '92b61c6e9db4';
COMMIT;
