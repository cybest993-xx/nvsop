"""#199 再切片请求：真实 ASGI + PostgreSQL 的截止、权限、幂等与不可领取边界。

`TestClient` 驱动实际 FastAPI 应用、每请求事务与容器里的真实 PostgreSQL；会话与权限来自真实
登录与角色行，证据登记与主机签名复用 `test_evidence_access` / `test_runtime_observation_http`
的既有 helper，不另造同义造型。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from test_auth_http import CREDENTIALS, SESSION_PATH, an_account, settings
from test_evidence_access import _post_as_host
from test_runtime_observation_http import RuntimeTopology, _host_headers
from test_runtime_observation_http import runtime_topology as runtime_topology

from factory_sop.app import API_PREFIX, create_app
from factory_sop.auth.adapters.cookies import CSRF_COOKIE, CSRF_HEADER
from factory_sop.auth.adapters.repository import PostgresRoleRepository
from factory_sop.auth.model import Role, User
from factory_sop.auth.permissions import Permission
from factory_sop.identifiers import new_id
from factory_sop.persistence import session_factory

ROLE_CODE = "evidence_reclip_requester"
REGISTRATIONS = f"{API_PREFIX}/evidence/registrations"
WINDOW_SECONDS = 604800


def _grant(engine: Engine) -> User:
    """真实账户 + 真实角色行同时授予查看与复核编辑；发起与查询都按真实权限判定。"""
    user = an_account(engine)
    with session_factory(engine)() as session:
        roles = PostgresRoleRepository(session)
        role = Role(
            new_id(),
            ROLE_CODE,
            "再切片发起人",
            frozenset({Permission.EVIDENCE_VIEW, Permission.EVIDENCE_REVIEW_EDIT}),
        )
        roles.add(role)
        roles.assign(user_id=user.id, role_ids=(role.id,))
        session.commit()
    return user


def _registration(
    topology: RuntimeTopology,
    evidence_id: str,
    *,
    wall_offset: float,
    required_from: float,
    required_to: float,
) -> dict[str, object]:
    """来源机登记可用证据并附带冻结的录像墙钟映射与核心必需跨度。"""
    return {
        "evidence_id": evidence_id,
        "host_id": str(topology.host.id).upper(),
        "station_id": str(topology.station.id),
        "instance_id": 7,
        "violation_id": None,
        "kind": "clip",
        "origin": "automatic",
        "anchor": 100.0,
        "window_start": 95.0,
        "window_end": 105.0,
        "generation": "original",
        "sha256": "a" * 64,
        "size": 10,
        "reference": "artifact/camera-a.mp4",
        "wall_offset": wall_offset,
        "required_from": required_from,
        "required_to": required_to,
    }


def test_reclip_request_is_authorized_idempotent_and_bound_to_the_source_host(
    engine: Engine, runtime_topology: RuntimeTopology
) -> None:
    user = _grant(engine)
    host_id = str(runtime_topology.host.id)
    station_id = str(runtime_topology.station.id)
    evidence_id = f"{host_id}/再切片.mp4"
    app = create_app(settings(), session_factory=session_factory(engine))
    base = f"{API_PREFIX}/evidence/{evidence_id}/reclip-requests"
    request_id = uuid4()
    wall_offset = datetime.now(UTC).timestamp() - 100.0
    body = {
        "request_id": str(request_id),
        "generation": "original",
        "before_seconds": 2.0,
        "after_seconds": 3.0,
    }
    try:
        with (
            TestClient(app, base_url="https://testserver") as browser,
            TestClient(app, base_url="https://testserver") as host,
        ):
            # 匿名：请求与查询都必须被拒。
            assert browser.post(base, json=body).status_code == 401
            assert browser.get(base).status_code == 401

            registered = _post_as_host(
                host,
                runtime_topology,
                path=REGISTRATIONS,
                body=_registration(
                    runtime_topology,
                    evidence_id,
                    wall_offset=wall_offset,
                    required_from=90.0,
                    required_to=110.0,
                ),
            )
            assert registered.status_code == 200, registered.text
            assert browser.post(SESSION_PATH, json=CREDENTIALS).status_code == 201
            csrf = {CSRF_HEADER: browser.cookies[CSRF_COOKIE]}

            # 列表按来源机冻结映射给出绝对截止时刻（核心必需跨度最早点 + 滚动窗口）。
            listing = browser.get(
                f"{API_PREFIX}/evidence",
                params={"station_id": station_id, "instance_id": 7},
            ).json()
            row = next(item for item in listing if item["evidence_id"] == evidence_id)
            assert row["reclip_available"] is True
            assert row["reclip_deadline"] == 90.0 + wall_offset + WINDOW_SECONDS

            # 创建：窗口是核心必需跨度与请求余量的并集，锚点/来源主机由证据事实派生。
            created = browser.post(base, json=body, headers=csrf)
            assert created.status_code == 200, created.text
            record = created.json()
            assert record["evidence_id"] == evidence_id
            assert record["generation"] == "original"
            assert record["host_id"] == host_id
            assert record["anchor"] == 100.0
            assert record["before_seconds"] == 2.0
            assert record["after_seconds"] == 3.0
            assert record["window_start"] == 90.0
            assert record["window_end"] == 110.0
            assert record["requested_by"] == str(user.id)
            assert record["status"] == "pending"

            # 重复同一稳定请求 ID 与内容返回同一记录；改余量冲突且不覆盖。
            replay = browser.post(base, json=body, headers=csrf)
            assert replay.status_code == 200
            assert replay.json() == record
            conflict = browser.post(base, json=body | {"after_seconds": 30.0}, headers=csrf)
            assert (conflict.status_code, conflict.json()["error_code"]) == (
                409,
                "IDEMPOTENCY_CONFLICT",
            )

            # 查询列出该证据的请求，等待状态可查。
            listed = browser.get(base).json()
            assert [item["request_id"] for item in listed] == [str(request_id)]
            assert listed[0]["status"] == "pending"

            # 委托命令绑定来源主机、稳定幂等键，但当前不可领取：主机领取连接测试命令得到空。
            with engine.begin() as connection:
                command = connection.execute(
                    text(
                        "SELECT host_id, command_type, idempotency_key, target_id, status "
                        "FROM device_pending_command WHERE target_id = :request_id"
                    ),
                    {"request_id": request_id},
                ).one()
            assert command.host_id == runtime_topology.host.id
            assert command.command_type == "reclip_evidence"
            assert command.idempotency_key == f"evidence-reclip:{request_id}"
            assert command.status == "pending"
            claim = host.get(
                f"{API_PREFIX}/device-commands/next",
                headers=_host_headers(
                    runtime_topology, method="GET", path=f"{API_PREFIX}/device-commands/next"
                ),
            )
            assert claim.status_code == 204

            # 改目标资源 ID 读取：另一证据路径下查不到该请求，也不泄漏存在性。
            other = browser.get(f"{API_PREFIX}/evidence/{host_id}/missing.mp4/reclip-requests")
            assert other.status_code == 200
            assert other.json() == []

            # 撤权：删除真实角色分配后同一请求按权限拒绝。
            with session_factory(engine)() as session:
                PostgresRoleRepository(session).assign(user_id=user.id, role_ids=())
                session.commit()
            denied = browser.post(base, json=body | {"request_id": str(uuid4())}, headers=csrf)
            assert denied.status_code == 403
            assert denied.json()["error_code"] == "PERMISSION_DENIED"
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM evidence_evidence WHERE evidence_id = :id"), {"id": evidence_id}
            )
            connection.execute(
                text("DELETE FROM device_pending_command WHERE host_id = :host_id"),
                {"host_id": runtime_topology.host.id},
            )
            connection.execute(text("TRUNCATE auth_user CASCADE"))
            connection.execute(
                text("DELETE FROM auth_role WHERE code = :code"), {"code": ROLE_CODE}
            )


def test_reclip_rejects_non_finite_margins_before_writing(
    engine: Engine, runtime_topology: RuntimeTopology
) -> None:
    """NaN/Infinity 余量必须在任何写入前以 422 拒绝, 不得先入队再在领域校验里 500。"""
    _grant(engine)
    host_id = str(runtime_topology.host.id)
    evidence_id = f"{host_id}/nan.mp4"
    app = create_app(settings(), session_factory=session_factory(engine))
    request_id = uuid4()
    try:
        with (
            TestClient(app, base_url="https://testserver") as browser,
            TestClient(app, base_url="https://testserver") as host,
        ):
            registered = _post_as_host(
                host,
                runtime_topology,
                path=REGISTRATIONS,
                body=_registration(
                    runtime_topology,
                    evidence_id,
                    wall_offset=datetime.now(UTC).timestamp() - 100.0,
                    required_from=90.0,
                    required_to=110.0,
                ),
            )
            assert registered.status_code == 200, registered.text
            assert browser.post(SESSION_PATH, json=CREDENTIALS).status_code == 201
            csrf = {CSRF_HEADER: browser.cookies[CSRF_COOKIE]}
            raw = (
                b'{"request_id": "' + str(request_id).encode() + b'", '
                b'"generation": "original", "before_seconds": NaN, "after_seconds": 1.0}'
            )
            response = browser.post(
                f"{API_PREFIX}/evidence/{evidence_id}/reclip-requests",
                content=raw,
                headers={**csrf, "content-type": "application/json"},
            )
            assert response.status_code == 422, response.text
            with engine.begin() as connection:
                requests = connection.execute(
                    text("SELECT count(*) FROM evidence_reclip_request")
                ).scalar_one()
                commands = connection.execute(
                    text(
                        "SELECT count(*) FROM device_pending_command "
                        "WHERE host_id = :host_id AND command_type = 'reclip_evidence'"
                    ),
                    {"host_id": runtime_topology.host.id},
                ).scalar_one()
            assert (requests, commands) == (0, 0)
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM evidence_evidence WHERE evidence_id = :id"), {"id": evidence_id}
            )
            connection.execute(
                text("DELETE FROM device_pending_command WHERE host_id = :host_id"),
                {"host_id": runtime_topology.host.id},
            )
            connection.execute(text("TRUNCATE auth_user CASCADE"))
            connection.execute(
                text("DELETE FROM auth_role WHERE code = :code"), {"code": ROLE_CODE}
            )


def test_reclip_rejects_expired_and_unknown_metadata(
    engine: Engine, runtime_topology: RuntimeTopology
) -> None:
    _grant(engine)
    host_id = str(runtime_topology.host.id)
    expired_id, legacy_id = f"{host_id}/expired.mp4", f"{host_id}/legacy.mp4"
    app = create_app(settings(), session_factory=session_factory(engine))
    body = {
        "request_id": str(uuid4()),
        "generation": "original",
        "before_seconds": 1.0,
        "after_seconds": 1.0,
    }
    try:
        with (
            TestClient(app, base_url="https://testserver") as browser,
            TestClient(app, base_url="https://testserver") as host,
        ):
            expired = _post_as_host(
                host,
                runtime_topology,
                path=REGISTRATIONS,
                body=_registration(
                    runtime_topology,
                    expired_id,
                    wall_offset=datetime.now(UTC).timestamp() - 2 * WINDOW_SECONDS,
                    required_from=90.0,
                    required_to=110.0,
                ),
            )
            assert expired.status_code == 200, expired.text
            legacy = _post_as_host(
                host,
                runtime_topology,
                path=REGISTRATIONS,
                body={
                    key: value
                    for key, value in _registration(
                        runtime_topology,
                        legacy_id,
                        wall_offset=0.0,
                        required_from=90.0,
                        required_to=110.0,
                    ).items()
                    if key not in {"wall_offset", "required_from", "required_to"}
                },
            )
            assert legacy.status_code == 200, legacy.text
            assert browser.post(SESSION_PATH, json=CREDENTIALS).status_code == 201
            csrf = {CSRF_HEADER: browser.cookies[CSRF_COOKIE]}

            listing = browser.get(
                f"{API_PREFIX}/evidence",
                params={"station_id": str(runtime_topology.station.id), "instance_id": 7},
            ).json()
            by_id = {item["evidence_id"]: item for item in listing}
            assert by_id[expired_id]["reclip_available"] is False
            assert by_id[expired_id]["reclip_deadline"] is not None
            assert by_id[legacy_id]["reclip_available"] is False
            assert by_id[legacy_id]["reclip_deadline"] is None

            expired_request = browser.post(
                f"{API_PREFIX}/evidence/{expired_id}/reclip-requests", json=body, headers=csrf
            )
            assert (expired_request.status_code, expired_request.json()["error_code"]) == (
                410,
                "STATE_CONFLICT",
            )
            legacy_request = browser.post(
                f"{API_PREFIX}/evidence/{legacy_id}/reclip-requests", json=body, headers=csrf
            )
            assert (legacy_request.status_code, legacy_request.json()["error_code"]) == (
                409,
                "RESOURCE_MISMATCH",
            )
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM evidence_evidence WHERE evidence_id IN (:a, :b)"),
                {"a": expired_id, "b": legacy_id},
            )
            connection.execute(
                text("DELETE FROM device_pending_command WHERE host_id = :host_id"),
                {"host_id": runtime_topology.host.id},
            )
            connection.execute(text("TRUNCATE auth_user CASCADE"))
            connection.execute(
                text("DELETE FROM auth_role WHERE code = :code"), {"code": ROLE_CODE}
            )
