"""S120-EVIDENCE-ORIGIN-CSP — 官方渲染网关按部署来源映射向浏览器放行证据 origin。

公开 seam：`scripts/dev.py::render_gateway_config` 读取进程环境 `SOP_EVIDENCE_HOST_ORIGINS`
（规范 UUID→HTTP(S) origin JSON），在 `deploy/dev/nginx.conf` 的 `connect-src`/`media-src`
追加已配置来源；`deploy/dev/compose.yaml` 用同一映射组装中心环境。真实 Nginx 与真实浏览器
经该网关从非 8444 来源解码并 Range 读取 ffmpeg 合成媒体；CPU 合成只证明软件路径。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import httpx2
import pytest
from conftest import _free_port
from test_sys_35_evidence import _synthetic_media

REPO_ROOT = Path(__file__).resolve().parents[2]
NGINX_CONFIG = REPO_ROOT / "deploy" / "dev" / "nginx.conf"
COMPOSE_CONFIG = REPO_ROOT / "deploy" / "dev" / "compose.yaml"
WEB = REPO_ROOT / "apps" / "control-web"
NGINX_IMAGE = "nginx:1.27.3-alpine"
EVIDENCE_ENV = "SOP_EVIDENCE_HOST_ORIGINS"
ARTIFACTS = REPO_ROOT / ".nvsop" / "artifacts" / "evidence-origin-csp"
FIRST_HOST = "11111111-1111-4111-8111-111111111111"
SECOND_HOST = "22222222-2222-4222-8222-222222222222"
SECOND_ORIGIN = "https://evidence-secondary.internal:9443"

_SPEC = importlib.util.spec_from_file_location(
    "nvsop_dev_script_csp", REPO_ROOT / "scripts" / "dev.py"
)
assert _SPEC is not None
assert _SPEC.loader is not None
DEV = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = DEV
_SPEC.loader.exec_module(DEV)


def _payload_handler(payload: bytes, content_type: str) -> type[BaseHTTPRequestHandler]:
    """最小来源/页面服务：支持 Range 206 与 CORS，用于真实浏览器读取合成媒体。"""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            return None

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self._write_cors()
            self.send_header("Access-Control-Allow-Headers", "Range")
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
            self.end_headers()

        def do_HEAD(self) -> None:
            self._respond(head_only=True)

        def do_GET(self) -> None:
            self._respond(head_only=False)

        def _write_cors(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")

        def _respond(self, *, head_only: bool) -> None:
            start = 0
            end = len(payload) - 1
            status = 200
            content_range: str | None = None
            requested = self.headers.get("Range")
            if requested is not None and requested.startswith("bytes=") and end >= 0:
                first, _, last = requested[len("bytes=") :].partition("-")
                try:
                    start = int(first)
                    end = int(last) if last else end
                except ValueError:
                    start, end = 0, len(payload) - 1
                else:
                    end = min(end, len(payload) - 1)
                    status = 206
                    content_range = f"bytes {start}-{end}/{len(payload)}"
            chunk = payload[start : end + 1] if end >= start else b""
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(chunk)))
            if content_range is not None:
                self.send_header("Content-Range", content_range)
            self._write_cors()
            self.end_headers()
            if not head_only:
                self.wfile.write(chunk)

    return Handler


@contextmanager
def _payload_server(payload: bytes, content_type: str) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _payload_handler(payload, content_type))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _write_rendered_config(
    tmp_path: Path, *, mapping: dict[str, str], name: str, web_port: int
) -> tuple[Path, int]:
    """经公开渲染入口生成网关配置；来源映射只从进程环境读入，不在测试里改写 CSP。"""
    gateway_port = _free_port()
    media_port = _free_port()
    root = tmp_path / f"gateway-{name}"
    (root / "deploy" / "dev").mkdir(parents=True)
    template = (
        NGINX_CONFIG.read_text(encoding="utf-8")
        .replace("center-api:8000", f"127.0.0.1:{_free_port()}")
        .replace("web:8080", f"127.0.0.1:{web_port}")
        .replace("annotation-backend:8100", "127.0.0.1:9")
        .replace("annotation-frontend:80", "127.0.0.1:9")
        .replace("8443__NVSOP_LISTEN_SUFFIX__", f"{gateway_port}__NVSOP_LISTEN_SUFFIX__")
        .replace("8444__NVSOP_LISTEN_SUFFIX__", f"{media_port}__NVSOP_LISTEN_SUFFIX__")
    )
    (root / "deploy" / "dev" / "nginx.conf").write_text(template, encoding="utf-8")
    item = DEV.DevPaths(root=root, state=root / ".state")
    DEV.ensure_directories(item)
    with patch.dict(os.environ, {EVIDENCE_ENV: json.dumps(mapping)}, clear=False):
        DEV.render_gateway_config(item, source=root, protocol="http")
    return item.state / "nginx.conf", gateway_port


def _await_http(url: str, *, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    with httpx2.Client(trust_env=False, timeout=2.0) as probe:
        while time.monotonic() < deadline:
            try:
                if probe.get(url).status_code == 200:
                    return
            except httpx2.HTTPError:
                pass
            time.sleep(0.1)
    raise AssertionError(f"{url} 未就绪")


@contextmanager
def _nginx_gateway(config: Path, *, base_url: str, name: str) -> Iterator[None]:
    docker = shutil.which("docker")
    assert docker is not None, "真实 Nginx 网关证据需要 Docker"
    container = f"nvsop-csp-{name}-{uuid4().hex[:10]}"
    process = subprocess.Popen(
        [
            docker,
            "run",
            "--rm",
            "--name",
            container,
            "--network",
            "host",
            "-v",
            f"{config}:/etc/nginx/conf.d/default.conf:ro",
            NGINX_IMAGE,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _await_http(f"{base_url}/")
        yield
    finally:
        subprocess.run(
            [docker, "stop", "-t", "1", container],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=20)


@contextmanager
def _served_gateway(
    tmp_path: Path, *, mapping: dict[str, str], page: bytes, name: str
) -> Iterator[tuple[str, Path]]:
    with _payload_server(page, "text/html; charset=utf-8") as web_origin:
        web_port = urlsplit(web_origin).port
        assert web_port is not None
        rendered, gateway_port = _write_rendered_config(
            tmp_path, mapping=mapping, name=name, web_port=web_port
        )
        base_url = f"http://127.0.0.1:{gateway_port}"
        with _nginx_gateway(rendered, base_url=base_url, name=name):
            yield base_url, rendered


def _csp_directives(header: str) -> dict[str, list[str]]:
    directives: dict[str, list[str]] = {}
    for part in header.split(";"):
        tokens = part.split()
        if tokens:
            directives[tokens[0]] = tokens[1:]
    return directives


def _browser_page(media_url: str) -> bytes:
    return (
        "<!doctype html>\n"
        '<html lang="zh">\n<head><meta charset="utf-8"><title>evidence csp</title>'
        "</head>\n<body>\n"
        f'<video id="evidence" aria-label="证据片段回放" preload="auto" muted src="{media_url}">'
        "</video>\n<script>\n"
        "window.__video = document.getElementById('evidence');\n"
        f"fetch('{media_url}', {{ headers: {{ Range: 'bytes=0-9' }} }})\n"
        "  .then(function (response) { window.__range = { status: response.status }; })\n"
        "  .catch(function (error) {\n"
        "    window.__range = { status: 'failed', message: String(error) };\n"
        "  });\n"
        "</script>\n</body></html>\n"
    ).encode()


DRIVER_JS = """
const { chromium } = require(process.env.NVSOP_PW_PACKAGE);
const pageUrl = process.env.NVSOP_CSP_PAGE_URL;
const mediaUrl = process.env.NVSOP_CSP_MEDIA_URL;
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const consoleErrors = [];
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });
  const responses = [];
  page.on('response', (response) => {
    if (response.url().startsWith(mediaUrl)) {
      responses.push({
        status: response.status(),
        contentRange: response.headers()['content-range'] || null,
      });
    }
  });
  await page.goto(pageUrl, { waitUntil: 'load' });
  await page.waitForFunction(() => window.__range, null, { timeout: 20000 });
  const range = await page.evaluate(() => window.__range);
  await page.waitForFunction(
    () => window.__video && (window.__video.readyState >= 2 || window.__video.error),
    null,
    { timeout: 20000 },
  );
  const video = await page.evaluate(() => ({
    readyState: window.__video.readyState,
    videoWidth: window.__video.videoWidth,
    error: window.__video.error ? window.__video.error.code : null,
  }));
  await browser.close();
  process.stdout.write(JSON.stringify({ range, video, responses, consoleErrors }));
})().catch((error) => {
  console.error(String(error));
  process.exit(1);
});
"""


def _run_browser(base_url: str, media_url: str, tmp_path: Path) -> dict[str, object]:
    driver = tmp_path / "evidence-csp-driver.cjs"
    driver.write_text(DRIVER_JS, encoding="utf-8")
    environment = {
        **os.environ,
        "NVSOP_PW_PACKAGE": str(WEB / "node_modules" / "@playwright" / "test"),
        "NVSOP_CSP_PAGE_URL": f"{base_url}/",
        "NVSOP_CSP_MEDIA_URL": media_url,
    }
    played = subprocess.run(
        ["node", str(driver)],
        cwd=WEB,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
    )
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / "browser.command").write_text(f"node {driver}\n", encoding="utf-8")
    (ARTIFACTS / "browser.stdout").write_text(played.stdout, encoding="utf-8")
    (ARTIFACTS / "browser.stderr").write_text(played.stderr, encoding="utf-8")
    assert played.returncode == 0, played.stdout + played.stderr
    return cast("dict[str, object]", json.loads(played.stdout.strip().splitlines()[-1]))


def test_rendered_gateway_csp_allows_only_configured_evidence_origins(tmp_path: Path) -> None:
    """AC1：真实渲染 Nginx 只精确放行两个显式配置来源，未配置的第三来源被排除。"""
    if shutil.which("docker") is None:
        pytest.skip("真实 Nginx 网关证据需要 Docker")
    first = f"http://127.0.0.1:{_free_port()}"
    unconfigured = f"http://127.0.0.1:{_free_port()}"
    page = b"<!doctype html><title>csp</title>"
    with _served_gateway(
        tmp_path, mapping={FIRST_HOST: first, SECOND_HOST: SECOND_ORIGIN}, page=page, name="header"
    ) as (base_url, rendered):
        with httpx2.Client(base_url=base_url, trust_env=False, timeout=5.0) as client:
            response = client.get("/")
        assert response.status_code == 200, response.text
        header = response.headers["Content-Security-Policy"]
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        (ARTIFACTS / "rendered-nginx.conf").write_text(
            rendered.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (ARTIFACTS / "response-csp.txt").write_text(header + "\n", encoding="utf-8")
    directives = _csp_directives(header)
    assert directives["connect-src"] == ["'self'", "http://localhost:8444", first, SECOND_ORIGIN]
    assert directives["media-src"] == [
        "'self'",
        "blob:",
        "http://localhost:8444",
        first,
        SECOND_ORIGIN,
    ]
    assert unconfigured not in directives["connect-src"]
    assert unconfigured not in directives["media-src"]
    assert directives["default-src"] == ["'self'"]
    assert directives["object-src"] == ["'none'"]
    assert directives["base-uri"] == ["'self'"]
    assert "data:" in directives["img-src"]
    assert "blob:" in directives["img-src"]
    assert "'unsafe-inline'" in directives["script-src"]
    assert not any("__NVSOP" in token for tokens in directives.values() for token in tokens)


def test_browser_decodes_and_ranges_from_configured_evidence_origin(tmp_path: Path) -> None:
    """AC2：真实浏览器经该网关从合法非 8444 来源解码并 Range 读取合成媒体。"""
    if shutil.which("docker") is None:
        pytest.skip("真实 Nginx 网关证据需要 Docker")
    if shutil.which("node") is None or not (WEB / "node_modules" / "@playwright" / "test").is_dir():
        pytest.skip("真实浏览器证据需要 node 与 apps/control-web 的 Playwright")
    if shutil.which("ffmpeg") is None:
        pytest.skip("浏览器证据需要 ffmpeg 合成媒体")
    clip, _frame, _digest, _size, _frame_digest, _frame_size = _synthetic_media(tmp_path)
    media_bytes = clip.read_bytes()
    with _payload_server(media_bytes, "video/mp4") as media_origin:
        media_url = f"{media_origin}/clip.mp4"
        with _served_gateway(
            tmp_path,
            mapping={FIRST_HOST: media_origin, SECOND_HOST: SECOND_ORIGIN},
            page=_browser_page(media_url),
            name="browser",
        ) as (base_url, _rendered):
            result = _run_browser(base_url, media_url, tmp_path)
    range_result = result.get("range")
    video_result = result.get("video")
    assert isinstance(range_result, dict), result
    assert isinstance(video_result, dict), result
    assert range_result.get("status") == 206, result
    assert video_result.get("error") is None, result
    assert int(cast("int", video_result.get("readyState", 0))) >= 2, result
    assert int(cast("int", video_result.get("videoWidth", 0))) > 0, result
    responses = result.get("responses")
    assert isinstance(responses, list), result
    assert any(isinstance(item, dict) and item.get("status") == 206 for item in responses), result


def test_compose_center_environment_carries_the_same_evidence_origin_mapping(
    tmp_path: Path,
) -> None:
    """AC3/AC4：Compose 对 unset 用 {}、显式空值原样传递、显式 mapping 与网关同源。"""
    if shutil.which("docker") is None:
        pytest.skip("Compose 解析证据需要 Docker CLI")
    mapping = {FIRST_HOST: f"http://127.0.0.1:{_free_port()}", SECOND_HOST: SECOND_ORIGIN}
    rendered, _gateway_port = _write_rendered_config(
        tmp_path, mapping=mapping, name="compose", web_port=_free_port()
    )
    gateway_text = rendered.read_text(encoding="utf-8")
    for origin in mapping.values():
        assert gateway_text.count(origin) == 2, gateway_text
    unset = {key: value for key, value in os.environ.items() if key != EVIDENCE_ENV}
    cases = {
        "unset": (unset, "{}"),
        "empty": ({**os.environ, EVIDENCE_ENV: ""}, ""),
        "mapping": ({**os.environ, EVIDENCE_ENV: json.dumps(mapping)}, json.dumps(mapping)),
    }
    for name, (environment, expected) in cases.items():
        completed = subprocess.run(
            ["docker", "compose", "--file", str(COMPOSE_CONFIG), "config", "--format", "json"],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert completed.returncode == 0, (name, completed.stderr)
        config = json.loads(completed.stdout)
        center_environment = config["services"]["center-api"]["environment"]
        assert center_environment[EVIDENCE_ENV] == expected, name
