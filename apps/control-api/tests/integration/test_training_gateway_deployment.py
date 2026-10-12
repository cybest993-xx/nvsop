"""S068：原五个 app、真实授权/DB/Nginx；CPU 不执行训练计算。"""

from __future__ import annotations

import contextlib
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx2
import pytest
import uvicorn
from _integration_support import RedisServer
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.network import Network

from factory_sop.app import create_app
from factory_sop.bootstrap import main as bootstrap
from factory_sop.settings import Settings

ROOT = Path(__file__).resolve().parents[4]
VENDOR = ROOT / "vendor/sop-monitoring-blueprints/microservices/sop-training-bp/microservices"
TARGETS = {
    "augmentation": ("data-generation-pipeline", 32070),
    "vlm-training": ("cr-training-ms", 32080),
    "ddm-training": ("ddm-training-ms", 32090),
    "evaluation": ("evaluation-ms", 32100),
}


def wait_status(client: httpx2.Client, path: str, status: int) -> httpx2.Response:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            response = client.get(path)
            if response.status_code == status:
                return response
        except httpx2.HTTPError:
            pass
        time.sleep(0.2)
    pytest.fail(f"{path} did not return {status}")


def raw_request_status(url: str, target: str, cookie: str = "") -> int:
    """用原始请求行发送 URI：httpx 会把字面 '#' 当作 URL fragment 剥离。"""
    parsed = urlsplit(url)
    assert parsed.hostname is not None
    assert parsed.port is not None
    request = f"GET {target} HTTP/1.1\r\nHost: {parsed.netloc}\r\n"
    if cookie:
        request += f"Cookie: {cookie}\r\n"
    request += "Connection: close\r\n\r\n"
    with socket.create_connection((parsed.hostname, parsed.port), timeout=8) as connection:
        connection.sendall(request.encode())
        response = b""
        while chunk := connection.recv(4096):
            response += chunk
    return int(response.split(b" ", 2)[1])


