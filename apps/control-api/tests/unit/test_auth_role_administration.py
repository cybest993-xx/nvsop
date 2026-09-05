"""Managing roles: a role is a named set of registered permissions, and nothing else.

AC2. What is asserted here is mostly about what a role may *not* contain — §5.15 makes the
permission enumeration closed, so the interesting cases are the strings that must be refused
rather than the ones that work. The store is the in-memory stand-in at the repository seam
(harness §4): none of these rules needs PostgreSQL to be decided.
"""

from __future__ import annotations

import io
import json

import pytest
from auth_fakes import FakeRoles, FakeUsers, caller_holding

from factory_sop.auth.authorization import AuthorizationRefusedError
from factory_sop.auth.errors import AdministrationRefusalCode, AdministrationRefusedError
from factory_sop.auth.model import Role
from factory_sop.auth.permissions import Permission
from factory_sop.auth.repository import RoleCodeTakenError, RoleNotFoundError
from factory_sop.auth.usecases.roles import (
    create_role,
    delete_role,
    edit_role,
    list_permissions,
    list_roles,
)
from factory_sop.identifiers import new_id
from factory_sop.observability import configure_logging


@pytest.fixture
def store() -> FakeRoles:
    return FakeRoles()


def test_a_role_is_created_from_registered_permission_strings(store: FakeRoles) -> None:
    role = create_role(
        caller=caller_holding(Permission.ROLE_EDIT),
        code="device_administrator",
        name="设备管理员",
        permissions=["auth.user.view", "auth.role.view"],
        roles=store,
    )

    assert role.code == "device_administrator"
    assert role.name == "设备管理员"
    assert role.permissions == frozenset({Permission.USER_VIEW, Permission.ROLE_VIEW})


def test_the_stored_role_holds_enum_members_not_strings(store: FakeRoles) -> None:
    # AC2's "不能引入裸字符串权限". The strings arrive from HTTP; what the domain keeps is
    # `Permission` members, so a comparison against a typo cannot silently be `False`.
    created = create_role(
        caller=caller_holding(Permission.ROLE_EDIT),
        code="viewer",
        name="只读",
        permissions=["auth.user.view"],
        roles=store,
    )

    stored = store.by_identifier(created.id)
    assert stored is not None
    assert all(isinstance(item, Permission) for item in stored.permissions)


def test_an_unregistered_permission_is_refused_and_nothing_is_stored(store: FakeRoles) -> None:
    with pytest.raises(AdministrationRefusedError) as refused:
        create_role(
            caller=caller_holding(Permission.ROLE_EDIT),
            code="approver",
            name="审批人",
            permissions=["auth.user.view", "auth.user.approve"],
            roles=store,
        )

    assert refused.value.code is AdministrationRefusalCode.PERMISSION_UNREGISTERED
    # Validated as a whole before anything is written: a role half-built from the members that
    # happened to parse would be a role granting something nobody asked for.
    assert store.by_code("approver") is None


def test_the_refusal_names_the_offending_permission(store: FakeRoles) -> None:
    with pytest.raises(AdministrationRefusedError) as refused:
        create_role(
            caller=caller_holding(Permission.ROLE_EDIT),
            code="approver",
            name="审批人",
            permissions=["auth.user.approve"],
            roles=store,
        )

    # The administrator submitted a list; the message has to say which entry was wrong.
    assert "auth.user.approve" in refused.value.detail


def test_two_roles_cannot_share_a_code(store: FakeRoles) -> None:
    author = caller_holding(Permission.ROLE_EDIT)
    create_role(caller=author, code="viewer", name="只读", permissions=[], roles=store)

    with pytest.raises(AdministrationRefusedError) as refused:
        create_role(caller=author, code="viewer", name="另一个只读", permissions=[], roles=store)

    assert refused.value.code is AdministrationRefusalCode.ROLE_CODE_TAKEN


