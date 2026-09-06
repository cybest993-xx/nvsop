"""The urllib probe against real local HTTP servers — no stubs.

Q31 requires that a connection test say only what a real request produced, so this suite
runs the adapter against an actual loopback server. The successful server exposes the base's
/v1/metadata and /v1/models endpoints separately; the probe must ask both and combine their
self-reported model identities without creating a center-side model registry.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from factory_sop.device.adapters.probe import UrllibConnectionProbe
from factory_sop.device.model import ConnectionState

CREDENTIAL_MARKER = "fixture-token"
METADATA_ANSWER = {
    "modelInfo": {"ddm_cv_model": {"id": "ddm_finetune"}},
}
MODELS_ANSWER = {
    "object": "list",
    "data": [
        {
            "id": "ds_sop_model",
            "object": "model",
            "created": 0,
            "owned_by": "nvds-sop-action-detector",
        }
    ],
}


class _Handler(BaseHTTPRequestHandler):
    response_status = 200
    metadata_body: bytes = b"{}"
    models_body: bytes = b"{}"
    requests: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        self.requests.append(self.path)
        if self.path == "/v1/metadata":
            body = self.metadata_body
        elif self.path == "/v1/models":
            body = self.models_body
        else:
            body = b"{}"
        self.send_response(self.response_status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return None


@contextmanager
def _server(
    *,
    status: int = 200,
    metadata_body: object = METADATA_ANSWER,
    models_body: object = MODELS_ANSWER,
) -> Iterator[tuple[str, list[str]]]:
    metadata_payload = (
        metadata_body if isinstance(metadata_body, bytes) else json.dumps(metadata_body).encode()
    )
    models_payload = (
        models_body if isinstance(models_body, bytes) else json.dumps(models_body).encode()
    )

    class Handler(_Handler):
        response_status = status
        metadata_body = metadata_payload
        models_body = models_payload
        requests: ClassVar[list[str]] = []

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", Handler.requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _unbound_port() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{listener.getsockname()[1]}"


def test_a_reachable_endpoint_reports_metadata_and_models_itself_reports() -> None:
    with _server() as (endpoint, requests):
        report = UrllibConnectionProbe().probe(base_url=endpoint)

    assert report.status is ConnectionState.SUCCESS
    assert report.model_ids == ("ddm_finetune", "ds_sop_model")
    assert report.detail is None
    assert requests == ["/v1/metadata", "/v1/models"]


def test_an_http_error_is_a_failure_that_names_the_status() -> None:
    with _server(status=503) as (endpoint, _):
        report = UrllibConnectionProbe().probe(base_url=endpoint)

    assert report.status is ConnectionState.FAILURE
    assert report.detail == "HTTP 503"


@pytest.mark.parametrize(
    ("metadata_body", "detail"),
    [
        ({"hello": "world"}, "response was not metadata with model info"),
        ({"modelInfo": {}}, "metadata reported no model identities"),
        (
            {"modelInfo": {"ddm_cv_model": {"name": "missing-id"}}},
            "metadata reported no model identities",
        ),
        (b"not json", "response was not JSON"),
    ],
)
def test_a_metadata_response_without_a_model_identity_is_a_failure(
    metadata_body: object, detail: str
) -> None:
    with _server(metadata_body=metadata_body) as (endpoint, _):
        report = UrllibConnectionProbe().probe(base_url=endpoint)

    assert report.status is ConnectionState.FAILURE
    assert report.detail == detail
    assert report.model_ids == ()


@pytest.mark.parametrize(
    ("models_body", "detail"),
    [
        ({"hello": "world"}, "response was not a models list"),
        ({"object": "list", "data": []}, "endpoint reported no models"),
        ({"object": "list", "data": [{"id": 123}]}, "endpoint reported no models"),
        (b"not json", "response was not JSON"),
    ],
)
def test_a_models_response_without_a_model_identity_is_a_failure(
    models_body: object, detail: str
) -> None:
    with _server(models_body=models_body) as (endpoint, _):
        report = UrllibConnectionProbe().probe(base_url=endpoint)

    assert report.status is ConnectionState.FAILURE
    assert report.detail == detail
    assert report.model_ids == ()


def test_a_url_carrying_credentials_is_refused_without_being_dialed() -> None:
    report = UrllibConnectionProbe().probe(
        base_url=f"http://user:{CREDENTIAL_MARKER}@127.0.0.1:{_unbound_port()[16:]}"
    )

    assert report.status is ConnectionState.FAILURE
    assert report.detail == "endpoint URL carries credentials"
    assert CREDENTIAL_MARKER not in (report.detail or "")


def test_a_port_where_nothing_listens_is_a_failure_not_a_simulated_success() -> None:
    report = UrllibConnectionProbe().probe(base_url=_unbound_port())

    assert report.status is ConnectionState.FAILURE
    assert report.detail
    assert report.model_ids == ()
