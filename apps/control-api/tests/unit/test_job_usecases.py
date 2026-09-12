"""任务读取用例的权限矩阵与资源归属 seam。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest

from factory_sop.auth.authorization import AuthorizationRefusedError, Caller
from factory_sop.auth.model import User, UserStatus
from factory_sop.auth.permissions import Permission
from factory_sop.job.api import ApplicationJob, JobRepository, JobStatus, JobType
from factory_sop.job.usecases import read_job

NOW = datetime(2026, 9, 12, 1, 0, tzinfo=UTC)
ACTOR_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f501")
DATASET_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f502")
MEMBER_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f503")


@dataclass
class FakeJobs:
    job: ApplicationJob

    def by_id(self, job_id: UUID) -> ApplicationJob | None:
        return self.job if self.job.id == job_id else None


def caller(*permissions: Permission) -> Caller:
    return Caller(
        user=User(
            id=ACTOR_ID,
            login_name="job-reader",
            display_name="任务读取测试",
            password_hash="unused",  # pragma: allowlist secret
            status=UserStatus.ACTIVE,
        ),
        granted=frozenset(permissions),
    )


def job(job_type: JobType) -> ApplicationJob:
    return ApplicationJob(
        id=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f504"),
        job_type=job_type,
        status=JobStatus.PENDING,
        member_id=MEMBER_ID,
        attempt_id=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f505"),
        created_at=NOW,
        updated_at=NOW,
        failure_code=None,
        dataset_id=None if job_type is JobType.DATASET_VALIDATION else DATASET_ID,
    )


def read(job_value: ApplicationJob, permissions: frozenset[Permission]) -> ApplicationJob:
    return read_job(
        job_id=job_value.id,
        caller=caller(*permissions),
        jobs=cast(JobRepository, FakeJobs(job_value)),
        resource_exists=lambda _member_id, _attempt_id: True,
        dataset_resource_exists=lambda _dataset_id, _resource_id: True,
    )


@pytest.mark.parametrize("job_type", list(JobType))
def test_dataset_view_can_read_every_persisted_job_type(job_type: JobType) -> None:
    value = job(job_type)

    assert read(value, frozenset({Permission.DATASET_VIEW})) == value


@pytest.mark.parametrize("job_type", list(JobType))
def test_import_only_can_read_only_video_validation_jobs(job_type: JobType) -> None:
    value = job(job_type)

    if job_type is JobType.DATASET_VALIDATION:
        assert read(value, frozenset({Permission.DATASET_IMPORT})) == value
    else:
        with pytest.raises(AuthorizationRefusedError) as refused:
            read(value, frozenset({Permission.DATASET_IMPORT}))
        assert refused.value.permission is Permission.DATASET_VIEW
