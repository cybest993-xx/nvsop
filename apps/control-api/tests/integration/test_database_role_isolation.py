"""S065：单 PostgreSQL 实例上 `nvsop` / `training` 的真实角色权限隔离。

证据使用真实新连接验证双向允许/拒绝，再验证两个 database 各自的对象操作、禁止操作与
未来对象 default privileges；不使用 `SET ROLE`、HTTP mock 或 `search_path` 代替真实登录。
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from docker import DockerClient  # type: ignore[import-untyped]
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import OperationalError, ProgrammingError
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.network import Network

REPO_ROOT = Path(__file__).resolve().parents[4]
CONTROL_API = REPO_ROOT / "apps/control-api"
COMPOSE = REPO_ROOT / "deploy" / "dev" / "compose.yaml"
ROLE_SQL = REPO_ROOT / "deploy" / "dev" / "db-roles.sql"
ANNOTATION_ENTRYPOINT = REPO_ROOT / "deploy" / "dev" / "annotation-entrypoint.sh"
POSTGRES_IMAGE = "postgres:17.2-bookworm"
INSTALL_ROLE = "nvsop"
CENTER_DATABASE = "nvsop"
TRAINING_DATABASE = "training"
CENTER_RUNTIME_ROLE = "nvsop_runtime"
TRAINING_RUNTIME_ROLE = "training_runtime"
ROLE_INIT_SERVICES = ("center-role-init", "training-role-init")
PERMISSION_DENIED = "permission denied"
NON_SUPERUSER_ATTRIBUTES = (False, False, False, False, False, False)


@dataclass(frozen=True, slots=True)
class Runtime:
    """一个 database 及其专属非超级用户运行身份。"""

    database: str
    role: str
    password: str


@dataclass(frozen=True, slots=True)
class IsolatedInstance:
    """真实实例上建立的角色隔离，供各断言复用。"""

    base_url: URL
    install_password: str
    runtimes: tuple[Runtime, ...]
    init_outputs: dict[str, bytes]
    reinit: Callable[[], dict[str, Any]]

    def runtime(self, database: str) -> Runtime:
        return next(item for item in self.runtimes if item.database == database)


def _require_docker() -> None:
    """没有 Docker daemon 时让真实基础设施测试明确失败，而不是跳过。"""
    docker_client: DockerClient | None = None
    try:
        docker_client = DockerClient.from_env()
        docker_client.ping()
    except Exception as error:  # pragma: no cover - 环境不可用时显式失败
        pytest.fail(f"Docker daemon 不可用：{type(error).__name__}")
    finally:
        if docker_client is not None:
            docker_client.close()


def _compose_document() -> dict[str, Any]:
    """用 Compose 自身解析部署文件。"""
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "config", "--format", "json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return cast("dict[str, Any]", json.loads(result.stdout))


def _compose_services() -> dict[str, dict[str, Any]]:
    return cast("dict[str, dict[str, Any]]", _compose_document()["services"])


def _rendered_command(service: dict[str, Any]) -> str:
    """还原 Compose 运行时的 `$$` → `$`，得到真实 shell 命令。"""
    return " ".join(cast("list[str]", service["command"])).replace("$$", "$")


def _migrate(base: URL) -> None:
    """以安装身份把中心迁移应用到 `nvsop`，验证 default privileges 覆盖真实中心表。"""
    configuration = Config(str(CONTROL_API / "alembic.ini"))
    configuration.set_main_option("script_location", str(CONTROL_API / "migrations"))
    configuration.set_main_option(
        "sqlalchemy.url", base.set(database=CENTER_DATABASE).render_as_string(hide_password=False)
    )
    command.upgrade(configuration, "head")


def _engine(instance: IsolatedInstance, *, database: str, user: str, password: str) -> Engine:
    return create_engine(instance.base_url.set(database=database, username=user, password=password))


def _attempt_login(
    instance: IsolatedInstance, *, database: str, user: str, password: str
) -> str | None:
    """发起一次真实登录；成功返回 None，被拒绝返回 PostgreSQL 的拒绝信息。"""
    engine = _engine(instance, database=database, user=user, password=password)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return None
    except OperationalError as error:
        return str(error.orig)
    finally:
        engine.dispose()


def _role_attributes(instance: IsolatedInstance, role: str) -> tuple[object, ...]:
    """读全局角色属性；角色是实例级的，从任一 database 都可查。"""
    install = _engine(
        instance,
        database=CENTER_DATABASE,
        user=INSTALL_ROLE,
        password=instance.install_password,
    )
    try:
        with install.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT rolsuper, rolcreatedb, rolcreaterole, rolinherit, "
                    "rolreplication, rolbypassrls "
                    "FROM pg_roles WHERE rolname = :role"
                ),
                {"role": role},
            ).one()
        return tuple(row)
    finally:
        install.dispose()


def _expect_denied(
    instance: IsolatedInstance, runtime: Runtime, statement: str, match: str
) -> None:
    """runtime 执行一条被拒绝的语句：每个断言用独立连接，避免事务中止互相污染。"""
    engine = _engine(
        instance, database=runtime.database, user=runtime.role, password=runtime.password
    )
    try:
        with pytest.raises(ProgrammingError, match=match), engine.begin() as connection:
            connection.execute(text(statement))
    finally:
        engine.dispose()


def _assert_runtime_can_use_future_objects(instance: IsolatedInstance, runtime: Runtime) -> None:
    """安装身份在初始化之后建表/序列，runtime 仅凭 default privileges 即可 DML。"""
    probe = f"s065_default_privilege_probe_{runtime.database}"
    install = _engine(
        instance,
        database=runtime.database,
        user=INSTALL_ROLE,
        password=instance.install_password,
    )
    try:
        with install.begin() as connection:
            connection.execute(text(f"DROP TABLE IF EXISTS {probe}"))
            # `bigserial` 同时验证未来序列的 default privileges，而不只是表。
            connection.execute(text(f"CREATE TABLE {probe} (id bigserial primary key, note text)"))
        engine = _engine(
            instance, database=runtime.database, user=runtime.role, password=runtime.password
        )
        try:
            with engine.begin() as connection:
                connection.execute(text(f"INSERT INTO {probe} (note) VALUES ('a')"))
                connection.execute(text(f"UPDATE {probe} SET note = 'b' WHERE id = 1"))
                assert connection.execute(text(f"SELECT note FROM {probe}")).scalar_one() == "b"
                connection.execute(text(f"DELETE FROM {probe} WHERE id = 1"))
        finally:
            engine.dispose()
    finally:
        with install.begin() as connection:
            connection.execute(text(f"DROP TABLE IF EXISTS {probe}"))
        install.dispose()


@pytest.fixture(scope="module")
def isolated_instance(tmp_path_factory: pytest.TempPathFactory) -> Iterator[IsolatedInstance]:
    """一套真实 PostgreSQL 实例：执行 Compose 的角色初始化命令后应用中心迁移。"""
    _require_docker()
    services = _compose_services()
    center_init = services["center-role-init"]
    training_init = services["training-role-init"]
    assert cast("dict[str, str]", center_init["environment"])["PGHOST"] == "center-db"

    install_password = "install-" + uuid4().hex  # pragma: allowlist secret
    center_runtime_password = "center-runtime-" + uuid4().hex  # pragma: allowlist secret
    training_runtime_password = "training-runtime-" + uuid4().hex  # pragma: allowlist secret
    secrets = tmp_path_factory.mktemp("role-secrets")
    (secrets / "center-db-password").write_text(install_password + "\n", encoding="utf-8")
    (secrets / "nvsop-runtime-password").write_text(
        center_runtime_password + "\n", encoding="utf-8"
    )
    (secrets / "training-runtime-password").write_text(
        training_runtime_password + "\n", encoding="utf-8"
    )

    with Network() as network:
        server = (
            PostgresContainer(
                POSTGRES_IMAGE,
                driver="psycopg",
                username=INSTALL_ROLE,
                password=install_password,
                dbname=CENTER_DATABASE,
            )
            .with_network(network)
            .with_network_aliases("center-db")
        )
        client = (
            DockerContainer(POSTGRES_IMAGE)
            .with_network(network)
            .with_env("PGHOST", "center-db")
            .with_env("PGUSER", INSTALL_ROLE)
            .with_volume_mapping(ROLE_SQL, "/opt/nvsop/db-roles.sql")
            .with_volume_mapping(secrets / "center-db-password", "/run/secrets/center-db-password")
            .with_volume_mapping(
                secrets / "nvsop-runtime-password", "/run/secrets/nvsop-runtime-password"
            )
            .with_volume_mapping(
                secrets / "training-runtime-password", "/run/secrets/training-runtime-password"
            )
            .with_command(["/bin/sh", "-c", "sleep infinity"])
        )
        with server, client:
            created = client.exec(
                [
                    "/bin/sh",
                    "-c",
                    'PGPASSWORD="$(cat /run/secrets/center-db-password)" '
                    f"psql -v ON_ERROR_STOP=1 -d {CENTER_DATABASE} "
                    f'-c "CREATE DATABASE {TRAINING_DATABASE}"',
                ]
            )
            assert created.exit_code == 0, created.output

            init_outputs: dict[str, bytes] = {}
            for name, service in (("center", center_init), ("training", training_init)):
                result = client.exec(["/bin/sh", "-c", _rendered_command(service)])
                assert result.exit_code == 0, result.output
                init_outputs[name] = result.output

            def reinit() -> dict[str, Any]:
                """并发重跑两个真实 Compose 命令，验证可重复运行且互不争用角色对象。"""
                commands = {
                    name: ["/bin/sh", "-c", _rendered_command(service)]
                    for name, service in (("center", center_init), ("training", training_init))
                }
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = {
                        name: executor.submit(client.exec, command)
                        for name, command in commands.items()
                    }
                    return {name: future.result() for name, future in futures.items()}

            base = make_url(server.get_connection_url())
            _migrate(base)

            yield IsolatedInstance(
                base_url=base,
                install_password=install_password,
                runtimes=(
                    Runtime(CENTER_DATABASE, CENTER_RUNTIME_ROLE, center_runtime_password),
                    Runtime(TRAINING_DATABASE, TRAINING_RUNTIME_ROLE, training_runtime_password),
                ),
                init_outputs=init_outputs,
                reinit=reinit,
            )


def test_runtime_roles_connect_only_their_own_database(
    isolated_instance: IsolatedInstance,
) -> None:
    instance = isolated_instance

    for runtime in instance.runtimes:
        assert (
            _attempt_login(
                instance,
                database=runtime.database,
                user=runtime.role,
                password=runtime.password,
            )
            is None
        )
        other = TRAINING_DATABASE if runtime.database == CENTER_DATABASE else CENTER_DATABASE
        denied = _attempt_login(
            instance, database=other, user=runtime.role, password=runtime.password
        )
        assert denied is not None
        assert PERMISSION_DENIED in denied

    # 安装身份与两个 runtime 分离，且仍能进入两个 database（迁移与对象安装需要）。
    for database in (CENTER_DATABASE, TRAINING_DATABASE):
        assert (
            _attempt_login(
                instance, database=database, user=INSTALL_ROLE, password=instance.install_password
            )
            is None
        )


def test_public_cannot_bypass_connect_or_object_privileges(
    isolated_instance: IsolatedInstance,
) -> None:
    """只依赖 PUBLIC 默认权限的角色在两个 database 都既不能 CONNECT，也拿不到 schema / 表权限。"""
    instance = isolated_instance
    probe = "public_probe_" + uuid4().hex[:8]
    probe_password = "probe-" + uuid4().hex  # pragma: allowlist secret
    install = _engine(
        instance,
        database=CENTER_DATABASE,
        user=INSTALL_ROLE,
        password=instance.install_password,
    )
    try:
        with install.begin() as connection:
            # DDL 不接受绑定参数；密码是测试内生成的十六进制串。
            connection.execute(text(f"CREATE ROLE \"{probe}\" LOGIN PASSWORD '{probe_password}'"))
        for database in (CENTER_DATABASE, TRAINING_DATABASE):
            table = f"s065_public_probe_{database}"
            database_engine = _engine(
                instance,
                database=database,
                user=INSTALL_ROLE,
                password=instance.install_password,
            )
            try:
                with database_engine.begin() as connection:
                    connection.execute(text(f"DROP TABLE IF EXISTS {table}"))
                    connection.execute(text(f"CREATE TABLE {table} (id int)"))
                    for query, parameters in (
                        (
                            "SELECT has_database_privilege(:role, :database, 'CONNECT')",
                            {"role": probe, "database": database},
                        ),
                        ("SELECT has_schema_privilege(:role, 'public', 'USAGE')", {"role": probe}),
                        (
                            f"SELECT has_table_privilege(:role, '{table}', 'SELECT')",
                            {"role": probe},
                        ),
                    ):
                        assert connection.execute(text(query), parameters).scalar_one() is False
                    # runtime 对安装身份新建的表有 default privilege SELECT。
                    assert (
                        connection.execute(
                            text(f"SELECT has_table_privilege(:role, '{table}', 'SELECT')"),
                            {"role": instance.runtime(database).role},
                        ).scalar_one()
                        is True
                    )
                with database_engine.begin() as connection:
                    connection.execute(text(f"DROP TABLE IF EXISTS {table}"))
            finally:
                database_engine.dispose()
        for database in (CENTER_DATABASE, TRAINING_DATABASE):
            denied = _attempt_login(
                instance, database=database, user=probe, password=probe_password
            )
            assert denied is not None
            assert PERMISSION_DENIED in denied
    finally:
        with install.begin() as connection:
            connection.execute(text(f'DROP ROLE IF EXISTS "{probe}"'))
        install.dispose()


def test_runtime_is_non_superuser_and_cannot_use_ddl(
    isolated_instance: IsolatedInstance,
) -> None:
    instance = isolated_instance
    for runtime in instance.runtimes:
        assert _role_attributes(instance, runtime.role) == NON_SUPERUSER_ATTRIBUTES
        _expect_denied(
            instance,
            runtime,
            "CREATE TABLE forbidden_probe (id int)",
            "permission denied for schema",
        )

    # Center runtime 能读安装身份迁移出的真实表，但不能改其结构。
    center = instance.runtime(CENTER_DATABASE)
    engine = _engine(instance, database=center.database, user=center.role, password=center.password)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM auth_user")).scalar_one() == 0
    finally:
        engine.dispose()
    _expect_denied(instance, center, "DROP TABLE auth_user", "must be owner")
    _expect_denied(
        instance, center, "ALTER TABLE auth_user ADD COLUMN forbidden int", "must be owner"
    )

    # AC2：runtime 显式获得中心枚举类型的 USAGE，而不是只依赖 PUBLIC 内建授权。
    install = _engine(
        instance,
        database=CENTER_DATABASE,
        user=INSTALL_ROLE,
        password=instance.install_password,
    )
    try:
        with install.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT has_type_privilege(:role, 'auth_user_status', 'USAGE')"),
                    {"role": center.role},
                ).scalar_one()
                is True
            )
    finally:
        install.dispose()


def test_default_privileges_cover_objects_created_after_init(
    isolated_instance: IsolatedInstance,
) -> None:
    for runtime in isolated_instance.runtimes:
        _assert_runtime_can_use_future_objects(isolated_instance, runtime)


def test_role_init_is_idempotent(isolated_instance: IsolatedInstance) -> None:
    """并发重跑两个真实 Compose 角色初始化命令必须各自成功且不改动已有对象。"""
    results = isolated_instance.reinit()
    for name, result in results.items():
        assert result.exit_code == 0, (name, result.output)
        assert b"ERROR" not in result.output, (name, result.output)


def test_deployment_never_inlines_runtime_passwords(
    isolated_instance: IsolatedInstance,
) -> None:
    instance = isolated_instance
    document = _compose_document()
    for name, service in document["services"].items():
        environment = cast("dict[str, str]", service.get("environment") or {})
        assert "SOP_DATABASE_PASSWORD" not in environment, name
        assert "POSTGRES_PASSWORD" not in environment, name
        for key in ("SOP_DATABASE_PASSWORD_FILE", "POSTGRES_PASSWORD_FILE"):
            if key in environment:
                assert environment[key].startswith("/run/secrets/"), (name, key)

    passwords = (
        instance.install_password,
        *(runtime.password for runtime in instance.runtimes),
    )
    for name in ROLE_INIT_SERVICES:
        command = _rendered_command(document["services"][name])
        assert "cat /run/secrets/" in command
    # 真实初始化输出会把代入后的密码交给 psql；日志/输出里不能出现它。
    for output in instance.init_outputs.values():
        for password in passwords:
            assert password.encode() not in output

    entrypoint = ANNOTATION_ENTRYPOINT.read_text(encoding="utf-8")
    assert "/run/secrets/training-runtime-password" in entrypoint
    assert "center-db-password" not in entrypoint
