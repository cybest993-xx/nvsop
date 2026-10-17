"""训练命令 owner 的共享派发执行器：意图先落库，网络副作用之后才绑定。

状态机对「是否向不幂等的基座 start 发送、发送后能否绑定真实基座任务身份、已绑定任务如何
观测与登记结果」只有一份实现。发送前把「逻辑基座目标 + 发送前任务集合 + 实际消费事实」作为
一次性意图 ``result_unknown`` 落库（``begin_training_dispatch``），只有该 CAS 的唯一获胜者
可以发送；``service + backend_target`` 的部分唯一索引把「同一服务同一逻辑基座的在该派发」
变成独立于通用任务租约的持久约束。响应丢失、发送后崩溃、4xx/5xx 或绑定提交失败一律保留
``result_unknown`` 与在途保护，绝不重发 start、也不直接读训练私有表兜底；恢复只按原查询接口
的服务身份谓词唯一匹配后绑定。

服务差异（输入材料化与消费核对、基座 HTTP 契约、恢复身份谓词、结果核实与命名、真实取消
入口）由 ``TrainingServiceAdapter`` 提供；执行器不假设任何服务的私有表或目录布局。

``backend`` 与 ``dataset_root`` 由受信组合层注入：adapter 的 start/all_jobs/status 必须指向
同一个逻辑训练微服务及其持久任务库，输入根必须是该服务 ``DATASET_ROOT`` 指向的共享挂载，
且 adapter 使用的按角色 reader 参数就是服务当前实际使用的参数。HTTP 契约本身无法证明这些
同源事实，执行器不对此做断言；组合层未满足时应保持 held。
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from copy import copy
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol, assert_never
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy.orm import Session, sessionmaker

from factory_sop.auth.api import (
    AuthorizationRefusedError,
    CurrentCallerResolver,
    Permission,
    require_current_actor,
)
from factory_sop.dataset.adapters.repository import PostgresDatasetRepository
from factory_sop.dataset.api import (
    ArtifactTransactionCommitter,
    TrainingJobFinisher,
    TrainingJobOutcome,
    TrainingJobResult,
)
from factory_sop.dataset.errors import DatasetRefusalCode, DatasetRefusedError
from factory_sop.dataset.model import (
    TrainingBaseStatus,
    TrainingCommand,
    TrainingCommandStatus,
    TrainingResultStatus,
    TrainingService,
)
from factory_sop.dataset.storage import ObjectStorage, ObjectStorageUnavailableError
from factory_sop.observability import get_logger

_logger = get_logger("dataset")

FAILURE_CODE_LENGTH = 64
FAILURE_DETAIL_LENGTH = 1024
RESULT_COPY_CHUNK_BYTES = 1024 * 1024
_JOB_TERMINAL_OUTCOMES = {
    TrainingCommandStatus.COMPLETED: TrainingJobOutcome.COMPLETED,
    TrainingCommandStatus.FAILED: TrainingJobOutcome.FAILED,
    TrainingCommandStatus.CANCELLED: TrainingJobOutcome.CANCELLED,
}


class TrainingBackendUnavailableError(Exception):
    """只把 HTTP 状态或异常类别传给持久诊断，不保存响应体/URL/原异常文本。"""

    def __init__(
        self, detail: str, *, http_status: int | None = None, error_type: str | None = None
    ) -> None:
        super().__init__(detail)
        self.http_status = http_status
        self.error_type = error_type

    @property
    def safe_fact(self) -> str:
        if self.http_status is not None:
            return f"（HTTP {self.http_status}）"
        if self.error_type is not None:
            return f"（{self.error_type}）"
        return "（响应契约无效）"


class TrainingPreparationRefusedError(Exception):
    """准备阶段的可分类失败：``code`` 与 ``detail`` 直接进入持久诊断。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class TrainingResultUnavailableError(Exception):
    """基座结果文件尚未写出或不可读；保留在途保护，下次观测可再试。"""


class TrainingResultInvalidError(Exception):
    """基座结果内容与本命令的冻结事实不一致；必须拒绝登记而不是发布错误结果。"""


@dataclass(frozen=True, slots=True)
class PreparedTrainingDispatch:
    """一次派发准备：唯一匹配身份、原子落库事实与（意图落库后才调用的）启动入口。"""

    identity: str
    dispatch_facts: Mapping[str, Any]
    start: Callable[[], str]


@dataclass(frozen=True, slots=True)
class RegisteredTrainingResult:
    """已核实并写入不可变对象的结果事实。"""

    config_object_key: str
    config_sha256: str
    config_size: int
    checkpoint_object_key: str
    checkpoint_sha256: str
    checkpoint_size: int


