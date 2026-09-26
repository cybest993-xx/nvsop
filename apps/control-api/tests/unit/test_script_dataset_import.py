"""脚本通过真实 control-api ASGI seam 调用数据集正式入口的契约测试。"""

# center-unit 的工作目录不包含仓库根目录；只有本测试需要导入脚本。
# ruff: noqa: E402

from __future__ import annotations

import json
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

_REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPOSITORY_ROOT))

import scripts.import_training_dataset as dataset_script
from scripts.import_training_dataset import (
    CSRF_COOKIE,
    CSRF_HEADER,
    ControlPlaneClient,
    DatasetImportError,
    HttpResponse,
    HttpTransport,
    import_training_dataset,
)
from test_dataset_usecases import FakeDatasets, FakeJobs, FakeStorage

from factory_sop.app import create_app
from factory_sop.auth.adapters import dependencies as auth_dependencies
from factory_sop.auth.csrf import csrf_token_for
from factory_sop.auth.model import Session, User, UserStatus
from factory_sop.auth.permissions import Permission
from factory_sop.auth.tokens import SessionToken
from factory_sop.auth.usecases.sessions import RestoredSession
from factory_sop.dataset.adapters import dependencies as dataset_dependencies
from factory_sop.job.adapters import dependencies as job_dependencies
from factory_sop.job.api import ApplicationJob
from factory_sop.settings import Settings

NOW = datetime(2026, 9, 9, 1, 0, tzinfo=UTC)
ACTOR_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f701")
SESSION_COOKIE_VALUE = "script-session-token"  # pragma: allowlist secret
CSRF_SECRET = "csrf-secret"  # pragma: allowlist secret


@dataclass(frozen=True, slots=True)
class RequestRecord:
    """控制面收到的一次 JSON 请求。"""

    method: str
    path: str
    headers: dict[str, str]
    body: dict[str, object] | None


@dataclass(frozen=True, slots=True)
class DirectUploadRecord:
    """发往中心上传入口的一次流式上传。"""

    url: str
    method: str
    headers: dict[str, str]
    path: Path
    content: bytes


@dataclass
class FakeJobRepository:
    """把同一个任务 seam 接给正式 job HTTP 路由。"""

    jobs: FakeJobs

    def by_id(self, job_id: UUID) -> ApplicationJob | None:
        return self.jobs.jobs.get(job_id)


class AsgiTransport:
    """用真实 FastAPI 路由承载控制面，用独立上传 seam 承载视频。"""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.control_requests: list[RequestRecord] = []
        self.direct_uploads: list[DirectUploadRecord] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> HttpResponse:
        response = self.client.request(method, url, headers=dict(headers), content=body)
        parsed_body: dict[str, object] | None = None
        if body is not None:
            parsed = json.loads(body.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise AssertionError("control-plane body must be an object")
            parsed_body = cast(dict[str, object], parsed)
        self.control_requests.append(
            RequestRecord(
                method=method,
                path=urlsplit(url).path,
                headers={key.lower(): value for key, value in headers.items()},
                body=parsed_body,
            )
        )
        cookies = SimpleCookie()
        for value in response.headers.get_list("set-cookie"):
            cookies.load(value)
        return HttpResponse(
            status=response.status_code,
            headers={key.lower(): value for key, value in response.headers.items()},
            body=response.content,
            cookies={name: morsel.value for name, morsel in cookies.items()},
        )

    def upload_file(
        self,
        url: str,
        *,
        method: str,
        headers: Mapping[str, str],
        path: Path,
    ) -> HttpResponse:
        self.direct_uploads.append(
            DirectUploadRecord(
                url=url,
                method=method,
                headers={key.lower(): value for key, value in headers.items()},
                path=path,
                content=path.read_bytes(),
            )
        )
        return HttpResponse(status=204, headers={}, body=b"", cookies={})


@dataclass
class PollingTransport:
    """为脚本任务轮询提供连续的正式 HTTP 响应。"""

    responses: tuple[dict[str, object], ...]
    reads: int = 0

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> HttpResponse:
        del url, headers, body
        assert method == "GET"
        response = self.responses[min(self.reads, len(self.responses) - 1)]
        self.reads += 1
        return HttpResponse(
            status=200,
            headers={},
            body=json.dumps(response).encode("utf-8"),
            cookies={},
        )

    def upload_file(
        self,
        url: str,
        *,
        method: str,
        headers: Mapping[str, str],
        path: Path,
    ) -> HttpResponse:
        del url, method, headers, path
        raise AssertionError("任务轮询不应上传文件")


@dataclass
class FakeClock:
    now: float = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 1.0)


