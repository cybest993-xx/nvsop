#!/usr/bin/env python3
"""通过正式 HTTP(S) API 幂等初始化固定开发实例的最小可视化样例。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from html import escape
from io import BytesIO
from pathlib import Path
from typing import cast
from urllib.parse import quote
from zipfile import ZIP_DEFLATED, ZipFile

_SOURCE = Path(__file__).resolve().parents[1] / "apps" / "control-api" / "src"
sys.path.insert(0, str(_SOURCE))

from dev_support import list_items, write_report  # noqa: E402
from factory_sop.dataset.client import (  # noqa: E402
    ControlPlaneClient,
    DatasetImportError,
    UrllibTransport,
)

SAMPLE_STATION_CODE = "dev-sample-station"
SAMPLE_STATION_NAME = "开发样例工位"
SAMPLE_TEMPLATE_FILENAME = "dev-sample.xlsx"
SAMPLE_DATASET_NAME = "开发样例数据集"
SAMPLE_VIDEO_FILENAME = "dev-sample.mp4"
SAMPLE_ACTIONS = ["(1)放置工件", "(2)锁紧夹具", "(3)完成检查"]


def inline_cell(reference: str, value: str) -> str:
    return f'<c r="{reference}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'


def sheet_xml(rows: list[list[str]]) -> bytes:
    rendered = []
    for row_number, row in enumerate(rows, start=1):
        cells = "".join(
            inline_cell(f"{chr(65 + column)}{row_number}", value)
            for column, value in enumerate(row)
        )
        rendered.append(f'<row r="{row_number}">{cells}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(rendered)}</sheetData></worksheet>"
    ).encode()


def sample_workbook() -> bytes:
    station_rows = [["工位号", "工位名称"], [SAMPLE_STATION_CODE, SAMPLE_STATION_NAME]]
    step_rows = [
        ["工位号", "步骤号", "步骤名称", "步骤描述"],
        [SAMPLE_STATION_CODE, "1", "放置工件", SAMPLE_ACTIONS[0]],
        [SAMPLE_STATION_CODE, "2", "锁紧夹具", SAMPLE_ACTIONS[1]],
        [SAMPLE_STATION_CODE, "3", "完成检查", SAMPLE_ACTIONS[2]],
    ]
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="工位表" sheetId="1" r:id="rId1"/>'
        '<sheet name="步骤表" sheetId="2" r:id="rId2"/></sheets></workbook>'
    ).encode()
    relationships = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" '
        b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        b'Target="worksheets/sheet1.xml"/>'
        b'<Relationship Id="rId2" '
        b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        b'Target="worksheets/sheet2.xml"/>'
        b"</Relationships>"
    )
    content_types = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Default Extension="rels" '
        b'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        b'<Default Extension="xml" ContentType="application/xml"/>'
        b'<Override PartName="/xl/workbook.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        b'<Override PartName="/xl/worksheets/sheet1.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        b'<Override PartName="/xl/worksheets/sheet2.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        b"</Types>"
    )
    root_relationships = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" '
        b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        b'officeDocument" '
        b'Target="xl/workbook.xml"/>'
        b"</Relationships>"
    )
    output = BytesIO()
    with ZipFile(output, mode="w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_relationships)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml(station_rows))
        archive.writestr("xl/worksheets/sheet2.xml", sheet_xml(step_rows))
    return output.getvalue()


def station_id(client: ControlPlaneClient) -> str:
    for station in list_items(client, "/api/v1/stations?page=1&page_size=100"):
        if station.get("code") == SAMPLE_STATION_CODE:
            value = station.get("id")
            if isinstance(value, str):
                return value
    created = client.request_json(
        "POST",
        "/api/v1/stations",
        {"code": SAMPLE_STATION_CODE, "name": SAMPLE_STATION_NAME, "tags": ["development"]},
        expected={201},
    )
    value = created.get("id")
    if not isinstance(value, str):
        raise DatasetImportError("创建开发样例工位的响应没有 id")
    return value


def ensure_template(client: ControlPlaneClient) -> str:
    drafts = list_items(client, "/api/v1/templates/drafts?page=1&page_size=100")
    for draft in drafts:
        if draft.get("station_code") == SAMPLE_STATION_CODE:
            value = draft.get("id")
            if isinstance(value, str):
                return value
    response = client.request_bytes(
        "POST",
        f"/api/v1/templates/imports?filename={quote(SAMPLE_TEMPLATE_FILENAME)}",
        sample_workbook(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        expected={201},
    )
    payload = json.loads(response.body.decode("utf-8"))
    payload_object = cast(dict[str, object], payload) if isinstance(payload, dict) else {}
    draft = payload_object.get("draft")
    if not isinstance(draft, dict) or not isinstance(draft.get("id"), str):
        raise DatasetImportError("模板导入响应没有可用草稿")
    return str(draft["id"])


def ensure_dataset(client: ControlPlaneClient) -> tuple[str, str, str]:
    dataset_id: str | None = None
    for dataset in list_items(client, "/api/v1/training-datasets?page=1&page_size=100"):
        if dataset.get("name") == SAMPLE_DATASET_NAME:
            value = dataset.get("id")
            if isinstance(value, str):
                dataset_id = value
                break
    if dataset_id is None:
        created = client.create_dataset(name=SAMPLE_DATASET_NAME)
        value = created.get("id")
        if not isinstance(value, str):
            raise DatasetImportError("创建开发样例数据集的响应没有 id")
        dataset_id = value
    try:
        action_list = client.request_json(
            "GET", f"/api/v1/training-datasets/{dataset_id}/action-list", expected={200}
        )
    except DatasetImportError as error:
        if "HTTP 404" not in str(error):
            raise
        action_list = client.request_json(
            "POST",
            f"/api/v1/training-datasets/{dataset_id}/action-list",
            {"actions": SAMPLE_ACTIONS},
            expected={201},
        )
    revision = action_list.get("revision")
    if not isinstance(revision, int):
        raise DatasetImportError("开发样例动作清单响应没有 revision")
    return dataset_id, str(revision), str(action_list.get("created_at", ""))


def complete_video_upload(
    client: ControlPlaneClient,
    *,
    dataset_id: str,
    member_id: str,
    attempt_id: str,
    upload: dict[str, object],
    video: Path,
) -> str:
    """完成一次直传、确认和 worker 校验，供首次申请和失败重试共用。"""
    uploaded = client.upload_file(instructions=upload, path=video)
    if uploaded.status not in {200, 201, 204}:
        raise DatasetImportError(f"开发样例视频直传失败：HTTP {uploaded.status}")
    confirmed = client.confirm_video_upload(
        dataset_id=dataset_id,
        member_id=member_id,
        attempt_id=attempt_id,
    )
    job = confirmed.get("job")
    if isinstance(job, dict) and isinstance(job.get("id"), str):
        result = client.wait_for_job(job_id=str(job["id"]))
        if result.get("status") != "succeeded":
            raise DatasetImportError(f"开发样例视频校验失败：{result}")
    return member_id


def ensure_video(client: ControlPlaneClient, dataset_id: str, video: Path) -> str:
    members = list_items(
        client, f"/api/v1/training-datasets/{dataset_id}/members?page=1&page_size=100"
    )
    for member in members:
        if member.get("original_filename") != SAMPLE_VIDEO_FILENAME:
            continue
        member_id = member.get("id")
        status = member.get("status")
        if not isinstance(member_id, str):
            continue
        if status == "registered":
            return member_id
        if status in {"pending_validation", "validating"} and isinstance(
            member.get("validation_job_id"), str
        ):
            client.wait_for_job(job_id=str(member["validation_job_id"]))
            refreshed = client.request_json(
                "GET", f"/api/v1/training-datasets/{dataset_id}/members/{member_id}", expected={200}
            )
            if refreshed.get("status") == "registered":
                return member_id
        if status == "failed":
            retry = client.request_json(
                "POST",
                f"/api/v1/training-datasets/{dataset_id}/members/{member_id}/retry",
                {"mode": "retry_upload"},
                expected={200, 201},
                idempotency_key="dev-seed-retry",
            )
            retry_member = retry.get("member")
            retry_attempt = retry.get("attempt")
            retry_upload = retry.get("upload")
            if not all(
                isinstance(value, dict) for value in (retry_member, retry_attempt, retry_upload)
            ):
                raise DatasetImportError("开发样例视频重试响应不完整")
            retry_member_id = retry_member.get("id")
            retry_attempt_id = retry_attempt.get("id")
            if not isinstance(retry_member_id, str) or not isinstance(retry_attempt_id, str):
                raise DatasetImportError("开发样例视频重试响应缺少成员或尝试 id")
            return complete_video_upload(
                client,
                dataset_id=dataset_id,
                member_id=retry_member_id,
                attempt_id=retry_attempt_id,
                upload=cast(dict[str, object], retry_upload),
                video=video,
            )
        raise DatasetImportError(f"开发样例视频处于不可恢复状态：{status}")
    content = video.read_bytes()
    declaration = {
        "original_filename": SAMPLE_VIDEO_FILENAME,
        "source": "synthetic-development",
        "declared_size": len(content),
        "declared_sha256": hashlib.sha256(content).hexdigest(),
    }
    requested = client.request_video_upload(
        dataset_id=dataset_id,
        declaration=declaration,
        idempotency_key="dev-seed-video-1",
    )
    member = requested.get("member")
    attempt = requested.get("attempt")
    upload = requested.get("upload")
    if not all(isinstance(value, dict) for value in (member, attempt, upload)):
        raise DatasetImportError("开发样例视频申请响应不完整")
    member_id = member.get("id")
    attempt_id = attempt.get("id")
    if not isinstance(member_id, str) or not isinstance(attempt_id, str):
        raise DatasetImportError("开发样例视频申请响应缺少成员或尝试 id")
    return complete_video_upload(
        client,
        dataset_id=dataset_id,
        member_id=member_id,
        attempt_id=attempt_id,
        upload=cast(dict[str, object], upload),
        video=video,
    )


def login_when_ready(client: ControlPlaneClient, *, login_name: str, password: str) -> None:
    """等待网关/中心完成启动和 bootstrap，再执行一次正式会话登录。"""
    retryable = ("HTTP 401", "HTTP 502", "HTTP 503", "HTTP 504", "无法连接 HTTP 服务")
    last_error: DatasetImportError | None = None
    for attempt in range(60):
        try:
            client.login(login_name=login_name, password=password)
            return
        except DatasetImportError as error:
            if not any(marker in str(error) for marker in retryable):
                raise
            last_error = error
            if attempt < 59:
                time.sleep(2)
    assert last_error is not None
    raise last_error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="初始化固定开发实例样例数据")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--login-name", required=True)
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--report-file", type=Path)
    arguments = parser.parse_args(argv)
    report = arguments.report_file or Path(".tmp/dev-main/reports/sample-manual.json")
    result: dict[str, object] = {"status": "failed", "base_url": arguments.base_url}
    try:
        password = arguments.password_file.read_text(encoding="utf-8").strip()
        client = ControlPlaneClient(UrllibTransport(), base_url=arguments.base_url)
        login_when_ready(client, login_name=arguments.login_name, password=password)
        station = station_id(client)
        draft = ensure_template(client)
        dataset, action_revision, action_created_at = ensure_dataset(client)
        member = ensure_video(client, dataset, arguments.video)
        result.update(
            {
                "status": "passed",
                "station_id": station,
                "template_draft_id": draft,
                "dataset_id": dataset,
                "action_list_revision": action_revision,
                "action_list_created_at": action_created_at,
                "member_id": member,
            }
        )
        write_report(report, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, DatasetImportError, ValueError, json.JSONDecodeError) as error:
        result["error"] = str(error)
        write_report(report, result)
        print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
