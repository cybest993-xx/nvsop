"""SYS-93 — 委托连接测试通过真实中心 HTTP 完成闭环。

前置：真实 PostgreSQL、真实 TLS Uvicorn 中心、真实 bootstrap 操作员和已登记的推理机。
动作：操作员入队，推理机按主机领取并回报，操作员从另一会话查询；另外安排配置修订变化安全边界。
可观察结果：命令跨请求持久化且按主机隔离，重复回报幂等，迟到结果被拒绝，
拒绝原因可见，连接器健康状态不会被安全拒绝覆盖，中心 API 不携带连接器凭据。
"""

from __future__ import annotations

import io
import json
import os
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import count
from pathlib import Path
from random import Random
from typing import Any, ClassVar, cast
from unittest.mock import patch
from urllib.parse import urlsplit

from conftest import ADMIN_CREDENTIALS, BootstrapCommand, csrf_header, log_in
from edge_runtime.connectors.hikvision import CANDIDATE_PROFILE
from edge_runtime.runtime import LocalIsapiConnectorConfiguration, build_connection_test_loop
from httpx2 import Client, Response

from nvsop_contracts import (
    HostIdentityKeyPair,
    HostIdentityRequest,
    Unverified,
    capability_to_wire,
    connection_test_claim_from_wire,
    connection_test_claim_to_wire,
    generate_host_identity_key_pair,
    sign_host_identity_request,
)

HOSTS = "/api/v1/inference-hosts"
STATIONS = "/api/v1/stations"
CONNECTORS = "/api/v1/connectors"
COMMANDS = "/api/v1/device-commands"
_NONCES = count()


def _fixture_host_identity(seed: int) -> HostIdentityKeyPair:
    """用固定伪随机流生成合成测试密钥, 避免测试依赖系统熵。"""
    random = Random(seed)
    with (
        patch("nvsop_contracts.host_identity.secrets.randbits", side_effect=random.getrandbits),
        patch("nvsop_contracts.host_identity.secrets.randbelow", side_effect=random.randrange),
    ):
        return generate_host_identity_key_pair()


Target = dict[str, str]
_PENDING_COMMAND_FIELDS = frozenset(
    {
        "id",
        "host_id",
        "command_type",
        "target_id",
        "target_revision",
        "idempotency_key",
        "status",
        "attempt",
        "claimed_at",
        "lease_expires_at",
        "result",
        "result_detail",
        "failure_code",
        "result_credentials_configured",
        "completed_at",
        "created_by",
        "created_at",
        "updated_at",
    }
)


class LocalDeviceHandler(BaseHTTPRequestHandler):
    """真实 urllib 连接器测试使用的本地 HTTP 设备端点。"""

    status_code: ClassVar[int] = 200

    def do_GET(self) -> None:
        self.send_response(self.status_code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format_string: str, *args: object) -> None:
        del format_string, args


class LocalInferenceHandler(BaseHTTPRequestHandler):
    """真实生产工位输入使用的本地 SSE 端点。"""

    payload: ClassVar[bytes]
    requests: ClassVar[int] = 0

    def do_POST(self) -> None:
        type(self).requests += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(self.payload)
        self.wfile.flush()

    def log_message(self, format_string: str, *args: object) -> None:
        del format_string, args


@contextmanager
def _local_device(status_code: int) -> Iterator[str]:
    """提供一个可返回真实可达或不可达状态的本地设备 socket。"""
    LocalDeviceHandler.status_code = status_code
    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalDeviceHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@contextmanager
