"""The deployment's first account, and only that.

`POST /auth/session` authenticates against accounts, and a fresh deployment has none: without
this, "用户可以登录" is unreachable no matter what the routes do. Administration of accounts —
create, edit, deactivate, and the permission checks those carry — is C2.2's; this use case is
the one account a deployment cannot reach the system without, created by the deployment's own
command from credentials the deployment supplies, and guarded by the rule that it may never
create a second one.
"""

from __future__ import annotations

from factory_sop.auth.model import User, UserStatus
from factory_sop.auth.passwords import hash_password
from factory_sop.auth.repository import UserRepository
from factory_sop.identifiers import new_id
from factory_sop.observability import get_logger

_logger = get_logger("auth")


def register_first_operator(
    *,
    login_name: str,
    password: str,
    display_name: str,
    users: UserRepository,
) -> User | None:
    """Create the deployment's first account, or report that the store already has one.

    Returns the created `User`, or `None` when any account exists. Skipping is what makes the
    command idempotent across restarts with the deployment's credentials still set — and it is
    also the ceiling: a bootstrap that could mint further accounts from a start-up secret would
    keep minting them for as long as the secret stayed readable. The caller learns the outcome
    from the return value and the diagnostic line, not from an exception: nothing here failed.
    """
    if users.has_any():
        _logger.info("auth.bootstrap.skipped")
        return None

    user = User(
        id=new_id(),
        login_name=login_name,
        display_name=display_name,
        password_hash=hash_password(password),
        status=UserStatus.ACTIVE,
    )
    users.add(user)
    _logger.info("auth.bootstrap.created", user_id=str(user.id), login_name=login_name)
    return user
