"""Managing accounts: create, edit, deactivate, reactivate, delete, and assign roles.

AC1 and AC4. Two properties carry most of the weight and neither is visible from the HTTP
surface: deactivating an account revokes its sessions **in the same business operation** as the
deactivation, and 删除 is opened by its own permission rather than by 编辑. Both are asserted
against the in-memory stores at the repository seam (harness §4).
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta

import pytest
from auth_fakes import FakeRoles, FakeSessions, FakeUsers, caller_holding

from factory_sop.auth.authorization import AuthorizationRefusedError
from factory_sop.auth.errors import AdministrationRefusalCode, AdministrationRefusedError
from factory_sop.auth.model import SessionPolicy, User, UserStatus
from factory_sop.auth.passwords import verify_password
from factory_sop.auth.permissions import Permission
from factory_sop.auth.repository import LoginNameTakenError, UserNotFoundError
from factory_sop.auth.usecases.roles import create_role
from factory_sop.auth.usecases.sessions import open_session
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
from factory_sop.identifiers import new_id
from factory_sop.observability import configure_logging

# Synthetic fixture passwords (§4). Defined once, here, so every call site reads the same
# values and none of them is a credential the scanner needs to see inline.
PASSWORD = "assembly-line-3"  # pragma: allowlist secret
NEW_PASSWORD = "assembly-line-4"  # pragma: allowlist secret
RESET_PASSWORD = "assembly-line-9"  # pragma: allowlist secret
SHORT_PASSWORD = "short"  # pragma: allowlist secret
KEEPER_PASSWORD = "keeper-password-1"  # pragma: allowlist secret

POLICY = SessionPolicy(idle_timeout=timedelta(hours=12), absolute_lifetime=timedelta(days=30))
MONDAY_MORNING = datetime(2026, 9, 7, 1, 0, tzinfo=UTC)


class Store:
    """The three stand-ins a user use case may touch, kept together for brevity."""

    def __init__(self) -> None:
        self.users = FakeUsers()
        self.roles = FakeRoles(users=self.users)
        self.sessions = FakeSessions()

    def administration_elsewhere(self) -> None:
        """Give some other account the administration permissions.

        The last-administrator guard is about the permission surviving somewhere. A test that is
        not about that guard needs a second holder in place, or every deactivation it performs
        would be refused for a reason it is not asserting.
        """
        role = create_role(
            caller=caller_holding(Permission.ROLE_EDIT),
            code="system_administrator",
            name="系统管理员",
            permissions=["auth.user.edit", "auth.role.edit"],
            roles=self.roles,
        )
        keeper = self.users.register(login_name="keeper", password=KEEPER_PASSWORD)
        self.roles.assign(user_id=keeper.id, role_ids=[role.id])


@pytest.fixture
def store() -> Store:
    built = Store()
    built.administration_elsewhere()
    return built


def test_an_account_is_created_and_can_be_found_by_its_login_name(store: Store) -> None:
    created = create_user(
        caller=caller_holding(Permission.USER_EDIT),
        login_name="wang.li",
        display_name="王力",
        password=PASSWORD,
        users=store.users,
    )

    assert created.login_name == "wang.li"
    assert created.display_name == "王力"
    assert created.status is UserStatus.ACTIVE
    assert store.users.by_login_name("wang.li") is not None


def test_a_created_account_stores_a_hash_and_not_the_password(store: Store) -> None:
    created = create_user(
        caller=caller_holding(Permission.USER_EDIT),
        login_name="wang.li",
        display_name="王力",
        password=PASSWORD,
        users=store.users,
    )

    assert created.password_hash != PASSWORD
    assert verify_password(
        password=PASSWORD,
        stored_hash=created.password_hash,
    )


def test_the_created_account_names_who_created_it(store: Store) -> None:
    # §5.15 carries 变更归属 in `created_by` / `updated_by`; the diagnostic log carries the
    # trail. The row itself has to answer "who made this account" without a log search.
    author = caller_holding(Permission.USER_EDIT)
    created = create_user(
        caller=author,
        login_name="wang.li",
        display_name="王力",
        password=PASSWORD,
        users=store.users,
    )

    assert created.created_by == author.user.id
    assert created.updated_by == author.user.id


def test_a_created_account_can_log_in_immediately(store: Store) -> None:
    # The one test that proves creation and the login path agree about the hash: a use case that
    # stored a differently encoded hash would pass every assertion above and lock the operator
    # out on their first shift.
    create_user(
        caller=caller_holding(Permission.USER_EDIT),
        login_name="wang.li",
        display_name="王力",
        password=PASSWORD,
        users=store.users,
    )

    opened = open_session(
        login_name="wang.li",
        password=PASSWORD,
        users=store.users,
        sessions=store.sessions,
        policy=POLICY,
        now=MONDAY_MORNING,
    )

    assert opened.user.login_name == "wang.li"


def test_two_accounts_cannot_share_a_login_name(store: Store) -> None:
    author = caller_holding(Permission.USER_EDIT)
    create_user(
        caller=author,
        login_name="wang.li",
        display_name="王力",
        password=PASSWORD,
        users=store.users,
    )

    with pytest.raises(AdministrationRefusedError) as refused:
        create_user(
            caller=author,
            login_name="wang.li",
            display_name="另一个王力",
            password=NEW_PASSWORD,
            users=store.users,
        )

    assert refused.value.code is AdministrationRefusalCode.LOGIN_NAME_TAKEN


def test_a_unique_conflict_after_the_precheck_is_refused_as_login_name_taken(store: Store) -> None:
    # Two requests can both observe an unused name before one reaches the database unique
    # constraint. The repository reports that race at the seam; the use case must turn it into
    # the same stable refusal as its fast pre-check rather than leaking an IntegrityError.
    class RacingUsers(FakeUsers):
        def by_login_name(self, login_name: str) -> User | None:
            return None

        def add(self, user: User) -> None:
            raise LoginNameTakenError(user.login_name)

    users = RacingUsers()
    with pytest.raises(AdministrationRefusedError) as refused:
        create_user(
            caller=caller_holding(Permission.USER_EDIT),
            login_name="wang.li",
            display_name="王力",
            password=PASSWORD,
            users=users,
        )

    assert refused.value.code is AdministrationRefusalCode.LOGIN_NAME_TAKEN


def test_a_password_below_the_minimum_length_is_refused(store: Store) -> None:
    with pytest.raises(AdministrationRefusedError) as refused:
        create_user(
            caller=caller_holding(Permission.USER_EDIT),
            login_name="wang.li",
            display_name="王力",
            password=SHORT_PASSWORD,
            users=store.users,
        )

    assert refused.value.code is AdministrationRefusalCode.PASSWORD_TOO_SHORT


def test_editing_after_the_target_vanishes_is_not_reported_as_success(store: Store) -> None:
    class VanishingUsers(FakeUsers):
        def update(self, user: User) -> None:
            self.by_id.pop(user.id, None)
            raise UserNotFoundError(user.id)

    users = VanishingUsers()
    target = users.register(login_name="wang.li", password=PASSWORD)

    with pytest.raises(AdministrationRefusedError) as refused:
        edit_user(
            caller=caller_holding(Permission.USER_EDIT),
            user_id=target.id,
            display_name="已消失",
            users=users,
        )

    assert refused.value.code is AdministrationRefusalCode.USER_NOT_FOUND


def test_editing_changes_the_display_name_and_not_the_login_name(store: Store) -> None:
    author = caller_holding(Permission.USER_EDIT)
    created = create_user(
        caller=author,
        login_name="wang.li",
        display_name="王力",
        password=PASSWORD,
        users=store.users,
    )

    edited = edit_user(
        caller=author, user_id=created.id, display_name="王力（三号线）", users=store.users
    )

    # The login name is the natural key an import matches on (§5.15) and is what the diagnostic
    # log records an operator by. Renaming it would break the trail; a new account is the
    # honest way to express "this is a different person".
    assert edited.display_name == "王力（三号线）"
    assert edited.login_name == "wang.li"


def test_editing_records_who_made_the_change(store: Store) -> None:
    author = caller_holding(Permission.USER_EDIT)
    created = create_user(
        caller=author,
        login_name="wang.li",
        display_name="王力",
        password=PASSWORD,
        users=store.users,
    )
    other = caller_holding(Permission.USER_EDIT)

    edited = edit_user(
        caller=other, user_id=created.id, display_name="王力（三号线）", users=store.users
    )

    assert edited.created_by == author.user.id
    assert edited.updated_by == other.user.id


def test_a_password_is_reset_without_knowing_the_old_one(store: Store) -> None:
    # An administrator resetting a forgotten password does not have the old one. Distinct from
    # an operator changing their own, which is a later ticket's self-service path.
    author = caller_holding(Permission.USER_EDIT)
    created = create_user(
        caller=author,
        login_name="wang.li",
        display_name="王力",
        password=PASSWORD,
        users=store.users,
    )

    reset_password(caller=author, user_id=created.id, password=RESET_PASSWORD, users=store.users)

    stored = store.users.by_identifier(created.id)
    assert stored is not None
    # Synthetic fixture values; nothing here is a credential.
    assert verify_password(  # pragma: allowlist secret
        password=RESET_PASSWORD,
        stored_hash=stored.password_hash,
    )


def test_deactivating_an_account_stops_it_logging_in(store: Store) -> None:
    author = caller_holding(Permission.USER_EDIT)
    created = create_user(
        caller=author,
        login_name="wang.li",
        display_name="王力",
        password=PASSWORD,
        users=store.users,
    )

    deactivate_user(
        caller=author,
        user_id=created.id,
        users=store.users,
        sessions=store.sessions,
        roles=store.roles,
    )

    stored = store.users.by_identifier(created.id)
    assert stored is not None
    assert stored.status is UserStatus.DEACTIVATED


def test_deactivating_revokes_every_session_in_the_same_operation(store: Store) -> None:
    # AC4, and the property the whole use case exists for: an account refused at the login form
    # while its open browsers keep working is not deactivated in any sense an operator means.
    # Same call, not a sweep and not a background job.
    author = caller_holding(Permission.USER_EDIT)
    created = create_user(
        caller=author,
        login_name="wang.li",
        display_name="王力",
        password=PASSWORD,
        users=store.users,
    )
    for _ in range(3):
        open_session(
            login_name="wang.li",
            password=PASSWORD,
            users=store.users,
            sessions=store.sessions,
            policy=POLICY,
            now=MONDAY_MORNING,
        )
    assert len(store.sessions.by_fingerprint) == 3

    revoked = deactivate_user(
        caller=author,
        user_id=created.id,
        users=store.users,
        sessions=store.sessions,
        roles=store.roles,
    )

    assert revoked.revoked_sessions == 3
    assert store.sessions.by_fingerprint == {}


def test_reactivating_restores_the_ability_to_log_in(store: Store) -> None:
    author = caller_holding(Permission.USER_EDIT)
    created = create_user(
        caller=author,
        login_name="wang.li",
        display_name="王力",
        password=PASSWORD,
        users=store.users,
    )
    deactivate_user(
        caller=author,
        user_id=created.id,
        users=store.users,
        sessions=store.sessions,
        roles=store.roles,
    )

    reactivate_user(caller=author, user_id=created.id, users=store.users)

    opened = open_session(
        login_name="wang.li",
        password=PASSWORD,
        users=store.users,
        sessions=store.sessions,
        policy=POLICY,
        now=MONDAY_MORNING,
    )
    assert opened.user.status is UserStatus.ACTIVE


def test_reactivating_does_not_restore_the_revoked_sessions(store: Store) -> None:
    # 恢复 gives the account back; it does not resurrect the browsers that were signed in when
    # it was taken away. Those rows are gone, and the operator logs in again.
    author = caller_holding(Permission.USER_EDIT)
    created = create_user(
        caller=author,
        login_name="wang.li",
        display_name="王力",
        password=PASSWORD,
        users=store.users,
    )
    open_session(
        login_name="wang.li",
        password=PASSWORD,
        users=store.users,
        sessions=store.sessions,
        policy=POLICY,
        now=MONDAY_MORNING,
    )
    deactivate_user(
        caller=author,
        user_id=created.id,
        users=store.users,
        sessions=store.sessions,
        roles=store.roles,
    )

    reactivate_user(caller=author, user_id=created.id, users=store.users)

    assert store.sessions.by_fingerprint == {}


def test_deleting_an_account_needs_its_own_permission(store: Store) -> None:
    # §5.15: 停用 is the reversible path and 删除 is a separate operation opened by permission.
    # An administrator may be given the first without the second.
    created = create_user(
        caller=caller_holding(Permission.USER_EDIT),
        login_name="wang.li",
        display_name="王力",
        password=PASSWORD,
        users=store.users,
    )

    with pytest.raises(AuthorizationRefusedError):
        delete_user(
            caller=caller_holding(Permission.USER_EDIT),
            user_id=created.id,
            users=store.users,
            sessions=store.sessions,
            roles=store.roles,
        )


def test_deleting_an_account_removes_it_and_its_sessions(store: Store) -> None:
    created = create_user(
        caller=caller_holding(Permission.USER_EDIT),
        login_name="wang.li",
        display_name="王力",
        password=PASSWORD,
        users=store.users,
    )
    open_session(
        login_name="wang.li",
        password=PASSWORD,
        users=store.users,
        sessions=store.sessions,
        policy=POLICY,
        now=MONDAY_MORNING,
    )

    delete_user(
        caller=caller_holding(Permission.USER_DELETE),
        user_id=created.id,
        users=store.users,
        sessions=store.sessions,
        roles=store.roles,
    )

    assert store.users.by_identifier(created.id) is None
    assert store.sessions.by_fingerprint == {}


def test_a_user_may_hold_several_roles(store: Store) -> None:
    # Q5/Q14: a role is a set of permissions and a user may have more than one.
    author = caller_holding(Permission.USER_EDIT)
    created = create_user(
        caller=author,
        login_name="wang.li",
        display_name="王力",
        password=PASSWORD,
        users=store.users,
    )
    role_author = caller_holding(Permission.ROLE_EDIT)
    viewer = create_role(
        caller=role_author,
        code="viewer",
        name="只读",
        permissions=["auth.user.view"],
        roles=store.roles,
    )
    reviewer = create_role(
        caller=role_author,
        code="reviewer",
        name="复核人员",
        permissions=["auth.role.view"],
        roles=store.roles,
    )

    assign_roles(
        caller=author,
        user_id=created.id,
        role_ids=[viewer.id, reviewer.id],
        users=store.users,
        roles=store.roles,
    )

    assert store.roles.permissions_of(created.id) == frozenset(
        {Permission.USER_VIEW, Permission.ROLE_VIEW}
    )


def test_assigning_replaces_the_previous_set(store: Store) -> None:
    author = caller_holding(Permission.USER_EDIT)
    created = create_user(
        caller=author,
        login_name="wang.li",
        display_name="王力",
        password=PASSWORD,
        users=store.users,
    )
    role_author = caller_holding(Permission.ROLE_EDIT)
    viewer = create_role(
        caller=role_author,
        code="viewer",
        name="只读",
        permissions=["auth.user.view"],
        roles=store.roles,
    )
    reviewer = create_role(
        caller=role_author,
        code="reviewer",
        name="复核人员",
        permissions=["auth.role.view"],
        roles=store.roles,
    )
    assign_roles(
        caller=author,
        user_id=created.id,
        role_ids=[viewer.id],
        users=store.users,
        roles=store.roles,
    )

    assign_roles(
        caller=author,
        user_id=created.id,
        role_ids=[reviewer.id],
        users=store.users,
        roles=store.roles,
    )

    assert store.roles.permissions_of(created.id) == frozenset({Permission.ROLE_VIEW})


def test_assigning_a_role_that_does_not_exist_is_refused(store: Store) -> None:
    author = caller_holding(Permission.USER_EDIT)
    created = create_user(
        caller=author,
        login_name="wang.li",
        display_name="王力",
        password=PASSWORD,
        users=store.users,
    )

    with pytest.raises(AdministrationRefusedError) as refused:
        assign_roles(
            caller=author,
            user_id=created.id,
            role_ids=[new_id()],
            users=store.users,
            roles=store.roles,
        )

    assert refused.value.code is AdministrationRefusalCode.ROLE_NOT_FOUND


def test_a_user_with_no_roles_holds_no_permissions(store: Store) -> None:
    created = create_user(
        caller=caller_holding(Permission.USER_EDIT),
        login_name="wang.li",
        display_name="王力",
        password=PASSWORD,
        users=store.users,
    )

    assert store.roles.permissions_of(created.id) == frozenset()


def test_listing_accounts_needs_user_view(store: Store) -> None:
    with pytest.raises(AuthorizationRefusedError):
        list_users(
            caller=caller_holding(Permission.ROLE_VIEW), users=store.users, page=1, page_size=50
        )


def test_a_listing_includes_deactivated_accounts(store: Store) -> None:
    # An administrator has to be able to find one in order to restore it. A listing that hid
    # them would make 恢复 unreachable from the screen that offers it.
    author = caller_holding(Permission.USER_EDIT)
    created = create_user(
        caller=author,
        login_name="wang.li",
        display_name="王力",
        password=PASSWORD,
        users=store.users,
    )
    deactivate_user(
        caller=author,
        user_id=created.id,
        users=store.users,
        sessions=store.sessions,
        roles=store.roles,
    )

    listed, total = list_users(
        caller=caller_holding(Permission.USER_VIEW), users=store.users, page=1, page_size=50
    )

    assert "wang.li" in [user.login_name for user in listed]
    assert total == 2


def test_accounts_are_listed_in_a_stable_order(store: Store) -> None:
    author = caller_holding(Permission.USER_EDIT)
    for login_name in ("zhao.min", "wang.li", "chen.yu"):
        create_user(
            caller=author,
            login_name=login_name,
            display_name=login_name,
            password=PASSWORD,
            users=store.users,
        )

    listed, total = list_users(
        caller=caller_holding(Permission.USER_VIEW), users=store.users, page=1, page_size=50
    )

    assert [user.login_name for user in listed] == ["chen.yu", "keeper", "wang.li", "zhao.min"]
    assert total == 4


def test_editing_an_account_that_does_not_exist_is_refused(store: Store) -> None:
    with pytest.raises(AdministrationRefusedError) as refused:
        edit_user(
            caller=caller_holding(Permission.USER_EDIT),
            user_id=new_id(),
            display_name="不存在",
            users=store.users,
        )

    assert refused.value.code is AdministrationRefusalCode.USER_NOT_FOUND


def test_an_account_change_is_a_diagnostic_event_naming_the_operator(store: Store) -> None:
    # AC4. `actor_id` is who did it and `user_id` is who it was done to — the same line has to
    # answer both, or an administrator reading it cannot tell a self-service change from an
    # administrative one.
    stream = io.StringIO()
    configure_logging(log_level="info", stream=stream)
    author = caller_holding(Permission.USER_EDIT)

    created = create_user(
        caller=author,
        login_name="wang.li",
        display_name="王力",
        password=PASSWORD,
        users=store.users,
    )

    (line,) = [json.loads(entry) for entry in stream.getvalue().splitlines() if entry]
    assert line["event"] == "auth.user.created"
    assert line["module"] == "auth"
    assert line["user_id"] == str(created.id)
    assert line["login_name"] == "wang.li"
    assert line["actor_id"] == str(author.user.id)


def test_no_password_reaches_a_diagnostic_line(store: Store) -> None:
    # §5.15 forbids a credential in a diagnostic line, in any form. Administration is the path
    # where a plaintext password is in scope, so it is the path where this can regress.
    stream = io.StringIO()
    configure_logging(log_level="info", stream=stream)
    author = caller_holding(Permission.USER_EDIT)

    created = create_user(
        caller=author,
        login_name="wang.li",
        display_name="王力",
        password=PASSWORD,
        users=store.users,
    )
    reset_password(caller=author, user_id=created.id, password=RESET_PASSWORD, users=store.users)

    rendered = stream.getvalue()
    assert "assembly-line-3" not in rendered
    assert "assembly-line-9" not in rendered
    stored = store.users.by_identifier(created.id)
    assert stored is not None
    assert stored.password_hash not in rendered


def test_deactivating_reports_how_many_sessions_it_revoked_in_its_line(store: Store) -> None:
    stream = io.StringIO()
    configure_logging(log_level="info", stream=stream)
    author = caller_holding(Permission.USER_EDIT)
    created = create_user(
        caller=author,
        login_name="wang.li",
        display_name="王力",
        password=PASSWORD,
        users=store.users,
    )
    open_session(
        login_name="wang.li",
        password=PASSWORD,
        users=store.users,
        sessions=store.sessions,
        policy=POLICY,
        now=MONDAY_MORNING,
    )

    deactivate_user(
        caller=author,
        user_id=created.id,
        users=store.users,
        sessions=store.sessions,
        roles=store.roles,
    )

    deactivated = [
        json.loads(entry)
        for entry in stream.getvalue().splitlines()
        if entry and json.loads(entry)["event"] == "auth.user.deactivated"
    ]
    assert deactivated[0]["revoked_sessions"] == 1


def test_the_last_administrator_cannot_deactivate_themselves(store: Store) -> None:
    # The lockout this guard exists for, by the shortest route: one administrator, who takes
    # their own account out of service and cannot put it back.
    users = FakeUsers()
    roles = FakeRoles(users=users)
    sessions = FakeSessions()
    administration = create_role(
        caller=caller_holding(Permission.ROLE_EDIT),
        code="system_administrator",
        name="系统管理员",
        permissions=["auth.user.edit", "auth.role.edit"],
        roles=roles,
    )
    only = users.register(login_name="wang.li", password=PASSWORD)
    roles.assign(user_id=only.id, role_ids=[administration.id])

    with pytest.raises(AdministrationRefusedError) as refused:
        deactivate_user(
            caller=caller_holding(Permission.USER_EDIT, user=only),
            user_id=only.id,
            users=users,
            sessions=sessions,
            roles=roles,
        )

    assert refused.value.code is AdministrationRefusalCode.ADMINISTRATION_WOULD_BE_LOST
    stored = users.by_identifier(only.id)
    assert stored is not None
    assert stored.status is UserStatus.ACTIVE


def test_the_last_administrator_cannot_be_deleted(store: Store) -> None:
    users = FakeUsers()
    roles = FakeRoles(users=users)
    administration = create_role(
        caller=caller_holding(Permission.ROLE_EDIT),
        code="system_administrator",
        name="系统管理员",
        permissions=["auth.user.edit", "auth.role.edit"],
        roles=roles,
    )
    only = users.register(login_name="wang.li", password=PASSWORD)
    roles.assign(user_id=only.id, role_ids=[administration.id])

    with pytest.raises(AdministrationRefusedError) as refused:
        delete_user(
            caller=caller_holding(Permission.USER_DELETE),
            user_id=only.id,
            users=users,
            sessions=FakeSessions(),
            roles=roles,
        )

    assert refused.value.code is AdministrationRefusalCode.ADMINISTRATION_WOULD_BE_LOST


def test_the_last_administrator_cannot_have_the_role_taken_away(store: Store) -> None:
    # The third route to the same loss: keep the account, unassign the role.
    users = FakeUsers()
    roles = FakeRoles(users=users)
    administration = create_role(
        caller=caller_holding(Permission.ROLE_EDIT),
        code="system_administrator",
        name="系统管理员",
        permissions=["auth.user.edit", "auth.role.edit"],
        roles=roles,
    )
    only = users.register(login_name="wang.li", password=PASSWORD)
    roles.assign(user_id=only.id, role_ids=[administration.id])

    with pytest.raises(AdministrationRefusedError) as refused:
        assign_roles(
            caller=caller_holding(Permission.USER_EDIT),
            user_id=only.id,
            role_ids=[],
            users=users,
            roles=roles,
        )

    assert refused.value.code is AdministrationRefusalCode.ADMINISTRATION_WOULD_BE_LOST
    assert roles.permissions_of(only.id) == frozenset({Permission.USER_EDIT, Permission.ROLE_EDIT})


def test_a_second_administrator_makes_the_first_deactivatable(store: Store) -> None:
    users = FakeUsers()
    roles = FakeRoles(users=users)
    administration = create_role(
        caller=caller_holding(Permission.ROLE_EDIT),
        code="system_administrator",
        name="系统管理员",
        permissions=["auth.user.edit", "auth.role.edit"],
        roles=roles,
    )
    first = users.register(login_name="wang.li", password=PASSWORD)
    second = users.register(login_name="zhao.min", password=NEW_PASSWORD)
    roles.assign(user_id=first.id, role_ids=[administration.id])
    roles.assign(user_id=second.id, role_ids=[administration.id])

    deactivate_user(
        caller=caller_holding(Permission.USER_EDIT),
        user_id=first.id,
        users=users,
        sessions=FakeSessions(),
        roles=roles,
    )

    stored = users.by_identifier(first.id)
    assert stored is not None
    assert stored.status is UserStatus.DEACTIVATED


def test_a_deactivated_administrator_does_not_count_as_one(store: Store) -> None:
    # The guard counts accounts that can actually administer. A deactivated holder cannot log
    # in, so it is not a surviving administrator — otherwise deactivating both in turn would be
    # permitted and the second deactivation would be the lockout.
    users = FakeUsers()
    roles = FakeRoles(users=users)
    administration = create_role(
        caller=caller_holding(Permission.ROLE_EDIT),
        code="system_administrator",
        name="系统管理员",
        permissions=["auth.user.edit", "auth.role.edit"],
        roles=roles,
    )
    first = users.register(login_name="wang.li", password=PASSWORD)
    second = users.register(login_name="zhao.min", password=NEW_PASSWORD)
    roles.assign(user_id=first.id, role_ids=[administration.id])
    roles.assign(user_id=second.id, role_ids=[administration.id])
    author = caller_holding(Permission.USER_EDIT)
    deactivate_user(
        caller=author, user_id=first.id, users=users, sessions=FakeSessions(), roles=roles
    )

    with pytest.raises(AdministrationRefusedError):
        deactivate_user(
            caller=author, user_id=second.id, users=users, sessions=FakeSessions(), roles=roles
        )


def test_a_taken_login_name_is_a_refusal_event_naming_the_value(store: Store) -> None:
    # AC4 covers refusals as well as changes. The line carries the actor and the natural key an
    # administrator searches by — not the password, which was never the subject (§5.15).
    stream = io.StringIO()
    configure_logging(log_level="info", stream=stream)
    author = caller_holding(Permission.USER_EDIT)
    create_user(
        caller=author,
        login_name="wang.li",
        display_name="王力",
        password=PASSWORD,
        users=store.users,
    )

    with pytest.raises(AdministrationRefusedError):
        create_user(
            caller=author,
            login_name="wang.li",
            display_name="另一个王力",
            password=NEW_PASSWORD,
            users=store.users,
        )

    (line,) = [
        json.loads(entry)
        for entry in stream.getvalue().splitlines()
        if entry and json.loads(entry)["event"] == "auth.user.refused"
    ]
    assert line["error_code"] == "LOGIN_NAME_TAKEN"
    assert line["login_name"] == "wang.li"
    assert line["actor_id"] == str(author.user.id)


def test_an_unknown_target_is_a_refusal_event(store: Store) -> None:
    stream = io.StringIO()
    configure_logging(log_level="info", stream=stream)
    author = caller_holding(Permission.USER_EDIT)
    missing = new_id()

    with pytest.raises(AdministrationRefusedError):
        edit_user(caller=author, user_id=missing, display_name="不存在", users=store.users)

    (line,) = [
        json.loads(entry)
        for entry in stream.getvalue().splitlines()
        if entry and json.loads(entry)["event"] == "auth.user.refused"
    ]
    assert line["error_code"] == "USER_NOT_FOUND"
    assert line["user_id"] == str(missing)
    assert line["actor_id"] == str(author.user.id)


def test_the_last_administrator_refusal_is_a_diagnostic_event(store: Store) -> None:
    # The refusal an administrator most needs explained: the line names the permission that
    # would have lost its last holder and who attempted the operation.
    users = FakeUsers()
    roles = FakeRoles(users=users)
    administration = create_role(
        caller=caller_holding(Permission.ROLE_EDIT),
        code="system_administrator",
        name="系统管理员",
        permissions=["auth.user.edit", "auth.role.edit"],
        roles=roles,
    )
    only = users.register(login_name="wang.li", password=PASSWORD)
    roles.assign(user_id=only.id, role_ids=[administration.id])
    stream = io.StringIO()
    configure_logging(log_level="info", stream=stream)
    author = caller_holding(Permission.USER_EDIT, user=only)

    with pytest.raises(AdministrationRefusedError):
        deactivate_user(
            caller=author, user_id=only.id, users=users, sessions=FakeSessions(), roles=roles
        )

    (line,) = [json.loads(entry) for entry in stream.getvalue().splitlines() if entry]
    assert line["event"] == "auth.administration.refused"
    assert line["error_code"] == "ADMINISTRATION_WOULD_BE_LOST"
    # The guard reports the first alphabetically of the permissions that would be lost; the
    # role holds both, and `auth.role.edit` sorts first.
    assert line["permission"] == "auth.role.edit"
    assert line["actor_id"] == str(only.id)
