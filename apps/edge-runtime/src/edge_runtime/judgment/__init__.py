"""The judgment core: a pure state-transition function over normalized observations.

`(state, event) -> (new state, decisions, next wake-up)`. The core reads no clock,
queries no state, and performs no I/O (judgment-and-boundary.md §5.18). Time enters
only as data on an event; the supervisor holds and persists the state.
"""

from __future__ import annotations

from edge_runtime.judgment.reasons import (
    INDETERMINATE_REASONS,
    VIOLATION_REASONS,
    ReasonCode,
    Verdict,
)

__all__ = [
    "INDETERMINATE_REASONS",
    "VIOLATION_REASONS",
    "ReasonCode",
    "Verdict",
]
