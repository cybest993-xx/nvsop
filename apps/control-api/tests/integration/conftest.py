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

import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from _integration_support import MinioServer, RedisServer
from alembic import command
from alembic.config import Config
from docker import DockerClient  # type: ignore[import-untyped]
from minio import Minio
from redis import Redis
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer

# The version the center machine's Compose runs (§六). Pinned rather than `latest`, so a
# release upstream cannot change what the suite proved.
POSTGRES_IMAGE = "postgres:17.6-alpine"

CONTROL_API = Path(__file__).resolve().parents[2]
MINIO_IMAGE = "quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z"
MINIO_CORS_ORIGIN = "*"
REDIS_IMAGE = "redis:7.4-alpine"


def _require_docker() -> None:
    """没有 Docker daemon 时让真实基础设施测试按现有集成层整体跳过。"""
    docker_client: DockerClient | None = None
    try:
        docker_client = DockerClient.from_env()
        docker_client.ping()
    except Exception as error:
        pytest.skip(f"Docker daemon 不可用：{type(error).__name__}")
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
                pytest.skip(f"MinIO 服务不可用：{type(error).__name__}")
            time.sleep(0.25)


def _wait_for_redis(client: Redis) -> None:
    deadline = time.monotonic() + 30
    while True:
        try:
            client.ping()
            return
        except Exception as error:
            if time.monotonic() >= deadline:
                pytest.skip(f"Redis 服务不可用：{type(error).__name__}")
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
def minio_server() -> Iterator[MinioServer]:
    """启动临时 MinIO，并只创建本会话使用的测试 bucket。"""
    _require_docker()
    access_key = f"nvsop{uuid4().hex[:15]}"
    secret_key = f"nvsop{uuid4().hex}{uuid4().hex[:8]}"
    bucket = f"datasets-{uuid4().hex[:12]}"
    container = (
        DockerContainer(MINIO_IMAGE)
        .with_env("MINIO_ROOT_USER", access_key)
        .with_env("MINIO_ROOT_PASSWORD", secret_key)
        .with_env("MINIO_API_CORS_ALLOW_ORIGIN", MINIO_CORS_ORIGIN)
        .with_command("server /data --address :9000 --console-address :9001")
        .with_exposed_ports(9000)
    )
    try:
        container.start()
    except Exception as error:
        pytest.skip(f"MinIO 镜像不可用：{type(error).__name__}")
    try:
        endpoint = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(9000)}"
        client = Minio(endpoint.removeprefix("http://"), access_key, secret_key, secure=False)
        _wait_for_minio(client)
        client.make_bucket(bucket)
        yield MinioServer(
            endpoint=endpoint,
            bucket=bucket,
            access_key=access_key,
            secret_key=secret_key,
        )
    finally:
        container.stop()


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
        pytest.skip(f"Redis 镜像不可用：{type(error).__name__}")
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
        pytest.skip("真实媒体集成测试需要 ffmpeg 和 ffprobe")
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
        pytest.skip("ffmpeg 无法生成真实媒体集成视频")
    if completed.returncode != 0 or not output.is_file():
        pytest.skip("当前 ffmpeg 不提供 libx264 编码器")
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
