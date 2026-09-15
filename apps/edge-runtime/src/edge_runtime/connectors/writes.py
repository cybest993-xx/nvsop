"""派发一次输出点写入:能力门、账本和结果记录。

监督器的处置派发到达继电器前必须经过能力校验、幂等账本和无条件诊断事件。适配器只报告设备结果,
不在此处重新判定;重试策略集中写在 ``_replayable``。

本模块只依赖标准库和既有契约。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from nvsop_contracts import PointRole, Unfitness, unfit_for

from edge_runtime.connectors.port import (
    Connector,
    Failed,
    OutputPoint,
    PointState,
    Refused,
    TimedOut,
    Unknown,
    WriteOutcome,
    WriteRefusal,
    Written,
)
from edge_runtime.judgment.model import HostInstant
from edge_runtime.local_state.disposal import (
    DISPOSAL_RESULT_TIMED_OUT,
    DISPOSAL_RESULT_UNKNOWN,
    DISPOSAL_RESULT_WRITTEN,
    DisposalIntent,
    LocalDisposalLedger,
    StoredDisposalResult,
)

UNVERIFIED_DETAIL = "连接器能力声明未验证。不驱动物理执行器"
TOO_SLOW_DETAIL = "连接器最大投递延迟超出安全输出预算。不驱动物理执行器"
PERSISTENT_UNKNOWN_DETAIL = "持久化处置结果未知。不会自动重复驱动物理执行器"
UNEXPECTED_WRITE_DETAIL = "连接器适配器异常。物理结果未知, 不会自动重复驱动物理执行器"
LEASE_ACTIVE_DETAIL = "同一幂等键已有活动物理尝试租约。不会重复驱动物理执行器"


@dataclass(frozen=True, slots=True)
class WriteRequest:
    """一次请求的点位写入,包含安全执行和追踪所需的全部信息。"""

    point: OutputPoint
    state: PointState
    key: str
    """幂等键由调用方拥有,因为“同一动作”的含义属于调用方;处置重启后重派仍携带同一键。"""

    actor: str
    """请求方身份,写入诊断事件;监督器处置和操作员手动测试都属于 actor。"""

    timeout: float
    """等待设备的时长。物理控制路径不能隐藏默认时间,因此必须由配置传入。"""

    capability_budget: float
    """安全输出允许连接器投递消耗的时长;由已验证绑定传入,无默认值。"""

    station_id: str
    """持久处置账本使用的本地状态工位作用域。"""

    connector_id: str
    """拥有目标点位的已配置连接器。"""

    attempt_at: HostInstant | None = None
    """预留持久尝试时使用的主机单调时刻。"""

    lease_seconds: float | None = None
    """持久账本必需的租约时长;物理路径不设置默认 cadence。"""


WRITE_ATTEMPTED_EVENT = "connector.write.attempted"


@dataclass(frozen=True, slots=True)
class WriteAttempted:
    """一次输出写入的稳定结构化诊断事件。"""

    station_id: str
    connector_id: str
    point: OutputPoint
    state: PointState
    key: str
    actor: str
    outcome: WriteOutcome
    result: str
    result_detail: str | None
    result_reason: str | None
    replayed: bool
    event: str = WRITE_ATTEMPTED_EVENT

    def as_dict(self) -> dict[str, object]:
        """返回可直接交给结构化日志适配器的稳定字段。"""
        return {
            "event": self.event,
            "station_id": self.station_id,
            "connector_id": self.connector_id,
            "point": {"label": self.point.label, "address": self.point.address},
            "state": self.state.value,
            "key": self.key,
            "actor": self.actor,
            "result": self.result,
            "result_detail": self.result_detail,
            "result_reason": self.result_reason,
            "replayed": self.replayed,
        }


class WriteLedger(Protocol):
    """记录已认领的物理写入结果, 防止同一动作执行两次。

    生产实现是 local_state 的 SQLite 账本, 必须跨进程重启存活。能力或拓扑拒绝发生在
    claim 之前, 只产生结构化诊断, 不写入物理尝试结果; timed-out、unknown 和其他适配器结果
    则按结果类型持久化。
    """

    def outcome_for(self, request: WriteRequest, /) -> WriteOutcome | None:
        """按工位作用域和幂等键读取已记录结果。"""
        ...

    def record(self, request: WriteRequest, outcome: WriteOutcome, /) -> None:
        """按同一工位作用域记录一次已认领的物理写入结果。"""
        ...


class InMemoryWriteLedger:
    """只覆盖一个进程生命周期的测试账本。

    它不伪装成跨重启幂等;跨重启幂等必须使用 local_state 拥有的 SQLite 表。
    """

    def __init__(self) -> None:
        self._outcomes: dict[tuple[str, str], WriteOutcome] = {}

    def outcome_for(self, request: WriteRequest, /) -> WriteOutcome | None:
        return self._outcomes.get((request.station_id, request.key))

    def record(self, request: WriteRequest, outcome: WriteOutcome, /) -> None:
        self._outcomes[(request.station_id, request.key)] = outcome


class OutputDispatcher:
    """执行 supervisor 处置派发请求的点位写入。

    每个连接器只有一个 dispatcher;能力声明和设备都属于连接器。账本由外部传入,以便注入持久实现。
    """

    def __init__(
        self,
        *,
        connector: Connector,
        ledger: WriteLedger,
        diagnostics: Callable[[WriteAttempted], None],
    ) -> None:
        self._connector = connector
        self._ledger = ledger
        self._diagnostics = diagnostics

    def write(self, request: WriteRequest) -> WriteOutcome:
        """驱动点位,或返回拒绝原因;结果本身就是回答,不用异常表示物理结果。

        结果采用结构化值,因为调用方需要持久化它;异常越过事务边界会丢失实际尝试记录。
        """
        prepare = getattr(self._ledger, "prepare", None)
        if prepare is not None:
            prepare(request)
        held = self._ledger.outcome_for(request)
        if held is not None and not _replayable(held):
            return self._note(request, held, replayed=True)

        unfitness = unfit_for(
            self._connector.capability,
            role=PointRole.SAFETY_OUTPUT,
            budget=request.capability_budget,
        )
        if unfitness:
            match unfitness[0]:
                case Unfitness.CAPABILITY_UNVERIFIED:
                    refusal = Refused(
                        reason=WriteRefusal.CAPABILITY_UNVERIFIED,
                        detail=UNVERIFIED_DETAIL,
                    )
                case Unfitness.DELIVERY_TOO_SLOW:
                    refusal = Refused(
                        reason=WriteRefusal.DELIVERY_TOO_SLOW,
                        detail=TOO_SLOW_DETAIL,
                    )
                case Unfitness.MAY_DROP_EDGES | Unfitness.NOT_SEQUENCED:
                    raise AssertionError("安全输出规则不读取输入边沿或到达顺序")
            # 这是 claim 之前的能力拒绝: 没有物理尝试结果, local_disposal 只保留身份行,
            # 诊断事件保留本次拒绝;能力修复后同一 key 可以重新评估。
            return self._note(request, refusal, replayed=False)

        claim = getattr(self._ledger, "claim", None)
        if claim is not None:
            held = claim(request)
            if held is not None:
                return self._note(request, held, replayed=True)

        try:
            outcome = self._connector.write(request.point, request.state, timeout=request.timeout)
        except Exception:
            # claim 已经保留物理尝试租约, 适配器异常时无法判断设备是否收到请求。
            # 记录 Unknown 并关闭租约, 避免异常路径既没有诊断又被下一次静默重放。
            outcome = Unknown(detail=UNEXPECTED_WRITE_DETAIL)
        # 适配器在 claim 之后返回的 Refused 属于一次已 claim 的结构化结果, 由账本记录;
        # 这与 claim 之前的 capability/topology refusal 是两个明确阶段。
        self._ledger.record(request, outcome)
        return self._note(request, outcome, replayed=False)

    def _note(
        self, request: WriteRequest, outcome: WriteOutcome, *, replayed: bool
    ) -> WriteOutcome:
        """发出诊断事件并返回结果。

        拒绝和抑制重放路径也统一经过这里,因此 Q37 的“必产生事件”由结构保证,而不是
        依赖每个分支自行记住发送事件。
        """
        self._diagnostics(diagnostic_event(request, outcome, replayed=replayed))
        return outcome


class SQLiteWriteLedger:
    """对 ``local_state.local_disposal`` 的连接器侧转换。

    这是适配器,不是另一份账本;所有身份和结果行都由 ``LocalDisposalLedger`` 拥有并跨重启保存。
    """

    def __init__(self, ledger: LocalDisposalLedger) -> None:
        self._ledger = ledger

    @staticmethod
    def _intent(request: WriteRequest) -> DisposalIntent:
        """从请求重建 local_disposal 的完整身份, 不缓存第二份身份。"""
        return DisposalIntent(
            station_id=request.station_id,
            idempotency_key=request.key,
            connector_id=request.connector_id,
            point_id=request.point.address,
            actor=request.actor,
            requested_state=request.state.value,
        )

    def prepare(self, request: WriteRequest) -> None:
        """让唯一权威账本校验幂等键和目标身份。"""
        self._ledger.ensure_intent(self._intent(request))

    def claim(self, request: WriteRequest) -> WriteOutcome | None:
        if request.attempt_at is None or request.lease_seconds is None:
            raise ValueError("durable writes require attempt_at and lease_seconds")
        claim = self._ledger.claim(
            self._intent(request),
            now=request.attempt_at.seconds,
            lease_seconds=request.lease_seconds,
        )
        if claim.claimed:
            return None
        if claim.result is not None:
            return _outcome_from_storage(claim.result.kind, claim.result.detail, claim.result.at)
        if claim.reason == "lease_active":
            return Refused(reason=WriteRefusal.LEASE_ACTIVE, detail=LEASE_ACTIVE_DETAIL)
        return Failed(detail=claim.reason or "durable disposal attempt was not claimed")

    def outcome_for(self, request: WriteRequest, /) -> WriteOutcome | None:
        result = self._ledger.result_for(request.station_id, request.key)
        if result is None:
            return None
        return _outcome_from_storage(result.kind, result.detail, result.at)

    def record(self, request: WriteRequest, outcome: WriteOutcome, /) -> None:
        self._ledger.record_result(
            self._intent(request),
            result=StoredDisposalResult(
                kind=_outcome_kind(outcome),
                detail=_outcome_detail(outcome),
                at=_outcome_at(outcome),
            ),
        )


def _outcome_kind(outcome: WriteOutcome) -> str:
    if isinstance(outcome, Written):
        return DISPOSAL_RESULT_WRITTEN
    if isinstance(outcome, Refused):
        return f"refused:{outcome.reason.value}"
    if isinstance(outcome, TimedOut):
        return DISPOSAL_RESULT_TIMED_OUT
    if isinstance(outcome, Unknown):
        return DISPOSAL_RESULT_UNKNOWN
    if isinstance(outcome, Failed):
        return "failed"
    raise AssertionError(f"unsupported write outcome: {outcome!r}")


def _outcome_detail(outcome: WriteOutcome) -> str | None:
    if isinstance(outcome, Refused):
        return outcome.detail
    if isinstance(outcome, TimedOut):
        return str(outcome.after)
    if isinstance(outcome, Unknown):
        return outcome.detail
    if isinstance(outcome, Failed):
        return outcome.detail
    return None


def _outcome_at(outcome: WriteOutcome) -> float:
    if isinstance(outcome, Written):
        return outcome.at.seconds
    if isinstance(outcome, TimedOut):
        return outcome.after
    return 0.0


def _outcome_from_storage(kind: str, detail: str | None, at: float) -> WriteOutcome:
    from edge_runtime.judgment.model import HostInstant

    if kind == DISPOSAL_RESULT_WRITTEN:
        return Written(at=HostInstant(at))
    if kind == DISPOSAL_RESULT_TIMED_OUT:
        return TimedOut(after=at)
    if kind == "failed":
        return Failed(detail=detail or "persistent write failure")
    if kind == DISPOSAL_RESULT_UNKNOWN:
        return Unknown(detail=detail or PERSISTENT_UNKNOWN_DETAIL)
    if kind.startswith("refused:"):
        reason = WriteRefusal(kind.removeprefix("refused:"))
        return Refused(reason=reason, detail=detail or "")
    return Unknown(detail=detail or f"unknown persistent write result: {kind}")


def diagnostic_event(
    request: WriteRequest, outcome: WriteOutcome, *, replayed: bool
) -> WriteAttempted:
    """从请求和结构化结果构造稳定的写入诊断事件。"""
    result, detail, reason = _diagnostic_result(outcome)
    return WriteAttempted(
        station_id=request.station_id,
        connector_id=request.connector_id,
        point=request.point,
        state=request.state,
        key=request.key,
        actor=request.actor,
        outcome=outcome,
        result=result,
        result_detail=detail,
        result_reason=reason,
        replayed=replayed,
    )


def _diagnostic_result(outcome: WriteOutcome) -> tuple[str, str | None, str | None]:
    """把写入结果投影为稳定的事件类型、明细和原因。"""
    if isinstance(outcome, Written):
        return "written", None, None
    if isinstance(outcome, Refused):
        return "refused", outcome.detail or None, outcome.reason.value
    if isinstance(outcome, TimedOut):
        return "timed_out", str(outcome.after), None
    if isinstance(outcome, Unknown):
        return "unknown", outcome.detail, None
    if isinstance(outcome, Failed):
        return "failed", outcome.detail, None
    raise AssertionError(f"unsupported write outcome: {outcome!r}")


def _replayable(held: WriteOutcome) -> bool:
    """判断已记录结果是否仍值得尝试。

    成功写入、适配器超时和租约过期都会关闭幂等键。物理结果无法安全重建,静默重放
    可能导致执行器动作两次。只有明确没有进入物理尝试的结果可以重试:

    - ``Refused`` 和 ``Failed``: 没有发生物理写入,抑制重试会让联锁可能保持未生效。
    - ``TimedOut`` 和 ``Unknown``: 物理结果未知, 必须等待人工或更高层策略处理。

    如果以后需要脉冲输出,不能直接复用这里的规则;它需要设备侧幂等能力,不能默默加入。
    """
    return not isinstance(held, (Written, TimedOut, Unknown))
