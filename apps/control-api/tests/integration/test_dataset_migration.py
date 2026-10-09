"""用真实 PostgreSQL 走过 0023 → 当前 head 的训练数据集用途迁移。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from factory_sop.dataset.adapters.repository import PostgresDatasetRepository
from factory_sop.dataset.model import (
    ArtifactStatus,
    DatasetArtifact,
    TrainingDataset,
    UsageCheck,
    UsageCheckStatus,
    UsageKind,
    VlmCandidate,
    VlmCandidateKind,
    VlmMediaReference,
)
from factory_sop.device.adapters.repository import (
    PostgresInferenceHostRepository,
    PostgresStationRepository,
)
from factory_sop.device.model import DeviceStatus, InferenceHost, Station
from factory_sop.execution.adapters.repository import PostgresHandoverRepository
from factory_sop.execution.model import HandoverConfirmation

CONTROL_API = Path(__file__).resolve().parents[2]


@pytest.fixture
def database_at_0023(engine: Engine) -> Iterator[Engine]:
    """在独立数据库中保留 0023 状态，再由测试执行升级。"""
    server = engine.url._replace(database="postgres")
    installer = create_engine(server, isolation_level="AUTOCOMMIT")
    database_name = f"nvsop_migration_{uuid4().hex[:12]}"
    with installer.connect() as connection:
        connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database_name}"')
        connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')

    upgraded = create_engine(engine.url._replace(database=database_name))
    configuration = Config(str(CONTROL_API / "alembic.ini"))
    configuration.set_main_option("script_location", str(CONTROL_API / "migrations"))
    configuration.set_main_option(
        "sqlalchemy.url", upgraded.url.render_as_string(hide_password=False)
    )
    command.upgrade(configuration, "0023")
    try:
        yield upgraded
    finally:
        upgraded.dispose()
        with installer.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database_name}"')
        installer.dispose()


@pytest.fixture
def database_at_0033(engine: Engine) -> Iterator[Engine]:
    """构造真实 0033 数据库，用于验证已下发配置的升级窗口。"""
    server = engine.url._replace(database="postgres")
    installer = create_engine(server, isolation_level="AUTOCOMMIT")
    database_name = f"nvsop_configuration_migration_{uuid4().hex[:12]}"
    with installer.connect() as connection:
        connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database_name}"')
        connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')

    upgraded = create_engine(engine.url._replace(database=database_name))
    configuration = Config(str(CONTROL_API / "alembic.ini"))
    configuration.set_main_option("script_location", str(CONTROL_API / "migrations"))
    configuration.set_main_option(
        "sqlalchemy.url", upgraded.url.render_as_string(hide_password=False)
    )
    command.upgrade(configuration, "0033")
    try:
        yield upgraded
    finally:
        upgraded.dispose()
        with installer.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database_name}"')
        installer.dispose()


def _tables(database: Engine) -> set[str]:
    with database.connect() as connection:
        return set(
            connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
            .scalars()
            .all()
        )


def _permission_codes(database: Engine) -> set[str]:
    with database.connect() as connection:
        return set(connection.execute(text("SELECT code FROM auth_permission")).scalars().all())


def _columns(database: Engine, table: str) -> set[str]:
    with database.connect() as connection:
        return set(
            connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :table"
                ),
                {"table": table},
            )
            .scalars()
            .all()
        )


def _nullable_columns(database: Engine, table: str) -> dict[str, bool]:
    with database.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :table"
            ),
            {"table": table},
        ).all()
    return {str(name): value == "YES" for name, value in rows}


def _handover_foreign_keys(database: Engine) -> dict[str, str]:
    """返回 `execution_handover` 每个外键的删除行为（'r'=RESTRICT, 'c'=CASCADE）。"""
    with database.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT conname, confdeltype FROM pg_constraint "
                "WHERE conrelid = 'execution_handover'::regclass AND contype = 'f'"
            )
        ).all()
    return {str(name): str(deltype) for name, deltype in rows}


def test_usage_records_round_trip_through_real_postgres(session: Session) -> None:
    now = datetime(2026, 9, 12, tzinfo=UTC)
    actor_id, dataset_id = uuid4(), uuid4()
    candidate_id, check_id, artifact_id = uuid4(), uuid4(), uuid4()
    repository = PostgresDatasetRepository(session)
    repository.add_dataset(
        TrainingDataset(
            id=dataset_id,
            name="用途检查持久化集",
            created_by=actor_id,
            updated_by=actor_id,
            created_at=now,
            updated_at=now,
        )
    )
    candidate = VlmCandidate(
        id=candidate_id,
        dataset_id=dataset_id,
        revision=1,
        kind=VlmCandidateKind.GQA,
        action_list_revision=1,
        records=({"conversations": []},),
        media=(
            VlmMediaReference(
                key="video.mp4",
                member_id=uuid4(),
                source_object_key="version-1",
                source_sha256="a" * 64,
                action_indices=(1, 2),
            ),
        ),
        created_by=actor_id,
        created_at=now,
    )
    repository.add_vlm_candidate(candidate)
    check = UsageCheck(
        id=check_id,
        dataset_id=dataset_id,
        kind=UsageKind.VLM,
        status=UsageCheckStatus.FAILED,
        input_digest="b" * 64,
        input_snapshot={"candidate_id": str(candidate_id)},
        summary={"record_count": 1},
        issues=({"code": "bad"},),
        base_commit="base-commit",
        contract_version="vlm-v1",
        candidate_id=candidate_id,
        job_id=None,
        created_by=actor_id,
        created_at=now,
        updated_at=now,
    )
    repository.add_usage_check(check)
    artifact = DatasetArtifact(
        id=artifact_id,
        dataset_id=dataset_id,
        usage_check_id=check_id,
        kind=UsageKind.DDM,
        status=ArtifactStatus.FAILED,
        input_digest="c" * 64,
        object_key=None,
        artifact_sha256=None,
        artifact_size=None,
        manifest={"artifact_format_version": 1},
        failure_code="ARTIFACT_GENERATION_FAILED",
        failure_detail="synthetic",
        job_id=None,
        created_by=actor_id,
        created_at=now,
        updated_at=now,
    )
    repository.add_artifact(artifact)
    session.flush()

    assert repository.vlm_candidate_by_id(candidate_id) == candidate
    assert repository.usage_check_by_id(check_id) == check
    assert repository.artifact_by_id(artifact_id) == artifact


def test_0034_backfills_only_the_0033_issued_configuration_fact(
    database_at_0033: Engine,
) -> None:
    host_id = uuid4()
    actor_id = uuid4()
    issued_digest = "d" * 64
    now = datetime(2026, 9, 16, tzinfo=UTC)
    with database_at_0033.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO device_inference_host (
                    id, name, address, mediamtx_address, mediamtx_playback_address,
                    recording_window_seconds, disk_watermark_percent, status, revision,
                    configuration_revision, configuration_sha256,
                    created_by, updated_by, created_at, updated_at
                ) VALUES (
                    :id, 'migration-host', '10.9.0.1', NULL, NULL,
                    604800, 85, 'active', 1,
                    7, :configuration_sha256,
                    :actor, :actor, :now, :now
                )
                """
            ),
            {
                "id": host_id,
                "actor": actor_id,
                "now": now,
                "configuration_sha256": issued_digest,
            },
        )

    configuration = Config(str(CONTROL_API / "alembic.ini"))
    configuration.set_main_option("script_location", str(CONTROL_API / "migrations"))
    configuration.set_main_option(
        "sqlalchemy.url", database_at_0033.url.render_as_string(hide_password=False)
    )
    command.upgrade(configuration, "0034")

    with database_at_0033.connect() as connection:
        issue = connection.execute(
            text(
                """
                SELECT configuration_revision, configuration_sha256
                  FROM device_configuration_issue
                 WHERE host_id = :host_id
                """
            ),
            {"host_id": host_id},
        ).one()
        assignment_count = connection.execute(
            text("SELECT count(*) FROM device_configuration_assignment WHERE host_id = :host_id"),
            {"host_id": host_id},
        ).scalar_one()
    assert issue == (7, issued_digest)
    assert assignment_count == 0

    command.downgrade(configuration, "0033")
    assert "device_configuration_issue" not in _tables(database_at_0033)
    assert "device_configuration_assignment" not in _tables(database_at_0033)


