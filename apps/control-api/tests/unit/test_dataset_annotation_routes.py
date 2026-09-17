"""标注控制面与基座兼容路径的 HTTP seam。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from urllib.parse import quote
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_dataset_annotations import FakeAnnotationJobs, FakeAnnotationStore, context_for, store

from factory_sop.app import API_PREFIX, create_app
from factory_sop.auth.adapters import dependencies as auth_dependencies
from factory_sop.auth.errors import AuthenticationRefusedError, RefusalCode
from factory_sop.auth.model import Session, User, UserStatus
from factory_sop.auth.permissions import Permission
from factory_sop.auth.usecases.sessions import RestoredSession
from factory_sop.dataset.adapters import annotation_routes as annotation_route_module
from factory_sop.dataset.adapters import dependencies as dataset_dependencies
from factory_sop.dataset.model import (
    AnnotationExecution,
    AnnotationExecutionStatus,
    AnnotationMode,
    AnnotationSubmission,
)
from factory_sop.job.api import ApplicationJob
from factory_sop.problem import PROBLEM_MEDIA_TYPE
from factory_sop.settings import Settings

NOW = datetime(2026, 9, 11, 1, 0, tzinfo=UTC)
DATASET_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f101")
MEMBER_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f102")
ACTOR_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f103")
SUBMISSION_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f106")
EXECUTION_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f107")
WIRE_NOW = NOW.isoformat().replace("+00:00", "Z")
EXPIRES_AT = (NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
SOURCE_SHA256 = sha256(b"synthetic source").hexdigest()


class FrozenDateTime:
    @classmethod
    def now(cls, tz: object) -> datetime:
        assert tz is UTC
        return NOW


@pytest.fixture(autouse=True)
def freeze_annotation_route_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(annotation_route_module, "datetime", FrozenDateTime)


def expected_context(
    *,
    token: str,
    preparation_job_id: UUID | None,
    annotation_revision: int,
    preparation_status: str = "pending",
    derived_video_size: int | None = None,
    derived_video_sha256: str | None = None,
    derived_video_duration_seconds: float | None = None,
    initial_timestamps: list[dict[str, object]] | None = None,
    two_operator_mode: bool = False,
    latest_submission: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "context_token": token,
        "dataset_id": str(DATASET_ID),
        "member_id": str(MEMBER_ID),
        "action_list_revision": 1,
        "annotation_revision": annotation_revision,
        "source_object_version_id": "version-1",
        "source_sha256": SOURCE_SHA256,
        "derived_video_size": derived_video_size,
        "derived_video_sha256": derived_video_sha256,
        "derived_video_duration_seconds": derived_video_duration_seconds,
        "preparation_job_id": str(preparation_job_id) if preparation_job_id else None,
        "preparation_status": preparation_status,
        "preparation_failure_code": None,
        "preparation_failure_detail": None,
        "original_filename": "line.mp4",
        "source": "camera-a",
        "duration_seconds": 20.0,
        "actions": ["(1)拿取工件", "(2)安装部件"],
        "video_url": (
            "https://annotation-media.test:8444/annotation/media/videos/"
            f"{quote(token, safe='')}/download"
        ),
        "initial_timestamps": initial_timestamps or [],
        "two_operator_mode": two_operator_mode,
        "expires_at": EXPIRES_AT,
        "latest_submission": latest_submission,
    }


def execution_view(value: AnnotationExecution) -> dict[str, object]:
    return {
        "id": str(value.id),
        "submission_id": str(value.submission_id),
        "generation": value.generation,
        "job_id": str(value.job_id) if value.job_id is not None else None,
        "status": value.status.value,
        "clips": list(value.clips),
        "failure_code": value.failure_code,
        "failure_detail": value.failure_detail,
        "created_at": WIRE_NOW,
        "updated_at": WIRE_NOW,
    }


def job_view(value: ApplicationJob) -> dict[str, object]:
    return {
        "id": str(value.id),
        "job_type": value.job_type.value,
        "status": value.status,
        "member_id": str(value.member_id),
        "attempt_id": str(value.attempt_id),
        "failure_code": value.failure_code,
        "created_at": WIRE_NOW,
        "updated_at": WIRE_NOW,
    }


def submission_view(
    value: AnnotationSubmission,
    execution: AnnotationExecution,
) -> dict[str, object]:
    return {
        "id": str(value.id),
        "dataset_id": str(value.dataset_id),
        "member_id": str(value.member_id),
        "context_id": str(value.context_id),
        "revision": value.revision,
        "action_list_revision": value.action_list_revision,
        "source_object_version_id": value.source_object_version_id,
        "source_sha256": value.source_sha256,
        "idempotency_key": value.idempotency_key,
        "mode": value.mode.value,
        "segments": [
            {
                "start": segment.start,
                "end": segment.end,
                "action_index": segment.action_index,
                "action_description": segment.action_description,
            }
            for segment in value.segments
        ],
        "raw_segments": list(value.raw_segments),
        "created_by": str(value.created_by),
        "created_at": WIRE_NOW,
        "executions": [execution_view(execution)],
    }


def settings() -> Settings:
    return Settings(
        log_level="warning",
        database_host="unused",
        database_port=5432,
        database_name="unused",
        database_user="unused",
        database_password=SecretStr("unused"),
        redis_url=SecretStr("redis://127.0.0.1:1/0"),
        session_idle_timeout_minutes=720,
        session_absolute_lifetime_minutes=43200,
        session_cookie_transport="require_https",
        csrf_secret=SecretStr("annotation-context-secret"),
        annotation_backend_url="http://annotation-backend.test",
        annotation_media_origin="https://annotation-media.test:8444",
        annotation_data_root="/tmp/nvsop-annotation-data",
    )


def actor() -> RestoredSession:
    return RestoredSession(
        session=Session(
            id=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f105"),
            user_id=ACTOR_ID,
            token_fingerprint="unused",
            created_at=NOW,
            last_used_at=NOW,
        ),
        user=User(
            id=ACTOR_ID,
            login_name="annotator",
            display_name="标注员",
            password_hash="unused",  # pragma: allowlist secret
            status=UserStatus.ACTIVE,
        ),
    )


def backend(
    permissions: frozenset[Permission] | None = None,
) -> tuple[TestClient, FakeAnnotationStore, FakeAnnotationJobs]:
    datasets = store()
    payload_digest = sha256(b"synthetic source").hexdigest()
    datasets.members[MEMBER_ID] = replace(
        datasets.members[MEMBER_ID],
        actual_size=len(b"synthetic source"),
        actual_sha256=payload_digest,
    )
    jobs = FakeAnnotationJobs()
    app = create_app(settings())
    app.dependency_overrides[auth_dependencies.authenticated_caller] = actor
    granted = (
        permissions
        if permissions is not None
        else frozenset(
            {
                Permission.DATASET_VIEW,
                Permission.DATASET_EDIT,
                Permission.DATASET_IMPORT,
            }
        )
    )
    app.dependency_overrides[auth_dependencies.granted_permissions] = lambda granted=granted: (
        granted
    )
    app.dependency_overrides[dataset_dependencies.datasets] = lambda: datasets
    app.dependency_overrides[dataset_dependencies.annotation_jobs] = lambda: jobs
    return TestClient(app, base_url="https://testserver"), datasets, jobs


def test_gateway_rejects_anonymous_and_expired_sessions_with_one_problem_shape() -> None:
    for refusal, title in (
        (RefusalCode.AUTHENTICATION_REQUIRED, "请先登录"),
        (RefusalCode.SESSION_INVALID, "会话已失效，请重新登录"),
    ):
        datasets = store()
        app = create_app(settings())

        def refuse(refusal: RefusalCode = refusal) -> None:
            raise AuthenticationRefusedError(refusal)

        app.dependency_overrides[auth_dependencies.authenticated_caller] = refuse
        app.dependency_overrides[dataset_dependencies.datasets] = lambda datasets=datasets: datasets
        with TestClient(app, base_url="https://testserver") as client:
            response = client.get(
                f"{API_PREFIX}/annotation/gateway-authorize",
                headers={
                    "X-Original-URI": f"{API_PREFIX}/training-datasets/{DATASET_ID}/members",
                    "X-Original-Method": "GET",
                },
            )

        assert response.status_code == 401
        assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
        assert response.json() == {
            "type": "about:blank",
            "title": title,
            "status": 401,
            "error_code": refusal.value,
        }


def test_action_context_and_annotation_submission_use_one_authenticated_seam() -> None:
    client, datasets, jobs = backend()

    registered = client.post(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/action-list",
        json={"actions": ["(1)拿取工件", "(2)安装部件"]},
    )
    assert registered.status_code == 201, registered.text
    assert registered.json() == {
        "dataset_id": str(DATASET_ID),
        "revision": 1,
        "actions": ["(1)拿取工件", "(2)安装部件"],
        "created_by": str(ACTOR_ID),
        "created_at": WIRE_NOW,
    }

    context = client.post(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/members/{MEMBER_ID}/annotation-context",
        json={},
    )
    assert context.status_code == 201, context.text
    context_body = context.json()
    token = context_body["context_token"]
    context_row = next(iter(datasets.contexts.values()))
    preparation_job = next(job for job in jobs.jobs.values() if job.attempt_id == context_row.id)
    assert context_body == expected_context(
        token=context_row.token,
        preparation_job_id=preparation_job.id,
        annotation_revision=0,
    )
    media_pending = client.get(
        f"{API_PREFIX}/annotation/media/authorize",
        params={"kind": "video", "resource": token},
    )
    assert media_pending.status_code == 409, media_pending.text
    assert media_pending.json() == {
        "type": "about:blank",
        "title": "标注当前状态不允许该操作",
        "status": 409,
        "error_code": "ANNOTATION_STATE_CONFLICT",
        "detail": "标注媒体仍在准备，请稍后重试",
    }
    context_id = context_row.id
    datasets.contexts[context_id] = replace(
        datasets.contexts[context_id],
        preparation_status="succeeded",
        upstream_data_id="base-dataset",
        upstream_video_id="base-video",
        upstream_video_size=len(b"derived video"),
        upstream_video_sha256=sha256(b"derived video").hexdigest(),
        upstream_video_duration_seconds=20.0,
    )
    media_authorized = client.get(
        f"{API_PREFIX}/annotation/media/authorize",
        params={"kind": "video", "resource": token},
    )
    assert media_authorized.status_code == 204, media_authorized.text
    assert media_authorized.headers["X-Annotation-Upstream-Video-ID"] == "base-video"
    stale_context_response = client.post(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/members/{MEMBER_ID}/annotation-context",
        json={},
    )
    assert stale_context_response.status_code == 201, stale_context_response.text
    stale_context = stale_context_response.json()
    stale_context_row = next(
        row for row in datasets.contexts.values() if row.token == stale_context["context_token"]
    )
    stale_context_job = next(
        job for job in jobs.jobs.values() if job.attempt_id == stale_context_row.id
    )
    assert stale_context == expected_context(
        token=stale_context_row.token,
        preparation_job_id=stale_context_job.id,
        annotation_revision=0,
        latest_submission=None,
    )
    # 测试 token 只用于定位，实际准备状态由中心记录决定。
    datasets.contexts[stale_context_row.id] = replace(
        stale_context_row,
        preparation_status="succeeded",
        upstream_data_id="base-dataset",
        upstream_video_id="base-video",
        upstream_video_size=len(b"derived video"),
        upstream_video_sha256=sha256(b"derived video").hexdigest(),
        upstream_video_duration_seconds=20.0,
    )

    submission = client.post(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/members/{MEMBER_ID}/annotations",
        headers={"Idempotency-Key": "annotation-route-1", "If-Match": "0"},
        json={
            "context_token": token,
            "mode": "single_operator",
            "segments": [
                {
                    "start": 0.0,
                    "end": 3.5,
                    "action_index": 0,
                    "action_description": "客户端可以保留原始描述",
                }
            ],
        },
    )
    assert submission.status_code == 202, submission.text
    submission_body = submission.json()
    submission_row = next(iter(datasets.submissions.values()))
    execution_row = next(iter(datasets.executions.values()))
    annotation_job = next(job for job in jobs.jobs.values() if job.attempt_id == execution_row.id)
    expected_submission = submission_view(submission_row, execution_row)
    assert submission_body == {
        "submission": expected_submission,
        "execution": execution_view(execution_row),
        "job": job_view(annotation_job),
    }
    assert len(datasets.submissions) == 1
    assert len(jobs.jobs) == 3

    reopened = client.post(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/members/{MEMBER_ID}/annotation-context",
        json={},
    )
    assert reopened.status_code == 201, reopened.text
    reopened_body = reopened.json()
    reopened_row = next(
        row for row in datasets.contexts.values() if row.token == reopened_body["context_token"]
    )
    reopened_job = next(job for job in jobs.jobs.values() if job.attempt_id == reopened_row.id)
    assert reopened_body == expected_context(
        token=reopened_row.token,
        preparation_job_id=reopened_job.id,
        annotation_revision=1,
        initial_timestamps=[
            {
                "start": 0.0,
                "end": 3.5,
                "action_index": 0,
                "action_description": "(1)拿取工件",
            }
        ],
        latest_submission=expected_submission,
    )

    stale = client.post(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/members/{MEMBER_ID}/annotations",
        headers={"Idempotency-Key": "annotation-route-stale", "If-Match": "0"},
        json={
            "context_token": stale_context["context_token"],
            "mode": "single_operator",
            "segments": [{"start": 0.0, "end": 3.5, "action_index": 0}],
        },
    )
    assert stale.status_code == 409
    assert stale.json() == {
        "type": "about:blank",
        "title": "标注修订号已变化（STALE_REVISION）",
        "status": 409,
        "error_code": "STALE_REVISION",
        "detail": "标注已被其他用户修改，请重新读取后再提交",
    }

    replay = client.post(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/members/{MEMBER_ID}/annotations",
        headers={"Idempotency-Key": "annotation-route-1", "If-Match": "0"},
        json={
            "context_token": token,
            "mode": "single_operator",
            "segments": [
                {
                    "start": 0.0,
                    "end": 3.5,
                    "action_index": 0,
                    "action_description": "客户端可以保留原始描述",
                }
            ],
        },
    )
    assert replay.status_code == 202
    assert replay.json() == submission_body


def test_media_gateway_maps_an_unknown_resource_to_an_auth_request_denial() -> None:
    client, _, _ = backend()

    response = client.get(
        f"{API_PREFIX}/annotation/media/gateway-authorize",
        params={"kind": "video", "resource": "not-a-context"},
    )

    assert response.status_code == 403
    assert response.content == b""


def test_gateway_rejects_an_existing_dataset_without_the_required_permission() -> None:
    for method, permissions in (
        ("GET", frozenset()),
        ("POST", frozenset({Permission.DATASET_VIEW})),
    ):
        client, _, _ = backend(permissions=permissions)
        response = client.get(
            f"{API_PREFIX}/annotation/gateway-authorize",
            headers={
                "X-Original-URI": f"{API_PREFIX}/training-datasets/{DATASET_ID}/members",
                "X-Original-Method": method,
            },
        )
        assert response.status_code == 403
        assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
        assert response.json() == {
            "type": "about:blank",
            "title": "没有执行该操作的权限",
            "status": 403,
            "error_code": "PERMISSION_DENIED",
        }


@pytest.mark.parametrize(
    "path",
    [
        f"{API_PREFIX}/training-datasets",
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/members",
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/members/{MEMBER_ID}/confirm",
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/members/{MEMBER_ID}/retry",
    ],
)
def test_gateway_preserves_dataset_import_permission_for_upload_requests(path: str) -> None:
    client, _, _ = backend(permissions=frozenset({Permission.DATASET_IMPORT}))

    response = client.get(
        f"{API_PREFIX}/annotation/gateway-authorize",
        headers={
            "X-Original-URI": path,
            "X-Original-Method": "POST",
        },
    )

    assert response.status_code == 204, response.text


def test_gateway_does_not_assign_dataset_permissions_to_other_control_plane_writes() -> None:
    client, _, _ = backend(permissions=frozenset({Permission.TEMPLATE_DRAFT_EDIT}))

    response = client.get(
        f"{API_PREFIX}/annotation/gateway-authorize",
        headers={
            "X-Original-URI": f"{API_PREFIX}/templates/drafts/1",
            "X-Original-Method": "POST",
        },
    )

    assert response.status_code == 204, response.text


def test_gateway_authorization_checks_dataset_and_annotation_resource_ownership() -> None:
    client, datasets, _ = backend()

    dataset_allowed = client.get(
        f"{API_PREFIX}/annotation/gateway-authorize",
        headers={
            "X-Original-URI": f"{API_PREFIX}/training-datasets/{DATASET_ID}/members",
            "X-Original-Method": "GET",
        },
    )
    assert dataset_allowed.status_code == 204, dataset_allowed.text

    dataset_denied = client.get(
        f"{API_PREFIX}/annotation/gateway-authorize",
        headers={
            "X-Original-URI": f"{API_PREFIX}/training-datasets/{UUID(int=999)}/members",
            "X-Original-Method": "GET",
        },
    )
    assert dataset_denied.status_code == 403, dataset_denied.text

    member_denied = client.get(
        f"{API_PREFIX}/annotation/gateway-authorize",
        headers={
            "X-Original-URI": (
                f"{API_PREFIX}/training-datasets/{DATASET_ID}/members/{UUID(int=1000)}"
            ),
            "X-Original-Method": "GET",
        },
    )
    assert member_denied.status_code == 403, member_denied.text

    context_denied = client.get(
        f"{API_PREFIX}/annotation/gateway-authorize",
        headers={
            "X-Original-URI": "/api/annotation/api/v1/videos/not-a-context/download",
            "X-Original-Method": "GET",
        },
    )
    assert context_denied.status_code == 403, context_denied.text

    context_for(datasets)
    context = next(iter(datasets.contexts.values()))
    datasets.add_annotation_submission(
        AnnotationSubmission(
            id=SUBMISSION_ID,
            dataset_id=DATASET_ID,
            member_id=MEMBER_ID,
            context_id=context.id,
            revision=1,
            action_list_revision=1,
            source_object_version_id="version-1",
            source_sha256="a" * 64,
            idempotency_key="gateway-submission",
            request_digest="b" * 64,
            mode=AnnotationMode.SINGLE_OPERATOR,
            segments=(),
            raw_segments=(),
            created_by=ACTOR_ID,
            created_at=NOW,
        )
    )

    datasets.add_annotation_execution(
        AnnotationExecution(
            id=EXECUTION_ID,
            submission_id=SUBMISSION_ID,
            generation=1,
            job_id=None,
            status=AnnotationExecutionStatus.SUCCEEDED,
            clips=({"id": "clip-1"},),
            failure_code=None,
            failure_detail=None,
            created_at=NOW,
            updated_at=NOW,
            upstream_data_id="base-dataset",
            upstream_video_id="base-video",
        )
    )

    submission_allowed = client.get(
        f"{API_PREFIX}/annotation/gateway-authorize",
        headers={
            "X-Original-URI": (
                "/api/annotation/api/v1/annotation-submissions/"
                f"{SUBMISSION_ID}/executions/{EXECUTION_ID}/clips/0"
            ),
            "X-Original-Method": "GET",
        },
    )
    assert submission_allowed.status_code == 204, submission_allowed.text

    product_submission_allowed = client.get(
        f"{API_PREFIX}/annotation/gateway-authorize",
        headers={
            "X-Original-URI": (
                f"{API_PREFIX}/training-datasets/{DATASET_ID}/members/{MEMBER_ID}/"
                f"annotations/{SUBMISSION_ID}"
            ),
            "X-Original-Method": "GET",
        },
    )
    assert product_submission_allowed.status_code == 204, product_submission_allowed.text


def test_unsupported_base_operations_are_rejected_by_the_compatibility_adapter() -> None:
    client, _, _ = backend()

    response = client.post("/api/annotation/api/v1/actions/upload", content=b"not allowed")

    assert response.status_code == 403, response.text
    assert response.json() == {
        "type": "about:blank",
        "title": "标注操作未开放",
        "status": 403,
        "error_code": "ANNOTATION_OPERATION_NOT_ALLOWED",
        "detail": "该标注基座操作未通过产品数据集接口开放",
    }


def test_preparing_a_context_rejects_a_changed_registered_source() -> None:
    client, datasets, jobs = backend()
    action_list = client.post(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/action-list",
        json={"actions": ["(1)拿取工件", "(2)安装部件"]},
    )
    assert action_list.status_code == 201, action_list.text
    assert action_list.json() == {
        "dataset_id": str(DATASET_ID),
        "revision": 1,
        "actions": ["(1)拿取工件", "(2)安装部件"],
        "created_by": str(ACTOR_ID),
        "created_at": WIRE_NOW,
    }
    context_response = client.post(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/members/{MEMBER_ID}/annotation-context",
        json={},
    )
    assert context_response.status_code == 201, context_response.text
    context = context_response.json()
    context_row = next(iter(datasets.contexts.values()))
    preparation_job = next(job for job in jobs.jobs.values() if job.attempt_id == context_row.id)
    assert context == expected_context(
        token=context_row.token,
        preparation_job_id=preparation_job.id,
        annotation_revision=0,
    )
    datasets.members[MEMBER_ID] = replace(
        datasets.members[MEMBER_ID],
        actual_sha256="c" * 64,
    )
    context_row = next(iter(datasets.contexts.values()))
    datasets.contexts[context_row.id] = replace(
        context_row,
        preparation_status="failed",
        preparation_failure_code="ANNOTATION_CONTEXT_INVALID",
        preparation_failure_detail="源视频在准备期间发生变化",
    )

    response = client.get(f"/api/annotation/api/v1/videos/{context['context_token']}")

    assert response.status_code == 404
    assert response.json() == {
        "type": "about:blank",
        "title": "标注上下文无效或已过期",
        "status": 404,
        "error_code": "ANNOTATION_CONTEXT_INVALID",
        "detail": "视频对象已变化，请重新打开标注上下文",
    }


def test_legacy_split_is_async_and_video_access_redirects_to_separate_media_origin() -> None:
    client, datasets, jobs = backend()
    action_list = client.post(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/action-list",
        json={"actions": ["(1)拿取工件", "(2)安装部件"]},
    )
    assert action_list.status_code == 201, action_list.text
    assert action_list.json() == {
        "dataset_id": str(DATASET_ID),
        "revision": 1,
        "actions": ["(1)拿取工件", "(2)安装部件"],
        "created_by": str(ACTOR_ID),
        "created_at": WIRE_NOW,
    }
    context_response = client.post(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/members/{MEMBER_ID}/annotation-context",
        json={},
    )
    assert context_response.status_code == 201, context_response.text
    context = context_response.json()
    context_row = next(iter(datasets.contexts.values()))
    preparation_job = next(job for job in jobs.jobs.values() if job.attempt_id == context_row.id)
    assert context == expected_context(
        token=context_row.token,
        preparation_job_id=preparation_job.id,
        annotation_revision=0,
    )
    token = context["context_token"]
    datasets.contexts[context_row.id] = replace(
        context_row,
        preparation_status="succeeded",
        upstream_data_id="base-dataset",
        upstream_video_id="base-video",
        upstream_video_size=len(b"derived video"),
        upstream_video_sha256=sha256(b"derived video").hexdigest(),
        upstream_video_duration_seconds=20.0,
    )

    split = client.post(
        f"/api/annotation/api/v1/videos/{token}/split",
        json={
            "timestamps": [
                {
                    "start": 0.0,
                    "end": 3.5,
                    "actionIndex": 0,
                    "actionDescription": "(1)拿取工件",
                }
            ],
            "twoOperatorMode": False,
        },
    )
    assert split.status_code == 202, split.text
    split_body = split.json()
    split_submission = next(iter(datasets.submissions.values()))
    split_execution = next(iter(datasets.executions.values()))
    split_job = next(job for job in jobs.jobs.values() if job.attempt_id == split_execution.id)
    assert split_body == {
        "status": "accepted",
        "submission_id": str(split_submission.id),
        "execution_id": str(split_execution.id),
        "job_id": str(split_job.id),
        "generation": split_execution.generation,
        "clips": [],
        "poll_url": (
            "/api/annotation/api/v1/annotation-submissions/"
            f"{split_submission.id}/executions/{split_execution.id}"
        ),
    }
    execution_id = split_execution.id
    pending = client.get(split_body["poll_url"])
    assert pending.status_code == 200, pending.text
    assert pending.json() == {
        "status": "pending",
        "clips": [],
        "failure_code": None,
        "failure_detail": None,
    }
    datasets.executions[execution_id] = replace(
        datasets.executions[execution_id],
        status=AnnotationExecutionStatus.SUCCEEDED,
        clips=({"id": "base-clip", "filename": "clip.mp4"},),
        upstream_video_id="base-video",
    )

    completed = client.get(split_body["poll_url"])
    assert completed.status_code == 200, completed.text
    assert completed.json() == {
        "status": "succeeded",
        "clips": [{"id": "base-clip", "filename": "clip.mp4"}],
        "failure_code": None,
        "failure_detail": None,
    }

    metadata = client.get(f"/api/annotation/api/v1/videos/{token}")
    assert metadata.status_code == 200, metadata.text
    assert metadata.json() == {
        "id": token,
        "filename": "line.mp4",
        "file_path": "annotation-resource",
        "file_size": len(b"derived video"),
        "upload_time": WIRE_NOW,
        "mime_type": "video/mp4",
        "action_list_revision": 1,
    }
    assert datasets.contexts[next(iter(datasets.contexts))].upstream_video_id == "base-video"
    refreshed_context = client.get(f"/api/v1/annotation-contexts/{token}")
    assert refreshed_context.status_code == 200, refreshed_context.text
    refreshed_execution = datasets.executions[execution_id]
    assert refreshed_context.json() == expected_context(
        token=context_row.token,
        preparation_job_id=preparation_job.id,
        annotation_revision=0,
        preparation_status="succeeded",
        derived_video_size=len(b"derived video"),
        derived_video_sha256=sha256(b"derived video").hexdigest(),
        derived_video_duration_seconds=20.0,
        initial_timestamps=[
            {
                "start": 0.0,
                "end": 3.5,
                "action_index": 0,
                "action_description": "(1)拿取工件",
            }
        ],
        latest_submission=submission_view(split_submission, refreshed_execution),
    )

    video_authorized = client.get(
        f"{API_PREFIX}/annotation/media/authorize",
        params={"kind": "video", "resource": token},
    )
    assert video_authorized.status_code == 204
    assert video_authorized.headers["X-Annotation-Upstream-Video-ID"] == "base-video"

    clip_authorized = client.get(
        f"{API_PREFIX}/annotation/media/authorize",
        params={
            "kind": "clip",
            "resource": f"{split.json()['submission_id']}:{execution_id}:0",
        },
    )
    assert clip_authorized.status_code == 204
    assert clip_authorized.headers["X-Annotation-Upstream-Clip-ID"] == "base-clip"

    archive_authorized = client.get(
        f"{API_PREFIX}/annotation/media/authorize",
        params={
            "kind": "archive",
            "resource": f"{split.json()['submission_id']}:{execution_id}",
        },
    )
    assert archive_authorized.status_code == 204
    assert archive_authorized.headers["X-Annotation-Upstream-Video-ID"] == "base-video"

    invalid_clip = client.get(
        f"{API_PREFIX}/annotation/media/authorize",
        params={
            "kind": "clip",
            "resource": f"{split.json()['submission_id']}:{execution_id}:1",
        },
    )
    assert invalid_clip.status_code == 404
    assert invalid_clip.json() == {
        "type": "about:blank",
        "title": "请求的资源归属不匹配",
        "status": 404,
        "error_code": "RESOURCE_MISMATCH",
        "detail": "RESOURCE_MISMATCH",
    }

    redirect = client.get(
        f"/api/annotation/api/v1/videos/{token}/download",
        follow_redirects=False,
    )
    assert redirect.status_code == 302
    assert redirect.headers["location"].startswith(
        "https://annotation-media.test:8444/annotation/media/videos/"
    )

    clip_redirect = client.get(
        f"/api/annotation/api/v1/annotation-submissions/{split.json()['submission_id']}"
        f"/executions/{execution_id}/clips/0/download",
        follow_redirects=False,
    )
    assert clip_redirect.status_code == 302
    assert clip_redirect.headers["location"] == (
        "https://annotation-media.test:8444/annotation/media/clips/"
        f"{split.json()['submission_id']}/{execution_id}/0/download"
    )

    archive_redirect = client.get(
        f"/api/annotation/api/v1/annotation-submissions/{split.json()['submission_id']}"
        f"/executions/{execution_id}/download-all",
        follow_redirects=False,
    )
    assert archive_redirect.status_code == 302
    assert "/annotation/media/archives/" in archive_redirect.headers["location"]

    before_retry = len(datasets.executions)
    wrong_scope_retry = client.post(
        f"{API_PREFIX}/training-datasets/019937d8-0d10-7b31-8d2d-4e60c8f4f199"
        f"/members/{MEMBER_ID}/annotations/{split.json()['submission_id']}/retry",
    )
    assert wrong_scope_retry.status_code == 404
    assert wrong_scope_retry.json() == {
        "type": "about:blank",
        "title": "请求的资源归属不匹配",
        "status": 404,
        "error_code": "RESOURCE_MISMATCH",
        "detail": "RESOURCE_MISMATCH",
    }
    assert len(datasets.executions) == before_retry
