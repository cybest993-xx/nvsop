"""SYS-24-02 — an inference backend belongs to one existing active host."""

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
A_BACKEND = {"base_url": "http://10.0.8.11:8000"}


def test_topology_constraints_hold_over_http(client: Client, bootstrap: BootstrapCommand) -> None:
    log_in(client, bootstrap)
    headers = csrf_header(client)
    host_id = client.post(HOSTS, json=A_HOST, headers=headers).json()["id"]

    created = client.post(BACKENDS, json=A_BACKEND | {"host_id": host_id}, headers=headers)
    assert created.status_code == 201, created.text
    backend_id = created.json()["id"]

    unknown_host = client.post(
        BACKENDS,
        json=A_BACKEND | {"host_id": "00000000-0000-0000-0000-000000000001"},
        headers=headers,
    )
    assert unknown_host.status_code == 404
    assert unknown_host.json()["error_code"] == "INFERENCE_HOST_NOT_FOUND"

    deactivated = client.put(
        f"{HOSTS}/{host_id}/status",
        json={"status": "deactivated"},
        headers={**headers, "If-Match": "1"},
    )
    assert deactivated.status_code == 200
    refused = client.post(BACKENDS, json=A_BACKEND | {"host_id": host_id}, headers=headers)
    assert refused.status_code == 409
    assert refused.json()["error_code"] == "INFERENCE_HOST_DEACTIVATED"

    restored = client.put(
        f"{HOSTS}/{host_id}/status",
        json={"status": "active"},
        headers={**headers, "If-Match": str(deactivated.json()["revision"])},
    )
    assert restored.status_code == 200
    assert (
        client.delete(
            f"{HOSTS}/{host_id}",
            headers={**headers, "If-Match": str(restored.json()["revision"])},
        ).status_code
        == 409
    )
    assert (
        client.delete(
            f"{BACKENDS}/{backend_id}",
            headers={**headers, "If-Match": str(created.json()["revision"])},
        ).status_code
        == 204
    )
    assert (
        client.delete(
            f"{HOSTS}/{host_id}",
            headers={**headers, "If-Match": str(restored.json()["revision"])},
        ).status_code
        == 204
    )
    assert client.get(f"{HOSTS}/{host_id}").status_code == 404
