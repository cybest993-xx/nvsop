"""正常交权 HTTP 边界：真实 ASGI + PostgreSQL 的签名领取/回报（#214）。

`TestClient` 驱动生产 `create_app`：会话/CSRF/权限走真实 auth 主路径；主机领取与回报走
真实公钥签名。专用端点只领取 `stop_execution_handover`；默认 `/device-commands/next`
不领取新类型；回报在同一请求事务内复核冻结身份、写状态并结案命令。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text

from factory_sop.app import API_PREFIX, create_app
from factory_sop.auth.adapters.repository import PostgresRoleRepository, PostgresUserRepository
from factory_sop.auth.model import Role, User, UserStatus
from factory_sop.auth.passwords import hash_password
from factory_sop.auth.permissions import Permission
from factory_sop.device.adapters.repository import (
    PostgresInferenceHostRepository,
    PostgresStationRepository,
)
from factory_sop.device.model import DeviceStatus, InferenceHost, Station
from factory_sop.execution.adapters.repository import PostgresExecutionGrantRepository
from factory_sop.execution.model import StationGrant
from factory_sop.identifiers import new_id
from factory_sop.persistence import session_factory
from factory_sop.settings import Settings
from nvsop_contracts import (
    HostIdentityKeyPair,
    HostIdentityRequest,
    generate_host_identity_key_pair,
    sign_host_identity_request,
)

NOW = datetime(2026, 9, 22, 1, 0, tzinfo=UTC)
PASSWORD = "assembly-line-3"  # pragma: allowlist secret
SESSION_PATH = f"{API_PREFIX}/auth/session"
NORMAL_HANDOVERS = f"{API_PREFIX}/execution/normal-handovers"
CLAIM_PATH = f"{API_PREFIX}/device-commands/execution-handover/next"
DEFAULT_NEXT = f"{API_PREFIX}/device-commands/next"


def _host(name: str, identity: HostIdentityKeyPair | None = None) -> InferenceHost:
    return InferenceHost(
        id=new_id(),
        name=name,
        address="10.0.8.11",
        mediamtx_address=None,
        recording_window_seconds=604800,
        disk_watermark_percent=85,
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=new_id(),
        updated_by=new_id(),
        created_at=NOW,
        updated_at=NOW,
        identity_public_key=None if identity is None else identity.public_key,
    )


def _station() -> Station:
    return Station(
        id=new_id(),
        code=f"NHTTP-{new_id().hex[:8]}",
        name="正常交权 HTTP 工位",
        tags=(),
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=new_id(),
        updated_by=new_id(),
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
def identity() -> HostIdentityKeyPair:
    import random
    from unittest.mock import patch

    rng = random.Random(214)
    with (
        patch("nvsop_contracts.host_identity.secrets.randbits", side_effect=rng.getrandbits),
        patch("nvsop_contracts.host_identity.secrets.randbelow", side_effect=rng.randrange),
    ):
        return generate_host_identity_key_pair()


@pytest.fixture
def arrangement(engine: Engine, identity: HostIdentityKeyPair) -> Iterator[dict[str, object]]:
    suffix = new_id().hex[:8]
    operator = User(
        id=new_id(),
        login_name=f"normal.http.{suffix}",
        display_name="正常交权操作者",
        password_hash=hash_password(PASSWORD),
        status=UserStatus.ACTIVE,
    )
    role = Role(
        id=new_id(),
        code=f"normal_http_{suffix}",
        name="正常交权 HTTP",
        permissions=frozenset({Permission.HANDOVER_EDIT}),
    )
    station = _station()
    from_host = _host("正常交权 HTTP 旧机", identity)
    to_host = _host("正常交权 HTTP 新机")
    session = session_factory(engine)()
    try:
        PostgresUserRepository(session).add(operator)
        PostgresRoleRepository(session).add(role)
        PostgresRoleRepository(session).assign(user_id=operator.id, role_ids=[role.id])
        PostgresStationRepository(session).add(station)
        hosts = PostgresInferenceHostRepository(session)
        hosts.add(from_host)
        hosts.add(to_host)
        PostgresExecutionGrantRepository(session).acquire_if_available(
            StationGrant(
                grant_id=new_id(),
                station_id=station.id,
                holder_host_id=from_host.id,
                lease_expires_at=(renewed_at := datetime.now(UTC)) + timedelta(days=7),
                renewed_at=renewed_at,
                request_id=new_id(),
            )
        )
        session.commit()
    finally:
        session.close()
    yield {
        "operator": operator.login_name,
        "station_id": station.id,
        "from_host_id": from_host.id,
        "to_host_id": to_host.id,
        "identity": identity,
    }
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM execution_normal_handover WHERE station_id = :sid"),
            {"sid": station.id},
        )
        connection.execute(
            text("DELETE FROM device_pending_command WHERE host_id = :hid"), {"hid": from_host.id}
        )
        connection.execute(
            text(
                "UPDATE execution_station_grant SET lease_expires_at = n - INTERVAL '1 day',"
                " renewed_at = n - INTERVAL '8 days' FROM (SELECT clock_timestamp() AS n) sub"
                " WHERE station_id = :sid"
            ),
            {"sid": station.id},
        )
        connection.execute(
            text("DELETE FROM execution_station_grant WHERE station_id = :sid"), {"sid": station.id}
        )
        connection.execute(text("DELETE FROM device_station WHERE id = :sid"), {"sid": station.id})
        connection.execute(
            text("DELETE FROM device_inference_host WHERE id = ANY(:ids)"),
            {"ids": [from_host.id, to_host.id]},
        )
        connection.execute(text("DELETE FROM auth_user WHERE id = :uid"), {"uid": operator.id})
        connection.execute(text("DELETE FROM auth_role WHERE id = :rid"), {"rid": role.id})


def _settings() -> Settings:
    return Settings(
        log_level="warning",
        database_host="unused",
        database_port=5432,
        database_name="unused",
        database_user="unused",
        database_password=SecretStr("unused"),
        session_idle_timeout_minutes=720,
        session_absolute_lifetime_minutes=43200,
        session_cookie_transport="require_https",
        csrf_secret=SecretStr("csrf-secret"),
        redis_url=SecretStr("redis://127.0.0.1:1/0"),
    )


@pytest.fixture
def app(engine: Engine) -> FastAPI:
    return create_app(_settings(), session_factory=session_factory(engine))


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """带会话 cookie 的浏览器侧客户端; CSRF 中间件按 cookie 判定。"""
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client


@pytest.fixture
def host_client(app: FastAPI) -> Iterator[TestClient]:
    """无 cookie 的主机侧客户端: 主机签名请求不走 CSRF。"""
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client


def _login(client: TestClient, login_name: str) -> dict[str, str]:
    opened = client.post(SESSION_PATH, json={"login_name": login_name, "password": PASSWORD})
    assert opened.status_code == 201, opened.text
    return {"x-csrf-token": client.cookies["sop_csrf"]}


def _host_headers(
    arrangement: dict[str, object],
    *,
    method: str,
    path: str,
    body: Mapping[str, object] | None = None,
) -> dict[str, str]:
    import time

    identity = cast(HostIdentityKeyPair, arrangement["identity"])
    host_id = str(arrangement["from_host_id"])
    timestamp = int(time.time())
    nonce = f"normal-{new_id().hex}"
    request = HostIdentityRequest(
        method=method,
        path=path,
        host_id=host_id,
        timestamp=timestamp,
        nonce=nonce,
        body=body,
    )
    return {
        "X-Inference-Host-ID": host_id,
        "X-Inference-Host-Timestamp": str(timestamp),
        "X-Inference-Host-Nonce": nonce,
        "X-Inference-Host-Signature": sign_host_identity_request(
            request, private_key=identity.private_key
        ),
    }


def _create_normal_handover(
    client: TestClient, arrangement: dict[str, object], csrf: dict[str, str]
) -> dict[str, object]:
    response = client.post(
        NORMAL_HANDOVERS,
        headers={**csrf, "Idempotency-Key": f"normal-http-{new_id().hex}"},
        json={
            "station_id": str(arrangement["station_id"]),
            "from_host_id": str(arrangement["from_host_id"]),
            "to_host_id": str(arrangement["to_host_id"]),
        },
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, object], response.json())


def test_signed_claim_returns_the_frozen_command_and_default_next_does_not(
    client: TestClient, host_client: TestClient, arrangement: dict[str, object]
) -> None:
    csrf = _login(client, str(arrangement["operator"]))
    record = _create_normal_handover(client, arrangement, csrf)

    # 默认通用领取端点不认识新命令类型。
    empty = host_client.get(
        DEFAULT_NEXT, headers=_host_headers(arrangement, method="GET", path=DEFAULT_NEXT)
    )
    assert empty.status_code == 204

    claim = host_client.get(
        CLAIM_PATH, headers=_host_headers(arrangement, method="GET", path=CLAIM_PATH)
    )
    assert claim.status_code == 200, claim.text
    document: dict[str, object] = claim.json()
    command: dict[str, object] = document["command"]  # type: ignore[assignment]
    assert command["command_type"] == "stop_execution_handover"
    assert command["handover_id"] == str(record["handover_id"])
    assert command["grant_id"] == str(record["grant_id"])
    assert command["station_id"] == str(arrangement["station_id"])
    assert document["claim_token"]

    # 回报确认: 冻结身份匹配, 请求转 confirmed, 命令同事务结案。
    result_path = f"{API_PREFIX}/device-commands/{record['command_id']}/execution-handover-result"
    body = {
        "handover_id": str(record["handover_id"]),
        "grant_id": str(record["grant_id"]),
        "outcome": "confirmed",
        "detail": None,
        "failure_code": None,
    }
    token = str(document["claim_token"])
    completed = host_client.post(
        result_path,
        json=body,
        headers={
            **_host_headers(arrangement, method="POST", path=result_path, body=body),
            "X-Command-Claim-Token": token,
        },
    )
    assert completed.status_code == 200, completed.text
    assert completed.json() == {
        "accepted": True,
        "handover_id": str(record["handover_id"]),
        "status": "confirmed",
    }

    read_back = client.get(f"{NORMAL_HANDOVERS}/{record['handover_id']}", headers=csrf)
    assert read_back.status_code == 200
    assert read_back.json()["status"] == "confirmed"


def test_unauthenticated_claim_is_rejected(
    client: TestClient, host_client: TestClient, arrangement: dict[str, object]
) -> None:
    csrf = _login(client, str(arrangement["operator"]))
    _create_normal_handover(client, arrangement, csrf)

    forged = host_client.get(
        CLAIM_PATH, headers={"X-Inference-Host-ID": str(arrangement["from_host_id"])}
    )
    assert forged.status_code in (401, 403), forged.text


def test_stale_grant_confirmation_is_rejected_but_command_settled(
    client: TestClient, host_client: TestClient, engine: Engine, arrangement: dict[str, object]
) -> None:
    csrf = _login(client, str(arrangement["operator"]))
    record = _create_normal_handover(client, arrangement, csrf)

    # 真实替换租约: 冻结 grant 失效。先让旧租约过期, 绕过活跃租约删除守卫。
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE execution_station_grant SET lease_expires_at = n - INTERVAL '1 day',"
                " renewed_at = n - INTERVAL '8 days' FROM (SELECT clock_timestamp() AS n) sub"
                " WHERE station_id = :sid"
            ),
            {"sid": arrangement["station_id"]},
        )
        connection.execute(
            text("DELETE FROM execution_station_grant WHERE station_id = :sid"),
            {"sid": arrangement["station_id"]},
        )
        renewed_at = datetime.now(UTC)
        connection.execute(
            text(
                "INSERT INTO execution_station_grant "
                "(station_id, grant_id, holder_host_id, lease_expires_at, renewed_at, request_id) "
                "VALUES (:sid, :gid, :hid, :exp, :ren, :rid)"
            ),
            {
                "sid": arrangement["station_id"],
                "gid": new_id(),
                "hid": arrangement["from_host_id"],
                "exp": renewed_at + timedelta(days=7),
                "ren": renewed_at,
                "rid": new_id(),
            },
        )

    claim = host_client.get(
        CLAIM_PATH, headers=_host_headers(arrangement, method="GET", path=CLAIM_PATH)
    )
    assert claim.status_code == 200, claim.text
    document: dict[str, object] = claim.json()
    result_path = f"{API_PREFIX}/device-commands/{record['command_id']}/execution-handover-result"
    body = {
        "handover_id": str(record["handover_id"]),
        "grant_id": str(record["grant_id"]),
        "outcome": "confirmed",
        "detail": None,
        "failure_code": None,
    }
    completed = host_client.post(
        result_path,
        json=body,
        headers={
            **_host_headers(arrangement, method="POST", path=result_path, body=body),
            "X-Command-Claim-Token": str(document["claim_token"]),
        },
    )
    assert completed.status_code == 200, completed.text
    payload: dict[str, object] = completed.json()
    assert payload["accepted"] is False

    read_back = client.get(f"{NORMAL_HANDOVERS}/{record['handover_id']}", headers=csrf)
    assert read_back.json()["status"] == "pending"


def test_cancel_is_rejected_for_callers_without_the_permission(
    client: TestClient, engine: Engine, arrangement: dict[str, object]
) -> None:
    outsider = User(
        id=new_id(),
        login_name=f"normal.outsider.{new_id().hex[:8]}",
        display_name="无权用户",
        password_hash=hash_password(PASSWORD),
        status=UserStatus.ACTIVE,
    )
    session = session_factory(engine)()
    try:
        PostgresUserRepository(session).add(outsider)
        session.commit()
    finally:
        session.close()
    try:
        csrf = _login(client, str(arrangement["operator"]))
        record = _create_normal_handover(client, arrangement, csrf)
        outsider_csrf = _login(client, outsider.login_name)

        hidden = client.get(f"{NORMAL_HANDOVERS}/{record['handover_id']}")
        assert hidden.status_code == 403, hidden.text
        refused = client.post(
            f"{NORMAL_HANDOVERS}/{record['handover_id']}/cancellation", headers=outsider_csrf
        )
        assert refused.status_code == 403, refused.text
    finally:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM auth_user WHERE id = :uid"), {"uid": outsider.id})


def test_removing_a_host_referenced_by_history_is_structurally_refused(
    client: TestClient, host_client: TestClient, engine: Engine, arrangement: dict[str, object]
) -> None:
    """确认历史引用旧机: DELETE 是 409/结构化拒绝, 不是 500。"""
    csrf = _login(client, str(arrangement["operator"]))
    record = _create_normal_handover(client, arrangement, csrf)
    claim = host_client.get(
        CLAIM_PATH, headers=_host_headers(arrangement, method="GET", path=CLAIM_PATH)
    )
    document: dict[str, object] = claim.json()
    result_path = f"{API_PREFIX}/device-commands/{record['command_id']}/execution-handover-result"
    body = {
        "handover_id": str(record["handover_id"]),
        "grant_id": str(record["grant_id"]),
        "outcome": "confirmed",
        "detail": None,
        "failure_code": None,
    }
    completed = host_client.post(
        result_path,
        json=body,
        headers={
            **_host_headers(arrangement, method="POST", path=result_path, body=body),
            "X-Command-Claim-Token": str(document["claim_token"]),
        },
    )
    assert completed.status_code == 200

    removed = client.delete(
        f"{API_PREFIX}/inference-hosts/{arrangement['from_host_id']}",
        headers=csrf,
    )
    assert removed.status_code in (409, 422), removed.text
    assert removed.status_code != 500


def test_duplicate_result_is_idempotent_for_the_same_outcome(
    client: TestClient, host_client: TestClient, arrangement: dict[str, object]
) -> None:
    csrf = _login(client, str(arrangement["operator"]))
    record = _create_normal_handover(client, arrangement, csrf)
    claim = host_client.get(
        CLAIM_PATH, headers=_host_headers(arrangement, method="GET", path=CLAIM_PATH)
    )
    document: dict[str, object] = claim.json()
    result_path = f"{API_PREFIX}/device-commands/{record['command_id']}/execution-handover-result"
    body = {
        "handover_id": str(record["handover_id"]),
        "grant_id": str(record["grant_id"]),
        "outcome": "confirmed",
        "detail": None,
        "failure_code": None,
    }
    first = host_client.post(
        result_path,
        json=body,
        headers={
            **_host_headers(arrangement, method="POST", path=result_path, body=body),
            "X-Command-Claim-Token": str(document["claim_token"]),
        },
    )
    assert first.status_code == 200
    assert first.json()["accepted"] is True

    # nonce 一次性: 重放用新签名, 但同一 claim token 与相同结果。
    replayed = host_client.post(
        result_path,
        json=body,
        headers={
            **_host_headers(arrangement, method="POST", path=result_path, body=body),
            "X-Command-Claim-Token": str(document["claim_token"]),
        },
    )
    # 相同结果的重复回报幂等, 不翻转状态也不制造新确认。
    assert replayed.status_code == 200, replayed.text
    read_back = client.get(f"{NORMAL_HANDOVERS}/{record['handover_id']}", headers=csrf)
    assert read_back.json()["status"] == "confirmed"
