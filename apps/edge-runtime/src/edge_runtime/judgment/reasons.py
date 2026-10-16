"""Reason codes: the machine-readable reason a verdict was reached.

The judgment core is the producer of reason codes, so it owns their definition
(judgment-and-boundary.md §5.2). The OpenAPI enumeration and the front end's hint
table are derived from this module rather than maintained beside it, because three
independent copies drift silently.

Adding a value is a compatible change; every client renders an unknown code as the
raw code plus a generic hint (ADR-0003). Deleting a value or changing what one means
is breaking.
"""

from __future__ import annotations

from enum import Enum


class Verdict(Enum):
    """An SOP instance's business conclusion. Three values, never two.

    `INDETERMINATE` says the conclusion is not reliable. It does not say the operator
    failed: telling those apart is what the base cannot do (§2.1), and collapsing them
    is what makes a shift lead confront the wrong person.
    """

    PASS = "pass"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"


class ReasonCode(Enum):
    """Why a verdict was reached. Each code carries the verdict class it belongs to.

    The verdict travels with the member rather than in a lookup table beside it, so a
    code cannot be added without classifying it.
    """

    verdict: Verdict
    # 人类提示随成员携带。它是 OpenAPI 元数据与 Web 常量的派生源。
    hint: str

    def __new__(cls, wire: str, verdict: Verdict, hint: str) -> ReasonCode:
        member = object.__new__(cls)
        member._value_ = wire
        member.verdict = verdict
        member.hint = hint
        return member

    # Indeterminate: we could not observe reliably, so we do not conclude.
    STREAM_LOST = ("STREAM_LOST", Verdict.INDETERMINATE, "观测流中断")
    INFERENCE_BACKEND_UNREACHABLE = (
        "INFERENCE_BACKEND_UNREACHABLE",
        Verdict.INDETERMINATE,
        "推理后端不可达",
    )
    INFERENCE_TIMEOUT = ("INFERENCE_TIMEOUT", Verdict.INDETERMINATE, "推理超时")
    TIMESTAMP_DISCONTINUITY = (
        "TIMESTAMP_DISCONTINUITY",
        Verdict.INDETERMINATE,
        "时间轴不连续",
    )
    CHUNK_BACKLOG_EXCEEDED = ("CHUNK_BACKLOG_EXCEEDED", Verdict.INDETERMINATE, "观测积压超限")
    ACTION_ID_UNKNOWN = ("ACTION_ID_UNKNOWN", Verdict.INDETERMINATE, "动作编号不在模板中")
    INFERENCE_HOST_DOWN = ("INFERENCE_HOST_DOWN", Verdict.INDETERMINATE, "推理机失联")
    # 中断的四种成因都算。配置切换、进程重启、本地暂停、显式终止。
    RUN_INTERRUPTED = (
        "RUN_INTERRUPTED",
        Verdict.INDETERMINATE,
        "运行被中断。配置切换、进程重启、本地暂停或显式终止。",
    )
    STORAGE_UNAVAILABLE = (
        "STORAGE_UNAVAILABLE",
        Verdict.INDETERMINATE,
        "边缘存储不可用或超过安全水位",
    )
    IO_SIGNAL_LOST = ("IO_SIGNAL_LOST", Verdict.INDETERMINATE, "外部信号不可达")
    IO_TIME_UNALIGNED = ("IO_TIME_UNALIGNED", Verdict.INDETERMINATE, "外部信号时间未对齐")

    # Failing: a confirmed deviation. These four are the violation kinds.
    MISSED_STEP = ("MISSED_STEP", Verdict.FAIL, "缺少必做步骤")
    WRONG_STEP = ("WRONG_STEP", Verdict.FAIL, "出现了错误步骤")
    OUT_OF_ORDER = ("OUT_OF_ORDER", Verdict.FAIL, "步骤顺序错误")
    DEADLINE_EXCEEDED = ("DEADLINE_EXCEEDED", Verdict.FAIL, "步骤超时")

    @classmethod
    def from_wire(cls, wire: str) -> ReasonCode:
        """The member with this wire value, for a caller decoding what was stored or sent.

        `ReasonCode(wire)` is what Python's enum offers, but this class defines `__new__` to
        carry the verdict, so that call reads as construction with a missing argument — to a
        type checker as much as to a reader. This says which of the two it is, and resolves it
        in one place instead of at each call site.

        Raises `KeyError` for an unknown value, which is correct on the two paths that use it —
        both read back what this same enumeration wrote (local state, and E4's health events).
        ADR-0003's requirement that an unknown code render as the raw code plus a generic hint
        belongs to the cross-process wire boundary, where the centre and the inference host
        upgrade independently; that boundary must not use this.
        """
        return _BY_WIRE[wire]


_BY_WIRE = {code.value: code for code in ReasonCode}
"""Wire value to member. Built from the members, so it cannot fall behind them."""


INDETERMINATE_REASONS = frozenset(
    code for code in ReasonCode if code.verdict is Verdict.INDETERMINATE
)
"""Codes that say the observation window was not usable. Never a failing verdict."""

VIOLATION_REASONS = frozenset(code for code in ReasonCode if code.verdict is Verdict.FAIL)
"""The four violation kinds: wrong step, missed step, out of order, deadline exceeded."""
