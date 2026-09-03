"""SYS-22-05 — 匿名与 CSRF 拒绝共用一个形状。

前置 1：匿名（无任何 cookie）。动作 1：`GET /api/v1/auth/session`。
可观察 1：401 `problem+json`，`error_code` 为 `AUTHENTICATION_REQUIRED`，响应带
`x-correlation-id` 头。

前置 2：已登录。动作 2：`DELETE /api/v1/auth/session` 不带 CSRF 头。
可观察 2：403 `problem+json`，`error_code` 为 `CSRF_TOKEN_INVALID`；会话行仍在——拒绝的
请求什么也不做。
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.auth.adapters.repository import PostgresUserRepository
from factory_sop.auth.model import User, UserStatus
from factory_sop.auth.passwords import hash_password
from factory_sop.identifiers import new_id

PASSWORD = "assembly-line-3"
LOGIN_NAME = "wang.li"
SESSION_PATH = "/api/v1/auth/session"


def an_account(engine: Engine) -> User:
    session = DatabaseSession(engine)
    try:
        user = User(
            id=new_id(),
            login_name=LOGIN_NAME,
            display_name="王丽",
            password_hash=hash_password(PASSWORD),
            status=UserStatus.ACTIVE,
        )
        PostgresUserRepository(session).add(user)
        session.commit()
        return user
    finally:
        session.close()


def test_an_anonymous_caller_gets_one_problem_shape(client: TestClient) -> None:
    response = client.get(SESSION_PATH)

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["error_code"] == "AUTHENTICATION_REQUIRED"
    # The correlation header is on every response, whatever it says (SYS-22-06 drives the
    # value through to the diagnostic lines).
    assert response.headers["x-correlation-id"]


def test_a_modifying_request_without_the_csrf_header_changes_nothing(
    client: TestClient, engine: Engine
) -> None:
    an_account(engine)
    assert (
        client.post(SESSION_PATH, json={"login_name": LOGIN_NAME, "password": PASSWORD}).status_code
        == 201
    )

    response = client.delete(SESSION_PATH)

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["error_code"] == "CSRF_TOKEN_INVALID"
    with engine.begin() as connection:
        rows = connection.execute(text("select count(*) from auth_session")).scalar_one()
    assert rows == 1
