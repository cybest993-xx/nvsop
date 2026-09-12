"""supervisor 的启动与重启编排。

启动时先由 local_state 恢复未结实例, 再由 supervisor 以 RUN_INTERRUPTED 结案。
这样 local_state 只保存领域数据, 不需要反向依赖编排它的 supervisor。
"""

from __future__ import annotations

from collections.abc import Callable
from time import monotonic

from edge_runtime.judgment.evidence import EvidenceMargins
from edge_runtime.judgment.model import RuntimeParameters, Template
from edge_runtime.local_state.store import StationStore
from edge_runtime.supervisor.station import StationSupervisor


def resume_station(
    store: StationStore,
    *,
    template: Template,
    parameters: RuntimeParameters,
    margins: EvidenceMargins,
    clock: Callable[[], float] = monotonic,
) -> StationSupervisor:
    """恢复工位并结案启动前遗留的实例。"""
    supervisor = StationSupervisor(
        state=store.resume(template, parameters), store=store, margins=margins, clock=clock
    )
    supervisor.interrupt()
    return supervisor


__all__ = ["resume_station"]