def test_a_unique_conflict_after_the_precheck_is_refused_as_role_code_taken(
    store: FakeRoles,
) -> None:
    # Concurrent requests can both observe an unused code before one reaches the database unique
    # constraint. The repository reports that race at the seam; the use case must preserve the
    # stable API refusal instead of leaking an IntegrityError.
    class RacingRoles(FakeRoles):
        def by_code(self, code: str) -> Role | None:
            return None

        def add(self, role: Role) -> None:
            raise RoleCodeTakenError(role.code)

    with pytest.raises(AdministrationRefusedError) as refused:
        create_role(
            caller=caller_holding(Permission.ROLE_EDIT),
            code="viewer",
            name="只读",
            permissions=[],
            roles=RacingRoles(),
        )

    assert refused.value.code is AdministrationRefusalCode.ROLE_CODE_TAKEN


def test_a_role_with_no_permissions_is_allowed(store: FakeRoles) -> None:
    # Useful on purpose: a role is created and then filled in, and an empty one grants nothing,
    # which is the safe direction. Refusing it would only push the administrator to put a
    # placeholder permission in.
    role = create_role(
        caller=caller_holding(Permission.ROLE_EDIT),
        code="placeholder",
        name="待定",
        permissions=[],
        roles=store,
    )

    assert role.permissions == frozenset()


def test_editing_a_role_replaces_its_permission_set(store: FakeRoles) -> None:
    author = caller_holding(Permission.ROLE_EDIT)
    role = create_role(
        caller=author,
        code="viewer",
        name="只读",
        permissions=["auth.user.view", "auth.role.view"],
        roles=store,
    )

    edited = edit_role(
        caller=author,
        role_id=role.id,
        name="只读（用户）",
        permissions=["auth.user.view"],
        roles=store,
    )

    # Replaced rather than merged: the screen shows a set of checkboxes, and a merge would make
    # unticking one do nothing.
    assert edited.permissions == frozenset({Permission.USER_VIEW})
    assert edited.name == "只读（用户）"


def test_editing_after_the_role_vanishes_is_not_reported_as_success(store: FakeRoles) -> None:
    class VanishingRoles(FakeRoles):
        def update(self, role: Role) -> None:
            self.by_id.pop(role.id, None)
            raise RoleNotFoundError(role.id)

    roles = VanishingRoles()
    role = create_role(
        caller=caller_holding(Permission.ROLE_EDIT),
        code="vanishing",
        name="即将消失",
        permissions=[],
        roles=roles,
    )

    with pytest.raises(AdministrationRefusedError) as refused:
        edit_role(
            caller=caller_holding(Permission.ROLE_EDIT),
            role_id=role.id,
            name="已消失",
            permissions=[],
            roles=roles,
        )

    assert refused.value.code is AdministrationRefusalCode.ROLE_NOT_FOUND


def test_editing_a_role_that_does_not_exist_is_refused(store: FakeRoles) -> None:
    with pytest.raises(AdministrationRefusedError) as refused:
        edit_role(
            caller=caller_holding(Permission.ROLE_EDIT),
            role_id=new_id(),
            name="不存在",
            permissions=[],
            roles=store,
        )

    assert refused.value.code is AdministrationRefusalCode.ROLE_NOT_FOUND


def test_a_role_is_deleted_by_its_own_permission(store: FakeRoles) -> None:
    role = create_role(
        caller=caller_holding(Permission.ROLE_EDIT),
        code="obsolete",
        name="废弃",
        permissions=[],
        roles=store,
    )

    delete_role(caller=caller_holding(Permission.ROLE_DELETE), role_id=role.id, roles=store)

    assert store.by_identifier(role.id) is None


def test_editing_does_not_authorize_deleting(store: FakeRoles) -> None:
    role = create_role(
        caller=caller_holding(Permission.ROLE_EDIT),
        code="obsolete",
        name="废弃",
        permissions=[],
        roles=store,
    )

    with pytest.raises(AuthorizationRefusedError):
        delete_role(caller=caller_holding(Permission.ROLE_EDIT), role_id=role.id, roles=store)


def test_creating_a_role_needs_role_edit(store: FakeRoles) -> None:
    with pytest.raises(AuthorizationRefusedError):
        create_role(
            caller=caller_holding(Permission.USER_EDIT),
            code="viewer",
            name="只读",
            permissions=[],
            roles=store,
        )


