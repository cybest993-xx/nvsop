"""SYS-24-05 — 连接器配置走真实 HTTP，凭据只显示状态。"""

from __future__ import annotations

from conftest import BootstrapCommand, csrf_header, log_in
from httpx2 import Client

HOSTS = "/api/v1/inference-hosts"
STATIONS = "/api/v1/stations"
CONNECTORS = "/api/v1/connectors"
A_HOST = {
    "name": "装配A线-推理机1",
    "address": "10.0.8.11",
    "mediamtx_address": None,
    "recording_window_seconds": 7 * 24 * 3600,
    "disk_watermark_percent": 85,
}
A_STATION = {"code": "A-001", "name": "装配一号工位", "tags": []}
A_CONFIGURATION = {
    "name": "一号连接器",
    "connector_type": "hikvision_isapi",
    "configuration": {"address": "10.0.8.21", "port": 80},
}


def _create_host_and_station(client: Client, headers: dict[str, str]) -> tuple[str, str]:
    host = client.post(HOSTS, json=A_HOST, headers=headers)
    assert host.status_code == 201, host.text
    station = client.post(STATIONS, json=A_STATION, headers=headers)
    assert station.status_code == 201, station.text
    return host.json()["id"], station.json()["id"]


def test_connector_configuration_crud_is_reversible_and_nonsecret(
    client: Client, bootstrap: BootstrapCommand
) -> None:
    log_in(client, bootstrap)
    headers = csrf_header(client)
    host_id, station_id = _create_host_and_station(client, headers)
    placement = A_CONFIGURATION | {"host_id": host_id, "station_id": station_id}

    created = client.post(CONNECTORS, json=placement, headers=headers)
    assert created.status_code == 201, created.text
    connector_id = created.json()["id"]
    assert created.json()["configuration"] == {"address": "10.0.8.21", "port": 80}
    assert created.json()["credentials_configured"] is False
    assert created.json()["reachability"] == "unverified"
    assert "password" not in created.text.lower()

    listed = client.get(CONNECTORS, params={"station_id": station_id})
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0] == created.json()

    edited = client.patch(
        f"{CONNECTORS}/{connector_id}",
        json=A_CONFIGURATION
        | {
            "name": "板卡连接器",
            "connector_type": "board_card",
            "configuration": {"address": "/dev/board0"},
            "host_id": host_id,
            "station_id": station_id,
        },
        headers={**headers, "If-Match": "1"},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["revision"] == 2
    assert edited.json()["configuration"] == {"address": "/dev/board0", "port": None}
    assert edited.json()["reachability"] == "unverified"

    stale = client.patch(
        f"{CONNECTORS}/{connector_id}",
        json=placement,
        headers={**headers, "If-Match": "1"},
    )
    assert stale.status_code == 409
    assert stale.json()["error_code"] == "STALE_REVISION"

    deactivated = client.put(
        f"{CONNECTORS}/{connector_id}/status",
        json={"status": "deactivated"},
        headers={**headers, "If-Match": "2"},
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["revision"] == 3
    assert deactivated.json()["status"] == "deactivated"

    restored = client.put(
        f"{CONNECTORS}/{connector_id}/status",
        json={"status": "active"},
        headers={**headers, "If-Match": "3"},
    )
    assert restored.status_code == 200
    assert restored.json()["revision"] == 4
    assert restored.json()["status"] == "active"

    deleted = client.delete(
        f"{CONNECTORS}/{connector_id}",
        headers={**headers, "If-Match": "4"},
    )
    assert deleted.status_code == 204
    assert client.get(f"{CONNECTORS}/{connector_id}").status_code == 404
    assert client.get(CONNECTORS, params={"station_id": station_id}).json()["total"] == 0


def test_empty_stations_are_deletable_and_connector_history_guards_both_parents(
    client: Client, bootstrap: BootstrapCommand
) -> None:
    log_in(client, bootstrap)
    headers = csrf_header(client)
    host_id, station_id = _create_host_and_station(client, headers)

    assert (
        client.delete(f"{STATIONS}/{station_id}", headers={**headers, "If-Match": "1"}).status_code
        == 204
    )
    assert (
        client.delete(f"{HOSTS}/{host_id}", headers={**headers, "If-Match": "1"}).status_code == 204
    )

    host_id, station_id = _create_host_and_station(
        client, {**headers, "x-correlation-id": "connector-parent-guards"}
    )
    created = client.post(
        CONNECTORS,
        json=A_CONFIGURATION | {"host_id": host_id, "station_id": station_id},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    connector_id = created.json()["id"]

    station_refused = client.delete(
        f"{STATIONS}/{station_id}", headers={**headers, "If-Match": "1"}
    )
    assert station_refused.status_code == 409
    assert station_refused.json()["error_code"] == "STATION_HAS_CONNECTORS"

    host_refused = client.delete(f"{HOSTS}/{host_id}", headers={**headers, "If-Match": "1"})
    assert host_refused.status_code == 409
    assert host_refused.json()["error_code"] == "INFERENCE_HOST_HAS_CONNECTORS"

    assert (
        client.delete(
            f"{CONNECTORS}/{connector_id}", headers={**headers, "If-Match": "1"}
        ).status_code
        == 204
    )
    assert (
        client.delete(f"{STATIONS}/{station_id}", headers={**headers, "If-Match": "1"}).status_code
        == 204
    )
    assert (
        client.delete(f"{HOSTS}/{host_id}", headers={**headers, "If-Match": "1"}).status_code == 204
    )


def test_connector_configuration_rejects_secrets_at_the_http_seam(
    client: Client, bootstrap: BootstrapCommand
) -> None:
    log_in(client, bootstrap)
    headers = csrf_header(client)
    host_id, station_id = _create_host_and_station(client, headers)

    refused = client.post(
        CONNECTORS,
        json=A_CONFIGURATION
        | {
            "host_id": host_id,
            "station_id": station_id,
            "configuration": {
                "address": "http://connector-user:secret@10.0.8.21"  # pragma: allowlist secret
            },
        },
        headers=headers,
    )
    assert refused.status_code == 422
    assert refused.json()["error_code"] == "REQUEST_INVALID"
    assert "secret" not in refused.text  # pragma: allowlist secret

    raw_refused = client.post(
        CONNECTORS,
        json=A_CONFIGURATION
        | {
            "host_id": host_id,
            "station_id": station_id,
            "configuration": {
                "address": "connector-user:secret@[10.0.8.21]"  # pragma: allowlist secret
            },
        },
        headers=headers,
    )
    assert raw_refused.status_code == 422
    assert raw_refused.json()["error_code"] == "REQUEST_INVALID"
    assert "secret" not in raw_refused.text  # pragma: allowlist secret

    unknown = client.post(
        CONNECTORS,
        json=A_CONFIGURATION
        | {
            "host_id": host_id,
            "station_id": station_id,
            # 合成凭据，仅验证拒绝路径。
            "configuration": {
                "address": "10.0.8.21",
                "password": "secret",  # pragma: allowlist secret
            },
        },
        headers=headers,
    )
    assert unknown.status_code == 422
    assert unknown.json()["error_code"] == "REQUEST_INVALID"
    assert "secret" not in unknown.text  # pragma: allowlist secret
