"""The judgment core's own types: what the supervisor passes in and what it gets back.

Everything here is data the supervisor holds and persists. The core owns no tables, no
module-level state, and no clock — time arrives as a `HostInstant` on an event
(judgment-and-boundary.md §5.18). The template and the station's resolved runtime
parameters travel with the state, so the core never reads configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from edge_runtime.judgment.reasons import INDETERMINATE_REASONS, ReasonCode, Verdict

StepSignal = str
"""A step's identity, as the template declares it.

The core does not know whether this names an action number the VLM produced or an
external signal's semantic label. The glossary makes them the same kind of thing: an
external signal is an observation, with no special standing in judgment. A station with
no connector configured therefore runs the same code path as one with several — a
`if a connector is configured` branch would violate edge-autonomy.md §5.8.
"""


@dataclass(frozen=True, slots=True, order=True)
class HostInstant:
    """A point on the inference host's monotonic clock, in seconds.

    A distinct type because the two clocks in play must not mix: the idle timeout has to
    be measured on this one, never on chunk timestamps, which cannot measure chunks
    ceasing to arrive (§5.1). Every arithmetic the core performs is on this clock.
    """

    seconds: float


class Ordering(Enum):
    """Whether the template's steps are strictly ordered.

    A template semantic, not a runtime parameter: it decides whether a skipped number is
    a violation on arrival (§5.1), so changing it changes the judgment's shape, while
    changing a runtime parameter does not (control-plane.md §5.3).
    """

    ORDERED = "ordered"
    UNORDERED = "unordered"


@dataclass(frozen=True, slots=True)
class Template:
    """The declared steps and instance boundary.

    Every step is required; the first version has no skippable step, because the base's
    `actions_can_be_skipped` measurably means "not part of this SOP" rather than
    "optional" (§5.1).
    """

    steps: tuple[StepSignal, ...]
    ordering: Ordering
    start_signal: StepSignal
    end_signals: tuple[StepSignal, ...] = ()
    """Declared end signals. Empty is normal: a complete step set and the idle timeout
    also close an instance, so an end signal is one of three closing conditions."""

    def __post_init__(self) -> None:
        if not self.steps:
            raise ValueError("a template declares at least one step")
        if len(set(self.steps)) != len(self.steps):
            raise ValueError(f"template steps must be distinct: {self.steps}")

    def index_of(self, signal: StepSignal) -> int | None:
        """The step's position, or None when the signal names no step of this template."""
        try:
            return self.steps.index(signal)
        except ValueError:
            return None


@dataclass(frozen=True, slots=True)
class Observation:
    """A step signal was recognized at a point in time.

    `source_time` is the chunk's own timeline, relative to the stream's start. The core
    performs no arithmetic on it — it belongs to the supervisor's latency accounting,
    whose arrival-class start point is the last source frame (§5.6).
    """

    signal: StepSignal
    at: HostInstant
    source_time: float


@dataclass(frozen=True, slots=True)
class ValidityImpaired:
    """Observation became unreliable, and why.

    Stream health is not an observation: an observation states what happened at the
    station, this states whether we could see it at all (CONTEXT.md). It therefore never
    enters the missing-step set comparison.

    It carries no instant, unlike the other events. What the core needs is that this
    happened and where it falls in the event sequence, which is the order the supervisor
    calls in (§5.18); the timeline of stream health is the supervisor's own record.
    """

    reason: ReasonCode

    def __post_init__(self) -> None:
        if self.reason not in INDETERMINATE_REASONS:
            raise ValueError(f"{self.reason.value} does not describe impaired observation")


@dataclass(frozen=True, slots=True)
class ValidityRestored:
    """The named impairment no longer holds.

    It stays in the open instance's record regardless: a pass that could not be observed
    for part of its duration cannot be concluded on afterwards.
    """

    reason: ReasonCode


@dataclass(frozen=True, slots=True)
class RunInterrupted:
    """A configuration switch or a process restart ended this run.

    In-flight instances are concluded as indeterminate rather than carried across, so one
    maintenance action does not manufacture a violation.
    """

    at: HostInstant


