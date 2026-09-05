"""The bootstrap command against real PostgreSQL, and the login it makes reachable.

The unit suite settles what the use case creates and what it refuses. What only this one
can settle is the command itself — that the environment it documents is sufficient, that the
account it writes is committed by the time the process exits, and that the login form's
`POST /auth/session` accepts what it created. That last one is the point: a bootstrap that
wrote an account the routes could not authenticate would pass every unit test and still
leave a deployment nobody can enter.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text

from factory_sop.app import API_PREFIX, create_app
from factory_sop.auth.adapters.repository import (
    PostgresSessionRepository,
    PostgresUserRepository,
)
from factory_sop.auth.model import SessionPolicy
from factory_sop.auth.usecases.sessions import open_session
from factory_sop.bootstrap import main
from factory_sop.persistence import session_factory
from factory_sop.settings import Settings

SESSION_PATH = f"{API_PREFIX}/auth/session"
BOOTSTRAP_PASSWORD = "first-shift-key"  # pragma: allowlist secret


@pytest.fixture
def deployment_environ(engine: Engine, tmp_path: Path) -> dict[str, str]:
    """The environment a real deployment runs the command with, aimed at the container.

    Every setting is stated rather than defaulted — that is the point of the exercise. The
    secrets arrive as file paths, the way the deployment's secret store would mount them.
    """
    database_password = tmp_path / "database-password"
    database_password.write_text(f"{engine.url.password}\n", encoding="utf-8")
    bootstrap_password = tmp_path / "bootstrap-password"
    bootstrap_password.write_text(f"{BOOTSTRAP_PASSWORD}\n", encoding="utf-8")
    csrf_secret = tmp_path / "csrf-secret"
    csrf_secret.write_text("csrf-secret\n", encoding="utf-8")
    url = engine.url
    # The container URL carries all five parts; the annotations on `URL` allow `None` for
    # URLs built by hand, which this is not.
    assert url.host is not None
    assert url.port is not None
    assert url.database is not None
    assert url.username is not None
    return {
        "SOP_LOG_LEVEL": "warning",
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


@pytest.fixture(autouse=True)
def clean_slate(engine: Engine) -> Iterator[None]:
    """Start from the empty schema the migration produces, as every system test does.

    `auth_role` is truncated alongside the accounts: the bootstrap now seeds the administrator
    role, the command commits it, and a role surviving into the next test would collide with
    the seed's unique code. `CASCADE` carries the assignment and permission rows with it; the
    `auth_permission` registry is migration-seeded reference data and is never truncated.
    """
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE auth_bootstrap_guard, auth_user, auth_role CASCADE"))
    yield
    with engine.begin() as connection:
        connection.execute(
            text("TRUNCATE auth_bootstrap_guard, auth_user, auth_role, auth_session CASCADE")
        )


def bootstrap_argv(tmp_path: Path) -> list[str]:
    return [
        "--login-name",
        "wang.admin",
        "--display-name",
        "王管理员",
        "--password-file",
        str(tmp_path / "bootstrap-password"),
    ]


def an_application(engine: Engine) -> FastAPI:
    """The real application, its Unit of Work drawing on the containerized database."""
    settings = Settings(
        log_level="warning",
        database_host="unused",
        database_port=5432,
        database_name="unused",
        database_user="unused",
        database_password=SecretStr("unused"),
        session_idle_timeout_minutes=720,
        session_absolute_lifetime_minutes=43200,
        session_cookie_transport="require_https",
        csrf_secret=SecretStr("csrf-secret"),
    )
    app = create_app(settings)
    app.state.session_factory = session_factory(engine)
    return app


def test_the_command_makes_the_deployment_enterable(
    engine: Engine, deployment_environ: dict[str, str], tmp_path: Path
) -> None:
    assert main(argv=bootstrap_argv(tmp_path), environ=deployment_environ) == 0

    # The command ran in its own unit of work, so what it wrote has to be visible to this
    # one — committed, not merely flushed. The session is closed before returning: a test
    # that left its read transaction open would block the next test's `TRUNCATE`.
    with session_factory(engine)() as session:
        users = PostgresUserRepository(session)
        operator = users.by_login_name("wang.admin")
        assert operator is not None
        assert operator.display_name == "王管理员"

        # The login the command exists for: the use case behind `POST /auth/session` accepts
        # the account with the password the secret file held.
        opened = open_session(
            login_name="wang.admin",
            password=BOOTSTRAP_PASSWORD,
            users=users,
            sessions=PostgresSessionRepository(session),
            policy=SessionPolicy(
                idle_timeout=timedelta(hours=12), absolute_lifetime=timedelta(days=30)
            ),
            now=datetime.now(UTC),
        )
        assert opened.user.login_name == "wang.admin"


def test_running_the_command_again_creates_nothing(
    engine: Engine, deployment_environ: dict[str, str], tmp_path: Path
) -> None:
    assert main(argv=bootstrap_argv(tmp_path), environ=deployment_environ) == 0
    with session_factory(engine)() as session:
        users = PostgresUserRepository(session)
        first = users.by_login_name("wang.admin")
        assert first is not None

        assert main(argv=bootstrap_argv(tmp_path), environ=deployment_environ) == 0

        # Same account, same identity: the second run skipped rather than minted or replaced.
        assert users.by_login_name("wang.admin") == first


def test_the_bootstrapped_account_logs_in_over_http(
    engine: Engine, deployment_environ: dict[str, str], tmp_path: Path
) -> None:
    """The whole chain: command → row → HTTP session, against the real application."""
    assert main(argv=bootstrap_argv(tmp_path), environ=deployment_environ) == 0

    with TestClient(an_application(engine), base_url="https://testserver") as client:
        response = client.post(
            SESSION_PATH,
            json={"login_name": "wang.admin", "password": BOOTSTRAP_PASSWORD},
        )

    assert response.status_code == 201
    assert response.json()["login_name"] == "wang.admin"
    assert response.json()["expires_at"].endswith("Z")
