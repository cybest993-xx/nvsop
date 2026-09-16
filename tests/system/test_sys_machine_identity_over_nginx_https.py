"""主机签名 Edge 流量先经过仓库 Nginx HTTP/HTTPS 入口，再到 FastAPI。"""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from io import BytesIO
from itertools import count
from pathlib import Path
from random import Random
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from conftest import (
    ADMIN_CREDENTIALS,
    BootstrapCommand,
    _free_port,
    _make_tls_certificate,
    csrf_header,
)
from httpx2 import Client, HTTPError, Response

from nvsop_contracts import (
    HostIdentityRequest,
    generate_host_identity_key_pair,
    sign_host_identity_request,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
NGINX_CONFIG = REPO_ROOT / "deploy/dev/nginx.conf"
NGINX_IMAGE = "nginx:1.27.3-alpine"
HOSTS = "/api/v1/inference-hosts"
BACKENDS = "/api/v1/inference-backends"
STATIONS = "/api/v1/stations"
CAMERAS = "/api/v1/cameras"
CONNECTORS = "/api/v1/connectors"
COMMANDS = "/api/v1/device-commands"
TEMPLATES = "/api/v1/templates"
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_NONCES = count()


def _fixture_host_identity() -> tuple[str, str]:
    random = Random(119)
    with (
        patch("nvsop_contracts.host_identity.secrets.randbits", side_effect=random.getrandbits),
        patch("nvsop_contracts.host_identity.secrets.randbelow", side_effect=random.randrange),
    ):
        key_pair = generate_host_identity_key_pair()
    return key_pair.public_key, key_pair.private_key


def _signed_headers(
    *,
    host_id: str,
    private_key: str,
    method: str,
    path: str,
    body: Mapping[str, object] | None = None,
    timestamp: int | None = None,
    nonce: str | None = None,
    claim_token: str | None = None,
) -> dict[str, str]:
    request = HostIdentityRequest(
        method=method,
        path=path,
        host_id=host_id,
        timestamp=int(time.time()) if timestamp is None else timestamp,
        nonce=nonce or f"sys-nginx-machine-{next(_NONCES)}",
        body=body,
    )
    headers = {
        "X-Inference-Host-ID": host_id,
        "X-Inference-Host-Timestamp": str(request.timestamp),
        "X-Inference-Host-Nonce": request.nonce,
        "X-Inference-Host-Signature": sign_host_identity_request(request, private_key=private_key),
    }
    if claim_token is not None:
        headers["X-Command-Claim-Token"] = claim_token
    return headers


def _free_gateway_ports() -> tuple[int, int, int]:
    ports: list[int] = []
    while len(ports) < 3:
        candidate = _free_port()
        if candidate not in ports:
            ports.append(candidate)
    return ports[0], ports[1], ports[2]


def _render_nginx_config(
    center: Client, tmp_path: Path, *, gateway_scheme: str
) -> tuple[Path, Path | None, int]:
    parsed = urlsplit(str(center.base_url))
    assert parsed.hostname == "127.0.0.1"
    assert parsed.port is not None
    gateway_port, media_port, minio_port = _free_gateway_ports()

    source = (
        NGINX_CONFIG.read_text(encoding="utf-8")
        .replace("center-api:8000", f"127.0.0.1:{parsed.port}")
        .replace("web:8080", "127.0.0.1:9")
        .replace("annotation-backend:8100", "127.0.0.1:9")
        .replace("annotation-frontend:80", "127.0.0.1:9")
        .replace("minio:9000", "127.0.0.1:9")
        .replace("proxy_pass http://nvsop_center_api", "proxy_pass https://nvsop_center_api")
    )
    tls: Path | None = None
    if gateway_scheme == "https":
        tls = tmp_path / "nginx-tls"
        tls.mkdir()
        _make_tls_certificate(tls)
        source = (
            source.replace("8443__NVSOP_LISTEN_SUFFIX__", f"{gateway_port} ssl")
            .replace("8444__NVSOP_LISTEN_SUFFIX__", f"{media_port} ssl")
            .replace("9443__NVSOP_LISTEN_SUFFIX__", f"{minio_port} ssl")
            .replace(
                "    # __NVSOP_TLS_DIRECTIVES__\n",
                "    ssl_certificate /nvsop-tls/uvicorn.crt;\n"
                "    ssl_certificate_key /nvsop-tls/uvicorn.key;\n",
            )
        )
    else:
        assert gateway_scheme == "http"
        source = (
            source.replace("8443__NVSOP_LISTEN_SUFFIX__", str(gateway_port))
            .replace("8444__NVSOP_LISTEN_SUFFIX__", str(media_port))
            .replace("9443__NVSOP_LISTEN_SUFFIX__", str(minio_port))
            .replace("    # __NVSOP_TLS_DIRECTIVES__\n", "")
        )

    rendered = tmp_path / f"nginx-{gateway_scheme}.conf"
    rendered.write_text(source, encoding="utf-8")
    return rendered, tls, gateway_port


@contextmanager
def _nginx_gateway(center: Client, tmp_path: Path, *, gateway_scheme: str) -> Iterator[Client]:
    docker = shutil.which("docker")
    assert docker is not None, "system gateway evidence requires Docker"
    config, tls, gateway_port = _render_nginx_config(
        center, tmp_path, gateway_scheme=gateway_scheme
    )
    name = f"nvsop-machine-gateway-{uuid4().hex[:12]}"
    command = [
        docker,
        "run",
        "--rm",
        "--name",
        name,
        "--network",
        "host",
        "-v",
        f"{config}:/etc/nginx/conf.d/default.conf:ro",
    ]
    if tls is not None:
        command.extend(["-v", f"{tls}:/nvsop-tls:ro"])
    command.append(NGINX_IMAGE)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    base_url = f"{gateway_scheme}://127.0.0.1:{gateway_port}"
    deadline = time.monotonic() + 30
    try:
        with Client(base_url=base_url, verify=False, trust_env=False, timeout=2.0) as probe:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    raise AssertionError(
                        "nginx exited before liveness succeeded\n"
                        f"stdout:\n{stdout}\nstderr:\n{stderr}"
                    )
                try:
                    if probe.get("/api/v1/liveness").status_code == 200:
                        break
                except HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise AssertionError(f"nginx {gateway_scheme.upper()} gateway did not become ready")
        with Client(base_url=base_url, verify=False, trust_env=False, timeout=10.0) as gateway:
            yield gateway
    finally:
        subprocess.run(
            [docker, "stop", "-t", "1", name],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def _create_host(gateway: Client, browser_headers: dict[str, str]) -> tuple[str, str]:
    created = gateway.post(
        HOSTS,
        headers=browser_headers,
        json={
            "name": "Nginx 机器身份测试主机",
            "address": "10.0.8.119",
            "mediamtx_address": None,
            "recording_window_seconds": 7 * 24 * 3600,
            "disk_watermark_percent": 85,
        },
    )
    assert created.status_code == 201, created.text
    host_id = created.json()["id"]
    public_key, private_key = _fixture_host_identity()
    registered = gateway.post(
        f"{HOSTS}/{host_id}/identity-key",
        headers={**browser_headers, "If-Match": "1"},
        json={"public_key": public_key},
    )
    assert registered.status_code == 200, registered.text
    return host_id, private_key


def _template_workbook(*, station_code: str, station_name: str) -> bytes:
    """构造本场景需要的最小合成模板工作簿。"""
    sheets = {
        "工位表": [["工位号", "工位名称"], [station_code, station_name]],
        "步骤表": [
            ["工位号", "步骤号", "步骤名称", "步骤描述"],
            [station_code, "1", "取料", "(1)取料"],
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


def _send_machine_request(
    edge: Client,
    *,
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: Mapping[str, object] | None,
) -> Response:
    if body is None:
        return edge.request(method, path, headers=headers)
    return edge.request(method, path, headers=headers, json=body)


def _assert_signature_rejection_matrix(
    edge: Client,
    *,
    host_id: str,
    private_key: str,
    method: str,
    path: str,
    body: Mapping[str, object] | None = None,
    claim_token: str | None = None,
    success_status: int,
) -> Response:
    """每条机器路由都由 FastAPI 主机签名边界拒绝五类无效身份。"""
    missing_headers = {"X-Inference-Host-ID": host_id}
    if claim_token is not None:
        missing_headers["X-Command-Claim-Token"] = claim_token
    missing = _send_machine_request(
        edge, method=method, path=path, headers=missing_headers, body=body
    )
    assert missing.status_code == 401, missing.text

    forged_headers = _signed_headers(
        host_id=host_id,
        private_key=private_key,
        method=method,
        path=path,
        body=body,
        claim_token=claim_token,
    )
    forged_headers["X-Inference-Host-Signature"] = "invalid-signature"
    forged = _send_machine_request(
        edge, method=method, path=path, headers=forged_headers, body=body
    )
    assert forged.status_code == 401, forged.text

    expired_headers = _signed_headers(
        host_id=host_id,
        private_key=private_key,
        method=method,
        path=path,
        body=body,
        timestamp=int(time.time()) - 600,
        claim_token=claim_token,
    )
    expired = _send_machine_request(
        edge, method=method, path=path, headers=expired_headers, body=body
    )
    assert expired.status_code == 401, expired.text

    wrong_host_headers = _signed_headers(
        host_id=str(uuid4()),
        private_key=private_key,
        method=method,
        path=path,
        body=body,
        claim_token=claim_token,
    )
    wrong_host = _send_machine_request(
        edge, method=method, path=path, headers=wrong_host_headers, body=body
    )
    assert wrong_host.status_code == 401, wrong_host.text

    replay_headers = _signed_headers(
        host_id=host_id,
        private_key=private_key,
        method=method,
        path=path,
        body=body,
        nonce=f"sys-nginx-replay-{next(_NONCES)}",
        claim_token=claim_token,
    )
    accepted = _send_machine_request(
        edge, method=method, path=path, headers=replay_headers, body=body
    )
    assert accepted.status_code == success_status, accepted.text
    replayed = _send_machine_request(
        edge, method=method, path=path, headers=replay_headers, body=body
    )
    assert replayed.status_code == 401, replayed.text
    return accepted


def _create_owned_topology(
    gateway: Client, browser_headers: dict[str, str], host_id: str
) -> tuple[str, int, str, str]:
    station = gateway.post(
        STATIONS,
        headers=browser_headers,
        json={"code": "NGINX-119", "name": "Nginx 网关工位", "tags": []},
    )
    assert station.status_code == 201, station.text
    station_body = station.json()
    station_id = station_body["id"]
    station_revision = int(station_body["revision"])

    backend = gateway.post(
        BACKENDS,
        headers=browser_headers,
        json={"host_id": host_id, "base_url": "http://127.0.0.1:19000"},
    )
    assert backend.status_code == 201, backend.text
    backend_id = backend.json()["id"]

    camera = gateway.post(
        CAMERAS,
        headers=browser_headers,
        json={
            "name": "Nginx 网关相机",
            "address": "127.0.0.1",
            "main_stream_path": "/Streaming/Channels/101",
            "sub_stream_path": "/Streaming/Channels/102",
            "station_id": station_id,
            "host_id": host_id,
            "backend_id": backend_id,
        },
    )
    assert camera.status_code == 201, camera.text

    connector = gateway.post(
        CONNECTORS,
        headers=browser_headers,
        json={
            "name": "Nginx 网关连接器",
            "connector_type": "hikvision_isapi",
            "configuration": {"address": "127.0.0.1", "port": 80},
            "station_id": station_id,
            "host_id": host_id,
        },
    )
    assert connector.status_code == 201, connector.text
    return station_id, station_revision, backend_id, connector.json()["id"]


def _create_bound_template(
    gateway: Client,
    browser_headers: dict[str, str],
    *,
    station_id: str,
    station_revision: int,
) -> tuple[str, str, int]:
    imported = gateway.post(
        f"{TEMPLATES}/imports",
        params={"filename": "nginx-machine-api.xlsx"},
        headers={**browser_headers, "Content-Type": XLSX_MEDIA_TYPE},
        content=_template_workbook(station_code="NGINX-119", station_name="Nginx 网关工位"),
    )
    assert imported.status_code == 201, imported.text
    draft = imported.json()["draft"]
    draft_id = draft["id"]
    edited = gateway.patch(
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
    published = gateway.post(
        f"{TEMPLATES}/drafts/{draft_id}/publish",
        headers={**browser_headers, "If-Match": str(edited.json()["revision"])},
    )
    assert published.status_code == 201, published.text
    version = published.json()
    bound = gateway.post(
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
    desired = bound.json()["desired"]
    return version["id"], version["sha256"], int(desired["config_revision"])


@pytest.mark.parametrize("gateway_scheme", ["http", "https"])
def test_host_signed_machine_api_crosses_real_nginx_gateway_without_browser_session(
    client: Client,
    bootstrap: BootstrapCommand,
    tmp_path: Path,
    gateway_scheme: str,
) -> None:
    bootstrapped = bootstrap.run(
        login_name=ADMIN_CREDENTIALS["login_name"],
        password=ADMIN_CREDENTIALS["password"],
        display_name="陈伟",
    )
    assert bootstrapped.returncode == 0, bootstrapped.stderr
    login = client.post("/api/v1/auth/session", json=ADMIN_CREDENTIALS)
    assert login.status_code == 201, login.text
    browser_headers = csrf_header(client)
    host_id, private_key = _create_host(client, browser_headers)
    station_id, station_revision, backend_id, connector_id = _create_owned_topology(
        client, browser_headers, host_id
    )
    version_id, version_sha256, template_config_revision = _create_bound_template(
        client,
        browser_headers,
        station_id=station_id,
        station_revision=station_revision,
    )

    with _nginx_gateway(client, tmp_path, gateway_scheme=gateway_scheme) as gateway:
        assert gateway.get("/api/v1/liveness").status_code == 200
        assert gateway.post("/api/v1/annotation/gateway-authorize").status_code == 404
        assert gateway.get(HOSTS).status_code == 401
        assert gateway.get("/annotation/").status_code == 401
        configuration_path = f"{HOSTS}/{host_id}/configuration"

        with Client(base_url=str(gateway.base_url), verify=False, trust_env=False) as edge:
            configuration = edge.get(
                configuration_path,
                headers=_signed_headers(
                    host_id=host_id,
                    private_key=private_key,
                    method="GET",
                    path=configuration_path,
                ),
            )
            assert configuration.status_code == 200, configuration.text
            configuration_body = configuration.json()
            assert configuration_body["host_id"] == host_id
            _assert_signature_rejection_matrix(
                edge,
                host_id=host_id,
                private_key=private_key,
                method="GET",
                path=configuration_path,
                success_status=200,
            )

            confirmed_configuration_path = f"{HOSTS}/{host_id}/confirmed-configuration"
            confirmed = _assert_signature_rejection_matrix(
                edge,
                host_id=host_id,
                private_key=private_key,
                method="POST",
                path=confirmed_configuration_path,
                body=configuration_body,
                success_status=200,
            )
            assert confirmed.json() == {"decision_report_contract_version": 2}

            health_path = "/api/v1/monitor/health"
            health: dict[str, object] = {
                "contract_version": 1,
                "event_id": f"sys-nginx-health-{gateway_scheme}",
                "trace_id": f"sys-nginx-trace-health-{gateway_scheme}",
                "host_id": host_id,
                "station_id": None,
                "status": "healthy",
                "reason_code": None,
                "detail": None,
                "reported_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }
            _assert_signature_rejection_matrix(
                edge,
                host_id=host_id,
                private_key=private_key,
                method="POST",
                path=health_path,
                body=health,
                success_status=200,
            )

            decision_path = "/api/v1/monitor/reported-decisions"
            decision: dict[str, object] = {
                "contract_version": 1,
                "event_id": f"sys-nginx-decision-{gateway_scheme}",
                "trace_id": f"sys-nginx-trace-decision-{gateway_scheme}",
                "host_id": host_id,
                "station_id": station_id,
                "backend_id": backend_id,
                "instance_id": 1,
                "verdict": "pass",
                "reason_codes": [],
                "violations": [],
                "lifecycle": "closed",
                "evidence": {"anchor": None, "start": None, "end": None},
                "template_version_id": None,
                "template_sha256": None,
                "model_ids": ["sys-nginx-model"],
                "reported_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }
            _assert_signature_rejection_matrix(
                edge,
                host_id=host_id,
                private_key=private_key,
                method="POST",
                path=decision_path,
                body=decision,
                success_status=200,
            )

            claim_path = f"{COMMANDS}/next"
            _assert_signature_rejection_matrix(
                edge,
                host_id=host_id,
                private_key=private_key,
                method="GET",
                path=claim_path,
                success_status=204,
            )

            queued = client.post(
                f"{CONNECTORS}/{connector_id}/connection-test",
                headers={
                    **browser_headers,
                    "Idempotency-Key": f"sys-nginx-command-{gateway_scheme}",
                },
            )
            assert queued.status_code == 202, queued.text
            command_id = queued.json()["id"]

            claim_path = f"{COMMANDS}/next"
            claim = edge.get(
                claim_path,
                headers=_signed_headers(
                    host_id=host_id,
                    private_key=private_key,
                    method="GET",
                    path=claim_path,
                ),
            )
            assert claim.status_code == 200, claim.text
            claim_token = claim.json()["claim_token"]

            result_path = f"{COMMANDS}/{command_id}/result"
            result: dict[str, object] = {
                "outcome": "reachable",
                "detail": f"real Nginx {gateway_scheme.upper()} machine path",
                "credentials_configured": True,
                "failure_code": None,
            }
            _assert_signature_rejection_matrix(
                edge,
                host_id=host_id,
                private_key=private_key,
                method="POST",
                path=result_path,
                body=result,
                claim_token=claim_token,
                success_status=200,
            )

            template_report_path = "/api/v1/templates/configuration-reports"
            template_report: dict[str, object] = {
                "station_id": station_id,
                "backend_id": backend_id,
                "version_id": version_id,
                "sha256": version_sha256,
                "config_revision": template_config_revision,
            }
            template_response = _assert_signature_rejection_matrix(
                edge,
                host_id=host_id,
                private_key=private_key,
                method="POST",
                path=template_report_path,
                body=template_report,
                success_status=200,
            )
            assert template_response.json()["accepted"] is True
