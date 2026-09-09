"""训练视频的真实 PostgreSQL、MinIO 和 ffprobe 生命周期。"""

from __future__ import annotations

import asyncio
import hashlib
import io
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from typing import Any, cast
from urllib.request import Request, urlopen
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from _integration_support import (
    MinioServer,
    RedisServer,
    caller,
    cleanup_dataset,
    client_for,
    row,
    settings_for,
    upload_presigned,
)
from arq import Worker
from fastapi.testclient import TestClient
from minio import Minio
from minio.error import S3Error
from redis import Redis
from sqlalchemy import Engine

from factory_sop.app import API_PREFIX, create_app
from factory_sop.auth.adapters.cookies import CSRF_COOKIE, CSRF_HEADER
from factory_sop.auth.adapters.repository import PostgresRoleRepository, PostgresUserRepository
from factory_sop.auth.model import Role, User, UserStatus
from factory_sop.auth.passwords import hash_password
from factory_sop.auth.permissions import Permission
from factory_sop.dataset.adapters.dependencies import validation_runtime
from factory_sop.dataset.adapters.repository import PostgresDatasetRepository
from factory_sop.dataset.adapters.storage import MinioObjectStorage
from factory_sop.dataset.model import (
    AttemptStatus,
    DatasetMember,
    MemberStatus,
    RetryMode,
    UploadAttempt,
)
from factory_sop.dataset.usecases import confirm_video_upload, request_video_upload
from factory_sop.identifiers import new_id
from factory_sop.job.adapters.dispatcher import ArqJobDispatcher
from factory_sop.job.adapters.repository import PostgresValidationJobQueue
from factory_sop.job.adapters.worker import validate_dataset_job
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
) -> dict[str, Any]:
    response = client.post(
        f"{DATASETS}/{dataset_id}/members",
        headers={"Idempotency-Key": idempotency_key},
        json={
            "original_filename": filename,
            "source": "synthetic-camera",
            "declared_size": len(content),
            "declared_sha256": hashlib.sha256(content).hexdigest(),
        },
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


def _run_arq_worker(engine: Engine, settings: Settings) -> int:
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
    return asyncio.run(worker.run_check(max_burst_jobs=1))


def _minio_client(server: MinioServer) -> Minio:
    return Minio(
        server.endpoint.removeprefix("http://"),
        access_key=server.access_key,
        secret_key=server.secret_key,
        secure=False,
    )


def _download(client: Minio, bucket: str, object_key: str) -> bytes:
    response = client.get_object(bucket, object_key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def _put_object(client: Minio, bucket: str, object_key: str, content: bytes) -> None:
    client.put_object(
        bucket,
        object_key,
        io.BytesIO(content),
        length=len(content),
        content_type="video/mp4",
    )


def test_real_minio_upload_endpoint_allows_browser_cors_preflight(
    minio_server: MinioServer,
) -> None:
    origin = "http://control-web.test"
    request = Request(
        f"{minio_server.endpoint}/{minio_server.bucket}",
        method="OPTIONS",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    with urlopen(request, timeout=10) as response:
        assert response.status in {200, 204}
        assert response.headers.get("Access-Control-Allow-Origin") in {"*", origin}
        allow_methods = response.headers.get("Access-Control-Allow-Methods")
        assert allow_methods is not None
        assert "POST" in allow_methods


def _zip_content() -> bytes:
    buffer = io.BytesIO()
    with ZipFile(buffer, mode="w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("synthetic.txt", "not a video")
    return buffer.getvalue()


def _assert_job_was_enqueued(redis_client: Redis, job_id: UUID) -> None:
    marker = str(job_id).encode()
    keys = cast(list[bytes], redis_client.keys("arq:*"))
    assert any(marker in key for key in keys)


def test_real_presigned_upload_cannot_target_another_object(
    engine: Engine,
    minio_server: MinioServer,
    real_video_bytes: bytes,
) -> None:
    settings = settings_for(engine, minio=minio_server)
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
            tampered = dict(upload)
            tampered_fields = dict(cast(dict[str, str], upload["fields"]))
            tampered_key = f"{upload['object_key']}-other"
            tampered_fields["key"] = tampered_key
            tampered["fields"] = tampered_fields

            response = upload_presigned(tampered, real_video_bytes)

            assert response.status_code >= 400
            with pytest.raises(S3Error) as missing:
                _minio_client(minio_server).stat_object(minio_server.bucket, tampered_key)
            assert missing.value.code in {"NoSuchKey", "NoSuchObject", "NotFound"}
        finally:
            cleanup_dataset(engine, dataset_id)


def test_real_login_and_role_permissions_gate_dataset_upload(
    engine: Engine,
    minio_server: MinioServer,
    real_video_bytes: bytes,
) -> None:
    settings = settings_for(engine, minio=minio_server)
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
            uploaded = upload_presigned(upload, real_video_bytes)
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
            _minio_client(minio_server).remove_object(minio_server.bucket, object_key)
        if dataset_id is not None:
            cleanup_dataset(engine, dataset_id)
        _remove_real_dataset_manager(engine, user_id=user_id, role_id=role_id)


def test_real_ffmpeg_video_upload_records_object_and_media_facts_and_is_idempotent(
    engine: Engine,
    minio_server: MinioServer,
    redis_server: RedisServer,
    redis_client: Redis,
    real_video_bytes: bytes,
) -> None:
    """真实直传、服务端校验、定稿和重复投递共同走生产实现。"""
    settings = settings_for(engine, minio=minio_server, redis_url=redis_server.url)
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
            upload_response = upload_presigned(requested["upload"], real_video_bytes)
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

            storage = _minio_client(minio_server)
            final_stat = storage.stat_object(minio_server.bucket, registered.object_key)
            assert final_stat.size == len(real_video_bytes)
            downloaded = _download(storage, minio_server.bucket, registered.object_key)
            assert downloaded == real_video_bytes
            with pytest.raises(S3Error) as missing_source:
                storage.stat_object(minio_server.bucket, requested["upload"]["object_key"])
            assert missing_source.value.code in {"NoSuchKey", "NoSuchObject", "NotFound"}
            before_duplicate = registered

            # ARQ 至少一次语义会再次传递同一 job id。
            # 已结案任务不得重写成员或定稿对象。
            _run_worker(engine, settings, job_id)
            after_duplicate = _persisted_member(engine, member_id)
            assert after_duplicate == before_duplicate
            duplicate_stat = storage.stat_object(minio_server.bucket, registered.object_key)
            assert duplicate_stat.size == final_stat.size
            duplicate_download = _download(storage, minio_server.bucket, registered.object_key)
            assert duplicate_download == real_video_bytes
        finally:
            cleanup_dataset(engine, dataset_id)


def test_real_arq_worker_consumes_validation_job_from_redis(
    engine: Engine,
    minio_server: MinioServer,
    redis_server: RedisServer,
    redis_client: Redis,
    real_video_bytes: bytes,
) -> None:
    settings = settings_for(engine, minio=minio_server, redis_url=redis_server.url)
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
            uploaded = upload_presigned(requested["upload"], real_video_bytes)
            assert uploaded.status_code == 204, uploaded.text
            job_id = _confirm_upload(client, dataset_id, requested)
            _assert_job_was_enqueued(redis_client, job_id)

            assert _run_arq_worker(engine, settings) == 1
            member = _persisted_member(engine, UUID(requested["member"]["id"]))
            assert member.status == MemberStatus.REGISTERED
            assert member.actual_sha256 == hashlib.sha256(real_video_bytes).hexdigest()
        finally:
            cleanup_dataset(engine, dataset_id)


def test_archive_filename_is_rejected_before_real_object_storage(
    engine: Engine,
    minio_server: MinioServer,
) -> None:
    settings = settings_for(engine, minio=minio_server)
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
    minio_server: MinioServer,
) -> None:
    settings = settings_for(engine, minio=minio_server)
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
            upload_response = upload_presigned(requested["upload"], content)
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


def test_real_object_size_mismatch_is_rejected_from_actual_minio_stat(
    engine: Engine,
    minio_server: MinioServer,
    real_video_bytes: bytes,
) -> None:
    settings = settings_for(engine, minio=minio_server)
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
                _minio_client(minio_server),
                minio_server.bucket,
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
    minio_server: MinioServer,
    real_video_bytes: bytes,
) -> None:
    settings = settings_for(engine, minio=minio_server)
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
            )
            _put_object(
                _minio_client(minio_server),
                minio_server.bucket,
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
    minio_server: MinioServer,
    real_video_bytes: bytes,
) -> None:
    settings = settings_for(engine, minio=minio_server).model_copy(
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
            uploaded = upload_presigned(requested["upload"], real_video_bytes)
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
    minio_server: MinioServer,
    real_video_bytes: bytes,
) -> None:
    settings = settings_for(engine, minio=minio_server)
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

            upload_response = upload_presigned(retried["upload"], real_video_bytes)
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
    minio_server: MinioServer,
    real_video_bytes: bytes,
) -> None:
    settings = settings_for(engine, minio=minio_server, upload_ttl_seconds=1)
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
            expired_response = upload_presigned(requested["upload"], real_video_bytes)
            assert expired_response.status_code >= 400

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
            fresh_response = upload_presigned(retried["upload"], real_video_bytes)
            assert fresh_response.status_code == 204, fresh_response.text
            new_job_id = _confirm_upload(client, dataset_id, retried)
            _run_worker(engine, settings, new_job_id)
            registered = _persisted_member(engine, old_member_id)
            assert registered.status == MemberStatus.REGISTERED
        finally:
            cleanup_dataset(engine, dataset_id)


def test_media_probe_outage_can_retry_validation_without_reupload(
    engine: Engine,
    minio_server: MinioServer,
    real_video_bytes: bytes,
) -> None:
    good_settings = settings_for(engine, minio=minio_server)
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
            uploaded = upload_presigned(requested["upload"], real_video_bytes)
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
    minio_server: MinioServer,
) -> None:
    settings = settings_for(engine, minio=minio_server)
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
                        storage=MinioObjectStorage.from_settings(settings),
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
    minio_server: MinioServer,
) -> None:
    settings = settings_for(engine, minio=minio_server)
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
