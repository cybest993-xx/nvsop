"""AC3, mechanically: every write route declares a permission, and the use case behind it checks
that same permission.

§5.15 puts enforcement in the `usecases/` layer and makes the route's declaration metadata for
OpenAPI. Two declarations of one fact can drift, so this suite is the mechanical check the section
asks for — and it checks the pair by *behaviour*, not by reading the source. For each write route
it sends a request as a caller holding every permission except the declared one; if the use case
does not check what the route advertises, the request succeeds and the test fails.

Two properties together are what make it a gate rather than a sample:

1. **Completeness.** `ROUTES` below is asserted to cover every modifying route the application
   serves. A new write route that nobody adds here fails the suite, so the check cannot be
   outgrown silently.
2. **Enforcement.** Each entry is exercised twice — once without the permission, expecting
   `PERMISSION_DENIED`, and once with it, expecting anything but that. The second half is what
   stops the first from passing for the wrong reason, such as a route that refuses everyone
   because its body never validates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import pytest
from auth_fakes import FakeRoles, FakeSessions, FakeUsers, prepared_password_hash
from device_fakes import (
    DEFAULT_HOST_IDENTITY,
    FakeCameras,
    FakeConnectors,
    FakeInferenceBackends,
    FakeInferenceHosts,
    FakeInferenceStations,
    FakePendingCommands,
    FakePoints,
    FakeProbe,
)
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_dataset_usecases import (
    FakeDatasets as DatasetFakeDatasets,
)
from test_dataset_usecases import (
    FakeJobs as DatasetFakeJobs,
)
from test_dataset_usecases import (
    FakeStorage as DatasetFakeStorage,
)
from test_evidence_usecases import MemoryEvidence
from test_evidence_usecases import _register as register_evidence_reference
from test_evidence_usecases import _registration as evidence_registration

from factory_sop.app import API_PREFIX, MODIFYING_METHODS, create_app
from factory_sop.auth.adapters import dependencies
from factory_sop.auth.adapters.cookies import CSRF_COOKIE, CSRF_HEADER
from factory_sop.auth.adapters.dependencies import DECLARED_PERMISSION
from factory_sop.auth.adapters.dependencies import handover_authority as auth_handover_authority
from factory_sop.auth.model import Role, User, UserStatus
from factory_sop.auth.permissions import Permission
from factory_sop.dataset.adapters import dependencies as dataset_dependencies
from factory_sop.dataset.model import (
    AttemptStatus,
    DatasetMember,
    MemberStatus,
    RetryMode,
    TrainingDataset,
    UploadAttempt,
)
from factory_sop.device.adapters import dependencies as device_dependencies
from factory_sop.device.errors import DeviceRefusalCode, DeviceRefusedError
from factory_sop.device.model import (
    PendingCommand,
    PendingCommandCompletion,
    PendingCommandType,
    PointDirection,
)
from factory_sop.device.usecases.retention import RepositoryDeviceStationFacts
from factory_sop.evidence.adapters import dependencies as evidence_dependencies
from factory_sop.evidence.model import EvidenceReclipRequest, EvidenceReview, EvidenceReviewHold
from factory_sop.execution.adapters import dependencies as execution_dependencies
from factory_sop.execution.model import (
    HANDOVER_RISK_STATEMENT,
    HandoverConfirmation,
    NormalHandover,
    NormalHandoverStatus,
)
from factory_sop.identifiers import new_id
from factory_sop.persistence import request_session
from factory_sop.retention.adapters import dependencies as retention_dependencies
from factory_sop.retention.api import (
    DEFAULT_RETENTION_POLICY,
    EvidenceInstanceUsage,
    ImpactAggregate,
    InstanceRetentionFact,
    RetentionPolicyState,
    StationRetentionRecord,
    StationUsage,
)
from factory_sop.settings import Settings
from factory_sop.template.adapters import dependencies as template_dependencies
from factory_sop.template.model import TemplateImport

# A password long enough to pass `MINIMUM_PASSWORD_LENGTH`, so a refusal in these tests is always
# the authorization one and never the password rule.
PASSWORD = "assembly-line-3"  # pragma: allowlist secret
DATASET_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f101")
DATASET_MEMBER_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f102")
DATASET_ATTEMPT_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f103")
EVIDENCE_ID = "evidence/review.mp4"
REVIEW_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f104")


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
        retention_estimate_ttl_seconds=60,
    )


@dataclass(frozen=True)
class Outcome:
    """Just enough of a response for these assertions: the status, and the `error_code` if any.

    Returned instead of the `httpx.Response` itself so this suite needs no annotation naming a
    package the repository does not declare as a dependency — `httpx` arrives transitively under
    `TestClient`, and importing it here would be depending on somebody else's dependency.
    """

    status_code: int
    error_code: str | None
    body: str


@dataclass(frozen=True)
class Target:
    """One write route, and a body that reaches its use case rather than the validator.

    `headers` carries route preconditions that FastAPI resolves before the handler runs —
    the If-Match of an edit — so the refusal test proves the use case's refusal and not a
    422 for a missing precondition；`raw_body` 覆盖二进制工作簿导入路由。
    """

    method: str
    template: str
    body: dict[str, object] | None = None
    headers: dict[str, str] | None = None
    query: dict[str, str] | None = None
    raw_body: bytes | None = None


# Every modifying route the backend serves, other than the session ones — those are how a caller
# acquires an identity and deliberately require no permission (`auth/usecases/sessions.py`).
# `{user_id}` and `{role_id}` are filled in with the fixture's existing rows. The status `PUT`
# deactivates rather than reactivates: the deactivated branch is the one whose use case does the
# harder thing (revoking every session), and a route that refuses every caller would satisfy the
# enforcement half for the wrong reason.
ROUTES = [
    Target(
        "POST",
        "/auth/users",
        {"login_name": "new.person", "display_name": "新人", "password": PASSWORD},
    ),
    Target("PATCH", "/auth/users/{user_id}", {"display_name": "改名"}),
    Target("PUT", "/auth/users/{user_id}/password", {"password": PASSWORD}),
    Target("PUT", "/auth/users/{user_id}/status", {"status": "deactivated"}),
    # `device`'s host routes. The `{host_id}` template resolves against the store the Backend
    # registers below, so a target reaches the use case instead of dying on an unknown identifier.
    Target(
        "POST",
        "/inference-hosts",
        {
            "name": "装配A线-推理机1",
            "address": "10.0.8.11",
            "mediamtx_address": None,
            "recording_window_seconds": 7 * 24 * 3600,
            "disk_watermark_percent": 85,
        },
    ),
    Target(
        "PATCH",
        "/inference-hosts/{host_id}",
        {
            "name": "装配A线-推理机1",
            "address": "10.0.8.12",
            "mediamtx_address": None,
            "recording_window_seconds": 7 * 24 * 3600,
            "disk_watermark_percent": 85,
        },
        headers={"If-Match": "1"},
    ),
    Target(
        "PUT",
        "/inference-hosts/{host_id}/status",
        {"status": "deactivated"},
        headers={"If-Match": "1"},
    ),
    Target(
        "POST",
        "/inference-hosts/{host_id}/credential",
        headers={"If-Match": "1"},
    ),
    Target(
        "POST",
        "/inference-hosts/{host_id}/identity-key",
        {"public_key": DEFAULT_HOST_IDENTITY.public_key},
        headers={"If-Match": "1"},
    ),
    Target("DELETE", "/inference-hosts/{host_id}", headers={"If-Match": "1"}),
    Target(
        "POST",
        "/inference-backends",
        {"host_id": "{host_id}", "base_url": "http://10.0.8.11:8001"},
    ),
    Target(
        "PATCH",
        "/inference-backends/{backend_id}",
        {"host_id": "{host_id}", "base_url": "http://10.0.8.11:8001"},
        headers={"If-Match": "1"},
    ),
    Target(
        "PUT",
        "/inference-backends/{backend_id}/status",
        {"status": "deactivated"},
        headers={"If-Match": "1"},
    ),
    Target(
        "POST",
        "/inference-backends/{backend_id}/connection-test",
        headers={"If-Match": "1"},
    ),
    Target("DELETE", "/inference-backends/{backend_id}", headers={"If-Match": "1"}),
    Target("POST", "/stations", {"code": "station-2", "name": "新工位", "tags": []}),
    Target(
        "PATCH",
        "/stations/{station_id}",
        {"code": "station-1", "name": "修改工位", "tags": []},
        headers={"If-Match": "1"},
    ),
    Target(
        "PUT",
        "/stations/{station_id}/status",
        {"status": "deactivated"},
        headers={"If-Match": "1"},
    ),
    Target("DELETE", "/stations/{station_id}", headers={"If-Match": "1"}),
    Target(
        "POST",
        "/cameras",
        {
            "name": "新相机",
            "address": "10.0.8.22",
            "main_stream_path": "/Streaming/Channels/101",
            "sub_stream_path": "/Streaming/Channels/102",
            "station_id": "{station_id}",
            "host_id": "{host_id}",
            "backend_id": "{backend_id}",
        },
    ),
    Target(
        "PATCH",
        "/cameras/{camera_id}",
        {
            "name": "修改相机",
            "address": "10.0.8.23",
            "main_stream_path": "/Streaming/Channels/101",
            "sub_stream_path": "/Streaming/Channels/102",
            "station_id": "{station_id}",
            "host_id": "{host_id}",
            "backend_id": "{backend_id}",
        },
        headers={"If-Match": "1"},
    ),
    Target(
        "PUT",
        "/cameras/{camera_id}/status",
        {"status": "deactivated"},
        headers={"If-Match": "1"},
    ),
    Target("DELETE", "/cameras/{camera_id}", headers={"If-Match": "1"}),
    Target(
        "POST",
        "/connectors",
        {
            "name": "新连接器",
            "connector_type": "hikvision_isapi",
            "configuration": {"address": "10.0.8.24", "port": 80},
            "station_id": "{station_id}",
            "host_id": "{host_id}",
        },
    ),
    Target(
        "POST",
        "/connectors/{connector_id}/connection-test",
        headers={"Idempotency-Key": "authorization-command-check"},
    ),
    Target(
        "PATCH",
        "/connectors/{connector_id}",
        {
            "name": "修改连接器",
            "connector_type": "board_card",
            "configuration": {"address": "/dev/board0"},
            "station_id": "{station_id}",
            "host_id": "{host_id}",
        },
        headers={"If-Match": "1"},
    ),
    Target(
        "PUT",
        "/connectors/{connector_id}/status",
        {"status": "deactivated"},
        headers={"If-Match": "1"},
    ),
    Target(
        "PUT",
        "/connectors/{connector_id}/capability",
        {"verification": "unverified"},
        headers={"If-Match": "1"},
    ),
    Target("DELETE", "/connectors/{connector_id}", headers={"If-Match": "1"}),
    Target(
        "POST",
        "/points",
        {
            "station_id": "{station_id}",
            "connector_id": "{connector_id}",
            "direction": "input",
            "identifier": "DI-02",
            "semantic_label": "新点位",
        },
    ),
    Target(
        "PATCH",
        "/points/{point_id}",
        {
            "station_id": "{station_id}",
            "connector_id": "{connector_id}",
            "direction": "input",
            "identifier": "DI-01",
            "semantic_label": "修改点位",
        },
        headers={"If-Match": "1"},
    ),
    Target(
        "PUT",
        "/points/{point_id}/status",
        {"status": "deactivated"},
        headers={"If-Match": "1"},
    ),
    Target("DELETE", "/points/{point_id}", headers={"If-Match": "1"}),
    Target(
        "POST",
        "/templates/imports",
        headers={
            "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        },
        query={"filename": "invalid.xlsx"},
        raw_body=b"not an xlsx",
    ),
    Target(
        "PATCH",
        "/templates/drafts/{draft_id}",
        {
            "steps": [{"number": 1, "name": "取料", "description": "(1)取料"}],
            "ordering": "strict",
            "runtime_defaults": {
                "idle_timeout_seconds": None,
                "step_deadline_seconds": None,
                "disposition_policy": None,
            },
        },
        headers={"If-Match": "1"},
    ),
    Target(
        "POST",
        "/templates/drafts/{draft_id}/publish",
        headers={"If-Match": "1"},
    ),
    Target(
        "POST",
        "/templates/bindings/validate",
        {
            "station_id": "{station_id}",
            "version_id": "00000000-0000-0000-0000-000000000001",
            "runtime_parameter_mode": "follow_template",
        },
    ),
    Target(
        "POST",
        "/templates/bindings",
        {
            "station_id": "{station_id}",
            "version_id": "00000000-0000-0000-0000-000000000001",
            "runtime_parameter_mode": "follow_template",
        },
        headers={"If-Match": "1"},
    ),
    Target(
        "PUT",
        "/templates/stations/{station_id}/runtime-parameters",
        {"mode": "follow_template"},
        headers={"If-Match": "1"},
    ),
    Target(
        "POST",
        "/point-binding-validations",
        {
            "station_id": "{station_id}",
            "role": "start_signal",
            "budget_seconds": 0.2,
        },
    ),
    Target("PUT", "/auth/users/{user_id}/roles", {"role_ids": []}),
    Target("DELETE", "/auth/users/{user_id}"),
    Target("POST", "/auth/roles", {"code": "fresh", "name": "新角色", "permissions": []}),
    Target("PUT", "/auth/roles/{role_id}", {"name": "改名", "permissions": []}),
    Target("DELETE", "/auth/roles/{role_id}"),
    Target("POST", "/training-datasets", {"name": "新的训练集"}),
    Target(
        "POST",
        "/training-datasets/{dataset_id}/action-list",
        {"actions": ["(1) 取料"]},
    ),
    Target(
        "POST",
        "/training-datasets/{dataset_id}/members/{member_id}/annotation-context",
        {},
    ),
    Target(
        "POST",
        "/training-datasets/{dataset_id}/members/{member_id}/annotations",
        {
            "context_token": "invalid-context",
            "mode": "single_operator",
            "segments": [{"start": 0.0, "end": 0.5, "action_index": 0}],
        },
        headers={"Idempotency-Key": "authorization-annotation", "If-Match": "0"},
    ),
    Target(
        "POST",
        "/training-datasets/{dataset_id}/members/{member_id}/annotations/{submission_id}/retry",
    ),
    Target(
        "POST",
        "/training-datasets/{dataset_id}/members",
        {
            "original_filename": "sample.mp4",
            "source": "授权测试",
            "declared_size": 1,
            "declared_sha256": "a" * 64,
        },
    ),
    Target(
        "POST",
        "/training-datasets/{dataset_id}/members/{member_id}/confirm",
        {"attempt_id": "{attempt_id}"},
    ),
    Target(
        "POST",
        "/training-datasets/{dataset_id}/members/{member_id}/retry",
        {"mode": RetryMode.UPLOAD.value},
    ),
    Target(
        "PUT",
        "/training-datasets/{dataset_id}/members/{member_id}/attempts/{attempt_id}/content",
        raw_body=b"",
    ),
    Target(
        "POST",
        "/training-datasets/{dataset_id}/vlm-candidates",
        {
            "kind": "gqa",
            "action_list_revision": 1,
            "records": [],
            "media": [],
        },
        headers={"If-Match": "0"},
    ),
    Target(
        "POST",
        "/training-datasets/{dataset_id}/usage-checks",
        {"kind": "ddm"},
    ),
    Target(
        "POST",
        "/training-datasets/{dataset_id}/artifacts",
        {"check_id": "{attempt_id}"},
    ),
    Target(
        "POST",
        "/training-datasets/{dataset_id}/ddm-training-commands",
        {
            "training": {
                "dataset_id": "019937d8-0d10-7b31-8d2d-4e60c8f4f101",
                "usage_check_id": "019937d8-0d10-7b31-8d2d-4e60c8f4f302",
                "artifact_id": "019937d8-0d10-7b31-8d2d-4e60c8f4f303",
            },
            "validation": {
                "dataset_id": "019937d8-0d10-7b31-8d2d-4e60c8f4f304",
                "usage_check_id": "019937d8-0d10-7b31-8d2d-4e60c8f4f305",
                "artifact_id": "019937d8-0d10-7b31-8d2d-4e60c8f4f306",
            },
        },
        headers={"Idempotency-Key": "authz-ddm"},
    ),
    Target(
        "POST",
        "/execution/handovers",
        {
            "station_id": "{station_id}",
            "from_host_id": "{host_id}",
            "to_host_id": "{host_id}",
            "risk_acknowledgement": HANDOVER_RISK_STATEMENT,
        },
    ),
    Target(
        "POST",
        "/execution/handovers/{handover_id}/confirmation",
        {
            "station_id": "{station_id}",
            "from_host_id": "{host_id}",
            "to_host_id": "{host_id}",
            "risk_acknowledgement": HANDOVER_RISK_STATEMENT,
        },
    ),
    Target(
        "POST",
        "/execution/normal-handovers",
        {
            "station_id": "{station_id}",
            "from_host_id": "{host_id}",
            "to_host_id": "{host_id}",
        },
        headers={"Idempotency-Key": "authz-normal-handover"},
    ),
    Target("POST", "/execution/normal-handovers/{handover_id}/cancellation", {}),
    Target(
        "POST",
        "/evidence/{evidence_id}/reviews",
        {"review_id": str(REVIEW_ID), "generation": "original", "conclusion": "confirmed"},
    ),
    Target(
        "POST",
        "/evidence/{evidence_id}/reclip-requests",
        {
            "request_id": str(REVIEW_ID),
            "generation": "original",
            "before_seconds": 1.0,
            "after_seconds": 1.0,
        },
    ),
    Target(
        "POST",
        "/evidence/{evidence_id}/review-holds",
        {"review_id": str(REVIEW_ID), "generation": "original"},
    ),
    Target(
        "POST",
        "/evidence/{evidence_id}/review-holds/cancel",
        {"review_id": str(REVIEW_ID)},
    ),
    Target(
        "POST",
        "/retention/policy/impact-estimate",
        {"policy": DEFAULT_RETENTION_POLICY.to_wire()},
    ),
    Target(
        "POST",
        "/retention/stations/{station_id}/impact-estimate",
        {"mode": "custom", "overrides": DEFAULT_RETENTION_POLICY.to_wire()},
    ),
    Target(
        "PUT",
        "/retention/policy",
        {"policy": DEFAULT_RETENTION_POLICY.to_wire()},
        headers={"If-Match": "0", "X-Retention-Estimate": "not-a-token"},
    ),
    Target(
        "PUT",
        "/retention/stations/{station_id}",
        {"mode": "custom", "overrides": DEFAULT_RETENTION_POLICY.to_wire()},
        headers={"If-Match": "0", "X-Retention-Estimate": "not-a-token"},
    ),
]


# The session resource: no permission, by design, and therefore not part of the check above.
EXEMPT = {
    ("POST", f"{API_PREFIX}/auth/session"),
    ("DELETE", f"{API_PREFIX}/auth/session"),
    ("POST", f"{API_PREFIX}/device-commands/{{command_id}}/result"),
    # 再切片结果回报同样是主机签名机器端点, 不接受浏览器会话或人类权限。
    ("POST", f"{API_PREFIX}/device-commands/{{command_id}}/reclip-result"),
    # 主机配置上报用公钥签名认证，不接受浏览器会话或人类权限。
    ("POST", f"{API_PREFIX}/templates/configuration-reports"),
    # Historical configuration confirmation is also a host-signed machine endpoint.
    ("POST", f"{API_PREFIX}/inference-hosts/{{host_id}}/confirmed-configuration"),
    # Edge report intake is authenticated by the registered host key, not a browser permission.
    ("POST", f"{API_PREFIX}/monitor/reported-decisions"),
    ("POST", f"{API_PREFIX}/monitor/reported-decisions/enveloped"),
    ("POST", f"{API_PREFIX}/monitor/health"),
    ("POST", f"{API_PREFIX}/monitor/reported-instances"),
    ("POST", f"{API_PREFIX}/monitor/reported-disposals"),
    ("POST", f"{API_PREFIX}/monitor/reported-observations"),
    ("POST", f"{API_PREFIX}/monitor/execution-authority"),
    # Evidence reference registration authenticates the inference host signature.
    ("POST", f"{API_PREFIX}/evidence/registrations"),
    # 来源机逐次回查要求主机签名，同时复核票据绑定的真实会话与权限。
    ("POST", f"{API_PREFIX}/evidence/access-check"),
    # 证据删除资格的 claim/verify/result 同样只认主机签名，不接受浏览器会话或人类权限。
    ("POST", f"{API_PREFIX}/retention/evidence-deletion-claims"),
    ("POST", f"{API_PREFIX}/retention/evidence-deletion-claims/verify"),
    ("POST", f"{API_PREFIX}/retention/evidence-deletion-claims/result"),
    ("POST", f"{API_PREFIX}/retention/reported-impact/access-check"),
    # 正常交权停机命令的领取与回报同样只认主机签名 (#214)。
    ("GET", f"{API_PREFIX}/device-commands/execution-handover/next"),
    ("POST", f"{API_PREFIX}/device-commands/{{command_id}}/execution-handover-result"),
}


class AuthorizationPendingCommands(FakePendingCommands):
    """只为授权机械测试保存入队结果, 不模拟推理机领取或硬件。"""

    def claim_next(
        self,
        *,
        host_id: UUID,
        command_type: PendingCommandType,
        claim_token: str,
        claimed_at: datetime,
        lease_expires_at: datetime,
    ) -> PendingCommand | None:
        del host_id, command_type, claim_token, claimed_at, lease_expires_at
        return None

    def complete(self, completion: PendingCommandCompletion) -> PendingCommand:
        del completion
        raise AssertionError("authorization route test does not complete a command")


class FakeRetentionPolicies:
    """授权机械测试只需保存一次全局策略写入，不触碰数据库。"""

    def __init__(self) -> None:
        self.state: RetentionPolicyState | None = None

    def get_global(self) -> RetentionPolicyState | None:
        return self.state

    def replace_if_current(self, *, expected_revision: int, value: RetentionPolicyState) -> bool:
        current = 0 if self.state is None else self.state.revision
        if current != expected_revision:
            return False
        self.state = value
        return True


class FakeStationRetentionOverrides:
    """内存工位覆盖记录，让两条写路由走到真实用例而不是只解析依赖。"""

    def __init__(self) -> None:
        self.records: dict[UUID, StationRetentionRecord] = {}

    def get(self, station_id: UUID) -> StationRetentionRecord | None:
        return self.records.get(station_id)

    def list_all(self) -> tuple[StationRetentionRecord, ...]:
        return tuple(self.records.values())

    def insert_if_absent(self, record: StationRetentionRecord) -> bool:
        if record.station_id in self.records:
            return False
        self.records[record.station_id] = record
        return True

    def replace_if_current(self, *, expected_revision: int, record: StationRetentionRecord) -> bool:
        current = self.records.get(record.station_id)
        if current is None or current.revision != expected_revision:
            return False
        self.records[record.station_id] = record
        return True


def _empty_aggregate(*, bytes_measurable: bool) -> ImpactAggregate:
    return ImpactAggregate(
        objects=0,
        known_bytes=0 if bytes_measurable else None,
        unknown_size_objects=0,
        earliest=None,
        latest=None,
    )


class FakeEvidenceImpact:
    """授权机械测试只需解析依赖；估算返回零影响，不触碰真实证据。"""

    def evidence_usage(
        self, *, station_id: object, exposed_from: object, exposed_to: object
    ) -> tuple[EvidenceInstanceUsage, ...]:
        del station_id, exposed_from, exposed_to
        return ()

    def review_usage(
        self, *, station_id: object, exposed_from: object, exposed_to: object
    ) -> tuple[object, ...]:
        del station_id, exposed_from, exposed_to
        return ()


class FakeMonitorImpact:
    """授权机械测试只需解析依赖；实例事实与记录/处置计数都返回零影响。"""

    def instance_facts(
        self, *, instance_ids: tuple[tuple[UUID, UUID, int], ...]
    ) -> tuple[InstanceRetentionFact, ...]:
        del instance_ids
        return ()

    def record_usage(
        self, *, station_id: object, exposed_from: object, exposed_to: object
    ) -> tuple[StationUsage, ...]:
        del station_id, exposed_from, exposed_to
        station = new_id()
        return (
            StationUsage(
                station_id=station,
                exposed=_empty_aggregate(bytes_measurable=True),
                protected=_empty_aggregate(bytes_measurable=True),
            ),
        )

    def disposal_usage(
        self, *, station_id: object, exposed_from: object, exposed_to: object
    ) -> tuple[StationUsage, ...]:
        del station_id, exposed_from, exposed_to
        return ()


class TemplateStore:
    """授权测试只需保存一次无效导入尝试。"""

    def __init__(self) -> None:
        self.imports: list[TemplateImport] = []

    def add_import(self, record: TemplateImport) -> None:
        self.imports.append(record)

    def draft_by_id(self, _draft_id: object) -> None:
        return None

    def draft_for_publish(self, _draft_id: object) -> None:
        return None

    def version_by_id(self, _version_id: object) -> None:
        return None

    def template_by_id(self, _template_id: object) -> None:
        return None

    def binding_by_station(self, _station_id: object) -> None:
        return None

    def version_by_source(self, *, draft_id: object, revision: int) -> None:
        del draft_id, revision
        return None


class BindingGateway:
    """让授权测试在通过权限后得到稳定的资源拒绝，而不是触碰数据库。"""

    def read_station_runtime_parameters(self, *_args: object, **_kwargs: object) -> None:
        raise DeviceRefusedError(DeviceRefusalCode.STATION_NOT_FOUND)


class StubRequestSession:
    """流式上传路由在读取请求体前结束事务；授权套件不需要真实数据库连接。"""

    def rollback(self) -> None:
        return None


class FakeDeletionAuthorityFence:
    """授权机械测试不建立真实删除认领；权威锁与检查是无操作。"""

    def lock_authority(self) -> None:
        return None

    def ensure_mutable(self) -> None:
        return None


class FakeHandoverAuthority:
    """授权机械测试的持有者集合，不触碰真实管理锁。"""

    def __init__(self, holders: frozenset[UUID]) -> None:
        self._holders = holders

    def lock_and_read_holders(self) -> frozenset[UUID]:
        return self._holders


class FakeHandovers:
    """内存确认记录，让创建路由在授权通过后走完 use case。"""

    def __init__(self) -> None:
        self.records: dict[UUID, HandoverConfirmation] = {}

    def add(self, value: HandoverConfirmation) -> None:
        self.records[value.handover_id] = value

    def by_identifier(self, handover_id: UUID) -> HandoverConfirmation | None:
        return self.records.get(handover_id)

    def confirm_second(self, value: HandoverConfirmation) -> HandoverConfirmation | None:
        del value
        return None


class FakeNormalHandovers:
    """正常交权状态机的内存 fake: 取消路由只需读取/保存到真实拒绝或幂等分支。"""

    def __init__(self) -> None:
        self.records: dict[UUID, NormalHandover] = {}

    def by_identifier_for_update(self, handover_id: UUID) -> NormalHandover | None:
        return self.records.get(handover_id)

    def by_idempotency_key(self, key: str) -> NormalHandover | None:
        return next((r for r in self.records.values() if r.idempotency_key == key), None)

    def pending_for_station(self, station_id: UUID) -> NormalHandover | None:
        return next(
            (
                r
                for r in self.records.values()
                if r.station_id == station_id and r.status is NormalHandoverStatus.PENDING
            ),
            None,
        )

    def add_or_get(self, value: NormalHandover) -> tuple[NormalHandover, bool]:
        self.records[value.handover_id] = value
        return value, True

    def save(self, value: NormalHandover) -> NormalHandover:
        self.records[value.handover_id] = value
        return value


class FakeExecutionGrants:
    """机械授权测试没有真实租约: 交权取消/读取路径不读取执行权。"""

    def for_station(self, station_id: UUID) -> None:
        return None

    def for_stations(self, station_ids: tuple[UUID, ...]) -> tuple[object, ...]:
        return ()

    def for_holder(self, host_id: UUID) -> tuple[object, ...]:
        return ()


class FakeNormalHandoverCommands:
    """正常交权命令队列 fake: 取消路由不命令队列。"""

    def enqueue(self, **kwargs: object) -> None:
        del kwargs

    def claim(self, **kwargs: object) -> None:
        return None

    def complete(self, **kwargs: object) -> None:
        return None


class FakeEvidenceReviews:
    """内存只追加复核仓储，足够让授权用例跑到真实拒绝/成功分支。"""

    def __init__(self) -> None:
        self.records: dict[UUID, EvidenceReview] = {}

    def insert(self, value: EvidenceReview) -> bool:
        if value.review_id in self.records:
            return False
        self.records[value.review_id] = value
        return True

    def find(self, review_id: UUID) -> EvidenceReview | None:
        return self.records.get(review_id)

    def list_for_evidence(self, evidence_id: str) -> tuple[EvidenceReview, ...]:
        return tuple(row for row in self.records.values() if row.evidence_id == evidence_id)

    def conclude_own(self, *, evidence_id: str, reviewer_id: UUID, at: datetime) -> int:
        """授权机械测试不保留在审占用；结论提交只跑到真实成功分支。"""
        del evidence_id, reviewer_id, at
        return 0

    def begin(self, value: EvidenceReviewHold) -> bool:
        del value
        return True

    def cancel(self, *, review_id: UUID, reviewer_id: UUID, at: datetime) -> bool:
        del review_id, reviewer_id, at
        return True

    def active_reviewers(self, *, evidence_id: str) -> frozenset[UUID]:
        del evidence_id
        return frozenset()


class FakeEvidenceReclipRequests:
    """内存再切片请求仓储，足够让授权用例跑到真实拒绝/成功分支。"""

    def __init__(self) -> None:
        self.records: dict[UUID, EvidenceReclipRequest] = {}

    def insert(self, value: EvidenceReclipRequest) -> bool:
        if value.request_id in self.records:
            return False
        self.records[value.request_id] = value
        return True

    def find(self, request_id: UUID) -> EvidenceReclipRequest | None:
        return self.records.get(request_id)

    def list_for_evidence(self, evidence_id: str) -> tuple[EvidenceReclipRequest, ...]:
        return tuple(row for row in self.records.values() if row.evidence_id == evidence_id)


class FakeEvidenceReclipGateway:
    """授权机械测试只解析依赖；证据缺冻结元数据时用例不会触及网关。"""

    def recording_window_seconds(self, *, host_id: UUID) -> int | None:
        del host_id
        return 604800

    def enqueue_reclip_command(
        self,
        *,
        host_id: UUID,
        request_id: UUID,
        idempotency_key: str,
        actor_id: UUID,
        now: object,
    ) -> UUID:
        del host_id, idempotency_key, actor_id, now
        return request_id


class Backend:
    """使用内存适配器构造可控权限的应用。"""

    def __init__(self) -> None:
        self.users = FakeUsers()
        self.roles = FakeRoles(users=self.users)
        self.sessions = FakeSessions()
        self.granted: frozenset[Permission] = frozenset()

        # The account the requests are made as. Its permissions come from `self.granted` rather
        # than from an assigned role, so a test can hold "everything except one" without building
        # a role for each combination.
        self.actor = User(
            id=new_id(),
            login_name="administrator",
            display_name="系统管理员",
            password_hash=prepared_password_hash(PASSWORD),
            status=UserStatus.ACTIVE,
        )
        self.users.add(self.actor)
        # A second account and a role for the `{user_id}` and `{role_id}` templates: the target of
        # the operation is never the caller, so a refusal is never the last-administrator guard.
        self.subject = self.users.register(login_name="wang.li", password=PASSWORD)
        self.role = Role(id=new_id(), code="viewer", name="只读", permissions=frozenset())
        self.roles.add(self.role)

        self.app = create_app(settings())
        # A stored backend reference makes the host-delete target reach its history guard, and
        # the same row makes every backend write target a real resource for this suite.
        self.hosts = FakeInferenceHosts()
        self.backends_store = FakeInferenceBackends()
        self.host = self.hosts.register(name="装配A线-推理机1")
        self.backend = self.backends_store.register(
            host_id=self.host.id, base_url="http://10.0.8.11:8000"
        )
        self.probe = FakeProbe()
        self.stations = FakeInferenceStations()
        self.cameras = FakeCameras()
        self.connectors = FakeConnectors()
        self.points = FakePoints()
        self.template_store = TemplateStore()
        self.station = self.stations.register(code="station-1", name="工位一")
        self.camera = self.cameras.register(
            station_id=self.station.id, host_id=self.host.id, backend_id=self.backend.id
        )
        self.connector = self.connectors.register(station_id=self.station.id, host_id=self.host.id)
        self.pending_commands = AuthorizationPendingCommands()
        self.point = self.points.register(
            station_id=self.station.id,
            connector_id=self.connector.id,
            direction=PointDirection.INPUT,
            identifier="DI-01",
            semantic_label="工件到位",
        )
        self.dataset_store = DatasetFakeDatasets()
        self.dataset_store.add_dataset(
            TrainingDataset(
                id=DATASET_ID,
                name="授权测试训练集",
                created_by=self.actor.id,
                updated_by=self.actor.id,
                created_at=datetime(2026, 9, 9),
                updated_at=datetime(2026, 9, 9),
            )
        )
        self.dataset_store.add_member(
            DatasetMember(
                id=DATASET_MEMBER_ID,
                dataset_id=DATASET_ID,
                original_filename="failed.mp4",
                source="授权测试",
                declared_size=1,
                declared_sha256="a" * 64,
                current_attempt_id=DATASET_ATTEMPT_ID,
                status=MemberStatus.FAILED,
                actual_size=1,
                actual_sha256="b" * 64,
                duration_seconds=None,
                codec=None,
                container=None,
                object_key=None,
                validation_job_id=None,
                failure_code="SHA256_MISMATCH",
                failure_detail="授权测试失败",
                recovery_action=RetryMode.UPLOAD.value,
                created_by=self.actor.id,
                updated_by=self.actor.id,
                created_at=datetime(2026, 9, 9),
                updated_at=datetime(2026, 9, 9),
            )
        )
        self.dataset_store.add_attempt(
            UploadAttempt(
                id=DATASET_ATTEMPT_ID,
                dataset_id=DATASET_ID,
                member_id=DATASET_MEMBER_ID,
                idempotency_key=None,
                object_key="training-datasets/test/member/attempt/video",
                declared_size=1,
                declared_sha256="a" * 64,
                expires_at=datetime(2026, 9, 9),
                status=AttemptStatus.FAILED,
                created_at=datetime(2026, 9, 9),
                validation_job_id=None,
                final_object_key=None,
            )
        )
        self.dataset_storage = DatasetFakeStorage()
        self.dataset_jobs = DatasetFakeJobs()
        self.handover_authority = FakeHandoverAuthority(frozenset({self.actor.id}))
        self.handovers = FakeHandovers()
        self.normal_handovers = FakeNormalHandovers()
        self.execution_grants = FakeExecutionGrants()
        self.normal_handover_commands = FakeNormalHandoverCommands()
        # 复用 evidence 用例测试的完整 Protocol fake 及其登记 helper，不再另造只读替身。
        self.evidence = MemoryEvidence()
        register_evidence_reference(self.evidence, evidence_registration(evidence_id=EVIDENCE_ID))
        self.evidence_reviews = FakeEvidenceReviews()
        self.evidence_reclip_requests = FakeEvidenceReclipRequests()
        self.evidence_reclip_gateway = FakeEvidenceReclipGateway()

        self.app.dependency_overrides[dependencies.users] = lambda: self.users
        self.app.dependency_overrides[dependencies.sessions] = lambda: self.sessions
        self.app.dependency_overrides[dependencies.roles] = lambda: self.roles
        self.app.dependency_overrides[dependencies.granted_permissions] = lambda: self.granted
        self.retention_policies = FakeRetentionPolicies()
        self.retention_overrides = FakeStationRetentionOverrides()
        self.app.dependency_overrides[device_dependencies.hosts] = lambda: self.hosts

        self.app.dependency_overrides[device_dependencies.backends] = lambda: self.backends_store
        self.app.dependency_overrides[device_dependencies.probe] = lambda: self.probe
        self.app.dependency_overrides[device_dependencies.stations] = lambda: self.stations
        self.app.dependency_overrides[device_dependencies.cameras] = lambda: self.cameras
        self.app.dependency_overrides[device_dependencies.connectors] = lambda: self.connectors
        self.app.dependency_overrides[device_dependencies.pending_commands] = lambda: (
            self.pending_commands
        )
        self.app.dependency_overrides[device_dependencies.points] = lambda: self.points
        self.app.dependency_overrides[template_dependencies.stations] = lambda: self.stations
        self.app.dependency_overrides[template_dependencies.templates] = lambda: self.template_store
        # 绑定/参数路由在权限机械测试中只需解析依赖；未知版本/资源会在 handler 内
        # 给出非 403 结果，避免把数据库装配错误误判为授权通过。
        self.app.dependency_overrides[template_dependencies.binding_gateway] = BindingGateway
        self.app.dependency_overrides[dataset_dependencies.datasets] = lambda: self.dataset_store
        self.app.dependency_overrides[dataset_dependencies.storage] = lambda: self.dataset_storage
        self.app.dependency_overrides[dataset_dependencies.jobs] = lambda: self.dataset_jobs
        self.app.dependency_overrides[dataset_dependencies.annotation_jobs] = lambda: (
            self.dataset_jobs
        )
        self.app.dependency_overrides[dataset_dependencies.usage_jobs] = lambda: self.dataset_jobs
        self.app.dependency_overrides[execution_dependencies.handovers] = lambda: self.handovers
        # 正常交权: 状态机 fake 与 grants/命令 fake, 让取消路由跑到真实拒绝/幂等分支。
        self.app.dependency_overrides[execution_dependencies.normal_handovers] = lambda: (
            self.normal_handovers
        )
        self.app.dependency_overrides[execution_dependencies.grants] = lambda: self.execution_grants
        self.app.dependency_overrides[execution_dependencies.normal_handover_commands] = lambda: (
            self.normal_handover_commands
        )
        self.app.dependency_overrides[evidence_dependencies.evidence] = lambda: self.evidence
        self.app.dependency_overrides[evidence_dependencies.reviews] = lambda: self.evidence_reviews
        self.app.dependency_overrides[evidence_dependencies.reclip_requests] = lambda: (
            self.evidence_reclip_requests
        )
        self.app.dependency_overrides[evidence_dependencies.reclip_gateway] = lambda: (
            self.evidence_reclip_gateway
        )
        self.app.dependency_overrides[auth_handover_authority] = lambda: self.handover_authority
        self.app.dependency_overrides[retention_dependencies.policy_repository] = lambda: (
            self.retention_policies
        )
        self.app.dependency_overrides[retention_dependencies.station_overrides] = lambda: (
            self.retention_overrides
        )
        # 组合根把工位存在/启用事实 seam 接到 device owner；这里把它接到内存工位存储。
        self.app.dependency_overrides[retention_dependencies.station_facts] = lambda: (
            RepositoryDeviceStationFacts(stations=self.stations)
        )
        # 影响估算的 owner 计数 seam 同样在测试里接到内存替身。
        self.evidence_impact = FakeEvidenceImpact()
        self.monitor_impact = FakeMonitorImpact()
        from test_retention_impact import FakeEdgeImpact

        self.edge_impact = FakeEdgeImpact()
        self.app.dependency_overrides[retention_dependencies.edge_impact] = lambda: self.edge_impact
        self.app.dependency_overrides[retention_dependencies.evidence_impact] = lambda: (
            self.evidence_impact
        )
        self.app.dependency_overrides[retention_dependencies.monitor_impact] = lambda: (
            self.monitor_impact
        )
        self.app.dependency_overrides[request_session] = StubRequestSession
        self.app.dependency_overrides[device_dependencies.deletion_authority_fence] = lambda: (
            FakeDeletionAuthorityFence()
        )
        self.app.dependency_overrides[retention_dependencies.deletion_authority_fence] = lambda: (
            FakeDeletionAuthorityFence()
        )
        self.client = TestClient(self.app, base_url="https://testserver")
        assert (
            self.client.post(
                f"{API_PREFIX}/auth/session",
                json={"login_name": "administrator", "password": PASSWORD},
            ).status_code
            == 201
        )

    def send(self, target: Target, *, granted: frozenset[Permission]) -> Outcome:
        self.granted = granted
        identifiers = {
            "user_id": self.subject.id,
            "role_id": self.role.id,
            "host_id": self.host.id,
            "backend_id": self.backend.id,
            "station_id": self.station.id,
            "camera_id": self.camera.id,
            "connector_id": self.connector.id,
            "point_id": self.point.id,
            "draft_id": UUID(int=1),
            "dataset_id": DATASET_ID,
            "member_id": DATASET_MEMBER_ID,
            "attempt_id": DATASET_ATTEMPT_ID,
            "submission_id": DATASET_ATTEMPT_ID,
            "handover_id": UUID(int=2),
            "evidence_id": EVIDENCE_ID,
        }
        path = target.template.format(**identifiers)

        def format_body_value(value: object) -> object:
            if not isinstance(value, str):
                return value
            try:
                return value.format(**identifiers)
            except KeyError:
                return value

        body = (
            {key: format_body_value(value) for key, value in target.body.items()}
            if target.body is not None
            else None
        )
        headers = {CSRF_HEADER: self.client.cookies[CSRF_COOKIE], **(target.headers or {})}
        if target.raw_body is not None:
            response = self.client.request(
                target.method,
                f"{API_PREFIX}{path}",
                content=target.raw_body,
                params=target.query,
                headers=headers,
            )
        else:
            response = self.client.request(
                target.method,
                f"{API_PREFIX}{path}",
                json=body,
                params=target.query,
                headers=headers,
            )
        error_code: str | None = None
        if response.content:
            document = response.json()
            if isinstance(document, dict):
                code = document.get("error_code")
                error_code = code if isinstance(code, str) else None
        return Outcome(status_code=response.status_code, error_code=error_code, body=response.text)


@pytest.fixture
def backend() -> Backend:
    return Backend()


def declarations(backend: Backend) -> dict[tuple[str, str], str | list[str] | None]:
    """Every modifying operation the application serves, and the permission it declares.

    Read out of the generated OpenAPI document rather than off `app.routes`: the declaration exists
    to appear in that document, so this asserts on what an integrator actually receives.
    """
    document = backend.app.openapi()
    found: dict[tuple[str, str], str | list[str] | None] = {}
    for path, operations in document["paths"].items():
        for method, operation in operations.items():
            if method.upper() in MODIFYING_METHODS:
                declared = operation.get(DECLARED_PERMISSION)
                assert declared is None or isinstance(declared, (str, list))
                found[(method.upper(), path)] = declared
    return found


def test_every_write_route_is_covered_by_this_suite(backend: Backend) -> None:
    # Without this, the enforcement test below is a sample of whatever someone remembered to list.
    # With it, a new write route fails the suite until it is listed and proved.
    declared = {(target.method, f"{API_PREFIX}{target.template}") for target in ROUTES}

    served = set(declarations(backend))
    # Non-empty as its own assertion: an enumeration that silently found nothing would make this
    # comparison pass between two empty sets, which is exactly the failure this suite must not have.
    assert served
    assert served - EXEMPT == declared


def test_every_write_route_declares_the_permission_it_needs(backend: Backend) -> None:
    # The declaration is what OpenAPI shows and what an integrator reads. A route without one is a
    # route whose permission is only discoverable by reading the use case.
    registered = {item.value for item in Permission}
    for (method, path), declared in declarations(backend).items():
        if (method, path) in EXEMPT:
            continue
        assert declared is not None, f"{method} {path} declares no permission"
        values = [declared] if isinstance(declared, str) else declared
        assert values
        assert set(values) <= registered, (
            f"{method} {path} declares {declared}, which is not registered"
        )


def declared_permission_of(backend: Backend, target: Target) -> frozenset[Permission]:
    """The permissions the route advertises for this method and path."""
    declared = declarations(backend).get((target.method, f"{API_PREFIX}{target.template}"))
    assert declared is not None, f"no declaration for {target.method} {target.template}"
    values = [declared] if isinstance(declared, str) else declared
    return frozenset(Permission(value) for value in values)


@pytest.mark.parametrize("target", ROUTES, ids=lambda target: f"{target.method} {target.template}")
def test_the_use_case_refuses_a_caller_without_the_declared_permission(
    backend: Backend, target: Target
) -> None:
    declared = declared_permission_of(backend, target)
    # Everything *except* the declared one. A caller with no permissions at all would be refused by
    # a use case checking some other permission too, and the test would pass without proving that
    # the route's declaration is the one being enforced.
    others = frozenset(Permission) - declared

    response = backend.send(target, granted=others)

    assert response.status_code == 403, response.body
    assert response.error_code == "PERMISSION_DENIED"


@pytest.mark.parametrize("target", ROUTES, ids=lambda target: f"{target.method} {target.template}")
def test_the_declared_permission_is_sufficient(backend: Backend, target: Target) -> None:
    # The other half: holding exactly what the route declares gets past authorization. Without
    # this, a route that refused every caller for an unrelated reason would satisfy the test above.
    declared = declared_permission_of(backend, target)

    response = backend.send(target, granted=declared)

    assert response.status_code != 403, response.body
