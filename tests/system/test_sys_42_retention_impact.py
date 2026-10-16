"""S042:真实 PG 来源投影、HTTPS 签名回查、SQLite 只读统计与确认失败路径。"""

from __future__ import annotations

import json
import sqlite3
import ssl
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from conftest import BootstrapCommand, _free_port, _stop_server, _wait_for_server, log_in
from edge_runtime.center_client import CenterClient
from edge_runtime.evidence_access import create_server
from edge_runtime.local_state import open_local_state
from httpx2 import Client
from test_sys_machine_identity_over_nginx_https import (
    _create_bound_template,
    _create_host,
    _create_owned_topology,
    _nginx_gateway,
    _signed_headers,
)

from nvsop_contracts import configuration_from_wire


def test_local_reported_estimate_crosses_real_sources_and_never_deletes(
    client: Client,
    bootstrap: BootstrapCommand,
    deployment_environ: dict[str, str],
    tls_certificate: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    log_in(client, bootstrap)
    browser_headers = {"x-csrf-token": client.cookies["sop_csrf"]}
    host_id, private_key = _create_host(client, browser_headers)
    station_id, station_revision, _, _ = _create_owned_topology(client, browser_headers, host_id)
    _create_bound_template(
        client, browser_headers, station_id=station_id, station_revision=station_revision
    )
    configuration_path = f"/api/v1/inference-hosts/{host_id}/configuration"
    pulled = client.get(
        configuration_path,
        headers=_signed_headers(
            host_id=host_id,
            private_key=private_key,
            method="GET",
            path=configuration_path,
        ),
    )
    assert pulled.status_code == 200, pulled.text
    bundle = configuration_from_wire(pulled.json())
    local_path = tmp_path / "edge-state.sqlite"
    state = open_local_state(str(local_path))
    try:
        state.configuration().confirm(bundle, confirmed_at=1.0)
    finally:
        state.close()
    ack = (datetime.now(UTC) - timedelta(days=4)).isoformat()
    with sqlite3.connect(local_path) as local:
        local.execute(
            """INSERT INTO local_health_queue
            (station_id,status,occurred_at,report_host_id,sent_at,acknowledged_at)
            VALUES (?, 'healthy', ?, ?, 10, ?)""",
            (station_id, ack, host_id, ack),
        )
        local.execute(
            """INSERT INTO local_health_queue
            (station_id,status,occurred_at,report_host_id,sent_at,acknowledged_at)
            VALUES (?, 'healthy', ?, ?, NULL, NULL)""",
            (station_id, ack, host_id),
        )
        local.execute(
            """INSERT INTO local_observation_queue
            (station_id,instance_id,source,signal,observed_at,report_host_id,sent_at,acknowledged_at)
            VALUES (?, 123, 'action', 'fixture', 1, ?, 10, ?)""",
            (station_id, host_id, ack),
        )
        local.row_factory = sqlite3.Row
        health = dict(local.execute("SELECT * FROM local_health_queue WHERE queue_id=1").fetchone())
    logical_bytes = len(
        json.dumps(health, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )
    evidence_directory = tmp_path / "evidence"
    evidence_directory.mkdir()
    evidence = evidence_directory / "synthetic.mp4"
    media_bytes = b"synthetic evidence fixture; not a physical space estimate"
    evidence.write_bytes(media_bytes)
    database_before = local_path.read_bytes()
    center_port, edge_port = _free_port(), _free_port()
    assert center_port != edge_port
    center_url, edge_origin = f"https://127.0.0.1:{center_port}", f"https://127.0.0.1:{edge_port}"
    certificate, key = tls_certificate
    trusted = ssl.create_default_context(cafile=str(certificate))
    stdout_path, stderr_path = tmp_path / "retention.stdout", tmp_path / "retention.stderr"
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
                str(center_port),
                "--ssl-certfile",
                str(certificate),
                "--ssl-keyfile",
                str(key),
                "--no-access-log",
            ],
            env=deployment_environ
            | {
                "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "apps/control-api/src"),
                "SOP_EVIDENCE_HOST_ORIGINS": json.dumps({host_id: edge_origin}),
                "SOP_RETENTION_ESTIMATE_TTL_SECONDS": "60",
                "SOP_RETENTION_IMPACT_TIMEOUT_SECONDS": "5",
                "SSL_CERT_FILE": str(certificate),
            },
            stdout=stdout,
            stderr=stderr,
            text=True,
        )
        edge = None
        thread = None
        try:
            _wait_for_server(
                process, base_url=center_url, stdout_path=stdout_path, stderr_path=stderr_path
            )
            edge = create_server(
                address=("127.0.0.1", edge_port),
                client=CenterClient(
                    center_url=center_url,
                    host_id=host_id,
                    host_private_key=private_key,
                    timeout=5,
                    ssl_context=trusted,
                ),
                directory=evidence_directory,
                web_origin="https://web.fixture",
                local_state_path=local_path,
            )
            edge_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            edge_tls.load_cert_chain(str(certificate), str(key))
            edge.socket = edge_tls.wrap_socket(edge.socket, server_side=True)
            thread = threading.Thread(target=edge.serve_forever, daemon=True)
            thread.start()
            with Client(base_url=center_url, verify=trusted, trust_env=False, timeout=15) as center:
                center.cookies.update(client.cookies)
                before = center.get("/api/v1/retention/policy")
                assert before.status_code == 200, before.text
                original = before.json()
                assert original["policy"]["local_reported"] == {
                    "mode": "duration",
                    "seconds": 14 * 86400,
                }
                proposed = original["policy"] | {
                    "local_reported": {"mode": "duration", "seconds": 86400}
                }
                body = {"policy": proposed}
                estimated = center.post(
                    "/api/v1/retention/policy/impact-estimate", json=body, headers=browser_headers
                )
                assert estimated.status_code == 200, estimated.text
                impact = estimated.json()
                assert impact["complete"] is True, impact
                assert len(impact["fields"]) == 1
                field = impact["fields"][0]
                assert field["field"] == "local_reported"
                assert field["bytes_basis"] == "serialized_rows"
                assert field["exposed"]["objects"] == 1
                assert field["exposed"]["known_bytes"] == logical_bytes
                assert field["protected"]["objects"] == 1
                assert field["source_configurations"] == [
                    {
                        "host_id": host_id,
                        "configuration_revision": bundle.config_revision,
                        "configuration_sha256": bundle.effective_sha256,
                    }
                ]
                station = center.post(
                    f"/api/v1/retention/stations/{station_id}/impact-estimate",
                    json={"mode": "custom", "overrides": proposed},
                    headers=browser_headers,
                )
                assert station.status_code == 200, station.text
                assert station.json()["complete"] is True, station.text
                assert station.json()["fields"][0]["exposed"]["objects"] == 1
                # 取消不需要写请求:两种估算都未改变策略、覆盖记录或来源文件。
                assert center.get("/api/v1/retention/policy").json() == original
                station_before = center.get(f"/api/v1/retention/stations/{station_id}").json()
                assert station_before["revision"] == 0
                assert local_path.read_bytes() == database_before
                assert evidence.read_bytes() == media_bytes
                headers = browser_headers | {
                    "If-Match": str(original["revision"]),
                    "X-Retention-Estimate": impact["estimate_token"],
                }
                with sqlite3.connect(local_path) as local:
                    local.execute(
                        "UPDATE local_health_queue SET acknowledged_at=NULL WHERE queue_id=1"
                    )
                unavailable = center.post(
                    "/api/v1/retention/policy/impact-estimate", json=body, headers=browser_headers
                )
                assert unavailable.status_code == 200, unavailable.text
                assert unavailable.json()["complete"] is False
                assert unavailable.json()["fields"][0]["exposed"] is None
                refused = center.put("/api/v1/retention/policy", json=body, headers=headers)
                assert refused.status_code == 409, refused.text
                assert refused.json()["error_code"] == "RETENTION_ESTIMATE_INCOMPLETE"
                assert center.get("/api/v1/retention/policy").json() == original
                with sqlite3.connect(local_path) as local:
                    local.execute(
                        "UPDATE local_health_queue SET acknowledged_at=? WHERE queue_id=1", (ack,)
                    )
                confirmed = center.put("/api/v1/retention/policy", json=body, headers=headers)
                assert confirmed.status_code == 200, confirmed.text
                assert confirmed.json()["revision"] == original["revision"] + 1
                assert confirmed.json()["policy"] == proposed
                # 此入口仅改未来回收依据,不把结构化记录估算当作本机媒体删除资格。
                with sqlite3.connect(local_path) as local:
                    assert (
                        local.execute("SELECT count(*) FROM local_health_queue").fetchone()[0] == 2
                    )
                    assert (
                        local.execute("SELECT count(*) FROM local_observation_queue").fetchone()[0]
                        == 1
                    )
                    assert (
                        local.execute(
                            "SELECT count(*) FROM local_health_queue WHERE sent_at IS NULL"
                        ).fetchone()[0]
                        == 1
                    )
                assert evidence.read_bytes() == media_bytes
                # 公开网关同样将此精确入口交给主机认证，而不是浏览器会话。
                with _nginx_gateway(center, tmp_path, gateway_scheme="https") as machine:
                    check_path = "/api/v1/retention/reported-impact/access-check"
                    invalid_body = {"context": "not-a-grant"}
                    checked = machine.post(
                        check_path,
                        json=invalid_body,
                        headers=_signed_headers(
                            host_id=host_id,
                            private_key=private_key,
                            method="POST",
                            path=check_path,
                            body=invalid_body,
                        ),
                    )
                    assert checked.status_code == 403, checked.text
                    assert checked.json() == {"detail": "access_denied"}
                    unsigned = machine.post(
                        check_path, json=invalid_body, headers={"X-Inference-Host-ID": host_id}
                    )
                    assert unsigned.status_code == 401, unsigned.text
                    assert unsigned.json()["error_code"] == "INFERENCE_HOST_AUTHENTICATION_FAILED"
                center.cookies.clear()  # 机器请求不携带浏览器会话。
                check_path = "/api/v1/retention/reported-impact/access-check"
                unauthenticated = center.post(check_path, json={"context": "not-a-grant"})
                assert unauthenticated.status_code == 403
                forged = center.post(
                    check_path,
                    json={"context": "not-a-grant"},
                    headers={"X-Inference-Host-ID": host_id},
                )
                assert forged.status_code == 401, forged.text
                assert forged.json()["error_code"] == "INFERENCE_HOST_AUTHENTICATION_FAILED"
        finally:
            if edge is not None:
                edge.shutdown()
                edge.server_close()
            if thread is not None:
                thread.join()
            _stop_server(process)
