"""VLM 训练命令 HTTP seam：按服务隔离的查询/取消与已授权结果访问。"""

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
    TrainingCommand,
    TrainingCommandStatus,
    TrainingDataset,
    TrainingResultStatus,
    TrainingService,
)
from factory_sop.dataset.repository import TrainingCommandRepository
from factory_sop.persistence import request_session
from factory_sop.settings import Settings

NOW = datetime(2026, 10, 13, 1, 0, tzinfo=UTC)
ACTOR_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f301")
DATASET_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f302")
COMMAND_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f303")
_OBJECT_TOKEN = "11111111111141118111111111111111"


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


class _VlmDatasets:
    """只实现命令查询/取消所需的最小仓储；按服务拒绝不匹配的读取。"""

    def __init__(self, command: TrainingCommand) -> None:
        self._command = command

    def dataset_by_id(self, dataset_id: UUID) -> TrainingDataset | None:
        if dataset_id != DATASET_ID:
            return None
        return TrainingDataset(
            id=DATASET_ID,
            name="vlm-route",
            created_by=ACTOR_ID,
            updated_by=ACTOR_ID,
            created_at=NOW,
            updated_at=NOW,
        )

    def list_training_commands(
        self, *, service: TrainingService, dataset_id: UUID
    ) -> list[TrainingCommand]:
        if dataset_id != DATASET_ID or service is not self._command.service:
            return []
        return [self._command]

    def training_command_by_id(self, command_id: UUID) -> TrainingCommand | None:
        return self._command if command_id == self._command.id else None

    def cancel_pending_training_command(
        self, *, dataset_id: UUID, command_id: UUID, actor_id: UUID, now: datetime
    ) -> bool:
        if (
            self._command.dataset_id != dataset_id
            or self._command.id != command_id
            or self._command.status is not TrainingCommandStatus.PENDING
        ):
            return False
        self._command = replace(
            self._command,
            status=TrainingCommandStatus.CANCELLED,
            cancelled_by=actor_id,
            cancelled_at=now,
            updated_at=now,
        )
        return True

    def request_training_cancellation(
        self, *, command_id: UUID, actor_id: UUID, now: datetime
    ) -> bool:
        if self._command.id != command_id or self._command.cancel_requested_at is not None:
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
            login_name="vlm-route",
            display_name="VLM 路由测试",
            password_hash="unused",  # pragma: allowlist secret
            status=UserStatus.ACTIVE,
        ),
    )


def _vlm_command(*, result_registered: bool) -> TrainingCommand:
    return TrainingCommand(
        id=COMMAND_ID,
        dataset_id=DATASET_ID,
        service=TrainingService.VLM,
        status=(
            TrainingCommandStatus.COMPLETED if result_registered else TrainingCommandStatus.PENDING
        ),
        idempotency_key="vlm-route-key",
        request_digest="a" * 64,
        payload={"input_id": "internal", "secret_path": "/raw/source/path"},
        job_id=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f305"),
        created_by=ACTOR_ID,
        created_at=NOW,
        updated_at=NOW,
        base_observation={"status": "completed", "progress": 1.0},
        last_observed_at=NOW,
        result_status=(
            TrainingResultStatus.REGISTERED
            if result_registered
            else TrainingResultStatus.NOT_REGISTERED
        ),
        result_generation=1 if result_registered else 0,
        result_config_sha256="b" * 64 if result_registered else None,
        result_config_size=10 if result_registered else None,
        result_config_object_key=(
            f"vlm-results/{COMMAND_ID}/{_OBJECT_TOKEN}/config.toml" if result_registered else None
        ),
        result_checkpoint_sha256="c" * 64 if result_registered else None,
        result_checkpoint_size=20 if result_registered else None,
        result_checkpoint_object_key=(
            f"vlm-results/{COMMAND_ID}/{_OBJECT_TOKEN}/checkpoint.manifest.json"
            if result_registered
            else None
        ),
        result_registered_at=NOW if result_registered else None,
    )


