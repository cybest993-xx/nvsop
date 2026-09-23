"""ARQ worker 阻塞隔离与晚到结果执行权回归测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from threading import Event, get_ident
from types import SimpleNamespace, TracebackType
from typing import Any
from uuid import UUID, uuid4

import pytest

import factory_sop.job.adapters.worker as worker_module
from factory_sop.dataset.api import ArtifactExecutionOutcome, ArtifactExecutionResult, MemberStatus
from factory_sop.job.api import ApplicationJob, JobStatus, JobType

NOW = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)


class _WorkerState:
    def __init__(self, *, finish_allowed: bool = True) -> None:
        self.finish_allowed = finish_allowed
        self.finish_calls = 0
        self.published = False
        self.sessions: list[_TrackingSession] = []


class _TrackingSession:
    def __init__(self, state: _WorkerState) -> None:
        self._state = state
        self.creator_thread = get_ident()
        self.used_threads: set[int] = {self.creator_thread}
        self.pending_publish = False
        self.commits = 0
        self.rollbacks = 0
        self.closed = Event()

    def _touch(self) -> None:
        current = get_ident()
        self.used_threads.add(current)
        assert current == self.creator_thread

    def __enter__(self) -> _TrackingSession:
        self._touch()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.close()

    def commit(self) -> None:
        self._touch()
        self.commits += 1
        if self.pending_publish:
            self._state.published = True
            self.pending_publish = False

    def rollback(self) -> None:
        self._touch()
        self.rollbacks += 1
        self.pending_publish = False

    def close(self) -> None:
        self._touch()
        self.closed.set()


class _TrackingSessionFactory:
    def __init__(self, state: _WorkerState) -> None:
        self._state = state

    def __call__(self) -> _TrackingSession:
        session = _TrackingSession(self._state)
        self._state.sessions.append(session)
        return session


class _Datasets:
    def __init__(self, state: _WorkerState, session: _TrackingSession, job: ApplicationJob) -> None:
        self._state = state
        self._session = session
        self._job = job

    def member_by_id(self, member_id: UUID) -> SimpleNamespace:
        assert member_id == self._job.member_id
        return SimpleNamespace(current_attempt_id=self._job.attempt_id)

    def stage_publish(self) -> None:
        self._session.pending_publish = True


class _ValidationRuntime:
    def __init__(self, state: _WorkerState, job: ApplicationJob) -> None:
        self._state = state
        self._job = job

    def repository(self, session: object) -> _Datasets:
        assert isinstance(session, _TrackingSession)
        return _Datasets(self._state, session, self._job)

    def storage(self) -> object:
        return object()

    def media_probe(self) -> object:
        return object()

    def supported_codecs(self) -> frozenset[str]:
        return frozenset({"h264"})


def _running_job() -> ApplicationJob:
    return ApplicationJob(
        id=uuid4(),
        job_type=JobType.DATASET_VALIDATION,
        status=JobStatus.RUNNING,
        member_id=uuid4(),
        attempt_id=uuid4(),
        created_at=NOW,
        updated_at=NOW,
        failure_code=None,
    )


def _install_validation_seams(
    monkeypatch: pytest.MonkeyPatch,
    *,
    state: _WorkerState,
    job: ApplicationJob,
    validate_upload: object,
) -> Mapping[str, Any]:
    class FakeJobRepository:
        def __init__(self, session: object) -> None:
            assert isinstance(session, _TrackingSession)

        def mark_running(self, *, job_id: UUID, now: datetime) -> ApplicationJob | None:
            del now
            assert job_id == job.id
            return job

        def finish(
            self,
            *,
            job_id: UUID,
            status: str,
            failure_code: str | None,
            now: datetime,
            expected_updated_at: datetime,
        ) -> bool:
            del status, failure_code, now
            assert job_id == job.id
            assert expected_updated_at == job.updated_at
            state.finish_calls += 1
            return state.finish_allowed

    def begin_validation(*, job: object, datasets: object, now: datetime) -> object:
        del job, datasets, now
        return object()

    monkeypatch.setattr(worker_module, "PostgresJobRepository", FakeJobRepository)
    monkeypatch.setattr(worker_module, "begin_video_validation", begin_validation)
    monkeypatch.setattr(worker_module, "validate_video_upload", validate_upload)
    return {
        "session_factory": _TrackingSessionFactory(state),
        "dataset_runtime": _ValidationRuntime(state, job),
    }


def test_blocking_jobs_leave_event_loop_responsive_and_cancel_promptly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = Event()
    release = Event()
    physical_finished = Event()
    second_finished = Event()
    cron_finished = Event()
    cancelled_seen: list[bool] = []

    def slow_job(
        ctx: Mapping[str, Any],
        job_id: str,
        fence: worker_module._ExecutionFence,
    ) -> None:
        del ctx, job_id
        started.set()
        release.wait()
        cancelled_seen.append(fence.cancelled)
        physical_finished.set()

    def quick_job(
        ctx: Mapping[str, Any],
        job_id: str,
        fence: worker_module._ExecutionFence,
    ) -> None:
        del ctx, job_id
        assert not fence.cancelled
        second_finished.set()

    class CronJobRepository:
        def __init__(self, session: object) -> None:
            assert isinstance(session, _TrackingSession)

        def recover_stale_running(self, *, now: datetime, stale_after_seconds: int) -> None:
            del now
            assert stale_after_seconds == 330

        def pending(self, *, limit: int) -> tuple[object, ...]:
            assert limit == 100
            return ()

    class FakeDispatcher:
        async def dispatch_async(self, job_id: UUID) -> None:
            raise AssertionError(f"no pending jobs expected: {job_id}")

    class CleanupExecutor:
        def cleanup_candidates(self) -> None:
            cron_finished.set()

    cleanup_ctx: Mapping[str, Any] = {
        "dispatcher": FakeDispatcher(),
        "settings": SimpleNamespace(media_probe_timeout_seconds=30),
        "session_factory": _TrackingSessionFactory(_WorkerState()),
        "artifact_executor": CleanupExecutor(),
    }

    monkeypatch.setattr(worker_module, "_validate_dataset_job", slow_job)
    monkeypatch.setattr(worker_module, "_check_dataset_usage_job", quick_job)
    monkeypatch.setattr(worker_module, "ArqJobDispatcher", FakeDispatcher)
    monkeypatch.setattr(worker_module, "PostgresJobRepository", CronJobRepository)

    async def scenario() -> None:
        slow = asyncio.create_task(worker_module.validate_dataset_job({}, str(uuid4())))
        assert await asyncio.to_thread(started.wait, 1.0)

        await worker_module.check_dataset_usage_job({}, str(uuid4()))
        assert second_finished.is_set()

        await worker_module.dispatch_pending_jobs(cleanup_ctx)
        assert cron_finished.is_set()

        slow.cancel()
        with pytest.raises(asyncio.CancelledError):
            await slow
        assert not physical_finished.is_set()

        release.set()
        assert await asyncio.to_thread(physical_finished.wait, 1.0)

    asyncio.run(scenario())
    assert cancelled_seen == [True]


def test_validation_sessions_stay_on_the_blocking_execution_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller_thread = get_ident()
    state = _WorkerState()
    job = _running_job()

    def validate_upload(
        *,
        job: object,
        datasets: _Datasets,
        storage: object,
        probe: object,
        supported_codecs: frozenset[str],
        now: datetime,
        target: object,
    ) -> SimpleNamespace:
        del job, storage, probe, supported_codecs, now, target
        datasets.stage_publish()
        return SimpleNamespace(status=MemberStatus.REGISTERED.value, failure_code=None)

    ctx = _install_validation_seams(
        monkeypatch,
        state=state,
        job=job,
        validate_upload=validate_upload,
    )

    asyncio.run(worker_module.validate_dataset_job(ctx, str(job.id)))

    assert state.finish_calls == 1
    assert state.published
    assert len(state.sessions) == 3
    execution_threads = {session.creator_thread for session in state.sessions}
    assert len(execution_threads) == 1
    assert caller_thread not in execution_threads
    assert all(session.used_threads == {session.creator_thread} for session in state.sessions)
    assert all(session.closed.is_set() for session in state.sessions)


def test_cancelled_validation_discards_late_result_without_finishing_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _WorkerState()
    job = _running_job()
    started = Event()
    release = Event()

    def validate_upload(
        *,
        job: object,
        datasets: _Datasets,
        storage: object,
        probe: object,
        supported_codecs: frozenset[str],
        now: datetime,
        target: object,
    ) -> SimpleNamespace:
        del job, storage, probe, supported_codecs, now, target
        datasets.stage_publish()
        started.set()
        release.wait()
        return SimpleNamespace(status=MemberStatus.REGISTERED.value, failure_code=None)

    ctx = _install_validation_seams(
        monkeypatch,
        state=state,
        job=job,
        validate_upload=validate_upload,
    )

    async def scenario() -> None:
        running = asyncio.create_task(worker_module.validate_dataset_job(ctx, str(job.id)))
        assert await asyncio.to_thread(started.wait, 1.0)

        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

        release.set()
        result_session = state.sessions[-1]
        assert await asyncio.to_thread(result_session.closed.wait, 1.0)

    asyncio.run(scenario())

    assert state.finish_calls == 0
    assert not state.published
    assert state.sessions[-1].rollbacks == 1


def test_validation_job_cas_loss_rolls_back_business_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _WorkerState(finish_allowed=False)
    job = _running_job()

    def validate_upload(
        *,
        job: object,
        datasets: _Datasets,
        storage: object,
        probe: object,
        supported_codecs: frozenset[str],
        now: datetime,
        target: object,
    ) -> SimpleNamespace:
        del job, storage, probe, supported_codecs, now, target
        datasets.stage_publish()
        return SimpleNamespace(status=MemberStatus.REGISTERED.value, failure_code=None)

    ctx = _install_validation_seams(
        monkeypatch,
        state=state,
        job=job,
        validate_upload=validate_upload,
    )

    asyncio.run(worker_module.validate_dataset_job(ctx, str(job.id)))

    assert state.finish_calls == 1
    assert not state.published
    assert state.sessions[-1].rollbacks == 1


def test_cancelled_artifact_generation_discards_late_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _WorkerState()
    started = Event()
    release = Event()
    candidate_deleted = Event()
    physical_finished = Event()
    candidate_exists = [False]
    finish_results: list[bool] = []
    expected_job_id: UUID
    job = ApplicationJob(
        id=uuid4(),
        job_type=JobType.DATASET_ARTIFACT,
        status=JobStatus.RUNNING,
        member_id=uuid4(),
        attempt_id=uuid4(),
        dataset_id=uuid4(),
        created_at=NOW,
        updated_at=NOW,
        failure_code=None,
    )
    expected_job_id = job.id

    class FakeJobRepository:
        def __init__(self, session: object) -> None:
            assert isinstance(session, _TrackingSession)

        def mark_running(self, *, job_id: UUID, now: datetime) -> ApplicationJob | None:
            del now
            assert job_id == job.id
            return job

        def finish(
            self,
            *,
            job_id: UUID,
            status: str,
            failure_code: str | None,
            now: datetime,
            expected_updated_at: datetime,
        ) -> bool:
            del status, failure_code, now
            assert job_id == job.id
            assert expected_updated_at == job.updated_at
            state.finish_calls += 1
            return True

    class ArtifactExecutor:
        def execute(
            self,
            *,
            job: ApplicationJob,
            finish_job: Callable[[object, ArtifactExecutionResult], bool],
        ) -> ArtifactExecutionResult:
            assert job.id == expected_job_id
            candidate_exists[0] = True
            started.set()
            release.wait()
            result = ArtifactExecutionResult(ArtifactExecutionOutcome.SUCCEEDED)
            accepted = finish_job(_TrackingSessionFactory(state)(), result)
            finish_results.append(accepted)
            if not accepted:
                candidate_exists[0] = False
                candidate_deleted.set()
                result = ArtifactExecutionResult(ArtifactExecutionOutcome.LEASE_LOST)
            physical_finished.set()
            return result

        def cleanup_candidates(self) -> None:
            return None

    monkeypatch.setattr(worker_module, "PostgresJobRepository", FakeJobRepository)
    ctx: Mapping[str, Any] = {
        "session_factory": _TrackingSessionFactory(state),
        "artifact_executor": ArtifactExecutor(),
    }

    async def scenario() -> None:
        running = asyncio.create_task(worker_module.generate_dataset_artifact_job(ctx, str(job.id)))
        assert await asyncio.to_thread(started.wait, 1.0)

        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

        release.set()
        assert await asyncio.to_thread(candidate_deleted.wait, 1.0)
        assert await asyncio.to_thread(physical_finished.wait, 1.0)

    asyncio.run(scenario())

    assert not candidate_exists[0]
    assert finish_results == [False]
    assert state.finish_calls == 0


def test_cancelled_annotation_discards_late_backend_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _WorkerState()
    started = Event()
    release = Event()
    physical_finished = Event()
    job = ApplicationJob(
        id=uuid4(),
        job_type=JobType.DATASET_ANNOTATION,
        status=JobStatus.RUNNING,
        member_id=uuid4(),
        attempt_id=uuid4(),
        created_at=NOW,
        updated_at=NOW,
        failure_code=None,
    )
    target = SimpleNamespace(
        job=job,
        execution=SimpleNamespace(id=job.attempt_id, upstream_video_id="video-1"),
        submission=SimpleNamespace(segments=(), mode="segment"),
    )
    completed_calls: list[object] = []

    class FakeJobRepository:
        def __init__(self, session: object) -> None:
            assert isinstance(session, _TrackingSession)

        def mark_running(self, *, job_id: UUID, now: datetime) -> ApplicationJob | None:
            del now
            assert job_id == job.id
            return job

        def finish(
            self,
            *,
            job_id: UUID,
            status: str,
            failure_code: str | None,
            now: datetime,
            expected_updated_at: datetime,
        ) -> bool:
            del job_id, status, failure_code, now, expected_updated_at
            state.finish_calls += 1
            return True

    class Backend:
        def split_video(
            self, *, video_id: object, segments: object, mode: object
        ) -> tuple[dict[str, str], ...]:
            del video_id, segments, mode
            started.set()
            release.wait()
            physical_finished.set()
            return ({"id": "late-clip"},)

    backend = Backend()

    class Runtime:
        def repository(self, session: object) -> object:
            assert isinstance(session, _TrackingSession)
            return object()

        def storage(self) -> object:
            return object()

        def backend(self) -> Backend:
            return backend

        def media_probe(self) -> object:
            return object()

    def begin_annotation(*, job: object, datasets: object, now: datetime) -> object:
        del job, datasets, now
        return target

    def prepare_copy(
        *, target: object, storage: object, backend: object, media_probe: object
    ) -> object:
        del target, storage, backend, media_probe
        return object()

    def save_copy(*, target: object, prepared: object, now: datetime, datasets: object) -> object:
        del prepared, now, datasets
        return target

    def complete_annotation(*args: object, **kwargs: object) -> object:
        completed_calls.append((args, kwargs))
        return object()

    monkeypatch.setattr(worker_module, "PostgresJobRepository", FakeJobRepository)
    monkeypatch.setattr(worker_module, "begin_annotation_execution", begin_annotation)
    monkeypatch.setattr(worker_module, "prepare_annotation_execution_copy", prepare_copy)
    monkeypatch.setattr(worker_module, "save_annotation_execution_copy", save_copy)
    monkeypatch.setattr(worker_module, "complete_annotation_execution", complete_annotation)
    ctx: Mapping[str, Any] = {
        "session_factory": _TrackingSessionFactory(state),
        "annotation_runtime": Runtime(),
    }

    async def scenario() -> None:
        running = asyncio.create_task(worker_module.annotate_dataset_job(ctx, str(job.id)))
        assert await asyncio.to_thread(started.wait, 1.0)

        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

        release.set()
        assert await asyncio.to_thread(physical_finished.wait, 1.0)

    asyncio.run(scenario())

    assert state.finish_calls == 0
    assert completed_calls == []
