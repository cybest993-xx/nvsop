"""训练数据异步任务的 ARQ worker 与提交后 outbox 补投任务。"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from io import BytesIO
from typing import Any, Protocol, assert_never, cast
from uuid import UUID

from arq import Worker, cron
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from factory_sop.dataset.api import (
    AnnotationBackendExecutionError,
    AnnotationBackendUnavailableError,
    AnnotationContextPreparationTarget,
    AnnotationDataVolumeUnavailableError,
    AnnotationExecutionTarget,
    ArtifactTarget,
    DatasetAnnotationRuntime,
    DatasetRefusalCode,
    DatasetRefusedError,
    DatasetUsageRuntime,
    DatasetValidationRuntime,
    MediaProbeUnavailableError,
    MemberStatus,
    ObjectStorageUnavailableError,
    UsageCheckStatus,
    UsageCheckTarget,
    UsageKind,
    apply_usage_check_currentness,
    begin_annotation_context_preparation,
    begin_annotation_execution,
    begin_artifact_generation,
    begin_usage_check,
    begin_video_validation,
    complete_annotation_context_preparation,
    complete_annotation_execution,
    complete_artifact,
    complete_artifact_candidate_cleanup,
    complete_usage_check,
    fail_annotation_context_preparation,
    fail_annotation_execution,
    fail_artifact,
    fail_usage_check,
    invalidate_artifact_for_job,
    mark_artifact_cleanup_pending,
    prepare_annotation_context_copy,
    prepare_annotation_execution_copy,
    record_artifact_orphan_candidate,
    render_ddm_artifact_with_base,
    run_usage_check,
    save_annotation_execution_copy,
    validate_video_upload,
)
from factory_sop.job.adapters.dispatcher import ArqJobDispatcher
from factory_sop.job.adapters.repository import PostgresJobRepository
from factory_sop.job.api import JobStatus
from factory_sop.observability import configure_logging, get_logger
from factory_sop.persistence import create_database_engine, session_factory
from factory_sop.settings import Settings

_logger = get_logger("job")


class _ArtifactCandidateStorage(Protocol):
    """候选制品清理所需的最小对象存储契约。"""

    def delete(self, *, object_key: str) -> None:
        """删除未发布的候选对象。"""
        ...


def _artifact_candidate_keys(
    *, object_key: str | None, manifest: Mapping[str, Any]
) -> tuple[str, ...]:
    """返回当前及持久化记录的未发布候选键，去重后稳定排序。"""
    keys = [object_key] if object_key else []
    keys.extend(
        key for key in manifest.get("orphan_candidate_keys", []) if isinstance(key, str) and key
    )
    return tuple(sorted(set(keys)))


def _usage_requires_annotation_volume(target: UsageCheckTarget) -> bool:
    """仅在 DDM 或 VLM 引用已保存切片时创建标注卷 adapter。"""
    match target.check.kind:
        case UsageKind.DDM:
            return True
        case UsageKind.VLM:
            scope = target.check.input_snapshot.get("scope")
            return isinstance(scope, list) and any(
                isinstance(item, dict) and item.get("annotation_execution_id") is not None
                for item in scope
            )
        case _:
            assert_never(target.check.kind)


async def validate_dataset_job(ctx: Mapping[str, Any], job_id: str) -> None:
    """按 job id 幂等执行视频校验；Redis payload 不携带任何凭据或对象地址。"""
    identifier = UUID(job_id)
    factory = _session_factory(ctx)
    runtime = _dataset_runtime(ctx)
    now = datetime.now(UTC)

    with factory() as session:
        jobs = PostgresJobRepository(session)
        running = jobs.mark_running(job_id=identifier, now=now)
        session.commit()
    if running is None:
        return

    with factory() as session:
        datasets = runtime.repository(session)
        target = begin_video_validation(job=running, datasets=datasets, now=datetime.now(UTC))
        if target is None:
            jobs = PostgresJobRepository(session)
            jobs.finish(
                job_id=running.id,
                status=JobStatus.SUPERSEDED.value,
                failure_code=None,
                now=datetime.now(UTC),
                expected_updated_at=running.updated_at,
            )
            session.commit()
            return
        session.commit()

    storage = runtime.storage()
    probe = runtime.media_probe()
    result_session = factory()
    try:
        datasets = runtime.repository(result_session)
        jobs = PostgresJobRepository(result_session)
        result = validate_video_upload(
            job=running,
            datasets=datasets,
            storage=storage,
            probe=probe,
            supported_codecs=runtime.supported_codecs(),
            now=datetime.now(UTC),
            target=target,
        )
        current = datasets.member_by_id(running.member_id)
        if current is not None and current.current_attempt_id != running.attempt_id:
            final_status = JobStatus.SUPERSEDED.value
            failure_code = None
        elif result.status == MemberStatus.REGISTERED.value:
            final_status = JobStatus.SUCCEEDED.value
            failure_code = None
        else:
            final_status = JobStatus.FAILED.value
            failure_code = result.failure_code
        finished = jobs.finish(
            job_id=running.id,
            status=final_status,
            failure_code=failure_code,
            now=datetime.now(UTC),
            expected_updated_at=running.updated_at,
        )
        result_session.commit()
        _logger.info(
            "job.dataset_validation.finished" if finished else "job.dataset_validation.lease_lost",
            job_id=str(running.id),
            member_id=str(running.member_id),
            attempt_id=str(running.attempt_id),
            status=final_status if finished else "lease_lost",
        )
    finally:
        result_session.close()


async def check_dataset_usage_job(ctx: Mapping[str, Any], job_id: str) -> None:
    """在事务外复核冻结用途输入，并把检查结果写回中心。"""
    identifier = UUID(job_id)
    factory = _session_factory(ctx)
    runtime = _usage_runtime(ctx)
    with factory() as session:
        jobs = PostgresJobRepository(session)
        running = jobs.mark_running(job_id=identifier, now=datetime.now(UTC))
        session.commit()
    if running is None:
        return

    with factory() as session:
        datasets = runtime.repository(session)
        target = begin_usage_check(job=running, datasets=datasets, now=datetime.now(UTC))
        if target is None:
            jobs = PostgresJobRepository(session)
            jobs.finish(
                job_id=running.id,
                status=JobStatus.SUPERSEDED.value,
                failure_code=None,
                now=datetime.now(UTC),
                expected_updated_at=running.updated_at,
            )
            session.commit()
            return
        session.commit()

    try:
        annotation_volume = (
            runtime.annotation_volume() if _usage_requires_annotation_volume(target) else None
        )
        result = run_usage_check(
            target=target,
            storage=runtime.storage(),
            media_probe=runtime.media_probe(),
            annotation_volume=annotation_volume,
            ddm_reader=runtime.ddm_reader(),
            vlm_reader=runtime.vlm_reader(),
        )
    except ObjectStorageUnavailableError as error:
        _finish_usage_check_failure(
            factory=factory,
            runtime=runtime,
            target=target,
            code="USAGE_STORAGE_UNAVAILABLE",
            detail="对象存储暂时不可用，请稍后重试",
            error=error,
        )
        return
    except AnnotationDataVolumeUnavailableError as error:
        _finish_usage_check_failure(
            factory=factory,
            runtime=runtime,
            target=target,
            code="USAGE_ANNOTATION_VOLUME_UNAVAILABLE",
            detail="标注数据卷暂时不可用，请稍后重试",
            error=error,
        )
        return
    except MediaProbeUnavailableError as error:
        _finish_usage_check_failure(
            factory=factory,
            runtime=runtime,
            target=target,
            code="USAGE_MEDIA_PROBE_UNAVAILABLE",
            detail="媒体探测工具暂时不可用，请稍后重试",
            error=error,
        )
        return
    except Exception as error:  # pragma: no cover - worker 安全兜底
        _finish_usage_check_failure(
            factory=factory,
            runtime=runtime,
            target=target,
            code="USAGE_CHECK_EXECUTION_FAILED",
            detail="用途检查执行失败",
            error=error,
        )
        return

    try:
        with factory() as result_session:
            datasets = runtime.repository(result_session)
            result = apply_usage_check_currentness(
                check=target.check,
                validation=result,
                datasets=datasets,
            )
            completed = complete_usage_check(
                target=target,
                validation=result,
                now=datetime.now(UTC),
                datasets=datasets,
            )
            jobs = PostgresJobRepository(result_session)
            issue_code = completed.issues[0].get("code") if completed.issues else None
            match completed.status:
                case UsageCheckStatus.PASSED:
                    job_status = JobStatus.SUCCEEDED.value
                case UsageCheckStatus.FAILED:
                    job_status = JobStatus.FAILED.value
                case (
                    UsageCheckStatus.UNCHECKED | UsageCheckStatus.PENDING | UsageCheckStatus.RUNNING
                ):
                    raise RuntimeError("用途检查结果状态仍未结案")
            finished = jobs.finish(
                job_id=running.id,
                status=job_status,
                failure_code=issue_code,
                now=datetime.now(UTC),
                expected_updated_at=running.updated_at,
            )
            if not finished:
                result_session.rollback()
                return
            result_session.commit()
    except DatasetRefusedError as error:
        _finish_usage_check_failure(
            factory=factory,
            runtime=runtime,
            target=target,
            code=error.code.value,
            detail=error.detail,
            error=error,
        )
        return
    except Exception as error:  # pragma: no cover - 数据库故障由真实集成测试覆盖
        _finish_usage_check_failure(
            factory=factory,
            runtime=runtime,
            target=target,
            code="USAGE_CHECK_DATABASE_FAILURE",
            detail="用途检查结果登记失败",
            error=error,
        )
        return
    _logger.info(
        "job.dataset_usage_check.finished",
        job_id=str(running.id),
        dataset_id=str(target.check.dataset_id),
        usage_kind=target.check.kind.value,
        check_id=str(target.check.id),
        input_digest=target.check.input_digest,
        actor_id=str(target.check.created_by),
        result="succeeded" if finished else "lease_lost",
    )


def _finish_usage_check_failure(
    *,
    factory: sessionmaker[Session],
    runtime: DatasetUsageRuntime,
    target: UsageCheckTarget,
    code: str,
    detail: str,
    error: Exception,
) -> None:
    """在独立事务中记录用途检查 worker 失败。"""
    with factory() as session:
        datasets = runtime.repository(session)
        try:
            fail_usage_check(
                target=target,
                code=code,
                detail=detail,
                now=datetime.now(UTC),
                datasets=datasets,
            )
        except DatasetRefusedError:
            _logger.warning(
                "job.dataset_usage_check.lease_lost",
                job_id=str(target.job.id),
                error_type=type(error).__name__,
            )
            return
        jobs = PostgresJobRepository(session)
        finished = jobs.finish(
            job_id=target.job.id,
            status=JobStatus.FAILED.value,
            failure_code=code,
            now=datetime.now(UTC),
            expected_updated_at=target.job.updated_at,
        )
        if not finished:
            session.rollback()
            return
        session.commit()
    _logger.warning(
        "job.dataset_usage_check.failed",
        job_id=str(target.job.id),
        failure_code=code,
        error_type=type(error).__name__,
    )


async def generate_dataset_artifact_job(ctx: Mapping[str, Any], job_id: str) -> None:
    """把通过的 DDM 检查转换为不可覆盖 annotation 制品。"""
    identifier = UUID(job_id)
    factory = _session_factory(ctx)
    runtime = _usage_runtime(ctx)
    with factory() as session:
        jobs = PostgresJobRepository(session)
        running = jobs.mark_running(job_id=identifier, now=datetime.now(UTC))
        session.commit()
    if running is None:
        return

    stale_object_keys: tuple[str, ...] = ()
    with factory() as session:
        datasets = runtime.repository(session)
        target = begin_artifact_generation(job=running, datasets=datasets, now=datetime.now(UTC))
        if target is None:
            stale_artifact = datasets.artifact_by_id(running.attempt_id)
            if (
                stale_artifact is not None
                and stale_artifact.job_id == running.id
                and stale_artifact.status.value in {"pending", "running", "failed"}
            ):
                stale_object_keys = _artifact_candidate_keys(
                    object_key=stale_artifact.object_key,
                    manifest=stale_artifact.manifest,
                )
            invalidate_artifact_for_job(
                job=running,
                datasets=datasets,
                now=datetime.now(UTC),
            )
            session.commit()
        else:
            session.commit()
    if target is None:
        if stale_object_keys:
            try:
                storage = runtime.storage()
                cleanup_succeeded = all(
                    _discard_artifact_candidate(storage=storage, object_key=object_key)
                    for object_key in stale_object_keys
                )
            except Exception:
                cleanup_succeeded = False
                _logger.exception(
                    "job.dataset_artifact.candidate_cleanup_unavailable",
                    job_id=str(running.id),
                    dataset_id=str(running.dataset_id),
                    artifact_id=str(running.attempt_id),
                    object_keys=stale_object_keys,
                    result="retry_pending",
                )
            if not cleanup_succeeded:
                _logger.warning(
                    "job.dataset_artifact.candidate_cleanup_retry",
                    job_id=str(running.id),
                    dataset_id=str(running.dataset_id),
                    artifact_id=str(running.attempt_id),
                    object_keys=stale_object_keys,
                    result="retry_pending",
                )
                return
            with factory() as cleanup_session:
                datasets = runtime.repository(cleanup_session)
                cleared = complete_artifact_candidate_cleanup(
                    artifact_id=running.attempt_id,
                    job_id=running.id,
                    now=datetime.now(UTC),
                    datasets=datasets,
                )
                if not cleared:
                    cleanup_session.rollback()
                    _logger.warning(
                        "job.dataset_artifact.candidate_cleanup_lease_lost",
                        job_id=str(running.id),
                        dataset_id=str(running.dataset_id),
                        artifact_id=str(running.attempt_id),
                        object_keys=stale_object_keys,
                        result="retry_pending",
                    )
                    return
                jobs = PostgresJobRepository(cleanup_session)
                finished = jobs.finish(
                    job_id=running.id,
                    status=JobStatus.SUPERSEDED.value,
                    failure_code=None,
                    now=datetime.now(UTC),
                    expected_updated_at=running.updated_at,
                )
                if not finished:
                    cleanup_session.rollback()
                    return
                cleanup_session.commit()
            _logger.info(
                "job.dataset_artifact.candidate_cleanup.finished",
                job_id=str(running.id),
                dataset_id=str(running.dataset_id),
                artifact_id=str(running.attempt_id),
                object_keys=stale_object_keys,
                result="succeeded",
            )
            return
        with factory() as finish_session:
            jobs = PostgresJobRepository(finish_session)
            finished = jobs.finish(
                job_id=running.id,
                status=JobStatus.SUPERSEDED.value,
                failure_code=None,
                now=datetime.now(UTC),
                expected_updated_at=running.updated_at,
            )
            if not finished:
                finish_session.rollback()
                return
            finish_session.commit()
        return

    object_key = target.artifact.object_key or (
        f"training-datasets/{target.artifact.dataset_id}/artifacts/"
        f"{target.artifact.id}/{target.job.id}/annotation.json"
    )
    try:
        artifact_storage = runtime.storage()
    except Exception as error:
        _finish_artifact_failure(
            factory=factory,
            runtime=runtime,
            target=target,
            code="STORAGE_UNAVAILABLE",
            detail="对象存储暂时不可用，请稍后重试",
            error=error,
        )
        return
    try:
        # 重试先清理持久化记录的旧候选键和当前候选键。
        for orphan_key in _artifact_candidate_keys(
            object_key=None, manifest=target.artifact.manifest
        ):
            if not _discard_artifact_candidate(storage=artifact_storage, object_key=orphan_key):
                raise DatasetRefusedError(
                    DatasetRefusalCode.STORAGE_UNAVAILABLE,
                    detail="旧制品候选清理失败，请稍后重试",
                )
        _discard_artifact_candidate(storage=artifact_storage, object_key=object_key)
        content, manifest = render_ddm_artifact_with_base(
            target,
            storage=artifact_storage,
            generate=runtime.ddm_generator().generate,
        )
        stored = artifact_storage.finalize_upload(
            object_key=object_key,
            source=BytesIO(content),
            size=len(content),
        )
        if stored.size != len(content):
            _discard_artifact_candidate(storage=artifact_storage, object_key=object_key)
            raise DatasetRefusedError(
                DatasetRefusalCode.ARTIFACT_INTEGRITY_FAILURE,
                detail="制品对象大小与生成内容不一致",
            )
        readback = BytesIO()
        artifact_storage.download_to(object_key=object_key, destination=readback)
        if readback.getvalue() != content:
            _discard_artifact_candidate(storage=artifact_storage, object_key=object_key)
            raise DatasetRefusedError(
                DatasetRefusalCode.ARTIFACT_INTEGRITY_FAILURE,
                detail="制品对象回读摘要与生成内容不一致",
            )
    except Exception as error:  # pragma: no cover - worker 安全兜底
        candidate_cleaned = _discard_artifact_candidate(
            storage=artifact_storage, object_key=object_key
        )
        if not candidate_cleaned:
            _finish_artifact_cleanup_failure(
                factory=factory,
                runtime=runtime,
                target=target,
                object_key=object_key,
                error=error,
            )
            return
        code = (
            error.code.value
            if isinstance(error, DatasetRefusedError)
            else (
                "STORAGE_UNAVAILABLE"
                if isinstance(error, ObjectStorageUnavailableError)
                else "ARTIFACT_GENERATION_FAILED"
            )
        )
        detail = (
            error.detail
            if isinstance(error, DatasetRefusedError)
            else (
                "对象存储暂时不可用，请稍后重试"
                if isinstance(error, ObjectStorageUnavailableError)
                else "DDM annotation 制品生成失败"
            )
        )
        _finish_artifact_failure(
            factory=factory,
            runtime=runtime,
            target=target,
            code=code,
            detail=detail,
            error=error,
        )
        return

    with factory() as session:
        datasets = runtime.repository(session)
        try:
            completed = complete_artifact(
                target=target,
                object_key=object_key,
                artifact_sha256=str(manifest["artifact_sha256"]),
                artifact_size=int(manifest["artifact_size"]),
                manifest=manifest,
                now=datetime.now(UTC),
                datasets=datasets,
            )
            jobs = PostgresJobRepository(session)
            finished = jobs.finish(
                job_id=running.id,
                status=JobStatus.SUCCEEDED.value,
                failure_code=None,
                now=datetime.now(UTC),
                expected_updated_at=running.updated_at,
            )
            if not finished:
                session.rollback()
                _record_artifact_orphan(
                    factory=factory,
                    runtime=runtime,
                    artifact_id=target.artifact.id,
                    object_key=object_key,
                    error=RuntimeError("artifact job lease lost"),
                )
                return
            try:
                session.commit()
            except Exception:  # pragma: no cover - 提交结果需真实数据库故障注入验证
                _logger.exception(
                    "job.dataset_artifact.commit_unknown",
                    job_id=str(running.id),
                    dataset_id=str(completed.dataset_id),
                    usage_kind=completed.kind.value,
                    artifact_id=str(completed.id),
                    usage_check_id=str(completed.usage_check_id),
                    input_digest=completed.input_digest,
                    actor_id=str(completed.created_by),
                    result="commit_unknown",
                )
                return
        except DatasetRefusedError as error:
            session.rollback()
            candidate_cleaned = _discard_artifact_candidate(
                storage=artifact_storage, object_key=object_key
            )
            if not candidate_cleaned:
                _finish_artifact_cleanup_failure(
                    factory=factory,
                    runtime=runtime,
                    target=target,
                    object_key=object_key,
                    error=error,
                )
                return
            _finish_artifact_failure(
                factory=factory,
                runtime=runtime,
                target=target,
                code=error.code.value,
                detail=error.detail,
                error=error,
            )
            return
        except Exception as error:  # pragma: no cover - 数据库故障由真实集成测试覆盖
            session.rollback()
            candidate_cleaned = _discard_artifact_candidate(
                storage=artifact_storage, object_key=object_key
            )
            if not candidate_cleaned:
                _finish_artifact_cleanup_failure(
                    factory=factory,
                    runtime=runtime,
                    target=target,
                    object_key=object_key,
                    error=error,
                )
                return
            _finish_artifact_failure(
                factory=factory,
                runtime=runtime,
                target=target,
                code="ARTIFACT_DATABASE_FAILURE",
                detail="制品结果登记失败",
                error=error,
            )
            return
    _logger.info(
        "job.dataset_artifact.finished",
        job_id=str(running.id),
        dataset_id=str(completed.dataset_id),
        usage_kind=completed.kind.value,
        artifact_id=str(completed.id),
        usage_check_id=str(completed.usage_check_id),
        input_digest=completed.input_digest,
        actor_id=str(completed.created_by),
        result="succeeded" if finished else "lease_lost",
    )


def _discard_artifact_candidate(*, storage: _ArtifactCandidateStorage, object_key: str) -> bool:
    """删除未被 PostgreSQL 权威记录引用的候选对象。"""
    try:
        storage.delete(object_key=object_key)
    except Exception:
        _logger.exception(
            "job.dataset_artifact.orphan_candidate",
            object_key=object_key,
        )
        return False
    return True


def _record_artifact_orphan(
    *,
    factory: sessionmaker[Session],
    runtime: DatasetUsageRuntime,
    artifact_id: UUID,
    object_key: str,
    error: Exception,
) -> None:
    """租约丢失后持久化候选键，避免直接删除未知归属对象。"""
    with factory() as session:
        datasets = runtime.repository(session)
        if not record_artifact_orphan_candidate(
            artifact_id=artifact_id,
            object_key=object_key,
            datasets=datasets,
        ):
            session.rollback()
            _logger.warning(
                "job.dataset_artifact.orphan_candidate_record_failed",
                artifact_id=str(artifact_id),
                object_key=object_key,
                error_type=type(error).__name__,
                result="retry_pending",
            )
            return
        session.commit()
    _logger.warning(
        "job.dataset_artifact.orphan_candidate_recorded",
        artifact_id=str(artifact_id),
        object_key=object_key,
        error_type=type(error).__name__,
        result="retry_pending",
    )


def _finish_artifact_cleanup_failure(
    *,
    factory: sessionmaker[Session],
    runtime: DatasetUsageRuntime,
    target: ArtifactTarget,
    object_key: str,
    error: Exception,
) -> None:
    """候选清理失败时持久化可恢复状态并保留运行租约。"""
    with factory() as session:
        datasets = runtime.repository(session)
        if not mark_artifact_cleanup_pending(
            target=target,
            object_key=object_key,
            now=datetime.now(UTC),
            datasets=datasets,
        ):
            session.rollback()
            _logger.warning(
                "job.dataset_artifact.cleanup_lease_lost",
                job_id=str(target.job.id),
                dataset_id=str(target.artifact.dataset_id),
                artifact_id=str(target.artifact.id),
                error_type=type(error).__name__,
                result="retry_pending",
            )
            return
        session.commit()
    _logger.warning(
        "job.dataset_artifact.cleanup_retry",
        job_id=str(target.job.id),
        dataset_id=str(target.artifact.dataset_id),
        artifact_id=str(target.artifact.id),
        error_type=type(error).__name__,
        result="retry_pending",
    )


def _finish_artifact_failure(
    *,
    factory: sessionmaker[Session],
    runtime: DatasetUsageRuntime,
    target: ArtifactTarget,
    code: str,
    detail: str,
    error: Exception,
) -> None:
    """在独立事务中记录制品生成失败。"""
    with factory() as session:
        datasets = runtime.repository(session)
        try:
            fail_artifact(
                target=target,
                code=code,
                detail=detail,
                now=datetime.now(UTC),
                datasets=datasets,
            )
        except DatasetRefusedError:
            _logger.warning(
                "job.dataset_artifact.lease_lost",
                job_id=str(target.job.id),
                artifact_id=str(target.artifact.id),
                error_type=type(error).__name__,
            )
            return
        jobs = PostgresJobRepository(session)
        finished = jobs.finish(
            job_id=target.job.id,
            status=JobStatus.FAILED.value,
            failure_code=code,
            now=datetime.now(UTC),
            expected_updated_at=target.job.updated_at,
        )
        if not finished:
            session.rollback()
            return
        session.commit()
    _logger.warning(
        "job.dataset_artifact.failed",
        job_id=str(target.job.id),
        artifact_id=str(target.artifact.id),
        failure_code=code,
        error_type=type(error).__name__,
    )


async def prepare_annotation_context_job(ctx: Mapping[str, Any], job_id: str) -> None:
    """在事务外准备标注上下文基座副本，并短事务登记真实身份。"""
    identifier = UUID(job_id)
    factory = _session_factory(ctx)
    runtime = _annotation_runtime(ctx)
    now = datetime.now(UTC)

    with factory() as session:
        jobs = PostgresJobRepository(session)
        running = jobs.mark_running(job_id=identifier, now=now)
        session.commit()
    if running is None:
        return

    with factory() as session:
        datasets = runtime.repository(session)
        target = begin_annotation_context_preparation(
            job=running,
            datasets=datasets,
        )
        if target is None:
            jobs = PostgresJobRepository(session)
            jobs.finish(
                job_id=running.id,
                status=JobStatus.SUPERSEDED.value,
                failure_code=None,
                now=datetime.now(UTC),
                expected_updated_at=running.updated_at,
            )
            session.commit()
            return
        session.commit()

    try:
        prepared = prepare_annotation_context_copy(
            context=target.context,
            member=target.member,
            actions=target.actions,
            storage=runtime.storage(),
            backend=runtime.backend(),
            media_probe=runtime.media_probe(),
        )
    except AnnotationBackendUnavailableError as error:
        _finish_context_preparation_failure(
            factory=factory,
            runtime=runtime,
            target=target,
            code="ANNOTATION_BACKEND_UNAVAILABLE",
            detail="标注基座暂时不可用",
            error=error,
        )
        return
    except DatasetRefusedError as error:
        _finish_context_preparation_failure(
            factory=factory,
            runtime=runtime,
            target=target,
            code=error.code.value,
            detail=error.detail,
            error=error,
        )
        return
    except Exception as error:  # pragma: no cover - worker 安全兜底
        _finish_context_preparation_failure(
            factory=factory,
            runtime=runtime,
            target=target,
            code="ANNOTATION_EXECUTION_FAILED",
            detail="标注媒体准备失败",
            error=error,
        )
        return

    try:
        with factory() as session:
            datasets = runtime.repository(session)
            complete_annotation_context_preparation(
                target=target,
                prepared=prepared,
                datasets=datasets,
            )
            jobs = PostgresJobRepository(session)
            finished = jobs.finish(
                job_id=running.id,
                status=JobStatus.SUCCEEDED.value,
                failure_code=None,
                now=datetime.now(UTC),
                expected_updated_at=running.updated_at,
            )
            if not finished:
                session.rollback()
                _logger.warning(
                    "job.dataset_annotation_preparation.lease_lost",
                    job_id=str(running.id),
                    context_id=str(target.context.id),
                )
                return
            session.commit()
    except DatasetRefusedError as error:
        _finish_context_preparation_failure(
            factory=factory,
            runtime=runtime,
            target=target,
            code=error.code.value,
            detail=error.detail,
            error=error,
        )
        return
    _logger.info(
        "job.dataset_annotation_preparation.finished"
        if finished
        else "job.dataset_annotation_preparation.lease_lost",
        job_id=str(running.id),
        member_id=str(running.member_id),
        context_id=str(target.context.id),
        status="succeeded" if finished else "lease_lost",
    )


def _finish_context_preparation_failure(
    *,
    factory: sessionmaker[Session],
    runtime: DatasetAnnotationRuntime,
    target: AnnotationContextPreparationTarget,
    code: str,
    detail: str,
    error: Exception,
) -> None:
    """在独立事务中保存上下文准备失败并结案任务。"""
    with factory() as session:
        datasets = runtime.repository(session)
        try:
            failed = fail_annotation_context_preparation(
                target=target,
                code=code,
                detail=detail,
                datasets=datasets,
            )
        except DatasetRefusedError:
            _logger.warning(
                "job.dataset_annotation_preparation.lease_lost",
                job_id=str(target.job.id),
                context_id=str(target.context.id),
                failure_code=code,
                error_type=type(error).__name__,
            )
            return
        jobs = PostgresJobRepository(session)
        finished = jobs.finish(
            job_id=target.job.id,
            status=JobStatus.FAILED.value,
            failure_code=code,
            now=datetime.now(UTC),
            expected_updated_at=target.job.updated_at,
        )
        if not finished:
            session.rollback()
            _logger.warning(
                "job.dataset_annotation_preparation.lease_lost",
                job_id=str(target.job.id),
                context_id=str(target.context.id),
                failure_code=code,
            )
            return
        session.commit()
    _logger.warning(
        "job.dataset_annotation_preparation.failed"
        if finished
        else "job.dataset_annotation_preparation.lease_lost",
        job_id=str(target.job.id),
        context_id=str(failed.id),
        failure_code=code,
        error_type=type(error).__name__,
    )


async def annotate_dataset_job(ctx: Mapping[str, Any], job_id: str) -> None:
    """按执行代次调用复用的标注基座，并追加候选结果。"""
    identifier = UUID(job_id)
    factory = _session_factory(ctx)
    runtime = _annotation_runtime(ctx)
    now = datetime.now(UTC)

    with factory() as session:
        jobs = PostgresJobRepository(session)
        running = jobs.mark_running(job_id=identifier, now=now)
        session.commit()
    if running is None:
        return

    with factory() as session:
        datasets = runtime.repository(session)
        target = begin_annotation_execution(
            job=running,
            datasets=datasets,
            now=datetime.now(UTC),
        )
        if target is None:
            jobs = PostgresJobRepository(session)
            jobs.finish(
                job_id=running.id,
                status=JobStatus.SUPERSEDED.value,
                failure_code=None,
                now=datetime.now(UTC),
                expected_updated_at=running.updated_at,
            )
            session.commit()
            return
        session.commit()

    try:
        prepared = prepare_annotation_execution_copy(
            target=target,
            storage=runtime.storage(),
            backend=runtime.backend(),
            media_probe=runtime.media_probe(),
        )
        with factory() as session:
            datasets = runtime.repository(session)
            target = save_annotation_execution_copy(
                target=target,
                prepared=prepared,
                now=datetime.now(UTC),
                datasets=datasets,
            )
            session.commit()

        if target.execution.upstream_video_id is None:
            raise AnnotationBackendExecutionError("标注执行没有基座视频身份")
        clips = runtime.backend().split_video(
            video_id=target.execution.upstream_video_id,
            segments=target.submission.segments,
            mode=target.submission.mode,
        )
        if not clips:
            raise AnnotationBackendExecutionError("标注基座没有返回切片结果")
    except AnnotationBackendUnavailableError as error:
        _finish_annotation_failure(
            factory=factory,
            runtime=runtime,
            target=target,
            code="ANNOTATION_BACKEND_UNAVAILABLE",
            detail="标注基座暂时不可用",
            error=error,
        )
        return
    except AnnotationBackendExecutionError as error:
        _finish_annotation_failure(
            factory=factory,
            runtime=runtime,
            target=target,
            code="ANNOTATION_EXECUTION_FAILED",
            detail=str(error),
            error=error,
        )
        return
    except DatasetRefusedError as error:
        _finish_annotation_failure(
            factory=factory,
            runtime=runtime,
            target=target,
            code=error.code.value,
            detail=error.detail,
            error=error,
        )
        return
    except Exception as error:  # pragma: no cover - worker 安全兜底
        _finish_annotation_failure(
            factory=factory,
            runtime=runtime,
            target=target,
            code="ANNOTATION_EXECUTION_FAILED",
            detail="标注切片执行失败",
            error=error,
        )
        return

    try:
        with factory() as session:
            datasets = runtime.repository(session)
            completed = complete_annotation_execution(
                target=target,
                clips=clips,
                now=datetime.now(UTC),
                datasets=datasets,
            )
            jobs = PostgresJobRepository(session)
            finished = jobs.finish(
                job_id=running.id,
                status=JobStatus.SUCCEEDED.value,
                failure_code=None,
                now=datetime.now(UTC),
                expected_updated_at=running.updated_at,
            )
            if not finished:
                session.rollback()
                _logger.warning(
                    "job.dataset_annotation.lease_lost",
                    job_id=str(running.id),
                    execution_id=str(target.execution.id),
                )
                return
            session.commit()
    except DatasetRefusedError as error:
        _finish_annotation_failure(
            factory=factory,
            runtime=runtime,
            target=target,
            code=error.code.value,
            detail=error.detail,
            error=error,
        )
        return
    _logger.info(
        "job.dataset_annotation.finished" if finished else "job.dataset_annotation.lease_lost",
        job_id=str(running.id),
        member_id=str(running.member_id),
        execution_id=str(completed.id),
        status="succeeded" if finished else "lease_lost",
    )


def _finish_annotation_failure(
    *,
    factory: sessionmaker[Session],
    runtime: DatasetAnnotationRuntime,
    target: AnnotationExecutionTarget,
    code: str,
    detail: str,
    error: Exception,
) -> None:
    """在独立事务中保存失败候选并结案任务。"""
    with factory() as session:
        datasets = runtime.repository(session)
        try:
            failed = fail_annotation_execution(
                target=target,
                code=code,
                detail=detail,
                now=datetime.now(UTC),
                datasets=datasets,
            )
        except DatasetRefusedError:
            _logger.warning(
                "job.dataset_annotation.lease_lost",
                job_id=str(target.job.id),
                execution_id=str(target.execution.id),
                failure_code=code,
                error_type=type(error).__name__,
            )
            return
        jobs = PostgresJobRepository(session)
        finished = jobs.finish(
            job_id=target.job.id,
            status=JobStatus.FAILED.value,
            failure_code=code,
            now=datetime.now(UTC),
            expected_updated_at=target.job.updated_at,
        )
        if not finished:
            session.rollback()
            _logger.warning(
                "job.dataset_annotation.lease_lost",
                job_id=str(target.job.id),
                execution_id=str(target.execution.id),
                failure_code=code,
            )
            return
        session.commit()
    _logger.warning(
        "job.dataset_annotation.failed" if finished else "job.dataset_annotation.lease_lost",
        job_id=str(target.job.id),
        execution_id=str(failed.id),
        failure_code=code,
        error_type=type(error).__name__,
    )


async def cleanup_dataset_artifact_candidates(ctx: Mapping[str, Any]) -> None:
    """周期清理持久化记录的未发布候选对象。"""
    factory = _session_factory(ctx)
    runtime = _usage_runtime(ctx)
    with factory() as session:
        datasets = runtime.repository(session)
        candidates = tuple(datasets.list_artifact_cleanup_candidates(limit=100))
    if not candidates:
        return
    try:
        storage = runtime.storage()
    except Exception:
        _logger.exception("job.dataset_artifact.cleanup_storage_unavailable")
        return
    for artifact in candidates:
        keys = _artifact_candidate_keys(
            object_key=(artifact.object_key if artifact.status.value != "available" else None),
            manifest=artifact.manifest,
        )
        if not keys:
            continue
        if not all(
            _discard_artifact_candidate(storage=storage, object_key=object_key)
            for object_key in keys
        ):
            continue
        with factory() as session:
            datasets = runtime.repository(session)
            cleared = complete_artifact_candidate_cleanup(
                artifact_id=artifact.id,
                job_id=None,
                candidate_keys=keys,
                expected_updated_at=artifact.updated_at,
                now=datetime.now(UTC),
                datasets=datasets,
            )
            if not cleared:
                session.rollback()
                continue
            session.commit()
        _logger.info(
            "job.dataset_artifact.candidate_cleanup.finished",
            dataset_id=str(artifact.dataset_id),
            artifact_id=str(artifact.id),
            object_keys=keys,
            result="succeeded",
        )


async def dispatch_pending_jobs(ctx: Mapping[str, Any]) -> None:
    """先恢复过期执行租约，再补投 PostgreSQL outbox 中的任务。"""
    dispatcher = ctx["dispatcher"]
    if not isinstance(dispatcher, ArqJobDispatcher):
        return
    settings = cast(Settings, ctx["settings"])
    factory = _session_factory(ctx)
    stale_after_seconds = settings.media_probe_timeout_seconds + 300
    with factory() as session:
        repository = PostgresJobRepository(session)
        repository.recover_stale_running(
            now=datetime.now(UTC),
            stale_after_seconds=stale_after_seconds,
        )
        session.commit()
    with factory() as session:
        pending = PostgresJobRepository(session).pending(limit=100)
    for job in pending:
        await dispatcher.dispatch_async(job.id)
    if "usage_runtime" in ctx:
        await cleanup_dataset_artifact_candidates(ctx)


def build_worker(
    settings: Settings,
    *,
    engine: Engine,
    factory: sessionmaker[Session],
    runtime: DatasetValidationRuntime,
    usage_runtime: DatasetUsageRuntime,
    annotation_runtime: DatasetAnnotationRuntime,
) -> Worker:
    """构造带数据库、Redis 和显式数据集运行时的 worker。"""
    dispatcher = ArqJobDispatcher.from_settings(settings, session_factory=factory)
    if not isinstance(dispatcher, ArqJobDispatcher):
        raise RuntimeError("ARQ worker requires a valid Redis URL")
    return Worker(
        functions=[
            validate_dataset_job,
            check_dataset_usage_job,
            generate_dataset_artifact_job,
            prepare_annotation_context_job,
            annotate_dataset_job,
        ],
        cron_jobs=[
            cron(
                dispatch_pending_jobs,
                second={0, 30},
                run_at_startup=True,
                max_tries=1,
            )
        ],
        redis_settings=dispatcher.redis_settings,
        ctx={
            "settings": settings,
            "session_factory": factory,
            "engine": engine,
            "dispatcher": dispatcher,
            "dataset_runtime": runtime,
            "usage_runtime": usage_runtime,
            "annotation_runtime": annotation_runtime,
        },
        max_jobs=4,
        job_timeout=settings.media_probe_timeout_seconds + 300,
        max_tries=5,
    )


def _dataset_runtime(ctx: Mapping[str, Any]) -> DatasetValidationRuntime:
    return cast(DatasetValidationRuntime, ctx["dataset_runtime"])


def _usage_runtime(ctx: Mapping[str, Any]) -> DatasetUsageRuntime:
    return cast(DatasetUsageRuntime, ctx["usage_runtime"])


def _annotation_runtime(ctx: Mapping[str, Any]) -> DatasetAnnotationRuntime:
    value = ctx.get("annotation_runtime")
    if value is None:
        raise RuntimeError("标注 worker 未装配标注运行时")
    return cast(DatasetAnnotationRuntime, value)


def _session_factory(ctx: Mapping[str, Any]) -> sessionmaker[Session]:
    value = ctx["session_factory"]
    return cast(sessionmaker[Session], value)


def run_worker(
    environment: Mapping[str, str],
    *,
    runtime_factory: Callable[[Settings], DatasetValidationRuntime],
    usage_runtime_factory: Callable[[Settings], DatasetUsageRuntime],
    annotation_runtime_factory: Callable[[Settings], DatasetAnnotationRuntime],
) -> None:
    """解析给定环境并运行 worker；真实运行时由组合根工厂显式装配。"""
    settings = Settings.from_environment(environment)
    configure_logging(log_level=settings.log_level, stream=sys.stdout)
    engine = create_database_engine(settings)
    factory = session_factory(engine)
    runtime = runtime_factory(settings)
    usage_runtime = usage_runtime_factory(settings)
    annotation_runtime = annotation_runtime_factory(settings)
    worker = build_worker(
        settings,
        engine=engine,
        factory=factory,
        runtime=runtime,
        usage_runtime=usage_runtime,
        annotation_runtime=annotation_runtime,
    )
    try:
        worker.run()
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit("请使用 python -m factory_sop.worker_entrypoint 启动 worker")
