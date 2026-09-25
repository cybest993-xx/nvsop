from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from factory_sop.job.adapters import worker as worker_module
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


def test_stale_recovery_uses_job_type_specific_execution_lease(
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
    settings = SimpleNamespace(
        media_probe_timeout_seconds=30,
        annotation_http_timeout_seconds=120,
    )
    ctx = {
        "dispatcher": FakeDispatcher(),
        "settings": settings,
        "session_factory": _Factory(),
    }

    asyncio.run(worker_module.dispatch_pending_jobs(ctx))

    assert dict(recovered) == {
        JobType.DATASET_VALIDATION: 330,
        JobType.DATASET_ANNOTATION: 930,
        JobType.DATASET_ANNOTATION_PREPARATION: 930,
        JobType.DATASET_USAGE_CHECK: 330,
        JobType.DATASET_ARTIFACT: 330,
    }
