"""工位和相机管理的 HTTP 合约。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest
from auth_fakes import FakeRoles, FakeSessions, FakeUsers
from device_fakes import (
    FakeCameras,
    FakeConnectors,
    FakeInferenceBackends,
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
from factory_sop.device.errors import DeviceRefusalCode, DeviceRefusedError
from factory_sop.device.model import Camera
from factory_sop.problem import PROBLEM_MEDIA_TYPE
from factory_sop.settings import Settings

STATIONS = f"{API_PREFIX}/stations"
CAMERAS = f"{API_PREFIX}/cameras"
CREDENTIALS = {  # pragma: allowlist secret
    "login_name": "chen.wei",
    "password": "assembly-line-4",  # pragma: allowlist secret
}
A_STATION = {"code": "A-001", "name": "装配一号工位", "tags": ["装配", "一线"]}
ALL_DEVICE_PERMISSIONS = frozenset(
    {
        Permission.INFERENCE_HOST_VIEW,
        Permission.INFERENCE_HOST_EDIT,
        Permission.INFERENCE_HOST_DELETE,
        Permission.INFERENCE_BACKEND_VIEW,
        Permission.INFERENCE_BACKEND_EDIT,
        Permission.INFERENCE_BACKEND_DELETE,
        Permission.STATION_VIEW,
        Permission.STATION_EDIT,
        Permission.STATION_DELETE,
        Permission.CAMERA_VIEW,
        Permission.CAMERA_EDIT,
        Permission.CAMERA_DELETE,
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


class TriggeredCameraWrite(FakeCameras):
    """HTTP 适配器仓储接口处的数据库风格拓扑拒绝。"""

    def add(self, camera: Camera) -> None:
        raise DeviceRefusedError(DeviceRefusalCode.CAMERA_HOST_BACKEND_MISMATCH)


class RefusingStations(FakeInferenceStations):
    def __init__(self, refusal: DeviceRefusalCode) -> None:
        super().__init__()
        self.refusal = refusal

    def remove(self, station_id: UUID, *, expected_revision: int) -> bool:
        raise DeviceRefusedError(self.refusal)


class RefusingInferenceBackends(FakeInferenceBackends):
    def __init__(self, refusal: DeviceRefusalCode) -> None:
        super().__init__()
        self.refusal = refusal

    def remove(self, backend_id: UUID, *, expected_revision: int) -> bool:
        raise DeviceRefusedError(self.refusal)


class RefusingInferenceHosts(FakeInferenceHosts):
    def __init__(self, refusal: DeviceRefusalCode) -> None:
        super().__init__()
        self.refusal = refusal

    def remove(self, host_id: UUID, *, expected_revision: int) -> bool:
        raise DeviceRefusedError(self.refusal)


class Center:
    """已登录的应用；每个仓储都在其公开接口处被替换。"""

    def __init__(self) -> None:
        self.users = FakeUsers()
        self.sessions = FakeSessions()
        self.roles = FakeRoles(users=self.users)
        self.hosts = FakeInferenceHosts()
        self.backends = FakeInferenceBackends()
        self.stations = FakeInferenceStations()
        self.cameras = FakeCameras()
        self.connectors = FakeConnectors()
        self.points = FakePoints()
        self.granted = ALL_DEVICE_PERMISSIONS
        self.app = create_app(settings())
        self.app.dependency_overrides[auth_dependencies.users] = lambda: self.users
        self.app.dependency_overrides[auth_dependencies.sessions] = lambda: self.sessions
        self.app.dependency_overrides[auth_dependencies.roles] = lambda: self.roles
        self.app.dependency_overrides[auth_dependencies.granted_permissions] = lambda: self.granted
        self.app.dependency_overrides[device_dependencies.hosts] = lambda: self.hosts
        self.app.dependency_overrides[device_dependencies.backends] = lambda: self.backends
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
        **kwargs: Any,  # noqa: ANN401 — 传递给 HTTP 客户端
    ) -> HttpResponse:
        injected = dict(headers or {})
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            injected[CSRF_HEADER] = self.client.cookies[CSRF_COOKIE]
        return self.client.request(method, path, headers=injected, **kwargs)

    def create_host_and_backend(self, *, name: str = "推理机-1") -> tuple[str, str]:
        host = self.hosts.register(name=name)
        backend = self.backends.register(
            host_id=host.id, base_url=f"http://10.0.8.{len(self.hosts.rows) + 10}:8000"
        )
        return str(host.id), str(backend.id)

    def create_station(self, *, code: str = "A-001") -> str:
        response = self.send("POST", STATIONS, json=A_STATION | {"code": code})
        assert response.status_code == 201, response.text
        return str(response.json()["id"])

    def create_camera(self, station_id: str, host_id: str, backend_id: str) -> str:
        response = self.send(
            "POST",
            CAMERAS,
            json={
                "name": "一号相机",
                "address": "10.0.8.21",
                "main_stream_path": "/Streaming/Channels/101",
                "sub_stream_path": "/Streaming/Channels/102",
                "station_id": station_id,
                "host_id": host_id,
                "backend_id": backend_id,
            },
        )
        assert response.status_code == 201, response.text
        return str(response.json()["id"])


@pytest.fixture
def center() -> Center:
    return Center()


@pytest.mark.parametrize(
    ("resource", "refusal"),
    [
        ("station", DeviceRefusalCode.STATION_HAS_TEMPLATE_BINDING),
        ("backend", DeviceRefusalCode.INFERENCE_BACKEND_HAS_CONFIGURATION_REPORT),
        ("host", DeviceRefusalCode.INFERENCE_HOST_HAS_CONFIGURATION_REPORT),
    ],
)
def test_template_history_delete_refusals_are_stable_problem_responses(
    center: Center, resource: str, refusal: DeviceRefusalCode
) -> None:
    if resource == "station":
        center.stations = RefusingStations(refusal)
        station_id = center.stations.register(code="history-station", name="历史工位").id
        path = f"{STATIONS}/{station_id}"
    elif resource == "backend":
        center.backends = RefusingInferenceBackends(refusal)
        host = center.hosts.register(name="历史后端主机")
        backend_id = center.backends.register(host_id=host.id, base_url="http://10.0.8.90:8000").id
        path = f"{API_PREFIX}/inference-backends/{backend_id}"
    else:
        center.hosts = RefusingInferenceHosts(refusal)
        host_id = center.hosts.register(name="历史报告主机").id
        path = f"{API_PREFIX}/inference-hosts/{host_id}"

    response = center.send("DELETE", path, headers={"If-Match": "1"})

    assert response.status_code == 409
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.json()["error_code"] == refusal.value


def _normalize_dto(body: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(body)
    for field in ("id", "created_by", "updated_by", "created_at", "updated_at"):
        normalized[field] = "<dynamic>"
    return normalized


def test_station_and_camera_are_saved_offline_with_streams_and_status_only_credentials(
    center: Center,
) -> None:
    host_id, backend_id = center.create_host_and_backend()
    station_id = center.create_station()
    camera_id = center.create_camera(station_id, host_id, backend_id)

    station = center.send("GET", f"{STATIONS}/{station_id}")
    camera = center.send("GET", f"{CAMERAS}/{camera_id}")

    assert station.status_code == camera.status_code == 200
    assert _normalize_dto(station.json()) == {
        "id": "<dynamic>",
        "code": "A-001",
        "name": "装配一号工位",
        "tags": ["装配", "一线"],
        "status": "active",
        "revision": 1,
        "created_by": "<dynamic>",
        "updated_by": "<dynamic>",
        "created_at": "<dynamic>",
        "updated_at": "<dynamic>",
    }
    assert _normalize_dto(camera.json()) == {
        "id": "<dynamic>",
        "name": "一号相机",
        "address": "10.0.8.21",
        "main_stream_path": "/Streaming/Channels/101",
        "sub_stream_path": "/Streaming/Channels/102",
        "credentials_configured": False,
        "station_id": station_id,
        "host_id": host_id,
        "backend_id": backend_id,
        "status": "active",
        "revision": 1,
        "created_by": "<dynamic>",
        "updated_by": "<dynamic>",
        "created_at": "<dynamic>",
        "updated_at": "<dynamic>",
    }
    assert "password" not in camera.text.lower()


def test_station_code_is_unique_and_lists_include_deactivated_history(center: Center) -> None:
    station_id = center.create_station()
    duplicate = center.send("POST", STATIONS, json=A_STATION)
    assert duplicate.status_code == 409
    assert duplicate.json()["error_code"] == "STATION_CODE_TAKEN"

    deactivated = center.send(
        "PUT",
        f"{STATIONS}/{station_id}/status",
        json={"status": "deactivated"},
        headers={"If-Match": "1"},
    )
    assert deactivated.status_code == 200
    assert center.send("GET", STATIONS).json()["items"][0]["status"] == "deactivated"
    restored = center.send(
        "PUT",
        f"{STATIONS}/{station_id}/status",
        json={"status": "active"},
        headers={"If-Match": "2"},
    )
    assert restored.status_code == 200
    assert restored.json()["revision"] == 3


def test_camera_topology_conflicts_return_field_errors(center: Center) -> None:
    host_a, backend_a = center.create_host_and_backend()
    host_b, backend_b = center.create_host_and_backend(name="推理机-2")
    center.backends.rows[UUID(backend_a)] = replace(
        center.backends.rows[UUID(backend_a)], template_version_id=UUID(int=101)
    )
    center.backends.rows[UUID(backend_b)] = replace(
        center.backends.rows[UUID(backend_b)], template_version_id=UUID(int=101)
    )
    station_id = center.create_station()
    center.create_camera(station_id, host_a, backend_a)

    cross_host = center.send(
        "POST",
        CAMERAS,
        json={
            "name": "跨机相机",
            "address": "10.0.8.22",
            "main_stream_path": "/Streaming/Channels/101",
            "sub_stream_path": "/Streaming/Channels/102",
            "station_id": station_id,
            "host_id": host_b,
            "backend_id": backend_b,
        },
    )
    assert cross_host.status_code == 409
    assert cross_host.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert cross_host.json()["error_code"] == "CAMERA_STATION_HOST_CONFLICT"
    assert cross_host.json()["field_errors"] == [
        {"field": "host_id", "message": "同一工位的相机必须属于同一推理机"}
    ]

    backend_c = center.backends.register(
        host_id=UUID(host_a),
        base_url="http://10.0.8.11:8001",
        template_version_id=UUID(int=202),
    )
    conflict = center.send(
        "POST",
        CAMERAS,
        json={
            "name": "异模板相机",
            "address": "10.0.8.23",
            "main_stream_path": "/Streaming/Channels/101",
            "sub_stream_path": "/Streaming/Channels/102",
            "station_id": station_id,
            "host_id": host_a,
            "backend_id": str(backend_c.id),
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["error_code"] == "CAMERA_STATION_TEMPLATE_CONFLICT"
    assert conflict.json()["field_errors"][0]["field"] == "backend_id"


def test_camera_lifecycle_distinguishes_deactivation_from_delete_and_station_delete(
    center: Center,
) -> None:
    host_id, backend_id = center.create_host_and_backend()
    station_id = center.create_station()
    camera_id = center.create_camera(station_id, host_id, backend_id)

    deactivated = center.send(
        "PUT",
        f"{CAMERAS}/{camera_id}/status",
        json={"status": "deactivated"},
        headers={"If-Match": "1"},
    )
    assert deactivated.status_code == 200
    assert center.send("GET", f"{CAMERAS}/{camera_id}").json()["status"] == "deactivated"
    station_delete = center.send("DELETE", f"{STATIONS}/{station_id}", headers={"If-Match": "1"})
    assert station_delete.status_code == 409
    assert station_delete.json()["error_code"] == "STATION_HAS_CAMERAS"

    restored = center.send(
        "PUT",
        f"{CAMERAS}/{camera_id}/status",
        json={"status": "active"},
        headers={"If-Match": "2"},
    )
    assert restored.status_code == 200
    assert (
        center.send("DELETE", f"{CAMERAS}/{camera_id}", headers={"If-Match": "3"}).status_code
        == 204
    )
    assert center.send("GET", f"{CAMERAS}/{camera_id}").status_code == 404

    assert (
        center.send("DELETE", f"{STATIONS}/{station_id}", headers={"If-Match": "1"}).status_code
        == 204
    )


def test_camera_stream_fields_reject_embedded_credentials_without_echoing_them(
    center: Center,
) -> None:
    host_id, backend_id = center.create_host_and_backend()
    station_id = center.create_station()
    response = center.send(
        "POST",
        CAMERAS,
        json={
            "name": "含凭据相机",
            "address": "rtsp://user:secret@10.0.8.21",  # pragma: allowlist secret
            "main_stream_path": "/Streaming/Channels/101",
            "sub_stream_path": "/Streaming/Channels/102",
            "station_id": station_id,
            "host_id": host_id,
            "backend_id": backend_id,
        },
    )
    assert response.status_code == 422
    assert response.json()["error_code"] == "REQUEST_INVALID"
    assert response.json()["field_errors"][0]["field"] == "address"
    assert "secret" not in response.text  # pragma: allowlist secret


def test_camera_routes_declare_permissions_and_if_match(center: Center) -> None:
    schema = center.app.openapi()
    declared = {
        f"{method.upper()} {path.removeprefix(API_PREFIX)}": operation["x-required-permission"]
        for path, item in schema["paths"].items()
        if path.startswith(f"{API_PREFIX}/stations") or path.startswith(f"{API_PREFIX}/cameras")
        for method, operation in item.items()
        if method in {"post", "patch", "put", "delete"}
    }
    assert declared == {
        "POST /stations": Permission.STATION_EDIT.value,
        "PATCH /stations/{station_id}": Permission.STATION_EDIT.value,
        "PUT /stations/{station_id}/status": Permission.STATION_EDIT.value,
        "DELETE /stations/{station_id}": Permission.STATION_DELETE.value,
        "POST /cameras": Permission.CAMERA_EDIT.value,
        "PATCH /cameras/{camera_id}": Permission.CAMERA_EDIT.value,
        "PUT /cameras/{camera_id}/status": Permission.CAMERA_EDIT.value,
        "DELETE /cameras/{camera_id}": Permission.CAMERA_DELETE.value,
    }
    method_paths = (
        ("PATCH", "/api/v1/stations/{station_id}"),
        ("PUT", "/api/v1/stations/{station_id}/status"),
        ("DELETE", "/api/v1/stations/{station_id}"),
        ("PATCH", "/api/v1/cameras/{camera_id}"),
        ("PUT", "/api/v1/cameras/{camera_id}/status"),
        ("DELETE", "/api/v1/cameras/{camera_id}"),
    )
    for method, path in method_paths:
        operation = schema["paths"][path][method.lower()]
        assert any(parameter["name"] == "If-Match" for parameter in operation["parameters"])


def test_station_and_camera_read_routes_declare_view_permissions(center: Center) -> None:
    schema = center.app.openapi()
    assert {
        (method.upper(), path): operation["x-required-permission"]
        for path, item in schema["paths"].items()
        if path.startswith(f"{API_PREFIX}/stations") or path.startswith(f"{API_PREFIX}/cameras")
        for method, operation in item.items()
        if method in {"get"}
    } == {
        ("GET", "/api/v1/stations"): Permission.STATION_VIEW.value,
        ("GET", "/api/v1/stations/{station_id}"): Permission.STATION_VIEW.value,
        ("GET", "/api/v1/cameras"): Permission.CAMERA_VIEW.value,
        ("GET", "/api/v1/cameras/{camera_id}"): Permission.CAMERA_VIEW.value,
    }


def test_station_and_camera_problem_responses_match_the_operation_kind(center: Center) -> None:
    schema = center.app.openapi()

    for path in ("/api/v1/stations", "/api/v1/cameras"):
        assert set(schema["paths"][path]["get"]["responses"]) == {
            "200",
            "401",
            "403",
            "422",
            "500",
        }
    for path in ("/api/v1/stations/{station_id}", "/api/v1/cameras/{camera_id}"):
        assert set(schema["paths"][path]["get"]["responses"]) == {
            "200",
            "401",
            "403",
            "404",
            "422",
            "500",
        }

    expected_mutating = {
        ("post", "/api/v1/stations"): {"201", "401", "403", "409", "422", "500"},
        ("patch", "/api/v1/stations/{station_id}"): {
            "200",
            "401",
            "403",
            "404",
            "409",
            "422",
            "500",
        },
        ("put", "/api/v1/stations/{station_id}/status"): {
            "200",
            "401",
            "403",
            "404",
            "409",
            "422",
            "500",
        },
        ("delete", "/api/v1/stations/{station_id}"): {
            "204",
            "401",
            "403",
            "404",
            "409",
            "422",
            "500",
        },
        ("post", "/api/v1/cameras"): {"201", "401", "403", "404", "409", "422", "500"},
        ("patch", "/api/v1/cameras/{camera_id}"): {
            "200",
            "401",
            "403",
            "404",
            "409",
            "422",
            "500",
        },
        ("put", "/api/v1/cameras/{camera_id}/status"): {
            "200",
            "401",
            "403",
            "404",
            "409",
            "422",
            "500",
        },
        ("delete", "/api/v1/cameras/{camera_id}"): {
            "204",
            "401",
            "403",
            "404",
            "409",
            "422",
            "500",
        },
    }
    for (method, path), expected in expected_mutating.items():
        assert set(schema["paths"][path][method]["responses"]) == expected


def test_database_style_trigger_refusal_reaches_http_with_field_errors(center: Center) -> None:
    host_id, backend_id = center.create_host_and_backend()
    station_id = center.create_station()
    center.cameras = TriggeredCameraWrite()

    response = center.send(
        "POST",
        CAMERAS,
        json={
            "name": "触发器拒绝相机",
            "address": "10.0.8.21",
            "main_stream_path": "/Streaming/Channels/101",
            "sub_stream_path": "/Streaming/Channels/102",
            "station_id": station_id,
            "host_id": host_id,
            "backend_id": backend_id,
        },
    )

    assert response.status_code == 409
    assert response.json()["field_errors"] == [
        {"field": "host_id", "message": "必须与推理后端所属推理机一致"},
        {"field": "backend_id", "message": "必须属于所选推理机"},
    ]
