"""SYS-92 — 点位 CRUD、能力 gate 与 HTTP 授权走真实中心链路。

前置：真实 PostgreSQL 已由系统夹具执行 Alembic 迁移，管理员经 bootstrap 登录。
动作：管理员通过 HTTP 创建工位、连接器和点位，读取新事务中的记录，完成能力测量与绑定预检；
再以只读点位权限的独立账户访问同一 API。
可观察结果：点位跨请求可重载，跨工位写入在事务内拒绝且不留下记录，绑定预检返回具体拒绝/接受理由，
无点位的普通动作可通过而安全输出明确拒绝，只读账户可读但不能创建点位。
"""

from __future__ import annotations

from conftest import BootstrapCommand, csrf_header, log_in
from httpx2 import Client

HOSTS = "/api/v1/inference-hosts"
STATIONS = "/api/v1/stations"
CONNECTORS = "/api/v1/connectors"
POINTS = "/api/v1/points"
VALIDATIONS = "/api/v1/point-binding-validations"
ROLES = "/api/v1/auth/roles"
USERS = "/api/v1/auth/users"
SESSIONS = "/api/v1/auth/session"

HOST = {
    "name": "装配A线-推理机1",
    "address": "10.0.8.11",
    "mediamtx_address": None,
    "recording_window_seconds": 7 * 24 * 3600,
    "disk_watermark_percent": 85,
}
STATION = {"code": "A-001", "name": "装配一号工位", "tags": []}
EMPTY_STATION = {"code": "EMPTY-001", "name": "无外部信号工位", "tags": []}
CONNECTOR = {
    "name": "一号连接器",
    "connector_type": "hikvision_isapi",
    "configuration": {"address": "10.0.8.21", "port": 80},
}
MEASURED_CAPABILITY = {
    "verification": "measured",
    "delivery": "polled",
    "polling_interval_seconds": 0.05,
    "max_delivery_delay_seconds": 0.08,
    "sequencing": "sequenced",
    "edge_preservation": "preserved",
    "timestamp_source": "host_receipt",
}


# 仅用于黑盒场景的合成凭据。
OPERATOR_CREDENTIALS = {  # pragma: allowlist secret
    "login_name": "point.viewer",
    "display_name": "点位只读用户",
    "password": "point-viewer-3",  # pragma: allowlist secret
}


def _normalize_dto(body: dict[str, object]) -> dict[str, object]:
    normalized = dict(body)
    for field in ("id", "created_by", "updated_by", "created_at", "updated_at"):
        normalized[field] = "<dynamic>"
    return normalized


