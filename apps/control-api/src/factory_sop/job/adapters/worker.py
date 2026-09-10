"""训练数据异步任务的 ARQ worker 与提交后 outbox 补投任务。"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from arq import Worker, cron
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from factory_sop.dataset.api import (
    AnnotationBackendExecutionError,
    AnnotationBackendUnavailableError,
    AnnotationContextPreparationTarget,
    AnnotationExecutionTarget,
    DatasetAnnotationRuntime,
    DatasetRefusedError,
    DatasetValidationRuntime,
    MemberStatus,
    begin_annotation_context_preparation,
    begin_annotation_execution,
    begin_video_validation,
    complete_annotation_context_preparation,
    complete_annotation_execution,
    fail_annotation_context_preparation,
    fail_annotation_execution,
    prepare_annotation_context_copy,
    prepare_annotation_execution_copy,
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


def build_worker(
    settings: Settings,
    *,
    engine: Engine,
    factory: sessionmaker[Session],
    runtime: DatasetValidationRuntime,
    annotation_runtime: DatasetAnnotationRuntime,
) -> Worker:
    """构造带数据库、Redis 和显式数据集运行时的 worker。"""
    dispatcher = ArqJobDispatcher.from_settings(settings, session_factory=factory)
    if not isinstance(dispatcher, ArqJobDispatcher):
        raise RuntimeError("ARQ worker requires a valid Redis URL")
    return Worker(
        functions=[
            validate_dataset_job,
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
            "annotation_runtime": annotation_runtime,
        },
        max_jobs=4,
        job_timeout=settings.media_probe_timeout_seconds + 300,
        max_tries=5,
    )


def _dataset_runtime(ctx: Mapping[str, Any]) -> DatasetValidationRuntime:
    return cast(DatasetValidationRuntime, ctx["dataset_runtime"])


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
    annotation_runtime_factory: Callable[[Settings], DatasetAnnotationRuntime],
) -> None:
    """解析给定环境并运行 worker；真实运行时由组合根工厂显式装配。"""
    settings = Settings.from_environment(environment)
    configure_logging(log_level=settings.log_level, stream=sys.stdout)
    engine = create_database_engine(settings)
    factory = session_factory(engine)
    runtime = runtime_factory(settings)
    annotation_runtime = annotation_runtime_factory(settings)
    worker = build_worker(
        settings,
        engine=engine,
        factory=factory,
        runtime=runtime,
        annotation_runtime=annotation_runtime,
    )
    try:
        worker.run()
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit("请使用 python -m factory_sop.worker_entrypoint 启动 worker")