def test_listing_roles_needs_role_view(store: FakeRoles) -> None:
    with pytest.raises(AuthorizationRefusedError):
        list_roles(caller=caller_holding(Permission.USER_VIEW), roles=store, page=1, page_size=50)


def test_roles_are_listed_in_a_stable_order(store: FakeRoles) -> None:
    author = caller_holding(Permission.ROLE_EDIT)
    for code in ("reviewer", "operator", "administrator"):
        create_role(caller=author, code=code, name=code, permissions=[], roles=store)

    listed, total = list_roles(
        caller=caller_holding(Permission.ROLE_VIEW), roles=store, page=1, page_size=50
    )

    # By code, not by creation order: the screen is a list an administrator scans, and an order
    # that depends on when a row was written makes the same list look different every visit.
    assert [role.code for role in listed] == ["administrator", "operator", "reviewer"]
    assert total == 3


def test_the_permission_catalogue_is_what_the_role_screen_offers(store: FakeRoles) -> None:
    # The Web renders checkboxes from this rather than from a list of its own, so a permission
    # added to the backend appears without a front-end change — and one that is not registered
    # cannot be offered at all.
    catalogue = list_permissions(caller=caller_holding(Permission.ROLE_VIEW))

    assert [item.value for item in catalogue] == sorted(item.value for item in Permission)


def test_the_catalogue_needs_role_view(store: FakeRoles) -> None:
    with pytest.raises(AuthorizationRefusedError):
        list_permissions(caller=caller_holding(Permission.USER_VIEW))


def test_a_role_change_is_a_diagnostic_event(store: FakeRoles) -> None:
    # AC4: an entity change produces a stable diagnostic event naming the operator and the
    # target (§5.15).
    stream = io.StringIO()
    configure_logging(log_level="info", stream=stream)
    author = caller_holding(Permission.ROLE_EDIT)

    role = create_role(
        caller=author, code="viewer", name="只读", permissions=["auth.user.view"], roles=store
    )

    (line,) = [json.loads(entry) for entry in stream.getvalue().splitlines() if entry]
    assert line["event"] == "auth.role.created"
    assert line["module"] == "auth"
    assert line["role_id"] == str(role.id)
    assert line["role_code"] == "viewer"
    assert line["actor_id"] == str(author.user.id)


def test_the_last_administration_role_cannot_be_deleted(store: FakeRoles) -> None:
    # Not in the acceptance criteria, and included anyway: `auth.user.edit` and `auth.role.edit`
    # are the permissions without which no other permission can be restored. Deleting the only
    # role that grants them leaves a system nobody can administer, and no screen can fix it —
    # the recovery is SQL against production. Refusing the operation is the cheap half of that
    # trade.
    users = FakeUsers()
    store.users = users
    administration = create_role(
        caller=caller_holding(Permission.ROLE_EDIT),
        code="system_administrator",
        name="系统管理员",
        permissions=["auth.user.edit", "auth.role.edit"],
        roles=store,
    )
    holder = users.register(login_name="wang.li", password="assembly-line-3")
    store.assign(user_id=holder.id, role_ids=[administration.id])

    with pytest.raises(AdministrationRefusedError) as refused:
        delete_role(
            caller=caller_holding(Permission.ROLE_DELETE), role_id=administration.id, roles=store
        )

    assert refused.value.code is AdministrationRefusalCode.ADMINISTRATION_WOULD_BE_LOST
    assert store.by_identifier(administration.id) is not None


def test_administration_may_be_moved_from_one_role_to_another(store: FakeRoles) -> None:
    # The guard is about the permission surviving somewhere, not about a particular role being
    # sacred. With a second holder in place the first role is deletable.
    users = FakeUsers()
    store.users = users
    author = caller_holding(Permission.ROLE_EDIT)
    first = create_role(
        caller=author,
        code="system_administrator",
        name="系统管理员",
        permissions=["auth.user.edit", "auth.role.edit"],
        roles=store,
    )
    second = create_role(
        caller=author,
        code="platform_owner",
        name="平台负责人",
        permissions=["auth.user.edit", "auth.role.edit"],
        roles=store,
    )
    holder = users.register(login_name="wang.li", password="assembly-line-3")
    store.assign(user_id=holder.id, role_ids=[first.id, second.id])

    delete_role(caller=caller_holding(Permission.ROLE_DELETE), role_id=first.id, roles=store)

    assert store.by_identifier(first.id) is None


