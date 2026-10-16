"""推理机对正常交权停机命令的本机执行接缝(#214).

顺序即安全不变量: 先把停写 fence 落到本机 SQLite(崩溃/重启不得复活物理写入), 再等待
在途判定与写入收敛并关闭工位资源(SSE reader 有界 join, 卡死显式失败). 全部成功才回报
``confirmed``; 任一步失败回报 ``failed`` 并携带原因, 不吞异常伪造释放. 旧租约身份与命令
冻结身份不符时不动工位, 也不写 fence——陈旧确认不得影响后继交权.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from nvsop_contracts import (
    ExecutionHandoverCommand,
    ExecutionHandoverOutcome,
    ExecutionHandoverResult,
)

from edge_runtime.local_state.execution import LocalExecutionLeaseStore

GRANT_STALE = "COMMAND_GRANT_STALE"
"""回报失败原因: 命令冻结的 grant 不再是本机当前租约, 不停写也不停机. """

STOP_FAILED = "COMMAND_STOP_FAILED"
"""回报失败原因: fence 已写入但工位资源未在限时内安全关闭; 中心按失败结案, 本机保持禁写. """

TARGET_NOT_FOUND = "COMMAND_TARGET_NOT_FOUND"
"""回报失败原因: 本机没有运行该工位; 不停写也不停机. """

_MAX_DETAIL = 255


@dataclass(frozen=True, slots=True)
class StationStop:
    """一个可停机工位的真实资源句柄, 由组合根从活动工位对象投影."""

    station_id: str
    grant_id: str | None
    close: Callable[[], None]
    """等待在途判定/写入并释放全部资源; 抛出异常表示未安全关闭, 不得伪确认. """


class ExecutionHandoverStopper(Protocol):
    """按命令目标返回本机可停机工位的接缝."""

    def resolve(self, station_id: str) -> StationStop | None: ...


class _ClosableStation(Protocol):
    """组合根活动工位对象只需向本接缝暴露安全关闭。"""

    def stop_for_handover(self) -> None: ...


class AutonomousStationStopper:
    """从活动工位映射构造停机接缝; grant 身份读本机唯一租约事实."""

    def __init__(
        self,
        stations: Mapping[str, _ClosableStation],
        leases: LocalExecutionLeaseStore,
    ) -> None:
        self._stations = stations
        self._leases = leases

    def resolve(self, station_id: str) -> StationStop | None:
        station = self._stations.get(station_id)
        if station is None:
            return None
        fact = self._leases.fact(station_id)
        return StationStop(
            station_id=station_id,
            grant_id=None if fact is None else fact.grant_id,
            close=station.stop_for_handover,
        )


class ExecutionHandoverCommandRunner:
    """执行一条正常交权停机命令并产出严格结果; 不发新机 grant, 不恢复已停旧机."""

    def __init__(
        self,
        *,
        stopper: ExecutionHandoverStopper,
        leases: LocalExecutionLeaseStore,
        clock: Callable[[], float],
    ) -> None:
        self._stopper = stopper
        self._leases = leases
        self._clock = clock

    def execute(self, command: ExecutionHandoverCommand) -> ExecutionHandoverResult:
        stop = self._stopper.resolve(command.station_id)
        if stop is None:
            return ExecutionHandoverResult(
                handover_id=command.handover_id,
                grant_id=command.grant_id,
                outcome=ExecutionHandoverOutcome.FAILED,
                failure_code=TARGET_NOT_FOUND,
                detail=f"本机没有运行工位 {command.station_id}",
            )
        if stop.grant_id != command.grant_id:
            return ExecutionHandoverResult(
                handover_id=command.handover_id,
                grant_id=command.grant_id,
                outcome=ExecutionHandoverOutcome.FAILED,
                failure_code=GRANT_STALE,
                detail="本机当前租约与命令冻结身份不一致",
            )
        # 先持久禁写再停机: 停机过程中崩溃, 重启后门禁仍拒绝写入 (edge-autonomy.md §5.17).
        try:
            self._leases.stop_writes(
                command.station_id, grant_id=command.grant_id, stopped_at=self._clock()
            )
        except ValueError as error:
            return ExecutionHandoverResult(
                handover_id=command.handover_id,
                grant_id=command.grant_id,
                outcome=ExecutionHandoverOutcome.FAILED,
                failure_code=GRANT_STALE,
                detail=str(error)[:_MAX_DETAIL],
            )
        try:
            stop.close()
        except Exception as error:
            # 不吞异常伪释放: fence 已持久禁写, 旧机保持停写, 中心按失败结案.
            return ExecutionHandoverResult(
                handover_id=command.handover_id,
                grant_id=command.grant_id,
                outcome=ExecutionHandoverOutcome.FAILED,
                failure_code=STOP_FAILED,
                detail=f"station resources did not close safely: {type(error).__name__}",
            )
        return ExecutionHandoverResult(
            handover_id=command.handover_id,
            grant_id=command.grant_id,
            outcome=ExecutionHandoverOutcome.CONFIRMED,
        )


__all__ = [
    "GRANT_STALE",
    "STOP_FAILED",
    "TARGET_NOT_FOUND",
    "AutonomousStationStopper",
    "ExecutionHandoverCommandRunner",
    "ExecutionHandoverStopper",
]
