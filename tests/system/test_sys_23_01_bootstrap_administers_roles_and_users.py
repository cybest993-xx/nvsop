"""SYS-23-01 — 引导账户经由角色持有全部权限，并可用同一套 API 创建角色与用户。

前置：全新部署，`auth_user` 为空。
动作：运行 bootstrap；用其凭据登录；`POST /api/v1/auth/roles` 建一个含注册权限的角色；
`POST /api/v1/auth/users` 建一个账户；`PUT /api/v1/auth/users/{id}/roles` 把角色赋给它。
可观察结果：引导登录响应的 `permissions` 即全部注册权限；各写操作返回 201/200；列表接口
能看到新角色（含权限串）与新账户（含其角色）；诊断流出现 `auth.user.created` 行。
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
USERS_PATH = "/api/v1/auth/users"


def lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


def csrf(client: Client) -> dict[str, str]:
    token = client.cookies.get("sop_csrf")
    assert token is not None
    return {"x-csrf-token": token}


def test_the_first_administrator_administers_roles_and_users_through_the_api(
    client: Client,
    bootstrap: BootstrapCommand,
    log: io.StringIO,
) -> None:
    command = bootstrap.run(
        login_name="wang.admin",
        password=ADMIN_PASSWORD,
        display_name="王管理员",
    )
    assert command.returncode == 0, command.stderr

    login = client.post(SESSION_PATH, json={"login_name": "wang.admin", "password": ADMIN_PASSWORD})
    assert login.status_code == 201
    # The bootstrap grants through the seed role, and the response reports the effective set:
    # everything registered in this build, not a curated subset.
    granted = set(login.json()["permissions"])
    assert "auth.role.edit" in granted
    assert "auth.user.delete" in granted

    created_role = client.post(
        ROLES_PATH,
        json={
            "code": "dataset_manager",
            "name": "数据集管理者",
            "permissions": ["auth.user.view", "auth.role.view"],
        },
        headers=csrf(client),
    )
    assert created_role.status_code == 201, created_role.text
    role_id = created_role.json()["id"]
    assert created_role.json()["permissions"] == ["auth.role.view", "auth.user.view"]

    created_user = client.post(
        USERS_PATH,
        # Synthetic scenario credentials (§4); the pragma marks the scanner false positive.
        json={
            "login_name": "zhao.min",
            "display_name": "赵敏",
            "password": "assembly-line-3",  # pragma: allowlist secret
        },
        headers=csrf(client),
    )
    assert created_user.status_code == 201, created_user.text
    user_id = created_user.json()["id"]
    assert created_user.json()["status"] == "active"

    assigned = client.put(
        f"{USERS_PATH}/{user_id}/roles",
        json={"role_ids": [role_id]},
        headers=csrf(client),
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["role_ids"] == [role_id]

    users = client.get(USERS_PATH)
    assert users.status_code == 200
    listed = {user["login_name"]: user for user in users.json()["items"]}
    assert set(listed) == {"wang.admin", "zhao.min"}

    roles = client.get(ROLES_PATH)
    assert roles.status_code == 200
    assert {role["code"] for role in roles.json()["items"]} == {
        "system_administrator",
        "dataset_manager",
    }
    # The §5.15 envelope: a listing is a page, not a bare array.
    assert users.json()["page"] == 1
    assert users.json()["total"] == 2

    events = lines(log)
    created_lines = [
        line for line in events if line["event"] == "auth.user.created" and "zhao.min" in str(line)
    ]
    assert created_lines