def test_0035_allows_v2_reports_without_a_single_backend_column(
    database_at_0033: Engine,
) -> None:
    configuration = Config(str(CONTROL_API / "alembic.ini"))
    configuration.set_main_option("script_location", str(CONTROL_API / "migrations"))
    configuration.set_main_option(
        "sqlalchemy.url", database_at_0033.url.render_as_string(hide_password=False)
    )
    command.upgrade(configuration, "head")
    with database_at_0033.connect() as connection:
        nullable = connection.execute(
            text(
                """
                SELECT is_nullable
                  FROM information_schema.columns
                 WHERE table_schema = 'public'
                   AND table_name = 'monitor_reported_decision'
                   AND column_name = 'backend_id'
                """
            )
        ).scalar_one()
    assert nullable == "YES"


def test_training_dataset_migration_upgrades_and_rolls_back_on_real_postgres(
    database_at_0023: Engine,
) -> None:
    assert "job_application_job" in _tables(database_at_0023)
    assert not {"dataset_training_dataset", "dataset_member", "dataset_upload_attempt"} & _tables(
        database_at_0023
    )

    configuration = Config(str(CONTROL_API / "alembic.ini"))
    configuration.set_main_option("script_location", str(CONTROL_API / "migrations"))
    configuration.set_main_option(
        "sqlalchemy.url", database_at_0023.url.render_as_string(hide_password=False)
    )
    command.upgrade(configuration, "head")

    assert {
        "dataset_training_dataset",
        "dataset_member",
        "dataset_upload_attempt",
        "dataset_action_list_revision",
        "dataset_annotation_context",
        "dataset_annotation_submission",
        "dataset_annotation_execution",
        "dataset_vlm_candidate",
        "dataset_usage_check",
        "dataset_artifact",
    } <= _tables(database_at_0023)
    assert {
        "id",
        "dataset_id",
        "original_filename",
        "declared_size",
        "declared_sha256",
        "current_attempt_id",
        "actual_size",
        "actual_sha256",
        "duration_seconds",
        "codec",
        "container",
        "object_key",
        "validation_job_id",
        "failure_code",
        "recovery_action",
    } <= _columns(database_at_0023, "dataset_member")
    assert {
        "id",
        "dataset_id",
        "member_id",
        "idempotency_key",
        "object_key",
        "declared_size",
        "declared_sha256",
        "expires_at",
        "status",
        "validation_job_id",
        "final_object_key",
    } <= _columns(database_at_0023, "dataset_upload_attempt")
    assert {
        "annotation_revision",
        "upstream_data_id",
        "upstream_video_id",
        "upstream_video_size",
        "upstream_video_sha256",
        "upstream_video_duration_seconds",
        "preparation_job_id",
        "preparation_status",
        "preparation_failure_code",
        "preparation_failure_detail",
    } <= _columns(database_at_0023, "dataset_annotation_context")
    assert {"revision"} <= _columns(database_at_0023, "dataset_annotation_submission")
    assert {
        "upstream_data_id",
        "upstream_video_id",
        "derived_video_size",
        "derived_video_sha256",
        "derived_video_duration_seconds",
    } <= _columns(database_at_0023, "dataset_annotation_execution")
    assert {
        "id",
        "dataset_id",
        "revision",
        "kind",
        "action_list_revision",
        "records",
        "media",
    } <= _columns(database_at_0023, "dataset_vlm_candidate")
    assert {
        "id",
        "dataset_id",
        "kind",
        "status",
        "input_digest",
        "input_snapshot",
        "summary",
        "issues",
        "base_commit",
        "contract_version",
        "candidate_id",
        "job_id",
    } <= _columns(database_at_0023, "dataset_usage_check")
    assert {
        "id",
        "dataset_id",
        "usage_check_id",
        "kind",
        "status",
        "input_digest",
        "object_key",
        "artifact_sha256",
        "artifact_size",
        "manifest",
        "job_id",
    } <= _columns(database_at_0023, "dataset_artifact")
    assert {"dataset_id"} <= _columns(database_at_0023, "job_application_job")
    assert {
        "mediamtx_playback_address",
        "configuration_revision",
        "configuration_sha256",
    } <= _columns(database_at_0023, "device_inference_host")
    assert {"media_path_mode", "recording_mode"} <= _columns(database_at_0023, "device_camera")
    assert {
        "device_configuration_assignment",
        "device_configuration_issue",
    } <= _tables(database_at_0023)
    assert {"monitor_sop_instance", "monitor_disposal"} <= _tables(database_at_0023)
    assert {"execution_handover"} <= _tables(database_at_0023)
    assert "execution.handover.edit" in _permission_codes(database_at_0023)
    assert {
        "open_boundary_signal",
        "close_boundary_signal",
    } <= _columns(database_at_0023, "monitor_sop_instance")

    with database_at_0023.connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version == "0053"
    assert {"latched_at", "realtime"} <= _columns(database_at_0023, "monitor_reported_decision")
    # 客户端不再必须预读整段视频计算摘要：声明列可为空，权威摘要由中心登记。
    assert _nullable_columns(database_at_0023, "dataset_member")["declared_sha256"] is True
    assert _nullable_columns(database_at_0023, "dataset_upload_attempt")["declared_sha256"] is True
    # 公开/业务契约不再携带 S3 对象代次语义：列名改为定稿文件身份。
    assert "object_version_id" not in _columns(database_at_0023, "dataset_member")
    assert "final_object_key" in _columns(database_at_0023, "dataset_upload_attempt")
    assert "source_object_key" in _columns(database_at_0023, "dataset_annotation_submission")
    assert "source_object_key" in _columns(database_at_0023, "dataset_annotation_context")
    assert "dataset.dataset.edit" in _permission_codes(database_at_0023)

    command.downgrade(configuration, "0025")
    assert "dataset.dataset.edit" not in _permission_codes(database_at_0023)
    assert _nullable_columns(database_at_0023, "dataset_member")["declared_sha256"] is False
    assert "object_version_id" in _columns(database_at_0023, "dataset_member")
    assert "final_object_key" not in _columns(database_at_0023, "dataset_upload_attempt")
    assert "source_object_version_id" in _columns(database_at_0023, "dataset_annotation_submission")
    assert "mediamtx_playback_address" not in _columns(database_at_0023, "device_inference_host")
    assert not {
        "configuration_revision",
        "configuration_sha256",
    } & _columns(database_at_0023, "device_inference_host")
    assert not {"media_path_mode", "recording_mode"} & _columns(database_at_0023, "device_camera")
    assert not {
        "device_configuration_assignment",
        "device_configuration_issue",
    } & _tables(database_at_0023)
    command.downgrade(configuration, "0024")
    assert not {
        "dataset_action_list_revision",
        "dataset_annotation_context",
        "dataset_annotation_submission",
        "dataset_annotation_execution",
    } & _tables(database_at_0023)
    command.downgrade(configuration, "0023")
    assert not {"dataset_training_dataset", "dataset_member", "dataset_upload_attempt"} & _tables(
        database_at_0023
    )
    command.downgrade(configuration, "0022")
    assert "job_application_job" not in _tables(database_at_0023)


