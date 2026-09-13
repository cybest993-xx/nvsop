"""SYS-23-04 — 角色不能引入未注册权限，拒绝留下关联的诊断事件。

前置：管理员已登录，带入站关联 ID。
动作：`POST /api/v1/auth/roles`，权限列表里混入一个未注册的三段式字符串
`auth.user.approve`（形似权限，但没有任何模块注册它）。
可观察结果：422 `problem+json`、`PERMISSION_UNREGISTERED`，`detail` 指名是哪个权限；
诊断流出现 `auth.role.refused` 行（error_code、permission、actor_id 与请求对应）；
随后 `GET /api/v1/auth/roles` 中不存在该角色——整个请求被拒，而不是把能解析的一半存下来。
"""

from __future__ import annotations

import io
import json
from typing import Any

from conftest import BootstrapCommand
from httpx2 import Client

ADMIN_PASSWORD = "first-shift-key"  # pragma: allowlist secret
SESSION_PATH = "/api/v1/auth/session"
ROLES_PATH = "/api/v1/auth/roles"


def csrf(client: Client) -> dict[str, str]:
    token = client.cookies.get("sop_csrf")
    assert token is not None
    return {"x-csrf-token": token}


def test_a_role_naming_an_unregistered_permission_is_refused_whole(
    client: Client,
    bootstrap: BootstrapCommand,
    log: io.StringIO,
) -> None:
    def lines(stream: io.StringIO) -> list[dict[str, Any]]:
        return [json.loads(entry) for entry in stream.getvalue().splitlines() if entry]

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

    refused = client.post(
        ROLES_PATH,
        json={
            "code": "approver",
            "name": "审批人",
            "permissions": ["auth.user.view", "auth.user.approve"],
        },
        headers={**csrf(client), "x-correlation-id": "op-23-04"},
    )

    assert refused.status_code == 422
    assert refused.headers["content-type"].startswith("application/problem+json")
    body = refused.json()
    assert body["error_code"] == "PERMISSION_UNREGISTERED"
    assert "auth.user.approve" in body["detail"]
    # The registered half is not silently stored: nothing is.
    roles = client.get(ROLES_PATH)
    assert roles.status_code == 200
    assert {role["code"] for role in roles.json()["items"]} == {"system_administrator"}

    # AC4: the refusal is a stable diagnostic event, correlated with the request.
    events = [json.loads(line) for line in log.getvalue().splitlines() if line]
    refusals = [
        line
        for line in events
        if line["event"] == "auth.role.refused" and line["correlation_id"] == "op-23-04"
    ]
    assert refusals
    assert refusals[-1]["error_code"] == "PERMISSION_UNREGISTERED"
    assert refusals[-1]["permission"] == "auth.user.approve"