def _local_inference() -> Iterator[str]:
    def action(signal: str, source_time: float) -> bytes:
        return (
            b"data: "
            + json.dumps(
                {
                    "choices": [
                        {
                            "delta": {"content": signal},
                            "chunk_metadata": {
                                "response": signal,
                                "start_time": source_time,
                                "first_timestamp": 1.0,
                            },
                        }
                    ]
                }
            ).encode("utf-8")
            + b"\n\n"
        )

    LocalInferenceHandler.requests = 0
    LocalInferenceHandler.payload = b"".join(
        (
            action("(1) start", 1.0),
            action("(2) done", 2.0),
            b"data: [DONE]\n\n",
        )
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalInferenceHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _create_target(
    client: Client,
    headers: dict[str, str],
    suffix: str,
    *,
    connector_address: str | None = None,
    connector_port: int = 80,
) -> Target:
    """通过公开 API 创建一台主机、一个工位和一个连接器。"""
    host = client.post(
        HOSTS,
        json={
            "name": f"装配A线-推理机-{suffix}",
            "address": f"10.0.8.{10 + int(suffix)}",
            "mediamtx_address": None,
            "recording_window_seconds": 7 * 24 * 3600,
            "disk_watermark_percent": 85,
        },
        headers=headers,
    )
    assert host.status_code == 201, host.text
    host_id = host.json()["id"]

    station = client.post(
        STATIONS,
        json={"code": f"A-{suffix}", "name": f"装配工位-{suffix}", "tags": []},
        headers=headers,
    )
    assert station.status_code == 201, station.text
    station_id = station.json()["id"]

    connector = client.post(
        CONNECTORS,
        json={
            "name": f"一号连接器-{suffix}",
            "connector_type": "hikvision_isapi",
            "configuration": {
                "address": connector_address or f"10.0.8.{20 + int(suffix)}",
                "port": connector_port,
            },
            "station_id": station_id,
            "host_id": host_id,
        },
        headers=headers,
    )
    assert connector.status_code == 201, connector.text

    identity = _fixture_host_identity(10 + int(suffix))
    registered = client.post(
        f"{HOSTS}/{host_id}/identity-key",
        headers={**headers, "If-Match": "1"},
        json={"public_key": identity.public_key},
    )
    assert registered.status_code == 200, registered.text
    return {
        "host_id": host_id,
        "host_private_key": identity.private_key,
        "station_id": station_id,
        "connector_id": connector.json()["id"],
    }


def _edge_headers(
    host_id: str,
    host_private_key: str,
    *,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    claim_token: str | None = None,
) -> dict[str, str]:
    request = HostIdentityRequest(
        method=method,
        path=path,
        host_id=host_id,
        timestamp=int(time.time()),
        nonce=f"sys93-nonce-{next(_NONCES)}",
        body=body,
    )
    headers = {
        "X-Inference-Host-ID": host_id,
        "X-Inference-Host-Timestamp": str(request.timestamp),
        "X-Inference-Host-Nonce": request.nonce,
        "X-Inference-Host-Signature": sign_host_identity_request(
            request, private_key=host_private_key
        ),
    }
    if claim_token is not None:
        headers["X-Command-Claim-Token"] = claim_token
    return headers


@contextmanager
def _open_second_operator_session(client: Client) -> Iterator[Client]:
    with Client(base_url=str(client.base_url), verify=False, trust_env=False) as observer:
        opened = observer.post(
            "/api/v1/auth/session",
            json=ADMIN_CREDENTIALS,
        )
        assert opened.status_code == 201, opened.text
        yield observer


@contextmanager
def _open_edge_session(client: Client) -> Iterator[Client]:
    """用没有浏览器会话的客户端模拟推理机身份。"""
    with Client(base_url=str(client.base_url), verify=False, trust_env=False) as edge:
        yield edge


def _edge_post(
    client: Client,
    path: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> Response:
    """通过不携带操作员 Cookie 的真实 HTTPS 客户端回报。"""
    with _open_edge_session(client) as edge:
        return edge.post(path, headers=headers, json=payload)


def _assert_pending_command_document(document: dict[str, Any]) -> None:
    """独立校验公开命令 JSON 的完整字段集, 让异常或新增字段触发失败。"""
    assert frozenset(document) == _PENDING_COMMAND_FIELDS


def _enqueue(
    client: Client, headers: dict[str, str], connector_id: str, key: str
) -> dict[str, Any]:
    response = client.post(
        f"{CONNECTORS}/{connector_id}/connection-test",
        headers={**headers, "Idempotency-Key": key},
    )
    assert response.status_code == 202, response.text
    assert "claim_token" not in response.text
    assert "password" not in response.text.lower()
    document = cast(dict[str, Any], response.json())
    _assert_pending_command_document(document)
    return document


def _claim(client: Client, target: Target) -> dict[str, Any]:
    response = client.get(
        f"{COMMANDS}/next",
        headers=_edge_headers(
            target["host_id"],
            target["host_private_key"],
            method="GET",
            path=f"{COMMANDS}/next",
        ),
    )
    assert response.status_code == 200, response.text
    assert "password" not in response.text.lower()
    document = cast(dict[str, Any], response.json())
    assert document == connection_test_claim_to_wire(connection_test_claim_from_wire(document))
    return document


def test_delegated_command_is_persistent_and_host_scoped(
    client: Client, bootstrap: BootstrapCommand, log: io.StringIO
) -> None:
    log_in(client, bootstrap)
    headers = csrf_header(client)
    owner = _create_target(client, headers, "1")
    other = _create_target(client, headers, "2")

    queued = _enqueue(client, headers, owner["connector_id"], "sys93-host-isolation-1")
    assert queued["status"] == "pending"
    assert queued["host_id"] == owner["host_id"]
    assert queued["target_id"] == owner["connector_id"]
    assert queued["target_revision"] == 1

    # 新浏览器会话从真实 PostgreSQL 中心读取同一行。
    with _open_second_operator_session(client) as observer:
        persisted = observer.get(f"{COMMANDS}/{queued['id']}")
        assert persisted.status_code == 200, persisted.text
        assert persisted.json() == queued
        assert "claim_token" not in persisted.text
        assert "password" not in persisted.text.lower()

    with _open_edge_session(client) as edge:
        other_claim = edge.get(
            f"{COMMANDS}/next",
            headers=_edge_headers(
                other["host_id"],
                other["host_private_key"],
                method="GET",
                path=f"{COMMANDS}/next",
            ),
        )
        assert other_claim.status_code == 204

        owner_claim = _claim(edge, owner)
        assert owner_claim["command"]["command_id"] == queued["id"]
        assert owner_claim["command"]["connector_id"] == owner["connector_id"]
        assert owner_claim["command"]["connector_revision"] == 1
        assert owner_claim["command"]["configuration"] == {
            "address": "10.0.8.21",
            "port": 80,
        }
        assert owner_claim["claim_token"]
        assert "credentials_configured" not in owner_claim["command"]

    assert "password" not in log.getvalue().lower()


def test_connector_configuration_and_type_change_before_edge_claim_is_rejected_on_real_center(
    client: Client, bootstrap: BootstrapCommand
) -> None:
    log_in(client, bootstrap)
    headers = csrf_header(client)
    target = _create_target(client, headers, "9")
    queued = _enqueue(client, headers, target["connector_id"], "sys93-revision-before-claim-1")

    edited = client.patch(
        f"{CONNECTORS}/{target['connector_id']}",
        headers={**headers, "If-Match": "1"},
        json={
            "name": "领取前修改的连接器-9",
            "connector_type": "board_card",
            "configuration": {"address": "10.0.8.109", "port": 80},
            "station_id": target["station_id"],
            "host_id": target["host_id"],
        },
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["revision"] == 2

    with _open_edge_session(client) as edge:
        claim = edge.get(
            f"{COMMANDS}/next",
            headers=_edge_headers(
                target["host_id"],
                target["host_private_key"],
                method="GET",
                path=f"{COMMANDS}/next",
            ),
        )
    assert claim.status_code == 204, claim.text

    status = client.get(f"{COMMANDS}/{queued['id']}")
    assert status.status_code == 200, status.text
    document = cast(dict[str, Any], status.json())
    _assert_pending_command_document(document)
    assert document["status"] == "rejected"
    assert document["failure_code"] == "COMMAND_CONFIGURATION_CHANGED"
    assert document["result"] is None

    connector = client.get(f"{CONNECTORS}/{target['connector_id']}")
    assert connector.status_code == 200, connector.text
    assert connector.json()["revision"] == 2
    assert connector.json()["reachability"] == "unverified"


def test_duplicate_result_is_idempotent_over_real_center_http(
    client: Client, bootstrap: BootstrapCommand
) -> None:
    log_in(client, bootstrap)
    headers = csrf_header(client)
    target = _create_target(client, headers, "3")
    queued = _enqueue(client, headers, target["connector_id"], "sys93-lease-retry-1")

    with _open_edge_session(client) as edge:
        claim = _claim(edge, target)
        assert claim["command"]["command_id"] == queued["id"]
        status = client.get(f"{COMMANDS}/{queued['id']}")
        assert status.status_code == 200, status.text
        assert status.json()["attempt"] == 1

        result = {
            "outcome": "reachable",
            "detail": "本地真实探测成功",
            "credentials_configured": True,
            "failure_code": None,
        }
        completed = edge.post(
            f"{COMMANDS}/{queued['id']}/result",
            headers=_edge_headers(
                target["host_id"],
                target["host_private_key"],
                method="POST",
                path=f"{COMMANDS}/{queued['id']}/result",
                body=result,
                claim_token=claim["claim_token"],
            ),
            json=result,
        )
        assert completed.status_code == 200, completed.text
        completed_document = cast(dict[str, Any], completed.json())
        _assert_pending_command_document(completed_document)
        assert completed_document["status"] == "succeeded"
        assert "password" not in completed.text.lower()

        repeated = edge.post(
            f"{COMMANDS}/{queued['id']}/result",
            headers=_edge_headers(
                target["host_id"],
                target["host_private_key"],
                method="POST",
                path=f"{COMMANDS}/{queued['id']}/result",
                body=result,
                claim_token=claim["claim_token"],
            ),
            json=result,
        )
        assert repeated.status_code == 200, repeated.text
        repeated_document = cast(dict[str, Any], repeated.json())
        _assert_pending_command_document(repeated_document)
        assert repeated_document == completed_document

        conflicting = edge.post(
            f"{COMMANDS}/{queued['id']}/result",
            headers=_edge_headers(
                target["host_id"],
                target["host_private_key"],
                method="POST",
                path=f"{COMMANDS}/{queued['id']}/result",
                body={**result, "detail": "另一份不同的回报"},
                claim_token=claim["claim_token"],
            ),
            json={**result, "detail": "另一份不同的回报"},
        )
        assert conflicting.status_code == 409, conflicting.text
        assert conflicting.json()["error_code"] == "COMMAND_ALREADY_COMPLETED"

    connector = client.get(f"{CONNECTORS}/{target['connector_id']}")
    assert connector.status_code == 200, connector.text
    assert connector.json()["reachability"] == "reachable"


def test_concurrent_http_results_serialize_before_connector_health_is_written(
    client: Client, bootstrap: BootstrapCommand
) -> None:
    """Two real edge HTTP clients yield one winner and never commit the loser's health fact."""
    log_in(client, bootstrap)
    headers = csrf_header(client)
    target = _create_target(client, headers, "7")
    queued = _enqueue(client, headers, target["connector_id"], "sys93-concurrent-result-1")
    claim = _claim(client, target)
    result_path = f"{COMMANDS}/{queued['id']}/result"
    base_url = str(client.base_url)

    def report(detail: str) -> Response:
        result = {
            "outcome": "reachable",
            "detail": detail,
            "credentials_configured": True,
            "failure_code": None,
        }
        with Client(base_url=base_url, verify=False, trust_env=False) as edge:
            return edge.post(
                result_path,
                headers=_edge_headers(
                    target["host_id"],
                    target["host_private_key"],
                    method="POST",
                    path=f"{COMMANDS}/{queued['id']}/result",
                    body=result,
                    claim_token=claim["claim_token"],
                ),
                json=result,
            )

    with ThreadPoolExecutor(max_workers=2) as workers:
        responses = list(workers.map(report, ("并发结果-A", "并发结果-B")))

    assert sorted(response.status_code for response in responses) == [200, 409]
    completed = next(response for response in responses if response.status_code == 200)
    conflict = next(response for response in responses if response.status_code == 409)
    completed_document = cast(dict[str, Any], completed.json())
    assert completed_document["status"] == "succeeded"
    assert conflict.json()["error_code"] == "COMMAND_ALREADY_COMPLETED"

    connector = client.get(f"{CONNECTORS}/{target['connector_id']}")
    assert connector.status_code == 200, connector.text
    connector_document = cast(dict[str, Any], connector.json())
    assert connector_document["reachability"] == "reachable"
    assert connector_document["health_detail"] == completed_document["result_detail"]


def test_configuration_change_and_edge_rejection_are_visible_without_health_mutation(
    client: Client, bootstrap: BootstrapCommand
) -> None:
    log_in(client, bootstrap)
    headers = csrf_header(client)
    changed = _create_target(client, headers, "4")
    queued = _enqueue(client, headers, changed["connector_id"], "sys93-revision-guard-1")
    claim = _claim(client, changed)

    edited = client.patch(
        f"{CONNECTORS}/{changed['connector_id']}",
        headers={**headers, "If-Match": "1"},
        json={
            "name": "改过的连接器-4",
            "connector_type": "hikvision_isapi",
            "configuration": {"address": "10.0.8.99", "port": 80},
            "station_id": changed["station_id"],
            "host_id": changed["host_id"],
        },
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["revision"] == 2

    late = _edge_post(
        client,
        f"{COMMANDS}/{queued['id']}/result",
        headers=_edge_headers(
            changed["host_id"],
            changed["host_private_key"],
            method="POST",
            path=f"{COMMANDS}/{queued['id']}/result",
            body={
                "outcome": "reachable",
                "detail": "迟到的真实结果",
                "credentials_configured": True,
                "failure_code": None,
            },
            claim_token=claim["claim_token"],
        ),
        payload={
            "outcome": "reachable",
            "detail": "迟到的真实结果",
            "credentials_configured": True,
            "failure_code": None,
        },
    )
    assert late.status_code == 200, late.text
    late_document = cast(dict[str, Any], late.json())
    _assert_pending_command_document(late_document)
    assert late_document["status"] == "rejected"
    assert late_document["failure_code"] == "COMMAND_CONFIGURATION_CHANGED"
    assert "claim_token" not in late.text

    unchanged_health = client.get(f"{CONNECTORS}/{changed['connector_id']}")
    assert unchanged_health.status_code == 200, unchanged_health.text
    assert unchanged_health.json()["revision"] == 2
    assert unchanged_health.json()["reachability"] == "unverified"

    rejected_target = _create_target(client, headers, "5")
    rejected_command = _enqueue(
        client, headers, rejected_target["connector_id"], "sys93-rejection-visible-1"
    )
    rejected_claim = _claim(client, rejected_target)
    rejected = _edge_post(
        client,
        f"{COMMANDS}/{rejected_command['id']}/result",
        headers=_edge_headers(
            rejected_target["host_id"],
            rejected_target["host_private_key"],
            method="POST",
            path=f"{COMMANDS}/{rejected_command['id']}/result",
            body={
                "outcome": "rejected",
                "detail": "推理机未配置该连接器凭据",
                "credentials_configured": False,
                "failure_code": "COMMAND_CREDENTIALS_NOT_CONFIGURED",
            },
            claim_token=rejected_claim["claim_token"],
        ),
        payload={
            "outcome": "rejected",
            "detail": "推理机未配置该连接器凭据",
            "credentials_configured": False,
            "failure_code": "COMMAND_CREDENTIALS_NOT_CONFIGURED",
        },
    )
    assert rejected.status_code == 200, rejected.text
    rejected_document = cast(dict[str, Any], rejected.json())
    _assert_pending_command_document(rejected_document)
    assert rejected_document["status"] == "rejected"
    assert rejected_document["result"] is None
    assert rejected_document["failure_code"] == "COMMAND_CREDENTIALS_NOT_CONFIGURED"
    assert rejected_document["result_detail"] == "推理机未配置该连接器凭据"

    with _open_second_operator_session(client) as observer:
        visible = observer.get(f"{COMMANDS}/{rejected_command['id']}")
        assert visible.status_code == 200, visible.text
        visible_document = cast(dict[str, Any], visible.json())
        _assert_pending_command_document(visible_document)
        assert visible_document["status"] == "rejected"
        assert visible_document["failure_code"] == "COMMAND_CREDENTIALS_NOT_CONFIGURED"
        assert "claim_token" not in visible.text
        assert "password" not in visible.text.lower()

    untouched = client.get(f"{CONNECTORS}/{rejected_target['connector_id']}")
    assert untouched.status_code == 200, untouched.text
    assert untouched.json()["reachability"] == "unverified"


def test_production_runner_uses_the_real_local_connector_and_reports_both_outcomes(
    client: Client, bootstrap: BootstrapCommand
) -> None:
    log_in(client, bootstrap)
    headers = csrf_header(client)

    with _local_device(200) as device_url:
        device_port = urlsplit(device_url).port
        assert device_port is not None
        reachable_target = _create_target(
            client,
            headers,
            "6",
            connector_address="127.0.0.1",
            connector_port=device_port,
        )
        reachable_command = _enqueue(
            client, headers, reachable_target["connector_id"], "sys93-runner-reachable-1"
        )
        loop = build_connection_test_loop(
            center_url=str(client.base_url).rstrip("/"),
            host_id=reachable_target["host_id"],
            host_private_key=reachable_target["host_private_key"],
            command_timeout=2.0,
            command_poll_interval=0.1,
            ssl_context=ssl._create_unverified_context(),
            local_connectors=(
                LocalIsapiConnectorConfiguration(
                    connector_id=reachable_target["connector_id"],
                    revision=1,
                    credentials_configured=True,
                    base_url=device_url,
                    username="edge-user",
                    password="edge-password",  # pragma: allowlist secret
                    profile=CANDIDATE_PROFILE,
                    capability=Unverified(),
                ),
            ),
        )

        assert loop.run_once()
        completed = client.get(f"{COMMANDS}/{reachable_command['id']}")
        assert completed.status_code == 200, completed.text
        assert completed.json()["status"] == "succeeded"

    with _local_device(503) as device_url:
        device_port = urlsplit(device_url).port
        assert device_port is not None
        unreachable_target = _create_target(
            client,
            headers,
            "7",
            connector_address="127.0.0.1",
            connector_port=device_port,
        )
        unreachable_command = _enqueue(
            client, headers, unreachable_target["connector_id"], "sys93-runner-unreachable-1"
        )
        loop = build_connection_test_loop(
            center_url=str(client.base_url).rstrip("/"),
            host_id=unreachable_target["host_id"],
            host_private_key=unreachable_target["host_private_key"],
            command_timeout=2.0,
            command_poll_interval=0.1,
            ssl_context=ssl._create_unverified_context(),
            local_connectors=(
                LocalIsapiConnectorConfiguration(
                    connector_id=unreachable_target["connector_id"],
                    revision=1,
                    credentials_configured=True,
                    base_url=device_url,
                    username="edge-user",
                    password="edge-password",  # pragma: allowlist secret
                    profile=CANDIDATE_PROFILE,
                    capability=Unverified(),
                ),
            ),
        )

        assert loop.run_once()
        completed = client.get(f"{COMMANDS}/{unreachable_command['id']}")
        assert completed.status_code == 200, completed.text
        assert completed.json()["status"] == "failed"
        assert completed.json()["result"] == "unreachable"

    connector = client.get(f"{CONNECTORS}/{unreachable_target['connector_id']}")
    assert connector.status_code == 200, connector.text
    assert connector.json()["reachability"] == "unreachable"


def test_production_entrypoint_runs_the_autonomous_loop_in_a_process(
    client: Client, bootstrap: BootstrapCommand, tmp_path: Path
) -> None:
    log_in(client, bootstrap)
    headers = csrf_header(client)

    with _local_device(200) as device_url, _local_inference() as inference_url:
        device_port = urlsplit(device_url).port
        assert device_port is not None
        target = _create_target(
            client,
            headers,
            "8",
            connector_address="127.0.0.1",
            connector_port=device_port,
        )
        queued = _enqueue(client, headers, target["connector_id"], "sys93-entrypoint-1")

        host_private_key_file = tmp_path / "host-private-key"
        host_private_key_file.write_text(target["host_private_key"] + "\n", encoding="utf-8")
        username_file = tmp_path / "connector-username"
        username_file.write_text("edge-user\n", encoding="utf-8")
        password_file = tmp_path / "connector-password"
        password_file.write_text("edge-password\n", encoding="utf-8")  # pragma: allowlist secret

        profile = {
            "input_status_path": CANDIDATE_PROFILE.input_status_path,
            "output_trigger_path": CANDIDATE_PROFILE.output_trigger_path,
            "output_body": CANDIDATE_PROFILE.output_body,
            "device_info_path": CANDIDATE_PROFILE.device_info_path,
            "input_state_element": CANDIDATE_PROFILE.input_state_element,
            "input_tokens": [
                {"token": token, "state": state.value}
                for token, state in CANDIDATE_PROFILE.input_tokens
            ],
            "output_tokens": [
                {"state": state.value, "token": token}
                for state, token in CANDIDATE_PROFILE.output_tokens
            ],
        }
        local_state_path = tmp_path / "local-state.sqlite"
        config = {
            "center_url": str(client.base_url).rstrip("/"),
            "center_ca_file": str(client.nvsop_tls_certificate),
            "host_id": target["host_id"],
            "host_private_key_file": str(host_private_key_file),
            "local_state_path": str(local_state_path),
            "stations": [
                {
                    "station_id": "station-entrypoint-8",
                    "inference_url": inference_url,
                    "request": {"stream": True, "messages": []},
                    "template": {
                        "steps": ["(1) start", "(2) done"],
                        "ordering": "ordered",
                        "start_signal": "(1) start",
                        "end_signals": ["(2) done"],
                    },
                    "parameters": {"idle_timeout": 10.0, "step_deadline": 10.0},
                    "margins": {"leading": 0.0, "trailing": 0.0},
                }
            ],
            "command_timeout_seconds": 2.0,
            "command_poll_interval_seconds": 0.05,
            "connectors": [
                {
                    "connector_id": target["connector_id"],
                    "revision": 1,
                    "credentials_configured": True,
                    "base_url": device_url,
                    "username_file": str(username_file),
                    "password_file": str(password_file),
                    "profile": profile,
                    "capability": capability_to_wire(Unverified()),
                }
            ],
        }
        config_file = tmp_path / "edge-command-loop.json"
        config_file.write_text(json.dumps(config), encoding="utf-8")

        repo_root = Path(__file__).resolve().parents[2]
        edge_source = repo_root / "apps" / "edge-runtime" / "src"
        contracts_source = repo_root / "packages" / "contracts" / "src"
        process = subprocess.Popen(
            [sys.executable, "-m", "edge_runtime"],
            cwd=repo_root,
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join((str(edge_source), str(contracts_source))),
                "NVSOP_EDGE_COMMAND_CONFIG_FILE": str(config_file),
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
                "PYTHONUNBUFFERED": "1",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10.0
            completed: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    raise AssertionError(
                        f"edge runtime exited before reporting: {process.returncode}\n"
                        f"stdout={stdout}\nstderr={stderr}"
                    )
                status = client.get(f"{COMMANDS}/{queued['id']}")
                assert status.status_code == 200, status.text
                document = cast(dict[str, Any], status.json())
                _assert_pending_command_document(document)
                if document["status"] == "succeeded":
                    completed = document
                    break
                time.sleep(0.05)
            assert completed is not None, "production edge entrypoint did not complete the command"
        finally:
            if process.poll() is None:
                process.terminate()
            stdout, stderr = process.communicate(timeout=10)
            assert "edge-password" not in stdout
            assert "edge-password" not in stderr
            assert process.returncode == 0, f"stdout={stdout}\nstderr={stderr}"

        assert LocalInferenceHandler.requests >= 1

        connector = client.get(f"{CONNECTORS}/{target['connector_id']}")
        assert connector.status_code == 200, connector.text
        assert connector.json()["reachability"] == "reachable"
