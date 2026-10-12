"""证据授权协议经真实 HTTPS Nginx、中心进程与 PostgreSQL，不涉及媒体代理。"""

import hashlib
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx2
import pytest
from conftest import (
    SESSION_PATH,
    BootstrapCommand,
    _free_port,
    _stop_server,
    _wait_for_server,
    log_in,
)
from edge_runtime.center_client import CenterClient
from edge_runtime.evidence_access import create_server
from httpx2 import Client, Response
from test_sys_machine_identity_over_nginx_https import (
    _create_host,
    _create_owned_topology,
    _nginx_gateway,
    _signed_headers,
)

ORIGIN = "https://evidence-host.internal:8765"
INSTANCE_ID = 7


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
                evidence_id = f"{host_id}/gateway/clip.mp4"
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
                context_path = f"/api/v1/evidence/{quote(evidence_id, safe='')}/access-context"
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


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _synthetic_media(directory: Path) -> tuple[Path, Path, str, int, str, int]:
    """用真实 ffmpeg 生成 H.264 片段与 JPEG 关键帧；返回文件、摘要与大小。"""
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None, "证据回看系统测试需要 ffmpeg"
    clip = directory / "clip.mp4"
    frame = directory / "frame.jpg"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=64x48:rate=10:duration=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(clip),
        ],
        check=True,
    )
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=64x48",
            "-frames:v",
            "1",
            str(frame),
        ],
        check=True,
    )
    return clip, frame, _digest(clip), clip.stat().st_size, _digest(frame), frame.stat().st_size


def _registration(
    *,
    evidence_id: str,
    host_id: str,
    station_id: str,
    kind: str,
    digest: str,
    size: int,
    reference: str,
) -> dict[str, object]:
    """来源推理机登记的最小受控引用：相对路径 + 摘要 + 大小 + 冻结录像时间元数据。"""
    return {
        "evidence_id": evidence_id,
        "host_id": str(UUID(host_id)).upper(),
        "station_id": station_id,
        "instance_id": INSTANCE_ID,
        "violation_id": None,
        "kind": kind,
        "origin": "automatic",
        "anchor": 1.0,
        "window_start": 0.0,
        "window_end": 2.0,
        "generation": "original",
        "sha256": digest,
        "size": size,
        "reference": reference,
        # 冻结的墙钟映射与核心必需跨度：截止落在未来，使再切片在浏览器场景中可用。
        "wall_offset": time.time() - 100.0,
        "required_from": 0.0,
        "required_to": 2.0,
    }


