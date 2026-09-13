"""The deployment's first account: what the bootstrap creates, and what it refuses to.

`POST /auth/session` authenticates against accounts, and a fresh deployment has none — the
bootstrap command is how "用户可以登录" is reachable at all. It is also the one account created
without a `Caller`: every write use case requires a `Caller` holding a permission, and on an
empty database nobody can be one. What keeps the bootstrap from being a way around authorization
is its precondition — it skips once any account exists at all, which is the version that cannot
be gamed by deleting the administrator.
"""

from __future__ import annotations

import io
import json

import pytest
from auth_fakes import FakeRoles, FakeUsers

from factory_sop.auth.errors import AdministrationRefusalCode, AdministrationRefusedError
from factory_sop.auth.model import UserStatus
from factory_sop.auth.passwords import verify_password
from factory_sop.auth.permissions import Permission
from factory_sop.auth.usecases.bootstrap import (
    ADMINISTRATOR_ROLE_CODE,
    register_first_operator,
)
from factory_sop.observability import configure_logging

PASSWORD = "first-shift-key"  # pragma: allowlist secret


@pytest.fixture
def users() -> FakeUsers:
    return FakeUsers()


@pytest.fixture
def roles(users: FakeUsers) -> FakeRoles:
    return FakeRoles(users=users)


def test_a_fresh_deployment_gets_its_first_operator(users: FakeUsers, roles: FakeRoles) -> None:
    created = register_first_operator(
        login_name="admin",
        password=PASSWORD,
        display_name="管理员",
        users=users,
        roles=roles,
    )

    assert created is not None
    stored = users.by_login_name("admin")
    assert stored is not None
    assert stored.status is UserStatus.ACTIVE
    assert stored.display_name == "管理员"
    # The password is verifiable and never stored: what the table holds is Argon2's hash,
    # exactly what a login will be checked against.
    assert stored.password_hash != PASSWORD  # pragma: allowlist secret


def test_the_first_operator_holds_every_registered_permission(
    users: FakeUsers, roles: FakeRoles
) -> None:
    # Every registered permission, not a curated subset. The named roles §5.4 lists are made of
    # permissions from modules that do not exist yet; this account is what an administrator uses
    # to build them, and a subset would leave a permission grantable by nobody.
    created = register_first_operator(
        login_name="admin",
        password=PASSWORD,
        display_name="管理员",
        users=users,
        roles=roles,
    )
    assert created is not None

    assert roles.permissions_of(created.id) == frozenset(Permission)


def test_permissions_reach_the_account_through_a_role_not_directly(
    users: FakeUsers, roles: FakeRoles
) -> None:
    # Permissions reach an account only through a role (Q5/Q14). A bootstrap that attached them
    # to the account would be a second grant mechanism, and the role screen would not show why
    # this account can do everything. The seed is an ordinary editable row: an administrator can
    # rename, narrow, or delete it once they have built the roles their site actually wants.
    register_first_operator(
        login_name="admin",
        password=PASSWORD,
        display_name="管理员",
        users=users,
        roles=roles,
    )

    role = roles.by_code(ADMINISTRATOR_ROLE_CODE)
    assert role is not None
    assert role.name == "系统管理员"
    assert role.permissions == frozenset(Permission)


def test_the_first_account_is_its_own_creator(users: FakeUsers, roles: FakeRoles) -> None:
    # There is no caller to attribute the row to — the deployment itself ran the command — so
    # the account is recorded as its own creator (§5.15 carries 变更归属 in `created_by`).
    created = register_first_operator(
        login_name="admin",
        password=PASSWORD,
        display_name="管理员",
        users=users,
        roles=roles,
    )
    assert created is not None

    stored = users.by_identifier(created.id)
    assert stored is not None
    assert stored.created_by == created.id
    assert stored.updated_by == created.id
    seeded = roles.by_code(ADMINISTRATOR_ROLE_CODE)
    assert seeded is not None
    assert seeded.created_by == created.id


def test_a_deployment_with_an_account_skips_instead_of_creating_a_second(
    users: FakeUsers, roles: FakeRoles
) -> None:
    # The command runs on every start with the deployment's credentials. Skipping is what makes
    # that idempotent — and it is also the ceiling: a bootstrap that could mint a second account
    # from a leaked start-up secret would keep minting them.
    register_first_operator(
        login_name="admin",
        password=PASSWORD,
        display_name="管理员",
        users=users,
        roles=roles,
    )
    stored_users = dict(users.by_id)
    stored_roles = dict(roles.by_id)

    again = register_first_operator(
        login_name="admin",
        password=PASSWORD,
        display_name="管理员",
        users=users,
        roles=roles,
    )

    assert again is None
    assert users.by_id == stored_users
    assert roles.by_id == stored_roles


def test_a_short_password_is_refused(users: FakeUsers, roles: FakeRoles) -> None:
    # The same minimum every other password-setting path enforces; the account this creates is
    # an operator's daily login, not a one-off system secret.
    with pytest.raises(AdministrationRefusedError) as refused:
        register_first_operator(
            login_name="admin",
            password="short",  # pragma: allowlist secret
            display_name="管理员",
            users=users,
            roles=roles,
        )

    assert refused.value.code is AdministrationRefusalCode.PASSWORD_TOO_SHORT
    assert users.by_login_name("admin") is None


def test_bootstrapping_is_a_diagnostic_event_without_the_password(
    users: FakeUsers, roles: FakeRoles
) -> None:
    stream = io.StringIO()
    configure_logging(log_level="info", stream=stream)

    created = register_first_operator(
        login_name="admin",
        password=PASSWORD,
        display_name="管理员",
        users=users,
        roles=roles,
    )
    assert created is not None

    events = [json.loads(entry) for entry in stream.getvalue().splitlines() if entry]
    assert [line["event"] for line in events] == ["auth.bootstrap.created"]
    assert events[0]["user_id"] == str(created.id)
    assert events[0]["login_name"] == "admin"
    assert PASSWORD not in stream.getvalue()  # pragma: allowlist secret


def test_a_verified_password_round_trips_through_the_login_path(
    users: FakeUsers, roles: FakeRoles
) -> None:
    created = register_first_operator(
        login_name="admin",
        password=PASSWORD,
        display_name="管理员",
        users=users,
        roles=roles,
    )
    assert created is not None

    stored = users.by_login_name("admin")
    assert stored is not None
    assert verify_password(password=PASSWORD, stored_hash=stored.password_hash)
