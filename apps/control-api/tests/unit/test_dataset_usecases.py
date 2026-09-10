"""`dataset` 用例的第一条红—绿切片：授权先于任何上传分配。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import BinaryIO
from uuid import UUID

import pytest

from factory_sop.auth.authorization import AuthorizationRefusedError, Caller
from factory_sop.auth.model import User, UserStatus
from factory_sop.auth.permissions import Permission
from factory_sop.dataset.errors import DatasetRefusedError
from factory_sop.dataset.media import MediaMetadata, MediaProbeUnavailableError
from factory_sop.dataset.model import (
    DatasetMember,
    MemberStatus,
    ObjectStat,
    RetryMode,
    TrainingDataset,
    UploadAttempt,
    UploadInstructions,
)
from factory_sop.dataset.storage import ObjectStorage
from factory_sop.dataset.usecases import (
    ValidationResult,
    begin_video_validation,
    confirm_video_upload,
    request_video_upload,
    retry_video_upload,
    validate_video_upload,
)
from factory_sop.job.api import ApplicationJob, JobStatus, JobType

NOW = datetime(2026, 9, 9, 1, 0, tzinfo=UTC)
DATASET_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f001")
ACTOR_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f002")


class UnreachableStorage:
    """若权限检查顺序错误，测试会在这里失败。"""

    def finalize_upload(self, *, object_key: str, source: BinaryIO, size: int) -> ObjectStat:
        del object_key, source, size
        raise AssertionError("未授权请求不应写入定稿对象")

    def create_upload(
        self,
        *,
        object_key: str,
        declared_size: int,
        max_bytes: int,
        expires_at: datetime,
    ) -> UploadInstructions:
        del object_key, declared_size, max_bytes, expires_at
        raise AssertionError("未授权请求不应申请对象存储凭据")

    def stat(self, *, object_key: str) -> ObjectStat:
        del object_key
        raise AssertionError("未授权请求不应读取对象")

    def download_to(self, *, object_key: str, destination: BinaryIO) -> None:
        del object_key, destination
        raise AssertionError("未授权请求不应下载对象")

    def delete(self, *, object_key: str) -> None:
        del object_key
        raise AssertionError("未授权请求不应删除对象")


def caller_without_import() -> Caller:
    return Caller(
        user=User(
            id=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f002"),
            login_name="viewer",
            display_name="只读用户",
            password_hash="unused",  # pragma: allowlist secret
            status=UserStatus.ACTIVE,
        ),
        granted=frozenset(),
    )


def test_request_upload_requires_dataset_import_permission() -> None:
    with pytest.raises(AuthorizationRefusedError):
        request_video_upload(
            dataset_id=DATASET_ID,
            original_filename="sample.mp4",
            source="产线相机",
            declared_size=12,
            declared_sha256="a" * 64,
            idempotency_key="request-1",
            caller=caller_without_import(),
            now=NOW,
            datasets=None,  # type: ignore[arg-type]
            storage=UnreachableStorage(),
            max_upload_bytes=1024,
            upload_ttl_seconds=900,
        )


@dataclass
class FakeDatasets:
    datasets: dict[UUID, TrainingDataset] = field(default_factory=dict)
    members: dict[UUID, DatasetMember] = field(default_factory=dict)
    attempts: dict[UUID, UploadAttempt] = field(default_factory=dict)

    def dataset_by_id(self, dataset_id: UUID) -> TrainingDataset | None:
        return self.datasets.get(dataset_id)

    def add_dataset(self, dataset: TrainingDataset) -> None:
        self.datasets[dataset.id] = dataset

    def add_member(self, member: DatasetMember) -> None:
        self.members[member.id] = member

    def add_attempt(self, attempt: UploadAttempt) -> None:
        self.attempts[attempt.id] = attempt

    def member_by_id(self, member_id: UUID) -> DatasetMember | None:
        return self.members.get(member_id)

    def attempt_by_id(self, attempt_id: UUID) -> UploadAttempt | None:
        return self.attempts.get(attempt_id)

    def page_datasets(self, *, page: int, page_size: int) -> tuple[list[TrainingDataset], int]:
        rows = list(self.datasets.values())
        start = (page - 1) * page_size
        return rows[start : start + page_size], len(rows)

    def page_members(
        self, *, dataset_id: UUID, page: int, page_size: int
    ) -> tuple[list[DatasetMember], int]:
        rows = [member for member in self.members.values() if member.dataset_id == dataset_id]
        start = (page - 1) * page_size
        return rows[start : start + page_size], len(rows)

    def attempt_by_idempotency(
        self, *, dataset_id: UUID, idempotency_key: str
    ) -> UploadAttempt | None:
        return next(
            (
                attempt
                for attempt in self.attempts.values()
                if attempt.idempotency_key == idempotency_key
                and self.members[attempt.member_id].dataset_id == dataset_id
            ),
            None,
        )

    def save_member(
        self,
        member: DatasetMember,
        *,
        expected_attempt_id: UUID,
        expected_updated_at: datetime | None = None,
    ) -> bool:
        stored = self.members.get(member.id)
        if stored is None or stored.current_attempt_id != expected_attempt_id:
            return False
        if expected_updated_at is not None and stored.updated_at != expected_updated_at:
            return False
        self.members[member.id] = member
        return True

    def save_attempt(self, attempt: UploadAttempt) -> None:
        self.attempts[attempt.id] = attempt


@dataclass
class FakeStorage(ObjectStorage):
    objects: dict[str, bytes] = field(default_factory=dict)
    requests: list[tuple[str, int, datetime]] = field(default_factory=list)
    finalized_content: bytes | None = None

    def create_upload(
        self,
        *,
        object_key: str,
        declared_size: int,
        max_bytes: int,
        expires_at: datetime,
    ) -> UploadInstructions:
        self.requests.append((object_key, max_bytes, expires_at))
        return UploadInstructions(
            method="PUT",
            url=f"https://minio.test/{object_key}",
            fields={},
            headers={"Content-Length": str(max_bytes)},
            expires_at=expires_at,
            max_bytes=max_bytes,
            object_key=object_key,
        )

    def stat(self, *, object_key: str) -> ObjectStat:
        content = self.objects[object_key]
        return ObjectStat(size=len(content), version_id="version-1")

    def download_to(self, *, object_key: str, destination: BinaryIO) -> None:
        destination.write(self.objects[object_key])

    def finalize_upload(self, *, object_key: str, source: BinaryIO, size: int) -> ObjectStat:
        source.seek(0)
        content = source.read()
        self.objects[object_key] = self.finalized_content or content
        return ObjectStat(size=size, version_id="final-version")

    def delete(self, *, object_key: str) -> None:
        self.objects.pop(object_key, None)


@dataclass
class FakeProbe:
    metadata: MediaMetadata

    def probe(self, path: str) -> MediaMetadata:
        del path
        return self.metadata


class UnavailableProbe:
    """让恢复测试证明媒体探测失败可重新校验。"""

    def probe(self, path: str) -> MediaMetadata:
        del path
        raise MediaProbeUnavailableError("ffprobe 暂时不可用")


@dataclass
class FakeJobs:
    jobs: dict[UUID, ApplicationJob] = field(default_factory=dict)

    def get_or_create_validation(
        self, *, member_id: UUID, attempt_id: UUID, now: datetime
    ) -> ApplicationJob:
        current = next(
            (
                job
                for job in self.jobs.values()
                if job.member_id == member_id and job.attempt_id == attempt_id
            ),
            None,
        )
        if current is not None:
            return current
        job = ApplicationJob(
            id=UUID(f"019937d8-0d10-7b31-8d2d-{len(self.jobs) + 10:012d}"),
            job_type=JobType.DATASET_VALIDATION,
            status=JobStatus.PENDING,
            member_id=member_id,
            attempt_id=attempt_id,
            created_at=now,
            updated_at=now,
            failure_code=None,
        )
        self.jobs[job.id] = job
        return job


def caller_with_import() -> Caller:
    return Caller(
        user=User(
            id=ACTOR_ID,
            login_name="manager",
            display_name="数据集管理者",
            password_hash="unused",  # pragma: allowlist secret
            status=UserStatus.ACTIVE,
        ),
        granted=frozenset({Permission.DATASET_IMPORT, Permission.DATASET_VIEW}),
    )


def dataset_store() -> FakeDatasets:
    store = FakeDatasets()
    store.add_dataset(
        TrainingDataset(
            id=DATASET_ID,
            name="装配训练集",
            created_by=ACTOR_ID,
            updated_by=ACTOR_ID,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    return store


def test_request_upload_rejects_an_archive_before_object_storage() -> None:
    datasets = dataset_store()
    storage = FakeStorage()

    with pytest.raises(DatasetRefusedError) as refused:
        request_video_upload(
            dataset_id=DATASET_ID,
            original_filename="samples.zip",
            source="导入文件",
            declared_size=12,
            declared_sha256="a" * 64,
            idempotency_key="archive-1",
            caller=caller_with_import(),
            now=NOW,
            datasets=datasets,
            storage=storage,
            max_upload_bytes=1024,
            upload_ttl_seconds=900,
        )

    assert refused.value.code.value == "ARCHIVE_REJECTED"
    assert storage.requests == []


def test_sha256_declaration_is_canonicalized_before_storage() -> None:
    datasets = dataset_store()
    storage = FakeStorage()

    result = request_video_upload(
        dataset_id=DATASET_ID,
        original_filename="uppercase-digest.mp4",
        source="产线相机",
        declared_size=12,
        declared_sha256="A" * 64,
        idempotency_key="uppercase-digest",
        caller=caller_with_import(),
        now=NOW,
        datasets=datasets,
        storage=storage,
        max_upload_bytes=1024,
        upload_ttl_seconds=900,
    )

    assert result.member.declared_sha256 == "a" * 64
    assert result.attempt.declared_sha256 == "a" * 64


def test_upload_request_is_idempotent_for_the_same_key() -> None:
    datasets = dataset_store()
    storage = FakeStorage()
    first = request_video_upload(
        dataset_id=DATASET_ID,
        original_filename="sample.mp4",
        source="产线相机",
        declared_size=12,
        declared_sha256="a" * 64,
        idempotency_key="same-request",
        caller=caller_with_import(),
        now=NOW,
        datasets=datasets,
        storage=storage,
        max_upload_bytes=1024,
        upload_ttl_seconds=900,
    )
    second = request_video_upload(
        dataset_id=DATASET_ID,
        original_filename="sample.mp4",
        source="产线相机",
        declared_size=12,
        declared_sha256="a" * 64,
        idempotency_key="same-request",
        caller=caller_with_import(),
        now=NOW,
        datasets=datasets,
        storage=storage,
        max_upload_bytes=1024,
        upload_ttl_seconds=900,
    )

    assert first.member.id == second.member.id
    assert first.attempt.id == second.attempt.id
    assert len(datasets.members) == 1
    assert len(datasets.attempts) == 1


def test_confirming_one_attempt_twice_returns_one_validation_job() -> None:
    datasets = dataset_store()
    storage = FakeStorage()
    jobs = FakeJobs()
    created = request_video_upload(
        dataset_id=DATASET_ID,
        original_filename="sample.mp4",
        source="产线相机",
        declared_size=12,
        declared_sha256="a" * 64,
        idempotency_key="confirm-1",
        caller=caller_with_import(),
        now=NOW,
        datasets=datasets,
        storage=storage,
        max_upload_bytes=1024,
        upload_ttl_seconds=900,
    )

    first = confirm_video_upload(
        dataset_id=DATASET_ID,
        member_id=created.member.id,
        attempt_id=created.attempt.id,
        caller=caller_with_import(),
        now=NOW,
        datasets=datasets,
        jobs=jobs,
    )
    second = confirm_video_upload(
        dataset_id=DATASET_ID,
        member_id=created.member.id,
        attempt_id=created.attempt.id,
        caller=caller_with_import(),
        now=NOW,
        datasets=datasets,
        jobs=jobs,
    )

    assert first.job is not None
    assert second.job is not None
    assert first.job.id == second.job.id
    assert len(jobs.jobs) == 1
    assert datasets.members[created.member.id].status == MemberStatus.PENDING_VALIDATION


def _prepared_validation(content: bytes) -> tuple[FakeDatasets, FakeStorage, ApplicationJob]:
    datasets = dataset_store()
    storage = FakeStorage()
    created = request_video_upload(
        dataset_id=DATASET_ID,
        original_filename="sample.mp4",
        source="产线相机",
        declared_size=len(content),
        declared_sha256=sha256(content).hexdigest(),
        idempotency_key="archive-validation",
        caller=caller_with_import(),
        now=NOW,
        datasets=datasets,
        storage=storage,
        max_upload_bytes=1024,
        upload_ttl_seconds=900,
    )
    storage.objects[created.attempt.object_key] = content
    jobs = FakeJobs()
    confirmation = confirm_video_upload(
        dataset_id=DATASET_ID,
        member_id=created.member.id,
        attempt_id=created.attempt.id,
        caller=caller_with_import(),
        now=NOW,
        datasets=datasets,
        jobs=jobs,
    )
    assert confirmation.job is not None
    return datasets, storage, confirmation.job


@pytest.mark.parametrize(
    "signature",
    [
        b"7z\xbc\xaf'\x1c",
        b"Rar!\x1a\x07\x00",
        b"Rar!\x1a\x07\x01\x00",
        b"BZh",
        b"\xfd7zXZ\x00",
        b"\x28\xb5\x2f\xfd",
        b"MSCF",
        b"!<arch>\n",
    ],
)
def test_archive_content_is_rejected_before_media_probe(signature: bytes) -> None:
    content = signature + b"synthetic content"
    datasets, storage, job = _prepared_validation(content)
    target = begin_video_validation(job=job, datasets=datasets, now=NOW)
    assert target is not None

    result = validate_video_upload(
        job=job,
        datasets=datasets,
        storage=storage,
        probe=UnavailableProbe(),
        supported_codecs=frozenset({"h264"}),
        now=NOW,
        target=target,
    )

    assert result == ValidationResult(
        status=MemberStatus.FAILED,
        actual_size=len(content),
        actual_sha256=sha256(content).hexdigest(),
        duration_seconds=None,
        codec=None,
        failure_code="ARCHIVE_CONTENT_REJECTED",
        failure_detail="压缩包或伪装成视频的压缩包不允许导入",
        recovery_action=RetryMode.UPLOAD,
    )


def test_validation_uses_object_facts_and_media_probe_to_register_video() -> None:
    datasets = dataset_store()
    content = b"synthetic video bytes"
    storage = FakeStorage()
    created = request_video_upload(
        dataset_id=DATASET_ID,
        original_filename="sample.mp4",
        source="产线相机",
        declared_size=len(content),
        declared_sha256=sha256(content).hexdigest(),
        idempotency_key="validate-1",
        caller=caller_with_import(),
        now=NOW,
        datasets=datasets,
        storage=storage,
        max_upload_bytes=1024,
        upload_ttl_seconds=900,
    )
    storage.objects[created.attempt.object_key] = content
    jobs = FakeJobs()
    confirm_video_upload(
        dataset_id=DATASET_ID,
        member_id=created.member.id,
        attempt_id=created.attempt.id,
        caller=caller_with_import(),
        now=NOW,
        datasets=datasets,
        jobs=jobs,
    )
    probe = FakeProbe(MediaMetadata(duration_seconds=12.5, codec="h264", container="mp4"))
    target = begin_video_validation(
        job=next(iter(jobs.jobs.values())),
        datasets=datasets,
        now=NOW,
    )
    assert target is not None

    result = validate_video_upload(
        job=next(iter(jobs.jobs.values())),
        datasets=datasets,
        storage=storage,
        probe=probe,
        supported_codecs=frozenset({"h264"}),
        now=NOW,
        target=target,
    )

    assert result == ValidationResult(
        status=MemberStatus.REGISTERED,
        actual_size=len(content),
        actual_sha256=sha256(content).hexdigest(),
        duration_seconds=12.5,
        codec="h264",
        failure_code=None,
        failure_detail=None,
        recovery_action=None,
    )
    assert created.attempt.object_key not in storage.objects


def test_ffprobe_hevc_matches_the_h265_deployment_name() -> None:
    content = b"hevc video bytes"
    datasets, storage, job = _prepared_validation(content)
    target = begin_video_validation(job=job, datasets=datasets, now=NOW)
    assert target is not None

    result = validate_video_upload(
        job=job,
        datasets=datasets,
        storage=storage,
        probe=FakeProbe(MediaMetadata(duration_seconds=12.5, codec="hevc", container="mp4")),
        supported_codecs=frozenset({"h265"}),
        now=NOW,
        target=target,
    )

    assert result == ValidationResult(
        status=MemberStatus.REGISTERED,
        actual_size=len(content),
        actual_sha256=sha256(content).hexdigest(),
        duration_seconds=12.5,
        codec="hevc",
        failure_code=None,
        failure_detail=None,
        recovery_action=None,
    )


def test_finalized_object_content_mismatch_is_not_registered() -> None:
    datasets = dataset_store()
    content = b"validated source content"
    storage = FakeStorage(finalized_content=b"corrupted final content")
    created = request_video_upload(
        dataset_id=DATASET_ID,
        original_filename="finalizer-mismatch.mp4",
        source="产线相机",
        declared_size=len(content),
        declared_sha256=sha256(content).hexdigest(),
        idempotency_key="finalizer-mismatch",
        caller=caller_with_import(),
        now=NOW,
        datasets=datasets,
        storage=storage,
        max_upload_bytes=1024,
        upload_ttl_seconds=900,
    )
    storage.objects[created.attempt.object_key] = content
    jobs = FakeJobs()
    confirmation = confirm_video_upload(
        dataset_id=DATASET_ID,
        member_id=created.member.id,
        attempt_id=created.attempt.id,
        caller=caller_with_import(),
        now=NOW,
        datasets=datasets,
        jobs=jobs,
    )
    assert confirmation.job is not None
    target = begin_video_validation(
        job=confirmation.job,
        datasets=datasets,
        now=NOW,
    )
    assert target is not None

    result = validate_video_upload(
        job=confirmation.job,
        datasets=datasets,
        storage=storage,
        probe=FakeProbe(MediaMetadata(duration_seconds=12.5, codec="h264", container="mp4")),
        supported_codecs=frozenset({"h264"}),
        now=NOW,
        target=target,
    )

    assert result == ValidationResult(
        status=MemberStatus.FAILED,
        actual_size=len(content),
        actual_sha256=sha256(content).hexdigest(),
        duration_seconds=None,
        codec=None,
        failure_code="STORAGE_UNAVAILABLE",
        failure_detail="定稿对象内容与已校验内容不一致，请稍后重新校验",
        recovery_action=RetryMode.VALIDATION,
    )
    assert all(not key.endswith("/registered-video") for key in storage.objects)
    assert created.attempt.object_key in storage.objects


def test_stale_validation_snapshot_cannot_write_after_lease_renewal() -> None:
    datasets = dataset_store()
    content = b"video content for lease renewal"
    storage = FakeStorage()
    created = request_video_upload(
        dataset_id=DATASET_ID,
        original_filename="lease-renewal.mp4",
        source="产线相机",
        declared_size=len(content),
        declared_sha256=sha256(content).hexdigest(),
        idempotency_key="lease-renewal",
        caller=caller_with_import(),
        now=NOW,
        datasets=datasets,
        storage=storage,
        max_upload_bytes=1024,
        upload_ttl_seconds=900,
    )
    storage.objects[created.attempt.object_key] = content
    jobs = FakeJobs()
    confirmation = confirm_video_upload(
        dataset_id=DATASET_ID,
        member_id=created.member.id,
        attempt_id=created.attempt.id,
        caller=caller_with_import(),
        now=NOW,
        datasets=datasets,
        jobs=jobs,
    )
    assert confirmation.job is not None

    first_target = begin_video_validation(
        job=confirmation.job,
        datasets=datasets,
        now=NOW + timedelta(seconds=1),
    )
    second_target = begin_video_validation(
        job=confirmation.job,
        datasets=datasets,
        now=NOW + timedelta(seconds=2),
    )
    assert first_target is not None
    assert second_target is not None

    stale_result = validate_video_upload(
        job=confirmation.job,
        datasets=datasets,
        storage=storage,
        probe=FakeProbe(MediaMetadata(duration_seconds=12.5, codec="h264", container="mp4")),
        supported_codecs=frozenset({"h264"}),
        now=NOW + timedelta(seconds=3),
        target=first_target,
    )

    current = datasets.members[created.member.id]
    assert stale_result == ValidationResult(
        status=MemberStatus.VALIDATING,
        actual_size=None,
        actual_sha256=None,
        duration_seconds=None,
        codec=None,
        failure_code=None,
        failure_detail=None,
        recovery_action=None,
    )
    assert current.status == MemberStatus.VALIDATING
    assert current.updated_at == NOW + timedelta(seconds=2)


def test_validation_persists_digest_failure_with_upload_recovery() -> None:
    datasets = dataset_store()
    content = b"different content"
    storage = FakeStorage()
    created = request_video_upload(
        dataset_id=DATASET_ID,
        original_filename="sample.mp4",
        source="产线相机",
        declared_size=len(content),
        declared_sha256="b" * 64,
        idempotency_key="bad-digest",
        caller=caller_with_import(),
        now=NOW,
        datasets=datasets,
        storage=storage,
        max_upload_bytes=1024,
        upload_ttl_seconds=900,
    )
    storage.objects[created.attempt.object_key] = content
    jobs = FakeJobs()
    confirm_video_upload(
        dataset_id=DATASET_ID,
        member_id=created.member.id,
        attempt_id=created.attempt.id,
        caller=caller_with_import(),
        now=NOW,
        datasets=datasets,
        jobs=jobs,
    )
    target = begin_video_validation(
        job=next(iter(jobs.jobs.values())),
        datasets=datasets,
        now=NOW,
    )
    assert target is not None

    result = validate_video_upload(
        job=next(iter(jobs.jobs.values())),
        datasets=datasets,
        storage=storage,
        probe=FakeProbe(MediaMetadata(duration_seconds=12.5, codec="h264", container="mp4")),
        supported_codecs=frozenset({"h264"}),
        now=NOW,
        target=target,
    )

    assert result == ValidationResult(
        status=MemberStatus.FAILED,
        actual_size=len(content),
        actual_sha256=sha256(content).hexdigest(),
        duration_seconds=None,
        codec=None,
        failure_code="SHA256_MISMATCH",
        failure_detail="对象内容摘要与登记声明不一致",
        recovery_action=RetryMode.UPLOAD,
    )


def test_failed_upload_retry_key_renews_the_same_attempt() -> None:
    datasets = dataset_store()
    storage = FakeStorage()
    created = request_video_upload(
        dataset_id=DATASET_ID,
        original_filename="sample.mp4",
        source="产线相机",
        declared_size=12,
        declared_sha256="a" * 64,
        idempotency_key=None,
        caller=caller_with_import(),
        now=NOW,
        datasets=datasets,
        storage=storage,
        max_upload_bytes=1024,
        upload_ttl_seconds=900,
    )
    failed = replace(
        created.member,
        status=MemberStatus.FAILED,
        failure_code="OBJECT_NOT_FOUND",
        recovery_action=RetryMode.UPLOAD.value,
        updated_at=NOW + timedelta(seconds=1),
    )
    assert datasets.save_member(failed, expected_attempt_id=created.attempt.id)

    retry = retry_video_upload(
        dataset_id=DATASET_ID,
        member_id=created.member.id,
        mode=RetryMode.UPLOAD,
        idempotency_key="retry-upload-1",
        caller=caller_with_import(),
        now=NOW + timedelta(seconds=2),
        datasets=datasets,
        storage=storage,
        jobs=FakeJobs(),
        max_upload_bytes=1024,
        upload_ttl_seconds=900,
    )

    assert retry.attempt.idempotency_key == "retry-upload-1"
    assert retry.upload is not None
    renewed = request_video_upload(
        dataset_id=DATASET_ID,
        original_filename="sample.mp4",
        source="产线相机",
        declared_size=12,
        declared_sha256="a" * 64,
        idempotency_key="retry-upload-1",
        caller=caller_with_import(),
        now=NOW + timedelta(seconds=3),
        datasets=datasets,
        storage=storage,
        max_upload_bytes=1024,
        upload_ttl_seconds=900,
    )
    assert renewed.attempt.id == retry.attempt.id
    assert renewed.member.id == created.member.id


def test_media_probe_failure_can_retry_validation_without_new_upload() -> None:
    datasets = dataset_store()
    content = b"video content retained for validation retry"
    storage = FakeStorage()
    created = request_video_upload(
        dataset_id=DATASET_ID,
        original_filename="sample.mp4",
        source="产线相机",
        declared_size=len(content),
        declared_sha256=sha256(content).hexdigest(),
        idempotency_key="probe-retry",
        caller=caller_with_import(),
        now=NOW,
        datasets=datasets,
        storage=storage,
        max_upload_bytes=1024,
        upload_ttl_seconds=900,
    )
    storage.objects[created.attempt.object_key] = content
    jobs = FakeJobs()
    confirmation = confirm_video_upload(
        dataset_id=DATASET_ID,
        member_id=created.member.id,
        attempt_id=created.attempt.id,
        caller=caller_with_import(),
        now=NOW,
        datasets=datasets,
        jobs=jobs,
    )
    assert confirmation.job is not None
    target = begin_video_validation(
        job=confirmation.job,
        datasets=datasets,
        now=NOW,
    )
    assert target is not None

    failed = validate_video_upload(
        job=confirmation.job,
        datasets=datasets,
        storage=storage,
        probe=UnavailableProbe(),
        supported_codecs=frozenset({"h264"}),
        now=NOW,
        target=target,
    )

    assert failed.failure_code == "MEDIA_PROBE_UNAVAILABLE"
    assert failed.recovery_action == RetryMode.VALIDATION
    assert failed.actual_size == len(content)
    assert failed.actual_sha256 == sha256(content).hexdigest()
    retry = retry_video_upload(
        dataset_id=DATASET_ID,
        member_id=created.member.id,
        mode=RetryMode.VALIDATION,
        caller=caller_with_import(),
        now=NOW,
        datasets=datasets,
        storage=storage,
        jobs=jobs,
        max_upload_bytes=1024,
        upload_ttl_seconds=900,
    )

    assert retry.attempt.id == created.attempt.id
    assert retry.attempt.status == "pending_validation"
    assert retry.upload is None
    assert retry.job is not None
    assert retry.job.id == confirmation.job.id
    retry_target = begin_video_validation(
        job=retry.job,
        datasets=datasets,
        now=NOW,
    )
    assert retry_target is not None

    registered = validate_video_upload(
        job=retry.job,
        datasets=datasets,
        storage=storage,
        probe=FakeProbe(MediaMetadata(duration_seconds=2.0, codec="h264", container="mp4")),
        supported_codecs=frozenset({"h264"}),
        now=NOW,
        target=retry_target,
    )
    assert registered == ValidationResult(
        status=MemberStatus.REGISTERED,
        actual_size=len(content),
        actual_sha256=sha256(content).hexdigest(),
        duration_seconds=2.0,
        codec="h264",
        failure_code=None,
        failure_detail=None,
        recovery_action=None,
    )
