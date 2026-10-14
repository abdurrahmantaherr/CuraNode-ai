"""Medical timeline JSON API (P3 — FR8, FR9).

  GET /api/v1/me/timeline                                    the signed-in patient's own timeline
  GET /api/v1/me/timeline/{kind}/{id}                        one event with its details
  GET /api/v1/doctor/patients/{patient_id}/timeline          a patient's timeline, for a verified doctor
  GET /api/v1/doctor/patients/{patient_id}/timeline/{kind}/{id}

The list endpoints take `type` (consultation | lab | prescription | vaccination | document |
profile), `q` (search text), `date_from` / `date_to` (inclusive, YYYY-MM-DD), `facility`,
`doctor`, `limit`, `offset` — validated here and in `service.parse_filters`, applied AFTER the
consent gateway has decided what the reader may receive. A value that is not understood is a 422
naming it; an unknown parameter is refused rather than ignored.

The patient routes read the caller's own record (their patient id comes from the verified
session, never from the request). The doctor routes go through the consent gateway like every
other read of someone else's record: a doctor sees only the sections the patient shared, each
read is written to `access_log`, and a patient who never shared (or revoked, or whose grant
lapsed, or who does not exist) is the identical 404. Clinic admins have no route here at all.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request

# Module-scope imports: with `from __future__ import annotations` FastAPI resolves annotations
# against this module's globals, so a function-local import would turn a dependency into a
# query parameter.
from ..deps import PatientDep, SessionDep, VerifiedDoctorDep
from ..errors import ValidationFailed
from . import events as ev
from . import service
from .schemas import TimelineDetail, TimelineOut

router = APIRouter(tags=["timeline"])

# The only query parameters the list endpoints take. Anything else is REFUSED rather than
# ignored: a misspelt filter that was silently dropped would hand back the whole timeline as
# though it had been the answer.
_LIST_PARAMS = frozenset(
    {"type", "q", "date_from", "date_to", "facility", "doctor", "limit", "offset"}
)


def _only_known_params(request: Request) -> None:
    unknown = sorted(set(request.query_params) - _LIST_PARAMS)
    if unknown:
        raise ValidationFailed({"fields": {name: "errors.unknown_parameter" for name in unknown}})


def _filters(
    type: str | None,
    q: str | None,
    date_from: str | None,
    date_to: str | None,
    facility: str | None,
    doctor: str | None,
) -> ev.Filters:
    parsed, problems = service.parse_filters(
        {
            "type": type,
            "q": q,
            "date_from": date_from,
            "date_to": date_to,
            "facility": facility,
            "doctor": doctor,
        }
    )
    service.require_valid(problems)
    return parsed


Limit = Annotated[int, Query(ge=1, le=service.MAX_LIMIT)]
Offset = Annotated[int, Query(ge=0)]


# ── The patient's own timeline ───────────────────────────────────────────
@router.get("/api/v1/me/timeline", response_model=TimelineOut)
async def my_timeline(
    request: Request,
    actor: PatientDep,
    session: SessionDep,
    type: str | None = None,
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    facility: str | None = None,
    doctor: str | None = None,
    limit: Limit = service.DEFAULT_LIMIT,
    offset: Offset = 0,
) -> TimelineOut:
    _only_known_params(request)
    scoped = _filters(type, q, date_from, date_to, facility, doctor)
    patient_id = await service.own_patient_id(session, actor)
    return await service.timeline(
        session, actor, patient_id, scoped, loc=actor.locale, limit=limit, offset=offset
    )


@router.get("/api/v1/me/timeline/{kind}/{event_id}", response_model=TimelineDetail)
async def my_timeline_event(
    kind: str, event_id: uuid.UUID, actor: PatientDep, session: SessionDep
) -> TimelineDetail:
    patient_id = await service.own_patient_id(session, actor)
    return await service.event_detail(session, actor, patient_id, kind, event_id, loc=actor.locale)


# ── A doctor's view of a patient who shared with them ────────────────────
@router.get("/api/v1/doctor/patients/{patient_id}/timeline", response_model=TimelineOut)
async def patient_timeline(
    request: Request,
    patient_id: uuid.UUID,
    actor: VerifiedDoctorDep,
    session: SessionDep,
    type: str | None = None,
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    facility: str | None = None,
    doctor: str | None = None,
    limit: Limit = service.DEFAULT_LIMIT,
    offset: Offset = 0,
) -> TimelineOut:
    _only_known_params(request)
    scoped = _filters(type, q, date_from, date_to, facility, doctor)
    return await service.timeline(
        session, actor, patient_id, scoped, loc=actor.locale, limit=limit, offset=offset
    )


@router.get(
    "/api/v1/doctor/patients/{patient_id}/timeline/{kind}/{event_id}",
    response_model=TimelineDetail,
)
async def patient_timeline_event(
    patient_id: uuid.UUID,
    kind: str,
    event_id: uuid.UUID,
    actor: VerifiedDoctorDep,
    session: SessionDep,
) -> TimelineDetail:
    return await service.event_detail(session, actor, patient_id, kind, event_id, loc=actor.locale)
