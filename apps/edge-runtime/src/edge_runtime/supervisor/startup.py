"""supervisor 的启动与重启编排。

启动时先由 local_state 恢复未结实例, 再由 supervisor 以 RUN_INTERRUPTED 结案。
这样 local_state 只保存领域数据, 不需要反向依赖编排它的 supervisor。
"""

from __future__ import annotations

from collections.abc import Callable
from time import monotonic

from edge_runtime.judgment.evidence import EvidenceMargins
from edge_runtime.judgment.model import HostInstant, RuntimeParameters, Template
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
    state = store.resume(template, parameters)
    supervisor = StationSupervisor(
        state=state,
        store=store,
        margins=margins,
        clock=clock,
        initial_report_provenance=store.resume_report_provenance(),
    )
    interruption_at: HostInstant | None = None
    if state.instance is not None:
        current = HostInstant(clock())
        interruption_at = (
            state.instance.last_observation_at
            if current.seconds < state.instance.last_observation_at.seconds
            else current
        )
    supervisor.interrupt(at=interruption_at)
    return supervisor


__all__ = ["resume_station"]
