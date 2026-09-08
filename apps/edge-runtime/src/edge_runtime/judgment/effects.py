"""判定结果产生的副作用数据。

这些类型只描述 supervisor 交给持久化、处置和证据模块的效果, 不执行任何效果。
它们放在 judgment 的数据层, 使 local_state 只依赖领域数据, 不反向依赖 supervisor。
"""

from __future__ import annotations

from dataclasses import dataclass

from edge_runtime.judgment.model import Decision, HostInstant, Lifecycle, Violation


@dataclass(frozen=True, slots=True)
class RecordDecision:
    """把判定写入推理机本地状态。"""

    decision: Decision


@dataclass(frozen=True, slots=True)
class LatchViolation:
    """锁存一条已经确认的违规。"""

    instance_id: int
    violation: Violation


@dataclass(frozen=True, slots=True)
class ClipEvidence:
    """按判定声明的锚点和窗口提取证据。"""

    instance_id: int
    anchor: HostInstant
    start: HostInstant
    end: HostInstant


@dataclass(frozen=True, slots=True)
class CloseInstance:
    """按判定给出的生命周期结案。"""

    instance_id: int
    lifecycle: Lifecycle


Command = RecordDecision | LatchViolation | ClipEvidence | CloseInstance
"""需要由推理机执行的效果数据。"""


__all__ = [
    "ClipEvidence",
    "CloseInstance",
    "Command",
    "LatchViolation",
    "RecordDecision",
]
