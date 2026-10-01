"""S067：受支持的历史非空标注数据增量升级到 `training`。

在真实 PostgreSQL 上把 Vendor 原始 standalone 标注结构 + 合成历史数据（含一个 provenance 明确、
非 Vendor 拥有的旧用户关联附加表）作为受支持来源，按文档化工作流用 `pg_dump`/`pg_restore` 恢复到
空 `training` database，再执行 S065 角色初始化与 Compose 渲染的 `training-objects-install` 入口。
验证：原视频身份/片段/用户关联/数据保留完整，五类训练服务对象可读写；重复启动跳过且 OID/版本行/
数据不变；DDL 失败整事务回滚不留版本；未知/部分/结构不符来源明确拒绝；Center Alembic 升级与回滚
不修改 `training` 对象。

五类 Vendor 服务使用 asyncpg 异步引擎（不在本仓库冻结环境内）；本测试直接加载 Vendor 的真实 ORM
模型类，用同步 psycopg 引擎按真实列集读写，是与原始适配器等价的 SQL 路径证据，不是完整异步服务
进程证据（无 GPU）。
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session
from test_training_objects_install import (
    INSTALL_ROLE,
    REPO_ROOT,
    TRAINING_RUNTIME_ROLE,
    TRAINING_TABLES,
    TRAINING_TYPES,
    VERSION_TABLE,
    TrainingInstall,
    build_training_install,
)
from testcontainers.core.container import BytesExecResult

CONTROL_API = REPO_ROOT / "apps/control-api"
VENDOR_TRAINING = (
    REPO_ROOT / "vendor/sop-monitoring-blueprints/microservices/sop-training-bp/microservices"
)
STANDALONE_DDL = (
    VENDOR_TRAINING / "video-annotator-ms/annotation_backend/db-init-scripts/01-init-tables.sql"
)
SERVICE_MODELS = {
    "annotation": (
        VENDOR_TRAINING / "video-annotator-ms/annotation_backend/validations/db_models.py"
    ),
    "data-generation": (
        VENDOR_TRAINING / "data-generation-pipeline/validation/postgres_validation.py"
    ),
    "cr-training": VENDOR_TRAINING / "cr-training-ms/validation/postgres_validation.py",
    "ddm-training": VENDOR_TRAINING / "ddm-training-ms/validation/postgres_validation.py",
    "evaluation": VENDOR_TRAINING / "evaluation-ms/validation/postgres_validation.py",
}
LEGACY_DATABASE = "legacy_annotation"
CENTER_DATABASE = "nvsop"
# 合成旧用户关联附加表：Vendor 四表没有用户字段，用这张非 Vendor 拥有的表表达历史用户关联。
# provenance 明确为 S067 fixture，不是生产 user 模型；升级不得改动或删除它。
USER_TABLE = "historic_annotation_user"
SOURCE_TABLES = ("dataset", "video", "chunk", "annotation")
ALL_TABLES = SOURCE_TABLES + tuple(sorted(TRAINING_TABLES))


@pytest.fixture(scope="module")
def training_install(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TrainingInstall]:
    yield from build_training_install(tmp_path_factory)


SNAPSHOT_SQL = {
    "dataset": "SELECT id, actions, created_at, updated_at FROM dataset ORDER BY id",
    "video": (
        "SELECT id, dataset_id, name, mime_type, file_size, created_at, updated_at "
        "FROM video ORDER BY id"
    ),
    "chunk": (
        "SELECT id, video_id, name, action, mime_type, file_size, created_at, updated_at "
        "FROM chunk ORDER BY id"
    ),
    "annotation": (
        "SELECT id, video_id, chunk_id, start_time, end_time, action_index, "
        "action_description, created_at, updated_at FROM annotation ORDER BY id"
    ),
    USER_TABLE: (
        "SELECT video_id, user_name, assigned_at FROM historic_annotation_user "
        "ORDER BY video_id, user_name"
    ),
}
SYNTHETIC_ROWS = """
INSERT INTO dataset (id, actions, created_at, updated_at) VALUES
  ('ds-historic-1', ARRAY['pick','place'],
   TIMESTAMP '2024-01-02 03:04:05', TIMESTAMP '2024-01-02 03:04:05');
