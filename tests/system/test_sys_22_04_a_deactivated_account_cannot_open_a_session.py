"""SYS-22-04 — a deactivated account cannot open a session.

前置：一个 status 为 deactivated 的账户（前置状态直接置库；停用用例本身属 C2.2）。
动作：用**正确**凭据 `POST /api/v1/auth/session`。
可观察结果：403 `problem+json`，`error_code` 为 `ACCOUNT_DEACTIVATED`——与凭据错误
（401）不同码，因为它向刚证明持有凭据的人披露状态；无新 `auth_session` 行；响应无
`Set-Cookie`。
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


def a_deactivated_account(engine: Engine) -> None:
    session = DatabaseSession(engine)
    try:
        user = User(
            id=new_id(),
            login_name=LOGIN_NAME,
            display_name="王丽",
            password_hash=hash_password(PASSWORD),
            status=UserStatus.DEACTIVATED,
        )
        PostgresUserRepository(session).add(user)
        session.commit()
    finally:
        session.close()


def test_the_correct_password_is_refused_under_its_own_code(
    client: TestClient, engine: Engine
) -> None:
    a_deactivated_account(engine)

    response = client.post(SESSION_PATH, json={"login_name": LOGIN_NAME, "password": PASSWORD})

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["error_code"] == "ACCOUNT_DEACTIVATED"
    assert response.headers.get_list("set-cookie") == []
    with engine.begin() as connection:
        rows = connection.execute(text("select count(*) from auth_session")).scalar_one()
    assert rows == 0
