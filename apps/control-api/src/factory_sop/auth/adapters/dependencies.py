"""What a protected route declares to get an authenticated caller.

The two dependencies here are the same for every module's routes: `Authenticated` resolves the
session cookie into a user, and the repositories come from the request's one transaction
(ADR-0002). Authorization is **not** here — §5.15 puts it in each module's `usecases/`, because
an ARQ worker and a smoke script call those too. What this file establishes is *who* is
calling, which a permission check needs before it can decide anything.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.auth.adapters.cookies import SESSION_COOKIE
from factory_sop.auth.adapters.repository import (
    PostgresSessionRepository,
    PostgresUserRepository,
)
from factory_sop.auth.errors import AuthenticationRefusedError, RefusalCode
from factory_sop.auth.model import SessionPolicy
from factory_sop.auth.repository import SessionRepository, UserRepository
from factory_sop.auth.tokens import SessionToken
from factory_sop.auth.usecases.sessions import RestoredSession, restore_session
from factory_sop.persistence import request_session


def users(session: Annotated[DatabaseSession, Depends(request_session)]) -> UserRepository:
    """`auth_user` on the request's transaction.

    Overridden in the adapter's own tests with an in-memory stand-in at this same seam, which
    is what lets cookie attributes and CSRF behavior be proved without a database.
    """
    return PostgresUserRepository(session)


def sessions(session: Annotated[DatabaseSession, Depends(request_session)]) -> SessionRepository:
    """`auth_session` on the request's transaction."""
    return PostgresSessionRepository(session)


def session_policy(request: Request) -> SessionPolicy:
    """The configured session lifetimes, resolved once at start-up."""
    policy: SessionPolicy = request.app.state.session_policy
    return policy


def presented_token(request: Request) -> SessionToken | None:
    """The session token in this request's cookie, if it carries one."""
    value = request.cookies.get(SESSION_COOKIE)
    return SessionToken(value=value) if value else None


def authenticated_caller(
    request: Request,
    users: Annotated[UserRepository, Depends(users)],
    sessions: Annotated[SessionRepository, Depends(sessions)],
    policy: Annotated[SessionPolicy, Depends(session_policy)],
) -> RestoredSession:
    """Resolve the caller, and slide their session's idle window forward.

    Refuses with `AUTHENTICATION_REQUIRED` when no cookie arrived and `SESSION_INVALID` when
    one did but is not usable. Both become a 401 `problem+json`, so a protected route answers
    an anonymous caller identically whichever of the two it was — the client's next move is the
    same: go to the login page.

    It hands back the session as well as the user because the session is what carries the
    lifetime, and a route reporting when it expires would otherwise have to look up by the
    token a second time.

    The clock is read here. `restore_session` takes `now` as an argument and the domain reads
    no clock, so this is the one place in the authentication path that knows the time.
    """
    token = presented_token(request)
    if token is None:
        raise AuthenticationRefusedError(RefusalCode.AUTHENTICATION_REQUIRED)
    return restore_session(
        token=token,
        users=users,
        sessions=sessions,
        policy=policy,
        now=datetime.now(UTC),
    )


# What a protected route annotates its caller parameter with. Named, so a route reads
# `caller: Authenticated` and every protected route in the backend says it the same way.
Authenticated = Annotated[RestoredSession, Depends(authenticated_caller)]
