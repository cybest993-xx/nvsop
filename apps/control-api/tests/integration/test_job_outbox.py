"""PostgreSQL 权威任务、真实 Redis 投递和 outbox 补投。"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from _integration_support import (
    MinioServer,
    RedisServer,
    cleanup_dataset,
    client_for,
    row,
    settings_for,
    upload_presigned,
)
from arq.connections import RedisSettings
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy import Engine, text

import factory_sop.job.adapters.dispatcher as dispatcher_module
from factory_sop.app import API_PREFIX
from factory_sop.job.adapters.dispatcher import ArqJobDispatcher
from factory_sop.job.adapters.repository import PostgresJobRepository
from factory_sop.job.adapters.worker import dispatch_pending_jobs
from factory_sop.job.api import ApplicationJob, JobStatus, JobType
from factory_sop.persistence import session_factory

DATASETS = f"{API_PREFIX}/training-datasets"


def test_dispatcher_routes_annotation_preparation_jobs_to_preparation_worker(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = ApplicationJob(
        id=uuid4(),
        job_type=JobType.DATASET_ANNOTATION_PREPARATION,
        status=JobStatus.PENDING,
        member_id=uuid4(),
        attempt_id=uuid4(),
        created_at=datetime(2026, 9, 9, 1, 0, tzinfo=UTC),
        updated_at=datetime(2026, 9, 9, 1, 0, tzinfo=UTC),
        failure_code=None,
    )
    with session_factory(engine).begin() as session:
        PostgresJobRepository(session).add(job)

    calls: dict[str, Any] = {}

    class FakePool:
        async def enqueue_job(self, function: str, *args: str, **kwargs: str) -> None:
            calls.update(function=function, args=args, kwargs=kwargs)

        async def close(self) -> None:
            return None

    async def fake_create_pool(_: RedisSettings) -> FakePool:
        return FakePool()

    monkeypatch.setattr(dispatcher_module, "create_pool", fake_create_pool)
    try:
        dispatcher = ArqJobDispatcher(
            RedisSettings(),
            session_factory=session_factory(engine),
        )
        asyncio.run(dispatcher.dispatch_async(job.id))
        assert calls == {
            "function": "prepare_annotation_context_job",
            "args": (str(job.id),),
            "kwargs": {"_job_id": str(job.id)},
        }
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM job_application_job WHERE id = :job_id"),
                {"job_id": job.id},
            )


def test_dispatcher_routes_annotation_jobs_to_annotation_worker(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = ApplicationJob(
        id=uuid4(),
        job_type=JobType.DATASET_ANNOTATION,
        status=JobStatus.PENDING,
        member_id=uuid4(),
        attempt_id=uuid4(),
        created_at=datetime(2026, 9, 9, 1, 0, tzinfo=UTC),
        updated_at=datetime(2026, 9, 9, 1, 0, tzinfo=UTC),
        failure_code=None,
    )
    with session_factory(engine).begin() as session:
        PostgresJobRepository(session).add(job)

    calls: dict[str, Any] = {}

    class FakePool:
        async def enqueue_job(self, function: str, *args: str, **kwargs: str) -> None:
            calls.update(function=function, args=args, kwargs=kwargs)

        async def close(self) -> None:
            return None

    async def fake_create_pool(_: RedisSettings) -> FakePool:
        return FakePool()

    monkeypatch.setattr(dispatcher_module, "create_pool", fake_create_pool)
    try:
        dispatcher = ArqJobDispatcher(
            RedisSettings(),
            session_factory=session_factory(engine),
        )
        asyncio.run(dispatcher.dispatch_async(job.id))
        assert calls == {
            "function": "annotate_dataset_job",
            "args": (str(job.id),),
            "kwargs": {"_job_id": str(job.id)},
        }
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM job_application_job WHERE id = :job_id"),
                {"job_id": job.id},
            )


def test_annotation_job_replay_does_not_reset_a_failed_execution(
    engine: Engine,
) -> None:
    now = datetime(2026, 9, 9, 1, 0, tzinfo=UTC)
    job = ApplicationJob(
        id=uuid4(),
        job_type=JobType.DATASET_ANNOTATION,
        status=JobStatus.FAILED,
        member_id=uuid4(),
        attempt_id=uuid4(),
        created_at=now,
        updated_at=now,
        failure_code="ANNOTATION_EXECUTION_FAILED",
    )
    with session_factory(engine).begin() as session:
        repository = PostgresJobRepository(session)
        repository.add(job)
        replay = repository.get_or_create_annotation(
            member_id=job.member_id,
            attempt_id=job.attempt_id,
            now=now + timedelta(seconds=1),
        )

    assert replay == job
    try:
        facts = row(
            engine,
            "SELECT status, failure_code, outbox_status "
            "FROM job_application_job WHERE id = :job_id",
            job_id=job.id,
        )
        assert facts == ("failed", "ANNOTATION_EXECUTION_FAILED", "pending")
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM job_application_job WHERE id = :job_id"),
                {"job_id": job.id},
            )


def _create_dataset(client: TestClient) -> UUID:
    response = client.post(f"{DATASETS}", json={"name": "outbox 集成测试集"})
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


def _request_upload(client: TestClient, dataset_id: UUID) -> dict[str, Any]:
    content = b"outbox-only-synthetic-object"
    response = client.post(
        f"{DATASETS}/{dataset_id}/members",
        headers={"Idempotency-Key": "outbox-upload-1"},
        json={
            "original_filename": "outbox.mp4",
            "source": "synthetic-camera",
            "declared_size": len(content),
            "declared_sha256": hashlib.sha256(content).hexdigest(),
        },
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json())


def _confirm(client: TestClient, dataset_id: UUID, requested: dict[str, Any]) -> UUID:
    response = client.post(
        f"{DATASETS}/{dataset_id}/members/{requested['member']['id']}/confirm",
        json={"attempt_id": requested["attempt"]["id"]},
    )
    assert response.status_code == 202, response.text
    return UUID(response.json()["job"]["id"])


def _assert_redis_job(redis_client: Redis, job_id: UUID) -> None:
    marker = str(job_id).encode()
    keys = cast(list[bytes], redis_client.keys("arq:*"))
    assert any(marker in key for key in keys)


def test_committed_confirmation_is_delivered_to_real_redis_after_postgres_commit(
    engine: Engine,
    minio_server: MinioServer,
    redis_server: RedisServer,
    redis_client: Redis,
) -> None:
    settings = settings_for(engine, minio=minio_server, redis_url=redis_server.url)
    with client_for(engine, settings) as client:
        dataset_id = _create_dataset(client)
        try:
            requested = _request_upload(client, dataset_id)
            upload = requested["upload"]
            content = b"outbox-only-synthetic-object"
            uploaded = upload_presigned(upload, content)
            assert uploaded.status_code == 204, uploaded.text
            job_id = _confirm(client, dataset_id, requested)

            facts = row(
                engine,
                "SELECT status, outbox_status, dispatch_attempts, last_dispatch_error "
                "FROM job_application_job WHERE id = :job_id",
                job_id=job_id,
            )
            assert tuple(facts) == ("enqueued", "dispatched", 1, None)
            _assert_redis_job(redis_client, job_id)
        finally:
            cleanup_dataset(engine, dataset_id)


def test_failed_real_redis_dispatch_stays_pending_and_is_retried_from_outbox(
    engine: Engine,
    minio_server: MinioServer,
    redis_server: RedisServer,
    redis_client: Redis,
) -> None:
    # 端口 1 上没有服务：这是一次真实连接失败。
    # 测试仍使用生产 dispatcher，只让真实连接失败触发 outbox 保留。
    failed_settings = settings_for(
        engine,
        minio=minio_server,
        redis_url="redis://127.0.0.1:1/0",
    )
    with client_for(engine, failed_settings) as client:
        dataset_id = _create_dataset(client)
        try:
            requested = _request_upload(client, dataset_id)
            job_id = _confirm(client, dataset_id, requested)

            failed = row(
                engine,
                "SELECT status, outbox_status, dispatch_attempts, last_dispatch_error "
                "FROM job_application_job WHERE id = :job_id",
                job_id=job_id,
            )
            assert failed[0] == "pending"
            assert failed[1] == "pending"
            assert failed[2] == 1
            assert failed[3]

            recovered_settings = settings_for(
                engine,
                minio=minio_server,
                redis_url=redis_server.url,
            )
            dispatcher = ArqJobDispatcher.from_settings(
                recovered_settings,
                session_factory=session_factory(engine),
            )
            context: dict[str, Any] = {
                "dispatcher": dispatcher,
                "session_factory": session_factory(engine),
                "settings": recovered_settings,
            }
            asyncio.run(dispatch_pending_jobs(context))

            recovered = row(
                engine,
                "SELECT status, outbox_status, dispatch_attempts, last_dispatch_error "
                "FROM job_application_job WHERE id = :job_id",
                job_id=job_id,
            )
            assert tuple(recovered) == ("enqueued", "dispatched", 2, None)
            _assert_redis_job(redis_client, job_id)
        finally:
            cleanup_dataset(engine, dataset_id)


def test_stale_running_job_returns_to_pending_and_pending_scan_keeps_fresh_job_running(
    engine: Engine,
) -> None:
    """过期运行租约恢复为待投递，未过期任务保持运行中。"""
    now = datetime(2026, 9, 9, 1, 0, tzinfo=UTC)
    stale_id = uuid4()
    fresh_id = uuid4()
    stale_at = now - timedelta(seconds=361)
    fresh_at = now - timedelta(seconds=359)

    def application_job(job_id: UUID, updated_at: datetime) -> ApplicationJob:
        return ApplicationJob(
            id=job_id,
            job_type=JobType.DATASET_VALIDATION,
            status=JobStatus.PENDING,
            member_id=uuid4(),
            attempt_id=uuid4(),
            created_at=updated_at,
            updated_at=updated_at,
            failure_code=None,
        )

    try:
        with session_factory(engine).begin() as session:
            repository = PostgresJobRepository(session)
            repository.add(application_job(stale_id, stale_at))
            repository.add(application_job(fresh_id, fresh_at))
            assert repository.mark_running(job_id=stale_id, now=stale_at) is not None
            assert repository.mark_running(job_id=fresh_id, now=fresh_at) is not None
            repository.mark_enqueued(job_id=stale_id, now=stale_at)
            repository.mark_enqueued(job_id=fresh_id, now=fresh_at)

        with session_factory(engine)() as session:
            recovered = PostgresJobRepository(session).recover_stale_running(
                now=now,
                stale_after_seconds=360,
            )
            session.commit()

        assert recovered == 1
        stale_facts = row(
            engine,
            "SELECT status, outbox_status, last_dispatch_error "
            "FROM job_application_job WHERE id = :job_id",
            job_id=stale_id,
        )
        fresh_facts = row(
            engine,
            "SELECT status, outbox_status, last_dispatch_error "
            "FROM job_application_job WHERE id = :job_id",
            job_id=fresh_id,
        )
        assert stale_facts[0:2] == ("pending", "pending")
        assert stale_facts[2] is not None
        assert fresh_facts == ("running", "dispatched", None)

        with session_factory(engine)() as session:
            pending = PostgresJobRepository(session).pending(limit=100)
        assert stale_id in {job.id for job in pending}
        assert fresh_id not in {job.id for job in pending}

        renewed_at = now + timedelta(seconds=2)
        with session_factory(engine)() as session:
            repository = PostgresJobRepository(session)
            repository.mark_enqueued(job_id=stale_id, now=now + timedelta(seconds=1))
            assert repository.mark_running(job_id=stale_id, now=renewed_at) is not None
            assert not repository.finish(
                job_id=stale_id,
                status=JobStatus.SUCCEEDED.value,
                failure_code=None,
                now=renewed_at + timedelta(seconds=1),
                expected_updated_at=stale_at,
            )
            session.commit()

        renewed_facts = row(
            engine,
            "SELECT status, outbox_status FROM job_application_job WHERE id = :job_id",
            job_id=stale_id,
        )
        assert renewed_facts == ("running", "dispatched")
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM job_application_job WHERE id IN (:stale_id, :fresh_id)"),
                {"stale_id": stale_id, "fresh_id": fresh_id},
            )
