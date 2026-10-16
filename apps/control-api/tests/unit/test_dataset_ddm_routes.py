"""DDM 训练命令 HTTP seam：查询恢复、原状态/结果分离与授权结果访问地址。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from fastapi.testclient import TestClient
from pydantic import SecretStr

from factory_sop.app import API_PREFIX, create_app
from factory_sop.auth.adapters import dependencies as auth_dependencies
from factory_sop.auth.model import Session, User, UserStatus
from factory_sop.auth.permissions import Permission
from factory_sop.auth.usecases.sessions import RestoredSession
from factory_sop.dataset.adapters import dependencies as dataset_dependencies
from factory_sop.dataset.model import (
    DdmTrainingCommand,
    DdmTrainingCommandStatus,
    DdmTrainingResultStatus,
    TrainingDataset,
)
from factory_sop.dataset.repository import DdmTrainingCommandRepository
from factory_sop.persistence import request_session
from factory_sop.settings import Settings

NOW = datetime(2026, 10, 13, 1, 0, tzinfo=UTC)
ACTOR_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f301")
DATASET_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f302")
COMMAND_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f303")


def _settings() -> Settings:
    return Settings(
        log_level="warning",
        database_host="unused",
        database_port=5432,
        database_name="unused",
        database_user="unused",
        database_password=SecretStr("unused"),
        session_idle_timeout_minutes=720,
        session_absolute_lifetime_minutes=43200,
        session_cookie_transport="require_https",
        csrf_secret=SecretStr("csrf-secret"),
        redis_url=SecretStr("redis://127.0.0.1:1/0"),
        annotation_backend_url="https://annotation.example.test",
        annotation_media_origin="https://media.example.test",
        annotation_data_root="/var/lib/nvsop/annotation-data",
    )


class _StubSession:
    def rollback(self) -> None:
        return None


class _DdmDatasets:
    """只实现 DDM 命令查询所需的最小仓储；不接触真实数据库。"""

    def __init__(self, command: DdmTrainingCommand) -> None:
        self._command = command

    def dataset_by_id(self, dataset_id: UUID) -> TrainingDataset | None:
        if dataset_id != DATASET_ID:
            return None
        return TrainingDataset(
            id=DATASET_ID,
            name="ddm-route",
            created_by=ACTOR_ID,
            updated_by=ACTOR_ID,
            created_at=NOW,
            updated_at=NOW,
        )

    def list_ddm_training_commands(self, dataset_id: UUID) -> list[DdmTrainingCommand]:
        return [self._command] if dataset_id == DATASET_ID else []

    def ddm_training_command_by_id(self, command_id: UUID) -> DdmTrainingCommand | None:
        return self._command if command_id == self._command.id else None

    def cancel_pending_ddm_training_command(
        self, *, dataset_id: UUID, command_id: UUID, actor_id: UUID, now: datetime
    ) -> bool:
        if (
            self._command.dataset_id != dataset_id
            or self._command.id != command_id
            or self._command.status is not DdmTrainingCommandStatus.PENDING
        ):
            return False
        self._command = replace(
            self._command,
            status=DdmTrainingCommandStatus.CANCELLED,
            cancelled_by=actor_id,
            cancelled_at=now,
            updated_at=now,
        )
        return True

    def request_ddm_training_cancellation(
        self, *, command_id: UUID, actor_id: UUID, now: datetime
    ) -> bool:
        if (
            self._command.id != command_id
            or self._command.status
            not in {DdmTrainingCommandStatus.BOUND, DdmTrainingCommandStatus.RESULT_UNKNOWN}
            or self._command.cancel_requested_at is not None
        ):
            return False
        self._command = replace(
            self._command,
            cancel_requested_by=actor_id,
            cancel_requested_at=now,
            updated_at=now,
        )
        return True


def _caller(permissions: frozenset[Permission]) -> RestoredSession:
    return RestoredSession(
        session=Session(
            id=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f304"),
            user_id=ACTOR_ID,
            token_fingerprint="unused",
            created_at=NOW,
            last_used_at=NOW,
        ),
        user=User(
            id=ACTOR_ID,
            login_name="ddm-route",
            display_name="DDM 路由测试",
            password_hash="unused",  # pragma: allowlist secret
            status=UserStatus.ACTIVE,
        ),
    )


def _command(*, result_registered: bool) -> DdmTrainingCommand:
    return DdmTrainingCommand(
        id=COMMAND_ID,
        dataset_id=DATASET_ID,
        status=(
            DdmTrainingCommandStatus.COMPLETED
            if result_registered
            else DdmTrainingCommandStatus.BOUND
        ),
        idempotency_key="route-key",
        request_digest="a" * 64,
        payload={"input_id": "internal", "secret_path": "/raw/source/path"},
        job_id=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f305"),
        created_by=ACTOR_ID,
        created_at=NOW,
        updated_at=NOW,
        backend_target="http://ddm:8090",
        base_job_id="019937d8-0d10-7b31-8d2d-4e60c8f4f306",
        base_observation={"status": "running", "progress": 0.5},
        last_observed_at=NOW,
        result_status=(
            DdmTrainingResultStatus.REGISTERED
            if result_registered
            else DdmTrainingResultStatus.NOT_REGISTERED
        ),
        result_generation=1 if result_registered else 0,
        result_config_sha256="b" * 64 if result_registered else None,
        result_config_size=10 if result_registered else None,
        result_config_object_key=(
            f"ddm-results/{COMMAND_ID}/11111111111141118111111111111111/config.yaml"
            if result_registered
            else None
        ),
        result_checkpoint_sha256="c" * 64 if result_registered else None,
        result_checkpoint_size=20 if result_registered else None,
        result_checkpoint_object_key=(
            f"ddm-results/{COMMAND_ID}/11111111111141118111111111111111/checkpoint.ckpt"
            if result_registered
            else None
        ),
        result_registered_at=NOW if result_registered else None,
    )


def _client(datasets: _DdmDatasets, permissions: frozenset[Permission]) -> TestClient:
    app = create_app(_settings())
    app.dependency_overrides[auth_dependencies.authenticated_caller] = lambda: _caller(permissions)
    app.dependency_overrides[auth_dependencies.granted_permissions] = lambda: permissions
    app.dependency_overrides[dataset_dependencies.datasets] = lambda: cast(
        DdmTrainingCommandRepository, datasets
    )
    app.dependency_overrides[request_session] = _StubSession
    return TestClient(app)


def test_list_and_read_expose_observation_and_result_without_payload() -> None:
    datasets = _DdmDatasets(_command(result_registered=True))
    with _client(datasets, frozenset({Permission.DATASET_VIEW})) as client:
        listed = client.get(f"{API_PREFIX}/training-datasets/{DATASET_ID}/ddm-training-commands")
        assert listed.status_code == 200
        item = listed.json()["items"][0]
        assert item["id"] == str(COMMAND_ID)
        assert item["base_status"] == "running"
        assert item["base_progress"] == 0.5
        assert "backend_target" not in item
        assert item["last_observed_at"] is not None
        assert item["result_status"] == "registered"
        # 冻结负载与内部路径绝不进入公开响应。
        rendered = listed.text
        assert "input_id" not in rendered
        assert "/raw/source/path" not in rendered

        detail = client.get(
            f"{API_PREFIX}/training-datasets/{DATASET_ID}/ddm-training-commands/{COMMAND_ID}"
        )
        assert detail.status_code == 200
        assert detail.json()["base_job_id"] == "019937d8-0d10-7b31-8d2d-4e60c8f4f306"

        result = client.get(
            f"{API_PREFIX}/training-datasets/{DATASET_ID}/ddm-training-commands/{COMMAND_ID}/result"
        )
        assert result.status_code == 200
        body = result.json()
        assert body["status"] == "registered"
        assert body["access"]["checkpoint_url"].endswith(f"/ddm-results/{COMMAND_ID}/checkpoint")
        assert body["config"]["size"] == 10


def test_cancel_pending_replays_first_fact_and_exposes_it_on_read() -> None:
    pending = replace(
        _command(result_registered=False),
        status=DdmTrainingCommandStatus.PENDING,
        base_job_id=None,
        base_observation=None,
    )
    datasets = _DdmDatasets(pending)
    path = f"{API_PREFIX}/training-datasets/{DATASET_ID}/ddm-training-commands/{COMMAND_ID}"
    with _client(datasets, frozenset({Permission.DATASET_EDIT, Permission.DATASET_VIEW})) as client:
        first = client.post(f"{path}/cancel")
        assert first.status_code == 200, first.text
        fact = first.json()
        assert fact["status"] == "cancelled"
        assert fact["cancelled_by"] == str(ACTOR_ID)
        assert fact["cancelled_at"].endswith("Z")
        assert fact["base_job_id"] is None
        assert "payload" not in fact
        assert client.post(f"{path}/cancel").json() == fact
        assert client.get(path).json() == fact
        listed = client.get(f"{API_PREFIX}/training-datasets/{DATASET_ID}/ddm-training-commands")
        assert listed.json()["items"] == [fact]


def test_cancel_checks_permissions_ownership_and_dispatch_state() -> None:
    datasets = _DdmDatasets(_command(result_registered=False))
    path = f"{API_PREFIX}/training-datasets/{DATASET_ID}/ddm-training-commands/{COMMAND_ID}/cancel"
    for permissions in (
        frozenset(),
        frozenset({Permission.DATASET_VIEW}),
        frozenset({Permission.DATASET_IMPORT}),
        frozenset({Permission.DATASET_VIEW, Permission.DATASET_IMPORT}),
    ):
        with _client(datasets, permissions) as client:
            denied = client.post(path)
            assert denied.status_code == 403, denied.text
    with _client(datasets, frozenset({Permission.DATASET_EDIT})) as client:
        dispatched = client.post(path)
        assert dispatched.status_code == 200, dispatched.text
        requested = dispatched.json()
        assert requested["status"] == "bound"
        assert requested["cancel_requested_by"] == str(ACTOR_ID)
        assert requested["cancel_requested_at"].endswith("Z")
        assert requested["cancelled_by"] is None
        assert "payload" not in requested
        # 重复请求幂等：保留首次请求者与时刻。
        assert client.post(path).json() == requested
        missing = client.post(path.replace(str(COMMAND_ID), str(ACTOR_ID)))
        mismatch = client.post(path.replace(str(DATASET_ID), str(ACTOR_ID)))
        for response in (missing, mismatch):
            assert response.status_code == 404
            assert response.json()["error_code"] == "DDM_TRAINING_COMMAND_NOT_FOUND"


def test_unregistered_result_has_no_access_url() -> None:
    datasets = _DdmDatasets(_command(result_registered=False))
    with _client(datasets, frozenset({Permission.DATASET_VIEW})) as client:
        result = client.get(
            f"{API_PREFIX}/training-datasets/{DATASET_ID}/ddm-training-commands/{COMMAND_ID}/result"
        )
        assert result.status_code == 200
        body = result.json()
        assert body["status"] == "not_registered"
        assert body["access"] is None
        assert body["checkpoint"] is None


def test_query_requires_dataset_view_permission() -> None:
    datasets = _DdmDatasets(_command(result_registered=True))
    with _client(datasets, frozenset()) as client:
        denied = client.get(f"{API_PREFIX}/training-datasets/{DATASET_ID}/ddm-training-commands")
        assert denied.status_code == 403


def test_unknown_or_mismatched_command_is_a_404_problem_document() -> None:
    """未知或不匹配的命令按不存在返回 404；错误码必须在共享 wire 枚举中，不能升级为 500。"""
    datasets = _DdmDatasets(_command(result_registered=True))
    missing = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f3ff")
    other_dataset = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f3fe")
    with _client(datasets, frozenset({Permission.DATASET_VIEW})) as client:
        unknown = client.get(
            f"{API_PREFIX}/training-datasets/{DATASET_ID}/ddm-training-commands/{missing}"
        )
        assert unknown.status_code == 404
        assert unknown.json()["error_code"] == "DDM_TRAINING_COMMAND_NOT_FOUND"

        mismatch = client.get(
            f"{API_PREFIX}/training-datasets/{other_dataset}/ddm-training-commands/{COMMAND_ID}"
        )
        assert mismatch.status_code == 404
        assert mismatch.json()["error_code"] == "DDM_TRAINING_COMMAND_NOT_FOUND"

        unknown_result = client.get(
            f"{API_PREFIX}/training-datasets/{DATASET_ID}/ddm-training-commands/{missing}/result"
        )
        assert unknown_result.status_code == 404
        assert unknown_result.json()["error_code"] == "DDM_TRAINING_COMMAND_NOT_FOUND"


def test_result_authorization_hides_command_facts_from_unauthorized_callers() -> None:
    """缺 DATASET_VIEW 时数据读取授权返回 403，不因命令是否存在或结果是否登记而泄露事实。"""
    datasets = _DdmDatasets(_command(result_registered=True))
    with _client(datasets, frozenset()) as client:
        denied = client.get(
            f"{API_PREFIX}/ddm-results/authorize?command_id={COMMAND_ID}&kind=config"
        )
        assert denied.status_code == 403


def test_result_authorize_endpoint_accepts_view_and_rejects_unknown_kind() -> None:
    datasets = _DdmDatasets(_command(result_registered=True))
    with _client(datasets, frozenset({Permission.DATASET_VIEW})) as client:
        allowed = client.get(
            f"{API_PREFIX}/ddm-results/authorize?command_id={COMMAND_ID}&kind=checkpoint"
        )
        assert allowed.status_code == 204
        assert (
            allowed.headers["X-DDM-Result-Object"]
            == f"ddm-results/{COMMAND_ID}/11111111111141118111111111111111/checkpoint.ckpt"
        )
        rejected = client.get(
            f"{API_PREFIX}/ddm-results/authorize?command_id={COMMAND_ID}&kind=weights"
        )
        assert rejected.status_code == 403
