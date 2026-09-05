"""The HTTP surface for managing roles, and the permission catalogue the screens render from.

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

Resource-shaped, like the session (§5.15). The account half of administration — users, their
status, their role assignments — lives in `user_administration.py`.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, Field

from factory_sop.auth.adapters.dependencies import Authorized, needs, roles
from factory_sop.auth.model import Role
from factory_sop.auth.permissions import Permission
from factory_sop.auth.repository import RoleRepository
from factory_sop.auth.usecases.roles import (
    create_role,
    delete_role,
    edit_role,
    list_permissions,
    list_roles,
)
from factory_sop.problem import problem_openapi_response
from factory_sop.responses import DEFAULT_PAGE_SIZE, MAXIMUM_PAGE_SIZE, ItemPage, paginate

router = APIRouter(prefix="/auth", tags=["auth"])

# The shape FastAPI's `responses` declares, so the shared dictionaries below and the literals
# that extend them both satisfy it (its keys admit `int | str`).
ProblemResponses = dict[int | str, dict[str, Any]]

# What every route here answers an unauthenticated or unauthorized caller with. Declared once:
# the handlers in `app.py` produce exactly these, and repeating the mapping per route would let
# one route drift out of it.
_UNAUTHORIZED: ProblemResponses = {
    401: problem_openapi_response("Authentication required or session invalid"),
    403: problem_openapi_response("Permission denied or CSRF token invalid"),
}

# The catalogue is the permission vocabulary itself: nothing to take or to conflict with.
_CATALOGUE_RESPONSES: ProblemResponses = {
    **_UNAUTHORIZED,
    422: problem_openapi_response("Page or page size out of range"),
}
_PAGE_OR_PATH_INVALID: ProblemResponses = {
    **_UNAUTHORIZED,
    422: problem_openapi_response("Request invalid"),
}


class RoleView(BaseModel):
    """A role and its permission set, sorted so the response is stable between requests."""

    id: UUID
    code: str
    name: str
    permissions: list[str]


class NewRole(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    # Plain strings on the wire, not an enum. A value the backend does not register has to come
    # back as `PERMISSION_UNREGISTERED` from the use case that owns the rule, naming the offending
    # entry; typing it as the enum here would answer with a validation error against a list of
    # every permission that does exist, which is both less useful and a longer response.
    permissions: list[str]


class EditedRole(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    permissions: list[str]


def _role_view(role: Role) -> RoleView:
    return RoleView(
        id=role.id,
        code=role.code,
        name=role.name,
        permissions=sorted(item.value for item in role.permissions),
    )


@router.get("/permissions", operation_id="listPermissions", responses=_CATALOGUE_RESPONSES)
def read_the_permission_catalogue(
    caller: Authorized,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAXIMUM_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ItemPage[str]:
    """Every permission a role may contain, in the §5.15 list envelope.

    The role screen renders its checkboxes from this rather than from a list of its own, so a
    permission the backend registers appears without a front-end change and one it does not
    register cannot be offered.
    """
    items, total = paginate(
        [permission.value for permission in list_permissions(caller=caller)],
        page=page,
        page_size=page_size,
    )
    return ItemPage(items=items, page=page, page_size=page_size, total=total)


@router.get("/roles", operation_id="listRoles", responses=_PAGE_OR_PATH_INVALID)
def read_the_roles(
    caller: Authorized,
    roles: Annotated[RoleRepository, Depends(roles)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAXIMUM_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ItemPage[RoleView]:
    """Every role, with its permission set, in the §5.15 list envelope."""
    listed, total = list_roles(
        caller=caller,
        roles=roles,
        page=page,
        page_size=page_size,
    )
    return ItemPage(
        items=[_role_view(role) for role in listed],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.post(
    "/roles",
    status_code=status.HTTP_201_CREATED,
    operation_id="createRole",
    openapi_extra=needs(Permission.ROLE_EDIT),
    responses={
        **_UNAUTHORIZED,
        409: problem_openapi_response("Role code already taken"),
        422: problem_openapi_response("Request invalid, or a permission is not registered"),
    },
)
def create_a_role(
    submitted: NewRole,
    caller: Authorized,
    roles: Annotated[RoleRepository, Depends(roles)],
) -> RoleView:
    """Create a role from registered permission strings."""
    created = create_role(
        caller=caller,
        code=submitted.code,
        name=submitted.name,
        permissions=submitted.permissions,
        roles=roles,
    )
    return _role_view(created)


@router.put(
    "/roles/{role_id}",
    operation_id="editRole",
    openapi_extra=needs(Permission.ROLE_EDIT),
    responses={
        **_UNAUTHORIZED,
        404: problem_openapi_response("Role not found"),
        409: problem_openapi_response(
            "Role code already taken, or the edit would leave the system unadministrable"
        ),
        422: problem_openapi_response("Request invalid, or a permission is not registered"),
    },
)
def edit_a_role(
    role_id: UUID,
    submitted: EditedRole,
    caller: Authorized,
    roles: Annotated[RoleRepository, Depends(roles)],
) -> RoleView:
    """Replace a role's name and permission set. `PUT`, because the set is replaced whole."""
    edited = edit_role(
        caller=caller,
        role_id=role_id,
        name=submitted.name,
        permissions=submitted.permissions,
        roles=roles,
    )
    return _role_view(edited)


@router.delete(
    "/roles/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="deleteRole",
    openapi_extra=needs(Permission.ROLE_DELETE),
    responses={
        **_UNAUTHORIZED,
        404: problem_openapi_response("Role not found"),
        409: problem_openapi_response("The deletion would leave the system unadministrable"),
        422: problem_openapi_response("Request invalid"),
    },
)
def delete_a_role(
    role_id: UUID,
    caller: Authorized,
    roles: Annotated[RoleRepository, Depends(roles)],
) -> Response:
    """Delete a role and every assignment of it."""
    delete_role(caller=caller, role_id=role_id, roles=roles)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
