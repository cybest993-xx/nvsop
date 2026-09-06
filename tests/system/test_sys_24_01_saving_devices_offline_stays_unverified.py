"""SYS-24-01 — device configuration saves offline and starts unverified.

Precondition: a device administrator is logged in.
Action: create an inference host and one backend without testing the connection, then read both.
Observation: both save and read successfully; the backend remains `unverified`.
"""

from __future__ import annotations

from conftest import BootstrapCommand, csrf_header, log_in
from httpx2 import Client

HOSTS = "/api/v1/inference-hosts"
BACKENDS = "/api/v1/inference-backends"
A_HOST = {
    "name": "装配A线-推理机1",
    "address": "10.0.8.11",
    "mediamtx_address": "http://10.0.8.11:8888",
    "recording_window_seconds": 7 * 24 * 3600,
    "disk_watermark_percent": 85,
}


def test_devices_save_offline_and_start_unverified(
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

    fetched_host = client.get(f"{HOSTS}/{host_id}")
    assert fetched_host.status_code == 200
    assert fetched_host.json()["status"] == "active"

    fetched_backend = client.get(f"{BACKENDS}/{backend.json()['id']}")
    assert fetched_backend.status_code == 200
    assert fetched_backend.json()["connection"]["state"] == "unverified"
    assert fetched_backend.json()["connection"]["self_reported_model_ids"] == []
    assert fetched_backend.json()["base_url"] == "http://10.0.8.11:8000"
