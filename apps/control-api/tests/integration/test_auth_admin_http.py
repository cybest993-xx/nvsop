"""账户管理走真实 PostgreSQL 的 HTTP 面：用例边界验证目标，而不是靠外键兜底。

给不存在的账户分配角色曾被外键拦下并变成 500，而空角色列表却走到后面的未找到检查变成
404——同一个资源是否存在，答案取决于请求体。`assign_roles` 现在在写入前验证目标账户，
这里断言 HTTP 层看到的是同一个答案，且没有留下半条分配。
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session as DatabaseSession
from sqlalchemy.orm import sessionmaker

from factory_sop.app import API_PREFIX, create_app
from factory_sop.auth.adapters import dependencies
from factory_sop.auth.adapters.repository import (
    PostgresRoleRepository,
    PostgresUserRepository,
)
from factory_sop.auth.model import Role, User, UserStatus
from factory_sop.auth.passwords import hash_password
from factory_sop.auth.permissions import Permission
from factory_sop.identifiers import new_id
from factory_sop.persistence import session_factory
from factory_sop.settings import Settings

BOOTSTRAP_PASSWORD = "first-shift-key"  # pragma: allowlist secret

SESSION_PATH = f"{API_PREFIX}/auth/session"


@pytest.fixture
def client(engine: Engine) -> Iterator[tuple[TestClient, DatabaseSession]]:
    """The real application over the containerized database, on one request transaction.

    The dependency overrides point the route providers at the same repositories they build in
    production, only bound to this suite's transaction — replaced at the seam, not mocked
    through the call chain (harness §4). The caller's permission set is supplied at the same
    seam the request path resolves it (`dependencies.granted_permissions`).
    """
    connection = engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection)()
    settings = Settings(
        log_level="warning",
        database_host="unused",
        database_port=5432,
        database_name="unused",
        database_user="unused",
        database_password=SecretStr("unused"),
        session_idle_timeout_minutes=720,
        session_absolute_lifetime_minutes=43200,
        session_cookie_transport="require_https",
        csrf_secret=SecretStr("csrf-secret"),
        redis_url=SecretStr("redis://127.0.0.1:1/0"),
    )
    app = create_app(settings)
    app.dependency_overrides[dependencies.users] = lambda: PostgresUserRepository(session)
    app.dependency_overrides[dependencies.roles] = lambda: PostgresRoleRepository(session)
    app.dependency_overrides[dependencies.granted_permissions] = lambda: frozenset(Permission)
    # The session routes keep the production path: their `request_session` draws on the app's
    # factory, so the login below commits against the container like a real request does.
    app.state.session_factory = session_factory(engine)
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client, session
    session.close()
    if transaction.is_active:
        transaction.rollback()
    connection.close()


def test_assigning_roles_to_an_unknown_account_is_user_not_found(
    client: tuple[TestClient, DatabaseSession],
    engine: Engine,
) -> None:
    test_client, _ = client
    # Arranged through a session of its own and committed: a session bound to this fixture's
    # already-begun transaction would turn `commit()` into a savepoint release, and the rows
    # would never reach the login request the production path runs.
    setup = sessionmaker(bind=engine)()
    administrator = User(
        id=new_id(),
        login_name="wang.admin",
        display_name="王管理员",
        password_hash=hash_password(BOOTSTRAP_PASSWORD),
        status=UserStatus.ACTIVE,
    )
    PostgresUserRepository(setup).add(administrator)
    role = Role(
        id=new_id(), code="viewer", name="只读", permissions=frozenset({Permission.USER_VIEW})
    )
    PostgresRoleRepository(setup).add(role)
    setup.commit()
    setup.close()

    assert (
        test_client.post(
            SESSION_PATH,
            json={
                "login_name": "wang.admin",
                "password": BOOTSTRAP_PASSWORD,
            },  # pragma: allowlist secret
        ).status_code
        == 201
    )
    missing = uuid4()

    refused = test_client.put(
        f"{API_PREFIX}/auth/users/{missing}/roles",
        json={"role_ids": [str(role.id)]},
        headers={"x-csrf-token": test_client.cookies["sop_csrf"]},
    )

    # The same answer an empty submission gets, and the only one the resource supports: the
    # account does not exist, whatever the body carried.
    assert refused.status_code == 404
    assert refused.json()["error_code"] == "USER_NOT_FOUND"

    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT count(*) FROM auth_user_role WHERE user_id = :id"), {"id": missing}
        ).scalar_one()
    assert rows == 0