@dataclass
class ScriptBackend:
    """正式 HTTP 路由 over 内存领域 seam 的最小组合根。"""

    permissions: frozenset[Permission]
    datasets: FakeDatasets
    storage: FakeStorage
    jobs: FakeJobs
    app: FastAPI
    client: TestClient
    transport: AsgiTransport

    @classmethod
    def build(cls, permissions: frozenset[Permission]) -> ScriptBackend:
        datasets = FakeDatasets()
        storage = FakeStorage()
        jobs = FakeJobs()
        app = create_app(_settings())
        app.dependency_overrides[auth_dependencies.authenticated_caller] = _caller
        app.dependency_overrides[auth_dependencies.granted_permissions] = lambda: permissions
        app.dependency_overrides[dataset_dependencies.datasets] = lambda: datasets
        app.dependency_overrides[dataset_dependencies.storage] = lambda: storage
        app.dependency_overrides[dataset_dependencies.jobs] = lambda: jobs
        app.dependency_overrides[job_dependencies.job_repository] = lambda: FakeJobRepository(jobs)
        client = TestClient(app, base_url="https://testserver")
        transport = AsgiTransport(client)
        return cls(permissions, datasets, storage, jobs, app, client, transport)


@pytest.fixture
def backend() -> Iterator[ScriptBackend]:
    value = ScriptBackend.build(frozenset({Permission.DATASET_IMPORT, Permission.DATASET_VIEW}))
    try:
        yield value
    finally:
        value.client.close()


def _settings() -> Settings:
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
        csrf_secret=SecretStr(CSRF_SECRET),
        redis_url=SecretStr("redis://127.0.0.1:1/0"),
    )


def _caller() -> RestoredSession:
    return RestoredSession(
        session=Session(
            id=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f702"),
            user_id=ACTOR_ID,
            token_fingerprint="script-session-fingerprint",
            created_at=NOW,
            last_used_at=NOW,
        ),
        user=User(
            id=ACTOR_ID,
            login_name="script-importer",
            display_name="脚本导入测试",
            password_hash="unused",  # pragma: allowlist secret
            status=UserStatus.ACTIVE,
        ),
    )


def _client(transport: HttpTransport) -> ControlPlaneClient:
    csrf = csrf_token_for(
        SessionToken(value=SESSION_COOKIE_VALUE),
        secret=CSRF_SECRET,
    )
    return ControlPlaneClient(
        transport,
        base_url="https://testserver",
        session_cookie=SESSION_COOKIE_VALUE,
        csrf_token=csrf,
    )


