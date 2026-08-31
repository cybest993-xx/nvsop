"""The login flow end to end: real HTTP, real PostgreSQL, real transactions.

The adapter suite proves the same routes over in-memory stores, which settles cookie
attributes and the `problem+json` shape. What only this one can settle is what the transaction
does — that a login is durable after the response, that a refusal leaves nothing behind, and
that a session survives the process that opened it, which is the reason §六 stores sessions in
PostgreSQL rather than in a signed cookie.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.app import API_PREFIX, create_app
from factory_sop.auth.adapters.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from factory_sop.auth.adapters.repository import PostgresUserRepository
from factory_sop.auth.model import User, UserStatus
from factory_sop.auth.passwords import hash_password
from factory_sop.identifiers import new_id
from factory_sop.persistence import session_factory
from factory_sop.settings import Settings

SESSION_PATH = f"{API_PREFIX}/auth/session"
CREDENTIALS = {"login_name": "wang.li", "password": "assembly-line-3"}


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


@pytest.fixture
def backend(engine: Engine) -> Iterator[FastAPI]:
    """An application whose Unit of Work draws on the containerized database.

    Each test starts from an empty `auth_user`, so one test's account cannot log in during
    another. `TRUNCATE ... CASCADE` rather than the transaction-rollback fixture the repository
    suite uses: these requests commit, which is the property under test.
    """
    app = create_app(settings())
    app.state.session_factory = session_factory(engine)
    yield app
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE auth_user CASCADE"))


@pytest.fixture
def client(backend: FastAPI) -> Iterator[TestClient]:
    # `https`, because the cookies carry `Secure` and a browser would not send one back over
    # plain HTTP.
    with TestClient(backend, base_url="https://testserver") as opened:
        yield opened


def an_account(engine: Engine, *, status: UserStatus = UserStatus.ACTIVE) -> User:
    """Commit an account, the way the bootstrap command will."""
    user = User(
        id=new_id(),
        login_name=CREDENTIALS["login_name"],
        display_name="王丽",
        password_hash=hash_password(CREDENTIALS["password"]),
        status=status,
    )
    with session_factory(engine)() as session:
        PostgresUserRepository(session).add(user)
        session.commit()
    return user


def open_sessions(engine: Engine) -> int:
    with session_factory(engine)() as session:
        return session.scalar(text("SELECT count(*) FROM auth_session")) or 0


def test_a_login_commits_a_session_row(client: TestClient, engine: Engine) -> None:
    an_account(engine)

    response = client.post(SESSION_PATH, json=CREDENTIALS)

    assert response.status_code == 201
    # Read on a connection of its own: the request's transaction has to have committed for this
    # to see anything, which is what ADR-0002's Unit of Work is responsible for.
    assert open_sessions(engine) == 1


def test_a_refused_login_leaves_no_session_behind(client: TestClient, engine: Engine) -> None:
    an_account(engine)

    response = client.post(SESSION_PATH, json={**CREDENTIALS, "password": "wrong"})

    assert response.status_code == 401
    assert open_sessions(engine) == 0


def test_a_deactivated_account_is_refused_against_the_real_row(
    client: TestClient, engine: Engine
) -> None:
    an_account(engine, status=UserStatus.DEACTIVATED)

    response = client.post(SESSION_PATH, json=CREDENTIALS)

    assert response.status_code == 403
    assert response.json()["error_code"] == "ACCOUNT_DEACTIVATED"
    assert open_sessions(engine) == 0


def test_a_session_survives_the_process_that_opened_it(
    backend: FastAPI, client: TestClient, engine: Engine
) -> None:
    # §六 stores sessions in PostgreSQL so a restart does not log everyone out. A second
    # application over the same database is what a restart looks like from the browser's side:
    # the cookie is unchanged and nothing was kept in memory.
    an_account(engine)
    client.post(SESSION_PATH, json=CREDENTIALS)

    restarted = create_app(settings())
    restarted.state.session_factory = session_factory(engine)
    with TestClient(restarted, base_url="https://testserver") as after:
        after.cookies.set(SESSION_COOKIE, client.cookies[SESSION_COOKIE])
        response = after.get(SESSION_PATH)

    assert response.status_code == 200
    assert response.json()["login_name"] == "wang.li"


def test_using_a_session_commits_the_slid_idle_window(client: TestClient, engine: Engine) -> None:
    an_account(engine)
    client.post(SESSION_PATH, json=CREDENTIALS)
    with session_factory(engine)() as session:
        opened_at = session.scalar(text("SELECT last_used_at FROM auth_session"))

    client.get(SESSION_PATH)

    with session_factory(engine)() as session:
        after = session.scalar(text("SELECT last_used_at FROM auth_session"))
    assert opened_at is not None
    assert after is not None
    assert after >= opened_at


def test_logging_out_deletes_the_row(client: TestClient, engine: Engine) -> None:
    an_account(engine)
    client.post(SESSION_PATH, json=CREDENTIALS)

    response = client.delete(SESSION_PATH, headers={CSRF_HEADER: client.cookies[CSRF_COOKIE]})

    assert response.status_code == 204
    assert open_sessions(engine) == 0


def test_a_refused_csrf_check_does_not_end_the_session(client: TestClient, engine: Engine) -> None:
    an_account(engine)
    client.post(SESSION_PATH, json=CREDENTIALS)

    response = client.delete(SESSION_PATH)

    assert response.status_code == 403
    assert open_sessions(engine) == 1


def test_two_logins_by_one_account_are_two_rows(client: TestClient, engine: Engine) -> None:
    an_account(engine)

    client.post(SESSION_PATH, json=CREDENTIALS)
    client.post(SESSION_PATH, json=CREDENTIALS)

    assert open_sessions(engine) == 2


def test_an_unknown_account_is_refused_without_a_session(
    client: TestClient, engine: Engine
) -> None:
    response = client.post(SESSION_PATH, json=CREDENTIALS)

    assert response.status_code == 401
    assert response.json()["error_code"] == "CREDENTIALS_REJECTED"
    assert open_sessions(engine) == 0


def test_the_liveness_route_touches_no_database(client: TestClient) -> None:
    # It must answer while PostgreSQL is down, or the container is restarted for a fault that
    # is not the container's.
    assert client.get(f"{API_PREFIX}/liveness").status_code == 200


def test_nothing_in_a_response_carries_the_stored_password_hash(
    client: TestClient, engine: Engine
) -> None:
    stored = an_account(engine)

    opened = client.post(SESSION_PATH, json=CREDENTIALS)
    read = client.get(SESSION_PATH)

    for response in (opened, read):
        assert stored.password_hash not in response.text
        assert CREDENTIALS["password"] not in response.text


def test_the_session_token_itself_is_not_stored(client: TestClient, engine: Engine) -> None:
    # The table holds a fingerprint (`auth/tokens.py`), so a database dump cannot be replayed
    # as a login.
    an_account(engine)
    client.post(SESSION_PATH, json=CREDENTIALS)

    with session_factory(engine)() as session:
        stored = session.scalar(text("SELECT token_fingerprint FROM auth_session"))

    assert stored is not None
    assert stored != client.cookies[SESSION_COOKIE]


def test_a_request_scoped_transaction_is_not_shared_between_requests(
    backend: FastAPI, engine: Engine
) -> None:
    # ADR-0002: one request, one Unit of Work. Two requests sharing a session would let one
    # request's uncommitted write be visible to another's read.
    an_account(engine)
    seen: list[DatabaseSession] = []
    original = backend.state.session_factory

    def recording() -> DatabaseSession:
        opened: DatabaseSession = original()
        seen.append(opened)
        return opened

    backend.state.session_factory = recording
    with TestClient(backend, base_url="https://testserver") as client:
        client.post(SESSION_PATH, json=CREDENTIALS)
        client.get(SESSION_PATH)

    assert len(seen) == 2
    assert seen[0] is not seen[1]
