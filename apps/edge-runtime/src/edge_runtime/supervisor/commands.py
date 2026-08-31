"""What the supervisor is told to do, one command per effect the core described.

The core describes and executes nothing (judgment-and-boundary.md §5.18). This module is
the translation from a `Decision` into explicit commands, so that latching, persistence,
disposal dispatch and evidence extraction each have a named thing to implement rather than
a decision object every later ticket re-interprets its own way.

Commands are data and this module performs none of them: #19 persists, #49 latches and
archives the alert, #50/#51 dispatch disposal, #52 extracts and uploads the clip.

Standard library only, like the core it sits behind (edge-autonomy.md §5.11).
"""

from __future__ import annotations

from dataclasses import dataclass

from edge_runtime.judgment.model import Decision, EvidenceSpan, HostInstant, Lifecycle, Violation


@dataclass(frozen=True, slots=True)
class EvidenceMargins:
    """How much context to keep around a conclusion's anchor, in seconds.

    The station's resolved runtime parameters, arriving as data: the core reads no
    configuration, so widening the required span to something a reviewer can read is the
    supervisor's job (evidence-and-retention.md §5.20). No default here — the value is
    configured per station with a global default, and a time literal in this code path is
    exactly what §5.19 forbids.
    """

    leading: float
    trailing: float

    def __post_init__(self) -> None:
        if self.leading < 0 or self.trailing < 0:
            raise ValueError(
                f"evidence margins cannot be negative, got {self.leading}/{self.trailing}"
            )


@dataclass(frozen=True, slots=True)
class RecordDecision:
    """Write this decision to local state, the authority for decisions (§5.7).

    First of the commands for one decision: the violation and its report event are written
    in one transaction with the decision they belong to (#19).
    """

    decision: Decision


@dataclass(frozen=True, slots=True)
class LatchViolation:
    """Latch one confirmed deviation. Never withdrawn, whatever the instance concludes.

    Independent of the instance's verdict on purpose (§5.2): an instance may be
    indeterminate while carrying a latched violation. One says this pass's conclusion is
    unreliable, the other says this deviation is confirmed.
    """

    instance_id: int
    violation: Violation


@dataclass(frozen=True, slots=True)
class ClipEvidence:
    """Extract the video for one conclusion, already widened by the margins.

    `anchor` travels with the window because it is the judgment fact — the moment the
    conclusion is about, where the keyframe is taken and the only thing a re-clip may not
    change (§5.20).
    """

    instance_id: int
    anchor: HostInstant
    start: HostInstant
    end: HostInstant


@dataclass(frozen=True, slots=True)
class CloseInstance:
    """This instance is concluded, by the named condition.

    The condition is carried rather than inferred: it is what `monitor_sop_instance`
    records as the close reason.
    """

    instance_id: int
    lifecycle: Lifecycle


Command = RecordDecision | LatchViolation | ClipEvidence | CloseInstance
"""One effect the supervisor is to perform. Exhaustive: a new command kind must be handled
by every executor rather than silently ignored by one."""


def commands_for(decision: Decision, *, margins: EvidenceMargins) -> tuple[Command, ...]:
    """Everything this decision asks the supervisor to do, in the order to do it.

    A pass gets a clip too, because §5.19 retains pass-class evidence *by default*:
    compliance rate needs a denominator. That default is configurable off, and the switch is
    not here — it belongs to the retention policy the executing ticket reads. Emitting is
    what follows the documented default; suppressing here would hardcode the non-default and
    leave the command stream unable to express the default at all.
    """
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
    """One clip per distinct anchor, each covering everything required at that anchor.

    The decision and the violations it carries usually anchor at the same instant, and
    cutting the same seconds twice is waste no reviewer ever sees. Where a violation
    anchors elsewhere — a deadline reported at close, whose anchor is when the limit was
    crossed — it keeps its own clip, because the anchor is what makes the clip meaningful.
    """
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
    """The span covering both, so neither conclusion loses evidence it required."""
    return EvidenceSpan(
        anchor=held.anchor,
        required_from=min(held.required_from, arriving.required_from),
        required_to=max(held.required_to, arriving.required_to),
    )
