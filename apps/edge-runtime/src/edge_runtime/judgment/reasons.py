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

    def __new__(cls, wire: str, verdict: Verdict) -> ReasonCode:
        member = object.__new__(cls)
        member._value_ = wire
        member.verdict = verdict
        return member

    # Indeterminate: we could not observe reliably, so we do not conclude.
    STREAM_LOST = ("STREAM_LOST", Verdict.INDETERMINATE)
    INFERENCE_BACKEND_UNREACHABLE = ("INFERENCE_BACKEND_UNREACHABLE", Verdict.INDETERMINATE)
    INFERENCE_TIMEOUT = ("INFERENCE_TIMEOUT", Verdict.INDETERMINATE)
    TIMESTAMP_DISCONTINUITY = ("TIMESTAMP_DISCONTINUITY", Verdict.INDETERMINATE)
    CHUNK_BACKLOG_EXCEEDED = ("CHUNK_BACKLOG_EXCEEDED", Verdict.INDETERMINATE)
    ACTION_ID_UNKNOWN = ("ACTION_ID_UNKNOWN", Verdict.INDETERMINATE)
    INFERENCE_HOST_DOWN = ("INFERENCE_HOST_DOWN", Verdict.INDETERMINATE)
    RUN_INTERRUPTED = ("RUN_INTERRUPTED", Verdict.INDETERMINATE)
    IO_SIGNAL_LOST = ("IO_SIGNAL_LOST", Verdict.INDETERMINATE)
    IO_TIME_UNALIGNED = ("IO_TIME_UNALIGNED", Verdict.INDETERMINATE)

    # Failing: a confirmed deviation. These four are the violation kinds.
    MISSED_STEP = ("MISSED_STEP", Verdict.FAIL)
    WRONG_STEP = ("WRONG_STEP", Verdict.FAIL)
    OUT_OF_ORDER = ("OUT_OF_ORDER", Verdict.FAIL)
    DEADLINE_EXCEEDED = ("DEADLINE_EXCEEDED", Verdict.FAIL)

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
