"""隔离真实 HTTP/PostgreSQL 验证 SSE 有界资源和业务请求互不饥饿。"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, suppress
from dataclasses import replace
from unittest.mock import patch

import httpx2
import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import Engine, create_engine, text
from test_auth_http import CREDENTIALS, SESSION_PATH, an_account, settings
from test_monitor_streaming import _clear_s143_monitor_rows, _health
from test_runtime_observation_http import RuntimeTopology, _host_headers
from test_runtime_observation_http import runtime_topology as runtime_topology

from factory_sop.app import create_app
from factory_sop.auth.adapters.repository import PostgresRoleRepository
from factory_sop.auth.api import Permission
from factory_sop.auth.model import Role
from factory_sop.identifiers import new_id
from factory_sop.monitor.adapters.repository import PostgresMonitorRepository
from factory_sop.monitor.adapters.streaming import PostgresMonitorStreamSource
from factory_sop.persistence import session_factory
from nvsop_contracts import reported_health_to_wire


@pytest.fixture
def constrained_monitor(engine: Engine) -> Iterator[tuple[str, FastAPI]]:
    # 仅缩小隔离测试池以确定性触及限额，不向共享开发实例施压。
    business = create_engine(engine.url, pool_size=2, max_overflow=0, pool_timeout=0.2)
    configuration = settings().model_copy(
        update={
            "monitor_max_subscriptions": 2,
            "deployment_mode": "fixed_main",
            "session_cookie_transport": "allow_http",
        }
    )
    app = create_app(configuration, session_factory=session_factory(business))
    user = an_account(engine)
    with session_factory(engine)() as session:
        roles = PostgresRoleRepository(session)
        role = Role(
            id=new_id(),
            code="s150-viewer",
            name="SSE 资源测试订阅者",
            permissions=frozenset({Permission.MONITOR_VIEW}),
        )
        roles.add(role)
        roles.assign(user_id=user.id, role_ids=(role.id,))
        PostgresMonitorRepository(session).upsert_health(_health("s150-initial"))
        session.commit()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="on"))
        worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        worker.start()
        deadline = time.monotonic() + 10
        listener_pid: int | None = None
        try:
            while not server.started:
                assert worker.is_alive()
                assert time.monotonic() < deadline
                time.sleep(0.01)
            with app.state.monitor_listener_engine.connect() as connection:
                listener_pid = connection.scalar(text("SELECT pg_backend_pid()"))
            yield f"http://127.0.0.1:{port}", app
        finally:
            server.should_exit = True
            worker.join(timeout=20)
            assert not worker.is_alive()
            business.dispose()
            _clear_s143_monitor_rows(engine)
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM monitor_health_identity WHERE event_id LIKE 's150-%'")
                )
                connection.execute(text("TRUNCATE auth_user CASCADE"))
                connection.execute(text("DELETE FROM auth_role WHERE code = 's150-viewer'"))
            if listener_pid is not None:
                with engine.connect() as connection:
                    assert (
                        connection.scalar(
                            text("SELECT count(*) FROM pg_stat_activity WHERE pid = :pid"),
                            {"pid": listener_pid},
                        )
                        == 0
                    )


def _wait_for_listeners(engine: Engine, expected: int) -> None:
    deadline = time.monotonic() + 17
    while True:
        with engine.connect() as connection:
            count = connection.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND query LIKE 'LISTEN nvsop_monitor_stream%'"
                )
            )
        if count == expected:
            return
        assert time.monotonic() < deadline, f"LISTEN 数量未恢复：{count} != {expected}"
        time.sleep(0.02)


def test_subscription_limit_preserves_queries_reports_and_recovers_after_disconnect(
    engine: Engine,
    constrained_monitor: tuple[str, FastAPI],
    runtime_topology: RuntimeTopology,
) -> None:
    url, _ = constrained_monitor
    with httpx2.Client(base_url=url, timeout=20) as client, ExitStack() as streams:
        assert client.post(SESSION_PATH, json=CREDENTIALS).status_code == 201
        first = streams.enter_context(client.stream("GET", "/api/v1/monitor/stream"))
        second = streams.enter_context(client.stream("GET", "/api/v1/monitor/stream"))
        assert first.status_code == 200
        assert second.status_code == 200
        _wait_for_listeners(engine, 2)
        # 订阅预算耗尽不能夺走普通请求的业务连接。
        assert client.get("/api/v1/monitor/violations").status_code == 200
        assert client.get("/api/v1/monitor/stream?once=true").status_code == 200
        started = time.monotonic()
        assert client.get("/api/v1/monitor/stream").status_code == 503
        assert time.monotonic() - started < 1
        body = reported_health_to_wire(
            replace(
                _health("s150-host-report").report,
                host_id=str(runtime_topology.host.id),
                station_id=str(runtime_topology.station.id),
            )
        )
        path = "/api/v1/monitor/health"
        # 主机签名上报独立于浏览器 Session/CSRF 认证。
        with httpx2.Client(base_url=url, timeout=20) as host_client:
            response = host_client.post(
                path,
                json=body,
                headers=_host_headers(
                    runtime_topology,
                    method="POST",
                    path=path,
                    body=body,
                ),
            )
        assert response.status_code == 200
        first.close()
        _wait_for_listeners(engine, 1)
        with client.stream("GET", "/api/v1/monitor/stream") as recovered:
            assert recovered.status_code == 200
            assert next(recovered.iter_lines()).startswith("id: ")
        second.close()
    _wait_for_listeners(engine, 0)


def test_stream_failure_and_application_shutdown_release_listener_connections(
    engine: Engine,
    constrained_monitor: tuple[str, FastAPI],
) -> None:
    url, app = constrained_monitor
    with (
        httpx2.Client(base_url=url, timeout=20) as client,
        patch.object(
            PostgresMonitorStreamSource,
            "read_after_sequences",
            side_effect=RuntimeError("synthetic stream failure"),
        ) as failed_read,
    ):
        assert client.post(SESSION_PATH, json=CREDENTIALS).status_code == 201
        with client.stream("GET", "/api/v1/monitor/stream") as response:
            assert response.status_code == 200
            # HTTP 中间件可表现为 EOF 或连接中断；都必须释放真实 listener。
            with suppress(httpx2.RemoteProtocolError):
                list(response.iter_lines())
        failed_read.assert_called_once()
    _wait_for_listeners(engine, 0)
    with httpx2.Client(base_url=url, timeout=20) as client:
        assert client.post(SESSION_PATH, json=CREDENTIALS).status_code == 201
        with client.stream("GET", "/api/v1/monitor/stream") as response:
            assert response.status_code == 200
            assert next(response.iter_lines()).startswith("id: ")
    _wait_for_listeners(engine, 0)
    # lifespan 的 dispose 另由 fixture 退出后验证；这里确认配额已回收。
    assert app.state.monitor_listener_engine.pool.checkedout() == 0
