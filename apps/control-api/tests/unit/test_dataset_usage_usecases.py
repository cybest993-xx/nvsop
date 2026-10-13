"""用途检查用例的数据库、任务和训练素材存储 seam。"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO, cast
from uuid import UUID

import pytest

from factory_sop.auth.authorization import AuthorizationRefusedError, Caller
from factory_sop.auth.model import User, UserStatus
from factory_sop.auth.permissions import Permission
from factory_sop.dataset.adapters.ddm import NvidiaDdmAnnotationGenerator
from factory_sop.dataset.adapters.storage import LocalFileObjectStorage
from factory_sop.dataset.api import CheckedDatasetInput, checked_input_reader
from factory_sop.dataset.errors import DatasetRefusalCode, DatasetRefusedError
from factory_sop.dataset.media import MediaMetadata
from factory_sop.dataset.model import (
    ActionListRevision,
    AnnotationExecution,
    AnnotationExecutionStatus,
    AnnotationMode,
    AnnotationSegment,
    AnnotationSubmission,
    ArtifactStatus,
    DatasetArtifact,
    DatasetMember,
    DdmTrainingCommand,
    DdmTrainingCommandStatus,
    MemberStatus,
    ObjectStat,
    TrainingDataset,
    UsageCheck,
    UsageCheckStatus,
    UsageKind,
    VlmCandidate,
    VlmCandidateKind,
    VlmMediaReference,
)
from factory_sop.dataset.repository import DdmTrainingCommandRepository, UsageDatasetRepository
from factory_sop.dataset.storage import ObjectStorage
from factory_sop.dataset.usage import (
    VLM_CONSUMER_PARAMETERS,
    DdmReaderInputError,
    DdmReaderUnavailableError,
    UsageIssue,
    UsageValidationResult,
    canonical_json,
)
from factory_sop.dataset.usecases.ddm_command import (
    DdmTrainingCommandResult,
    request_ddm_training_command,
    request_digest,
)
from factory_sop.dataset.usecases.usage import (
    BASE_COMMIT,
    DDM_CONSUMER_PARAMETERS,
    DDM_CONTRACT_VERSION,
    DDM_VALIDATION_CONSUMER_PARAMETERS,
    VLM_CONTRACT_VERSION,
    VLM_MANIFEST_FILENAME,
    VLM_RECORDS_FILENAME,
    ConsumedDdmFile,
    DdmInputConsumption,
    DdmInputSelection,
    FrozenDdmInput,
    FrozenVlmInput,
    MaterializedDdmInput,
    UsageCheckTarget,
    VlmInputSelection,
    apply_usage_check_currentness,
    begin_artifact_generation,
    begin_usage_check,
    complete_artifact,
    complete_usage_check,
    fail_artifact,
    fail_usage_check,
    freeze_ddm_input,
    freeze_vlm_input,
    materialize_ddm_input,
    materialize_vlm_input,
    register_vlm_candidate,
    render_ddm_artifact_with_base,
    request_artifact,
    request_usage_check,
    run_usage_check,
    usage_check_is_current,
    verify_ddm_input_consumption,
)
from factory_sop.identifiers import new_id
from factory_sop.job.api import (
    ApplicationJob,
    DdmTrainingJobQueue,
    JobStatus,
    JobType,
    UsageJobQueue,
)

NOW = datetime(2026, 9, 12, 1, 0, tzinfo=UTC)
DATASET_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f301")
MEMBER_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f302")
ACTOR_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f303")
ATTEMPT_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f304")
SUBMISSION_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f305")
EXECUTION_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f30c")
NEW_EXECUTION_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f30e")


def caller(*permissions: Permission) -> Caller:
    return Caller(
        user=User(
            id=ACTOR_ID,
            login_name="operator",
            display_name="操作员",
            password_hash="unused",  # pragma: allowlist secret
            status=UserStatus.ACTIVE,
        ),
        granted=frozenset(permissions),
    )


def make_dataset() -> TrainingDataset:
    return TrainingDataset(
        id=DATASET_ID,
        name="测试数据集",
        created_by=ACTOR_ID,
        updated_by=ACTOR_ID,
        created_at=NOW,
        updated_at=NOW,
    )


def make_member() -> DatasetMember:
    return DatasetMember(
        id=MEMBER_ID,
        dataset_id=DATASET_ID,
        original_filename="line-a.mp4",
        source="相机 A",
        declared_size=100,
        declared_sha256="a" * 64,
        current_attempt_id=ATTEMPT_ID,
        status=MemberStatus.REGISTERED,
        actual_size=100,
        actual_sha256="a" * 64,
        duration_seconds=10.0,
        codec="h264",
        container="mp4",
        object_key="datasets/source-a.mp4",
        validation_job_id=None,
        failure_code=None,
        failure_detail=None,
        recovery_action=None,
        created_by=ACTOR_ID,
        updated_by=ACTOR_ID,
        created_at=NOW,
        updated_at=NOW,
    )


def make_submission() -> AnnotationSubmission:
    return AnnotationSubmission(
        id=SUBMISSION_ID,
        dataset_id=DATASET_ID,
        member_id=MEMBER_ID,
        context_id=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f306"),
        revision=1,
        action_list_revision=1,
        source_object_key="datasets/source-a.mp4",
        source_sha256="a" * 64,
        idempotency_key="annotation-1",
        request_digest="b" * 64,
        mode=AnnotationMode.SINGLE_OPERATOR,
        segments=(
            AnnotationSegment(0.0, 5.0, 0, "(1) 取料"),
            AnnotationSegment(5.0, 10.0, 1, "(2) 安装"),
        ),
        raw_segments=(),
        created_by=ACTOR_ID,
        created_at=NOW,
    )


@dataclass
class FakeUsageDatasets:
    dataset: TrainingDataset = field(default_factory=make_dataset)
    member: DatasetMember = field(default_factory=make_member)
    action_list: ActionListRevision = field(
        default_factory=lambda: ActionListRevision(
            dataset_id=DATASET_ID,
            revision=1,
            actions=("(1) 取料", "(2) 安装"),
            created_by=ACTOR_ID,
            created_at=NOW,
        )
    )
    submission: AnnotationSubmission = field(default_factory=make_submission)
    execution: AnnotationExecution = field(
        default_factory=lambda: AnnotationExecution(
            id=EXECUTION_ID,
            submission_id=SUBMISSION_ID,
            generation=1,
            job_id=None,
            status=AnnotationExecutionStatus.SUCCEEDED,
            clips=(
                {
                    "id": "clip-1",
                    "filename": "01_line-a_1_1.mp4",
                    "start_time": 0.0,
                    "end_time": 5.0,
                    "action_index": 0,
                    "action_description": "(1) 取料",
                    "is_concurrent": False,
                },
                {
                    "id": "clip-2",
                    "filename": "02_line-a_1_2.mp4",
                    "start_time": 5.0,
                    "end_time": 10.0,
                    "action_index": 1,
                    "action_description": "(2) 安装",
                    "is_concurrent": False,
                },
            ),
            failure_code=None,
            failure_detail=None,
            created_at=NOW,
            updated_at=NOW,
            upstream_data_id="data-1",
            upstream_video_id="video-1",
        )
    )
    candidates: dict[UUID, VlmCandidate] = field(default_factory=dict)
    checks: dict[UUID, UsageCheck] = field(default_factory=dict)
    artifacts: dict[UUID, DatasetArtifact] = field(default_factory=dict)
    ddm_commands: dict[UUID, DdmTrainingCommand] = field(default_factory=dict)

    def dataset_by_id(self, dataset_id: UUID) -> TrainingDataset | None:
        return self.dataset if dataset_id == self.dataset.id else None

    def list_members(self, *, dataset_id: UUID) -> list[DatasetMember]:
        return [self.member] if dataset_id == self.dataset.id else []

    def member_by_id(self, member_id: UUID) -> DatasetMember | None:
        return self.member if member_id == self.member.id else None

    def latest_action_list(self, dataset_id: UUID) -> ActionListRevision | None:
        return self.action_list if dataset_id == self.dataset.id else None

    def action_list_by_revision(
        self, *, dataset_id: UUID, revision: int
    ) -> ActionListRevision | None:
        return (
            self.action_list
            if dataset_id == self.dataset.id and revision == self.action_list.revision
            else None
        )

    def latest_annotation_submission(self, *, member_id: UUID) -> AnnotationSubmission | None:
        return self.submission if member_id == self.member.id else None

    def annotation_submission_by_id(self, submission_id: UUID) -> AnnotationSubmission | None:
        return self.submission if submission_id == self.submission.id else None

    def latest_annotation_execution(self, submission_id: UUID) -> AnnotationExecution | None:
        return self.execution if submission_id == self.submission.id else None

    def annotation_execution_by_id(self, execution_id: UUID) -> AnnotationExecution | None:
        return self.execution if execution_id == self.execution.id else None

    def add_vlm_candidate(self, value: VlmCandidate) -> None:
        self.candidates[value.id] = value

    def vlm_candidate_by_id(self, candidate_id: UUID) -> VlmCandidate | None:
        return self.candidates.get(candidate_id)

    def latest_vlm_candidate(self, dataset_id: UUID) -> VlmCandidate | None:
        values = [item for item in self.candidates.values() if item.dataset_id == dataset_id]
        return max(values, key=lambda item: item.revision) if values else None

    def list_vlm_candidates(self, dataset_id: UUID) -> list[VlmCandidate]:
        return sorted(
            (item for item in self.candidates.values() if item.dataset_id == dataset_id),
            key=lambda item: item.revision,
        )

    def add_usage_check(self, value: UsageCheck) -> None:
        self.checks[value.id] = value

    def usage_check_by_id(self, check_id: UUID) -> UsageCheck | None:
        return self.checks.get(check_id)

    def list_usage_checks(self, dataset_id: UUID) -> list[UsageCheck]:
        return [item for item in self.checks.values() if item.dataset_id == dataset_id]

    def latest_usage_check(self, *, dataset_id: UUID, kind: UsageKind) -> UsageCheck | None:
        values = [
            item
            for item in self.checks.values()
            if item.dataset_id == dataset_id and item.kind is kind
        ]
        return max(values, key=lambda item: item.created_at) if values else None

    def save_usage_check(self, value: UsageCheck, *, expected_updated_at: datetime) -> bool:
        stored = self.checks.get(value.id)
        if stored is None or stored.updated_at != expected_updated_at:
            return False
        self.checks[value.id] = value
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

    def list_artifact_cleanup_candidates(self, *, limit: int) -> list[DatasetArtifact]:
        return [
            item for item in self.artifacts.values() if item.manifest.get("orphan_candidate_keys")
        ][:limit]

    def save_artifact(self, value: DatasetArtifact, *, expected_updated_at: datetime) -> bool:
        stored = self.artifacts.get(value.id)
        if stored is None or stored.updated_at != expected_updated_at:
            return False
        self.artifacts[value.id] = value
        return True

    def add_ddm_training_command(self, value: DdmTrainingCommand) -> bool:
        if any(
            item.dataset_id == value.dataset_id and item.idempotency_key == value.idempotency_key
            for item in self.ddm_commands.values()
        ):
            return False
        self.ddm_commands[value.id] = value
        return True

    def ddm_training_command_by_idempotency(
        self, *, dataset_id: UUID, idempotency_key: str
    ) -> DdmTrainingCommand | None:
        for item in self.ddm_commands.values():
            if item.dataset_id == dataset_id and item.idempotency_key == idempotency_key:
                return item
        return None

    def ddm_training_command_by_id(self, command_id: UUID) -> DdmTrainingCommand | None:
        return self.ddm_commands.get(command_id)

    def save_ddm_training_command(
        self, value: DdmTrainingCommand, *, expected_updated_at: datetime
    ) -> bool:
        stored = self.ddm_commands.get(value.id)
        if stored is None or stored.updated_at != expected_updated_at:
            return False
        self.ddm_commands[value.id] = value
        return True


class SupersededExecutionDatasets(FakeUsageDatasets):
    def __init__(self) -> None:
        super().__init__()
        self.newest_execution = replace(self.execution, id=NEW_EXECUTION_ID, generation=2)

    def latest_annotation_execution(self, submission_id: UUID) -> AnnotationExecution | None:
        return self.newest_execution if submission_id == self.submission.id else None


@dataclass
class FakeUsageJobs:
    next_id: UUID = field(default_factory=lambda: UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f307"))
    jobs: dict[UUID, ApplicationJob] = field(default_factory=dict)

    def get_or_create_usage_check(
        self, *, dataset_id: UUID, check_id: UUID, now: datetime
    ) -> ApplicationJob:
        return self._get(
            dataset_id=dataset_id, resource_id=check_id, kind=JobType.DATASET_USAGE_CHECK, now=now
        )

    def get_or_create_artifact(
        self, *, dataset_id: UUID, artifact_id: UUID, now: datetime
    ) -> ApplicationJob:
        return self._get(
            dataset_id=dataset_id, resource_id=artifact_id, kind=JobType.DATASET_ARTIFACT, now=now
        )

    def get_or_create_ddm_training(
        self, *, dataset_id: UUID, command_id: UUID, now: datetime
    ) -> ApplicationJob:
        for job in self.jobs.values():
            if job.job_type is JobType.DATASET_DDM_TRAINING and job.attempt_id == command_id:
                return job
        job = ApplicationJob(
            id=self.next_id,
            job_type=JobType.DATASET_DDM_TRAINING,
            status=JobStatus.PENDING,
            member_id=dataset_id,
            attempt_id=command_id,
            created_at=now,
            updated_at=now,
            failure_code=None,
            dataset_id=dataset_id,
        )
        self.jobs[job.id] = job
        return job

    def _get(
        self, *, dataset_id: UUID, resource_id: UUID, kind: JobType, now: datetime
    ) -> ApplicationJob:
        for job in self.jobs.values():
            if job.job_type is kind and job.attempt_id == resource_id:
                if job.status == JobStatus.FAILED:
                    job = replace(job, status=JobStatus.PENDING, failure_code=None, updated_at=now)
                    self.jobs[job.id] = job
                return job
        job = ApplicationJob(
            id=self.next_id,
            job_type=kind,
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


class TracingDatasets(FakeUsageDatasets):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []

    def list_members(self, *, dataset_id: UUID) -> list[DatasetMember]:
        self.events.append("database")
        return super().list_members(dataset_id=dataset_id)

    def latest_action_list(self, dataset_id: UUID) -> ActionListRevision | None:
        self.events.append("database")
        return super().latest_action_list(dataset_id)


class FakeStorage:
    def writing(self, *, object_key: str) -> AbstractContextManager[BinaryIO]:
        del object_key
        raise AssertionError("测试不应写入对象")

    def stat(self, *, object_key: str) -> ObjectStat:
        assert object_key == "datasets/source-a.mp4"
        return ObjectStat(size=100)

    def download_to(self, *, object_key: str, destination: BinaryIO) -> None:
        assert object_key == "datasets/source-a.mp4"
        destination.write(b"a" * 100)

    def delete(self, *, object_key: str) -> None:
        del object_key


class TracingStorage(FakeStorage):
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def stat(self, *, object_key: str) -> ObjectStat:
        self.events.append("media")
        return super().stat(object_key=object_key)

    def download_to(self, *, object_key: str, destination: BinaryIO) -> None:
        self.events.append("media")
        super().download_to(object_key=object_key, destination=destination)


class FakeMediaProbe:
    def probe(self, path: str) -> MediaMetadata:
        assert path.endswith(".mp4")
        return MediaMetadata(
            duration_seconds=10.0, codec="h264", container="mp4", fps=10.0, frame_count=100
        )


class ClipMediaProbe(FakeMediaProbe):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def probe(self, path: str) -> MediaMetadata:
        self.calls.append(path)
        return MediaMetadata(
            duration_seconds=5.0, codec="h264", container="mp4", fps=10.0, frame_count=50
        )


class FakeAnnotationVolume:
    def __init__(self) -> None:
        self.video_reads: list[tuple[str, str, str | None]] = []
        self.video_bytes = b"a" * 100
        self.annotation_bytes = json.dumps(
            [
                {
                    "action": 1,
                    "description": "(1) 取料",
                    "start_timestamp": 0.0,
                    "end_timestamp": 5.0,
                },
                {
                    "action": 2,
                    "description": "(2) 安装",
                    "start_timestamp": 5.0,
                    "end_timestamp": 10.0,
                },
            ]
        ).encode()

    def read_annotation(self, *, data_id: str, video_id: str) -> bytes:
        assert (data_id, video_id) == ("data-1", "video-1")
        return self.annotation_bytes

    def read_video(
        self,
        *,
        data_id: str,
        video_id: str,
        filename: str | None,
        destination: BinaryIO,
    ) -> None:
        assert (data_id, video_id) == ("data-1", "video-1")
        assert filename in {None, "01_line-a_1_1.mp4"}
        self.video_reads.append((data_id, video_id, filename))
        destination.write(self.video_bytes)


class RecordingDdmReader:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, str]] = []

    def sample_counts(
        self,
        *,
        workspace: Path,
        annotation_filename: str,
        parameters: Mapping[str, object],
    ) -> Mapping[str, int]:
        assert parameters == dict(DDM_CONSUMER_PARAMETERS)
        self.calls.append((workspace, annotation_filename))
        return {"boundary_sample_count": 1, "non_boundary_sample_count": 1}


class FailingDdmReader:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def sample_counts(
        self,
        *,
        workspace: Path,
        annotation_filename: str,
        parameters: Mapping[str, object],
    ) -> Mapping[str, int]:
        del workspace, annotation_filename, parameters
        raise self.error


def _running_ddm_usage_target() -> tuple[FakeUsageDatasets, UsageCheckTarget]:
    datasets = FakeUsageDatasets()
    source_sha256 = hashlib.sha256(b"a" * 100).hexdigest()
    datasets.member = replace(datasets.member, actual_sha256=source_sha256)
    datasets.submission = replace(datasets.submission, source_sha256=source_sha256)
    requested = request_usage_check(
        dataset_id=DATASET_ID,
        kind=UsageKind.DDM,
        candidate_id=None,
        caller=caller(Permission.DATASET_EDIT),
        now=NOW,
        datasets=cast(UsageDatasetRepository, datasets),
        jobs=cast(UsageJobQueue, FakeUsageJobs()),
    )
    target = begin_usage_check(
        job=requested.job,
        datasets=cast(UsageDatasetRepository, datasets),
        now=NOW + timedelta(seconds=1),
    )
    assert target is not None
    return datasets, target


class RecordingVlmReader:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, str]] = []

    def validate(self, *, workspace: Path, annotation_filename: str) -> None:
        self.calls.append((workspace, annotation_filename))


class ConcurrentAnnotationVolume(FakeAnnotationVolume):
    def read_annotation(self, *, data_id: str, video_id: str) -> bytes:
        assert (data_id, video_id) == ("data-1", "video-1")
        return json.dumps(
            [
                {
                    "start_timestamp": 0.0,
                    "end_timestamp": 5.0,
                    "descriptions": ["(1) 取料", "(2) 安装"],
                    "is_concurrent": True,
                },
                {
                    "start_timestamp": 5.0,
                    "end_timestamp": 10.0,
                    "descriptions": ["(1) 取料", "(2) 安装"],
                    "is_concurrent": True,
                },
            ],
            ensure_ascii=False,
        ).encode()


class WrongDigestStorage(FakeStorage):
    def download_to(self, *, object_key: str, destination: BinaryIO) -> None:
        assert object_key == "datasets/source-a.mp4"
        destination.write(b"z" * 100)


def test_usage_check_rejects_a_frozen_source_digest_mismatch() -> None:
    datasets = FakeUsageDatasets()
    jobs = FakeUsageJobs()
    requested = request_usage_check(
        dataset_id=DATASET_ID,
        kind=UsageKind.DDM,
        candidate_id=None,
        caller=caller(Permission.DATASET_EDIT),
        now=NOW,
        datasets=cast(UsageDatasetRepository, datasets),
        jobs=cast(UsageJobQueue, jobs),
    )
    target = begin_usage_check(
        job=requested.job,
        datasets=cast(UsageDatasetRepository, datasets),
        now=NOW + timedelta(seconds=1),
    )
    assert target is not None
    result = run_usage_check(
        target=target,
        storage=cast(ObjectStorage, WrongDigestStorage()),
        media_probe=FakeMediaProbe(),
        annotation_volume=FakeAnnotationVolume(),
    )
    assert result.passed is False
    assert any(issue.code == "USAGE_SOURCE_DIGEST_MISMATCH" for issue in result.issues)


def test_usage_check_reads_media_before_final_database_currentness_query() -> None:
    datasets = TracingDatasets()
    source_sha256 = hashlib.sha256(b"a" * 100).hexdigest()
    datasets.member = replace(datasets.member, actual_sha256=source_sha256)
    datasets.submission = replace(datasets.submission, source_sha256=source_sha256)
    jobs = FakeUsageJobs()
    requested = request_usage_check(
        dataset_id=DATASET_ID,
        kind=UsageKind.DDM,
        candidate_id=None,
        caller=caller(Permission.DATASET_EDIT),
        now=NOW,
        datasets=cast(UsageDatasetRepository, datasets),
        jobs=cast(UsageJobQueue, jobs),
    )
    target = begin_usage_check(
        job=requested.job,
        datasets=cast(UsageDatasetRepository, datasets),
        now=NOW + timedelta(seconds=1),
    )
    assert target is not None
    datasets.events.clear()
    reader = RecordingDdmReader()

    result = run_usage_check(
        target=target,
        storage=cast(ObjectStorage, TracingStorage(datasets.events)),
        media_probe=FakeMediaProbe(),
        annotation_volume=FakeAnnotationVolume(),
        ddm_reader=reader,
    )
    result = apply_usage_check_currentness(
        check=target.check,
        validation=result,
        datasets=cast(UsageDatasetRepository, datasets),
    )

    assert result.passed is True
    assert reader.calls
    assert datasets.events
    assert datasets.events[0] == "media"
    assert datasets.events[-1] == "database"


@pytest.mark.parametrize(
    ("error", "expected_code", "expected_retryable"),
    [
        (DdmReaderInputError(), "DDM_READER_INPUT_INVALID", False),
        (DdmReaderUnavailableError(), "DDM_READER_UNAVAILABLE", True),
    ],
)
def test_ddm_reader_known_failures_keep_distinct_usage_semantics(
    error: Exception, expected_code: str, expected_retryable: bool
) -> None:
    _, target = _running_ddm_usage_target()

    result = run_usage_check(
        target=target,
        storage=cast(ObjectStorage, FakeStorage()),
        media_probe=FakeMediaProbe(),
        annotation_volume=FakeAnnotationVolume(),
        ddm_reader=FailingDdmReader(error),
    )

    issue = next(item for item in result.issues if item.location == "reader")
    assert (issue.code, issue.retryable) == (expected_code, expected_retryable)


def test_ddm_reader_unknown_failure_propagates_from_usage_check() -> None:
    _, target = _running_ddm_usage_target()

    with pytest.raises(RuntimeError, match="reader internal failure"):
        run_usage_check(
            target=target,
            storage=cast(ObjectStorage, FakeStorage()),
            media_probe=FakeMediaProbe(),
            annotation_volume=FakeAnnotationVolume(),
            ddm_reader=FailingDdmReader(RuntimeError("reader internal failure")),
        )


def test_unknown_usage_execution_failure_is_persisted_without_retry_hint() -> None:
    datasets, target = _running_ddm_usage_target()

    failed = fail_usage_check(
        target=target,
        code="USAGE_CHECK_EXECUTION_FAILED",
        detail="用途检查执行失败",
        now=NOW + timedelta(seconds=2),
        datasets=cast(UsageDatasetRepository, datasets),
    )

    persisted = datasets.usage_check_by_id(failed.id)
    assert persisted is not None
    assert persisted.status is UsageCheckStatus.FAILED
    assert persisted.issues == (
        {
            "code": "USAGE_CHECK_EXECUTION_FAILED",
            "detail": "用途检查执行失败",
            "location": "worker",
            "retryable": False,
            "recovery_action": None,
        },
    )


def test_ddm_currentness_includes_derived_annotation_facts() -> None:
    datasets = FakeUsageDatasets()
    requested = request_usage_check(
        dataset_id=DATASET_ID,
        kind=UsageKind.DDM,
        candidate_id=None,
        caller=caller(Permission.DATASET_EDIT),
        now=NOW,
        datasets=cast(UsageDatasetRepository, datasets),
        jobs=cast(UsageJobQueue, FakeUsageJobs()),
    )
    assert requested.check.input_snapshot["videos"][0]["derived_video_size"] is None
    datasets.execution = replace(datasets.execution, derived_video_size=123)

    assert (
        usage_check_is_current(
            check=requested.check,
            datasets=cast(UsageDatasetRepository, datasets),
        )
        is False
    )


def test_ddm_accepts_concurrent_base_descriptions_without_action_indices() -> None:
    datasets = FakeUsageDatasets()
    source_sha256 = hashlib.sha256(b"a" * 100).hexdigest()
    datasets.member = replace(datasets.member, actual_sha256=source_sha256)
    datasets.submission = replace(
        datasets.submission,
        mode=AnnotationMode.TWO_OPERATOR,
        source_sha256=source_sha256,
        segments=(
            AnnotationSegment(0.0, 5.0, 0, "(1) 取料"),
            AnnotationSegment(0.0, 5.0, 1, "(2) 安装"),
            AnnotationSegment(5.0, 10.0, 0, "(1) 取料"),
            AnnotationSegment(5.0, 10.0, 1, "(2) 安装"),
        ),
    )
    datasets.execution = replace(
        datasets.execution,
        clips=(
            {
                "id": "clip-1",
                "filename": "01_line-a_1_1.mp4",
                "start_time": 0.0,
                "end_time": 5.0,
                "action_indices": [0, 1],
                "action_descriptions": ["(1) 取料", "(2) 安装"],
                "is_concurrent": True,
            },
            {
                "id": "clip-2",
                "filename": "02_line-a_1_2.mp4",
                "start_time": 5.0,
                "end_time": 10.0,
                "action_indices": [0, 1],
                "action_descriptions": ["(1) 取料", "(2) 安装"],
                "is_concurrent": True,
            },
        ),
    )
    requested = request_usage_check(
        dataset_id=DATASET_ID,
        kind=UsageKind.DDM,
        candidate_id=None,
        caller=caller(Permission.DATASET_EDIT),
        now=NOW,
        datasets=cast(UsageDatasetRepository, datasets),
        jobs=cast(UsageJobQueue, FakeUsageJobs()),
    )
    target = begin_usage_check(
        job=requested.job,
        datasets=cast(UsageDatasetRepository, datasets),
        now=NOW + timedelta(seconds=1),
    )
    assert target is not None

    result = run_usage_check(
        target=target,
        storage=cast(ObjectStorage, FakeStorage()),
        media_probe=FakeMediaProbe(),
        annotation_volume=ConcurrentAnnotationVolume(),
        ddm_reader=RecordingDdmReader(),
    )

    assert result.passed is True


def _ddm_snapshot() -> dict[str, object]:
    return {
        "dataset_id": str(DATASET_ID),
        "base_commit": BASE_COMMIT,
        "contract_version": DDM_CONTRACT_VERSION,
        "consumer_parameters": dict(DDM_CONSUMER_PARAMETERS),
        "scope": [
            {
                "member_id": str(MEMBER_ID),
                "status": "registered",
                "object_version_id": "datasets/source-a.mp4",
                "source_sha256": "a" * 64,
                "actual_size": 100,
                "annotation_submission_id": str(SUBMISSION_ID),
                "annotation_revision": 1,
                "annotation_action_list_revision": 1,
                "annotation_execution_id": str(EXECUTION_ID),
                "annotation_execution_status": "succeeded",
            }
        ],
        "videos": [
            {
                "member_id": str(MEMBER_ID),
                "object_version_id": "datasets/source-a.mp4",
                "source_sha256": "a" * 64,
                "annotation_execution_id": str(EXECUTION_ID),
                "upstream_data_id": "data-1",
                "upstream_video_id": "video-1",
                "annotation_revision": 1,
                "action_list_revision": 1,
                "derived_video_size": None,
                "derived_video_sha256": None,
                "derived_video_duration_seconds": None,
            }
        ],
    }


def test_checked_input_interface_exposes_only_passed_owned_resources() -> None:
    datasets = FakeUsageDatasets()
    check = UsageCheck(
        id=new_id(),
        dataset_id=DATASET_ID,
        kind=UsageKind.VLM,
        status=UsageCheckStatus.PASSED,
        input_digest="a" * 64,
        input_snapshot={"records": [{"video": "member.mp4"}]},
        summary={"record_count": 1},
        issues=(),
        base_commit=BASE_COMMIT,
        contract_version=VLM_CONTRACT_VERSION,
        candidate_id=None,
        job_id=None,
        created_by=ACTOR_ID,
        created_at=NOW,
        updated_at=NOW,
    )
    datasets.add_usage_check(check)
    reader = checked_input_reader(cast(UsageDatasetRepository, datasets))

    assert reader.checked_input(dataset_id=DATASET_ID, usage_check_id=check.id) == (
        CheckedDatasetInput(
            dataset_id=DATASET_ID,
            usage_check_id=check.id,
            kind=UsageKind.VLM,
            input_digest="a" * 64,
            input_snapshot={"records": [{"video": "member.mp4"}]},
            base_commit=BASE_COMMIT,
            contract_version=VLM_CONTRACT_VERSION,
            candidate_id=None,
        )
    )
    assert reader.checked_input(dataset_id=new_id(), usage_check_id=check.id) is None
    datasets.add_usage_check(replace(check, id=new_id(), status=UsageCheckStatus.FAILED))
    failed_id = next(item for item in datasets.checks if item != check.id)
    assert reader.checked_input(dataset_id=DATASET_ID, usage_check_id=failed_id) is None

    artifact = DatasetArtifact(
        id=new_id(),
        dataset_id=DATASET_ID,
        usage_check_id=check.id,
        kind=UsageKind.DDM,
        status=ArtifactStatus.AVAILABLE,
        input_digest="b" * 64,
        object_key="datasets/artifact.json",
        artifact_sha256="c" * 64,
        artifact_size=12,
        manifest={"input_digest": "b" * 64},
        failure_code=None,
        failure_detail=None,
        job_id=None,
        created_by=ACTOR_ID,
        created_at=NOW,
        updated_at=NOW,
    )
    datasets.add_artifact(artifact)
    published = reader.published_artifact(dataset_id=DATASET_ID, artifact_id=artifact.id)
    assert published is not None
    assert published.object_key == artifact.object_key
    assert reader.published_artifact(dataset_id=new_id(), artifact_id=artifact.id) is None


def test_changed_frozen_input_creates_a_new_artifact_without_overwriting_old() -> None:
    datasets = FakeUsageDatasets()
    jobs = FakeUsageJobs()
    first = UsageCheck(
        id=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f309"),
        dataset_id=DATASET_ID,
        kind=UsageKind.DDM,
        status=UsageCheckStatus.PASSED,
        input_digest="a" * 64,
        input_snapshot=_ddm_snapshot(),
        summary={},
        issues=(),
        base_commit=BASE_COMMIT,
        contract_version=DDM_CONTRACT_VERSION,
        candidate_id=None,
        job_id=None,
        created_by=ACTOR_ID,
        created_at=NOW,
        updated_at=NOW,
    )
    second = replace(
        first,
        id=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f30a"),
        input_digest="b" * 64,
        input_snapshot={**_ddm_snapshot(), "marker": "new-input"},
    )
    datasets.add_usage_check(first)
    datasets.add_usage_check(second)
    first_artifact = request_artifact(
        dataset_id=DATASET_ID,
        check_id=first.id,
        caller=caller(Permission.DATASET_EDIT),
        now=NOW,
        datasets=cast(UsageDatasetRepository, datasets),
        jobs=cast(UsageJobQueue, jobs),
    )
    second_artifact = request_artifact(
        dataset_id=DATASET_ID,
        check_id=second.id,
        caller=caller(Permission.DATASET_EDIT),
        now=NOW + timedelta(seconds=1),
        datasets=cast(UsageDatasetRepository, datasets),
        jobs=cast(UsageJobQueue, jobs),
    )
    assert first_artifact.artifact.id != second_artifact.artifact.id
    assert {item.input_digest for item in datasets.artifacts.values()} == {"a" * 64, "b" * 64}


def test_artifact_request_rejects_a_ddm_check_after_action_list_changes() -> None:
    datasets = FakeUsageDatasets()
    check = UsageCheck(
        id=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f30b"),
        dataset_id=DATASET_ID,
        kind=UsageKind.DDM,
        status=UsageCheckStatus.PASSED,
        input_digest="c" * 64,
        input_snapshot=_ddm_snapshot(),
        summary={},
        issues=(),
        base_commit=BASE_COMMIT,
        contract_version=DDM_CONTRACT_VERSION,
        candidate_id=None,
        job_id=None,
        created_by=ACTOR_ID,
        created_at=NOW,
        updated_at=NOW,
    )
    datasets.add_usage_check(check)
    datasets.action_list = replace(datasets.action_list, revision=2)
    with pytest.raises(DatasetRefusedError) as refused:
        request_artifact(
            dataset_id=DATASET_ID,
            check_id=check.id,
            caller=caller(Permission.DATASET_EDIT),
            now=NOW,
            datasets=cast(UsageDatasetRepository, datasets),
            jobs=cast(UsageJobQueue, FakeUsageJobs()),
        )
    assert refused.value.code is DatasetRefusalCode.USAGE_STATE_CONFLICT


def test_vlm_candidate_accepts_database_string_status_for_registered_media() -> None:
    datasets = FakeUsageDatasets(member=replace(make_member(), status="registered"))
    candidate = register_vlm_candidate(
        dataset_id=DATASET_ID,
        kind=VlmCandidateKind.GQA,
        action_list_revision=1,
        records=(),
        media=(VlmMediaReference("line-a.mp4", MEMBER_ID, "datasets/source-a.mp4", "a" * 64),),
        expected_revision=0,
        caller=caller(Permission.DATASET_EDIT),
        now=NOW,
        datasets=cast(UsageDatasetRepository, datasets),
    )
    assert candidate.revision == 1


def test_vlm_candidate_revision_uses_if_match_and_binds_current_media() -> None:
    datasets = FakeUsageDatasets()
    media = VlmMediaReference("line-a.mp4", MEMBER_ID, "datasets/source-a.mp4", "a" * 64)
    value = register_vlm_candidate(
        dataset_id=DATASET_ID,
        kind=VlmCandidateKind.GQA,
        action_list_revision=1,
        records=({"conversations": []},),
        media=(media,),
        expected_revision=0,
        caller=caller(Permission.DATASET_EDIT),
        now=NOW,
        datasets=cast(UsageDatasetRepository, datasets),
    )
    assert value.revision == 1
    with pytest.raises(DatasetRefusedError) as error:
        register_vlm_candidate(
            dataset_id=DATASET_ID,
            kind=VlmCandidateKind.GQA,
            action_list_revision=1,
            records=(),
            media=(media,),
            expected_revision=0,
            caller=caller(Permission.DATASET_EDIT),
            now=NOW + timedelta(seconds=1),
            datasets=cast(UsageDatasetRepository, datasets),
        )
    assert error.value.code is DatasetRefusalCode.STALE_REVISION


def test_vlm_clip_rejects_a_superseded_annotation_execution() -> None:
    datasets = SupersededExecutionDatasets()
    media = VlmMediaReference(
        key="clip-key.mp4",
        member_id=MEMBER_ID,
        source_object_key="datasets/source-a.mp4",
        source_sha256="a" * 64,
        annotation_submission_id=SUBMISSION_ID,
        annotation_execution_id=EXECUTION_ID,
        clip_index=0,
    )

    with pytest.raises(DatasetRefusedError) as refused:
        register_vlm_candidate(
            dataset_id=DATASET_ID,
            kind=VlmCandidateKind.GQA,
            action_list_revision=1,
            records=(),
            media=(media,),
            expected_revision=0,
            caller=caller(Permission.DATASET_EDIT),
            now=NOW,
            datasets=cast(UsageDatasetRepository, datasets),
        )

    assert refused.value.code is DatasetRefusalCode.VLM_CANDIDATE_INVALID


def test_vlm_clip_requires_submission_identity_alongside_execution_identity() -> None:
    datasets = FakeUsageDatasets()
    media = VlmMediaReference(
        key="clip-key.mp4",
        member_id=MEMBER_ID,
        source_object_key="datasets/source-a.mp4",
        source_sha256="a" * 64,
        annotation_execution_id=EXECUTION_ID,
        clip_index=0,
    )

    with pytest.raises(DatasetRefusedError) as refused:
        register_vlm_candidate(
            dataset_id=DATASET_ID,
            kind=VlmCandidateKind.GQA,
            action_list_revision=1,
            records=(),
            media=(media,),
            expected_revision=0,
            caller=caller(Permission.DATASET_EDIT),
            now=NOW,
            datasets=cast(UsageDatasetRepository, datasets),
        )

    assert refused.value.code is DatasetRefusalCode.VLM_CANDIDATE_INVALID


def test_vlm_check_reads_a_fixed_annotation_clip_instead_of_the_full_source() -> None:
    datasets = FakeUsageDatasets()
    datasets.execution = replace(
        datasets.execution,
        derived_video_size=100,
        derived_video_sha256=hashlib.sha256(b"a" * 100).hexdigest(),
    )
    media = VlmMediaReference(
        key="clip-key.mp4",
        member_id=MEMBER_ID,
        source_object_key="datasets/source-a.mp4",
        source_sha256="a" * 64,
        annotation_submission_id=SUBMISSION_ID,
        annotation_execution_id=EXECUTION_ID,
        clip_index=0,
    )
    candidate = register_vlm_candidate(
        dataset_id=DATASET_ID,
        kind=VlmCandidateKind.GQA,
        action_list_revision=1,
        records=(
            {
                "conversations": [
                    {"from": "human", "value": "<video>问题"},
                    {"from": "gpt", "value": "取料"},
                ],
                "video": "clip-key.mp4",
            },
        ),
        media=(media,),
        expected_revision=0,
        caller=caller(Permission.DATASET_EDIT),
        now=NOW,
        datasets=cast(UsageDatasetRepository, datasets),
    )
    jobs = FakeUsageJobs()
    requested = request_usage_check(
        dataset_id=DATASET_ID,
        kind=UsageKind.VLM,
        candidate_id=candidate.id,
        caller=caller(Permission.DATASET_EDIT),
        now=NOW,
        datasets=cast(UsageDatasetRepository, datasets),
        jobs=cast(UsageJobQueue, jobs),
    )
    target = begin_usage_check(
        job=requested.job,
        datasets=cast(UsageDatasetRepository, datasets),
        now=NOW + timedelta(seconds=1),
    )
    assert target is not None
    volume = FakeAnnotationVolume()
    reader = RecordingVlmReader()
    result = run_usage_check(
        target=target,
        storage=cast(ObjectStorage, FakeStorage()),
        media_probe=ClipMediaProbe(),
        annotation_volume=volume,
        vlm_reader=reader,
    )
    assert result.passed is True
    assert reader.calls
    assert volume.video_reads == [("data-1", "video-1", "01_line-a_1_1.mp4")]


@pytest.mark.parametrize(
    ("video_bytes", "expected_size", "expected_digest", "expected_issue"),
    [
        (
            b"b" * 100,
            100,
            hashlib.sha256(b"a" * 100).hexdigest(),
            "USAGE_SOURCE_DIGEST_MISMATCH",
        ),
        (
            b"b" * 99,
            100,
            hashlib.sha256(b"b" * 99).hexdigest(),
            "USAGE_OBJECT_SIZE_CHANGED",
        ),
        (b"a" * 100, 100, None, "USAGE_SOURCE_DIGEST_MISSING"),
        (
            b"a" * 100,
            None,
            hashlib.sha256(b"a" * 100).hexdigest(),
            "USAGE_SOURCE_SIZE_MISSING",
        ),
        (b"a" * 100, 100, "a" * 63, "USAGE_SOURCE_DIGEST_MISSING"),
        (b"a" * 100, 100, "g" * 64, "USAGE_SOURCE_DIGEST_MISSING"),
        (
            b"a" * 100,
            100,
            hashlib.sha256(b"a" * 100).hexdigest().upper(),
            "USAGE_SOURCE_DIGEST_MISMATCH",
        ),
    ],
    ids=(
        "digest",
        "size",
        "missing_digest",
        "missing_size",
        "short_digest",
        "non_hex_digest",
        "uppercase_digest",
    ),
)
def test_vlm_check_rejects_changed_annotation_clip_before_reader(
    video_bytes: bytes, expected_size: int | None, expected_digest: str | None, expected_issue: str
) -> None:
    datasets = FakeUsageDatasets()
    datasets.execution = replace(
        datasets.execution,
        derived_video_size=expected_size,
        derived_video_sha256=expected_digest,
    )
    media = VlmMediaReference(
        key="clip-key.mp4",
        member_id=MEMBER_ID,
        source_object_key="datasets/source-a.mp4",
        source_sha256="a" * 64,
        annotation_submission_id=SUBMISSION_ID,
        annotation_execution_id=EXECUTION_ID,
        clip_index=0,
    )
    candidate = register_vlm_candidate(
        dataset_id=DATASET_ID,
        kind=VlmCandidateKind.GQA,
        action_list_revision=1,
        records=(
            {
                "conversations": [
                    {"from": "human", "value": "<video>问题"},
                    {"from": "gpt", "value": "取料"},
                ],
                "video": "clip-key.mp4",
            },
        ),
        media=(media,),
        expected_revision=0,
        caller=caller(Permission.DATASET_EDIT),
        now=NOW,
        datasets=cast(UsageDatasetRepository, datasets),
    )
    requested = request_usage_check(
        dataset_id=DATASET_ID,
        kind=UsageKind.VLM,
        candidate_id=candidate.id,
        caller=caller(Permission.DATASET_EDIT),
        now=NOW,
        datasets=cast(UsageDatasetRepository, datasets),
        jobs=cast(UsageJobQueue, FakeUsageJobs()),
    )
    target = begin_usage_check(
        job=requested.job,
        datasets=cast(UsageDatasetRepository, datasets),
        now=NOW + timedelta(seconds=1),
    )
    assert target is not None
    volume = FakeAnnotationVolume()
    volume.video_bytes = video_bytes
    media_probe = ClipMediaProbe()
    reader = RecordingVlmReader()
    result = run_usage_check(
        target=target,
        storage=cast(ObjectStorage, FakeStorage()),
        media_probe=media_probe,
        annotation_volume=volume,
        vlm_reader=reader,
    )

    assert result.passed is False
    assert any(issue.code == expected_issue for issue in result.issues)
    assert media_probe.calls == []
    assert reader.calls == []


_VLM_SOURCE_DIGEST = hashlib.sha256(b"a" * 100).hexdigest()


def _vlm_records_for(video_key: str) -> tuple[dict[str, object], ...]:
    return (
        {
            "conversations": [
                {"from": "human", "value": "<video>问题"},
                {"from": "gpt", "value": "取料"},
            ],
            "video": video_key,
        },
    )


def _fresh_vlm_datasets() -> FakeUsageDatasets:
    """已确认媒体摘要与 VLM 检查一致的候选仓储。"""
    datasets = FakeUsageDatasets()
    datasets.member = replace(datasets.member, actual_sha256=_VLM_SOURCE_DIGEST)
    datasets.submission = replace(datasets.submission, source_sha256=_VLM_SOURCE_DIGEST)
    return datasets


def _whole_video_media(*, key: str = "vlm-source.mp4") -> VlmMediaReference:
    return VlmMediaReference(
        key=key,
        member_id=MEMBER_ID,
        source_object_key="datasets/source-a.mp4",
        source_sha256=_VLM_SOURCE_DIGEST,
    )


def _passing_vlm_check(
    datasets: FakeUsageDatasets,
    *,
    media: VlmMediaReference,
    annotation_volume: FakeAnnotationVolume | None = None,
    media_probe: FakeMediaProbe | None = None,
    records: tuple[dict[str, object], ...] | None = None,
    kind: VlmCandidateKind = VlmCandidateKind.GQA,
    expected_revision: int = 0,
) -> UsageCheck:
    """用显式媒体事实产生一个已通过且当前可用的 VLM 用途检查。"""
    candidate = register_vlm_candidate(
        dataset_id=DATASET_ID,
        kind=kind,
        action_list_revision=1,
        records=records if records is not None else _vlm_records_for(media.key),
        media=(media,),
        expected_revision=expected_revision,
        caller=caller(Permission.DATASET_EDIT),
        now=NOW,
        datasets=cast(UsageDatasetRepository, datasets),
    )
    requested = request_usage_check(
        dataset_id=DATASET_ID,
        kind=UsageKind.VLM,
        candidate_id=candidate.id,
        caller=caller(Permission.DATASET_EDIT),
        now=NOW,
        datasets=cast(UsageDatasetRepository, datasets),
        jobs=cast(UsageJobQueue, FakeUsageJobs()),
    )
    target = begin_usage_check(
        job=requested.job,
        datasets=cast(UsageDatasetRepository, datasets),
        now=NOW + timedelta(seconds=1),
    )
    assert target is not None
    validation = run_usage_check(
        target=target,
        storage=cast(ObjectStorage, FakeStorage()),
        media_probe=media_probe if media_probe is not None else FakeMediaProbe(),
        annotation_volume=annotation_volume,
        vlm_reader=RecordingVlmReader(),
    )
    assert validation.passed is True, validation.issues
    return complete_usage_check(
        target=target,
        validation=validation,
        now=NOW + timedelta(seconds=2),
        datasets=cast(UsageDatasetRepository, datasets),
    )


def test_freeze_vlm_input_freezes_records_bytes_and_media_provenance() -> None:
    datasets = _fresh_vlm_datasets()
    check = _passing_vlm_check(datasets, media=_whole_video_media())

    frozen = freeze_vlm_input(
        selection=VlmInputSelection(dataset_id=DATASET_ID, usage_check_id=check.id),
        caller=caller(Permission.DATASET_EDIT),
        datasets=cast(UsageDatasetRepository, datasets),
    )

    assert frozen.dataset_id == DATASET_ID
    assert frozen.usage_check_id == check.id
    assert frozen.candidate_id == check.candidate_id
    assert frozen.input_digest == check.input_digest
    assert frozen.base_commit == BASE_COMMIT
    assert frozen.contract_version == VLM_CONTRACT_VERSION
    assert check.input_snapshot["consumer_parameters"] == {"vision": {}, "system_prompt": ""}
    assert dict(frozen.consumer_parameters) == check.input_snapshot["consumer_parameters"]
    # 记录冻结为字节而不是可变字典，摘要独立可复验。
    assert frozen.records_bytes == canonical_json(check.input_snapshot["records"])
    assert frozen.records_sha256 == hashlib.sha256(frozen.records_bytes).hexdigest()
    assert len(frozen.media) == 1
    media = frozen.media[0]
    assert (media.key, media.member_id) == ("vlm-source.mp4", MEMBER_ID)
    assert media.is_annotation_clip is False
    assert media.object_key == "datasets/source-a.mp4"
    assert media.source_sha256 == _VLM_SOURCE_DIGEST
    assert media.actual_size == 100
    assert media.derived_video_sha256 is None


def test_frozen_vlm_consumer_parameters_have_no_shared_mutable_substructure() -> None:
    datasets = _fresh_vlm_datasets()
    check = _passing_vlm_check(datasets, media=_whole_video_media())
    first = _frozen_vlm_input(datasets, check)
    second = _frozen_vlm_input(datasets, check)
    check.input_snapshot["consumer_parameters"]["vision"]["fps"] = 8

    first_vision = cast(dict[str, object], first.consumer_parameters)["vision"]
    with pytest.raises(TypeError):
        cast(dict[str, object], first_vision)["fps"] = 8
    constant_vision = cast(dict[str, object], VLM_CONSUMER_PARAMETERS)["vision"]
    with pytest.raises(TypeError):
        cast(dict[str, object], constant_vision)["fps"] = 8

    # 两份冻结输入与常量不共享可变的 vision 子映射。
    assert dict(first.consumer_parameters) == dict(second.consumer_parameters)
    assert dict(first.consumer_parameters) == dict(VLM_CONSUMER_PARAMETERS)


@pytest.mark.parametrize("parameters", [None, {"vision": {"fps": 8}, "system_prompt": ""}])
def test_freeze_vlm_input_rejects_missing_or_changed_checked_parameters(
    parameters: dict[str, object] | None,
) -> None:
    datasets = _fresh_vlm_datasets()
    check = _passing_vlm_check(datasets, media=_whole_video_media())
    snapshot = json.loads(canonical_json(check.input_snapshot))
    if parameters is None:
        snapshot.pop("consumer_parameters", None)
    else:
        snapshot["consumer_parameters"] = parameters
    datasets.checks[check.id] = replace(check, input_snapshot=snapshot)

    with pytest.raises(DatasetRefusedError) as refused:
        _frozen_vlm_input(datasets, check)
    assert refused.value.code is DatasetRefusalCode.USAGE_STATE_CONFLICT


def test_freeze_vlm_input_requires_dataset_edit_permission() -> None:
    datasets = _fresh_vlm_datasets()
    check = _passing_vlm_check(datasets, media=_whole_video_media())

    with pytest.raises(AuthorizationRefusedError):
        freeze_vlm_input(
            selection=VlmInputSelection(dataset_id=DATASET_ID, usage_check_id=check.id),
            caller=caller(Permission.DATASET_VIEW),
            datasets=cast(UsageDatasetRepository, datasets),
        )


def test_freeze_vlm_input_rejects_an_inapplicable_check() -> None:
    datasets = FakeUsageDatasets()
    jobs = FakeUsageJobs()
    artifact, _ = _passing_ddm_artifact(datasets, jobs)

    with pytest.raises(DatasetRefusedError) as refused:
        freeze_vlm_input(
            selection=VlmInputSelection(
                dataset_id=DATASET_ID, usage_check_id=artifact.usage_check_id
            ),
            caller=caller(Permission.DATASET_EDIT),
            datasets=cast(UsageDatasetRepository, datasets),
        )
    assert refused.value.code is DatasetRefusalCode.USAGE_STATE_CONFLICT


def test_freeze_vlm_input_rejects_a_changed_candidate_revision() -> None:
    datasets = _fresh_vlm_datasets()
    check = _passing_vlm_check(datasets, media=_whole_video_media())
    register_vlm_candidate(
        dataset_id=DATASET_ID,
        kind=VlmCandidateKind.GQA,
        action_list_revision=1,
        records=_vlm_records_for("vlm-source.mp4"),
        media=(_whole_video_media(),),
        expected_revision=1,
        caller=caller(Permission.DATASET_EDIT),
        now=NOW + timedelta(seconds=3),
        datasets=cast(UsageDatasetRepository, datasets),
    )

    with pytest.raises(DatasetRefusedError) as refused:
        freeze_vlm_input(
            selection=VlmInputSelection(dataset_id=DATASET_ID, usage_check_id=check.id),
            caller=caller(Permission.DATASET_EDIT),
            datasets=cast(UsageDatasetRepository, datasets),
        )
    assert refused.value.code is DatasetRefusalCode.USAGE_STATE_CONFLICT


def test_freeze_vlm_input_rejects_an_escaping_media_key() -> None:
    datasets = _fresh_vlm_datasets()
    check = _passing_vlm_check(datasets, media=_whole_video_media())
    stored = datasets.checks[check.id]
    snapshot = json.loads(json.dumps(stored.input_snapshot))
    snapshot["media"][0]["key"] = "../escape.mp4"
    datasets.checks[check.id] = replace(stored, input_snapshot=snapshot)

    with pytest.raises(DatasetRefusedError) as refused:
        freeze_vlm_input(
            selection=VlmInputSelection(dataset_id=DATASET_ID, usage_check_id=check.id),
            caller=caller(Permission.DATASET_EDIT),
            datasets=cast(UsageDatasetRepository, datasets),
        )
    assert refused.value.code is DatasetRefusalCode.USAGE_STATE_CONFLICT


_VENDOR_VLM_JSON_DISCOVERY = (
    Path(__file__).resolve().parents[4]
    / "vendor/sop-monitoring-blueprints/microservices/sop-training-bp"
    / "microservices/cr-training-ms/utils/dataset_utils.py"
)


def _base_json_discovery(dataset_path: Path) -> list[str]:
    """调用 NVIDIA 真实递归 JSON 发现函数，而不是自建目录扫描。"""
    spec = importlib.util.spec_from_file_location(
        "nvsop_base_vlm_json_discovery", _VENDOR_VLM_JSON_DISCOVERY
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [str(path) for path in module.get_all_json_paths(str(dataset_path))]


def _frozen_vlm_input(datasets: FakeUsageDatasets, check: UsageCheck) -> FrozenVlmInput:
    return freeze_vlm_input(
        selection=VlmInputSelection(dataset_id=DATASET_ID, usage_check_id=check.id),
        caller=caller(Permission.DATASET_EDIT),
        datasets=cast(UsageDatasetRepository, datasets),
    )


@pytest.mark.parametrize(
    ("kind", "prompt", "response"),
    [
        (VlmCandidateKind.GQA, "问题", "取料"),
        (VlmCandidateKind.BCQ, "是否取料", "是"),
        (VlmCandidateKind.MCQ, "选择动作\n(1) 取料\n(2) 安装", "(1) 取料"),
        (VlmCandidateKind.GOLDEN_GQA, "黄金问题", "取料"),
    ],
)
def test_materialize_vlm_input_is_consumed_by_original_reader_without_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: VlmCandidateKind,
    prompt: str,
    response: str,
) -> None:
    datasets = _fresh_vlm_datasets()
    record: dict[str, object] = {
        "conversations": [
            {"from": "human", "value": "<video>\n" + prompt},
            {"from": "gpt", "value": response},
        ],
        "video": "registered/vlm-source.mp4",
    }
    if kind is VlmCandidateKind.MCQ:
        record["action_indices"] = [1]
    check = _passing_vlm_check(
        datasets,
        media=_whole_video_media(key="registered/vlm-source.mp4"),
        kind=kind,
        records=(record,),
    )
    frozen = _frozen_vlm_input(datasets, check)
    storage = _seeded_storage(tmp_path / "objects", {"datasets/source-a.mp4": b"a" * 100})

    materialized = materialize_vlm_input(
        frozen=frozen, storage=storage, annotation_volume=None, root=tmp_path / "input"
    )

    assert materialized.reader_directory == tmp_path / "input" / str(frozen.input_id) / "reader"
    assert (
        materialized.reader_directory / VLM_RECORDS_FILENAME
    ).read_bytes() == frozen.records_bytes
    assert (materialized.reader_directory / "vlm-source.mp4").read_bytes() == b"a" * 100
    manifest = json.loads(materialized.manifest_path.read_bytes())
    assert manifest["input_digest"] == check.input_digest
    assert manifest["consumer_parameters"] == dict(VLM_CONSUMER_PARAMETERS)
    assert manifest["records_sha256"] == frozen.records_sha256
    assert [item["filename"] for item in manifest["media"]] == ["vlm-source.mp4"]
    # 管理清单写在 input_id 根目录，真实递归发现只拿到候选记录。
    assert not (materialized.reader_directory / VLM_MANIFEST_FILENAME).exists()
    discovered = _base_json_discovery(materialized.reader_directory)
    assert discovered == [str(materialized.reader_directory / VLM_RECORDS_FILENAME)]
    contract_path = (
        Path(__file__).resolve().parents[4] / "tests/contract/base/test_vlm_reader_contract.py"
    )
    contract_spec = importlib.util.spec_from_file_location(
        "nvsop_vlm_contract_stubs", contract_path
    )
    assert contract_spec is not None
    assert contract_spec.loader is not None
    contract = importlib.util.module_from_spec(contract_spec)
    contract_spec.loader.exec_module(contract)
    captured: dict[str, object] = {}
    with monkeypatch.context() as context:
        for name, module_stub in contract._module_stubs(captured).items():
            context.setitem(sys.modules, name, module_stub)
        reader_spec = importlib.util.spec_from_file_location(
            "nvsop_materialized_vlm_reader", contract.MODULE_PATH
        )
        assert reader_spec is not None
        assert reader_spec.loader is not None
        reader = importlib.util.module_from_spec(reader_spec)
        reader_spec.loader.exec_module(reader)
        config = SimpleNamespace(
            train=SimpleNamespace(
                train_policy=SimpleNamespace(
                    dataset=SimpleNamespace(name=discovered, split=["train"])
                )
            )
        )
        custom_config = SimpleNamespace(
            vision=SimpleNamespace(
                model_dump=lambda exclude_none=True: dict(
                    cast(Mapping[str, object], frozen.consumer_parameters["vision"])
                )
            ),
            system_prompt=frozen.consumer_parameters["system_prompt"],
        )
        dataset = reader.CosmosSFTDataset(config=config, custom_config=custom_config)
        assert len(dataset) == 1
        dataset[0]
        assert captured["user_prompt"] == prompt
        assert captured["response"] == response
        assert captured["videos"] == [str(materialized.reader_directory / "vlm-source.mp4")]
        assert captured["system_prompt"] == ""
        assert captured["vision_kwargs"] == {}


def test_materialize_vlm_input_refuses_overwrite_and_preserves_in_flight_bytes(
    tmp_path: Path,
) -> None:
    datasets = _fresh_vlm_datasets()
    check = _passing_vlm_check(datasets, media=_whole_video_media())
    frozen = _frozen_vlm_input(datasets, check)
    storage = _seeded_storage(tmp_path / "objects", {"datasets/source-a.mp4": b"a" * 100})

    first = materialize_vlm_input(
        frozen=frozen, storage=storage, annotation_volume=None, root=tmp_path / "input"
    )
    in_flight = (first.reader_directory / "vlm-source.mp4").read_bytes()

    with pytest.raises(DatasetRefusedError) as refused:
        materialize_vlm_input(
            frozen=frozen, storage=storage, annotation_volume=None, root=tmp_path / "input"
        )
    assert refused.value.code is DatasetRefusalCode.USAGE_STATE_CONFLICT
    assert (first.reader_directory / "vlm-source.mp4").read_bytes() == in_flight


def test_materialize_vlm_input_keeps_prior_preparation_after_candidate_edit(
    tmp_path: Path,
) -> None:
    datasets = _fresh_vlm_datasets()
    first_check = _passing_vlm_check(datasets, media=_whole_video_media())
    first_frozen = _frozen_vlm_input(datasets, first_check)
    storage = _seeded_storage(tmp_path / "objects", {"datasets/source-a.mp4": b"a" * 100})
    first = materialize_vlm_input(
        frozen=first_frozen, storage=storage, annotation_volume=None, root=tmp_path / "input"
    )
    history = {path: path.read_bytes() for path in first.root.rglob("*") if path.is_file()}
    changed_records: tuple[dict[str, object], ...] = (
        {
            "conversations": [
                {"from": "human", "value": "<video>修改后问题"},
                {"from": "gpt", "value": "安装"},
            ],
            "video": "vlm-source.mp4",
        },
    )
    second_check = _passing_vlm_check(
        datasets, media=_whole_video_media(), records=changed_records, expected_revision=1
    )
    second_frozen = _frozen_vlm_input(datasets, second_check)
    second = materialize_vlm_input(
        frozen=second_frozen, storage=storage, annotation_volume=None, root=tmp_path / "input"
    )
    assert first_frozen.input_id != second_frozen.input_id
    assert first_frozen.candidate_id != second_frozen.candidate_id
    assert first_frozen.input_digest != second_frozen.input_digest
    assert first_frozen.records_bytes != second_frozen.records_bytes
    assert (second.reader_directory / VLM_RECORDS_FILENAME).read_bytes() == canonical_json(
        changed_records
    )
    assert all(path.read_bytes() == content for path, content in history.items())
    assert (
        first.reader_directory / VLM_RECORDS_FILENAME
    ).read_bytes() == first_frozen.records_bytes
    assert (tmp_path / "objects/datasets/source-a.mp4").read_bytes() == b"a" * 100


def test_materialize_vlm_input_refuses_a_symlinked_input_identity(tmp_path: Path) -> None:
    datasets = _fresh_vlm_datasets()
    check = _passing_vlm_check(datasets, media=_whole_video_media())
    frozen = _frozen_vlm_input(datasets, check)
    storage = _seeded_storage(tmp_path / "objects", {"datasets/source-a.mp4": b"a" * 100})
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "input"
    root.mkdir()
    (root / str(frozen.input_id)).symlink_to(outside, target_is_directory=True)

    with pytest.raises(DatasetRefusedError):
        materialize_vlm_input(frozen=frozen, storage=storage, annotation_volume=None, root=root)
    assert list(outside.iterdir()) == []


def test_materialize_vlm_input_fails_closed_without_partial_output(tmp_path: Path) -> None:
    datasets = _fresh_vlm_datasets()
    check = _passing_vlm_check(datasets, media=_whole_video_media())
    frozen = _frozen_vlm_input(datasets, check)
    storage = _seeded_storage(tmp_path / "objects", {"datasets/source-a.mp4": b"wrong-bytes"})

    with pytest.raises(DatasetRefusedError) as refused:
        materialize_vlm_input(
            frozen=frozen, storage=storage, annotation_volume=None, root=tmp_path / "input"
        )
    assert refused.value.code is DatasetRefusalCode.ARTIFACT_INTEGRITY_FAILURE
    assert not (tmp_path / "input" / str(frozen.input_id)).exists()
    assert (tmp_path / "objects" / "datasets/source-a.mp4").read_bytes() == b"wrong-bytes"


def test_materialize_vlm_input_reports_write_failure_and_preserves_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    datasets = _fresh_vlm_datasets()
    check = _passing_vlm_check(datasets, media=_whole_video_media())
    storage = _seeded_storage(tmp_path / "objects", {"datasets/source-a.mp4": b"a" * 100})
    first = materialize_vlm_input(
        frozen=_frozen_vlm_input(datasets, check),
        storage=storage,
        annotation_volume=None,
        root=tmp_path / "input",
    )
    history = {path: path.read_bytes() for path in first.root.rglob("*") if path.is_file()}
    frozen = _frozen_vlm_input(datasets, check)
    original_write = Path.write_bytes

    def write_bytes(path: Path, content: bytes) -> int:
        if path == tmp_path / "input" / str(frozen.input_id) / VLM_MANIFEST_FILENAME:
            raise OSError("synthetic disk full")
        return original_write(path, content)

    monkeypatch.setattr(Path, "write_bytes", write_bytes)
    with pytest.raises(DatasetRefusedError) as refused:
        materialize_vlm_input(
            frozen=frozen, storage=storage, annotation_volume=None, root=tmp_path / "input"
        )
    assert refused.value.code is DatasetRefusalCode.STORAGE_UNAVAILABLE
    assert not (tmp_path / "input" / str(frozen.input_id)).exists()
    assert all(path.read_bytes() == content for path, content in history.items())
    assert (tmp_path / "objects/datasets/source-a.mp4").read_bytes() == b"a" * 100


def test_materialize_vlm_input_uses_the_fixed_annotation_clip(tmp_path: Path) -> None:
    datasets = _fresh_vlm_datasets()
    datasets.execution = replace(
        datasets.execution,
        derived_video_size=100,
        derived_video_sha256=_VLM_SOURCE_DIGEST,
    )
    media = VlmMediaReference(
        key="clip-key.mp4",
        member_id=MEMBER_ID,
        source_object_key="datasets/source-a.mp4",
        source_sha256=_VLM_SOURCE_DIGEST,
        annotation_submission_id=SUBMISSION_ID,
        annotation_execution_id=EXECUTION_ID,
        clip_index=0,
    )
    check = _passing_vlm_check(
        datasets,
        media=media,
        annotation_volume=FakeAnnotationVolume(),
        media_probe=ClipMediaProbe(),
    )
    frozen = _frozen_vlm_input(datasets, check)
    assert frozen.media[0].is_annotation_clip is True

    materialize_volume = FakeAnnotationVolume()
    materialized = materialize_vlm_input(
        frozen=frozen,
        storage=cast(ObjectStorage, FakeStorage()),
        annotation_volume=materialize_volume,
        root=tmp_path / "input",
    )

    assert (materialized.reader_directory / "clip-key.mp4").read_bytes() == b"a" * 100
    assert materialize_volume.video_reads == [("data-1", "video-1", "01_line-a_1_1.mp4")]


def test_complete_usage_check_persists_recovery_metadata() -> None:
    datasets = FakeUsageDatasets()
    jobs = FakeUsageJobs()
    requested = request_usage_check(
        dataset_id=DATASET_ID,
        kind=UsageKind.DDM,
        candidate_id=None,
        caller=caller(Permission.DATASET_EDIT),
        now=NOW,
        datasets=cast(UsageDatasetRepository, datasets),
        jobs=cast(UsageJobQueue, jobs),
    )
    target = begin_usage_check(
        job=requested.job,
        datasets=cast(UsageDatasetRepository, datasets),
        now=NOW + timedelta(seconds=1),
    )
    assert target is not None
    issue = UsageIssue(
        "USAGE_STORAGE_UNAVAILABLE",
        "训练素材存储暂时不可用",
        str(MEMBER_ID),
        retryable=True,
        recovery_action="retry_usage_check",
    )
    result = complete_usage_check(
        target=target,
        validation=UsageValidationResult(
            passed=False,
            issues=(issue,),
            input_snapshot=dict(target.check.input_snapshot),
            input_digest=target.check.input_digest,
            summary={},
        ),
        now=NOW + timedelta(seconds=2),
        datasets=cast(UsageDatasetRepository, datasets),
    )
    assert result.issues == (
        {
            "code": "USAGE_STORAGE_UNAVAILABLE",
            "detail": "训练素材存储暂时不可用",
            "location": str(MEMBER_ID),
            "retryable": True,
            "recovery_action": "retry_usage_check",
        },
    )


def test_ddm_check_freezes_sources_runs_async_and_generates_stable_artifact() -> None:
    datasets = FakeUsageDatasets()
    source_digest = hashlib.sha256(b"a" * 100).hexdigest()
    datasets.member = replace(datasets.member, actual_sha256=source_digest)
    datasets.submission = replace(datasets.submission, source_sha256=source_digest)
    jobs = FakeUsageJobs()
    requested = request_usage_check(
        dataset_id=DATASET_ID,
        kind=UsageKind.DDM,
        candidate_id=None,
        caller=caller(Permission.DATASET_EDIT),
        now=NOW,
        datasets=cast(UsageDatasetRepository, datasets),
        jobs=cast(UsageJobQueue, jobs),
    )
    target = begin_usage_check(
        job=requested.job,
        datasets=cast(UsageDatasetRepository, datasets),
        now=NOW + timedelta(seconds=1),
    )
    assert target is not None
    volume = FakeAnnotationVolume()
    validation = run_usage_check(
        target=target,
        storage=cast(ObjectStorage, FakeStorage()),
        media_probe=FakeMediaProbe(),
        annotation_volume=volume,
        ddm_reader=RecordingDdmReader(),
    )
    check = complete_usage_check(
        target=target,
        validation=validation,
        now=NOW + timedelta(seconds=2),
        datasets=cast(UsageDatasetRepository, datasets),
    )
    assert check.status is UsageCheckStatus.PASSED
    original_annotation = volume.annotation_bytes
    source_copy = check.input_snapshot["annotation_sources"][str(MEMBER_ID)]
    assert base64.b64decode(source_copy["content_base64"], validate=True) == original_annotation
    assert source_copy["sha256"] == hashlib.sha256(original_annotation).hexdigest()
    requested_artifact = request_artifact(
        dataset_id=DATASET_ID,
        check_id=check.id,
        caller=caller(Permission.DATASET_EDIT),
        now=NOW + timedelta(seconds=3),
        datasets=cast(UsageDatasetRepository, datasets),
        jobs=cast(UsageJobQueue, jobs),
    )
    artifact_target = begin_artifact_generation(
        job=requested_artifact.job,
        datasets=cast(UsageDatasetRepository, datasets),
        now=NOW + timedelta(seconds=4),
    )
    assert artifact_target is not None
    failed = fail_artifact(
        target=artifact_target,
        code="STORAGE_UNAVAILABLE",
        detail="旧制品候选清理失败，请稍后重试",
        now=NOW + timedelta(seconds=4, milliseconds=500),
        datasets=cast(UsageDatasetRepository, datasets),
    )
    assert (failed.retryable, failed.recovery_action) == (True, "retry_artifact")
    retried = request_artifact(
        dataset_id=DATASET_ID,
        check_id=check.id,
        caller=caller(Permission.DATASET_EDIT),
        now=NOW + timedelta(seconds=5),
        datasets=cast(UsageDatasetRepository, datasets),
        jobs=cast(UsageJobQueue, jobs),
    )
    artifact_target = begin_artifact_generation(
        job=retried.job,
        datasets=cast(UsageDatasetRepository, datasets),
        now=NOW + timedelta(seconds=5, milliseconds=500),
    )
    assert artifact_target is not None
    failed_integrity = fail_artifact(
        target=artifact_target,
        code="ARTIFACT_INTEGRITY_FAILURE",
        detail="制品对象回读摘要与生成内容不一致",
        now=NOW + timedelta(seconds=5, milliseconds=750),
        datasets=cast(UsageDatasetRepository, datasets),
    )
    assert (failed_integrity.retryable, failed_integrity.recovery_action) == (
        True,
        "retry_artifact",
    )
    retried = request_artifact(
        dataset_id=DATASET_ID,
        check_id=check.id,
        caller=caller(Permission.DATASET_EDIT),
        now=NOW + timedelta(seconds=6),
        datasets=cast(UsageDatasetRepository, datasets),
        jobs=cast(UsageJobQueue, jobs),
    )
    artifact_target = begin_artifact_generation(
        job=retried.job,
        datasets=cast(UsageDatasetRepository, datasets),
        now=NOW + timedelta(seconds=6, milliseconds=500),
    )
    assert artifact_target is not None

    seen_annotation_copies: list[bytes] = []
    volume.annotation_bytes = b"[]"

    def generate(workspace: Path, _output_filename: str) -> bytes:
        seen_annotation_copies.append(
            (workspace / str(MEMBER_ID) / f"{MEMBER_ID}_annotation.json").read_bytes()
        )
        return json.dumps(
            {
                str(MEMBER_ID): [
                    {
                        "action": 1,
                        "description": "(1) 取料",
                        "start_timestamp": 0.0,
                        "end_timestamp": 5.0,
                    },
                    {
                        "action": 2,
                        "description": "(2) 安装",
                        "start_timestamp": 5.0,
                        "end_timestamp": 10.0,
                    },
                ]
            },
            ensure_ascii=False,
        ).encode()

    content, manifest = render_ddm_artifact_with_base(
        artifact_target,
        storage=cast(ObjectStorage, FakeStorage()),
        generate=generate,
    )
    assert seen_annotation_copies == [original_annotation]
    assert json.loads(content) == {
        str(MEMBER_ID): [
            {"start_timestamp": 0.0, "end_timestamp": 5.0, "description": "(1) 取料"},
            {"start_timestamp": 5.0, "end_timestamp": 10.0, "description": "(2) 安装"},
        ]
    }
    artifact = complete_artifact(
        target=artifact_target,
        object_key="training-datasets/artifact/annotation.json",
        artifact_sha256=str(manifest["artifact_sha256"]),
        artifact_size=len(content),
        manifest=manifest,
        now=NOW + timedelta(seconds=5),
        datasets=cast(UsageDatasetRepository, datasets),
    )
    assert artifact.status is ArtifactStatus.AVAILABLE
    assert artifact.manifest["input_digest"] == check.input_digest
    replay = request_artifact(
        dataset_id=DATASET_ID,
        check_id=check.id,
        caller=caller(Permission.DATASET_EDIT),
        now=NOW + timedelta(seconds=6),
        datasets=cast(UsageDatasetRepository, datasets),
        jobs=cast(UsageJobQueue, jobs),
    )
    assert replay.artifact.id == artifact.id
    assert len(datasets.artifacts) == 1


VALIDATION_DATASET_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f331")
VALIDATION_MEMBER_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f332")
VALIDATION_SUBMISSION_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f333")
VALIDATION_EXECUTION_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f334")


def _validation_datasets() -> FakeUsageDatasets:
    datasets = FakeUsageDatasets()
    datasets.dataset = replace(datasets.dataset, id=VALIDATION_DATASET_ID, name="验证数据集")
    datasets.member = replace(
        datasets.member, id=VALIDATION_MEMBER_ID, dataset_id=VALIDATION_DATASET_ID
    )
    datasets.submission = replace(
        datasets.submission,
        id=VALIDATION_SUBMISSION_ID,
        dataset_id=VALIDATION_DATASET_ID,
        member_id=VALIDATION_MEMBER_ID,
    )
    datasets.execution = replace(
        datasets.execution, id=VALIDATION_EXECUTION_ID, submission_id=VALIDATION_SUBMISSION_ID
    )
    datasets.action_list = replace(datasets.action_list, dataset_id=VALIDATION_DATASET_ID)
    return datasets


def _passing_ddm_artifact(
    datasets: FakeUsageDatasets, jobs: FakeUsageJobs
) -> tuple[DatasetArtifact, bytes]:
    source_digest = hashlib.sha256(b"a" * 100).hexdigest()
    datasets.member = replace(datasets.member, actual_sha256=source_digest)
    datasets.submission = replace(datasets.submission, source_sha256=source_digest)
    requested = request_usage_check(
        dataset_id=datasets.dataset.id,
        kind=UsageKind.DDM,
        candidate_id=None,
        caller=caller(Permission.DATASET_EDIT),
        now=NOW,
        datasets=cast(UsageDatasetRepository, datasets),
        jobs=cast(UsageJobQueue, jobs),
    )
    target = begin_usage_check(
        job=requested.job,
        datasets=cast(UsageDatasetRepository, datasets),
        now=NOW + timedelta(seconds=1),
    )
    assert target is not None
    validation = run_usage_check(
        target=target,
        storage=cast(ObjectStorage, FakeStorage()),
        media_probe=FakeMediaProbe(),
        annotation_volume=FakeAnnotationVolume(),
        ddm_reader=RecordingDdmReader(),
    )
    check = complete_usage_check(
        target=target,
        validation=validation,
        now=NOW + timedelta(seconds=2),
        datasets=cast(UsageDatasetRepository, datasets),
    )
    requested_artifact = request_artifact(
        dataset_id=datasets.dataset.id,
        check_id=check.id,
        caller=caller(Permission.DATASET_EDIT),
        now=NOW + timedelta(seconds=3),
        datasets=cast(UsageDatasetRepository, datasets),
        jobs=cast(UsageJobQueue, jobs),
    )
    artifact_target = begin_artifact_generation(
        job=requested_artifact.job,
        datasets=cast(UsageDatasetRepository, datasets),
        now=NOW + timedelta(seconds=4),
    )
    assert artifact_target is not None

    def generate(_workspace: Path, _output_filename: str) -> bytes:
        return json.dumps(
            {
                str(datasets.member.id): [
                    {
                        "action": 1,
                        "description": "(1) 取料",
                        "start_timestamp": 0.0,
                        "end_timestamp": 5.0,
                    },
                    {
                        "action": 2,
                        "description": "(2) 安装",
                        "start_timestamp": 5.0,
                        "end_timestamp": 10.0,
                    },
                ]
            },
            ensure_ascii=False,
        ).encode()

    content, manifest = render_ddm_artifact_with_base(
        artifact_target,
        storage=cast(ObjectStorage, FakeStorage()),
        generate=generate,
    )
    artifact = complete_artifact(
        target=artifact_target,
        object_key=f"training-datasets/{datasets.member.id}/artifact/annotation.json",
        artifact_sha256=str(manifest["artifact_sha256"]),
        artifact_size=len(content),
        manifest=manifest,
        now=NOW + timedelta(seconds=5),
        datasets=cast(UsageDatasetRepository, datasets),
    )
    return artifact, content


class TwoSetDatasets:
    """按数据集/成员身份路由到单个 FakeUsageDatasets 的双集仓储。"""

    def __init__(self, *sets: FakeUsageDatasets) -> None:
        self._sets = tuple(sets)

    def _for_dataset(self, dataset_id: UUID) -> FakeUsageDatasets | None:
        return next((item for item in self._sets if item.dataset.id == dataset_id), None)

    def _for_member(self, member_id: UUID) -> FakeUsageDatasets | None:
        return next((item for item in self._sets if item.member.id == member_id), None)

    def _for_check(self, check_id: UUID) -> FakeUsageDatasets | None:
        return next((item for item in self._sets if check_id in item.checks), None)

    def _for_artifact(self, artifact_id: UUID) -> FakeUsageDatasets | None:
        return next((item for item in self._sets if artifact_id in item.artifacts), None)

    def dataset_by_id(self, dataset_id: UUID) -> TrainingDataset | None:
        owner = self._for_dataset(dataset_id)
        return owner.dataset if owner is not None else None

    def list_members(self, *, dataset_id: UUID) -> list[DatasetMember]:
        owner = self._for_dataset(dataset_id)
        return [owner.member] if owner is not None else []

    def member_by_id(self, member_id: UUID) -> DatasetMember | None:
        owner = self._for_member(member_id)
        return owner.member if owner is not None else None

    def latest_action_list(self, dataset_id: UUID) -> ActionListRevision | None:
        owner = self._for_dataset(dataset_id)
        return owner.action_list if owner is not None else None

    def latest_annotation_submission(self, *, member_id: UUID) -> AnnotationSubmission | None:
        owner = self._for_member(member_id)
        return owner.submission if owner is not None else None

    def latest_annotation_execution(self, submission_id: UUID) -> AnnotationExecution | None:
        return next(
            (item.execution for item in self._sets if item.submission.id == submission_id), None
        )

    def usage_check_by_id(self, check_id: UUID) -> UsageCheck | None:
        owner = self._for_check(check_id)
        return owner.checks.get(check_id) if owner is not None else None

    def artifact_by_id(self, artifact_id: UUID) -> DatasetArtifact | None:
        owner = self._for_artifact(artifact_id)
        return owner.artifacts.get(artifact_id) if owner is not None else None

    def add_ddm_training_command(self, value: DdmTrainingCommand) -> bool:
        owner = self._for_dataset(value.dataset_id)
        return owner.add_ddm_training_command(value) if owner is not None else False

    def ddm_training_command_by_idempotency(
        self, *, dataset_id: UUID, idempotency_key: str
    ) -> DdmTrainingCommand | None:
        owner = self._for_dataset(dataset_id)
        if owner is None:
            return None
        return owner.ddm_training_command_by_idempotency(
            dataset_id=dataset_id, idempotency_key=idempotency_key
        )

    def ddm_training_command_by_id(self, command_id: UUID) -> DdmTrainingCommand | None:
        for owner in self._sets:
            value = owner.ddm_training_command_by_id(command_id)
            if value is not None:
                return value
        return None

    def save_ddm_training_command(
        self, value: DdmTrainingCommand, *, expected_updated_at: datetime
    ) -> bool:
        owner = self._for_dataset(value.dataset_id)
        if owner is None:
            return False
        return owner.save_ddm_training_command(value, expected_updated_at=expected_updated_at)


def _frozen_selection(datasets: FakeUsageDatasets, artifact: DatasetArtifact) -> DdmInputSelection:
    return DdmInputSelection(
        dataset_id=datasets.dataset.id,
        usage_check_id=artifact.usage_check_id,
        artifact_id=artifact.id,
    )


def _seeded_storage(root: Path, objects: Mapping[str, bytes]) -> LocalFileObjectStorage:
    storage = LocalFileObjectStorage(root)
    for object_key, content in objects.items():
        with storage.writing(object_key=object_key) as sink:
            sink.write(content)
    return storage


def _artifact_key(artifact: DatasetArtifact) -> str:
    assert artifact.object_key is not None
    return artifact.object_key


def _two_frozen_sets() -> tuple[FakeUsageDatasets, FakeUsageDatasets, FrozenDdmInput, bytes, bytes]:
    training = FakeUsageDatasets()
    validation = _validation_datasets()
    jobs = FakeUsageJobs()
    training_artifact, training_content = _passing_ddm_artifact(training, jobs)
    validation_artifact, validation_content = _passing_ddm_artifact(validation, jobs)
    datasets = TwoSetDatasets(training, validation)
    frozen = freeze_ddm_input(
        training=_frozen_selection(training, training_artifact),
        validation=_frozen_selection(validation, validation_artifact),
        caller=caller(Permission.DATASET_EDIT),
        datasets=cast(UsageDatasetRepository, datasets),
    )
    return training, validation, frozen, training_content, validation_content


def test_freeze_ddm_input_freezes_two_independent_sets() -> None:
    training = FakeUsageDatasets()
    validation = _validation_datasets()
    jobs = FakeUsageJobs()
    training_artifact, _ = _passing_ddm_artifact(training, jobs)
    validation_artifact, _ = _passing_ddm_artifact(validation, jobs)
    datasets = TwoSetDatasets(training, validation)

    frozen = freeze_ddm_input(
        training=_frozen_selection(training, training_artifact),
        validation=_frozen_selection(validation, validation_artifact),
        caller=caller(Permission.DATASET_EDIT),
        datasets=cast(UsageDatasetRepository, datasets),
    )

    assert frozen.training.dataset_id == training.dataset.id
    assert frozen.validation.dataset_id == validation.dataset.id
    assert frozen.training.artifact_id == training_artifact.id
    assert frozen.validation.artifact_id == validation_artifact.id
    assert frozen.training.input_digest == training_artifact.input_digest
    assert frozen.training.base_commit == BASE_COMMIT
    assert frozen.training.contract_version == DDM_CONTRACT_VERSION
    assert frozen.training.consumer_parameters == dict(DDM_CONSUMER_PARAMETERS)
    assert frozen.training.artifact_object_key == training_artifact.object_key
    assert frozen.training.artifact_sha256 == training_artifact.artifact_sha256
    assert len(frozen.training.members) == 1
    assert frozen.training.members[0].member_id == training.member.id
    assert frozen.training.members[0].annotation_bytes == FakeAnnotationVolume().annotation_bytes


def test_freeze_ddm_input_exposes_immutable_consumer_parameters() -> None:
    training = FakeUsageDatasets()
    validation = _validation_datasets()
    jobs = FakeUsageJobs()
    training_artifact, _ = _passing_ddm_artifact(training, jobs)
    validation_artifact, _ = _passing_ddm_artifact(validation, jobs)
    datasets = TwoSetDatasets(training, validation)

    frozen = freeze_ddm_input(
        training=_frozen_selection(training, training_artifact),
        validation=_frozen_selection(validation, validation_artifact),
        caller=caller(Permission.DATASET_EDIT),
        datasets=cast(UsageDatasetRepository, datasets),
    )

    assert dict(frozen.training.consumer_parameters) == dict(DDM_CONSUMER_PARAMETERS)
    with pytest.raises(TypeError):
        cast(dict[str, object], frozen.training.consumer_parameters)["mode"] = "eval"


def test_freeze_ddm_input_requires_dataset_edit_permission() -> None:
    training = FakeUsageDatasets()
    validation = _validation_datasets()
    jobs = FakeUsageJobs()
    training_artifact, _ = _passing_ddm_artifact(training, jobs)
    validation_artifact, _ = _passing_ddm_artifact(validation, jobs)
    datasets = TwoSetDatasets(training, validation)

    with pytest.raises(AuthorizationRefusedError):
        freeze_ddm_input(
            training=_frozen_selection(training, training_artifact),
            validation=_frozen_selection(validation, validation_artifact),
            caller=caller(Permission.DATASET_VIEW),
            datasets=cast(UsageDatasetRepository, datasets),
        )


def test_freeze_ddm_input_rejects_the_same_dataset_for_both_sets() -> None:
    training = FakeUsageDatasets()
    jobs = FakeUsageJobs()
    artifact, _ = _passing_ddm_artifact(training, jobs)
    datasets = TwoSetDatasets(training)

    with pytest.raises(DatasetRefusedError) as refused:
        freeze_ddm_input(
            training=_frozen_selection(training, artifact),
            validation=_frozen_selection(training, artifact),
            caller=caller(Permission.DATASET_EDIT),
            datasets=cast(UsageDatasetRepository, datasets),
        )
    assert refused.value.code is DatasetRefusalCode.USAGE_STATE_CONFLICT


def test_freeze_ddm_input_rejects_an_artifact_from_another_check() -> None:
    training = FakeUsageDatasets()
    validation = _validation_datasets()
    jobs = FakeUsageJobs()
    training_artifact, _ = _passing_ddm_artifact(training, jobs)
    validation_artifact, _ = _passing_ddm_artifact(validation, jobs)
    datasets = TwoSetDatasets(training, validation)

    with pytest.raises(DatasetRefusedError):
        freeze_ddm_input(
            training=_frozen_selection(training, training_artifact),
            validation=_frozen_selection(validation, training_artifact),
            caller=caller(Permission.DATASET_EDIT),
            datasets=cast(UsageDatasetRepository, datasets),
        )
    assert validation_artifact.id != training_artifact.id


def test_freeze_ddm_input_rejects_a_changed_input() -> None:
    training = FakeUsageDatasets()
    validation = _validation_datasets()
    jobs = FakeUsageJobs()
    training_artifact, _ = _passing_ddm_artifact(training, jobs)
    validation_artifact, _ = _passing_ddm_artifact(validation, jobs)
    datasets = TwoSetDatasets(training, validation)
    training.member = replace(training.member, object_key="training-datasets/changed.mp4")

    with pytest.raises(DatasetRefusedError) as refused:
        freeze_ddm_input(
            training=_frozen_selection(training, training_artifact),
            validation=_frozen_selection(validation, validation_artifact),
            caller=caller(Permission.DATASET_EDIT),
            datasets=cast(UsageDatasetRepository, datasets),
        )
    assert refused.value.code is DatasetRefusalCode.USAGE_STATE_CONFLICT


def test_freeze_ddm_input_rejects_an_inapplicable_base() -> None:
    training = FakeUsageDatasets()
    validation = _validation_datasets()
    jobs = FakeUsageJobs()
    training_artifact, _ = _passing_ddm_artifact(training, jobs)
    validation_artifact, _ = _passing_ddm_artifact(validation, jobs)
    datasets = TwoSetDatasets(training, validation)
    stored = training.checks[training_artifact.usage_check_id]
    training.checks[training_artifact.usage_check_id] = replace(
        stored, base_commit="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    )

    with pytest.raises(DatasetRefusedError) as refused:
        freeze_ddm_input(
            training=_frozen_selection(training, training_artifact),
            validation=_frozen_selection(validation, validation_artifact),
            caller=caller(Permission.DATASET_EDIT),
            datasets=cast(UsageDatasetRepository, datasets),
        )
    assert refused.value.code is DatasetRefusalCode.USAGE_STATE_CONFLICT


def test_materialize_ddm_input_writes_vendor_layout_and_artifact_bytes(tmp_path: Path) -> None:
    training, validation, frozen, training_content, validation_content = _two_frozen_sets()
    training_artifact = next(iter(training.artifacts.values()))
    validation_artifact = next(iter(validation.artifacts.values()))
    storage = _seeded_storage(
        tmp_path / "objects",
        {
            "datasets/source-a.mp4": b"a" * 100,
            _artifact_key(training_artifact): training_content,
            _artifact_key(validation_artifact): validation_content,
        },
    )

    materialized = materialize_ddm_input(frozen=frozen, storage=storage, root=tmp_path / "input")

    assert materialized.training_directory != materialized.validation_directory
    train = materialized.training_directory
    assert (train / f"{training.member.id}.mp4").read_bytes() == b"a" * 100
    assert (
        train / str(training.member.id) / f"{training.member.id}_annotation.json"
    ).read_bytes() == FakeAnnotationVolume().annotation_bytes
    assert (train / "annotation.json").read_bytes() == training_content
    validate = materialized.validation_directory
    assert (validate / "annotation.json").read_bytes() == validation_content
    assert (validate / f"{validation.member.id}.mp4").read_bytes() == b"a" * 100


def test_materialize_ddm_input_does_not_change_an_in_flight_copy(tmp_path: Path) -> None:
    training, validation, frozen, training_content, validation_content = _two_frozen_sets()
    training_artifact = next(iter(training.artifacts.values()))
    validation_artifact = next(iter(validation.artifacts.values()))
    storage = _seeded_storage(
        tmp_path / "objects",
        {
            "datasets/source-a.mp4": b"a" * 100,
            _artifact_key(training_artifact): training_content,
            _artifact_key(validation_artifact): validation_content,
        },
    )

    first = materialize_ddm_input(frozen=frozen, storage=storage, root=tmp_path / "input")
    first_bytes = (first.training_directory / "annotation.json").read_bytes()
    # 另一命令用同一冻结身份准备时被拒绝，不改动在途副本。
    with pytest.raises(DatasetRefusedError):
        materialize_ddm_input(frozen=frozen, storage=storage, root=tmp_path / "input")

    assert (first.training_directory / "annotation.json").read_bytes() == first_bytes


def test_materialize_ddm_input_fails_closed_without_leaving_partial(tmp_path: Path) -> None:
    training, validation, frozen, training_content, _validation_content = _two_frozen_sets()
    training_artifact = next(iter(training.artifacts.values()))
    validation_artifact = next(iter(validation.artifacts.values()))
    storage = _seeded_storage(
        tmp_path / "objects",
        {
            "datasets/source-a.mp4": b"a" * 100,
            _artifact_key(training_artifact): training_content,
            _artifact_key(validation_artifact): b"wrong-bytes",
        },
    )

    with pytest.raises(DatasetRefusedError):
        materialize_ddm_input(frozen=frozen, storage=storage, root=tmp_path / "input")

    assert not (tmp_path / "input" / str(frozen.input_id)).exists()
    # 历史 available 制品与原媒体保持可读。
    assert (
        tmp_path / "objects" / _artifact_key(validation_artifact)
    ).read_bytes() == b"wrong-bytes"
    assert (tmp_path / "objects" / "datasets/source-a.mp4").read_bytes() == b"a" * 100


def _materialized_two_sets(
    tmp_path: Path,
) -> tuple[FakeUsageDatasets, FakeUsageDatasets, FrozenDdmInput, MaterializedDdmInput]:
    training, validation, frozen, training_content, validation_content = _two_frozen_sets()
    training_artifact = next(iter(training.artifacts.values()))
    validation_artifact = next(iter(validation.artifacts.values()))
    storage = _seeded_storage(
        tmp_path / "objects",
        {
            "datasets/source-a.mp4": b"a" * 100,
            _artifact_key(training_artifact): training_content,
            _artifact_key(validation_artifact): validation_content,
        },
    )
    materialized = materialize_ddm_input(frozen=frozen, storage=storage, root=tmp_path / "input")
    return training, validation, frozen, materialized


def _ddm_role_parameters() -> dict[str, Mapping[str, object]]:
    """实际调用者应显式供给的按角色 reader 参数（与冻结值一致）。"""
    return {
        "training_consumer_parameters": dict(DDM_CONSUMER_PARAMETERS),
        "validation_consumer_parameters": dict(DDM_VALIDATION_CONSUMER_PARAMETERS),
    }


def test_verify_ddm_input_consumption_runs_real_base_aggregation(tmp_path: Path) -> None:
    training, validation, frozen, materialized = _materialized_two_sets(tmp_path)

    consumption = verify_ddm_input_consumption(
        frozen=frozen,
        materialized=materialized,
        generate=NvidiaDdmAnnotationGenerator().generate,
        **_ddm_role_parameters(),
    )

    assert isinstance(consumption, DdmInputConsumption)
    assert consumption.input_id == frozen.input_id
    assert consumption.training_annotation_filename == "ddm_train_annotation.json"
    assert consumption.validation_annotation_filename == "ddm_val_annotation.json"
    # 冻结值来自固定 base 的实际验证 reader 参数。
    assert dict(frozen.validation_consumer_parameters) == dict(DDM_VALIDATION_CONSUMER_PARAMETERS)
    # 真实基座聚合器写出的训练/验证消费文件各自只覆盖本集合的冻结成员。
    training_document = json.loads(
        (materialized.training_directory / "ddm_train_annotation.json").read_bytes()
    )
    validation_document = json.loads(
        (materialized.validation_directory / "ddm_val_annotation.json").read_bytes()
    )
    assert set(training_document) == {str(training.member.id)}
    assert set(validation_document) == {str(validation.member.id)}
    assert all(isinstance(item, ConsumedDdmFile) for item in consumption.consumed_files)
    assert {
        (item.role, item.filename, item.source_object_key) for item in consumption.consumed_files
    } == {
        ("training", f"{training.member.id}.mp4", "datasets/source-a.mp4"),
        ("validation", f"{validation.member.id}.mp4", "datasets/source-a.mp4"),
    }
    # 登记的是真实写出文件的摘要，不是原制品摘要。
    assert (
        consumption.training_annotation_sha256
        == hashlib.sha256(
            (materialized.training_directory / "ddm_train_annotation.json").read_bytes()
        ).hexdigest()
    )
    assert (
        consumption.validation_annotation_sha256
        == hashlib.sha256(
            (materialized.validation_directory / "ddm_val_annotation.json").read_bytes()
        ).hexdigest()
    )
    record = json.loads((materialized.root / "ddm_input_consumption.json").read_bytes())
    assert record["training_annotation_sha256"] == consumption.training_annotation_sha256
    assert record["validation_annotation_sha256"] == consumption.validation_annotation_sha256
    assert record["training_consumer_parameters"] == dict(DDM_CONSUMER_PARAMETERS)
    assert record["validation_consumer_parameters"] == dict(DDM_VALIDATION_CONSUMER_PARAMETERS)
    assert {item["filename"] for item in record["consumed_files"]} == {
        f"{training.member.id}.mp4",
        f"{validation.member.id}.mp4",
    }


def test_verify_ddm_input_consumption_rejects_changed_segment_content(tmp_path: Path) -> None:
    _training, _validation, frozen, materialized = _materialized_two_sets(tmp_path)
    member = frozen.training.members[0]
    kept_ids_but_changed = [
        {
            "action": 1,
            "description": "(1) 取料",
            "start_timestamp": 0.0,
            "end_timestamp": 3.0,
        }
    ]
    annotation_path = (
        materialized.training_directory
        / str(member.member_id)
        / f"{member.member_id}_annotation.json"
    )
    annotation_path.write_text(json.dumps(kept_ids_but_changed), encoding="utf-8")

    with pytest.raises(DatasetRefusedError) as refused:
        verify_ddm_input_consumption(
            frozen=frozen,
            materialized=materialized,
            generate=NvidiaDdmAnnotationGenerator().generate,
            **_ddm_role_parameters(),
        )

    assert refused.value.code is DatasetRefusalCode.ARTIFACT_UNAVAILABLE


def test_verify_ddm_input_consumption_rejects_a_video_the_base_would_skip(tmp_path: Path) -> None:
    _training, _validation, frozen, materialized = _materialized_two_sets(tmp_path)
    training_artifact_key = frozen.training.artifact_object_key
    before = (tmp_path / "objects" / training_artifact_key).read_bytes()
    (materialized.training_directory / f"{frozen.training.members[0].member_id}.mp4").unlink()

    with pytest.raises(DatasetRefusedError) as refused:
        verify_ddm_input_consumption(
            frozen=frozen,
            materialized=materialized,
            generate=NvidiaDdmAnnotationGenerator().generate,
            **_ddm_role_parameters(),
        )

    assert refused.value.code is DatasetRefusalCode.ARTIFACT_UNAVAILABLE
    # 失败不重写不可变制品。
    assert (tmp_path / "objects" / training_artifact_key).read_bytes() == before


def test_verify_ddm_input_consumption_rejects_a_shared_validation_copy(tmp_path: Path) -> None:
    _training, _validation, frozen, materialized = _materialized_two_sets(tmp_path)

    with pytest.raises(DatasetRefusedError) as refused:
        verify_ddm_input_consumption(
            frozen=frozen,
            materialized=replace(
                materialized, validation_directory=materialized.training_directory
            ),
            generate=NvidiaDdmAnnotationGenerator().generate,
            **_ddm_role_parameters(),
        )

    assert refused.value.code is DatasetRefusalCode.USAGE_STATE_CONFLICT


def test_verify_ddm_input_consumption_rejects_changed_consumer_parameters(tmp_path: Path) -> None:
    _training, _validation, frozen, materialized = _materialized_two_sets(tmp_path)
    changed = replace(
        frozen,
        training=replace(
            frozen.training,
            consumer_parameters={**DDM_CONSUMER_PARAMETERS, "mode": "eval"},
        ),
    )

    with pytest.raises(DatasetRefusedError) as refused:
        verify_ddm_input_consumption(
            frozen=changed,
            materialized=materialized,
            generate=NvidiaDdmAnnotationGenerator().generate,
            **_ddm_role_parameters(),
        )

    assert refused.value.code is DatasetRefusalCode.USAGE_STATE_CONFLICT


def test_verify_ddm_input_consumption_refuses_to_overwrite_a_successful_root(
    tmp_path: Path,
) -> None:
    _training, _validation, frozen, materialized = _materialized_two_sets(tmp_path)
    real_generate = NvidiaDdmAnnotationGenerator().generate
    calls: list[tuple[Path, str]] = []

    def counting_generate(directory: Path, filename: str) -> bytes:
        calls.append((directory, filename))
        return real_generate(directory, filename)

    first = verify_ddm_input_consumption(
        frozen=frozen,
        materialized=materialized,
        generate=counting_generate,
        **_ddm_role_parameters(),
    )
    calls_after_first = len(calls)
    annotation_path = materialized.training_directory / "ddm_train_annotation.json"
    saved_annotation = annotation_path.read_bytes()
    record_path = materialized.root / "ddm_input_consumption.json"
    saved_record = record_path.read_bytes()
    training_artifact_key = frozen.training.artifact_object_key
    saved_artifact = (tmp_path / "objects" / training_artifact_key).read_bytes()
    saved_sha = json.loads(saved_record)["training_annotation_sha256"]

    # 篡改成员标注：若再次生成，会写出一份与旧成功记录 SHA 不符的新消费文件。
    member = frozen.training.members[0]
    annotation_directory = materialized.training_directory / str(member.member_id)
    (annotation_directory / f"{member.member_id}_annotation.json").write_text(
        json.dumps(
            [
                {
                    "action": 1,
                    "description": "(1) 取料",
                    "start_timestamp": 0.0,
                    "end_timestamp": 3.0,
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(DatasetRefusedError) as refused:
        verify_ddm_input_consumption(
            frozen=frozen,
            materialized=materialized,
            generate=counting_generate,
            **_ddm_role_parameters(),
        )

    assert refused.value.code is DatasetRefusalCode.USAGE_STATE_CONFLICT
    # 任何原聚合器写入之前就拒绝，历史成功产物、记录与不可变制品均未被覆盖。
    assert len(calls) == calls_after_first
    assert annotation_path.read_bytes() == saved_annotation
    assert record_path.read_bytes() == saved_record
    assert (tmp_path / "objects" / training_artifact_key).read_bytes() == saved_artifact
    assert saved_sha == first.training_annotation_sha256


def test_verify_ddm_input_consumption_rejects_changed_validation_temporal_stride(
    tmp_path: Path,
) -> None:
    _training, _validation, frozen, materialized = _materialized_two_sets(tmp_path)
    parameters = _ddm_role_parameters()
    parameters["validation_consumer_parameters"] = {
        **parameters["validation_consumer_parameters"],
        "temporal_stride": 2,
    }

    with pytest.raises(DatasetRefusedError) as refused:
        verify_ddm_input_consumption(
            frozen=frozen,
            materialized=materialized,
            generate=NvidiaDdmAnnotationGenerator().generate,
            **parameters,
        )

    assert refused.value.code is DatasetRefusalCode.USAGE_STATE_CONFLICT
    # 参数不符时不得落下成功消费记录。
    assert not (materialized.root / "ddm_input_consumption.json").exists()


def test_verify_ddm_input_consumption_rejects_changed_training_parameter(
    tmp_path: Path,
) -> None:
    _training, _validation, frozen, materialized = _materialized_two_sets(tmp_path)
    parameters = _ddm_role_parameters()
    parameters["training_consumer_parameters"] = {
        **parameters["training_consumer_parameters"],
        "frames_per_side": 3,
    }

    with pytest.raises(DatasetRefusedError) as refused:
        verify_ddm_input_consumption(
            frozen=frozen,
            materialized=materialized,
            generate=NvidiaDdmAnnotationGenerator().generate,
            **parameters,
        )

    assert refused.value.code is DatasetRefusalCode.USAGE_STATE_CONFLICT
    assert not (materialized.root / "ddm_input_consumption.json").exists()


def _second_validation_datasets() -> FakeUsageDatasets:
    """构造第二套内容不同的验证集，用于证明相同幂等身份下的内容冲突。"""
    datasets = _validation_datasets()
    dataset_id = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f341")
    member_id = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f342")
    submission_id = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f343")
    execution_id = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f344")
    datasets.dataset = replace(datasets.dataset, id=dataset_id, name="第二验证数据集")
    datasets.member = replace(datasets.member, id=member_id, dataset_id=dataset_id)
    datasets.submission = replace(
        datasets.submission,
        id=submission_id,
        dataset_id=dataset_id,
        member_id=member_id,
    )
    datasets.execution = replace(datasets.execution, id=execution_id, submission_id=submission_id)
    datasets.action_list = replace(datasets.action_list, dataset_id=dataset_id)
    return datasets


def _ddm_command_fixture() -> tuple[
    TwoSetDatasets, FakeUsageDatasets, FakeUsageDatasets, DdmInputSelection, DdmInputSelection
]:
    training = FakeUsageDatasets()
    validation = _validation_datasets()
    jobs = FakeUsageJobs()
    training_artifact, _ = _passing_ddm_artifact(training, jobs)
    validation_artifact, _ = _passing_ddm_artifact(validation, jobs)
    datasets = TwoSetDatasets(training, validation)
    return (
        datasets,
        training,
        validation,
        _frozen_selection(training, training_artifact),
        _frozen_selection(validation, validation_artifact),
    )


def test_request_ddm_training_command_binds_frozen_inputs_to_pending_command_and_job() -> None:
    datasets, training, validation, training_selection, validation_selection = (
        _ddm_command_fixture()
    )
    jobs = FakeUsageJobs()

    result = request_ddm_training_command(
        training=training_selection,
        validation=validation_selection,
        idempotency_key="ddm-1",
        caller=caller(Permission.DATASET_EDIT),
        now=NOW,
        datasets=cast(DdmTrainingCommandRepository, datasets),
        jobs=cast(DdmTrainingJobQueue, jobs),
    )

    assert result.command.status is DdmTrainingCommandStatus.PENDING
    assert result.command.dataset_id == training.dataset.id
    assert result.command.idempotency_key == "ddm-1"
    assert result.command.job_id == result.job.id
    assert result.command.request_digest == request_digest(result.command.payload)
    assert result.job.job_type is JobType.DATASET_DDM_TRAINING
    assert result.job.status == JobStatus.PENDING
    payload = result.command.payload
    assert payload["training"]["dataset_id"] == str(training.dataset.id)
    assert payload["validation"]["dataset_id"] == str(validation.dataset.id)
    assert payload["training"]["consumer_parameters"] == dict(DDM_CONSUMER_PARAMETERS)
    assert payload["validation_consumer_parameters"] == dict(DDM_VALIDATION_CONSUMER_PARAMETERS)
    assert payload["training"]["members"][0]["source_sha256"] == training.member.actual_sha256
    assert training.ddm_commands[result.command.id] == result.command
    assert jobs.jobs[result.job.id] == result.job


def test_request_ddm_training_command_replay_same_content_returns_original_command() -> None:
    datasets, training, _validation, training_selection, validation_selection = (
        _ddm_command_fixture()
    )
    jobs = FakeUsageJobs()

    def submit() -> DdmTrainingCommandResult:
        return request_ddm_training_command(
            training=training_selection,
            validation=validation_selection,
            idempotency_key="ddm-1",
            caller=caller(Permission.DATASET_EDIT),
            now=NOW,
            datasets=cast(DdmTrainingCommandRepository, datasets),
            jobs=cast(DdmTrainingJobQueue, jobs),
        )

    first = submit()
    second = submit()

    # 相同幂等身份与选择在冻结前直接重放，不产生第二次冻结，也不改变原输入身份。
    assert second.command == first.command
    assert second.job == first.job
    assert second.command.payload["input_id"] == first.command.payload["input_id"]
    assert len(training.ddm_commands) == 1
    assert len(jobs.jobs) == 1


def test_request_ddm_training_command_changed_content_conflicts() -> None:
    datasets, training, _validation, training_selection, validation_selection = (
        _ddm_command_fixture()
    )
    jobs = FakeUsageJobs()
    request_ddm_training_command(
        training=training_selection,
        validation=validation_selection,
        idempotency_key="ddm-1",
        caller=caller(Permission.DATASET_EDIT),
        now=NOW,
        datasets=cast(DdmTrainingCommandRepository, datasets),
        jobs=cast(DdmTrainingJobQueue, jobs),
    )
    other_validation = _second_validation_datasets()
    other_artifact, _ = _passing_ddm_artifact(other_validation, jobs)
    other_selection = _frozen_selection(other_validation, other_artifact)
    changed_repository = TwoSetDatasets(training, other_validation)

    with pytest.raises(DatasetRefusedError) as refused:
        request_ddm_training_command(
            training=training_selection,
            validation=other_selection,
            idempotency_key="ddm-1",
            caller=caller(Permission.DATASET_EDIT),
            now=NOW,
            datasets=cast(DdmTrainingCommandRepository, changed_repository),
            jobs=cast(DdmTrainingJobQueue, jobs),
        )

    assert refused.value.code is DatasetRefusalCode.IDEMPOTENCY_CONFLICT


def test_request_ddm_training_command_requires_edit_permission() -> None:
    datasets, _training, _validation, training_selection, validation_selection = (
        _ddm_command_fixture()
    )

    with pytest.raises(AuthorizationRefusedError):
        request_ddm_training_command(
            training=training_selection,
            validation=validation_selection,
            idempotency_key="ddm-1",
            caller=caller(Permission.DATASET_VIEW),
            now=NOW,
            datasets=cast(DdmTrainingCommandRepository, datasets),
            jobs=cast(DdmTrainingJobQueue, FakeUsageJobs()),
        )


def test_request_ddm_training_command_rejects_a_blank_idempotency_key() -> None:
    datasets, _training, _validation, training_selection, validation_selection = (
        _ddm_command_fixture()
    )

    with pytest.raises(DatasetRefusedError) as refused:
        request_ddm_training_command(
            training=training_selection,
            validation=validation_selection,
            idempotency_key="   ",
            caller=caller(Permission.DATASET_EDIT),
            now=NOW,
            datasets=cast(DdmTrainingCommandRepository, datasets),
            jobs=cast(DdmTrainingJobQueue, FakeUsageJobs()),
        )

    assert refused.value.code is DatasetRefusalCode.IDEMPOTENCY_CONFLICT
