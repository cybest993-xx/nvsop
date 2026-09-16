"""主机签名 Edge 流量先经过仓库 Nginx HTTPS 入口，再到 FastAPI。"""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from random import Random
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

from conftest import (
    ADMIN_CREDENTIALS,
    BootstrapCommand,
    _free_port,
    _make_tls_certificate,
    csrf_header,
)
from httpx2 import Client, HTTPError

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


def _render_nginx_config(center: Client, tmp_path: Path) -> tuple[Path, Path, int]:
    parsed = urlsplit(str(center.base_url))
    assert parsed.hostname == "127.0.0.1"
    assert parsed.port is not None
    gateway_port, media_port, minio_port = _free_gateway_ports()

    tls = tmp_path / "nginx-tls"
    tls.mkdir()
    _make_tls_certificate(tls)

    source = NGINX_CONFIG.read_text(encoding="utf-8")
    source = (
        source.replace("center-api:8000", f"127.0.0.1:{parsed.port}")
        .replace("web:8080", "127.0.0.1:9")
        .replace("annotation-backend:8100", "127.0.0.1:9")
        .replace("annotation-frontend:80", "127.0.0.1:9")
        .replace("minio:9000", "127.0.0.1:9")
        .replace("proxy_pass http://nvsop_center_api", "proxy_pass https://nvsop_center_api")
        .replace("8443__NVSOP_LISTEN_SUFFIX__", f"{gateway_port} ssl")
        .replace("8444__NVSOP_LISTEN_SUFFIX__", f"{media_port} ssl")
        .replace("9443__NVSOP_LISTEN_SUFFIX__", f"{minio_port} ssl")
        .replace(
            "    # __NVSOP_TLS_DIRECTIVES__\n",
            "    ssl_certificate /nvsop-tls/uvicorn.crt;\n"
            "    ssl_certificate_key /nvsop-tls/uvicorn.key;\n",
        )
    )
    rendered = tmp_path / "nginx.conf"
    rendered.write_text(source, encoding="utf-8")
    return rendered, tls, gateway_port


