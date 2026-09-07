"""The inference backend HTTP surface: placement, deactivation, and connection testing.

The repositories and probe are replaced at their public seams. Real HTTP honesty belongs to
`test_device_probe.py`; this suite proves the request contract and use-case wiring.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from auth_fakes import FakeRoles, FakeSessions, FakeUsers
from device_fakes import FakeConnectors, FakeInferenceBackends, FakeInferenceHosts, FakeProbe
from fastapi.testclient import TestClient
from httpx2 import Response as HttpResponse
from pydantic import SecretStr

from factory_sop.app import API_PREFIX, create_app
from factory_sop.auth.adapters import dependencies as auth_dependencies
from factory_sop.auth.adapters.cookies import CSRF_COOKIE, CSRF_HEADER
from factory_sop.auth.api import Permission
from factory_sop.device.adapters import dependencies as device_dependencies
from factory_sop.device.model import ConnectionState
from factory_sop.device.probing import ProbeReport
from factory_sop.problem import PROBLEM_MEDIA_TYPE
from factory_sop.settings import Settings

HOSTS = f"{API_PREFIX}/inference-hosts"
BACKENDS = f"{API_PREFIX}/inference-backends"
CREDENTIALS = {  # pragma: allowlist secret
    "login_name": "chen.wei",
    "password": "assembly-line-4",  # pragma: allowlist secret
}
A_HOST_NAME = "装配A线-推理机1"
A_HOST = {
    "name": A_HOST_NAME,
    "address": "10.0.8.11",
    "mediamtx_address": None,
    "recording_window_seconds": 7 * 24 * 3600,
    "disk_watermark_percent": 85,
}
ALL_DEVICE_PERMISSIONS = (
    Permission.INFERENCE_HOST_VIEW,
    Permission.INFERENCE_HOST_EDIT,
    Permission.INFERENCE_HOST_DELETE,
    Permission.INFERENCE_BACKEND_VIEW,
    Permission.INFERENCE_BACKEND_EDIT,
    Permission.INFERENCE_BACKEND_DELETE,
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
    """A running application over in-memory repositories and a scripted probe."""

    def __init__(self) -> None:
        self.users = FakeUsers()
        self.sessions = FakeSessions()
        self.roles = FakeRoles(users=self.users)
        self.hosts = FakeInferenceHosts()
        self.backends = FakeInferenceBackends()
        self.connectors = FakeConnectors()
        self.probe = FakeProbe()
        self.granted: frozenset[Permission] = frozenset()
        self.app = create_app(settings())
        self.app.dependency_overrides[auth_dependencies.users] = lambda: self.users
        self.app.dependency_overrides[auth_dependencies.sessions] = lambda: self.sessions
        self.app.dependency_overrides[auth_dependencies.roles] = lambda: self.roles
        self.app.dependency_overrides[auth_dependencies.granted_permissions] = lambda: self.granted
        self.app.dependency_overrides[device_dependencies.hosts] = lambda: self.hosts
        self.app.dependency_overrides[device_dependencies.backends] = lambda: self.backends
        self.app.dependency_overrides[device_dependencies.connectors] = lambda: self.connectors
        self.app.dependency_overrides[device_dependencies.probe] = lambda: self.probe
        self.client = TestClient(self.app, base_url="https://testserver")

    def with_account(self) -> Center:
        self.users.register(login_name=CREDENTIALS["login_name"], password=CREDENTIALS["password"])
        return self

    def log_in(self) -> Center:
        assert self.client.post(f"{API_PREFIX}/auth/session", json=CREDENTIALS).status_code == 201
        return self

    def with_permissions(self, *permissions: Permission) -> Center:
        self.granted = frozenset(permissions)
        return self

    def send(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        **kwargs: Any,  # noqa: ANN401 — passthrough to the HTTP client
    ) -> HttpResponse:
        injected = dict(headers or {})
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            injected[CSRF_HEADER] = self.client.cookies[CSRF_COOKIE]
        return self.client.request(method, path, headers=injected, **kwargs)

    def create_host(self, *, name: str = A_HOST_NAME) -> str:
        response = self.send("POST", HOSTS, json=A_HOST | {"name": name})
        assert response.status_code == 201, response.text
        return str(response.json()["id"])

    def create_backend(self, host_id: str, base_url: str = "http://10.0.8.11:8000") -> str:
        response = self.send("POST", BACKENDS, json={"host_id": host_id, "base_url": base_url})
        assert response.status_code == 201, response.text
        return str(response.json()["id"])


@pytest.fixture
def center() -> Center:
    return Center().with_account().log_in().with_permissions(*ALL_DEVICE_PERMISSIONS)


def test_a_backend_needs_an_existing_active_host(center: Center) -> None:
    missing = center.send(
        "POST",
        BACKENDS,
        json={"host_id": str(UUID(int=1)), "base_url": "http://10.0.8.11:8000"},
    )
    assert missing.status_code == 404
    assert missing.json()["error_code"] == "INFERENCE_HOST_NOT_FOUND"

    host_id = center.create_host()
    assert (
        center.send(
            "PUT",
            f"{HOSTS}/{host_id}/status",
            json={"status": "deactivated"},
            headers={"If-Match": "1"},
        ).status_code
        == 200
    )
    refused = center.send(
        "POST", BACKENDS, json={"host_id": host_id, "base_url": "http://10.0.8.11:8000"}
    )
    assert refused.status_code == 409
    assert refused.json()["error_code"] == "INFERENCE_HOST_DEACTIVATED"


def test_endpoint_uniqueness_is_per_host(center: Center) -> None:
    host_a = center.create_host()
    host_b = center.create_host(name="装配B线-推理机2")
    center.create_backend(host_a)

    duplicate = center.send(
        "POST", BACKENDS, json={"host_id": host_a, "base_url": "http://10.0.8.11:8000"}
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error_code"] == "INFERENCE_BACKEND_ENDPOINT_TAKEN"
    assert center.create_backend(host_b) != ""


def test_a_backend_url_with_credentials_is_refused_before_storage(center: Center) -> None:
    response = center.send(
        "POST",
        BACKENDS,
        json={
            "host_id": center.create_host(),
            "base_url": "http://user:secret@10.0.8.11:8000",  # pragma: allowlist secret
        },
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "REQUEST_INVALID"
    assert response.json()["field_errors"][0]["field"] == "base_url"
    assert "secret" not in response.text  # pragma: allowlist secret


@pytest.mark.parametrize(
    "base_url",
    [
        "http://10.0.8.11:8000?access_token=fixture-marker",
        "http://10.0.8.11:8000#access_token=fixture-marker",
    ],
)
def test_a_backend_url_with_a_query_or_fragment_is_refused_before_storage(
    center: Center, base_url: str
) -> None:
    response = center.send(
        "POST",
        BACKENDS,
        json={"host_id": center.create_host(), "base_url": base_url},
    )

    assert response.status_code == 422
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.json()["error_code"] == "REQUEST_INVALID"
    assert response.json()["field_errors"][0]["field"] == "base_url"
    assert "fixture-marker" not in response.text


def test_the_backend_view_carries_one_template_binding_slot_and_connection_state(
    center: Center,
) -> None:
    backend_id = center.create_backend(center.create_host())

    response = center.send("GET", f"{BACKENDS}/{backend_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["template_version_id"] is None
    assert body["connection"] == {
        "state": "unverified",
        "checked_at": None,
        "detail": None,
        "self_reported_model_ids": [],
        "self_reported_at": None,
    }


def test_the_backend_list_filters_by_host_and_keeps_the_total_true(center: Center) -> None:
    host_a = center.create_host()
    host_b = center.create_host(name="装配B线-推理机2")
    center.create_backend(host_a, "http://10.0.8.11:8000")
    center.create_backend(host_a, "http://10.0.8.11:8001")
    center.create_backend(host_b, "http://10.0.8.12:8000")

    body = center.send("GET", BACKENDS, params={"host_id": host_a}).json()

    assert body["total"] == 2
    assert {item["host_id"] for item in body["items"]} == {host_a}


def test_editing_requires_if_match_and_resets_facts_when_the_endpoint_moves(center: Center) -> None:
    host_id = center.create_host()
    backend_id = center.create_backend(host_id)

    stale = center.send(
        "PATCH",
        f"{BACKENDS}/{backend_id}",
        json={"host_id": host_id, "base_url": "http://10.0.8.11:8001"},
        headers={"If-Match": "9"},
    )
    assert stale.status_code == 409
    assert stale.json()["error_code"] == "STALE_REVISION"

    moved = center.send(
        "PATCH",
        f"{BACKENDS}/{backend_id}",
        json={"host_id": host_id, "base_url": "http://10.0.8.11:8001"},
        headers={"If-Match": "1"},
    )
    assert moved.status_code == 200
    assert moved.json()["revision"] == 2
    assert moved.json()["connection"]["state"] == "unverified"


def test_device_routes_document_unified_validation_and_stale_revision_problems(
    center: Center,
) -> None:
    schema = center.app.openapi()
    device_operations = {
        (method.upper(), path): operation
        for path, item in schema["paths"].items()
        if path.startswith(f"{API_PREFIX}/inference-")
        for method, operation in item.items()
        if method in {"get", "post", "patch", "put", "delete"}
    }

    assert device_operations
    for method_path, operation in device_operations.items():
        validation = operation["responses"]["422"]
        assert set(validation["content"]) == {PROBLEM_MEDIA_TYPE}, method_path

    for method_path in {
        ("PATCH", f"{API_PREFIX}/inference-hosts/{{host_id}}"),
        ("PUT", f"{API_PREFIX}/inference-hosts/{{host_id}}/status"),
        ("PATCH", f"{API_PREFIX}/inference-backends/{{backend_id}}"),
        ("PUT", f"{API_PREFIX}/inference-backends/{{backend_id}}/status"),
        ("POST", f"{API_PREFIX}/inference-backends/{{backend_id}}/connection-test"),
        ("DELETE", f"{API_PREFIX}/inference-backends/{{backend_id}}"),
    }:
        stale = device_operations[method_path]["responses"]["409"]
        assert "STALE_REVISION" in stale["description"], method_path
        assert set(stale["content"]) == {PROBLEM_MEDIA_TYPE}, method_path


def test_a_connection_test_reports_success_and_failure_from_the_probe(center: Center) -> None:
    host_id = center.create_host()
    backend_id = center.create_backend(host_id)

    center.probe.report = ProbeReport(
        status=ConnectionState.SUCCESS, model_ids=("ds_sop_model", "qwen2-7b")
    )
    reached = center.send(
        "POST",
        f"{BACKENDS}/{backend_id}/connection-test",
        headers={"If-Match": "1"},
    )
    assert reached.status_code == 200
    assert reached.json()["connection"]["state"] == "success"
    assert reached.json()["connection"]["self_reported_model_ids"] == [
        "ds_sop_model",
        "qwen2-7b",
    ]
    assert reached.json()["revision"] == 2
    assert center.probe.asked_for == ["http://10.0.8.11:8000"]

    center.probe.report = ProbeReport(status=ConnectionState.FAILURE, detail="connection refused")
    failed = center.send(
        "POST",
        f"{BACKENDS}/{backend_id}/connection-test",
        headers={"If-Match": "2"},
    )
    assert failed.status_code == 200
    assert failed.json()["connection"]["state"] == "failure"
    assert failed.json()["connection"]["detail"] == "connection refused"
    assert failed.json()["connection"]["self_reported_model_ids"] == []


def test_the_backend_lifecycle_is_reversible_and_deletable(center: Center) -> None:
    host_id = center.create_host()
    backend_id = center.create_backend(host_id)

    deactivated = center.send(
        "PUT",
        f"{BACKENDS}/{backend_id}/status",
        json={"status": "deactivated"},
        headers={"If-Match": "1"},
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["status"] == "deactivated"
    restored = center.send(
        "PUT",
        f"{BACKENDS}/{backend_id}/status",
        json={"status": "active"},
        headers={"If-Match": "2"},
    )
    assert restored.status_code == 200
    assert restored.json()["status"] == "active"

    assert (
        center.send("DELETE", f"{BACKENDS}/{backend_id}", headers={"If-Match": "3"}).status_code
        == 204
    )
    missing = center.send("GET", f"{BACKENDS}/{backend_id}")
    assert missing.status_code == 404
    assert missing.json()["error_code"] == "INFERENCE_BACKEND_NOT_FOUND"


def test_a_caller_without_backend_edit_permission_is_refused_with_403(center: Center) -> None:
    host_id = center.create_host()
    backend_id = center.create_backend(host_id)
    center.with_permissions(Permission.INFERENCE_BACKEND_VIEW)

    denied = center.send(
        "POST",
        f"{BACKENDS}/{backend_id}/connection-test",
        headers={"If-Match": "1"},
    )

    assert denied.status_code == 403
    assert denied.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert denied.json()["error_code"] == "PERMISSION_DENIED"
    assert center.send("GET", f"{BACKENDS}/{backend_id}").status_code == 200


def test_every_backend_route_declares_the_permission_its_use_case_enforces(center: Center) -> None:
    schema = center.app.openapi()
    declared = {
        f"{method.upper()} {path.removeprefix(API_PREFIX)}": operation["x-required-permission"]
        for path, item in schema["paths"].items()
        if "/inference-backends" in path
        for method, operation in item.items()
        if method in {"get", "post", "patch", "put", "delete"}
    }

    assert declared == {
        "POST /inference-backends": Permission.INFERENCE_BACKEND_EDIT.value,
        "GET /inference-backends": Permission.INFERENCE_BACKEND_VIEW.value,
        "GET /inference-backends/{backend_id}": Permission.INFERENCE_BACKEND_VIEW.value,
        "PATCH /inference-backends/{backend_id}": Permission.INFERENCE_BACKEND_EDIT.value,
        "PUT /inference-backends/{backend_id}/status": Permission.INFERENCE_BACKEND_EDIT.value,
        "POST /inference-backends/{backend_id}/connection-test": (
            Permission.INFERENCE_BACKEND_EDIT.value
        ),
        "DELETE /inference-backends/{backend_id}": Permission.INFERENCE_BACKEND_DELETE.value,
    }


def test_deleting_a_host_is_possible_once_its_backends_are_gone(center: Center) -> None:
    host_id = center.create_host()
    backend_id = center.create_backend(host_id)

    assert center.send("DELETE", f"{HOSTS}/{host_id}", headers={"If-Match": "1"}).status_code == 409
    assert (
        center.send("DELETE", f"{BACKENDS}/{backend_id}", headers={"If-Match": "1"}).status_code
        == 204
    )
    assert center.send("DELETE", f"{HOSTS}/{host_id}", headers={"If-Match": "1"}).status_code == 204
