"""SYS-24-04 — host CRUD is reversible and preserves historical backend references.

Backend management is intentionally phase two. The system precondition therefore inserts one
synthetic backend row directly into the migrated PostgreSQL database, while every host action and
observation crosses the real HTTPS control-plane API.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import uuid4

from conftest import BootstrapCommand, csrf_header, log_in
from httpx2 import Client
from sqlalchemy import Engine, text

HOSTS = "/api/v1/inference-hosts"
A_HOST = {
    "name": "装配A线-推理机1",
    "address": "10.0.8.11",
    "mediamtx_address": None,
    "recording_window_seconds": 7 * 24 * 3600,
    "disk_watermark_percent": 85,
}


def arrange_backend_history(engine: Engine, host: Mapping[str, object]) -> str:
    """Arrange the later-phase backend precondition in the real PostgreSQL store."""
    backend_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO device_inference_backend (
                    id, host_id, base_url, template_version_id, status, connection_state,
                    connection_checked_at, connection_detail, self_reported_model_ids,
                    self_reported_at, revision, created_by, updated_by, created_at, updated_at
                ) VALUES (
                    :id, :host_id, :base_url, NULL, 'active', 'unverified',
                    NULL, NULL, CAST(:model_ids AS jsonb), NULL, 1,
                    :actor, :actor, now(), now()
                )
                """
            ),
            {
                "id": backend_id,
                "host_id": host["id"],
                "base_url": "http://10.0.8.11:8000",
                "model_ids": "[]",
                "actor": host["created_by"],
            },
        )
    return backend_id


def test_host_crud_is_reversible_and_history_blocks_delete(
    client: Client, bootstrap: BootstrapCommand, engine: Engine
) -> None:
    log_in(client, bootstrap)
    headers = csrf_header(client)

    created = client.post(HOSTS, json=A_HOST, headers=headers)
    assert created.status_code == 201, created.text
    host = created.json()
    host_id = host["id"]

    fetched = client.get(f"{HOSTS}/{host_id}")
    assert fetched.status_code == 200
    assert fetched.json() == host

    edited = client.patch(
        f"{HOSTS}/{host_id}",
        json=A_HOST | {"address": "10.0.8.50"},
        headers={**headers, "If-Match": str(host["revision"])},
    )
    assert edited.status_code == 200
    assert edited.json()["address"] == "10.0.8.50"
    assert edited.json()["revision"] == 2

    backend_id = arrange_backend_history(engine, edited.json())

    deactivated = client.put(
        f"{HOSTS}/{host_id}/status",
        json={"status": "deactivated"},
        headers={**headers, "If-Match": str(edited.json()["revision"])},
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["status"] == "deactivated"

    restored = client.put(
        f"{HOSTS}/{host_id}/status",
        json={"status": "active"},
        headers={**headers, "If-Match": str(deactivated.json()["revision"])},
    )
    assert restored.status_code == 200
    assert restored.json()["status"] == "active"
    assert restored.json()["id"] == host_id

    refused = client.delete(
        f"{HOSTS}/{host_id}", headers={**headers, "If-Match": str(restored.json()["revision"])}
    )
    assert refused.status_code == 409
    assert refused.json()["error_code"] == "INFERENCE_HOST_HAS_BACKENDS"

    # The later backend-management phase owns the row's public deletion operation. Removing the
    # synthetic precondition directly lets this system scenario also prove successful host delete.
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM device_inference_backend WHERE id = :backend_id"),
            {"backend_id": backend_id},
        )

    deleted = client.delete(
        f"{HOSTS}/{host_id}",
        headers={**headers, "If-Match": str(restored.json()["revision"])},
    )
    assert deleted.status_code == 204
    assert client.get(f"{HOSTS}/{host_id}").status_code == 404
