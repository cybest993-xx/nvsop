"""What arrives at the supervisor, and how it becomes the core's events.

The core consumes normalized observations and must not know whether one came from a chunk,
a camera or a connector (harness §1). This module is where that ignorance is produced: it
holds the vocabulary of what actually arrives on the inference host, and the translation
into the core's four event kinds.

It is also where two of the fourteen reason codes are derived rather than reported, because
only this side holds the state they are read from:

- `TIMESTAMP_DISCONTINUITY`, from the base's `first_timestamp` changing underneath us. That
  is not an event anyone sends — it is a field every chunk and every health event carries,
  and re-anchoring after a reconnect shows up as its value changing (§5.11).
- `IO_TIME_UNALIGNED`, from an external signal the connector could not place on the video
  timeline. §5.8 requires that such a judgment be indeterminate rather than failing.

Standard library only (edge-autonomy.md §5.11).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import assert_never

from edge_runtime.judgment.model import (
    Event,
    HostInstant,
    Observation,
    StepSignal,
    StreamHealth,
    ValidityImpaired,
    ValidityRestored,
)
from edge_runtime.judgment.reasons import INDETERMINATE_REASONS, ReasonCode
from edge_runtime.stream_health import StreamFact, StreamHealthEvent


@dataclass(frozen=True, slots=True)
class ActionRecognized:
    """One action number the inference service recognized, off one chunk.

    Both clocks ride along, because they answer different questions: `at` is the host's
    monotonic instant, which is the only clock that can measure chunks ceasing to arrive
    (§5.1), while `source_time` is the chunk's own timeline, which the supervisor's latency
    accounting needs (§5.6). `source_anchor` is the base's `first_timestamp`, carried so
    that re-anchoring is detectable at all.
    """

    signal: StepSignal
    at: HostInstant
    source_time: float
    source_anchor: float
    """Not optional here, unlike on a health event.

    A chunk only exists once the pipeline anchored the timeline, so the base's
    `first_timestamp` is always set by the time an action is recognized. A health event can
    arrive without one — a malformed event decodes to None — and that asymmetry is real
    rather than an oversight: the anchor comparison skips what it cannot compare.
    """


class TimeAlignment(Enum):
    """Whether this signal could be placed on the video timeline.

    Named rather than a boolean, because the call site has to state which it is: an
    unaligned signal is the difference between a judgment that may fail and one that may
    not (§5.8).
    """

    ALIGNED = "aligned"
    UNALIGNED = "unaligned"


@dataclass(frozen=True, slots=True)
class ExternalSignal:
    """One point-level change a connector observed, already on the host's monotonic clock.

    The connector converts: it holds both the device's timestamp and the offset, and its
    capability declaration is what says how well it can (§5.8). What reaches here is the
    monotonic instant the core measures on, plus whether that conversion was trustworthy.

    No `source_time`: a physical signal sits on no stream's timeline. It takes the same code
    path an action number does — a station with no connector and a station with three run
    the same judgment code, which §5.8 makes a hard constraint.
    """

    signal: StepSignal
    at: HostInstant
    alignment: TimeAlignment


@dataclass(frozen=True, slots=True)
class StreamHealthObserved:
    """One synthetic health event, as E4's channel delivered it.

    Wrapped rather than passed bare so this module's input vocabulary is one closed set,
    and so the wire type stays owned by `stream_health`, which holds both halves of it.
    """

    event: StreamHealthEvent


class Validity(Enum):
    """Which way an impairment the caller holds first-hand has just moved."""

    IMPAIRED = "impaired"
    RESTORED = "restored"


@dataclass(frozen=True, slots=True)
class ValidityChanged:
    """An impairment the caller observed directly, reported as it happens.

    The SSE loop knows the backend stopped answering or the queue is backing up; the
    connector runtime knows a point went unreachable. Those facts do not pass through the
    health channel, so their holder states them — while the codes that must be *derived*
    from carried state are derived here rather than asked for.
    """

    reason: ReasonCode
    now: Validity

    def __post_init__(self) -> None:
        if self.reason not in INDETERMINATE_REASONS:
            raise ValueError(
                f"{self.reason.value} is a violation kind, not an impairment; a caller "
                "cannot report a deviation as a reason we could not observe"
            )


SupervisorInput = ActionRecognized | ExternalSignal | StreamHealthObserved | ValidityChanged
"""What reaches the supervisor from outside itself. Its own timer and its own run
interruption are not here: those the supervisor raises, and they carry an instant it reads
from the clock rather than one that arrived with them."""


class Normalizer:
    """Turns arriving input into core events, holding the little state that requires.

    Two things are remembered: the last source anchor, so a change in it can be seen at
    all, and the last stream-health fact, so the supervisor can report the stream's state
    when the timer fires (§5.18). Neither is judgment — the core owns every verdict.
    """

    def __init__(self) -> None:
        self._anchor: float | None = None
        self._stream: StreamHealth = StreamHealth.HEALTHY

    @property
    def stream_health(self) -> StreamHealth:
        """The stream's state as the last health event left it.

        Starts healthy: a station is only driven while a stream request is being watched.
        An arriving chunk never clears a loss — treating it as recovery would be the
        optimistic reading §5.2 forbids, and would stop an impairment from surviving the
        chunks that arrive while the window is degraded.
        """
        return self._stream

    @property
    def stream_impairment(self) -> ReasonCode | None:
        """返回当前流健康对应的不可判定原因, 健康时返回 None."""
        if self._stream is StreamHealth.HEALTHY:
            return None
        return _stream_reason(self._stream)

    def events_for(self, arriving: SupervisorInput) -> tuple[Event, ...]:
        """The core events this input becomes, in the order to send them."""
        match arriving:
            case ActionRecognized():
                return self._momentary(
                    self._reanchoring(arriving.source_anchor),
                    Observation(
                        signal=arriving.signal,
                        at=arriving.at,
                        source_time=arriving.source_time,
                    ),
                )
            case ExternalSignal():
                return self._momentary(
                    _misalignment(arriving.alignment),
                    Observation(signal=arriving.signal, at=arriving.at, source_time=None),
                )
            case StreamHealthObserved():
                return (
                    *self._momentary(self._reanchoring(arriving.event.source_anchor)),
                    *self._health(arriving.event),
                )
            case ValidityChanged():
                if arriving.now is Validity.IMPAIRED:
                    return (ValidityImpaired(reason=arriving.reason),)
                return (ValidityRestored(reason=arriving.reason),)
            case _:
                assert_never(arriving)

    def _momentary(self, reasons: tuple[ReasonCode, ...], *events: Event) -> tuple[Event, ...]:
        """Impairments that hold across `events` only, then stop holding.

        The impairment comes first because `events` may be the end signal that closes the
        instance, and the closing gate reads the record as it stands at that moment. The
        restoration comes last so the pass in flight keeps the impairment on its record
        while a later pass starts clean — a re-anchoring taints the pass that straddles it,
        not every pass after it.
        """
        return (
            *(ValidityImpaired(reason=reason) for reason in reasons),
            *events,
            *(ValidityRestored(reason=reason) for reason in reasons),
        )

    def _reanchoring(self, anchor: float | None) -> tuple[ReasonCode, ...]:
        """Whether the source timeline moved, by comparing anchors (§5.11).

        The first anchor seen is not a change, and a missing one is nothing to compare
        against — a malformed health event carries none, and it already impairs on its own
        account.
        """
        if anchor is None:
            return ()
        known, self._anchor = self._anchor, anchor
        if known is None or known == anchor:
            return ()
        return (ReasonCode.TIMESTAMP_DISCONTINUITY,)

    def _health(self, event: StreamHealthEvent) -> tuple[Event, ...]:
        """更新流健康状态, 并只在状态变化时产生有效性事件。"""
        arriving = _stream_health(event)
        if arriving is self._stream:
            return ()
        previous = self._stream
        self._stream = arriving
        events: list[Event] = []
        if previous is not StreamHealth.HEALTHY:
            events.append(ValidityRestored(reason=_stream_reason(previous)))
        if arriving is not StreamHealth.HEALTHY:
            events.append(ValidityImpaired(reason=_stream_reason(arriving)))
        return tuple(events)


def _stream_health(event: StreamHealthEvent) -> StreamHealth:
    """把流事件映射为核心使用的健康状态, 未知事实按失联处理。"""
    if not event.impairs_observation:
        return StreamHealth.HEALTHY
    if event.fact is StreamFact.INFERENCE_TIMEOUT:
        return StreamHealth.INFERENCE_TIMEOUT
    if event.fact is StreamFact.CHUNK_BACKLOG_EXCEEDED:
        return StreamHealth.CHUNK_BACKLOG_EXCEEDED
    return StreamHealth.LOST


def _stream_reason(health: StreamHealth) -> ReasonCode:
    """返回当前流状态对应的不可判定原因。"""
    match health:
        case StreamHealth.LOST:
            return ReasonCode.STREAM_LOST
        case StreamHealth.INFERENCE_TIMEOUT:
            return ReasonCode.INFERENCE_TIMEOUT
        case StreamHealth.CHUNK_BACKLOG_EXCEEDED:
            return ReasonCode.CHUNK_BACKLOG_EXCEEDED
        case StreamHealth.HEALTHY:
            raise ValueError("healthy stream has no impairment reason")
        case _:
            assert_never(health)


def _misalignment(alignment: TimeAlignment) -> tuple[ReasonCode, ...]:
    """§5.8: a signal that could not be placed on the timeline may not produce a failure.

    Momentary rather than lasting, because misalignment is a property of this signal's
    arrival. Drift that persists reports itself again on the next signal, so one bad
    reading does not condemn every pass that follows.
    """
    if alignment is TimeAlignment.UNALIGNED:
        return (ReasonCode.IO_TIME_UNALIGNED,)
    return ()
