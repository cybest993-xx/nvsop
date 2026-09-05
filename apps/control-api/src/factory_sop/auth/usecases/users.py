"""Managing accounts: create, edit, reset, deactivate, reactivate, delete, and assign roles.

AC1 and AC4. Two of the operations here are the ones the acceptance criteria are really about:

- **Deactivating revokes every session in the same call.** Not a sweep, not a background job, not
  a second request the HTTP layer makes: one business operation inside one transaction
  (ADR-0002). An account refused at the login form while its open browsers keep working is not
  deactivated in any sense an operator means by the word.
- **Deleting is opened by its own permission.** §5.15 makes 停用 the reversible path and 删除 a
  separate operation, so an administrator can be given day-to-day account management without the
  irreversible half.

The operations that can take a grant away — deactivating, deleting, reassigning roles — acquire
`acquire_administration_lock` **before** they read anything, so two transactions cannot both
decide against the same pre-state and commit the permanent lockout the last-administration guard
exists to prevent (`auth/usecases/administration_guard.py`). The lock is the first read.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import NoReturn
from uuid import UUID

from factory_sop.auth.authorization import Caller, authorize
from factory_sop.auth.errors import AdministrationRefusalCode, AdministrationRefusedError
from factory_sop.auth.model import User, UserStatus
from factory_sop.auth.passwords import MINIMUM_PASSWORD_LENGTH, hash_password
from factory_sop.auth.permissions import Permission
from factory_sop.auth.repository import (
    LoginNameTakenError,
    RoleRepository,
    SessionRepository,
    UserNotFoundError,
    UserRepository,
)
from factory_sop.auth.usecases.administration_guard import guard_assignment, guard_user_leaving
from factory_sop.identifiers import new_id
from factory_sop.observability import get_logger

_logger = get_logger("auth")


def list_users(
    *, caller: Caller, users: UserRepository, page: int, page_size: int
) -> tuple[Sequence[User], int]:
    """Return one ordered account page and its total, including deactivated accounts."""
    authorize(caller, Permission.USER_VIEW)
    return users.page(page=page, page_size=page_size)


def create_user(
    *,
    caller: Caller,
    login_name: str,
    display_name: str,
    password: str,
    users: UserRepository,
) -> User:
    """Create an active local account.

    Active on creation rather than pending an activation step: there is no e-mail in this
    deployment (§六), so a pending state would need an administrator to flip it and would be a
    state nothing else acts on. It holds no roles until one is assigned — an account that can
    log in and do nothing is the safe direction.
    """
    authorize(caller, Permission.USER_EDIT)
    _require_usable_password(password, caller=caller)
    if users.by_login_name(login_name) is not None:
        _refuse(
            AdministrationRefusalCode.LOGIN_NAME_TAKEN,
            f"登录名 {login_name} 已被占用",
            caller=caller,
            login_name=login_name,
        )

    user = User(
        id=new_id(),
        login_name=login_name,
        display_name=display_name,
        password_hash=hash_password(password),
        status=UserStatus.ACTIVE,
        created_by=caller.user.id,
        updated_by=caller.user.id,
    )
    try:
        users.add(user)
    except LoginNameTakenError:
        # The pre-check is only an optimization; a concurrent creator can win between it and the
        # insert. Keep that race on the same stable refusal path as the fast check.
        _refuse(
            AdministrationRefusalCode.LOGIN_NAME_TAKEN,
            f"登录名 {login_name} 已被占用",
            caller=caller,
            login_name=login_name,
        )
    _log("auth.user.created", user=user, caller=caller)
    return user


def edit_user(*, caller: Caller, user_id: UUID, display_name: str, users: UserRepository) -> User:
    """Change an account's display name.

    The login name is not editable. It is the natural key an Excel import matches on (§5.15) and
    the key every diagnostic line records an operator by; renaming it would break that trail
    retroactively. A different person is a different account.
    """
    authorize(caller, Permission.USER_EDIT)
    user = _require(user_id, users, caller=caller)

    edited = replace(user, display_name=display_name, updated_by=caller.user.id)
    try:
        users.update(edited)
    except UserNotFoundError:
        _refuse(
            AdministrationRefusalCode.USER_NOT_FOUND,
            "账户不存在或已被删除",
            caller=caller,
            user_id=str(user_id),
        )
    _log("auth.user.edited", user=edited, caller=caller)
    return edited


def reset_password(*, caller: Caller, user_id: UUID, password: str, users: UserRepository) -> None:
    """Set a new password without presenting the old one.

    An administrator resetting a forgotten password does not have the old one, which is what
    separates this from an operator changing their own — that path presents the current password
    and belongs to whoever owns the account, and it is not in this slice.

    Open sessions are deliberately left alone. A password reset is routine (an operator forgot
    it), and signing them out of the terminal they are standing at helps nobody; taking an account
    away is what 停用 is for, and it does revoke them.
    """
    authorize(caller, Permission.USER_EDIT)
    _require_usable_password(password, caller=caller)
    user = _require(user_id, users, caller=caller)

    updated = replace(user, password_hash=hash_password(password), updated_by=caller.user.id)
    try:
        users.update(updated)
    except UserNotFoundError:
        _refuse(
            AdministrationRefusalCode.USER_NOT_FOUND,
            "账户不存在或已被删除",
            caller=caller,
            user_id=str(user_id),
        )
    # The password does not appear here in any form — not the value, not its length, not the hash
    # (§5.15 forbids a credential in a diagnostic line).
    _log("auth.user.password_reset", user=updated, caller=caller)


@dataclass(frozen=True, slots=True)
class DeactivatedUser:
    """The deactivated account and how many sessions went with it.

    The count is returned rather than discarded so the response can tell the administrator "已停
    用，同时下线 3 个会话". Silence about it would leave them wondering whether the operator
    standing at a terminal is still signed in.
    """

    user: User
    revoked_sessions: int


def deactivate_user(
    *,
    caller: Caller,
    user_id: UUID,
    users: UserRepository,
    sessions: SessionRepository,
    roles: RoleRepository,
) -> DeactivatedUser:
    """Take an account out of service and revoke all of its sessions, in one operation.

    Both writes are in the caller's transaction (ADR-0002), so the account cannot end up
    deactivated with its sessions surviving, nor the reverse.
    """
    authorize(caller, Permission.USER_EDIT)
    # The lock is taken before anything is read: the guard's answer must reflect every
    # administration-reducing write that has already happened, not the state this transaction
    # happened to observe.
    roles.acquire_administration_lock()
    user = _require(user_id, users, caller=caller)
    guard_user_leaving(user_id=user_id, roles=roles, actor_id=caller.user.id)

    deactivated = replace(user, status=UserStatus.DEACTIVATED, updated_by=caller.user.id)
    try:
        users.update(deactivated)
    except UserNotFoundError:
        _refuse(
            AdministrationRefusalCode.USER_NOT_FOUND,
            "账户不存在或已被删除",
            caller=caller,
            user_id=str(user_id),
        )
    revoked = sessions.remove_every_session_of(user_id)
    _log("auth.user.deactivated", user=deactivated, caller=caller, revoked_sessions=revoked)
    return DeactivatedUser(user=deactivated, revoked_sessions=revoked)


def reactivate_user(*, caller: Caller, user_id: UUID, users: UserRepository) -> User:
    """Put a deactivated account back in service (`CONTEXT.md`: 恢复).

    It does not restore the sessions that were revoked — those rows are gone, and the operator
    logs in again. Reactivation only ever widens what is possible, so it needs no guard.
    """
    authorize(caller, Permission.USER_EDIT)
    user = _require(user_id, users, caller=caller)

    reactivated = replace(user, status=UserStatus.ACTIVE, updated_by=caller.user.id)
    try:
        users.update(reactivated)
    except UserNotFoundError:
        _refuse(
            AdministrationRefusalCode.USER_NOT_FOUND,
            "账户不存在或已被删除",
            caller=caller,
            user_id=str(user_id),
        )
    _log("auth.user.reactivated", user=reactivated, caller=caller)
    return reactivated


def delete_user(
    *,
    caller: Caller,
    user_id: UUID,
    users: UserRepository,
    sessions: SessionRepository,
    roles: RoleRepository,
) -> None:
    """Delete an account outright, with its sessions and role assignments.

    `USER_DELETE`, never `USER_EDIT` (§5.15). The sessions are revoked explicitly rather than
    left to the foreign key's `ON DELETE CASCADE`: the cascade is the database's backstop against
    an orphan row, and a use case that relied on it would behave differently against a store
    without one — which is exactly what the in-memory stand-in is.
    """
    authorize(caller, Permission.USER_DELETE)
    roles.acquire_administration_lock()
    user = _require(user_id, users, caller=caller)
    guard_user_leaving(user_id=user_id, roles=roles, actor_id=caller.user.id)

    sessions.remove_every_session_of(user_id)
    try:
        users.remove(user_id)
    except UserNotFoundError:
        _refuse(
            AdministrationRefusalCode.USER_NOT_FOUND,
            "账户不存在或已被删除",
            caller=caller,
            user_id=str(user_id),
        )
    _log("auth.user.deleted", user=user, caller=caller)


def assign_roles(
    *,
    caller: Caller,
    user_id: UUID,
    role_ids: Iterable[UUID],
    users: UserRepository,
    roles: RoleRepository,
) -> frozenset[UUID]:
    """Replace an account's roles with exactly `role_ids`, and return what it now holds.

    Replace rather than add, matching the screen: the administrator submits the set the account
    should have. An empty set is a legitimate submission and means the account holds no
    permissions.

    Behind `USER_EDIT` rather than `ROLE_EDIT`: this changes an account, not a role. The
    distinction matters — an administrator who may staff the shop floor's accounts should not
    thereby be able to redefine what a role grants.
    """
    authorize(caller, Permission.USER_EDIT)
    roles.acquire_administration_lock()
    # The target is validated here, before anything is written: the assignment rows carry a
    # foreign key to `auth_user`, and an unknown account would otherwise surface as a database
    # integrity error — a 500 — while an empty list would reach the not-found answer. Whether
    # the account exists cannot depend on what the request body happened to contain.
    _require(user_id, users, caller=caller)
    requested = list(role_ids)
    for role_id in requested:
        if roles.by_identifier(role_id) is None:
            _refuse(
                AdministrationRefusalCode.ROLE_NOT_FOUND,
                "指定的角色不存在或已被删除",
                caller=caller,
                user_id=str(user_id),
                role_id=str(role_id),
            )
    guard_assignment(user_id=user_id, role_ids=requested, roles=roles, actor_id=caller.user.id)

    roles.assign(user_id=user_id, role_ids=requested)
    _logger.info(
        "auth.user.roles_assigned",
        user_id=str(user_id),
        role_ids=sorted(str(role_id) for role_id in requested),
        actor_id=str(caller.user.id),
    )
    return frozenset(requested)


def _refuse(
    code: AdministrationRefusalCode, detail: str, *, caller: Caller, **target: str
) -> NoReturn:
    """Log the refusal under a stable event name, then raise it.

    AC4 makes a refusal a diagnostic event, and the client-visible `error_code` alone cannot
    answer "why was this refused at 14:32" — the line carries the actor and the safe target
    identifiers (a login name, an account id). A password never appears in `target`, in any
    form (§5.15).
    """
    _logger.info(
        "auth.user.refused",
        error_code=code.value,
        actor_id=str(caller.user.id),
        **target,
    )
    raise AdministrationRefusedError(code, detail)


def _require_usable_password(password: str, *, caller: Caller) -> None:
    """Refuse a password below the minimum length (`auth/passwords.py`)."""
    if len(password) < MINIMUM_PASSWORD_LENGTH:
        _refuse(
            AdministrationRefusalCode.PASSWORD_TOO_SHORT,
            f"密码至少需要 {MINIMUM_PASSWORD_LENGTH} 个字符",
            caller=caller,
        )


def _require(user_id: UUID, users: UserRepository, *, caller: Caller) -> User:
    user = users.by_identifier(user_id)
    if user is None:
        _refuse(
            AdministrationRefusalCode.USER_NOT_FOUND,
            "账户不存在或已被删除",
            caller=caller,
            user_id=str(user_id),
        )
    return user


def _log(event: str, *, user: User, caller: Caller, **extra: object) -> None:
    """One shape for every account-change line.

    `user_id` is who it was done to and `actor_id` is who did it; both are on every line, because
    a line with only one of them cannot distinguish an administrative change from a self-service
    one. No credential appears in any of them (§5.15).
    """
    _logger.info(
        event,
        user_id=str(user.id),
        login_name=user.login_name,
        actor_id=str(caller.user.id),
        **extra,
    )
