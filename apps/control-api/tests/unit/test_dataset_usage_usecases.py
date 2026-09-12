"""用途检查用例的数据库、任务和对象存储 seam。"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO, cast
from uuid import UUID

import pytest

from factory_sop.auth.authorization import Caller
from factory_sop.auth.model import User, UserStatus
from factory_sop.auth.permissions import Permission
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
from factory_sop.dataset.repository import UsageDatasetRepository
from factory_sop.dataset.storage import ObjectStorage
from factory_sop.dataset.usage import UsageIssue, UsageValidationResult
from factory_sop.dataset.usecases.usage import (
    BASE_COMMIT,
    DDM_CONSUMER_PARAMETERS,
    DDM_CONTRACT_VERSION,
    VLM_CONTRACT_VERSION,
    apply_usage_check_currentness,
    begin_artifact_generation,
    begin_usage_check,
    complete_artifact,
    complete_usage_check,
    fail_artifact,
    register_vlm_candidate,
    render_ddm_artifact_with_base,
    request_artifact,
    request_usage_check,
    run_usage_check,
    usage_check_is_current,
)
from factory_sop.identifiers import new_id
from factory_sop.job.api import ApplicationJob, JobStatus, JobType, UsageJobQueue

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
        object_version_id="version-1",
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
        source_object_version_id="version-1",
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

    def dataset_by_id(self, dataset_id: UUID) -> TrainingDataset | None:
        return self.dataset if dataset_id == DATASET_ID else None

    def list_members(self, *, dataset_id: UUID) -> list[DatasetMember]:
        return [self.member] if dataset_id == DATASET_ID else []

    def member_by_id(self, member_id: UUID) -> DatasetMember | None:
        return self.member if member_id == MEMBER_ID else None

    def latest_action_list(self, dataset_id: UUID) -> ActionListRevision | None:
        return self.action_list if dataset_id == DATASET_ID else None

    def action_list_by_revision(
        self, *, dataset_id: UUID, revision: int
    ) -> ActionListRevision | None:
        return (
            self.action_list
            if dataset_id == DATASET_ID and revision == self.action_list.revision
            else None
        )

    def latest_annotation_submission(self, *, member_id: UUID) -> AnnotationSubmission | None:
        return self.submission if member_id == MEMBER_ID else None

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
    def stat(self, *, object_key: str) -> ObjectStat:
        assert object_key == "datasets/source-a.mp4"
        return ObjectStat(size=100, version_id="version-1")

    def create_upload(self, **kwargs: object) -> object:
        raise AssertionError(kwargs)

    def download_to(
        self, *, object_key: str, destination: BinaryIO, version_id: str | None = None
    ) -> None:
        assert object_key == "datasets/source-a.mp4"
        assert version_id == "version-1"
        destination.write(b"a" * 100)

    def finalize_upload(self, *, object_key: str, source: BinaryIO, size: int) -> ObjectStat:
        return ObjectStat(size=size, version_id="artifact-1")

    def delete(self, *, object_key: str) -> None:
        del object_key


class TracingStorage(FakeStorage):
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def stat(self, *, object_key: str) -> ObjectStat:
        self.events.append("media")
        return super().stat(object_key=object_key)

    def download_to(
        self, *, object_key: str, destination: BinaryIO, version_id: str | None = None
    ) -> None:
        self.events.append("media")
        super().download_to(object_key=object_key, destination=destination, version_id=version_id)


class FakeMediaProbe:
    def probe(self, path: str) -> MediaMetadata:
        assert path.endswith(".mp4")
        return MediaMetadata(
            duration_seconds=10.0, codec="h264", container="mp4", fps=10.0, frame_count=100
        )


class ClipMediaProbe(FakeMediaProbe):
    def probe(self, path: str) -> MediaMetadata:
        return MediaMetadata(
            duration_seconds=5.0, codec="h264", container="mp4", fps=10.0, frame_count=50
        )


class FakeAnnotationVolume:
    def __init__(self) -> None:
        self.video_reads: list[tuple[str, str, str | None]] = []
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
        destination.write(b"a" * 100)


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
    def download_to(
        self, *, object_key: str, destination: BinaryIO, version_id: str | None = None
    ) -> None:
        assert object_key == "datasets/source-a.mp4"
        assert version_id == "version-1"
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
                "object_version_id": "version-1",
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
                "object_version_id": "version-1",
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
        media=(VlmMediaReference("line-a.mp4", MEMBER_ID, "version-1", "a" * 64),),
        expected_revision=0,
        caller=caller(Permission.DATASET_EDIT),
        now=NOW,
        datasets=cast(UsageDatasetRepository, datasets),
    )
    assert candidate.revision == 1


def test_vlm_candidate_revision_uses_if_match_and_binds_current_media() -> None:
    datasets = FakeUsageDatasets()
    media = VlmMediaReference("line-a.mp4", MEMBER_ID, "version-1", "a" * 64)
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
        source_object_version_id="version-1",
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
        source_object_version_id="version-1",
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
    media = VlmMediaReference(
        key="clip-key.mp4",
        member_id=MEMBER_ID,
        source_object_version_id="version-1",
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
        "对象存储暂时不可用",
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
            "detail": "对象存储暂时不可用",
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