class TrainingServiceAdapter(Protocol):
    """一种训练服务对共享命令状态机的实际差异。"""

    @property
    def service(self) -> TrainingService:
        """本 adapter 归属的训练服务。"""
        ...

    @property
    def backend_target(self) -> str:
        """逻辑基座目标身份；既是在途保护的范围键，也是「同一任务库」的可追溯声明。"""
        ...

    @property
    def result_registration_available(self) -> bool:
        """结果读取是否已配置；未配置时基座完成也不登记，只保留可诊断的在途保护。"""
        ...

    def prepare(
        self,
        *,
        command: TrainingCommand,
        storage: ObjectStorage,
        owned: AllocatedWorkcopies,
    ) -> PreparedTrainingDispatch:
        """材料化并核对本次派发要消费的输入，返回唯一匹配身份与消费事实。

        只消费命令的冻结负载；可归因于并发准备/目录占用的冲突抛 ``DatasetRefusedError``
        且 ``code`` 为 ``USAGE_STATE_CONFLICT``（执行器保留 ``pending`` 待重试），其余确定
        失败抛 ``TrainingPreparationRefusedError``。本次新建、可确证属于本命令的工作副本
        记入 ``owned``。
        """
        ...

    def all_jobs(self) -> Mapping[str, Mapping[str, Any]]:
        """返回基座当前全部任务，按任务身份为键。"""
        ...

    def status(self, job_id: str) -> Mapping[str, Any]:
        """查询原任务的状态。"""
        ...

    def identity_matches(self, job: Mapping[str, Any], *, identity: str) -> bool:
        """恢复时判断基座任务是否属于本命令；不能证明归属时必须为假。"""
        ...

    def input_identity(self, command_id: UUID) -> str:
        """返回本命令在基座消费目录上的唯一身份，供恢复时唯一匹配。"""
        ...

    def result_object_keys(self, *, command_id: UUID, candidate: str) -> tuple[str, str]:
        """返回本次登记的配置与结果对象键。"""
        ...

    def copy_result(
        self,
        *,
        command: TrainingCommand,
        base_job_id: str,
        storage: ObjectStorage,
        config_object_key: str,
        checkpoint_object_key: str,
    ) -> RegisteredTrainingResult:
        """核实真实结果并写入对象键；不可读或与冻结事实不符时抛出。"""
        ...

    def cancel(self, *, job_id: str) -> Mapping[str, Any]:
        """调用该服务真实的取消入口并返回原始响应。

        没有真实取消入口的服务必须显式失败：HTTP 200 与成功文案都不是进程停止证明。
        """
        ...


@dataclass(slots=True)
class AllocatedWorkcopies:
    """本次调用实际新建、可确证属于本命令的工作副本路径。

    ``root`` 只在材料化返回后记录，平铺目标只在每次成功 move 后记录；预存在的目标不进入
    此集合。只有命令确证 FAILED 或确证已取消时才删除这些路径。
    """

    root: Path | None = None
    flat_targets: list[Path] = field(default_factory=list)


class TrainingDispatchOutcome(StrEnum):
    """一次派发执行的可观察结果。"""

    BOUND = "bound"
    RESULT_UNKNOWN = "result_unknown"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class TrainingDispatchResult:
    """派发执行返回的持久事实与可诊断原因。"""

    outcome: TrainingDispatchOutcome
    command: TrainingCommand | None
    failure_code: str | None = None
    failure_detail: str | None = None


