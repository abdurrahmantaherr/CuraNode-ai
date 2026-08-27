"""JSON API for authentication (SPEC 6.1).

Cookie attributes are set in exactly one place (`set_session_cookies`) so a
future endpoint cannot accidentally issue a weaker cookie.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Request, Response, status
from sqlalchemy import select

from ..db.models import Clinic
from ..deps import ActorDep, AuthRateLimit, SessionDep
from ..identity import service
from ..identity.schemas import (
    ClinicOut,
    LoginRequest,
    MeOut,
    RegisterRequest,
    SessionOut,
)
from ..identity.security import TokenPair
from ..settings import settings

router = APIRouter(prefix="/api/v1", tags=["identity"])


def set_session_cookies(response: Response, pair: TokenPair) -> None:
    """HttpOnly + Secure + SameSite=Strict, always (SPEC AC-20, BL-12)."""
    common = {
        "httponly": True,
        "secure": settings.cookie_secure,
        "samesite": settings.cookie_samesite,
        "path": "/",
    }
    response.set_cookie(
        settings.access_cookie_name,
        pair.access_token,
        max_age=settings.access_cookie_max_age,
        **common,
    )
    response.set_cookie(
        settings.refresh_cookie_name,
        pair.refresh_token,
        max_age=settings.refresh_cookie_max_age,
        **common,
    )


def clear_session_cookies(response: Response) -> None:
    for name in (settings.access_cookie_name, settings.refresh_cookie_name):
        response.delete_cookie(
            name,
            path="/",
            httponly=True,
            secure=settings.cookie_secure,
            samesite=settings.cookie_samesite,
        )


@router.post(
    "/auth/register",
    status_code=status.HTTP_201_CREATED,
    response_model=SessionOut,
    dependencies=[AuthRateLimit],
)
async def register(
    request: Request,
    response: Response,
    session: SessionDep,
    body: Annotated[RegisterRequest, Body()],
) -> SessionOut:
    out, pair = await service.register(
        session,
        body,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    # `pair is None` is the duplicate-email path: identical body, no cookies.
    if pair is not None:
        set_session_cookies(response, pair)
    return out


@router.post("/auth/login", response_model=SessionOut, dependencies=[AuthRateLimit])
async def login(
    request: Request, response: Response, session: SessionDep, body: LoginRequest
) -> SessionOut:
    out, pair = await service.login(
        session,
        body,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    set_session_cookies(response, pair)
    return out


@router.post("/auth/refresh", response_model=SessionOut, dependencies=[AuthRateLimit])
async def refresh(request: Request, response: Response, session: SessionDep) -> SessionOut:
    presented = request.cookies.get(settings.refresh_cookie_name)
    try:
        out, pair = await service.rotate_refresh(session, presented)
    except Exception:
        clear_session_cookies(response)
        raise
    set_session_cookies(response, pair)
    return out


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, session: SessionDep) -> Response:
    await service.logout(session, request.cookies.get(settings.access_cookie_name))
    clear_session_cookies(response)
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers=response.headers)


@router.get("/me", response_model=MeOut)
async def me(actor: ActorDep, session: SessionDep) -> MeOut:
    from ..db.models import Profile

    user = await session.get(Profile, actor.user_id)
    assert user is not None  # current_actor already proved the row exists
    return await service.build_me(session, user)


@router.get("/clinics", response_model=list[ClinicOut])
async def list_clinics(session: SessionDep) -> list[ClinicOut]:
    """Public — feeds the doctor-registration clinic selector (SPEC BL-21)."""
    rows = await session.execute(select(Clinic).order_by(Clinic.city, Clinic.name))
    return [ClinicOut(id=c.id, name=c.name, city=c.city) for c in rows.scalars().all()]
