"""S066：空 `training` database 的五类训练/标注对象安装。

在真实 PostgreSQL 上执行 Compose 渲染的 `training-objects-install` 入口，验证：空库首次安装
出五类进程所需对象并记录消费的 Vendor 提交/DDL 摘要，S065 的 `training_runtime` 能实际读写
这些对象；重复启动跳过且保留既有数据；未知非空状态与版本不符明确拒绝；DDL 失败时整事务回滚，
不留下半安装状态。受支持历史数据的增量升级由 S067 负责，不在此票。
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from docker import DockerClient  # type: ignore[import-untyped]
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import BytesExecResult, DockerContainer, ExecConfig
from testcontainers.core.network import Network

REPO_ROOT = Path(__file__).resolve().parents[4]
COMPOSE = REPO_ROOT / "deploy" / "dev" / "compose.yaml"
POSTGRES_IMAGE = "postgres:17.2-bookworm"
INSTALL_SERVICE = "training-objects-install"
ROLE_INIT_SERVICE = "training-role-init"
INSTALL_ROLE = "nvsop"
TRAINING_DATABASE = "training"
# 重复启动场景用独立 database，避免与首次安装场景共享状态而互相依赖执行顺序。
REINSTALL_DATABASE = "training_reinstall"
TRAINING_RUNTIME_ROLE = "training_runtime"
VERSION_TABLE = "nvsop_training_install"
# Vendor 合并 DDL（sop-training-bp/db-init-scripts/01-init-tables.sql）覆盖五类进程的对象。
TRAINING_TYPES = frozenset({"status_enum", "training_status_enum"})
TRAINING_TABLES = frozenset(
    {
        "dataset",
        "video",
        "chunk",
        "annotation",
        "augmented_data",
        "augmentation_stages",
        "training_job",
        "ddm_training_job",
        "evaluation_job",
        "e2e_evaluation_job",
    }
)


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


def _compose_services() -> dict[str, dict[str, Any]]:
    """用 Compose 自身解析部署文件，测试跟随真实挂载与环境。"""
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


def _rendered_command(service: dict[str, Any]) -> str:
    """还原 Compose 运行时的 `$$` → `$`，得到真实 shell 命令。"""
    return " ".join(cast("list[str]", service["command"])).replace("$$", "$")


def _merged_client_container(
    services: dict[str, dict[str, Any]], network: Network, secrets: Path
) -> DockerContainer:
    """按真实 Compose 定义合并 role-init / install 的挂载、环境与 secret。"""
    container = DockerContainer(POSTGRES_IMAGE).with_network(network)
    environment: dict[str, str] = {}
    volumes: dict[str, str] = {}
    secret_targets: dict[str, str] = {}
    for name in (ROLE_INIT_SERVICE, INSTALL_SERVICE):
        service = services[name]
        environment.update(cast("dict[str, str]", service.get("environment") or {}))
        for volume in cast("list[dict[str, str]]", service.get("volumes") or []):
            volumes[volume["target"]] = volume["source"]
        for secret in cast("list[dict[str, str]]", service.get("secrets") or []):
            secret_targets[secret["target"]] = secret["source"]
    for key, value in environment.items():
        container = container.with_env(key, value)
    for target, source in volumes.items():
        container = container.with_volume_mapping(source, target, "ro")
    for target, source in secret_targets.items():
        container = container.with_volume_mapping(secrets / source, target)
    return container.with_command(["/bin/sh", "-c", "sleep infinity"])


@dataclass(frozen=True, slots=True)
class TrainingInstall:
    """真实实例上的安装环境：安装身份、runtime 身份、挂载好的客户端与真实入口。"""

    base_url: URL
    install_password: str
    runtime_password: str
    client: DockerContainer
    install_entrypoint: list[str]
    vendor_commit: str
    ddl_path: str
    ddl_sha256: str

    def engine(self, *, database: str, role: str, password: str) -> Engine:
        return create_engine(self.base_url.set(database=database, username=role, password=password))

    def run_install(
        self, *, database: str | None = None, extra_env: dict[str, str] | None = None
    ) -> BytesExecResult:
        environment = dict(extra_env or {})
        if database is not None:
            environment["PGDATABASE"] = database
        return self.client.exec(
            ExecConfig(command=self.install_entrypoint, environment=environment or None)
        )

    def public_tables(self, *, database: str) -> set[str]:
        engine = self.engine(database=database, role=INSTALL_ROLE, password=self.install_password)
        try:
            with engine.connect() as connection:
                return set(
                    connection.execute(
                        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                    ).scalars()
                )
        finally:
            engine.dispose()


@pytest.fixture(scope="module")
def training_install(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TrainingInstall]:
    """一套真实 PostgreSQL 实例：执行 Compose 的 role-init 与 install 入口。"""
    yield from build_training_install(tmp_path_factory)


def build_training_install(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[TrainingInstall]:
    """构建 S066/S067 共用的真实实例：执行 Compose 的 role-init 与 install 入口。"""
    _require_docker()
    services = _compose_services()
    install_service = services[INSTALL_SERVICE]
    install_environment = cast("dict[str, str]", install_service["environment"])

    install_password = "install-" + uuid4().hex  # pragma: allowlist secret
    runtime_password = "training-runtime-" + uuid4().hex  # pragma: allowlist secret
    secrets = tmp_path_factory.mktemp("training-install-secrets")
    (secrets / "center-db-password").write_text(install_password + "\n", encoding="utf-8")
    (secrets / "training-runtime-password").write_text(runtime_password + "\n", encoding="utf-8")

    with Network() as network:
        server = (
            PostgresContainer(
                cast("str", services["center-db"]["image"]),
                driver="psycopg",
                username=INSTALL_ROLE,
                password=install_password,
                dbname="postgres",
            )
            .with_network(network)
            .with_network_aliases("center-db")
        )
        client = _merged_client_container(services, network, secrets)
        with server, client:
            base_url = make_url(server.get_connection_url()).set(database=TRAINING_DATABASE)
            admin = create_engine(base_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
            try:
                with admin.connect() as connection:
                    # template0 保持 training 为普通库；其余库用于拒绝、失败和重复启动。
                    for name in (
                        TRAINING_DATABASE,
                        "training_unknown",
                        "training_mismatch",
                        "training_broken",
                        "training_drifted",
                        REINSTALL_DATABASE,
                    ):
                        connection.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0'))
            finally:
                admin.dispose()

            role_init = client.exec(
                ["/bin/sh", "-c", _rendered_command(services[ROLE_INIT_SERVICE])]
            )
            assert role_init.exit_code == 0, role_init.output
            yield TrainingInstall(
                base_url=base_url,
                install_password=install_password,
                runtime_password=runtime_password,
                client=client,
                install_entrypoint=cast("list[str]", install_service["entrypoint"]),
                vendor_commit=install_environment["NVSOP_TRAINING_VENDOR_COMMIT"],
                ddl_path=install_environment["NVSOP_TRAINING_DDL_PATH"],
                ddl_sha256=install_environment["NVSOP_TRAINING_DDL_SHA256"],
            )


def test_install_creates_training_objects_and_records_vendor_version(
    training_install: TrainingInstall,
) -> None:
    """空库首次安装：五类对象与版本记录出现，`training_runtime` 能实际读写。"""
    instance = training_install
    result = instance.run_install(database=TRAINING_DATABASE)
    assert result.exit_code == 0, result.output

    install_engine = instance.engine(
        database=TRAINING_DATABASE, role=INSTALL_ROLE, password=instance.install_password
    )
    try:
        with install_engine.connect() as connection:
            assert instance.public_tables(database=TRAINING_DATABASE) >= TRAINING_TABLES
            enums = set(
                connection.execute(
                    text(
                        "SELECT typname FROM pg_type t "
                        "JOIN pg_namespace n ON n.oid = t.typnamespace "
                        "WHERE n.nspname = 'public' AND t.typtype = 'e'"
                    )
                ).scalars()
            )
            assert enums >= TRAINING_TYPES
            recorded = connection.execute(
                text(f"SELECT vendor_commit, ddl_path, ddl_sha256 FROM {VERSION_TABLE}")
            ).one()
            assert tuple(recorded) == (
                instance.vendor_commit,
                instance.ddl_path,
                instance.ddl_sha256,
            )
    finally:
        install_engine.dispose()

    # S065 的 runtime 角色凭 default privileges 读写安装出的对象。
    runtime_engine = instance.engine(
        database=TRAINING_DATABASE,
        role=TRAINING_RUNTIME_ROLE,
        password=instance.runtime_password,
    )
    try:
        with runtime_engine.begin() as connection:
            for table in sorted(TRAINING_TABLES):
                connection.execute(text(f"SELECT count(*) FROM {table}"))
            connection.execute(
                text("INSERT INTO dataset (id, actions) VALUES ('runtime-probe', ARRAY['a'])")
            )
            connection.execute(
                text("INSERT INTO training_job (id, status) VALUES ('runtime-job', 'queued')")
            )
            assert (
                connection.execute(
                    text("SELECT count(*) FROM dataset WHERE id = 'runtime-probe'")
                ).scalar_one()
                == 1
            )
            assert (
                connection.execute(
                    text("SELECT count(*) FROM training_job WHERE id = 'runtime-job'")
                ).scalar_one()
                == 1
            )
    finally:
        runtime_engine.dispose()


def test_install_skips_second_startup_and_preserves_data(training_install: TrainingInstall) -> None:
    """重复启动不重跑非幂等 CREATE，也不清空已有数据或重复写版本。"""
    instance = training_install
    first = instance.run_install(database=REINSTALL_DATABASE)
    assert first.exit_code == 0, first.output

    install_engine = instance.engine(
        database=REINSTALL_DATABASE, role=INSTALL_ROLE, password=instance.install_password
    )
    try:
        with install_engine.begin() as connection:
            connection.execute(
                text("INSERT INTO dataset (id, actions) VALUES ('preserved', ARRAY['a'])")
            )
    finally:
        install_engine.dispose()

    result = instance.run_install(database=REINSTALL_DATABASE)
    assert result.exit_code == 0, result.output
    assert "跳过安装" in result.output.decode("utf-8")

    try:
        with install_engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT count(*) FROM dataset WHERE id = 'preserved'")
                ).scalar_one()
                == 1
            )
            assert (
                connection.execute(text(f"SELECT count(*) FROM {VERSION_TABLE}")).scalar_one() == 1
            )
    finally:
        install_engine.dispose()


def test_matching_marker_refuses_missing_vendor_owned_object(
    training_install: TrainingInstall,
) -> None:
    """安装标记不能替代必需 Vendor 对象的只读完整性证明。"""
    instance = training_install
    first = instance.run_install(database="training_drifted")
    assert first.exit_code == 0, first.output

    engine = instance.engine(
        database="training_drifted", role=INSTALL_ROLE, password=instance.install_password
    )
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP INDEX idx_augmentation_stages_stage_name"))
    finally:
        engine.dispose()

    result = instance.run_install(database="training_drifted")
    assert result.exit_code != 0
    assert "必需对象缺失或类型不符" in result.output.decode("utf-8")
    assert VERSION_TABLE in instance.public_tables(database="training_drifted")


def test_install_refuses_unknown_non_empty_state(training_install: TrainingInstall) -> None:
    """未知非空状态明确拒绝，不伪报安装完成，也不改动既有对象。"""
    instance = training_install
    unknown_engine = instance.engine(
        database="training_unknown", role=INSTALL_ROLE, password=instance.install_password
    )
    try:
        with unknown_engine.begin() as connection:
            connection.execute(text("CREATE TABLE legacy_annotation (id int)"))
    finally:
        unknown_engine.dispose()

    result = instance.run_install(database="training_unknown")
    assert result.exit_code != 0
    assert "拒绝" in result.output.decode("utf-8")
    assert "legacy_annotation" in instance.public_tables(database="training_unknown")
    assert VERSION_TABLE not in instance.public_tables(database="training_unknown")


def test_install_refuses_recorded_version_mismatch(training_install: TrainingInstall) -> None:
    """已记录其它版本时拒绝覆盖，保留已安装对象（增量升级由 S067 负责）。"""
    instance = training_install
    first = instance.run_install(database="training_mismatch")
    assert first.exit_code == 0, first.output

    mismatch_engine = instance.engine(
        database="training_mismatch", role=INSTALL_ROLE, password=instance.install_password
    )
    try:
        with mismatch_engine.begin() as connection:
            connection.execute(
                text(f"UPDATE {VERSION_TABLE} SET vendor_commit = 'different-vendor-commit'")
            )
    finally:
        mismatch_engine.dispose()

    result = instance.run_install(database="training_mismatch")
    assert result.exit_code != 0
    assert "拒绝" in result.output.decode("utf-8")
    assert instance.public_tables(database="training_mismatch") >= TRAINING_TABLES


def test_install_failure_leaves_no_partial_state(training_install: TrainingInstall) -> None:
    """DDL 中途失败时整事务回滚：不发布版本，也不留下半安装对象。"""
    instance = training_install
    bad_ddl = "CREATE TABLE partial_probe (id int);\nSELECT 1 / 0;\n"
    written = instance.client.exec(
        ["/bin/sh", "-c", f"printf '%s' '{bad_ddl}' > /tmp/bad-training-ddl.sql"]
    )
    assert written.exit_code == 0, written.output
    checksum = instance.client.exec(
        ["/bin/sh", "-c", "sha256sum /tmp/bad-training-ddl.sql | cut -d' ' -f1"]
    )
    assert checksum.exit_code == 0, checksum.output

    result = instance.run_install(
        database="training_broken",
        extra_env={
            "NVSOP_TRAINING_DDL_FILE": "/tmp/bad-training-ddl.sql",
            "NVSOP_TRAINING_DDL_SHA256": checksum.output.decode("utf-8").strip(),
        },
    )
    assert result.exit_code != 0
    broken_tables = instance.public_tables(database="training_broken")
    assert "partial_probe" not in broken_tables
    assert VERSION_TABLE not in broken_tables