def test_control_plane_polls_known_states_and_preserves_unknown_state() -> None:
    transport = PollingTransport(
        responses=(
            {"id": "job-1", "status": "pending"},
            {"id": "job-1", "status": "running"},
            {"id": "job-1", "status": "succeeded"},
        )
    )
    clock = FakeClock()
    result = _client(transport).wait_for_job(
        job_id="job-1",
        poll_interval_seconds=1,
        poll_timeout_seconds=10,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert result == {"id": "job-1", "status": "succeeded"}
    assert transport.reads == 3

    unknown_transport = PollingTransport(
        responses=({"id": "job-2", "status": "future_state", "failure_code": "NEW_CODE"},)
    )
    unknown = _client(unknown_transport).wait_for_job(
        job_id="job-2",
        poll_interval_seconds=1,
        poll_timeout_seconds=10,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert unknown == {"id": "job-2", "status": "future_state", "failure_code": "NEW_CODE"}
    assert unknown_transport.reads == 1


@dataclass
class StubImportClient:
    """为脚本公开导入用例提供一个只返回固定任务状态的客户端。"""

    job: dict[str, object]

    def create_dataset(self, *, name: str) -> dict[str, object]:
        return {"id": "dataset-1", "name": name}

    def request_video_upload(
        self,
        *,
        dataset_id: str,
        declaration: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]:
        del dataset_id, declaration, idempotency_key
        return {
            "member": {"id": "member-1"},
            "attempt": {"id": "attempt-1"},
            "upload": {
                "method": "PUT",
                "url": (
                    "/api/v1/training-datasets/dataset-1/members/member-1"
                    "/attempts/attempt-1/content"
                ),
                "fields": {},
            },
        }

    def upload_file(self, *, instructions: dict[str, object], path: Path) -> HttpResponse:
        del instructions, path
        return HttpResponse(status=204, headers={}, body=b"", cookies={})

    def confirm_video_upload(
        self,
        *,
        dataset_id: str,
        member_id: str,
        attempt_id: str,
    ) -> dict[str, object]:
        del dataset_id, member_id, attempt_id
        return {"job": self.job}

    def wait_for_job(
        self,
        *,
        job_id: str,
        poll_interval_seconds: float,
        poll_timeout_seconds: float,
    ) -> dict[str, object]:
        del job_id, poll_interval_seconds, poll_timeout_seconds
        return self.job


@pytest.mark.parametrize(
    "job",
    [
        {"id": "job-1", "status": "failed", "failure_code": "SHA256_MISMATCH"},
        {"id": "job-2", "status": "future_state"},
    ],
)
def test_import_rejects_non_successful_validation_job(
    job: dict[str, object],
    tmp_path: Path,
) -> None:
    video = tmp_path / "line-1.mp4"
    video.write_bytes(b"synthetic video")

    with pytest.raises(DatasetImportError, match="视频校验未成功"):
        import_training_dataset(
            cast(ControlPlaneClient, StubImportClient(job)),
            dataset_name="脚本导入",
            source="camera-A12",
            videos=(video,),
        )


def test_cli_does_not_report_success_while_validation_is_still_running(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    video = tmp_path / "line-1.mp4"
    video.write_bytes(b"synthetic video")

    exit_code = dataset_script.main(
        [
            "--base-url",
            "https://center.example",
            "--dataset-name",
            "脚本导入",
            "--source",
            "camera-A12",
            "--session-cookie",
            "cookie",
            "--csrf-token",
            "csrf",
            "--job-poll-timeout",
            "0",
            str(video),
        ],
        client=cast(
            ControlPlaneClient,
            StubImportClient({"id": "job-1", "status": "pending"}),
        ),
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "dataset_id": "dataset-1",
        "videos": [
            {
                "filename": "line-1.mp4",
                "member_id": "member-1",
                "attempt_id": "attempt-1",
                "job_id": "job-1",
                "job_status": "pending",
            }
        ],
    }
    assert "导入未完成" in captured.err


def test_script_uses_formal_api_for_each_video_and_never_sends_media_to_fastapi(
    backend: ScriptBackend,
    tmp_path: Path,
) -> None:
    first = tmp_path / "line-1.mp4"
    second = tmp_path / "line-2.mp4"
    first.write_bytes(b"first synthetic video")
    second.write_bytes(b"second synthetic video")

    result = import_training_dataset(
        _client(backend.transport),
        dataset_name="脚本训练集",
        source="camera-A12",
        videos=(first, second),
        poll_timeout_seconds=0,
    )

    dataset_id = str(result.dataset["id"])
    first_member = str(result.videos[0].member["id"])
    first_attempt = str(result.videos[0].attempt["id"])
    second_member = str(result.videos[1].member["id"])
    second_attempt = str(result.videos[1].attempt["id"])
    first_job = str(result.videos[0].job["id"])
    second_job = str(result.videos[1].job["id"])
    auth_headers = {
        "accept": "application/json",
        "cookie": (
            f"sop_session={SESSION_COOKIE_VALUE}; "
            f"{CSRF_COOKIE}={backend.transport.control_requests[0].headers['x-csrf-token']}"
        ),
        CSRF_HEADER: backend.transport.control_requests[0].headers[CSRF_HEADER],
    }

    records = backend.transport.control_requests
    assert [record.path for record in records] == [
        "/api/v1/training-datasets",
        "/api/v1/training-datasets/" + dataset_id + "/members",
        "/api/v1/training-datasets/" + dataset_id + "/members/" + first_member + "/confirm",
        "/api/v1/jobs/" + first_job,
        "/api/v1/training-datasets/" + dataset_id + "/members",
        "/api/v1/training-datasets/" + dataset_id + "/members/" + second_member + "/confirm",
        "/api/v1/jobs/" + second_job,
    ]
    assert records[0].method == "POST"
    assert records[0].body == {"name": "脚本训练集"}
    assert records[0].headers == {**auth_headers, "content-type": "application/json"}
    assert records[1].method == "POST"
    assert records[1].body == {
        "original_filename": "line-1.mp4",
        "source": "camera-A12",
        "declared_size": len(first.read_bytes()),
    }
    assert records[1].headers == {
        **auth_headers,
        "content-type": "application/json",
        "idempotency-key": f"dataset-{dataset_id}-video-1",
    }
    assert records[2].method == "POST"
    assert records[2].body == {"attempt_id": first_attempt}
    assert records[2].headers == {**auth_headers, "content-type": "application/json"}
    assert records[3].method == "GET"
    assert records[3].body is None
    assert records[3].headers == {
        "accept": "application/json",
        "cookie": auth_headers["cookie"],
    }
    assert records[4].body == {
        "original_filename": "line-2.mp4",
        "source": "camera-A12",
        "declared_size": len(second.read_bytes()),
    }
    assert records[4].headers == {
        **auth_headers,
        "content-type": "application/json",
        "idempotency-key": f"dataset-{dataset_id}-video-2",
    }
    assert records[5].body == {"attempt_id": second_attempt}
    assert records[6].method == "GET"
    assert records[6].body is None
    assert records[6].headers == {
        "accept": "application/json",
        "cookie": auth_headers["cookie"],
    }
    assert all("Authorization".lower() not in record.headers for record in records)
    assert [upload.path for upload in backend.transport.direct_uploads] == [first, second]
    assert [upload.content for upload in backend.transport.direct_uploads] == [
        first.read_bytes(),
        second.read_bytes(),
    ]
    assert all(
        "/api/v1/training-datasets/" in upload.url for upload in backend.transport.direct_uploads
    )
    assert all(
        upload.headers["cookie"] == auth_headers["cookie"]
        and upload.headers[CSRF_HEADER] == auth_headers[CSRF_HEADER]
        for upload in backend.transport.direct_uploads
    )


def test_script_can_poll_with_dataset_import_permission_only(tmp_path: Path) -> None:
    backend = ScriptBackend.build(frozenset({Permission.DATASET_IMPORT}))
    try:
        video = tmp_path / "import-only.mp4"
        video.write_bytes(b"video for import-only caller")

        result = import_training_dataset(
            _client(backend.transport),
            dataset_name="仅导入权限训练集",
            source="camera-A12",
            videos=(video,),
            poll_timeout_seconds=0,
        )

        assert result.videos[0].job["status"] == "pending"
    finally:
        backend.client.close()


def test_script_is_refused_by_the_real_route_without_dataset_import_permission(
    tmp_path: Path,
) -> None:
    backend = ScriptBackend.build(frozenset({Permission.DATASET_VIEW}))
    try:
        video = tmp_path / "not-uploaded.mp4"
        video.write_bytes(b"video that must not be sent")

        with pytest.raises(DatasetImportError, match="HTTP 403"):
            import_training_dataset(
                _client(backend.transport),
                dataset_name="越权训练集",
                source="camera-A12",
                videos=(video,),
            )

        assert len(backend.transport.control_requests) == 1
        assert backend.transport.control_requests[0].path == "/api/v1/training-datasets"
        assert backend.transport.direct_uploads == []
    finally:
        backend.client.close()