INSERT INTO video (id, dataset_id, name, mime_type, file_size, created_at, updated_at) VALUES
  ('vid-historic-1', 'ds-historic-1', 'historic-1.mp4', 'video/mp4', 111,
   TIMESTAMP '2024-01-02 03:04:05', TIMESTAMP '2024-01-02 03:04:05'),
  ('vid-historic-2', 'ds-historic-1', 'historic-2.mp4', 'video/mp4', 222,
   TIMESTAMP '2024-01-02 03:04:05', TIMESTAMP '2024-01-02 03:04:05');
INSERT INTO chunk (id, video_id, name, action, mime_type, file_size, created_at, updated_at)
VALUES
  ('chk-historic-1', 'vid-historic-1', 'chunk-1', 'pick', 'video/mp4', 11,
   TIMESTAMP '2024-01-02 03:04:05', TIMESTAMP '2024-01-02 03:04:05'),
  ('chk-historic-2', 'vid-historic-2', 'chunk-2', 'place', 'video/mp4', 22,
   TIMESTAMP '2024-01-02 03:04:05', TIMESTAMP '2024-01-02 03:04:05');
INSERT INTO annotation
  (id, video_id, chunk_id, start_time, end_time, action_index, action_description,
   created_at, updated_at)
VALUES
  ('ann-historic-1', 'vid-historic-1', 'chk-historic-1', 0.0, 1.5, 0, 'pick',
   TIMESTAMP '2024-01-02 03:04:05', TIMESTAMP '2024-01-02 03:04:05'),
  ('ann-historic-2', 'vid-historic-2', 'chk-historic-2', 1.5, 3.0, 1, 'place',
   TIMESTAMP '2024-01-02 03:04:05', TIMESTAMP '2024-01-02 03:04:05');
CREATE TABLE historic_annotation_user (
    video_id varchar NOT NULL REFERENCES video(id) ON DELETE CASCADE,
    user_name text NOT NULL,
    assigned_at timestamptz NOT NULL DEFAULT TIMESTAMPTZ '2024-01-02 03:04:05+00'
);
INSERT INTO historic_annotation_user (video_id, user_name) VALUES
  ('vid-historic-1', 'legacy-user-a'), ('vid-historic-2', 'legacy-user-b');
