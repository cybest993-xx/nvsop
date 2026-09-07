"""点位、连接器能力与绑定预检的 HTTP 合约。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from auth_fakes import FakeRoles, FakeSessions, FakeUsers
from device_fakes import (
    FakeCameras,
    FakeConnectors,
    FakeInferenceHosts,
    FakeInferenceStations,
    FakePoints,
)
from fastapi.testclient import TestClient
from httpx2 import Response as HttpResponse
from pydantic import SecretStr

from factory_sop.app import API_PREFIX, create_app
from factory_sop.auth.adapters import dependencies as auth_dependencies
from factory_sop.auth.adapters.cookies import CSRF_COOKIE, CSRF_HEADER
from factory_sop.auth.api import Permission
from factory_sop.device.adapters import dependencies as device_dependencies
from factory_sop.device.model import DeviceStatus, PointDirection
from factory_sop.problem import PROBLEM_MEDIA_TYPE
from factory_sop.settings import Settings
from nvsop_contracts import (
    EdgePreservation,
    Measured,
    PointRole,
    Polled,
    Sequencing,
    TimestampSource,
)

POINTS = f"{API_PREFIX}/points"
VALIDATIONS = f"{API_PREFIX}/point-binding-validations"
CONNECTORS = f"{API_PREFIX}/connectors"
CREDENTIALS = {  # pragma: allowlist secret
    "login_name": "chen.wei",
    "password": "assembly-line-4",  # pragma: allowlist secret
}
ALL_PERMISSIONS = frozenset(
    {
        Permission.POINT_VIEW,
        Permission.POINT_EDIT,
        Permission.POINT_DELETE,
        Permission.CONNECTOR_VIEW,
        Permission.CONNECTOR_EDIT,
    }
)


def settings() -> Settings:
    return Settings(
        log_level="info",
        database_host="postgres.internal",
        database_port=5432,
        database_name="factory_sop",
        database_user="factory_sop",
        database_password=SecretStr("hunter2"),
        session_idle_timeout_minutes=720,
        session_absolute_lifetime_minutes=43200,
        session_cookie_transport="require_https",
        csrf_secret=SecretStr("csrf-secret"),
    )


class Center:
    """在仓储接口处替换适配器的已登录中心后台。"""

    def __init__(self) -> None:
        self.users = FakeUsers()
        self.sessions = FakeSessions()
        self.roles = FakeRoles(users=self.users)
        self.hosts = FakeInferenceHosts()
        self.stations = FakeInferenceStations()
        self.cameras = FakeCameras()
        self.connectors = FakeConnectors()
        self.points = FakePoints()
        self.granted = ALL_PERMISSIONS
        self.app = create_app(settings())
        self.app.dependency_overrides[auth_dependencies.users] = lambda: self.users
        self.app.dependency_overrides[auth_dependencies.sessions] = lambda: self.sessions
        self.app.dependency_overrides[auth_dependencies.roles] = lambda: self.roles
        self.app.dependency_overrides[auth_dependencies.granted_permissions] = lambda: self.granted
        self.app.dependency_overrides[device_dependencies.hosts] = lambda: self.hosts
        self.app.dependency_overrides[device_dependencies.stations] = lambda: self.stations
        self.app.dependency_overrides[device_dependencies.cameras] = lambda: self.cameras
        self.app.dependency_overrides[device_dependencies.connectors] = lambda: self.connectors
        self.app.dependency_overrides[device_dependencies.points] = lambda: self.points
        self.client = TestClient(self.app, base_url="https://testserver")
        self.users.register(login_name=CREDENTIALS["login_name"], password=CREDENTIALS["password"])
        assert self.client.post(f"{API_PREFIX}/auth/session", json=CREDENTIALS).status_code == 201

    def send(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        **kwargs: Any,  # noqa: ANN401 — 透传给 HTTP 客户端
    ) -> HttpResponse:
        injected = dict(headers or {})
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            injected[CSRF_HEADER] = self.client.cookies[CSRF_COOKIE]
        return self.client.request(method, path, headers=injected, **kwargs)


@pytest.fixture
def center() -> Center:
    return Center()


def _normalize_point(body: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(body)
    for field in ("id", "created_by", "updated_by", "created_at", "updated_at"):
        normalized[field] = "<dynamic>"
    return normalized


def _expected_point(
    *,
    station_id: str,
    connector_id: str,
    direction: str,
    identifier: str,
    semantic_label: str,
    status: str,
    revision: int,
) -> dict[str, Any]:
    return {
        "id": "<dynamic>",
        "station_id": station_id,
        "connector_id": connector_id,
        "direction": direction,
        "identifier": identifier,
        "semantic_label": semantic_label,
        "status": status,
        "revision": revision,
        "created_by": "<dynamic>",
        "updated_by": "<dynamic>",
        "created_at": "<dynamic>",
        "updated_at": "<dynamic>",
    }


def test_point_creation_and_detail_return_the_whole_authorized_record(center: Center) -> None:
    station = center.stations.register(code="A-001", name="装配一号工位")
    host = center.hosts.register(name="推理机-1")
    connector = center.connectors.register(station_id=station.id, host_id=host.id)

    created = center.send(
        "POST",
        POINTS,
        json={
            "station_id": str(station.id),
            "connector_id": str(connector.id),
            "direction": "input",
            "identifier": "DI-01",
            "semantic_label": "工件到位",
        },
    )

    assert created.status_code == 201, created.text
    body = created.json()
    assert _normalize_point(body) == _expected_point(
        station_id=str(station.id),
        connector_id=str(connector.id),
        direction="input",
        identifier="DI-01",
        semantic_label="工件到位",
        status="active",
        revision=1,
    )
    assert center.send("GET", f"{POINTS}/{body['id']}").json() == body


def test_point_listing_edit_status_and_delete_keep_filters_and_revision_contract(
    center: Center,
) -> None:
    station = center.stations.register(code="A-001", name="装配一号工位")
    other_station = center.stations.register(code="B-001", name="装配二号工位")
    host = center.hosts.register(name="推理机-1")
    connector = center.connectors.register(station_id=station.id, host_id=host.id)
    other_connector = center.connectors.register(
        station_id=other_station.id,
        host_id=host.id,
        name="二号连接器",
    )
    first = center.send(
        "POST",
        POINTS,
        json={
            "station_id": str(station.id),
            "connector_id": str(connector.id),
            "direction": "input",
            "identifier": "DI-01",
            "semantic_label": "工件到位",
        },
    ).json()
    center.send(
        "POST",
        POINTS,
        json={
            "station_id": str(other_station.id),
            "connector_id": str(other_connector.id),
            "direction": "output",
            "identifier": "DO-01",
            "semantic_label": "停线联锁",
        },
    )

    listed = center.send(
        "GET",
        POINTS,
        params={"station_id": str(station.id), "connector_id": str(connector.id)},
    )
    assert listed.status_code == 200
    assert listed.json() == {
        "items": [first],
        "page": 1,
        "page_size": 50,
        "total": 1,
    }

    edited = center.send(
        "PATCH",
        f"{POINTS}/{first['id']}",
        json={
            "station_id": str(station.id),
            "connector_id": str(connector.id),
            "direction": "input",
            "identifier": "DI-001",
            "semantic_label": "上料工件到位",
        },
        headers={"If-Match": "1"},
    )
    assert edited.status_code == 200
    assert _normalize_point(edited.json()) == _expected_point(
        station_id=str(station.id),
        connector_id=str(connector.id),
        direction="input",
        identifier="DI-001",
        semantic_label="上料工件到位",
        status="active",
        revision=2,
    )

    stale = center.send(
        "PUT",
        f"{POINTS}/{first['id']}/status",
        json={"status": "deactivated"},
        headers={"If-Match": "1"},
    )
    assert stale.status_code == 409
    assert stale.json()["error_code"] == "STALE_REVISION"

    deactivated = center.send(
        "PUT",
        f"{POINTS}/{first['id']}/status",
        json={"status": "deactivated"},
        headers={"If-Match": "2"},
    )
    assert deactivated.status_code == 200
    assert _normalize_point(deactivated.json()) == _expected_point(
        station_id=str(station.id),
        connector_id=str(connector.id),
        direction="input",
        identifier="DI-001",
        semantic_label="上料工件到位",
        status="deactivated",
        revision=3,
    )

    restored = center.send(
        "PUT",
        f"{POINTS}/{first['id']}/status",
        json={"status": "active"},
        headers={"If-Match": "3"},
    )
    assert restored.status_code == 200
    assert _normalize_point(restored.json()) == _expected_point(
        station_id=str(station.id),
        connector_id=str(connector.id),
        direction="input",
        identifier="DI-001",
        semantic_label="上料工件到位",
        status="active",
        revision=4,
    )

    deleted = center.send(
        "DELETE",
        f"{POINTS}/{first['id']}",
        headers={"If-Match": "4"},
    )
    assert deleted.status_code == 204
    assert center.send("GET", f"{POINTS}/{first['id']}").status_code == 404


def test_connector_capability_is_explicitly_unverified_or_measured_and_revisioned(
    center: Center,
) -> None:
    station = center.stations.register(code="A-001", name="装配一号工位")
    host = center.hosts.register(name="推理机-1")
    connector = center.connectors.register(station_id=station.id, host_id=host.id)

    before = center.send("GET", f"{CONNECTORS}/{connector.id}")
    assert before.status_code == 200
    assert before.json()["capability"] == {"verification": "unverified"}

    measured = {
        "verification": "measured",
        "delivery": "polled",
        "polling_interval_seconds": 0.05,
        "max_delivery_delay_seconds": 0.08,
        "sequencing": "sequenced",
        "edge_preservation": "preserved",
        "timestamp_source": "host_receipt",
    }
    updated = center.send(
        "PUT",
        f"{CONNECTORS}/{connector.id}/capability",
        json=measured,
        headers={"If-Match": "1"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["capability"] == measured
    assert updated.json()["revision"] == 2

    stale = center.send(
        "PUT",
        f"{CONNECTORS}/{connector.id}/capability",
        json={"verification": "unverified"},
        headers={"If-Match": "1"},
    )
    assert stale.status_code == 409
    assert stale.json()["error_code"] == "STALE_REVISION"

    reset = center.send(
        "PUT",
        f"{CONNECTORS}/{connector.id}/capability",
        json={"verification": "unverified"},
        headers={"If-Match": "2"},
    )
    assert reset.status_code == 200
    assert reset.json()["capability"] == {"verification": "unverified"}
    assert reset.json()["revision"] == 3


def test_connector_capability_route_documents_edit_authority_and_if_match(center: Center) -> None:
    schema = center.app.openapi()
    operation = schema["paths"][f"{API_PREFIX}/connectors/{{connector_id}}/capability"]["put"]
    assert operation["x-required-permission"] == Permission.CONNECTOR_EDIT.value
    if_match = next(
        parameter for parameter in operation["parameters"] if parameter["name"] == "If-Match"
    )
    assert if_match["required"] is True
    for response_code in ("401", "403", "404", "409", "422"):
        assert set(operation["responses"][response_code]["content"]) == {PROBLEM_MEDIA_TYPE}
    assert operation["requestBody"]["content"]["application/json"]["schema"]["discriminator"] == {
        "propertyName": "verification",
        "mapping": {
            "unverified": "#/components/schemas/UnverifiedCapabilityDocument",
            "measured": "#/components/schemas/MeasuredCapabilityDocument",
        },
    }
    capability_schemas = str(schema["components"]["schemas"]["MeasuredCapabilityDocument"]) + str(
        schema["components"]["schemas"]["UnverifiedCapabilityDocument"]
    )
    assert "password" not in capability_schemas


def test_capability_wire_refuses_unknown_invalid_and_credential_bearing_documents(
    center: Center,
) -> None:
    station = center.stations.register(code="A-001", name="装配一号工位")
    host = center.hosts.register(name="推理机-1")
    connector = center.connectors.register(station_id=station.id, host_id=host.id)
    invalid_documents = [
        {"verification": "future"},
        {"verification": "unverified", "password": "fixture-secret"},  # pragma: allowlist secret
        {
            "verification": "measured",
            "delivery": "polled",
            "polling_interval_seconds": 0.05,
            "max_delivery_delay_seconds": "0.08",
            "sequencing": "sequenced",
            "edge_preservation": "preserved",
            "timestamp_source": "host_receipt",
        },
        {
            "verification": "measured",
            "delivery": "pushed",
            "polling_interval_seconds": 0.05,
            "max_delivery_delay_seconds": 0.08,
            "sequencing": "sequenced",
            "edge_preservation": "preserved",
            "timestamp_source": "host_receipt",
        },
    ]

    for document in invalid_documents:
        response = center.send(
            "PUT",
            f"{CONNECTORS}/{connector.id}/capability",
            json=document,
            headers={"If-Match": "1"},
        )
        assert response.status_code == 422, response.text
        assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
        assert response.json()["error_code"] == "REQUEST_INVALID"
        assert "fixture-secret" not in response.text

    assert center.connectors.by_id(connector.id) == connector


def test_point_listing_paginates_and_filters_by_each_parent_independently(center: Center) -> None:
    station = center.stations.register(code="A-001", name="装配一号工位")
    other_station = center.stations.register(code="B-001", name="装配二号工位")
    host = center.hosts.register(name="推理机-1")
    first_connector = center.connectors.register(station_id=station.id, host_id=host.id)
    second_connector = center.connectors.register(
        station_id=station.id,
        host_id=host.id,
        name="二号连接器",
    )
    other_connector = center.connectors.register(
        station_id=other_station.id,
        host_id=host.id,
        name="三号连接器",
    )
    first = center.points.register(
        station_id=station.id,
        connector_id=first_connector.id,
        direction=PointDirection.INPUT,
        identifier="DI-01",
        semantic_label="工件到位",
    )
    second = center.points.register(
        station_id=station.id,
        connector_id=second_connector.id,
        direction=PointDirection.OUTPUT,
        identifier="DO-01",
        semantic_label="停线联锁",
    )
    other = center.points.register(
        station_id=other_station.id,
        connector_id=other_connector.id,
        direction=PointDirection.INPUT,
        identifier="DI-02",
        semantic_label="二号到位",
    )

    station_page = center.send(
        "GET",
        POINTS,
        params={"station_id": str(station.id), "page": 2, "page_size": 1},
    )
    assert station_page.status_code == 200
    assert station_page.json()["total"] == 2
    assert len(station_page.json()["items"]) == 1
    assert station_page.json()["items"][0]["station_id"] == str(station.id)

    connector_page = center.send(
        "GET",
        POINTS,
        params={"connector_id": str(first_connector.id)},
    )
    assert connector_page.status_code == 200
    assert connector_page.json() == {
        "items": [
            {
                "id": str(first.id),
                "station_id": str(station.id),
                "connector_id": str(first_connector.id),
                "direction": "input",
                "identifier": "DI-01",
                "semantic_label": "工件到位",
                "status": "active",
                "revision": 1,
                "created_by": str(first.created_by),
                "updated_by": str(first.updated_by),
                "created_at": first.created_at.isoformat().replace("+00:00", "Z"),
                "updated_at": first.updated_at.isoformat().replace("+00:00", "Z"),
            }
        ],
        "page": 1,
        "page_size": 50,
        "total": 1,
    }

    both = center.send(
        "GET",
        POINTS,
        params={
            "station_id": str(station.id),
            "connector_id": str(second_connector.id),
        },
    )
    assert both.status_code == 200
    assert [item["id"] for item in both.json()["items"]] == [str(second.id)]
    assert str(other.id) not in {item["id"] for item in both.json()["items"]}


def test_point_detail_and_http_validation_failures_use_problem_json(center: Center) -> None:
    missing = center.send("GET", f"{POINTS}/00000000-0000-0000-0000-000000000001")
    assert missing.status_code == 404
    assert missing.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert missing.json()["error_code"] == "POINT_NOT_FOUND"

    malformed = center.send("GET", f"{POINTS}/not-a-uuid")
    assert malformed.status_code == 422
    assert malformed.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert malformed.json()["error_code"] == "REQUEST_INVALID"

    station = center.stations.register(code="A-001", name="装配一号工位")
    host = center.hosts.register(name="推理机-1")
    connector = center.connectors.register(station_id=station.id, host_id=host.id)
    invalid = center.send(
        "POST",
        POINTS,
        json={
            "station_id": str(station.id),
            "connector_id": str(connector.id),
            "direction": "input",
            "identifier": "DI-01",
            "semantic_label": "工件到位",
            "unexpected": "拒绝",
        },
    )
    assert invalid.status_code == 422
    assert invalid.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert invalid.json()["error_code"] == "REQUEST_INVALID"
    assert invalid.json()["field_errors"][0]["field"] == "unexpected"


def test_point_routes_document_independent_permissions_and_revision_preconditions(
    center: Center,
) -> None:
    schema = center.app.openapi()
    declared = {
        f"{method.upper()} {path.removeprefix(API_PREFIX)}": operation["x-required-permission"]
        for path, item in schema["paths"].items()
        if "/points" in path
        for method, operation in item.items()
        if method in {"get", "post", "patch", "put", "delete"}
    }
    assert declared == {
        "POST /points": Permission.POINT_EDIT.value,
        "GET /points": Permission.POINT_VIEW.value,
        "GET /points/{point_id}": Permission.POINT_VIEW.value,
        "PATCH /points/{point_id}": Permission.POINT_EDIT.value,
        "PUT /points/{point_id}/status": Permission.POINT_EDIT.value,
        "DELETE /points/{point_id}": Permission.POINT_DELETE.value,
    }
    for method, path in (
        ("patch", f"{API_PREFIX}/points/{{point_id}}"),
        ("put", f"{API_PREFIX}/points/{{point_id}}/status"),
        ("delete", f"{API_PREFIX}/points/{{point_id}}"),
    ):
        operation = schema["paths"][path][method]
        if_match = next(
            parameter for parameter in operation["parameters"] if parameter["name"] == "If-Match"
        )
        assert if_match["required"] is True
        assert "409" in operation["responses"]
        assert set(operation["responses"]["409"]["content"]) == {PROBLEM_MEDIA_TYPE}


def test_point_view_edit_and_delete_permissions_are_independent(center: Center) -> None:
    station = center.stations.register(code="A-001", name="装配一号工位")
    host = center.hosts.register(name="推理机-1")
    connector = center.connectors.register(station_id=station.id, host_id=host.id)
    point = center.points.register(
        station_id=station.id,
        connector_id=connector.id,
        direction=PointDirection.INPUT,
        identifier="DI-01",
        semantic_label="工件到位",
    )

    center.granted = frozenset({Permission.POINT_VIEW})
    assert center.send("GET", f"{POINTS}/{point.id}").status_code == 200
    denied_edit = center.send(
        "PATCH",
        f"{POINTS}/{point.id}",
        json={
            "station_id": str(station.id),
            "connector_id": str(connector.id),
            "direction": "input",
            "identifier": "DI-02",
            "semantic_label": "改名",
        },
        headers={"If-Match": "1"},
    )
    assert denied_edit.status_code == 403
    assert denied_edit.json()["error_code"] == "PERMISSION_DENIED"

    center.granted = frozenset({Permission.POINT_EDIT})
    edited = center.send(
        "PATCH",
        f"{POINTS}/{point.id}",
        json={
            "station_id": str(station.id),
            "connector_id": str(connector.id),
            "direction": "input",
            "identifier": "DI-02",
            "semantic_label": "改名",
        },
        headers={"If-Match": "1"},
    )
    assert edited.status_code == 200
    denied_view = center.send("GET", f"{POINTS}/{point.id}")
    assert denied_view.status_code == 403
    assert denied_view.json()["error_code"] == "PERMISSION_DENIED"

    center.granted = frozenset({Permission.POINT_DELETE})
    deleted = center.send(
        "DELETE",
        f"{POINTS}/{point.id}",
        headers={"If-Match": str(edited.json()["revision"])},
    )
    assert deleted.status_code == 204
    denied_after_delete = center.send("GET", f"{POINTS}/{point.id}")
    assert denied_after_delete.status_code == 403
    assert denied_after_delete.json()["error_code"] == "PERMISSION_DENIED"


def test_binding_validation_route_returns_stable_reasons_without_connectors_for_actions(
    center: Center,
) -> None:
    station = center.stations.register(code="EMPTY", name="无外部信号工位")

    action = center.send(
        "POST",
        VALIDATIONS,
        json={
            "station_id": str(station.id),
            "role": PointRole.START_SIGNAL.value,
            "budget_seconds": 0.2,
        },
    )
    assert action.status_code == 200
    assert action.json() == {"accepted": True, "reasons": []}

    safety = center.send(
        "POST",
        VALIDATIONS,
        json={
            "station_id": str(station.id),
            "role": PointRole.SAFETY_OUTPUT.value,
            "budget_seconds": 0.2,
        },
    )
    assert safety.status_code == 200
    assert safety.json() == {
        "accepted": False,
        "reasons": [
            {
                "code": "point_required",
                "field": "point_id",
                "message": "安全输出必须选择一个输出点位",
            }
        ],
    }

    missing_budget = center.send(
        "POST",
        VALIDATIONS,
        json={"station_id": str(station.id), "role": PointRole.START_SIGNAL.value},
    )
    assert missing_budget.status_code == 422
    assert missing_budget.json()["error_code"] == "REQUEST_INVALID"
    assert any(
        error["field"] == "budget_seconds" for error in missing_budget.json()["field_errors"]
    )


def test_binding_validation_route_covers_topology_status_and_capability_reasons(
    center: Center,
) -> None:
    station = center.stations.register(code="A-001", name="装配一号工位")
    other_station = center.stations.register(code="B-001", name="装配二号工位")
    host = center.hosts.register(name="推理机-1")
    connector = center.connectors.register(station_id=station.id, host_id=host.id)
    other_connector = center.connectors.register(
        station_id=other_station.id,
        host_id=host.id,
        name="二号连接器",
    )
    input_point = center.points.register(
        station_id=station.id,
        connector_id=connector.id,
        direction=PointDirection.INPUT,
        identifier="DI-01",
        semantic_label="工件到位",
    )
    output_point = center.points.register(
        station_id=station.id,
        connector_id=connector.id,
        direction=PointDirection.OUTPUT,
        identifier="DO-01",
        semantic_label="停线联锁",
    )
    other_point = center.points.register(
        station_id=other_station.id,
        connector_id=other_connector.id,
        direction=PointDirection.INPUT,
        identifier="DI-02",
        semantic_label="二号到位",
    )

    cross_station = center.send(
        "POST",
        VALIDATIONS,
        json={
            "station_id": str(station.id),
            "point_id": str(other_point.id),
            "role": PointRole.START_SIGNAL.value,
            "budget_seconds": 0.2,
        },
    )
    assert cross_station.status_code == 200
    assert cross_station.json()["reasons"] == [
        {
            "code": "point_station_mismatch",
            "field": "station_id",
            "message": "所选点位属于其他工位",
        }
    ]

    wrong_direction = center.send(
        "POST",
        VALIDATIONS,
        json={
            "station_id": str(station.id),
            "point_id": str(output_point.id),
            "role": PointRole.START_SIGNAL.value,
            "budget_seconds": 0.2,
        },
    )
    assert wrong_direction.status_code == 200
    assert wrong_direction.json()["reasons"][0]["code"] == "wrong_direction"

    inactive_point = replace(center.points.rows[input_point.id], status=DeviceStatus.DEACTIVATED)
    center.points.rows[input_point.id] = inactive_point
    point_status = center.send(
        "POST",
        VALIDATIONS,
        json={
            "station_id": str(station.id),
            "point_id": str(input_point.id),
            "role": PointRole.START_SIGNAL.value,
            "budget_seconds": 0.2,
        },
    )
    assert point_status.status_code == 200
    assert point_status.json()["reasons"][0]["code"] == "point_deactivated"
    center.points.rows[input_point.id] = replace(input_point, status=DeviceStatus.ACTIVE)

    inactive_station = center.stations.register(
        code="INACTIVE",
        name="停用工位",
        status=DeviceStatus.DEACTIVATED,
    )
    station_status = center.send(
        "POST",
        VALIDATIONS,
        json={
            "station_id": str(inactive_station.id),
            "role": PointRole.START_SIGNAL.value,
            "budget_seconds": 0.2,
        },
    )
    assert station_status.status_code == 409
    assert station_status.json()["error_code"] == "STATION_DEACTIVATED"

    center.connectors.rows[connector.id] = replace(connector, status=DeviceStatus.DEACTIVATED)
    connector_status = center.send(
        "POST",
        VALIDATIONS,
        json={
            "station_id": str(station.id),
            "point_id": str(input_point.id),
            "role": PointRole.START_SIGNAL.value,
            "budget_seconds": 0.2,
        },
    )
    assert connector_status.status_code == 200
    assert connector_status.json()["reasons"][0]["code"] == "connector_deactivated"
    center.connectors.rows[connector.id] = connector

    unverified = center.send(
        "POST",
        VALIDATIONS,
        json={
            "station_id": str(station.id),
            "point_id": str(input_point.id),
            "role": PointRole.START_SIGNAL.value,
            "budget_seconds": 0.2,
        },
    )
    assert unverified.status_code == 200
    assert unverified.json()["reasons"] == [
        {
            "code": "capability_unverified",
            "field": "capability.verification",
            "message": "连接器能力尚未实测验证",
        }
    ]

    center.connectors.rows[connector.id] = replace(
        connector,
        capability=Measured(
            delivery=Polled(interval=0.05),
            max_delivery_delay=0.3,
            sequencing=Sequencing.UNSEQUENCED,
            edges=EdgePreservation.MAY_DROP,
            timestamps=TimestampSource.HOST_RECEIPT,
        ),
    )
    all_unfit = center.send(
        "POST",
        VALIDATIONS,
        json={
            "station_id": str(station.id),
            "point_id": str(input_point.id),
            "role": PointRole.ORDERED_STEP.value,
            "budget_seconds": 0.2,
        },
    )
    assert all_unfit.status_code == 200
    assert all_unfit.json()["reasons"] == [
        {
            "code": "may_drop_edges",
            "field": "capability.edge_preservation",
            "message": "连接器可能丢失瞬时边沿",
        },
        {
            "code": "not_sequenced",
            "field": "capability.sequencing",
            "message": "连接器不能保证点位变化顺序",
        },
        {
            "code": "delivery_too_slow",
            "field": "capability.max_delivery_delay_seconds",
            "message": "连接器最大投递延迟 0.3 秒超出角色预算 0.2 秒",
        },
    ]


def test_binding_validation_route_accepts_valid_start_end_and_safety_roles(
    center: Center,
) -> None:
    station = center.stations.register(code="A-001", name="装配一号工位")
    host = center.hosts.register(name="推理机-1")
    connector = center.connectors.register(station_id=station.id, host_id=host.id)
    center.connectors.rows[connector.id] = replace(
        connector,
        capability=Measured(
            delivery=Polled(interval=0.05),
            max_delivery_delay=0.08,
            sequencing=Sequencing.SEQUENCED,
            edges=EdgePreservation.PRESERVED,
            timestamps=TimestampSource.HOST_RECEIPT,
        ),
    )
    input_point = center.points.register(
        station_id=station.id,
        connector_id=connector.id,
        direction=PointDirection.INPUT,
        identifier="DI-01",
        semantic_label="工件到位",
    )
    output_point = center.points.register(
        station_id=station.id,
        connector_id=connector.id,
        direction=PointDirection.OUTPUT,
        identifier="DO-01",
        semantic_label="停线联锁",
    )

    for role, point_id in (
        (PointRole.START_SIGNAL, input_point.id),
        (PointRole.END_SIGNAL, input_point.id),
        (PointRole.SAFETY_OUTPUT, output_point.id),
    ):
        response = center.send(
            "POST",
            VALIDATIONS,
            json={
                "station_id": str(station.id),
                "point_id": str(point_id),
                "role": role.value,
                "budget_seconds": 0.2,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"accepted": True, "reasons": []}


def test_binding_validation_documents_view_permission_and_problem_responses(center: Center) -> None:
    schema = center.app.openapi()
    operation = schema["paths"][f"{API_PREFIX}/point-binding-validations"]["post"]
    assert operation["x-required-permission"] == Permission.POINT_VIEW.value
    assert set(operation["responses"]["422"]["content"]) == {PROBLEM_MEDIA_TYPE}
    assert set(operation["responses"]["404"]["content"]) == {PROBLEM_MEDIA_TYPE}
    assert set(operation["responses"]["409"]["content"]) == {PROBLEM_MEDIA_TYPE}
