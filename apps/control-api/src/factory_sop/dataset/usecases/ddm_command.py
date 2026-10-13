"""DDM 训练命令：把授权上下文、独立训练/验证冻结输入与消费参数持久绑定到应用命令。

S072 只受理命令并把命令与 outbox 事实写入同一请求事务，命令保持 ``pending``；不注册
可实际 start 的公开操作，也不伪称训练开始。真正的派发（S073）与结果登记（S074）就绪后
再释放并投递该命令。冻结负载由 dataset 拥有的窄编码器编码，`job` owner 不接触其中
的 JSON 细节。
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import Any
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.dataset.errors import DatasetRefusalCode, DatasetRefusedError
from factory_sop.dataset.model import (
    DdmTrainingCommand,
    DdmTrainingCommandStatus,
    DdmTrainingResultStatus,
)
from factory_sop.dataset.repository import DdmTrainingCommandRepository
from factory_sop.dataset.usage import canonical_json
from factory_sop.dataset.usecases.usage import (
    DdmInputSelection,
    FrozenDdmInput,
    FrozenDdmInputSet,
    FrozenDdmMemberInput,
    freeze_ddm_input,
)
from factory_sop.identifiers import new_id
from factory_sop.job.api import ApplicationJob, DdmTrainingJobQueue
from factory_sop.observability import get_logger

_logger = get_logger("dataset")
_MAX_IDEMPOTENCY_KEY_LENGTH = 255


@dataclass(frozen=True, slots=True)
class DdmTrainingCommandResult:
    """受理 DDM 训练命令返回的持久命令和待派发的异步任务。"""

    command: DdmTrainingCommand
    job: ApplicationJob


def request_ddm_training_command(
    *,
    training: DdmInputSelection,
    validation: DdmInputSelection,
    idempotency_key: str,
    caller: Caller,
    now: datetime,
    datasets: DdmTrainingCommandRepository,
    jobs: DdmTrainingJobQueue,
) -> DdmTrainingCommandResult:
    """受理一条 DDM 训练命令：冻结两套独立输入，并与 outbox 事实同事务持久化。

    授权先于任何读取与重放判断；只做短事务的业务读取与写入，耗时的材料化/HTTP 派发属于
    后续 S073 阶段，不占请求事务。返回 ``pending`` 命令表示已受理，绝不表示训练已开始。
    已受理幂等身份先于冻结重放首次命令，来源数据集当前修订变化不会改写已受理内容。
    """
    authorize(caller, Permission.DATASET_EDIT)
    if not idempotency_key.strip() or len(idempotency_key) > _MAX_IDEMPOTENCY_KEY_LENGTH:
        raise DatasetRefusedError(
            DatasetRefusalCode.IDEMPOTENCY_CONFLICT,
            detail="DDM 训练命令幂等键不能为空且不能超过 255 个字符",
        )
    dataset_id = training.dataset_id
    # 幂等身份在冻结之前裁决：已受理命令按首次冻结内容重放，不因来源数据集当前
    # 修订/标注/成员变化而重新冻结成“新内容”，也不对重放重复执行当前性校验。
    existing = datasets.ddm_training_command_by_idempotency(
        dataset_id=dataset_id,
        idempotency_key=idempotency_key,
    )
    if existing is not None:
        return _replay_command(
            existing=existing,
            training=training,
            validation=validation,
            dataset_id=dataset_id,
            now=now,
            jobs=jobs,
        )
    frozen = freeze_ddm_input(
        training=training,
        validation=validation,
        caller=caller,
        datasets=datasets,
    )
    payload = encode_frozen_ddm_input(frozen)
    digest = request_digest(payload)
    command = DdmTrainingCommand(
        id=new_id(),
        dataset_id=dataset_id,
        status=DdmTrainingCommandStatus.PENDING,
        idempotency_key=idempotency_key,
        request_digest=digest,
        payload=payload,
        job_id=None,
        created_by=caller.user.id,
        created_at=now,
        updated_at=now,
    )
    if not datasets.add_ddm_training_command(command):
        # 并发提交由唯一键裁决：读取已提交的获胜命令，只产生一个逻辑命令。
        existing = datasets.ddm_training_command_by_idempotency(
            dataset_id=dataset_id,
            idempotency_key=idempotency_key,
        )
        if existing is None:
            raise DatasetRefusedError(
                DatasetRefusalCode.USAGE_STATE_CONFLICT,
                detail="DDM 训练命令并发受理失败，请重试",
            )
        return _replay_command(
            existing=existing,
            training=training,
            validation=validation,
            dataset_id=dataset_id,
            now=now,
            jobs=jobs,
        )
    job = jobs.get_or_create_ddm_training(
        dataset_id=dataset_id,
        command_id=command.id,
        now=now,
    )
    command = replace(command, job_id=job.id)
    if not datasets.save_ddm_training_command(command, expected_updated_at=now):
        raise DatasetRefusedError(
            DatasetRefusalCode.USAGE_STATE_CONFLICT,
            detail="DDM 训练命令保存失败",
        )
    _logger.info(
        "dataset.ddm_training_command.accepted",
        dataset_id=str(dataset_id),
        command_id=str(command.id),
        request_digest=digest,
        actor_id=str(caller.user.id),
        result=command.status.value,
    )
    return DdmTrainingCommandResult(command=command, job=job)


def _replay_command(
    *,
    existing: DdmTrainingCommand,
    training: DdmInputSelection,
    validation: DdmInputSelection,
    dataset_id: UUID,
    now: datetime,
    jobs: DdmTrainingJobQueue,
) -> DdmTrainingCommandResult:
    """同一幂等身份且调用者选择的训练/验证来源不变时返回原命令；否则冲突。

    只比较调用者提供的选择三元组与首次受理时冻结的载荷，不重新冻结，也不复核来源数据集的
    当前修订/标注/成员；服务端固定的消费参数与派生身份保持首次受理事实。
    """
    if not _matches_recorded_selection(
        existing=existing,
        training=training,
        validation=validation,
    ):
        raise DatasetRefusedError(
            DatasetRefusalCode.IDEMPOTENCY_CONFLICT,
            detail="幂等键已对应另一份 DDM 训练命令内容",
        )
    job = jobs.get_or_create_ddm_training(
        dataset_id=dataset_id,
        command_id=existing.id,
        now=now,
    )
    return DdmTrainingCommandResult(command=existing, job=job)


def _matches_recorded_selection(
    *,
    existing: DdmTrainingCommand,
    training: DdmInputSelection,
    validation: DdmInputSelection,
) -> bool:
    """比较调用者选择三元组与已冻结负载，不读来源数据集的当前状态。"""
    payload = existing.payload
    training_set = payload.get("training")
    validation_set = payload.get("validation")
    if not isinstance(training_set, Mapping) or not isinstance(validation_set, Mapping):
        return False
    return _recorded_triple(training_set) == _selection_triple(training) and _recorded_triple(
        validation_set
    ) == _selection_triple(validation)


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


def request_digest(payload: Mapping[str, Any]) -> str:
    """对调用者内容取稳定摘要，作为幂等身份的内容事实。

    服务端在每次冻结时生成的 ``input_id`` 不属于调用者内容：重放同一幂等身份时必须得到同一
    摘要，否则同内容重放会被误判为内容变化。冻结输入本身仍原样保留首次受理时的 ``input_id``。
    """
    content = {key: value for key, value in payload.items() if key != "input_id"}
    return hashlib.sha256(canonical_json(content)).hexdigest()


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
        "consumer_parameters": dict(value.consumer_parameters),
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


def list_ddm_training_commands(
    *,
    dataset_id: UUID,
    caller: Caller,
    datasets: DdmTrainingCommandRepository,
) -> Sequence[DdmTrainingCommand]:
    """按数据集列出已受理的 DDM 训练命令，供重新打开页面后恢复原命令。

    授权在用例边界强制，路由上的声明只是元数据；命令负载、内部路径与媒体身份不进入
    任何公开返回，调用者只拿命令身份与观测/结果事实。
    """
    authorize(caller, Permission.DATASET_VIEW)
    if datasets.dataset_by_id(dataset_id) is None:
        raise DatasetRefusedError(DatasetRefusalCode.DATASET_NOT_FOUND)
    return datasets.list_ddm_training_commands(dataset_id)


def read_ddm_training_command(
    *,
    dataset_id: UUID,
    command_id: UUID,
    caller: Caller,
    datasets: DdmTrainingCommandRepository,
) -> DdmTrainingCommand:
    """读取一条属于该数据集的 DDM 训练命令；不匹配按不存在处理。"""
    authorize(caller, Permission.DATASET_VIEW)
    command = datasets.ddm_training_command_by_id(command_id)
    if command is None or command.dataset_id != dataset_id:
        raise DatasetRefusedError(DatasetRefusalCode.DDM_TRAINING_COMMAND_NOT_FOUND)
    return command


def authorize_ddm_result_access(
    *,
    command_id: UUID,
    caller: Caller,
    datasets: DdmTrainingCommandRepository,
) -> DdmTrainingCommand:
    """授权读取已发布 DDM 训练结果的最小用例。

    调用方是 Nginx auth_request 子请求；只有当前登录用户具备 DATASET_VIEW、命令存在且
    结果已真实登记时放行。内部对象键、源路径与原始负载不在此返回，数据由数据通道直接读取。
    """
    authorize(caller, Permission.DATASET_VIEW)
    command = datasets.ddm_training_command_by_id(command_id)
    if (
        command is None
        or command.status is not DdmTrainingCommandStatus.COMPLETED
        or command.result_status is not DdmTrainingResultStatus.REGISTERED
        or command.result_config_object_key is None
        or command.result_checkpoint_object_key is None
    ):
        raise DatasetRefusedError(DatasetRefusalCode.DDM_TRAINING_RESULT_NOT_FOUND)
    binding = re.fullmatch(
        rf"ddm-results/{command.id}/([0-9a-f]{{32}})/config\.yaml", command.result_config_object_key
    )
    if (
        binding is None
        or command.result_checkpoint_object_key
        != f"ddm-results/{command.id}/{binding.group(1)}/checkpoint.ckpt"
    ):
        raise DatasetRefusedError(DatasetRefusalCode.DDM_TRAINING_RESULT_NOT_FOUND)
    return command
