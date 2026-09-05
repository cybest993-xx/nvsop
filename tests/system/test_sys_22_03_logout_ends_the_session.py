"""SYS-22-03 — logging out ends the session everywhere.

前置：一个已激活账户，客户端已登录并持有两枚 Cookie。
动作：带 CSRF 头 `DELETE /api/v1/auth/session`；随后重复一次 `DELETE`。
可观察结果：两枚 Cookie 被清除，再次 `GET` 为 401；重复 `DELETE` 仍为 204（幂等）。
"""

from __future__ import annotations

from conftest import BootstrapCommand
from httpx2 import Client

PASSWORD = "assembly-line-3"  # pragma: allowlist secret
LOGIN_NAME = "wang.li"
SESSION_PATH = "/api/v1/auth/session"


def test_logout_revokes_the_session_and_the_cookies(
    client: Client,
    bootstrap: BootstrapCommand,
) -> None:
    command = bootstrap.run(login_name=LOGIN_NAME, password=PASSWORD, display_name="王丽")
    assert command.returncode == 0, command.stderr
    assert (
        client.post(SESSION_PATH, json={"login_name": LOGIN_NAME, "password": PASSWORD}).status_code
        == 201
    )

    csrf = client.cookies["sop_csrf"]
    first = client.delete(SESSION_PATH, headers={"x-csrf-token": csrf})

    assert first.status_code == 204
    assert client.cookies.get("sop_session") is None
    assert client.cookies.get("sop_csrf") is None
    assert client.get(SESSION_PATH).status_code == 401
    # A retry or a second tab: still 204, because a token naming no session has nothing to do.
    assert client.delete(SESSION_PATH).status_code == 204