def _wait_for_http(url: str, *, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    with httpx2.Client(verify=False, trust_env=False, timeout=2.0) as probe:
        while time.monotonic() < deadline:
            try:
                if probe.get(url).status_code == 200:
                    return
            except httpx2.HTTPError:
                pass
            time.sleep(0.1)
    raise TimeoutError(f"{url} did not become ready")


def _reserve_port() -> tuple[socket.socket, int]:
    """占用 loopback 端口直到显式释放，避免分配与绑定之间被同一夹具或外部进程抢占。"""
    reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    reservation.bind(("127.0.0.1", 0))
    reservation.listen(1)
    return reservation, int(reservation.getsockname()[1])


class LiveEvidence(NamedTuple):
    """软件与浏览器两条用例共享的真实 Center/PG + Nginx + Edge + 合成媒体。"""

    gateway: httpx2.Client
    edge_origin: str
    vite_origin: str
    clip: Path
    frame: Path
    clip_id: str
    clip_digest: str
    clip_size: int
    frame_digest: str
    frame_size: int
    host_id: str
    canonical_host: str
    station_id: str
    session_cookie: str
    vite_reservation: socket.socket


@pytest.fixture
def live_evidence(
    client: Client,
    bootstrap: BootstrapCommand,
    deployment_environ: dict[str, str],
    tls_certificate: tuple[Path, Path],
    tmp_path: Path,
) -> Iterator[LiveEvidence]:
    """启动有 origin 映射的中心、Nginx 网关与真实 Edge HTTPS，并登记真实摘要/大小。"""
    log_in(client, bootstrap)
    browser_headers = {"x-csrf-token": client.cookies["sop_csrf"]}
    host_id, private_key = _create_host(client, browser_headers)
    station_id, _, _, _ = _create_owned_topology(client, browser_headers, host_id)
    canonical_host = str(UUID(host_id))

    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    clip, frame, clip_digest, clip_size, frame_digest, frame_size = _synthetic_media(evidence_dir)
    clip_id = f"{host_id}/证据 clip.mp4"
    frame_id = f"{host_id}/证据 关键帧.jpg"
    registrations = (
        _registration(
            evidence_id=clip_id,
            host_id=host_id,
            station_id=station_id,
            kind="clip",
            digest=clip_digest,
            size=clip_size,
            reference="clip.mp4",
        ),
        _registration(
            evidence_id=frame_id,
            host_id=host_id,
            station_id=station_id,
            kind="keyframe",
            digest=frame_digest,
            size=frame_size,
            reference="frame.jpg",
        ),
    )

    center_reservation, center_port = _reserve_port()
    edge_reservation, edge_port = _reserve_port()
    vite_reservation, vite_port = _reserve_port()
    edge_origin = f"https://127.0.0.1:{edge_port}"
    vite_origin = f"http://127.0.0.1:{vite_port}"
    center_url = f"https://127.0.0.1:{center_port}"
    certificate, key = tls_certificate
    center_stdout = (tmp_path / "center2.stdout").open("w")
    center_stderr = (tmp_path / "center2.stderr").open("w")
    center_reservation.close()
    center = subprocess.Popen(
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
            "SOP_EVIDENCE_HOST_ORIGINS": json.dumps({canonical_host: edge_origin}),
        },
        stdout=center_stdout,
        stderr=center_stderr,
        text=True,
    )
    edge_server = None
    try:
        _wait_for_server(
            center,
            base_url=center_url,
            stdout_path=tmp_path / "center2.stdout",
            stderr_path=tmp_path / "center2.stderr",
        )
        with (
            httpx2.Client(base_url=center_url, verify=False, trust_env=False) as configured,
            _nginx_gateway(configured, tmp_path, gateway_scheme="http") as gateway,
        ):
            # 网关是 HTTP，Secure 会话 cookie 不会随请求发出；显式以非 Secure 属性注入。
            gateway.cookies.set(
                "sop_session", client.cookies["sop_session"], domain="127.0.0.1", path="/"
            )
            for body in registrations:
                path = "/api/v1/evidence/registrations"
                registered = gateway.post(
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
                assert registered.status_code == 200, registered.text

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
                directory=evidence_dir,
                web_origin=vite_origin,
            )
            # 中心只接受 HTTPS 来源地址；部署里由可信终止承担，这里用同一证书包住监听 socket。
            edge_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            edge_tls.load_cert_chain(str(certificate), str(key))
            edge_server.socket = edge_tls.wrap_socket(edge_server.socket, server_side=True)
            threading.Thread(target=edge_server.serve_forever, daemon=True).start()
            try:
                yield LiveEvidence(
                    gateway=gateway,
                    edge_origin=edge_origin,
                    vite_origin=vite_origin,
                    clip=clip,
                    frame=frame,
                    clip_id=clip_id,
                    clip_digest=clip_digest,
                    clip_size=clip_size,
                    frame_digest=frame_digest,
                    frame_size=frame_size,
                    host_id=host_id,
                    canonical_host=canonical_host,
                    station_id=station_id,
                    session_cookie=client.cookies["sop_session"],
                    vite_reservation=vite_reservation,
                )
            finally:
                edge_server.shutdown()
                edge_server.server_close()
    finally:
        for reservation in (center_reservation, edge_reservation, vite_reservation):
            reservation.close()
        _stop_server(center)
        center_stdout.close()
        center_stderr.close()


def _edge_source(stack: LiveEvidence, context: str) -> str:
    return (
        f"{stack.edge_origin}/evidence/{quote(stack.clip_id, safe='')}"
        f"?context={quote(context, safe='')}"
    )


