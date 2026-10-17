"""训练命令受理、查询、取消与结果授权：各训练服务共享的同一套编排。

幂等裁决、outbox 事实、取消与派发意图的竞争、以及授权边界在这里只有一份实现；服务差异
（冻结负载、重放比较、公开错误码）由各服务自己的 ``TrainingCommandInput`` 提供，派发与
观测差异由各服务的执行 adapter 提供。用例只做短事务的业务读写，不承担材料化或 HTTP。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.dataset.errors import DatasetRefusalCode, DatasetRefusedError
from factory_sop.dataset.model import (
    TRAINING_RESULT_NAMING,
    TRAINING_SERVICE_JOB_TYPES,
    TrainingCommand,
    TrainingCommandStatus,
    TrainingResultStatus,
    TrainingService,
)
from factory_sop.dataset.repository import TrainingCommandRepository
from factory_sop.dataset.usage import canonical_json
from factory_sop.identifiers import new_id
from factory_sop.job.api import ApplicationJob, TrainingJobQueue
from factory_sop.observability import get_logger

_logger = get_logger("dataset")
MAX_IDEMPOTENCY_KEY_LENGTH = 255

# 冻结负载中由服务端生成、不属于调用者内容的事实：重放同一幂等身份时必须得到同一摘要。
_GENERATED_PAYLOAD_KEYS = frozenset({"input_id"})

# 已派发命令的取消只调用该服务自己的真实取消入口，且不把响应文案当成进程停止证明。
# DDM 按 S082 核实真实进程停止后才释放在途约束；VLM 暂无可信停止证明。
DISPATCHED_CANCELLATION_SERVICES = frozenset({TrainingService.DDM, TrainingService.VLM})

_COMMAND_NOT_FOUND = {
    TrainingService.DDM: DatasetRefusalCode.DDM_TRAINING_COMMAND_NOT_FOUND,
    TrainingService.VLM: DatasetRefusalCode.VLM_TRAINING_COMMAND_NOT_FOUND,
}
_RESULT_NOT_FOUND = {
    TrainingService.DDM: DatasetRefusalCode.DDM_TRAINING_RESULT_NOT_FOUND,
    TrainingService.VLM: DatasetRefusalCode.VLM_TRAINING_RESULT_NOT_FOUND,
}


class TrainingCommandInput(Protocol):
    """一种训练服务的命令受理输入：冻结本次授权选择并判定重放内容是否一致。"""

    @property
    def service(self) -> TrainingService:
        """本输入归属的训练服务。"""
        ...

    @property
    def dataset_id(self) -> UUID:
        """本次受理归属的数据集。"""
        ...

    def freeze(self) -> tuple[Mapping[str, Any], str]:
        """按调用者本次选择冻结负载，返回可持久化负载与内容摘要。"""
        ...

    def matches(self, payload: Mapping[str, Any]) -> bool:
        """判断本次选择是否与已持久化负载一致；不重新读取来源的当前修订。"""
        ...


@dataclass(frozen=True, slots=True)
class TrainingCommandResult:
    """受理训练命令返回的持久命令和待派发的异步任务。"""

    command: TrainingCommand
    job: ApplicationJob


def request_digest(payload: Mapping[str, Any]) -> str:
    """对调用者内容取稳定摘要，作为幂等身份的内容事实。

    服务端在每次冻结时生成的 ``input_id`` 不属于调用者内容：重放同一幂等身份时必须得到同一
    摘要，否则同内容重放会被误判为内容变化。冻结输入本身仍原样保留首次受理时的 ``input_id``。
    """
    content = {key: value for key, value in payload.items() if key not in _GENERATED_PAYLOAD_KEYS}
    return hashlib.sha256(canonical_json(content)).hexdigest()


def request_training_command(
    *,
    request: TrainingCommandInput,
    idempotency_key: str,
    caller: Caller,
    now: datetime,
    datasets: TrainingCommandRepository,
    jobs: TrainingJobQueue,
) -> TrainingCommandResult:
    """受理一条训练命令：冻结本次选择，并与 outbox 事实同事务持久化。

    授权先于任何读取与重放判断；只做短事务的业务读取与写入，耗时的材料化/HTTP 派发属于
    worker 阶段。返回 ``pending`` 命令表示已受理，绝不表示训练已开始。已受理幂等身份先于
    冻结重放首次命令，来源数据集当前修订变化不会改写已受理内容。
    """
    authorize(caller, Permission.DATASET_EDIT)
    if not idempotency_key.strip() or len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise DatasetRefusedError(
            DatasetRefusalCode.IDEMPOTENCY_CONFLICT,
            detail="训练命令幂等键不能为空且不能超过 255 个字符",
        )
    dataset_id = request.dataset_id
    existing = datasets.training_command_by_idempotency(
        service=request.service,
        dataset_id=dataset_id,
        idempotency_key=idempotency_key,
    )
    if existing is not None:
        return _replay_command(
            existing=existing,
            request=request,
            idempotency_key=idempotency_key,
            now=now,
            jobs=jobs,
        )
    payload, digest = request.freeze()
    command = TrainingCommand(
        id=new_id(),
        dataset_id=dataset_id,
        service=request.service,
        status=TrainingCommandStatus.PENDING,
        idempotency_key=idempotency_key,
        request_digest=digest,
        payload=payload,
        job_id=None,
        created_by=caller.user.id,
        created_at=now,
        updated_at=now,
    )
    if not datasets.add_training_command(command):
        # 并发提交由唯一键裁决：读取已提交的获胜命令，只产生一个逻辑命令。
        existing = datasets.training_command_by_idempotency(
            service=request.service,
            dataset_id=dataset_id,
            idempotency_key=idempotency_key,
        )
        if existing is None:
            raise DatasetRefusedError(
                DatasetRefusalCode.USAGE_STATE_CONFLICT,
                detail="训练命令并发受理失败，请重试",
            )
        return _replay_command(
            existing=existing,
            request=request,
            idempotency_key=idempotency_key,
            now=now,
            jobs=jobs,
        )
    job = jobs.get_or_create_training(
        job_type=TRAINING_SERVICE_JOB_TYPES[request.service],
        dataset_id=dataset_id,
        command_id=command.id,
        now=now,
    )
    command = replace(command, job_id=job.id)
    if not datasets.save_training_command(command, expected_updated_at=now):
        raise DatasetRefusedError(
            DatasetRefusalCode.USAGE_STATE_CONFLICT,
            detail="训练命令保存失败",
        )
    _logger.info(
        "dataset.training_command.accepted",
        dataset_id=str(dataset_id),
        command_id=str(command.id),
        service=request.service.value,
        request_digest=digest,
        actor_id=str(caller.user.id),
        result=command.status.value,
    )
    return TrainingCommandResult(command=command, job=job)


def cancel_training_command(
    *,
    service: TrainingService,
    dataset_id: UUID,
    command_id: UUID,
    caller: Caller,
    now: datetime,
    datasets: TrainingCommandRepository,
) -> TrainingCommand:
    """取消训练命令：未派发直接取消，支持的服务只记录已派发取消请求。

    未派发取消与 start 意图以同一数据库状态竞争。已进入派发意图的命令只持久化首次取消
    请求并保持原状态与在途保护；真实停止核实属于执行 adapter 与后续观测，用例不伪称已停。
    已结束命令拒绝；只写取消事实，保留冻结输入、历史结果与 outbox。
    """
    authorize(caller, Permission.DATASET_EDIT)
    changed = datasets.cancel_pending_training_command(
        dataset_id=dataset_id, command_id=command_id, actor_id=caller.user.id, now=now
    )
    command = datasets.training_command_by_id(command_id)
    if command is None or command.dataset_id != dataset_id or command.service is not service:
        raise DatasetRefusedError(_COMMAND_NOT_FOUND[service])
    if changed:
        _logger.info(
            "dataset.training_command.cancelled",
            dataset_id=str(dataset_id),
            command_id=str(command_id),
            service=service.value,
            actor_id=str(caller.user.id),
            result=command.status.value,
        )
        return command
    if command.status is TrainingCommandStatus.CANCELLED:
        # 重复取消返回首次取消事实，不改写请求者与时刻。
        return command
    if (
        command.status in {TrainingCommandStatus.RESULT_UNKNOWN, TrainingCommandStatus.BOUND}
        and service in DISPATCHED_CANCELLATION_SERVICES
    ):
        # 意图已落库：只记录取消请求，由执行 adapter 调用原服务真实取消入口，不伪称已停止。
        requested = datasets.request_training_cancellation(
            command_id=command_id, actor_id=caller.user.id, now=now
        )
        command = datasets.training_command_by_id(command_id) or command
        if requested:
            _logger.info(
                "dataset.training_command.cancellation_requested",
                dataset_id=str(dataset_id),
                command_id=str(command_id),
                service=service.value,
                actor_id=str(caller.user.id),
                result=command.status.value,
            )
        return command
    raise DatasetRefusedError(
        DatasetRefusalCode.USAGE_STATE_CONFLICT,
        detail="命令已进入派发或已结束，不能按未派发任务取消；此操作不证明基座已停止；历史事实保持不变",
    )


def list_training_commands(
    *,
    service: TrainingService,
    dataset_id: UUID,
    caller: Caller,
    datasets: TrainingCommandRepository,
) -> Sequence[TrainingCommand]:
    """按数据集列出已受理的训练命令，供重新打开页面后恢复原命令。

    授权在用例边界强制，路由上的声明只是元数据；命令负载、内部路径与媒体身份不进入
    任何公开返回，调用者只拿命令身份与观测/结果事实。
    """
    authorize(caller, Permission.DATASET_VIEW)
    if datasets.dataset_by_id(dataset_id) is None:
        raise DatasetRefusedError(DatasetRefusalCode.DATASET_NOT_FOUND)
    return datasets.list_training_commands(service=service, dataset_id=dataset_id)


def read_training_command(
    *,
    service: TrainingService,
    dataset_id: UUID,
    command_id: UUID,
    caller: Caller,
    datasets: TrainingCommandRepository,
) -> TrainingCommand:
    """读取一条属于该数据集的训练命令；服务或数据集不匹配按不存在处理。"""
    authorize(caller, Permission.DATASET_VIEW)
    command = datasets.training_command_by_id(command_id)
    if command is None or command.dataset_id != dataset_id or command.service is not service:
        raise DatasetRefusedError(_COMMAND_NOT_FOUND[service])
    return command


def authorize_training_result_access(
    *,
    service: TrainingService,
    command_id: UUID,
    caller: Caller,
    datasets: TrainingCommandRepository,
) -> TrainingCommand:
    """授权读取已发布训练结果的最小用例。

    调用方是 Nginx auth_request 子请求；只有当前登录用户具备 DATASET_VIEW、命令存在且
    结果已真实登记时放行。内部对象键、源路径与原始负载不在此返回，数据由数据通道直接读取。
    """
    authorize(caller, Permission.DATASET_VIEW)
    command = datasets.training_command_by_id(command_id)
    if (
        command is None
        or command.service is not service
        or command.status is not TrainingCommandStatus.COMPLETED
        or command.result_status is not TrainingResultStatus.REGISTERED
        or command.result_config_object_key is None
        or command.result_checkpoint_object_key is None
    ):
        raise DatasetRefusedError(_RESULT_NOT_FOUND[service])
    naming = TRAINING_RESULT_NAMING[service]
    binding = re.fullmatch(
        rf"{re.escape(naming.prefix)}/{command.id}/([0-9a-f]{{32}})/"
        rf"{re.escape(naming.config_filename)}",
        command.result_config_object_key,
    )
    if (
        binding is None
        or command.result_checkpoint_object_key
        != f"{naming.prefix}/{command.id}/{binding.group(1)}/{naming.checkpoint_filename}"
    ):
        raise DatasetRefusedError(_RESULT_NOT_FOUND[service])
    return command


def _replay_command(
    *,
    existing: TrainingCommand,
    request: TrainingCommandInput,
    idempotency_key: str,
    now: datetime,
    jobs: TrainingJobQueue,
) -> TrainingCommandResult:
    """同一幂等身份且调用者本次选择不变时返回原命令；否则冲突。

    只比较调用者提供的选择与首次受理时冻结的载荷，不重新冻结，也不复核来源的当前修订；
    服务端固定的消费参数与派生身份保持首次受理事实。
    """
    if not request.matches(existing.payload):
        raise DatasetRefusedError(
            DatasetRefusalCode.IDEMPOTENCY_CONFLICT,
            detail=f"幂等键 {idempotency_key} 已对应另一份训练命令内容",
        )
    job = jobs.get_or_create_training(
        job_type=TRAINING_SERVICE_JOB_TYPES[request.service],
        dataset_id=existing.dataset_id,
        command_id=existing.id,
        now=now,
    )
    return TrainingCommandResult(command=existing, job=job)
