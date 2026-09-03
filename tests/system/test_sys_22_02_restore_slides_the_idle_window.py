"""SYS-22-02 — a held session is restored, and using it slides the idle window.

前置：一个已激活账户，其会话已由登录打开，客户端持有两枚 cookie。
动作：`GET /api/v1/auth/session`。
可观察结果：200 且为同一身份；`auth_session.last_used_at` 前移——同一行在 GET 前后两次读
到的值不同。
"""

from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.auth.adapters.repository import PostgresUserRepository
from factory_sop.auth.model import User, UserStatus
from factory_sop.auth.passwords import hash_password
from factory_sop.identifiers import new_id

PASSWORD = "assembly-line-3"
LOGIN_NAME = "wang.li"


def an_account(engine: Engine) -> User:
    """前置状态：一个已激活账户。Written through the store directly, not the routes under test."""
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


def last_used_at(engine: Engine) -> datetime:
    with engine.begin() as connection:
        row = connection.execute(text("select last_used_at from auth_session")).scalar_one()
    assert row is not None
    return row


def test_the_held_session_is_restored_and_slides(client: TestClient, engine: Engine) -> None:
    an_account(engine)
    assert (
        client.post(
            "/api/v1/auth/session", json={"login_name": LOGIN_NAME, "password": PASSWORD}
        ).status_code
        == 201
    )
    before = last_used_at(engine)

    response = client.get("/api/v1/auth/session")

    assert response.status_code == 200
    assert response.json()["login_name"] == LOGIN_NAME
    assert last_used_at(engine) > before
