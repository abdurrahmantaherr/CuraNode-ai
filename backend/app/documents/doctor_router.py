"""Doctor-generated prescription creation — backend/API support only.

    POST /api/v1/doctor/prescriptions   a verified doctor's structured
                                         prescription for one of their
                                         patients -> automatically finalized,
                                         no patient review

No Doctor Dashboard UI calls this yet; it exists so one can, later. The
created document is read back through the SAME patient-facing endpoints
`documents/router.py` already exposes (`GET /api/v1/documents`, `GET
/api/v1/documents/{id}`, `GET /api/v1/documents/{id}/file`) — there is no
separate "doctor documents" read path.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, status

from ..deps import DoctorDocumentWriteRateLimit, SessionDep, VerifiedDoctorDep
from . import service
from .schemas import CreateDoctorPrescriptionRequest, DocumentDetailOut

router = APIRouter(prefix="/api/v1/doctor/prescriptions", tags=["doctor-documents"])


def _ua(request: Request) -> str | None:
    return request.headers.get("user-agent")


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=DocumentDetailOut,
    dependencies=[DoctorDocumentWriteRateLimit],
)
async def create_prescription(
    request: Request,
    actor: VerifiedDoctorDep,
    session: SessionDep,
    body: CreateDoctorPrescriptionRequest,
) -> DocumentDetailOut:
    doc = await service.create_doctor_prescription(
        session,
        actor,
        patient_id=body.patient_id,
        medicines=[m.model_dump() for m in body.medicines],
        notes=body.notes,
        ip=_ip(request),
        user_agent=_ua(request),
    )
    # NOT `actor.user_id`: the caller here is the doctor, not the patient the
    # document belongs to — `_document_source` needs the latter.
    patient_user_id = await service.patient_owner_user_id(session, doc.patient_id)
    return await service.to_detail(session, doc, patient_user_id)
