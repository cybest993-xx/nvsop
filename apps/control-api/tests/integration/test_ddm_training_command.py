"""DDM 训练命令的真实 PostgreSQL outbox 提交与 held 派发集成证据。

命令与 outbox 事实用同一个真实 Session 写入；用途检查/制品用真实 PostgreSQL 仓储与
真实用例封存为确定性的合成输入。真正的 DDM start 不在 S072：dispatcher 必须显式识别
held 类型，真实 Redis 不产生任务，outbox 行保持 pending。
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import Callable, Generator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO, cast
from uuid import UUID, uuid4

import pytest
from _integration_support import RedisServer, cleanup_dataset, row, settings_for
from fastapi import Request
from redis import Redis
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.auth.authorization import AuthorizationRefusedError, Caller
from factory_sop.auth.model import User, UserStatus
from factory_sop.auth.permissions import Permission
from factory_sop.dataset.adapters.repository import PostgresDatasetRepository
from factory_sop.dataset.annotation import AnnotationDataVolume
from factory_sop.dataset.errors import DatasetRefusalCode, DatasetRefusedError
from factory_sop.dataset.media import MediaMetadata, MediaProbe
from factory_sop.dataset.model import (
    ActionListRevision,
    AnnotationContext,
    AnnotationExecution,
    AnnotationExecutionStatus,
    AnnotationMode,
    AnnotationPreparationStatus,
    AnnotationSegment,
    AnnotationSubmission,
    ArtifactStatus,
    DatasetMember,
    MemberStatus,
    ObjectStat,
    TrainingDataset,
    UsageKind,
)
from factory_sop.dataset.storage import ObjectStorage
from factory_sop.dataset.usecases.ddm_command import (
    DdmTrainingCommandResult,
    request_ddm_training_command,
)
from factory_sop.dataset.usecases.usage import (
    DDM_CONSUMER_PARAMETERS,
    DdmInputSelection,
    begin_artifact_generation,
    begin_usage_check,
    complete_artifact,
    complete_usage_check,
    request_artifact,
    request_usage_check,
    run_usage_check,
)
from factory_sop.job.adapters.dispatcher import ArqJobDispatcher
from factory_sop.job.adapters.repository import PostgresJobRepository, PostgresUsageJobQueue
from factory_sop.job.adapters.worker import dispatch_pending_jobs
from factory_sop.job.api import ApplicationJob, JobStatus, JobType
from factory_sop.persistence import request_session, session_factory

NOW = datetime(2026, 10, 13, 1, 0, tzinfo=UTC)
ACTOR_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f401")
_OBJECT_KEY = "datasets/source-a.mp4"
_SOURCE_BYTES = b"a" * 100
_SOURCE_SHA256 = hashlib.sha256(_SOURCE_BYTES).hexdigest()
_EXECUTION_CLIPS = (
    {
        "id": "clip-1",
        "filename": "01_line-a_1_1.mp4",
        "start_time": 0.0,
        "end_time": 5.0,
        "action_index": 0,
        "action_description": "(1) 取料",
        "is_concurrent": False,
    },
    {
        "id": "clip-2",
        "filename": "02_line-a_1_2.mp4",
        "start_time": 5.0,
        "end_time": 10.0,
        "action_index": 1,
        "action_description": "(2) 安装",
        "is_concurrent": False,
    },
)


def _caller(*permissions: Permission) -> Caller:
    return Caller(
        user=User(
            id=ACTOR_ID,
            login_name="ddm-integration",
            display_name="DDM 集成测试",
            password_hash="integration-only",  # pragma: allowlist secret
            status=UserStatus.ACTIVE,
        ),
        granted=frozenset(permissions),
    )


def _noop_dispatch(_job_id: UUID) -> None:
    return None


class _FakeStorage:
    """只提供冻结视频的确定性读取；是外部对象存储的测试替身。"""

    def writing(self, *, object_key: str) -> AbstractContextManager[BinaryIO]:
        raise AssertionError(f"seed only reads objects: {object_key}")

    def stat(self, *, object_key: str) -> ObjectStat:
        assert object_key == _OBJECT_KEY
        return ObjectStat(size=len(_SOURCE_BYTES))

    def download_to(self, *, object_key: str, destination: BinaryIO) -> None:
        assert object_key == _OBJECT_KEY
        destination.write(_SOURCE_BYTES)

    def delete(self, *, object_key: str) -> None:
        del object_key


class _FakeMediaProbe:
    def probe(self, path: str) -> MediaMetadata:
        assert path.endswith(".mp4")
        return MediaMetadata(
            duration_seconds=10.0, codec="h264", container="mp4", fps=10.0, frame_count=100
        )


class _FakeAnnotationVolume:
    def read_annotation(self, *, data_id: str, video_id: str) -> bytes:
        assert (data_id, video_id) == ("data-1", "video-1")
        return (
            "["
            '{"action": 1, "description": "(1) 取料", "start_timestamp": 0.0,'
            ' "end_timestamp": 5.0},'
            '{"action": 2, "description": "(2) 安装", "start_timestamp": 5.0,'
            ' "end_timestamp": 10.0}'
            "]"
        ).encode()

    def read_video(
        self,
        *,
        data_id: str,
        video_id: str,
        filename: str | None,
        destination: BinaryIO,
    ) -> None:
        assert (data_id, video_id) == ("data-1", "video-1")
        assert filename in {None, "01_line-a_1_1.mp4"}
        destination.write(_SOURCE_BYTES)


class _RecordingDdmReader:
    def sample_counts(
        self,
        *,
        workspace: Path,
        annotation_filename: str,
        parameters: Mapping[str, object],
    ) -> Mapping[str, int]:
        del workspace, annotation_filename
        assert parameters == dict(DDM_CONSUMER_PARAMETERS)
        return {"boundary_sample_count": 1, "non_boundary_sample_count": 1}


def _seed_ddm_rows(
    session: DatabaseSession,
    *,
    label: str,
    dataset_id: UUID,
    member_id: UUID,
    context_id: UUID,
    submission_id: UUID,
    execution_id: UUID,
    now: datetime,
) -> None:
    """通过 PostgreSQL 仓储写入一套确定性的已登记视频与成功标注执行。"""
    datasets = PostgresDatasetRepository(session)
    datasets.add_dataset(
        TrainingDataset(
            id=dataset_id,
            name=f"ddm-{label}",
            created_by=ACTOR_ID,
            updated_by=ACTOR_ID,
            created_at=now,
            updated_at=now,
        )
    )
    datasets.add_member(
        DatasetMember(
            id=member_id,
            dataset_id=dataset_id,
            original_filename=f"{label}.mp4",
            source="integration",
            declared_size=len(_SOURCE_BYTES),
            declared_sha256=_SOURCE_SHA256,
            current_attempt_id=uuid4(),
            status=MemberStatus.REGISTERED,
            actual_size=len(_SOURCE_BYTES),
            actual_sha256=_SOURCE_SHA256,
            duration_seconds=10.0,
            codec="h264",
            container="mp4",
            object_key=_OBJECT_KEY,
            validation_job_id=None,
            failure_code=None,
            failure_detail=None,
            recovery_action=None,
            created_by=ACTOR_ID,
            updated_by=ACTOR_ID,
            created_at=now,
            updated_at=now,
        )
    )
    assert datasets.add_action_list(
        ActionListRevision(
            dataset_id=dataset_id,
            revision=1,
            actions=("(1) 取料", "(2) 安装"),
            created_by=ACTOR_ID,
            created_at=now,
        )
    )
    datasets.add_annotation_context(
        AnnotationContext(
            id=context_id,
            dataset_id=dataset_id,
            member_id=member_id,
            action_list_revision=1,
            annotation_revision=1,
            source_object_key=_OBJECT_KEY,
            source_sha256=_SOURCE_SHA256,
            created_by=ACTOR_ID,
            created_at=now,
            expires_at=now + timedelta(hours=1),
            upstream_data_id="data-1",
            upstream_video_id="video-1",
            upstream_video_size=len(_SOURCE_BYTES),
            upstream_video_sha256=_SOURCE_SHA256,
            upstream_video_duration_seconds=10.0,
            preparation_status=AnnotationPreparationStatus.SUCCEEDED.value,
        )
    )
    datasets.add_annotation_submission(
        AnnotationSubmission(
            id=submission_id,
            dataset_id=dataset_id,
            member_id=member_id,
            context_id=context_id,
            revision=1,
            action_list_revision=1,
            source_object_key=_OBJECT_KEY,
            source_sha256=_SOURCE_SHA256,
            idempotency_key=f"annotation-{label}",
            request_digest="a" * 64,
            mode=AnnotationMode.SINGLE_OPERATOR,
            segments=(
                AnnotationSegment(
                    start=0.0, end=5.0, action_index=0, action_description="(1) 取料"
                ),
                AnnotationSegment(
                    start=5.0, end=10.0, action_index=1, action_description="(2) 安装"
                ),
            ),
            raw_segments=(),
            created_by=ACTOR_ID,
            created_at=now,
        )
    )
    datasets.add_annotation_execution(
        AnnotationExecution(
            id=execution_id,
            submission_id=submission_id,
            generation=1,
            job_id=None,
            status=AnnotationExecutionStatus.SUCCEEDED,
            clips=_EXECUTION_CLIPS,
            failure_code=None,
            failure_detail=None,
            created_at=now,
            updated_at=now,
            upstream_data_id="data-1",
            upstream_video_id="video-1",
            derived_video_size=len(_SOURCE_BYTES),
            derived_video_sha256=_SOURCE_SHA256,
            derived_video_duration_seconds=10.0,
        )
    )


def _seed_passing_ddm(
    session: DatabaseSession,
    *,
    label: str,
    dataset_id: UUID,
    member_id: UUID,
    context_id: UUID,
    submission_id: UUID,
    execution_id: UUID,
    now: datetime,
) -> tuple[UUID, UUID]:
    """把一套合成输入跑过真实用途/制品用例，返回 (passed check id, available artifact id)。"""
    _seed_ddm_rows(
        session,
        label=label,
        dataset_id=dataset_id,
        member_id=member_id,
        context_id=context_id,
        submission_id=submission_id,
        execution_id=execution_id,
        now=now,
    )
    datasets = PostgresDatasetRepository(session)
    jobs = PostgresUsageJobQueue(session, _noop_dispatch)
    requested = request_usage_check(
        dataset_id=dataset_id,
        kind=UsageKind.DDM,
        candidate_id=None,
        caller=_caller(Permission.DATASET_EDIT),
        now=now,
        datasets=datasets,
        jobs=jobs,
    )
    target = begin_usage_check(job=requested.job, datasets=datasets, now=now + timedelta(seconds=1))
    assert target is not None
    validation = run_usage_check(
        target=target,
        storage=cast(ObjectStorage, _FakeStorage()),
        media_probe=cast(MediaProbe, _FakeMediaProbe()),
        annotation_volume=cast(AnnotationDataVolume, _FakeAnnotationVolume()),
        ddm_reader=_RecordingDdmReader(),
    )
    check = complete_usage_check(
        target=target,
        validation=validation,
        now=now + timedelta(seconds=2),
        datasets=datasets,
    )
    requested_artifact = request_artifact(
        dataset_id=dataset_id,
        check_id=check.id,
        caller=_caller(Permission.DATASET_EDIT),
        now=now + timedelta(seconds=3),
        datasets=datasets,
        jobs=jobs,
    )
    artifact_target = begin_artifact_generation(
        job=requested_artifact.job,
        datasets=datasets,
        now=now + timedelta(seconds=4),
    )
    assert artifact_target is not None
    artifact_sha256 = hashlib.sha256(f"{label}-artifact".encode()).hexdigest()
    artifact_size = 128
    artifact = complete_artifact(
        target=artifact_target,
        object_key=f"artifacts/{label}.json",
        artifact_sha256=artifact_sha256,
        artifact_size=artifact_size,
        manifest={
            "input_digest": check.input_digest,
            "base_commit": check.base_commit,
            "contract_version": check.contract_version,
            "consumer_parameters": dict(DDM_CONSUMER_PARAMETERS),
            "artifact_sha256": artifact_sha256,
            "artifact_size": artifact_size,
        },
        now=now + timedelta(seconds=5),
        datasets=datasets,
    )
    assert check.status.value == "passed"
    assert artifact.status is ArtifactStatus.AVAILABLE
    return check.id, artifact.id


def _fake_request(engine: Engine) -> Request:
    """最小请求对象：只提供生产 request_session 读取的 session_factory。"""
    return cast(
        Request,
        SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(session_factory=session_factory(engine)))
        ),
    )


def _submit(
    engine: Engine,
    *,
    idempotency_key: str,
    training: DdmInputSelection,
    validation: DdmInputSelection,
    dispatch: Callable[[UUID], None] | None = None,
) -> DdmTrainingCommandResult:
    with session_factory(engine)() as session:
        result = request_ddm_training_command(
            training=training,
            validation=validation,
            idempotency_key=idempotency_key,
            caller=_caller(Permission.DATASET_EDIT),
            now=datetime.now(UTC),
            datasets=PostgresDatasetRepository(session),
            jobs=PostgresUsageJobQueue(session, dispatch or _noop_dispatch),
        )
        session.commit()
        return result


def _assert_no_redis_job(redis_client: Redis, job_id: UUID) -> None:
    marker = str(job_id).encode()
    assert not any(marker in key for key in cast("list[bytes]", redis_client.keys("arq:*")))


def test_ddm_command_outbox_atomicity_and_held_dispatch(
    engine: Engine,
    dataset_storage_root: Path,
    redis_server: RedisServer,
    redis_client: Redis,
) -> None:
    settings = settings_for(engine, storage_root=dataset_storage_root, redis_url=redis_server.url)
    training_id, validation_id = uuid4(), uuid4()
    with session_factory(engine).begin() as session:
        train_check, train_artifact = _seed_passing_ddm(
            session,
            label="train",
            dataset_id=training_id,
            member_id=uuid4(),
            context_id=uuid4(),
            submission_id=uuid4(),
            execution_id=uuid4(),
            now=NOW,
        )
        val_check, val_artifact = _seed_passing_ddm(
            session,
            label="val",
            dataset_id=validation_id,
            member_id=uuid4(),
            context_id=uuid4(),
            submission_id=uuid4(),
            execution_id=uuid4(),
            now=NOW + timedelta(seconds=10),
        )
    training = DdmInputSelection(
        dataset_id=training_id, usage_check_id=train_check, artifact_id=train_artifact
    )
    validation = DdmInputSelection(
        dataset_id=validation_id, usage_check_id=val_check, artifact_id=val_artifact
    )
    try:
        # 回滚：命令与 outbox 任务都不落库，且提交后唤醒不触发。
        rolled_back: list[UUID] = []
        rollback_generator = cast(
            Generator[DatabaseSession, None, None], request_session(_fake_request(engine))
        )
        rollback_session = next(rollback_generator)
        request_ddm_training_command(
            training=training,
            validation=validation,
            idempotency_key="ddm-rollback",
            caller=_caller(Permission.DATASET_EDIT),
            now=datetime.now(UTC),
            datasets=PostgresDatasetRepository(rollback_session),
            jobs=PostgresUsageJobQueue(rollback_session, rolled_back.append),
        )
        with pytest.raises(RuntimeError):
            rollback_generator.throw(RuntimeError("synthetic rollback"))
        assert rolled_back == []
        assert (
            row(
                engine,
                "SELECT count(*) FROM dataset_ddm_training_command WHERE dataset_id = :dataset_id",
                dataset_id=training_id,
            )[0]
            == 0
        )
        assert (
            row(
                engine,
                "SELECT count(*) FROM job_application_job "
                "WHERE dataset_id = :dataset_id AND job_type = 'dataset_ddm_training'",
                dataset_id=training_id,
            )[0]
            == 0
        )

        # 提交：生产 request_session UoW 提交并触发提交后唤醒，且只在事实可持久时触发一次。
        woke: list[UUID] = []
        commit_generator = cast(
            Generator[DatabaseSession, None, None], request_session(_fake_request(engine))
        )
        commit_session = next(commit_generator)
        submitted = request_ddm_training_command(
            training=training,
            validation=validation,
            idempotency_key="ddm-commit",
            caller=_caller(Permission.DATASET_EDIT),
            now=datetime.now(UTC),
            datasets=PostgresDatasetRepository(commit_session),
            jobs=PostgresUsageJobQueue(commit_session, woke.append),
        )
        with pytest.raises(StopIteration):
            next(commit_generator)
        assert woke == [submitted.job.id]
        command_facts = row(
            engine,
            "SELECT status, job_id FROM dataset_ddm_training_command "
            "WHERE idempotency_key = 'ddm-commit'",
        )
        assert tuple(command_facts) == ("pending", submitted.job.id)
        job_facts = row(
            engine,
            "SELECT status, outbox_status FROM job_application_job WHERE id = :job_id",
            job_id=submitted.job.id,
        )
        assert tuple(job_facts) == ("pending", "pending")

        # 真实 dispatcher 识别 held 类型：不投递、Redis 不产生任务、扫描后仍为 pending。
        dispatcher = ArqJobDispatcher.from_settings(
            settings, session_factory=session_factory(engine)
        )
        asyncio.run(dispatcher.dispatch_async(submitted.job.id))
        _assert_no_redis_job(redis_client, submitted.job.id)
        context: dict[str, Any] = {
            "dispatcher": dispatcher,
            "settings": settings,
            "session_factory": session_factory(engine),
        }
        asyncio.run(dispatch_pending_jobs(context))
        _assert_no_redis_job(redis_client, submitted.job.id)
        assert row(
            engine,
            "SELECT status, outbox_status FROM job_application_job WHERE id = :job_id",
            job_id=submitted.job.id,
        ) == ("pending", "pending")
    finally:
        cleanup_dataset(engine, training_id)
        cleanup_dataset(engine, validation_id)


def test_ddm_command_replay_conflict_and_concurrent_submission(
    engine: Engine,
    dataset_storage_root: Path,
) -> None:
    del dataset_storage_root
    training_id, validation_id, other_validation_id = uuid4(), uuid4(), uuid4()
    with session_factory(engine).begin() as session:
        train_check, train_artifact = _seed_passing_ddm(
            session,
            label="replay-train",
            dataset_id=training_id,
            member_id=uuid4(),
            context_id=uuid4(),
            submission_id=uuid4(),
            execution_id=uuid4(),
            now=NOW,
        )
        val_check, val_artifact = _seed_passing_ddm(
            session,
            label="replay-val",
            dataset_id=validation_id,
            member_id=uuid4(),
            context_id=uuid4(),
            submission_id=uuid4(),
            execution_id=uuid4(),
            now=NOW + timedelta(seconds=10),
        )
        other_check, other_artifact = _seed_passing_ddm(
            session,
            label="other-val",
            dataset_id=other_validation_id,
            member_id=uuid4(),
            context_id=uuid4(),
            submission_id=uuid4(),
            execution_id=uuid4(),
            now=NOW + timedelta(seconds=20),
        )
    training = DdmInputSelection(
        dataset_id=training_id, usage_check_id=train_check, artifact_id=train_artifact
    )
    validation = DdmInputSelection(
        dataset_id=validation_id, usage_check_id=val_check, artifact_id=val_artifact
    )
    changed_validation = DdmInputSelection(
        dataset_id=other_validation_id, usage_check_id=other_check, artifact_id=other_artifact
    )
    try:
        first = _submit(
            engine, idempotency_key="ddm-replay", training=training, validation=validation
        )
        replay = _submit(
            engine, idempotency_key="ddm-replay", training=training, validation=validation
        )
        assert replay.command.id == first.command.id
        assert replay.job.id == first.job.id
        assert replay.command.payload["input_id"] == first.command.payload["input_id"]
        assert (
            row(
                engine,
                "SELECT count(*) FROM dataset_ddm_training_command "
                "WHERE idempotency_key = 'ddm-replay'",
            )[0]
            == 1
        )

        _submit(engine, idempotency_key="ddm-conflict", training=training, validation=validation)
        with pytest.raises(DatasetRefusedError) as refused:
            _submit(
                engine,
                idempotency_key="ddm-conflict",
                training=training,
                validation=changed_validation,
            )
        assert refused.value.code is DatasetRefusalCode.IDEMPOTENCY_CONFLICT

        # 并发：两个真实 Session 用同一幂等键同时受理，仅一个逻辑命令/任务。
        barrier = threading.Barrier(2)

        def submit_concurrently() -> UUID:
            barrier.wait(timeout=30)
            return _submit(
                engine,
                idempotency_key="ddm-concurrent",
                training=training,
                validation=validation,
            ).command.id

        with ThreadPoolExecutor(max_workers=2) as pool:
            command_ids = list(pool.map(lambda _index: submit_concurrently(), range(2)))
        assert len(set(command_ids)) == 1
        assert (
            row(
                engine,
                "SELECT count(*) FROM dataset_ddm_training_command "
                "WHERE idempotency_key = 'ddm-concurrent'",
            )[0]
            == 1
        )
        assert (
            row(
                engine,
                "SELECT count(*) FROM job_application_job j "
                "JOIN dataset_ddm_training_command c ON c.job_id = j.id "
                "WHERE c.idempotency_key = 'ddm-concurrent'",
            )[0]
            == 1
        )
    finally:
        cleanup_dataset(engine, training_id)
        cleanup_dataset(engine, validation_id)
        cleanup_dataset(engine, other_validation_id)


def test_ddm_command_replay_survives_source_current_revision_change(
    engine: Engine,
    dataset_storage_root: Path,
) -> None:
    """来源当前修订变化后，同键同选择仍重放原命令；新键或新选择才重新判定。"""
    del dataset_storage_root
    training_id, validation_id, other_validation_id = uuid4(), uuid4(), uuid4()
    with session_factory(engine).begin() as session:
        train_check, train_artifact = _seed_passing_ddm(
            session,
            label="stale-train",
            dataset_id=training_id,
            member_id=uuid4(),
            context_id=uuid4(),
            submission_id=uuid4(),
            execution_id=uuid4(),
            now=NOW,
        )
        val_check, val_artifact = _seed_passing_ddm(
            session,
            label="stale-val",
            dataset_id=validation_id,
            member_id=uuid4(),
            context_id=uuid4(),
            submission_id=uuid4(),
            execution_id=uuid4(),
            now=NOW + timedelta(seconds=10),
        )
        other_check, other_artifact = _seed_passing_ddm(
            session,
            label="stale-other",
            dataset_id=other_validation_id,
            member_id=uuid4(),
            context_id=uuid4(),
            submission_id=uuid4(),
            execution_id=uuid4(),
            now=NOW + timedelta(seconds=20),
        )
    training = DdmInputSelection(
        dataset_id=training_id, usage_check_id=train_check, artifact_id=train_artifact
    )
    validation = DdmInputSelection(
        dataset_id=validation_id, usage_check_id=val_check, artifact_id=val_artifact
    )
    changed_validation = DdmInputSelection(
        dataset_id=other_validation_id, usage_check_id=other_check, artifact_id=other_artifact
    )
    try:
        first = _submit(
            engine, idempotency_key="ddm-stale", training=training, validation=validation
        )

        # 训练数据集出现新动作清单修订，令已通过用途检查不再是当前输入。
        with session_factory(engine).begin() as session:
            assert PostgresDatasetRepository(session).add_action_list(
                ActionListRevision(
                    dataset_id=training_id,
                    revision=2,
                    actions=("(1) 取料", "(2) 安装", "(3) 复核"),
                    created_by=ACTOR_ID,
                    created_at=NOW + timedelta(seconds=30),
                )
            )

        # 相同键与选择：在冻结前重放首次命令，不重新冻结、不因当前性变化被拒。
        replay = _submit(
            engine, idempotency_key="ddm-stale", training=training, validation=validation
        )
        assert replay.command.id == first.command.id
        assert replay.job.id == first.job.id
        assert replay.command.payload["input_id"] == first.command.payload["input_id"]

        # 新键：仍执行完整冻结与当前性校验，过期来源必须被拒。
        with pytest.raises(DatasetRefusedError) as stale:
            _submit(
                engine,
                idempotency_key="ddm-stale-fresh",
                training=training,
                validation=validation,
            )
        assert stale.value.code is DatasetRefusalCode.USAGE_STATE_CONFLICT

        # 相同键但选择变化：冲突，不静默重绑定到另一份来源。
        with pytest.raises(DatasetRefusedError) as conflict:
            _submit(
                engine,
                idempotency_key="ddm-stale",
                training=training,
                validation=changed_validation,
            )
        assert conflict.value.code is DatasetRefusalCode.IDEMPOTENCY_CONFLICT

        # 未授权者不能借重放读取或恢复已受理命令。
        with session_factory(engine)() as session, pytest.raises(AuthorizationRefusedError):
            request_ddm_training_command(
                training=training,
                validation=validation,
                idempotency_key="ddm-stale",
                caller=_caller(Permission.DATASET_VIEW),
                now=datetime.now(UTC),
                datasets=PostgresDatasetRepository(session),
                jobs=PostgresUsageJobQueue(session, _noop_dispatch),
            )
    finally:
        cleanup_dataset(engine, training_id)
        cleanup_dataset(engine, validation_id)
        cleanup_dataset(engine, other_validation_id)


def _assert_redis_job(redis_client: Redis, job_id: UUID) -> None:
    marker = str(job_id).encode()
    assert any(marker in key for key in cast("list[bytes]", redis_client.keys("arq:*")))


def test_outbox_scan_skips_held_ddm_jobs_and_dispatches_later_executable_job(
    engine: Engine,
    dataset_storage_root: Path,
    redis_server: RedisServer,
    redis_client: Redis,
) -> None:
    """held 的 DDM 行不得占满补投窗口，窗口之后的可执行任务仍要被真实投递。"""
    settings = settings_for(engine, storage_root=dataset_storage_root, redis_url=redis_server.url)
    held_dataset_id = uuid4()
    executable_member_id = uuid4()
    held_jobs = [
        ApplicationJob(
            id=uuid4(),
            job_type=JobType.DATASET_DDM_TRAINING,
            status=JobStatus.PENDING,
            member_id=held_dataset_id,
            attempt_id=uuid4(),
            created_at=NOW - timedelta(minutes=5) + timedelta(seconds=index),
            updated_at=NOW - timedelta(minutes=5) + timedelta(seconds=index),
            failure_code=None,
            dataset_id=held_dataset_id,
        )
        for index in range(100)
    ]
    try:
        with session_factory(engine).begin() as session:
            repository = PostgresJobRepository(session)
            for job in held_jobs:
                repository.add(job)
            executable = repository.get_or_create_validation(
                member_id=executable_member_id,
                attempt_id=uuid4(),
                now=NOW,
            )
        dispatcher = ArqJobDispatcher.from_settings(
            settings, session_factory=session_factory(engine)
        )
        context: dict[str, Any] = {
            "dispatcher": dispatcher,
            "settings": settings,
            "session_factory": session_factory(engine),
        }
        asyncio.run(dispatch_pending_jobs(context))

        _assert_redis_job(redis_client, executable.id)
        for held in held_jobs:
            _assert_no_redis_job(redis_client, held.id)
        assert row(
            engine,
            "SELECT status, outbox_status FROM job_application_job WHERE id = :job_id",
            job_id=executable.id,
        ) == ("enqueued", "dispatched")
        for held in held_jobs:
            assert row(
                engine,
                "SELECT status, outbox_status FROM job_application_job WHERE id = :job_id",
                job_id=held.id,
            ) == ("pending", "pending")
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM job_application_job WHERE dataset_id = :dataset_id"),
                {"dataset_id": held_dataset_id},
            )
            connection.execute(
                text("DELETE FROM job_application_job WHERE member_id = :member_id"),
                {"member_id": executable_member_id},
            )
