"""SYS-24-04 — deactivation is reversible and preserves historical references."""

from __future__ import annotations

from conftest import BootstrapCommand, csrf_header, log_in
from httpx2 import Client

HOSTS = "/api/v1/inference-hosts"
BACKENDS = "/api/v1/inference-backends"
A_HOST = {
    "name": "装配A线-推理机1",
    "address": "10.0.8.11",
    "mediamtx_address": None,
    "recording_window_seconds": 7 * 24 * 3600,
    "disk_watermark_percent": 85,
}


def test_deactivation_is_reversible_and_keeps_history_intact(
    client: Client, bootstrap: BootstrapCommand
) -> None:
    log_in(client, bootstrap)
    headers = csrf_header(client)
    host_id = client.post(HOSTS, json=A_HOST, headers=headers).json()["id"]
    backend_id = client.post(
        BACKENDS, json={"host_id": host_id, "base_url": "http://10.0.8.11:8000"}, headers=headers
    ).json()["id"]

    deactivated = client.put(
        f"{HOSTS}/{host_id}/status",
        json={"status": "deactivated"},
        headers={**headers, "If-Match": "1"},
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["status"] == "deactivated"

    backend_while_deactivated = client.get(f"{BACKENDS}/{backend_id}")
    assert backend_while_deactivated.status_code == 200
    assert backend_while_deactivated.json()["host_id"] == host_id
    assert backend_while_deactivated.json()["status"] == "active"

    restored = client.put(
        f"{HOSTS}/{host_id}/status",
        json={"status": "active"},
        headers={**headers, "If-Match": str(deactivated.json()["revision"])},
    )
    assert restored.status_code == 200
    assert restored.json()["status"] == "active"
    assert restored.json()["id"] == host_id
    assert client.get(f"{BACKENDS}/{backend_id}").json()["host_id"] == host_id