@pytest.fixture(scope="module")
def gateway_images(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    context = tmp_path_factory.mktemp("training-cpu")
    shutil.copyfile(ROOT / "deploy/dev/training-control.lock", context / "requirements.txt")
    (context / "Dockerfile").write_text(
        "FROM python:3.10.16-slim-bookworm@"
        "sha256:f9fd9a142c9e3bc54d906053b756eb7e7e386ee1cf784d82c251cf640c502512\n"
        "COPY requirements.txt /tmp/requirements.txt\n"
        "RUN pip install --no-deps --no-build-isolation -r /tmp/requirements.txt && pip check\n"
        "RUN mkdir -p /workspace/assets/logs /workspace/assets/config /workspace/assets/data\n"
        "ENV PYTHONDONTWRITEBYTECODE=1\n"
    )
    images = {
        name: f"nvsop-s068-{name}:{uuid4().hex}" for name in ("cpu", "annotation", "frontend")
    }
    for name, dockerfile, build_context in (
        ("cpu", context / "Dockerfile", context),
        ("annotation", ROOT / "deploy/dev/annotation-backend.Dockerfile", ROOT),
        ("frontend", ROOT / "deploy/dev/annotation-frontend.Dockerfile", ROOT),
    ):
        with (context / f"{name}-build.log").open("wb") as log:
            subprocess.run(
                [
                    "docker",
                    "build",
                    "--network",
                    "host",
                    "-f",
                    str(dockerfile),
                    "-t",
                    images[name],
                    str(build_context),
                ],
                check=True,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
    return images


@pytest.fixture
def training_database() -> Iterator[tuple[Network, Engine]]:
    # 网络在数据库启动前接入，避免运行中接网改变宿主端口的 Docker NAT。
    with Network() as network:
        postgres = (
            PostgresContainer("timescale/timescaledb:2.22.1-pg17", driver="psycopg")
            .with_network(network)
            .with_network_aliases("training-db")
        )
        with postgres:
            url = postgres.get_connection_url()
            configuration = Config(str(ROOT / "apps/control-api/alembic.ini"))
            configuration.set_main_option(
                "script_location", str(ROOT / "apps/control-api/migrations")
            )
            configuration.set_main_option("sqlalchemy.url", url)
            command.upgrade(configuration, "head")
            engine = create_engine(url)
            try:
                yield network, engine
            finally:
                engine.dispose()


def test_fixed_targets_fail_independently_without_blocking_center_or_annotation(
    training_database: tuple[Network, Engine],
    redis_server: RedisServer,
    tmp_path: Path,
    gateway_images: dict[str, str],
) -> None:
    network, engine = training_database
    database = f"training_{uuid4().hex}"
    role = f"training_runtime_{uuid4().hex[:12]}"
    password = uuid4().hex
    installer = engine.execution_options(isolation_level="AUTOCOMMIT")
    with installer.connect() as connection:
        connection.execute(text(f"CREATE DATABASE {database} TEMPLATE template0"))
        connection.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}' NOSUPERUSER"))
        connection.execute(text(f"REVOKE CONNECT ON DATABASE {database} FROM PUBLIC"))
        connection.execute(text(f"GRANT CONNECT ON DATABASE {database} TO {role}"))
    training = create_engine(engine.url.set(database=database))
    with training.begin() as connection:
        connection.exec_driver_sql(
            (VENDOR.parent / "db-init-scripts/01-init-tables.sql").read_text()
        )
        connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
        connection.execute(text(f"GRANT ALL ON ALL TABLES IN SCHEMA public TO {role}"))
        connection.execute(text(f"GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO {role}"))
        connection.execute(
            text("INSERT INTO ddm_training_job (id, status) VALUES ('synthetic-orphan', 'queued')")
        )
    training.dispose()

    secrets = {
        "db": str(engine.url.password),
        "runtime": password,
        "csrf": uuid4().hex,
        "redis": redis_server.url,
        "operator": uuid4().hex,
    }
    for name, value in secrets.items():
        (tmp_path / name).write_text(value)
    environ = {
        "SOP_LOG_LEVEL": "warning",
        "SOP_DATABASE_HOST": str(engine.url.host),
        "SOP_DATABASE_PORT": str(engine.url.port),
        "SOP_DATABASE_NAME": str(engine.url.database),
        "SOP_DATABASE_USER": str(engine.url.username),
        "SOP_DATABASE_PASSWORD_FILE": str(tmp_path / "db"),
        "SOP_CSRF_SECRET_FILE": str(tmp_path / "csrf"),
        "SOP_REDIS_URL_FILE": str(tmp_path / "redis"),
        "SOP_DATASET_STORAGE_ROOT": str(tmp_path / "dataset"),
        "SOP_DEPLOYMENT_MODE": "fixed_main",
        "SOP_SESSION_COOKIE_TRANSPORT": "require_https",
        "SOP_SESSION_IDLE_TIMEOUT_MINUTES": "30",
        "SOP_SESSION_ABSOLUTE_LIFETIME_MINUTES": "480",
        "SOP_DATASET_UPLOAD_TTL_SECONDS": "900",
        "SOP_DATASET_MAX_UPLOAD_BYTES": str(8 * 1024**3),
        "SOP_DATASET_SUPPORTED_CODECS": "h264,h265",
        "SOP_MEDIA_PROBE_BINARY": "ffprobe",
        "SOP_MEDIA_PROBE_TIMEOUT_SECONDS": "60",
    }
    assert (
        bootstrap(
            argv=["--login-name", "s068", "--password-file", str(tmp_path / "operator")],
            environ=environ,
        )
        == 0
    )

    with contextlib.ExitStack() as stack:
        annotation = stack.enter_context(
            DockerContainer(gateway_images["annotation"])
            .with_network(network)
            .with_network_aliases("annotation-backend")
            .with_exposed_ports(8100)
            .with_env("POSTGRES_HOST", "training-db")
            .with_env("POSTGRES_PORT", "5432")
            .with_env("POSTGRES_DB", database)
            .with_env("POSTGRES_USER", role)
            .with_env("SERVICE_PORT", "8100")
            .with_volume_mapping(
                str(tmp_path / "runtime"), "/run/secrets/training-runtime-password", "ro"
            )
            .with_kwargs(extra_hosts={"host.docker.internal": "host-gateway"})
        )
        annotation_url = (
            f"http://{annotation.get_container_host_ip()}:{annotation.get_exposed_port(8100)}"
        )
        with httpx2.Client(base_url=annotation_url, timeout=5) as direct:
            wait_status(direct, "/health/ready", 200)
        environ["SOP_ANNOTATION_BACKEND_URL"] = annotation_url
        environ["SOP_ANNOTATION_MEDIA_ORIGIN"] = "http://localhost:8444"
        environ["SOP_ANNOTATION_DATA_ROOT"] = str(tmp_path / "annotation-data")
        environ["SOP_SESSION_COOKIE_TRANSPORT"] = "allow_http"
        server = uvicorn.Server(
            uvicorn.Config(create_app(Settings.from_environment(environ)), log_level="warning")
        )
        listener = socket.socket()
        listener.bind(("0.0.0.0", 0))
        port = listener.getsockname()[1]
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()

        def stop_center() -> None:
            server.should_exit = True
            thread.join(timeout=10)
            listener.close()

        stack.callback(stop_center)
        stack.enter_context(
            DockerContainer(gateway_images["frontend"])
            .with_network(network)
            .with_network_aliases("annotation-frontend", "web")
        )
        configuration = tmp_path / "nginx.conf"
        configuration.write_text(
            (ROOT / "deploy/dev/nginx.conf")
            .read_text()
            .replace("__NVSOP_LISTEN_SUFFIX__", "")
            .replace("center-api:8000", f"host.docker.internal:{port}")
        )
        gateway = stack.enter_context(
            DockerContainer("nginx:1.27.3-alpine")
            .with_network(network)
            .with_exposed_ports(8443)
            .with_volume_mapping(str(configuration), "/etc/nginx/conf.d/default.conf", "ro")
            .with_kwargs(extra_hosts={"host.docker.internal": "host-gateway"})
        )
        url = f"http://{gateway.get_container_host_ip()}:{gateway.get_exposed_port(8443)}"
        with httpx2.Client(base_url=url, timeout=8) as client:
            wait_status(client, "/api/v1/liveness", 200)
            assert client.get("/api/augmentation/health/ready").status_code == 401
            login = client.post(
                "/api/v1/auth/session",
                json={"login_name": "s068", "password": secrets["operator"]},
            )
            assert login.status_code == 201
            session_cookie = "; ".join(
                part.split(";", 1)[0] for part in login.headers.get_list("set-cookie")
            )
            for service in TARGETS:
                # Nginx 已在四个 DNS 名全部不存在时启动成功，且不能伪造健康。
                assert client.get(f"/api/{service}/health/ready").status_code == 502
            processes = {}
            for service, (source, service_port) in TARGETS.items():
                process = stack.enter_context(
                    DockerContainer(gateway_images["cpu"])
                    .with_network(network)
                    .with_network_aliases(service)
                    .with_volume_mapping(str(VENDOR / source), "/app", "ro")
                    .with_volume_mapping(
                        str(ROOT / "deploy/dev/training-bootstrap.py"),
                        "/bootstrap/training_bootstrap.py",
                        "ro",
                    )
                    .with_volume_mapping(
                        str(tmp_path / "runtime"), "/run/secrets/training-runtime-password", "ro"
                    )
                    .with_env("POSTGRES_HOST", "training-db")
                    .with_env("POSTGRES_PORT", "5432")
                    .with_env("POSTGRES_DB", database)
                    .with_env("POSTGRES_USER", role)
                    .with_env("POSTGRES_PASSWORD_FILE", "/run/secrets/training-runtime-password")
                    .with_env(
                        "PYTHONPATH",
                        "/bootstrap:/app:/app/source/multi-modal-autolabel-augmentation-pipeline",
                    )
                    .with_kwargs(extra_hosts={"host.docker.internal": "host-gateway"})
                    .with_command(
                        'sh -c "cd /app && exec uvicorn training_bootstrap:app '
                        f'--host 0.0.0.0 --port {service_port}"'
                    )
                )
                processes[service] = process.get_wrapped_container()
                assert wait_status(client, f"/api/{service}/health/ready", 200).json() == {
                    "ready": True
                }
            for alias in (
                "/api/augment%61tion/health/ready",
                "/api%2faugmentation/health/ready",
                "/api/augmentation/%2e/health/ready",
                "/api/augmentation/debug/%2e%2e/health/ready",
                "/api//augmentation//health//ready",
            ):
                assert client.get(alias).status_code == 200, alias
            # 请求行里的字面 '#' 是 Nginx 的 URI 终点（ngx_http_parse_complex_uri 走 done），
            # httpx 会剥离 URL fragment；只能用原始请求行证明映射与授权。
            fragment = "/api/augmentation/health/ready#suffix"
            assert raw_request_status(url, fragment) == 401
            assert raw_request_status(url, fragment, session_cookie) == 200
            with installer.connect() as connection:
                connection.execute(
                    text(
                        "DELETE FROM auth_role_permission WHERE permission = "
                        "'dataset.dataset.view' AND role_id IN (SELECT role_id FROM "
                        "auth_user_role WHERE user_id = (SELECT id FROM auth_user WHERE "
                        "login_name = 's068'))"
                    )
                )
            # 无 DATASET_VIEW 时字面 '#' 不能绕过网关（修复前会返回 200）。
            assert raw_request_status(url, fragment, session_cookie) == 403
            with installer.connect() as connection:
                connection.execute(
                    text(
                        "INSERT INTO auth_role_permission (role_id, permission) SELECT role_id, "
                        "'dataset.dataset.view' FROM auth_user_role WHERE user_id = "
                        "(SELECT id FROM auth_user WHERE login_name = 's068')"
                    )
                )
            assert raw_request_status(url, fragment, session_cookie) == 200
            training = create_engine(engine.url.set(database=database))
            with training.connect() as connection:
                assert (
                    connection.scalar(
                        text("SELECT status FROM ddm_training_job WHERE id = 'synthetic-orphan'")
                    )
                    == "cancelled"
                )
            training.dispose()
            for service, process in processes.items():
                process.stop()
                assert client.get(f"/api/{service}/health/ready").status_code in {502, 504}
                for other in TARGETS.keys() - {service}:
                    assert client.get(f"/api/{other}/health/ready").status_code == 200
                for path in (
                    "/api/v1/auth/session",
                    "/api/v1/templates/drafts",
                    "/api/v1/cameras",
                    "/annotation/",
                ):
                    assert client.get(path).status_code == 200, path
                assert (
                    client.post(
                        "/api/v1/auth/session",
                        json={"login_name": "s068", "password": secrets["operator"]},
                    ).status_code
                    == 201
                )
                with httpx2.Client(base_url=annotation_url, timeout=5) as direct:
                    assert direct.get("/health/ready").status_code == 200
                    assert direct.get("/api/v1/videos").status_code == 200
                process.start()
                wait_status(client, f"/api/{service}/health/ready", 200)
            for service in TARGETS:
                for path in ("health", "debug", "clear", "api/v1/videos", "health/ready/"):
                    assert client.get(f"/api/{service}/{path}").status_code == 404
                for method in ("HEAD", "POST", "PUT", "DELETE"):
                    assert client.request(method, f"/api/{service}/health/ready").status_code == 405
            with installer.connect() as connection:
                connection.execute(text(f"REVOKE CONNECT ON DATABASE {database} FROM {role}"))
                connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        f"WHERE usename = '{role}'"
                    )
                )
            for service in TARGETS:
                assert wait_status(client, f"/api/{service}/health/ready", 503).json() == {
                    "ready": False
                }
            assert client.get("/api/v1/templates/drafts").status_code == 200
            with installer.connect() as connection:
                connection.execute(text(f"GRANT CONNECT ON DATABASE {database} TO {role}"))
            for service in TARGETS:
                wait_status(client, f"/api/{service}/health/ready", 200)
