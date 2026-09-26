"""Real PostgreSQL for the integration suite, brought up once per session.

Harness §6 keeps this out of `make check`: a developer without Docker must still be able to
run the gate. §4 is why it is not SQLite either — transaction isolation, `JSONB`, timezone
and deferred foreign key behavior differ enough between the two to produce a false green, and
this suite exists precisely to prove the adapter against what production runs.

The schema is created by running the Alembic migrations, not by `metadata.create_all`. The
migrations are what production applies, so this is also the test that they produce the schema
the ORM expects; `create_all` would build the schema from the same model definitions the code
under test uses and could never disagree with them.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4

import pytest
from _integration_support import MinioServer, RedisServer
from alembic import command
from alembic.config import Config
from docker import DockerClient  # type: ignore[import-untyped]
from minio import Minio
from minio.versioningconfig import VersioningConfig
from redis import Redis
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer

# The version the center machine's Compose runs (§六). Pinned rather than `latest`, so a
# release upstream cannot change what the suite proved.
POSTGRES_IMAGE = "postgres:17.6-alpine"

CONTROL_API = Path(__file__).resolve().parents[2]
MINIO_RELEASE = "RELEASE.2025-09-07T16-13-09Z"
MINIO_RELEASE_URL = f"https://github.com/minio/minio/releases/download/{MINIO_RELEASE}"
MINIO_BINARIES = {
    "x86_64": (
        f"minio.linux-amd64.{MINIO_RELEASE}",
        "7c5bd8512c6e966455b1d198209358b2"  # pragma: allowlist secret
        "d191c77a83ab377c4073281065fb855f",  # pragma: allowlist secret
    ),
    "aarch64": (
        f"minio.linux-arm64.{MINIO_RELEASE}",
        "5c83cd2cf151717ba0243f73e1c7802f"  # pragma: allowlist secret
        "f36e272b67144bdd7f1f7d684fd6f03d",  # pragma: allowlist secret
    ),
}
MINIO_CORS_ORIGIN = "*"
REDIS_IMAGE = "redis:7.4-alpine"


def _require_docker() -> None:
    """没有 Docker daemon 时让真实基础设施测试明确失败。"""
    docker_client: DockerClient | None = None
    try:
        docker_client = DockerClient.from_env()
        docker_client.ping()
    except Exception as error:
        pytest.fail(f"Docker daemon 不可用：{type(error).__name__}")
    finally:
        if docker_client is not None:
            docker_client.close()


def _wait_for_minio(client: Minio) -> None:
    deadline = time.monotonic() + 30
    while True:
        try:
            client.list_buckets()
            return
        except Exception as error:
            if time.monotonic() >= deadline:
                pytest.fail(f"MinIO 服务不可用：{type(error).__name__}")
            time.sleep(0.25)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _minio_binary() -> Path:
    """下载并校验固定官方 release binary，避免依赖已下线的 Quay manifest。"""
    architecture = platform.machine().lower()
    specification = MINIO_BINARIES.get(architecture)
    if specification is None:
        pytest.fail(f"MinIO 集成测试不支持当前架构：{architecture}")
    asset, expected_sha256 = specification
    cache_dir = CONTROL_API.parents[1] / ".nvsop" / "cache" / "minio"
    cache_dir.mkdir(parents=True, exist_ok=True)
    binary = cache_dir / asset
    if binary.exists() and _sha256(binary) == expected_sha256:
        binary.chmod(0o755)
        return binary
    with suppress(FileNotFoundError):
        binary.unlink()
    temporary = binary.with_suffix(binary.suffix + ".tmp")
    try:
        with (
            urlopen(f"{MINIO_RELEASE_URL}/{asset}", timeout=120) as response,
            temporary.open("wb") as destination,
        ):
            shutil.copyfileobj(response, destination)
        actual_sha256 = _sha256(temporary)
        if actual_sha256 != expected_sha256:
            pytest.fail(
                f"MinIO release binary 校验失败：expected={expected_sha256}, actual={actual_sha256}"
            )
        temporary.chmod(0o755)
        temporary.replace(binary)
    except (OSError, URLError) as error:
        with suppress(FileNotFoundError):
            temporary.unlink()
        pytest.fail(f"MinIO release binary 不可用：{type(error).__name__}")
    return binary


def _reserve_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_redis(client: Redis) -> None:
    deadline = time.monotonic() + 30
    while True:
        try:
            client.ping()
            return
        except Exception as error:
            if time.monotonic() >= deadline:
                pytest.fail(f"Redis 服务不可用：{type(error).__name__}")
            time.sleep(0.25)


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """A migrated database. One container for the whole session — startup is the slow part."""
    _require_docker()
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as container:
        url = container.get_connection_url()
        configuration = Config(str(CONTROL_API / "alembic.ini"))
        configuration.set_main_option("script_location", str(CONTROL_API / "migrations"))
        configuration.set_main_option("sqlalchemy.url", url)
        command.upgrade(configuration, "head")

        built = create_engine(url)
        yield built
        built.dispose()


@pytest.fixture(scope="session")
def minio_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[MinioServer]:
    """启动固定官方 release binary，并只创建本会话使用的测试 bucket。"""
    access_key = f"nvsop{uuid4().hex[:15]}"
    secret_key = f"nvsop{uuid4().hex}{uuid4().hex[:8]}"
    bucket = f"datasets-{uuid4().hex[:12]}"
    binary = _minio_binary()
    runtime_dir = tmp_path_factory.mktemp("minio")
    data_dir = runtime_dir / "data"
    data_dir.mkdir()
    port = _reserve_tcp_port()
    console_port = _reserve_tcp_port()
    endpoint = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    environment.update(
        {
            "MINIO_ROOT_USER": access_key,
            "MINIO_ROOT_PASSWORD": secret_key,
            "MINIO_API_CORS_ALLOW_ORIGIN": MINIO_CORS_ORIGIN,
        }
    )
    log_path = runtime_dir / "minio.log"
    log = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            str(binary),
            "server",
            str(data_dir),
            "--address",
            f"127.0.0.1:{port}",
            "--console-address",
            f"127.0.0.1:{console_port}",
        ],
        env=environment,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        client = Minio(endpoint.removeprefix("http://"), access_key, secret_key, secure=False)
        _wait_for_minio(client)
        if process.poll() is not None:
            pytest.fail(f"MinIO 进程提前退出：exit={process.returncode}")
        client.make_bucket(bucket)
        client.set_bucket_versioning(bucket, VersioningConfig("Enabled"))
        yield MinioServer(
            endpoint=endpoint,
            bucket=bucket,
            access_key=access_key,
            secret_key=secret_key,
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        log.close()


@pytest.fixture(scope="session")
def redis_server() -> Iterator[RedisServer]:
    """启动临时 Redis，供真实 ARQ dispatcher 使用。"""
    _require_docker()
    container = (
        DockerContainer(REDIS_IMAGE)
        .with_command(["redis-server", "--save", "", "--appendonly", "no"])
        .with_exposed_ports(6379)
    )
    try:
        container.start()
    except Exception as error:
        pytest.fail(f"Redis 镜像不可用：{type(error).__name__}")
    try:
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(6379))
        client = Redis(host=host, port=port, decode_responses=False)
        _wait_for_redis(client)
        client.flushdb()
        yield RedisServer(url=f"redis://{host}:{port}/0", host=host, port=port)
        client.flushdb()
        client.close()
    finally:
        container.stop()


@pytest.fixture
def redis_client(redis_server: RedisServer) -> Iterator[Redis]:
    """每个使用者拥有空的真实 Redis 数据库，避免队列消息互相污染。"""
    client = Redis(
        host=redis_server.host,
        port=redis_server.port,
        decode_responses=False,
    )
    client.flushdb()
    try:
        yield client
    finally:
        client.flushdb()
        client.close()


@pytest.fixture
def real_video_bytes(tmp_path: Path) -> bytes:
    """用真实 ffmpeg 生成临时 H.264/MP4，不把媒体样本写入仓库。"""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None or shutil.which("ffprobe") is None:
        pytest.fail("真实媒体集成测试需要 ffmpeg 和 ffprobe")
    output = tmp_path / "synthetic.mp4"
    try:
        completed = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=32x32:r=24",
                "-t",
                "1",
                "-an",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(output),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.fail("ffmpeg 无法生成真实媒体集成视频")
    if completed.returncode != 0 or not output.is_file():
        pytest.fail("当前 ffmpeg 不提供 libx264 编码器")
    return output.read_bytes()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """A transaction rolled back when the test ends.

    Every test therefore starts from the migrated empty schema without paying for a new
    container, and nothing one test writes can reach another.
    """
    connection = engine.connect()
    transaction = connection.begin()
    opened = sessionmaker(bind=connection)()
    try:
        yield opened
    finally:
        opened.close()
        # A test that provoked an `IntegrityError` has already had its transaction rolled
        # back by the failed statement, so rolling back again warns about a transaction that
        # is no longer associated with the connection.
        if transaction.is_active:
            transaction.rollback()
        connection.close()
