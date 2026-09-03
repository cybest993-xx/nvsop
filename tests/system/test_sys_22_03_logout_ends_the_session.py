"""SYS-22-03 — logging out ends the session everywhere.

前置：一个已激活账户，客户端已登录并持有两枚 cookie。
动作：带 CSRF 头 `DELETE /api/v1/auth/session`；随后重复一次 `DELETE`。
可观察结果：`auth_session` 无行、两枚 cookie 被清除、再次 `GET` 为 401；重复 `DELETE` 仍为
204（幂等），不是错误。
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


def test_logout_revokes_the_row_and_the_cookies(client: TestClient, engine: Engine) -> None:
    an_account(engine)
    assert (
        client.post(SESSION_PATH, json={"login_name": LOGIN_NAME, "password": PASSWORD}).status_code
        == 201
    )

    csrf = client.cookies["sop_csrf"]
    first = client.delete(SESSION_PATH, headers={"x-csrf-token": csrf})

    assert first.status_code == 204
    with engine.begin() as connection:
        rows = connection.execute(text("select count(*) from auth_session")).scalar_one()
    assert rows == 0
    assert client.cookies.get("sop_session") is None
    assert client.get(SESSION_PATH).status_code == 401

    # A retry or a second tab: still 204, because a token naming no session has nothing to do.
    assert client.delete(SESSION_PATH).status_code == 204
