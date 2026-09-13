"""The HTTP surface for managing accounts: create, edit, reset, 停用, 恢复, delete, assign roles.

Each route here declares the permission it needs in its OpenAPI extension — `needs`, from
`adapters/dependencies` — and **does not enforce it**. Enforcement is the use case's, at the
boundary an ARQ worker and a smoke script also cross (§5.15). The declaration is metadata: it is
what the generated OpenAPI document shows an integrator, and what
`tests/unit/test_authorization_is_enforced.py` checks the use case against by behaviour, so the
two cannot drift apart silently.

Every route also declares the `problem+json` responses it can actually answer with — the custom
handlers in `app.py` produce them at runtime, and a route that left them undeclared would export
a contract whose documented failure shape is FastAPI's default rather than the real one. Lists
are served in §5.15's one envelope (`factory_sop/responses.py`), paginated by the query
parameters the envelope expects.

Resource-shaped, like the session (§5.15): 停用 and 恢复 are one `PUT` on a `status` subresource
rather than two verb paths, because they are the two values of one field and an operator toggling
them is performing the same operation in two directions. The role half of administration lives in
`role_administration.py`.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, Field

from factory_sop.auth.adapters.dependencies import Authorized, needs, roles, sessions, users
from factory_sop.auth.errors import AdministrationRefusalCode, AdministrationRefusedError
from factory_sop.auth.model import User, UserStatus
from factory_sop.auth.passwords import MINIMUM_PASSWORD_LENGTH
from factory_sop.auth.permissions import Permission
from factory_sop.auth.repository import RoleRepository, SessionRepository, UserRepository
from factory_sop.auth.usecases.users import (
    assign_roles,
    create_user,
    deactivate_user,
    delete_user,
    edit_user,
    list_users,
    reactivate_user,
    reset_password,
)
from factory_sop.problem import problem_openapi_response
from factory_sop.responses import DEFAULT_PAGE_SIZE, MAXIMUM_PAGE_SIZE, ItemPage

router = APIRouter(prefix="/auth", tags=["auth"])

# The shape FastAPI's `responses` declares, so the shared dictionaries below and the
# literals that extend them both satisfy it (its keys admit `int | str`).
ProblemResponses = dict[int | str, dict[str, Any]]

# What every route here answers an unauthenticated or unauthorized caller with (see
# `role_administration.py` for why this is declared once rather than composed per route).
_UNAUTHORIZED: ProblemResponses = {
    401: problem_openapi_response("Authentication required or session invalid"),
    403: problem_openapi_response("Permission denied or CSRF token invalid"),
}

_ACCOUNT_NOT_FOUND: ProblemResponses = {
    404: problem_openapi_response("Account not found"),
}
_CONFLICT: ProblemResponses = {
    409: problem_openapi_response(
        "Login name taken, or the change would leave the system unadministrable"
    ),
}
_PAGE_OR_PATH_INVALID: ProblemResponses = {
    **_UNAUTHORIZED,
    422: problem_openapi_response("Request invalid"),
}


class UserView(BaseModel):
    """An account as the administration screens show it.

    No `password_hash`. Not because it is secret from an administrator who could reset it anyway,
    but because a hash on the wire is a hash in a browser's cache, a proxy log and a screenshot,
    and nothing on the screen has a use for it.
    """

    id: UUID
    login_name: str
    display_name: str
    status: UserStatus
    role_ids: list[UUID]


class NewUser(BaseModel):
    login_name: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    # The same minimum the use case enforces, imported rather than repeated as a literal: a form
    # can then refuse a short password without a round trip, and the two cannot disagree.
    password: str = Field(min_length=MINIMUM_PASSWORD_LENGTH, max_length=1024)


class EditedUser(BaseModel):
    display_name: str = Field(min_length=1, max_length=128)


class NewPassword(BaseModel):
    password: str = Field(min_length=MINIMUM_PASSWORD_LENGTH, max_length=1024)


class RequestedStatus(BaseModel):
    """Which of the two states the account should be in.

    `UserStatus` itself, so an unknown value is refused by the contract as a `field_errors[]` entry
    rather than reaching a branch that would have to decide what a third status means.
    """

    status: UserStatus


class AssignedRoles(BaseModel):
    """The complete set of roles the account should hold. An empty list is a valid submission."""

    role_ids: list[UUID]


class StatusChanged(BaseModel):
    """The account's new state, and how many sessions the change closed.

    `revoked_sessions` is on the response so the screen can say "已停用，同时下线 3 个会话".
    Always present, and `0` on a reactivation — a field that appeared only sometimes would have
    the front end branching on its existence.
    """

    user: UserView
    revoked_sessions: int


def _user_view(user: User, *, role_ids: frozenset[UUID]) -> UserView:
    return UserView(
        id=user.id,
        login_name=user.login_name,
        display_name=user.display_name,
        status=user.status,
        role_ids=sorted(role_ids),
    )


@router.get("/users", operation_id="listUsers", responses=_PAGE_OR_PATH_INVALID)
def read_the_users(
    caller: Authorized,
    users: Annotated[UserRepository, Depends(users)],
    roles: Annotated[RoleRepository, Depends(roles)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAXIMUM_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ItemPage[UserView]:
    """Every account, deactivated ones included, in the §5.15 list envelope."""
    listed, total = list_users(
        caller=caller,
        users=users,
        page=page,
        page_size=page_size,
    )
    return ItemPage(
        items=[_user_view(user, role_ids=roles.role_ids_of(user.id)) for user in listed],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.post(
    "/users",
    status_code=status.HTTP_201_CREATED,
    operation_id="createUser",
    openapi_extra=needs(Permission.USER_EDIT),
    responses={
        **_UNAUTHORIZED,
        409: problem_openapi_response("Login name already taken"),
        422: problem_openapi_response("Request invalid, or the password is too short"),
    },
)
def create_an_account(
    submitted: NewUser,
    caller: Authorized,
    users: Annotated[UserRepository, Depends(users)],
) -> UserView:
    """Create an active account. It can log in immediately; it holds no roles until assigned."""
    created = create_user(
        caller=caller,
        login_name=submitted.login_name,
        display_name=submitted.display_name,
        password=submitted.password,
        users=users,
    )
    return _user_view(created, role_ids=frozenset())


@router.patch(
    "/users/{user_id}",
    operation_id="editUser",
    openapi_extra=needs(Permission.USER_EDIT),
    responses={
        **_UNAUTHORIZED,
        **_ACCOUNT_NOT_FOUND,
        422: problem_openapi_response("Request invalid"),
    },
)
def edit_an_account(
    user_id: UUID,
    submitted: EditedUser,
    caller: Authorized,
    users: Annotated[UserRepository, Depends(users)],
    roles: Annotated[RoleRepository, Depends(roles)],
) -> UserView:
    """Change the display name. `PATCH`, because the login name is deliberately not editable."""
    edited = edit_user(
        caller=caller, user_id=user_id, display_name=submitted.display_name, users=users
    )
    return _user_view(edited, role_ids=roles.role_ids_of(user_id))


@router.put(
    "/users/{user_id}/password",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="resetUserPassword",
    openapi_extra=needs(Permission.USER_EDIT),
    responses={
        **_UNAUTHORIZED,
        **_ACCOUNT_NOT_FOUND,
        422: problem_openapi_response("Request invalid, or the password is too short"),
    },
)
def reset_an_account_password(
    user_id: UUID,
    submitted: NewPassword,
    caller: Authorized,
    users: Annotated[UserRepository, Depends(users)],
) -> Response:
    """Set a new password without presenting the old one.

    204 with no body: there is nothing to report that the administrator does not already know, and
    a response echoing any part of the request would put a password in a browser's cache.
    """
    reset_password(caller=caller, user_id=user_id, password=submitted.password, users=users)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put(
    "/users/{user_id}/status",
    operation_id="setUserStatus",
    openapi_extra=needs(Permission.USER_EDIT),
    responses={
        **_UNAUTHORIZED,
        **_ACCOUNT_NOT_FOUND,
        **_CONFLICT,
        422: problem_openapi_response("Request invalid"),
    },
)
def set_an_account_status(
    user_id: UUID,
    submitted: RequestedStatus,
    caller: Authorized,
    users: Annotated[UserRepository, Depends(users)],
    sessions: Annotated[SessionRepository, Depends(sessions)],
    roles: Annotated[RoleRepository, Depends(roles)],
) -> StatusChanged:
    """停用 or 恢复, as one operation on one field.

    Both directions need `USER_EDIT`, which is what lets a single route declare a single
    permission: they are the reversible state of a mutable configuration object (§5.15), not two
    operations of different weight. Deactivating revokes every session of the account inside the
    same transaction as the status write (ADR-0002).
    """
    if submitted.status is UserStatus.DEACTIVATED:
        outcome = deactivate_user(
            caller=caller, user_id=user_id, users=users, sessions=sessions, roles=roles
        )
        return StatusChanged(
            user=_user_view(outcome.user, role_ids=roles.role_ids_of(user_id)),
            revoked_sessions=outcome.revoked_sessions,
        )
    reactivated = reactivate_user(caller=caller, user_id=user_id, users=users)
    return StatusChanged(
        user=_user_view(reactivated, role_ids=roles.role_ids_of(user_id)),
        revoked_sessions=0,
    )


@router.put(
    "/users/{user_id}/roles",
    operation_id="setUserRoles",
    openapi_extra=needs(Permission.USER_EDIT),
    responses={
        **_UNAUTHORIZED,
        **_ACCOUNT_NOT_FOUND,
        **_CONFLICT,
        422: problem_openapi_response("Request invalid"),
    },
)
def set_the_roles_of_an_account(
    user_id: UUID,
    submitted: AssignedRoles,
    caller: Authorized,
    users: Annotated[UserRepository, Depends(users)],
    roles: Annotated[RoleRepository, Depends(roles)],
) -> UserView:
    """Replace the account's roles with exactly the submitted set.

    `USER_EDIT`, not `ROLE_EDIT`: this changes an account. An administrator who may staff the shop
    floor's accounts should not thereby be able to redefine what a role grants.

    The use case validates the account before it writes anything, so an unknown `user_id` is
    `USER_NOT_FOUND` whether or not the submission carried roles.
    """
    assign_roles(
        caller=caller, user_id=user_id, role_ids=submitted.role_ids, users=users, roles=roles
    )
    stored = users.by_identifier(user_id)
    if stored is None:  # pragma: no cover - assign_roles validated the account under the lock
        raise AdministrationRefusedError(
            AdministrationRefusalCode.USER_NOT_FOUND, "账户不存在或已被删除"
        )
    return _user_view(stored, role_ids=roles.role_ids_of(user_id))


@router.delete(
    "/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="deleteUser",
    openapi_extra=needs(Permission.USER_DELETE),
    responses={
        **_UNAUTHORIZED,
        **_ACCOUNT_NOT_FOUND,
        409: problem_openapi_response("The deletion would leave the system unadministrable"),
        422: problem_openapi_response("Request invalid"),
    },
)
def delete_an_account(
    user_id: UUID,
    caller: Authorized,
    users: Annotated[UserRepository, Depends(users)],
    sessions: Annotated[SessionRepository, Depends(sessions)],
    roles: Annotated[RoleRepository, Depends(roles)],
) -> Response:
    """Delete an account with its sessions and assignments. `USER_DELETE`, never `USER_EDIT`."""
    delete_user(caller=caller, user_id=user_id, users=users, sessions=sessions, roles=roles)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
