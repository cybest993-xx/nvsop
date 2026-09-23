"""训练数据集标注用例的公开 seam：动作版本、上下文和候选代次。"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, BinaryIO
from uuid import UUID

import pytest

from factory_sop.auth.authorization import AuthorizationRefusedError, Caller
from factory_sop.auth.model import User, UserStatus
from factory_sop.auth.permissions import Permission
from factory_sop.dataset.annotation import (
    AnnotationBackendExecutionError,
    AnnotationCleanupPendingError,
    PreparedAnnotationVideo,
)
from factory_sop.dataset.errors import DatasetFieldError
from factory_sop.dataset.media import MediaMetadata
from factory_sop.dataset.model import (
    ActionListRevision,
    AnnotationContext,
    AnnotationExecution,
    AnnotationMode,
    AnnotationSegment,
    AnnotationSubmission,
    DatasetMember,
    MemberStatus,
    ObjectStat,
    TrainingDataset,
    UploadAttempt,
)
from factory_sop.dataset.storage import ObjectStorage
from factory_sop.dataset.usecases.annotation import (
    AnnotationRefusedError,
    PreparedAnnotationCopy,
    begin_annotation_context_preparation,
    begin_annotation_execution,
    complete_annotation_context_preparation,
    complete_annotation_execution,
    create_annotation_context,
    decode_annotation_context_token,
    fail_annotation_context_preparation,
    prepare_annotation_execution_copy,
    record_annotation_context_cleanup_candidate,
    record_annotation_execution_cleanup_candidate,
    register_action_list,
    retry_annotation,
    save_annotation_execution_copy,
    submit_annotation,
)
from factory_sop.job.api import ApplicationJob, JobStatus, JobType

NOW = datetime(2026, 9, 11, 1, 0, tzinfo=UTC)
DATASET_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f101")
MEMBER_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f102")
ACTOR_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f103")
# 仅用于测试上下文签名，不是部署凭据。
SECRET = "annotation-context-secret"  # pragma: allowlist secret


@dataclass
class FakeAnnotationStore:
    datasets: dict[UUID, TrainingDataset] = field(default_factory=dict)
    members: dict[UUID, DatasetMember] = field(default_factory=dict)
    attempts: dict[UUID, UploadAttempt] = field(default_factory=dict)
    action_lists: dict[tuple[UUID, int], ActionListRevision] = field(default_factory=dict)
    contexts: dict[UUID, AnnotationContext] = field(default_factory=dict)
    submissions: dict[UUID, AnnotationSubmission] = field(default_factory=dict)
    executions: dict[UUID, AnnotationExecution] = field(default_factory=dict)

    def add_dataset(self, value: TrainingDataset) -> None:
        self.datasets[value.id] = value

    def dataset_by_id(self, dataset_id: UUID) -> TrainingDataset | None:
        return self.datasets.get(dataset_id)

    def page_datasets(self, *, page: int, page_size: int) -> tuple[list[TrainingDataset], int]:
        rows = list(self.datasets.values())
        start = (page - 1) * page_size
        return rows[start : start + page_size], len(rows)

    def add_member(self, value: DatasetMember) -> None:
        self.members[value.id] = value

    def member_by_id(self, member_id: UUID) -> DatasetMember | None:
        return self.members.get(member_id)

    def lock_annotation_member(self, member_id: UUID) -> DatasetMember | None:
        return self.members.get(member_id)

    def page_members(
        self, *, dataset_id: UUID, page: int, page_size: int
    ) -> tuple[list[DatasetMember], int]:
        rows = [value for value in self.members.values() if value.dataset_id == dataset_id]
        start = (page - 1) * page_size
        return rows[start : start + page_size], len(rows)

    def add_attempt(self, value: UploadAttempt) -> None:
        self.attempts[value.id] = value

    def attempt_by_id(self, attempt_id: UUID) -> UploadAttempt | None:
        return self.attempts.get(attempt_id)

    def attempt_by_idempotency(
        self, *, dataset_id: UUID, idempotency_key: str
    ) -> UploadAttempt | None:
        return next(
            (
                value
                for value in self.attempts.values()
                if value.dataset_id == dataset_id and value.idempotency_key == idempotency_key
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
        current = self.members.get(member.id)
        if current is None or current.current_attempt_id != expected_attempt_id:
            return False
        if expected_updated_at is not None and current.updated_at != expected_updated_at:
            return False
        self.members[member.id] = member
        return True

    def save_attempt(self, value: UploadAttempt) -> None:
        self.attempts[value.id] = value

    def latest_action_list(self, dataset_id: UUID) -> ActionListRevision | None:
        rows = [row for (owner, _), row in self.action_lists.items() if owner == dataset_id]
        return max(rows, key=lambda row: row.revision, default=None)

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
                row
                for row in self.submissions.values()
                if row.dataset_id == dataset_id and row.idempotency_key == idempotency_key
            ),
            None,
        )

    def annotation_submission_by_id(self, submission_id: UUID) -> AnnotationSubmission | None:
        return self.submissions.get(submission_id)

    def list_annotation_submissions(
        self, *, dataset_id: UUID, member_id: UUID
    ) -> list[AnnotationSubmission]:
        return sorted(
            (
                row
                for row in self.submissions.values()
                if row.dataset_id == dataset_id and row.member_id == member_id
            ),
            key=lambda row: row.created_at,
        )

    def add_annotation_submission(self, value: AnnotationSubmission) -> None:
        self.submissions[value.id] = value

    def annotation_execution_by_id(self, execution_id: UUID) -> AnnotationExecution | None:
        return self.executions.get(execution_id)

    def latest_annotation_execution(self, submission_id: UUID) -> AnnotationExecution | None:
        rows = [row for row in self.executions.values() if row.submission_id == submission_id]
        return max(rows, key=lambda row: row.generation, default=None)

    def list_annotation_executions(self, submission_id: UUID) -> list[AnnotationExecution]:
        return sorted(
            (row for row in self.executions.values() if row.submission_id == submission_id),
            key=lambda row: row.generation,
        )

    def add_annotation_execution(self, value: AnnotationExecution) -> None:
        self.executions[value.id] = value

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

    def annotation_context_preparation_exists(self, member_id: UUID, context_id: UUID) -> bool:
        context = self.contexts.get(context_id)
        return context is not None and context.member_id == member_id

    def annotation_execution_exists(self, member_id: UUID, execution_id: UUID) -> bool:
        submission = next(
            (
                row
                for row in self.submissions.values()
                if row.id == self.executions.get(execution_id, _MISSING).submission_id
            ),
            None,
        )
        return submission is not None and submission.member_id == member_id


_MISSING = type("Missing", (), {"submission_id": None})()


@dataclass
class FakeAnnotationCopyStorage(ObjectStorage):
    source: bytes

    def writing(self, *, object_key: str) -> AbstractContextManager[BinaryIO]:
        del object_key
        raise AssertionError("测试不应写入对象")

    def stat(self, *, object_key: str) -> ObjectStat:
        del object_key
        raise AssertionError("测试不应读取对象元数据")

    def download_to(self, *, object_key: str, destination: BinaryIO) -> None:
        assert object_key
        destination.write(self.source)

    def delete(self, *, object_key: str) -> None:
        del object_key


@dataclass
class FakeAnnotationCopyBackend:
    calls: int = 0
    discarded_data_ids: list[str] = field(default_factory=list)
    fail_download: bool = False
    fail_discard: bool = False

    def prepare_video(
        self,
        *,
        source: BinaryIO,
        filename: str,
        actions: Sequence[str],
    ) -> PreparedAnnotationVideo:
        assert source.read() == b"annotation source"
        assert filename == "line.mp4"
        assert list(actions) == ["(1)拿取工件", "(2)安装部件"]
        self.calls += 1
        return PreparedAnnotationVideo(
            data_id=f"base-dataset-{self.calls}",
            video_id=f"base-video-{self.calls}",
        )

    def discard_prepared_video(self, *, data_id: str) -> None:
        assert data_id.startswith("base-dataset-")
        self.discarded_data_ids.append(data_id)
        if self.fail_discard:
            raise AnnotationBackendExecutionError("cleanup failed")

    def download_video(self, *, video_id: str, destination: BinaryIO) -> None:
        assert video_id.startswith("base-video-")
        if self.fail_download:
            raise AnnotationBackendExecutionError("derived download failed")
        destination.write(b"derived video")

    def split_video(
        self,
        *,
        video_id: str,
        segments: Sequence[AnnotationSegment],
        mode: AnnotationMode,
    ) -> list[dict[str, Any]]:
        del video_id, segments, mode
        return [{"id": "unused"}]


class FakeAnnotationCopyProbe:
    def probe(self, path: str) -> MediaMetadata:
        assert path
        return MediaMetadata(duration_seconds=20.0, codec="h264", container="mp4")


@dataclass
class FakeAnnotationJobs:
    jobs: dict[UUID, ApplicationJob] = field(default_factory=dict)

    def get_or_create_annotation(
        self, *, member_id: UUID, attempt_id: UUID, now: datetime
    ) -> ApplicationJob:
        existing = next(
            (job for job in self.jobs.values() if job.attempt_id == attempt_id),
            None,
        )
        if existing is not None:
            return existing
        job = ApplicationJob(
            id=UUID(f"019937d8-0d10-7b31-8d2d-{len(self.jobs) + 1:012d}"),
            job_type=JobType.DATASET_ANNOTATION,
            status=JobStatus.PENDING,
            member_id=member_id,
            attempt_id=attempt_id,
            created_at=now,
            updated_at=now,
            failure_code=None,
        )
        self.jobs[job.id] = job
        return job

    def get_or_create_annotation_preparation(
        self, *, member_id: UUID, attempt_id: UUID, now: datetime
    ) -> ApplicationJob:
        existing = next(
            (
                job
                for job in self.jobs.values()
                if job.attempt_id == attempt_id
                and job.job_type is JobType.DATASET_ANNOTATION_PREPARATION
            ),
            None,
        )
        if existing is not None:
            return existing
        job = ApplicationJob(
            id=UUID(f"019937d8-0d10-7b31-8d2d-{len(self.jobs) + 1:012d}"),
            job_type=JobType.DATASET_ANNOTATION_PREPARATION,
            status=JobStatus.PENDING,
            member_id=member_id,
            attempt_id=attempt_id,
            created_at=now,
            updated_at=now,
            failure_code=None,
        )
        self.jobs[job.id] = job
        return job


def caller(*permissions: Permission) -> Caller:
    granted = set(permissions)
    # 既有用例沿用编辑前的导入权限标签；显式权限边界用例改用 `raw_caller`。
    if Permission.DATASET_IMPORT in granted:
        granted.update({Permission.DATASET_VIEW, Permission.DATASET_EDIT})
    return raw_caller(*granted)


def raw_caller(*permissions: Permission) -> Caller:
    return Caller(
        user=User(
            id=ACTOR_ID,
            login_name="annotator",
            display_name="标注员",
            password_hash="unused",  # pragma: allowlist secret
            status=UserStatus.ACTIVE,
        ),
        granted=frozenset(permissions),
    )


def store() -> FakeAnnotationStore:
    value = FakeAnnotationStore()
    value.datasets[DATASET_ID] = TrainingDataset(
        id=DATASET_ID,
        name="训练集",
        created_by=ACTOR_ID,
        updated_by=ACTOR_ID,
        created_at=NOW,
        updated_at=NOW,
    )
    value.members[MEMBER_ID] = DatasetMember(
        id=MEMBER_ID,
        dataset_id=DATASET_ID,
        original_filename="line.mp4",
        source="camera-a",
        declared_size=100,
        declared_sha256="a" * 64,
        current_attempt_id=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f104"),
        status=MemberStatus.REGISTERED,
        actual_size=100,
        actual_sha256="a" * 64,
        duration_seconds=20.0,
        codec="h264",
        container="mp4",
        object_key="training-datasets/dataset/member/video",
        validation_job_id=None,
        failure_code=None,
        failure_detail=None,
        recovery_action=None,
        created_by=ACTOR_ID,
        updated_by=ACTOR_ID,
        created_at=NOW,
        updated_at=NOW,
    )
    return value


def context_for(value: FakeAnnotationStore) -> str:
    register_action_list(
        dataset_id=DATASET_ID,
        actions=("(1)拿取工件", "(2)安装部件"),
        caller=caller(Permission.DATASET_IMPORT),
        now=NOW,
        datasets=value,
    )
    context = create_annotation_context(
        dataset_id=DATASET_ID,
        member_id=MEMBER_ID,
        caller=caller(Permission.DATASET_IMPORT),
        now=NOW,
        datasets=value,
        secret=SECRET,
        ttl_seconds=3600,
    )
    value.contexts[context.id] = replace(
        context,
        preparation_status="succeeded",
        upstream_data_id="base-dataset",
        upstream_video_id="base-video",
        upstream_video_size=100,
        upstream_video_sha256="b" * 64,
        upstream_video_duration_seconds=20.0,
    )
    return context.token


def segments() -> list[dict[str, Any]]:
    return [
        {
            "start": 0.0,
            "end": 3.5,
            "actionIndex": 0,
            "actionDescription": "(1)拿取工件",
        },
        {
            "start": 3.5,
            "end": 7.0,
            "actionIndex": 1,
            "actionDescription": "(2)安装部件",
        },
    ]


def test_action_list_revisions_are_append_only() -> None:
    value = store()
    first = register_action_list(
        dataset_id=DATASET_ID,
        actions=("(1)拿取工件",),
        caller=caller(Permission.DATASET_IMPORT),
        now=NOW,
        datasets=value,
    )
    second = register_action_list(
        dataset_id=DATASET_ID,
        actions=("(1)拿取工件", "(2)安装部件"),
        caller=caller(Permission.DATASET_IMPORT),
        now=NOW + timedelta(seconds=1),
        datasets=value,
    )

    assert first.revision == 1
    assert second.revision == 2
    assert value.action_list_by_revision(dataset_id=DATASET_ID, revision=1) == first


def test_stale_context_preparation_can_be_reclaimed_after_job_recovery() -> None:
    value = store()
    register_action_list(
        dataset_id=DATASET_ID,
        actions=("(1)拿取工件", "(2)安装部件"),
        caller=caller(Permission.DATASET_IMPORT),
        now=NOW,
        datasets=value,
    )
    context = create_annotation_context(
        dataset_id=DATASET_ID,
        member_id=MEMBER_ID,
        caller=caller(Permission.DATASET_IMPORT),
        now=NOW,
        datasets=value,
        secret=SECRET,
        ttl_seconds=3600,
    )
    jobs = FakeAnnotationJobs()
    job = jobs.get_or_create_annotation_preparation(
        member_id=MEMBER_ID,
        attempt_id=context.id,
        now=NOW,
    )
    value.contexts[context.id] = replace(
        context,
        preparation_job_id=job.id,
        preparation_status="running",
    )

    target = begin_annotation_context_preparation(job=job, datasets=value)

    assert target is not None
    assert target.context.preparation_status == "running"

    mismatched_job = replace(job, member_id=UUID(int=999))
    assert begin_annotation_context_preparation(job=mismatched_job, datasets=value) is None


def test_context_cleanup_candidate_fences_stale_completion_and_failure() -> None:
    value = store()
    register_action_list(
        dataset_id=DATASET_ID,
        actions=("(1)拿取工件", "(2)安装部件"),
        caller=caller(Permission.DATASET_IMPORT),
        now=NOW,
        datasets=value,
    )
    context = create_annotation_context(
        dataset_id=DATASET_ID,
        member_id=MEMBER_ID,
        caller=caller(Permission.DATASET_IMPORT),
        now=NOW,
        datasets=value,
        secret=SECRET,
        ttl_seconds=3600,
    )
    jobs = FakeAnnotationJobs()
    job = jobs.get_or_create_annotation_preparation(
        member_id=MEMBER_ID,
        attempt_id=context.id,
        now=NOW,
    )
    value.contexts[context.id] = replace(
        context,
        preparation_job_id=job.id,
        preparation_status="running",
    )
    stale = begin_annotation_context_preparation(job=job, datasets=value)
    assert stale is not None
    candidate = record_annotation_context_cleanup_candidate(
        target=stale,
        data_id="orphan-data",
        code=None,
        detail=None,
        datasets=value,
    )
    prepared = PreparedAnnotationCopy(
        prepared=PreparedAnnotationVideo(data_id="new-data", video_id="new-video"),
        size=123,
        sha256="b" * 64,
        duration_seconds=4.0,
    )

    with pytest.raises(AnnotationRefusedError) as completion:
        complete_annotation_context_preparation(
            target=stale,
            prepared=prepared,
            datasets=value,
        )
    assert completion.value.code.value == "ANNOTATION_STATE_CONFLICT"
    with pytest.raises(AnnotationRefusedError) as failure:
        fail_annotation_context_preparation(
            target=stale,
            code="ANNOTATION_EXECUTION_FAILED",
            detail="stale result",
            datasets=value,
        )
    assert failure.value.code.value == "ANNOTATION_STATE_CONFLICT"
    assert value.contexts[context.id] == candidate.context


def test_context_token_is_signed_and_binds_the_exact_registered_source() -> None:
    value = store()
    token = context_for(value)
    decoded = decode_annotation_context_token(token, secret=SECRET, now=NOW)

    assert token != str(decoded.context_id)
    assert decoded.dataset_id == DATASET_ID
    assert decoded.member_id == MEMBER_ID
    with pytest.raises(AnnotationRefusedError):
        decode_annotation_context_token(
            token[:-1] + ("A" if token[-1] != "A" else "B"), secret=SECRET, now=NOW
        )

    changed = replace(
        value.members[MEMBER_ID], object_key="training-datasets/dataset/member/video-2"
    )
    value.members[MEMBER_ID] = changed
    with pytest.raises(AnnotationRefusedError):
        submit_annotation(
            dataset_id=DATASET_ID,
            member_id=MEMBER_ID,
            context_token=token,
            raw_segments=segments(),
            mode=AnnotationMode.SINGLE_OPERATOR,
            idempotency_key="changed-source",
            caller=caller(Permission.DATASET_IMPORT),
            now=NOW,
            datasets=value,
            jobs=FakeAnnotationJobs(),
            secret=SECRET,
        )


@pytest.mark.parametrize(
    "invalid_segments",
    [
        [],
        [{"start": float("nan"), "end": 1.0, "actionIndex": 0}],
        [{"start": 0.0, "end": 21.0, "actionIndex": 0}],
        [{"start": 0.0, "end": 1.0, "actionIndex": 1.5}],
        [{"start": 0.0, "end": 1.0, "actionIndex": 2}],
        [{"start": 0.0, "end": 1.0, "actionIndex": 0, "action_index": 1}],
        [{"start": 2.0, "end": 1.0, "actionIndex": 0}],
    ],
)
def test_invalid_segments_reject_the_entire_submission(
    invalid_segments: list[dict[str, Any]],
) -> None:
    value = store()
    token = context_for(value)
    with pytest.raises(AnnotationRefusedError) as refused:
        submit_annotation(
            dataset_id=DATASET_ID,
            member_id=MEMBER_ID,
            context_token=token,
            raw_segments=invalid_segments,
            mode=AnnotationMode.SINGLE_OPERATOR,
            idempotency_key="invalid-segments",
            caller=caller(Permission.DATASET_IMPORT),
            now=NOW,
            datasets=value,
            jobs=FakeAnnotationJobs(),
            secret=SECRET,
        )
    assert refused.value.code.value == "ANNOTATION_STATE_CONFLICT"
    assert value.submissions == {}
    assert value.executions == {}


def test_annotation_idempotency_key_cannot_exceed_the_storage_limit() -> None:
    value = store()
    token = context_for(value)

    with pytest.raises(AnnotationRefusedError) as refused:
        submit_annotation(
            dataset_id=DATASET_ID,
            member_id=MEMBER_ID,
            context_token=token,
            raw_segments=segments(),
            mode=AnnotationMode.SINGLE_OPERATOR,
            idempotency_key="k" * 256,
            caller=caller(Permission.DATASET_IMPORT),
            now=NOW,
            datasets=value,
            jobs=FakeAnnotationJobs(),
            secret=SECRET,
        )

    assert refused.value.code.value == "ANNOTATION_IDEMPOTENCY_CONFLICT"
    assert value.submissions == {}


def test_same_submission_is_idempotent_and_conflicting_payload_is_refused() -> None:
    value = store()
    token = context_for(value)
    jobs = FakeAnnotationJobs()
    first = submit_annotation(
        dataset_id=DATASET_ID,
        member_id=MEMBER_ID,
        context_token=token,
        raw_segments=segments(),
        mode=AnnotationMode.SINGLE_OPERATOR,
        idempotency_key="submission-1",
        caller=caller(Permission.DATASET_IMPORT),
        now=NOW,
        datasets=value,
        jobs=jobs,
        secret=SECRET,
    )
    replay = submit_annotation(
        dataset_id=DATASET_ID,
        member_id=MEMBER_ID,
        context_token=token,
        raw_segments=segments(),
        mode=AnnotationMode.SINGLE_OPERATOR,
        idempotency_key="submission-1",
        caller=caller(Permission.DATASET_IMPORT),
        now=NOW + timedelta(seconds=1),
        datasets=value,
        jobs=jobs,
        secret=SECRET,
    )

    assert replay == first
    assert len(value.submissions) == 1
    assert len(value.executions) == 1
    assert len(jobs.jobs) == 1
    with pytest.raises(AnnotationRefusedError) as refused:
        submit_annotation(
            dataset_id=DATASET_ID,
            member_id=MEMBER_ID,
            context_token=token,
            raw_segments=[{**segments()[0], "end": 4.0}, segments()[1]],
            mode=AnnotationMode.SINGLE_OPERATOR,
            idempotency_key="submission-1",
            caller=caller(Permission.DATASET_IMPORT),
            now=NOW,
            datasets=value,
            jobs=jobs,
            secret=SECRET,
        )
    assert refused.value.code.value == "ANNOTATION_IDEMPOTENCY_CONFLICT"


def test_annotation_submission_uses_if_match_revision_for_concurrent_editors() -> None:
    value = store()
    first_token = context_for(value)
    second_context = create_annotation_context(
        dataset_id=DATASET_ID,
        member_id=MEMBER_ID,
        caller=caller(Permission.DATASET_IMPORT),
        now=NOW,
        datasets=value,
        secret=SECRET,
        ttl_seconds=3600,
    )
    value.contexts[second_context.id] = replace(
        second_context,
        preparation_status="succeeded",
        upstream_data_id="base-dataset-2",
        upstream_video_id="base-video-2",
        upstream_video_size=100,
        upstream_video_sha256="c" * 64,
        upstream_video_duration_seconds=20.0,
    )
    jobs = FakeAnnotationJobs()
    first = submit_annotation(
        dataset_id=DATASET_ID,
        member_id=MEMBER_ID,
        context_token=first_token,
        raw_segments=segments(),
        mode=AnnotationMode.SINGLE_OPERATOR,
        idempotency_key="concurrent-first",
        caller=caller(Permission.DATASET_IMPORT),
        now=NOW,
        datasets=value,
        jobs=jobs,
        secret=SECRET,
        expected_revision=0,
    )

    assert first.submission.revision == 1
    with pytest.raises(AnnotationRefusedError) as refused:
        submit_annotation(
            dataset_id=DATASET_ID,
            member_id=MEMBER_ID,
            context_token=second_context.token,
            raw_segments=segments(),
            mode=AnnotationMode.SINGLE_OPERATOR,
            idempotency_key="concurrent-second",
            caller=caller(Permission.DATASET_IMPORT),
            now=NOW,
            datasets=value,
            jobs=jobs,
            secret=SECRET,
            expected_revision=0,
        )
    assert refused.value.code.value == "STALE_REVISION"


def test_invalid_annotation_segments_identify_the_rejected_entry() -> None:
    value = store()
    token = context_for(value)
    with pytest.raises(AnnotationRefusedError) as refused:
        submit_annotation(
            dataset_id=DATASET_ID,
            member_id=MEMBER_ID,
            context_token=token,
            raw_segments=[{"start": 2.0, "end": 1.0, "actionIndex": 0}],
            mode=AnnotationMode.SINGLE_OPERATOR,
            idempotency_key="invalid-segment",
            caller=caller(Permission.DATASET_IMPORT),
            now=NOW,
            datasets=value,
            jobs=FakeAnnotationJobs(),
            secret=SECRET,
            expected_revision=0,
        )

    assert refused.value.field_errors == (
        DatasetFieldError(
            field="segments[0]",
            message="必须满足 0 ≤ start < end ≤ 视频时长，且动作编号有效",
        ),
    )


def test_a_prepared_context_can_submit_a_new_revision_with_a_fresh_if_match() -> None:
    value = store()
    source = b"annotation source"
    value.members[MEMBER_ID] = replace(
        value.members[MEMBER_ID],
        actual_size=len(source),
        actual_sha256=sha256(source).hexdigest(),
    )
    token = context_for(value)
    jobs = FakeAnnotationJobs()
    backend = FakeAnnotationCopyBackend()
    storage = FakeAnnotationCopyStorage(source)
    probe = FakeAnnotationCopyProbe()

    first = submit_annotation(
        dataset_id=DATASET_ID,
        member_id=MEMBER_ID,
        context_token=token,
        raw_segments=segments(),
        mode=AnnotationMode.SINGLE_OPERATOR,
        idempotency_key="revision-1",
        caller=caller(Permission.DATASET_IMPORT),
        now=NOW,
        datasets=value,
        jobs=jobs,
        secret=SECRET,
        expected_revision=0,
    )
    first_target = begin_annotation_execution(
        job=jobs.jobs[first.job.id], datasets=value, now=NOW + timedelta(seconds=1)
    )
    assert first_target is not None
    first_target = save_annotation_execution_copy(
        target=first_target,
        prepared=prepare_annotation_execution_copy(
            target=first_target,
            storage=storage,
            backend=backend,
            media_probe=probe,
        ),
        now=NOW + timedelta(seconds=2),
        datasets=value,
    )
    complete_annotation_execution(
        target=first_target,
        clips=({"id": "clip-first"},),
        now=NOW + timedelta(seconds=3),
        datasets=value,
    )

    second = submit_annotation(
        dataset_id=DATASET_ID,
        member_id=MEMBER_ID,
        context_token=token,
        raw_segments=segments(),
        mode=AnnotationMode.TWO_OPERATOR,
        idempotency_key="revision-2",
        caller=caller(Permission.DATASET_IMPORT),
        now=NOW + timedelta(seconds=4),
        datasets=value,
        jobs=jobs,
        secret=SECRET,
        expected_revision=1,
    )
    second_target = begin_annotation_execution(
        job=jobs.jobs[second.job.id], datasets=value, now=NOW + timedelta(seconds=5)
    )
    assert second_target is not None
    second_prepared = prepare_annotation_execution_copy(
        target=second_target,
        storage=storage,
        backend=backend,
        media_probe=probe,
    )
    second_target = save_annotation_execution_copy(
        target=second_target,
        prepared=second_prepared,
        now=NOW + timedelta(seconds=6),
        datasets=value,
    )
    complete_annotation_execution(
        target=second_target,
        clips=({"id": "clip-second"},),
        now=NOW + timedelta(seconds=7),
        datasets=value,
    )

    assert first.submission.revision == 1
    assert second.submission.revision == 2
    assert second.submission.mode is AnnotationMode.TWO_OPERATOR
    assert second_prepared.prepared.video_id == "base-video-2"
    assert len(value.submissions) == 2


def test_execution_rejects_a_clip_without_a_nonempty_identity() -> None:
    value = store()
    token = context_for(value)
    jobs = FakeAnnotationJobs()
    submitted = submit_annotation(
        dataset_id=DATASET_ID,
        member_id=MEMBER_ID,
        context_token=token,
        raw_segments=segments(),
        mode=AnnotationMode.SINGLE_OPERATOR,
        idempotency_key="empty-clip-id",
        caller=caller(Permission.DATASET_IMPORT),
        now=NOW,
        datasets=value,
        jobs=jobs,
        secret=SECRET,
    )
    running = begin_annotation_execution(
        job=jobs.jobs[submitted.job.id], datasets=value, now=NOW + timedelta(seconds=1)
    )
    assert running is not None

    with pytest.raises(AnnotationRefusedError) as refused:
        complete_annotation_execution(
            target=running,
            clips=({"id": ""},),
            now=NOW + timedelta(seconds=2),
            datasets=value,
        )

    assert refused.value.code.value == "ANNOTATION_EXECUTION_FAILED"
    assert value.executions[running.execution.id].status.value == "running"


def test_retry_creates_new_execution_without_overwriting_old_candidate() -> None:
    value = store()
    token = context_for(value)
    jobs = FakeAnnotationJobs()
    submitted = submit_annotation(
        dataset_id=DATASET_ID,
        member_id=MEMBER_ID,
        context_token=token,
        raw_segments=segments(),
        mode=AnnotationMode.TWO_OPERATOR,
        idempotency_key="submission-2",
        caller=caller(Permission.DATASET_IMPORT),
        now=NOW,
        datasets=value,
        jobs=jobs,
        secret=SECRET,
    )
    running = begin_annotation_execution(
        job=jobs.jobs[submitted.job.id], datasets=value, now=NOW + timedelta(seconds=1)
    )
    assert running is not None
    old = complete_annotation_execution(
        target=running,
        clips=({"id": "clip-old", "start_time": 0.0, "end_time": 3.5},),
        now=NOW + timedelta(seconds=2),
        datasets=value,
    )
    retried = retry_annotation(
        submission_id=submitted.submission.id,
        caller=caller(Permission.DATASET_IMPORT),
        now=NOW + timedelta(seconds=3),
        datasets=value,
        jobs=jobs,
    )

    assert old.clips == ({"id": "clip-old", "start_time": 0.0, "end_time": 3.5},)
    assert retried.execution.id != old.id
    assert retried.execution.generation == 2
    assert [item.clips for item in value.list_annotation_executions(submitted.submission.id)] == [
        ({"id": "clip-old", "start_time": 0.0, "end_time": 3.5},),
        (),
    ]


def test_execution_cleanup_candidate_preempts_newer_unpublished_retry() -> None:
    value = store()
    token = context_for(value)
    jobs = FakeAnnotationJobs()
    submitted = submit_annotation(
        dataset_id=DATASET_ID,
        member_id=MEMBER_ID,
        context_token=token,
        raw_segments=segments(),
        mode=AnnotationMode.SINGLE_OPERATOR,
        idempotency_key="cleanup-preempts-retry",
        caller=caller(Permission.DATASET_IMPORT),
        now=NOW,
        datasets=value,
        jobs=jobs,
        secret=SECRET,
    )
    stale = begin_annotation_execution(
        job=jobs.jobs[submitted.job.id],
        datasets=value,
        now=NOW + timedelta(seconds=1),
    )
    assert stale is not None
    newer_execution = replace(stale.execution, updated_at=NOW + timedelta(seconds=2))
    value.executions[stale.execution.id] = newer_execution
    newer = replace(stale, execution=newer_execution)

    recorded = record_annotation_execution_cleanup_candidate(
        target=stale,
        data_id="orphan-data",
        code=None,
        detail=None,
        now=NOW + timedelta(seconds=3),
        datasets=value,
    )
    prepared = PreparedAnnotationCopy(
        prepared=PreparedAnnotationVideo(data_id="new-data", video_id="new-video"),
        size=123,
        sha256="c" * 64,
        duration_seconds=4.0,
    )

    assert recorded.execution.upstream_data_id == "orphan-data"
    with pytest.raises(AnnotationRefusedError) as conflict:
        save_annotation_execution_copy(
            target=newer,
            prepared=prepared,
            now=NOW + timedelta(seconds=4),
            datasets=value,
        )
    assert conflict.value.code.value == "ANNOTATION_STATE_CONFLICT"
    assert value.executions[stale.execution.id].upstream_data_id == "orphan-data"


def test_prepare_execution_copy_discards_backend_copy_when_post_upload_step_fails() -> None:
    value = store()
    source = b"annotation source"
    value.members[MEMBER_ID] = replace(
        value.members[MEMBER_ID],
        actual_size=len(source),
        actual_sha256=sha256(source).hexdigest(),
    )
    token = context_for(value)
    jobs = FakeAnnotationJobs()
    submitted = submit_annotation(
        dataset_id=DATASET_ID,
        member_id=MEMBER_ID,
        context_token=token,
        raw_segments=segments(),
        mode=AnnotationMode.SINGLE_OPERATOR,
        idempotency_key="cleanup-post-upload-failure",
        caller=caller(Permission.DATASET_IMPORT),
        now=NOW,
        datasets=value,
        jobs=jobs,
        secret=SECRET,
    )
    target = begin_annotation_execution(
        job=jobs.jobs[submitted.job.id],
        datasets=value,
        now=NOW + timedelta(seconds=1),
    )
    assert target is not None
    backend = FakeAnnotationCopyBackend(fail_download=True)

    with pytest.raises(AnnotationRefusedError) as refused:
        prepare_annotation_execution_copy(
            target=target,
            storage=FakeAnnotationCopyStorage(source),
            backend=backend,
            media_probe=FakeAnnotationCopyProbe(),
        )

    assert refused.value.code.value == "ANNOTATION_EXECUTION_FAILED"
    assert backend.discarded_data_ids == ["base-dataset-1"]


def test_failed_backend_copy_cleanup_retains_candidate_identity() -> None:
    value = store()
    source = b"annotation source"
    value.members[MEMBER_ID] = replace(
        value.members[MEMBER_ID],
        actual_size=len(source),
        actual_sha256=sha256(source).hexdigest(),
    )
    token = context_for(value)
    jobs = FakeAnnotationJobs()
    submitted = submit_annotation(
        dataset_id=DATASET_ID,
        member_id=MEMBER_ID,
        context_token=token,
        raw_segments=segments(),
        mode=AnnotationMode.SINGLE_OPERATOR,
        idempotency_key="cleanup-candidate",
        caller=caller(Permission.DATASET_IMPORT),
        now=NOW,
        datasets=value,
        jobs=jobs,
        secret=SECRET,
    )
    target = begin_annotation_execution(
        job=jobs.jobs[submitted.job.id],
        datasets=value,
        now=NOW + timedelta(seconds=1),
    )
    assert target is not None
    backend = FakeAnnotationCopyBackend(fail_download=True, fail_discard=True)

    with pytest.raises(AnnotationCleanupPendingError) as pending:
        prepare_annotation_execution_copy(
            target=target,
            storage=FakeAnnotationCopyStorage(source),
            backend=backend,
            media_probe=FakeAnnotationCopyProbe(),
        )

    assert pending.value.data_id == "base-dataset-1"
    assert isinstance(pending.value.failure, AnnotationRefusedError)
    assert pending.value.failure.code.value == "ANNOTATION_EXECUTION_FAILED"
    assert backend.discarded_data_ids == ["base-dataset-1"]


def test_each_execution_uses_a_distinct_prepared_base_copy() -> None:
    value = store()
    source = b"annotation source"
    value.members[MEMBER_ID] = replace(
        value.members[MEMBER_ID],
        actual_size=len(source),
        actual_sha256=sha256(source).hexdigest(),
    )
    token = context_for(value)
    jobs = FakeAnnotationJobs()
    submitted = submit_annotation(
        dataset_id=DATASET_ID,
        member_id=MEMBER_ID,
        context_token=token,
        raw_segments=segments(),
        mode=AnnotationMode.SINGLE_OPERATOR,
        idempotency_key="distinct-copy",
        caller=caller(Permission.DATASET_IMPORT),
        now=NOW,
        datasets=value,
        jobs=jobs,
        secret=SECRET,
    )
    backend = FakeAnnotationCopyBackend()
    first = begin_annotation_execution(
        job=jobs.jobs[submitted.job.id], datasets=value, now=NOW + timedelta(seconds=1)
    )
    assert first is not None
    first = save_annotation_execution_copy(
        target=first,
        prepared=prepare_annotation_execution_copy(
            target=first,
            storage=FakeAnnotationCopyStorage(source),
            backend=backend,
            media_probe=FakeAnnotationCopyProbe(),
        ),
        now=NOW + timedelta(seconds=2),
        datasets=value,
    )
    complete_annotation_execution(
        target=first,
        clips=({"id": "clip-first"},),
        now=NOW + timedelta(seconds=3),
        datasets=value,
    )

    retried = retry_annotation(
        submission_id=submitted.submission.id,
        caller=caller(Permission.DATASET_IMPORT),
        now=NOW + timedelta(seconds=4),
        datasets=value,
        jobs=jobs,
    )
    second = begin_annotation_execution(
        job=jobs.jobs[retried.job.id], datasets=value, now=NOW + timedelta(seconds=5)
    )
    assert second is not None
    second = save_annotation_execution_copy(
        target=second,
        prepared=prepare_annotation_execution_copy(
            target=second,
            storage=FakeAnnotationCopyStorage(source),
            backend=backend,
            media_probe=FakeAnnotationCopyProbe(),
        ),
        now=NOW + timedelta(seconds=6),
        datasets=value,
    )

    assert first.execution.upstream_video_id == "base-video-1"
    assert second.execution.upstream_video_id == "base-video-2"
    assert value.executions[first.execution.id].upstream_video_id == "base-video-1"
    assert value.executions[second.execution.id].upstream_video_id == "base-video-2"


def test_annotation_requires_edit_permission_before_writing_resources() -> None:
    value = store()
    with pytest.raises(AuthorizationRefusedError):
        register_action_list(
            dataset_id=DATASET_ID,
            actions=("(1)拿取工件",),
            caller=raw_caller(Permission.DATASET_VIEW, Permission.DATASET_IMPORT),
            now=NOW,
            datasets=value,
        )