"""


def _shell(instance: TrainingInstall, script: str) -> BytesExecResult:
    return instance.client.exec(["/bin/sh", "-c", script])


def _run_sql(instance: TrainingInstall, database: str, sql: str) -> BytesExecResult:
    return _shell(
        instance,
        'export PGPASSWORD="$(cat /run/secrets/center-db-password)"\n'
        f"psql -v ON_ERROR_STOP=1 -d {database} <<'NVSOP_SQL'\n{sql}\nNVSOP_SQL\n",
    )


def _run_role_init(instance: TrainingInstall, database: str) -> BytesExecResult:
    return _shell(
        instance,
        'export PGPASSWORD="$(cat /run/secrets/center-db-password)"\n'
        'export RUNTIME_PASSWORD="$(cat /run/secrets/training-runtime-password)"\n'
        f"psql -v ON_ERROR_STOP=1 -v database={database} -v runtime_role={TRAINING_RUNTIME_ROLE} "
        f"-d {database} -f /opt/nvsop/db-roles.sql\n",
    )


def _create_database(instance: TrainingInstall, name: str) -> None:
    engine = create_engine(
        instance.base_url.set(
            database="postgres", username=INSTALL_ROLE, password=instance.install_password
        ),
        isolation_level="AUTOCOMMIT",
    )
    try:
        with engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        engine.dispose()


def _admin_engine(instance: TrainingInstall, database: str) -> Engine:
    return instance.engine(database=database, role=INSTALL_ROLE, password=instance.install_password)


def _rows(engine: Engine, query: str) -> list[tuple[object, ...]]:
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(text(query)).all()]


def _snapshot(engine: Engine) -> dict[str, list[tuple[object, ...]]]:
    return {table: _rows(engine, query) for table, query in SNAPSHOT_SQL.items()}


def _table_oids(engine: Engine, tables: tuple[str, ...]) -> dict[str, int]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT relname, oid FROM pg_class "
                "WHERE relnamespace = 'public'::regnamespace AND relname = ANY(:names)"
            ),
            {"names": list(tables)},
        ).all()
    return {str(row[0]): int(row[1]) for row in rows}


def _seed_legacy_source(instance: TrainingInstall, database: str) -> None:
    """在 `database` 里创建 Vendor 原始 standalone 标注结构与合成历史数据。"""
    _create_database(instance, database)
    sql = STANDALONE_DDL.read_text(encoding="utf-8") + SYNTHETIC_ROWS
    result = _run_sql(instance, database, sql)
    assert result.exit_code == 0, result.output


def _restore_backup(instance: TrainingInstall, source: str, target: str) -> None:
    """按文档化工作流 `pg_dump`/`pg_restore` 把来源备份恢复到空目标，原库保留。"""
    result = _shell(
        instance,
        'export PGPASSWORD="$(cat /run/secrets/center-db-password)"\n'
        f"pg_dump -Fc -d {source} > /tmp/nvsop-s067.dump\n"
        f"pg_restore -d {target} /tmp/nvsop-s067.dump\n",
    )
    assert result.exit_code == 0, result.output


def _upgrade(instance: TrainingInstall, database: str) -> BytesExecResult:
    """S065 角色初始化先于安装入口执行，与文档化的 restore→role-init→installer 顺序一致。"""
    role_init = _run_role_init(instance, database)
    assert role_init.exit_code == 0, role_init.output
    return instance.run_install(database=database)


def _load_models(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _exercise_five_services(engine: Engine) -> None:
    """按各服务真实 ORM 模型的列集做一次写入 + 读取（与原始适配器等价的 SQL 路径）。"""
    annotation = _load_models(SERVICE_MODELS["annotation"], "s067_annotation_models")
    generation = _load_models(SERVICE_MODELS["data-generation"], "s067_generation_models")
    cr = _load_models(SERVICE_MODELS["cr-training"], "s067_cr_models")
    ddm = _load_models(SERVICE_MODELS["ddm-training"], "s067_ddm_models")
    evaluation = _load_models(SERVICE_MODELS["evaluation"], "s067_evaluation_models")
    gen_status = generation.StatusEnum
    cr_status = cr.TrainingStatusEnum
    ddm_status = ddm.TrainingStatusEnum
    eval_status = evaluation.TrainingStatusEnum

    # Vendor 模型只有 ForeignKey 没有 relationship，unit-of-work 不保证插入顺序，按依赖 flush。
    with Session(engine) as session:
        session.add(annotation.Dataset(id="d1", actions=["pick"], two_operator_mode=True))
        session.flush()
        session.add(annotation.Video(id="v1", dataset_id="d1"))
        session.flush()
        session.add(annotation.Chunk(id="c1", video_id="v1"))
        session.flush()
        session.add(annotation.Annotation(id="a1", video_id="v1", chunk_id="c1"))
        session.flush()
        session.add(generation.Augmentation(id="g1", dataset_id="d1", status=gen_status.running))
        session.flush()
        session.add(generation.AugmentationStage(id="s1", augmentation_id="g1", stage_name="vlm"))
        session.flush()
        session.add(cr.TrainingJob(id="t1", aug_dataset_id="g1", status=cr_status.queued))
        session.flush()
        session.add(
            ddm.TrainingJob(id="t2", aug_dataset_id="g1", status=ddm_status.running, process_pid=7)
        )
        session.flush()
        session.add(
            evaluation.EvaluationJob(id="e1", training_job_id="t1", status=eval_status.running)
        )
        session.flush()
        session.add(
            evaluation.E2eEvaluationJob(
                id="e2", training_job_id="t1", ddm_training_job_id="t2", status=eval_status.running
            )
        )
        session.commit()

    # 按真实列集读回，证明升级后的表结构对该服务可用。
    with Session(engine) as session:
        dataset = session.get(annotation.Dataset, "d1")
        assert dataset is not None
        assert dataset.two_operator_mode is True
        assert session.get(annotation.Annotation, "a1") is not None
        augmentation = session.get(generation.Augmentation, "g1")
        assert augmentation is not None
        assert augmentation.status is gen_status.running
        assert session.get(generation.AugmentationStage, "s1") is not None
        job = session.get(cr.TrainingJob, "t1")
        assert job is not None
        assert job.status is cr_status.queued
        ddm_job = session.get(ddm.TrainingJob, "t2")
        assert ddm_job is not None
        assert ddm_job.process_pid == 7
        assert session.get(evaluation.EvaluationJob, "e1") is not None
        assert session.get(evaluation.E2eEvaluationJob, "e2") is not None


def test_backup_restore_upgrade_preserves_historic_data_and_enables_five_services(
    training_install: TrainingInstall,
) -> None:
    """AC1：备份恢复到目标后升级，身份/内容/引用/计数与附加用户表保留，五类对象可用。"""
    instance = training_install
    _seed_legacy_source(instance, LEGACY_DATABASE)
    legacy_engine = _admin_engine(instance, LEGACY_DATABASE)
    legacy_before = _snapshot(legacy_engine)

    _restore_backup(instance, LEGACY_DATABASE, "training")
    target = _admin_engine(instance, "training")
    restored = _snapshot(target)
    assert restored == legacy_before
    before_oids = _table_oids(target, SOURCE_TABLES)

    result = _upgrade(instance, "training")
    assert result.exit_code == 0, result.output
    assert "增量升级完成" in result.output.decode("utf-8")

    # 原库在备份/升级后保持不变。
    assert _snapshot(legacy_engine) == legacy_before
    # 目标身份/内容/引用/计数不变；标注表 OID 不变（未被重建）。
    assert _snapshot(target) == restored
    assert _table_oids(target, SOURCE_TABLES) == before_oids
    with target.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM annotation a JOIN video v ON v.id = a.video_id "
                    "JOIN chunk c ON c.id = a.chunk_id JOIN dataset d ON d.id = v.dataset_id"
                )
            ).scalar_one()
            == 2
        )
        assert (
            connection.execute(text(f"SELECT source_version FROM {VERSION_TABLE}")).scalar_one()
            == "annotation-standalone"
        )
        tables = set(
            connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )
        assert tables >= TRAINING_TABLES
        assert USER_TABLE in tables
        enums = set(
            connection.execute(
                text(
                    "SELECT typname FROM pg_type t JOIN pg_namespace n "
                    "ON n.oid = t.typnamespace WHERE n.nspname = 'public' AND t.typtype = 'e'"
                )
            ).scalars()
        )
        assert enums >= TRAINING_TYPES
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'dataset' "
                    "AND column_name = 'two_operator_mode'"
                )
            ).scalar_one()
            == 1
        )

    # S065 runtime 角色经安装身份默认权限读写升级后的全部对象。
    runtime = instance.engine(
        database="training", role=TRAINING_RUNTIME_ROLE, password=instance.runtime_password
    )
    _exercise_five_services(runtime)


def test_repeat_startup_is_idempotent_and_preserves_oids_version_and_data(
    training_install: TrainingInstall,
) -> None:
    """AC2：重复启动不重跑 DDL，OID、版本行与数据均不变。"""
    instance = training_install
    _seed_legacy_source(instance, "training_idempotent")
    first = _upgrade(instance, "training_idempotent")
    assert first.exit_code == 0, first.output

    engine = _admin_engine(instance, "training_idempotent")
    before = _snapshot(engine)
    before_oids = _table_oids(engine, ALL_TABLES)
    with engine.connect() as connection:
        before_version = connection.execute(
            text(f"SELECT vendor_commit, ddl_sha256, source_version FROM {VERSION_TABLE}")
        ).all()

    second = instance.run_install(database="training_idempotent")
    assert second.exit_code == 0, second.output
    assert "跳过安装" in second.output.decode("utf-8")

    assert _snapshot(engine) == before
    assert _table_oids(engine, ALL_TABLES) == before_oids
    with engine.connect() as connection:
        assert (
            connection.execute(
                text(f"SELECT vendor_commit, ddl_sha256, source_version FROM {VERSION_TABLE}")
            ).all()
            == before_version
        )


def test_upgrade_failure_rolls_back_without_version_or_partial_objects(
    training_install: TrainingInstall,
) -> None:
    """AC2：DDL 失败整事务回滚，不写成功版本，也不留半升级对象。"""
    instance = training_install
    _seed_legacy_source(instance, "training_upgrade_broken")
    role_init = _run_role_init(instance, "training_upgrade_broken")
    assert role_init.exit_code == 0, role_init.output
    engine = _admin_engine(instance, "training_upgrade_broken")
    before = _snapshot(engine)

    bad_ddl = "CREATE TABLE augmented_data (id int);\nSELECT 1 / 0;\n"
    written = _shell(instance, f"printf '%s' '{bad_ddl}' > /tmp/nvsop-bad-upgrade.sql")
    assert written.exit_code == 0, written.output
    checksum = _shell(instance, "sha256sum /tmp/nvsop-bad-upgrade.sql | cut -d' ' -f1")
    assert checksum.exit_code == 0, checksum.output

    result = instance.run_install(
        database="training_upgrade_broken",
        extra_env={
            "NVSOP_TRAINING_DDL_FILE": "/tmp/nvsop-bad-upgrade.sql",
            "NVSOP_TRAINING_DDL_SHA256": checksum.output.decode("utf-8").strip(),
        },
    )
    assert result.exit_code != 0
    with engine.connect() as connection:
        tables = set(
            connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )
        assert VERSION_TABLE not in tables
        assert "augmented_data" not in tables
    assert _snapshot(engine) == before


def test_unknown_partial_and_incompatible_sources_are_rejected(
    training_install: TrainingInstall,
) -> None:
    """AC2：未知来源、部分训练对象、结构不符的标注来源都明确拒绝且不写版本。"""
    instance = training_install
    _create_database(instance, "training_upgrade_unknown")
    unknown = _run_sql(instance, "training_upgrade_unknown", "CREATE TABLE random_legacy (id int);")
    assert unknown.exit_code == 0, unknown.output

    _seed_legacy_source(instance, "training_upgrade_partial")
    partial = _run_sql(
        instance, "training_upgrade_partial", "CREATE TABLE training_job (id varchar primary key);"
    )
    assert partial.exit_code == 0, partial.output

    _seed_legacy_source(instance, "training_upgrade_incompatible")
    incompatible = _run_sql(
        instance,
        "training_upgrade_incompatible",
        "ALTER TABLE video ADD COLUMN legacy_extra int;",
    )
    assert incompatible.exit_code == 0, incompatible.output

    for database in (
        "training_upgrade_unknown",
        "training_upgrade_partial",
        "training_upgrade_incompatible",
    ):
        result = instance.run_install(database=database)
        assert result.exit_code != 0, database
        assert "拒绝" in result.output.decode("utf-8"), database
        engine = _admin_engine(instance, database)
        try:
            with engine.connect() as connection:
                assert VERSION_TABLE not in set(
                    connection.execute(
                        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                    ).scalars()
                ), database
        finally:
            engine.dispose()


def test_center_migration_and_rollback_leave_training_objects_unchanged(
    training_install: TrainingInstall,
) -> None:
    """AC3：Center Alembic upgrade/downgrade 只落 `nvsop`，`training` 对象/OID/数据不变。"""
    instance = training_install
    _seed_legacy_source(instance, "training_center_isolated")
    result = _upgrade(instance, "training_center_isolated")
    assert result.exit_code == 0, result.output
    _create_database(instance, CENTER_DATABASE)

    training_engine = _admin_engine(instance, "training_center_isolated")
    before = _snapshot(training_engine)
    before_oids = _table_oids(training_engine, ALL_TABLES)

    configuration = Config(str(CONTROL_API / "alembic.ini"))
    configuration.set_main_option("script_location", str(CONTROL_API / "migrations"))
    configuration.set_main_option(
        "sqlalchemy.url",
        instance.base_url.set(database=CENTER_DATABASE).render_as_string(hide_password=False),
    )
    command.upgrade(configuration, "head")
    assert _snapshot(training_engine) == before
    assert _table_oids(training_engine, ALL_TABLES) == before_oids

    command.downgrade(configuration, "base")
    assert _snapshot(training_engine) == before
    assert _table_oids(training_engine, ALL_TABLES) == before_oids
