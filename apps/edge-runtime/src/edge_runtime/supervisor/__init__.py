"""工位驱动接口: 归一化输入、持有计时器并原子提交判定反应。"""

from edge_runtime.supervisor.inputs import (
    ActionRecognized,
    ExternalSignal,
    StreamHealthObserved,
    SupervisorInput,
    TimeAlignment,
    Validity,
    ValidityChanged,
)
from edge_runtime.supervisor.station import Reaction, StationSupervisor

__all__ = [
    "ActionRecognized",
    "ExternalSignal",
    "Reaction",
    "StationSupervisor",
    "StreamHealthObserved",
    "SupervisorInput",
    "TimeAlignment",
    "Validity",
    "ValidityChanged",
]
