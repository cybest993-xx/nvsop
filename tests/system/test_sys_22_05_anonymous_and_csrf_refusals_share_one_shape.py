"""SYS-22-05 — anonymous and CSRF refusals share one problem shape.

前置 1：匿名。动作 1：`GET /api/v1/auth/session`。
可观察 1：401 `problem+json`、`AUTHENTICATION_REQUIRED`，且响应带关联头。

前置 2：已登录。动作 2：不带 CSRF 头 `DELETE /api/v1/auth/session`。
可观察 2：403 `problem+json`、`CSRF_TOKEN_INVALID`；随后 `GET` 仍为 200，证明被拒请求
没有撤销会话。
"""

from __future__ import annotations

from conftest import BootstrapCommand
from httpx2 import Client

PASSWORD = "assembly-line-3"  # pragma: allowlist secret
LOGIN_NAME = "wang.li"
SESSION_PATH = "/api/v1/auth/session"


def test_an_anonymous_caller_gets_one_problem_shape(client: Client) -> None:
    response = client.get(SESSION_PATH)

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["error_code"] == "AUTHENTICATION_REQUIRED"
    assert response.headers["x-correlation-id"]


def test_a_modifying_request_without_the_csrf_header_changes_nothing(
    client: Client,
    bootstrap: BootstrapCommand,
) -> None:
    command = bootstrap.run(login_name=LOGIN_NAME, password=PASSWORD, display_name="王丽")
    assert command.returncode == 0, command.stderr
    assert (
        client.post(SESSION_PATH, json={"login_name": LOGIN_NAME, "password": PASSWORD}).status_code
        == 201
    )

    response = client.delete(SESSION_PATH)

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["error_code"] == "CSRF_TOKEN_INVALID"
    assert client.get(SESSION_PATH).status_code == 200