def test_0050_handover_history_survives_device_deletion(database_at_0023: Engine) -> None:
    """0049 已有强制改绑记录升级 0050：保留记录、三个引用改 RESTRICT，且可降级再升级。"""
    configuration = Config(str(CONTROL_API / "alembic.ini"))
    configuration.set_main_option("script_location", str(CONTROL_API / "migrations"))
    configuration.set_main_option(
        "sqlalchemy.url", database_at_0023.url.render_as_string(hide_password=False)
    )
    command.upgrade(configuration, "0049")

    now = datetime(2026, 9, 22, 1, 0, tzinfo=UTC)
    actor, station_id, from_host_id, to_host_id, handover_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    session = sessionmaker(bind=database_at_0023)()
    try:
        PostgresStationRepository(session).add(
            Station(
                id=station_id,
                code=f"MIG-{station_id.hex[:8]}",
                name="迁移工位",
                tags=(),
                status=DeviceStatus.ACTIVE,
                revision=1,
                created_by=actor,
                updated_by=actor,
                created_at=now,
                updated_at=now,
            )
        )
        hosts = PostgresInferenceHostRepository(session)
        for host_id, name in ((from_host_id, "迁移旧机"), (to_host_id, "迁移新机")):
            hosts.add(
                InferenceHost(
                    id=host_id,
                    name=name,
                    address="10.0.8.11",
                    mediamtx_address=None,
                    recording_window_seconds=604800,
                    disk_watermark_percent=85,
                    status=DeviceStatus.ACTIVE,
                    revision=1,
                    created_by=actor,
                    updated_by=actor,
                    created_at=now,
                    updated_at=now,
                )
            )
        PostgresHandoverRepository(session).add(
            HandoverConfirmation(
                handover_id=handover_id,
                station_id=station_id,
                from_host_id=from_host_id,
                to_host_id=to_host_id,
                operator_id=actor,
                operator_confirmed_at=now,
                operator_risk_shown=True,
            )
        )
        session.commit()
    finally:
        session.close()

    command.upgrade(configuration, "0050")
    assert _handover_foreign_keys(database_at_0023) == {
        "fk_execution_handover_station_id_device_station": "r",
        "fk_execution_handover_from_host_id_device_inference_host": "r",
        "fk_execution_handover_to_host_id_device_inference_host": "r",
    }
    with database_at_0023.connect() as connection:
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == "0050"
        )
        assert connection.execute(text("SELECT count(*) FROM execution_handover")).scalar_one() == 1

    # RESTRICT 是真实约束：删除仍被历史引用的推理机被拒绝，记录保留。
    with pytest.raises(IntegrityError), database_at_0023.begin() as connection:
        connection.execute(
            text("DELETE FROM device_inference_host WHERE id = :id"), {"id": from_host_id}
        )
    with database_at_0023.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM execution_handover")).scalar_one() == 1

    # 降级按原 CASCADE 重建且不删历史；再升级回到 RESTRICT，记录仍在。
    command.downgrade(configuration, "0049")
    assert _handover_foreign_keys(database_at_0023) == {
        "fk_execution_handover_station_id_device_station": "c",
        "fk_execution_handover_from_host_id_device_inference_host": "c",
        "fk_execution_handover_to_host_id_device_inference_host": "c",
    }
    with database_at_0023.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM execution_handover")).scalar_one() == 1
    command.upgrade(configuration, "0050")
    assert all(behaviour == "r" for behaviour in _handover_foreign_keys(database_at_0023).values())
    with database_at_0023.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM execution_handover")).scalar_one() == 1


