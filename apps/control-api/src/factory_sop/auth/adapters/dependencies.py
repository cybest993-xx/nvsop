"""What a protected route declares to get an authenticated caller.

The dependencies here are the same for every module's routes: `Authenticated` resolves the
session cookie into a user, `Authorized` adds what that user may do, and the repositories come
from the request's one transaction (ADR-0002). Authorization is **not** here — §5.15 puts it in
each module's `usecases/`, because an ARQ worker and a smoke script call those too. What this
file establishes is *who* is calling and *what they hold*, which a permission check needs
before it can decide anything.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request

from factory_sop.auth.adapters.cookies import SESSION_COOKIE
from factory_sop.auth.adapters.repository import (
    PostgresRoleRepository,
    PostgresSessionRepository,
    PostgresUserRepository,
)
from factory_sop.auth.authorization import Caller
from factory_sop.auth.errors import AuthenticationRefusedError, RefusalCode
from factory_sop.auth.model import SessionPolicy
from factory_sop.auth.permissions import Permission
from factory_sop.auth.repository import RoleRepository, SessionRepository, UserRepository
from factory_sop.auth.tokens import SessionToken
from factory_sop.auth.usecases.sessions import RestoredSession, restore_session
from factory_sop.persistence import RequestSession

# The OpenAPI extension member a write route carries its permission in. `x-` prefixed, as
# OpenAPI requires of an extension, and read by the mechanical consistency check
# (`tests/unit/test_authorization_is_enforced.py`).
DECLARED_PERMISSION = "x-required-permission"
DECLARED_PERMISSIONS = "x-required-permissions"


def needs(*permissions: Permission) -> dict[str, str | list[str]]:
    """生成路由权限元数据；实际授权仍由用例层执行。"""
    if not permissions:
        raise ValueError("至少声明一项权限")
    values = [permission.value for permission in permissions]
    return {DECLARED_PERMISSION: values[0] if len(values) == 1 else values}


def needs_any(*permissions: Permission) -> dict[str, list[str]]:
    """返回允许任一权限的 OpenAPI 声明。"""
    if not permissions:
        raise ValueError("至少需要一个权限")
    return {DECLARED_PERMISSIONS: [permission.value for permission in permissions]}


def users(session: RequestSession) -> UserRepository:
    """`auth_user` on the request's transaction.

    Overridden in the adapter's own tests with an in-memory stand-in at this same seam, which
    is what lets cookie attributes and CSRF behavior be proved without a database.
    """
    return PostgresUserRepository(session)


def sessions(session: RequestSession) -> SessionRepository:
    """`auth_session` on the request's transaction."""
    return PostgresSessionRepository(session)


def roles(session: RequestSession) -> RoleRepository:
    """`auth_role`, `auth_role_permission` and `auth_user_role` on the request's transaction."""
    return PostgresRoleRepository(session)


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


def granted_permissions(
    caller: Authenticated, roles: Annotated[RoleRepository, Depends(roles)]
) -> frozenset[Permission]:
    """The union of everything the caller's roles grant, resolved once for this request.

    Its own dependency rather than a line inside `authenticated_caller` so that a test can supply
    a permission set directly, at this seam, instead of building a role for every combination it
    wants to exercise (harness §4).
    """
    return roles.permissions_of(caller.user.id)


def authorized_caller(
    caller: Authenticated,
    granted: Annotated[frozenset[Permission], Depends(granted_permissions)],
) -> Caller:
    """Who is calling and what they may do — the argument every administration use case takes.

    Assembled here, in the HTTP adapter, and passed down as a value. That is what lets the
    `authorize` call live in the use case (§5.15) without the use case needing a role repository:
    an ARQ worker or a smoke script builds the same `Caller` from the same two pieces.
    """
    return Caller(user=caller.user, granted=granted)


# What a route whose use case enforces a permission annotates its caller parameter with.
# `Authenticated` says who; this says who *and* what they may do.
Authorized = Annotated[Caller, Depends(authorized_caller)]
