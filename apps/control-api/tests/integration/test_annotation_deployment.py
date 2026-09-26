"""标注统一入口部署模板的认证与媒体转发契约。"""

import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import httpx2
import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.network import Network

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG = REPO_ROOT / "docs/deployment/nginx-annotation.conf.example"
DEV_CONFIG = REPO_ROOT / "deploy/dev/nginx.conf"
MACHINE_LOCATION_PREFIX = 'location ~ "^/api/v1/(?:inference-hosts/'
TRAINING_COMPOSE = (
    REPO_ROOT / "vendor/sop-monitoring-blueprints/microservices/sop-training-bp/docker-compose.yml"
)
DATASET_VOLUME_COMPOSE = REPO_ROOT / "deploy/dataset-annotation-volume.compose.example.yml"
NGINX_IMAGE = "nginx:1.27.3-alpine"


def test_control_requests_use_center_session_auth_without_auth_loop() -> None:
    source = CONFIG.read_text()

    assert "location = /_nvsop_center_auth" in source
    assert "internal;" in source
    assert "proxy_pass http://nvsop_center_api/api/v1/annotation/gateway-authorize;" in source
    assert "location /api/v1/ {\n        auth_request /_nvsop_center_auth;" in source
    assert "location /api/annotation/ {\n        auth_request /_nvsop_center_auth;" in source
    assert "upstream nvsop_annotation_frontend" in source
    assert "server annotation-frontend:80;" in source
    assert "location ^~ /annotation/" in source
    assert "map $uri $nvsop_annotation_context_token" in source
    assert "location @nvsop_auth_required" in source
    assert "location @nvsop_auth_denied" in source
    assert (
        "location ~ ^/api/(augmentation|vlm-training|ddm-training|evaluation)/ {\n"
        "        return 404;\n"
        "    }"
    ) in source
    assert "location = /api/v1/auth/session" in source
    assert "location = /api/v1/annotation/gateway-authorize" in source
    assert "api/v1/annotation/media/gateway-authorize?kind=video" in source
    assert "api/v1/annotation/media/gateway-authorize?kind=clip" in source
    assert "api/v1/annotation/media/gateway-authorize?kind=archive" in source
    assert "location = /api/v1/annotation/gateway-authorize {\n        return 404;" in source
    assert "location = /api/v1/annotation/media/authorize {\n        return 404;" in source
    assert "location = /api/v1/annotation/media/gateway-authorize {\n        return 404;" in source
    assert "location = /api/v1/liveness" in source


def test_host_signed_machine_routes_bypass_only_browser_auth_and_preserve_signature_headers() -> (
    None
):
    machine_location_lines: list[str] = []
    for path in (CONFIG, DEV_CONFIG):
        source = path.read_text()
        start = source.index(MACHINE_LOCATION_PREFIX)
        end = source.index("\n    }\n", start) + len("\n    }")
        location = source[start:end]
        machine_location_lines.append(location.splitlines()[0])

        assert "auth_request" not in location
        assert 'proxy_set_header Cookie "";' in location
        assert 'proxy_set_header Authorization "";' in location
        assert 'proxy_set_header X-CSRF-Token "";' in location
        assert "proxy_set_header X-Inference-Host-ID" not in location
        assert "proxy_set_header X-Inference-Host-Timestamp" not in location
        assert "proxy_set_header X-Inference-Host-Nonce" not in location
        assert "proxy_set_header X-Inference-Host-Signature" not in location
        for route in (
            "inference-hosts/",
            "/configuration",
            "confirmed-configuration",
            "monitor/(?:reported-decisions|reported-instances|health)",
            "device-commands/(?:next|",
            "/result)",
            "templates/configuration-reports",
        ):
            assert route in location

    assert machine_location_lines[0] == machine_location_lines[1]
    assert "location /api/v1/ {\n        auth_request /_nvsop_center_auth;" in CONFIG.read_text()
    assert (
        "location /api/v1/ {\n        auth_request /_nvsop_center_auth;" in DEV_CONFIG.read_text()
    )


