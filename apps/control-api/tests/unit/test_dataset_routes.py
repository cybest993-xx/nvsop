"""训练数据集 HTTP seam：权限、直传说明和任务幂等。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_dataset_usecases import (
    FakeDatasets,
    FakeJobs,
    FakeStorage,
)

from factory_sop.app import API_PREFIX, create_app
from factory_sop.auth.adapters import dependencies as auth_dependencies
from factory_sop.auth.model import Session, User, UserStatus
from factory_sop.auth.permissions import Permission
from factory_sop.auth.usecases.sessions import RestoredSession
from factory_sop.dataset.adapters import dependencies as dataset_dependencies
from factory_sop.dataset.adapters.routes import (
    ArtifactAcceptedView,
    UsageCheckAcceptedView,
    UsageCheckView,
    UsageJobView,
    VlmCandidateView,
)
from factory_sop.dataset.model import (
    ActionListRevision,
    AnnotationExecution,
    AnnotationExecutionStatus,
    AnnotationMode,
    AnnotationSegment,
    AnnotationSubmission,
    AttemptStatus,
    DatasetMember,
    MemberStatus,
    TrainingDataset,
    UploadAttempt,
)
from factory_sop.dataset.repository import UsageDatasetRepository
from factory_sop.dataset.usage import UsageValidationResult
from factory_sop.dataset.usecases.usage import (
    begin_usage_check,
    complete_usage_check,
    fail_usage_check,
)
from factory_sop.job.adapters import dependencies as job_dependencies
from factory_sop.job.adapters.routes import JobView as GenericJobView
from factory_sop.persistence import request_session
from factory_sop.settings import Settings

NOW = datetime(2026, 9, 9, 1, 0, tzinfo=UTC)
ACTOR_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f201")
DATASET_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f202")
MEMBER_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f203")
ATTEMPT_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f204")
CONTEXT_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f207")
SUBMISSION_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f208")


def settings() -> Settings:
    return Settings(
        log_level="warning",
        database_host="unused",
        database_port=5432,
        database_name="unused",
        database_user="unused",
        database_password=SecretStr("unused"),
        session_idle_timeout_minutes=720,
        session_absolute_lifetime_minutes=43200,
        session_cookie_transport="require_https",
        csrf_secret=SecretStr("csrf-secret"),
        redis_url=SecretStr("redis://127.0.0.1:1/0"),
    )


def actor() -> RestoredSession:
    return RestoredSession(
        session=Session(
            id=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f205"),
            user_id=ACTOR_ID,
            token_fingerprint="unused",
            created_at=NOW,
            last_used_at=NOW,
        ),
        user=User(
            id=ACTOR_ID,
            login_name="dataset-test",
            display_name="数据集测试",
            password_hash="unused",  # pragma: allowlist secret
            status=UserStatus.ACTIVE,
        ),
    )


class StubRequestSession:
    """流式上传路由在读取请求体前结束事务；路由套件不需要真实数据库连接。"""

    def rollback(self) -> None:
        return None


class Backend:
    """正式路由 over 三个领域 seam 的内存测试组合根。"""

    def __init__(self, permissions: frozenset[Permission]) -> None:
        self.datasets = FakeDatasets()
        self.datasets.add_dataset(
            TrainingDataset(
                id=DATASET_ID,
                name="路由测试集",
                created_by=ACTOR_ID,
                updated_by=ACTOR_ID,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        self.datasets.add_member(
            DatasetMember(
                id=MEMBER_ID,
                dataset_id=DATASET_ID,
                original_filename="failed.mp4",
                source="测试来源",
                declared_size=1,
                declared_sha256="a" * 64,
                current_attempt_id=ATTEMPT_ID,
                status=MemberStatus.PENDING_UPLOAD,
                actual_size=None,
                actual_sha256=None,
                duration_seconds=None,
                codec=None,
                container=None,
                object_key=None,
                object_version_id=None,
                validation_job_id=None,
                failure_code=None,
                failure_detail=None,
                recovery_action=None,
                created_by=ACTOR_ID,
                updated_by=ACTOR_ID,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        self.datasets.add_attempt(
            UploadAttempt(
                id=ATTEMPT_ID,
                dataset_id=DATASET_ID,
                member_id=MEMBER_ID,
                idempotency_key="existing",
                object_key="training-datasets/test/member/attempt/video",
                declared_size=1,
                declared_sha256="a" * 64,
                expires_at=NOW,
                status=AttemptStatus.PENDING_UPLOAD,
                created_at=NOW,
                validation_job_id=None,
                object_version_id=None,
            )
        )
        self.storage = FakeStorage()
        self.jobs = FakeJobs()
        self.app = create_app(settings())
        self.app.dependency_overrides[auth_dependencies.authenticated_caller] = actor
        self.app.dependency_overrides[auth_dependencies.granted_permissions] = lambda: permissions
        self.app.dependency_overrides[dataset_dependencies.datasets] = lambda: self.datasets
        self.app.dependency_overrides[dataset_dependencies.storage] = lambda: self.storage
        self.app.dependency_overrides[dataset_dependencies.jobs] = lambda: self.jobs
        self.app.dependency_overrides[dataset_dependencies.usage_jobs] = lambda: self.jobs
        self.app.dependency_overrides[job_dependencies.job_repository] = lambda: self.jobs
        self.app.dependency_overrides[request_session] = StubRequestSession
        self.client = TestClient(self.app, base_url="https://testserver")


def seed_registered_annotation(backend: Backend) -> None:
    member = backend.datasets.members[MEMBER_ID]
    backend.datasets.members[MEMBER_ID] = replace(
        member,
        status=MemberStatus.REGISTERED,
        actual_size=1,
        actual_sha256="a" * 64,
        duration_seconds=2.0,
        object_key=backend.datasets.attempts[ATTEMPT_ID].object_key,
        object_version_id="version-1",
    )
    backend.datasets.attempts[ATTEMPT_ID] = replace(
        backend.datasets.attempts[ATTEMPT_ID],
        status=AttemptStatus.REGISTERED,
        object_version_id="version-1",
    )
    backend.datasets.add_action_list(
        ActionListRevision(
            dataset_id=DATASET_ID,
            revision=1,
            actions=("(1) 取料", "(2) 安装"),
            created_by=ACTOR_ID,
            created_at=NOW,
        )
    )
    submission = AnnotationSubmission(
        id=SUBMISSION_ID,
        dataset_id=DATASET_ID,
        member_id=MEMBER_ID,
        context_id=CONTEXT_ID,
        revision=1,
        action_list_revision=1,
        source_object_version_id="version-1",
        source_sha256="a" * 64,
        idempotency_key="route-annotation",
        request_digest="b" * 64,
        mode=AnnotationMode.SINGLE_OPERATOR,
        segments=(
            AnnotationSegment(0.0, 1.0, 0, "(1) 取料"),
            AnnotationSegment(1.0, 2.0, 1, "(2) 安装"),
        ),
        raw_segments=(),
        created_by=ACTOR_ID,
        created_at=NOW,
    )
    backend.datasets.add_annotation_submission(submission)
    backend.datasets.add_annotation_execution(
        AnnotationExecution(
            id=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f30d"),
            submission_id=submission.id,
            generation=1,
            job_id=None,
            status=AnnotationExecutionStatus.SUCCEEDED,
            clips=({"id": "clip-1"},),
            failure_code=None,
            failure_detail=None,
            created_at=NOW,
            updated_at=NOW,
        )
    )


@pytest.fixture
def import_backend() -> Backend:
    return Backend(frozenset({Permission.DATASET_IMPORT}))


@pytest.fixture
def view_backend() -> Backend:
    return Backend(frozenset({Permission.DATASET_VIEW}))


@pytest.fixture
def editor_backend() -> Backend:
    return Backend(frozenset({Permission.DATASET_EDIT, Permission.DATASET_VIEW}))


def test_create_and_list_dataset_use_the_same_control_plane_seam(
    import_backend: Backend,
) -> None:
    created = import_backend.client.post(
        f"{API_PREFIX}/training-datasets",
        json={"name": "新增训练集"},
    )

    assert created.status_code == 201
    dataset_id = created.json()["id"]
    listed = import_backend.client.get(f"{API_PREFIX}/training-datasets")

    assert listed.status_code == 403
    assert listed.json() == {
        "type": "about:blank",
        "title": "没有执行该操作的权限",
        "status": 403,
        "error_code": "PERMISSION_DENIED",
    }
    assert dataset_id in {str(item.id) for item in import_backend.datasets.datasets.values()}


def test_read_only_caller_cannot_request_an_upload(view_backend: Backend) -> None:
    response = view_backend.client.post(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/members",
        json={
            "original_filename": "sample.mp4",
            "source": "相机",
            "declared_size": 1,
            "declared_sha256": "a" * 64,
        },
    )

    assert response.status_code == 403
    assert response.json() == {
        "type": "about:blank",
        "title": "没有执行该操作的权限",
        "status": 403,
        "error_code": "PERMISSION_DENIED",
    }
    assert view_backend.storage.writes == []


def test_upload_request_returns_short_lived_instructions_but_member_read_does_not(
    import_backend: Backend,
) -> None:
    requested = import_backend.client.post(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/members",
        headers={"Idempotency-Key": "route-request-1"},
        json={
            "original_filename": "sample.mp4",
            "source": "相机",
            "declared_size": 1,
            "declared_sha256": "a" * 64,
        },
    )

    assert requested.status_code == 201
    upload = requested.json()["upload"]
    member_id = requested.json()["member"]["id"]
    attempt_id = requested.json()["attempt"]["id"]
    assert upload["method"] == "PUT"
    assert upload["url"] == (
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/members/{member_id}"
        f"/attempts/{attempt_id}/content"
    )
    assert upload["fields"] == {}
    read = import_backend.client.get(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/members/{member_id}"
    )

    assert read.status_code == 403


def test_archive_request_is_rejected_before_storage_and_exposes_recovery_action(
    import_backend: Backend,
) -> None:
    response = import_backend.client.post(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/members",
        json={
            "original_filename": "samples.zip",
            "source": "文件",
            "declared_size": 1,
            "declared_sha256": "a" * 64,
        },
    )

    assert response.status_code == 422
    assert response.json() == {
        "type": "about:blank",
        "title": "视频上传声明不符合要求",
        "status": 422,
        "error_code": "ARCHIVE_REJECTED",
        "detail": "压缩包不允许导入，请逐个选择视频文件",
        "recovery_action": "retry_upload",
    }
    assert import_backend.storage.writes == []


def _make_attempt_uploadable(
    backend: Backend, *, declared_size: int, expires_in: timedelta
) -> None:
    attempt = backend.datasets.attempts[ATTEMPT_ID]
    backend.datasets.attempts[ATTEMPT_ID] = replace(
        attempt,
        declared_size=declared_size,
        expires_at=datetime.now(UTC) + expires_in,
    )


def test_uploading_video_content_streams_the_body_into_dataset_storage(
    import_backend: Backend,
) -> None:
    _make_attempt_uploadable(import_backend, declared_size=4, expires_in=timedelta(minutes=5))

    response = import_backend.client.put(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/members/{MEMBER_ID}"
        f"/attempts/{ATTEMPT_ID}/content",
        content=b"data",
        headers={"Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 204
    object_key = import_backend.datasets.attempts[ATTEMPT_ID].object_key
    assert import_backend.storage.writes == [object_key]
    assert import_backend.storage.objects[object_key] == b"data"


def test_uploading_more_than_the_declared_size_leaves_no_finalized_object(
    import_backend: Backend,
) -> None:
    _make_attempt_uploadable(import_backend, declared_size=2, expires_in=timedelta(minutes=5))

    response = import_backend.client.put(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/members/{MEMBER_ID}"
        f"/attempts/{ATTEMPT_ID}/content",
        content=b"too much",
        headers={"Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "SIZE_EXCEEDED"
    assert import_backend.storage.objects == {}


def test_uploading_video_content_requires_dataset_import(view_backend: Backend) -> None:
    response = view_backend.client.put(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/members/{MEMBER_ID}"
        f"/attempts/{ATTEMPT_ID}/content",
        content=b"data",
        headers={"Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 403
    assert view_backend.storage.writes == []


def test_uploading_an_expired_attempt_is_refused(import_backend: Backend) -> None:
    _make_attempt_uploadable(import_backend, declared_size=4, expires_in=timedelta(seconds=-1))

    response = import_backend.client.put(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/members/{MEMBER_ID}"
        f"/attempts/{ATTEMPT_ID}/content",
        content=b"data",
        headers={"Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 409
    assert import_backend.storage.writes == []


def test_confirming_the_same_attempt_returns_the_same_validation_job(
    import_backend: Backend,
) -> None:
    first = import_backend.client.post(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/members/{MEMBER_ID}/confirm",
        json={"attempt_id": str(ATTEMPT_ID)},
    )
    second = import_backend.client.post(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/members/{MEMBER_ID}/confirm",
        json={"attempt_id": str(ATTEMPT_ID)},
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["job"]["id"] == second.json()["job"]["id"]
    assert len(import_backend.jobs.jobs) == 1


def test_usage_checks_and_artifacts_use_the_published_http_contract(
    editor_backend: Backend,
) -> None:
    seed_registered_annotation(editor_backend)
    requested = editor_backend.client.post(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/usage-checks",
        json={"kind": "ddm"},
    )

    assert requested.status_code == 202
    requested_body = requested.json()
    assert requested_body == UsageCheckAcceptedView.model_validate(requested_body).model_dump(
        mode="json"
    )
    check = requested_body["check"]
    job = requested_body["job"]
    assert set(check) == {
        "id",
        "dataset_id",
        "kind",
        "status",
        "is_current",
        "input_digest",
        "input_snapshot",
        "summary",
        "issues",
        "base_commit",
        "contract_version",
        "candidate_id",
        "job_id",
        "created_by",
        "created_at",
        "updated_at",
    }
    assert set(job) == {
        "id",
        "job_type",
        "status",
        "dataset_id",
        "attempt_id",
        "failure_code",
        "created_at",
        "updated_at",
    }

    assert check["is_current"] is True
    assert check == UsageCheckView.model_validate(check).model_dump(mode="json")
    assert job == UsageJobView.model_validate(job).model_dump(mode="json")
    usage_job = editor_backend.jobs.jobs[UUID(job["id"])]
    target = begin_usage_check(
        job=usage_job,
        datasets=cast(UsageDatasetRepository, editor_backend.datasets),
        now=NOW,
    )
    assert target is not None
    complete_usage_check(
        target=target,
        validation=UsageValidationResult(
            passed=True,
            issues=(),
            input_snapshot=dict(target.check.input_snapshot),
            input_digest=target.check.input_digest,
            summary=dict(target.check.summary),
        ),
        now=NOW,
        datasets=cast(UsageDatasetRepository, editor_backend.datasets),
    )
    artifact = editor_backend.client.post(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/artifacts",
        json={"check_id": check["id"]},
    )

    assert artifact.status_code == 202
    artifact_body = artifact.json()
    normalized_artifact = ArtifactAcceptedView.model_validate(artifact_body).model_dump(mode="json")
    assert artifact_body == normalized_artifact
    assert artifact_body["artifact"]["input_digest"] == check["input_digest"]
    generic_job = editor_backend.client.get(f"{API_PREFIX}/jobs/{artifact.json()['job']['id']}")
    assert generic_job.status_code == 200
    generic_job_body = generic_job.json()
    assert generic_job_body == GenericJobView.model_validate(generic_job_body).model_dump(
        mode="json"
    )


def test_usage_check_api_preserves_no_recovery_hint_for_execution_failure(
    editor_backend: Backend,
) -> None:
    seed_registered_annotation(editor_backend)
    requested = editor_backend.client.post(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/usage-checks",
        json={"kind": "ddm"},
    )
    assert requested.status_code == 202
    requested_body = requested.json()
    job = editor_backend.jobs.jobs[UUID(requested_body["job"]["id"])]
    target = begin_usage_check(
        job=job,
        datasets=cast(UsageDatasetRepository, editor_backend.datasets),
        now=NOW,
    )
    assert target is not None
    fail_usage_check(
        target=target,
        code="USAGE_CHECK_EXECUTION_FAILED",
        detail="用途检查执行失败",
        now=NOW,
        datasets=cast(UsageDatasetRepository, editor_backend.datasets),
    )

    response = editor_backend.client.get(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/usage-checks/{target.check.id}"
    )

    assert response.status_code == 200
    issue = response.json()["issues"][0]
    assert issue["retryable"] is False
    assert issue["recovery_action"] is None


def test_import_only_caller_cannot_read_a_usage_job(editor_backend: Backend) -> None:
    seed_registered_annotation(editor_backend)
    requested = editor_backend.client.post(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/usage-checks",
        json={"kind": "ddm"},
    )
    assert requested.status_code == 202
    job_id = requested.json()["job"]["id"]
    editor_backend.app.dependency_overrides[auth_dependencies.granted_permissions] = lambda: (
        frozenset({Permission.DATASET_IMPORT})
    )

    response = editor_backend.client.get(f"{API_PREFIX}/jobs/{job_id}")

    assert response.status_code == 403
    assert response.json()["error_code"] == "PERMISSION_DENIED"


def test_read_only_caller_cannot_start_a_usage_check(view_backend: Backend) -> None:
    seed_registered_annotation(view_backend)

    response = view_backend.client.post(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/usage-checks",
        json={"kind": "ddm"},
    )

    assert response.status_code == 403
    assert view_backend.datasets.usage_checks == {}
    assert view_backend.jobs.jobs == {}


def test_vlm_candidate_route_freezes_explicit_media_and_requires_revision(
    editor_backend: Backend,
) -> None:
    seed_registered_annotation(editor_backend)
    candidate = editor_backend.client.post(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/vlm-candidates",
        headers={"If-Match": "0"},
        json={
            "kind": "gqa",
            "action_list_revision": 1,
            "records": [
                {
                    "conversations": [
                        {"from": "human", "value": "描述视频"},
                        {"from": "gpt", "value": "(1) 取料"},
                    ],
                    "video": "line-a.mp4",
                    "action_indices": [1],
                }
            ],
            "media": [
                {
                    "key": "line-a.mp4",
                    "member_id": str(MEMBER_ID),
                    "source_object_version_id": "version-1",
                    "source_sha256": "a" * 64,
                }
            ],
        },
    )

    assert candidate.status_code == 201
    candidate_body = candidate.json()
    assert candidate_body == VlmCandidateView.model_validate(candidate_body).model_dump(mode="json")
    candidate_id = candidate_body["id"]
    checked = editor_backend.client.post(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/usage-checks",
        json={"kind": "vlm", "candidate_id": candidate_id},
    )
    assert checked.status_code == 202
    checked_body = checked.json()
    assert checked_body == UsageCheckAcceptedView.model_validate(checked_body).model_dump(
        mode="json"
    )
    assert checked_body["check"]["candidate_id"] == candidate_id


def test_async_dataset_routes_document_their_accepted_job_response(
    import_backend: Backend,
) -> None:
    schema = import_backend.app.openapi()

    confirm_responses = schema["paths"][
        f"{API_PREFIX}/training-datasets/{{dataset_id}}/members/{{member_id}}/confirm"
    ]["post"]["responses"]
    retry_responses = schema["paths"][
        f"{API_PREFIX}/training-datasets/{{dataset_id}}/members/{{member_id}}/retry"
    ]["post"]["responses"]

    assert "202" in confirm_responses
    assert "202" in retry_responses
    assert confirm_responses["202"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ConfirmationView"
    )
    assert retry_responses["202"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/RetryView"
    )
    download = schema["paths"][
        f"{API_PREFIX}/training-datasets/{{dataset_id}}/artifacts/{{artifact_id}}/download"
    ]["get"]
    assert download["responses"]["200"]["content"]["application/json"]["schema"] == {
        "type": "string",
        "format": "binary",
    }