def test_evidence_source_reads_are_authorized_and_bounded(
    live_evidence: LiveEvidence, client: Client
) -> None:
    """真实 Center/PG + 真实 Edge：Range、缺失、撤权与字节不变，无需 Node。"""
    stack = live_evidence
    issued = stack.gateway.get(f"/api/v1/evidence/{quote(stack.clip_id, safe='')}/access-context")
    assert issued.status_code == 200, issued.text
    context = issued.json()["context"]
    source = _edge_source(stack, context)
    with httpx2.Client(trust_env=False, verify=False) as direct:
        ranged = direct.get(source, headers={"Origin": stack.vite_origin, "Range": "bytes=0-3"})
        assert ranged.status_code == 206, ranged.text
        assert ranged.content == stack.clip.read_bytes()[:4]
        assert ranged.headers["Content-Range"] == f"bytes 0-3/{stack.clip_size}"
        assert ranged.headers["Cache-Control"] == "no-store"
        full = direct.get(source, headers={"Origin": stack.vite_origin})
        assert full.status_code == 200
        assert full.content == stack.clip.read_bytes()
        # 缺文件：来源侧显式区分缺失，且读取不改写原始字节。
        hidden = stack.clip.with_name("clip.hidden")
        stack.clip.rename(hidden)
        try:
            missing = direct.get(source, headers={"Origin": stack.vite_origin})
            assert missing.status_code == 404
            assert missing.headers["X-Evidence-Error"] == "media_missing"
        finally:
            hidden.rename(stack.clip)
        # 撤权：登出后同一票据的逐次回查必须拒绝。
        logged_out = client.delete(
            SESSION_PATH, headers={"x-csrf-token": client.cookies["sop_csrf"]}
        )
        assert logged_out.status_code == 204
        revoked = direct.get(source, headers={"Origin": stack.vite_origin})
        assert revoked.status_code == 403
        assert revoked.headers["X-Evidence-Error"] == "session_invalid"
    assert _digest(stack.clip) == stack.clip_digest
    assert stack.clip.stat().st_size == stack.clip_size
    assert _digest(stack.frame) == stack.frame_digest
    assert stack.frame.stat().st_size == stack.frame_size


@pytest.mark.skipif(
    os.environ.get("NVSOP_EVIDENCE_BROWSER") != "1",
    reason="真实浏览器证据需显式 NVSOP_EVIDENCE_BROWSER=1（本地需 Node/pnpm/Chromium）",
)
def test_evidence_playback_reaches_real_browser(
    live_evidence: LiveEvidence, client: Client, tmp_path: Path
) -> None:
    """显式 opt-in：真实 Vue UI 经 Vite 读取真实 Edge 并解码片段与关键帧。"""
    stack = live_evidence
    pnpm = shutil.which("pnpm")
    assert pnpm is not None, "真实浏览器证据需要 pnpm/node"
    web = Path(__file__).resolve().parents[2] / "apps" / "control-web"
    vite_port = urlsplit(stack.vite_origin).port
    assert vite_port is not None
    node_env = {
        **os.environ,
        "SOP_BACKEND_ORIGIN": str(stack.gateway.base_url),
        "NO_PROXY": f"{os.environ.get('NO_PROXY', '')},127.0.0.1,localhost",
    }
    vite_stdout = (tmp_path / "vite.stdout").open("w")
    vite_stderr = (tmp_path / "vite.stderr").open("w")
    # 释放预约端口后立刻绑定，并用 --strictPort 保证不会静默漂移出预期 origin。
    stack.vite_reservation.close()
    vite = subprocess.Popen(
        [
            pnpm,
            "exec",
            "vite",
            "--host",
            "127.0.0.1",
            "--port",
            str(vite_port),
            "--strictPort",
        ],
        cwd=web,
        env=node_env,
        stdout=vite_stdout,
        stderr=vite_stderr,
        text=True,
    )
    try:
        _wait_for_http(f"{stack.vite_origin}/api/v1/liveness")
        played = subprocess.run(
            [
                pnpm,
                "exec",
                "playwright",
                "test",
                "tests/e2e/sys-35-evidence.spec.ts",
                "--project=chromium-1366x768",
                "--reporter=line",
            ],
            cwd=web,
            env=node_env
            | {
                "NVSOP_BASE_URL": stack.vite_origin,
                "NVSOP_EVIDENCE_HOST_ID": stack.canonical_host,
                "NVSOP_EVIDENCE_STATION_ID": stack.station_id,
                "NVSOP_EVIDENCE_INSTANCE_ID": str(INSTANCE_ID),
                "NVSOP_EVIDENCE_SESSION": stack.session_cookie,
                "NVSOP_EVIDENCE_CSRF": client.cookies["sop_csrf"],
                "NVSOP_EVIDENCE_ORIGIN": stack.edge_origin,
            },
            capture_output=True,
            text=True,
            timeout=300,
        )
        (tmp_path / "playwright.stdout").write_text(played.stdout, encoding="utf-8")
        (tmp_path / "playwright.stderr").write_text(played.stderr, encoding="utf-8")
        assert played.returncode == 0, played.stdout + played.stderr
        # 断言真的执行并通过，而不是因环境变量缺失被 skip。
        assert "1 passed" in played.stdout, played.stdout
        assert "skipped" not in played.stdout, played.stdout
        # 人工复核不重编码、不修改证据媒体：原始摘要与大小保持不变。
        assert _digest(stack.clip) == stack.clip_digest
        assert stack.clip.stat().st_size == stack.clip_size
    finally:
        _stop_server(vite)
        vite_stdout.close()
        vite_stderr.close()
