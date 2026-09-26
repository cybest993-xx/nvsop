"""ARQ worker 阻塞隔离与晚到结果执行权回归测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from io import BytesIO
from threading import Event, get_ident
from types import SimpleNamespace, TracebackType
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

import factory_sop.job.adapters.worker as worker_module
from factory_sop.dataset.api import (
    AnnotationBackendExecutionError,
    AnnotationBackendUnavailableError,
    AnnotationCleanupPendingError,
    ArtifactExecutionOutcome,
    ArtifactExecutionResult,
    MemberStatus,
)
from factory_sop.job.api import ApplicationJob, JobStatus, JobType

NOW = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)
_UNEXPECTED_FINISH = object()


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
    def __init__(
        self,
        state: _WorkerState,
        job: ApplicationJob,
        *,
        storage: object | None = None,
    ) -> None:
        self._state = state
        self._job = job
        self._storage = storage if storage is not None else object()

    def repository(self, session: object) -> _Datasets:
        assert isinstance(session, _TrackingSession)
        return _Datasets(self._state, session, self._job)

    def storage(self) -> object:
        return self._storage

    def media_probe(self) -> object:
        return object()

    def supported_codecs(self) -> frozenset[str]:
        return frozenset({"h264"})


class _ValidationStorage:
    def __init__(self) -> None:
        self.objects = {"attempt-object": b"source"}

    @contextmanager
    def writing(self, *, object_key: str) -> Iterator[BytesIO]:
        sink = BytesIO()
        yield sink
        self.objects[object_key] = sink.getvalue()

    def delete(self, *, object_key: str) -> None:
        self.objects.pop(object_key, None)


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


def _job(job_type: JobType) -> ApplicationJob:
    return ApplicationJob(
        id=uuid4(),
        job_type=job_type,
        status=JobStatus.RUNNING,
        member_id=uuid4(),
        attempt_id=uuid4(),
        created_at=NOW,
        updated_at=NOW,
        failure_code=None,
    )


def _context_target(
    job: ApplicationJob,
    *,
    upstream_data_id: str | None = None,
    upstream_video_id: str | None = None,
    failure_code: str | None = None,
    failure_detail: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        job=job,
        context=SimpleNamespace(
            id=job.attempt_id,
            upstream_data_id=upstream_data_id,
            upstream_video_id=upstream_video_id,
            preparation_failure_code=failure_code,
            preparation_failure_detail=failure_detail,
        ),
        member=object(),
        actions=(),
    )


def _execution_target(
    job: ApplicationJob,
    *,
    upstream_data_id: str | None = None,
    upstream_video_id: str | None = None,
    failure_code: str | None = None,
    failure_detail: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        job=job,
        execution=SimpleNamespace(
            id=job.attempt_id,
            upstream_data_id=upstream_data_id,
            upstream_video_id=upstream_video_id,
            failure_code=failure_code,
            failure_detail=failure_detail,
        ),
        submission=SimpleNamespace(segments=(), mode="segment"),
    )


def _install_running_job_repository(
    monkeypatch: pytest.MonkeyPatch,
    *,
    job: ApplicationJob,
    state: _WorkerState,
    finish_result: bool | object = _UNEXPECTED_FINISH,
) -> None:
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
            if finish_result is _UNEXPECTED_FINISH:
                raise AssertionError("job finish must not be reached in this scenario")
            state.finish_calls += 1
            return cast(bool, finish_result)

    monkeypatch.setattr(worker_module, "PostgresJobRepository", FakeJobRepository)


def _install_validation_seams(
    monkeypatch: pytest.MonkeyPatch,
    *,
    state: _WorkerState,
    job: ApplicationJob,
    validate_upload: object,
    storage: object | None = None,
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
        return SimpleNamespace(attempt=SimpleNamespace(object_key="attempt-object"))

    monkeypatch.setattr(worker_module, "PostgresJobRepository", FakeJobRepository)
    monkeypatch.setattr(worker_module, "begin_video_validation", begin_validation)
    monkeypatch.setattr(worker_module, "validate_video_upload", validate_upload)
    return {
        "session_factory": _TrackingSessionFactory(state),
        "dataset_runtime": _ValidationRuntime(state, job, storage=storage),
        "blocking_job_slots": asyncio.Semaphore(worker_module._BLOCKING_JOB_LIMIT),
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
    settings = SimpleNamespace(
        media_probe_timeout_seconds=30,
        annotation_http_timeout_seconds=120,
        dataset_upload_ttl_seconds=900,
    )
    assert worker_module._blocking_execution_timeout_seconds(cast(Any, settings)) == 930

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

        def recover_stale_running(
            self, *, job_type: JobType, now: datetime, stale_after_seconds: int
        ) -> None:
            del now
            expected = {
                JobType.DATASET_VALIDATION: 3030,
                JobType.DATASET_ANNOTATION: 930,
                JobType.DATASET_ANNOTATION_PREPARATION: 930,
                JobType.DATASET_USAGE_CHECK: 330,
                JobType.DATASET_ARTIFACT: 330,
            }[job_type]
            assert stale_after_seconds == expected

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
        "settings": settings,
        "session_factory": _TrackingSessionFactory(_WorkerState()),
        "artifact_executor": CleanupExecutor(),
    }

    monkeypatch.setattr(worker_module, "_validate_dataset_job", slow_job)
    monkeypatch.setattr(worker_module, "_check_dataset_usage_job", quick_job)
    monkeypatch.setattr(worker_module, "ArqJobDispatcher", FakeDispatcher)
    monkeypatch.setattr(worker_module, "PostgresJobRepository", CronJobRepository)

    async def scenario() -> None:
        job_ctx: Mapping[str, Any] = {
            "blocking_job_slots": asyncio.Semaphore(worker_module._BLOCKING_JOB_LIMIT)
        }
        slow = asyncio.create_task(worker_module.validate_dataset_job(job_ctx, str(uuid4())))
        assert await asyncio.to_thread(started.wait, 1.0)

        await worker_module.check_dataset_usage_job(job_ctx, str(uuid4()))
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


def test_cancelled_jobs_hold_slots_until_physical_threads_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = [Event() for _ in range(worker_module._BLOCKING_JOB_LIMIT)]
    releases = [Event() for _ in range(worker_module._BLOCKING_JOB_LIMIT)]
    fifth_started = Event()

    def blocking_job(
        ctx: Mapping[str, Any],
        job_id: str,
        fence: worker_module._ExecutionFence,
    ) -> None:
        del ctx, fence
        if job_id == "fifth":
            fifth_started.set()
            return
        index = int(job_id)
        started[index].set()
        releases[index].wait()

    monkeypatch.setattr(worker_module, "_validate_dataset_job", blocking_job)

    async def scenario() -> None:
        ctx: Mapping[str, Any] = {
            "blocking_job_slots": asyncio.Semaphore(worker_module._BLOCKING_JOB_LIMIT)
        }
        running = [
            asyncio.create_task(worker_module.validate_dataset_job(ctx, str(index)))
            for index in range(worker_module._BLOCKING_JOB_LIMIT)
        ]
        for event in started:
            assert await asyncio.to_thread(event.wait, 1.0)

        for task in running:
            task.cancel()
        for task in running:
            with pytest.raises(asyncio.CancelledError):
                await task

        fifth = asyncio.create_task(worker_module.validate_dataset_job(ctx, "fifth"))
        await asyncio.sleep(0)
        assert not fifth_started.is_set()

        releases[0].set()
        assert await asyncio.to_thread(fifth_started.wait, 1.0)
        await fifth
        for event in releases[1:]:
            event.set()

    asyncio.run(scenario())


def test_cancel_before_execution_slot_restores_dispatchable_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _WorkerState()
    job_id = uuid4()
    restored: list[UUID] = []

    class FakeJobRepository:
        def __init__(self, session: object) -> None:
            assert isinstance(session, _TrackingSession)

        def restore_unstarted(self, *, job_id: UUID, now: datetime) -> bool:
            del now
            restored.append(job_id)
            return True

    monkeypatch.setattr(worker_module, "PostgresJobRepository", FakeJobRepository)
    ctx: Mapping[str, Any] = {
        "blocking_job_slots": asyncio.Semaphore(0),
        "session_factory": _TrackingSessionFactory(state),
    }

    async def scenario() -> None:
        task = asyncio.create_task(worker_module.validate_dataset_job(ctx, str(job_id)))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert restored == [job_id]
    assert len(state.sessions) == 1
    assert state.sessions[0].commits == 1
    assert state.sessions[0].closed.is_set()


def test_execution_fence_serializes_cancel_with_commit() -> None:
    commit_started = Event()
    release_commit = Event()

    class BlockingSession:
        def __init__(self) -> None:
            self.committed = False
            self.rolled_back = False

        def commit(self) -> None:
            commit_started.set()
            release_commit.wait()
            self.committed = True

        def rollback(self) -> None:
            self.rolled_back = True

    async def scenario() -> None:
        fence = worker_module._ExecutionFence()
        session = BlockingSession()
        committing = asyncio.create_task(asyncio.to_thread(fence.commit, cast(Any, session)))
        assert await asyncio.to_thread(commit_started.wait, 1.0)

        cancelling = asyncio.create_task(asyncio.to_thread(fence.request_cancel))
        await asyncio.sleep(0)
        assert not cancelling.done()

        release_commit.set()
        assert await committing
        await cancelling
        assert session.committed

        denied = BlockingSession()
        assert not fence.commit(cast(Any, denied))
        assert denied.rolled_back
        assert not denied.committed

    asyncio.run(scenario())


def test_validation_sessions_stay_on_the_blocking_execution_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller_thread = get_ident()
    state = _WorkerState()
    job = _running_job()
    object_storage = _ValidationStorage()

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
        del job, probe, supported_codecs, now, target
        fenced_storage = cast(Any, storage)
        with fenced_storage.writing(object_key="final-object") as sink:
            sink.write(b"x" * 5)
        fenced_storage.delete(object_key="attempt-object")
        datasets.stage_publish()
        return SimpleNamespace(status=MemberStatus.REGISTERED.value, failure_code=None)

    ctx = _install_validation_seams(
        monkeypatch,
        state=state,
        job=job,
        validate_upload=validate_upload,
        storage=object_storage,
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
    assert "attempt-object" not in object_storage.objects
    assert "final-object" in object_storage.objects


def test_cancelled_validation_discards_late_result_without_finishing_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _WorkerState()
    job = _running_job()
    started = Event()
    release = Event()
    object_storage = _ValidationStorage()

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
        del job, probe, supported_codecs, now, target
        fenced_storage = cast(Any, storage)
        with fenced_storage.writing(object_key="final-object") as sink:
            sink.write(b"x" * 5)
        fenced_storage.delete(object_key="attempt-object")
        datasets.stage_publish()
        started.set()
        release.wait()
        return SimpleNamespace(status=MemberStatus.REGISTERED.value, failure_code=None)

    ctx = _install_validation_seams(
        monkeypatch,
        state=state,
        job=job,
        validate_upload=validate_upload,
        storage=object_storage,
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
    assert "attempt-object" in object_storage.objects
    assert "final-object" not in object_storage.objects


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
            commit_transaction: Callable[[object], bool],
        ) -> ArtifactExecutionResult:
            assert job.id == expected_job_id
            candidate_exists[0] = True
            started.set()
            release.wait()
            result = ArtifactExecutionResult(ArtifactExecutionOutcome.SUCCEEDED)
            session = _TrackingSessionFactory(state)()
            accepted = finish_job(session, result)
            finish_results.append(accepted)
            if accepted:
                accepted = commit_transaction(session)
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
        "blocking_job_slots": asyncio.Semaphore(worker_module._BLOCKING_JOB_LIMIT),
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


def test_cancelled_annotation_preparation_discards_unpersisted_backend_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _WorkerState()
    started = Event()
    release = Event()
    discarded = Event()
    job = _job(JobType.DATASET_ANNOTATION_PREPARATION)
    target = _context_target(job)

    class Backend:
        def discard_prepared_video(self, *, data_id: str) -> None:
            assert data_id == "orphan-data"
            discarded.set()

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

    def begin_preparation(*, job: object, datasets: object) -> object:
        del job, datasets
        return target

    def prepare_copy(**kwargs: object) -> SimpleNamespace:
        del kwargs
        started.set()
        release.wait()
        return SimpleNamespace(prepared=SimpleNamespace(data_id="orphan-data"))

    _install_running_job_repository(monkeypatch, job=job, state=state)
    monkeypatch.setattr(
        worker_module,
        "begin_annotation_context_preparation",
        begin_preparation,
    )
    monkeypatch.setattr(worker_module, "prepare_annotation_context_copy", prepare_copy)
    ctx: Mapping[str, Any] = {
        "session_factory": _TrackingSessionFactory(state),
        "annotation_runtime": Runtime(),
        "blocking_job_slots": asyncio.Semaphore(worker_module._BLOCKING_JOB_LIMIT),
    }

    async def scenario() -> None:
        running = asyncio.create_task(
            worker_module.prepare_annotation_context_job(ctx, str(job.id))
        )
        assert await asyncio.to_thread(started.wait, 1.0)

        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

        release.set()
        assert await asyncio.to_thread(discarded.wait, 1.0)

    asyncio.run(scenario())
    assert state.finish_calls == 0


def test_annotation_preparation_lease_loss_discards_unpublished_backend_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _WorkerState()
    discarded: list[str] = []
    job = _job(JobType.DATASET_ANNOTATION_PREPARATION)
    target = _context_target(job)

    class Backend:
        def discard_prepared_video(self, *, data_id: str) -> None:
            discarded.append(data_id)

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

    _install_running_job_repository(monkeypatch, job=job, state=state, finish_result=False)
    monkeypatch.setattr(
        worker_module,
        "begin_annotation_context_preparation",
        lambda **kwargs: target,
    )
    monkeypatch.setattr(
        worker_module,
        "prepare_annotation_context_copy",
        lambda **kwargs: SimpleNamespace(prepared=SimpleNamespace(data_id="lease-lost-data")),
    )
    monkeypatch.setattr(
        worker_module,
        "complete_annotation_context_preparation",
        lambda **kwargs: None,
    )

    ctx: Mapping[str, Any] = {
        "session_factory": _TrackingSessionFactory(state),
        "annotation_runtime": Runtime(),
    }
    worker_module._prepare_annotation_context_job(
        ctx,
        str(job.id),
        worker_module._ExecutionFence(),
    )

    assert state.finish_calls == 1
    assert discarded == ["lease-lost-data"]
    assert state.sessions[-1].rollbacks == 1


@pytest.mark.parametrize("kind", ["context", "execution"])
def test_annotation_backend_construction_failure_is_classified(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    state = _WorkerState()
    job_type = (
        JobType.DATASET_ANNOTATION_PREPARATION if kind == "context" else JobType.DATASET_ANNOTATION
    )
    job = _job(job_type)
    captured: list[str] = []

    class Runtime:
        def repository(self, session: object) -> object:
            assert isinstance(session, _TrackingSession)
            return object()

        def backend(self) -> object:
            raise AnnotationBackendUnavailableError("not configured")

    _install_running_job_repository(monkeypatch, job=job, state=state)
    if kind == "context":
        target = _context_target(job)
        monkeypatch.setattr(
            worker_module,
            "begin_annotation_context_preparation",
            lambda **kwargs: target,
        )
        monkeypatch.setattr(
            worker_module,
            "_finish_context_preparation_failure",
            lambda **kwargs: captured.append(cast(str, kwargs["code"])),
        )
        runner = worker_module._prepare_annotation_context_job
    else:
        target = _execution_target(job)
        monkeypatch.setattr(
            worker_module,
            "begin_annotation_execution",
            lambda **kwargs: target,
        )
        monkeypatch.setattr(
            worker_module,
            "_finish_annotation_failure",
            lambda **kwargs: captured.append(cast(str, kwargs["code"])),
        )
        runner = worker_module._annotate_dataset_job

    ctx: Mapping[str, Any] = {
        "session_factory": _TrackingSessionFactory(state),
        "annotation_runtime": Runtime(),
    }
    runner(ctx, str(job.id), worker_module._ExecutionFence())

    assert captured == ["ANNOTATION_BACKEND_UNAVAILABLE"]


@pytest.mark.parametrize("kind", ["context", "execution"])
def test_cleanup_candidate_survives_backend_construction_failure(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    state = _WorkerState()
    job_type = (
        JobType.DATASET_ANNOTATION_PREPARATION if kind == "context" else JobType.DATASET_ANNOTATION
    )
    job = _job(job_type)
    finished: list[str] = []

    class Runtime:
        def repository(self, session: object) -> object:
            assert isinstance(session, _TrackingSession)
            return object()

        def backend(self) -> object:
            raise AnnotationBackendUnavailableError("not configured")

    _install_running_job_repository(monkeypatch, job=job, state=state)
    if kind == "context":
        target = _context_target(
            job,
            upstream_data_id="cleanup-data",
            failure_code="ANNOTATION_EXECUTION_FAILED",
            failure_detail="cleanup pending",
        )
        monkeypatch.setattr(
            worker_module,
            "begin_annotation_context_preparation",
            lambda **kwargs: target,
        )
        monkeypatch.setattr(
            worker_module,
            "_finish_context_preparation_failure",
            lambda **kwargs: finished.append(cast(str, kwargs["code"])),
        )
        runner = worker_module._prepare_annotation_context_job
    else:
        target = _execution_target(
            job,
            upstream_data_id="cleanup-data",
            failure_code="ANNOTATION_EXECUTION_FAILED",
            failure_detail="cleanup pending",
        )
        monkeypatch.setattr(
            worker_module,
            "begin_annotation_execution",
            lambda **kwargs: target,
        )
        monkeypatch.setattr(
            worker_module,
            "_finish_annotation_failure",
            lambda **kwargs: finished.append(cast(str, kwargs["code"])),
        )
        runner = worker_module._annotate_dataset_job

    ctx: Mapping[str, Any] = {
        "session_factory": _TrackingSessionFactory(state),
        "annotation_runtime": Runtime(),
    }
    runner(ctx, str(job.id), worker_module._ExecutionFence())

    assert finished == []


def test_annotation_cleanup_pending_is_persisted_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _WorkerState()
    job = _job(JobType.DATASET_ANNOTATION)
    target = _execution_target(job)
    recorded: list[tuple[str, str | None, str | None]] = []

    class Runtime:
        def repository(self, session: object) -> object:
            assert isinstance(session, _TrackingSession)
            return object()

        def storage(self) -> object:
            return object()

        def backend(self) -> object:
            return object()

        def media_probe(self) -> object:
            return object()

    def record_candidate(**kwargs: object) -> object:
        recorded.append(
            (
                cast(str, kwargs["data_id"]),
                cast(str | None, kwargs["code"]),
                cast(str | None, kwargs["detail"]),
            )
        )
        return target

    _install_running_job_repository(monkeypatch, job=job, state=state)
    monkeypatch.setattr(worker_module, "begin_annotation_execution", lambda **kwargs: target)
    monkeypatch.setattr(
        worker_module,
        "prepare_annotation_execution_copy",
        lambda **kwargs: (_ for _ in ()).throw(
            AnnotationCleanupPendingError(
                data_id="cleanup-data",
                failure=AnnotationBackendExecutionError("derived download failed"),
            )
        ),
    )
    monkeypatch.setattr(
        worker_module,
        "record_annotation_execution_cleanup_candidate",
        record_candidate,
    )
    ctx: Mapping[str, Any] = {
        "session_factory": _TrackingSessionFactory(state),
        "annotation_runtime": Runtime(),
    }

    worker_module._annotate_dataset_job(
        ctx,
        str(job.id),
        worker_module._ExecutionFence(),
    )

    assert recorded == [("cleanup-data", "ANNOTATION_EXECUTION_FAILED", "derived download failed")]


def test_annotation_cleanup_candidate_is_retried_before_new_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _WorkerState()
    job = _job(JobType.DATASET_ANNOTATION)
    target = _execution_target(
        job,
        upstream_data_id="cleanup-data",
        failure_code="ANNOTATION_EXECUTION_FAILED",
        failure_detail="derived download failed",
    )
    discarded: list[str] = []
    failures: list[tuple[str, str]] = []

    class Backend:
        def discard_prepared_video(self, *, data_id: str) -> None:
            discarded.append(data_id)

    class Runtime:
        def repository(self, session: object) -> object:
            assert isinstance(session, _TrackingSession)
            return object()

        def backend(self) -> Backend:
            return Backend()

    _install_running_job_repository(monkeypatch, job=job, state=state)
    monkeypatch.setattr(worker_module, "begin_annotation_execution", lambda **kwargs: target)
    monkeypatch.setattr(
        worker_module,
        "prepare_annotation_execution_copy",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("cleanup candidate retry must not prepare a second copy")
        ),
    )
    monkeypatch.setattr(
        worker_module,
        "_finish_annotation_failure",
        lambda **kwargs: failures.append((cast(str, kwargs["code"]), cast(str, kwargs["detail"]))),
    )
    ctx: Mapping[str, Any] = {
        "session_factory": _TrackingSessionFactory(state),
        "annotation_runtime": Runtime(),
    }

    worker_module._annotate_dataset_job(
        ctx,
        str(job.id),
        worker_module._ExecutionFence(),
    )

    assert discarded == ["cleanup-data"]
    assert failures == [("ANNOTATION_EXECUTION_FAILED", "derived download failed")]


@pytest.mark.parametrize("reconcile_available", [True, False])
def test_annotation_copy_commit_unknown_preserves_backend_copy(
    monkeypatch: pytest.MonkeyPatch,
    reconcile_available: bool,
) -> None:
    state = _WorkerState()
    discarded: list[str] = []
    split_calls = 0
    job = _job(JobType.DATASET_ANNOTATION)
    target = _execution_target(job)

    class CommitUnknownSession(_TrackingSession):
        def __init__(self, state: _WorkerState, *, commit_unknown: bool) -> None:
            super().__init__(state)
            self._commit_unknown = commit_unknown

        def commit(self) -> None:
            super().commit()
            if self._commit_unknown:
                raise RuntimeError("commit result unknown")

    class CommitUnknownFactory:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self) -> _TrackingSession:
            self.calls += 1
            session = CommitUnknownSession(state, commit_unknown=self.calls == 3)
            state.sessions.append(session)
            return session

    class Backend:
        def discard_prepared_video(self, *, data_id: str) -> None:
            discarded.append(data_id)

        def split_video(
            self, *, video_id: object, segments: object, mode: object
        ) -> tuple[dict[str, str], ...]:
            nonlocal split_calls
            del video_id, segments, mode
            split_calls += 1
            return ({"id": "unexpected"},)

    backend = Backend()

    class Repository:
        def annotation_execution_by_id(self, execution_id: UUID) -> object:
            assert execution_id == target.execution.id
            if not reconcile_available:
                raise RuntimeError("reconcile unavailable")
            return SimpleNamespace(
                upstream_data_id="commit-unknown-data",
                upstream_video_id="persisted-video",
            )

    class Runtime:
        def repository(self, session: object) -> object:
            assert isinstance(session, _TrackingSession)
            return Repository()

        def storage(self) -> object:
            return object()

        def backend(self) -> Backend:
            return backend

        def media_probe(self) -> object:
            return object()

    _install_running_job_repository(monkeypatch, job=job, state=state)
    monkeypatch.setattr(
        worker_module,
        "begin_annotation_execution",
        lambda **kwargs: target,
    )
    monkeypatch.setattr(
        worker_module,
        "prepare_annotation_execution_copy",
        lambda **kwargs: SimpleNamespace(prepared=SimpleNamespace(data_id="commit-unknown-data")),
    )
    monkeypatch.setattr(
        worker_module,
        "save_annotation_execution_copy",
        lambda **kwargs: target,
    )
    ctx: Mapping[str, Any] = {
        "session_factory": CommitUnknownFactory(),
        "annotation_runtime": Runtime(),
    }

    worker_module._annotate_dataset_job(
        ctx,
        str(job.id),
        worker_module._ExecutionFence(),
    )

    assert len(state.sessions) == 4
    assert state.sessions[2].commits == 1
    assert discarded == []
    assert split_calls == 0


def test_annotation_copy_commit_failure_records_cleanup_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _WorkerState()
    recorded: list[str] = []
    split_calls = 0
    job = _job(JobType.DATASET_ANNOTATION)
    target = _execution_target(job)

    class CommitFailedSession(_TrackingSession):
        def __init__(self, state: _WorkerState, *, fail_commit: bool) -> None:
            super().__init__(state)
            self._fail_commit = fail_commit

        def commit(self) -> None:
            if self._fail_commit:
                self._touch()
                raise RuntimeError("commit failed")
            super().commit()

    class CommitFailedFactory:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self) -> _TrackingSession:
            self.calls += 1
            session = CommitFailedSession(state, fail_commit=self.calls == 3)
            state.sessions.append(session)
            return session

    class Repository:
        def annotation_execution_by_id(self, execution_id: UUID) -> object:
            assert execution_id == target.execution.id
            return SimpleNamespace(upstream_data_id=None, upstream_video_id=None)

    class Backend:
        def split_video(
            self, *, video_id: object, segments: object, mode: object
        ) -> tuple[dict[str, str], ...]:
            nonlocal split_calls
            del video_id, segments, mode
            split_calls += 1
            return ({"id": "unexpected"},)

    class Runtime:
        def repository(self, session: object) -> object:
            assert isinstance(session, _TrackingSession)
            return Repository()

        def storage(self) -> object:
            return object()

        def backend(self) -> Backend:
            return Backend()

        def media_probe(self) -> object:
            return object()

    _install_running_job_repository(monkeypatch, job=job, state=state)
    monkeypatch.setattr(worker_module, "begin_annotation_execution", lambda **kwargs: target)
    monkeypatch.setattr(
        worker_module,
        "prepare_annotation_execution_copy",
        lambda **kwargs: SimpleNamespace(prepared=SimpleNamespace(data_id="commit-failed-data")),
    )
    monkeypatch.setattr(worker_module, "save_annotation_execution_copy", lambda **kwargs: target)

    def record_cleanup(**kwargs: object) -> bool:
        recorded.append(cast(str, kwargs["data_id"]))
        return True

    monkeypatch.setattr(worker_module, "_record_execution_cleanup_candidate", record_cleanup)
    ctx: Mapping[str, Any] = {
        "session_factory": CommitFailedFactory(),
        "annotation_runtime": Runtime(),
    }

    worker_module._annotate_dataset_job(
        ctx,
        str(job.id),
        worker_module._ExecutionFence(),
    )

    assert recorded == ["commit-failed-data"]
    assert split_calls == 0


def test_annotation_retry_reuses_persisted_copy_after_uncertain_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _WorkerState()
    job = _job(JobType.DATASET_ANNOTATION)
    target = _execution_target(job, upstream_video_id="persisted-video")
    split_calls = 0

    class Backend:
        def discard_prepared_video(self, *, data_id: str) -> None:
            raise AssertionError(f"persisted copy must not be discarded: {data_id}")

        def split_video(
            self, *, video_id: object, segments: object, mode: object
        ) -> tuple[dict[str, str], ...]:
            nonlocal split_calls
            assert video_id == "persisted-video"
            del segments, mode
            split_calls += 1
            return ({"id": "clip-1"},)

    backend = Backend()

    class Runtime:
        def repository(self, session: object) -> object:
            assert isinstance(session, _TrackingSession)
            return object()

        def storage(self) -> object:
            raise AssertionError("persisted copy retry must not read source storage")

        def backend(self) -> Backend:
            return backend

        def media_probe(self) -> object:
            raise AssertionError("persisted copy retry must not probe a new copy")

    _install_running_job_repository(monkeypatch, job=job, state=state, finish_result=True)
    monkeypatch.setattr(
        worker_module,
        "begin_annotation_execution",
        lambda **kwargs: target,
    )
    monkeypatch.setattr(
        worker_module,
        "prepare_annotation_execution_copy",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("persisted copy retry must not prepare a new copy")
        ),
    )
    monkeypatch.setattr(
        worker_module,
        "complete_annotation_execution",
        lambda **kwargs: SimpleNamespace(id=job.attempt_id),
    )
    ctx: Mapping[str, Any] = {
        "session_factory": _TrackingSessionFactory(state),
        "annotation_runtime": Runtime(),
    }

    worker_module._annotate_dataset_job(
        ctx,
        str(job.id),
        worker_module._ExecutionFence(),
    )

    assert split_calls == 1
    assert state.finish_calls == 1


def test_cancelled_annotation_discards_late_backend_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _WorkerState()
    started = Event()
    release = Event()
    physical_finished = Event()
    job = _job(JobType.DATASET_ANNOTATION)
    target = _execution_target(job, upstream_video_id="video-1")
    completed_calls: list[object] = []

    class Backend:
        def discard_prepared_video(self, *, data_id: str) -> None:
            raise AssertionError(f"persisted annotation copy must not be discarded: {data_id}")

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
    ) -> SimpleNamespace:
        del target, storage, backend, media_probe
        return SimpleNamespace(prepared=SimpleNamespace(data_id="persisted-data"))

    def save_copy(*, target: object, prepared: object, now: datetime, datasets: object) -> object:
        del prepared, now, datasets
        return target

    def complete_annotation(*args: object, **kwargs: object) -> object:
        completed_calls.append((args, kwargs))
        return object()

    _install_running_job_repository(monkeypatch, job=job, state=state)
    monkeypatch.setattr(worker_module, "begin_annotation_execution", begin_annotation)
    monkeypatch.setattr(worker_module, "prepare_annotation_execution_copy", prepare_copy)
    monkeypatch.setattr(worker_module, "save_annotation_execution_copy", save_copy)
    monkeypatch.setattr(worker_module, "complete_annotation_execution", complete_annotation)
    ctx: Mapping[str, Any] = {
        "session_factory": _TrackingSessionFactory(state),
        "annotation_runtime": Runtime(),
        "blocking_job_slots": asyncio.Semaphore(worker_module._BLOCKING_JOB_LIMIT),
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
