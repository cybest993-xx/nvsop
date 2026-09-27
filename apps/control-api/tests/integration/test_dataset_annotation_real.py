"""训练数据标注的真实 PostgreSQL、本地持久卷、HTTP 和 worker 事务边界。"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, cast
from uuid import UUID, uuid4

import pytest
from _integration_support import (
    RedisServer,
    cleanup_dataset,
    settings_for,
    upload_video_content,
)
from arq import Worker
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.app import API_PREFIX, create_app
from factory_sop.auth.adapters.cookies import CSRF_COOKIE, CSRF_HEADER
from factory_sop.auth.adapters.repository import PostgresRoleRepository, PostgresUserRepository
from factory_sop.auth.model import Role, User, UserStatus
from factory_sop.auth.passwords import hash_password
from factory_sop.auth.permissions import Permission
from factory_sop.dataset.adapters import dependencies as dataset_dependencies
from factory_sop.dataset.adapters.media import FfprobeMediaProbe
from factory_sop.dataset.adapters.repository import PostgresDatasetRepository
from factory_sop.dataset.adapters.storage import LocalFileObjectStorage
from factory_sop.dataset.annotation import AnnotationBackend, PreparedAnnotationVideo
from factory_sop.dataset.api import DatasetAnnotationRuntime
from factory_sop.dataset.model import AnnotationMode, AnnotationSegment
from factory_sop.identifiers import new_id
from factory_sop.job.adapters.dispatcher import ArqJobDispatcher
from factory_sop.job.adapters.worker import (
    annotate_dataset_job,
    prepare_annotation_context_job,
    validate_dataset_job,
)
from factory_sop.persistence import session_factory
from factory_sop.settings import Settings

DATASETS = f"{API_PREFIX}/training-datasets"


@dataclass
class FakeAnnotationBackend:
    """只替换 HTTP 基座 adapter；源对象、事务和任务仍走真实实现。"""

    source: bytes | None = None
    copies: list[str] | None = None

    def __post_init__(self) -> None:
        self.copies = []

    def prepare_video(
        self,
        *,
        source: BinaryIO,
        filename: str,
        actions: Sequence[str],
    ) -> PreparedAnnotationVideo:
        assert filename.endswith(".mp4")
        assert list(actions) == ["(1) 取料"]
        self.source = source.read()
        assert self.source
        assert self.copies is not None
        video_id = f"real-test-video-{len(self.copies) + 1}"
        self.copies.append(video_id)
        return PreparedAnnotationVideo(
            data_id=f"real-test-data-{len(self.copies)}",
            video_id=video_id,
        )

    def download_video(self, *, video_id: str, destination: BinaryIO) -> None:
        assert self.copies is not None
        assert video_id in self.copies
        assert self.source is not None
        destination.write(self.source)

    def split_video(
        self,
        *,
        video_id: str,
        segments: Sequence[AnnotationSegment],
        mode: AnnotationMode,
    ) -> list[dict[str, Any]]:
        assert self.copies is not None
        assert video_id in self.copies
        assert len(segments) == 1
        assert mode is AnnotationMode.SINGLE_OPERATOR
        return [
            {
                "id": f"real-test-clip-{video_id}",
                "filename": "clip.mp4",
                "start_time": segments[0].start,
                "end_time": segments[0].end,
            }
        ]


class FakeAnnotationRuntime:
    def __init__(self, engine: Engine, settings: Settings, backend: FakeAnnotationBackend) -> None:
        self.engine = engine
        self.settings = settings
        self.annotation_backend = backend

    def repository(self, session: object) -> PostgresDatasetRepository:
        return PostgresDatasetRepository(cast(DatabaseSession, session))

    def storage(self) -> LocalFileObjectStorage:
        return LocalFileObjectStorage.from_settings(self.settings)

    def backend(self) -> AnnotationBackend:
        return self.annotation_backend

    def media_probe(self) -> FfprobeMediaProbe:
        return FfprobeMediaProbe(
            binary=self.settings.media_probe_binary,
            timeout_seconds=self.settings.media_probe_timeout_seconds,
        )


def _seed_manager(engine: Engine) -> tuple[UUID, UUID, str, str]:
    user_id = new_id()
    role_id = new_id()
    login_name = f"annotation-manager-{uuid4().hex[:12]}"
    password = f"annotation-password-{uuid4().hex}"
    user = User(
        id=user_id,
        login_name=login_name,
        display_name="真实标注管理员",
        password_hash=hash_password(password),
        status=UserStatus.ACTIVE,
        created_by=user_id,
        updated_by=user_id,
    )
    role = Role(
        id=role_id,
        code=f"annotation-manager-{uuid4().hex[:12]}",
        name="真实标注管理员角色",
        permissions=frozenset(
            {
                Permission.DATASET_IMPORT,
                Permission.DATASET_VIEW,
                Permission.DATASET_EDIT,
            }
        ),
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


def _remove_manager(engine: Engine, *, user_id: UUID, role_id: UUID) -> None:
    with session_factory(engine)() as database:
        PostgresRoleRepository(database).remove(role_id)
        PostgresUserRepository(database).remove(user_id)
        database.commit()


def _annotation_app(engine: Engine, settings: Settings) -> TestClient:
    app = create_app(settings)
    factory = session_factory(engine)
    app.state.session_factory = factory
    app.state.job_dispatcher = ArqJobDispatcher.from_settings(settings, session_factory=factory)
    return TestClient(app, base_url="https://testserver")


async def _run_validation(engine: Engine, settings: Settings, job_id: UUID) -> None:
    await validate_dataset_job(
        {
            "settings": settings,
            "session_factory": session_factory(engine),
            "dataset_runtime": _ValidationRuntime(engine, settings),
        },
        str(job_id),
    )


class _ValidationRuntime:
    def __init__(self, engine: Engine, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings

    def repository(self, session: object) -> PostgresDatasetRepository:
        return PostgresDatasetRepository(cast(DatabaseSession, session))

    def storage(self) -> LocalFileObjectStorage:
        return LocalFileObjectStorage.from_settings(self.settings)

    def media_probe(self) -> FfprobeMediaProbe:
        return FfprobeMediaProbe(
            binary=self.settings.media_probe_binary,
            timeout_seconds=self.settings.media_probe_timeout_seconds,
        )

    def supported_codecs(self) -> frozenset[str]:
        return frozenset({"h264", "h265"})


async def _run_actual_arq_worker(
    engine: Engine,
    settings: Settings,
    *,
    annotation_runtime: DatasetAnnotationRuntime | None = None,
) -> None:
    """用真实 ARQ worker 消费 Redis 中已经提交的任务。"""
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
            "dataset_runtime": _ValidationRuntime(engine, settings),
            "annotation_runtime": annotation_runtime
            or dataset_dependencies.annotation_runtime(settings),
        },
        burst=True,
        max_burst_jobs=20,
        max_jobs=1,
        max_tries=1,
        handle_signals=False,
    )
    await worker.async_run()


def _login(client: TestClient, login_name: str, password: str) -> dict[str, str]:
    response = client.post(
        f"{API_PREFIX}/auth/session",
        json={"login_name": login_name, "password": password},
    )
    assert response.status_code == 201, response.text
    csrf = client.cookies.get(CSRF_COOKIE)
    assert isinstance(csrf, str)
    return {CSRF_HEADER: csrf}


def _create_dataset(client: TestClient, headers: dict[str, str]) -> UUID:
    response = client.post(
        DATASETS,
        headers=headers,
        json={"name": f"真实标注-{uuid4().hex[:8]}"},
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


def _persisted_duration(engine: Engine, member_id: UUID) -> float:
    with session_factory(engine)() as database:
        member = PostgresDatasetRepository(database).member_by_id(member_id)
    assert member is not None
    assert member.duration_seconds is not None
    return member.duration_seconds


def _remove_object(root: Path, object_key: str) -> None:
    (root / object_key).unlink(missing_ok=True)


def test_real_http_annotation_persists_revisions_and_isolates_retry_copies(
    engine: Engine,
    dataset_storage_root: Path,
    redis_server: RedisServer,
    real_video_bytes: bytes,
) -> None:
    settings = settings_for(
        engine, storage_root=dataset_storage_root, redis_url=redis_server.url
    ).model_copy(
        update={
            "annotation_backend_url": "http://annotation-backend.internal:8000",
            "annotation_media_origin": "https://sop.example.internal:8444",
        }
    )
    user_id, role_id, login_name, password = _seed_manager(engine)
    dataset_id: UUID | None = None
    object_key: str | None = None
    try:
        with _annotation_app(engine, settings) as client:
            csrf = _login(client, login_name, password)
            dataset_id = _create_dataset(client, csrf)
            action_list = client.post(
                f"{DATASETS}/{dataset_id}/action-list",
                headers=csrf,
                json={"actions": ["(1) 取料"]},
            )
            assert action_list.status_code == 201, action_list.text

            requested = client.post(
                f"{DATASETS}/{dataset_id}/members",
                headers={**csrf, "Idempotency-Key": "annotation-real-upload"},
                json={
                    "original_filename": "annotation-real.mp4",
                    "source": "synthetic-camera",
                    "declared_size": len(real_video_bytes),
                    "declared_sha256": hashlib.sha256(real_video_bytes).hexdigest(),
                },
            )
            assert requested.status_code == 201, requested.text
            upload = requested.json()["upload"]
            object_key = upload["object_key"]
            assert upload_video_content(client, upload, real_video_bytes).status_code == 204
            confirm = client.post(
                f"{DATASETS}/{dataset_id}/members/{requested.json()['member']['id']}/confirm",
                headers=csrf,
                json={"attempt_id": requested.json()["attempt"]["id"]},
            )
            assert confirm.status_code == 202, confirm.text
            validation_job_id = UUID(confirm.json()["job"]["id"])
            asyncio.run(_run_validation(engine, settings, validation_job_id))

            member_id = UUID(requested.json()["member"]["id"])
            duration = _persisted_duration(engine, member_id)
            context = client.post(
                f"{DATASETS}/{dataset_id}/members/{member_id}/annotation-context",
                headers=csrf,
                json={},
            )
            assert context.status_code == 201, context.text
            context_body = context.json()
            assert context_body["video_url"].startswith(
                "https://sop.example.internal:8444/annotation/media/videos/"
            )

            runtime = FakeAnnotationRuntime(engine, settings, FakeAnnotationBackend())
            asyncio.run(
                _run_actual_arq_worker(
                    engine,
                    settings,
                    annotation_runtime=runtime,
                )
            )

            submitted = client.post(
                f"{DATASETS}/{dataset_id}/members/{member_id}/annotations",
                headers={
                    **csrf,
                    "Idempotency-Key": "annotation-real-1",
                    "If-Match": "0",
                },
                json={
                    "context_token": context_body["context_token"],
                    "mode": "single_operator",
                    "segments": [
                        {
                            "start": 0.0,
                            "end": duration,
                            "action_index": 0,
                            "action_description": "客户端不能改变权威描述",
                        }
                    ],
                },
            )
            assert submitted.status_code == 202, submitted.text
            asyncio.run(
                _run_actual_arq_worker(
                    engine,
                    settings,
                    annotation_runtime=runtime,
                )
            )

            history = client.get(f"{DATASETS}/{dataset_id}/members/{member_id}/annotations")
            assert history.status_code == 200, history.text
            first_submission = history.json()["items"][0]
            assert first_submission["segments"][0]["action_description"] == "(1) 取料"
            assert first_submission["executions"][0]["status"] == "succeeded"
            first_execution_id = first_submission["executions"][0]["id"]

            retried = client.post(
                f"{DATASETS}/{dataset_id}/members/{member_id}/annotations/"
                f"{first_submission['id']}/retry",
                headers=csrf,
            )
            assert retried.status_code == 202, retried.text
            asyncio.run(
                _run_actual_arq_worker(
                    engine,
                    settings,
                    annotation_runtime=runtime,
                )
            )

            refreshed = client.get(f"{DATASETS}/{dataset_id}/members/{member_id}/annotations")
            assert refreshed.status_code == 200, refreshed.text
            executions = refreshed.json()["items"][0]["executions"]
            assert [item["status"] for item in executions] == ["succeeded", "succeeded"]
            assert executions[0]["id"] == first_execution_id
            assert executions[0]["clips"]
            assert executions[1]["clips"]
            assert executions[0]["clips"][0]["id"] != executions[1]["clips"][0]["id"]
            assert runtime.annotation_backend.copies == [
                "real-test-video-1",
                "real-test-video-2",
                "real-test-video-3",
            ]
    finally:
        if object_key is not None:
            _remove_object(dataset_storage_root, object_key)
        if dataset_id is not None:
            cleanup_dataset(engine, dataset_id)
        _remove_manager(engine, user_id=user_id, role_id=role_id)


def test_real_nvidia_annotation_backend_and_arq_worker_complete_a_submission(
    engine: Engine,
    dataset_storage_root: Path,
    redis_server: RedisServer,
    real_video_bytes: bytes,
) -> None:
    """在显式部署真实基座时验证 HTTP adapter、ARQ worker 和真实切片。"""
    backend_url = os.getenv("NVSOP_ANNOTATION_BACKEND_URL")
    media_origin = os.getenv("NVSOP_ANNOTATION_MEDIA_ORIGIN")
    if not backend_url or not media_origin:
        pytest.skip(
            "需要 NVSOP_ANNOTATION_BACKEND_URL 和 NVSOP_ANNOTATION_MEDIA_ORIGIN 才运行真实基座闭环"
        )
    settings = settings_for(
        engine,
        storage_root=dataset_storage_root,
        redis_url=redis_server.url,
    ).model_copy(
        update={
            "annotation_backend_url": backend_url,
            "annotation_media_origin": media_origin,
        }
    )
    user_id, role_id, login_name, password = _seed_manager(engine)
    dataset_id: UUID | None = None
    object_key: str | None = None
    try:
        with _annotation_app(engine, settings) as client:
            csrf = _login(client, login_name, password)
            dataset_id = _create_dataset(client, csrf)
            action_list = client.post(
                f"{DATASETS}/{dataset_id}/action-list",
                headers=csrf,
                json={"actions": ["(1) 取料"]},
            )
            assert action_list.status_code == 201, action_list.text

            requested = client.post(
                f"{DATASETS}/{dataset_id}/members",
                headers={**csrf, "Idempotency-Key": "annotation-vendor-upload"},
                json={
                    "original_filename": "annotation-vendor.mp4",
                    "source": "synthetic-camera",
                    "declared_size": len(real_video_bytes),
                    "declared_sha256": hashlib.sha256(real_video_bytes).hexdigest(),
                },
            )
            assert requested.status_code == 201, requested.text
            request_body = requested.json()
            object_key = request_body["upload"]["object_key"]
            assert (
                upload_video_content(client, request_body["upload"], real_video_bytes).status_code
                == 204
            )
            confirm = client.post(
                f"{DATASETS}/{dataset_id}/members/{request_body['member']['id']}/confirm",
                headers=csrf,
                json={"attempt_id": request_body["attempt"]["id"]},
            )
            assert confirm.status_code == 202, confirm.text
            asyncio.run(_run_actual_arq_worker(engine, settings))

            member_id = UUID(request_body["member"]["id"])
            context_response = client.post(
                f"{DATASETS}/{dataset_id}/members/{member_id}/annotation-context",
                headers=csrf,
                json={},
            )
            assert context_response.status_code == 201, context_response.text
            context_body = context_response.json()
            asyncio.run(_run_actual_arq_worker(engine, settings))
            prepared = client.get(
                f"{API_PREFIX}/annotation-contexts/{context_body['context_token']}",
                headers=csrf,
            )
            assert prepared.status_code == 200, prepared.text
            assert prepared.json()["preparation_status"] == "succeeded"

            duration = prepared.json()["duration_seconds"]
            assert isinstance(duration, (int, float))
            assert duration > 0
            submitted = client.post(
                f"{DATASETS}/{dataset_id}/members/{member_id}/annotations",
                headers={
                    **csrf,
                    "Idempotency-Key": "annotation-vendor-submission",
                    "If-Match": "0",
                },
                json={
                    "context_token": context_body["context_token"],
                    "mode": "single_operator",
                    "segments": [{"start": 0.0, "end": duration, "action_index": 0}],
                },
            )
            assert submitted.status_code == 202, submitted.text
            asyncio.run(_run_actual_arq_worker(engine, settings))

            history = client.get(
                f"{DATASETS}/{dataset_id}/members/{member_id}/annotations",
                headers=csrf,
            )
            assert history.status_code == 200, history.text
            submission = history.json()["items"][0]
            execution = submission["executions"][0]
            assert execution["status"] == "succeeded"
            assert execution["clips"]
    finally:
        if object_key is not None:
            _remove_object(dataset_storage_root, object_key)
        if dataset_id is not None:
            cleanup_dataset(engine, dataset_id)
        _remove_manager(engine, user_id=user_id, role_id=role_id)
