"""SYS-24-03 — connection testing reports real three-valued answers."""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from conftest import BootstrapCommand, csrf_header, log_in
from httpx2 import Client

HOSTS = "/api/v1/inference-hosts"
BACKENDS = "/api/v1/inference-backends"
METADATA_ANSWER = {
    "modelInfo": {"ddm_cv_model": {"id": "ddm_finetune"}},
}
MODELS_ANSWER = {
    "object": "list",
    "data": [{"id": "ds_sop_model", "object": "model", "created": 0}],
}


class _InferenceApiHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/v1/metadata":
            body = json.dumps(METADATA_ANSWER).encode()
        elif self.path == "/v1/models":
            body = json.dumps(MODELS_ANSWER).encode()
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return None


@pytest.fixture
def a_reachable_endpoint() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _InferenceApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def an_unreachable_endpoint() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{listener.getsockname()[1]}"


def test_connection_tests_are_real_three_valued_answers(
    client: Client,
    bootstrap: BootstrapCommand,
    a_reachable_endpoint: str,
    an_unreachable_endpoint: str,
) -> None:
    log_in(client, bootstrap)
    headers = csrf_header(client)
    host_id = client.post(
        HOSTS,
        json={
            "name": "装配A线-推理机1",
            "address": "10.0.8.11",
            "mediamtx_address": None,
            "recording_window_seconds": 7 * 24 * 3600,
            "disk_watermark_percent": 85,
        },
        headers=headers,
    ).json()["id"]
    reachable_id = client.post(
        BACKENDS, json={"host_id": host_id, "base_url": a_reachable_endpoint}, headers=headers
    ).json()["id"]
    unreachable_id = client.post(
        BACKENDS,
        json={"host_id": host_id, "base_url": an_unreachable_endpoint},
        headers=headers,
    ).json()["id"]

    reached = client.post(
        f"{BACKENDS}/{reachable_id}/connection-test",
        headers={**headers, "If-Match": "1"},
    )
    assert reached.status_code == 200
    assert reached.json()["connection"]["state"] == "success"
    assert reached.json()["connection"]["self_reported_model_ids"] == [
        "ddm_finetune",
        "ds_sop_model",
    ]

    failed = client.post(
        f"{BACKENDS}/{unreachable_id}/connection-test",
        headers={**headers, "If-Match": "1"},
    )
    assert failed.status_code == 200
    assert failed.json()["connection"]["state"] == "failure"
    assert failed.json()["connection"]["detail"]
    assert failed.json()["connection"]["self_reported_model_ids"] == []
