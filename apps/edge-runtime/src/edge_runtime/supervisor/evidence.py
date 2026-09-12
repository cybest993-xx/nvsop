"""supervisor 用配置余量加宽核心声明的必需证据跨度 (§5.20)。"""

from edge_runtime.judgment.evidence import EvidenceClip, EvidenceMargins
from edge_runtime.judgment.model import Decision, EvidenceSpan, HostInstant


def clips_for(decision: Decision, *, margins: EvidenceMargins) -> tuple[EvidenceClip, ...]:
    """按锚点合并必需跨度, 再加宽配置余量, 不改变判定事实。"""
    required: dict[HostInstant, EvidenceSpan] = {}
    for span in (decision.evidence, *(violation.evidence for violation in decision.violations)):
        held = required.get(span.anchor)
        required[span.anchor] = (
            span
            if held is None
            else EvidenceSpan(
                anchor=held.anchor,
                required_from=min(held.required_from, span.required_from),
                required_to=max(held.required_to, span.required_to),
            )
        )
    return tuple(
        EvidenceClip(
            instance_id=decision.instance_id,
            anchor=anchor,
            start=HostInstant(span.required_from.seconds - margins.leading),
            end=HostInstant(span.required_to.seconds + margins.trailing),
        )
        for anchor, span in required.items()
    )
