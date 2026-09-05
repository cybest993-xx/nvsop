"""`RoleRepository` against real PostgreSQL: the joins, the cascades and the constraints.

The use-case suite proves the rules against an in-memory stand-in. What can only be proved here is
that the adapter behaves the same way at that seam — and specifically the things a dict
cannot model: that deleting a role really removes its assignments through `ON DELETE CASCADE`, that
the permission union is one correct join across the tables, that `active_holders_of` excludes
a deactivated account because the database says so rather than because a fake remembered to, and
that the permission registry (`auth_permission`) refuses a value no module registered.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import Engine, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.auth.adapters.repository import (
    PostgresRoleRepository,
    PostgresUserRepository,
)
from factory_sop.auth.adapters.tables import RoleRow
from factory_sop.auth.model import Role, User, UserStatus
from factory_sop.auth.passwords import hash_password
from factory_sop.auth.permissions import Permission
from factory_sop.auth.repository import RoleCodeTakenError, RoleNotFoundError
from factory_sop.identifiers import new_id


def an_account(*, login_name: str, status: UserStatus = UserStatus.ACTIVE) -> User:
    return User(
        id=new_id(),
        login_name=login_name,
        display_name="王丽",
        password_hash=hash_password("assembly-line-3"),
        status=status,
    )


def a_role(*, code: str, permissions: frozenset[Permission] = frozenset()) -> Role:
    return Role(id=new_id(), code=code, name=f"角色 {code}", permissions=permissions)


def test_a_role_round_trips_with_its_permission_set(session: DatabaseSession) -> None:
    roles = PostgresRoleRepository(session)
    stored = a_role(
        code="viewer", permissions=frozenset({Permission.USER_VIEW, Permission.ROLE_VIEW})
    )
    roles.add(stored)
    session.flush()
    session.expunge_all()

    assert roles.by_identifier(stored.id) == stored
    assert roles.by_code("viewer") == stored


def test_a_role_with_no_permissions_round_trips_as_an_empty_set(session: DatabaseSession) -> None:
    # Not as `None`, and not as a row with an empty string in it: the empty set is a legitimate
    # role that grants nothing.
    roles = PostgresRoleRepository(session)
    stored = a_role(code="placeholder")
    roles.add(stored)
    session.flush()
    session.expunge_all()

    read = roles.by_identifier(stored.id)
    assert read is not None
    assert read.permissions == frozenset()


def test_two_roles_cannot_share_a_code(session: DatabaseSession) -> None:
    roles = PostgresRoleRepository(session)
    roles.add(a_role(code="viewer"))

    with pytest.raises(RoleCodeTakenError):
        roles.add(a_role(code="viewer"))


def test_a_permission_outside_the_registry_is_refused_by_the_database(
    session: DatabaseSession,
) -> None:
    # `parse_permission` refuses an unregistered value at the use case; the foreign key to
    # `auth_permission` is the second gate, for a writer that bypassed the seam. A permission
    # exists in the registry because a module registered it — not because a role named it.
    from factory_sop.auth.adapters.tables import RolePermissionRow

    roles = PostgresRoleRepository(session)
    stored = a_role(code="viewer")
    roles.add(stored)
    session.flush()

    session.add(RolePermissionRow(role_id=stored.id, permission="device.camera.edit"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_updating_after_an_external_delete_reports_role_not_found(engine: Engine) -> None:
    role = a_role(code="vanishing_role", permissions=frozenset({Permission.USER_VIEW}))
    writer = DatabaseSession(engine)
    reader = DatabaseSession(engine)
    try:
        PostgresRoleRepository(writer).add(role)
        writer.commit()

        roles = PostgresRoleRepository(reader)
        # Populate the ORM identity map before the other transaction deletes the row. A later
        # update must not mistake that cached object for a successful write.
        assert roles.by_identifier(role.id) == role
        with engine.begin() as connection:
            connection.execute(delete(RoleRow).where(RoleRow.id == role.id))

        with pytest.raises(RoleNotFoundError):
            roles.update(replace(role, name="已消失"))
    finally:
        reader.close()
        writer.close()
        with engine.begin() as connection:
            connection.execute(delete(RoleRow).where(RoleRow.id == role.id))


def test_removing_after_an_external_delete_reports_role_not_found(engine: Engine) -> None:
    role = a_role(code="vanishing_remove")
    writer = DatabaseSession(engine)
    reader = DatabaseSession(engine)
    try:
        PostgresRoleRepository(writer).add(role)
        writer.commit()

        roles = PostgresRoleRepository(reader)
        assert roles.by_identifier(role.id) == role
        with engine.begin() as connection:
            connection.execute(delete(RoleRow).where(RoleRow.id == role.id))

        with pytest.raises(RoleNotFoundError):
            roles.remove(role.id)
    finally:
        reader.close()
        writer.close()
        with engine.begin() as connection:
            connection.execute(delete(RoleRow).where(RoleRow.id == role.id))


def test_editing_replaces_the_permission_rows_rather_than_adding_to_them(
    session: DatabaseSession,
) -> None:
    roles = PostgresRoleRepository(session)
    stored = a_role(
        code="viewer", permissions=frozenset({Permission.USER_VIEW, Permission.ROLE_VIEW})
    )
    roles.add(stored)

    roles.update(replace(stored, permissions=frozenset({Permission.USER_VIEW})))
    session.flush()
    session.expunge_all()

    read = roles.by_identifier(stored.id)
    assert read is not None
    assert read.permissions == frozenset({Permission.USER_VIEW})


def test_assigning_replaces_the_previous_set(session: DatabaseSession) -> None:
    users = PostgresUserRepository(session)
    roles = PostgresRoleRepository(session)
    operator = an_account(login_name="wang.li")
    users.add(operator)
    first = a_role(code="viewer", permissions=frozenset({Permission.USER_VIEW}))
    second = a_role(code="reviewer", permissions=frozenset({Permission.ROLE_VIEW}))
    roles.add(first)
    roles.add(second)

    roles.assign(user_id=operator.id, role_ids=[first.id])
    roles.assign(user_id=operator.id, role_ids=[second.id])
    session.flush()
    session.expunge_all()

    assert roles.role_ids_of(operator.id) == frozenset({second.id})


def test_the_permission_set_is_the_union_of_every_role_held(session: DatabaseSession) -> None:
    users = PostgresUserRepository(session)
    roles = PostgresRoleRepository(session)
    operator = an_account(login_name="wang.li")
    users.add(operator)
    first = a_role(code="viewer", permissions=frozenset({Permission.USER_VIEW}))
    second = a_role(
        code="editor", permissions=frozenset({Permission.USER_EDIT, Permission.USER_VIEW})
    )
    roles.add(first)
    roles.add(second)
    roles.assign(user_id=operator.id, role_ids=[first.id, second.id])
    session.flush()
    session.expunge_all()

    # A union, so the permission both roles grant appears once and neither is lost.
    assert roles.permissions_of(operator.id) == frozenset(
        {Permission.USER_VIEW, Permission.USER_EDIT}
    )


def test_an_account_with_no_roles_holds_no_permissions(session: DatabaseSession) -> None:
    users = PostgresUserRepository(session)
    roles = PostgresRoleRepository(session)
    operator = an_account(login_name="wang.li")
    users.add(operator)
    session.flush()

    assert roles.permissions_of(operator.id) == frozenset()


def test_deleting_a_role_removes_every_assignment_of_it(session: DatabaseSession) -> None:
    # `ON DELETE CASCADE`, which a dict cannot prove. A surviving assignment row would name a role
    # that is gone: `permissions_of` would join to nothing while `role_ids_of` still reported it.
    users = PostgresUserRepository(session)
    roles = PostgresRoleRepository(session)
    operator = an_account(login_name="wang.li")
    users.add(operator)
    doomed = a_role(code="obsolete", permissions=frozenset({Permission.USER_VIEW}))
    roles.add(doomed)
    roles.assign(user_id=operator.id, role_ids=[doomed.id])
    session.flush()

    roles.remove(doomed.id)
    session.flush()
    session.expunge_all()

    assert roles.role_ids_of(operator.id) == frozenset()
    assert roles.permissions_of(operator.id) == frozenset()


def test_active_holders_excludes_a_deactivated_account(session: DatabaseSession) -> None:
    # The last-administration guard depends on this being the database's answer. A deactivated
    # holder counted as an administrator would let both be deactivated in turn.
    users = PostgresUserRepository(session)
    roles = PostgresRoleRepository(session)
    active = an_account(login_name="wang.li")
    inactive = an_account(login_name="zhao.min", status=UserStatus.DEACTIVATED)
    users.add(active)
    users.add(inactive)
    role = a_role(code="administrator", permissions=frozenset({Permission.USER_EDIT}))
    roles.add(role)
    roles.assign(user_id=active.id, role_ids=[role.id])
    roles.assign(user_id=inactive.id, role_ids=[role.id])
    session.flush()

    assert roles.active_holders_of(Permission.USER_EDIT) == frozenset({active.id})
    assert roles.active_assignees_of(role.id) == frozenset({active.id})


def test_active_holders_can_be_asked_as_if_one_role_did_not_exist(
    session: DatabaseSession,
) -> None:
    # What makes the guard answerable before the write: "who would still hold this if that role
    # were deleted".
    users = PostgresUserRepository(session)
    roles = PostgresRoleRepository(session)
    only = an_account(login_name="wang.li")
    users.add(only)
    role = a_role(code="administrator", permissions=frozenset({Permission.USER_EDIT}))
    roles.add(role)
    roles.assign(user_id=only.id, role_ids=[role.id])
    session.flush()

    assert roles.active_holders_of(Permission.USER_EDIT) == frozenset({only.id})
    assert roles.active_holders_of(Permission.USER_EDIT, ignoring_role=role.id) == frozenset()


def test_a_holder_through_a_second_role_still_counts_when_one_is_ignored(
    session: DatabaseSession,
) -> None:
    users = PostgresUserRepository(session)
    roles = PostgresRoleRepository(session)
    operator = an_account(login_name="wang.li")
    users.add(operator)
    first = a_role(code="administrator", permissions=frozenset({Permission.USER_EDIT}))
    second = a_role(code="owner", permissions=frozenset({Permission.USER_EDIT}))
    roles.add(first)
    roles.add(second)
    roles.assign(user_id=operator.id, role_ids=[first.id, second.id])
    session.flush()

    assert roles.active_holders_of(Permission.USER_EDIT, ignoring_role=first.id) == frozenset(
        {operator.id}
    )


def test_a_permission_outside_the_current_build_grants_nothing_rather_than_raising(
    session: DatabaseSession,
) -> None:
    # A module removed from the product leaves its permission rows behind. Reading a role that
    # still names one must not fail every request by every holder — the value grants nothing,
    # which is exactly what dropping it means. The registry row survives (removing it belongs to
    # that module's own removal migration); only the in-memory member is gone.
    from sqlalchemy import text

    users = PostgresUserRepository(session)
    roles = PostgresRoleRepository(session)
    operator = an_account(login_name="wang.li")
    users.add(operator)
    role = a_role(code="legacy", permissions=frozenset({Permission.USER_VIEW}))
    roles.add(role)
    roles.assign(user_id=operator.id, role_ids=[role.id])
    session.flush()
    session.execute(
        text("INSERT INTO auth_permission (code) VALUES ('retired.thing.edit')"),
    )
    session.execute(
        text(
            "INSERT INTO auth_role_permission (role_id, permission) "
            "VALUES (:role_id, 'retired.thing.edit')"
        ),
        {"role_id": role.id},
    )
    session.flush()
    session.expunge_all()

    read = roles.by_identifier(role.id)
    assert read is not None
    assert read.permissions == frozenset({Permission.USER_VIEW})
    assert roles.permissions_of(operator.id) == frozenset({Permission.USER_VIEW})


def test_roles_are_listed_in_code_order(session: DatabaseSession) -> None:
    roles = PostgresRoleRepository(session)
    for code in ("reviewer", "operator", "administrator"):
        roles.add(a_role(code=code))
    session.flush()
    session.expunge_all()

    assert [role.code for role in roles.every()] == ["administrator", "operator", "reviewer"]


def test_role_page_applies_limit_and_reports_total(session: DatabaseSession) -> None:
    roles = PostgresRoleRepository(session)
    for code in ("reviewer", "operator", "administrator"):
        roles.add(a_role(code=code))
    session.flush()

    page, total = roles.page(page=2, page_size=1)

    assert [role.code for role in page] == ["operator"]
    assert total == 3


def test_deleting_an_account_removes_its_assignments_and_sessions(
    session: DatabaseSession,
) -> None:
    # `ON DELETE CASCADE` on `auth_user_role` and `auth_session`, which a dict cannot prove. A
    # surviving assignment row would name an account that is gone; a surviving session would
    # authenticate requests as someone who is gone.
    from datetime import UTC, datetime

    from factory_sop.auth.adapters.repository import PostgresSessionRepository
    from factory_sop.auth.model import Session

    users = PostgresUserRepository(session)
    roles = PostgresRoleRepository(session)
    sessions = PostgresSessionRepository(session)
    operator = an_account(login_name="wang.li")
    users.add(operator)
    role = a_role(code="viewer", permissions=frozenset({Permission.USER_VIEW}))
    roles.add(role)
    roles.assign(user_id=operator.id, role_ids=[role.id])
    sessions.add(
        Session(
            id=new_id(),
            user_id=operator.id,
            token_fingerprint="a" * 64,
            created_at=datetime(2026, 9, 7, 1, 0, tzinfo=UTC),
            last_used_at=datetime(2026, 9, 7, 1, 0, tzinfo=UTC),
        )
    )
    session.flush()

    users.remove(operator.id)
    session.flush()
    session.expunge_all()

    assert roles.role_ids_of(operator.id) == frozenset()
    assert sessions.by_token_fingerprint("a" * 64) is None
    # The role itself survives: deleting an account does not delete what it was allowed to do.
    assert roles.by_identifier(role.id) is not None


def test_an_edited_account_keeps_its_identity_and_gains_the_attribution(
    session: DatabaseSession,
) -> None:
    from dataclasses import replace

    users = PostgresUserRepository(session)
    operator = an_account(login_name="wang.li")
    users.add(operator)

    users.update(replace(operator, display_name="王丽（三号线）", updated_by=operator.id))
    session.flush()
    session.expunge_all()

    read = users.by_identifier(operator.id)
    assert read is not None
    assert read.login_name == "wang.li"
    assert read.display_name == "王丽（三号线）"
    assert read.updated_by == operator.id


def test_accounts_are_listed_in_login_name_order(session: DatabaseSession) -> None:
    users = PostgresUserRepository(session)
    for login_name in ("zhao.min", "chen.yu", "wang.li"):
        users.add(an_account(login_name=login_name))
    session.flush()
    session.expunge_all()

    assert [user.login_name for user in users.every()] == ["chen.yu", "wang.li", "zhao.min"]


def test_account_page_applies_limit_and_reports_total(session: DatabaseSession) -> None:
    users = PostgresUserRepository(session)
    for login_name in ("zhao.min", "chen.yu", "wang.li"):
        users.add(an_account(login_name=login_name))
    session.flush()

    page, total = users.page(page=2, page_size=1)

    assert [user.login_name for user in page] == ["wang.li"]
    assert total == 3
