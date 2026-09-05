"""SYS-22-02 — a held session is restored, and using it slides the idle window.

前置：一个已激活账户，其会话已由登录打开，客户端持有两枚 Cookie。
动作：`GET /api/v1/auth/session`。
可观察结果：200 且为同一身份；响应中的 `expires_at` 前移，证明同一会话的滑动空闲窗已
更新，无需从系统边界之外读取会话表。
"""

from __future__ import annotations

from datetime import datetime

from conftest import BootstrapCommand
from httpx2 import Client

PASSWORD = "assembly-line-3"  # pragma: allowlist secret
LOGIN_NAME = "wang.li"
SESSION_PATH = "/api/v1/auth/session"


def test_the_held_session_is_restored_and_slides(
    client: Client,
    bootstrap: BootstrapCommand,
) -> None:
    command = bootstrap.run(login_name=LOGIN_NAME, password=PASSWORD, display_name="王丽")
    assert command.returncode == 0, command.stderr
    opened = client.post(
        SESSION_PATH,
        json={"login_name": LOGIN_NAME, "password": PASSWORD},
    )
    assert opened.status_code == 201
    before = datetime.fromisoformat(opened.json()["expires_at"])

    response = client.get(SESSION_PATH)

    assert response.status_code == 200
    assert response.json()["login_name"] == LOGIN_NAME
    assert datetime.fromisoformat(response.json()["expires_at"]) > before
