"""Who is calling, and whether they may. The one authorization decision in the backend.

§5.15 puts the `authorize` call at the use-case boundary rather than on the HTTP route, because
the ARQ worker, the inference-host report intake and a smoke script all call the same use
cases; enforcing at the route would leave the permission to each caller's discretion. What
makes that enforceable rather than aspirational is the shape here: `authorize` is a pure
function over a `Caller` that the use case must be handed, so a caller with no identity cannot
reach a write use case without visibly passing something in — there is no ambient request, no
thread-local and no "system" default to fall back on.

Standard library only, and no import of `adapters` — an `import-linter` contract holds both.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn

from factory_sop.auth.model import User, UserStatus
from factory_sop.auth.permissions import Permission
from factory_sop.observability import get_logger

_logger = get_logger("auth")


class AuthorizationRefusalCode(StrEnum):
    """The `error_code` an authorization failure reports."""

    # One code for every denial, and deliberately not a per-permission family: what a client
    # does about it is the same in each case, and a code naming the permission would tell an
    # unauthorized caller which permission guards the resource.
    PERMISSION_DENIED = "PERMISSION_DENIED"


class AuthorizationRefusedError(Exception):
    """The caller is known and may not do this.

    A separate exception from `AuthenticationRefusedError`: that one means "we do not know who
    you are" and sends a browser to the login page, this one means "we do, and no" and must
    not. The HTTP layer maps them to 401 and 403 accordingly.
    """

    def __init__(self, permission: Permission) -> None:
        super().__init__(f"permission denied: {permission.value}")
        self.code = AuthorizationRefusalCode.PERMISSION_DENIED
        self.permission = permission


@dataclass(frozen=True, slots=True)
class Caller:
    """An identified caller and everything their roles grant, flattened.

    The permission set is resolved once, where the roles are read, and passed down as a value.
    Use cases therefore need no role repository to check a permission — which is what keeps a
    use case in some later module from having to reach into `auth`'s tables to authorize
    itself; it takes a `Caller` and calls `authorize`.

    Frozen, because it is the authorization input for the whole request: a use case that could
    add to `granted` would be a use case that could grant itself a permission.
    """

    user: User
    granted: frozenset[Permission]

    def holds(self, permission: Permission) -> bool:
        """Report whether this caller has `permission`, with no implication between members.

        `edit` does not imply `view` and does not imply `delete`. A role is a plain set
        (Q5/Q14), and an implication rule here would mean the set an administrator sees on the
        role screen is not the set that is enforced.
        """
        return self.user.status is UserStatus.ACTIVE and permission in self.granted


def authorize(caller: Caller, permission: Permission) -> None:
    """Allow the operation, or raise `AuthorizationRefusedError`.

    Returns `None` on success rather than a boolean, so the call cannot be written as an `if`
    whose empty branch silently proceeds. A use case reads:

        authorize(caller, Permission.USER_EDIT)

    as its first statement, and the permission it names is the one this backend enforces for
    that operation — there is no second place to keep in step.
    """
    if not caller.holds(permission):
        _refuse(caller, permission)


def _refuse(caller: Caller, permission: Permission) -> NoReturn:
    """Log the denial under a stable event name and raise it.

    Only denials are logged. A line per granted check would put one on every use case of every
    authorized request, and the denials an administrator is searching for would be buried in
    them. `login_name` is here as well as `user_id` because the question being answered is
    usually "why can't 王力 do this", and the identifier alone would need a second lookup.
    """
    _logger.info(
        "auth.authorization.refused",
        error_code=AuthorizationRefusalCode.PERMISSION_DENIED.value,
        permission=permission.value,
        user_id=str(caller.user.id),
        login_name=caller.user.login_name,
    )
    raise AuthorizationRefusedError(permission)
