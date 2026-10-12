"""S038 再切片执行：真实 ASGI + PostgreSQL 的签名领取、成功追加与授权复核。

`TestClient` 驱动实际 FastAPI 应用与每请求事务；会话、权限、主机签名与证据登记复用
`test_evidence_reclip` / `test_evidence_access` / `test_runtime_observation_http` 的既有
helper。再切片领取端点由生产 `create_app` 挂载，这里直接驱动该应用验证完整链路。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from test_auth_http import CREDENTIALS, SESSION_PATH, settings
from test_evidence_access import _post_as_host
from test_evidence_reclip import ROLE_CODE, _grant, _registration
from test_runtime_observation_http import RuntimeTopology, _host_headers
from test_runtime_observation_http import runtime_topology as runtime_topology

from factory_sop.app import API_PREFIX, create_app
from factory_sop.auth.adapters.cookies import CSRF_COOKIE, CSRF_HEADER
from factory_sop.persistence import session_factory
from nvsop_contracts import reclip_evidence_claim_from_wire

RECLIP_CLAIM_PATH = f"{API_PREFIX}/device-commands/reclip/next"
REGISTRATIONS = f"{API_PREFIX}/evidence/registrations"
WINDOW_SECONDS = 604800


def _reclip_app(engine: Engine) -> FastAPI:
    """生产 ``create_app`` 已挂载再切片端点；此处按同一接缝构造应用。"""
    return create_app(settings(), session_factory=session_factory(engine))


def _reclip_registration(
    topology: RuntimeTopology,
    evidence_id: str,
    *,
    origin: str = "reclip",
    instance_id: int = 7,
    anchor: float = 100.0,
    window_start: float = 90.0,
    window_end: float = 110.0,
) -> dict[str, object]:
    return {
        "evidence_id": evidence_id,
        "host_id": str(topology.host.id).upper(),
        "station_id": str(topology.station.id),
        "instance_id": instance_id,
        "violation_id": None,
        "kind": "clip",
        "origin": origin,
        "anchor": anchor,
        "window_start": window_start,
        "window_end": window_end,
        "generation": "original",
        "sha256": "b" * 64,
        "size": 20,
        "reference": "artifact/reclip-camera-a.mp4",
        "wall_offset": 90.0,
        "required_from": 100.0,
        "required_to": 100.0,
    }


def test_signed_reclip_claim_appends_reference_and_settles_success(
    engine: Engine, runtime_topology: RuntimeTopology
) -> None:
    user = _grant(engine)
    host_id = str(runtime_topology.host.id)
    station_id = str(runtime_topology.station.id)
    original_id = f"{host_id}/reclip-source.mp4"
    app = _reclip_app(engine)
    base = f"{API_PREFIX}/evidence/{original_id}/reclip-requests"
    wall_offset = datetime.now(UTC).timestamp() - 100.0
    request_id = uuid4()
    body = {
        "request_id": str(request_id),
        "generation": "original",
        "before_seconds": 2.0,
        "after_seconds": 3.0,
    }
    new_id = f"{host_id}/reclip-result.mp4"
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
                    original_id,
                    wall_offset=wall_offset,
                    required_from=90.0,
                    required_to=110.0,
                ),
            )
            assert registered.status_code == 200, registered.text
            assert browser.post(SESSION_PATH, json=CREDENTIALS).status_code == 201
            csrf = {CSRF_HEADER: browser.cookies[CSRF_COOKIE]}
            created = browser.post(base, json=body, headers=csrf)
            assert created.status_code == 200, created.text
            assert created.json()["status"] == "pending"

            # 生产默认领取入口只领连接测试: 待办的再切片命令不可被它取走。
            normal = host.get(
                f"{API_PREFIX}/device-commands/next",
                headers=_host_headers(
                    runtime_topology,
                    method="GET",
                    path=f"{API_PREFIX}/device-commands/next",
                ),
            )
            assert normal.status_code == 204

            # 主机签名领取再切片命令: 载荷由请求与原始证据事实派生, 不是客户端可覆写的。
            claim_response = host.get(
                RECLIP_CLAIM_PATH,
                headers=_host_headers(runtime_topology, method="GET", path=RECLIP_CLAIM_PATH),
            )
            assert claim_response.status_code == 200, claim_response.text
            claim = reclip_evidence_claim_from_wire(claim_response.json())
            assert claim.command.request_id == str(request_id)
            assert claim.command.station_id == station_id
            assert claim.command.instance_id == 7
            assert claim.command.anchor == 100.0
            assert (claim.command.window_start, claim.command.window_end) == (90.0, 110.0)
            # 权威截止由中心按原登记冻结墙钟与滚动窗口算出, 随命令下发。
            assert claim.command.available_until == 90.0 + wall_offset + WINDOW_SECONDS

            # 伪造的 reclip 登记 (无对应请求) 被拒, 不追加无来源关联的代次。
            forged = _post_as_host(
                host,
                runtime_topology,
                path=REGISTRATIONS,
                body=_reclip_registration(runtime_topology, f"{host_id}/forged.mp4")
                | {"anchor": 100.0, "window_start": 70.0, "window_end": 130.0},
            )
            assert forged.status_code == 409, forged.text

            # 真实新引用登记: 与请求的工位/实例/锚点/窗口精确对应, 被接受。
            new_reference = _post_as_host(
                host,
                runtime_topology,
                path=REGISTRATIONS,
                body=_reclip_registration(runtime_topology, new_id),
            )
            assert new_reference.status_code == 200, new_reference.text
            assert new_reference.json()["status"] == "available"

            # 成功回报: 主机签名 + 领取令牌, 请求转 succeeded 并记录新引用身份。
            result_path = f"{API_PREFIX}/device-commands/{claim.command.command_id}/reclip-result"
            result_body: dict[str, object] = {"evidence_id": new_id}
            reported = host.post(
                result_path,
                json=result_body,
                headers=_host_headers(
                    runtime_topology, method="POST", path=result_path, body=result_body
                )
                | {"X-Command-Claim-Token": claim.claim_token},
            )
            assert reported.status_code == 200, reported.text
            assert reported.json() == {
                "accepted": True,
                "request_id": str(request_id),
                "status": "succeeded",
                "evidence_id": new_id,
            }
            with engine.begin() as connection:
                row = connection.execute(
                    text(
                        "SELECT status, result_evidence_id, requested_by "
                        "FROM evidence_reclip_request WHERE request_id = :id"
                    ),
                    {"id": request_id},
                ).one()
            assert row.status == "succeeded"
            assert row.result_evidence_id == new_id
            assert row.requested_by == user.id

            # 授权读取: 新引用出现在列表, 并可按 S035 复核准确引用。
            listing = browser.get(
                f"{API_PREFIX}/evidence",
                params={"station_id": station_id, "instance_id": 7},
            ).json()
            appended = next(item for item in listing if item["evidence_id"] == new_id)
            assert appended["origin"] == "reclip"
            assert appended["status"] == "available"
            review_id = uuid4()
            review = browser.post(
                f"{API_PREFIX}/evidence/{new_id}/reviews",
                json={
                    "review_id": str(review_id),
                    "generation": "original",
                    "conclusion": "confirmed",
                },
                headers=csrf,
            )
            assert review.status_code == 200, review.text
            assert review.json()["evidence_id"] == new_id
            listed_reviews = browser.get(f"{API_PREFIX}/evidence/{new_id}/reviews").json()
            assert [item["review_id"] for item in listed_reviews] == [str(review_id)]
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM evidence_evidence WHERE evidence_id IN (:a, :b)"),
                {"a": original_id, "b": new_id},
            )
            connection.execute(
                text("DELETE FROM device_pending_command WHERE host_id = :host_id"),
                {"host_id": runtime_topology.host.id},
            )
            connection.execute(text("TRUNCATE auth_user CASCADE"))
            connection.execute(
                text("DELETE FROM auth_role WHERE code = :code"), {"code": ROLE_CODE}
            )


def test_reclip_settlement_requires_the_matching_request_scope(
    engine: Engine, runtime_topology: RuntimeTopology
) -> None:
    """成功回报必须匹配本请求的工位/实例/锚点/并集窗口; 另一请求的合法结果不能结案本请求。"""
    _grant(engine)
    host_id = str(runtime_topology.host.id)
    source_a = f"{host_id}/reclip-source-a.mp4"
    source_b = f"{host_id}/reclip-source-b.mp4"
    result_a = f"{host_id}/reclip-result-a.mp4"
    result_b = f"{host_id}/reclip-result-b.mp4"
    request_a, request_b = uuid4(), uuid4()
    wall_offset = datetime.now(UTC).timestamp() - 100.0
    # A 与 B 同主机同锚点: A=实例 7 并集窗口 (90,110), B=实例 8 并集窗口 (80,120)。
    cases = (
        (source_a, request_a, 7, 90.0, 110.0),
        (source_b, request_b, 8, 80.0, 120.0),
    )
    app = _reclip_app(engine)
    body = {"generation": "original", "before_seconds": 2.0, "after_seconds": 3.0}
    try:
        with (
            TestClient(app, base_url="https://testserver") as browser,
            TestClient(app, base_url="https://testserver") as host,
        ):
            for source, _request_id, instance_id, required_from, required_to in cases:
                registered = _post_as_host(
                    host,
                    runtime_topology,
                    path=REGISTRATIONS,
                    body=_registration(
                        runtime_topology,
                        source,
                        wall_offset=wall_offset,
                        required_from=required_from,
                        required_to=required_to,
                    )
                    | {"instance_id": instance_id},
                )
                assert registered.status_code == 200, registered.text
            assert browser.post(SESSION_PATH, json=CREDENTIALS).status_code == 201
            csrf = {CSRF_HEADER: browser.cookies[CSRF_COOKIE]}
            for source, request_id, _instance_id, _required_from, _required_to in cases:
                created = browser.post(
                    f"{API_PREFIX}/evidence/{source}/reclip-requests",
                    json=body | {"request_id": str(request_id)},
                    headers=csrf,
                )
                assert created.status_code == 200, created.text
                assert created.json()["status"] == "pending"

            claim_a = reclip_evidence_claim_from_wire(
                host.get(
                    RECLIP_CLAIM_PATH,
                    headers=_host_headers(runtime_topology, method="GET", path=RECLIP_CLAIM_PATH),
                ).json()
            )
            claim_b = reclip_evidence_claim_from_wire(
                host.get(
                    RECLIP_CLAIM_PATH,
                    headers=_host_headers(runtime_topology, method="GET", path=RECLIP_CLAIM_PATH),
                ).json()
            )
            assert claim_a.command.request_id == str(request_a)
            assert claim_b.command.request_id == str(request_b)

            # B 的合法新引用已登记, 但它不属于 A。
            registered_b = _post_as_host(
                host,
                runtime_topology,
                path=REGISTRATIONS,
                body=_reclip_registration(
                    runtime_topology, result_b, instance_id=8, window_start=80.0, window_end=120.0
                ),
            )
            assert registered_b.status_code == 200, registered_b.text

            path_a = f"{API_PREFIX}/device-commands/{claim_a.command.command_id}/reclip-result"
            rejected = host.post(
                path_a,
                json={"evidence_id": result_b},
                headers=_host_headers(
                    runtime_topology, method="POST", path=path_a, body={"evidence_id": result_b}
                )
                | {"X-Command-Claim-Token": claim_a.claim_token},
            )
            assert rejected.status_code == 409, rejected.text
            # 命令结案与请求结案在同一请求事务; 拒绝必须回滚, 不留半结案成功事实。
            with engine.begin() as connection:
                command_status = connection.execute(
                    text("SELECT status FROM device_pending_command WHERE id = :id"),
                    {"id": claim_a.command.command_id},
                ).scalar_one()
                request_status = connection.execute(
                    text("SELECT status FROM evidence_reclip_request WHERE request_id = :id"),
                    {"id": request_a},
                ).scalar_one()
                untouched = connection.execute(
                    text(
                        "SELECT count(*) FROM evidence_evidence "
                        "WHERE evidence_id IN (:a, :b, :c) AND status = 'available'"
                    ),
                    {"a": source_a, "b": source_b, "c": result_b},
                ).scalar_one()
            assert command_status == "claimed"
            assert request_status == "pending"
            assert untouched == 3

            # 与 A 精确匹配的结果被接受; 同一结果重放幂等, 换结果冲突。
            registered_a = _post_as_host(
                host,
                runtime_topology,
                path=REGISTRATIONS,
                body=_reclip_registration(
                    runtime_topology, result_a, instance_id=7, window_start=90.0, window_end=110.0
                ),
            )
            assert registered_a.status_code == 200, registered_a.text
            settle_body: dict[str, object] = {"evidence_id": result_a}
            settled = host.post(
                path_a,
                json=settle_body,
                headers=_host_headers(
                    runtime_topology, method="POST", path=path_a, body=settle_body
                )
                | {"X-Command-Claim-Token": claim_a.claim_token},
            )
            assert settled.status_code == 200, settled.text
            assert settled.json()["status"] == "succeeded"
            assert settled.json()["evidence_id"] == result_a
            replay = host.post(
                path_a,
                json=settle_body,
                headers=_host_headers(
                    runtime_topology, method="POST", path=path_a, body=settle_body
                )
                | {"X-Command-Claim-Token": claim_a.claim_token},
            )
            assert replay.status_code == 200, replay.text
            assert replay.json() == settled.json()
            conflict = host.post(
                path_a,
                json={"evidence_id": result_b},
                headers=_host_headers(
                    runtime_topology, method="POST", path=path_a, body={"evidence_id": result_b}
                )
                | {"X-Command-Claim-Token": claim_a.claim_token},
            )
            assert conflict.status_code == 409, conflict.text
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM evidence_evidence WHERE evidence_id IN (:a, :b, :c, :d)"),
                {"a": source_a, "b": source_b, "c": result_a, "d": result_b},
            )
            connection.execute(
                text("DELETE FROM device_pending_command WHERE host_id = :host_id"),
                {"host_id": runtime_topology.host.id},
            )
            connection.execute(text("TRUNCATE auth_user CASCADE"))
            connection.execute(
                text("DELETE FROM auth_role WHERE code = :code"), {"code": ROLE_CODE}
            )


def test_claim_settles_a_command_whose_request_vanished(
    engine: Engine, runtime_topology: RuntimeTopology
) -> None:
    """请求在领取前消失时, 命令必须终局结案, 不静默 204 留下永久领取循环。"""
    _grant(engine)
    host_id = str(runtime_topology.host.id)
    original_id = f"{host_id}/reclip-vanished.mp4"
    app = _reclip_app(engine)
    base = f"{API_PREFIX}/evidence/{original_id}/reclip-requests"
    wall_offset = datetime.now(UTC).timestamp() - 100.0
    request_id = uuid4()
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
            assert (
                _post_as_host(
                    host,
                    runtime_topology,
                    path=REGISTRATIONS,
                    body=_registration(
                        runtime_topology,
                        original_id,
                        wall_offset=wall_offset,
                        required_from=90.0,
                        required_to=110.0,
                    ),
                ).status_code
                == 200
            )
            assert browser.post(SESSION_PATH, json=CREDENTIALS).status_code == 201
            csrf = {CSRF_HEADER: browser.cookies[CSRF_COOKIE]}
            assert browser.post(base, json=body, headers=csrf).status_code == 200
            # 模拟原证据被删除 (cascade 掉请求) 而命令仍在队列。
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM evidence_reclip_request WHERE request_id = :id"),
                    {"id": request_id},
                )
            claimed = host.get(
                RECLIP_CLAIM_PATH,
                headers=_host_headers(runtime_topology, method="GET", path=RECLIP_CLAIM_PATH),
            )
            assert claimed.status_code == 204, claimed.text
            with engine.begin() as connection:
                row = connection.execute(
                    text(
                        "SELECT status, failure_code FROM device_pending_command "
                        "WHERE target_id = :id"
                    ),
                    {"id": request_id},
                ).one()
            assert row.status == "failed"
            assert row.failure_code == "reclip_source_missing"
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM evidence_evidence WHERE evidence_id = :a"),
                {"a": original_id},
            )
            connection.execute(
                text("DELETE FROM device_pending_command WHERE host_id = :host_id"),
                {"host_id": runtime_topology.host.id},
            )
            connection.execute(text("TRUNCATE auth_user CASCADE"))
            connection.execute(
                text("DELETE FROM auth_role WHERE code = :code"), {"code": ROLE_CODE}
            )


def test_production_app_mounts_reclip_endpoints(engine: Engine) -> None:
    """生产应用挂载再切片签名端点, 使 S039 失败/成功结案都可达。"""
    app = create_app(settings(), session_factory=session_factory(engine))
    paths = set(app.openapi()["paths"])
    assert RECLIP_CLAIM_PATH in paths
    assert f"{API_PREFIX}/device-commands/{{command_id}}/reclip-result" in paths


def test_signed_reclip_failure_settles_a_queryable_terminal_reason(
    engine: Engine, runtime_topology: RuntimeTopology
) -> None:
    """素材已过期/缺失等终局失败: 命令与请求同事务结案, 原因可查询且不动原证据。"""
    _grant(engine)
    host_id = str(runtime_topology.host.id)
    station_id = str(runtime_topology.station.id)
    original_id = f"{host_id}/reclip-failure-source.mp4"
    app = _reclip_app(engine)
    base = f"{API_PREFIX}/evidence/{original_id}/reclip-requests"
    wall_offset = datetime.now(UTC).timestamp() - 100.0
    request_id = uuid4()
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
            registered = _post_as_host(
                host,
                runtime_topology,
                path=REGISTRATIONS,
                body=_registration(
                    runtime_topology,
                    original_id,
                    wall_offset=wall_offset,
                    required_from=90.0,
                    required_to=110.0,
                ),
            )
            assert registered.status_code == 200, registered.text
            assert browser.post(SESSION_PATH, json=CREDENTIALS).status_code == 201
            csrf = {CSRF_HEADER: browser.cookies[CSRF_COOKIE]}
            created = browser.post(base, json=body, headers=csrf)
            assert created.status_code == 200, created.text
            claim = reclip_evidence_claim_from_wire(
                host.get(
                    RECLIP_CLAIM_PATH,
                    headers=_host_headers(runtime_topology, method="GET", path=RECLIP_CLAIM_PATH),
                ).json()
            )
            result_path = f"{API_PREFIX}/device-commands/{claim.command.command_id}/reclip-result"
            failure_body: dict[str, object] = {
                "evidence_id": None,
                "failure_code": "material_missing",
                "detail": "no recording segment overlaps the window",
            }
            reported = host.post(
                result_path,
                json=failure_body,
                headers=_host_headers(
                    runtime_topology, method="POST", path=result_path, body=failure_body
                )
                | {"X-Command-Claim-Token": claim.claim_token},
            )
            assert reported.status_code == 200, reported.text
            assert reported.json() == {
                "accepted": True,
                "request_id": str(request_id),
                "status": "failed",
                "failure_reason": "material_missing",
            }
            with engine.begin() as connection:
                request_row = connection.execute(
                    text(
                        "SELECT status, failure_reason, result_evidence_id "
                        "FROM evidence_reclip_request WHERE request_id = :id"
                    ),
                    {"id": request_id},
                ).one()
                command_row = connection.execute(
                    text(
                        "SELECT status, failure_code, result_detail "
                        "FROM device_pending_command WHERE id = :id"
                    ),
                    {"id": claim.command.command_id},
                ).one()
            assert request_row.status == "failed"
            assert request_row.failure_reason == "material_missing"
            assert request_row.result_evidence_id is None
            assert command_row.status == "failed"
            assert command_row.failure_code == "material_missing"
            assert command_row.result_detail == "no recording segment overlaps the window"
            # 失败不覆盖原证据: 原引用仍可回看, 且失败原因按查看权限可查。
            listing = browser.get(
                f"{API_PREFIX}/evidence",
                params={"station_id": station_id, "instance_id": 7},
            ).json()
            original = next(item for item in listing if item["evidence_id"] == original_id)
            assert original["status"] == "available"
            queried = browser.get(base).json()
            assert queried[0]["status"] == "failed"
            assert queried[0]["failure_reason"] == "material_missing"
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM evidence_evidence WHERE evidence_id = :a"),
                {"a": original_id},
            )
            connection.execute(
                text("DELETE FROM device_pending_command WHERE host_id = :host_id"),
                {"host_id": runtime_topology.host.id},
            )
            connection.execute(text("TRUNCATE auth_user CASCADE"))
            connection.execute(
                text("DELETE FROM auth_role WHERE code = :code"), {"code": ROLE_CODE}
            )


def test_reclip_failure_cannot_overwrite_a_successful_generation(
    engine: Engine, runtime_topology: RuntimeTopology
) -> None:
    """已成功结案的请求不得被迟到的失败回报改写 (AC2)。"""
    _grant(engine)
    host_id = str(runtime_topology.host.id)
    original_id = f"{host_id}/reclip-keep-source.mp4"
    new_id = f"{host_id}/reclip-keep-result.mp4"
    app = _reclip_app(engine)
    base = f"{API_PREFIX}/evidence/{original_id}/reclip-requests"
    wall_offset = datetime.now(UTC).timestamp() - 100.0
    request_id = uuid4()
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
            assert (
                _post_as_host(
                    host,
                    runtime_topology,
                    path=REGISTRATIONS,
                    body=_registration(
                        runtime_topology,
                        original_id,
                        wall_offset=wall_offset,
                        required_from=90.0,
                        required_to=110.0,
                    ),
                ).status_code
                == 200
            )
            assert browser.post(SESSION_PATH, json=CREDENTIALS).status_code == 201
            csrf = {CSRF_HEADER: browser.cookies[CSRF_COOKIE]}
            assert browser.post(base, json=body, headers=csrf).status_code == 200
            claim = reclip_evidence_claim_from_wire(
                host.get(
                    RECLIP_CLAIM_PATH,
                    headers=_host_headers(runtime_topology, method="GET", path=RECLIP_CLAIM_PATH),
                ).json()
            )
            assert (
                _post_as_host(
                    host,
                    runtime_topology,
                    path=REGISTRATIONS,
                    body=_reclip_registration(runtime_topology, new_id),
                ).status_code
                == 200
            )
            result_path = f"{API_PREFIX}/device-commands/{claim.command.command_id}/reclip-result"
            settle_body: dict[str, object] = {"evidence_id": new_id}
            settled = host.post(
                result_path,
                json=settle_body,
                headers=_host_headers(
                    runtime_topology, method="POST", path=result_path, body=settle_body
                )
                | {"X-Command-Claim-Token": claim.claim_token},
            )
            assert settled.status_code == 200, settled.text
            assert settled.json()["status"] == "succeeded"

            # 同一命令/请求的迟到失败回报必须被拒, 不覆盖成功代次。
            failure_body: dict[str, object] = {
                "evidence_id": None,
                "failure_code": "material_missing",
                "detail": None,
            }
            rejected = host.post(
                result_path,
                json=failure_body,
                headers=_host_headers(
                    runtime_topology, method="POST", path=result_path, body=failure_body
                )
                | {"X-Command-Claim-Token": claim.claim_token},
            )
            assert rejected.status_code == 409, rejected.text
            with engine.begin() as connection:
                row = connection.execute(
                    text(
                        "SELECT status, result_evidence_id, failure_reason "
                        "FROM evidence_reclip_request WHERE request_id = :id"
                    ),
                    {"id": request_id},
                ).one()
            assert row.status == "succeeded"
            assert row.result_evidence_id == new_id
            assert row.failure_reason is None
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM evidence_evidence WHERE evidence_id IN (:a, :b)"),
                {"a": original_id, "b": new_id},
            )
            connection.execute(
                text("DELETE FROM device_pending_command WHERE host_id = :host_id"),
                {"host_id": runtime_topology.host.id},
            )
            connection.execute(text("TRUNCATE auth_user CASCADE"))
            connection.execute(
                text("DELETE FROM auth_role WHERE code = :code"), {"code": ROLE_CODE}
            )


def test_repeated_reclip_failure_is_idempotent_and_success_cannot_replace_it(
    engine: Engine, runtime_topology: RuntimeTopology
) -> None:
    """同一失败原因重放幂等; 失败结案后成功回报也拒绝, 不把失败悄悄改成成功。"""
    _grant(engine)
    host_id = str(runtime_topology.host.id)
    original_id = f"{host_id}/reclip-fail-source.mp4"
    new_id = f"{host_id}/reclip-fail-result.mp4"
    app = _reclip_app(engine)
    base = f"{API_PREFIX}/evidence/{original_id}/reclip-requests"
    wall_offset = datetime.now(UTC).timestamp() - 100.0
    request_id = uuid4()
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
            assert (
                _post_as_host(
                    host,
                    runtime_topology,
                    path=REGISTRATIONS,
                    body=_registration(
                        runtime_topology,
                        original_id,
                        wall_offset=wall_offset,
                        required_from=90.0,
                        required_to=110.0,
                    ),
                ).status_code
                == 200
            )
            assert browser.post(SESSION_PATH, json=CREDENTIALS).status_code == 201
            csrf = {CSRF_HEADER: browser.cookies[CSRF_COOKIE]}
            assert browser.post(base, json=body, headers=csrf).status_code == 200
            claim = reclip_evidence_claim_from_wire(
                host.get(
                    RECLIP_CLAIM_PATH,
                    headers=_host_headers(runtime_topology, method="GET", path=RECLIP_CLAIM_PATH),
                ).json()
            )
            result_path = f"{API_PREFIX}/device-commands/{claim.command.command_id}/reclip-result"
            failure_body: dict[str, object] = {
                "evidence_id": None,
                "failure_code": "material_missing",
                "detail": None,
            }
            headers = _host_headers(
                runtime_topology, method="POST", path=result_path, body=failure_body
            ) | {"X-Command-Claim-Token": claim.claim_token}
            first = host.post(result_path, json=failure_body, headers=headers)
            assert first.status_code == 200, first.text
            # 真实重试使用新 nonce; 同一失败原因仍应幂等返回同一结案。
            replay = host.post(
                result_path,
                json=failure_body,
                headers=_host_headers(
                    runtime_topology, method="POST", path=result_path, body=failure_body
                )
                | {"X-Command-Claim-Token": claim.claim_token},
            )
            assert replay.status_code == 200, replay.text
            assert replay.json() == first.json()

            # 失败是终态: 即使登记了合法的 reclip 新引用, 成功回报也必须拒绝。
            assert (
                _post_as_host(
                    host,
                    runtime_topology,
                    path=REGISTRATIONS,
                    body=_reclip_registration(runtime_topology, new_id),
                ).status_code
                == 200
            )
            settle_body: dict[str, object] = {"evidence_id": new_id}
            rejected = host.post(
                result_path,
                json=settle_body,
                headers=_host_headers(
                    runtime_topology, method="POST", path=result_path, body=settle_body
                )
                | {"X-Command-Claim-Token": claim.claim_token},
            )
            assert rejected.status_code == 409, rejected.text
            with engine.begin() as connection:
                row = connection.execute(
                    text(
                        "SELECT status, result_evidence_id, failure_reason "
                        "FROM evidence_reclip_request WHERE request_id = :id"
                    ),
                    {"id": request_id},
                ).one()
            assert row.status == "failed"
            assert row.result_evidence_id is None
            assert row.failure_reason == "material_missing"
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM evidence_evidence WHERE evidence_id IN (:a, :b)"),
                {"a": original_id, "b": new_id},
            )
            connection.execute(
                text("DELETE FROM device_pending_command WHERE host_id = :host_id"),
                {"host_id": runtime_topology.host.id},
            )
            connection.execute(text("TRUNCATE auth_user CASCADE"))
            connection.execute(
                text("DELETE FROM auth_role WHERE code = :code"), {"code": ROLE_CODE}
            )
