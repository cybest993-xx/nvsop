"""The deployment's first account — and the one role that makes it an administrator.

Every write use case requires a `Caller` holding a permission (§5.15). On an empty database
nobody can be one, so a fresh installation would have no administrator and no way to create
one: the administration screens would be unreachable on the machine they were built for. This
use case is the way out of that deadlock, created by the deployment's own command from
credentials the deployment supplies.

What keeps it from being a way around authorization is its precondition: it skips once any
account exists at all — not "once an administrator exists", which could be gamed by deleting
the administrator, and not "once this login name is taken", which a different name would
sidestep. The window in which it works is the window in which there is nothing to protect.
"""

from __future__ import annotations

from uuid import UUID

from factory_sop.auth.errors import AdministrationRefusalCode, AdministrationRefusedError
from factory_sop.auth.model import Role, User, UserStatus
from factory_sop.auth.passwords import MINIMUM_PASSWORD_LENGTH, hash_password
from factory_sop.auth.permissions import Permission
from factory_sop.auth.repository import RoleRepository, UserRepository
from factory_sop.identifiers import new_id
from factory_sop.observability import get_logger

_logger = get_logger("auth")

# The one role this use case seeds, and the only role the product ships with.
#
# §5.4 forbids a preset role catalogue: roles are created and named by an administrator inside
# the system, the permission enumeration is fixed and the roles are not (Q5/Q14). This single
# seed is not that catalogue, and it is not optional either — permissions reach an account only
# through a role, so a database with no role at all is one where the first administrator holds
# nothing and therefore cannot create the role that would grant them anything. One seed breaks
# that deadlock and nothing more: it is an ordinary editable row, which an administrator can
# rename, narrow, or delete once they have built the roles their site actually wants. Every
# *other* role is theirs to define, not ours to ship.
ADMINISTRATOR_ROLE_CODE = "system_administrator"
ADMINISTRATOR_ROLE_NAME = "系统管理员"


def register_first_operator(
    *,
    login_name: str,
    password: str,
    display_name: str,
    users: UserRepository,
    roles: RoleRepository,
) -> User | None:
    """Create the deployment's first account with a role granting every registered permission.

    Returns the created `User`, or `None` when any account exists. Skipping is what makes the
    command idempotent across restarts with the deployment's credentials still set — and it is
    also the ceiling: a bootstrap that could mint further accounts from a start-up secret would
    keep minting them for as long as the secret stayed readable. The caller learns the outcome
    from the return value and the diagnostic line, not from an exception: nothing here failed.

    Every registered permission, not a curated subset: the named roles §5.4 lists are made of
    permissions belonging to modules that do not exist yet, and this account is what an
    administrator uses to build them once they do. A curated subset would leave some permission
    grantable by nobody.
    """
    if len(password) < MINIMUM_PASSWORD_LENGTH:
        _logger.info(
            "auth.bootstrap.refused",
            error_code=AdministrationRefusalCode.PASSWORD_TOO_SHORT.value,
            login_name=login_name,
        )
        raise AdministrationRefusedError(
            AdministrationRefusalCode.PASSWORD_TOO_SHORT,
            f"密码至少需要 {MINIMUM_PASSWORD_LENGTH} 个字符",
        )
    if not users.claim_bootstrap():
        _logger.info("auth.bootstrap.skipped", login_name=login_name)
        return None

    # The identifier is minted before the rows are built so both can name their creator: there
    # is no caller to attribute a bootstrap to — the deployment itself ran the command — so the
    # account is recorded as its own creator (§5.15 carries 变更归属 in these columns).
    user_id: UUID = new_id()
    user = User(
        id=user_id,
        login_name=login_name,
        display_name=display_name,
        password_hash=hash_password(password),
        status=UserStatus.ACTIVE,
        created_by=user_id,
        updated_by=user_id,
    )
    role = Role(
        id=new_id(),
        code=ADMINISTRATOR_ROLE_CODE,
        name=ADMINISTRATOR_ROLE_NAME,
        permissions=frozenset(Permission),
        created_by=user_id,
        updated_by=user_id,
    )
    # Granted through a role rather than attached to the account: permissions reach an account
    # only by assignment (Q5/Q14), so the role screen shows why this account can do everything.
    roles.add(role)
    users.add(user)
    roles.assign(user_id=user.id, role_ids=[role.id])
    _logger.info(
        "auth.bootstrap.created",
        user_id=str(user.id),
        login_name=login_name,
        role_code=role.code,
    )
    return user