def _client(datasets: _VlmDatasets, permissions: frozenset[Permission]) -> TestClient:
    app = create_app(_settings())
    app.dependency_overrides[auth_dependencies.authenticated_caller] = lambda: _caller(permissions)
    app.dependency_overrides[auth_dependencies.granted_permissions] = lambda: permissions
    app.dependency_overrides[dataset_dependencies.datasets] = lambda: cast(
        TrainingCommandRepository, datasets
    )
    app.dependency_overrides[request_session] = _StubSession
    return TestClient(app)


def test_vlm_commands_are_listed_read_and_cancelled_through_vlm_routes() -> None:
    """重新打开页面能恢复原命令；未派发取消写入取消事实并公开身份。"""
    datasets = _VlmDatasets(_vlm_command(result_registered=False))
    client = _client(datasets, frozenset({Permission.DATASET_VIEW, Permission.DATASET_EDIT}))

    listed = client.get(f"{API_PREFIX}/training-datasets/{DATASET_ID}/vlm-training-commands")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [str(COMMAND_ID)]
    read = client.get(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/vlm-training-commands/{COMMAND_ID}"
    )
    assert read.status_code == 200
    body = read.json()
    assert body["status"] == "pending"
    # 内部路径与冻结负载不进入公开响应。
    assert "secret_path" not in read.text
    assert "payload" not in body

    cancelled = client.post(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/vlm-training-commands/{COMMAND_ID}/cancel"
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["cancelled_by"] == str(ACTOR_ID)


def test_vlm_command_is_invisible_through_ddm_routes() -> None:
    """命令按服务隔离：VLM 命令在 DDM 路由上按不存在处理，反之亦然。"""
    datasets = _VlmDatasets(_vlm_command(result_registered=True))
    client = _client(datasets, frozenset({Permission.DATASET_VIEW}))

    assert (
        client.get(
            f"{API_PREFIX}/training-datasets/{DATASET_ID}/ddm-training-commands/{COMMAND_ID}"
        ).status_code
        == 404
    )
    assert (
        client.get(f"{API_PREFIX}/training-datasets/{DATASET_ID}/ddm-training-commands").json()[
            "items"
        ]
        == []
    )
    assert (
        client.get(
            f"{API_PREFIX}/training-datasets/{DATASET_ID}/ddm-training-commands/{COMMAND_ID}/result"
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"{API_PREFIX}/training-datasets/{DATASET_ID}/vlm-training-commands/{COMMAND_ID}"
        ).status_code
        == 200
    )


def test_vlm_result_read_and_authorization_expose_only_vlm_object_keys() -> None:
    """结果读回给出数据通道地址；授权端点只放行已登记结果并回传自己的对象键头。"""
    datasets = _VlmDatasets(_vlm_command(result_registered=True))
    client = _client(datasets, frozenset({Permission.DATASET_VIEW}))

    result = client.get(
        f"{API_PREFIX}/training-datasets/{DATASET_ID}/vlm-training-commands/{COMMAND_ID}/result"
    )
    assert result.status_code == 200
    body = result.json()
    assert body["config"]["sha256"] == "b" * 64
    assert body["checkpoint"]["sha256"] == "c" * 64
    assert body["access"]["config_url"].endswith(f"/vlm-results/{COMMAND_ID}/config")
    assert body["access"]["checkpoint_url"].endswith(f"/vlm-results/{COMMAND_ID}/checkpoint")

    granted = client.get(
        f"{API_PREFIX}/vlm-results/authorize",
        params={"command_id": str(COMMAND_ID), "kind": "checkpoint"},
    )
    assert granted.status_code == 204
    assert granted.headers["X-VLM-Result-Object"] == (
        f"vlm-results/{COMMAND_ID}/{_OBJECT_TOKEN}/checkpoint.manifest.json"
    )
    denied = client.get(
        f"{API_PREFIX}/vlm-results/authorize",
        params={"command_id": str(COMMAND_ID), "kind": "archive"},
    )
    assert denied.status_code == 403
    # 结果只属于 VLM：DDM 授权端点不认领它。
    assert (
        client.get(
            f"{API_PREFIX}/ddm-results/authorize",
            params={"command_id": str(COMMAND_ID), "kind": "config"},
        ).status_code
        == 403
    )
