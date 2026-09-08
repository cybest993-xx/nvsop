"""委托连接测试命令的中心与推理机 HTTP 接缝。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest
from auth_fakes import FakeRoles, FakeSessions, FakeUsers
from device_fakes import FakeConnectors, FakeInferenceHosts, FakePendingCommands
from fastapi.testclient import TestClient
from httpx2 import Response as HttpResponse
from pydantic import SecretStr

from factory_sop.app import API_PREFIX, create_app
from factory_sop.auth.adapters import dependencies as auth_dependencies
from factory_sop.auth.adapters.cookies import CSRF_COOKIE, CSRF_HEADER
from factory_sop.auth.api import Permission
from factory_sop.device.adapters import dependencies as device_dependencies
from factory_sop.device.adapters.routes_commands import (
    ConnectionTestClaimDocument,
    ConnectionTestCommandDocument,
    PendingCommandView,
)
from factory_sop.device.model import (
    Connector,
    ConnectorReachability,
    PendingCommandStatus,
    PendingCommandType,
)
from factory_sop.settings import Settings

COMMANDS = f"{API_PREFIX}/device-commands"
CONNECTORS = f"{API_PREFIX}/connectors"
EDGE_TOKEN_HEADER = "X-Inference-Host-Token"
ALL_PERMISSIONS = frozenset(
    {
        Permission.CONNECTOR_VIEW,
        Permission.CONNECTOR_EDIT,
        Permission.CONNECTOR_DELETE,
    }
)


class Center:
    """带操作员会话和独立推理机 HTTP 客户端的中心应用。"""

    def __init__(self) -> None:
        self.users = FakeUsers()
        self.sessions = FakeSessions()
        self.roles = FakeRoles(users=self.users)
        self.hosts = FakeInferenceHosts()
        self.connectors = FakeConnectors()
        self.commands = FakePendingCommands()
        self.granted = ALL_PERMISSIONS
        self.app = create_app(self._settings())
        self.app.dependency_overrides[auth_dependencies.users] = lambda: self.users
        self.app.dependency_overrides[auth_dependencies.sessions] = lambda: self.sessions
        self.app.dependency_overrides[auth_dependencies.roles] = lambda: self.roles
        self.app.dependency_overrides[auth_dependencies.granted_permissions] = lambda: self.granted
        self.app.dependency_overrides[device_dependencies.hosts] = lambda: self.hosts
        self.app.dependency_overrides[device_dependencies.connectors] = lambda: self.connectors
        self.app.dependency_overrides[device_dependencies.pending_commands] = lambda: self.commands
        self.operator = self.users.register(login_name="operator", password="assembly-line-4")
        self.client = TestClient(self.app, base_url="https://testserver")
        opened = self.client.post(
            f"{API_PREFIX}/auth/session",
            json={
                "login_name": "operator",
                "password": "assembly-line-4",  # pragma: allowlist secret
            },
        )
        assert opened.status_code == 201, opened.text
        self.edge = TestClient(self.app, base_url="https://testserver")

    @staticmethod
    def _settings() -> Settings:
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

    def operator_headers(self, **extra: str) -> dict[str, str]:
        return {CSRF_HEADER: self.client.cookies[CSRF_COOKIE], **extra}

    def edge_headers(
        self, host_id: UUID, host_token: str | None = None, **extra: str
    ) -> dict[str, str]:
        return {
            "X-Inference-Host-ID": str(host_id),
            EDGE_TOKEN_HEADER: host_token or self.hosts.credential_for(host_id),
            **extra,
        }

    def send_operator(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        **kwargs: Any,  # noqa: ANN401 — 透传给 HTTP 客户端
    ) -> HttpResponse:
        return self.client.request(
            method,
            path,
            headers=self.operator_headers(**(headers or {})),
            **kwargs,
        )


def _connector(center: Center, host_id: UUID) -> Connector:
    return center.connectors.register(station_id=UUID(int=1), host_id=host_id)


@pytest.fixture
def center() -> Center:
    return Center()


def test_operator_enqueues_idempotently_and_can_read_the_status(center: Center) -> None:
    host = center.hosts.register(name="推理机 A")
    connector = _connector(center, host.id)

    first = center.send_operator(
        "POST",
        f"{CONNECTORS}/{connector.id}/connection-test",
        headers={"Idempotency-Key": "ui-test-1"},
    )

    assert first.status_code == 202, first.text
    first_body = first.json()
    expected = PendingCommandView(
        id=UUID(first_body["id"]),
        host_id=host.id,
        command_type=PendingCommandType.TEST_CONNECTOR_CONNECTION,
        target_id=connector.id,
        target_revision=connector.revision,
        idempotency_key="ui-test-1",
        status=PendingCommandStatus.PENDING,
        attempt=0,
        claimed_at=None,
        lease_expires_at=None,
        result=None,
        result_detail=None,
        failure_code=None,
        completed_at=None,
        created_by=center.operator.id,
        created_at=first_body["created_at"],
        updated_at=first_body["updated_at"],
    )
    assert first_body == expected.model_dump(mode="json")
    assert "claim_token" not in first.text
    assert "password" not in first.text.lower()

    repeated = center.send_operator(
        "POST",
        f"{CONNECTORS}/{connector.id}/connection-test",
        headers={"Idempotency-Key": "ui-test-1"},
    )
    assert repeated.status_code == 202
    assert repeated.json() == first_body
    assert len(center.commands.rows) == 1

    status = center.send_operator("GET", f"{COMMANDS}/{first_body['id']}")
    assert status.status_code == 200
    assert status.json() == first_body


def test_only_the_target_host_can_pull_and_report_a_real_result(center: Center) -> None:
    owner = center.hosts.register(name="推理机 A")
    other = center.hosts.register(name="推理机 B")
    connector = _connector(center, owner.id)
    queued = center.send_operator(
        "POST",
        f"{CONNECTORS}/{connector.id}/connection-test",
        headers={"Idempotency-Key": "host-isolation-1"},
    ).json()

    other_claim = center.edge.get(f"{COMMANDS}/next", headers=center.edge_headers(other.id))
    assert other_claim.status_code == 204

    claimed = center.edge.get(f"{COMMANDS}/next", headers=center.edge_headers(owner.id))
    assert claimed.status_code == 200, claimed.text
    claim_body = claimed.json()
    expected_claim = ConnectionTestClaimDocument(
        command=ConnectionTestCommandDocument(
            command_type="test_connector_connection",
            command_id=queued["id"],
            connector_id=str(connector.id),
            connector_revision=connector.revision,
            connector_type=connector.connector_type.value,
            configuration=connector.configuration.to_wire(),
        ),
        claim_token=claim_body["claim_token"],
        lease_expires_at=claim_body["lease_expires_at"],
    )
    assert claim_body == expected_claim.model_dump(mode="json")
    assert "password" not in claimed.text.lower()

    rejected_report = center.edge.post(
        f"{COMMANDS}/{queued['id']}/result",
        headers=center.edge_headers(
            other.id, **{"X-Command-Claim-Token": claim_body["claim_token"]}
        ),
        json={
            "outcome": "reachable",
            "detail": None,
            "credentials_configured": True,
            "failure_code": None,
        },
    )
    assert rejected_report.status_code == 403
    assert rejected_report.json()["error_code"] == "COMMAND_HOST_MISMATCH"
    assert center.connectors.by_id(connector.id) == connector

    completed = center.edge.post(
        f"{COMMANDS}/{queued['id']}/result",
        headers=center.edge_headers(
            owner.id,
            **{"X-Command-Claim-Token": claim_body["claim_token"]},
        ),
        json={
            "outcome": "reachable",
            "detail": None,
            "credentials_configured": True,
            "failure_code": None,
        },
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "succeeded"
    updated = center.connectors.by_id(connector.id)
    assert updated is not None
    assert updated.reachability is ConnectorReachability.REACHABLE
    assert updated.credentials_configured is True


def test_changed_connector_revision_is_rejected_before_command_delivery(center: Center) -> None:
    owner = center.hosts.register(name="推理机 A")
    connector = _connector(center, owner.id)
    queued = center.send_operator(
        "POST",
        f"{CONNECTORS}/{connector.id}/connection-test",
        headers={"Idempotency-Key": "revision-before-claim-1"},
    ).json()

    center.connectors.rows[connector.id] = replace(
        connector,
        name="改过的连接器",
        revision=2,
    )

    claim = center.edge.get(f"{COMMANDS}/next", headers=center.edge_headers(owner.id))
    assert claim.status_code == 204

    status = center.send_operator("GET", f"{COMMANDS}/{queued['id']}")
    assert status.status_code == 200, status.text
    assert status.json()["status"] == "rejected"
    assert status.json()["failure_code"] == "COMMAND_CONFIGURATION_CHANGED"
    assert status.json()["result"] is None


def test_missing_host_credential_cannot_claim_a_command(center: Center) -> None:
    owner = center.hosts.register(name="推理机 A")
    connector = _connector(center, owner.id)
    center.send_operator(
        "POST",
        f"{CONNECTORS}/{connector.id}/connection-test",
        headers={"Idempotency-Key": "missing-credential-1"},
    )

    response = center.edge.get(
        f"{COMMANDS}/next",
        headers={"X-Inference-Host-ID": str(owner.id)},
    )

    assert response.status_code == 401
    assert response.json()["error_code"] == "INFERENCE_HOST_AUTHENTICATION_FAILED"


def test_missing_host_credential_cannot_report_a_command(center: Center) -> None:
    owner = center.hosts.register(name="推理机 A")
    connector = _connector(center, owner.id)
    queued = center.send_operator(
        "POST",
        f"{CONNECTORS}/{connector.id}/connection-test",
        headers={"Idempotency-Key": "missing-report-credential-1"},
    ).json()
    claim = center.edge.get(f"{COMMANDS}/next", headers=center.edge_headers(owner.id)).json()

    response = center.edge.post(
        f"{COMMANDS}/{queued['id']}/result",
        headers={
            "X-Inference-Host-ID": str(owner.id),
            "X-Command-Claim-Token": claim["claim_token"],
        },
        json={
            "outcome": "reachable",
            "detail": None,
            "credentials_configured": True,
            "failure_code": None,
        },
    )

    assert response.status_code == 401
    assert response.json()["error_code"] == "INFERENCE_HOST_AUTHENTICATION_FAILED"


def test_a_host_credential_cannot_be_used_to_claim_another_host_command(
    center: Center,
) -> None:
    owner = center.hosts.register(name="推理机 A")
    other = center.hosts.register(name="推理机 B")
    connector = _connector(center, owner.id)
    center.send_operator(
        "POST",
        f"{CONNECTORS}/{connector.id}/connection-test",
        headers={"Idempotency-Key": "credential-isolation-1"},
    )

    forged = center.edge.get(
        f"{COMMANDS}/next",
        headers=center.edge_headers(owner.id, center.hosts.credential_for(other.id)),
    )

    assert forged.status_code == 401
    assert forged.json()["error_code"] == "INFERENCE_HOST_AUTHENTICATION_FAILED"


def test_an_editor_without_view_cannot_read_a_completed_result_through_idempotent_enqueue(
    center: Center,
) -> None:
    owner = center.hosts.register(name="推理机 A")
    connector = _connector(center, owner.id)
    key = "redacted-completed-result-1"
    queued = center.send_operator(
        "POST",
        f"{CONNECTORS}/{connector.id}/connection-test",
        headers={"Idempotency-Key": key},
    ).json()
    claim = center.edge.get(f"{COMMANDS}/next", headers=center.edge_headers(owner.id)).json()
    completed = center.edge.post(
        f"{COMMANDS}/{queued['id']}/result",
        headers=center.edge_headers(
            owner.id,
            **{"X-Command-Claim-Token": claim["claim_token"]},
        ),
        json={
            "outcome": "unreachable",
            "detail": "真实设备拒绝连接",
            "credentials_configured": True,
            "failure_code": None,
        },
    )
    assert completed.status_code == 200

    center.granted = frozenset({Permission.CONNECTOR_EDIT})
    repeated = center.send_operator(
        "POST",
        f"{CONNECTORS}/{connector.id}/connection-test",
        headers={"Idempotency-Key": key},
    )

    assert repeated.status_code == 202
    assert repeated.json() == {
        **completed.json(),
        "result": None,
        "result_detail": None,
        "failure_code": None,
        "completed_at": completed.json()["completed_at"],
    }


def test_claim_response_is_published_as_the_shared_connection_test_contract(
    center: Center,
) -> None:
    operation = center.app.openapi()["paths"][f"{API_PREFIX}/device-commands/next"]["get"]
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]

    assert schema == {"$ref": "#/components/schemas/ConnectionTestClaimDocument"}
    assert center.app.openapi()["components"]["schemas"]["ConnectionTestClaimDocument"] == {
        "additionalProperties": False,
        "description": "正式发布的领取响应契约，供 OpenAPI 和边缘运行时共同验证。",
        "properties": {
            "command": {"$ref": "#/components/schemas/ConnectionTestCommandDocument"},
            "claim_token": {"type": "string", "title": "Claim Token"},
            "lease_expires_at": {"type": "string", "title": "Lease Expires At"},
        },
        "type": "object",
        "required": ["command", "claim_token", "lease_expires_at"],
        "title": "ConnectionTestClaimDocument",
    }


def test_rejected_edge_result_is_visible_without_changing_connector_status(center: Center) -> None:
    owner = center.hosts.register(name="推理机 A")
    connector = _connector(center, owner.id)
    queued = center.send_operator(
        "POST",
        f"{CONNECTORS}/{connector.id}/connection-test",
        headers={"Idempotency-Key": "rejection-visible-1"},
    ).json()
    claim = center.edge.get(f"{COMMANDS}/next", headers=center.edge_headers(owner.id)).json()

    rejected = center.edge.post(
        f"{COMMANDS}/{queued['id']}/result",
        headers=center.edge_headers(
            owner.id,
            **{"X-Command-Claim-Token": claim["claim_token"]},
        ),
        json={
            "outcome": "rejected",
            "detail": "推理机未配置该连接器凭据",
            "credentials_configured": False,
            "failure_code": "COMMAND_CREDENTIALS_NOT_CONFIGURED",
        },
    )

    assert rejected.status_code == 200
    assert rejected.json() == {
        **PendingCommandView.model_validate(
            center.commands.by_id(UUID(queued["id"])), from_attributes=True
        ).model_dump(mode="json"),
    }
    assert center.connectors.by_id(connector.id) == connector
