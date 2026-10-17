"""S049: 真实 Center/PG/TLS 委托已有 FFmpeg 任务暂停、中止、恢复。"""

from __future__ import annotations

import hashlib
import ssl
import subprocess
from datetime import datetime
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep, time

from conftest import BootstrapCommand, csrf_header, log_in
from edge_runtime.center_client import CenterClient
from edge_runtime.compression_commands import CompressionCommandLoop
from edge_runtime.evidence_media import recording_segment_generation
from edge_runtime.recording_compression import (
    RecordingCompressionConfiguration,
    RecordingCompressionWorker,
    validated_candidate,
)
from httpx2 import Client
from test_sys_93_delegated_connection_test_over_real_center_http import _create_target
from test_sys_machine_identity_over_nginx_https import _create_host, _nginx_gateway


def test_real_host_control_and_observation(
    client: Client,
    bootstrap: BootstrapCommand,
    tmp_path: Path,
    tls_certificate: tuple[Path, Path],
) -> None:
    log_in(client, bootstrap)
    target = _create_target(client, csrf_header(client), "21")
    directory = tmp_path / "recordings" / "camera-a"
    directory.mkdir(parents=True)
    stamp = datetime.fromtimestamp(time() - 10000).strftime("%Y-%m-%d_%H-%M-%S-%f")
    source = directory / f"{stamp}.mp4"
    subprocess.run(
        [
            "/usr/bin/ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x120:rate=10",
            "-t",
            "2",
            "-c:v",
            "libx264",
            str(source),
        ],
        check=True,
    )
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    worker = RecordingCompressionWorker(
        configuration=RecordingCompressionConfiguration(
            directory.parent,
            Path("/usr/bin/ffmpeg"),
            Path("/usr/bin/ffprobe"),
            3600,
            2,
            30,
            60.0,
            None,
            readrate=0.5,
        ),
        interval=0.1,
    )
    signed = CenterClient(
        center_url=str(client.base_url),
        host_id=target["host_id"],
        host_private_key=target["host_private_key"],
        timeout=5.0,
        ssl_context=ssl.create_default_context(cafile=str(tls_certificate[0])),
    )
    runner = CompressionCommandLoop(client=signed, worker=worker, interval=0.1)
    stop = Event()
    thread = Thread(target=lambda: worker.run_forever(should_stop=stop.is_set))
    thread.start()
    path = f"/api/v1/inference-hosts/{target['host_id']}/recording-compression/commands"
    try:
        deadline = monotonic() + 10
        while worker.status().pid is None and monotonic() < deadline:
            sleep(0.01)
        assert worker.status().pid is not None
        for index, (action, expected) in enumerate(
            (("pause", "paused"), ("abort", "aborted"), ("resume", "idle"))
        ):
            headers = {**csrf_header(client), "Idempotency-Key": f"compression-{index}"}
            queued = client.post(path, json={"action": action}, headers=headers)
            assert queued.status_code == 202, queued.text
            assert queued.json()["observation"] is None
            assert runner.run_once()
            actual = client.get(f"{path}/{queued.json()['id']}")
            assert actual.status_code == 200, actual.text
            assert actual.json()["status"] == "succeeded"
            assert actual.json()["observation"]["state"] in (
                {"idle", "running"} if action == "resume" else {expected}
            )
            assert "claim_token" not in actual.json()
            assert actual.json()["observed_at"] is not None
            repeated = client.post(path, json={"action": action}, headers=headers)
            assert repeated.json()["id"] == queued.json()["id"]
            if action != "resume":
                assert hashlib.sha256(source.read_bytes()).hexdigest() == original
                assert validated_candidate(source) is None
        deadline = monotonic() + 15
        while recording_segment_generation(source) != "compressed" and monotonic() < deadline:
            sleep(0.05)
        assert recording_segment_generation(source) == "compressed"
        assert list(directory.glob(".*.candidate.mp4")) == []
        # 回报前不领取后继命令: 避免旧暂停的租约重放晚于新恢复。
        for index in (10, 11):
            assert (
                client.post(
                    path,
                    json={"action": "inspect"},
                    headers={
                        **csrf_header(client),
                        "Idempotency-Key": f"serial-{index}",
                    },
                ).status_code
                == 202
            )
        first = signed.get("/api/v1/device-commands/compression/next")
        assert first.status == 200
        assert signed.get("/api/v1/device-commands/compression/next").status == 204
    finally:
        stop.set()
        thread.join(5)
        assert not thread.is_alive()


def test_compression_machine_routes_cross_real_nginx_https(
    client: Client, bootstrap: BootstrapCommand, tmp_path: Path
) -> None:
    log_in(client, bootstrap)
    host_id, private_key = _create_host(client, csrf_header(client))
    path = f"/api/v1/inference-hosts/{host_id}/recording-compression/commands"
    queued = client.post(
        path,
        json={"action": "inspect"},
        headers={**csrf_header(client), "Idempotency-Key": "gateway-compression"},
    )
    assert queued.status_code == 202, queued.text
    with _nginx_gateway(client, tmp_path, gateway_scheme="https") as gateway:
        # 操作员路由继续要求浏览器会话; 只有签名机器领取/回报入口放行。
        assert gateway.get(f"{path}/{queued.json()['id']}").status_code == 401
        signed = CenterClient(
            center_url=str(gateway.base_url),
            host_id=host_id,
            host_private_key=private_key,
            timeout=5.0,
            ssl_context=ssl.create_default_context(
                cafile=str(tmp_path / "nginx-tls" / "uvicorn.crt")
            ),
        )
        runner = CompressionCommandLoop(client=signed, worker=None, interval=0.1)
        assert runner.run_once()
        actual = client.get(f"{path}/{queued.json()['id']}")
        assert actual.status_code == 200, actual.text
        assert actual.json()["status"] == "failed"
        assert actual.json()["failure_code"] == "compression_unavailable"
        assert actual.json()["observation"]["state"] == "unavailable"
