"""DDM 训练命令的冻结负载编解码与受理入口。

编排（幂等裁决、outbox 事实、取消竞争、授权）由 `usecases/training_command.py` 共享；
本模块只承担 DDM 自己的差异：两套独立训练/验证输入的冻结与重放比较，以及冻结负载的
可持久化编码。`job` owner 不接触其中的 JSON 细节。
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
    DdmInputSelection,
    FrozenDdmInput,
    FrozenDdmInputSet,
    FrozenDdmMemberInput,
    freeze_ddm_input,
)
from factory_sop.job.api import TrainingJobQueue


def request_ddm_training_command(
    *,
    training: DdmInputSelection,
    validation: DdmInputSelection,
    idempotency_key: str,
    caller: Caller,
    now: datetime,
    datasets: TrainingCommandRepository,
    jobs: TrainingJobQueue,
) -> TrainingCommandResult:
    """受理一条 DDM 训练命令：冻结两套独立输入，并与 outbox 事实同事务持久化。"""
    return request_training_command(
        request=_DdmCommandInput(
            training=training,
            validation=validation,
            caller=caller,
            datasets=datasets,
        ),
        idempotency_key=idempotency_key,
        caller=caller,
        now=now,
        datasets=datasets,
        jobs=jobs,
    )


@dataclass(frozen=True, slots=True)
class _DdmCommandInput:
    """DDM 的受理输入：冻结训练/验证两套选择，并只比较调用者选择三元组。"""

    training: DdmInputSelection
    validation: DdmInputSelection
    caller: Caller
    datasets: TrainingCommandRepository
    service: TrainingService = TrainingService.DDM

    @property
    def dataset_id(self) -> UUID:
        return self.training.dataset_id

    def freeze(self) -> tuple[Mapping[str, Any], str]:
        frozen = freeze_ddm_input(
            training=self.training,
            validation=self.validation,
            caller=self.caller,
            datasets=self.datasets,
        )
        payload = encode_frozen_ddm_input(frozen)
        return payload, request_digest(payload)

    def matches(self, payload: Mapping[str, Any]) -> bool:
        """比较调用者选择三元组与已冻结负载，不读来源数据集的当前状态。"""
        training_set = payload.get("training")
        validation_set = payload.get("validation")
        if not isinstance(training_set, Mapping) or not isinstance(validation_set, Mapping):
            return False
        return _recorded_triple(training_set) == _selection_triple(
            self.training
        ) and _recorded_triple(validation_set) == _selection_triple(self.validation)


def _recorded_triple(value: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(value.get("dataset_id")),
        str(value.get("usage_check_id")),
        str(value.get("artifact_id")),
    )


def _selection_triple(value: DdmInputSelection) -> tuple[str, str, str]:
    return (str(value.dataset_id), str(value.usage_check_id), str(value.artifact_id))


def encode_frozen_ddm_input(frozen: FrozenDdmInput) -> dict[str, Any]:
    """把冻结输入编码为可持久化、可复验的 JSON 值。"""
    return {
        "input_id": str(frozen.input_id),
        "training": _encode_set(frozen.training),
        "validation": _encode_set(frozen.validation),
        "validation_consumer_parameters": dict(frozen.validation_consumer_parameters),
    }


def decode_frozen_ddm_input(payload: Mapping[str, Any]) -> FrozenDdmInput:
    """把已受理命令的持久负载还原为派发时可独立材料化的冻结输入。

    只读取持久事实，不重新读取数据库当前修订；派发与恢复必须基于首次受理的同一份内容。
    """
    training = payload.get("training")
    validation = payload.get("validation")
    parameters = payload.get("validation_consumer_parameters")
    if (
        not isinstance(training, Mapping)
        or not isinstance(validation, Mapping)
        or not isinstance(parameters, Mapping)
    ):
        raise ValueError("DDM 训练命令冻结负载不完整")
    return FrozenDdmInput(
        input_id=UUID(str(payload["input_id"])),
        training=_decode_set(training),
        validation=_decode_set(validation),
        validation_consumer_parameters=MappingProxyType(dict(parameters)),
    )


def _decode_set(value: Mapping[str, Any]) -> FrozenDdmInputSet:
    raw_members = value.get("members")
    if not isinstance(raw_members, list):
        raise ValueError("DDM 训练命令冻结成员不完整")
    members = tuple(_decode_member(member) for member in raw_members if isinstance(member, Mapping))
    if len(members) != len(raw_members):
        raise ValueError("DDM 训练命令冻结成员不完整")
    return FrozenDdmInputSet(
        dataset_id=UUID(str(value["dataset_id"])),
        usage_check_id=UUID(str(value["usage_check_id"])),
        artifact_id=UUID(str(value["artifact_id"])),
        input_digest=str(value["input_digest"]),
        base_commit=str(value["base_commit"]),
        contract_version=str(value["contract_version"]),
        consumer_parameters=MappingProxyType(dict(value["consumer_parameters"])),
        artifact_object_key=str(value["artifact_object_key"]),
        artifact_sha256=str(value["artifact_sha256"]),
        artifact_size=int(value["artifact_size"]),
        members=members,
    )


def _decode_member(value: Mapping[str, Any]) -> FrozenDdmMemberInput:
    return FrozenDdmMemberInput(
        member_id=UUID(str(value["member_id"])),
        object_key=str(value["object_key"]),
        source_sha256=str(value["source_sha256"]),
        actual_size=int(value["actual_size"]),
        action_list_revision=int(value["action_list_revision"]),
        annotation_revision=int(value["annotation_revision"]),
        annotation_bytes=base64.b64decode(str(value["annotation_bytes_base64"]), validate=True),
        annotation_sha256=str(value["annotation_sha256"]),
    )


def _encode_set(value: FrozenDdmInputSet) -> dict[str, Any]:
    return {
        "dataset_id": str(value.dataset_id),
        "usage_check_id": str(value.usage_check_id),
        "artifact_id": str(value.artifact_id),
        "input_digest": value.input_digest,
        "base_commit": value.base_commit,
        "contract_version": value.contract_version,
        # 冻结消费参数可能是嵌套的只读映射，落库前规范化为纯 JSON 数据。
        "consumer_parameters": json_safe_value(value.consumer_parameters),
        "artifact_object_key": value.artifact_object_key,
        "artifact_sha256": value.artifact_sha256,
        "artifact_size": value.artifact_size,
        "members": [
            {
                "member_id": str(member.member_id),
                "object_key": member.object_key,
                "source_sha256": member.source_sha256,
                "actual_size": member.actual_size,
                "action_list_revision": member.action_list_revision,
                "annotation_revision": member.annotation_revision,
                "annotation_bytes_base64": base64.b64encode(member.annotation_bytes).decode(
                    "ascii"
                ),
                "annotation_sha256": member.annotation_sha256,
            }
            for member in value.members
        ],
    }
