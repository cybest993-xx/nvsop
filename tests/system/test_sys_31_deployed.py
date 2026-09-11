"""SYS-31 — 统一网关、真实媒体端点和真实标注部署的组合证据。"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx2
import pytest
from conftest import csrf_header

API_PREFIX = "/api/v1"
DATASETS = f"{API_PREFIX}/training-datasets"


@dataclass
class DeployedAnnotation:
    client: httpx2.Client
    csrf: dict[str, str]
    dataset_id: UUID
    member_id: UUID
    context: dict[str, object]


def _configured_environment() -> tuple[str, str, Path, Path] | None:
    base_url = os.getenv("NVSOP_SYS31_GATEWAY_URL")
    login_name = os.getenv("NVSOP_SYS31_LOGIN_NAME")
    password_file = os.getenv("NVSOP_SYS31_PASSWORD_FILE")
    if not base_url or not login_name or not password_file:
        return None
    parsed_url = urlsplit(base_url)
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        raise AssertionError("SYS-31 gateway evidence requires an HTTPS gateway URL")
    ca_file = os.getenv("NVSOP_SYS31_CA_FILE")
    if not ca_file:
        return None
    password_path = Path(password_file)
    ca_path = Path(ca_file)
    if not password_path.is_file():
        raise AssertionError(f"SYS-31 password file does not exist: {password_path}")
    if not ca_path.is_file():
        raise AssertionError(f"SYS-31 CA file does not exist: {ca_path}")
    return base_url, login_name, password_path, ca_path


def _synthetic_video(path: Path) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("SYS-31 gateway evidence needs ffmpeg")
    output = path / "sys-31-synthetic.mp4"
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=32x32:r=24",
            "-t",
            "3",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        pytest.skip(f"SYS-31 gateway evidence cannot create H.264 media: {result.stderr}")
    return output.read_bytes()


def _wait_for_member(
    client: httpx2.Client,
    dataset_id: UUID,
    member_id: UUID,
    headers: dict[str, str],
    *,
    expected: str,
    timeout_seconds: float = 180,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, object] | None = None
    while time.monotonic() < deadline:
        response = client.get(f"{DATASETS}/{dataset_id}/members", headers=headers)
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        last = next(item for item in items if item["id"] == str(member_id))
        if last["status"] == expected:
            return last
        time.sleep(1)
    raise AssertionError(f"member did not reach {expected}: {last}")


def _wait_for_context(
    client: httpx2.Client,
    token: str,
    headers: dict[str, str],
    *,
    expected: str,
    timeout_seconds: float = 180,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, object] | None = None
    while time.monotonic() < deadline:
        response = client.get(f"{API_PREFIX}/annotation-contexts/{token}", headers=headers)
        assert response.status_code == 200, response.text
        last = response.json()
        if last["preparation_status"] == expected:
            return last
        time.sleep(1)
    raise AssertionError(f"annotation context did not reach {expected}: {last}")


@pytest.fixture
def deployed_annotation(tmp_path: Path) -> Iterator[DeployedAnnotation]:
    if os.getenv("NVSOP_SYS31_ALLOW_MUTATION") != "1":
        pytest.skip("SYS-31 deployed evidence requires an explicitly disposable deployment")
    configured = _configured_environment()
    if configured is None:
        pytest.skip(
            "SYS-31 deployed evidence needs NVSOP_SYS31_GATEWAY_URL, "
            "NVSOP_SYS31_LOGIN_NAME, NVSOP_SYS31_PASSWORD_FILE and NVSOP_SYS31_CA_FILE"
        )
    base_url, login_name, password_file, ca_file = configured
    password = password_file.read_text(encoding="utf-8").strip()
    video = _synthetic_video(tmp_path)
    sha256 = hashlib.sha256(video).hexdigest()

    with httpx2.Client(
        base_url=base_url,
        verify=str(ca_file),
        trust_env=False,
        timeout=30,
    ) as client:
        login = client.post(
            f"{API_PREFIX}/auth/session",
            json={"login_name": login_name, "password": password},
        )
        assert login.status_code == 201, login.text
        csrf = csrf_header(client)
        dataset = client.post(
            DATASETS,
            headers=csrf,
            json={"name": f"SYS-31-{uuid4().hex[:8]}"},
        )
        assert dataset.status_code == 201, dataset.text
        dataset_id = UUID(dataset.json()["id"])
        actions = client.post(
            f"{DATASETS}/{dataset_id}/action-list",
            headers=csrf,
            json={"actions": ["(1) 取料"]},
        )
        assert actions.status_code == 201, actions.text
        requested = client.post(
            f"{DATASETS}/{dataset_id}/members",
            headers={**csrf, "Idempotency-Key": f"sys-31-{uuid4().hex}"},
            json={
                "original_filename": "sys-31.mp4",
                "source": "synthetic-camera",
                "declared_size": len(video),
                "declared_sha256": sha256,
            },
        )
        assert requested.status_code == 201, requested.text
        request_body = requested.json()
        member_id = UUID(request_body["member"]["id"])
        uploaded = httpx2.post(
            request_body["upload"]["url"],
            data=request_body["upload"]["fields"],
            files={"file": ("sys-31.mp4", video, "video/mp4")},
            timeout=30,
        )
        assert uploaded.status_code == 204, uploaded.text
        confirmed = client.post(
            f"{DATASETS}/{dataset_id}/members/{member_id}/confirm",
            headers=csrf,
            json={"attempt_id": request_body["attempt"]["id"]},
        )
        assert confirmed.status_code == 202, confirmed.text
        _wait_for_member(client, dataset_id, member_id, csrf, expected="registered")

        context_response = client.post(
            f"{DATASETS}/{dataset_id}/members/{member_id}/annotation-context",
            headers=csrf,
            json={},
        )
        assert context_response.status_code == 201, context_response.text
        context = _wait_for_context(
            client,
            context_response.json()["context_token"],
            csrf,
            expected="succeeded",
        )
        yield DeployedAnnotation(client, csrf, dataset_id, member_id, context)


def test_sys_31_01_real_gateway_registration_and_annotation_context(
    deployed_annotation: DeployedAnnotation,
) -> None:
    """正式数据集入口进入标注，不重新从浏览器上传视频。"""
    context = deployed_annotation.context
    assert context["dataset_id"] == str(deployed_annotation.dataset_id)
    assert context["member_id"] == str(deployed_annotation.member_id)
    assert context["preparation_status"] == "succeeded"
    assert context["actions"] == ["(1) 取料"]
    assert str(context["video_url"]).startswith("https://")


def test_sys_31_03_and_15_media_range_and_session_revocation(
    deployed_annotation: DeployedAnnotation,
) -> None:
    """媒体端点重新鉴权，并保留 Range/原生媒体读取语义。"""
    client = deployed_annotation.client
    video_url = str(deployed_annotation.context["video_url"])
    full = client.get(video_url, headers=deployed_annotation.csrf)
    assert full.status_code == 200, full.text
    assert full.content

    ranged = client.get(
        video_url,
        headers={**deployed_annotation.csrf, "Range": "bytes=0-15"},
    )
    assert ranged.status_code == 206, ranged.text
    assert ranged.headers.get("content-range", "").startswith("bytes 0-15/")

    logout = client.delete(
        f"{API_PREFIX}/auth/session",
        headers=deployed_annotation.csrf,
    )
    assert logout.status_code == 204, logout.text
    rejected = client.get(video_url)
    assert rejected.status_code == 401, rejected.text
    assert rejected.headers.get("content-type", "").startswith("application/problem+json")
    assert rejected.json() == {
        "type": "about:blank",
        "title": "请先登录",
        "status": 401,
        "error_code": "AUTHENTICATION_REQUIRED",
    }


def test_sys_31_02_gateway_rejects_an_unowned_dataset(
    deployed_annotation: DeployedAnnotation,
) -> None:
    response = deployed_annotation.client.get(
        f"{API_PREFIX}/training-datasets/{UUID(int=999)}/members"
    )
    assert response.status_code == 403, response.text
    assert response.headers.get("content-type", "").startswith("application/problem+json")
    assert response.json() == {
        "type": "about:blank",
        "title": "没有执行该操作的权限",
        "status": 403,
        "error_code": "PERMISSION_DENIED",
    }


def test_sys_31_05_and_16_compatibility_write_is_not_a_product_bypass(
    deployed_annotation: DeployedAnnotation,
) -> None:
    """基座上传等未开放写入口在网关和中心用例边界均被拒绝。"""
    response = deployed_annotation.client.post(
        "/api/annotation/api/v1/actions/upload",
        headers=deployed_annotation.csrf,
        content=b"not a product upload",
    )
    assert response.status_code == 403, response.text
    assert response.json() == {
        "type": "about:blank",
        "title": "标注操作未开放",
        "status": 403,
        "error_code": "ANNOTATION_OPERATION_NOT_ALLOWED",
        "detail": "该标注基座操作未通过产品数据集接口开放",
    }
