"""SYS-25-01 — 通过已发布控制平面配置工位和相机。

前置条件：设备管理员已登录。
操作：离线保存工位和相机，修改其可逆状态，并验证拓扑拒绝。
观察：可见视频流路径和凭据配置状态，但不可见凭据；停用保留关联，删除是独立操作。
"""

from __future__ import annotations

from conftest import BootstrapCommand, csrf_header, log_in
from httpx2 import Client

HOSTS = "/api/v1/inference-hosts"
BACKENDS = "/api/v1/inference-backends"
STATIONS = "/api/v1/stations"
CAMERAS = "/api/v1/cameras"
A_HOST = {
    "name": "装配A线-推理机1",
    "address": "10.0.8.11",
    "mediamtx_address": None,
    "recording_window_seconds": 604800,
    "disk_watermark_percent": 85,
}
CAMERA = {
    "name": "一号相机",
    "address": "10.0.8.21",
    "main_stream_path": "/Streaming/Channels/101",
    "sub_stream_path": "/Streaming/Channels/102",
}


def _normalize_dto(body: dict[str, object]) -> dict[str, object]:
    normalized = dict(body)
    for field in ("id", "created_by", "updated_by", "created_at", "updated_at"):
        normalized[field] = "<dynamic>"
    return normalized


def test_station_and_camera_lifecycle_and_topology(
    client: Client, bootstrap: BootstrapCommand
) -> None:
    log_in(client, bootstrap)
    headers = csrf_header(client)
    host = client.post(HOSTS, json=A_HOST, headers=headers)
    assert host.status_code == 201, host.text
    host_id = host.json()["id"]
    backend = client.post(
        BACKENDS,
        json={"host_id": host_id, "base_url": "http://10.0.8.11:8000"},
        headers=headers,
    )
    assert backend.status_code == 201, backend.text
    backend_id = backend.json()["id"]

    station = client.post(
        STATIONS,
        json={"code": "A-001", "name": "装配一号工位", "tags": ["装配", "一线"]},
        headers=headers,
    )
    assert station.status_code == 201, station.text
    station_id = station.json()["id"]
    camera = client.post(
        CAMERAS,
        json=CAMERA | {"station_id": station_id, "host_id": host_id, "backend_id": backend_id},
        headers=headers,
    )
    assert camera.status_code == 201, camera.text
    camera_id = camera.json()["id"]
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

    deactivated = client.put(
        f"{CAMERAS}/{camera_id}/status",
        json={"status": "deactivated"},
        headers={**headers, "If-Match": "1"},
    )
    assert deactivated.status_code == 200
    assert client.get(f"{CAMERAS}/{camera_id}").json()["station_id"] == station_id
    restored = client.put(
        f"{CAMERAS}/{camera_id}/status",
        json={"status": "active"},
        headers={**headers, "If-Match": str(deactivated.json()["revision"])},
    )
    assert restored.status_code == 200

    station_deactivated = client.put(
        f"{STATIONS}/{station_id}/status",
        json={"status": "deactivated"},
        headers={**headers, "If-Match": "1"},
    )
    assert station_deactivated.status_code == 200
    refused = client.post(
        CAMERAS,
        json=CAMERA
        | {
            "name": "停用工位相机",
            "station_id": station_id,
            "host_id": host_id,
            "backend_id": backend_id,
        },
        headers=headers,
    )
    assert refused.status_code == 409
    assert refused.json()["error_code"] == "STATION_DEACTIVATED"

    restored_station = client.put(
        f"{STATIONS}/{station_id}/status",
        json={"status": "active"},
        headers={**headers, "If-Match": str(station_deactivated.json()["revision"])},
    )
    assert restored_station.status_code == 200
    station_delete = client.delete(
        f"{STATIONS}/{station_id}",
        headers={**headers, "If-Match": str(restored_station.json()["revision"])},
    )
    assert station_delete.status_code == 409
    assert station_delete.json()["error_code"] == "STATION_HAS_CAMERAS"

    deleted_camera = client.delete(
        f"{CAMERAS}/{camera_id}",
        headers={**headers, "If-Match": str(restored.json()["revision"])},
    )
    assert deleted_camera.status_code == 204
    deleted_station = client.delete(
        f"{STATIONS}/{station_id}",
        headers={**headers, "If-Match": str(restored_station.json()["revision"])},
    )
    assert deleted_station.status_code == 204


def test_camera_refuses_a_cross_host_backend_binding(
    client: Client, bootstrap: BootstrapCommand
) -> None:
    log_in(client, bootstrap)
    headers = csrf_header(client)
    first = client.post(HOSTS, json=A_HOST, headers=headers).json()["id"]
    second = client.post(
        HOSTS,
        json=A_HOST | {"name": "装配A线-推理机2", "address": "10.0.8.12"},
        headers=headers,
    ).json()["id"]
    backend = client.post(
        BACKENDS,
        json={"host_id": first, "base_url": "http://10.0.8.11:8000"},
        headers=headers,
    ).json()["id"]
    station = client.post(
        STATIONS,
        json={"code": "A-001", "name": "装配一号工位", "tags": []},
        headers=headers,
    ).json()["id"]

    refused = client.post(
        CAMERAS,
        json=CAMERA | {"station_id": station, "host_id": second, "backend_id": backend},
        headers=headers,
    )
    assert refused.status_code == 409
    assert refused.json()["error_code"] == "CAMERA_HOST_BACKEND_MISMATCH"
    assert {item["field"] for item in refused.json()["field_errors"]} == {
        "host_id",
        "backend_id",
    }
