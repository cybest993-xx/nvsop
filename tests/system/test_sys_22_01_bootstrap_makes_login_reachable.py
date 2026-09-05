"""SYS-22-01 — bootstrap makes login reachable.

前置：全新部署，`auth_user` 为空，无任何会话。
动作：以真实子进程执行 `python -m factory_sop.bootstrap`；用其凭据
`POST /api/v1/auth/session`。
可观察结果：命令退出码 0 且留下 `auth.bootstrap.created` 诊断行；登录 201，两枚 Cookie
符合契约，`expires_at` 为 UTC RFC3339 的 `Z` 形；另一个进程可立即恢复该会话，证明命令与
请求的事务均已提交。
"""

from __future__ import annotations

import json
from typing import Any

from conftest import BootstrapCommand
from httpx2 import Client

BOOTSTRAP_PASSWORD = "first-shift-key"  # pragma: allowlist secret
BOOTSTRAP_LOGIN = "wang.admin"
SESSION_PATH = "/api/v1/auth/session"


def test_the_fresh_deployment_is_enterable(
    client: Client,
    bootstrap: BootstrapCommand,
) -> None:
    # System evidence must cross a real loopback listener, not use an in-process ASGI transport.
    assert str(client.base_url).startswith("https://127.0.0.1:")
    command = bootstrap.run(
        login_name=BOOTSTRAP_LOGIN,
        password=BOOTSTRAP_PASSWORD,
        display_name="王管理员",
    )

    assert command.returncode == 0, command.stderr
    assert "auth.bootstrap.created" in command.stdout

    response = client.post(
        SESSION_PATH,
        json={
            "login_name": BOOTSTRAP_LOGIN,
            "password": BOOTSTRAP_PASSWORD,
        },  # pragma: allowlist secret
    )

    assert response.status_code == 201
    assert response.json()["login_name"] == BOOTSTRAP_LOGIN
    assert response.json()["expires_at"].endswith("Z")
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2
    assert any("httponly" in cookie.lower() for cookie in cookies)
    assert all("secure" in cookie.lower() for cookie in cookies)
    assert client.get(SESSION_PATH).status_code == 200


def test_running_the_command_again_skips(
    bootstrap: BootstrapCommand,
) -> None:
    first = bootstrap.run(
        login_name=BOOTSTRAP_LOGIN,
        password=BOOTSTRAP_PASSWORD,
        display_name="王管理员",
    )
    second = bootstrap.run(
        login_name=BOOTSTRAP_LOGIN,
        password=BOOTSTRAP_PASSWORD,
        display_name="王管理员",
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert "auth.bootstrap.created" in first.stdout
    assert "auth.bootstrap.skipped" in second.stdout
    assert "auth.bootstrap.created" not in second.stdout


def test_two_concurrent_bootstrap_processes_cannot_create_two_first_accounts(
    client: Client,
    bootstrap: BootstrapCommand,
) -> None:
    first = bootstrap.start(
        login_name="wang.first",
        password="first-process-key",  # pragma: allowlist secret
        display_name="第一管理员",
    )
    second = bootstrap.start(
        login_name="zhang.first",
        password="second-process-key",  # pragma: allowlist secret
        display_name="第二管理员",
    )

    first_stdout, first_stderr = first.communicate(timeout=30)
    second_stdout, second_stderr = second.communicate(timeout=30)

    assert first.returncode == 0, first_stderr
    assert second.returncode == 0, second_stderr
    assert first_stderr == ""
    assert second_stderr == ""
    events: list[dict[str, Any]] = [
        json.loads(line)
        for output in (first_stdout, second_stdout)
        for line in output.splitlines()
        if line
    ]
    assert len(events) == 2
    assert {event["event"] for event in events} == {
        "auth.bootstrap.created",
        "auth.bootstrap.skipped",
    }
    for event in events:
        assert event.keys() >= {"event", "module", "correlation_id", "level", "ts", "login_name"}
        assert event["module"] == "auth"
        assert event["correlation_id"] == "none"
        assert event["level"] == "info"
    created = next(event for event in events if event["event"] == "auth.bootstrap.created")
    skipped = next(event for event in events if event["event"] == "auth.bootstrap.skipped")
    assert {created["login_name"], skipped["login_name"]} == {"wang.first", "zhang.first"}
    assert "first-process-key" not in first_stdout + second_stdout
    assert "second-process-key" not in first_stdout + second_stdout

    statuses = {
        "wang.first": client.post(
            SESSION_PATH,
            json={
                "login_name": "wang.first",
                "password": "first-process-key",  # pragma: allowlist secret
            },
        ).status_code,
        "zhang.first": client.post(
            SESSION_PATH,
            json={
                "login_name": "zhang.first",
                "password": "second-process-key",  # pragma: allowlist secret
            },
        ).status_code,
    }
    assert statuses[created["login_name"]] == 201
    assert statuses[skipped["login_name"]] == 401
