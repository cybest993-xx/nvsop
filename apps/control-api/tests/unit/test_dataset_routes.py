"""训练数据集 HTTP seam：权限、直传说明和任务幂等。"""

from __future__ import annotations

from datetime import UTC, datetime
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
from factory_sop.dataset.model import (
    AttemptStatus,
    DatasetMember,
    MemberStatus,
    TrainingDataset,
    UploadAttempt,
)
from factory_sop.settings import Settings

NOW = datetime(2026, 9, 9, 1, 0, tzinfo=UTC)
ACTOR_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f201")
DATASET_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f202")
MEMBER_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f203")
ATTEMPT_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f204")


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
        self.client = TestClient(self.app, base_url="https://testserver")


@pytest.fixture
def import_backend() -> Backend:
    return Backend(frozenset({Permission.DATASET_IMPORT}))


@pytest.fixture
def view_backend() -> Backend:
    return Backend(frozenset({Permission.DATASET_VIEW}))


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
    assert view_backend.storage.requests == []


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
    assert requested.json()["upload"]["url"]
    member_id = requested.json()["member"]["id"]
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
    assert import_backend.storage.requests == []


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
