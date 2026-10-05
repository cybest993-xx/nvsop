"""强制改绑双人确认的真实 PostgreSQL HTTP 边界。

用真实账户、真实会话 Cookie、真实 CSRF 双提交与真实管理锁走完整 auth 主路径：匿名/缺
CSRF 被现有中间件拒绝，权限由 `authorize` 与 `HandoverAuthority` 实时复核，未知工位/主机
由真实外键拒绝并映射成稳定 problem 而不是 500。不 override `authenticated_caller`。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response as HttpResponse
from pydantic import SecretStr
from sqlalchemy import Engine, text

from factory_sop.app import API_PREFIX, create_app
from factory_sop.auth.adapters.repository import (
    PostgresRoleRepository,
    PostgresUserRepository,
)
from factory_sop.auth.model import Role, User, UserStatus
from factory_sop.auth.passwords import hash_password
from factory_sop.auth.permissions import Permission
from factory_sop.device.adapters.repository import (
    PostgresInferenceHostRepository,
    PostgresStationRepository,
)
from factory_sop.device.model import DeviceStatus, InferenceHost, Station
from factory_sop.execution.adapters.repository import PostgresExecutionGrantRepository
from factory_sop.execution.model import HANDOVER_RISK_STATEMENT, StationGrant
from factory_sop.identifiers import new_id
from factory_sop.persistence import session_factory
from factory_sop.settings import Settings

NOW = datetime(2026, 9, 22, 1, 0, tzinfo=UTC)
PASSWORD = "assembly-line-3"  # pragma: allowlist secret
SESSION_PATH = f"{API_PREFIX}/auth/session"
HANDOVERS_PATH = f"{API_PREFIX}/execution/handovers"
RISK_PATH = f"{API_PREFIX}/execution/handovers/risk"


@dataclass(frozen=True, slots=True)
class Arrangement:
    operator_login: str
    second_login: str
    outsider_login: str
    station_id: UUID
    from_host_id: UUID
    to_host_id: UUID
    user_ids: tuple[UUID, ...]
    role_id: UUID


@dataclass(frozen=True, slots=True)
class DeletionArrangement:
    """双确认后验证真实 device DELETE 接口所需的最小布置。"""

    operator_login: str
    second_login: str
    station_id: UUID
    from_host_id: UUID
    to_host_id: UUID


def _user(login_name: str) -> User:
    return User(
        id=new_id(),
        login_name=login_name,
        display_name=login_name,
        password_hash=hash_password(PASSWORD),
        status=UserStatus.ACTIVE,
    )


def _host(name: str) -> InferenceHost:
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
    )


def _station() -> Station:
    return Station(
        id=new_id(),
        code=f"HTTP-{new_id().hex[:8]}",
        name="强制改绑 HTTP 工位",
        tags=(),
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=new_id(),
        updated_by=new_id(),
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
def arrangement(engine: Engine) -> Iterator[Arrangement]:
    """真实提交两名有权用户、一名无权用户、工位与两台推理机；测试后清理。"""
    suffix = new_id().hex[:8]
    operator = _user(f"http.operator.{suffix}")
    second = _user(f"http.second.{suffix}")
    outsider = _user(f"http.outsider.{suffix}")
    role = Role(
        id=new_id(),
        code=f"http_device_admin_{suffix}",
        name="HTTP 设备管理员",
        permissions=frozenset({Permission.HANDOVER_EDIT}),
    )
    station = _station()
    host_from, host_to = _host("HTTP 旧机"), _host("HTTP 新机")
    session = session_factory(engine)()
    try:
        users = PostgresUserRepository(session)
        users.add(operator)
        users.add(second)
        users.add(outsider)
        roles = PostgresRoleRepository(session)
        roles.add(role)
        roles.assign(user_id=operator.id, role_ids=[role.id])
        roles.assign(user_id=second.id, role_ids=[role.id])
        hosts = PostgresInferenceHostRepository(session)
        hosts.add(host_from)
        hosts.add(host_to)
        PostgresStationRepository(session).add(station)
        session.commit()
    finally:
        session.close()
    # 服务器关系复核以 execution 自己的当前租约为权威：该工位当前归属旧机。
    _seed_grant(engine, station.id, host_from.id)
    yield Arrangement(
        operator_login=operator.login_name,
        second_login=second.login_name,
        outsider_login=outsider.login_name,
        station_id=station.id,
        from_host_id=host_from.id,
        to_host_id=host_to.id,
        user_ids=(operator.id, second.id, outsider.id),
        role_id=role.id,
    )
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM execution_handover WHERE station_id = :station_id"),
            {"station_id": station.id},
        )
        connection.execute(
            text("DELETE FROM execution_station_grant WHERE station_id = :station_id"),
            {"station_id": station.id},
        )
        connection.execute(
            text("DELETE FROM auth_user WHERE id = ANY(:ids)"),
            {"ids": [operator.id, second.id, outsider.id]},
        )
        connection.execute(text("DELETE FROM auth_role WHERE id = :role_id"), {"role_id": role.id})
        connection.execute(
            text("DELETE FROM device_station WHERE id = :station_id"), {"station_id": station.id}
        )
        connection.execute(
            text("DELETE FROM device_inference_host WHERE id = ANY(:ids)"),
            {"ids": [host_from.id, host_to.id]},
        )


@pytest.fixture
def deletion_arrangement(engine: Engine) -> Iterator[DeletionArrangement]:
    """两名有权用户（操作者另有真实删除权限，仅本 case 赋予）、工位、两台推理机与已过期归属租约。"""
    suffix = new_id().hex[:8]
    operator = _user(f"http.delete.operator.{suffix}")
    second = _user(f"http.delete.second.{suffix}")
    handover_role = Role(
        id=new_id(),
        code=f"http_handover_{suffix}",
        name="HTTP 强制改绑",
        permissions=frozenset({Permission.HANDOVER_EDIT}),
    )
    delete_role = Role(
        id=new_id(),
        code=f"http_delete_{suffix}",
        name="HTTP 设备删除",
        permissions=frozenset({Permission.INFERENCE_HOST_DELETE, Permission.STATION_DELETE}),
    )
    station = _station()
    host_from, host_to = _host("HTTP 删除旧机"), _host("HTTP 删除新机")
    session = session_factory(engine)()
    try:
        users = PostgresUserRepository(session)
        users.add(operator)
        users.add(second)
        roles = PostgresRoleRepository(session)
        roles.add(handover_role)
        roles.add(delete_role)
        roles.assign(user_id=operator.id, role_ids=[handover_role.id, delete_role.id])
        roles.assign(user_id=second.id, role_ids=[handover_role.id])
        hosts = PostgresInferenceHostRepository(session)
        hosts.add(host_from)
        hosts.add(host_to)
        PostgresStationRepository(session).add(station)
        session.commit()
    finally:
        session.close()
    _seed_grant(engine, station.id, host_from.id)
    yield DeletionArrangement(
        operator_login=operator.login_name,
        second_login=second.login_name,
        station_id=station.id,
        from_host_id=host_from.id,
        to_host_id=host_to.id,
    )
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM execution_handover WHERE station_id = :station_id"),
            {"station_id": station.id},
        )
        connection.execute(
            text("DELETE FROM execution_station_grant WHERE station_id = :station_id"),
            {"station_id": station.id},
        )
        connection.execute(
            text("DELETE FROM auth_user WHERE id = ANY(:ids)"),
            {"ids": [operator.id, second.id]},
        )
        connection.execute(
            text("DELETE FROM auth_role WHERE id = ANY(:ids)"),
            {"ids": [handover_role.id, delete_role.id]},
        )
        connection.execute(
            text("DELETE FROM device_station WHERE id = :station_id"), {"station_id": station.id}
        )
        connection.execute(
            text("DELETE FROM device_inference_host WHERE id = ANY(:ids)"),
            {"ids": [host_from.id, host_to.id]},
        )


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
def client(engine: Engine) -> Iterator[TestClient]:
    app = create_app(_settings(), session_factory=session_factory(engine))
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client


def _login(client: TestClient, login_name: str) -> dict[str, str]:
    opened = client.post(SESSION_PATH, json={"login_name": login_name, "password": PASSWORD})
    assert opened.status_code == 201, opened.text
    return {"x-csrf-token": client.cookies["sop_csrf"]}


def _create(
    client: TestClient, arrangement: Arrangement, csrf: dict[str, str]
) -> dict[str, object]:
    response = client.post(
        HANDOVERS_PATH,
        headers=csrf,
        json={
            "station_id": str(arrangement.station_id),
            "from_host_id": str(arrangement.from_host_id),
            "to_host_id": str(arrangement.to_host_id),
            "risk_acknowledgement": HANDOVER_RISK_STATEMENT,
        },
    )
    assert response.status_code == 201, response.text
    created: dict[str, object] = response.json()
    return created


def _seed_grant(engine: Engine, station_id: UUID, holder_host_id: UUID) -> None:
    """真实提交一条相对墙钟已过期的租约：仍是当前归属事实，且删除时不触发 active-grant 守卫。"""
    wall_now = datetime.now(UTC)
    session = session_factory(engine)()
    try:
        PostgresExecutionGrantRepository(session).acquire_if_available(
            StationGrant(
                grant_id=new_id(),
                station_id=station_id,
                holder_host_id=holder_host_id,
                lease_expires_at=wall_now - timedelta(days=1),
                renewed_at=wall_now - timedelta(days=8),
                request_id=new_id(),
            )
        )
        session.commit()
    finally:
        session.close()


def _set_holder(engine: Engine, arrangement: Arrangement, holder_host_id: UUID) -> None:
    """用真实仓库替换当前 holder（先删旧行再 acquire），不 mock 租约。"""
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM execution_station_grant WHERE station_id = :id"),
            {"id": arrangement.station_id},
        )
    _seed_grant(engine, arrangement.station_id, holder_host_id)


def _clear_grant(engine: Engine, arrangement: Arrangement) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM execution_station_grant WHERE station_id = :id"),
            {"id": arrangement.station_id},
        )


def _post_create(
    client: TestClient, arrangement: Arrangement, csrf: dict[str, str], **overrides: str
) -> HttpResponse:
    body: dict[str, str] = {
        "station_id": str(arrangement.station_id),
        "from_host_id": str(arrangement.from_host_id),
        "to_host_id": str(arrangement.to_host_id),
        "risk_acknowledgement": HANDOVER_RISK_STATEMENT,
    }
    body.update(overrides)
    return client.post(HANDOVERS_PATH, headers=csrf, json=body)


def _handover_count(engine: Engine, station_id: UUID) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                text("SELECT count(*) FROM execution_handover WHERE station_id = :id"),
                {"id": station_id},
            ).scalar_one()
        )


def test_two_users_confirm_the_same_request_over_real_http(
    client: TestClient, engine: Engine, arrangement: Arrangement
) -> None:
    operator_csrf = _login(client, arrangement.operator_login)
    created = _create(client, arrangement, operator_csrf)
    assert created["operator_risk_shown"] is True
    assert created["second_operator_id"] is None
    assert created["risk_statement"] == HANDOVER_RISK_STATEMENT
    handover_id = created["handover_id"]

    # 第二名用户独立登录、按请求 id 读取同一冻结内容与风险原文。
    with TestClient(client.app, base_url="https://testserver") as second_client:
        second_csrf = _login(second_client, arrangement.second_login)
        read = second_client.get(f"{HANDOVERS_PATH}/{handover_id}")
        assert read.status_code == 200, read.text
        assert read.json()["station_id"] == str(arrangement.station_id)
        assert read.json()["risk_statement"] == HANDOVER_RISK_STATEMENT

        confirmed = second_client.post(
            f"{HANDOVERS_PATH}/{handover_id}/confirmation",
            headers=second_csrf,
            json={
                "station_id": str(arrangement.station_id),
                "from_host_id": str(arrangement.from_host_id),
                "to_host_id": str(arrangement.to_host_id),
                "risk_acknowledgement": HANDOVER_RISK_STATEMENT,
            },
        )
        assert confirmed.status_code == 200, confirmed.text
        body = confirmed.json()
        assert body["second_operator_id"] is not None
        assert body["second_risk_shown"] is True
        assert body["operator_id"] == created["operator_id"]

    # AC3：审批满足不激活新输出权，也不改物理执行权租约。
    with engine.begin() as connection:
        holder = connection.execute(
            text("SELECT holder_host_id FROM execution_station_grant WHERE station_id = :id"),
            {"id": arrangement.station_id},
        ).scalar_one()
    assert holder == arrangement.from_host_id


def test_confirmed_history_refuses_device_deletion_over_real_http(
    client: TestClient, deletion_arrangement: DeletionArrangement
) -> None:
    """两独立认证 session 完成双确认后，真实 device DELETE 对三个引用都返回 409，历史快照不变。"""
    operator_csrf = _login(client, deletion_arrangement.operator_login)
    created = client.post(
        HANDOVERS_PATH,
        headers=operator_csrf,
        json={
            "station_id": str(deletion_arrangement.station_id),
            "from_host_id": str(deletion_arrangement.from_host_id),
            "to_host_id": str(deletion_arrangement.to_host_id),
            "risk_acknowledgement": HANDOVER_RISK_STATEMENT,
        },
    )
    assert created.status_code == 201, created.text
    handover_id = created.json()["handover_id"]

    with TestClient(client.app, base_url="https://testserver") as second_client:
        second_csrf = _login(second_client, deletion_arrangement.second_login)
        confirmed = second_client.post(
            f"{HANDOVERS_PATH}/{handover_id}/confirmation",
            headers=second_csrf,
            json={
                "station_id": str(deletion_arrangement.station_id),
                "from_host_id": str(deletion_arrangement.from_host_id),
                "to_host_id": str(deletion_arrangement.to_host_id),
                "risk_acknowledgement": HANDOVER_RISK_STATEMENT,
            },
        )
    assert confirmed.status_code == 200, confirmed.text
    snapshot = confirmed.json()
    assert snapshot["second_operator_id"] is not None
    assert snapshot["second_confirmed_at"] is not None
    assert snapshot["second_risk_shown"] is True

    targets = (
        (
            f"{API_PREFIX}/inference-hosts/{deletion_arrangement.from_host_id}",
            "INFERENCE_HOST_HAS_HANDOVER_HISTORY",
        ),
        (
            f"{API_PREFIX}/inference-hosts/{deletion_arrangement.to_host_id}",
            "INFERENCE_HOST_HAS_HANDOVER_HISTORY",
        ),
        (
            f"{API_PREFIX}/stations/{deletion_arrangement.station_id}",
            "STATION_HAS_HANDOVER_HISTORY",
        ),
    )
    for path, expected_code in targets:
        refused = client.delete(path, headers={**operator_csrf, "If-Match": "1"})
        assert refused.status_code == 409, refused.text
        assert refused.json()["error_code"] == expected_code

    read = client.get(f"{HANDOVERS_PATH}/{handover_id}")
    assert read.status_code == 200, read.text
    assert read.json() == snapshot


def test_same_operator_cannot_second_own_request(
    client: TestClient, arrangement: Arrangement
) -> None:
    csrf = _login(client, arrangement.operator_login)
    created = _create(client, arrangement, csrf)
    response = client.post(
        f"{HANDOVERS_PATH}/{created['handover_id']}/confirmation",
        headers=csrf,
        json={
            "station_id": str(arrangement.station_id),
            "from_host_id": str(arrangement.from_host_id),
            "to_host_id": str(arrangement.to_host_id),
            "risk_acknowledgement": HANDOVER_RISK_STATEMENT,
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "HANDOVER_SAME_OPERATOR"


def test_changed_content_is_refused(client: TestClient, arrangement: Arrangement) -> None:
    operator_csrf = _login(client, arrangement.operator_login)
    created = _create(client, arrangement, operator_csrf)
    with TestClient(client.app, base_url="https://testserver") as second_client:
        second_csrf = _login(second_client, arrangement.second_login)
        response = second_client.post(
            f"{HANDOVERS_PATH}/{created['handover_id']}/confirmation",
            headers=second_csrf,
            json={
                "station_id": str(arrangement.station_id),
                "from_host_id": str(arrangement.to_host_id),
                "to_host_id": str(arrangement.from_host_id),
                "risk_acknowledgement": HANDOVER_RISK_STATEMENT,
            },
        )
    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "HANDOVER_CONTENT_MISMATCH"


def test_create_refused_when_source_host_is_not_current_holder(
    client: TestClient, engine: Engine, arrangement: Arrangement
) -> None:
    """请求的旧机不是该工位当前 holder：结构化 409，不落库。"""
    _set_holder(engine, arrangement, arrangement.to_host_id)
    csrf = _login(client, arrangement.operator_login)
    response = _post_create(client, arrangement, csrf)
    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "HANDOVER_SOURCE_MISMATCH"
    assert _handover_count(engine, arrangement.station_id) == 0


def test_create_refused_when_station_has_no_current_grant(
    client: TestClient, engine: Engine, arrangement: Arrangement
) -> None:
    """工位没有当前执行权归属：沿用 404，不落库。"""
    _clear_grant(engine, arrangement)
    csrf = _login(client, arrangement.operator_login)
    response = _post_create(client, arrangement, csrf)
    assert response.status_code == 404, response.text
    assert response.json()["error_code"] == "HANDOVER_TARGET_NOT_FOUND"
    assert _handover_count(engine, arrangement.station_id) == 0


def test_create_refused_when_from_and_to_are_same_host(
    client: TestClient, engine: Engine, arrangement: Arrangement
) -> None:
    """旧机与目标机同一台：422，不落库。"""
    csrf = _login(client, arrangement.operator_login)
    response = _post_create(
        client,
        arrangement,
        csrf,
        to_host_id=str(arrangement.from_host_id),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error_code"] == "HANDOVER_SAME_HOST"
    assert _handover_count(engine, arrangement.station_id) == 0


def test_confirm_refused_when_holder_changed_after_create(
    client: TestClient, engine: Engine, arrangement: Arrangement
) -> None:
    """建立后当前 holder 被替换：第二确认复核关系并拒绝，second 仍为空。"""
    operator_csrf = _login(client, arrangement.operator_login)
    created = _create(client, arrangement, operator_csrf)
    _set_holder(engine, arrangement, arrangement.to_host_id)
    with TestClient(client.app, base_url="https://testserver") as second_client:
        second_csrf = _login(second_client, arrangement.second_login)
        response = second_client.post(
            f"{HANDOVERS_PATH}/{created['handover_id']}/confirmation",
            headers=second_csrf,
            json={
                "station_id": str(arrangement.station_id),
                "from_host_id": str(arrangement.from_host_id),
                "to_host_id": str(arrangement.to_host_id),
                "risk_acknowledgement": HANDOVER_RISK_STATEMENT,
            },
        )
    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "HANDOVER_SOURCE_MISMATCH"
    with engine.connect() as connection:
        second = connection.execute(
            text("SELECT second_operator_id FROM execution_handover WHERE handover_id = :id"),
            {"id": created["handover_id"]},
        ).scalar_one()
    assert second is None


def test_unknown_station_is_not_a_raw_500(client: TestClient, arrangement: Arrangement) -> None:
    csrf = _login(client, arrangement.operator_login)
    response = client.post(
        HANDOVERS_PATH,
        headers=csrf,
        json={
            "station_id": str(new_id()),
            "from_host_id": str(arrangement.from_host_id),
            "to_host_id": str(arrangement.to_host_id),
            "risk_acknowledgement": HANDOVER_RISK_STATEMENT,
        },
    )
    assert response.status_code == 404, response.text
    assert response.json()["error_code"] == "HANDOVER_TARGET_NOT_FOUND"


def test_risk_must_be_acknowledged(client: TestClient, arrangement: Arrangement) -> None:
    csrf = _login(client, arrangement.operator_login)
    response = client.post(
        HANDOVERS_PATH,
        headers=csrf,
        json={
            "station_id": str(arrangement.station_id),
            "from_host_id": str(arrangement.from_host_id),
            "to_host_id": str(arrangement.to_host_id),
            "risk_acknowledgement": "",
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["error_code"] == "HANDOVER_RISK_NOT_ACKNOWLEDGED"


def test_malformed_uuid_is_request_invalid(client: TestClient, arrangement: Arrangement) -> None:
    csrf = _login(client, arrangement.operator_login)
    response = client.post(
        HANDOVERS_PATH,
        headers=csrf,
        json={
            "station_id": "not-a-uuid",
            "from_host_id": str(arrangement.from_host_id),
            "to_host_id": str(arrangement.to_host_id),
            "risk_acknowledgement": HANDOVER_RISK_STATEMENT,
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["error_code"] == "REQUEST_INVALID"


def test_anonymous_and_missing_csrf_are_refused(
    client: TestClient, arrangement: Arrangement
) -> None:
    anonymous = client.post(
        HANDOVERS_PATH,
        json={
            "station_id": str(arrangement.station_id),
            "from_host_id": str(arrangement.from_host_id),
            "to_host_id": str(arrangement.to_host_id),
            "risk_acknowledgement": HANDOVER_RISK_STATEMENT,
        },
    )
    assert anonymous.status_code == 401, anonymous.text

    _login(client, arrangement.operator_login)
    without_csrf = client.post(
        HANDOVERS_PATH,
        json={
            "station_id": str(arrangement.station_id),
            "from_host_id": str(arrangement.from_host_id),
            "to_host_id": str(arrangement.to_host_id),
            "risk_acknowledgement": HANDOVER_RISK_STATEMENT,
        },
    )
    assert without_csrf.status_code == 403, without_csrf.text
    assert without_csrf.json()["error_code"] == "CSRF_TOKEN_INVALID"


def test_caller_without_force_permission_is_denied(
    client: TestClient, arrangement: Arrangement
) -> None:
    csrf = _login(client, arrangement.outsider_login)
    response = client.post(
        HANDOVERS_PATH,
        headers=csrf,
        json={
            "station_id": str(arrangement.station_id),
            "from_host_id": str(arrangement.from_host_id),
            "to_host_id": str(arrangement.to_host_id),
            "risk_acknowledgement": HANDOVER_RISK_STATEMENT,
        },
    )
    assert response.status_code == 403, response.text
    assert response.json()["error_code"] == "PERMISSION_DENIED"


def test_read_handover_risk_returns_server_statement(
    client: TestClient, arrangement: Arrangement
) -> None:
    csrf = _login(client, arrangement.operator_login)
    response = client.get(RISK_PATH, headers=csrf)
    assert response.status_code == 200, response.text
    assert response.json() == {"risk_statement": HANDOVER_RISK_STATEMENT}

    client.cookies.clear()
    anonymous = client.get(RISK_PATH)
    assert anonymous.status_code == 401, anonymous.text