def test_streaming_upload_entry_is_open_in_both_nginx_templates() -> None:
    """正式与开发入口都必须为流式上传放开请求体上限并关闭请求缓冲。"""
    location = "location ~ ^/api/v1/training-datasets/[^/]+/members/[^/]+/attempts/[^/]+/content$ {"
    for config in (CONFIG, DEV_CONFIG):
        source = config.read_text()
        start = source.index(location)
        end = source.index("\n    }\n", start)
        block = source[start:end]
        assert "client_max_body_size 8192M;" in block
        assert "proxy_request_buffering off;" in block
        assert "auth_request /_nvsop_center_auth;" in block
        assert "proxy_pass http://nvsop_center_api;" in block
        # 通用 /api/v1/ 入口继承的默认 1 MiB 会拒绝真实训练视频，服务级必须显式放开。
        assert "client_max_body_size 2048M;" in source


def test_dataset_annotation_volume_wires_writer_and_read_only_worker() -> None:
    source = DATASET_VOLUME_COMPOSE.read_text()
    assert "annotation-backend:" in source
    assert (
        "${NVSOP_ANNOTATION_DATA_HOST:?设置标注基座数据卷宿主机路径}:/app/assets/videos:rw"
        in source
    )
    assert "control-api-worker:" in source
    assert (
        "${NVSOP_ANNOTATION_DATA_HOST:?设置标注基座数据卷宿主机路径}:/var/lib/nvsop/annotation-data:ro"
        in source
    )
    assert "SOP_ANNOTATION_DATA_ROOT: /var/lib/nvsop/annotation-data" in source
    assert "control-api:\n    volumes:" not in source


def test_annotation_services_are_internal_only() -> None:
    source = TRAINING_COMPOSE.read_text()
    for service in ("annotation-backend", "annotation-frontend"):
        start = source.index(f"  {service}:\n")
        next_service = re.search(r"\n  [A-Za-z][\w-]*:\n", source[start + 1 :])
        end = start + 1 + next_service.start() if next_service else len(source)
        assert "    ports:" not in source[start:end]


