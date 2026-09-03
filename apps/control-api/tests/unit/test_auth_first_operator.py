"""The deployment's first account: what the bootstrap creates, and what it refuses to.

`POST /auth/session` authenticates against accounts, and a fresh deployment has none — the
bootstrap command is how "用户可以登录" is reachable at all. Administration of accounts
(create, edit, deactivate) is C2.2's; this use case is only the first one, created by the
deployment's own command from credentials the deployment supplies.
"""

from __future__ import annotations

from auth_fakes import FakeUsers

from factory_sop.auth.model import UserStatus
from factory_sop.auth.usecases.bootstrap import register_first_operator


def test_a_fresh_deployment_gets_its_first_operator() -> None:
    users = FakeUsers()

    created = register_first_operator(
        login_name="admin",
        password="first-shift-key",
        display_name="管理员",
        users=users,
    )

    assert created is not None
    stored = users.by_login_name("admin")
    assert stored is not None
    assert stored.status is UserStatus.ACTIVE
    assert stored.display_name == "管理员"
    # The password is verifiable and never stored: what the table holds is Argon2's hash,
    # exactly what a login will be checked against.
    assert stored.password_hash != "first-shift-key"


def test_a_deployment_with_an_account_skips_instead_of_creating_a_second() -> None:
    # The command runs on every start with the deployment's credentials. Skipping is what makes
    # that idempotent — and it is also the ceiling: a bootstrap that could mint a second account
    # from a leaked start-up secret would keep minting them.
    users = FakeUsers()
    register_first_operator(
        login_name="admin",
        password="first-shift-key",
        display_name="管理员",
        users=users,
    )
    before = dict(users.by_id)

    again = register_first_operator(
        login_name="admin",
        password="first-shift-key",
        display_name="管理员",
        users=users,
    )

    assert again is None
    assert users.by_id == before
