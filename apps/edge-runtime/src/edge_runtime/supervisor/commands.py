"""supervisor 对判定结果的效果编排。

效果数据定义在 judgment 的数据层, 本模块只负责把一个判定扩展为证据窗口和效果序列。
执行仍由各自的 owner 负责, 因而持久化模块不需要依赖 supervisor。
"""

from __future__ import annotations

from dataclasses import dataclass

from edge_runtime.judgment.effects import (
    ClipEvidence,
    CloseInstance,
    Command,
    LatchViolation,
    RecordDecision,
)
from edge_runtime.judgment.model import Decision, EvidenceSpan, HostInstant, Lifecycle


@dataclass(frozen=True, slots=True)
class EvidenceMargins:
    """判定锚点前后的证据余量, 单位为秒。"""

    leading: float
    trailing: float

    def __post_init__(self) -> None:
        if self.leading < 0 or self.trailing < 0:
            raise ValueError(
                f"evidence margins cannot be negative, got {self.leading}/{self.trailing}"
            )


def commands_for(decision: Decision, *, margins: EvidenceMargins) -> tuple[Command, ...]:
    """把一个判定转换成需要执行的效果序列。"""
    commands: list[Command] = [RecordDecision(decision=decision)]
    commands.extend(
        LatchViolation(instance_id=decision.instance_id, violation=violation)
        for violation in decision.violations
    )
    commands.extend(_clips(decision, margins))
    if decision.lifecycle is not Lifecycle.STAYS_OPEN:
        commands.append(
            CloseInstance(instance_id=decision.instance_id, lifecycle=decision.lifecycle)
        )
    return tuple(commands)


def _clips(decision: Decision, margins: EvidenceMargins) -> list[ClipEvidence]:
    """按锚点合并必需跨度, 再加宽配置余量。"""
    required: dict[HostInstant, EvidenceSpan] = {}
    for span in (decision.evidence, *(violation.evidence for violation in decision.violations)):
        held = required.get(span.anchor)
        required[span.anchor] = span if held is None else _union(held, span)
    return [
        ClipEvidence(
            instance_id=decision.instance_id,
            anchor=anchor,
            start=HostInstant(span.required_from.seconds - margins.leading),
            end=HostInstant(span.required_to.seconds + margins.trailing),
        )
        for anchor, span in required.items()
    ]


def _union(held: EvidenceSpan, arriving: EvidenceSpan) -> EvidenceSpan:
    """返回覆盖两段必需跨度的最小区间。"""
    return EvidenceSpan(
        anchor=held.anchor,
        required_from=min(held.required_from, arriving.required_from),
        required_to=max(held.required_to, arriving.required_to),
    )


__all__ = [
    "ClipEvidence",
    "CloseInstance",
    "Command",
    "EvidenceMargins",
    "LatchViolation",
    "RecordDecision",
    "commands_for",
]