def test_nginx_template_parses_when_nginx_is_available() -> None:
    nginx = shutil.which("nginx")
    if nginx is None:
        pytest.skip("nginx 未安装，部署环境需运行 nginx -t")
    source = CONFIG.read_text()
    syntax_source = "\n".join(
        line
        for line in source.splitlines()
        if not line.lstrip().startswith(("ssl_certificate ", "ssl_certificate_key "))
    )
    # 语法检查不依赖 Compose DNS；真实服务名由部署网络解析。
    syntax_source = (
        syntax_source.replace("control-api:8000", "127.0.0.1:8000")
        .replace("annotation-backend:8100", "127.0.0.1:8100")
        .replace("annotation-frontend:80", "127.0.0.1:80")
        .replace("listen 443 ssl;", "listen 14443;")
        .replace("listen 8444 ssl;", "listen 18444;")
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        snippet = root / "annotation.conf"
        snippet.write_text(syntax_source + "\n")
        configuration = root / "nginx.conf"
        configuration.write_text(
            f"pid {root / 'nginx.pid'};\n"
            f"error_log stderr;\nevents {{}}\n"
            f"http {{ access_log off; include {snippet}; }}\n"
        )
        result = subprocess.run(
            [nginx, "-t", "-q", "-c", str(configuration), "-p", str(root)],
            capture_output=True,
            text=True,
            check=False,
        )
    assert result.returncode == 0, result.stderr


def _wait_for_container_http(container: DockerContainer, url: str) -> None:
    deadline = time.monotonic() + 5
    result = None
    while time.monotonic() < deadline:
        result = container.exec(["wget", "-q", "-O", "/dev/null", url])
        if result.exit_code == 0:
            return
        time.sleep(0.05)
    assert result is not None
    pytest.fail(container.get_wrapped_container().logs().decode(errors="replace"))


def test_clip_media_gateway_forces_ranges_at_the_public_http_seam(tmp_path: Path) -> None:
    upstream_config = tmp_path / "upstream.conf"
    upstream_source = """
server {
    listen 8000;
    location = /api/v1/annotation/media/gateway-authorize {
        add_header X-Annotation-Upstream-Clip-ID base-clip always;
        return 204;
    }
}
server {
    listen 8100;
    max_ranges 0;
    location = /api/v1/chunks/base-clip/download {
        default_type video/mp4;
        return 200 '__CLIP_BODY__';
    }
}
server { listen 80; return 404; }
server { listen 8080; return 404; }
server { listen 9000; return 404; }
"""
    upstream_config.write_text(
        upstream_source.replace("__CLIP_BODY__", "0123456789abcdef" * 8).strip() + "\n"
    )
    gateway_config = tmp_path / "gateway.conf"
    gateway_config.write_text(DEV_CONFIG.read_text().replace("__NVSOP_LISTEN_SUFFIX__", ""))
    submission_id = "019937d8-0d10-7b31-8d2d-4e60c8f4f501"
    execution_id = "019937d8-0d10-7b31-8d2d-4e60c8f4f502"
    clip_path = f"/annotation/media/clips/{submission_id}/{execution_id}/0/download"

    with Network() as network:
        upstream = (
            DockerContainer(NGINX_IMAGE)
            .with_network(network)
            .with_network_aliases("center-api", "annotation-backend", "annotation-frontend", "web")
            .with_volume_mapping(upstream_config, "/etc/nginx/conf.d/default.conf")
            .with_exposed_ports(8100)
        )
        with upstream:
            _wait_for_container_http(
                upstream, "http://127.0.0.1:8100/api/v1/chunks/base-clip/download"
            )
            direct = httpx2.get(
                f"http://{upstream.get_container_host_ip()}:{upstream.get_exposed_port(8100)}"
                "/api/v1/chunks/base-clip/download",
                headers={"Range": "bytes=0-31"},
                timeout=5,
            )
            assert direct.status_code == 200
            assert direct.headers["content-type"].startswith("video/mp4")
            assert len(direct.content) == 128

            gateway = (
                DockerContainer(NGINX_IMAGE)
                .with_network(network)
                .with_volume_mapping(gateway_config, "/etc/nginx/conf.d/default.conf")
                .with_exposed_ports(8444)
            )
            with gateway:
                _wait_for_container_http(gateway, f"http://127.0.0.1:8444{clip_path}")
                ranged = httpx2.get(
                    f"http://{gateway.get_container_host_ip()}:{gateway.get_exposed_port(8444)}"
                    f"{clip_path}",
                    headers={"Cookie": "sop_session=fixture", "Range": "bytes=0-31"},
                    timeout=5,
                )
                assert ranged.status_code == 206, ranged.text
                assert ranged.headers["content-type"].startswith("video/mp4")
                assert ranged.headers["content-range"] == "bytes 0-31/128"
                assert ranged.headers["content-length"] == "32"
                assert len(ranged.content) == 32


def test_deployment_template_keeps_clip_range_contract() -> None:
    source = CONFIG.read_text()
    marker = "location ^~ /annotation/media/clips/"
    start = source.index(marker)
    end = source.index("\n    }\n", start) + len("\n    }")
    location = source[start:end]
    assert "proxy_set_header Range $http_range;" in location
    assert "proxy_force_ranges on;" in location


def test_media_requests_authorize_before_proxying_upstream_identity() -> None:
    source = CONFIG.read_text()

    assert "rewrite" not in source
    assert "error_page 401 = @nvsop_media_auth_required;" in source
    assert "error_page 403 = @nvsop_media_auth_denied;" in source
    for marker, variable in (
        ("location ^~ /annotation/media/videos/", "$nvsop_annotation_upstream_video"),
        ("location ^~ /annotation/media/clips/", "$nvsop_annotation_upstream_clip"),
        ("location ^~ /annotation/media/archives/", "$nvsop_annotation_upstream_video"),
    ):
        start = source.index(marker)
        end = source.index("\n    }\n", start) + len("\n    }")
        location = source[start:end]
        assert "auth_request" in location
        assert 'add_header Cache-Control "no-store" always;' in location
        assert variable in location
        assert location.index("auth_request") < location.index("proxy_pass")
