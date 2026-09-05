"""The one invariant that survives no screen: somebody must still be able to administer.

`auth.user.edit` and `auth.role.edit` are the two permissions from which every other permission
can be restored. An operation that leaves no active account holding them leaves a system that
cannot be repaired through the product — the recovery is SQL against production, by someone with
the database password, which is the situation this file exists to prevent.

Not in #23's acceptance criteria, and here deliberately: the criteria describe the operations,
and this is the one way to perform them correctly that no acceptance test would have caught.
There are four routes to the same loss — delete the role, edit the permission out of the role,
unassign the role, deactivate or delete the holder — so the check is a function each of them
calls rather than a rule written once next to one of them.

The check runs **before** the write, against the store as it is once the caller has taken
`acquire_administration_lock` — the lock is what makes "as it still is" true under concurrency,
because it serializes every operation that can take a grant away, and the write happens in the
same transaction under the same lock. Each caller says how its own change would alter the
answer; that is why there are three entry points rather than one: the delta has a different
shape in each case, and a single generic one would take a callback that each caller would have
to get right anyway.

It guards a capability that currently exists rather than requiring one to exist. When no active
account holds a permission — an empty database before the first administrator is created — there
is nothing to lose, and refusing an operation would not produce an administrator; it would only
make a fresh system unable to build one.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import NoReturn
from uuid import UUID

from factory_sop.auth.errors import AdministrationRefusalCode, AdministrationRefusedError
from factory_sop.auth.permissions import Permission
from factory_sop.auth.repository import RoleRepository
from factory_sop.observability import get_logger

_logger = get_logger("auth")

# Without both of these, no permission can be granted to anybody ever again. `view` is not among
# them: an administrator who cannot read the list can still fix the grant, blind but able.
ADMINISTRATION_PERMISSIONS = frozenset({Permission.USER_EDIT, Permission.ROLE_EDIT})


def guard_role_change(
    *,
    role_id: UUID,
    prospective: frozenset[Permission] | None,
    roles: RoleRepository,
    actor_id: UUID,
) -> None:
    """Refuse a role deletion or edit that would remove the last grant of administration.

    `prospective` is the permission set the role would have afterwards, or `None` for a deletion.
    The permission survives if some other role still grants it to an active account, or if this
    role would still grant it and has an active holder.

    The caller has already taken `acquire_administration_lock`; this function only reads and
    decides, and the write it guards happens in the same transaction under the same lock.
    """
    for permission in sorted(ADMINISTRATION_PERMISSIONS):
        if not roles.active_holders_of(permission):
            continue
        elsewhere = roles.active_holders_of(permission, ignoring_role=role_id)
        kept_here = (
            prospective is not None
            and permission in prospective
            and bool(roles.active_assignees_of(role_id))
        )
        if not elsewhere and not kept_here:
            _refuse(
                permission,
                actor_id=actor_id,
                role_id=str(role_id),
            )


def guard_user_leaving(*, user_id: UUID, roles: RoleRepository, actor_id: UUID) -> None:
    """Refuse deactivating or deleting the last account able to administer.

    Reachable by the shortest route there is: one administrator who deactivates themselves, and
    then cannot put themselves back.
    """
    for permission in sorted(ADMINISTRATION_PERMISSIONS):
        holders = roles.active_holders_of(permission)
        if holders and not holders - {user_id}:
            _refuse(permission, actor_id=actor_id, user_id=str(user_id))


def guard_assignment(
    *, user_id: UUID, role_ids: Iterable[UUID], roles: RoleRepository, actor_id: UUID
) -> None:
    """Refuse a role assignment that would strip the last administrator of administration.

    The prospective permission set is the union of the roles being assigned, so this covers both
    "the roles are being emptied" and "they are being swapped for roles that grant less".
    """
    prospective: set[Permission] = set()
    for role_id in role_ids:
        role = roles.by_identifier(role_id)
        if role is not None:
            prospective |= role.permissions

    for permission in sorted(ADMINISTRATION_PERMISSIONS):
        holders = roles.active_holders_of(permission)
        if not holders:
            continue
        if not holders - {user_id} and permission not in prospective:
            _refuse(permission, actor_id=actor_id, user_id=str(user_id))


def _refuse(permission: Permission, *, actor_id: UUID, **target: str) -> NoReturn:
    """Log the near-loss under a stable event name and raise with it.

    AC4 covers refusals, and this one is the refusal an administrator most needs explained: the
    line names the permission that would have lost its last holder and what the operation was
    touching, so the incident ("why can nobody delete this role?") reads straight from the log.
    """
    _logger.info(
        "auth.administration.refused",
        error_code=AdministrationRefusalCode.ADMINISTRATION_WOULD_BE_LOST.value,
        permission=permission.value,
        actor_id=str(actor_id),
        **target,
    )
    raise AdministrationRefusedError(
        AdministrationRefusalCode.ADMINISTRATION_WOULD_BE_LOST,
        f"该操作会使系统再无任何在用账户持有 {permission.value} 权限，之后无人能管理用户与角色",
    )
