"""Managing roles: a role is a named set of registered permissions (Q5/Q14).

Every use case here begins with `authorize`, at the use-case boundary rather than on the HTTP
route (§5.15) — the ARQ worker and a smoke script reach these same functions, and a check placed
only on the route would leave the permission to each caller's discretion.

Nothing here reads a clock or the environment. A role has no timestamps: what an administrator
needs to know about a change is in the diagnostic log, and §5.15's Q37 is explicit that a
`created_at` column added in anticipation of an audit requirement nobody has stated is the wrong
shape to guess at. Who changed it is on the row (`created_by` / `updated_by`); when and what, in
the log.

The operations that can take a grant away — editing or deleting a role — acquire
`acquire_administration_lock` **before** they read anything, so two transactions cannot both
decide against the same pre-state and commit the permanent lockout the last-administration guard
exists to prevent (`auth/usecases/administration_guard.py`).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace
from typing import NoReturn
from uuid import UUID

from factory_sop.auth.authorization import Caller, authorize
from factory_sop.auth.errors import AdministrationRefusalCode, AdministrationRefusedError
from factory_sop.auth.model import Role
from factory_sop.auth.permissions import (
    Permission,
    UnregisteredPermissionError,
    parse_permission,
)
from factory_sop.auth.repository import RoleCodeTakenError, RoleNotFoundError, RoleRepository
from factory_sop.auth.usecases.administration_guard import guard_role_change
from factory_sop.identifiers import new_id
from factory_sop.observability import get_logger

_logger = get_logger("auth")


def list_roles(
    *, caller: Caller, roles: RoleRepository, page: int, page_size: int
) -> tuple[Sequence[Role], int]:
    """Return one ordered role page and its total."""
    authorize(caller, Permission.ROLE_VIEW)
    return roles.page(page=page, page_size=page_size)


def list_permissions(*, caller: Caller) -> Sequence[Permission]:
    """The catalogue of permissions a role may contain, sorted by value.

    The Web renders its checkboxes from this rather than from a list of its own, which is what
    makes AC2's "不能引入未注册权限" true at the screen as well as at the seam: a permission the
    backend does not register cannot be offered, and one it adds appears without a front-end
    change.

    Behind `ROLE_VIEW` because it is only meaningful next to the roles it describes; it reads no
    store and so takes none.
    """
    authorize(caller, Permission.ROLE_VIEW)
    return sorted(Permission, key=lambda permission: permission.value)


def create_role(
    *,
    caller: Caller,
    code: str,
    name: str,
    permissions: Iterable[str],
    roles: RoleRepository,
) -> Role:
    """Create a role from permission strings, refusing the whole thing if any is unregistered.

    No administration lock: a new role can only add grants, never take one away, so it cannot
    race the last-administration guard.
    """
    authorize(caller, Permission.ROLE_EDIT)
    granted = _parse_all(permissions, caller=caller)
    if roles.by_code(code) is not None:
        _refuse(
            AdministrationRefusalCode.ROLE_CODE_TAKEN,
            f"角色编码 {code} 已被占用",
            caller=caller,
            role_code=code,
        )

    role = Role(
        id=new_id(),
        code=code,
        name=name,
        permissions=granted,
        created_by=caller.user.id,
        updated_by=caller.user.id,
    )
    try:
        roles.add(role)
    except RoleCodeTakenError:
        # The pre-check is only an optimization; a concurrent creator can win between it and the
        # insert. Keep that race on the same stable refusal path as the fast check.
        _refuse(
            AdministrationRefusalCode.ROLE_CODE_TAKEN,
            f"角色编码 {code} 已被占用",
            caller=caller,
            role_code=code,
        )
    _log("auth.role.created", role=role, caller=caller)
    return role


def edit_role(
    *,
    caller: Caller,
    role_id: UUID,
    name: str,
    permissions: Iterable[str],
    roles: RoleRepository,
) -> Role:
    """Replace a role's name and permission set.

    The set is replaced rather than merged, matching the screen that submits it: a merge would
    make unticking a checkbox do nothing.
    """
    authorize(caller, Permission.ROLE_EDIT)
    # The lock before the read, for the same reason `deactivate_user` takes it: the guard's
    # answer has to reflect every administration-reducing write that has committed already.
    roles.acquire_administration_lock()
    granted = _parse_all(permissions, caller=caller)
    role = _require(role_id, roles, caller=caller)
    # Before the write, and with the set the role *would* have: the permission may be being
    # removed from the only role that grants it.
    guard_role_change(role_id=role_id, prospective=granted, roles=roles, actor_id=caller.user.id)

    edited = replace(role, name=name, permissions=granted, updated_by=caller.user.id)
    try:
        roles.update(edited)
    except RoleNotFoundError:
        _refuse(
            AdministrationRefusalCode.ROLE_NOT_FOUND,
            "角色不存在或已被删除",
            caller=caller,
            role_id=str(role_id),
        )
    _log("auth.role.edited", role=edited, caller=caller)
    return edited


def delete_role(*, caller: Caller, role_id: UUID, roles: RoleRepository) -> None:
    """Delete a role and every assignment of it.

    Its own permission, not `ROLE_EDIT`: §5.15 makes 删除 a separately opened operation, so an
    administrator can be given day-to-day role management without the irreversible half.
    """
    authorize(caller, Permission.ROLE_DELETE)
    roles.acquire_administration_lock()
    role = _require(role_id, roles, caller=caller)
    guard_role_change(role_id=role_id, prospective=None, roles=roles, actor_id=caller.user.id)

    try:
        roles.remove(role_id)
    except RoleNotFoundError:
        _refuse(
            AdministrationRefusalCode.ROLE_NOT_FOUND,
            "角色不存在或已被删除",
            caller=caller,
            role_id=str(role_id),
        )
    _log("auth.role.deleted", role=role, caller=caller)


def _refuse(
    code: AdministrationRefusalCode, detail: str, *, caller: Caller, **target: str
) -> NoReturn:
    """Log the refusal under a stable event name, then raise it.

    AC4 makes a refusal a diagnostic event; `target` carries the identifiers an administrator
    answering "why" needs — which role, which permission — and never a credential (§5.15).
    """
    _logger.info(
        "auth.role.refused",
        error_code=code.value,
        actor_id=str(caller.user.id),
        **target,
    )
    raise AdministrationRefusedError(code, detail)


def _parse_all(permissions: Iterable[str], *, caller: Caller) -> frozenset[Permission]:
    """Turn submitted strings into members, refusing the request whole if any is unregistered.

    Whole, rather than per entry: a role built from the members that happened to parse would be a
    role granting something nobody asked for, and the administrator would see a success.
    """
    parsed: set[Permission] = set()
    for value in permissions:
        try:
            parsed.add(parse_permission(value))
        except UnregisteredPermissionError as unregistered:
            _refuse(
                AdministrationRefusalCode.PERMISSION_UNREGISTERED,
                f"权限 {unregistered.value} 未注册，无法加入角色",
                caller=caller,
                permission=unregistered.value,
            )
    return frozenset(parsed)


def _require(role_id: UUID, roles: RoleRepository, *, caller: Caller) -> Role:
    role = roles.by_identifier(role_id)
    if role is None:
        _refuse(
            AdministrationRefusalCode.ROLE_NOT_FOUND,
            "角色不存在或已被删除",
            caller=caller,
            role_id=str(role_id),
        )
    return role


def _log(event: str, *, role: Role, caller: Caller) -> None:
    """One shape for every role-change line: who did it, to which role, by which code."""
    _logger.info(
        event,
        role_id=str(role.id),
        role_code=role.code,
        actor_id=str(caller.user.id),
    )
