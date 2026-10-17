"""真实 Redis/ARQ 相邻 cron 不被 60 秒 in-progress 保留期吞掉。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from _integration_support import RedisServer, settings_for
from arq.connections import RedisSettings, create_pool
from redis import Redis
from sqlalchemy import Engine
from sqlalchemy.orm import Session
from test_retention_sweep_postgres import NOW, _counts
from test_retention_sweep_postgres import committed_session as committed_session

from factory_sop.job.adapters.worker import build_worker
from factory_sop.job.api import JobType
from factory_sop.persistence import session_factory
from factory_sop.worker_entrypoint import retention_sweep_runner


def test_native_arq_cron_executes_subminute_cycles(
    committed_session: Session,
    engine: Engine,
    dataset_storage_root: Path,
    redis_server: RedisServer,
    redis_client: Redis,
) -> None:
    assert redis_client.ping()
    settings = settings_for(
        engine,
        storage_root=dataset_storage_root,
        redis_url=redis_server.url,
    ).model_copy(update={"retention_sweep_interval_seconds": 10})
    factory = session_factory(engine)

    async def exercise() -> None:
        # 数据集 ports 不参与本测试；任务取生产 build_worker 注册的回收 cron。
        worker = build_worker(
            settings,
            engine=engine,
            factory=factory,
            runtime=cast(Any, object()),
            usage_runtime=cast(Any, object()),
            artifact_executor=cast(Any, object()),
            annotation_runtime=cast(Any, object()),
            training_executors={
                JobType.DATASET_DDM_TRAINING: cast(Any, object()),
                JobType.DATASET_VLM_TRAINING: cast(Any, object()),
            },
            current_caller_resolver=cast(Any, object()),
            retention_sweep=retention_sweep_runner(settings, factory),
        )
        worker.cron_jobs = [
            job for job in worker.cron_jobs if job.name == "cron:run_retention_sweep_cron"
        ]
        worker._pool = await create_pool(RedisSettings.from_dsn(redis_server.url))
        try:
            now = datetime.now(UTC).replace(microsecond=0)
            worker.cron_jobs[0].next_run = now - timedelta(seconds=2)
            with patch("factory_sop.worker_entrypoint.datetime") as clock:
                clock.now.side_effect = [NOW, NOW + timedelta(seconds=10)]
                for _ in range(2):
                    # 两个真实已到期 cron；原固定 job_id 的进度 key 在第二次仍存活。
                    await worker.run_cron(now, 0.5)
                    job_ids = await worker.pool.zrange(worker.queue_name, 0, -1)
                    await worker.start_jobs(job_ids)
                    await asyncio.gather(*worker.tasks.values())
            assert worker.jobs_failed == 0
            assert worker.jobs_complete == 2
        finally:
            await worker.close()

    asyncio.run(exercise())
    assert _counts(engine, "retention_sweep") == 2
