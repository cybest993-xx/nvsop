#!/usr/bin/env python3
"""固定开发实例的真实 HTTP(S)、控制面、worker、标注准备和媒体入口冒烟。"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

_SOURCE = Path(__file__).resolve().parents[1] / "apps" / "control-api" / "src"
sys.path.insert(0, str(_SOURCE))

from dev_support import list_items, write_report  # noqa: E402
from factory_sop.dataset.client import (  # noqa: E402
    SESSION_COOKIE,
    ControlPlaneClient,
    DatasetImportError,
    UrllibTransport,
)

SAMPLE_DATASET_NAME = "开发样例数据集"
SAMPLE_VIDEO_FILENAME = "dev-sample.mp4"
MEDIA_RANGE_END = 31
MEDIA_CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")


def required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise DatasetImportError(f"响应缺少 {name}")
    return value


def media_probe(url: str, cookie: str, ca: Path) -> dict[str, object]:
    request = Request(url, headers={"Cookie": cookie, "Range": f"bytes=0-{MEDIA_RANGE_END}"})
    try:
        if urlsplit(url).scheme == "https":
            context = ssl.create_default_context(cafile=str(ca))
            response_context = urlopen(request, context=context, timeout=20)
        else:
            response_context = urlopen(request, timeout=20)
        with response_context as response:
            body = response.read(MEDIA_RANGE_END + 2)
            if not body:
                raise DatasetImportError("标注媒体响应为空")
            if response.status != 206:
                raise DatasetImportError(
                    f"标注媒体 Range 请求必须返回 HTTP 206，实际为 {response.status}"
                )
            content_range = response.headers.get("Content-Range")
            match = MEDIA_CONTENT_RANGE.fullmatch(content_range or "")
            if match is None:
                raise DatasetImportError("标注媒体 Range 响应缺少有效 Content-Range")
            range_start, range_end, total_size = (int(value) for value in match.groups())
            expected_end = min(MEDIA_RANGE_END, total_size - 1) if total_size > 0 else -1
            if range_start != 0 or range_end != expected_end:
                raise DatasetImportError(
                    f"标注媒体 Range 响应与 bytes=0-{MEDIA_RANGE_END} 不一致：{content_range!r}"
                )
            expected_length = range_end - range_start + 1
            if len(body) != expected_length:
                raise DatasetImportError(
                    f"标注媒体 Range 响应体长度应为 {expected_length}，实际为 {len(body)}"
                )
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except ValueError as error:
                    raise DatasetImportError(
                        f"标注媒体 Content-Length 无效：{content_length!r}"
                    ) from error
                if declared_length != len(body):
                    raise DatasetImportError(
                        f"标注媒体 Content-Length 应为 {len(body)}，实际为 {declared_length}"
                    )
            content_type = response.headers.get("Content-Type")
            if not content_type or not content_type.lower().startswith("video/"):
                raise DatasetImportError(f"标注媒体必须返回视频类型，实际为：{content_type!r}")
            return {
                "status": response.status,
                "content_type": content_type,
                "content_range": content_range,
                "bytes_read": len(body),
            }
    except (HTTPError, URLError, OSError, ssl.SSLError) as error:
        raise DatasetImportError(f"标注媒体入口不可用：{error}") from error


def wait_for_context(
    client: ControlPlaneClient,
    token: str,
    *,
    timeout_seconds: float = 600,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        context = client.request_json("GET", f"/api/v1/annotation-contexts/{token}", expected={200})
        status = context.get("preparation_status")
        if status == "succeeded":
            return context
        if status == "failed":
            raise DatasetImportError(
                f"标注准备失败：{context.get('preparation_failure_code')} "
                f"{context.get('preparation_failure_detail')}"
            )
        if time.monotonic() >= deadline:
            raise DatasetImportError(f"标注准备轮询超时，最后状态：{status}")
        time.sleep(2)


def wait_for_execution(
    client: ControlPlaneClient,
    poll_path: str,
    *,
    timeout_seconds: float = 600,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        execution = client.request_json("GET", poll_path, expected={200})
        status = execution.get("status")
        if status == "succeeded":
            return execution
        if status == "failed":
            raise DatasetImportError(
                f"标注切片失败：{execution.get('failure_code')} {execution.get('failure_detail')}"
            )
        if time.monotonic() >= deadline:
            raise DatasetImportError(f"标注切片轮询超时，最后状态：{status}")
        time.sleep(2)


def run(arguments: argparse.Namespace) -> dict[str, object]:
    password = arguments.password_file.read_text(encoding="utf-8").strip()
    client = ControlPlaneClient(UrllibTransport(), base_url=arguments.base_url)
    login = client.login(login_name=arguments.login_name, password=password)
    stations = list_items(client, "/api/v1/stations?page=1&page_size=100")
    drafts = list_items(client, "/api/v1/templates/drafts?page=1&page_size=100")
    datasets = list_items(client, "/api/v1/training-datasets?page=1&page_size=100")
    dataset = next((item for item in datasets if item.get("name") == SAMPLE_DATASET_NAME), None)
    if dataset is None:
        raise DatasetImportError("没有找到开发样例数据集")
    dataset_id = required_string(dataset.get("id"), "dataset.id")
    action_list = client.request_json(
        "GET", f"/api/v1/training-datasets/{dataset_id}/action-list", expected={200}
    )
    members = list_items(
        client,
        f"/api/v1/training-datasets/{dataset_id}/members?page=1&page_size=100",
    )
    member = next(
        (item for item in members if item.get("original_filename") == SAMPLE_VIDEO_FILENAME),
        None,
    )
    if member is None or member.get("status") != "registered":
        raise DatasetImportError("开发样例视频尚未 registered")
    member_id = required_string(member.get("id"), "member.id")
    action_revision = action_list.get("revision")
    if not isinstance(action_revision, int):
        raise DatasetImportError("开发样例动作清单缺少 revision")
    context = client.request_json(
        "POST",
        f"/api/v1/training-datasets/{dataset_id}/members/{member_id}/annotation-context",
        {"action_list_revision": action_revision},
        expected={201},
    )
    token = required_string(context.get("context_token"), "context_token")
    prepared = wait_for_context(client, token)
    cookie = f"{SESSION_COOKIE}={required_string(client.session_cookie, 'session_cookie')}"
    media = media_probe(
        required_string(prepared.get("video_url"), "video_url"),
        cookie,
        arguments.ca_file,
    )
    split = client.request_json(
        "POST",
        f"/api/annotation/api/v1/videos/{token}/split",
        {
            "timestamps": [
                {
                    "start": 0.0,
                    "end": 0.5,
                    "actionIndex": 0,
                    "actionDescription": "(1)放置工件",
                }
            ],
            "twoOperatorMode": False,
        },
        expected={202},
    )
    submission_id = required_string(split.get("submission_id"), "submission_id")
    execution_id = required_string(split.get("execution_id"), "execution_id")
    poll_path = required_string(split.get("poll_url"), "poll_url")
    execution = wait_for_execution(client, poll_path)
    clips = execution.get("clips")
    if not isinstance(clips, list) or not clips:
        raise DatasetImportError("标注切片响应没有 clips")
    clip_media = media_probe(
        (
            f"{arguments.base_url}/api/annotation/api/v1/annotation-submissions/"
            f"{submission_id}/executions/{execution_id}/clips/0/download"
        ),
        cookie,
        arguments.ca_file,
    )
    return {
        "status": "passed",
        "base_url": arguments.base_url,
        "login_user_id": login.get("user_id"),
        "station_count": len(stations),
        "template_draft_count": len(drafts),
        "dataset_id": dataset_id,
        "member_id": member_id,
        "action_list_revision": action_revision,
        "annotation_context_token": token,
        "annotation_preparation_status": prepared.get("preparation_status"),
        "media_probe": media,
        "annotation_execution_status": execution.get("status"),
        "clip_count": len(clips),
        "clip_media_probe": clip_media,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行固定开发实例真实功能冒烟")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--login-name", required=True)
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--report-file", type=Path, required=True)
    parser.add_argument(
        "--ca-file",
        type=Path,
        default=Path(__file__).resolve().parents[1] / ".tmp" / "dev-main" / "tls" / "ca.crt",
    )
    arguments = parser.parse_args(argv)
    try:
        result = run(arguments)
    except (OSError, DatasetImportError, ValueError, json.JSONDecodeError) as error:
        result = {"status": "failed", "base_url": arguments.base_url, "error": str(error)}
        write_report(arguments.report_file, result)
        print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    write_report(arguments.report_file, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