def test_administration_cannot_be_edited_out_of_the_only_role_that_grants_it(
    store: FakeRoles,
) -> None:
    # The same loss by a different route: keep the role, drop the permission from it.
    users = FakeUsers()
    store.users = users
    author = caller_holding(Permission.ROLE_EDIT)
    administration = create_role(
        caller=author,
        code="system_administrator",
        name="系统管理员",
        permissions=["auth.user.edit", "auth.role.edit"],
        roles=store,
    )
    holder = users.register(login_name="wang.li", password="assembly-line-3")
    store.assign(user_id=holder.id, role_ids=[administration.id])

    with pytest.raises(AdministrationRefusedError) as refused:
        edit_role(
            caller=author,
            role_id=administration.id,
            name="系统管理员",
            permissions=["auth.user.view"],
            roles=store,
        )

    assert refused.value.code is AdministrationRefusalCode.ADMINISTRATION_WOULD_BE_LOST
    surviving = store.by_identifier(administration.id)
    assert surviving is not None
    assert Permission.ROLE_EDIT in surviving.permissions


def test_a_role_nobody_administers_with_is_deletable(store: FakeRoles) -> None:
    # The guard protects a capability that exists; it does not require one to exist. On an empty
    # database — before the first administrator is created — refusing this would not produce an
    # administrator, it would only stop a fresh system from being built.
    role = create_role(
        caller=caller_holding(Permission.ROLE_EDIT),
        code="viewer",
        name="只读",
        permissions=["auth.user.view"],
        roles=store,
    )

    delete_role(caller=caller_holding(Permission.ROLE_DELETE), role_id=role.id, roles=store)

    assert store.by_identifier(role.id) is None


def test_an_unregistered_permission_is_a_refusal_event(store: FakeRoles) -> None:
    # AC4 covers refusals: the line names which submitted value was unregistered and who
    # submitted it, so the administrator does not have to resend the request to find out.
    stream = io.StringIO()
    configure_logging(log_level="info", stream=stream)
    author = caller_holding(Permission.ROLE_EDIT)

    with pytest.raises(AdministrationRefusedError):
        create_role(
            caller=author,
            code="approver",
            name="审批人",
            permissions=["auth.user.approve"],
            roles=store,
        )

    (line,) = [json.loads(entry) for entry in stream.getvalue().splitlines() if entry]
    assert line["event"] == "auth.role.refused"
    assert line["error_code"] == "PERMISSION_UNREGISTERED"
    assert line["permission"] == "auth.user.approve"
    assert line["actor_id"] == str(author.user.id)


def test_the_role_guard_refusal_is_a_diagnostic_event(store: FakeRoles) -> None:
    stream = io.StringIO()
    configure_logging(log_level="info", stream=stream)
    users = FakeUsers()
    store.users = users
    author = caller_holding(Permission.ROLE_EDIT)
    administration = create_role(
        caller=author,
        code="system_administrator",
        name="系统管理员",
        permissions=["auth.user.edit", "auth.role.edit"],
        roles=store,
    )
    holder = users.register(login_name="wang.li", password="assembly-line-3")
    store.assign(user_id=holder.id, role_ids=[administration.id])

    deleter = caller_holding(Permission.ROLE_DELETE)

    with pytest.raises(AdministrationRefusedError):
        delete_role(caller=deleter, role_id=administration.id, roles=store)

    (line,) = [
        json.loads(entry)
        for entry in stream.getvalue().splitlines()
        if entry and json.loads(entry)["event"] == "auth.administration.refused"
    ]
    assert line["error_code"] == "ADMINISTRATION_WOULD_BE_LOST"
    assert line["permission"] == "auth.role.edit"
    assert line["role_id"] == str(administration.id)
    assert line["actor_id"] == str(deleter.user.id)
