"""训练视频的真实 PostgreSQL、中心本地持久卷和 ffprobe 生命周期。"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import shutil
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any, BinaryIO, cast
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from _integration_support import (
    RedisServer,
    caller,
    cleanup_dataset,
    client_for,
    row,
    settings_for,
    upload_video_content,
)
from arq import Worker
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy import Engine

from factory_sop.app import API_PREFIX, create_app
from factory_sop.auth.adapters.cookies import CSRF_COOKIE, CSRF_HEADER
from factory_sop.auth.adapters.repository import PostgresRoleRepository, PostgresUserRepository
from factory_sop.auth.authorization import Caller
from factory_sop.auth.model import Role, User, UserStatus
from factory_sop.auth.passwords import hash_password
from factory_sop.auth.permissions import Permission
from factory_sop.dataset.adapters.dependencies import (
    artifact_executor,
    usage_runtime,
    validation_runtime,
)
from factory_sop.dataset.adapters.media import FfprobeMediaProbe
from factory_sop.dataset.adapters.repository import PostgresDatasetRepository
from factory_sop.dataset.adapters.routes import ArtifactView, UsageCheckView
from factory_sop.dataset.adapters.storage import LocalFileObjectStorage
from factory_sop.dataset.annotation import AnnotationBackend, PreparedAnnotationVideo
from factory_sop.dataset.model import (
    ActionListRevision,
    AnnotationMode,
    AnnotationSegment,
    AttemptStatus,
    DatasetMember,
    MemberStatus,
    RetryMode,
    TrainingDataset,
    UploadAttempt,
    UsageCheck,
    UsageCheckStatus,
    UsageKind,
)
from factory_sop.dataset.usecases import confirm_video_upload, request_video_upload
from factory_sop.dataset.usecases.usage import (
    BASE_COMMIT,
    DDM_CONSUMER_PARAMETERS,
    DDM_CONTRACT_VERSION,
    request_artifact,
)
from factory_sop.identifiers import new_id
from factory_sop.job.adapters.dispatcher import ArqJobDispatcher
from factory_sop.job.adapters.repository import (
    PostgresJobRepository,
    PostgresUsageJobQueue,
    PostgresValidationJobQueue,
)
from factory_sop.job.adapters.worker import (
    annotate_dataset_job,
    check_dataset_usage_job,
    dispatch_pending_jobs,
    generate_dataset_artifact_job,
    prepare_annotation_context_job,
    validate_dataset_job,
)
from factory_sop.persistence import session_factory
from factory_sop.settings import Settings

DATASETS = f"{API_PREFIX}/training-datasets"


def _seed_real_dataset_manager(engine: Engine) -> tuple[UUID, UUID, str, str]:
    user_id = new_id()
    role_id = new_id()
    login_name = f"dataset-manager-{uuid4().hex[:12]}"
    password = f"dataset-password-{uuid4().hex}"
    user = User(
        id=user_id,
        login_name=login_name,
        display_name="真实数据集管理员",
        password_hash=hash_password(password),
        status=UserStatus.ACTIVE,
        created_by=user_id,
        updated_by=user_id,
    )
    role = Role(
        id=role_id,
        code=f"dataset-manager-{uuid4().hex[:12]}",
        name="真实数据集管理员角色",
        permissions=frozenset({Permission.DATASET_IMPORT, Permission.DATASET_VIEW}),
        created_by=user_id,
        updated_by=user_id,
    )
    with session_factory(engine)() as database:
        PostgresUserRepository(database).add(user)
        roles = PostgresRoleRepository(database)
        roles.add(role)
        roles.assign(user_id=user_id, role_ids=[role_id])
        database.commit()
    return user_id, role_id, login_name, password


def _remove_real_dataset_manager(engine: Engine, *, user_id: UUID, role_id: UUID) -> None:
    with session_factory(engine)() as database:
        PostgresRoleRepository(database).remove(role_id)
        PostgresUserRepository(database).remove(user_id)
        database.commit()


def _create_dataset(client: TestClient) -> UUID:
    response = client.post(f"{DATASETS}", json={"name": f"真实素材-{uuid4().hex[:8]}"})
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


def _request_upload(
    client: TestClient,
    dataset_id: UUID,
    content: bytes,
    *,
    filename: str,
    idempotency_key: str,
    declared_sha256: str | None = None,
) -> dict[str, Any]:
    declaration: dict[str, Any] = {
        "original_filename": filename,
        "source": "synthetic-camera",
        "declared_size": len(content),
    }
    if declared_sha256 is not None:
        declaration["declared_sha256"] = declared_sha256
    response = client.post(
        f"{DATASETS}/{dataset_id}/members",
        headers={"Idempotency-Key": idempotency_key},
        json=declaration,
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json())


def _confirm_upload(client: TestClient, dataset_id: UUID, upload: dict[str, Any]) -> UUID:
    member_id = UUID(upload["member"]["id"])
    response = client.post(
        f"{DATASETS}/{dataset_id}/members/{member_id}/confirm",
        json={"attempt_id": upload["attempt"]["id"]},
    )
    assert response.status_code == 202, response.text
    return UUID(response.json()["job"]["id"])


def _persisted_member(engine: Engine, member_id: UUID) -> DatasetMember:
    with session_factory(engine)() as database:
        member = PostgresDatasetRepository(database).member_by_id(member_id)
    assert member is not None
    return member


def _persisted_attempt(engine: Engine, attempt_id: UUID) -> UploadAttempt:
    with session_factory(engine)() as database:
        attempt = PostgresDatasetRepository(database).attempt_by_id(attempt_id)
    assert attempt is not None
    return attempt


def _run_worker(engine: Engine, settings: Settings, job_id: UUID) -> None:
    context: dict[str, Any] = {
        "settings": settings,
        "session_factory": session_factory(engine),
        "dataset_runtime": validation_runtime(settings),
    }
    asyncio.run(validate_dataset_job(context, str(job_id)))


class _UsageAnnotationBackend:
    """只在标注基座 HTTP seam 替换服务；请求、任务和持久化仍是真实实现。"""

    def __init__(self, source: bytes) -> None:
        self.source = source
        self.data_id = "data-real"
        self.video_id = "video-real"

    def prepare_video(
        self,
        *,
        source: BinaryIO,
        filename: str,
        actions: Sequence[str],
    ) -> PreparedAnnotationVideo:
        assert filename.endswith(".mp4")
        assert list(actions) == ["(1) 取料", "(2) 安装"]
        self.source = source.read()
        return PreparedAnnotationVideo(data_id=self.data_id, video_id=self.video_id)

    def download_video(self, *, video_id: str, destination: BinaryIO) -> None:
        assert video_id == self.video_id
        destination.write(self.source)

    def split_video(
        self,
        *,
        video_id: str,
        segments: Sequence[AnnotationSegment],
        mode: AnnotationMode,
    ) -> Sequence[dict[str, Any]]:
        assert video_id == self.video_id
        assert mode is AnnotationMode.SINGLE_OPERATOR
        return [
            {
                "id": f"clip-{index}",
                "filename": f"{index + 1:02d}_{self.video_id}_1_{index + 1}.mp4",
                "start_time": segment.start,
                "end_time": segment.end,
                "action_index": segment.action_index,
                "action_description": segment.action_description,
                "is_concurrent": False,
            }
            for index, segment in enumerate(segments)
        ]


class _IntegrationDdmReader:
    def sample_counts(
        self,
        *,
        workspace: Path,
        annotation_filename: str,
        parameters: Mapping[str, object],
    ) -> Mapping[str, int]:
        assert (workspace / annotation_filename).is_file()
        assert parameters["num_classes"] == 2
        return {"boundary_sample_count": 1, "non_boundary_sample_count": 1}


class _IntegrationVlmReader:
    def validate(self, *, workspace: Path, annotation_filename: str) -> None:
        assert (workspace / annotation_filename).is_file()


class _UsageAnnotationRuntime:
    def __init__(
        self, engine: Engine, settings: Settings, backend: _UsageAnnotationBackend
    ) -> None:
        self.engine = engine
        self.settings = settings
        self._backend = backend

    def repository(self, session: object) -> PostgresDatasetRepository:
        return PostgresDatasetRepository(cast(Any, session))

    def storage(self) -> LocalFileObjectStorage:
        return LocalFileObjectStorage.from_settings(self.settings)

    def backend(self) -> AnnotationBackend:
        return self._backend

    def media_probe(self) -> FfprobeMediaProbe:
        return FfprobeMediaProbe(
            binary=self.settings.media_probe_binary,
            timeout_seconds=self.settings.media_probe_timeout_seconds,
        )


async def _run_annotation_arq_worker(
    engine: Engine, settings: Settings, runtime: _UsageAnnotationRuntime
) -> None:
    dispatcher = ArqJobDispatcher.from_settings(
        settings,
        session_factory=session_factory(engine),
    )
    assert isinstance(dispatcher, ArqJobDispatcher)
    worker = Worker(
        functions=[validate_dataset_job, prepare_annotation_context_job, annotate_dataset_job],
        redis_settings=dispatcher.redis_settings,
        ctx={
            "settings": settings,
            "session_factory": session_factory(engine),
            "dataset_runtime": validation_runtime(settings),
            "annotation_runtime": runtime,
        },
        burst=True,
        max_burst_jobs=20,
        max_jobs=1,
        max_tries=1,
        handle_signals=False,
    )
    await worker.async_run()


async def _run_usage_arq_worker(engine: Engine, settings: Settings) -> None:
    factory = session_factory(engine)
    dispatcher = ArqJobDispatcher.from_settings(
        settings,
        session_factory=factory,
    )
    assert isinstance(dispatcher, ArqJobDispatcher)
    runtime = usage_runtime(settings)
    executor = artifact_executor(settings, factory)
    test_runtime = cast(Any, runtime)
    if not test_runtime.ddm_reader().available():
        test_runtime.ddm_reader = lambda: _IntegrationDdmReader()
        test_runtime.vlm_reader = lambda: _IntegrationVlmReader()
    worker = Worker(
        functions=[check_dataset_usage_job, generate_dataset_artifact_job],
        redis_settings=dispatcher.redis_settings,
        ctx={
            "settings": settings,
            "session_factory": factory,
            "usage_runtime": runtime,
            "artifact_executor": executor,
        },
        burst=True,
        max_burst_jobs=20,
        max_jobs=1,
        max_tries=1,
        handle_signals=False,
    )
    await worker.async_run()


def _requeue_stale_job(engine: Engine, settings: Settings, job_id: UUID) -> None:
    """模拟 worker 重启后的租约恢复，并通过真实 Redis outbox 补投任务。"""
    now = datetime.now(UTC)
    with session_factory(engine)() as session:
        repository = PostgresJobRepository(session)
        job = repository.by_id(job_id)
        assert job is not None
        started = repository.mark_running(job_id=job_id, now=now - timedelta(seconds=361))
        assert started is not None
        session.commit()
    with session_factory(engine)() as session:
        recovered = PostgresJobRepository(session).recover_stale_running(
            now=now,
            stale_after_seconds=360,
        )
        session.commit()
    assert recovered == 1
    dispatcher = ArqJobDispatcher.from_settings(
        settings,
        session_factory=session_factory(engine),
    )
    assert isinstance(dispatcher, ArqJobDispatcher)
    asyncio.run(
        dispatch_pending_jobs(
            {
                "dispatcher": dispatcher,
                "session_factory": session_factory(engine),
                "settings": settings,
            }
        )
    )


async def _run_arq_worker(engine: Engine, settings: Settings) -> int:
    dispatcher = ArqJobDispatcher.from_settings(
        settings,
        session_factory=session_factory(engine),
    )
    assert isinstance(dispatcher, ArqJobDispatcher)
    worker = Worker(
        functions=[validate_dataset_job],
        redis_settings=dispatcher.redis_settings,
        ctx={
            "settings": settings,
            "session_factory": session_factory(engine),
            "dataset_runtime": validation_runtime(settings),
        },
        burst=True,
        max_jobs=1,
        max_burst_jobs=1,
    )
    try:
        return await worker.run_check(max_burst_jobs=1)
    finally:
        await worker.close()


def _remove_object(root: Path, object_key: str) -> None:
    (root / object_key).unlink(missing_ok=True)


def _download(root: Path, object_key: str) -> bytes:
    return (root / object_key).read_bytes()


def _put_object(root: Path, object_key: str, content: bytes) -> None:
    path = root / object_key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _zip_content() -> bytes:
    buffer = io.BytesIO()
    with ZipFile(buffer, mode="w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("synthetic.txt", "not a video")
    return buffer.getvalue()


def _assert_job_was_enqueued(redis_client: Redis, job_id: UUID) -> None:
    marker = str(job_id).encode()
    keys = cast(list[bytes], redis_client.keys("arq:*"))
    assert any(marker in key for key in keys)


def test_streaming_upload_cannot_target_another_object(
    engine: Engine,
    dataset_storage_root: Path,
    real_video_bytes: bytes,
) -> None:
    settings = settings_for(engine, storage_root=dataset_storage_root)
    with client_for(engine, settings) as client:
        dataset_id = _create_dataset(client)
        try:
            requested = _request_upload(
                client,
                dataset_id,
                real_video_bytes,
                filename="bound-object.mp4",
                idempotency_key="bound-object-1",
            )
            upload = cast(dict[str, Any], requested["upload"])
            object_key = str(upload["object_key"])
            # 对象键完全由服务端从授权的数据集、成员和尝试派生；客户端只能提交内容。
            assert upload["fields"] == {}
            assert object_key.endswith(
                f"/members/{requested['member']['id']}/attempts/{requested['attempt']['id']}/video"
            )

            tampered = dict(upload)
            tampered["url"] = str(upload["url"]).replace(
                str(requested["attempt"]["id"]), str(uuid4())
            )
            refused = upload_video_content(client, tampered, real_video_bytes)

            assert refused.status_code == 404
            assert not (dataset_storage_root / object_key).exists()

            accepted = upload_video_content(client, upload, real_video_bytes)
            assert accepted.status_code == 204, accepted.text
            stored = dataset_storage_root / object_key
            assert stored.is_file()
            assert stored.read_bytes() == real_video_bytes
        finally:
            cleanup_dataset(engine, dataset_id)


def test_real_login_and_role_permissions_gate_dataset_upload(
    engine: Engine,
    dataset_storage_root: Path,
    real_video_bytes: bytes,
) -> None:
    settings = settings_for(engine, storage_root=dataset_storage_root)
    user_id, role_id, login_name, password = _seed_real_dataset_manager(engine)
    dataset_id: UUID | None = None
    object_key: str | None = None
    try:
        app = create_app(settings)
        app.state.session_factory = session_factory(engine)
        with TestClient(app, base_url="https://testserver") as client:
            login = client.post(
                f"{API_PREFIX}/auth/session",
                json={"login_name": login_name, "password": password},
            )
            assert login.status_code == 201, login.text
            csrf = client.cookies.get(CSRF_COOKIE)
            assert isinstance(csrf, str)
            csrf_headers = {CSRF_HEADER: csrf}

            created = client.post(
                DATASETS,
                headers=csrf_headers,
                json={"name": f"真实权限-{uuid4().hex[:8]}"},
            )
            assert created.status_code == 201, created.text
            dataset_id = UUID(created.json()["id"])

            requested = client.post(
                f"{DATASETS}/{dataset_id}/members",
                headers={**csrf_headers, "Idempotency-Key": "real-auth-video-1"},
                json={
                    "original_filename": "real-auth.mp4",
                    "source": "synthetic-camera",
                    "declared_size": len(real_video_bytes),
                    "declared_sha256": hashlib.sha256(real_video_bytes).hexdigest(),
                },
            )
            assert requested.status_code == 201, requested.text
            upload = requested.json()["upload"]
            assert isinstance(upload, dict)
            object_key = upload["object_key"]
            uploaded = upload_video_content(client, upload, real_video_bytes)
            assert uploaded.status_code == 204, uploaded.text

            listed = client.get(f"{DATASETS}/{dataset_id}/members")
            assert listed.status_code == 200, listed.text
            assert listed.json()["total"] == 1

            with session_factory(engine)() as database:
                PostgresRoleRepository(database).assign(user_id=user_id, role_ids=[])
                database.commit()
            denied = client.post(
                DATASETS,
                headers=csrf_headers,
                json={"name": "撤销权限后不应创建"},
            )
            assert denied.status_code == 403, denied.text
    finally:
        if object_key is not None:
            _remove_object(dataset_storage_root, object_key)
        if dataset_id is not None:
            cleanup_dataset(engine, dataset_id)
        _remove_real_dataset_manager(engine, user_id=user_id, role_id=role_id)


def test_real_ffmpeg_video_upload_records_object_and_media_facts_and_is_idempotent(
    engine: Engine,
    dataset_storage_root: Path,
    redis_server: RedisServer,
    redis_client: Redis,
    real_video_bytes: bytes,
) -> None:
    """真实流式上传、服务端校验、定稿和重复投递共同走生产实现。"""
    settings = settings_for(engine, storage_root=dataset_storage_root, redis_url=redis_server.url)
    with client_for(engine, settings) as client:
        dataset_id = _create_dataset(client)
        try:
            requested = _request_upload(
                client,
                dataset_id,
                real_video_bytes,
                filename="synthetic.mp4",
                idempotency_key="real-video-1",
            )
            upload_response = upload_video_content(client, requested["upload"], real_video_bytes)
            assert upload_response.status_code == 204, upload_response.text

            job_id = _confirm_upload(client, dataset_id, requested)
            facts = row(
                engine,
                "SELECT status, outbox_status, dispatch_attempts, last_dispatch_error "
                "FROM job_application_job WHERE id = :job_id",
                job_id=job_id,
            )
            assert tuple(facts) == ("enqueued", "dispatched", 1, None)
            _assert_job_was_enqueued(redis_client, job_id)

            _run_worker(engine, settings, job_id)
            member_id = UUID(requested["member"]["id"])
            registered = _persisted_member(engine, member_id)
            assert (
                registered.status,
                registered.actual_size,
                registered.actual_sha256,
                registered.codec,
                registered.container,
            ) == (
                MemberStatus.REGISTERED,
                len(real_video_bytes),
                hashlib.sha256(real_video_bytes).hexdigest(),
                "h264",
                "mov",
            )
            assert registered.duration_seconds is not None
            assert 0 < registered.duration_seconds <= 1.1
            assert registered.object_key is not None
            assert registered.object_key.endswith("/registered-video")
            assert registered.object_version_id == registered.object_key

            final_path = dataset_storage_root / registered.object_key
            assert final_path.stat().st_size == len(real_video_bytes)
            downloaded = _download(dataset_storage_root, registered.object_key)
            assert downloaded == real_video_bytes
            # 校验成功后临时上传对象被清理，只留下按尝试隔离的定稿文件。
            assert not (dataset_storage_root / requested["upload"]["object_key"]).exists()
            before_duplicate = registered

            # ARQ 至少一次语义会再次传递同一 job id。
            # 已结案任务不得重写成员或定稿对象。
            _run_worker(engine, settings, job_id)
            after_duplicate = _persisted_member(engine, member_id)
            assert after_duplicate == before_duplicate
            duplicate_download = _download(dataset_storage_root, registered.object_key)
            assert duplicate_download == real_video_bytes
        finally:
            cleanup_dataset(engine, dataset_id)


def test_concurrent_artifact_requests_reuse_one_postgres_artifact(engine: Engine) -> None:
    dataset_id = new_id()
    check_id = new_id()
    now = datetime.now(UTC)
    editor = Caller(
        user=caller().user,
        granted=frozenset({Permission.DATASET_VIEW, Permission.DATASET_EDIT}),
    )
    with session_factory(engine).begin() as database:
        repository = PostgresDatasetRepository(database)
        repository.add_dataset(
            TrainingDataset(
                id=dataset_id,
                name="并发制品测试集",
                created_by=editor.user.id,
                updated_by=editor.user.id,
                created_at=now,
                updated_at=now,
            )
        )
        repository.add_action_list(
            ActionListRevision(
                dataset_id=dataset_id,
                revision=1,
                actions=("(1) 取料",),
                created_by=editor.user.id,
                created_at=now,
            )
        )
        repository.add_usage_check(
            UsageCheck(
                id=check_id,
                dataset_id=dataset_id,
                kind=UsageKind.DDM,
                status=UsageCheckStatus.PASSED,
                input_digest="f" * 64,
                input_snapshot={
                    "dataset_id": str(dataset_id),
                    "base_commit": BASE_COMMIT,
                    "contract_version": DDM_CONTRACT_VERSION,
                    "consumer_parameters": dict(DDM_CONSUMER_PARAMETERS),
                    "scope": [],
                    "videos": [],
                },
                summary={},
                issues=(),
                base_commit=BASE_COMMIT,
                contract_version=DDM_CONTRACT_VERSION,
                candidate_id=None,
                job_id=None,
                created_by=editor.user.id,
                created_at=now,
                updated_at=now,
            )
        )

    barrier = Barrier(2)

    def request_in_new_transaction(_: int) -> tuple[UUID, UUID]:
        barrier.wait()
        with session_factory(engine).begin() as database:
            result = request_artifact(
                dataset_id=dataset_id,
                check_id=check_id,
                caller=editor,
                now=datetime.now(UTC),
                datasets=PostgresDatasetRepository(database),
                jobs=PostgresUsageJobQueue(database),
            )
            return result.artifact.id, result.job.id

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(request_in_new_transaction, (1, 2)))

        assert results[0] == results[1]
        assert (
            row(
                engine,
                "SELECT count(*) FROM dataset_artifact WHERE dataset_id = :dataset_id",
                dataset_id=dataset_id,
            )[0]
            == 1
        )
        assert (
            row(
                engine,
                "SELECT count(*) FROM job_application_job WHERE dataset_id = :dataset_id",
                dataset_id=dataset_id,
            )[0]
            == 1
        )
    finally:
        cleanup_dataset(engine, dataset_id)


def test_real_usage_check_and_ddm_artifact_use_postgres_local_files_and_workers(
    engine: Engine,
    dataset_storage_root: Path,
    redis_server: RedisServer,
    real_video_bytes: bytes,
    tmp_path: Path,
) -> None:
    """真实本地文件、标注入口、ARQ、PostgreSQL 和用途 worker 共同完成 DDM 闭环。"""
    settings = settings_for(
        engine,
        storage_root=dataset_storage_root,
        redis_url=redis_server.url,
    ).model_copy(
        update={
            "annotation_backend_url": "http://annotation-backend.internal:8000",
            "annotation_media_origin": "https://sop.example.internal:8444",
            "annotation_data_root": str(tmp_path),
        }
    )
    generated_object_key: str | None = None
    with client_for(
        engine,
        settings,
        permissions=frozenset(
            {Permission.DATASET_IMPORT, Permission.DATASET_VIEW, Permission.DATASET_EDIT}
        ),
    ) as client:
        dataset_id = _create_dataset(client)
        try:
            requested = _request_upload(
                client,
                dataset_id,
                real_video_bytes,
                filename="usage-check.mp4",
                idempotency_key="usage-check-1",
            )
            uploaded = upload_video_content(client, requested["upload"], real_video_bytes)
            assert uploaded.status_code == 204, uploaded.text
            validation_job_id = _confirm_upload(client, dataset_id, requested)
            _run_worker(engine, settings, validation_job_id)
            member = _persisted_member(engine, UUID(requested["member"]["id"]))
            assert member.status == MemberStatus.REGISTERED
            assert member.object_key is not None
            assert member.object_version_id == member.object_key
            assert member.actual_sha256 is not None
            assert member.duration_seconds is not None

            action_list = client.post(
                f"{DATASETS}/{dataset_id}/action-list",
                json={"actions": ["(1) 取料", "(2) 安装"]},
            )
            assert action_list.status_code == 201, action_list.text
            midpoint = member.duration_seconds / 2
            backend = _UsageAnnotationBackend(real_video_bytes)
            annotation_runtime = _UsageAnnotationRuntime(engine, settings, backend)
            context_response = client.post(
                f"{DATASETS}/{dataset_id}/members/{member.id}/annotation-context",
                json={},
            )
            assert context_response.status_code == 201, context_response.text
            context_body = context_response.json()
            asyncio.run(_run_annotation_arq_worker(engine, settings, annotation_runtime))
            prepared = client.get(
                f"{API_PREFIX}/annotation-contexts/{context_body['context_token']}"
            )
            assert prepared.status_code == 200, prepared.text
            assert prepared.json()["preparation_status"] == "succeeded"
            submitted = client.post(
                f"{DATASETS}/{dataset_id}/members/{member.id}/annotations",
                json={
                    "context_token": context_body["context_token"],
                    "mode": "single_operator",
                    "segments": [
                        {
                            "start": 0.0,
                            "end": midpoint,
                            "action_index": 0,
                            "action_description": "任意描述会被服务端替换",
                        },
                        {
                            "start": midpoint,
                            "end": member.duration_seconds,
                            "action_index": 1,
                            "action_description": "任意描述会被服务端替换",
                        },
                    ],
                },
                headers={"If-Match": "0", "Idempotency-Key": "usage-annotation-1"},
            )
            assert submitted.status_code == 202, submitted.text
            asyncio.run(_run_annotation_arq_worker(engine, settings, annotation_runtime))
            history = client.get(f"{DATASETS}/{dataset_id}/members/{member.id}/annotations")
            assert history.status_code == 200, history.text
            execution_id = UUID(history.json()["items"][0]["executions"][0]["id"])
            with session_factory(engine)() as database:
                execution = PostgresDatasetRepository(database).annotation_execution_by_id(
                    execution_id
                )
            assert execution is not None
            assert execution.upstream_data_id == backend.data_id
            assert execution.upstream_video_id == backend.video_id
            upstream_data_id = execution.upstream_data_id
            upstream_video_id = execution.upstream_video_id

            base_video = tmp_path / upstream_data_id / f"{upstream_video_id}_source.mp4"
            base_video.parent.mkdir(parents=True)
            base_video.write_bytes(real_video_bytes)
            annotation_directory = base_video.parent / base_video.stem
            annotation_directory.mkdir()
            annotation_content = json.dumps(
                [
                    {
                        "action": 1,
                        "description": "(1) 取料",
                        "start_timestamp": 0.0,
                        "end_timestamp": midpoint,
                    },
                    {
                        "action": 2,
                        "description": "(2) 安装",
                        "start_timestamp": midpoint,
                        "end_timestamp": member.duration_seconds,
                    },
                ],
                ensure_ascii=False,
            ).encode("utf-8")
            annotation_source_sha256 = hashlib.sha256(annotation_content).hexdigest()
            (annotation_directory / f"{base_video.stem}_annotation.json").write_bytes(
                annotation_content
            )

            usage_response = client.post(
                f"{DATASETS}/{dataset_id}/usage-checks",
                json={"kind": "ddm", "candidate_id": None},
            )
            assert usage_response.status_code == 202, usage_response.text
            usage_body = usage_response.json()
            check_id = UUID(usage_body["check"]["id"])
            _requeue_stale_job(engine, settings, UUID(usage_body["job"]["id"]))
            asyncio.run(_run_usage_arq_worker(engine, settings))
            checked_response = client.get(f"{DATASETS}/{dataset_id}/usage-checks/{check_id}")
            assert checked_response.status_code == 200, checked_response.text
            checked = checked_response.json()
            assert checked == UsageCheckView.model_validate(checked).model_dump(mode="json")
            assert checked["status"] == "passed", checked
            assert checked["input_digest"]
            shutil.rmtree(tmp_path / upstream_data_id)

            artifact_response = client.post(
                f"{DATASETS}/{dataset_id}/artifacts",
                json={"check_id": str(check_id)},
            )
            assert artifact_response.status_code == 202, artifact_response.text
            artifact_body = artifact_response.json()
            artifact_id = UUID(artifact_body["artifact"]["id"])
            artifact_job_id = UUID(artifact_body["job"]["id"])
            _requeue_stale_job(engine, settings, artifact_job_id)
            asyncio.run(_run_usage_arq_worker(engine, settings))
            assert (
                row(
                    engine,
                    "SELECT status FROM job_application_job WHERE id = :job_id",
                    job_id=artifact_job_id,
                )[0]
                == "succeeded"
            )
            artifact_read = client.get(f"{DATASETS}/{dataset_id}/artifacts/{artifact_id}")
            assert artifact_read.status_code == 200, artifact_read.text
            artifact = artifact_read.json()
            assert artifact == ArtifactView.model_validate(artifact).model_dump(mode="json")
            assert artifact["status"] == "available", artifact
            assert artifact["object_key"] is not None
            assert artifact["manifest"]["input_digest"] == checked["input_digest"]
            assert artifact["manifest"]["sources"][0]["annotation_source_sha256"] == (
                annotation_source_sha256
            )
            generated_object_key = artifact["object_key"]
            content = _download(dataset_storage_root, artifact["object_key"])
            annotation = json.loads(content)
            assert annotation[str(member.id)][0]["description"] == "(1) 取料"
            assert artifact["artifact_sha256"] == hashlib.sha256(content).hexdigest()
            assert artifact["artifact_size"] == len(content)
            downloaded = client.get(f"{DATASETS}/{dataset_id}/artifacts/{artifact_id}/download")
            assert downloaded.status_code == 200, downloaded.text
            assert downloaded.content == content
            assert downloaded.headers["content-disposition"] == (
                f'attachment; filename="annotation-{artifact_id}.json"'
            )
        finally:
            if generated_object_key is not None:
                _remove_object(dataset_storage_root, generated_object_key)
            cleanup_dataset(engine, dataset_id)


@pytest.mark.parametrize("candidate_kind", ["gqa", "bcq", "mcq", "golden_gqa"])
def test_real_vlm_candidate_check_uses_explicit_media_mapping(
    candidate_kind: str,
    engine: Engine,
    dataset_storage_root: Path,
    redis_server: RedisServer,
    redis_client: Redis,
    real_video_bytes: bytes,
    tmp_path: Path,
) -> None:
    settings = settings_for(
        engine,
        storage_root=dataset_storage_root,
        redis_url=redis_server.url,
    ).model_copy(update={"annotation_data_root": str(tmp_path)})
    with client_for(
        engine,
        settings,
        permissions=frozenset(
            {Permission.DATASET_IMPORT, Permission.DATASET_VIEW, Permission.DATASET_EDIT}
        ),
    ) as client:
        dataset_id = _create_dataset(client)
        try:
            requested = _request_upload(
                client,
                dataset_id,
                real_video_bytes,
                filename="vlm-source.mp4",
                idempotency_key="vlm-source-1",
            )
            uploaded = upload_video_content(client, requested["upload"], real_video_bytes)
            assert uploaded.status_code == 204, uploaded.text
            validation_job_id = _confirm_upload(client, dataset_id, requested)
            _run_worker(engine, settings, validation_job_id)
            member_id = UUID(requested["member"]["id"])
            member = _persisted_member(engine, member_id)

            action_list = client.post(
                f"{DATASETS}/{dataset_id}/action-list",
                json={"actions": ["(1) 取料"]},
            )
            assert action_list.status_code == 201, action_list.text
            record_by_kind = {
                "gqa": {
                    "question": "<video>What step is happening?",
                    "answer": "The operator is picking material.",
                },
                "bcq": {
                    "question": "<video>Is the operator picking material?",
                    "answer": "yes",
                },
                "mcq": {
                    "question": "<video>选择动作：\n(1) 取料",
                    "answer": "(1) 取料",
                },
                "golden_gqa": {
                    "question": "<video>What is the operator doing?",
                    "answer": "Picking material.",
                },
            }[candidate_kind]
            candidate = client.post(
                f"{DATASETS}/{dataset_id}/vlm-candidates",
                headers={"If-Match": "0"},
                json={
                    "kind": candidate_kind,
                    "action_list_revision": 1,
                    "records": [
                        {
                            "conversations": [
                                {"from": "human", "value": record_by_kind["question"]},
                                {"from": "gpt", "value": record_by_kind["answer"]},
                            ],
                            "video": "vlm-source.mp4",
                        }
                    ],
                    "media": [
                        {
                            "key": "vlm-source.mp4",
                            "member_id": str(member_id),
                            "source_object_version_id": member.object_version_id,
                            "source_sha256": member.actual_sha256,
                            **({"action_indices": [1]} if candidate_kind == "mcq" else {}),
                        }
                    ],
                },
            )
            assert candidate.status_code == 201, candidate.text
            candidate_body = candidate.json()
            check_response = client.post(
                f"{DATASETS}/{dataset_id}/usage-checks",
                json={"kind": "vlm", "candidate_id": candidate_body["id"]},
            )
            assert check_response.status_code == 202, check_response.text
            check_body = check_response.json()

            vlm_job_id = UUID(check_body["job"]["id"])
            _assert_job_was_enqueued(redis_client, vlm_job_id)
            asyncio.run(_run_usage_arq_worker(engine, settings))

            checked = client.get(
                f"{DATASETS}/{dataset_id}/usage-checks/{check_body['check']['id']}"
            )
            assert checked.status_code == 200, checked.text
            checked_body = checked.json()
            assert checked_body == UsageCheckView.model_validate(checked_body).model_dump(
                mode="json"
            )
            assert checked_body["status"] == "passed"
            assert checked_body["is_current"] is True
            assert checked_body["summary"] == {"record_count": 1, "media_count": 1}
        finally:
            cleanup_dataset(engine, dataset_id)


def test_real_arq_worker_consumes_validation_job_from_redis(
    engine: Engine,
    dataset_storage_root: Path,
    redis_server: RedisServer,
    redis_client: Redis,
    real_video_bytes: bytes,
) -> None:
    settings = settings_for(engine, storage_root=dataset_storage_root, redis_url=redis_server.url)
    with client_for(engine, settings) as client:
        dataset_id = _create_dataset(client)
        try:
            requested = _request_upload(
                client,
                dataset_id,
                real_video_bytes,
                filename="arq-worker.mp4",
                idempotency_key="arq-worker-1",
            )
            uploaded = upload_video_content(client, requested["upload"], real_video_bytes)
            assert uploaded.status_code == 204, uploaded.text
            job_id = _confirm_upload(client, dataset_id, requested)
            _assert_job_was_enqueued(redis_client, job_id)

            assert asyncio.run(_run_arq_worker(engine, settings)) == 1
            member = _persisted_member(engine, UUID(requested["member"]["id"]))
            assert member.status == MemberStatus.REGISTERED
            assert member.actual_sha256 == hashlib.sha256(real_video_bytes).hexdigest()
        finally:
            cleanup_dataset(engine, dataset_id)


def test_archive_filename_is_rejected_before_local_storage(
    engine: Engine,
    dataset_storage_root: Path,
) -> None:
    settings = settings_for(engine, storage_root=dataset_storage_root)
    with client_for(engine, settings) as client:
        dataset_id = _create_dataset(client)
        try:
            response = client.post(
                f"{DATASETS}/{dataset_id}/members",
                json={
                    "original_filename": "batch.zip",
                    "source": "synthetic-camera",
                    "declared_size": 4,
                    "declared_sha256": "a" * 64,
                },
            )
            assert response.status_code == 422
            assert response.json()["error_code"] == "ARCHIVE_REJECTED"
            assert response.json()["recovery_action"] == "retry_upload"
        finally:
            cleanup_dataset(engine, dataset_id)


def test_archive_content_disguised_as_video_is_rejected_by_real_worker(
    engine: Engine,
    dataset_storage_root: Path,
) -> None:
    settings = settings_for(engine, storage_root=dataset_storage_root)
    content = _zip_content()
    with client_for(engine, settings) as client:
        dataset_id = _create_dataset(client)
        try:
            requested = _request_upload(
                client,
                dataset_id,
                content,
                filename="archive-disguised.mp4",
                idempotency_key="archive-content-1",
            )
            upload_response = upload_video_content(client, requested["upload"], content)
            assert upload_response.status_code == 204, upload_response.text
            job_id = _confirm_upload(client, dataset_id, requested)
            _run_worker(engine, settings, job_id)

            member = _persisted_member(engine, UUID(requested["member"]["id"]))
            assert (
                member.status,
                member.failure_code,
                member.recovery_action,
                member.actual_size,
                member.actual_sha256,
            ) == (
                MemberStatus.FAILED,
                "ARCHIVE_CONTENT_REJECTED",
                RetryMode.UPLOAD,
                len(content),
                hashlib.sha256(content).hexdigest(),
            )
        finally:
            cleanup_dataset(engine, dataset_id)


def test_real_object_size_mismatch_is_rejected_from_actual_local_stat(
    engine: Engine,
    dataset_storage_root: Path,
    real_video_bytes: bytes,
) -> None:
    settings = settings_for(engine, storage_root=dataset_storage_root)
    with client_for(engine, settings) as client:
        dataset_id = _create_dataset(client)
        try:
            requested = _request_upload(
                client,
                dataset_id,
                real_video_bytes,
                filename="size-mismatch.mp4",
                idempotency_key="size-mismatch-1",
            )
            object_key = requested["upload"]["object_key"]
            _put_object(
                dataset_storage_root,
                object_key,
                real_video_bytes + b"unexpected-byte",
            )
            job_id = _confirm_upload(client, dataset_id, requested)
            _run_worker(engine, settings, job_id)

            member = _persisted_member(engine, UUID(requested["member"]["id"]))
            assert (
                member.status,
                member.failure_code,
                member.recovery_action,
                member.actual_size,
            ) == (
                MemberStatus.FAILED,
                "SIZE_MISMATCH",
                RetryMode.UPLOAD,
                len(real_video_bytes) + len(b"unexpected-byte"),
            )
        finally:
            cleanup_dataset(engine, dataset_id)


def test_real_object_sha256_mismatch_is_rejected_from_downloaded_content(
    engine: Engine,
    dataset_storage_root: Path,
    real_video_bytes: bytes,
) -> None:
    settings = settings_for(engine, storage_root=dataset_storage_root)
    wrong_content = bytes([real_video_bytes[0] ^ 1]) + real_video_bytes[1:]
    with client_for(engine, settings) as client:
        dataset_id = _create_dataset(client)
        try:
            requested = _request_upload(
                client,
                dataset_id,
                real_video_bytes,
                filename="sha-mismatch.mp4",
                idempotency_key="sha-mismatch-1",
                declared_sha256=hashlib.sha256(real_video_bytes).hexdigest(),
            )
            _put_object(
                dataset_storage_root,
                requested["upload"]["object_key"],
                wrong_content,
            )
            job_id = _confirm_upload(client, dataset_id, requested)
            _run_worker(engine, settings, job_id)

            member = _persisted_member(engine, UUID(requested["member"]["id"]))
            assert (
                member.status,
                member.failure_code,
                member.recovery_action,
                member.actual_size,
                member.actual_sha256,
            ) == (
                MemberStatus.FAILED,
                "SHA256_MISMATCH",
                RetryMode.UPLOAD,
                len(wrong_content),
                hashlib.sha256(wrong_content).hexdigest(),
            )
        finally:
            cleanup_dataset(engine, dataset_id)


def test_real_h264_is_rejected_when_deployment_does_not_allow_its_codec(
    engine: Engine,
    dataset_storage_root: Path,
    real_video_bytes: bytes,
) -> None:
    settings = settings_for(engine, storage_root=dataset_storage_root).model_copy(
        update={"dataset_supported_codecs": "h265"}
    )
    with client_for(engine, settings) as client:
        dataset_id = _create_dataset(client)
        try:
            requested = _request_upload(
                client,
                dataset_id,
                real_video_bytes,
                filename="unsupported-codec.mp4",
                idempotency_key="unsupported-codec-1",
            )
            uploaded = upload_video_content(client, requested["upload"], real_video_bytes)
            assert uploaded.status_code == 204, uploaded.text
            job_id = _confirm_upload(client, dataset_id, requested)
            _run_worker(engine, settings, job_id)

            member = _persisted_member(engine, UUID(requested["member"]["id"]))
            assert (member.status, member.failure_code, member.recovery_action) == (
                MemberStatus.FAILED,
                "UNSUPPORTED_CODEC",
                RetryMode.UPLOAD,
            )
        finally:
            cleanup_dataset(engine, dataset_id)


def test_missing_object_failure_can_retry_upload_and_register_real_video(
    engine: Engine,
    dataset_storage_root: Path,
    real_video_bytes: bytes,
) -> None:
    settings = settings_for(engine, storage_root=dataset_storage_root)
    with client_for(engine, settings) as client:
        dataset_id = _create_dataset(client)
        try:
            requested = _request_upload(
                client,
                dataset_id,
                real_video_bytes,
                filename="missing-first.mp4",
                idempotency_key="missing-object-1",
            )
            old_member_id = UUID(requested["member"]["id"])
            old_attempt_id = UUID(requested["attempt"]["id"])
            old_job_id = _confirm_upload(client, dataset_id, requested)
            _run_worker(engine, settings, old_job_id)

            failed = _persisted_member(engine, old_member_id)
            assert (failed.status, failed.failure_code, failed.recovery_action) == (
                MemberStatus.FAILED,
                "OBJECT_NOT_FOUND",
                RetryMode.UPLOAD,
            )

            retried_response = client.post(
                f"{DATASETS}/{dataset_id}/members/{old_member_id}/retry",
                json={"mode": RetryMode.UPLOAD},
            )
            assert retried_response.status_code == 200, retried_response.text
            retried = retried_response.json()
            assert UUID(retried["attempt"]["id"]) != old_attempt_id
            assert retried["upload"] is not None
            old_attempt = _persisted_attempt(engine, old_attempt_id)
            assert old_attempt.status == AttemptStatus.SUPERSEDED

            upload_response = upload_video_content(client, retried["upload"], real_video_bytes)
            assert upload_response.status_code == 204, upload_response.text
            new_job_id = _confirm_upload(client, dataset_id, retried)
            _run_worker(engine, settings, new_job_id)
            registered = _persisted_member(engine, old_member_id)
            assert registered.status == MemberStatus.REGISTERED
            assert registered.actual_sha256 == hashlib.sha256(real_video_bytes).hexdigest()
        finally:
            cleanup_dataset(engine, dataset_id)


def test_expired_upload_authorization_fails_then_fresh_retry_succeeds(
    engine: Engine,
    dataset_storage_root: Path,
    real_video_bytes: bytes,
) -> None:
    settings = settings_for(engine, storage_root=dataset_storage_root, upload_ttl_seconds=1)
    with client_for(engine, settings) as client:
        dataset_id = _create_dataset(client)
        try:
            requested = _request_upload(
                client,
                dataset_id,
                real_video_bytes,
                filename="expired-first.mp4",
                idempotency_key="expired-upload-1",
            )
            time.sleep(2.1)
            expired_response = upload_video_content(client, requested["upload"], real_video_bytes)
            assert expired_response.status_code == 409, expired_response.text

            old_member_id = UUID(requested["member"]["id"])
            old_job_id = _confirm_upload(client, dataset_id, requested)
            _run_worker(engine, settings, old_job_id)
            failed = _persisted_member(engine, old_member_id)
            assert (failed.failure_code, failed.recovery_action) == (
                "OBJECT_NOT_FOUND",
                RetryMode.UPLOAD,
            )

            retried_response = client.post(
                f"{DATASETS}/{dataset_id}/members/{old_member_id}/retry",
                json={"mode": RetryMode.UPLOAD},
            )
            assert retried_response.status_code == 200, retried_response.text
            retried = retried_response.json()
            fresh_response = upload_video_content(client, retried["upload"], real_video_bytes)
            assert fresh_response.status_code == 204, fresh_response.text
            new_job_id = _confirm_upload(client, dataset_id, retried)
            _run_worker(engine, settings, new_job_id)
            registered = _persisted_member(engine, old_member_id)
            assert registered.status == MemberStatus.REGISTERED
        finally:
            cleanup_dataset(engine, dataset_id)


def test_media_probe_outage_can_retry_validation_without_reupload(
    engine: Engine,
    dataset_storage_root: Path,
    real_video_bytes: bytes,
) -> None:
    good_settings = settings_for(engine, storage_root=dataset_storage_root)
    unavailable_settings = good_settings.model_copy(
        update={"media_probe_binary": "ffprobe-not-installed-for-test"}
    )
    with client_for(engine, unavailable_settings) as client:
        dataset_id = _create_dataset(client)
        try:
            requested = _request_upload(
                client,
                dataset_id,
                real_video_bytes,
                filename="probe-outage.mp4",
                idempotency_key="probe-outage-1",
            )
            uploaded = upload_video_content(client, requested["upload"], real_video_bytes)
            assert uploaded.status_code == 204, uploaded.text
            first_job_id = _confirm_upload(client, dataset_id, requested)
            _run_worker(engine, unavailable_settings, first_job_id)

            member_id = UUID(requested["member"]["id"])
            failed = _persisted_member(engine, member_id)
            assert (failed.failure_code, failed.recovery_action) == (
                "MEDIA_PROBE_UNAVAILABLE",
                RetryMode.VALIDATION,
            )

            retried_response = client.post(
                f"{DATASETS}/{dataset_id}/members/{member_id}/retry",
                json={"mode": RetryMode.VALIDATION},
            )
            assert retried_response.status_code == 202, retried_response.text
            retried = retried_response.json()
            assert retried["attempt"]["id"] == requested["attempt"]["id"]
            assert retried["attempt"]["status"] == AttemptStatus.PENDING_VALIDATION
            assert retried["upload"] is None
            assert retried["job"]["status"] == "pending"
            second_job_id = UUID(retried["job"]["id"])

            _run_worker(engine, good_settings, second_job_id)
            registered = _persisted_member(engine, member_id)
            assert registered.status == MemberStatus.REGISTERED
            assert registered.actual_sha256 == hashlib.sha256(real_video_bytes).hexdigest()
        finally:
            cleanup_dataset(engine, dataset_id)


def test_concurrent_upload_requests_reuse_one_postgres_idempotent_attempt(
    engine: Engine,
    dataset_storage_root: Path,
) -> None:
    settings = settings_for(engine, storage_root=dataset_storage_root)
    content = b"concurrent-idempotent-upload"
    declared_sha256 = hashlib.sha256(content).hexdigest()
    with client_for(engine, settings) as client:
        dataset_id = _create_dataset(client)
        try:
            barrier = Barrier(2)

            def request_in_new_transaction(_: int) -> tuple[UUID, UUID]:
                barrier.wait()
                with session_factory(engine).begin() as database:
                    result = request_video_upload(
                        dataset_id=dataset_id,
                        original_filename="concurrent.mp4",
                        source="synthetic-camera",
                        declared_size=len(content),
                        declared_sha256=declared_sha256,
                        idempotency_key="concurrent-upload-1",
                        caller=caller(),
                        now=datetime.now(UTC),
                        datasets=PostgresDatasetRepository(database),
                        max_upload_bytes=settings.dataset_max_upload_bytes,
                        upload_ttl_seconds=settings.dataset_upload_ttl_seconds,
                    )
                    return result.member.id, result.attempt.id

            with ThreadPoolExecutor(max_workers=2) as workers:
                results = list(workers.map(request_in_new_transaction, (1, 2)))

            assert results[0] == results[1]
            assert (
                row(
                    engine,
                    "SELECT count(*) FROM dataset_member WHERE dataset_id = :dataset_id",
                    dataset_id=dataset_id,
                )[0]
                == 1
            )
            assert (
                row(
                    engine,
                    "SELECT count(*) FROM dataset_upload_attempt WHERE dataset_id = :dataset_id",
                    dataset_id=dataset_id,
                )[0]
                == 1
            )
        finally:
            cleanup_dataset(engine, dataset_id)


def test_concurrent_confirmations_create_one_postgres_validation_job(
    engine: Engine,
    dataset_storage_root: Path,
) -> None:
    settings = settings_for(engine, storage_root=dataset_storage_root)
    with client_for(engine, settings) as client:
        dataset_id = _create_dataset(client)
        try:
            requested = _request_upload(
                client,
                dataset_id,
                b"placeholder",
                filename="concurrent.mp4",
                idempotency_key="concurrent-confirm-1",
            )
            member_id = UUID(requested["member"]["id"])
            attempt_id = UUID(requested["attempt"]["id"])

            def confirm_in_new_transaction(_: int) -> UUID:
                with session_factory(engine).begin() as database:
                    result = confirm_video_upload(
                        dataset_id=dataset_id,
                        member_id=member_id,
                        attempt_id=attempt_id,
                        caller=caller(),
                        now=datetime.now(UTC),
                        datasets=PostgresDatasetRepository(database),
                        jobs=PostgresValidationJobQueue(database),
                    )
                    assert result.job is not None
                    return result.job.id

            with ThreadPoolExecutor(max_workers=2) as workers:
                job_ids = list(workers.map(confirm_in_new_transaction, (1, 2)))

            assert job_ids[0] == job_ids[1]
            duplicate_job_id = _confirm_upload(client, dataset_id, requested)
            assert duplicate_job_id == job_ids[0]
            facts = row(
                engine,
                "SELECT status, validation_job_id FROM dataset_member WHERE id = :member_id",
                member_id=member_id,
            )
            assert tuple(facts) == (MemberStatus.PENDING_VALIDATION, job_ids[0])
            assert (
                row(
                    engine,
                    "SELECT count(*) FROM job_application_job WHERE attempt_id = :attempt_id",
                    attempt_id=attempt_id,
                )[0]
                == 1
            )
        finally:
            cleanup_dataset(engine, dataset_id)
