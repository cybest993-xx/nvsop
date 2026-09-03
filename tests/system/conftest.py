"""§5.15's acceptance scenarios, run the way a deployment runs.

Each scenario is the ID + 前置 + 动作 + 可观察结果 row from issue #22's scenario table, and
the test named for it is what keeps the pair true. These are system suites, not module
suites: the actions go through the interfaces an operator actually uses (the bootstrap
command, the HTTP API), and only the 可观察结果 half looks at the store — that is what the
scenario's observable column names. The container fixture mirrors the center integration
suite's: one PostgreSQL per session, schema built by the Alembic migrations themselves, an
empty `auth_user` for every scenario.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, text
from testcontainers.community.postgres import PostgresContainer

from factory_sop.app import create_app
from factory_sop.observability import configure_logging
from factory_sop.persistence import session_factory
from factory_sop.settings import Settings

POSTGRES_IMAGE = "postgres:17.6-alpine"

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL_API = REPO_ROOT / "apps" / "control-api"


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
def app(engine: Engine) -> Iterator[FastAPI]:
    """The real application over the migrated schema, from an empty `auth_user`."""
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE auth_user, auth_session CASCADE"))
    application = create_app(settings())
    application.state.session_factory = session_factory(engine)
    yield application
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE auth_user, auth_session CASCADE"))


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    # `https`, because the cookies carry `Secure` and a browser would not send one back over
    # plain HTTP — the same reason the center integration suite uses it.
    with TestClient(app, base_url="https://testserver") as opened:
        yield opened


@pytest.fixture(autouse=True)
def log() -> io.StringIO:
    """The stream diagnostic lines render into, at a level that includes the refusals.

    Autouse, because diagnostic rendering is process-global: a scenario that ran the
    bootstrap command left the renderer pointed at that command's own stdout, which pytest
    closed when the scenario ended. Rebinding at every scenario's start is what keeps one
    scenario's infrastructure from becoming the next one's 500.
    """
    stream = io.StringIO()
    configure_logging(log_level="info", stream=stream)
    return stream


def settings() -> Settings:
    """Deployment settings. The database ones are unused: the engine is injected below."""
    return Settings(
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
