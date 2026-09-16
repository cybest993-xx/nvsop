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
from sqlalchemy.orm import Session

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
                source_object_version_id="version-1",
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
        "object_version_id",
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

    with database_at_0023.connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version == "0035"
    assert "dataset.dataset.edit" in _permission_codes(database_at_0023)

    command.downgrade(configuration, "0025")
    assert "dataset.dataset.edit" not in _permission_codes(database_at_0023)
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