def test_file_identity_rename_keeps_registered_values_on_real_postgres(
    database_at_0023: Engine,
) -> None:
    """0039 只改列名：已登记的文件身份保留，回滚时从 object_key 回填。"""
    configuration = Config(str(CONTROL_API / "alembic.ini"))
    configuration.set_main_option("script_location", str(CONTROL_API / "migrations"))
    configuration.set_main_option(
        "sqlalchemy.url", database_at_0023.url.render_as_string(hide_password=False)
    )
    command.upgrade(configuration, "head")

    dataset_id, member_id, attempt_id, actor_id = uuid4(), uuid4(), uuid4(), uuid4()
    long_member_id = uuid4()
    object_key = "training-datasets/migration/member/attempt/registered-video"
    overlong_key = "training-datasets/" + "x" * 300
    with database_at_0023.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO dataset_training_dataset "
                "(id, name, created_by, updated_by, created_at, updated_at) "
                "VALUES (:id, 'migration-dataset', :actor, :actor, now(), now())"
            ),
            {"id": dataset_id, "actor": actor_id},
        )
        connection.execute(
            text(
                "INSERT INTO dataset_member "
                "(id, dataset_id, original_filename, source, declared_size, current_attempt_id, "
                "status, object_key, created_by, updated_by, created_at, updated_at) "
                "VALUES (:id, :dataset, 'line.mp4', 'camera', 100, :attempt, 'registered', "
                ":object_key, :actor, :actor, now(), now())"
            ),
            {
                "id": member_id,
                "dataset": dataset_id,
                "attempt": attempt_id,
                "object_key": object_key,
                "actor": actor_id,
            },
        )
        # object_key 比回滚重建的 object_version_id（255）更宽：超长行不能截断，也不能让回滚失败。
        connection.execute(
            text(
                "INSERT INTO dataset_member "
                "(id, dataset_id, original_filename, source, declared_size, current_attempt_id, "
                "status, object_key, created_by, updated_by, created_at, updated_at) "
                "VALUES (:id, :dataset, 'long.mp4', 'camera', 100, :attempt, 'registered', "
                ":object_key, :actor, :actor, now(), now())"
            ),
            {
                "id": long_member_id,
                "dataset": dataset_id,
                "attempt": attempt_id,
                "object_key": overlong_key,
                "actor": actor_id,
            },
        )

    command.downgrade(configuration, "0038")
    with database_at_0023.connect() as connection:
        restored = connection.execute(
            text("SELECT object_version_id FROM dataset_member WHERE id = :id"),
            {"id": member_id},
        ).scalar_one()
        overlong = connection.execute(
            text("SELECT object_version_id FROM dataset_member WHERE id = :id"),
            {"id": long_member_id},
        ).scalar_one()
    assert restored == object_key
    assert overlong is None