def test_point_crud_binding_validation_and_permissions_use_real_http_and_postgres(
    client: Client,
    bootstrap: BootstrapCommand,
) -> None:
    log_in(client, bootstrap)
    headers = csrf_header(client)

    host = client.post(HOSTS, json=HOST, headers=headers)
    assert host.status_code == 201, host.text
    station = client.post(STATIONS, json=STATION, headers=headers)
    assert station.status_code == 201, station.text
    connector = client.post(
        CONNECTORS,
        json=CONNECTOR | {"host_id": host.json()["id"], "station_id": station.json()["id"]},
        headers=headers,
    )
    assert connector.status_code == 201, connector.text
    station_id = station.json()["id"]
    connector_id = connector.json()["id"]

    created = client.post(
        POINTS,
        json={
            "station_id": station_id,
            "connector_id": connector_id,
            "direction": "input",
            "identifier": "DI-01",
            "semantic_label": "工件到位",
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    point = created.json()

    # 第二个 HTTP 客户端经公开接缝重新读取已提交记录。
    with Client(base_url=client.base_url, verify=False, trust_env=False) as reloaded:
        opened = reloaded.post(
            SESSIONS,
            json={
                "login_name": "chen.wei",
                "password": "assembly-line-4",  # pragma: allowlist secret
            },
        )
        assert opened.status_code == 201, opened.text
        detail = reloaded.get(f"{POINTS}/{point['id']}")
        assert detail.status_code == 200
        assert detail.json() == point
        listing = reloaded.get(POINTS, params={"station_id": station_id})
        assert listing.status_code == 200
        assert listing.json()["items"] == [point]

    unverified = client.post(
        VALIDATIONS,
        json={
            "station_id": station_id,
            "point_id": point["id"],
            "role": "start_signal",
            "budget_seconds": 0.2,
        },
        headers=headers,
    )
    assert unverified.status_code == 200
    assert unverified.json() == {
        "accepted": False,
        "reasons": [
            {
                "code": "capability_unverified",
                "field": "capability.verification",
                "message": "连接器能力尚未实测验证",
            }
        ],
    }

    measured = client.put(
        f"{CONNECTORS}/{connector_id}/capability",
        json=MEASURED_CAPABILITY,
        headers={**headers, "If-Match": "1"},
    )
    assert measured.status_code == 200, measured.text
    assert _normalize_dto(measured.json()) == {
        "id": "<dynamic>",
        "station_id": station_id,
        "host_id": host.json()["id"],
        "name": CONNECTOR["name"],
        "connector_type": CONNECTOR["connector_type"],
        "configuration": CONNECTOR["configuration"],
        "credentials_configured": False,
        "reachability": "unverified",
        "health_detail": None,
        "capability": MEASURED_CAPABILITY,
        "status": "active",
        "revision": 2,
        "created_by": "<dynamic>",
        "updated_by": "<dynamic>",
        "created_at": "<dynamic>",
        "updated_at": "<dynamic>",
    }
    capability_reload = client.get(f"{CONNECTORS}/{connector_id}")
    assert capability_reload.status_code == 200
    assert _normalize_dto(capability_reload.json()) == _normalize_dto(measured.json())

    accepted = client.post(
        VALIDATIONS,
        json={
            "station_id": station_id,
            "point_id": point["id"],
            "role": "start_signal",
            "budget_seconds": 0.2,
        },
        headers=headers,
    )
    assert accepted.status_code == 200
    assert accepted.json() == {"accepted": True, "reasons": []}

    empty_station = client.post(STATIONS, json=EMPTY_STATION, headers=headers)
    assert empty_station.status_code == 201, empty_station.text
    empty_station_id = empty_station.json()["id"]
    no_point_action = client.post(
        VALIDATIONS,
        json={
            "station_id": empty_station_id,
            "role": "start_signal",
            "budget_seconds": 0.2,
        },
        headers=headers,
    )
    assert no_point_action.status_code == 200
    assert no_point_action.json() == {"accepted": True, "reasons": []}
    no_point_safety = client.post(
        VALIDATIONS,
        json={
            "station_id": empty_station_id,
            "role": "safety_output",
            "budget_seconds": 0.2,
        },
        headers=headers,
    )
    assert no_point_safety.status_code == 200
    assert no_point_safety.json() == {
        "accepted": False,
        "reasons": [
            {
                "code": "point_required",
                "field": "point_id",
                "message": "安全输出必须选择一个输出点位",
            }
        ],
    }

    cross_station = client.post(
        POINTS,
        json={
            "station_id": empty_station_id,
            "connector_id": connector_id,
            "direction": "input",
            "identifier": "DI-02",
            "semantic_label": "跨工位输入",
        },
        headers=headers,
    )
    assert cross_station.status_code == 409
    assert cross_station.json()["error_code"] == "POINT_CONNECTOR_STATION_MISMATCH"
    assert client.get(POINTS, params={"station_id": empty_station_id}).json()["total"] == 0

    viewer_role = client.post(
        ROLES,
        json={
            "code": "point_viewer",
            "name": "点位只读",
            "permissions": ["device.point.view"],
        },
        headers=headers,
    )
    assert viewer_role.status_code == 201, viewer_role.text
    operator = client.post(USERS, json=OPERATOR_CREDENTIALS, headers=headers)
    assert operator.status_code == 201, operator.text
    assigned = client.put(
        f"{USERS}/{operator.json()['id']}/roles",
        json={"role_ids": [viewer_role.json()["id"]]},
        headers=headers,
    )
    assert assigned.status_code == 200, assigned.text

    with Client(base_url=client.base_url, verify=False, trust_env=False) as operator_client:
        opened = operator_client.post(
            SESSIONS,
            json={
                "login_name": OPERATOR_CREDENTIALS["login_name"],
                "password": OPERATOR_CREDENTIALS["password"],
            },
        )
        assert opened.status_code == 201, opened.text
        assert opened.json()["permissions"] == ["device.point.view"]
        assert operator_client.get(POINTS, params={"station_id": station_id}).status_code == 200
        operator_validation = operator_client.post(
            VALIDATIONS,
            json={
                "station_id": station_id,
                "point_id": point["id"],
                "role": "start_signal",
                "budget_seconds": 0.2,
            },
            headers=csrf_header(operator_client),
        )
        assert operator_validation.status_code == 200
        assert operator_validation.json() == {"accepted": True, "reasons": []}
        denied_create = operator_client.post(
            POINTS,
            json={
                "station_id": station_id,
                "connector_id": connector_id,
                "direction": "input",
                "identifier": "DI-03",
                "semantic_label": "只读用户不能创建",
            },
            headers=csrf_header(operator_client),
        )
        assert denied_create.status_code == 403
        assert denied_create.json()["error_code"] == "PERMISSION_DENIED"
