"""SYS-23-03 — 停用用户在同一个业务操作中撤销其全部会话。

前置：管理员已登录；操作员赵敏经 API 创建并已登录（会话存活）。
动作：管理员 `PUT /api/v1/auth/users/{id}/status`（status=deactivated）。
可观察结果：响应 200 且 `revoked_sessions` 至少为 1；赵敏的下一个请求立即 401
`SESSION_INVALID`，再次登录被 403 `ACCOUNT_DEACTIVATED` 拒绝——不是等到会话自然过期，
也不是下一个请求才慢慢失效。
"""

from __future__ import annotations

from conftest import BootstrapCommand
from httpx2 import Client

ADMIN_PASSWORD = "first-shift-key"  # pragma: allowlist secret
OPERATOR_PASSWORD = "assembly-line-3"  # pragma: allowlist secret
SESSION_PATH = "/api/v1/auth/session"
USERS_PATH = "/api/v1/auth/users"


def csrf(client: Client) -> dict[str, str]:
    token = client.cookies.get("sop_csrf")
    assert token is not None
    return {"x-csrf-token": token}


def test_deactivating_an_account_ends_its_live_sessions_in_the_same_operation(
    client: Client,
    bootstrap: BootstrapCommand,
) -> None:
    command = bootstrap.run(
        login_name="wang.admin", password=ADMIN_PASSWORD, display_name="王管理员"
    )
    assert command.returncode == 0, command.stderr
    assert (
        client.post(
            SESSION_PATH, json={"login_name": "wang.admin", "password": ADMIN_PASSWORD}
        ).status_code
        == 201
    )

    operator = client.post(
        USERS_PATH,
        json={"login_name": "zhao.min", "display_name": "赵敏", "password": OPERATOR_PASSWORD},
        headers=csrf(client),
    )
    assert operator.status_code == 201, operator.text

    # The operator is standing at a terminal with a live session.
    with Client(base_url=client.base_url, verify=False, trust_env=False) as operator_client:
        opened = operator_client.post(
            SESSION_PATH, json={"login_name": "zhao.min", "password": OPERATOR_PASSWORD}
        )
        assert opened.status_code == 201
        assert operator_client.get(SESSION_PATH).status_code == 200

        deactivated = client.put(
            f"{USERS_PATH}/{operator.json()['id']}/status",
            json={"status": "deactivated"},
            headers=csrf(client),
        )
        assert deactivated.status_code == 200, deactivated.text
        outcome = deactivated.json()
        assert outcome["user"]["status"] == "deactivated"
        assert outcome["revoked_sessions"] >= 1

        # The next request on the already-open browser is refused immediately: the revocation
        # happened inside the deactivation, not by a sweep that might still be catching up.
        assert operator_client.get(SESSION_PATH).status_code == 401

    # And the account cannot open a new session, under its own code.
    refused = client.post(
        SESSION_PATH, json={"login_name": "zhao.min", "password": OPERATOR_PASSWORD}
    )
    assert refused.status_code == 403
    assert refused.json()["error_code"] == "ACCOUNT_DEACTIVATED"
