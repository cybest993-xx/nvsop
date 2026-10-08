from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from arq.connections import RedisSettings

from factory_sop.job.adapters import worker as worker_module
from factory_sop.job.adapters.dispatcher import ArqJobDispatcher
from factory_sop.job.api import JobType


class _Session:
    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def commit(self) -> None:
        return None


class _Factory:
    def __call__(self) -> _Session:
        return _Session()


def test_stale_recovery_preserves_existing_worker_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovered: list[tuple[JobType, int]] = []

    class FakeRepository:
        def __init__(self, session: object) -> None:
            assert isinstance(session, _Session)

        def recover_stale_running(
            self, *, job_type: JobType, now: object, stale_after_seconds: int
        ) -> int:
            del now
            recovered.append((job_type, stale_after_seconds))
            return 0

        def pending(self, *, limit: int) -> tuple[object, ...]:
            assert limit == 100
            return ()

    class FakeDispatcher:
        async def dispatch_async(self, job_id: object) -> None:
            raise AssertionError(f"no pending jobs expected: {job_id}")

    monkeypatch.setattr(worker_module, "PostgresJobRepository", FakeRepository)
    monkeypatch.setattr(worker_module, "ArqJobDispatcher", FakeDispatcher)
    settings = SimpleNamespace(media_probe_timeout_seconds=30)
    ctx = {
        "dispatcher": FakeDispatcher(),
        "settings": settings,
        "session_factory": _Factory(),
    }

    asyncio.run(worker_module.dispatch_pending_jobs(ctx))

    assert dict(recovered) == dict.fromkeys(JobType, 330)


def test_worker_preserves_existing_uniform_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        media_probe_timeout_seconds=30,
        worker_health_check_interval_seconds=5,
    )

    def fake_dispatcher(
        cls: type[ArqJobDispatcher],
        settings: object,
        *,
        session_factory: object = None,
    ) -> ArqJobDispatcher:
        del settings
        return cls(RedisSettings(), session_factory=cast(Any, session_factory))

    monkeypatch.setattr(
        ArqJobDispatcher,
        "from_settings",
        classmethod(fake_dispatcher),
    )
    worker = worker_module.build_worker(
        cast(Any, settings),
        engine=cast(Any, object()),
        factory=cast(Any, object()),
        runtime=cast(Any, object()),
        usage_runtime=cast(Any, object()),
        artifact_executor=cast(Any, object()),
        annotation_runtime=cast(Any, object()),
        current_caller_resolver=cast(Any, object()),
    )

    assert set(worker.functions) == {
        "validate_dataset_job",
        "check_dataset_usage_job",
        "generate_dataset_artifact_job",
        "prepare_annotation_context_job",
        "annotate_dataset_job",
        "cron:dispatch_pending_jobs",
    }
    assert all(function.timeout_s is None for function in worker.functions.values())
    assert worker.job_timeout_s == 330
    assert worker_module._stale_recovery_timeout_seconds(cast(Any, settings)) == 330
