"""从 C2.1（0002）升级的部署不失权：迁移自己把管理员角色还回去。

一个停在 0002 的部署持有 bootstrap 账户和 guard 行，但没有任何角色表——角色表是 0003
创建的。如果迁移只建表不回填，现有管理员会以 `permissions: []` 登录，再也无法通过产品
创建角色或授权，而 bootstrap 因 guard 永远跳过：唯一的修复是直接改生产库。这个套件对
着真实 PostgreSQL 走一遍 0002 → head，断言升级后的部署和全新 bootstrap 过的部署等价。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, text

from factory_sop.app import API_PREFIX, create_app
from factory_sop.auth.permissions import Permission
from factory_sop.persistence import session_factory
from factory_sop.settings import Settings

CONTROL_API = Path(__file__).resolve().parents[2]
SESSION_PATH = f"{API_PREFIX}/auth/session"
BOOTSTRAP_PASSWORD = "first-shift-key"  # pragma: allowlist secret
C2_1_ACCOUNT_ID = UUID("018f0000-0000-7000-8000-00000000c201")


@pytest.fixture
def upgraded_from_0002(engine: Engine, tmp_path: Path) -> Iterator[Engine]:
    """A database that lived at 0002 with a bootstrapped account, then upgraded to head.

    A separate database rather than the session's one: that one is already at head, and the
    point is to walk the upgrade path itself, not to rebuild it from inserts.
    """
    server = engine.url._replace(database="postgres")
    installer = create_engine(server, isolation_level="AUTOCOMMIT")
    scratch = f"nvsop_upgrade_{uuid4().hex[:12]}"
    with installer.connect() as connection:
        connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{scratch}"')
        connection.exec_driver_sql(f'CREATE DATABASE "{scratch}"')
    installer.dispose()

    upgraded = create_engine(engine.url._replace(database=scratch))
    configuration = Config(str(CONTROL_API / "alembic.ini"))
    configuration.set_main_option("script_location", str(CONTROL_API / "migrations"))
    # `str(url)` masks the password with `***` — deliberate for logs, fatal for a connection
    # string. Alembic reads this value and dials with it, so it needs the real one.
    configuration.set_main_option(
        "sqlalchemy.url", upgraded.url.render_as_string(hide_password=False)
    )
    command.upgrade(configuration, "0002")

    # The state 0002 leaves behind: one bootstrap account (the guard allowed only one), its
    # claim row, and nothing else. No `created_by` — the columns arrive with this upgrade.
    with upgraded.begin() as connection:
        connection.execute(
            text("INSERT INTO auth_bootstrap_guard (singleton) VALUES (1)"),
        )
        connection.execute(
            text(
                "INSERT INTO auth_user (id, login_name, display_name, password_hash, status) "
                "VALUES (:id, 'wang.admin', '王管理员', :password_hash, 'active')"
            ),
            {
                "id": C2_1_ACCOUNT_ID,
                "password_hash": _argon2_hash(tmp_path),
            },
        )

    command.upgrade(configuration, "head")
    yield upgraded

    upgraded.dispose()
    with installer.connect() as connection:
        connection.exec_driver_sql(f'DROP DATABASE "{scratch}"')


def _argon2_hash(tmp_path: Path) -> str:
    """A real Argon2id encoding for the account, so the login below is the real login path."""
    from factory_sop.auth.passwords import hash_password

    return hash_password(BOOTSTRAP_PASSWORD)


def test_the_upgrade_seeds_the_administrator_role_for_the_existing_account(
    upgraded_from_0002: Engine,
) -> None:
    with upgraded_from_0002.connect() as connection:
        role = connection.execute(
            text("SELECT id, code, created_by FROM auth_role WHERE code = 'system_administrator'")
        ).one()
        permissions = (
            connection.execute(
                text(
                    "SELECT permission FROM auth_role_permission "
                    "WHERE role_id = :id ORDER BY permission"
                ),
                {"id": role.id},
            )
            .scalars()
            .all()
        )
        holders = (
            connection.execute(
                text("SELECT user_id FROM auth_user_role WHERE role_id = :id"),
                {"id": role.id},
            )
            .scalars()
            .all()
        )

    # The seeded role is the ordinary one the bootstrap would have created — every permission
    # this build registers, attributed to the account it was created for.
    assert role.code == "system_administrator"
    assert role.created_by == C2_1_ACCOUNT_ID
    assert permissions == sorted(item.value for item in Permission)
    assert holders == [C2_1_ACCOUNT_ID]


def test_the_upgraded_administrator_logs_in_with_every_registered_permission(
    upgraded_from_0002: Engine,
) -> None:
    """The observable the review asked for: the login response carries the full grant."""
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
    app.state.session_factory = session_factory(upgraded_from_0002)

    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            SESSION_PATH,
            json={"login_name": "wang.admin", "password": BOOTSTRAP_PASSWORD},
        )

    assert response.status_code == 201
    assert response.json()["permissions"] == sorted(item.value for item in Permission)
