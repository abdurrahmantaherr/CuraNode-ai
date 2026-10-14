"""Typed output for the clinical record the consent gateway returns (D2).

Read-only views of doctor-owned rows; nothing here is accepted as input.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class PatientBasicOut(_Out):
    id: uuid.UUID
    full_name: str
    date_of_birth: date | None
    gender: str | None
    blood_group: str | None


class AllergyOut(_Out):
    id: uuid.UUID
    substance: str
    reaction: str | None
    severity: str | None
    recorded_at: datetime
    recorded_by: uuid.UUID | None


class ConditionOut(_Out):
    id: uuid.UUID
    name: str
    icd10_code: str | None
    onset_date: date | None
    status: str
    risk_level: str | None
    recorded_by: uuid.UUID | None


class MedicationOut(_Out):
    id: uuid.UUID
    name: str
    strength: str | None
    frequency: str | None
    started_on: date | None
    recorded_by: uuid.UUID | None


class DiagnosisOut(_Out):
    id: uuid.UUID
    icd10_code: str | None
    description: str | None
    severity: str | None
    is_chronic: bool


class VitalSignOut(_Out):
    id: uuid.UUID
    height_cm: Decimal | None
    weight_kg: Decimal | None
    bp_systolic: int | None
    bp_diastolic: int | None
    pulse_bpm: int | None
    temperature_c: Decimal | None
    spo2: int | None
    recorded_at: datetime


class MedicineOut(_Out):
    id: uuid.UUID
    generic_name: str
    brand_name: str | None
    strength: str | None
    form: str | None
    atc_code: str | None


class PrescriptionItemOut(_Out):
    id: uuid.UUID
    medicine: MedicineOut | None
    dosage: str | None
    frequency: str | None
    duration_days: int | None
    route: str | None
    instructions: str | None


class PrescriptionOut(_Out):
    id: uuid.UUID
    doctor_id: uuid.UUID
    issue_date: date
    source: str
    status: str
    created_at: datetime
    items: list[PrescriptionItemOut]


class EncounterOut(_Out):
    id: uuid.UUID
    doctor_id: uuid.UUID
    clinic_id: uuid.UUID
    visit_datetime: datetime
    chief_complaint: str | None
    clinical_notes: str | None
    ai_summary: str | None
    what_changed_diff: str | None
    diagnoses: list[DiagnosisOut]
    vitals: list[VitalSignOut]
    prescriptions: list[PrescriptionOut]


class LabResultOut(_Out):
    id: uuid.UUID
    test_name: str
    loinc_code: str | None
    value: Decimal | None
    unit: str | None
    ref_range_low: Decimal | None
    ref_range_high: Decimal | None
    flag: str | None


class LabReportOut(_Out):
    id: uuid.UUID
    encounter_id: uuid.UUID | None
    document_id: uuid.UUID
    lab_name: str | None
    report_date: date | None
    status: str
    results: list[LabResultOut]


class PatientRecordOut(_Out):
    patient: PatientBasicOut
    allergies: list[AllergyOut]
    conditions: list[ConditionOut]
    medications: list[MedicationOut]
    encounters: list[EncounterOut]
    lab_reports: list[LabReportOut]
