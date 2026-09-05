"""The session as one resource: `POST` opens it, `GET` reads it, `DELETE` ends it.

Login and logout are that resource's methods rather than two verb-shaped paths, which keeps the
control plane resource-shaped throughout (§5.15) and leaves one obvious place for "who am I" —
the `GET`, which is what the Web shell calls on load to find out whether it already has a
session.

The route declares its permission as metadata only; the enforcement is in the use case
(§5.15). These three need none: they are how a caller acquires an identity in the first place.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field

from factory_sop.auth.adapters.cookies import attach_session, clear_session
from factory_sop.auth.adapters.dependencies import (
    Authenticated,
    presented_token,
    session_policy,
    sessions,
    users,
)
from factory_sop.auth.model import SessionPolicy, User
from factory_sop.auth.repository import SessionRepository, UserRepository
from factory_sop.auth.usecases.sessions import open_session, revoke_session
from factory_sop.problem import problem_openapi_response
from factory_sop.settings import Settings

router = APIRouter(prefix="/auth", tags=["auth"])


class Credentials(BaseModel):
    """What the login form submits."""

    # Bounded so an oversized body is refused by the contract rather than reaching Argon2.
    login_name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class SessionView(BaseModel):
    """Who the caller is and when their session ends.

    `expires_at` is here so the Web shell can warn before a session lapses rather than
    discovering it through a failed request mid-form. It moves on every request that slides the
    idle window, which is why it is read from the policy rather than stored.
    """

    user_id: UUID
    login_name: str
    display_name: str
    expires_at: datetime


def _view(user: User, *, expires_at: datetime) -> SessionView:
    return SessionView(
        user_id=user.id,
        login_name=user.login_name,
        display_name=user.display_name,
        expires_at=expires_at,
    )


@router.post(
    "/session",
    status_code=status.HTTP_201_CREATED,
    operation_id="openSession",
    responses={
        401: problem_openapi_response("Credentials rejected"),
        403: problem_openapi_response("Account deactivated"),
        422: problem_openapi_response("Request invalid"),
    },
)
def open_a_session(
    credentials: Credentials,
    request: Request,
    response: Response,
    users: Annotated[UserRepository, Depends(users)],
    sessions: Annotated[SessionRepository, Depends(sessions)],
    policy: Annotated[SessionPolicy, Depends(session_policy)],
) -> SessionView:
    """Log in. Refuses with `CREDENTIALS_REJECTED` or `ACCOUNT_DEACTIVATED`.

    The one route that must not require a session cookie or a CSRF token: there is no session
    yet, and the credential in the body is what authorizes the call.
    """
    settings: Settings = request.app.state.settings
    opened = open_session(
        login_name=credentials.login_name,
        password=credentials.password,
        users=users,
        sessions=sessions,
        policy=policy,
        now=datetime.now(UTC),
    )
    attach_session(
        response,
        token=opened.token,
        settings=settings,
        max_age_seconds=int(policy.absolute_lifetime.total_seconds()),
    )
    return _view(opened.user, expires_at=policy.expires_at(opened.session))


@router.get(
    "/session",
    operation_id="readSession",
    responses={401: problem_openapi_response("Authentication required or session invalid")},
)
def read_the_session(
    caller: Authenticated,
    policy: Annotated[SessionPolicy, Depends(session_policy)],
) -> SessionView:
    """Report the caller's own session. What the Web shell calls to restore one on load.

    Reaching it at all slid the idle window forward, in `authenticated_caller`, so the
    `expires_at` reported here is the one that now applies.
    """
    return _view(caller.user, expires_at=policy.expires_at(caller.session))


@router.delete(
    "/session",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="endSession",
    responses={403: problem_openapi_response("CSRF token invalid")},
)
def end_the_session(
    request: Request,
    sessions: Annotated[SessionRepository, Depends(sessions)],
) -> Response:
    """Log out: revoke the session server-side and clear both cookies.

    It does not depend on `Authenticated`. A caller whose session has already lapsed still
    wants their cookies gone, and answering a logout with a 401 would leave the browser holding
    a cookie it cannot use and no way to discard it. Idempotent for the same reason.

    It **is** CSRF-protected, by the middleware that covers every modifying request: a
    cross-origin page able to force a logout is a denial of service on a shop-floor terminal.

    The response is constructed here rather than taking the injected one and copying its
    headers across. Two cookies mean two `Set-Cookie` headers, and a `dict` of the headers
    keeps only the last of them — which cleared the session cookie and left the CSRF cookie in
    the browser.
    """
    settings: Settings = request.app.state.settings
    token = presented_token(request)
    if token is not None:
        revoke_session(token=token, sessions=sessions)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session(response, settings=settings)
    return response
