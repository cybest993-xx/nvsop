"""SYS-22-04 — a deactivated account cannot open a session.

前置：一个 status 为 deactivated 的账户（停用接口属 C2.2，测试基础设施只安排此前置）。
动作：用正确凭据 `POST /api/v1/auth/session`。
可观察结果：403 `problem+json`、`ACCOUNT_DEACTIVATED`，无 `Set-Cookie`；随后恢复会话仍为
401，证明拒绝没有创建会话。
"""

from __future__ import annotations

from httpx2 import Client

PASSWORD = "assembly-line-3"  # pragma: allowlist secret
LOGIN_NAME = "wang.li"
SESSION_PATH = "/api/v1/auth/session"


def test_the_correct_password_is_refused_under_its_own_code(
    client: Client,
    deactivated_account: None,
) -> None:
    response = client.post(SESSION_PATH, json={"login_name": LOGIN_NAME, "password": PASSWORD})

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["error_code"] == "ACCOUNT_DEACTIVATED"
    assert response.headers.get_list("set-cookie") == []
    assert client.get(SESSION_PATH).status_code == 401
