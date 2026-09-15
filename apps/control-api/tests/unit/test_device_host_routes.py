"""The inference host's HTTP surface, with the seams replaced at their own boundaries.

The in-memory stand-ins are the same ones the use-case suite passes (harness §4). What this
suite is about is everything between the request and that seam — the `problem+json` shape,
the If-Match precondition, the paging envelope, and what an anonymous caller is told — none
of which needs a database. The flow against real PostgreSQL is `tests/integration/`; the
backend endpoints' HTTP suite is `test_device_backend_routes.py`.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from auth_fakes import FakeRoles, FakeSessions, FakeUsers
from device_fakes import (
    DEFAULT_HOST_IDENTITY,
    FakeConnectors,
    FakeInferenceBackends,
    FakeInferenceHosts,
)
from fastapi.testclient import TestClient
from httpx2 import Response as HttpResponse
from pydantic import SecretStr

from factory_sop.app import API_PREFIX, create_app
from factory_sop.auth.adapters import dependencies as auth_dependencies
from factory_sop.auth.adapters.cookies import CSRF_COOKIE, CSRF_HEADER
from factory_sop.auth.api import Permission
from factory_sop.device.adapters import dependencies as device_dependencies
from factory_sop.problem import PROBLEM_MEDIA_TYPE
from factory_sop.settings import Settings

HOSTS = f"{API_PREFIX}/inference-hosts"
CREDENTIALS = {  # pragma: allowlist secret
    "login_name": "chen.wei",
    "password": "assembly-line-4",  # pragma: allowlist secret
}

A_HOST = {
    "name": "装配A线-推理机1",
    "address": "10.0.8.11",
    "mediamtx_address": "http://10.0.8.11:8888",
    "recording_window_seconds": 7 * 24 * 3600,
    "disk_watermark_percent": 85,
}


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
        redis_url=SecretStr("redis://127.0.0.1:1/0"),
    )


class Center:
    """A running application over in-memory stores, plus a logged-in client.

    The client speaks `https` for the same reason `test_auth_routes`'s does: the cookies
    carry `Secure`, and a client on plain `http` would silently drop them.
    """

    def __init__(self) -> None:
        self.users = FakeUsers()
        self.sessions = FakeSessions()
        self.hosts = FakeInferenceHosts()
        self.backends = FakeInferenceBackends()
        self.connectors = FakeConnectors()
        self.granted: frozenset[Permission] = frozenset()
        self.app = create_app(settings())
        self.app.dependency_overrides[auth_dependencies.users] = lambda: self.users
        self.app.dependency_overrides[auth_dependencies.sessions] = lambda: self.sessions
        # The login response resolves the caller's granted set; with no roles registered the
        # set is empty, and the device permissions each test grants come from the override
        # below rather than from a role row.
        self.app.dependency_overrides[auth_dependencies.roles] = lambda: FakeRoles(users=self.users)
        # §5.15's scripted seam: the test supplies the permission set directly, so a suite
        # can exercise any caller shape without building a role for each.
        self.app.dependency_overrides[auth_dependencies.granted_permissions] = lambda: self.granted
        self.app.dependency_overrides[device_dependencies.hosts] = lambda: self.hosts
        self.app.dependency_overrides[device_dependencies.backends] = lambda: self.backends
        self.app.dependency_overrides[device_dependencies.connectors] = lambda: self.connectors
        self.client = TestClient(self.app, base_url="https://testserver")

    def with_account(self) -> Center:
        self.users.register(
            login_name=CREDENTIALS["login_name"],
            password=CREDENTIALS["password"],
        )
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
        **kwargs: Any,  # noqa: ANN401 — passthrough to the HTTP client, whose own signature is Any-typed
    ) -> HttpResponse:
        """Send a request the way the Web page does: CSRF header on every modifying call.

        The header is injected here rather than repeated at every call site, so a test that
        omits it deliberately (the CSRF suite) reads as the exception, not as the norm.
        """
        injected = dict(headers or {})
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            injected[CSRF_HEADER] = self.client.cookies[CSRF_COOKIE]
        return self.client.request(method, path, headers=injected, **kwargs)


ALL_HOST_PERMISSIONS = (
    Permission.INFERENCE_HOST_VIEW,
    Permission.INFERENCE_HOST_EDIT,
    Permission.INFERENCE_HOST_DELETE,
)


@pytest.fixture
def center() -> Center:
    return Center().with_account().log_in().with_permissions(*ALL_HOST_PERMISSIONS)


def test_an_anonymous_creation_is_refused_in_the_one_problem_shape() -> None:
    anonymous = Center().with_account()

    response = anonymous.client.post(HOSTS, json=A_HOST)

    assert response.status_code == 401
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body["type"] == "about:blank"
    assert body["error_code"] == "AUTHENTICATION_REQUIRED"


def test_a_logged_in_administrator_creates_a_host_whose_record_is_whole(center: Center) -> None:
    response = center.send("POST", HOSTS, json=A_HOST)

    assert response.status_code == 201
    body = response.json()
    host_id = UUID(body["id"])
    # Whole record, not selected fields: what the caller sees is what the store holds.
    assert body == {
        "id": body["id"],
        "name": "装配A线-推理机1",
        "address": "10.0.8.11",
        "mediamtx_address": "http://10.0.8.11:8888",
        "recording_window_seconds": 7 * 24 * 3600,
        "disk_watermark_percent": 85,
        "status": "active",
        "revision": 1,
        "created_by": str(next(iter(center.users.by_id))),
        "updated_by": str(next(iter(center.users.by_id))),
        "created_at": body["created_at"],
        "updated_at": body["updated_at"],
    }
    assert center.hosts.rows[host_id].name == "装配A线-推理机1"
    assert body["created_at"].endswith("Z")


def test_host_identity_registration_stores_only_the_public_key(center: Center) -> None:
    created = center.send("POST", HOSTS, json=A_HOST)
    host_id = created.json()["id"]
    key_pair = DEFAULT_HOST_IDENTITY

    registered = center.send(
        "POST",
        f"{HOSTS}/{host_id}/identity-key",
        headers={"If-Match": "1"},
        json={"public_key": key_pair.public_key},
    )

    assert registered.status_code == 200
    assert registered.json() == {"revision": 2}
    assert center.hosts.rows[UUID(host_id)].identity_public_key == key_pair.public_key
    assert key_pair.private_key not in center.send("GET", f"{HOSTS}/{host_id}").text

    stale = center.send(
        "POST",
        f"{HOSTS}/{host_id}/identity-key",
        headers={"If-Match": "1"},
        json={"public_key": key_pair.public_key},
    )
    assert stale.status_code == 409
    assert stale.json()["error_code"] == "STALE_REVISION"


def test_old_host_credential_route_is_retired_without_issuing_a_secret(center: Center) -> None:
    created = center.send("POST", HOSTS, json=A_HOST)
    host_id = created.json()["id"]

    retired = center.send(
        "POST",
        f"{HOSTS}/{host_id}/credential",
        headers={"If-Match": "1"},
    )

    assert retired.status_code == 410
    assert retired.json()["error_code"] == "INFERENCE_HOST_CREDENTIALS_REMOVED"
    assert center.hosts.rows[UUID(host_id)].identity_public_key is None


def test_a_taken_host_name_refuses_with_the_shared_problem_shape(center: Center) -> None:
    assert center.send("POST", HOSTS, json=A_HOST).status_code == 201

    response = center.send("POST", HOSTS, json=A_HOST | {"address": "10.0.8.12"})

    assert response.status_code == 409
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.json()["error_code"] == "INFERENCE_HOST_NAME_TAKEN"


@pytest.mark.parametrize(
    ("field", "value"),
    [("disk_watermark_percent", 100), ("recording_window_seconds", 0)],
)
def test_field_boundaries_are_refused_at_save_time_with_field_errors(
    center: Center, field: str, value: int
) -> None:
    response = center.send("POST", HOSTS, json=A_HOST | {field: value})

    assert response.status_code == 422
    body = response.json()
    assert body["error_code"] == "REQUEST_INVALID"
    assert [error["field"] for error in body["field_errors"]] == [field]


def test_the_host_list_is_a_paged_envelope(center: Center) -> None:
    center.send("POST", HOSTS, json=A_HOST)
    center.send("POST", HOSTS, json=A_HOST | {"name": "装配B线-推理机2"})

    response = center.send("GET", HOSTS, params={"page": 1, "page_size": 1})

    assert response.status_code == 200
    body = response.json()
    assert body["page"] == 1
    assert body["page_size"] == 1
    assert body["total"] == 2
    assert [item["name"] for item in body["items"]] == ["装配B线-推理机2"]


def test_reading_a_missing_host_refuses_with_a_404_problem(center: Center) -> None:
    response = center.send("GET", f"{HOSTS}/{UUID(int=1)}")

    assert response.status_code == 404
    assert response.json()["error_code"] == "INFERENCE_HOST_NOT_FOUND"


def test_editing_requires_if_match_and_refuses_a_lost_race(center: Center) -> None:
    host_id = center.send("POST", HOSTS, json=A_HOST).json()["id"]

    without = center.send("PATCH", f"{HOSTS}/{host_id}", json=A_HOST | {"address": "10.0.8.50"})
    assert without.status_code == 422
    assert without.json()["error_code"] == "REQUEST_INVALID"

    stale = center.send(
        "PATCH",
        f"{HOSTS}/{host_id}",
        json=A_HOST | {"address": "10.0.8.50"},
        headers={"If-Match": "9"},
    )
    assert stale.status_code == 409
    assert stale.json()["error_code"] == "STALE_REVISION"

    current = center.send(
        "PATCH",
        f"{HOSTS}/{host_id}",
        json=A_HOST | {"address": "10.0.8.50"},
        headers={"If-Match": "1"},
    )
    assert current.status_code == 200
    assert current.json()["revision"] == 2
    assert current.json()["address"] == "10.0.8.50"


def test_deactivation_is_reversible_over_one_status_subresource(center: Center) -> None:
    host_id = center.send("POST", HOSTS, json=A_HOST).json()["id"]

    deactivated = center.send(
        "PUT",
        f"{HOSTS}/{host_id}/status",
        json={"status": "deactivated"},
        headers={"If-Match": "1"},
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["status"] == "deactivated"
    assert deactivated.json()["revision"] == 2

    restored = center.send(
        "PUT",
        f"{HOSTS}/{host_id}/status",
        json={"status": "active"},
        headers={"If-Match": "2"},
    )
    assert restored.status_code == 200
    assert restored.json()["status"] == "active"
    assert restored.json()["revision"] == 3
    # 停用 never touched the history: the row is the same id, revisions later.
    assert restored.json()["id"] == host_id


def test_status_and_delete_require_a_current_if_match_revision(center: Center) -> None:
    host_id = center.send("POST", HOSTS, json=A_HOST).json()["id"]

    missing_status_revision = center.send(
        "PUT", f"{HOSTS}/{host_id}/status", json={"status": "deactivated"}
    )
    assert missing_status_revision.status_code == 422
    assert missing_status_revision.json()["error_code"] == "REQUEST_INVALID"

    stale_status_revision = center.send(
        "PUT",
        f"{HOSTS}/{host_id}/status",
        json={"status": "deactivated"},
        headers={"If-Match": "9"},
    )
    assert stale_status_revision.status_code == 409
    assert stale_status_revision.json()["error_code"] == "STALE_REVISION"

    missing_delete_revision = center.send("DELETE", f"{HOSTS}/{host_id}")
    assert missing_delete_revision.status_code == 422
    assert missing_delete_revision.json()["error_code"] == "REQUEST_INVALID"

    stale_delete_revision = center.send("DELETE", f"{HOSTS}/{host_id}", headers={"If-Match": "9"})
    assert stale_delete_revision.status_code == 409
    assert stale_delete_revision.json()["error_code"] == "STALE_REVISION"


def test_a_caller_without_the_permission_is_refused_with_403(center: Center) -> None:
    host_id = center.send("POST", HOSTS, json=A_HOST).json()["id"]

    center.with_permissions(Permission.INFERENCE_HOST_VIEW)
    denied = center.send(
        "PUT",
        f"{HOSTS}/{host_id}/status",
        json={"status": "deactivated"},
        headers={"If-Match": "1"},
    )
    assert denied.status_code == 403
    assert denied.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert denied.json()["error_code"] == "PERMISSION_DENIED"
    # View stays available to this caller: the refusal is per permission, not per caller.
    assert center.send("GET", f"{HOSTS}/{host_id}").status_code == 200


def test_every_host_route_declares_the_permission_its_use_case_enforces(center: Center) -> None:
    schema = center.app.openapi()
    declared = {
        f"{method.upper()} {path.removeprefix(API_PREFIX)}": operation["x-required-permission"]
        for path, item in schema["paths"].items()
        if "/inference-hosts" in path and not path.endswith("/configuration")
        for method, operation in item.items()
        if method in {"get", "post", "patch", "put", "delete"}
    }
    expected = {
        "POST /inference-hosts": Permission.INFERENCE_HOST_EDIT,
        "GET /inference-hosts": Permission.INFERENCE_HOST_VIEW,
        "GET /inference-hosts/{host_id}": Permission.INFERENCE_HOST_VIEW,
        "GET /inference-hosts/{host_id}/media-configuration": [
            Permission.INFERENCE_HOST_VIEW.value,
            Permission.CAMERA_VIEW.value,
        ],
        "PATCH /inference-hosts/{host_id}": Permission.INFERENCE_HOST_EDIT,
        "PUT /inference-hosts/{host_id}/status": Permission.INFERENCE_HOST_EDIT,
        "POST /inference-hosts/{host_id}/credential": Permission.INFERENCE_HOST_EDIT,
        "POST /inference-hosts/{host_id}/identity-key": Permission.INFERENCE_HOST_EDIT,
        "DELETE /inference-hosts/{host_id}": Permission.INFERENCE_HOST_DELETE,
    }
    assert declared == {
        path: permission.value if isinstance(permission, Permission) else permission
        for path, permission in expected.items()
    }


def test_status_and_delete_document_if_match_and_stale_revision_conflicts(center: Center) -> None:
    schema = center.app.openapi()

    for method, path in (
        ("put", "/api/v1/inference-hosts/{host_id}/status"),
        ("delete", "/api/v1/inference-hosts/{host_id}"),
    ):
        operation = schema["paths"][path][method]
        if_match = next(
            parameter for parameter in operation["parameters"] if parameter["name"] == "If-Match"
        )
        assert if_match["required"] is True
        assert "409" in operation["responses"]
        assert "STALE_REVISION" in operation["responses"]["409"]["description"]


def test_a_url_with_embedded_credentials_is_refused_at_the_contract(center: Center) -> None:
    # ADR-0008: a userinfo segment would store a credential, echo it on every read, and hand
    # it to whatever fails next. The contract refuses it before any of that.
    response = center.send(
        "POST",
        HOSTS,
        json=A_HOST  # pragma: allowlist secret
        | {"mediamtx_address": "http://user:secret@10.0.8.11:8888"},  # pragma: allowlist secret
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error_code"] == "REQUEST_INVALID"
    assert [error["field"] for error in body["field_errors"]] == ["mediamtx_address"]


def test_editing_a_host_into_a_taken_name_refuses_with_409(center: Center) -> None:
    host_id = center.send("POST", HOSTS, json=A_HOST).json()["id"]
    center.send("POST", HOSTS, json=A_HOST | {"name": "装配B线-推理机2"})

    response = center.send(
        "PATCH",
        f"{HOSTS}/{host_id}",
        json=A_HOST | {"name": "装配B线-推理机2"},
        headers={"If-Match": "1"},
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "INFERENCE_HOST_NAME_TAKEN"


def test_deleting_a_host_that_still_carries_a_backend_refuses(center: Center) -> None:
    host_id = center.send("POST", HOSTS, json=A_HOST).json()["id"]
    center.backends.register(host_id=UUID(host_id), base_url="http://10.0.8.11:8000")

    refused = center.send("DELETE", f"{HOSTS}/{host_id}", headers={"If-Match": "1"})

    assert refused.status_code == 409
    assert refused.json()["error_code"] == "INFERENCE_HOST_HAS_BACKENDS"
    # The store still holds the host: the refusal changed nothing.
    assert center.hosts.rows[UUID(host_id)].name == "装配A线-推理机1"


def test_a_modifying_request_without_the_csrf_token_refuses(center: Center) -> None:
    response = center.client.post(HOSTS, json=A_HOST)

    assert response.status_code == 403
    assert response.json()["error_code"] == "CSRF_TOKEN_INVALID"
