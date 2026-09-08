"""The login flow end to end: real HTTP, real PostgreSQL, real transactions.

The adapter suite proves the same routes over in-memory stores, which settles cookie
attributes and the `problem+json` shape. What only this one can settle is what the transaction
does — that a login is durable after the response, that a refusal leaves nothing behind, and
that a session survives the process that opened it, which is the reason §六 stores sessions in
PostgreSQL rather than in a signed cookie.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, event, text
from sqlalchemy.orm import Session as DatabaseSession
from starlette.types import Message, Receive, Scope, Send

from factory_sop.app import API_PREFIX, create_app
from factory_sop.auth.adapters.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from factory_sop.auth.adapters.repository import PostgresUserRepository
from factory_sop.auth.model import User, UserStatus
from factory_sop.auth.passwords import hash_password
from factory_sop.identifiers import new_id
from factory_sop.persistence import session_factory
from factory_sop.settings import Settings

SESSION_PATH = f"{API_PREFIX}/auth/session"
CREDENTIALS = {  # pragma: allowlist secret
    "login_name": "wang.li",
    "password": "assembly-line-3",  # pragma: allowlist secret
}


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


def test_a_session_is_usable_when_the_login_response_starts(
    backend: FastAPI, engine: Engine
) -> None:
    """在响应头发出时立即读取会话，不等待 ASGI 请求清理完成。"""
    an_account(engine)
    restored_statuses: list[int] = []

    async def observe_response(scope: Scope, receive: Receive, send: Send) -> None:
        async def observe_headers(message: Message) -> None:
            if (
                message["type"] == "http.response.start"
                and scope.get("method") == "POST"
                and scope["path"] == SESSION_PATH
            ):
                headers = httpx2.Response(
                    message["status"],
                    headers=message["headers"],
                    request=httpx2.Request("POST", f"https://testserver{SESSION_PATH}"),
                )
                async with httpx2.AsyncClient(
                    transport=httpx2.ASGITransport(app=backend),
                    base_url="https://testserver",
                    cookies=headers.cookies,
                ) as next_request:
                    restored = await next_request.get(SESSION_PATH)
                    restored_statuses.append(restored.status_code)
            await send(message)

        await backend(scope, receive, observe_headers)

    with TestClient(observe_response, base_url="https://testserver") as client:
        opened = client.post(SESSION_PATH, json=CREDENTIALS)

    assert (opened.status_code, restored_statuses) == (201, [200])


def test_a_failed_commit_cannot_publish_a_successful_login(
    backend: FastAPI, engine: Engine
) -> None:
    """在事务提交边界注入故障，成功响应及登录 Cookie 都必须被阻止。"""
    an_account(engine)

    def refuse_commit(session: DatabaseSession) -> None:
        raise OSError("injected commit failure")

    event.listen(backend.state.session_factory, "before_commit", refuse_commit)
    with TestClient(
        backend, base_url="https://testserver", raise_server_exceptions=False
    ) as client:
        response = client.post(SESSION_PATH, json=CREDENTIALS)

    assert (response.status_code, SESSION_COOKIE in response.cookies, open_sessions(engine)) == (
        500,
        False,
        0,
    )


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

    # The same request that touched the session can be followed by a normal logout.
    logout = client.delete(SESSION_PATH, headers={CSRF_HEADER: client.cookies[CSRF_COOKIE]})
    assert logout.status_code == 204


def test_logging_out_deletes_the_row(client: TestClient, engine: Engine) -> None:
    an_account(engine)
    client.post(SESSION_PATH, json=CREDENTIALS)
    session_cookie = client.cookies[SESSION_COOKIE]

    response = client.delete(SESSION_PATH, headers={CSRF_HEADER: client.cookies[CSRF_COOKIE]})

    assert response.status_code == 204
    assert open_sessions(engine) == 0

    # A stale tab can still present the old cookie after another tab logged out. It gets the
    # same public refusal as any other unusable session, not a leaked ORM error.
    client.cookies.set(SESSION_COOKIE, session_cookie)
    stale = client.get(SESSION_PATH)
    assert stale.status_code == 401
    assert stale.json()["error_code"] == "SESSION_INVALID"


def test_concurrent_restore_and_logout_are_serialized_without_a_500(
    backend: FastAPI, client: TestClient, engine: Engine
) -> None:
    """Touch-before-logout and logout-before-touch are both legal serial orders.

    The two requests use separate HTTP clients but the same PostgreSQL-backed application, as
    two browser tabs would. A losing restore may be refused as `SESSION_INVALID`; it must never
    leak SQLAlchemy's stale-write exception as a 500.
    """
    an_account(engine)
    opened = client.post(SESSION_PATH, json=CREDENTIALS)
    assert opened.status_code == 201
    session_cookie = client.cookies[SESSION_COOKIE]
    csrf_cookie = client.cookies[CSRF_COOKIE]
    ready = Barrier(2)

    def restore() -> tuple[int, str | None]:
        with TestClient(backend, base_url="https://testserver") as tab:
            tab.cookies.set(SESSION_COOKIE, session_cookie)
            tab.cookies.set(CSRF_COOKIE, csrf_cookie)
            ready.wait(timeout=10)
            response = tab.get(SESSION_PATH)
            return response.status_code, response.json().get("error_code")

    def logout() -> tuple[int, str | None]:
        with TestClient(backend, base_url="https://testserver") as tab:
            tab.cookies.set(SESSION_COOKIE, session_cookie)
            tab.cookies.set(CSRF_COOKIE, csrf_cookie)
            ready.wait(timeout=10)
            response = tab.delete(SESSION_PATH, headers={CSRF_HEADER: csrf_cookie})
            return response.status_code, response.json().get(
                "error_code"
            ) if response.content else None

    with ThreadPoolExecutor(max_workers=2) as executor:
        restored_result, logout_result = executor.map(lambda call: call(), (restore, logout))

    restored_status, restored_error = restored_result
    logout_status, logout_error = logout_result
    assert logout_status == 204
    assert logout_error is None
    assert restored_status in {200, 401}
    if restored_status == 401:
        assert restored_error == "SESSION_INVALID"


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