@contextmanager
def _nginx_gateway(center: Client, tmp_path: Path) -> Iterator[Client]:
    docker = shutil.which("docker")
    assert docker is not None, "system gateway evidence requires Docker"
    config, tls, gateway_port = _render_nginx_config(center, tmp_path)
    name = f"nvsop-machine-gateway-{uuid4().hex[:12]}"
    process = subprocess.Popen(
        [
            docker,
            "run",
            "--rm",
            "--name",
            name,
            "--network",
            "host",
            "-v",
            f"{config}:/etc/nginx/conf.d/default.conf:ro",
            "-v",
            f"{tls}:/nvsop-tls:ro",
            NGINX_IMAGE,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    base_url = f"https://127.0.0.1:{gateway_port}"
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
                raise AssertionError("nginx HTTPS gateway did not become ready")
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


def _create_owned_topology(
    gateway: Client, browser_headers: dict[str, str], host_id: str
) -> tuple[str, str, str]:
    station = gateway.post(
        STATIONS,
        headers=browser_headers,
        json={"code": "NGINX-119", "name": "Nginx 网关工位", "tags": []},
    )
    assert station.status_code == 201, station.text
    station_id = station.json()["id"]

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
    return station_id, backend_id, connector.json()["id"]


def test_host_signed_machine_api_crosses_real_nginx_https_without_browser_session(
    client: Client,
    bootstrap: BootstrapCommand,
    tmp_path: Path,
) -> None:
    bootstrapped = bootstrap.run(
        login_name=ADMIN_CREDENTIALS["login_name"],
        password=ADMIN_CREDENTIALS["password"],
        display_name="陈伟",
    )
    assert bootstrapped.returncode == 0, bootstrapped.stderr

    with _nginx_gateway(client, tmp_path) as gateway:
        assert gateway.get("/api/v1/liveness").status_code == 200
        assert gateway.post("/api/v1/annotation/gateway-authorize").status_code == 404
        assert gateway.get(HOSTS).status_code == 401

        login = gateway.post("/api/v1/auth/session", json=ADMIN_CREDENTIALS)
        assert login.status_code == 201, login.text
        browser_headers = csrf_header(gateway)
        assert gateway.get(HOSTS).status_code == 200

        host_id, private_key = _create_host(gateway, browser_headers)
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
            assert configuration.json()["host_id"] == host_id

            confirmed_configuration_path = f"{HOSTS}/{host_id}/confirmed-configuration"
            confirmed_configuration_body = configuration.json()
            confirmed_configuration = edge.post(
                confirmed_configuration_path,
                headers=_signed_headers(
                    host_id=host_id,
                    private_key=private_key,
                    method="POST",
                    path=confirmed_configuration_path,
                    body=confirmed_configuration_body,
                ),
                json=confirmed_configuration_body,
            )
            # #146 owns the gateway route. Before #151 lands the owning FastAPI endpoint is absent;
            # after it lands the same signed request is accepted. Neither state may become browser
            # session authentication.
            assert confirmed_configuration.status_code in {200, 404}, confirmed_configuration.text

            forged_id_only = edge.get(
                configuration_path,
                headers={"X-Inference-Host-ID": host_id},
            )
            assert forged_id_only.status_code == 401

            missing_signature_headers = _signed_headers(
                host_id=host_id,
                private_key=private_key,
                method="GET",
                path=configuration_path,
            )
            missing_signature_headers.pop("X-Inference-Host-Signature")
            missing_signature = edge.get(configuration_path, headers=missing_signature_headers)
            assert missing_signature.status_code == 401

            wrong_signature_headers = _signed_headers(
                host_id=host_id,
                private_key=private_key,
                method="GET",
                path=configuration_path,
            )
            wrong_signature_headers["X-Inference-Host-Signature"] = "invalid-signature"
            wrong_signature = edge.get(configuration_path, headers=wrong_signature_headers)
            assert wrong_signature.status_code == 401

            wrong_host = edge.get(
                configuration_path,
                headers=_signed_headers(
                    host_id=str(uuid4()),
                    private_key=private_key,
                    method="GET",
                    path=configuration_path,
                ),
            )
            assert wrong_host.status_code == 401

            expired = edge.get(
                configuration_path,
                headers=_signed_headers(
                    host_id=host_id,
                    private_key=private_key,
                    method="GET",
                    path=configuration_path,
                    timestamp=int(time.time()) - 600,
                ),
            )
            assert expired.status_code == 401

            replay_headers = _signed_headers(
                host_id=host_id,
                private_key=private_key,
                method="GET",
                path=configuration_path,
                nonce="sys-nginx-replay",
            )
            assert edge.get(configuration_path, headers=replay_headers).status_code == 200
            assert edge.get(configuration_path, headers=replay_headers).status_code == 401

            health_path = "/api/v1/monitor/health"
            health: dict[str, object] = {
                "contract_version": 1,
                "event_id": "sys-nginx-health-1",
                "trace_id": "sys-nginx-trace-health",
                "host_id": host_id,
                "station_id": None,
                "status": "healthy",
                "reason_code": None,
                "detail": None,
                "reported_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }
            health_response = edge.post(
                health_path,
                headers=_signed_headers(
                    host_id=host_id,
                    private_key=private_key,
                    method="POST",
                    path=health_path,
                    body=health,
                ),
                json=health,
            )
            assert health_response.status_code == 200, health_response.text

        station_id, backend_id, connector_id = _create_owned_topology(
            gateway, browser_headers, host_id
        )

        with Client(base_url=str(gateway.base_url), verify=False, trust_env=False) as edge:
            decision_path = "/api/v1/monitor/reported-decisions"
            decision: dict[str, object] = {
                "contract_version": 1,
                "event_id": "sys-nginx-decision-1",
                "trace_id": "sys-nginx-trace-decision",
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
            decision_response = edge.post(
                decision_path,
                headers=_signed_headers(
                    host_id=host_id,
                    private_key=private_key,
                    method="POST",
                    path=decision_path,
                    body=decision,
                ),
                json=decision,
            )
            assert decision_response.status_code == 200, decision_response.text

            queued = gateway.post(
                f"{CONNECTORS}/{connector_id}/connection-test",
                headers={**browser_headers, "Idempotency-Key": "sys-nginx-command-1"},
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
                "detail": "real Nginx HTTPS machine path",
                "credentials_configured": True,
                "failure_code": None,
            }
            completed = edge.post(
                result_path,
                headers=_signed_headers(
                    host_id=host_id,
                    private_key=private_key,
                    method="POST",
                    path=result_path,
                    body=result,
                    claim_token=claim_token,
                ),
                json=result,
            )
            assert completed.status_code == 200, completed.text

            template_report_path = "/api/v1/templates/configuration-reports"
            template_report: dict[str, object] = {
                "station_id": station_id,
                "backend_id": backend_id,
                "version_id": str(uuid4()),
                "sha256": "0" * 64,
                "config_revision": 1,
            }
            template_response = edge.post(
                template_report_path,
                headers=_signed_headers(
                    host_id=host_id,
                    private_key=private_key,
                    method="POST",
                    path=template_report_path,
                    body=template_report,
                ),
                json=template_report,
            )
            assert template_response.status_code == 409, template_response.text
            assert template_response.json().get("error_code") != "AUTHENTICATION_REQUIRED"
