"""SYS-23-02 — 无权限的调用方在用例边界被拒绝，拒绝产生稳定的关联诊断事件。

前置：管理员已登录；一个只持有 `auth.user.view` 的操作员账户已建立并登录。
动作：该操作员 `POST /api/v1/auth/users`，带入站 `x-correlation-id`。
可观察结果：403 `problem+json`、`PERMISSION_DENIED`，响应头回显关联 ID，不产生会话外的
副作用；诊断流出现 `auth.authorization.refused` 行，其 `correlation_id`、`permission`、
`user_id` 与该调用对应——脚本与后台任务调用同一批用例，绕不过这一检查。
"""

from __future__ import annotations

import io
import json
from typing import Any

from conftest import BootstrapCommand
from httpx2 import Client

ADMIN_PASSWORD = "first-shift-key"  # pragma: allowlist secret
OPERATOR_PASSWORD = "assembly-line-3"  # pragma: allowlist secret
SESSION_PATH = "/api/v1/auth/session"
ROLES_PATH = "/api/v1/auth/roles"
USERS_PATH = "/api/v1/auth/users"


def lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


def csrf(client: Client) -> dict[str, str]:
    token = client.cookies.get("sop_csrf")
    assert token is not None
    return {"x-csrf-token": token}


def test_a_view_only_operator_cannot_create_accounts_and_the_refusal_is_logged(
    client: Client,
    bootstrap: BootstrapCommand,
    log: io.StringIO,
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

    viewer_role = client.post(
        ROLES_PATH,
        json={"code": "viewer", "name": "只读", "permissions": ["auth.user.view"]},
        headers=csrf(client),
    )
    assert viewer_role.status_code == 201, viewer_role.text
    operator = client.post(
        USERS_PATH,
        json={"login_name": "zhao.min", "display_name": "赵敏", "password": OPERATOR_PASSWORD},
        headers=csrf(client),
    )
    assert operator.status_code == 201, operator.text
    assigned = client.put(
        f"{USERS_PATH}/{operator.json()['id']}/roles",
        json={"role_ids": [viewer_role.json()["id"]]},
        headers=csrf(client),
    )
    assert assigned.status_code == 200, assigned.text

    # The operator signs in as themselves, in their own browser.
    with Client(base_url=client.base_url, verify=False, trust_env=False) as operator_client:
        opened = operator_client.post(
            SESSION_PATH, json={"login_name": "zhao.min", "password": OPERATOR_PASSWORD}
        )
        assert opened.status_code == 201
        # The session response is where the shell learned what this caller may do.
        assert opened.json()["permissions"] == ["auth.user.view"]

        refused = operator_client.post(
            USERS_PATH,
            json={
                "login_name": "new.person",
                "display_name": "新人",
                "password": "assembly-line-9",  # pragma: allowlist secret
            },
            headers={**csrf(operator_client), "x-correlation-id": "op-23-02"},
        )

    assert refused.status_code == 403
    assert refused.headers["content-type"].startswith("application/problem+json")
    assert refused.json()["error_code"] == "PERMISSION_DENIED"
    assert refused.headers["x-correlation-id"] == "op-23-02"

    events = lines(log)
    denials = [line for line in events if line["event"] == "auth.authorization.refused"]
    assert denials
    denial = denials[-1]
    assert denial["correlation_id"] == "op-23-02"
    assert denial["permission"] == "auth.user.edit"
    assert denial["login_name"] == "zhao.min"
