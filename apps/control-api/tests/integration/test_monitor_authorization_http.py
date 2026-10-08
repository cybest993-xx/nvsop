"""合成账户经真实 HTTP/PostgreSQL 验证长连接撤权，不覆盖认证依赖。"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import replace

import httpx2
import pytest
import uvicorn
from sqlalchemy import Engine, text
from test_auth_http import CREDENTIALS, SESSION_PATH, an_account, settings
from test_monitor_streaming import _clear_s143_monitor_rows, _health

from factory_sop.app import create_app
from factory_sop.auth.adapters.cookies import CSRF_COOKIE, CSRF_HEADER
from factory_sop.auth.adapters.repository import PostgresRoleRepository, PostgresUserRepository
from factory_sop.auth.api import Permission
from factory_sop.auth.model import Role, UserStatus
from factory_sop.identifiers import new_id
from factory_sop.monitor.adapters.repository import PostgresMonitorRepository
from factory_sop.persistence import session_factory


@pytest.fixture
def live_monitor(engine: Engine) -> Iterator[str]:
    """临时 loopback HTTP 服务只连接测试数据库；不使用固定开发实例。"""
    configuration = settings().model_copy(
        update={"deployment_mode": "fixed_main", "session_cookie_transport": "allow_http"}
    )
    app = create_app(configuration, session_factory=session_factory(engine))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
        worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        worker.start()
        deadline = time.monotonic() + 10
        try:
            while not server.started:
                assert worker.is_alive()
                assert time.monotonic() < deadline
                time.sleep(0.01)
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            worker.join(timeout=20)
            assert not worker.is_alive(), "HTTP 服务未释放 SSE 资源"
            _clear_s143_monitor_rows(engine)
            with engine.begin() as connection:
                connection.execute(text("TRUNCATE auth_user CASCADE"))
                connection.execute(text("DELETE FROM auth_role WHERE code = 's149-viewer'"))


def _grant_monitor(engine: Engine) -> None:
    user = an_account(engine)
    with session_factory(engine)() as session:
        roles = PostgresRoleRepository(session)
        role = Role(
            id=new_id(),
            code="s149-viewer",
            name="合成订阅者",
            permissions=frozenset({Permission.MONITOR_VIEW}),
        )
        roles.add(role)
        roles.assign(user_id=user.id, role_ids=(role.id,))
        session.commit()


def _publish(engine: Engine, event_id: str) -> None:
    with session_factory(engine)() as session:
        PostgresMonitorRepository(session).upsert_health(_health(event_id))
        session.commit()


def _invalidate(engine: Engine, client: httpx2.Client, reason: str) -> None:
    if reason == "logout":
        response = client.delete(SESSION_PATH, headers={CSRF_HEADER: client.cookies[CSRF_COOKIE]})
        assert response.status_code == 204
        return
    with session_factory(engine)() as session:
        users = PostgresUserRepository(session)
        user = users.by_login_name(CREDENTIALS["login_name"])
        assert user is not None
        if reason == "deactivated":
            users.update(replace(user, status=UserStatus.DEACTIVATED))
        elif reason == "revoked":
            PostgresRoleRepository(session).assign(user_id=user.id, role_ids=())
        else:
            column = "created_at" if reason == "absolute_expired" else "last_used_at"
            session.execute(text(f"UPDATE auth_session SET {column} = now() - interval '60 days'"))
        session.commit()


@pytest.mark.parametrize(
    "reason", ["logout", "deactivated", "revoked", "idle_expired", "absolute_expired"]
)
def test_invalidated_stream_stops_before_the_next_batch(
    engine: Engine,
    live_monitor: str,
    reason: str,
) -> None:
    _grant_monitor(engine)
    _publish(engine, "s149-before")
    with httpx2.Client(base_url=live_monitor, timeout=20) as client:
        assert client.post(SESSION_PATH, json=CREDENTIALS).status_code == 201
        with client.stream("GET", "/api/v1/monitor/stream") as response:
            assert response.status_code == 200
            lines = response.iter_lines()
            assert next(lines) == "id: s149-before"
            # 消费整个初始批次，再从独立请求/事务使会话或权限失效。
            while next(lines):
                pass
            _publish(engine, "s149-valid")
            assert next(lines) == "id: s149-valid"
            while next(lines):
                pass
            _invalidate(engine, client, reason)
            _publish(engine, "s149-after")
            assert next(lines, None) is None, "失效授权仍收到新的 SSE 批次"


def test_idle_stream_closes_within_one_heartbeat_without_renewing_session(
    engine: Engine,
    live_monitor: str,
) -> None:
    _grant_monitor(engine)
    _publish(engine, "s149-before")
    with httpx2.Client(base_url=live_monitor, timeout=20) as client:
        assert client.post(SESSION_PATH, json=CREDENTIALS).status_code == 201
        with client.stream("GET", "/api/v1/monitor/stream") as response:
            lines = response.iter_lines()
            while next(lines):
                pass
            with engine.connect() as connection:
                used_at = connection.scalar(text("SELECT last_used_at FROM auth_session"))
            assert next(lines) == ": keep-alive"
            assert next(lines) == ""
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT last_used_at FROM auth_session")) == used_at
            started = time.monotonic()
            _invalidate(engine, client, "revoked")
            assert next(lines, None) is None
            assert time.monotonic() - started < 17, "撤权未在一个 15 秒心跳及调度余量内关闭"
