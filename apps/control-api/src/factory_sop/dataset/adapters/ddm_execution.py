"""DDM 训练命令 owner 的完整派发执行器：意图先落库，网络副作用后才绑定。

S073 只处理「是否向不幂等的基座 start 发送、以及发送后能否绑定真实基座任务身份」。
发送前把「逻辑基座目标 + 发送前任务集合 + 实际消费事实」作为一次性意图 ``result_unknown``
落库（``begin_ddm_training_dispatch``），只有该 CAS 的唯一获胜者可以发送；``backend_target``
的部分唯一索引把「同一逻辑基座的在途派发」变成独立于通用任务租约的持久约束。响应丢失、
发送后崩溃、4xx/5xx 或绑定提交失败一律保留 ``result_unknown`` 与在途保护，绝不重发 start、
也不直接读训练私有表兜底；恢复只按原查询接口的 ``aug_dataset_id`` 唯一匹配绑定。训练进度、
结果发布与约束释放属于 S074。

已知限制：意图落库前的材料化目录冲突会让命令保持 ``pending`` 并返回 ``blocked``（例如上一
个执行者在创建 ``input_root`` 后、写意图前崩溃）。此时不能用「删除/覆盖已存在工作副本」来自
auto 恢复，因为无法证明该副本不属于仍在进行的并发准备；本票保留这种安全阻塞，不做自动清理
或重试，等待操作者用新的输入身份重新受理。

``backend`` 与 ``dataset_root`` 由受信组合层注入：``backend`` 的 start/all_jobs/status 必须
指向同一个逻辑训练微服务及其持久任务库，``dataset_root`` 必须是该服务 ``DATASET_ROOT`` 指向
的共享挂载，且本执行器构造时传入的按角色 reader 参数就是服务当前实际使用的参数。HTTP 契约
本身无法证明这些同源事实，执行器不对此做断言；组合层未满足时应在 S074 保持 held。
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, assert_never
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from factory_sop.auth.api import (
    AuthorizationRefusedError,
    CurrentCallerResolver,
    Permission,
    require_current_actor,
)
from factory_sop.dataset.adapters.repository import PostgresDatasetRepository
from factory_sop.dataset.errors import DatasetRefusalCode, DatasetRefusedError
from factory_sop.dataset.model import DdmTrainingCommand, DdmTrainingCommandStatus
from factory_sop.dataset.storage import ObjectStorage, ObjectStorageUnavailableError
from factory_sop.dataset.usecases.ddm_command import decode_frozen_ddm_input
from factory_sop.dataset.usecases.usage import (
    DdmInputConsumption,
    FrozenDdmInput,
    materialize_ddm_input,
    verify_ddm_input_consumption,
)
from factory_sop.observability import get_logger

_logger = get_logger("dataset")

_FAILURE_CODE_LENGTH = 64
_FAILURE_DETAIL_LENGTH = 1024


class DdmTrainingBackendUnavailableError(Exception):
    """基座超时、5xx、连接失败或响应不完整：不能证明未执行。"""


class DdmTrainingBackend(Protocol):
    """DDM 训练基座的最小调用契约。"""

    def start(self, *, training_dataset_id: str, validation_dataset_id: str) -> str:
        """启动一次训练并返回基座任务身份；不可达时抛出。"""
        ...

    def all_jobs(self) -> Mapping[str, Mapping[str, Any]]:
        """返回基座当前全部训练任务，按任务身份为键。"""
        ...

    def status(self, job_id: str) -> Mapping[str, Any]:
        """查询原任务的状态。"""
        ...


class DdmTrainingDispatchOutcome(StrEnum):
    """一次 DDM 派发执行的可观察结果。"""

    BOUND = "bound"
    RESULT_UNKNOWN = "result_unknown"
    BLOCKED = "blocked"
    FAILED = "failed"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class DdmTrainingDispatchResult:
    """DDM 派发执行返回的持久事实与可诊断原因。"""

    outcome: DdmTrainingDispatchOutcome
    command: DdmTrainingCommand | None
    failure_code: str | None = None
    failure_detail: str | None = None


@dataclass(frozen=True, slots=True)
class _PreparedDispatch:
    """已材料化并通过消费核对、等待一次性意图落库的派发准备。"""

    frozen: FrozenDdmInput
    training_id: str
    validation_id: str
    consumption: DdmInputConsumption


@dataclass(slots=True)
class _AllocatedWorkcopies:
    """本次调用实际新建、可确证属于本命令的工作副本路径。

    ``root`` 只在材料化返回后记录，平铺目标只在每次成功 move 后记录；预存在的目标不进入
    此集合。只有命令确证 FAILED 时才删除这些路径。
    """

    root: Path | None = None
    flat_targets: list[Path] = field(default_factory=list)


class PostgresDdmTrainingExecutor:
    """在真实 PostgreSQL 上执行 DDM 训练命令的派发与恢复。"""

    def __init__(
        self,
        *,
        factory: sessionmaker[Session],
        storage_factory: Callable[[], ObjectStorage],
        generate: Callable[[Path, str], bytes],
        backend: DdmTrainingBackend,
        backend_target: str,
        dataset_root: Path,
        training_consumer_parameters: Mapping[str, object],
        validation_consumer_parameters: Mapping[str, object],
    ) -> None:
        if not backend_target.strip():
            raise ValueError("DDM 训练派发必须提供非空的逻辑基座目标身份")
        self._factory = factory
        self._storage_factory = storage_factory
        self._generate = generate
        self._backend = backend
        self._backend_target = backend_target
        self._dataset_root = dataset_root
        self._training_consumer_parameters = dict(training_consumer_parameters)
        self._validation_consumer_parameters = dict(validation_consumer_parameters)

    def execute(
        self,
        *,
        command_id: UUID,
        now: datetime,
        resolve_current_caller: CurrentCallerResolver,
    ) -> DdmTrainingDispatchResult:
        """派发或恢复一条 DDM 训练命令；重投递只对同一任务做查询与绑定。"""
        command = self._load(command_id)
        if command is None:
            return DdmTrainingDispatchResult(DdmTrainingDispatchOutcome.NOT_FOUND, None)
        return self._advance(command, now=now, resolve_current_caller=resolve_current_caller)

    def _advance(
        self,
        command: DdmTrainingCommand,
        *,
        now: datetime,
        resolve_current_caller: CurrentCallerResolver,
    ) -> DdmTrainingDispatchResult:
        match command.status:
            case DdmTrainingCommandStatus.PENDING:
                return self._dispatch(
                    command, now=now, resolve_current_caller=resolve_current_caller
                )
            case DdmTrainingCommandStatus.RESULT_UNKNOWN:
                return self._recover(command, now=now)
            case DdmTrainingCommandStatus.BOUND:
                return self._observe(command, now=now)
            case DdmTrainingCommandStatus.FAILED:
                return self._failed(command)
            case _:
                assert_never(command.status)

    def _dispatch(
        self,
        command: DdmTrainingCommand,
        *,
        now: datetime,
        resolve_current_caller: CurrentCallerResolver,
    ) -> DdmTrainingDispatchResult:
        owned = _AllocatedWorkcopies()
        denied = self._reject_if_unauthorized(
            command, resolve_current_caller=resolve_current_caller, now=now, owned=owned
        )
        if denied is not None:
            return denied
        prepared = self._prepare(command, now=now, owned=owned)
        if isinstance(prepared, DdmTrainingDispatchResult):
            return prepared
        try:
            jobs = self._backend.all_jobs()
        except DdmTrainingBackendUnavailableError:
            return self._fail(
                command_id=command.id,
                now=now,
                code="BASE_UNREACHABLE",
                detail="无法在发送前核对基座任务集合",
                owned=owned,
            )
        # 长准备/查询期间权限可能已被撤销：发送前必须按当前账号再次复核，意图落库后不再回退。
        denied = self._reject_if_unauthorized(
            command, resolve_current_caller=resolve_current_caller, now=now, owned=owned
        )
        if denied is not None:
            return denied
        baseline_ids = tuple(sorted(str(job_id) for job_id in jobs))
        begun = self._begin(
            command_id=command.id,
            baseline_job_ids=baseline_ids,
            dispatch_facts=_dispatch_facts(prepared),
            now=now,
        )
        if begun is None:
            current = self._load(command.id)
            if current is None:
                return DdmTrainingDispatchResult(DdmTrainingDispatchOutcome.NOT_FOUND, None)
            if current.status is DdmTrainingCommandStatus.PENDING:
                return DdmTrainingDispatchResult(
                    DdmTrainingDispatchOutcome.BLOCKED,
                    current,
                    "DISPATCH_IN_FLIGHT",
                    "同一训练基座已有在途命令，等待其身份确认",
                )
            if current.status is DdmTrainingCommandStatus.FAILED:
                # 意图 CAS 落空且权威状态已 FAILED：本次新建的自身工作副本不再需要，必须清理。
                self._discard_workcopies(command_id=current.id, owned=owned)
                return self._failed(current)
            return self._advance(current, now=now, resolve_current_caller=resolve_current_caller)
        return self._send(begun, prepared=prepared, baseline_ids=baseline_ids, now=now)

    def _prepare(
        self, command: DdmTrainingCommand, *, now: datetime, owned: _AllocatedWorkcopies
    ) -> _PreparedDispatch | DdmTrainingDispatchResult:
        try:
            frozen = decode_frozen_ddm_input(command.payload)
        except (KeyError, TypeError, ValueError):
            return self._fail(
                command_id=command.id,
                now=now,
                code="DISPATCH_PAYLOAD_INVALID",
                detail="DDM 训练命令冻结负载不可还原",
            )
        try:
            materialized = materialize_ddm_input(
                frozen=frozen,
                storage=self._storage_factory(),
                root=self._dataset_root,
            )
        except DatasetRefusedError as error:
            if error.code is DatasetRefusalCode.USAGE_STATE_CONFLICT:
                return self._blocked_on_preparation(command, now=now)
            return self._fail(
                command_id=command.id, now=now, code=error.code.value, detail=error.detail
            )
        except (ObjectStorageUnavailableError, OSError):
            return self._fail(
                command_id=command.id,
                now=now,
                code="STORAGE_UNAVAILABLE",
                detail="训练输入工作副本准备失败",
            )
        owned.root = materialized.root
        try:
            consumption = verify_ddm_input_consumption(
                frozen=frozen,
                materialized=materialized,
                training_consumer_parameters=self._training_consumer_parameters,
                validation_consumer_parameters=self._validation_consumer_parameters,
                generate=self._generate,
            )
        except DatasetRefusedError as error:
            return self._fail(
                command_id=command.id,
                now=now,
                code=error.code.value,
                detail=error.detail,
                owned=owned,
            )
        except (ObjectStorageUnavailableError, OSError):
            return self._fail(
                command_id=command.id,
                now=now,
                code="STORAGE_UNAVAILABLE",
                detail="训练输入消费核对失败",
                owned=owned,
            )
        training_id = self._training_directory_id(command.id)
        validation_id = self._validation_directory_id(command.id)
        try:
            self._expose(
                sources=(
                    (materialized.training_directory, self._dataset_root / training_id),
                    (materialized.validation_directory, self._dataset_root / validation_id),
                ),
                owned=owned,
            )
        except DatasetRefusedError as error:
            if error.code is DatasetRefusalCode.USAGE_STATE_CONFLICT:
                return self._blocked_on_preparation(command, now=now)
            return self._fail(
                command_id=command.id,
                now=now,
                code=error.code.value,
                detail=error.detail,
                owned=owned,
            )
        except OSError:
            return self._fail(
                command_id=command.id,
                now=now,
                code="STORAGE_UNAVAILABLE",
                detail="训练输入工作副本暴露失败",
                owned=owned,
            )
        return _PreparedDispatch(
            frozen=frozen,
            training_id=training_id,
            validation_id=validation_id,
            consumption=consumption,
        )

    def _reject_if_unauthorized(
        self,
        command: DdmTrainingCommand,
        *,
        resolve_current_caller: CurrentCallerResolver,
        now: datetime,
        owned: _AllocatedWorkcopies,
    ) -> DdmTrainingDispatchResult | None:
        try:
            require_current_actor(
                actor_id=command.created_by,
                permissions=(Permission.DATASET_EDIT,),
                resolve_current_caller=resolve_current_caller,
            )
        except AuthorizationRefusedError:
            return self._fail(
                command_id=command.id,
                now=now,
                code="PERMISSION_REVOKED",
                detail="执行者已无权派发 DDM 训练命令",
                owned=owned,
            )
        return None

    def _send(
        self,
        begun: DdmTrainingCommand,
        *,
        prepared: _PreparedDispatch,
        baseline_ids: Sequence[str],
        now: datetime,
    ) -> DdmTrainingDispatchResult:
        """只在一次性意图落库后调用不幂等的 start；任何后续失败都不释放保护。"""
        try:
            base_job_id = self._backend.start(
                training_dataset_id=prepared.training_id,
                validation_dataset_id=prepared.validation_id,
            )
        except DdmTrainingBackendUnavailableError:
            return self._unknown(
                command_id=begun.id,
                now=now,
                code="BASE_RESULT_UNKNOWN",
                detail="基座启动响应丢失或不可达，不能证明未执行",
            )
        if not _valid_base_job_id(base_job_id) or base_job_id in baseline_ids:
            return self._unknown(
                command_id=begun.id,
                now=now,
                code="BASE_IDENTITY_INVALID",
                detail="基座没有返回可核对的真实任务身份",
            )
        return self._bind(command_id=begun.id, base_job_id=base_job_id, now=now)

    def _recover(self, command: DdmTrainingCommand, *, now: datetime) -> DdmTrainingDispatchResult:
        """只按原查询接口精确匹配唯一新任务；零/多/不可达都保留未知与在途保护。"""
        if not self._target_matches(command):
            return self._target_mismatch(command)
        try:
            jobs = self._backend.all_jobs()
        except DdmTrainingBackendUnavailableError:
            return self._unknown(
                command_id=command.id,
                now=now,
                code="BASE_UNREACHABLE",
                detail="无法查询基座确认任务身份，保留在途保护",
            )
        training_id = self._training_directory_id(command.id)
        baseline = set(command.baseline_job_ids)
        for job_id, job in jobs.items():
            if not _valid_base_job_id(job_id) or str(job.get("id")) != str(job_id):
                return self._unknown(
                    command_id=command.id,
                    now=now,
                    code="DISPATCH_RESULT_UNVERIFIED",
                    detail="基座任务集合身份不一致，不能证明归属",
                )
        matches = [
            str(job_id)
            for job_id, job in jobs.items()
            if str(job.get("aug_dataset_id")) == training_id and str(job_id) not in baseline
        ]
        if len(matches) == 1:
            return self._bind(command_id=command.id, base_job_id=matches[0], now=now)
        detail = (
            "基座任务集合中没有本命令的唯一新任务，不能证明未执行"
            if not matches
            else "基座任务集合中出现多个匹配任务，不能证明归属"
        )
        return self._unknown(
            command_id=command.id,
            now=now,
            code="DISPATCH_RESULT_UNVERIFIED",
            detail=detail,
        )

    def _observe(self, command: DdmTrainingCommand, *, now: datetime) -> DdmTrainingDispatchResult:
        """已绑定只查询原任务并留窄观察，不发布结果、不释放在途保护。"""
        if not self._target_matches(command):
            return self._target_mismatch(command)
        base_job_id = command.base_job_id
        if base_job_id is not None:
            try:
                observation = self._backend.status(base_job_id)
            except DdmTrainingBackendUnavailableError:
                _logger.warning(
                    "dataset.ddm_training_dispatch.observe_unavailable",
                    command_id=str(command.id),
                    base_job_id=base_job_id,
                    backend_target=self._backend_target,
                )
            else:
                self._persist_observation(
                    command_id=command.id,
                    base_job_id=base_job_id,
                    observation=dict(observation),
                    now=now,
                )
        return DdmTrainingDispatchResult(DdmTrainingDispatchOutcome.BOUND, command)

    def _begin(
        self,
        *,
        command_id: UUID,
        baseline_job_ids: Sequence[str],
        dispatch_facts: dict[str, Any],
        now: datetime,
    ) -> DdmTrainingCommand | None:
        with self._factory() as session:
            updated = PostgresDatasetRepository(session).begin_ddm_training_dispatch(
                command_id=command_id,
                backend_target=self._backend_target,
                baseline_job_ids=baseline_job_ids,
                dispatch_facts=dispatch_facts,
                now=now,
            )
            session.commit()
        return updated

    def _bind(
        self, *, command_id: UUID, base_job_id: str, now: datetime
    ) -> DdmTrainingDispatchResult:
        try:
            with self._factory() as session:
                PostgresDatasetRepository(session).bind_ddm_training_base_job(
                    command_id=command_id, base_job_id=base_job_id, now=now
                )
                session.commit()
        except Exception:
            # 绑定提交不确定时保留 result_unknown：绝不重发 start，由后续投递重新唯一匹配。
            _logger.exception(
                "dataset.ddm_training_dispatch.bind_unverified",
                command_id=str(command_id),
                base_job_id=base_job_id,
                backend_target=self._backend_target,
            )
            return self._reload_result(
                command_id,
                now=now,
                fallback_code="BIND_UNVERIFIED",
                fallback_detail="基座任务身份绑定未确认，保留结果待核实",
            )
        current = self._load(command_id)
        if current is None:
            return DdmTrainingDispatchResult(DdmTrainingDispatchOutcome.NOT_FOUND, None)
        if current.status is DdmTrainingCommandStatus.BOUND:
            return self._observe(current, now=now)
        return self._reload_result(
            command_id,
            now=now,
            fallback_code="BIND_UNCONFIRMED",
            fallback_detail="基座任务身份尚未绑定，保留结果待核实",
        )

    def _unknown(
        self,
        *,
        command_id: UUID,
        now: datetime,
        code: str,
        detail: str,
    ) -> DdmTrainingDispatchResult:
        """记录未知诊断但保持 ``result_unknown`` 与在途保护，绝不释放或重发。"""
        try:
            with self._factory() as session:
                PostgresDatasetRepository(session).record_ddm_training_failure(
                    command_id=command_id,
                    expected_status=DdmTrainingCommandStatus.RESULT_UNKNOWN,
                    failure_code=_truncate(code, _FAILURE_CODE_LENGTH),
                    failure_detail=_truncate(detail, _FAILURE_DETAIL_LENGTH),
                    now=now,
                )
                session.commit()
        except Exception:
            _logger.exception(
                "dataset.ddm_training_dispatch.unknown_record_failed",
                command_id=str(command_id),
                backend_target=self._backend_target,
            )
            return self._reload_result(
                command_id, now=now, fallback_code=code, fallback_detail=detail
            )
        current = self._load(command_id)
        if current is None:
            return DdmTrainingDispatchResult(DdmTrainingDispatchOutcome.NOT_FOUND, None)
        if current.status is DdmTrainingCommandStatus.BOUND:
            return self._observe(current, now=now)
        if current.status is DdmTrainingCommandStatus.FAILED:
            return self._failed(current)
        _logger.warning(
            "dataset.ddm_training_dispatch.result_unknown",
            command_id=str(current.id),
            dataset_id=str(current.dataset_id),
            backend_target=self._backend_target,
            failure_code=current.failure_code,
            result="result_unknown",
        )
        return DdmTrainingDispatchResult(
            DdmTrainingDispatchOutcome.RESULT_UNKNOWN,
            current,
            current.failure_code,
            current.failure_detail,
        )

    def _fail(
        self,
        *,
        command_id: UUID,
        now: datetime,
        code: str,
        detail: str,
        owned: _AllocatedWorkcopies | None = None,
    ) -> DdmTrainingDispatchResult:
        """意图落库前的确定失败可以终止命令并释放保护；意图之后绝不走这里。"""
        try:
            with self._factory() as session:
                PostgresDatasetRepository(session).record_ddm_training_failure(
                    command_id=command_id,
                    expected_status=DdmTrainingCommandStatus.PENDING,
                    failure_code=_truncate(code, _FAILURE_CODE_LENGTH),
                    failure_detail=_truncate(detail, _FAILURE_DETAIL_LENGTH),
                    now=now,
                )
                session.commit()
        except Exception:
            _logger.exception(
                "dataset.ddm_training_dispatch.failure_record_failed",
                command_id=str(command_id),
                backend_target=self._backend_target,
            )
            return self._reload_result(
                command_id, now=now, fallback_code=code, fallback_detail=detail
            )
        current = self._load(command_id)
        if current is None:
            return DdmTrainingDispatchResult(DdmTrainingDispatchOutcome.NOT_FOUND, None)
        if current.status is DdmTrainingCommandStatus.FAILED:
            # 只在权威状态确证 FAILED 后清理本次新建的自身工作副本；BLOCKED/UNKNOWN/BOUND 保留。
            if owned is not None:
                self._discard_workcopies(command_id=command_id, owned=owned)
            _logger.warning(
                "dataset.ddm_training_dispatch.failed",
                command_id=str(current.id),
                dataset_id=str(current.dataset_id),
                failure_code=current.failure_code,
                result="failed",
            )
            return self._failed(current)
        if current.status is DdmTrainingCommandStatus.BOUND:
            return self._observe(current, now=now)
        if current.status is DdmTrainingCommandStatus.RESULT_UNKNOWN:
            return self._recover(current, now=now)
        return DdmTrainingDispatchResult(
            DdmTrainingDispatchOutcome.BLOCKED,
            current,
            code,
            detail,
        )

    def _reload_result(
        self,
        command_id: UUID,
        *,
        now: datetime,
        fallback_code: str,
        fallback_detail: str,
    ) -> DdmTrainingDispatchResult:
        current = self._load(command_id)
        if current is None:
            return DdmTrainingDispatchResult(DdmTrainingDispatchOutcome.NOT_FOUND, None)
        if current.status is DdmTrainingCommandStatus.BOUND:
            return self._observe(current, now=now)
        if current.status is DdmTrainingCommandStatus.FAILED:
            return self._failed(current)
        if current.status is DdmTrainingCommandStatus.RESULT_UNKNOWN:
            return DdmTrainingDispatchResult(
                DdmTrainingDispatchOutcome.RESULT_UNKNOWN,
                current,
                fallback_code,
                fallback_detail,
            )
        return DdmTrainingDispatchResult(
            DdmTrainingDispatchOutcome.BLOCKED,
            current,
            fallback_code,
            fallback_detail,
        )

    def _blocked_on_preparation(
        self, command: DdmTrainingCommand, *, now: datetime
    ) -> DdmTrainingDispatchResult:
        """准备目录冲突可能是并发准备而非确定失败：保留 pending，不误终止获胜派发。"""
        current = self._load(command.id)
        if current is None:
            return DdmTrainingDispatchResult(DdmTrainingDispatchOutcome.NOT_FOUND, None)
        if current.status is DdmTrainingCommandStatus.PENDING:
            return DdmTrainingDispatchResult(
                DdmTrainingDispatchOutcome.BLOCKED,
                current,
                "DISPATCH_PREPARATION_IN_FLIGHT",
                "同一命令的准备已在其它执行者进行或存在未清理的工作副本",
            )
        if current.status is DdmTrainingCommandStatus.BOUND:
            return self._observe(current, now=now)
        if current.status is DdmTrainingCommandStatus.RESULT_UNKNOWN:
            return self._recover(current, now=now)
        return self._failed(current)

    def _target_mismatch(self, command: DdmTrainingCommand) -> DdmTrainingDispatchResult:
        _logger.warning(
            "dataset.ddm_training_dispatch.backend_target_mismatch",
            command_id=str(command.id),
            recorded_backend_target=command.backend_target,
            injected_backend_target=self._backend_target,
        )
        return DdmTrainingDispatchResult(
            DdmTrainingDispatchOutcome.BLOCKED,
            command,
            "BACKEND_TARGET_MISMATCH",
            "注入的基座目标与命令冻结目标不一致，不能查询或绑定该任务",
        )

    def _persist_observation(
        self,
        *,
        command_id: UUID,
        base_job_id: str,
        observation: dict[str, Any],
        now: datetime,
    ) -> None:
        with self._factory() as session:
            PostgresDatasetRepository(session).observe_ddm_training_base_job(
                command_id=command_id,
                base_job_id=base_job_id,
                observation=observation,
                now=now,
            )
            session.commit()

    def _load(self, command_id: UUID) -> DdmTrainingCommand | None:
        with self._factory() as session:
            return PostgresDatasetRepository(session).ddm_training_command_by_id(command_id)

    def _target_matches(self, command: DdmTrainingCommand) -> bool:
        return command.backend_target == self._backend_target

    def _failed(self, command: DdmTrainingCommand) -> DdmTrainingDispatchResult:
        return DdmTrainingDispatchResult(
            DdmTrainingDispatchOutcome.FAILED,
            command,
            command.failure_code,
            command.failure_detail,
        )

    def _expose(
        self,
        *,
        sources: Sequence[tuple[Path, Path]],
        owned: _AllocatedWorkcopies,
    ) -> None:
        """把已验证的双集工作副本移动到基座可读的平铺目录，目标已存在即拒绝。"""
        for _source, target in sources:
            if target.exists():
                raise DatasetRefusedError(
                    DatasetRefusalCode.USAGE_STATE_CONFLICT,
                    detail="基座数据集目录已被其他准备占用",
                )
        for source, target in sources:
            os.replace(source, target)
            # 只有本次成功 move 出来的目标才属于本命令；预存在目标在 preflight 已拒绝。
            owned.flat_targets.append(target)

    def _discard_workcopies(self, *, command_id: UUID, owned: _AllocatedWorkcopies) -> None:
        """命令确证 FAILED 后只删除本次调用新建的工作副本。

        删除失败只记录，不重试、不声称已删除，也不改变返回的原始失败诊断。
        """
        paths = [*owned.flat_targets]
        if owned.root is not None:
            paths.append(owned.root)
        for path in paths:
            try:
                shutil.rmtree(path)
            except FileNotFoundError:
                continue
            except OSError:
                _logger.warning(
                    "dataset.ddm_training_dispatch.workcopy_cleanup_failed",
                    command_id=str(command_id),
                    path=str(path),
                )

    @staticmethod
    def _training_directory_id(command_id: UUID) -> str:
        return f"ddm-{command_id}-training"

    @staticmethod
    def _validation_directory_id(command_id: UUID) -> str:
        return f"ddm-{command_id}-validation"


class HttpDdmTrainingBackend:
    """通过真实 HTTP 调用 DDM 训练微服务的 start/all_jobs/status 契约。

    start 的 4xx 与 5xx 一律按不可达处理：请求可能已在基座创建任务，只有调用方的一次性
    意图与后续原查询接口能证明归属，HTTP 状态码本身不构成「未执行」的证据。
    """

    def __init__(self, *, base_url: str, timeout_seconds: int) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise DdmTrainingBackendUnavailableError("DDM 训练基座地址必须是 HTTP(S) 主机")
        if parsed.username is not None or parsed.password is not None:
            raise DdmTrainingBackendUnavailableError("DDM 训练基座地址不得包含 userinfo")
        if parsed.query or parsed.fragment:
            raise DdmTrainingBackendUnavailableError("DDM 训练基座地址不得包含 query 或 fragment")
        self._scheme = parsed.scheme
        self._host = parsed.hostname
        self._port = parsed.port
        self._base_path = parsed.path.rstrip("/")
        self._timeout = timeout_seconds

    def start(self, *, training_dataset_id: str, validation_dataset_id: str) -> str:
        query = urlencode(
            {"dataset_id": training_dataset_id, "validation_dataset_id": validation_dataset_id}
        )
        body = self._request(
            method="POST", path=f"{self._base_path}/api/v1/fine-tuning/start?{query}"
        )
        document = _json_document(body)
        job_id = document.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise DdmTrainingBackendUnavailableError("DDM 训练基座没有返回任务身份")
        return job_id

    def all_jobs(self) -> Mapping[str, Mapping[str, Any]]:
        body = self._request(method="GET", path=f"{self._base_path}/api/v1/fine-tuning/all_jobs")
        document = _json_document(body)
        jobs: dict[str, Mapping[str, Any]] = {}
        for job_id, job in document.items():
            if not isinstance(job, dict):
                raise DdmTrainingBackendUnavailableError("DDM 训练基座任务集合格式不合法")
            # 只有键与任务体自带身份一致的真实 UUID 才能用于唯一归属证明。
            if not _valid_base_job_id(job_id) or str(job.get("id")) != str(job_id):
                raise DdmTrainingBackendUnavailableError("DDM 训练基座任务身份不一致")
            jobs[str(job_id)] = job
        return jobs

    def status(self, job_id: str) -> Mapping[str, Any]:
        return _json_document(
            self._request(
                method="GET", path=f"{self._base_path}/api/v1/fine-tuning/status/{job_id}"
            )
        )

    def _request(self, *, method: str, path: str) -> bytes:
        connection = self._connection()
        try:
            connection.connect()
            connection.putrequest(method, path)
            connection.putheader("Content-Length", "0")
            connection.putheader("Connection", "close")
            connection.endheaders()
            response = connection.getresponse()
            body = response.read()
            if not 200 <= response.status < 300:
                raise DdmTrainingBackendUnavailableError(
                    f"DDM 训练基座未返回成功响应（HTTP {response.status}）"
                )
            return body
        except DdmTrainingBackendUnavailableError:
            raise
        except (OSError, http.client.HTTPException) as error:
            raise DdmTrainingBackendUnavailableError("无法连接 DDM 训练基座") from error
        finally:
            connection.close()

    def _connection(self) -> http.client.HTTPConnection:
        if self._scheme == "https":
            return http.client.HTTPSConnection(self._host, self._port, timeout=self._timeout)
        return http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)


def _dispatch_facts(prepared: _PreparedDispatch) -> dict[str, Any]:
    """冻结目标、独立输入身份与实际消费事实，作为意图的一部分原子落库。"""
    return {
        "input_id": str(prepared.frozen.input_id),
        "training_dataset_id": prepared.training_id,
        "validation_dataset_id": prepared.validation_id,
        "consumption": _consumption_facts(prepared.consumption),
    }


def _consumption_facts(consumption: DdmInputConsumption) -> dict[str, Any]:
    return {
        "input_id": str(consumption.input_id),
        "training_annotation_filename": consumption.training_annotation_filename,
        "training_annotation_sha256": consumption.training_annotation_sha256,
        "validation_annotation_filename": consumption.validation_annotation_filename,
        "validation_annotation_sha256": consumption.validation_annotation_sha256,
        "consumed_files": [
            {
                "role": item.role,
                "filename": item.filename,
                "source_object_key": item.source_object_key,
                "source_sha256": item.source_sha256,
                "size": item.size,
            }
            for item in consumption.consumed_files
        ],
    }


def _json_document(body: bytes) -> dict[str, Any]:
    try:
        document = json.loads(body)
    except (TypeError, ValueError, UnicodeDecodeError) as error:
        raise DdmTrainingBackendUnavailableError("DDM 训练基座响应不是有效 JSON") from error
    if not isinstance(document, dict):
        raise DdmTrainingBackendUnavailableError("DDM 训练基座响应不是 JSON 对象")
    return document


def _valid_base_job_id(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"
