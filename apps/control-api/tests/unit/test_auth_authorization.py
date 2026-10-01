"""The authorization decision itself: one function, one refusal, no ambient state.

§5.15 puts the `authorize` call in each module's `usecases/` layer rather than at the HTTP
route, because an ARQ worker, the report intake and a smoke script call those same use cases.
That only works if the decision needs nothing a request supplies — so it is a pure function
over a `Caller` the caller of the use case must hand it, and the tests here need no
application, no database and no clock.
"""

from __future__ import annotations

import io
import json

import pytest

from factory_sop.auth.authorization import (
    AuthorizationRefusedError,
    Caller,
    authorize,
    require_current_actor,
)
from factory_sop.auth.model import User, UserStatus
from factory_sop.auth.permissions import Permission
from factory_sop.identifiers import new_id
from factory_sop.observability import configure_logging


def caller(*granted: Permission) -> Caller:
    return Caller(
        user=User(
            id=new_id(),
            login_name="wang.li",
            display_name="王力",
            password_hash="argon2-encoded",  # pragma: allowlist secret
            status=UserStatus.ACTIVE,
        ),
        granted=frozenset(granted),
    )


def test_a_caller_holding_the_permission_is_allowed() -> None:
    authorize(caller(Permission.USER_EDIT), Permission.USER_EDIT)


def test_a_caller_without_the_permission_is_refused() -> None:
    with pytest.raises(AuthorizationRefusedError):
        authorize(caller(), Permission.USER_EDIT)


def test_holding_a_different_permission_does_not_grant_this_one() -> None:
    with pytest.raises(AuthorizationRefusedError):
        authorize(caller(Permission.USER_VIEW), Permission.USER_EDIT)


def test_edit_does_not_imply_delete() -> None:
    # §5.15 opens 删除 as its own operation precisely so an administrator can be given
    # day-to-day account management without the irreversible one. An implication here would
    # quietly undo that.
    with pytest.raises(AuthorizationRefusedError):
        authorize(caller(Permission.USER_EDIT), Permission.USER_DELETE)


def test_edit_does_not_imply_view() -> None:
    # No hierarchy in either direction. A role is a plain set (Q5/Q14), and an implication rule
    # would mean the set an administrator sees is not the set that is enforced.
    with pytest.raises(AuthorizationRefusedError):
        authorize(caller(Permission.USER_EDIT), Permission.USER_VIEW)


def test_the_permissions_of_several_roles_are_the_union() -> None:
    # A user may hold several roles (Q5/Q14), and what they may do is everything any of them
    # grants. The union is computed where roles are read; what this asserts is that `authorize`
    # is a plain membership test over whatever set it was handed.
    both = caller(Permission.USER_VIEW, Permission.ROLE_EDIT)

    authorize(both, Permission.USER_VIEW)
    authorize(both, Permission.ROLE_EDIT)


@pytest.mark.parametrize(
    ("granted", "allowed", "denied"),
    [
        (
            frozenset({Permission.USER_VIEW, Permission.ROLE_VIEW}),
            {Permission.USER_VIEW, Permission.ROLE_VIEW},
            {
                Permission.USER_EDIT,
                Permission.ROLE_EDIT,
                Permission.USER_DELETE,
                Permission.ROLE_DELETE,
            },
        ),
        (
            frozenset({Permission.USER_EDIT, Permission.ROLE_EDIT}),
            {Permission.USER_EDIT, Permission.ROLE_EDIT},
            {
                Permission.USER_VIEW,
                Permission.ROLE_VIEW,
                Permission.USER_DELETE,
                Permission.ROLE_DELETE,
            },
        ),
        (
            frozenset({Permission.USER_EDIT, Permission.USER_DELETE}),
            {Permission.USER_EDIT, Permission.USER_DELETE},
            {
                Permission.USER_VIEW,
                Permission.ROLE_VIEW,
                Permission.ROLE_EDIT,
                Permission.ROLE_DELETE,
            },
        ),
    ],
)
def test_permission_combinations_are_exact_sets(
    granted: frozenset[Permission],
    allowed: set[Permission],
    denied: set[Permission],
) -> None:
    # Roles are sets, not a hierarchy: combining two members must grant exactly those members,
    # with no implicit view/edit/delete inheritance across the same or another resource.
    subject = caller(*granted)

    for permission in allowed:
        authorize(subject, permission)
    for permission in denied:
        with pytest.raises(AuthorizationRefusedError):
            authorize(subject, permission)


