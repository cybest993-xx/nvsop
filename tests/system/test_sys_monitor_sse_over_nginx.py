"""monitor SSE 必须穿过仓库真实 Nginx 网关及时吐出完整 live 帧。

公开 seam：真实 PostgreSQL → GET /api/v1/monitor/stream → deploy/dev/nginx.conf 渲染的真实
Nginx → 客户端。仅“首行 200”不足以证明浏览器看板可用：网关若把响应整体缓冲，客户端在
读超时前拿不到任何完整事件帧，UI 会一直停在“正在连接”。
"""

from __future__ import annotations

import secrets
import time
from pathlib import Path

import httpx2
import pytest
from conftest import (
    ADMIN_CREDENTIALS,
    SESSION_PATH,
    BootstrapCommand,
    csrf_header,
    log_in,
)
from test_sys_machine_identity_over_nginx_https import _nginx_gateway

MONITOR_STREAM = "/api/v1/monitor/stream"
# 完整 SSE frame 必须包含结尾空行，否则 EventSource 不会派发该事件。
LIVE_FRAME = "event: live\ndata: {}\n\n"
# 必须小于服务端 keep-alive 周期（15s），否则“及时出帧”与“等 keep-alive”无法区分。
READ_TIMEOUT_SECONDS = 8.0
# 整个响应读取循环的总预算（不含 Nginx 启动探测），防止非 live 帧源源不断时无限等待。
LIVE_FRAME_DEADLINE_SECONDS = 20.0


def _cookie_header(client: httpx2.Client) -> dict[str, str]:
    """把登录会话放进 Cookie 头。

    手工 Cookie 头会绕过 HTTP `Secure` 属性，因此 http 参数只声称“裸 HTTP 客户端的流式入口”
    可被消费，不代表真实浏览器——真实浏览器走 HTTPS。
    """
    return {"Cookie": "; ".join(f"{name}={value}" for name, value in client.cookies.items())}


def _read_first_live_frame(response: httpx2.Response, *, deadline: float) -> str:
    collected = ""
    for line in response.iter_lines():
        collected += line + "\n"
        if LIVE_FRAME in collected:
            return collected
        assert time.monotonic() < deadline, f"live frame not received within budget: {collected!r}"
    raise AssertionError(f"stream ended before the live frame: {collected!r}")


@pytest.mark.parametrize("gateway_scheme", ["https", "http"])
def test_monitor_sse_gateway_delivers_live_frame(
    client: httpx2.Client,
    bootstrap: BootstrapCommand,
    tmp_path: Path,
    gateway_scheme: str,
) -> None:
    """登录会话经真实 Nginx 一跳，必须在读超时内收到完整 live 帧，而不是挂起。"""
    log_in(client, bootstrap)
    with (
        _nginx_gateway(client, tmp_path, gateway_scheme=gateway_scheme) as gateway,
        gateway.stream(
            "GET",
            MONITOR_STREAM,
            headers=_cookie_header(client),
            timeout=READ_TIMEOUT_SECONDS,
        ) as response,
    ):
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/event-stream")
        collected = _read_first_live_frame(
            response, deadline=time.monotonic() + LIVE_FRAME_DEADLINE_SECONDS
        )
    assert LIVE_FRAME in collected, collected


@pytest.mark.parametrize("gateway_scheme", ["https", "http"])
def test_monitor_sse_gateway_refuses_anonymous(
    client: httpx2.Client,
    bootstrap: BootstrapCommand,
    tmp_path: Path,
    gateway_scheme: str,
) -> None:
    """未登录会话在网关前置鉴权处被拒绝，不会拿到任何流。"""
    log_in(client, bootstrap)
    with _nginx_gateway(client, tmp_path, gateway_scheme=gateway_scheme) as gateway:
        response = gateway.get(MONITOR_STREAM, timeout=READ_TIMEOUT_SECONDS)
    assert response.status_code == 401, response.text


def test_monitor_sse_gateway_refuses_session_without_monitor_view(
    client: httpx2.Client,
    bootstrap: BootstrapCommand,
    tmp_path: Path,
) -> None:
    """有效会话但缺 monitor.report.view：经真实网关仍是 403，不被降级成流。

    仅用现有公开角色/用户/会话接口造最小无权限会话，不改任何产品 RBAC 设置。
    """
    log_in(client, bootstrap)
    browser_headers = csrf_header(client)
    password = secrets.token_urlsafe(24)
    role = client.post(
        "/api/v1/auth/roles",
        headers=browser_headers,
        json={
            "code": "sse_gateway_viewer",
            "name": "SSE 网关只读者",
            "permissions": ["device.station.view"],
        },
    )
    assert role.status_code == 201, role.text
    user = client.post(
        "/api/v1/auth/users",
        headers=browser_headers,
        json={
            "login_name": "sse.gateway.viewer",
            "display_name": "SSE 网关只读者",
            "password": password,
        },
    )
    assert user.status_code == 201, user.text
    assigned = client.put(
        f"/api/v1/auth/users/{user.json()['id']}/roles",
        headers=browser_headers,
        json={"role_ids": [role.json()["id"]]},
    )
    assert assigned.status_code == 200, assigned.text
    opened = client.post(
        SESSION_PATH,
        json={"login_name": "sse.gateway.viewer", "password": password},
    )
    assert opened.status_code == 201, opened.text
    viewer_cookies = {name: client.cookies[name] for name in client.cookies}
    restored = client.post(SESSION_PATH, json=ADMIN_CREDENTIALS)
    assert restored.status_code == 201, restored.text

    cookie_header = {
        "Cookie": "; ".join(f"{name}={value}" for name, value in viewer_cookies.items())
    }
    with _nginx_gateway(client, tmp_path, gateway_scheme="https") as gateway:
        response = gateway.get(MONITOR_STREAM, headers=cookie_header, timeout=READ_TIMEOUT_SECONDS)
    assert response.status_code == 403, response.text
