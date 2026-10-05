"""Medical Passport consent JSON API (passport SPEC §8; TDD §4.3).

Patient-only consent management behind `PatientDep` — a doctor calling these
routes is a role mismatch and gets 403 (AC-07), not the D2 404.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request, Response, status

# These MUST be imported at module scope. `from __future__ import annotations`
# turns annotations into strings that FastAPI resolves against this module's
# globals — a function-local import degrades the dependency into a query param.
from ..deps import ConsentWriteRateLimit, PatientDep, SessionDep
from . import service
from .schemas import ConsentGrantOut, GrantCreate

router = APIRouter(prefix="/api/v1/me", tags=["consent"])


def _ua(request: Request) -> str | None:
    return request.headers.get("user-agent")


@router.get("/consents", response_model=list[ConsentGrantOut])
async def list_my_consents(actor: PatientDep, session: SessionDep) -> list[ConsentGrantOut]:
    return await service.list_grants(session, actor)


@router.post(
    "/consents",
    status_code=status.HTTP_201_CREATED,
    response_model=ConsentGrantOut,
    dependencies=[ConsentWriteRateLimit],
)
async def grant_consent(
    request: Request, actor: PatientDep, session: SessionDep, body: GrantCreate
) -> ConsentGrantOut:
    return await service.grant_consent(
        session,
        actor,
        pmdc_number=body.pmdc_number,
        clinic_name=body.clinic_name,
        user_agent=_ua(request),
    )


@router.delete(
    "/consents/{grant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[ConsentWriteRateLimit],
)
async def revoke_consent(
    grant_id: uuid.UUID, request: Request, actor: PatientDep, session: SessionDep
) -> Response:
    await service.revoke_grant(session, actor, grant_id, user_agent=_ua(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
