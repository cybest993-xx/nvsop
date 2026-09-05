"""SYS-22-06 — 每个拒绝都被关联，且任何诊断行不含凭据。

前置：服务在跑，诊断渲染捕获到流；一个已激活账户；入站 `x-correlation-id` 由调用方带来。
动作：带入站关联 ID 的两次拒绝——错误密码 `POST`（401）、已登录无 CSRF 头 `DELETE`
（403，场景含登录自身）。
可观察结果：两个响应的 `x-correlation-id` 头回显入站值；`auth.session.refused`、
`http.request.csrf_rejected`、`http.request.completed` 各行的 `correlation_id` 与之同值，
每行含 §5.15 的五个必填字段；渲染文本不含密码原文。
"""

from __future__ import annotations

import io
import json
from typing import Any

from conftest import BootstrapCommand
from httpx2 import Client

PASSWORD = "assembly-line-3"  # pragma: allowlist secret
WRONG_PASSWORD = "assembly-line-4"  # pragma: allowlist secret
LOGIN_NAME = "wang.li"
SESSION_PATH = "/api/v1/auth/session"

# The five §5.15 requires on every line, whatever the event.
MANDATORY_FIELDS = {"event", "module", "correlation_id", "level", "ts"}


def lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


def test_refusals_carry_the_caller_s_correlation_id_and_no_credential(
    client: Client,
    bootstrap: BootstrapCommand,
    log: io.StringIO,
) -> None:
    command = bootstrap.run(login_name=LOGIN_NAME, password=PASSWORD, display_name="王丽")
    assert command.returncode == 0, command.stderr
    login = client.post(
        SESSION_PATH,
        json={"login_name": LOGIN_NAME, "password": WRONG_PASSWORD},
        headers={"x-correlation-id": "op-refused-login"},
    )
    assert login.status_code == 401
    assert login.headers["x-correlation-id"] == "op-refused-login"

    client.post(SESSION_PATH, json={"login_name": LOGIN_NAME, "password": PASSWORD})
    assert client.cookies.get("sop_session") is not None
    logout = client.delete(SESSION_PATH, headers={"x-correlation-id": "op-refused-csrf"})
    assert logout.status_code == 403
    assert logout.headers["x-correlation-id"] == "op-refused-csrf"

    rendered = log.getvalue()
    events = lines(log)

    refused = [
        line
        for line in events
        if line["event"] == "auth.session.refused" and line["correlation_id"] == "op-refused-login"
    ]
    assert refused
    rejected = [
        line
        for line in events
        if line["event"] == "http.request.csrf_rejected"
        and line["correlation_id"] == "op-refused-csrf"
    ]
    assert rejected
    login_completed = [
        line
        for line in events
        if line["event"] == "http.request.completed"
        and line["method"] == "POST"
        and line["correlation_id"] == "op-refused-login"
    ]
    assert login_completed
    csrf_completed = [
        line
        for line in events
        if line["event"] == "http.request.completed"
        and line["method"] == "DELETE"
        and line["correlation_id"] == "op-refused-csrf"
    ]
    assert csrf_completed

    for line in events:
        assert line.keys() >= MANDATORY_FIELDS
    assert WRONG_PASSWORD not in rendered
    assert PASSWORD not in rendered
