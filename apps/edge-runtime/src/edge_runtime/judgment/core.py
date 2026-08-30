"""The judgment core: a pure state-transition function over normalized observations.

`advance(state, event) -> Outcome`, where the outcome carries the new state and what the
supervisor should do. The core reads no clock, queries no state, and performs no I/O
(judgment-and-boundary.md §5.18): arithmetic on an instant that arrived on an event is
data, asking what time it is now would make the same events yield different output.

The instance boundary is declared by the template, never inferred from the sequence's
shape (ADR-0006). Sequence comparison keeps the base's semantics — a duplicate is not an
error, a missing number is the set difference, a complete set closes the pass — while the
boundary source is ours.
"""

from __future__ import annotations

from dataclasses import replace
from typing import assert_never

from edge_runtime.judgment.model import (
    Decision,
    Event,
    EvidenceSpan,
    HostInstant,
    Instance,
    JudgmentState,
    Lifecycle,
    Observation,
    Ordering,
    Outcome,
    Template,
    Violation,
    ViolationKey,
)
from edge_runtime.judgment.reasons import ReasonCode, Verdict

_REASON_ORDER = {code: position for position, code in enumerate(ReasonCode)}


def advance(state: JudgmentState, event: Event) -> Outcome:
    """Apply one event. The only entry point; the supervisor crosses this seam."""
    match event:
        case Observation():
            return _observe(state, event)
        case _:
            assert_never(event)


def _observe(state: JudgmentState, observation: Observation) -> Outcome:
    template = state.template
    instance = state.instance

    if instance is None:
        if observation.signal != template.start_signal:
            # Nothing is in flight and this is not the declared opening. Dropped rather
            # than guessed: opening on any recognized action would turn rework and
            # fetching parts into false instances, each ending as a missed step
            # (ADR-0009). The consequence — a pass whose opening action was skipped is
            # not recorded at all — is accepted there, not repaired by inference here.
            return Outcome(state=state)
        instance = Instance(
            instance_id=state.next_instance_id,
            opened_at=observation.at,
            last_observation_at=observation.at,
        )
        state = replace(state, instance=instance, next_instance_id=state.next_instance_id + 1)

    instance = replace(instance, last_observation_at=observation.at)
    index = template.index_of(observation.signal)

    if index is None:
        # The signal names no step of this template: either the start signal repeating
        # inside an open instance, which is rework rather than a new pass, or an end
        # signal, which closes it.
        if observation.signal in template.end_signals:
            return _close(state, instance, observation.at, Lifecycle.CLOSED_BY_END_SIGNAL)
        return Outcome(state=replace(state, instance=instance))

    return _observe_step(state, instance, observation.at, index)


def _observe_step(state: JudgmentState, instance: Instance, at: HostInstant, index: int) -> Outcome:
    template = state.template
    signal = template.steps[index]
    violations: tuple[Violation, ...] = ()

    if signal in instance.seen:
        # A duplicate is not an error. This is where the base opens a boundary and clears
        # its seen set, and it is what makes rework read as two violations (§2.2).
        pass
    elif template.ordering is Ordering.ORDERED:
        violations = _order_violations(template, instance, at, index)

    instance = replace(instance, seen=instance.seen | {signal})
    if template.ordering is Ordering.ORDERED and index >= instance.expected_index:
        instance = replace(instance, expected_index=index + 1)

    violations = _unsettled(instance, violations)
    instance = replace(instance, settled=instance.settled | {_key(v) for v in violations})

    if instance.seen == frozenset(template.steps):
        return _close(state, instance, at, Lifecycle.CLOSED_BY_COMPLETE_SET, carried=violations)

    state = replace(state, instance=instance)
    if not violations:
        return Outcome(state=state)
    return Outcome(
        state=state,
        decisions=(
            Decision(
                instance_id=instance.instance_id,
                verdict=Verdict.FAIL,
                reasons=_reasons(violations),
                violations=violations,
                lifecycle=Lifecycle.STAYS_OPEN,
                evidence=EvidenceSpan.at(at),
            ),
        ),
    )


def _order_violations(
    template: Template, instance: Instance, at: HostInstant, index: int
) -> tuple[Violation, ...]:
    """What an ordered template's arriving step says about the steps around it."""
    evidence = EvidenceSpan.at(at)
    if index == instance.expected_index:
        return ()

    if index > instance.expected_index:
        # The operator is doing a step that is not the one due now, and the steps jumped
        # over are missing. Two facts on one observation: which step is wrong tells the
        # operator what to stop, which steps are missing tells them what to add.
        skipped = tuple(
            step
            for step in template.steps[instance.expected_index : index]
            if step not in instance.seen
        )
        return (
            Violation(
                reason=ReasonCode.WRONG_STEP, steps=(template.steps[index],), evidence=evidence
            ),
            *(
                Violation(reason=ReasonCode.MISSED_STEP, steps=(step,), evidence=evidence)
                for step in skipped
            ),
        )

    # An unseen step arriving after a later one broke the order.
    return (
        Violation(
            reason=ReasonCode.OUT_OF_ORDER, steps=(template.steps[index],), evidence=evidence
        ),
    )


def _close(
    state: JudgmentState,
    instance: Instance,
    at: HostInstant,
    lifecycle: Lifecycle,
    carried: tuple[Violation, ...] = (),
) -> Outcome:
    """Conclude the instance. The set comparison is the backstop, not the first report."""
    evidence = EvidenceSpan.at(at)
    missing = _unsettled(
        instance,
        tuple(
            Violation(reason=ReasonCode.MISSED_STEP, steps=(step,), evidence=evidence)
            for step in state.template.steps
            if step not in instance.seen
        ),
    )
    violations = carried + missing
    latched = bool(instance.settled) or bool(violations)
    return Outcome(
        state=replace(state, instance=None),
        decisions=(
            Decision(
                instance_id=instance.instance_id,
                verdict=Verdict.FAIL if latched else Verdict.PASS,
                reasons=_closing_reasons(instance, violations),
                violations=violations,
                lifecycle=lifecycle,
                evidence=evidence,
            ),
        ),
    )


def _closing_reasons(
    instance: Instance, violations: tuple[Violation, ...]
) -> tuple[ReasonCode, ...]:
    """Every violation kind this instance carries, including ones already reported.

    The closing decision is the pass's conclusion, so it names why the pass failed even
    when the individual violations were latched earlier.
    """
    settled = {reason for reason, _ in instance.settled}
    return _sorted({violation.reason for violation in violations} | settled)


def _unsettled(instance: Instance, violations: tuple[Violation, ...]) -> tuple[Violation, ...]:
    """Drop what this instance already reported, so one fact is reported once."""
    return tuple(violation for violation in violations if _key(violation) not in instance.settled)


def _key(violation: Violation) -> ViolationKey:
    return (violation.reason, violation.steps)


def _reasons(violations: tuple[Violation, ...]) -> tuple[ReasonCode, ...]:
    return _sorted({violation.reason for violation in violations})


def _sorted(reasons: set[ReasonCode]) -> tuple[ReasonCode, ...]:
    """Declaration order, so a decision's reasons are comparable as a whole object."""
    return tuple(sorted(reasons, key=lambda reason: _REASON_ORDER[reason]))
