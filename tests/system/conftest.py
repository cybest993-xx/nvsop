"""§5.15's acceptance scenarios, run the way a deployment runs.

Each scenario is the ID + 前置 + 动作 + 可观察结果 row from issue #22's scenario table, and
the test named for it is what keeps the pair true. These are system suites, not module
suites: actions and observations cross the interfaces an operator actually uses (the bootstrap
command and the HTTP API). The fixture may touch PostgreSQL only to reset the schema and arrange
a precondition whose public operation belongs to a later slice; scenario assertions do not import
repositories or read tables as a side channel. The container fixture mirrors the center
integration suite's: one PostgreSQL per session, schema built by the Alembic migrations themselves,
an empty `auth_user` for every scenario.
"""

from __future__ import annotations

import io
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from testcontainers.community.postgres import PostgresContainer

POSTGRES_IMAGE = "postgres:17.6-alpine"
SERVER_HOST = "127.0.0.1"
SERVER_START_TIMEOUT_SECONDS = 30.0

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL_API = REPO_ROOT / "apps" / "control-api"


class ServerLog(io.StringIO):
    """Read the JSON diagnostics emitted by the real Uvicorn process."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path

    def getvalue(self) -> str:
        if not self.path.is_file():
            return ""
        return self.path.read_text(encoding="utf-8")


class BootstrapCommand:
    """Run the deployment's published bootstrap module against the test deployment."""

    def __init__(
        self,
        *,
        deployment_environ: dict[str, str],
        secrets_directory: Path,
    ) -> None:
        self._deployment_environ = deployment_environ
        self._secrets_directory = secrets_directory

    def start(
        self,
        *,
        login_name: str,
        password: str,
        display_name: str,
    ) -> subprocess.Popen[str]:
        password_file = self._secrets_directory / f"bootstrap-{login_name}.password"
        password_file.write_text(f"{password}\n", encoding="utf-8")
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "factory_sop.bootstrap",
                "--login-name",
                login_name,
                "--display-name",
                display_name,
                "--password-file",
                str(password_file),
            ],
            cwd=CONTROL_API,
            env={
                **self._deployment_environ,
                "PYTHONPATH": str(CONTROL_API / "src"),
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def run(
        self,
        *,
        login_name: str,
        password: str,
        display_name: str,
    ) -> subprocess.CompletedProcess[str]:
        process = self.start(
            login_name=login_name,
            password=password,
            display_name=display_name,
        )
        stdout, stderr = process.communicate(timeout=30)
        return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """A migrated database. One container for the whole session — startup is the slow part."""
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as container:
        url = container.get_connection_url()
        configuration = Config(str(CONTROL_API / "alembic.ini"))
        configuration.set_main_option("script_location", str(CONTROL_API / "migrations"))
        configuration.set_main_option("sqlalchemy.url", url)
        command.upgrade(configuration, "head")

        built = create_engine(url)
        yield built
        built.dispose()


@pytest.fixture
def deployment_environ(engine: Engine, tmp_path: Path) -> dict[str, str]:
    """The explicit environment passed to the real bootstrap process."""
    database_password = tmp_path / "database-password"
    database_password.write_text(f"{engine.url.password}\n", encoding="utf-8")
    csrf_secret = tmp_path / "csrf-secret"
    csrf_secret.write_text("csrf-secret\n", encoding="utf-8")
    url = engine.url
    assert url.host is not None
    assert url.port is not None
    assert url.database is not None
    assert url.username is not None
    return {
        "SOP_LOG_LEVEL": "info",
        "SOP_DATABASE_HOST": url.host,
        "SOP_DATABASE_PORT": str(url.port),
        "SOP_DATABASE_NAME": url.database,
        "SOP_DATABASE_USER": url.username,
        "SOP_DATABASE_PASSWORD_FILE": str(database_password),
        "SOP_SESSION_IDLE_TIMEOUT_MINUTES": "720",
        "SOP_SESSION_ABSOLUTE_LIFETIME_MINUTES": "43200",
        "SOP_SESSION_COOKIE_TRANSPORT": "require_https",
        "SOP_CSRF_SECRET_FILE": str(csrf_secret),
    }


@pytest.fixture
def bootstrap(deployment_environ: dict[str, str], tmp_path: Path) -> BootstrapCommand:
    return BootstrapCommand(
        deployment_environ=deployment_environ,
        secrets_directory=tmp_path,
    )


@pytest.fixture
def deactivated_account(bootstrap: BootstrapCommand, engine: Engine) -> None:
    """Arrange SYS-22-04's C2.2-owned precondition in one infrastructure fixture.

    Account deactivation has no published interface until C2.2. The scenario's action and every
    observation remain black-box HTTP; only this unavailable precondition is seeded directly.
    """
    command = bootstrap.run(
        login_name="wang.li",
        password="assembly-line-3",  # pragma: allowlist secret
        display_name="王丽",
    )
    assert command.returncode == 0, command.stderr
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE auth_user SET status = 'deactivated' WHERE login_name = 'wang.li'")
        )


