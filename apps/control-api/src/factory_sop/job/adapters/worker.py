"""训练数据异步任务的 ARQ worker 与提交后 outbox 补投任务。"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from threading import Event, Lock
from typing import Any, assert_never, cast
from uuid import UUID

from arq import Worker, cron
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from factory_sop.dataset.api import (
    AnnotationBackend,
    AnnotationBackendExecutionError,
    AnnotationBackendUnavailableError,
    AnnotationCleanupPendingError,
    AnnotationContextPreparationTarget,
    AnnotationDataVolumeUnavailableError,
    AnnotationExecutionTarget,
    ArtifactExecutionOutcome,
    ArtifactExecutionResult,
    DatasetAnnotationRuntime,
    DatasetArtifactExecutor,
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
    begin_usage_check,
    begin_video_validation,
    clear_annotation_context_cleanup_candidate,
    clear_annotation_execution_cleanup_candidate,
    complete_annotation_context_preparation,
    complete_annotation_execution,
    complete_usage_check,
    fail_annotation_context_preparation,
    fail_annotation_execution,
    fail_usage_check,
    prepare_annotation_context_copy,
    prepare_annotation_execution_copy,
    record_annotation_context_cleanup_candidate,
    record_annotation_execution_cleanup_candidate,
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

_BLOCKING_JOB_LIMIT = 4


class _ExecutionFence:
    """把取消请求与最终事务提交线性化到同一发布权边界。"""

    def __init__(self) -> None:
        self._cancelled = Event()
        self._publication_lock = Lock()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def request_cancel(self) -> None:
        """立即撤销后续发布权。"""
        self._cancelled.set()

    def wait_until_quiescent(self) -> None:
        """等待已经进入发布临界区的提交完成。"""
        with self._publication_lock:
            return

    def commit(self, session: Session) -> bool:
        """与取消请求共享临界区，避免检查与提交之间出现竞态。"""
        with self._publication_lock:
            if self._cancelled.is_set():
                session.rollback()
                return False
            session.commit()
            return True


async def _run_blocking_job(
    job: Callable[[Mapping[str, Any], str, _ExecutionFence], None],
    ctx: Mapping[str, Any],
    job_id: str,
) -> None:
    """在线程中执行同步业务，并以物理执行生命周期占用并发槽位。"""
    slots = _blocking_job_slots(ctx)
    loop = asyncio.get_running_loop()
    try:
        await slots.acquire()
    except asyncio.CancelledError:
        await asyncio.shield(loop.run_in_executor(None, _restore_unstarted_job, ctx, job_id))
        raise
    fence = _ExecutionFence()
    try:
        physical = loop.run_in_executor(None, job, ctx, job_id, fence)
    except BaseException:
        slots.release()
        raise
    physical.add_done_callback(lambda _future: slots.release())
    try:
        await asyncio.shield(physical)
    except asyncio.CancelledError:
        fence.request_cancel()
        await asyncio.shield(loop.run_in_executor(None, fence.wait_until_quiescent))
        raise


def _restore_unstarted_job(ctx: Mapping[str, Any], job_id: str) -> None:
    """恢复尚未进入 blocking execution 的投递；事务完整位于维护线程。"""
    identifier = UUID(job_id)
    factory = _session_factory(ctx)
    with factory() as session:
        restored = PostgresJobRepository(session).restore_unstarted(
            job_id=identifier,
            now=datetime.now(UTC),
        )
        session.commit()
    if restored:
        _logger.warning("job.execution_slot_wait_cancelled", job_id=job_id)


def _commit_if_active(session: Session, fence: _ExecutionFence) -> bool:
    """仅当当前逻辑执行仍有发布权时提交事务。"""
    return fence.commit(session)


def _annotation_failure_details(
    error: Exception,
    *,
    default_detail: str,
) -> tuple[str, str]:
    """保持标注准备阶段既有的失败分类。"""
    if isinstance(error, DatasetRefusedError):
        return error.code.value, error.detail
    if isinstance(error, AnnotationBackendUnavailableError):
        return "ANNOTATION_BACKEND_UNAVAILABLE", "标注基座暂时不可用"
    if isinstance(error, AnnotationBackendExecutionError):
        return "ANNOTATION_EXECUTION_FAILED", str(error)
    return "ANNOTATION_EXECUTION_FAILED", default_detail


def _record_context_cleanup_candidate(
    *,
    factory: sessionmaker[Session],
    runtime: DatasetAnnotationRuntime,
    target: AnnotationContextPreparationTarget,
    data_id: str,
    code: str | None,
    detail: str | None,
) -> bool:
    """持久化清理维护元数据；它不发布标注业务结果，不受取消发布权约束。"""
    try:
        with factory() as session:
            datasets = runtime.repository(session)
            record_annotation_context_cleanup_candidate(
                target=target,
                data_id=data_id,
                code=code,
                detail=detail,
                datasets=datasets,
            )
            session.commit()
    except Exception:
        _logger.exception(
            "job.dataset_annotation_preparation.cleanup_candidate_record_failed",
            job_id=str(target.job.id),
            context_id=str(target.context.id),
            data_id=data_id,
        )
        return False
    _logger.warning(
        "job.dataset_annotation_preparation.cleanup_candidate_recorded",
        job_id=str(target.job.id),
        context_id=str(target.context.id),
        data_id=data_id,
    )
    return True


def _record_execution_cleanup_candidate(
    *,
    factory: sessionmaker[Session],
    runtime: DatasetAnnotationRuntime,
    target: AnnotationExecutionTarget,
    data_id: str,
    code: str | None,
    detail: str | None,
) -> bool:
    """持久化清理维护元数据；它不发布标注业务结果，不受取消发布权约束。"""
    try:
        with factory() as session:
            datasets = runtime.repository(session)
            record_annotation_execution_cleanup_candidate(
                target=target,
                data_id=data_id,
                code=code,
                detail=detail,
                now=datetime.now(UTC),
                datasets=datasets,
            )
            session.commit()
    except Exception:
        _logger.exception(
            "job.dataset_annotation.cleanup_candidate_record_failed",
            job_id=str(target.job.id),
            execution_id=str(target.execution.id),
            data_id=data_id,
        )
        return False
    _logger.warning(
        "job.dataset_annotation.cleanup_candidate_recorded",
        job_id=str(target.job.id),
        execution_id=str(target.execution.id),
        data_id=data_id,
    )
    return True


def _discard_context_copy_or_record(
    *,
    backend: AnnotationBackend,
    factory: sessionmaker[Session],
    runtime: DatasetAnnotationRuntime,
    target: AnnotationContextPreparationTarget,
    data_id: str,
    code: str | None,
    detail: str | None,
) -> bool:
    """删除上下文工作副本；未确认删除时把身份留给 stale recovery。"""
    try:
        backend.discard_prepared_video(data_id=data_id)
    except Exception:
        recorded = _record_context_cleanup_candidate(
            factory=factory,
            runtime=runtime,
            target=target,
            data_id=data_id,
            code=code,
            detail=detail,
        )
        _logger.exception(
            "job.dataset_annotation_preparation.cleanup_failed",
            data_id=data_id,
            cleanup_candidate_recorded=recorded,
        )
        return False
    return True


def _discard_execution_copy_or_record(
    *,
    backend: AnnotationBackend,
    factory: sessionmaker[Session],
    runtime: DatasetAnnotationRuntime,
    target: AnnotationExecutionTarget,
    data_id: str,
    code: str | None,
    detail: str | None,
) -> bool:
    """删除执行工作副本；未确认删除时把身份留给 stale recovery。"""
    try:
        backend.discard_prepared_video(data_id=data_id)
    except Exception:
        recorded = _record_execution_cleanup_candidate(
            factory=factory,
            runtime=runtime,
            target=target,
            data_id=data_id,
            code=code,
            detail=detail,
        )
        _logger.exception(
            "job.dataset_annotation.cleanup_failed",
            data_id=data_id,
            cleanup_candidate_recorded=recorded,
        )
        return False
    return True


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
    """隔离同步视频校验，不阻塞 ARQ 事件循环。"""
    await _run_blocking_job(_validate_dataset_job, ctx, job_id)


def _validate_dataset_job(ctx: Mapping[str, Any], job_id: str, fence: _ExecutionFence) -> None:
    """按 job id 幂等执行视频校验；Redis payload 不携带任何凭据或对象地址。"""
    if fence.cancelled:
        return
    identifier = UUID(job_id)
    factory = _session_factory(ctx)
    runtime = _dataset_runtime(ctx)
    now = datetime.now(UTC)

    with factory() as session:
        jobs = PostgresJobRepository(session)
        running = jobs.mark_running(job_id=identifier, now=now)
        if not _commit_if_active(session, fence):
            return
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
            _commit_if_active(session, fence)
            return
        if not _commit_if_active(session, fence):
            return

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
        if fence.cancelled:
            result_session.rollback()
            return
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
        if not finished:
            result_session.rollback()
            _logger.info(
                "job.dataset_validation.lease_lost",
                job_id=str(running.id),
                member_id=str(running.member_id),
                attempt_id=str(running.attempt_id),
                status="lease_lost",
            )
            return
        if not _commit_if_active(result_session, fence):
            return
        _logger.info(
            "job.dataset_validation.finished",
            job_id=str(running.id),
            member_id=str(running.member_id),
            attempt_id=str(running.attempt_id),
            status=final_status,
        )
    finally:
        result_session.close()


async def check_dataset_usage_job(ctx: Mapping[str, Any], job_id: str) -> None:
    """隔离同步用途检查，不阻塞 ARQ 事件循环。"""
    await _run_blocking_job(_check_dataset_usage_job, ctx, job_id)


def _check_dataset_usage_job(ctx: Mapping[str, Any], job_id: str, fence: _ExecutionFence) -> None:
    """在事务外复核冻结用途输入，并把检查结果写回中心。"""
    if fence.cancelled:
        return
    identifier = UUID(job_id)
    factory = _session_factory(ctx)
    runtime = _usage_runtime(ctx)
    with factory() as session:
        jobs = PostgresJobRepository(session)
        running = jobs.mark_running(job_id=identifier, now=datetime.now(UTC))
        if not _commit_if_active(session, fence):
            return
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
            _commit_if_active(session, fence)
            return
        if not _commit_if_active(session, fence):
            return

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
        if fence.cancelled:
            return
    except ObjectStorageUnavailableError as error:
        _finish_usage_check_failure(
            factory=factory,
            runtime=runtime,
            target=target,
            code="USAGE_STORAGE_UNAVAILABLE",
            detail="训练素材存储暂时不可用，请稍后重试",
            error=error,
            fence=fence,
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
            fence=fence,
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
            fence=fence,
        )
        return
    except Exception as error:  # pragma: no cover - worker 安全兜底
        _logger.exception(
            "job.dataset_usage_check.unexpected_failure",
            job_id=str(target.job.id),
            check_id=str(target.check.id),
            usage_kind=target.check.kind.value,
            error_type=type(error).__name__,
        )
        _finish_usage_check_failure(
            factory=factory,
            runtime=runtime,
            target=target,
            code="USAGE_CHECK_EXECUTION_FAILED",
            detail="用途检查执行失败",
            error=error,
            fence=fence,
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
            if not _commit_if_active(result_session, fence):
                return
    except DatasetRefusedError as error:
        _finish_usage_check_failure(
            factory=factory,
            runtime=runtime,
            target=target,
            code=error.code.value,
            detail=error.detail,
            error=error,
            fence=fence,
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
            fence=fence,
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
    fence: _ExecutionFence,
) -> None:
    """在独立事务中记录用途检查 worker 失败。"""
    if fence.cancelled:
        return
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
        if not _commit_if_active(session, fence):
            return
    _logger.warning(
        "job.dataset_usage_check.failed",
        job_id=str(target.job.id),
        failure_code=code,
        error_type=type(error).__name__,
    )


async def generate_dataset_artifact_job(ctx: Mapping[str, Any], job_id: str) -> None:
    """隔离同步制品执行，不阻塞 ARQ 事件循环。"""
    await _run_blocking_job(_generate_dataset_artifact_job, ctx, job_id)


def _generate_dataset_artifact_job(
    ctx: Mapping[str, Any], job_id: str, fence: _ExecutionFence
) -> None:
    """领取通用任务，并把完整制品生命周期委托给 dataset owner。"""
    if fence.cancelled:
        return
    identifier = UUID(job_id)
    factory = _session_factory(ctx)
    with factory() as session:
        jobs = PostgresJobRepository(session)
        running = jobs.mark_running(job_id=identifier, now=datetime.now(UTC))
        if not _commit_if_active(session, fence):
            return
    if running is None:
        return

    def finish_job(session: object, result: ArtifactExecutionResult) -> bool:
        if fence.cancelled:
            return False
        match result.outcome:
            case ArtifactExecutionOutcome.SUCCEEDED:
                status = JobStatus.SUCCEEDED.value
            case ArtifactExecutionOutcome.FAILED:
                status = JobStatus.FAILED.value
            case ArtifactExecutionOutcome.SUPERSEDED:
                status = JobStatus.SUPERSEDED.value
            case _:
                raise RuntimeError("dataset artifact executor requested a non-terminal job finish")
        return PostgresJobRepository(cast(Session, session)).finish(
            job_id=running.id,
            status=status,
            failure_code=result.failure_code,
            now=datetime.now(UTC),
            expected_updated_at=running.updated_at,
        )

    def commit_transaction(session: object) -> bool:
        return _commit_if_active(cast(Session, session), fence)

    _artifact_executor(ctx).execute(
        job=running,
        finish_job=finish_job,
        commit_transaction=commit_transaction,
    )


async def prepare_annotation_context_job(ctx: Mapping[str, Any], job_id: str) -> None:
    """隔离同步标注上下文准备，不阻塞 ARQ 事件循环。"""
    await _run_blocking_job(_prepare_annotation_context_job, ctx, job_id)


def _prepare_annotation_context_job(
    ctx: Mapping[str, Any], job_id: str, fence: _ExecutionFence
) -> None:
    """在事务外准备标注上下文基座副本，并短事务登记真实身份。"""
    if fence.cancelled:
        return
    identifier = UUID(job_id)
    factory = _session_factory(ctx)
    runtime = _annotation_runtime(ctx)
    now = datetime.now(UTC)

    with factory() as session:
        jobs = PostgresJobRepository(session)
        running = jobs.mark_running(job_id=identifier, now=now)
        if not _commit_if_active(session, fence):
            return
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
            _commit_if_active(session, fence)
            return
        if not _commit_if_active(session, fence):
            return

    try:
        backend = runtime.backend()
        cleanup_data_id = (
            target.context.upstream_data_id if target.context.upstream_video_id is None else None
        )
        if cleanup_data_id is not None:
            try:
                backend.discard_prepared_video(data_id=cleanup_data_id)
            except Exception:
                _logger.exception(
                    "job.dataset_annotation_preparation.cleanup_retry_failed",
                    job_id=str(running.id),
                    context_id=str(target.context.id),
                    data_id=cleanup_data_id,
                )
                return
            if target.context.preparation_failure_code is not None:
                try:
                    _finish_context_preparation_failure(
                        factory=factory,
                        runtime=runtime,
                        target=target,
                        code=target.context.preparation_failure_code,
                        detail=target.context.preparation_failure_detail or "标注媒体准备失败",
                        error=RuntimeError("annotation cleanup candidate resolved"),
                        fence=fence,
                    )
                except Exception:
                    _logger.exception(
                        "job.dataset_annotation_preparation.cleanup_finalize_unknown",
                        job_id=str(running.id),
                        context_id=str(target.context.id),
                        data_id=cleanup_data_id,
                    )
                return
            try:
                with factory() as session:
                    datasets = runtime.repository(session)
                    target = clear_annotation_context_cleanup_candidate(
                        target=target,
                        datasets=datasets,
                    )
                    if not _commit_if_active(session, fence):
                        return
            except Exception:
                _logger.exception(
                    "job.dataset_annotation_preparation.cleanup_clear_unknown",
                    job_id=str(running.id),
                    context_id=str(target.context.id),
                    data_id=cleanup_data_id,
                )
                return

        prepared = prepare_annotation_context_copy(
            context=target.context,
            member=target.member,
            actions=target.actions,
            storage=runtime.storage(),
            backend=backend,
            media_probe=runtime.media_probe(),
        )
        if fence.cancelled:
            _discard_context_copy_or_record(
                backend=backend,
                factory=factory,
                runtime=runtime,
                target=target,
                data_id=prepared.prepared.data_id,
                code=None,
                detail=None,
            )
            return
    except AnnotationCleanupPendingError as error:
        code, detail = _annotation_failure_details(
            error.failure,
            default_detail="标注媒体准备失败",
        )
        _record_context_cleanup_candidate(
            factory=factory,
            runtime=runtime,
            target=target,
            data_id=error.data_id,
            code=code,
            detail=detail,
        )
        return
    except AnnotationBackendUnavailableError as error:
        _finish_context_preparation_failure(
            factory=factory,
            runtime=runtime,
            target=target,
            code="ANNOTATION_BACKEND_UNAVAILABLE",
            detail="标注基座暂时不可用",
            error=error,
            fence=fence,
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
            fence=fence,
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
            fence=fence,
        )
        return

    try:
        cleanup_reason: str | None = None
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
                cleanup_reason = "lease_lost"
            elif not _commit_if_active(session, fence):
                cleanup_reason = "cancelled"
        if cleanup_reason is not None:
            _discard_context_copy_or_record(
                backend=backend,
                factory=factory,
                runtime=runtime,
                target=target,
                data_id=prepared.prepared.data_id,
                code=None,
                detail=None,
            )
            if cleanup_reason == "lease_lost":
                _logger.warning(
                    "job.dataset_annotation_preparation.lease_lost",
                    job_id=str(running.id),
                    context_id=str(target.context.id),
                )
            return
    except DatasetRefusedError as error:
        if not _discard_context_copy_or_record(
            backend=backend,
            factory=factory,
            runtime=runtime,
            target=target,
            data_id=prepared.prepared.data_id,
            code=error.code.value,
            detail=error.detail,
        ):
            return
        _finish_context_preparation_failure(
            factory=factory,
            runtime=runtime,
            target=target,
            code=error.code.value,
            detail=error.detail,
            error=error,
            fence=fence,
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
    fence: _ExecutionFence,
) -> None:
    """在独立事务中保存上下文准备失败并结案任务。"""
    if fence.cancelled:
        return
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
        if not _commit_if_active(session, fence):
            return
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
    """隔离同步标注执行，不阻塞 ARQ 事件循环。"""
    await _run_blocking_job(_annotate_dataset_job, ctx, job_id)


def _annotate_dataset_job(ctx: Mapping[str, Any], job_id: str, fence: _ExecutionFence) -> None:
    """按执行代次调用复用的标注基座，并追加候选结果。"""
    if fence.cancelled:
        return
    identifier = UUID(job_id)
    factory = _session_factory(ctx)
    runtime = _annotation_runtime(ctx)
    now = datetime.now(UTC)

    with factory() as session:
        jobs = PostgresJobRepository(session)
        running = jobs.mark_running(job_id=identifier, now=now)
        if not _commit_if_active(session, fence):
            return
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
            _commit_if_active(session, fence)
            return
        if not _commit_if_active(session, fence):
            return

    prepared = None
    copy_persisted = target.execution.upstream_video_id is not None
    try:
        backend = runtime.backend()
        cleanup_data_id = (
            target.execution.upstream_data_id
            if target.execution.upstream_video_id is None
            else None
        )
        if cleanup_data_id is not None:
            try:
                backend.discard_prepared_video(data_id=cleanup_data_id)
            except Exception:
                _logger.exception(
                    "job.dataset_annotation.cleanup_retry_failed",
                    job_id=str(running.id),
                    execution_id=str(target.execution.id),
                    data_id=cleanup_data_id,
                )
                return
            if target.execution.failure_code is not None:
                try:
                    _finish_annotation_failure(
                        factory=factory,
                        runtime=runtime,
                        target=target,
                        code=target.execution.failure_code,
                        detail=target.execution.failure_detail or "标注切片执行失败",
                        error=RuntimeError("annotation cleanup candidate resolved"),
                        fence=fence,
                    )
                except Exception:
                    _logger.exception(
                        "job.dataset_annotation.cleanup_finalize_unknown",
                        job_id=str(running.id),
                        execution_id=str(target.execution.id),
                        data_id=cleanup_data_id,
                    )
                return
            try:
                with factory() as session:
                    datasets = runtime.repository(session)
                    target = clear_annotation_execution_cleanup_candidate(
                        target=target,
                        now=datetime.now(UTC),
                        datasets=datasets,
                    )
                    if not _commit_if_active(session, fence):
                        return
            except Exception:
                _logger.exception(
                    "job.dataset_annotation.cleanup_clear_unknown",
                    job_id=str(running.id),
                    execution_id=str(target.execution.id),
                    data_id=cleanup_data_id,
                )
                return

        if not copy_persisted:
            cleanup_target = target
            prepared = prepare_annotation_execution_copy(
                target=target,
                storage=runtime.storage(),
                backend=backend,
                media_probe=runtime.media_probe(),
            )
            if fence.cancelled:
                _discard_execution_copy_or_record(
                    backend=backend,
                    factory=factory,
                    runtime=runtime,
                    target=target,
                    data_id=prepared.prepared.data_id,
                    code=None,
                    detail=None,
                )
                return
            cleanup_after_save = False
            with factory() as session:
                datasets = runtime.repository(session)
                target = save_annotation_execution_copy(
                    target=target,
                    prepared=prepared,
                    now=datetime.now(UTC),
                    datasets=datasets,
                )
                try:
                    if not _commit_if_active(session, fence):
                        cleanup_after_save = True
                except Exception:
                    _logger.exception(
                        "job.dataset_annotation.copy_commit_unknown",
                        job_id=str(running.id),
                        execution_id=str(target.execution.id),
                        data_id=prepared.prepared.data_id,
                        result="commit_unknown",
                    )
                    return
            if cleanup_after_save:
                _discard_execution_copy_or_record(
                    backend=backend,
                    factory=factory,
                    runtime=runtime,
                    target=cleanup_target,
                    data_id=prepared.prepared.data_id,
                    code=None,
                    detail=None,
                )
                return
            copy_persisted = True

        if target.execution.upstream_video_id is None:
            raise AnnotationBackendExecutionError("标注执行没有基座视频身份")
        clips = backend.split_video(
            video_id=target.execution.upstream_video_id,
            segments=target.submission.segments,
            mode=target.submission.mode,
        )
        if fence.cancelled:
            return
        if not clips:
            raise AnnotationBackendExecutionError("标注基座没有返回切片结果")
    except AnnotationCleanupPendingError as error:
        code, detail = _annotation_failure_details(
            error.failure,
            default_detail="标注切片执行失败",
        )
        _record_execution_cleanup_candidate(
            factory=factory,
            runtime=runtime,
            target=target,
            data_id=error.data_id,
            code=code,
            detail=detail,
        )
        return
    except AnnotationBackendUnavailableError as error:
        if (
            prepared is not None
            and not copy_persisted
            and not _discard_execution_copy_or_record(
                backend=backend,
                factory=factory,
                runtime=runtime,
                target=target,
                data_id=prepared.prepared.data_id,
                code="ANNOTATION_BACKEND_UNAVAILABLE",
                detail="标注基座暂时不可用",
            )
        ):
            return
        _finish_annotation_failure(
            factory=factory,
            runtime=runtime,
            target=target,
            code="ANNOTATION_BACKEND_UNAVAILABLE",
            detail="标注基座暂时不可用",
            error=error,
            fence=fence,
        )
        return
    except AnnotationBackendExecutionError as error:
        if (
            prepared is not None
            and not copy_persisted
            and not _discard_execution_copy_or_record(
                backend=backend,
                factory=factory,
                runtime=runtime,
                target=target,
                data_id=prepared.prepared.data_id,
                code="ANNOTATION_EXECUTION_FAILED",
                detail=str(error),
            )
        ):
            return
        _finish_annotation_failure(
            factory=factory,
            runtime=runtime,
            target=target,
            code="ANNOTATION_EXECUTION_FAILED",
            detail=str(error),
            error=error,
            fence=fence,
        )
        return
    except DatasetRefusedError as error:
        if (
            prepared is not None
            and not copy_persisted
            and not _discard_execution_copy_or_record(
                backend=backend,
                factory=factory,
                runtime=runtime,
                target=target,
                data_id=prepared.prepared.data_id,
                code=error.code.value,
                detail=error.detail,
            )
        ):
            return
        _finish_annotation_failure(
            factory=factory,
            runtime=runtime,
            target=target,
            code=error.code.value,
            detail=error.detail,
            error=error,
            fence=fence,
        )
        return
    except Exception as error:  # pragma: no cover - worker 安全兜底
        if (
            prepared is not None
            and not copy_persisted
            and not _discard_execution_copy_or_record(
                backend=backend,
                factory=factory,
                runtime=runtime,
                target=target,
                data_id=prepared.prepared.data_id,
                code="ANNOTATION_EXECUTION_FAILED",
                detail="标注切片执行失败",
            )
        ):
            return
        _finish_annotation_failure(
            factory=factory,
            runtime=runtime,
            target=target,
            code="ANNOTATION_EXECUTION_FAILED",
            detail="标注切片执行失败",
            error=error,
            fence=fence,
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
            if not _commit_if_active(session, fence):
                return
    except DatasetRefusedError as error:
        _finish_annotation_failure(
            factory=factory,
            runtime=runtime,
            target=target,
            code=error.code.value,
            detail=error.detail,
            error=error,
            fence=fence,
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
    fence: _ExecutionFence,
) -> None:
    """在独立事务中保存失败候选并结案任务。"""
    if fence.cancelled:
        return
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
        if not _commit_if_active(session, fence):
            return
    _logger.warning(
        "job.dataset_annotation.failed" if finished else "job.dataset_annotation.lease_lost",
        job_id=str(target.job.id),
        execution_id=str(failed.id),
        failure_code=code,
        error_type=type(error).__name__,
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
    if "artifact_executor" in ctx:
        _artifact_executor(ctx).cleanup_candidates()


def build_worker(
    settings: Settings,
    *,
    engine: Engine,
    factory: sessionmaker[Session],
    runtime: DatasetValidationRuntime,
    usage_runtime: DatasetUsageRuntime,
    artifact_executor: DatasetArtifactExecutor,
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
            "artifact_executor": artifact_executor,
            "annotation_runtime": annotation_runtime,
            "blocking_job_slots": asyncio.Semaphore(_BLOCKING_JOB_LIMIT),
        },
        max_jobs=_BLOCKING_JOB_LIMIT,
        job_timeout=settings.media_probe_timeout_seconds + 300,
        max_tries=5,
        health_check_interval=settings.worker_health_check_interval_seconds,
    )


def _blocking_job_slots(ctx: Mapping[str, Any]) -> asyncio.Semaphore:
    return cast(asyncio.Semaphore, ctx["blocking_job_slots"])


def _dataset_runtime(ctx: Mapping[str, Any]) -> DatasetValidationRuntime:
    return cast(DatasetValidationRuntime, ctx["dataset_runtime"])


def _usage_runtime(ctx: Mapping[str, Any]) -> DatasetUsageRuntime:
    return cast(DatasetUsageRuntime, ctx["usage_runtime"])


def _artifact_executor(ctx: Mapping[str, Any]) -> DatasetArtifactExecutor:
    return cast(DatasetArtifactExecutor, ctx["artifact_executor"])


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
    artifact_executor_factory: Callable[
        [Settings, sessionmaker[Session]],
        DatasetArtifactExecutor,
    ],
    runtime_factory: Callable[[Settings], DatasetValidationRuntime],
    usage_runtime_factory: Callable[[Settings], DatasetUsageRuntime],
    annotation_runtime_factory: Callable[[Settings], DatasetAnnotationRuntime],
) -> None:
    """解析给定环境并运行 worker；真实运行时由组合根工厂显式装配。"""
    settings = Settings.from_environment(environment)
    configure_logging(log_level=settings.log_level, stream=sys.stdout)
    engine = create_database_engine(settings)
    factory = session_factory(engine)
    artifact_executor = artifact_executor_factory(settings, factory)
    runtime = runtime_factory(settings)
    usage_runtime = usage_runtime_factory(settings)
    annotation_runtime = annotation_runtime_factory(settings)
    worker = build_worker(
        settings,
        engine=engine,
        factory=factory,
        runtime=runtime,
        usage_runtime=usage_runtime,
        artifact_executor=artifact_executor,
        annotation_runtime=annotation_runtime,
    )
    try:
        worker.run()
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit("请使用 python -m factory_sop.worker_entrypoint 启动 worker")