def test_a_deactivated_caller_is_refused_whatever_it_holds() -> None:
    # Reachable in one request: an administrator deactivates an account whose session is live
    # and whose next request is already in flight. The session store is checked by
    # `restore_session`; this is the second gate, in the layer that decides.
    deactivated = Caller(
        user=User(
            id=new_id(),
            login_name="wang.li",
            display_name="王力",
            password_hash="argon2-encoded",  # pragma: allowlist secret
            status=UserStatus.DEACTIVATED,
        ),
        granted=frozenset({Permission.USER_EDIT}),
    )

    with pytest.raises(AuthorizationRefusedError):
        authorize(deactivated, Permission.USER_EDIT)


def test_the_refusal_carries_one_stable_error_code() -> None:
    with pytest.raises(AuthorizationRefusedError) as refused:
        authorize(caller(), Permission.USER_EDIT)

    assert refused.value.code.value == "PERMISSION_DENIED"


def test_a_refusal_is_a_diagnostic_event_naming_who_and_what() -> None:
    # AC4: a denial produces a stable diagnostic event. `event` is the stable identifier a
    # search finds (§5.15); the permission and the account are what an administrator answering
    # "why can't they do this" needs.
    stream = io.StringIO()
    configure_logging(log_level="info", stream=stream)
    refused_caller = caller()

    with pytest.raises(AuthorizationRefusedError):
        authorize(refused_caller, Permission.USER_EDIT)

    (line,) = [json.loads(entry) for entry in stream.getvalue().splitlines() if entry]
    assert line["event"] == "auth.authorization.refused"
    assert line["module"] == "auth"
    assert line["permission"] == "auth.user.edit"
    assert line["user_id"] == str(refused_caller.user.id)


def test_granting_does_not_log() -> None:
    # Every authorized request would otherwise emit a line per use case it touches, and the
    # denials — the ones an administrator is searching for — would be buried in them.
    stream = io.StringIO()
    configure_logging(log_level="info", stream=stream)

    authorize(caller(Permission.USER_EDIT), Permission.USER_EDIT)

    assert stream.getvalue() == ""


def test_require_current_actor_rechecks_the_persisted_account() -> None:
    # 后台 worker 在派发时按 created_by 复核：权限在入队时已检查，这里再看账号当前角色。
    current = caller(Permission.DATASET_EDIT)

    require_current_actor(
        actor_id=current.user.id,
        permissions=(Permission.DATASET_EDIT,),
        resolve_current_caller=lambda user_id: current,
    )


def test_require_current_actor_refuses_a_missing_or_deactivated_account() -> None:
    # 账号被删除或停用时解析器返回 None，按同一拒绝语义处理。
    with pytest.raises(AuthorizationRefusedError) as refused:
        require_current_actor(
            actor_id=new_id(),
            permissions=(Permission.DATASET_EDIT,),
            resolve_current_caller=lambda user_id: None,
        )

    assert refused.value.code.value == "PERMISSION_DENIED"


def test_require_current_actor_refuses_after_the_permission_is_revoked() -> None:
    # 撤销角色后，同一个持久化账号不再持有该权限。
    current = caller(Permission.DATASET_VIEW)

    with pytest.raises(AuthorizationRefusedError):
        require_current_actor(
            actor_id=current.user.id,
            permissions=(Permission.DATASET_EDIT,),
            resolve_current_caller=lambda user_id: current,
        )


def test_require_current_actor_requires_every_submitted_permission() -> None:
    # 提交用例同时要求读取前提与编辑时，worker 复核也要两项都满足。
    only_view = caller(Permission.DATASET_VIEW)

    with pytest.raises(AuthorizationRefusedError):
        require_current_actor(
            actor_id=only_view.user.id,
            permissions=(Permission.DATASET_VIEW, Permission.DATASET_EDIT),
            resolve_current_caller=lambda user_id: only_view,
        )

    require_current_actor(
        actor_id=only_view.user.id,
        permissions=(Permission.DATASET_VIEW,),
        resolve_current_caller=lambda user_id: only_view,
    )