def test_declared_sha256_rollback_reconciles_rows_without_a_declaration(
    database_at_0023: Engine,
) -> None:
    """0038 回滚恢复 NOT NULL 前必须补齐：否则真实数据会让降级失败。"""
    configuration = Config(str(CONTROL_API / "alembic.ini"))
    configuration.set_main_option("script_location", str(CONTROL_API / "migrations"))
    configuration.set_main_option(
        "sqlalchemy.url", database_at_0023.url.render_as_string(hide_password=False)
    )
    command.upgrade(configuration, "head")

    dataset_id, actor_id = uuid4(), uuid4()
    validated_id, pending_id = uuid4(), uuid4()
    attempt_id = uuid4()
    actual_digest = "a" * 64
    with database_at_0023.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO dataset_training_dataset "
                "(id, name, created_by, updated_by, created_at, updated_at) "
                "VALUES (:id, 'rollback-dataset', :actor, :actor, now(), now())"
            ),
            {"id": dataset_id, "actor": actor_id},
        )
        for member_id, digest in ((validated_id, actual_digest), (pending_id, None)):
            connection.execute(
                text(
                    "INSERT INTO dataset_member "
                    "(id, dataset_id, original_filename, source, declared_size, "
                    "current_attempt_id, status, declared_sha256, actual_sha256, "
                    "created_by, updated_by, created_at, updated_at) "
                    "VALUES (:id, :dataset, 'line.mp4', 'camera', 100, :attempt, "
                    "'pending_upload', NULL, :digest, :actor, :actor, now(), now())"
                ),
                {
                    "id": member_id,
                    "dataset": dataset_id,
                    "attempt": attempt_id,
                    "digest": digest,
                    "actor": actor_id,
                },
            )
        connection.execute(
            text(
                "INSERT INTO dataset_upload_attempt "
                "(id, dataset_id, member_id, idempotency_key, object_key, declared_size, "
                "declared_sha256, expires_at, status, created_at) "
                "VALUES (:id, :dataset, :member, 'idem-1', :object_key, 100, NULL, "
                "now() + interval '1 hour', 'pending_upload', now())"
            ),
            {
                "id": attempt_id,
                "dataset": dataset_id,
                "member": validated_id,
                "object_key": "training-datasets/rollback/member/attempt/video",
            },
        )

    command.downgrade(configuration, "0037")
    with database_at_0023.connect() as connection:
        restored = connection.execute(
            text("SELECT id, declared_sha256 FROM dataset_member WHERE dataset_id = :dataset"),
            {"dataset": dataset_id},
        ).all()
        attempt_digest = connection.execute(
            text("SELECT declared_sha256 FROM dataset_upload_attempt WHERE id = :id"),
            {"id": attempt_id},
        ).scalar_one()

    assert {row.id: row.declared_sha256 for row in restored} == {
        validated_id: actual_digest,
        pending_id: "0" * 64,
    }
    assert attempt_digest == actual_digest
