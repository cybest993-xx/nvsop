"""VLM 训练命令受理路径：冻结、幂等重放、内容冲突与持久负载的可还原性。

用途检查/候选仍走真实的 `dataset` 用例，只把仓储与任务队列替换为内存替身；真实
PostgreSQL 上的启动、查询与结果登记由 integration 套件覆盖。
"""

from __future__ import annotations

import json
from typing import cast
from uuid import uuid4

import pytest
from test_dataset_usage_usecases import (
    DATASET_ID,
    NOW,
    FakeUsageDatasets,
    FakeUsageJobs,
    _fresh_vlm_datasets,
    _passing_vlm_check,
    _whole_video_media,
    caller,
)

from factory_sop.auth.permissions import Permission
from factory_sop.dataset.errors import DatasetRefusalCode, DatasetRefusedError
from factory_sop.dataset.model import TrainingCommandStatus, TrainingService
from factory_sop.dataset.repository import (
    TrainingCommandRepository,
    UsageDatasetRepository,
)
from factory_sop.dataset.usecases.training_command import (
    TrainingCommandResult,
    request_digest,
)
from factory_sop.dataset.usecases.usage import VlmInputSelection, freeze_vlm_input
from factory_sop.dataset.usecases.vlm_command import (
    decode_frozen_vlm_input,
    encode_frozen_vlm_input,
    request_vlm_training_command,
)
from factory_sop.job.api import JobType


def _request(
    datasets: FakeUsageDatasets, *, usage_check_id: object, key: str = "vlm-key"
) -> TrainingCommandResult:
    return request_vlm_training_command(
        selection=VlmInputSelection(
            dataset_id=DATASET_ID,
            usage_check_id=usage_check_id,  # type: ignore[arg-type]
        ),
        idempotency_key=key,
        caller=caller(Permission.DATASET_EDIT),
        now=NOW,
        datasets=cast(TrainingCommandRepository, datasets),
        jobs=FakeUsageJobs(),
    )


def test_frozen_vlm_payload_is_json_safe_and_round_trips() -> None:
    """落库负载必须是纯 JSON，且能还原成同一份冻结事实。"""
    datasets = _fresh_vlm_datasets()
    check = _passing_vlm_check(datasets, media=_whole_video_media())
    frozen = freeze_vlm_input(
        selection=VlmInputSelection(dataset_id=DATASET_ID, usage_check_id=check.id),
        caller=caller(Permission.DATASET_EDIT),
        datasets=cast(UsageDatasetRepository, datasets),
    )

    payload = encode_frozen_vlm_input(frozen)

    # JSONB 持久化前必须可序列化：冻结值里的只读映射不能直接交给 JSON 编码器。
    serialized = json.loads(json.dumps(payload))
    assert decode_frozen_vlm_input(serialized) == frozen


def test_vlm_command_accepts_pending_command_with_vlm_job_type() -> None:
    """受理只写入 pending 命令与 VLM 待派发任务，不表示训练已开始。"""
    datasets = _fresh_vlm_datasets()
    check = _passing_vlm_check(datasets, media=_whole_video_media())

    result = _request(datasets, usage_check_id=check.id)

    assert result.command.status is TrainingCommandStatus.PENDING
    assert result.command.service is TrainingService.VLM
    assert result.command.request_digest == request_digest(dict(result.command.payload))
    assert result.job.job_type is JobType.DATASET_VLM_TRAINING
    assert result.command.job_id == result.job.id
    assert result.command.payload["usage_check_id"] == str(check.id)
    assert result.command.payload["candidate_id"] == str(check.candidate_id)


def test_vlm_command_replay_returns_original_and_changed_check_conflicts() -> None:
    """同一幂等键与同一选择重放原命令；换成另一份检查则是内容冲突。"""
    datasets = _fresh_vlm_datasets()
    first = _passing_vlm_check(datasets, media=_whole_video_media())

    accepted = _request(datasets, usage_check_id=first.id, key="replay")
    replayed = _request(datasets, usage_check_id=first.id, key="replay")

    assert replayed.command.id == accepted.command.id
    assert replayed.job.id == accepted.job.id
    assert replayed.command.payload == accepted.command.payload

    # 新的候选修订会让上一份检查不再当前，因此冲突只能在它之后、用新检查触发。
    second = _passing_vlm_check(
        datasets,
        media=_whole_video_media(key="vlm-other.mp4"),
        expected_revision=1,
    )
    with pytest.raises(DatasetRefusedError) as conflict:
        _request(datasets, usage_check_id=second.id, key="replay")
    assert conflict.value.code is DatasetRefusalCode.IDEMPOTENCY_CONFLICT


def test_vlm_command_refuses_an_unknown_check_without_persisting_anything() -> None:
    """不存在的用途检查不能受理；失败不留下命令事实，好让同一幂等键重试。"""
    datasets = _fresh_vlm_datasets()
    check = _passing_vlm_check(datasets, media=_whole_video_media())

    with pytest.raises(DatasetRefusedError) as refused:
        _request(datasets, usage_check_id=uuid4(), key="unavailable")
    assert refused.value.code is DatasetRefusalCode.USAGE_CHECK_NOT_FOUND
    assert datasets.ddm_commands == {}

    accepted = _request(datasets, usage_check_id=check.id, key="unavailable")
    assert accepted.command.status is TrainingCommandStatus.PENDING
    assert len(datasets.ddm_commands) == 1
