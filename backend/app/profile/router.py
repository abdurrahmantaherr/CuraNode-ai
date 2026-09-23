"""Patient profile JSON API (profile SPEC §6.4).

TDD §4.4's `PATCH /api/v1/me` is realised as `/api/v1/me/profile` (plus the
per-list routes) behind `PatientDep`: `GET /api/v1/me` is role-agnostic and
stays unchanged (SPEC §6.0).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request, Response, status

# These MUST be imported at module scope. `from __future__ import annotations`
# turns annotations into strings that FastAPI resolves against this module's
# globals — a function-local import degrades the dependency into a query param.
from ..deps import PatientDep, ProfileWriteRateLimit, SessionDep
from . import service
from .schemas import (
    AllergyCreate,
    AllergyOut,
    AllergyUpdate,
    ConditionCreate,
    ConditionOut,
    ConditionUpdate,
    MedicationCreate,
    MedicationOut,
    MedicationUpdate,
    PatientProfileOut,
    ProfileUpdateRequest,
)

router = APIRouter(prefix="/api/v1/me", tags=["profile"])


def _ua(request: Request) -> str | None:
    return request.headers.get("user-agent")


@router.get("/profile", response_model=PatientProfileOut)
async def get_my_profile(actor: PatientDep, session: SessionDep) -> PatientProfileOut:
    return await service.get_profile(session, actor)


@router.patch("/profile", response_model=PatientProfileOut, dependencies=[ProfileWriteRateLimit])
async def update_my_profile(
    request: Request, actor: PatientDep, session: SessionDep, body: ProfileUpdateRequest
) -> PatientProfileOut:
    return await service.update_profile(session, actor, body, user_agent=_ua(request))


# ── Allergies ────────────────────────────────────────────────────────────
@router.post(
    "/allergies",
    status_code=status.HTTP_201_CREATED,
    response_model=AllergyOut,
    dependencies=[ProfileWriteRateLimit],
)
async def add_allergy(
    request: Request, actor: PatientDep, session: SessionDep, body: AllergyCreate
) -> AllergyOut:
    return await service.add_allergy(session, actor, body, user_agent=_ua(request))


@router.patch(
    "/allergies/{allergy_id}", response_model=AllergyOut, dependencies=[ProfileWriteRateLimit]
)
async def update_allergy(
    allergy_id: uuid.UUID,
    request: Request,
    actor: PatientDep,
    session: SessionDep,
    body: AllergyUpdate,
) -> AllergyOut:
    return await service.update_allergy(session, actor, allergy_id, body, user_agent=_ua(request))


@router.delete(
    "/allergies/{allergy_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[ProfileWriteRateLimit],
)
async def remove_allergy(
    allergy_id: uuid.UUID, request: Request, actor: PatientDep, session: SessionDep
) -> Response:
    await service.remove_allergy(session, actor, allergy_id, user_agent=_ua(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Chronic conditions ───────────────────────────────────────────────────
@router.post(
    "/conditions",
    status_code=status.HTTP_201_CREATED,
    response_model=ConditionOut,
    dependencies=[ProfileWriteRateLimit],
)
async def add_condition(
    request: Request, actor: PatientDep, session: SessionDep, body: ConditionCreate
) -> ConditionOut:
    return await service.add_condition(session, actor, body, user_agent=_ua(request))


@router.patch(
    "/conditions/{condition_id}",
    response_model=ConditionOut,
    dependencies=[ProfileWriteRateLimit],
)
async def update_condition(
    condition_id: uuid.UUID,
    request: Request,
    actor: PatientDep,
    session: SessionDep,
    body: ConditionUpdate,
) -> ConditionOut:
    return await service.update_condition(
        session, actor, condition_id, body, user_agent=_ua(request)
    )


@router.delete(
    "/conditions/{condition_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[ProfileWriteRateLimit],
)
async def remove_condition(
    condition_id: uuid.UUID, request: Request, actor: PatientDep, session: SessionDep
) -> Response:
    await service.remove_condition(session, actor, condition_id, user_agent=_ua(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Current medications ──────────────────────────────────────────────────
@router.post(
    "/medications",
    status_code=status.HTTP_201_CREATED,
    response_model=MedicationOut,
    dependencies=[ProfileWriteRateLimit],
)
async def add_medication(
    request: Request, actor: PatientDep, session: SessionDep, body: MedicationCreate
) -> MedicationOut:
    return await service.add_medication(session, actor, body, user_agent=_ua(request))


@router.patch(
    "/medications/{medication_id}",
    response_model=MedicationOut,
    dependencies=[ProfileWriteRateLimit],
)
async def update_medication(
    medication_id: uuid.UUID,
    request: Request,
    actor: PatientDep,
    session: SessionDep,
    body: MedicationUpdate,
) -> MedicationOut:
    return await service.update_medication(
        session, actor, medication_id, body, user_agent=_ua(request)
    )


@router.delete(
    "/medications/{medication_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[ProfileWriteRateLimit],
)
async def remove_medication(
    medication_id: uuid.UUID, request: Request, actor: PatientDep, session: SessionDep
) -> Response:
    await service.remove_medication(session, actor, medication_id, user_agent=_ua(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
