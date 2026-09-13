"""标注统一入口部署模板的认证与媒体转发契约。"""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG = REPO_ROOT / "docs/deployment/nginx-annotation.conf.example"
TRAINING_COMPOSE = (
    REPO_ROOT / "vendor/sop-monitoring-blueprints/microservices/sop-training-bp/docker-compose.yml"
)
DATASET_VOLUME_COMPOSE = REPO_ROOT / "deploy/dataset-annotation-volume.compose.example.yml"


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
