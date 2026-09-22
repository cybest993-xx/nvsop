"""派发一次输出点写入:能力门、账本和结果记录。

监督器的处置派发到达继电器前必须经过能力校验、幂等账本和无条件诊断事件。适配器只报告设备结果,
不在此处重新判定;重试策略集中写在 ``_replayable``。

本模块只依赖标准库和既有契约。
"""

from __future__ import annotations

import logging
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
    WriteOutcome,
    WriteRefusal,
    Written,
)
from edge_runtime.judgment.model import HostInstant

UNVERIFIED_DETAIL = "连接器能力声明未验证。不驱动物理执行器"
TOO_SLOW_DETAIL = "连接器最大投递延迟超出安全输出预算。不驱动物理执行器"
PERSISTENT_UNKNOWN_DETAIL = "持久化处置结果未知。不会自动重复驱动物理执行器"

_logger = logging.getLogger("edge_runtime")


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

    station_id: str = "default"
    """持久处置账本使用的 local-state 作用域。"""

    connector_id: str = "unknown"
    """拥有目标点位的已配置连接器。"""

    attempt_at: HostInstant | None = None
    """预留持久尝试时使用的主机单调时刻。"""

    lease_seconds: float | None = None
    """持久账本必需的租约时长;物理路径不设置默认 cadence。"""


@dataclass(frozen=True, slots=True)
class WriteAttempted:
    """The diagnostic event for one attempt. Q37: 记操作人与目标点位.

    A stable event name and stable fields, per Q37's decision not to build an audit table:
    diagnostics are ordinary infrastructure, and this is one event on it.
    """

    point: OutputPoint
    state: PointState
    key: str
    actor: str
    outcome: WriteOutcome
    replayed: bool
    """表示结果来自账本而不是设备。

    该字段明确区分“因重复而抑制处置”和“从未派发处置”,便于操作员诊断。
    """


class WriteLedger(Protocol):
    """显式描述一次写入从意图、占用到结果落账的完整契约。

    生产实现由组合根绑定到 local_state 的 SQLite 账本,必须跨进程重启存活。
    """

    def prepare(self, request: WriteRequest, /) -> None: ...

    def outcome_for(self, key: str, /) -> WriteOutcome | None: ...

    def claim(self, request: WriteRequest, /) -> WriteOutcome | None: ...

    def record(self, key: str, outcome: WriteOutcome, /) -> None: ...


class InMemoryWriteLedger:
    """只覆盖一个进程生命周期的测试账本。

    它不伪装成跨重启幂等;跨重启幂等必须使用 local_state 拥有的 SQLite 表。
    """

    def __init__(self) -> None:
        self._outcomes: dict[str, WriteOutcome] = {}

    def prepare(self, request: WriteRequest, /) -> None:
        return None

    def outcome_for(self, key: str, /) -> WriteOutcome | None:
        return self._outcomes.get(key)

    def claim(self, request: WriteRequest, /) -> WriteOutcome | None:
        return None

    def record(self, key: str, outcome: WriteOutcome, /) -> None:
        self._outcomes[key] = outcome


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
        self._ledger.prepare(request)
        held = self._ledger.outcome_for(request.key)
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
            # 没有请求发往设备,因此不占用幂等键,修正能力后仍可重试。
            return self._note(request, refusal, replayed=False)

        held = self._ledger.claim(request)
        if held is not None:
            return self._note(request, held, replayed=True)

        try:
            outcome = self._connector.write(request.point, request.state, timeout=request.timeout)
        except Exception:
            # claim 已代表物理请求可能离开进程; 普通异常不能证明设备未执行。
            outcome = Failed(detail=PERSISTENT_UNKNOWN_DETAIL)
            _logger.exception(
                "connector write adapter raised key=%s actor=%s connector=%s point=%s state=%s",
                request.key,
                request.actor,
                request.connector_id,
                request.point.label,
                request.state.value,
            )
            self._ledger.record(request.key, outcome)
            return self._note(request, outcome, replayed=False)
        self._ledger.record(request.key, outcome)
        return self._note(request, outcome, replayed=False)

    def _note(
        self, request: WriteRequest, outcome: WriteOutcome, *, replayed: bool
    ) -> WriteOutcome:
        """Emit the diagnostic event and hand the outcome back.

        Every path goes through here, including the refused and the suppressed ones, so Q37's
        "必产生事件" holds by construction rather than by each branch remembering to.
        """
        self._diagnostics(
            WriteAttempted(
                point=request.point,
                state=request.state,
                key=request.key,
                actor=request.actor,
                outcome=outcome,
                replayed=replayed,
            )
        )
        return outcome


def _replayable(held: WriteOutcome) -> bool:
    """Whether a recorded outcome leaves the action still worth attempting.

    An accepted write and an expired lease both close the key. The expired-lease result is
    deliberately terminal because the physical outcome is unknowable; silently replaying it
    could drive a physical actuator twice. Other retryable outcomes mean "the state we asked
    for may not be in force", and for a 停线联锁 that is the dangerous direction to guess in:

    - `Refused` and `Failed`: nothing physical happened, so suppressing the retry would leave
      the interlock unasserted on the strength of a failure.
    - `TimedOut`: the physical outcome is unknown. A point write assigns a level rather than
      emitting a pulse, so repeating it converges on the state that was asked for — which is
      why "may have already happened" does not argue for holding back here.

    A pulsed output, if one is ever needed, cannot use this rule and must not be added behind
    it silently: it would need the device's own idempotency, not ours.
    """
    return not isinstance(held, Written) and not (
        isinstance(held, Failed) and held.detail == PERSISTENT_UNKNOWN_DETAIL
    )
