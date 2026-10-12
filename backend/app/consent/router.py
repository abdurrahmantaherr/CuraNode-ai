"""Consent JSON API (passport SPEC §8; P9 privacy & access spec §6.1).

Patient-only consent management behind `PatientDep` — a doctor calling these
routes is a role mismatch and gets 403 (passport AC-07), not the D2 404.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Request, Response, status
from pydantic_core import PydanticCustomError

# These MUST be imported at module scope. `from __future__ import annotations`
# turns annotations into strings that FastAPI resolves against this module's
# globals — a function-local import degrades the dependency into a query param.
from ..deps import ConsentSearchRateLimit, ConsentWriteRateLimit, PatientDep, SessionDep
from ..errors import ValidationFailed
from . import access_log, service
from .schemas import (
    AccessLogPage,
    ConsentGrantOut,
    GrantCreate,
    GranteeOut,
    SectionsUpdate,
    clean_query,
)

router = APIRouter(prefix="/api/v1/me", tags=["consent"])


def _ua(request: Request) -> str | None:
    return request.headers.get("user-agent")


# Declared before any `/consents/{grant_id}` route.
@router.get(
    "/consents/grantees",
    response_model=list[GranteeOut],
    dependencies=[ConsentSearchRateLimit],
)
async def search_grantees(
    actor: PatientDep, session: SessionDep, q: str = Query("", max_length=200)
) -> list[GranteeOut]:
    try:
        cleaned = clean_query(q)
    except PydanticCustomError as exc:
        raise ValidationFailed({"fields": {"q": exc.message_template}}) from None
    return await service.search_grantees(session, actor, cleaned)


@router.get("/consents", response_model=list[ConsentGrantOut])
async def list_my_consents(
    actor: PatientDep, session: SessionDep, include_revoked: bool = False
) -> list[ConsentGrantOut]:
    return await service.list_grants(session, actor, include_revoked=include_revoked)


@router.post(
    "/consents",
    status_code=status.HTTP_201_CREATED,
    response_model=ConsentGrantOut,
    dependencies=[ConsentWriteRateLimit],
)
async def grant_consent(
    request: Request,
    response: Response,
    actor: PatientDep,
    session: SessionDep,
    body: GrantCreate,
) -> ConsentGrantOut:
    grant, created = await service.grant_consent(
        session,
        actor,
        sections=body.sections,
        duration=body.duration,
        doctor_id=body.doctor_id,
        clinic_id=body.clinic_id,
        pmdc_number=body.pmdc_number,
        clinic_name=body.clinic_name,
        user_agent=_ua(request),
    )
    if not created:  # BL-05 — the existing active grant, unchanged
        response.status_code = status.HTTP_200_OK
    return grant


@router.put(
    "/consents/{grant_id}/sections",
    response_model=ConsentGrantOut,
    dependencies=[ConsentWriteRateLimit],
)
async def change_consent_sections(
    grant_id: uuid.UUID,
    request: Request,
    actor: PatientDep,
    session: SessionDep,
    body: SectionsUpdate,
) -> ConsentGrantOut:
    return await service.change_sections(
        session, actor, grant_id, sections=body.sections, user_agent=_ua(request)
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


@router.get("/access-log", response_model=AccessLogPage)
async def my_access_log(
    actor: PatientDep,
    session: SessionDep,
    cursor: str | None = Query(None, max_length=200),
    limit: int = Query(access_log.DEFAULT_LIMIT, ge=1, le=access_log.MAX_LIMIT),
) -> AccessLogPage:
    """FR5 — who read my record, newest first."""
    return await access_log.list_access_log(session, actor, cursor=cursor, limit=limit)
