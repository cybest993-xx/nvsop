"""VLM 训练命令的冻结负载编解码与受理入口。

编排（幂等裁决、outbox 事实、取消竞争、授权）由 `usecases/training_command.py` 共享；
本模块只承担 VLM 自己的差异：受检候选的冻结与重放比较，以及冻结负载的可持久化编码。
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any
from uuid import UUID

from factory_sop.auth.api import Caller
from factory_sop.dataset.model import TrainingService
from factory_sop.dataset.repository import TrainingCommandRepository
from factory_sop.dataset.usage import json_safe_value
from factory_sop.dataset.usecases.training_command import (
    TrainingCommandResult,
    request_digest,
    request_training_command,
)
from factory_sop.dataset.usecases.usage import (
    FrozenVlmInput,
    FrozenVlmMediaInput,
    VlmInputSelection,
    freeze_vlm_input,
)
from factory_sop.job.api import TrainingJobQueue


def request_vlm_training_command(
    *,
    selection: VlmInputSelection,
    idempotency_key: str,
    caller: Caller,
    now: datetime,
    datasets: TrainingCommandRepository,
    jobs: TrainingJobQueue,
) -> TrainingCommandResult:
    """受理一条 VLM 训练命令：冻结已通过的受检候选，并与 outbox 事实同事务持久化。"""
    return request_training_command(
        request=_VlmCommandInput(selection=selection, caller=caller, datasets=datasets),
        idempotency_key=idempotency_key,
        caller=caller,
        now=now,
        datasets=datasets,
        jobs=jobs,
    )


@dataclass(frozen=True, slots=True)
class _VlmCommandInput:
    """VLM 的受理输入：冻结已通过的候选，并只比较调用者选择的检查身份。"""

    selection: VlmInputSelection
    caller: Caller
    datasets: TrainingCommandRepository
    service: TrainingService = TrainingService.VLM

    @property
    def dataset_id(self) -> UUID:
        return self.selection.dataset_id

    def freeze(self) -> tuple[Mapping[str, Any], str]:
        frozen = freeze_vlm_input(
            selection=self.selection,
            caller=self.caller,
            datasets=self.datasets,
        )
        payload = encode_frozen_vlm_input(frozen)
        return payload, request_digest(payload)

    def matches(self, payload: Mapping[str, Any]) -> bool:
        """比较调用者选择的用途检查与已冻结负载，不读来源数据集的当前状态。"""
        return str(payload.get("dataset_id")) == str(self.selection.dataset_id) and str(
            payload.get("usage_check_id")
        ) == str(self.selection.usage_check_id)


def encode_frozen_vlm_input(frozen: FrozenVlmInput) -> dict[str, Any]:
    """把冻结输入编码为可持久化、可复验的 JSON 值。"""
    return {
        "input_id": str(frozen.input_id),
        "dataset_id": str(frozen.dataset_id),
        "usage_check_id": str(frozen.usage_check_id),
        "candidate_id": str(frozen.candidate_id),
        "candidate_kind": frozen.candidate_kind,
        "input_digest": frozen.input_digest,
        "base_commit": frozen.base_commit,
        "contract_version": frozen.contract_version,
        # 冻结消费参数可能是嵌套的只读映射，落库前规范化为纯 JSON 数据。
        "consumer_parameters": json_safe_value(frozen.consumer_parameters),
        "records_bytes_base64": base64.b64encode(frozen.records_bytes).decode("ascii"),
        "records_sha256": frozen.records_sha256,
        "media": [_encode_media(item) for item in frozen.media],
    }


def decode_frozen_vlm_input(payload: Mapping[str, Any]) -> FrozenVlmInput:
    """把已受理命令的持久负载还原为派发时可独立材料化的冻结输入。

    只读取持久事实，不重新读取候选或用途检查的当前修订；派发与恢复必须基于首次受理的
    同一份内容。
    """
    parameters = payload.get("consumer_parameters")
    raw_media = payload.get("media")
    if not isinstance(parameters, Mapping) or not isinstance(raw_media, list):
        raise ValueError("VLM 训练命令冻结负载不完整")
    media = tuple(_decode_media(item) for item in raw_media if isinstance(item, Mapping))
    if len(media) != len(raw_media):
        raise ValueError("VLM 训练命令冻结媒体不完整")
    return FrozenVlmInput(
        input_id=UUID(str(payload["input_id"])),
        dataset_id=UUID(str(payload["dataset_id"])),
        usage_check_id=UUID(str(payload["usage_check_id"])),
        candidate_id=UUID(str(payload["candidate_id"])),
        candidate_kind=str(payload["candidate_kind"]),
        input_digest=str(payload["input_digest"]),
        base_commit=str(payload["base_commit"]),
        contract_version=str(payload["contract_version"]),
        consumer_parameters=_decode_consumer_parameters(parameters),
        records_bytes=base64.b64decode(str(payload["records_bytes_base64"]), validate=True),
        records_sha256=str(payload["records_sha256"]),
        media=media,
    )


def _encode_media(value: FrozenVlmMediaInput) -> dict[str, Any]:
    return {
        "key": value.key,
        "member_id": str(value.member_id),
        "is_annotation_clip": value.is_annotation_clip,
        "object_key": value.object_key,
        "source_sha256": value.source_sha256,
        "actual_size": value.actual_size,
        "upstream_data_id": value.upstream_data_id,
        "upstream_video_id": value.upstream_video_id,
        "clip_filename": value.clip_filename,
        "derived_video_sha256": value.derived_video_sha256,
        "derived_video_size": value.derived_video_size,
    }


def _decode_media(value: Mapping[str, Any]) -> FrozenVlmMediaInput:
    return FrozenVlmMediaInput(
        key=str(value["key"]),
        member_id=UUID(str(value["member_id"])),
        is_annotation_clip=bool(value["is_annotation_clip"]),
        object_key=str(value["object_key"]),
        source_sha256=str(value["source_sha256"]),
        actual_size=int(value["actual_size"]),
        upstream_data_id=_optional_str(value.get("upstream_data_id")),
        upstream_video_id=_optional_str(value.get("upstream_video_id")),
        clip_filename=_optional_str(value.get("clip_filename")),
        derived_video_sha256=_optional_str(value.get("derived_video_sha256")),
        derived_video_size=_optional_int(value.get("derived_video_size")),
    )


def _decode_consumer_parameters(value: Mapping[str, Any]) -> Mapping[str, Any]:
    parameters: dict[str, Any] = {}
    for key, item in value.items():
        parameters[str(key)] = MappingProxyType(dict(item)) if isinstance(item, Mapping) else item
    return MappingProxyType(parameters)


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: object) -> int | None:
    return None if value is None else int(str(value))
