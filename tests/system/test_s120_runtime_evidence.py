"""S120 — 运行、告警、证据跨模块验证（真实 SQLite/PG/HTTPS Nginx/Edge 生产外观）。

本文件用生产外观产生事实：Edge 侧真实 `LocalState`（SQLite）+ `StationSupervisor`（规范化 CPU 输入）
+ `HostReportReconciler`/`EvidenceReferenceReconciler` + `HttpDecisionReportTransport`，
经仓库真实 HTTPS Nginx 主机签名入口到达中心，再经公开只读 API/REST 核对镜像、处置与证据引用。

CPU 合成观测与 ffmpeg 合成媒体只证明软件路径；真实现场（GPU/真实相机）由独立票负责。
中心不做二次判定、不保存证据媒体字节。原网关前置探测保存在
`.nvsop/artifacts/s120/original-gateway-probe.py.txt`。
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import ssl
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from http.server import HTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import quote
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile

import httpx2
import pytest
from conftest import (
    ADMIN_CREDENTIALS,
    SESSION_PATH,
    BootstrapCommand,
    _stop_server,
    _wait_for_server,
    csrf_header,
    log_in,
)
from edge_runtime.center_client import CenterClient
from edge_runtime.configuration_sync import HttpConfigurationPuller
from edge_runtime.evidence_media import evidence_registration_payloads
from edge_runtime.evidence_reporting import (
    EvidenceReferenceReconciler,
    register_evidence_payloads,
)
from edge_runtime.judgment.evidence import EvidenceMargins
from edge_runtime.judgment.model import (
    HostInstant,
    HostLiveness,
    JudgmentState,
    Lifecycle,
    Ordering,
    RuntimeParameters,
    Template,
)
from edge_runtime.local_state import (
    BackendReportContext,
    EvidenceSource,
    LocalState,
    PendingReport,
    ReportContext,
    StationStore,
    open_local_state,
)
from edge_runtime.reporting import HostReportReconciler, reported_decision_from_pending
from edge_runtime.reporting_transport import HttpDecisionReportTransport
from edge_runtime.runtime_configuration import confirmed_runtime_configuration
from edge_runtime.station_runtime import StationRuntimeConfiguration
from edge_runtime.stream_health import StreamFact, StreamHealthEvent
from edge_runtime.supervisor.inputs import ActionRecognized, StreamHealthObserved
from edge_runtime.supervisor.station import StationSupervisor
from httpx2 import Client
from test_sys_24_03_connection_tests_are_real_three_valued_answers import _InferenceApiHandler
from test_sys_35_evidence import _synthetic_media
from test_sys_machine_identity_over_nginx_https import (
    TEMPLATES,
    XLSX_MEDIA_TYPE,
    _create_host,
    _nginx_gateway,
)

from nvsop_contracts import (
    ConfigurationBundle,
    ReportEvidence,
    ReportViolation,
    configuration_from_wire,
    configuration_to_wire,
)

BACKENDS = "/api/v1/inference-backends"
REPO_ROOT = Path(__file__).resolve().parents[2]
STATIONS = "/api/v1/stations"
CAMERAS = "/api/v1/cameras"
MEDIA_PATH = "camera-main"
STATION_CODE = "S120-1"
STATION_NAME = "S120 跨模块工位"
STEPS = ((1, "取料", "(1)取料"), (2, "焊接", "(2)焊接"), (3, "检验", "(3)检验"))
STEPS_V2 = ((1, "上料", "(1)上料"), (2, "装配", "(2)装配"), (3, "复检", "(3)复检"))
MARGINS = EvidenceMargins(leading=0.5, trailing=0.5)

# 推理后端身份端点是 CPU 合成外部硬件边界，复用 SYS-24-03 的真实 HTTP 端点夹具；“可达端点”
# 不是真 GPU/真实模型推理，而是中心实测 `/v1/metadata`、`/v1/models` 的软件路径输入。


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _DropFirstEnvelope(httpx2.BaseTransport):
    """网络边界故障注入：首个 enveloped 判定 POST 已被中心持久化后丢弃响应。

    用真实 HTTPS 传输转发，只在响应返回后模拟一次丢包，代表“至少一次”重试，
    不伪造事件体、不替身中心/鉴权。
    """

    def __init__(self, inner: httpx2.BaseTransport) -> None:
        self._inner = inner
        self._dropped = False

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        response = self._inner.handle_request(request)
        if not self._dropped and request.url.path.endswith("/reported-decisions/enveloped"):
            self._dropped = True
            response.close()
            raise httpx2.ReadError("simulated lost reply after the center persisted")
        return response


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@pytest.fixture
def a_reachable_endpoint() -> Iterator[str]:
    """CPU 合成推理端点（复用 SYS-24-03 的处理器）供中心实测模型标识。"""
    server = HTTPServer(("127.0.0.1", 0), _InferenceApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _template_workbook(
    *, station_code: str, station_name: str, steps: tuple[tuple[int, str, str], ...] = STEPS
) -> bytes:
    """构造本票需要的合成模板工作簿（复用既有规范化工作簿写法，不改旧测试）。"""
    sheets = {
        "工位表": [["工位号", "工位名称"], [station_code, station_name]],
        "步骤表": [
            ["工位号", "步骤号", "步骤名称", "步骤描述"],
            *(
                [station_code, str(number), name, description]
                for number, name, description in steps
            ),
        ],
    }
    names = list(sheets)
    shared = list(dict.fromkeys(value for rows in sheets.values() for row in rows for value in row))
    shared_index = {value: index for index, value in enumerate(shared)}

    def cell(column: int, row: int, value: str) -> str:
        reference = ""
        current = column
        while current:
            current, remainder = divmod(current - 1, 26)
            reference = chr(65 + remainder) + reference
        return f'<c r="{reference}{row}" t="s"><v>{shared_index[value]}</v></c>'

    def sheet_xml(rows: list[list[str]]) -> str:
        rendered = []
        for row_number, row in enumerate(rows, start=1):
            cells = "".join(cell(column, row_number, value) for column, value in enumerate(row, 1))
            rendered.append(f'<row r="{row_number}">{cells}</row>')
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{''.join(rendered)}</sheetData></worksheet>"
        )

    workbook_sheets = "".join(
        f'<sheet name="{name}" sheetId="{index}" r:id="rId{index}" />'
        for index, name in enumerate(names, 1)
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{workbook_sheets}</sheets></workbook>"
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(
            f'<Relationship Id="rId{index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="/xl/worksheets/sheet{index}.xml" />'
            for index in range(1, len(names) + 1)
        )
        + "</Relationships>"
    )
    shared_strings = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        f'count="{len(shared)}" uniqueCount="{len(shared)}">'
        + "".join(f"<si><t>{value}</t></si>" for value in shared)
        + "</sst>"
    )

    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/sharedStrings.xml", shared_strings)
        for index, name in enumerate(names, 1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", sheet_xml(sheets[name]))
    return output.getvalue()


def _create_station_and_backend(
    client: Client, browser_headers: dict[str, str], host_id: str, backend_base_url: str
) -> tuple[str, int, str, list[str]]:
    station = client.post(
        STATIONS,
        headers=browser_headers,
        json={"code": STATION_CODE, "name": STATION_NAME, "tags": []},
    )
    assert station.status_code == 201, station.text
    station_body = station.json()
    station_id = station_body["id"]
    station_revision = int(station_body["revision"])

    backend = client.post(
        BACKENDS,
        headers=browser_headers,
        json={"host_id": host_id, "base_url": backend_base_url},
    )
    assert backend.status_code == 201, backend.text
    backend_id = backend.json()["id"]

    camera = client.post(
        CAMERAS,
        headers=browser_headers,
        json={
            "name": "S120 相机",
            "address": "127.0.0.1",
            "main_stream_path": "/Streaming/Channels/101",
            "sub_stream_path": "/Streaming/Channels/102",
            "station_id": station_id,
            "host_id": host_id,
            "backend_id": backend_id,
        },
    )
    assert camera.status_code == 201, camera.text

    tested = client.post(
        f"{BACKENDS}/{backend_id}/connection-test",
        headers={**browser_headers, "If-Match": "1"},
    )
    assert tested.status_code == 200, tested.text
    model_ids = list(tested.json()["connection"]["self_reported_model_ids"])
    assert model_ids == ["ddm_finetune", "ds_sop_model"], tested.text
    return station_id, station_revision, backend_id, model_ids


def _create_bound_template(
    client: Client,
    browser_headers: dict[str, str],
    *,
    station_id: str,
    station_revision: int | None = None,
    filename: str = "s120-cross-module.xlsx",
    steps: tuple[tuple[int, str, str], ...] = STEPS,
) -> tuple[str, str]:
    """导入三步工作簿并发布绑定；走公开模板 HTTP 入口，返回版本身份与摘要。

    ``station_revision=None`` 时读当前公开配置视图的 revision（第二次绑定用）。
    """
    if station_revision is None:
        current = client.get(f"{TEMPLATES}/stations/{station_id}/configuration")
        assert current.status_code == 200, current.text
        station_revision = int(current.json()["station_revision"])
    imported = client.post(
        f"{TEMPLATES}/imports",
        params={"filename": filename},
        headers={**browser_headers, "Content-Type": XLSX_MEDIA_TYPE},
        content=_template_workbook(
            station_code=STATION_CODE, station_name=STATION_NAME, steps=steps
        ),
    )
    assert imported.status_code == 201, imported.text
    draft = imported.json()["draft"]
    draft_id = draft["id"]
    edited = client.patch(
        f"{TEMPLATES}/drafts/{draft_id}",
        headers={**browser_headers, "If-Match": str(draft["revision"])},
        json={
            "steps": draft["steps"],
            "ordering": "strict",
            "start_signal": {"kind": "action", "action_number": 1},
            "end_signals": [],
            "runtime_defaults": {
                "idle_timeout_seconds": 30,
                "step_deadline_seconds": 90,
                "disposition_policy": "record",
            },
        },
    )
    assert edited.status_code == 200, edited.text
    published = client.post(
        f"{TEMPLATES}/drafts/{draft_id}/publish",
        headers={**browser_headers, "If-Match": str(edited.json()["revision"])},
    )
    assert published.status_code == 201, published.text
    version = published.json()
    bound = client.post(
        f"{TEMPLATES}/bindings",
        headers={**browser_headers, "If-Match": str(station_revision)},
        json={
            "station_id": station_id,
            "version_id": version["id"],
            "runtime_parameter_mode": "follow_template",
            "runtime_parameters": None,
        },
    )
    assert bound.status_code == 200, bound.text
    return version["id"], version["sha256"]


def _runtime_configuration(
    bundle: ConfigurationBundle, *, station_id: str, backend_id: str
) -> StationRuntimeConfiguration:
    bootstrap = StationRuntimeConfiguration(
        station_id=station_id,
        inference_url="http://127.0.0.1:9/v1/chat/completions",
        request_body={},
        template=Template(
            steps=("(1) placeholder",), ordering=Ordering.ORDERED, start_signal="(1) placeholder"
        ),
        parameters=RuntimeParameters(idle_timeout=1.0, step_deadline=1.0),
        margins=MARGINS,
        backend_id=backend_id,
    )
    runtime = confirmed_runtime_configuration(
        bundle=bundle, bootstrap_stations=(bootstrap,), local_connectors=()
    )
    return runtime.stations[0].configuration


def _report_context(
    bundle: ConfigurationBundle, *, backend_id: str, model_ids: tuple[str, ...]
) -> ReportContext:
    station = bundle.stations[0]
    return ReportContext(
        host_id=bundle.host_id,
        station_id=station.station_id,
        backends=(BackendReportContext(backend_id, model_ids),),
        template_version_id=None if station.template is None else station.template.version_id,
        template_sha256=None if station.template is None else station.template.version_sha256,
        configuration_revision=bundle.config_revision,
        configuration_sha256=bundle.effective_sha256,
        configuration_json=json.dumps(
            configuration_to_wire(bundle), ensure_ascii=False, separators=(",", ":")
        ),
    )


def _register_evidence(
    *, state: LocalState, store: StationStore, client: CenterClient, host_id: str, directory: Path
) -> int:
    """把本机待办媒体按真实 1s 窗口定稿后经生产对账器登记；不虚报切片覆盖。"""
    prepared = 0
    for pending in store.pending_evidence():
        if pending.media_results is not None:
            prepared += 1
            continue
        artifact = (
            directory / f"{host_id}-{pending.queue_id}-{round(pending.start.seconds * 1000)}-"
            f"{round(pending.end.seconds * 1000)}"
        )
        artifact.mkdir(parents=True, exist_ok=True)
        clip, frame, digest, size, _frame_digest, _frame_size = _synthetic_media(artifact)
        assert abs((pending.end.seconds - pending.start.seconds) - 1.0) < 0.01, (
            "合成媒体只覆盖 1s 窗口；证据跨度必须是同一受控窗口，不能宣称更长覆盖"
        )
        media_json = json.dumps(
            [
                {
                    "media_path": MEDIA_PATH,
                    "clip_file": clip.name,
                    "keyframe_file": frame.name,
                    "actual_from": pending.start.seconds,
                    "actual_to": pending.end.seconds,
                    "material_generation": "original",
                    "sha256": digest,
                    "size_bytes": size,
                }
            ]
        )
        store.record_evidence_slice(
            pending.queue_id,
            at=HostInstant(pending.end.seconds + 1.0),
            media_results=media_json,
            covered_from=pending.start.seconds,
            covered_to=pending.end.seconds,
        )
        prepared += 1
    reconciler = EvidenceReferenceReconciler(
        state=state,
        client=client,
        host_id=host_id,
        evidence_directory=directory,
        interval=0.01,
        clock=lambda: 1_000_000.0,
    )
    completed = reconciler.drain(limit=10)
    assert completed == prepared, (completed, prepared)
    return completed


def test_s120_ac1_one_instance_traverses_edge_persistence_to_center_mirror(
    client: Client,
    bootstrap: BootstrapCommand,
    a_reachable_endpoint: str,
    tmp_path: Path,
) -> None:
    """AC1：同一实例/观测/健康/判定/锁存违规/处置/证据引用沿原 ID 与来源可追溯。"""
    log_in(client, bootstrap)
    browser_headers = csrf_header(client)
    host_id, private_key = _create_host(client, browser_headers)
    station_id, station_revision, backend_id, model_ids = _create_station_and_backend(
        client, browser_headers, host_id, a_reachable_endpoint
    )
    version_id, version_sha256 = _create_bound_template(
        client, browser_headers, station_id=station_id, station_revision=station_revision
    )

    evidence_directory = tmp_path / "evidence"
    evidence_directory.mkdir()
    with _nginx_gateway(client, tmp_path, gateway_scheme="https") as gateway:
        # Edge 生产入口必须校验 TLS；信任网关自己签发的 loopback 证书，而不是关闭校验。
        gateway_certificate = tmp_path / "nginx-tls" / "uvicorn.crt"
        edge_client = CenterClient(
            center_url=str(gateway.base_url),
            host_id=host_id,
            host_private_key=private_key,
            timeout=15.0,
            ssl_context=ssl.create_default_context(cafile=str(gateway_certificate)),
        )
        bundle = HttpConfigurationPuller(client=edge_client, host_id=host_id).pull()
        state = open_local_state(
            str(tmp_path / "edge.sqlite"),
            evidence_source=lambda _: EvidenceSource(wall_offset=0.0, media_paths=(MEDIA_PATH,)),
        )
        try:
            state.configuration().confirm(bundle, confirmed_at=1.0)
            conf = _runtime_configuration(bundle, station_id=station_id, backend_id=backend_id)
            assert conf.template_version_id == version_id
            assert str(conf.template_sha256) == version_sha256
            assert conf.model_ids == tuple(model_ids)
            assert conf.template.steps == tuple(description for _, _, description in STEPS)
            context = _report_context(bundle, backend_id=backend_id, model_ids=tuple(model_ids))
            store = state.station(station_id, report_context=context)
            clock = _Clock(1000.0)
            supervisor = StationSupervisor(
                state=JudgmentState(template=conf.template, parameters=conf.parameters),
                store=store,
                margins=conf.margins,
                clock=clock,
                disposition_policy=conf.disposition_policy,
            )
            provenance = BackendReportContext(backend_id, conf.model_ids)
            steps = conf.template.steps
            # 正常首步开启实例；跨步立即产生 WRONG_STEP + MISSED_STEP 锁存违规，实例仍在飞。
            supervisor.receive(
                ActionRecognized(
                    signal=steps[0], at=HostInstant(1000.0), source_time=0.0, source_anchor=100.0
                ),
                report_provenance=provenance,
            )
            violation_reaction = supervisor.receive(
                ActionRecognized(
                    signal=steps[2], at=HostInstant(1001.0), source_time=1.0, source_anchor=100.0
                ),
                report_provenance=provenance,
            )
            assert violation_reaction.decisions, "跨步必须立即产生带锁存违规的判定"
            assert {v.reason.value for d in violation_reaction.decisions for v in d.violations} == {
                "WRONG_STEP",
                "MISSED_STEP",
            }
            # 流健康事实按单调时序排在跨步之后、结案之前。
            supervisor.receive(
                StreamHealthObserved(
                    event=StreamHealthEvent(
                        fact=StreamFact.DELIVERING,
                        at_monotonic=1001.5,
                        source_anchor=100.0,
                        stream_id=MEDIA_PATH,
                    )
                ),
                report_provenance=provenance,
            )
            # 越过空闲时限结案；已锁存违规保留，不被最终判定覆盖。
            clock.now = 2000.0
            close_reaction = supervisor.wake(host=HostLiveness.ALIVE)
            assert close_reaction.decisions, "空闲时限必须闭合在飞实例"
            assert all(d.lifecycle is not Lifecycle.STAYS_OPEN for d in close_reaction.decisions), (
                close_reaction.decisions
            )

            reconciler = HostReportReconciler(
                reports=state.reports(),
                transport=HttpDecisionReportTransport(client=edge_client, host_id=host_id),
            )
            attempts = reconciler.flush(now=HostInstant(2001.0), reported_at=_now())
            assert attempts, "主机对账器必须排空刚持久化的结构化事实"
            assert all(attempt.sent for attempt in attempts), attempts
            registered = _register_evidence(
                state=state,
                store=store,
                client=edge_client,
                host_id=host_id,
                directory=evidence_directory,
            )
            assert registered >= 1
        finally:
            state.close()

    # 事实核对全部经中心公开只读 API，不读中心表。
    violations = client.get("/api/v1/monitor/violations", params={"page_size": 50})
    assert violations.status_code == 200, violations.text
    items = [item for item in violations.json()["items"] if item["station_id"] == station_id]
    assert {item["violation"]["reason_code"] for item in items} == {"WRONG_STEP", "MISSED_STEP"}
    for mirrored in items:
        assert mirrored["host_id"] == host_id
        assert mirrored["station_id"] == station_id
        assert mirrored["instance_id"] == 1
        assert mirrored["decision_event_id"].startswith(f"{host_id}:")
        assert mirrored["latched_at"]

    instances = client.get("/api/v1/monitor/instances", params={"page_size": 50})
    assert instances.status_code == 200, instances.text
    instance_items = [item for item in instances.json()["items"] if item["host_id"] == host_id]
    assert len(instance_items) == 1, instance_items
    mirrored_instance = instance_items[0]
    assert mirrored_instance["instance_id"] == 1
    assert mirrored_instance["closed_at"]
    assert mirrored_instance["template_version_id"] == version_id
    assert mirrored_instance["template_sha256"] == version_sha256
    assert mirrored_instance["backend_provenance"] == [
        {"backend_id": backend_id, "model_ids": model_ids}
    ]

    observations = client.get(
        "/api/v1/monitor/observations",
        params={"station_id": station_id, "instance_id": 1, "page_size": 50},
    )
    assert observations.status_code == 200, observations.text
    assert {item["signal"] for item in observations.json()["items"]} == {steps[0], steps[2]}
    assert {item["source"] for item in observations.json()["items"]} == {"action"}

    health = client.get("/api/v1/monitor/stream-health", params={"station_id": station_id})
    assert health.status_code == 200, health.text
    assert health.json()["validity"] == "healthy", health.text
    assert health.json()["streams"][0]["stream_id"] == MEDIA_PATH

    disposals = client.get("/api/v1/monitor/disposals", params={"page_size": 50})
    assert disposals.status_code == 200, disposals.text
    disposal_items = [
        item for item in disposals.json()["items"] if item["station_id"] == station_id
    ]
    assert disposal_items, "锁存违规必须产生真实处置事实"
    for disposal in disposal_items:
        assert disposal["host_id"] == host_id
        assert disposal["station_id"] == station_id
        assert disposal["instance_id"] == 1
        assert disposal["result_kind"]
    assert {item["action_kind"] for item in disposal_items} >= {"record", "frontend_alert"}

    evidence = client.get("/api/v1/evidence", params={"station_id": station_id, "instance_id": 1})
    assert evidence.status_code == 200, evidence.text
    references = evidence.json()
    assert references, "证据引用必须登记到中心"
    assert {item["kind"] for item in references} == {"clip", "keyframe"}
    assert all(item["host_id"].lower() == host_id.lower() for item in references)
    assert all(item["status"] == "available" for item in references)
    assert all(item["sha256"] and item["size"] and item["reference"] for item in references)
    # 中心只持有引用，媒体字节只在本机来源目录。
    for item in references:
        assert (evidence_directory / item["reference"]).is_file()


def _prepare(
    client: Client, bootstrap: BootstrapCommand, endpoint: str
) -> tuple[str, str, str, str, tuple[str, ...], str, str]:
    log_in(client, bootstrap)
    browser_headers = csrf_header(client)
    host_id, private_key = _create_host(client, browser_headers)
    station_id, station_revision, backend_id, model_ids = _create_station_and_backend(
        client, browser_headers, host_id, endpoint
    )
    version_id, version_sha = _create_bound_template(
        client, browser_headers, station_id=station_id, station_revision=station_revision
    )
    return host_id, private_key, station_id, backend_id, tuple(model_ids), version_id, version_sha


def _sse_frames(
    client: Client, *, last_event_id: str | None, until: str, timeout: float = 10.0
) -> list[tuple[str, dict[str, object]]]:
    """真实读取公开 SSE 快照/续接帧（id 与 data）；找到目标 event id 后主动断开。"""
    headers = {} if last_event_id is None else {"Last-Event-ID": last_event_id}
    frames: list[tuple[str, dict[str, object]]] = []
    current_id: str | None = None
    with client.stream(
        "GET", "/api/v1/monitor/stream", headers=headers, timeout=timeout
    ) as response:
        assert response.status_code == 200, response.read()
        for line in response.iter_lines():
            if line.startswith("id: "):
                current_id = line[4:]
            elif line.startswith("data: ") and current_id is not None:
                frames.append((current_id, json.loads(line[6:])))
                if current_id == until:
                    return frames
    raise AssertionError(f"SSE 未在断线前送达 {until}: {[frame[0] for frame in frames]}")


def _create_restricted_session(client: Client, browser_headers: dict[str, str]) -> dict[str, str]:
    """经公开角色/用户/会话接口造一个已登录但缺 evidence view 的用户，返回其会话 Cookie。"""
    password = secrets.token_urlsafe(24)
    role = client.post(
        "/api/v1/auth/roles",
        headers=browser_headers,
        json={
            "code": "s120_viewer",
            "name": "S120 查看者",
            "permissions": ["monitor.report.view"],
        },
    )
    assert role.status_code == 201, role.text
    role_id = role.json()["id"]
    user = client.post(
        "/api/v1/auth/users",
        headers=browser_headers,
        json={
            "login_name": "s120.viewer",
            "display_name": "S120 查看者",
            "password": password,
        },
    )
    assert user.status_code == 201, user.text
    user_id = user.json()["id"]
    assigned = client.put(
        f"/api/v1/auth/users/{user_id}/roles",
        headers=browser_headers,
        json={"role_ids": [role_id]},
    )
    assert assigned.status_code == 200, assigned.text
    opened = client.post(
        SESSION_PATH,
        json={"login_name": "s120.viewer", "password": password},
    )
    assert opened.status_code == 201, opened.text
    cookies = {key: client.cookies[key] for key in client.cookies}
    restored = client.post(SESSION_PATH, json=ADMIN_CREDENTIALS)
    assert restored.status_code == 201, restored.text
    return cookies


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    """回收本测试启动的 Vite 进程组（含 pnpm 派生的 node 子进程），不波及其它进程。"""
    try:
        group = os.getpgid(process.pid)
    except ProcessLookupError:
        return
    if process.poll() is None:
        os.killpg(group, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(group, signal.SIGKILL)
        process.wait(timeout=10)


def _find_gateway_container(config_path: Path) -> str:
    """只读识别当次测试自己启动、精确挂载该渲染配置的 nginx 网关容器。

    按挂载来源精确匹配（不按镜像名/名称前缀猜测，免得误伤其它任务容器）；无匹配或
    多匹配都立即失败，把确切的容器 ID 交给浏览器阶段做真实 worker 故障注入。
    """
    canonical = str(config_path.resolve())
    listed = subprocess.run(
        ["docker", "ps", "--quiet"], capture_output=True, text=True, check=True
    ).stdout.split()
    matches: list[str] = []
    for candidate in listed:
        inspected = subprocess.run(
            ["docker", "inspect", candidate], capture_output=True, text=True, check=True
        ).stdout
        mounts = json.loads(inspected)[0].get("Mounts") or []
        if any(
            mount.get("Source") == canonical
            and mount.get("Destination") == "/etc/nginx/conf.d/default.conf"
            for mount in mounts
        ):
            matches.append(candidate)
    assert len(matches) == 1, f"需要唯一挂载 {canonical} 的网关容器，实际 {matches}"
    return matches[0]


def _reload_gateway_web_root(config_path: Path, preview_port: int) -> None:
    """把夹具网关唯一 ``nvsop_web`` 上游指向本阶段真实静态 Web 预览端口并热加载。

    只改写该渲染配置中完整的 ``upstream nvsop_web`` block（其余 upstream、CSP、路由不动），
    再用本测试自有网关容器的既有只读身份路径 ``nginx -t`` + reload 使其生效。
    """
    marker = "upstream nvsop_web {"
    original = config_path.read_text(encoding="utf-8")
    assert original.count(marker) == 1, "渲染配置必须包含唯一 nvsop_web upstream"
    start = original.index(marker)
    end = original.index("}", start) + 1
    block = f"upstream nvsop_web {{\n    server 127.0.0.1:{preview_port};\n}}"
    config_path.write_text(original[:start] + block + original[end:], encoding="utf-8")
    container = _find_gateway_container(config_path)
    for args in (("-t",), ("-s", "reload")):
        subprocess.run(
            ["docker", "exec", container, "nginx", *args],
            check=True,
            capture_output=True,
            text=True,
        )


def _run_s120_browser(
    *,
    stage: str,
    gateway: Client,
    edge_origin: str,
    canonical_host: str,
    station_id: str,
    instance_id: int,
    clip_id: str,
    session_cookie: str,
    csrf: str,
    preview_port: int,
    tmp_path: Path,
) -> None:
    """opt-in：生产 Web 入口（真实 nginx ``location /`` + make web-build 产物）+ Playwright。

    页面与 API 都经真实 Nginx（``browser→nginx→center``），不再经 Vite 开发代理；静态 Web 由
    ``vite preview`` 复用 ``make web-build`` 产物提供，只作生产 Web 根的同源静态服务，不代理 API。
    """
    import shutil

    from test_sys_35_evidence import _wait_for_http

    pnpm = shutil.which("pnpm")
    assert pnpm is not None, "真实浏览器证据需要 pnpm/node"
    web = REPO_ROOT / "apps" / "control-web"
    web_dist = REPO_ROOT / ".nvsop" / "artifacts" / "web" / "dist"
    assert web_dist.is_dir(), "opt-in 前需先运行 make web-build 生成真实 Web 构建产物"
    preview_origin = f"http://127.0.0.1:{preview_port}"
    web_origin = str(gateway.base_url)
    env = {**os.environ, "NO_PROXY": f"{os.environ.get('NO_PROXY', '')},127.0.0.1,localhost"}
    preview_stdout = (tmp_path / "web.stdout").open("w")
    preview_stderr = (tmp_path / "web.stderr").open("w")
    preview = subprocess.Popen(
        [
            pnpm,
            "exec",
            "vite",
            "preview",
            "--host",
            "127.0.0.1",
            "--port",
            str(preview_port),
            "--strictPort",
            "--outDir",
            str(web_dist),
        ],
        cwd=web,
        env=env,
        stdout=preview_stdout,
        stderr=preview_stderr,
        text=True,
        start_new_session=True,
    )
    try:
        _wait_for_http(f"{preview_origin}/")
        # 生产 Web 根：把夹具网关唯一 nvsop_web 上游指向本阶段静态 Web，并热加载后验证真实根 200。
        _reload_gateway_web_root(tmp_path / "nginx-https.conf", preview_port)
        _wait_for_http(f"{web_origin}/")
        grep = "读取并复核证据" if stage == "live" else "离线"
        # 仅 live 阶段需要真实故障注入能力：定位当次自己创建的网关容器，只读识别、唯一匹配。
        nginx_container = (
            _find_gateway_container(tmp_path / "nginx-https.conf") if stage == "live" else ""
        )
        command = [
            pnpm,
            "exec",
            "playwright",
            "test",
            "tests/e2e/s120-runtime-evidence.spec.ts",
            "--project=chromium-1366x768",
            "--reporter=line",
            "--grep",
            grep,
        ]
        (tmp_path / f"playwright.{stage}.command").write_text(" ".join(command), encoding="utf-8")
        played = subprocess.run(
            command,
            cwd=web,
            env=env
            | {
                "NVSOP_BASE_URL": web_origin,
                "NVSOP_EVIDENCE_HOST_ID": canonical_host,
                "NVSOP_EVIDENCE_STATION_ID": station_id,
                "NVSOP_EVIDENCE_INSTANCE_ID": str(instance_id),
                "NVSOP_EVIDENCE_CLIP_ID": clip_id,
                "NVSOP_EVIDENCE_SESSION": session_cookie,
                "NVSOP_EVIDENCE_CSRF": csrf,
                "NVSOP_EVIDENCE_ORIGIN": edge_origin,
                "NVSOP_EVIDENCE_OFFLINE": "1" if stage == "offline" else "0",
                "NVSOP_EVIDENCE_NGINX_CONTAINER": nginx_container,
            },
            capture_output=True,
            text=True,
            timeout=300,
        )
        (tmp_path / f"playwright.{stage}.stdout").write_text(played.stdout, encoding="utf-8")
        (tmp_path / f"playwright.{stage}.stderr").write_text(played.stderr, encoding="utf-8")
        archive = REPO_ROOT / ".nvsop" / "artifacts" / "s120" / "browser"
        archive.mkdir(parents=True, exist_ok=True)
        for name in (
            f"playwright.{stage}.command",
            f"playwright.{stage}.stdout",
            f"playwright.{stage}.stderr",
            "web.stdout",
            "web.stderr",
        ):
            source_file = tmp_path / name
            if source_file.is_file():
                (archive / name).write_text(
                    source_file.read_text(encoding="utf-8"), encoding="utf-8"
                )
        assert played.returncode == 0, played.stdout + played.stderr
        assert "1 passed" in played.stdout, played.stdout
    finally:
        _terminate_process_group(preview)
        preview_stdout.close()
        preview_stderr.close()


def test_s120_ac2_duplicate_facts_template_switch_backlog_sse_and_unknown_reason(
    client: Client,
    bootstrap: BootstrapCommand,
    a_reachable_endpoint: str,
    tmp_path: Path,
) -> None:
    """AC2：重复上报/重复登记幂等、模板切换与历史积压不丢事实、SSE 断线续接、未知原因保留原码。"""
    (
        host_id,
        private_key,
        station_id,
        backend_id,
        model_ids,
        version_id,
        version_sha,
    ) = _prepare(client, bootstrap, a_reachable_endpoint)
    browser_headers = csrf_header(client)
    evidence_directory = tmp_path / "evidence"
    evidence_directory.mkdir()
    with _nginx_gateway(client, tmp_path, gateway_scheme="https") as gateway:
        gateway_certificate = tmp_path / "nginx-tls" / "uvicorn.crt"
        edge_client = CenterClient(
            center_url=str(gateway.base_url),
            host_id=host_id,
            host_private_key=private_key,
            timeout=15.0,
            ssl_context=ssl.create_default_context(cafile=str(gateway_certificate)),
        )
        bundle = HttpConfigurationPuller(client=edge_client, host_id=host_id).pull()
        assert bundle.stations[0].template is not None
        assert bundle.stations[0].template.version_id == version_id
        v1_revision = bundle.config_revision
        state = open_local_state(
            str(tmp_path / "edge.sqlite"),
            evidence_source=lambda _: EvidenceSource(wall_offset=0.0, media_paths=(MEDIA_PATH,)),
        )
        try:
            state.configuration().confirm(bundle, confirmed_at=1.0)
            conf = _runtime_configuration(bundle, station_id=station_id, backend_id=backend_id)
            store = state.station(
                station_id,
                report_context=_report_context(bundle, backend_id=backend_id, model_ids=model_ids),
            )
            clock = _Clock(1000.0)
            supervisor = StationSupervisor(
                state=JudgmentState(template=conf.template, parameters=conf.parameters),
                store=store,
                margins=conf.margins,
                clock=clock,
                disposition_policy=conf.disposition_policy,
            )
            provenance = BackendReportContext(backend_id, conf.model_ids)
            steps = conf.template.steps
            supervisor.receive(
                ActionRecognized(
                    signal=steps[0], at=HostInstant(1000.0), source_time=0.0, source_anchor=100.0
                ),
                report_provenance=provenance,
            )
            supervisor.receive(
                ActionRecognized(
                    signal=steps[2], at=HostInstant(1001.0), source_time=1.0, source_anchor=100.0
                ),
                report_provenance=provenance,
            )
            flaky_context = ssl.create_default_context(cafile=str(gateway_certificate))
            flaky_client = CenterClient(
                center_url=str(gateway.base_url),
                host_id=host_id,
                host_private_key=private_key,
                timeout=15.0,
                ssl_context=flaky_context,
                transport=_DropFirstEnvelope(httpx2.HTTPTransport(verify=flaky_context)),
            )
            transport = HttpDecisionReportTransport(client=flaky_client, host_id=host_id)
            reports = state.reports()
            pending = next(
                item
                for queue_id in reports.pending_ids()
                if isinstance((item := reports.pending_item(queue_id)), PendingReport)
                and item.latched_at is not None
                and item.context is not None
                and item.context.configuration_json is not None
            )
            assert pending.context is not None
            assert pending.context.configuration_json is not None
            reconciler = HostReportReconciler(reports=reports, transport=transport)
            first = reconciler.flush(now=HostInstant(1001.0), reported_at=_now())
            assert any(not attempt.sent for attempt in first), first

            # AC2 模板切换 + 历史积压：真实第一次 v1 send 已建立不可变历史 assignment；
            # 公开 Centre 绑定 v2，Edge 拉取并确认 v2，旧 v1 队列仍在本地不得丢失。
            v2_id, _v2_sha = _create_bound_template(
                client,
                browser_headers,
                station_id=station_id,
                station_revision=None,
                filename="s120-cross-module-v2.xlsx",
                steps=STEPS_V2,
            )
            assert v2_id != version_id
            current_view = client.get(f"{TEMPLATES}/stations/{station_id}/configuration")
            assert current_view.status_code == 200, current_view.text
            assert current_view.json()["version"]["id"] == v2_id
            pulled_v2 = HttpConfigurationPuller(client=edge_client, host_id=host_id).pull()
            assert pulled_v2.stations[0].template is not None
            assert pulled_v2.stations[0].template.version_id == v2_id
            assert pulled_v2.config_revision > v1_revision
            state.configuration().confirm(pulled_v2, confirmed_at=2.0)
            assert reports.pending_ids(), "模板切换不得丢弃历史积压"

            # current v2 下重发旧 v1 队列：仍被真实镜像，不产生第二份记录。
            second = reconciler.flush(now=HostInstant(1002.0), reported_at=_now())
            assert all(attempt.sent for attempt in second), second
            assert not reports.pending_ids(), "历史积压必须排空"
            first_decision_id = next(
                attempt.event_id
                for attempt in second
                if attempt.sent
                and attempt.event_id.startswith(f"{host_id}:")
                and all(
                    marker not in attempt.event_id
                    for marker in (":observation:", ":health:", ":instance:")
                )
            )
            # 原 v1 事实保留：SSE 帧携带 v1 template version/sha/backend provenance。
            snapshot = _sse_frames(client, last_event_id=None, until=first_decision_id)
            decision_data = next(
                data for event_id, data in snapshot if event_id == first_decision_id
            )
            assert decision_data["template_version_id"] == version_id
            assert decision_data["template_sha256"] == version_sha
            assert decision_data["configuration_revision"] == v1_revision
            assert decision_data["backend_provenance"] == [
                {"backend_id": backend_id, "model_ids": list(model_ids)}
            ]
            cursor = [event_id for event_id, _ in snapshot]
            assert cursor[-1] == first_decision_id, cursor
            configuration = configuration_from_wire(json.loads(pending.context.configuration_json))
            report = reported_decision_from_pending(pending, reported_at=_now())
            assert pending.latched_at is not None
            # 未知原因码的新判定：中心只镜像原码，不重新判定也不改来源。
            unknown_event_id = f"{host_id}:unknown-reason-code"
            unknown = replace(
                report,
                event_id=unknown_event_id,
                trace_id=unknown_event_id,
                reason_codes=("FUTURE_REASON",),
                violations=(
                    ReportViolation(
                        reason_code="FUTURE_REASON",
                        detail=None,
                        step_ids=("1",),
                        evidence=ReportEvidence(anchor=1.0, start=0.0, end=2.0),
                    ),
                ),
            )
            transport.send_timed_decision(
                unknown,
                configuration=configuration,
                latched_at=pending.latched_at,
                realtime=False,
            )
            # Last-Event-ID 续接：只送游标之后的新事实，不重放旧事件。
            resumed = _sse_frames(client, last_event_id=first_decision_id, until=unknown_event_id)
            resumed_ids = [event_id for event_id, _ in resumed]
            assert resumed_ids[-1] == unknown_event_id, resumed_ids
            assert first_decision_id not in resumed_ids, resumed_ids
            # 证据引用重复登记：同一稳定 ID 再登记两次不产生第二份引用。
            registered = _register_evidence(
                state=state,
                store=store,
                client=edge_client,
                host_id=host_id,
                directory=evidence_directory,
            )
            assert registered >= 1
            (evidence_pending,) = [
                item for item in store.pending_evidence() if item.media_results is not None
            ]
            payloads = evidence_registration_payloads(
                evidence_pending,
                host_id=host_id,
                station_id=station_id,
                directory=evidence_directory,
            )
            register_evidence_payloads(edge_client, payloads)
            register_evidence_payloads(edge_client, payloads)
        finally:
            state.close()

    violations = client.get("/api/v1/monitor/violations", params={"page_size": 50})
    assert violations.status_code == 200, violations.text
    codes = sorted(
        item["violation"]["reason_code"]
        for item in violations.json()["items"]
        if item["station_id"] == station_id
    )
    assert codes == ["FUTURE_REASON", "MISSED_STEP", "WRONG_STEP"], codes
    for item in violations.json()["items"]:
        if item["violation"]["reason_code"] == "FUTURE_REASON":
            assert item["host_id"] == host_id
            assert item["station_id"] == station_id

    evidence = client.get("/api/v1/evidence", params={"station_id": station_id, "instance_id": 1})
    assert evidence.status_code == 200, evidence.text
    assert len(evidence.json()) == 2, evidence.json()


def test_s120_ac3_authorized_source_read_and_review_do_not_override(
    client: Client,
    bootstrap: BootstrapCommand,
    a_reachable_endpoint: str,
    deployment_environ: dict[str, str],
    tls_certificate: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """AC3：真实来源主机媒体授权读取与人工复核不改原判定；越权/失效/缺失真实可见。"""
    from edge_runtime.evidence_access import create_server
    from test_sys_35_evidence import _reserve_port

    host_id, private_key, station_id, backend_id, model_ids, _version_id, _version_sha = _prepare(
        client, bootstrap, a_reachable_endpoint
    )
    browser_headers = csrf_header(client)
    canonical_host = str(UUID(host_id))
    certificate, key = tls_certificate

    center_reservation, center_port = _reserve_port()
    edge_reservation, edge_port = _reserve_port()
    preview_reservation, preview_port = _reserve_port()
    edge_origin = f"https://127.0.0.1:{edge_port}"
    center_url = f"https://127.0.0.1:{center_port}"
    # 网关经公开渲染入口读取同一来源映射写入 CSP；与 centre2 进程使用同一 JSON。
    monkeypatch.setenv("SOP_EVIDENCE_HOST_ORIGINS", json.dumps({canonical_host: edge_origin}))
    center_stdout = (tmp_path / "center2.stdout").open("w")
    center_stderr = (tmp_path / "center2.stderr").open("w")
    center_reservation.close()
    center2 = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "--factory",
            "factory_sop.entrypoint:build",
            "--host",
            "127.0.0.1",
            "--port",
            str(center_port),
            "--ssl-certfile",
            str(certificate),
            "--ssl-keyfile",
            str(key),
            "--no-access-log",
        ],
        env=deployment_environ
        | {
            "PYTHONPATH": str(REPO_ROOT / "apps" / "control-api" / "src"),
            "SOP_EVIDENCE_HOST_ORIGINS": json.dumps({canonical_host: edge_origin}),
        },
        stdout=center_stdout,
        stderr=center_stderr,
        text=True,
    )
    edge_server = None
    evidence_directory = tmp_path / "evidence"
    evidence_directory.mkdir()
    try:
        _wait_for_server(
            center2,
            base_url=center_url,
            stdout_path=tmp_path / "center2.stdout",
            stderr_path=tmp_path / "center2.stderr",
        )
        with _nginx_gateway(
            httpx2.Client(base_url=center_url, verify=False, trust_env=False),
            tmp_path,
            gateway_scheme="https",
        ) as gateway:
            gateway_certificate = tmp_path / "nginx-tls" / "uvicorn.crt"
            web_origin = str(gateway.base_url)
            edge_context = ssl.create_default_context(cafile=str(gateway_certificate))
            edge_client = CenterClient(
                center_url=str(gateway.base_url),
                host_id=host_id,
                host_private_key=private_key,
                timeout=15.0,
                ssl_context=edge_context,
            )
            bundle = HttpConfigurationPuller(client=edge_client, host_id=host_id).pull()
            state = open_local_state(
                str(tmp_path / "edge.sqlite"),
                evidence_source=lambda _: EvidenceSource(
                    wall_offset=0.0, media_paths=(MEDIA_PATH,)
                ),
            )
            try:
                state.configuration().confirm(bundle, confirmed_at=1.0)
                conf = _runtime_configuration(bundle, station_id=station_id, backend_id=backend_id)
                store = state.station(
                    station_id,
                    report_context=_report_context(
                        bundle, backend_id=backend_id, model_ids=model_ids
                    ),
                )
                clock = _Clock(1000.0)
                supervisor = StationSupervisor(
                    state=JudgmentState(template=conf.template, parameters=conf.parameters),
                    store=store,
                    margins=conf.margins,
                    clock=clock,
                    disposition_policy=conf.disposition_policy,
                )
                provenance = BackendReportContext(backend_id, conf.model_ids)
                steps = conf.template.steps
                supervisor.receive(
                    ActionRecognized(
                        signal=steps[0],
                        at=HostInstant(1000.0),
                        source_time=0.0,
                        source_anchor=100.0,
                    ),
                    report_provenance=provenance,
                )
                violation = supervisor.receive(
                    ActionRecognized(
                        signal=steps[2],
                        at=HostInstant(1001.0),
                        source_time=1.0,
                        source_anchor=100.0,
                    ),
                    report_provenance=provenance,
                )
                reconciler = HostReportReconciler(
                    reports=state.reports(),
                    transport=HttpDecisionReportTransport(client=edge_client, host_id=host_id),
                )
                attempts = reconciler.flush(now=HostInstant(1001.0), reported_at=_now())
                assert all(attempt.sent for attempt in attempts), attempts
                assert violation.decisions
                _register_evidence(
                    state=state,
                    store=store,
                    client=edge_client,
                    host_id=host_id,
                    directory=evidence_directory,
                )
            finally:
                state.close()

            # 来源主机证据服务：真实 HTTPS，逐次回查中心授权。
            edge_reservation.close()
            edge_server = create_server(
                address=("127.0.0.1", edge_port),
                client=CenterClient(
                    center_url=center_url,
                    host_id=canonical_host,
                    host_private_key=private_key,
                    timeout=5.0,
                    ssl_context=ssl.create_default_context(cafile=str(certificate)),
                ),
                directory=evidence_directory,
                web_origin=web_origin,
            )
            edge_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            edge_tls.load_cert_chain(str(certificate), str(key))
            edge_server.socket = edge_tls.wrap_socket(edge_server.socket, server_side=True)
            threading.Thread(target=edge_server.serve_forever, daemon=True).start()

            with httpx2.Client(base_url=center_url, verify=False, trust_env=False) as browser:
                browser.cookies.update(client.cookies)
                listing = browser.get(
                    "/api/v1/evidence",
                    params={"station_id": station_id, "instance_id": 1},
                )
                assert listing.status_code == 200, listing.text
                clip = next(item for item in listing.json() if item["kind"] == "clip")
                clip_id = clip["evidence_id"]
                clip_path = evidence_directory / clip["reference"]

                context_path = f"/api/v1/evidence/{quote(clip_id, safe='')}/access-context"
                assert browser.get(context_path).status_code == 200
                context = browser.get(context_path).json()["context"]
                source = (
                    f"{edge_origin}/evidence/{quote(clip_id, safe='')}"
                    f"?context={quote(context, safe='')}"
                )
                with httpx2.Client(verify=False, trust_env=False) as direct:
                    played = direct.get(
                        source, headers={"Origin": web_origin, "Range": "bytes=0-3"}
                    )
                    assert played.status_code == 206, played.text
                    assert played.content == clip_path.read_bytes()[:4]
                    # 媒体缺失：来源侧显式区分，不冒充授权失败。
                    hidden = clip_path.with_name(clip_path.name + ".hidden")
                    clip_path.rename(hidden)
                    try:
                        missing = direct.get(source, headers={"Origin": web_origin})
                        assert missing.status_code == 404, missing.text
                        assert missing.headers["X-Evidence-Error"] == "media_missing"
                    finally:
                        hidden.rename(clip_path)

                before = client.get("/api/v1/monitor/violations", params={"page_size": 50})
                # 人工复核：结论另存，不改写原判定/违规。
                reviewed = browser.post(
                    f"/api/v1/evidence/{quote(clip_id, safe='')}/reviews",
                    headers={"x-csrf-token": client.cookies["sop_csrf"]},
                    json={
                        "review_id": "018f0000-0000-7000-8000-00000000a120",
                        "generation": "original",
                        "conclusion": "rejected",
                    },
                )
                assert reviewed.status_code in (200, 201), reviewed.text
                assert reviewed.json()["conclusion"] == "rejected"
                after = client.get("/api/v1/monitor/violations", params={"page_size": 50})
                assert before.json()["items"] == after.json()["items"], "复核不得改写原判定"
                reviews = browser.get(f"/api/v1/evidence/{quote(clip_id, safe='')}/reviews")
                assert len(reviews.json()) == 1, reviews.text

                # 单变量对照：同一 Case3 中心进程、同一会话，直连 HTTPS（不经网关）应即时出帧。
                with (
                    httpx2.Client(
                        base_url=center_url,
                        verify=False,
                        trust_env=False,
                        timeout=8.0,
                        cookies=client.cookies,
                    ) as direct_sse,
                    direct_sse.stream("GET", "/api/v1/monitor/stream") as direct_response,
                ):
                    assert direct_response.status_code == 200
                    direct_first = next(direct_response.iter_lines())
                    assert direct_first.startswith(("id:", "retry:", ":")), direct_first

                # 同一请求仅多一跳真实 nginx 网关：8s 内必须同样出帧，否则 UI 实时镜像会挂起。
                with (
                    httpx2.Client(
                        base_url=str(gateway.base_url),
                        verify=False,
                        trust_env=False,
                        timeout=8.0,
                        cookies=client.cookies,
                    ) as gateway_sse,
                    gateway_sse.stream("GET", "/api/v1/monitor/stream") as gateway_response,
                ):
                    assert gateway_response.status_code == 200
                    gateway_first = next(gateway_response.iter_lines())
                    assert gateway_first.startswith(("id:", "retry:", ":")), gateway_first

                # 已登录但缺 evidence view：经公开角色/用户接口造最小用户，真实 403。
                viewer_cookies = _create_restricted_session(client, browser_headers)
                with httpx2.Client(base_url=center_url, verify=False, trust_env=False) as viewer:
                    viewer.cookies.update(viewer_cookies)
                    assert viewer.get(context_path).status_code == 403
                    denied = viewer.get(
                        "/api/v1/evidence", params={"station_id": station_id, "instance_id": 1}
                    )
                    assert denied.status_code == 403, denied.text

                preview_reservation.close()
                if os.environ.get("NVSOP_EVIDENCE_BROWSER") == "1":
                    _run_s120_browser(
                        stage="live",
                        gateway=gateway,
                        edge_origin=edge_origin,
                        canonical_host=canonical_host,
                        station_id=station_id,
                        instance_id=1,
                        clip_id=clip_id,
                        session_cookie=client.cookies["sop_session"],
                        csrf=client.cookies["sop_csrf"],
                        preview_port=preview_port,
                        tmp_path=tmp_path,
                    )

                # 部分失败：同一实例两份媒体，一份真实缺失、另一份正常；中心引用仍在。
                frame = next(item for item in listing.json() if item["kind"] == "keyframe")
                frame_context = browser.get(
                    f"/api/v1/evidence/{quote(frame['evidence_id'], safe='')}/access-context"
                ).json()["context"]
                frame_source = (
                    f"{edge_origin}/evidence/{quote(frame['evidence_id'], safe='')}"
                    f"?context={quote(frame_context, safe='')}"
                )
                hidden = clip_path.with_name(clip_path.name + ".hidden")
                clip_path.rename(hidden)
                try:
                    with httpx2.Client(verify=False, trust_env=False) as direct:
                        missing = direct.get(source, headers={"Origin": web_origin})
                        assert missing.status_code == 404, missing.text
                        assert missing.headers["X-Evidence-Error"] == "media_missing"
                        intact = direct.get(frame_source, headers={"Origin": web_origin})
                        assert intact.status_code == 200, intact.text
                    still_listed = browser.get(
                        "/api/v1/evidence", params={"station_id": station_id, "instance_id": 1}
                    )
                    assert {item["kind"] for item in still_listed.json()} == {"clip", "keyframe"}
                finally:
                    hidden.rename(clip_path)

                # 会话失效：登出后同一票据逐次回查拒绝。
                logged_out = browser.delete(
                    SESSION_PATH, headers={"x-csrf-token": browser.cookies["sop_csrf"]}
                )
                assert logged_out.status_code == 204
                with httpx2.Client(verify=False, trust_env=False) as direct:
                    revoked = direct.get(source, headers={"Origin": web_origin})
                    assert revoked.status_code == 403, revoked.text
                    assert revoked.headers["X-Evidence-Error"] == "session_invalid"

                # 重新登录，为真实浏览器离线阶段保留有效会话。
                relogin = client.post(SESSION_PATH, json=ADMIN_CREDENTIALS)
                assert relogin.status_code == 201, relogin.text

                # 主机离线：真实停机后浏览器重开证据读取显示不可达。
                edge_server.shutdown()
                edge_server.server_close()
                edge_server = None
                if os.environ.get("NVSOP_EVIDENCE_BROWSER") == "1":
                    _run_s120_browser(
                        stage="offline",
                        gateway=gateway,
                        edge_origin=edge_origin,
                        canonical_host=canonical_host,
                        station_id=station_id,
                        instance_id=1,
                        clip_id=clip_id,
                        session_cookie=client.cookies["sop_session"],
                        csrf=client.cookies["sop_csrf"],
                        preview_port=preview_port,
                        tmp_path=tmp_path,
                    )
    finally:
        preview_reservation.close()
        if edge_server is not None:
            edge_server.shutdown()
            edge_server.server_close()
        edge_reservation.close()
        _stop_server(center2)
        center_stdout.close()
        center_stderr.close()
