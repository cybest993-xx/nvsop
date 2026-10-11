"""证据授权协议经真实 HTTPS Nginx、中心进程与 PostgreSQL，不涉及媒体代理。"""

import json
import subprocess
import sys
from pathlib import Path

from conftest import BootstrapCommand, _free_port, _stop_server, _wait_for_server, log_in
from httpx2 import Client, Response
from test_sys_machine_identity_over_nginx_https import (
    _create_host,
    _create_owned_topology,
    _nginx_gateway,
    _signed_headers,
)

ORIGIN = "https://evidence-host.internal:8765"


def test_evidence_authorization_crosses_real_https_gateway(
    client: Client,
    bootstrap: BootstrapCommand,
    deployment_environ: dict[str, str],
    tls_certificate: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    log_in(client, bootstrap)
    browser_headers = {"x-csrf-token": client.cookies["sop_csrf"]}
    host_id, private_key = _create_host(client, browser_headers)
    station_id, _, _, _ = _create_owned_topology(client, browser_headers, host_id)
    # 主机创建后才知道 UUID；按正式部署入口启动有 origin 映射的 API，共用真实数据库。
    port = _free_port()
    base_url = f"https://127.0.0.1:{port}"
    certificate, key = tls_certificate
    stdout_path, stderr_path = tmp_path / "evidence.stdout", tmp_path / "evidence.stderr"
    with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "--factory",
                "factory_sop.entrypoint:build",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--ssl-certfile",
                str(certificate),
                "--ssl-keyfile",
                str(key),
                "--no-access-log",
            ],
            env=deployment_environ
            | {
                "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "apps/control-api/src"),
                "SOP_EVIDENCE_HOST_ORIGINS": json.dumps({host_id: ORIGIN}),
            },
            stdout=stdout,
            stderr=stderr,
        )
        try:
            _wait_for_server(
                process, base_url=base_url, stdout_path=stdout_path, stderr_path=stderr_path
            )
            with (
                Client(base_url=base_url, verify=False, trust_env=False) as configured,
                _nginx_gateway(configured, tmp_path, gateway_scheme="https") as browser,
                Client(base_url=str(browser.base_url), verify=False, trust_env=False) as host,
            ):
                registration_path = "/api/v1/evidence/registrations"
                evidence_id = f"{host_id}-gateway-clip"
                registration = {
                    "evidence_id": evidence_id,
                    "host_id": host_id,
                    "station_id": station_id,
                    "instance_id": 7,
                    "violation_id": None,
                    "kind": "clip",
                    "origin": "automatic",
                    "anchor": 1.0,
                    "window_start": 0.0,
                    "window_end": 2.0,
                    "generation": "original",
                    "sha256": "a" * 64,
                    "size": 10,
                    "reference": "artifact/gateway.mp4",
                }

                def signed_post(path: str, body: dict[str, object]) -> Response:
                    return host.post(
                        path,
                        json=body,
                        headers=_signed_headers(
                            host_id=host_id,
                            private_key=private_key,
                            method="POST",
                            path=path,
                            body=body,
                        ),
                    )

                registered = signed_post(registration_path, registration)
                assert registered.status_code == 200, registered.text
                # 机器路由无 Cookie 仍通过签名；浏览器签发始终需要中心会话。
                context_path = f"/api/v1/evidence/{evidence_id}/access-context"
                assert browser.get(context_path).status_code == 401
                browser.cookies.update(client.cookies)
                context = browser.get(context_path)
                assert context.status_code == 200, context.text
                assert context.headers["Cache-Control"] == "no-store"
                assert context.json()["origin"] == ORIGIN
                check_path = "/api/v1/evidence/access-check"
                body = {"evidence_id": evidence_id, "context": context.json()["context"]}
                granted = signed_post(check_path, body)
                assert granted.status_code == 200, granted.text
                assert granted.json() == {
                    "reference": "artifact/gateway.mp4",
                    "sha256": "a" * 64,
                    "size": 10,
                    "kind": "clip",
                }
                for headers in (
                    {"X-Inference-Host-ID": host_id},
                    _signed_headers(
                        host_id=host_id,
                        private_key=private_key,
                        method="POST",
                        path=check_path,
                        body={"evidence_id": "forged", "context": "x"},
                    ),
                ):
                    rejected = host.post(check_path, json=body, headers=headers)
                    assert rejected.status_code == 401, rejected.text
                    assert rejected.json()["error_code"] == "INFERENCE_HOST_AUTHENTICATION_FAILED"
                # 额外路径字段不能变成文件接口。
                assert signed_post(check_path, body | {"path": "/etc/passwd"}).status_code == 422
        finally:
            _stop_server(process)
