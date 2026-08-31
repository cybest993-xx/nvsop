"""Translating between the core's types and their stored form.

Two rules hold everywhere in here. Every instant is stored as the monotonic float the core
produced, never re-derived: the store reads no clock (§5.18 applies to it for the same
reason it applies to the core). And every set is stored sorted, because a `frozenset` has no
order to preserve and an uncanonical dump would make the violation table's uniqueness depend
on iteration order — the same deviation latched twice under two spellings of one set.

Standard library only (edge-autonomy.md §5.11).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable

from edge_runtime.judgment.model import (
    EvidenceSpan,
    HostInstant,
    StepSignal,
    Violation,
    ViolationKey,
)
from edge_runtime.judgment.reasons import ReasonCode


def span(anchor: float, required_from: float, required_to: float) -> EvidenceSpan:
    return EvidenceSpan(
        anchor=HostInstant(anchor),
        required_from=HostInstant(required_from),
        required_to=HostInstant(required_to),
    )


def violation(row: sqlite3.Row) -> Violation:
    return Violation(
        reason=ReasonCode.from_wire(row["reason"]),
        steps=tuple(json.loads(row["steps"])),
        evidence=span(row["evidence_anchor"], row["evidence_from"], row["evidence_to"]),
    )


def dump_steps(steps: tuple[StepSignal, ...]) -> str:
    """A violation's steps, in the template's order, which is the order it names them in."""
    return json.dumps(list(steps))


def dump_signal_set(signals: Iterable[StepSignal]) -> str:
    """A set of signals, sorted: the seen set has no order of its own."""
    return json.dumps(sorted(signals))


def load_signal_set(dumped: str) -> frozenset[StepSignal]:
    return frozenset(json.loads(dumped))


def dump_reasons(reasons: Iterable[ReasonCode]) -> str:
    return json.dumps(sorted(reason.value for reason in reasons))


def load_reasons(dumped: str) -> frozenset[ReasonCode]:
    return frozenset(ReasonCode.from_wire(value) for value in json.loads(dumped))


def dump_settled(settled: frozenset[ViolationKey]) -> str:
    return json.dumps(sorted([reason.value, list(steps)] for reason, steps in settled))


def load_settled(dumped: str) -> frozenset[ViolationKey]:
    return frozenset(
        (ReasonCode.from_wire(reason), tuple(steps)) for reason, steps in json.loads(dumped)
    )
