"""`dataset` 用例的第一条红—绿切片：授权先于任何上传分配。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from typing import BinaryIO
from uuid import UUID

import pytest

from factory_sop.auth.authorization import AuthorizationRefusedError, Caller
from factory_sop.auth.model import User, UserStatus
from factory_sop.auth.permissions import Permission
from factory_sop.dataset.errors import DatasetRefusalCode, DatasetRefusedError
from factory_sop.dataset.media import MediaMetadata, MediaProbeUnavailableError
from factory_sop.dataset.model import (
    ActionListRevision,
    AnnotationContext,
    AnnotationExecution,
    AnnotationSubmission,
    DatasetArtifact,
    DatasetMember,
    MemberStatus,
    ObjectStat,
    RetryMode,
    TrainingDataset,
    UploadAttempt,
    UsageCheck,
    VlmCandidate,
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
            max_upload_bytes=1024,
            upload_ttl_seconds=900,
        )


@dataclass
class FakeDatasets:
    datasets: dict[UUID, TrainingDataset] = field(default_factory=dict)
    members: dict[UUID, DatasetMember] = field(default_factory=dict)
    attempts: dict[UUID, UploadAttempt] = field(default_factory=dict)
    action_lists: dict[tuple[UUID, int], ActionListRevision] = field(default_factory=dict)
    contexts: dict[UUID, AnnotationContext] = field(default_factory=dict)
    submissions: dict[UUID, AnnotationSubmission] = field(default_factory=dict)
    executions: dict[UUID, AnnotationExecution] = field(default_factory=dict)
    vlm_candidates: dict[UUID, VlmCandidate] = field(default_factory=dict)
    usage_checks: dict[UUID, UsageCheck] = field(default_factory=dict)
    artifacts: dict[UUID, DatasetArtifact] = field(default_factory=dict)

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

    def lock_annotation_member(self, member_id: UUID) -> DatasetMember | None:
        return self.members.get(member_id)

    def attempt_by_id(self, attempt_id: UUID) -> UploadAttempt | None:
        return self.attempts.get(attempt_id)

    def page_datasets(self, *, page: int, page_size: int) -> tuple[list[TrainingDataset], int]:
        rows = list(self.datasets.values())
        start = (page - 1) * page_size
        return rows[start : start + page_size], len(rows)

    def list_members(self, *, dataset_id: UUID) -> list[DatasetMember]:
        return [member for member in self.members.values() if member.dataset_id == dataset_id]

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

    def latest_action_list(self, dataset_id: UUID) -> ActionListRevision | None:
        values = [value for (owner, _), value in self.action_lists.items() if owner == dataset_id]
        return max(values, key=lambda value: value.revision, default=None)

    def action_list_by_revision(
        self, *, dataset_id: UUID, revision: int
    ) -> ActionListRevision | None:
        return self.action_lists.get((dataset_id, revision))

    def add_action_list(self, value: ActionListRevision) -> None:
        self.action_lists[(value.dataset_id, value.revision)] = value

    def add_annotation_context(self, value: AnnotationContext) -> None:
        self.contexts[value.id] = value

    def annotation_context_by_id(self, context_id: UUID) -> AnnotationContext | None:
        return self.contexts.get(context_id)

    def save_annotation_context(self, value: AnnotationContext) -> None:
        self.contexts[value.id] = value

    def annotation_submission_by_idempotency(
        self, *, dataset_id: UUID, idempotency_key: str
    ) -> AnnotationSubmission | None:
        return next(
            (
                value
                for value in self.submissions.values()
                if value.dataset_id == dataset_id and value.idempotency_key == idempotency_key
            ),
            None,
        )

    def annotation_submission_by_id(self, submission_id: UUID) -> AnnotationSubmission | None:
        return self.submissions.get(submission_id)

    def latest_annotation_submission(
        self, context_id: UUID | None = None, *, member_id: UUID | None = None
    ) -> AnnotationSubmission | None:
        values = [
            value
            for value in self.submissions.values()
            if (member_id is not None and value.member_id == member_id)
            or (context_id is not None and value.context_id == context_id)
        ]
        return max(values, key=lambda value: value.created_at, default=None)

    def list_annotation_submissions(
        self, *, dataset_id: UUID, member_id: UUID
    ) -> list[AnnotationSubmission]:
        return [
            value
            for value in self.submissions.values()
            if value.dataset_id == dataset_id and value.member_id == member_id
        ]

    def add_annotation_submission(self, value: AnnotationSubmission) -> None:
        self.submissions[value.id] = value

    def annotation_execution_by_id(self, execution_id: UUID) -> AnnotationExecution | None:
        return self.executions.get(execution_id)

    def latest_annotation_execution(self, submission_id: UUID) -> AnnotationExecution | None:
        values = [
            value for value in self.executions.values() if value.submission_id == submission_id
        ]
        return max(values, key=lambda value: value.generation, default=None)

    def list_annotation_executions(self, submission_id: UUID) -> list[AnnotationExecution]:
        return [value for value in self.executions.values() if value.submission_id == submission_id]

    def add_annotation_execution(self, value: AnnotationExecution) -> None:
        self.executions[value.id] = value

    def add_vlm_candidate(self, value: VlmCandidate) -> None:
        self.vlm_candidates[value.id] = value

    def vlm_candidate_by_id(self, candidate_id: UUID) -> VlmCandidate | None:
        return self.vlm_candidates.get(candidate_id)

    def latest_vlm_candidate(self, dataset_id: UUID) -> VlmCandidate | None:
        values = [item for item in self.vlm_candidates.values() if item.dataset_id == dataset_id]
        return max(values, key=lambda item: item.revision) if values else None

    def list_vlm_candidates(self, dataset_id: UUID) -> list[VlmCandidate]:
        return [item for item in self.vlm_candidates.values() if item.dataset_id == dataset_id]

    def add_usage_check(self, value: UsageCheck) -> None:
        self.usage_checks[value.id] = value

    def usage_check_by_id(self, check_id: UUID) -> UsageCheck | None:
        return self.usage_checks.get(check_id)

    def list_usage_checks(self, dataset_id: UUID) -> list[UsageCheck]:
        return [item for item in self.usage_checks.values() if item.dataset_id == dataset_id]

    def save_usage_check(self, value: UsageCheck, *, expected_updated_at: datetime) -> bool:
        stored = self.usage_checks.get(value.id)
        if stored is None or stored.updated_at != expected_updated_at:
            return False
        self.usage_checks[value.id] = value
        return True

    def add_artifact(self, value: DatasetArtifact) -> None:
        self.artifacts[value.id] = value

    def artifact_by_id(self, artifact_id: UUID) -> DatasetArtifact | None:
        return self.artifacts.get(artifact_id)

    def artifact_by_input(self, *, dataset_id: UUID, input_digest: str) -> DatasetArtifact | None:
        return next(
            (
                item
                for item in self.artifacts.values()
                if item.dataset_id == dataset_id and item.input_digest == input_digest
            ),
            None,
        )

    def list_artifacts(self, dataset_id: UUID) -> list[DatasetArtifact]:
        return [item for item in self.artifacts.values() if item.dataset_id == dataset_id]

    def save_artifact(self, value: DatasetArtifact, *, expected_updated_at: datetime) -> bool:
        stored = self.artifacts.get(value.id)
        if stored is None or stored.updated_at != expected_updated_at:
            return False
        self.artifacts[value.id] = value
        return True

    def save_annotation_execution(
        self,
        value: AnnotationExecution,
        *,
        expected_updated_at: datetime,
    ) -> bool:
        current = self.executions.get(value.id)
        if current is None or current.updated_at != expected_updated_at:
            return False
        self.executions[value.id] = value
        return True

    def annotation_context_preparation_exists(self, _member_id: UUID, _context_id: UUID) -> bool:
        return False

    def annotation_execution_exists(self, _member_id: UUID, _execution_id: UUID) -> bool:
        return False


@dataclass
class FakeStorage(ObjectStorage):
    objects: dict[str, bytes] = field(default_factory=dict)
    finalized_content: bytes | None = None
    writes: list[str] = field(default_factory=list)

    @contextmanager
    def writing(self, *, object_key: str) -> Iterator[BinaryIO]:
        self.writes.append(object_key)
        buffer = BytesIO()
        yield buffer
        self.objects[object_key] = self.finalized_content or buffer.getvalue()

    def stat(self, *, object_key: str) -> ObjectStat:
        return ObjectStat(size=len(self.objects[object_key]))

    def download_to(self, *, object_key: str, destination: BinaryIO) -> None:
        destination.write(self.objects[object_key])

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

    def by_id(self, job_id: UUID) -> ApplicationJob | None:
        return self.jobs.get(job_id)

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

    def get_or_create_annotation(
        self, *, member_id: UUID, attempt_id: UUID, now: datetime
    ) -> ApplicationJob:
        return self._get_or_create_annotation_job(
            member_id=member_id,
            attempt_id=attempt_id,
            now=now,
            job_type=JobType.DATASET_ANNOTATION,
        )

    def get_or_create_annotation_preparation(
        self, *, member_id: UUID, attempt_id: UUID, now: datetime
    ) -> ApplicationJob:
        return self._get_or_create_annotation_job(
            member_id=member_id,
            attempt_id=attempt_id,
            now=now,
            job_type=JobType.DATASET_ANNOTATION_PREPARATION,
        )

    def get_or_create_usage_check(
        self, *, dataset_id: UUID, check_id: UUID, now: datetime
    ) -> ApplicationJob:
        return self._get_or_create_dataset_job(
            dataset_id=dataset_id,
            resource_id=check_id,
            now=now,
            job_type=JobType.DATASET_USAGE_CHECK,
        )

    def get_or_create_artifact(
        self, *, dataset_id: UUID, artifact_id: UUID, now: datetime
    ) -> ApplicationJob:
        return self._get_or_create_dataset_job(
            dataset_id=dataset_id,
            resource_id=artifact_id,
            now=now,
            job_type=JobType.DATASET_ARTIFACT,
        )

    def _get_or_create_dataset_job(
        self,
        *,
        dataset_id: UUID,
        resource_id: UUID,
        now: datetime,
        job_type: JobType,
    ) -> ApplicationJob:
        current = next(
            (
                job
                for job in self.jobs.values()
                if job.job_type is job_type and job.attempt_id == resource_id
            ),
            None,
        )
        if current is not None:
            return current
        job = ApplicationJob(
            id=UUID(f"019937d8-0d10-7b31-8d2d-{len(self.jobs) + 30:012d}"),
            job_type=job_type,
            status=JobStatus.PENDING,
            member_id=dataset_id,
            attempt_id=resource_id,
            created_at=now,
            updated_at=now,
            failure_code=None,
            dataset_id=dataset_id,
        )
        self.jobs[job.id] = job
        return job

    def _get_or_create_annotation_job(
        self,
        *,
        member_id: UUID,
        attempt_id: UUID,
        now: datetime,
        job_type: JobType,
    ) -> ApplicationJob:
        current = next(
            (
                job
                for job in self.jobs.values()
                if job.member_id == member_id
                and job.attempt_id == attempt_id
                and job.job_type is job_type
            ),
            None,
        )
        if current is not None:
            return current
        job = ApplicationJob(
            id=UUID(f"019937d8-0d10-7b31-8d2d-{len(self.jobs) + 20:012d}"),
            job_type=job_type,
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


def test_request_upload_rejects_header_control_chars_in_filename() -> None:
    with pytest.raises(DatasetRefusedError) as refused:
        request_video_upload(
            dataset_id=DATASET_ID,
            original_filename='unsafe"\r\nX-Injected: yes.mp4',
            source="产线相机",
            declared_size=12,
            declared_sha256="a" * 64,
            idempotency_key="header-injection",
            caller=caller_with_import(),
            now=NOW,
            datasets=dataset_store(),
            max_upload_bytes=1024,
            upload_ttl_seconds=900,
        )

    assert refused.value.code is DatasetRefusalCode.FILENAME_INVALID

    datasets = dataset_store()

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
            max_upload_bytes=1024,
            upload_ttl_seconds=900,
        )

    assert refused.value.code.value == "ARCHIVE_REJECTED"
    assert datasets.members == {}
    assert datasets.attempts == {}


def test_sha256_declaration_is_canonicalized_before_storage() -> None:
    datasets = dataset_store()

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
        max_upload_bytes=1024,
        upload_ttl_seconds=900,
    )

    assert result.member.declared_sha256 == "a" * 64
    assert result.attempt.declared_sha256 == "a" * 64


def test_upload_request_is_idempotent_for_the_same_key() -> None:
    datasets = dataset_store()
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
        max_upload_bytes=1024,
        upload_ttl_seconds=900,
    )

    assert first.member.id == second.member.id
    assert first.attempt.id == second.attempt.id
    assert len(datasets.members) == 1
    assert len(datasets.attempts) == 1


def test_confirming_one_attempt_twice_returns_one_validation_job() -> None:
    datasets = dataset_store()
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


def test_finalized_object_isolated_by_job_generation() -> None:
    content = b"generation-isolation"
    datasets, storage, job = _prepared_validation(content)
    target = begin_video_validation(job=job, datasets=datasets, now=NOW)
    assert target is not None
    first_datasets = deepcopy(datasets)
    second_datasets = deepcopy(datasets)
    first_storage = deepcopy(storage)
    second_storage = deepcopy(storage)

    validate_video_upload(
        job=replace(job, updated_at=NOW + timedelta(seconds=1)),
        datasets=first_datasets,
        storage=first_storage,
        probe=FakeProbe(MediaMetadata(duration_seconds=12.5, codec="h264", container="mp4")),
        supported_codecs=frozenset({"h264"}),
        now=NOW + timedelta(seconds=3),
        target=target,
    )
    validate_video_upload(
        job=replace(job, updated_at=NOW + timedelta(seconds=2)),
        datasets=second_datasets,
        storage=second_storage,
        probe=FakeProbe(MediaMetadata(duration_seconds=12.5, codec="h264", container="mp4")),
        supported_codecs=frozenset({"h264"}),
        now=NOW + timedelta(seconds=3),
        target=target,
    )

    first_key = first_datasets.members[job.member_id].object_key
    second_key = second_datasets.members[job.member_id].object_key
    assert first_key is not None
    assert second_key is not None
    assert first_key != second_key
    assert first_key in first_storage.objects
    assert second_key in second_storage.objects


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


def test_validation_registers_a_video_without_a_client_declared_digest() -> None:
    """客户端不声明摘要时，中心仍从已定稿字节计算并登记权威 sha256。"""
    datasets = dataset_store()
    content = b"synthetic video bytes without a declared digest"
    storage = FakeStorage()
    created = request_video_upload(
        dataset_id=DATASET_ID,
        original_filename="no-digest.mp4",
        source="产线相机",
        declared_size=len(content),
        declared_sha256=None,
        idempotency_key="no-digest-1",
        caller=caller_with_import(),
        now=NOW,
        datasets=datasets,
        max_upload_bytes=1024,
        upload_ttl_seconds=900,
    )
    assert created.member.declared_sha256 is None
    assert created.attempt.declared_sha256 is None
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

    assert result.status is MemberStatus.REGISTERED
    assert result.actual_sha256 == sha256(content).hexdigest()


def test_idempotent_resume_without_a_declared_digest_keeps_the_original_declaration() -> None:
    """客户端省略可选摘要时，同一幂等键仍能续期原有声明，而不是报冲突。"""
    datasets = dataset_store()
    content = b"resumable bytes"
    digest = sha256(content).hexdigest()
    first = request_video_upload(
        dataset_id=DATASET_ID,
        original_filename="resume.mp4",
        source="产线相机",
        declared_size=len(content),
        declared_sha256=digest,
        idempotency_key="resume-1",
        caller=caller_with_import(),
        now=NOW,
        datasets=datasets,
        max_upload_bytes=1024,
        upload_ttl_seconds=900,
    )

    resumed = request_video_upload(
        dataset_id=DATASET_ID,
        original_filename="resume.mp4",
        source="产线相机",
        declared_size=len(content),
        declared_sha256=None,
        idempotency_key="resume-1",
        caller=caller_with_import(),
        now=NOW + timedelta(seconds=60),
        datasets=datasets,
        max_upload_bytes=1024,
        upload_ttl_seconds=900,
    )

    assert resumed.attempt.id == first.attempt.id
    assert resumed.attempt.declared_sha256 == digest
    assert resumed.attempt.expires_at == NOW + timedelta(seconds=960)


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
