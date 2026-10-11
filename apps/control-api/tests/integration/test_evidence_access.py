"""#170 中心证据授权：真实 ASGI 路由 + PostgreSQL 的签发、复核与拒绝边界。

`TestClient` 驱动的是实际 FastAPI 应用、每请求事务和容器里的真实 PostgreSQL；这里证明的是
HTTP 路由 / 用例 / 仓储边界，不是浏览器或 socket 级证据。会话与权限来自真实登录与角色行，
不覆盖 `authenticated_caller` / `granted_permissions`，也不使用假仓储。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping

from fastapi.testclient import TestClient
from httpx2 import Response as HttpResponse
from sqlalchemy import Engine, text
from test_auth_http import CREDENTIALS, SESSION_PATH, an_account, settings
from test_runtime_observation_http import IndependentTopology, RuntimeTopology, _host_headers
from test_runtime_observation_http import independent_topology as independent_topology
from test_runtime_observation_http import runtime_topology as runtime_topology

from factory_sop.app import API_PREFIX, create_app
from factory_sop.auth.adapters.cookies import CSRF_COOKIE, CSRF_HEADER
from factory_sop.auth.adapters.repository import PostgresRoleRepository
from factory_sop.auth.model import Role, User
from factory_sop.auth.permissions import Permission
from factory_sop.identifiers import new_id
from factory_sop.persistence import session_factory

ROLE_CODE = "evidence_access_viewer"
ORIGIN = "https://edge.internal:8765"


def _grant_evidence_view(engine: Engine) -> User:
    """真实账户 + 真实角色行授予唯一所需权限；登录随后在 HTTP 边界取得会话。"""
    user = an_account(engine)
    with session_factory(engine)() as session:
        roles = PostgresRoleRepository(session)
        role = Role(new_id(), ROLE_CODE, "证据查看者", frozenset({Permission.EVIDENCE_VIEW}))
        roles.add(role)
        roles.assign(user_id=user.id, role_ids=(role.id,))
        session.commit()
    return user


def _registration(topology: RuntimeTopology, evidence_id: str) -> dict[str, object]:
    """来源推理机登记的最小受控引用：相对路径 + 摘要 + 大小，故中心登记为可用。"""
    return {
        "evidence_id": evidence_id,
        "host_id": str(topology.host.id),
        "station_id": str(topology.station.id),
        "instance_id": 7,
        "violation_id": None,
        "kind": "clip",
        "origin": "automatic",
        "anchor": 1.0,
        "window_start": 0.0,
        "window_end": 2.0,
        "generation": "original",
        "sha256": "a" * 64,
        "size": 10,
        "reference": "artifact/camera-a.mp4",
    }


def _post_as_host(
    client: TestClient,
    topology: RuntimeTopology | IndependentTopology,
    *,
    path: str,
    body: Mapping[str, object],
) -> HttpResponse:
    """由真实登记主机发起签名 POST；nonce 一次性，故每次调用都重新签名。"""
    return client.post(
        path, json=body, headers=_host_headers(topology, method="POST", path=path, body=dict(body))
    )


def _expired(context: str, *, secret: str) -> str:
    """按 `issue_access_context` 的格式用真实密钥重签同一票据，只把过期时刻改到过去。"""
    encoded, _ = context.split(".")
    claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    claims[3] = int(time.time()) - 1
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    signature = hmac.new(secret.encode(), b"evidence\0" + payload.encode(), hashlib.sha256)
    return payload + "." + signature.hexdigest()


def test_evidence_access_is_issued_and_rechecked_at_real_boundaries(
    engine: Engine, runtime_topology: RuntimeTopology, independent_topology: IndependentTopology
) -> None:
    user = _grant_evidence_view(engine)
    secret = settings().csrf_secret.get_secret_value()
    host_id = str(runtime_topology.host.id)
    station_id = str(runtime_topology.station.id)
    evidence_id = f"{host_id}-clip-1"
    registration = _registration(runtime_topology, evidence_id)
    app = create_app(
        settings().model_copy(update={"evidence_host_origins": {host_id: ORIGIN}}),
        session_factory=session_factory(engine),
    )
    evidence_path = f"{API_PREFIX}/evidence"
    registration_path = f"{evidence_path}/registrations"
    check_path = f"{evidence_path}/access-check"
    try:
        with (
            TestClient(app, base_url="https://testserver") as browser,
            TestClient(app, base_url="https://testserver") as host,
        ):
            # 匿名：没有会话的浏览器查询受保护证据必须被拒。
            anonymous = browser.get(
                evidence_path, params={"station_id": station_id, "instance_id": 7}
            )
            assert anonymous.status_code == 401

            # 先例：来源主机用真实签名登记本机引用；浏览器无权走此路由。
            registered = _post_as_host(
                host, runtime_topology, path=registration_path, body=registration
            )
            assert registered.status_code == 200

            assert browser.post(SESSION_PATH, json=CREDENTIALS).status_code == 201

            listed = browser.get(evidence_path, params={"station_id": station_id, "instance_id": 7})
            assert [row["evidence_id"] for row in listed.json()] == [evidence_id]

            context = browser.get(f"{evidence_path}/{evidence_id}/access-context").json()
            assert (context["host_id"], context["origin"]) == (host_id, ORIGIN)

            body = {"evidence_id": evidence_id, "context": context["context"]}
            granted = _post_as_host(host, runtime_topology, path=check_path, body=body)
            assert granted.status_code == 200
            assert granted.json()["reference"] == "artifact/camera-a.mp4"

            # ID 不匹配：票据绑定的证据与请求体不一致，在实际回查点拒绝。
            mismatch = {"evidence_id": "unregistered", "context": context["context"]}
            denied = _post_as_host(host, runtime_topology, path=check_path, body=mismatch)
            assert denied.status_code == 403

            # 篡改：载荷被改写后签名不再吻合，必须在被解析成另一张票据之前拒绝。
            token = context["context"]
            flipped = "A" if token[0] != "A" else "B"
            tampered = {"evidence_id": evidence_id, "context": flipped + token[1:]}
            refused = _post_as_host(host, runtime_topology, path=check_path, body=tampered)
            assert (refused.status_code, refused.json()) == (403, {"detail": "access_denied"})

            # 无签名：没有主机身份头时身份未提供，与主机认证失败是两种拒绝。
            unsigned = host.post(check_path, json=body)
            assert unsigned.status_code == 403
            assert unsigned.json() == {"detail": "host_identity_required"}

            # 伪造签名：头齐全但签名不是登记公钥所签，主机认证失败。
            forged = _host_headers(runtime_topology, method="POST", path=check_path, body=body)
            forged["X-Inference-Host-Signature"] = "0" * 64
            forged_response = host.post(check_path, json=body, headers=forged)
            assert forged_response.status_code == 401
            assert forged_response.json()["error_code"] == "INFERENCE_HOST_AUTHENTICATION_FAILED"

            # 他机签名：另一台真实登记主机拿本机票据回查，票据绑定的来源主机与之不符。
            other = _post_as_host(host, independent_topology, path=check_path, body=body)
            assert (other.status_code, other.json()) == (403, {"detail": "access_denied"})

            # 过期票据：真实密钥签出的过期 claim 在逐次回查时报告 410，而不是通行。
            expired_context = _expired(token, secret=secret)
            expired_body = {"evidence_id": evidence_id, "context": expired_context}
            expired = _post_as_host(host, runtime_topology, path=check_path, body=expired_body)
            assert (expired.status_code, expired.json()) == (410, {"detail": "context_expired"})

            # 会话失效：登出删除会话行后，同一票据的逐次回查必须拒绝。
            logged_out = browser.delete(
                SESSION_PATH, headers={CSRF_HEADER: browser.cookies[CSRF_COOKIE]}
            )
            assert logged_out.status_code == 204
            revoked = _post_as_host(host, runtime_topology, path=check_path, body=body)
            assert revoked.status_code == 403

            # 撤权：重新登录取票据后删除真实角色分配行，签发与逐次回查都按权限拒绝。
            assert browser.post(SESSION_PATH, json=CREDENTIALS).status_code == 201
            reissued = browser.get(f"{evidence_path}/{evidence_id}/access-context")
            assert reissued.status_code == 200
            fresh = {"evidence_id": evidence_id, "context": reissued.json()["context"]}
            with session_factory(engine)() as session:
                PostgresRoleRepository(session).assign(user_id=user.id, role_ids=())
                session.commit()
            revoked_issue = browser.get(f"{evidence_path}/{evidence_id}/access-context")
            assert revoked_issue.status_code == 403
            assert revoked_issue.json()["error_code"] == "PERMISSION_DENIED"
            refused = _post_as_host(host, runtime_topology, path=check_path, body=fresh)
            assert (refused.status_code, refused.json()["error_code"]) == (403, "PERMISSION_DENIED")

            # 会话超时：真实会话行改成两天前使用，超过空闲窗口后同一票据按会话无效拒绝。
            with engine.begin() as connection:
                connection.execute(
                    text("UPDATE auth_session SET last_used_at = now() - interval '2 days'")
                )
            timed_out = _post_as_host(host, runtime_topology, path=check_path, body=fresh)
            assert timed_out.status_code == 403
            assert timed_out.json() == {"detail": "session_invalid"}

        # 注册事实未被读取路径改写：引用、摘要与可用状态保持原样。
        with engine.connect() as connection:
            stored = connection.execute(
                text(
                    "SELECT reference, sha256, status FROM evidence_evidence "
                    "WHERE evidence_id = :evidence_id"
                ),
                {"evidence_id": evidence_id},
            ).one()
        assert tuple(stored) == ("artifact/camera-a.mp4", "a" * 64, "available")
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM evidence_evidence WHERE evidence_id = :evidence_id"),
                {"evidence_id": evidence_id},
            )
            connection.execute(text("TRUNCATE auth_user CASCADE"))
            connection.execute(
                text("DELETE FROM auth_role WHERE code = :code"), {"code": ROLE_CODE}
            )
