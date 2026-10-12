"""S038 再切片执行：真实 ASGI + PostgreSQL 的签名领取、成功追加与授权复核。

`TestClient` 驱动实际 FastAPI 应用与每请求事务；会话、权限、主机签名与证据登记复用
`test_evidence_reclip` / `test_evidence_access` / `test_runtime_observation_http` 的既有
helper。再切片领取端点默认不挂载在生产 `create_app` 上，这里显式装配以验证完整链路。
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
from factory_sop.evidence.adapters.reclip_routes import router as reclip_router
from factory_sop.persistence import session_factory
from nvsop_contracts import reclip_evidence_claim_from_wire

RECLIP_CLAIM_PATH = f"{API_PREFIX}/device-commands/reclip/next"
REGISTRATIONS = f"{API_PREFIX}/evidence/registrations"
WINDOW_SECONDS = 604800


def _reclip_app(engine: Engine) -> FastAPI:
    """显式装配再切片端点；生产 `create_app` 不含它，真实派发保持关闭 (S039 就绪前)。"""
    app = create_app(settings(), session_factory=session_factory(engine))
    app.include_router(reclip_router, prefix=API_PREFIX)
    return app


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


def test_unmounted_reclip_router_is_absent_from_production_app(engine: Engine) -> None:
    """生产应用不挂载再切片端点, 故默认 Edge 无法领取, 真实派发保持关闭。"""
    app = create_app(settings(), session_factory=session_factory(engine))
    paths = set(app.openapi()["paths"])
    assert RECLIP_CLAIM_PATH not in paths
    assert f"{API_PREFIX}/device-commands/{{command_id}}/reclip-result" not in paths
