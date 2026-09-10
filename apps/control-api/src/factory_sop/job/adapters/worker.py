"""训练视频校验的 ARQ worker 与提交后 outbox 补投任务。"""

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
    DatasetValidationRuntime,
    MemberStatus,
    begin_video_validation,
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
) -> Worker:
    """构造带数据库、Redis 和显式数据集运行时的 worker。"""
    dispatcher = ArqJobDispatcher.from_settings(settings, session_factory=factory)
    if not isinstance(dispatcher, ArqJobDispatcher):
        raise RuntimeError("ARQ worker requires a valid Redis URL")
    return Worker(
        functions=[validate_dataset_job],
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
        },
        max_jobs=4,
        job_timeout=settings.media_probe_timeout_seconds + 300,
        max_tries=5,
    )


def _dataset_runtime(ctx: Mapping[str, Any]) -> DatasetValidationRuntime:
    return cast(DatasetValidationRuntime, ctx["dataset_runtime"])


def _session_factory(ctx: Mapping[str, Any]) -> sessionmaker[Session]:
    value = ctx["session_factory"]
    return cast(sessionmaker[Session], value)


def run_worker(
    environment: Mapping[str, str],
    *,
    runtime_factory: Callable[[Settings], DatasetValidationRuntime],
) -> None:
    """解析给定环境并运行 worker；真实运行时由组合根工厂显式装配。"""
    settings = Settings.from_environment(environment)
    configure_logging(log_level=settings.log_level, stream=sys.stdout)
    engine = create_database_engine(settings)
    factory = session_factory(engine)
    runtime = runtime_factory(settings)
    worker = build_worker(settings, engine=engine, factory=factory, runtime=runtime)
    try:
        worker.run()
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit("请使用 python -m factory_sop.worker_entrypoint 启动 worker")