def is_directory(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def is_regular_file(path: Path) -> bool:
    try:
        import stat

        return stat.S_ISREG(path.lstat().st_mode) and not path.is_symlink()
    except OSError:
        return False


def ensure_within_root(root: Path, path: Path) -> None:
    """确认结果路径留在受信挂载内且不经过任何符号链接。"""
    absolute_root = root.absolute()
    absolute_path = path.absolute()
    try:
        relative = absolute_path.relative_to(absolute_root)
    except ValueError as error:
        raise TrainingResultInvalidError("基座文件路径越出受信挂载") from error
    if ".." in relative.parts or absolute_root.is_symlink():
        raise TrainingResultInvalidError("基座文件路径越界或使用符号链接")
    current = absolute_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise TrainingResultInvalidError("基座文件路径使用符号链接")


def expose_workcopies(*, sources: Sequence[tuple[Path, Path]], owned: AllocatedWorkcopies) -> None:
    """把已验证的输入工作副本移动到基座可读的平铺目录，目标已存在即拒绝。"""
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


def copy_result_file(*, source: Path, storage: ObjectStorage, object_key: str) -> tuple[int, str]:
    """流式写入不可变对象键并计算大小/摘要；空文件或回读大小不符立即拒绝。"""
    digest = sha256()
    size = 0
    try:
        with source.open("rb") as reader, storage.writing(object_key=object_key) as sink:
            while True:
                chunk = reader.read(RESULT_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
                sink.write(chunk)
        if size <= 0:
            raise TrainingResultInvalidError("基座结果文件为空")
        if storage.stat(object_key=object_key).size != size:
            raise ObjectStorageUnavailableError("结果对象回读大小与源文件不一致")
    except OSError as error:
        raise TrainingResultUnavailableError("基座结果文件不可读") from error
    return size, digest.hexdigest()


def write_result_object(
    *, content: bytes, storage: ObjectStorage, object_key: str
) -> tuple[int, str]:
    """把规范字节写入不可变对象键并复核回读大小；空内容立即拒绝。"""
    if not content:
        raise TrainingResultInvalidError("登记结果内容为空")
    try:
        with storage.writing(object_key=object_key) as sink:
            sink.write(content)
        if storage.stat(object_key=object_key).size != len(content):
            raise ObjectStorageUnavailableError("结果对象回读大小与源内容不一致")
    except OSError as error:
        raise TrainingResultUnavailableError("登记结果对象不可写") from error
    return len(content), sha256(content).hexdigest()


def http_backend_connection(
    *, base_url: str, timeout_seconds: int, service_label: str
) -> tuple[str, str, int | None, str, int]:
    """校验训练基座地址并返回连接事实；非法地址按不可达处理，不静默回退。"""
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise TrainingBackendUnavailableError(f"{service_label}训练基座地址必须是 HTTP(S) 主机")
    if parsed.username is not None or parsed.password is not None:
        raise TrainingBackendUnavailableError(f"{service_label}训练基座地址不得包含 userinfo")
    if parsed.query or parsed.fragment:
        raise TrainingBackendUnavailableError(
            f"{service_label}训练基座地址不得包含 query 或 fragment"
        )
    return parsed.scheme, parsed.hostname, parsed.port, parsed.path.rstrip("/"), timeout_seconds


def http_response(
    *,
    scheme: str,
    host: str,
    port: int | None,
    timeout_seconds: int,
    service_label: str,
    method: str,
    path: str,
) -> tuple[int, bytes]:
    """向训练基座发一次无 body 的 HTTP 请求，返回原始状态与响应体。

    只在连接层失败时抛 ``TrainingBackendUnavailableError``；调用方自己决定哪些状态码属于
    可核对的响应、哪些属于不可用。
    """
    connection = _connection(
        scheme=scheme,
        host=host,
        port=port,
        timeout_seconds=timeout_seconds,
    )
    try:
        connection.connect()
        connection.putrequest(method, path)
        connection.putheader("Content-Length", "0")
        connection.putheader("Connection", "close")
        connection.endheaders()
        response = connection.getresponse()
        return response.status, response.read()
    except (OSError, http.client.HTTPException) as error:
        raise TrainingBackendUnavailableError(
            f"无法连接{service_label}训练基座", error_type=type(error).__name__
        ) from error
    finally:
        connection.close()


def http_request(
    *,
    scheme: str,
    host: str,
    port: int | None,
    timeout_seconds: int,
    service_label: str,
    method: str,
    path: str,
) -> bytes:
    """向训练基座发一次无 body 的 HTTP 请求；非 2xx 一律按不可达交给调用方分类。"""
    status, body = http_response(
        scheme=scheme,
        host=host,
        port=port,
        timeout_seconds=timeout_seconds,
        service_label=service_label,
        method=method,
        path=path,
    )
    if not 200 <= status < 300:
        raise TrainingBackendUnavailableError(
            f"{service_label}训练基座未返回成功响应（HTTP {status}）",
            http_status=status,
        )
    return body


def _connection(
    *,
    scheme: str,
    host: str,
    port: int | None,
    timeout_seconds: int,
) -> http.client.HTTPConnection:
    if scheme == "https":
        return http.client.HTTPSConnection(host, port, timeout=timeout_seconds)
    return http.client.HTTPConnection(host, port, timeout=timeout_seconds)


def json_document(body: bytes, *, service_label: str) -> dict[str, Any]:
    try:
        document = json.loads(body)
    except (TypeError, ValueError, UnicodeDecodeError) as error:
        raise TrainingBackendUnavailableError(
            f"{service_label}训练基座响应不是有效 JSON"
        ) from error
    if not isinstance(document, dict):
        raise TrainingBackendUnavailableError(f"{service_label}训练基座响应不是 JSON 对象")
    return document


def valid_base_job_id(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        UUID(value)
    except ValueError:
        return False
    return True


class PostgresTrainingCommandExecutor:
    """在真实 PostgreSQL 上执行训练命令的派发、观测、取消核实与结果登记。"""

    def __init__(
        self,
        *,
        factory: sessionmaker[Session],
        storage_factory: Callable[[], ObjectStorage],
        adapter: TrainingServiceAdapter,
    ) -> None:
        if not adapter.backend_target.strip():
            raise ValueError("训练派发必须提供非空的逻辑基座目标身份")
        self._factory = factory
        self._storage_factory = storage_factory
        self._adapter = adapter
        self._job_finished = False
        self._job_lease_lost = False
        self._finish_job: TrainingJobFinisher | None = None
        self._commit_transaction: ArtifactTransactionCommitter | None = None

    def execute(
        self,
        *,
        command_id: UUID,
        now: datetime,
        resolve_current_caller: CurrentCallerResolver,
    ) -> TrainingDispatchResult:
        """派发或恢复一条训练命令；重投递只对同一任务做查询与绑定。"""
        command = self._load(command_id)
        if command is None:
            return TrainingDispatchResult(TrainingDispatchOutcome.NOT_FOUND, None)
        return self._advance(command, now=now, resolve_current_caller=resolve_current_caller)

    def execute_job(
        self,
        *,
        command_id: UUID,
        now: datetime,
        resolve_current_caller: CurrentCallerResolver,
        finish_job: TrainingJobFinisher,
        commit_transaction: ArtifactTransactionCommitter,
    ) -> TrainingJobResult:
        """job 的租约/围栏回调参与结果、命令及通用终态的同一个事务。"""
        # 每次调用独立持有回调；共享执行器不保存另一个 worker 的租约或取消围栏。
        scoped = copy(self)
        scoped._finish_job = finish_job
        scoped._commit_transaction = commit_transaction
        scoped.execute(
            command_id=command_id, now=now, resolve_current_caller=resolve_current_caller
        )
        command = scoped._load(command_id)
        if command is None:
            return TrainingJobResult(TrainingJobOutcome.FAILED, "TRAINING_COMMAND_NOT_FOUND")
        if scoped._job_lease_lost:
            return TrainingJobResult(TrainingJobOutcome.LEASE_LOST)
        if command.status in _JOB_TERMINAL_OUTCOMES:
            terminal = TrainingJobResult(
                _JOB_TERMINAL_OUTCOMES[command.status], command.failure_code
            )
            if not scoped._job_finished:
                with self._factory() as session:
                    if not finish_job(session, terminal) or not commit_transaction(session):
                        session.rollback()
                        return TrainingJobResult(TrainingJobOutcome.LEASE_LOST)
            return terminal
        return TrainingJobResult(TrainingJobOutcome.PENDING)

    def _advance(
        self,
        command: TrainingCommand,
        *,
        now: datetime,
        resolve_current_caller: CurrentCallerResolver,
    ) -> TrainingDispatchResult:
        match command.status:
            case TrainingCommandStatus.PENDING:
                return self._dispatch(
                    command, now=now, resolve_current_caller=resolve_current_caller
                )
            case TrainingCommandStatus.RESULT_UNKNOWN:
                return self._recover(command, now=now)
            case TrainingCommandStatus.BOUND:
                return self._observe(command, now=now)
            case TrainingCommandStatus.COMPLETED:
                return self._completed(command)
            case TrainingCommandStatus.FAILED:
                return self._failed(command)
            case TrainingCommandStatus.CANCELLED:
                return self._cancelled(command)
            case _:
                assert_never(command.status)

    def _dispatch(
        self,
        command: TrainingCommand,
        *,
        now: datetime,
        resolve_current_caller: CurrentCallerResolver,
    ) -> TrainingDispatchResult:
        owned = AllocatedWorkcopies()
        denied = self._reject_if_unauthorized(
            command, resolve_current_caller=resolve_current_caller, now=now, owned=owned
        )
        if denied is not None:
            return denied
        prepared = self._prepare(command, now=now, owned=owned)
        if isinstance(prepared, TrainingDispatchResult):
            return prepared
        try:
            jobs = self._adapter.all_jobs()
        except TrainingBackendUnavailableError:
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
            dispatch_facts=dict(prepared.dispatch_facts),
            now=now,
        )
        if begun is None:
            current = self._load(command.id)
            if current is None:
                return TrainingDispatchResult(TrainingDispatchOutcome.NOT_FOUND, None)
            if current.status is TrainingCommandStatus.PENDING:
                return TrainingDispatchResult(
                    TrainingDispatchOutcome.BLOCKED,
                    current,
                    "DISPATCH_IN_FLIGHT",
                    "同一训练基座已有在途命令，等待其身份确认",
                )
            if current.status is TrainingCommandStatus.FAILED:
                # 意图 CAS 落空且权威状态已 FAILED：本次新建的自身工作副本不再需要，必须清理。
                self._discard_workcopies(command_id=current.id, owned=owned)
                return self._failed(current)
            if current.status is TrainingCommandStatus.CANCELLED:
                # 意图 CAS 落空且权威状态已取消：本次新建的自身工作副本不再需要，必须清理。
                return self._cancelled(current, owned=owned)
            return self._advance(current, now=now, resolve_current_caller=resolve_current_caller)
        return self._send(begun, prepared=prepared, baseline_ids=baseline_ids, now=now)

    def _prepare(
        self, command: TrainingCommand, *, now: datetime, owned: AllocatedWorkcopies
    ) -> PreparedTrainingDispatch | TrainingDispatchResult:
        try:
            return self._adapter.prepare(
                command=command, storage=self._storage_factory(), owned=owned
            )
        except DatasetRefusedError as error:
            if error.code is DatasetRefusalCode.USAGE_STATE_CONFLICT:
                return self._blocked_on_preparation(command, now=now, owned=owned)
            return self._fail(
                command_id=command.id,
                now=now,
                code=error.code.value,
                detail=error.detail,
                owned=owned,
            )
        except TrainingPreparationRefusedError as error:
            return self._fail(
                command_id=command.id,
                now=now,
                code=error.code,
                detail=error.detail,
                owned=owned,
            )

    def _reject_if_unauthorized(
        self,
        command: TrainingCommand,
        *,
        resolve_current_caller: CurrentCallerResolver,
        now: datetime,
        owned: AllocatedWorkcopies,
    ) -> TrainingDispatchResult | None:
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
                detail="执行者已无权派发训练命令",
                owned=owned,
            )
        return None

    def _send(
        self,
        begun: TrainingCommand,
        *,
        prepared: PreparedTrainingDispatch,
        baseline_ids: Sequence[str],
        now: datetime,
    ) -> TrainingDispatchResult:
        """只在一次性意图落库后调用不幂等的 start；任何后续失败都不释放保护。"""
        try:
            base_job_id = prepared.start()
        except TrainingBackendUnavailableError:
            return self._unknown(
                command_id=begun.id,
                now=now,
                code="BASE_RESULT_UNKNOWN",
                detail="基座启动响应丢失或不可达，不能证明未执行",
            )
        if not valid_base_job_id(base_job_id) or base_job_id in baseline_ids:
            return self._unknown(
                command_id=begun.id,
                now=now,
                code="BASE_IDENTITY_INVALID",
                detail="基座没有返回可核对的真实任务身份",
            )
        return self._bind(command_id=begun.id, base_job_id=base_job_id, now=now)

    def _recover(self, command: TrainingCommand, *, now: datetime) -> TrainingDispatchResult:
        """只按原查询接口精确匹配唯一新任务；零/多/不可达都保留未知与在途保护。"""
        if not self._target_matches(command):
            return self._target_mismatch(command)
        try:
            jobs = self._adapter.all_jobs()
        except TrainingBackendUnavailableError:
            return self._unknown(
                command_id=command.id,
                now=now,
                code="BASE_UNREACHABLE",
                detail="无法查询基座确认任务身份，保留在途保护",
            )
        identity = self._adapter.input_identity(command_id=command.id)
        baseline = set(command.baseline_job_ids)
        for job_id, job in jobs.items():
            if not valid_base_job_id(job_id) or str(job.get("id")) != str(job_id):
                return self._unknown(
                    command_id=command.id,
                    now=now,
                    code="DISPATCH_RESULT_UNVERIFIED",
                    detail="基座任务集合身份不一致，不能证明归属",
                )
        matches = [
            str(job_id)
            for job_id, job in jobs.items()
            if str(job_id) not in baseline
            and self._adapter.identity_matches(job, identity=identity)
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

    def _observe(self, command: TrainingCommand, *, now: datetime) -> TrainingDispatchResult:
        """已绑定查询原任务：保留基座原状态与最后观测时刻，只有真实可读结果才登记并发布。"""
        if not self._target_matches(command):
            return self._target_mismatch(command)
        base_job_id = command.base_job_id
        if base_job_id is None:
            return TrainingDispatchResult(TrainingDispatchOutcome.BOUND, command)
        if command.cancel_requested_at is not None and command.cancel_response is None:
            self._request_dispatched_cancellation(command, base_job_id=base_job_id, now=now)
        try:
            observation = self._adapter.status(base_job_id)
        except TrainingBackendUnavailableError as error:
            detail = "无法查询基座原任务状态，保留最后观测与在途保护" + error.safe_fact
            self._record_observation_failure(
                command_id=command.id,
                base_job_id=base_job_id,
                code="BASE_OBSERVATION_UNAVAILABLE",
                detail=detail,
                now=now,
            )
            return TrainingDispatchResult(
                TrainingDispatchOutcome.RESULT_UNKNOWN,
                command,
                "BASE_OBSERVATION_UNAVAILABLE",
                detail,
            )
        raw = _safe_observation(observation)
        self._persist_observation(
            command_id=command.id,
            base_job_id=base_job_id,
            observation=raw,
            now=now,
        )
        current = self._load(command.id)
        if current is None:
            return TrainingDispatchResult(TrainingDispatchOutcome.NOT_FOUND, None)
        if current.status is not TrainingCommandStatus.BOUND:
            return TrainingDispatchResult(TrainingDispatchOutcome.BOUND, current)
        return self._advance_observation(
            current, base_job_id=base_job_id, observation=current.base_observation or {}, now=now
        )

    def _request_dispatched_cancellation(
        self, command: TrainingCommand, *, base_job_id: str, now: datetime
    ) -> None:
        """已派发取消只调用该服务真实取消入口，并把原始响应与未核实结论持久化。

        HTTP 200、成功文案与基座 ``cancelled`` 元数据都不是进程停止证明：本执行器不据此
        进入取消终态，命令保持在途保护，也不重发 start。只有收到响应才停止重复调用；连接层
        不可达只记结论，下次观测再试。
        """
        try:
            response = self._adapter.cancel(job_id=base_job_id)
        except TrainingBackendUnavailableError as error:
            self._record_cancellation(
                command_id=command.id,
                base_job_id=base_job_id,
                response={"outcome": "unreachable", "stage": "cancel_request"},
                detail="基座取消入口未成功返回，不能证明进程已停止" + error.safe_fact,
                now=now,
            )
            return
        self._record_cancellation(
            command_id=command.id,
            base_job_id=base_job_id,
            response=_cancel_response_fact(response),
            detail="已调用基座真实取消入口；该基座不提供进程停止证明，保留未核实状态",
            now=now,
        )

    def _record_cancellation(
        self,
        *,
        command_id: UUID,
        base_job_id: str,
        response: Mapping[str, Any],
        detail: str,
        now: datetime,
    ) -> None:
        """只保存基座原响应与本次核实结论；命令保持在途，绝不在此释放保护。"""
        try:
            with self._factory() as session:
                PostgresDatasetRepository(session).record_training_cancellation(
                    command_id=command_id,
                    base_job_id=base_job_id,
                    response=dict(response),
                    detail=_truncate(detail, 512),
                    now=now,
                )
                session.commit()
        except Exception as error:
            _logger.error(
                "dataset.training_dispatch.cancellation_record_failed",
                command_id=str(command_id),
                error_type=type(error).__name__,
            )

    def _advance_observation(
        self,
        command: TrainingCommand,
        *,
        base_job_id: str,
        observation: Mapping[str, Any],
        now: datetime,
    ) -> TrainingDispatchResult:
        """按原状态推进：完成还需结果验证；失败终止，取消仍缺停止证明。"""
        status = _base_status(observation.get("status"))
        if status is TrainingBaseStatus.CANCELLED:
            # 基座取消元数据不能证明进程已停；仍只查询原任务并保持在途保护。
            self._record_observation_failure(
                command_id=command.id,
                base_job_id=base_job_id,
                code="BASE_CANCELLATION_UNVERIFIED",
                detail="基座报告 cancelled，但尚无进程停止证明，保留在途保护",
                now=now,
            )
            current = self._load(command.id)
            return TrainingDispatchResult(TrainingDispatchOutcome.BOUND, current)
        if status is TrainingBaseStatus.FAILED:
            return self._terminate_base_failure(
                command, base_job_id=base_job_id, status=status, now=now
            )
        if status is not TrainingBaseStatus.COMPLETED:
            # 排队/运行/未知都保持在途保护；未知绝不映射为完成。
            return TrainingDispatchResult(TrainingDispatchOutcome.BOUND, command)
        if command.result_status is TrainingResultStatus.REGISTERED:
            self._complete(command_id=command.id, base_job_id=base_job_id, now=now)
            return TrainingDispatchResult(TrainingDispatchOutcome.BOUND, command)
        if not self._adapter.result_registration_available:
            # 结果读取未配置：基座已完成但结果不可读，只保留诊断，不伪装成功。
            self._record_result_failure(
                command_id=command.id,
                base_job_id=base_job_id,
                code="RESULT_VOLUME_UNCONFIGURED",
                detail="训练结果读取尚未配置，结果不可读，保留在途保护",
                now=now,
            )
            return TrainingDispatchResult(TrainingDispatchOutcome.BOUND, command)
        return self._register_result(command, base_job_id=base_job_id, now=now)

    def _register_result(
        self,
        command: TrainingCommand,
        *,
        base_job_id: str,
        now: datetime,
    ) -> TrainingDispatchResult:
        """核实真实配置与非空结果后登记；不可读只记可恢复诊断。"""
        candidate = uuid4().hex
        config_key, checkpoint_key = self._adapter.result_object_keys(
            command_id=command.id, candidate=candidate
        )
        try:
            registered = self._adapter.copy_result(
                command=command,
                base_job_id=base_job_id,
                storage=self._storage_factory(),
                config_object_key=config_key,
                checkpoint_object_key=checkpoint_key,
            )
        except (
            TrainingResultUnavailableError,
            TrainingResultInvalidError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            self._discard_unregistered_candidates(command.id, (config_key, checkpoint_key))
            self._record_result_failure(
                command_id=command.id,
                base_job_id=base_job_id,
                code="RESULT_UNREADABLE",
                detail=str(error),
                now=now,
            )
            return TrainingDispatchResult(
                TrainingDispatchOutcome.BOUND, command, "RESULT_UNREADABLE", str(error)
            )
        except (ObjectStorageUnavailableError, OSError) as error:
            self._discard_unregistered_candidates(command.id, (config_key, checkpoint_key))
            _logger.warning(
                "dataset.training_dispatch.result_copy_failed",
                command_id=str(command.id),
                error_type=type(error).__name__,
            )
            self._record_result_failure(
                command_id=command.id,
                base_job_id=base_job_id,
                code="RESULT_STORAGE_UNAVAILABLE",
                detail="训练结果写入存储暂时失败，保留在途保护",
                now=now,
            )
            return TrainingDispatchResult(
                TrainingDispatchOutcome.BOUND,
                command,
                "RESULT_STORAGE_UNAVAILABLE",
                "训练结果写入存储暂时失败，保留在途保护",
            )
        try:
            self._store_result(
                command_id=command.id,
                base_job_id=base_job_id,
                config_object_key=registered.config_object_key,
                checkpoint_object_key=registered.checkpoint_object_key,
                config_size=registered.config_size,
                config_sha256=registered.config_sha256,
                checkpoint_size=registered.checkpoint_size,
                checkpoint_sha256=registered.checkpoint_sha256,
                now=now,
            )
        except Exception as error:
            # 提交可能已经成功：重读精确候选绑定，不因异常删除已发布对象。
            _logger.warning(
                "dataset.training_dispatch.result_commit_unknown",
                command_id=str(command.id),
                error_type=type(error).__name__,
            )
            self._record_result_failure(
                command_id=command.id,
                base_job_id=base_job_id,
                code="RESULT_REGISTRATION_UNVERIFIED",
                detail="训练结果登记提交未确认，保留原任务查询",
                now=now,
            )
        self._discard_unregistered_candidates(command.id, (config_key, checkpoint_key))
        current = self._load(command.id)
        if current is not None and current.result_status is TrainingResultStatus.REGISTERED:
            _logger.info(
                "dataset.training_dispatch.result_registered",
                command_id=str(command.id),
                base_job_id=base_job_id,
                result="registered",
            )
        return TrainingDispatchResult(TrainingDispatchOutcome.BOUND, current)

    def _terminate_base_failure(
        self,
        command: TrainingCommand,
        *,
        base_job_id: str,
        status: TrainingBaseStatus,
        now: datetime,
    ) -> TrainingDispatchResult:
        code = (
            "BASE_TRAINING_FAILED"
            if status is TrainingBaseStatus.FAILED
            else "BASE_TRAINING_CANCELLED"
        )
        detail = "基座训练已失败" if status is TrainingBaseStatus.FAILED else "基座训练已取消"
        with self._factory() as session:
            PostgresDatasetRepository(session).fail_training_command(
                command_id=command.id,
                base_job_id=base_job_id,
                failure_code=code,
                failure_detail=detail,
                now=now,
            )
            self._commit(session, command.id)
        current = self._load(command.id)
        if current is None:
            return TrainingDispatchResult(TrainingDispatchOutcome.NOT_FOUND, None)
        return self._failed(current)

    def _complete(self, *, command_id: UUID, base_job_id: str, now: datetime) -> None:
        with self._factory() as session:
            PostgresDatasetRepository(session).complete_training_command(
                command_id=command_id, base_job_id=base_job_id, now=now
            )
            self._commit(session, command_id)

    def _store_result(
        self,
        *,
        command_id: UUID,
        base_job_id: str,
        config_object_key: str,
        checkpoint_object_key: str,
        config_size: int,
        config_sha256: str,
        checkpoint_size: int,
        checkpoint_sha256: str,
        now: datetime,
    ) -> bool:
        with self._factory() as session:
            registered = PostgresDatasetRepository(session).register_training_result(
                command_id=command_id,
                base_job_id=base_job_id,
                config_object_key=config_object_key,
                config_sha256=config_sha256,
                config_size=config_size,
                checkpoint_object_key=checkpoint_object_key,
                checkpoint_sha256=checkpoint_sha256,
                checkpoint_size=checkpoint_size,
                now=now,
            )
            if registered:
                PostgresDatasetRepository(session).complete_training_command(
                    command_id=command_id, base_job_id=base_job_id, now=now
                )
            if not self._commit(session, command_id):
                return False
        return registered

    def _commit(self, session: Session, command_id: UUID) -> bool:
        if self._finish_job is None or self._commit_transaction is None:
            session.commit()
            return True
        command = PostgresDatasetRepository(session).training_command_by_id(command_id)
        if command is not None and command.status in _JOB_TERMINAL_OUTCOMES:
            result = TrainingJobResult(_JOB_TERMINAL_OUTCOMES[command.status], command.failure_code)
            if not self._finish_job(session, result):
                session.rollback()
                self._job_lease_lost = True
                return False
            committed = self._commit_transaction(session)
            self._job_finished = committed
            self._job_lease_lost = not committed
            return committed
        committed = self._commit_transaction(session)
        self._job_lease_lost = not committed
        return committed

    def _discard_unregistered_candidates(self, command_id: UUID, keys: tuple[str, str]) -> None:
        try:
            current = self._load(command_id)
            if current is None:
                return
            registered = {current.result_config_object_key, current.result_checkpoint_object_key}
            storage = self._storage_factory()
            for key in keys:
                if key not in registered:
                    storage.delete(object_key=key)
        except Exception as error:
            # 数据库不可达时无法证明未发布，保留候选而不是误删成功结果。
            _logger.warning(
                "dataset.training_dispatch.candidate_cleanup_unverified",
                command_id=str(command_id),
                error_type=type(error).__name__,
            )

    def _record_result_failure(
        self,
        *,
        command_id: UUID,
        base_job_id: str,
        code: str,
        detail: str,
        now: datetime,
    ) -> None:
        try:
            with self._factory() as session:
                PostgresDatasetRepository(session).record_training_result_failure(
                    command_id=command_id,
                    base_job_id=base_job_id,
                    failure_code=_truncate(code, FAILURE_CODE_LENGTH),
                    failure_detail=_truncate(detail, FAILURE_DETAIL_LENGTH),
                    now=now,
                )
                session.commit()
        except Exception as error:
            _logger.error(
                "dataset.training_dispatch.result_failure_record_failed",
                command_id=str(command_id),
                error_type=type(error).__name__,
            )

    def _record_observation_failure(
        self,
        *,
        command_id: UUID,
        base_job_id: str,
        code: str,
        detail: str,
        now: datetime,
    ) -> None:
        try:
            with self._factory() as session:
                PostgresDatasetRepository(session).record_training_observation_failure(
                    command_id=command_id,
                    base_job_id=base_job_id,
                    failure_code=_truncate(code, FAILURE_CODE_LENGTH),
                    failure_detail=_truncate(detail, FAILURE_DETAIL_LENGTH),
                    now=now,
                )
                session.commit()
        except Exception as error:
            _logger.error(
                "dataset.training_dispatch.observation_failure_record_failed",
                command_id=str(command_id),
                error_type=type(error).__name__,
            )

    def _begin(
        self,
        *,
        command_id: UUID,
        baseline_job_ids: Sequence[str],
        dispatch_facts: dict[str, Any],
        now: datetime,
    ) -> TrainingCommand | None:
        with self._factory() as session:
            updated = PostgresDatasetRepository(session).begin_training_dispatch(
                command_id=command_id,
                backend_target=self._adapter.backend_target,
                baseline_job_ids=baseline_job_ids,
                dispatch_facts=dispatch_facts,
                now=now,
            )
            session.commit()
        return updated

    def _bind(self, *, command_id: UUID, base_job_id: str, now: datetime) -> TrainingDispatchResult:
        try:
            with self._factory() as session:
                PostgresDatasetRepository(session).bind_training_base_job(
                    command_id=command_id, base_job_id=base_job_id, now=now
                )
                session.commit()
        except Exception as error:
            # 绑定提交不确定时保留 result_unknown：绝不重发 start，由后续投递重新唯一匹配。
            _logger.error(
                "dataset.training_dispatch.bind_unverified",
                command_id=str(command_id),
                base_job_id=base_job_id,
                error_type=type(error).__name__,
            )
            return self._reload_result(
                command_id,
                now=now,
                fallback_code="BIND_UNVERIFIED",
                fallback_detail="基座任务身份绑定未确认，保留结果待核实",
            )
        current = self._load(command_id)
        if current is None:
            return TrainingDispatchResult(TrainingDispatchOutcome.NOT_FOUND, None)
        if current.status is TrainingCommandStatus.BOUND:
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
    ) -> TrainingDispatchResult:
        """记录未知诊断但保持 ``result_unknown`` 与在途保护，绝不释放或重发。"""
        try:
            with self._factory() as session:
                PostgresDatasetRepository(session).record_training_failure(
                    command_id=command_id,
                    expected_status=TrainingCommandStatus.RESULT_UNKNOWN,
                    failure_code=_truncate(code, FAILURE_CODE_LENGTH),
                    failure_detail=_truncate(detail, FAILURE_DETAIL_LENGTH),
                    now=now,
                )
                session.commit()
        except Exception as error:
            _logger.error(
                "dataset.training_dispatch.unknown_record_failed",
                command_id=str(command_id),
                error_type=type(error).__name__,
            )
            return self._reload_result(
                command_id, now=now, fallback_code=code, fallback_detail=detail
            )
        current = self._load(command_id)
        if current is None:
            return TrainingDispatchResult(TrainingDispatchOutcome.NOT_FOUND, None)
        if current.status is TrainingCommandStatus.BOUND:
            return self._observe(current, now=now)
        if current.status is TrainingCommandStatus.FAILED:
            return self._failed(current)
        _logger.warning(
            "dataset.training_dispatch.result_unknown",
            command_id=str(current.id),
            dataset_id=str(current.dataset_id),
            service=current.service.value,
            failure_code=current.failure_code,
            result="result_unknown",
        )
        return TrainingDispatchResult(
            TrainingDispatchOutcome.RESULT_UNKNOWN,
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
        owned: AllocatedWorkcopies | None = None,
    ) -> TrainingDispatchResult:
        """意图落库前的确定失败可以终止命令并释放保护；意图之后绝不走这里。"""
        try:
            with self._factory() as session:
                PostgresDatasetRepository(session).record_training_failure(
                    command_id=command_id,
                    expected_status=TrainingCommandStatus.PENDING,
                    failure_code=_truncate(code, FAILURE_CODE_LENGTH),
                    failure_detail=_truncate(detail, FAILURE_DETAIL_LENGTH),
                    now=now,
                )
                self._commit(session, command_id)
        except Exception as error:
            _logger.error(
                "dataset.training_dispatch.failure_record_failed",
                command_id=str(command_id),
                error_type=type(error).__name__,
            )
            return self._reload_result(
                command_id, now=now, fallback_code=code, fallback_detail=detail, owned=owned
            )
        current = self._load(command_id)
        if current is None:
            return TrainingDispatchResult(TrainingDispatchOutcome.NOT_FOUND, None)
        if current.status is TrainingCommandStatus.FAILED:
            # 只在权威状态确证 FAILED 后清理本次新建的自身工作副本；BLOCKED/UNKNOWN/BOUND 保留。
            if owned is not None:
                self._discard_workcopies(command_id=command_id, owned=owned)
            _logger.warning(
                "dataset.training_dispatch.failed",
                command_id=str(current.id),
                dataset_id=str(current.dataset_id),
                service=current.service.value,
                failure_code=current.failure_code,
                result="failed",
            )
            return self._failed(current)
        if current.status is TrainingCommandStatus.CANCELLED:
            # 迟到失败不能覆盖先到的取消：只丢弃本次新建的自身副本，保留首次取消事实。
            return self._cancelled(current, owned=owned)
        if current.status is TrainingCommandStatus.BOUND:
            return self._observe(current, now=now)
        if current.status is TrainingCommandStatus.RESULT_UNKNOWN:
            return self._recover(current, now=now)
        return TrainingDispatchResult(
            TrainingDispatchOutcome.BLOCKED,
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
        owned: AllocatedWorkcopies | None = None,
    ) -> TrainingDispatchResult:
        current = self._load(command_id)
        if current is None:
            return TrainingDispatchResult(TrainingDispatchOutcome.NOT_FOUND, None)
        if current.status is TrainingCommandStatus.CANCELLED:
            # 迟到失败不能覆盖先到的取消：只丢弃本次新建的自身副本，保留首次取消事实。
            return self._cancelled(current, owned=owned)
        if current.status is TrainingCommandStatus.BOUND:
            return self._observe(current, now=now)
        if current.status is TrainingCommandStatus.FAILED:
            # 异常本身不授权删除；权威重读确证 FAILED 才清理，且只处理调用方提供的本地路径。
            if owned is not None:
                self._discard_workcopies(command_id=command_id, owned=owned)
            return self._failed(current)
        if current.status is TrainingCommandStatus.RESULT_UNKNOWN:
            return TrainingDispatchResult(
                TrainingDispatchOutcome.RESULT_UNKNOWN,
                current,
                fallback_code,
                fallback_detail,
            )
        return TrainingDispatchResult(
            TrainingDispatchOutcome.BLOCKED,
            current,
            fallback_code,
            fallback_detail,
        )

    def _blocked_on_preparation(
        self, command: TrainingCommand, *, now: datetime, owned: AllocatedWorkcopies
    ) -> TrainingDispatchResult:
        """准备目录冲突可能是并发准备而非确定失败：保留 pending，不误终止获胜派发。"""
        current = self._load(command.id)
        if current is None:
            return TrainingDispatchResult(TrainingDispatchOutcome.NOT_FOUND, None)
        if current.status is TrainingCommandStatus.PENDING:
            return TrainingDispatchResult(
                TrainingDispatchOutcome.BLOCKED,
                current,
                "DISPATCH_PREPARATION_IN_FLIGHT",
                "同一命令的准备已在其它执行者进行或存在未清理的工作副本",
            )
        if current.status is TrainingCommandStatus.CANCELLED:
            # 准备期冲突在取消之后到达：返回首次取消事实并只丢弃本次新建的自身副本。
            return self._cancelled(current, owned=owned)
        if current.status is TrainingCommandStatus.BOUND:
            return self._observe(current, now=now)
        if current.status is TrainingCommandStatus.RESULT_UNKNOWN:
            return self._recover(current, now=now)
        return self._failed(current)

    def _target_mismatch(self, command: TrainingCommand) -> TrainingDispatchResult:
        _logger.warning(
            "dataset.training_dispatch.backend_target_mismatch",
            command_id=str(command.id),
            failure_code="BACKEND_TARGET_MISMATCH",
        )
        return TrainingDispatchResult(
            TrainingDispatchOutcome.BLOCKED,
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
            PostgresDatasetRepository(session).observe_training_base_job(
                command_id=command_id,
                base_job_id=base_job_id,
                observation=observation,
                now=now,
            )
            session.commit()

    def _load(self, command_id: UUID) -> TrainingCommand | None:
        with self._factory() as session:
            return PostgresDatasetRepository(session).training_command_by_id(command_id)

    def _target_matches(self, command: TrainingCommand) -> bool:
        return command.backend_target == self._adapter.backend_target

    def _completed(self, command: TrainingCommand) -> TrainingDispatchResult:
        """发布终态：结果已登记，重复投递不再观察或重发。"""
        return TrainingDispatchResult(TrainingDispatchOutcome.BOUND, command)

    def _failed(self, command: TrainingCommand) -> TrainingDispatchResult:
        return TrainingDispatchResult(
            TrainingDispatchOutcome.FAILED,
            command,
            command.failure_code,
            command.failure_detail,
        )

    def _cancelled(
        self, command: TrainingCommand, *, owned: AllocatedWorkcopies | None = None
    ) -> TrainingDispatchResult:
        """已取消是终态：只丢弃本次调用新建的自身副本，保留首次取消人与时刻等其它事实。"""
        if owned is not None:
            self._discard_workcopies(command_id=command.id, owned=owned)
        return TrainingDispatchResult(TrainingDispatchOutcome.CANCELLED, command)

    def _discard_workcopies(self, *, command_id: UUID, owned: AllocatedWorkcopies) -> None:
        """命令确证 FAILED 或已取消后只删除本次调用新建的工作副本。

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
                    "dataset.training_dispatch.workcopy_cleanup_failed",
                    command_id=str(command_id),
                    path=str(path),
                )


def _base_status(value: object) -> TrainingBaseStatus | None:
    """把基座原状态字符串映射为已知枚举；未知/缺失返回 None，绝不映射为完成。"""
    if not isinstance(value, str):
        return None
    try:
        return TrainingBaseStatus(value)
    except ValueError:
        return None


def _cancel_response_fact(response: Mapping[str, Any]) -> dict[str, Any]:
    """只保存基座取消响应的可核对事实，不保存任意文本或内部路径。"""
    return dict(response)


def _safe_observation(observation: Mapping[str, Any]) -> dict[str, Any]:
    # 只持久化基座公开状态/进度；不存日志、路径、连接串或客户内容。
    safe: dict[str, Any] = {}
    for key in (
        "status",
        "progress",
        "current_step",
        "total_steps",
        "loss",
        "current_epoch",
        "total_epochs",
        "epoch",
        "max_epochs",
    ):
        value = observation.get(key)
        if isinstance(value, (int, float)) or (key == "status" and isinstance(value, str)):
            safe[key] = value[:128] if isinstance(value, str) else value
    return safe


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"
