"""单 PostgreSQL 实例内 `nvsop` / `training` 双 database 的部署契约（Q35）。

证据分三层：开发 Compose 只启动一套 PostgreSQL，并把训练/标注进程指向同一实例的 `training`
database；`training-db-init` 的真实命令在隔离实例上首次建库、二次幂等且不删已有内容；真实
PostgreSQL 上 Center Alembic 只落 `nvsop`，训练连接实际落在 `training`，且没有 `dblink` / FDW
等跨 database 直连路径。训练对象安装与角色隔离不在本票（见 #224 / #223）。
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from docker import DockerClient  # type: ignore[import-untyped]
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import URL, make_url
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.network import Network

REPO_ROOT = Path(__file__).resolve().parents[4]
CONTROL_API = REPO_ROOT / "apps/control-api"
COMPOSE = REPO_ROOT / "deploy" / "dev" / "compose.yaml"
VENDOR_TRAINING_COMPOSE = (
    REPO_ROOT / "vendor/sop-monitoring-blueprints/microservices/sop-training-bp/docker-compose.yml"
)
POSTGRES_IMAGE = "postgres:17.2-bookworm"
TRAINING_PROCESS_SERVICES = (
    "sop-data-gen",
    "cosmos-reason-microservice",
    "ddm-training-microservice",
    "evaluation-microservice",
    "annotation-backend",
)
# 本票只部署 annotation-backend；其余四类训练进程不在此启动。
UNDEPLOYED_TRAINING_SERVICES = TRAINING_PROCESS_SERVICES[:4]


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


def _dev_compose_services() -> dict[str, dict[str, Any]]:
    """用 Compose 自身解析部署文件，顺带验证服务图引用完整。"""
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "config", "--format", "json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    document = cast("dict[str, Any]", json.loads(result.stdout))
    return cast("dict[str, dict[str, Any]]", document["services"])


def _service_block(source: str, name: str) -> str:
    start = source.index(f"  {name}:\n")
    following = re.search(r"\n  [A-Za-z][\w-]*:\n", source[start + 1 :])
    end = start + 1 + following.start() if following else len(source)
    return source[start:end]


def test_dev_compose_runs_one_postgres_instance_for_center_and_training() -> None:
    services = _dev_compose_services()

    # 不再有第二个 PostgreSQL 服务或第二套 metadata_db。
    assert "annotation-db" not in services
    assert "metadata_db" not in services
    postgres_services = {
        name: service.get("image")
        for name, service in services.items()
        if str(service.get("image", "")).startswith("postgres:")
    }
    assert postgres_services == {
        "center-db": POSTGRES_IMAGE,
        "training-db-init": POSTGRES_IMAGE,
    }

    center_environment = cast("dict[str, str]", services["center-db"]["environment"])
    assert center_environment["POSTGRES_DB"] == "nvsop"
    assert center_environment["POSTGRES_USER"] == "nvsop"

    # Center 进程（含 Alembic）只连接 nvsop，且不依赖 training 初始化。
    for name in ("center-migrate", "center-api", "center-bootstrap", "worker"):
        environment = cast("dict[str, str]", services[name]["environment"])
        assert environment["SOP_DATABASE_NAME"] == "nvsop"
        assert "training-db-init" not in (services[name].get("depends_on") or {})

    # 训练/标注进程经 Vendor POSTGRES_* seam 连接同一实例的 training database。
    backend_environment = cast("dict[str, str]", services["annotation-backend"]["environment"])
    assert backend_environment["POSTGRES_HOST"] == "center-db"
    assert backend_environment["POSTGRES_DB"] == "training"
    assert backend_environment["POSTGRES_USER"] == "nvsop"
    assert services["annotation-backend"]["depends_on"]["training-db-init"]["condition"] == (
        "service_completed_successfully"
    )

    # training database 在同一实例上幂等创建，而不是启动第二套运行数据库。
    init = services["training-db-init"]
    assert init["depends_on"]["center-db"]["condition"] == "service_healthy"
    init_command = " ".join(cast("list[str]", init["command"]))
    assert "pg_database" in init_command
    assert "CREATE DATABASE training" in init_command

    # 本票只部署 annotation-backend；其余四类训练进程与第二套 metadata_db/adminer 不在此启动。
    assert "annotation-backend" in services
    for absent in (*UNDEPLOYED_TRAINING_SERVICES, "metadata_db", "adminer"):
        assert absent not in services


def test_vendor_training_processes_share_the_postgres_db_seam() -> None:
    """五类训练/标注进程都经同一 `POSTGRES_DB` seam 连接，不需要 search_path 适配。"""
    source = VENDOR_TRAINING_COMPOSE.read_text(encoding="utf-8")
    for name in TRAINING_PROCESS_SERVICES:
        block = _service_block(source, name)
        assert "POSTGRES_DB=${POSTGRES_DB" in block
        assert "POSTGRES_HOST=${POSTGRES_HOST" in block
        assert "POSTGRES_USER=${POSTGRES_USER}" in block
        assert "POSTGRES_PASSWORD=${POSTGRES_PASSWORD}" in block
        assert "search_path" not in block


def test_training_db_init_command_creates_training_idempotently(tmp_path: Path) -> None:
    """在隔离实例上执行 Compose 渲染的真实 init 命令：首次建库、二次幂等且不删已有内容。"""
    _require_docker()
    services = _dev_compose_services()
    init = services["training-db-init"]
    environment = cast("dict[str, str]", init["environment"])
    # Compose 运行时把 `$$` 还原为 `$`；config 输出保留转义，这里按同一规则还原后交给真实 shell。
    command = " ".join(cast("list[str]", init["command"])).replace("$$", "$")
    password = "training-init-" + uuid4().hex  # pragma: allowlist secret
    secret = tmp_path / "center-db-password"
    secret.write_text(password + "\n", encoding="utf-8")
    secret.chmod(0o600)

    with Network() as network:
        server = (
            PostgresContainer(
                POSTGRES_IMAGE,
                driver="psycopg",
                username="nvsop",
                password=password,
                dbname="nvsop",
            )
            .with_network(network)
            .with_network_aliases("center-db")
        )
        client = (
            DockerContainer(POSTGRES_IMAGE)
            .with_network(network)
            .with_env("PGHOST", environment["PGHOST"])
            .with_env("PGUSER", environment["PGUSER"])
            .with_env("PGDATABASE", environment["PGDATABASE"])
            .with_volume_mapping(secret, "/run/secrets/center-db-password")
            .with_command(["/bin/sh", "-c", "sleep infinity"])
        )
        with server, client:
            first = client.exec(["/bin/sh", "-c", command])
            assert first.exit_code == 0, first.output
            # 在 training 留下真实内容，证明第二次执行不会删除已有训练内容。
            marker = client.exec(
                [
                    "/bin/sh",
                    "-c",
                    'PGPASSWORD="$(cat /run/secrets/center-db-password)" psql -d training '
                    '-v ON_ERROR_STOP=1 -c "CREATE TABLE IF NOT EXISTS _init_probe (id int); '
                    'INSERT INTO _init_probe VALUES (1);"',
                ]
            )
            assert marker.exit_code == 0, marker.output
            second = client.exec(["/bin/sh", "-c", command])
            assert second.exit_code == 0, second.output
            probe = client.exec(
                [
                    "/bin/sh",
                    "-c",
                    'PGPASSWORD="$(cat /run/secrets/center-db-password)" psql -d training '
                    '-tAc "SELECT count(*) FROM _init_probe"',
                ]
            )
            assert probe.exit_code == 0, probe.output
            assert probe.output.strip() == b"1"


@pytest.fixture(scope="module")
def postgres_instance() -> Iterator[PostgresContainer]:
    """一套真实 PostgreSQL 实例，供 Center 与 training 共享。"""
    _require_docker()
    container = PostgresContainer(POSTGRES_IMAGE, driver="psycopg")
    container.start()
    try:
        base = make_url(container.get_connection_url())
        engine = create_engine(base, isolation_level="AUTOCOMMIT")
        with engine.connect() as connection:
            for name in ("nvsop", "training"):
                connection.execute(text(f'CREATE DATABASE "{name}"'))
        engine.dispose()
        yield container
    finally:
        container.stop()


def _assert_no_cross_database_paths(connection: Connection) -> None:
    extensions = set(connection.execute(text("SELECT extname FROM pg_extension")).scalars())
    assert "dblink" not in extensions
    assert "postgres_fdw" not in extensions
    assert connection.execute(text("SELECT count(*) FROM pg_foreign_server")).scalar_one() == 0


def _public_tables(connection: Connection) -> set[str]:
    return set(
        connection.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        ).scalars()
    )


def test_center_migration_stays_in_nvsop_and_training_connection_lands_in_training(
    postgres_instance: PostgresContainer,
) -> None:
    base: URL = make_url(postgres_instance.get_connection_url())
    nvsop_url = base.set(database="nvsop")
    training_url = base.set(database="training")

    configuration = Config(str(CONTROL_API / "alembic.ini"))
    configuration.set_main_option("script_location", str(CONTROL_API / "migrations"))
    configuration.set_main_option("sqlalchemy.url", nvsop_url.render_as_string(hide_password=False))
    command.upgrade(configuration, "head")

    nvsop_engine = create_engine(nvsop_url)
    training_engine = create_engine(training_url)
    try:
        with nvsop_engine.connect() as connection:
            databases = set(connection.execute(text("SELECT datname FROM pg_database")).scalars())
            assert {"nvsop", "training"} <= databases
            nvsop_tables = _public_tables(connection)
            assert "alembic_version" in nvsop_tables
            assert nvsop_tables
            _assert_no_cross_database_paths(connection)

        with training_engine.connect() as connection:
            # Vendor 用未限定对象名连接自身 database，不需要 search_path 适配。
            assert connection.execute(text("SELECT current_database()")).scalar_one() == "training"
            assert connection.execute(text("SELECT current_schema()")).scalar_one() == "public"
            assert _public_tables(connection) == set()
            _assert_no_cross_database_paths(connection)
    finally:
        nvsop_engine.dispose()
        training_engine.dispose()