Event = Observation | ValidityImpaired | ValidityRestored | RunInterrupted
"""What the supervisor normalizes and sends in. The core does not know whether one
arrived over SSE or from a connector. Timer expiry joins this union with the idle
timeout."""


@dataclass(frozen=True, slots=True)
class EvidenceSpan:
    """Where the evidence for one conclusion is, and how much of it is required.

    The core declares the anchor and the required span only. Leading and trailing
    margins are runtime parameters the supervisor adds, because the core reads no
    configuration (evidence-and-retention.md §5.20). A configured window may widen the
    required span but never shorten it: the supervisor takes the union.
    """

    anchor: HostInstant
    required_from: HostInstant
    required_to: HostInstant

    @classmethod
    def at(cls, anchor: HostInstant) -> EvidenceSpan:
        """A conclusion whose required evidence is the anchored moment itself."""
        return cls(anchor=anchor, required_from=anchor, required_to=anchor)


@dataclass(frozen=True, slots=True)
class Violation:
    """A confirmed deviation. Latched by the supervisor, never withdrawn (§5.2)."""

    reason: ReasonCode
    steps: tuple[StepSignal, ...]
    evidence: EvidenceSpan


class Lifecycle(Enum):
    """Whether this decision closes its instance, and by which condition.

    The closing condition is part of the decision rather than something the supervisor
    infers, because it is what `monitor_sop_instance` records as the close reason.
    """

    STAYS_OPEN = "stays_open"
    CLOSED_BY_END_SIGNAL = "closed_by_end_signal"
    CLOSED_BY_COMPLETE_SET = "closed_by_complete_set"
    CLOSED_BY_RUN_INTERRUPTION = "closed_by_run_interruption"


@dataclass(frozen=True, slots=True)
class Decision:
    """What the supervisor should do. The core describes; it executes nothing.

    Latching, disposal dispatch, evidence clipping, reporting and writing local state are
    all the supervisor's (§5.18).
    """

    instance_id: int
    verdict: Verdict
    reasons: tuple[ReasonCode, ...]
    violations: tuple[Violation, ...]
    lifecycle: Lifecycle
    evidence: EvidenceSpan

    def __post_init__(self) -> None:
        if self.verdict is Verdict.PASS and self.reasons:
            raise ValueError("a passing verdict carries no reason code")
        if any(reason.verdict is not self.verdict for reason in self.reasons):
            raise ValueError(f"reason codes do not all classify as {self.verdict.value}")


ViolationKey = tuple[ReasonCode, tuple[StepSignal, ...]]
"""Identity of a violation within one instance, so the same fact is reported once."""


@dataclass(frozen=True, slots=True)
class Instance:
    """One SOP instance in flight: a station's single pass of work, with a boundary."""

    instance_id: int
    opened_at: HostInstant
    last_observation_at: HostInstant
    seen: frozenset[StepSignal] = frozenset()
    expected_index: int = 0
    """The ordered template's next expected position. Unused when unordered."""
    impairments: frozenset[ReasonCode] = frozenset()
    """Every impairment in force when this instance opened or arriving since.

    Never cleared. This is the validity record the closing gate reads: a pass that was
    unobservable for part of its duration cannot be concluded on once sight returns.
    """
    settled: frozenset[ViolationKey] = frozenset()
    """Violations already reported for this instance, so one fact is reported once."""


@dataclass(frozen=True, slots=True)
class JudgmentState:
    """Everything the core needs, held and persisted by the supervisor."""

    template: Template
    instance: Instance | None = None
    active_impairments: frozenset[ReasonCode] = frozenset()
    """Impairments currently in force, which the next instance starts out carrying."""
    next_instance_id: int = 1


@dataclass(frozen=True, slots=True)
class Outcome:
    """The transition's result: `(new state, decisions, next wake-up)`.

    The wake-up joins it with the idle timeout, which is the first closing condition the
    core drives by time rather than by an arriving observation.
    """

    state: JudgmentState
    decisions: tuple[Decision, ...] = ()