def _reset_database(engine: Engine) -> None:
    """Reset the scenario state outside the system boundary, before and after each case."""
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE auth_bootstrap_guard, auth_user, auth_session CASCADE"))


def _make_tls_certificate(directory: Path) -> tuple[Path, Path]:
    """Create a short-lived loopback certificate without adding a test-only Python dependency."""
    certificate = directory / "uvicorn.crt"
    key = directory / "uvicorn.key"
    result = subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-days",
            "1",
            "-subj",
            f"/CN={SERVER_HOST}",
            "-addext",
            f"subjectAltName=IP:{SERVER_HOST}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"could not create the system-test TLS certificate: {result.stderr}")
    return certificate, key


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((SERVER_HOST, 0))
        return int(listener.getsockname()[1])


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _wait_for_server(
    process: subprocess.Popen[str],
    *,
    base_url: str,
    stdout_path: Path,
    stderr_path: Path,
) -> None:
    deadline = time.monotonic() + SERVER_START_TIMEOUT_SECONDS
    with httpx2.Client(
        base_url=base_url,
        verify=False,
        trust_env=False,
        timeout=1.0,
    ) as probe:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    "uvicorn exited before becoming ready\n"
                    f"stdout:\n{_read_text(stdout_path)}\n"
                    f"stderr:\n{_read_text(stderr_path)}"
                )
            try:
                if probe.get("/api/v1/liveness").status_code == 200:
                    return
            except httpx2.HTTPError:
                pass
            time.sleep(0.05)
    raise TimeoutError(
        "uvicorn did not become ready within 30 seconds\n"
        f"stdout:\n{_read_text(stdout_path)}\n"
        f"stderr:\n{_read_text(stderr_path)}"
    )


def _stop_server(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


@pytest.fixture
def client(
    engine: Engine,
    deployment_environ: dict[str, str],
    tmp_path: Path,
    log: ServerLog,
) -> Iterator[httpx2.Client]:
    """Talk to the published app through a real TLS-enabled Uvicorn subprocess."""
    certificate, key = _make_tls_certificate(tmp_path)
    _reset_database(engine)
    port = _free_port()
    base_url = f"https://{SERVER_HOST}:{port}"
    stderr_path = tmp_path / "uvicorn.stderr"
    stdout = log.path.open("w", encoding="utf-8", buffering=1)
    stderr = stderr_path.open("w", encoding="utf-8", buffering=1)
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "--factory",
                "factory_sop.entrypoint:build",
                "--host",
                SERVER_HOST,
                "--port",
                str(port),
                "--ssl-certfile",
                str(certificate),
                "--ssl-keyfile",
                str(key),
                "--no-access-log",
                "--log-level",
                "warning",
            ],
            cwd=CONTROL_API,
            env={
                **deployment_environ,
                "PYTHONPATH": str(CONTROL_API / "src"),
                "PYTHONUNBUFFERED": "1",
            },
            stdout=stdout,
            stderr=stderr,
            text=True,
        )
        _wait_for_server(
            process,
            base_url=base_url,
            stdout_path=log.path,
            stderr_path=stderr_path,
        )
        with httpx2.Client(
            base_url=base_url,
            verify=False,
            trust_env=False,
        ) as opened:
            yield opened
    finally:
        if process is not None:
            _stop_server(process)
        stdout.close()
        stderr.close()
        _reset_database(engine)


@pytest.fixture(autouse=True)
def log(tmp_path: Path) -> ServerLog:
    """Expose the real server's structured stdout without installing an in-process app."""
    return ServerLog(tmp_path / "uvicorn.stdout")
