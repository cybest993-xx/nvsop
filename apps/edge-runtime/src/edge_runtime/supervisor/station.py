"""One station's supervisor: it drives the core, holds the timer, and issues the commands.

The core is pure and cannot wake itself, so this is the half that touches the world. It does
three things and no more (§5.18):

- normalizes what arrives into core events, so the core never learns a source;
- holds the deadline the core declared, on the host's monotonic clock;
- turns each decision into commands.

It performs none of those commands. Persistence (#19), disposal (#50, #51), evidence
extraction (#52) and reporting (#46) each take them from here.

**How the timer cannot fire twice for one wait.** This holds a deadline, not a scheduled
callback, and a firing is honoured only once the clock has reached that deadline. So
"cancelling" is assigning None and "rearming" is assigning a new instant — there is no
callback left armed to race with, and a wake-up naming a superseded instant decides nothing.
The run loop is free to wake early, late, or spuriously.

Standard library only, like the core it drives (edge-autonomy.md §5.11).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic

from edge_runtime.judgment.core import advance
from edge_runtime.judgment.model import (
    Event,
    HostInstant,
    HostLiveness,
    Instance,
    JudgmentState,
    RunInterrupted,
    TimerFired,
)
from edge_runtime.supervisor.commands import Command, EvidenceMargins, commands_for
from edge_runtime.supervisor.inputs import Normalizer, SupervisorInput


@dataclass(frozen=True, slots=True)
class Reaction:
    """What one input produced: commands to perform, and when to be back.

    `wake_at` is repeated on every reaction rather than only when it changes, so a run loop
    reads its next deadline from the same value it just handled and cannot hold a stale one.
    """

    commands: tuple[Command, ...]
    wake_at: HostInstant | None
    closed_instances: tuple[Instance, ...] = ()
    """本次反应闭合前的完整实例快照, 供本地状态在判定前建立父行。"""


class StationSupervisor:
    """The judgment core plus the state and the clock it deliberately does not hold.

    One per station, because a station is the unit an SOP instance belongs to: judgment and
    violations hang on the station, not on a camera or a person (CONTEXT.md).

    The clock is read at exactly three points, all of them places where no instant arrived
    with the input: computing how long the loop should wait, stamping a timer firing, and
    stamping a run interruption. An arriving observation keeps its own instant — restamping
    it here would measure the supervisor's own lag as the operator's pace.
    """

    def __init__(
        self,
        *,
        state: JudgmentState,
        margins: EvidenceMargins,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._state = state
        self._margins = margins
        self._clock = clock
        self._normalizer = Normalizer()
        self._deadline: HostInstant | None = None

    @property
    def state(self) -> JudgmentState:
        """The core's state as it now stands, for the caller that persists it (#19)."""
        return self._state

    @property
    def wake_at(self) -> HostInstant | None:
        """The instant the core asked to be woken at, or None when nothing is in flight."""
        return self._deadline

    def timeout(self) -> float | None:
        """How long the run loop may wait, or None when it may wait indefinitely.

        Never negative: a loop would read that as an unbounded poll, and a deadline already
        passed means the wake-up is owed now.
        """
        if self._deadline is None:
            return None
        return max(0.0, self._deadline.seconds - self._clock())

    def receive(self, arriving: SupervisorInput) -> Reaction:
        """One thing that arrived from outside: a chunk, a point-level signal, a fact."""
        return self._advance(self._normalizer.events_for(arriving))

    def wake(self, *, host: HostLiveness) -> Reaction:
        """The timer the core asked for, if it is in fact due.

        `host` is passed in rather than probed here because the run loop is what holds that
        knowledge — it is watching the inference request, so it is the thing that finds out
        the process stopped answering. The stream's state comes from the health events this
        supervisor already consumed, which is the first-hand record (§2.4).

        Not due yet, or nothing in flight: nothing is decided and the deadline stands.
        """
        if self._deadline is None:
            return Reaction(commands=(), wake_at=None)
        now = self._clock()
        if now < self._deadline.seconds:
            return Reaction(commands=(), wake_at=self._deadline)
        return self._advance(
            (
                TimerFired(
                    at=HostInstant(now),
                    host=host,
                    stream=self._normalizer.stream_health,
                ),
            )
        )

    def interrupt(self) -> Reaction:
        """A configuration switch or a shutdown ended this run.

        The pass in flight is concluded as indeterminate rather than carried across, so one
        maintenance action does not manufacture a violation (§5.2).
        """
        return self._advance((RunInterrupted(at=HostInstant(self._clock())),))

    def _advance(self, events: tuple[Event, ...]) -> Reaction:
        """Send each event through the core, collecting commands and rearming the timer.

        One input can become several events — a re-anchoring impairs, the observation lands,
        the impairment lifts — and each is a separate transition, because the core takes one
        event at a time. The deadline is assigned from the last outcome unconditionally,
        which is how arming, rearming and cancelling are all one line.
        """
        commands: list[Command] = []
        closed_instances: list[Instance] = []
        for event in events:
            outcome = advance(self._state, event)
            self._state = outcome.state
            self._deadline = outcome.wake_at
            closed_instances.extend(outcome.closed_instances)
            for decision in outcome.decisions:
                commands.extend(commands_for(decision, margins=self._margins))
        return Reaction(
            commands=tuple(commands),
            wake_at=self._deadline,
            closed_instances=tuple(closed_instances),
        )
