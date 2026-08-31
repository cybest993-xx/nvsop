"""The supervisor driving the core: normalized input in, commands and a timer out.

This is the seam the inference host's run loop crosses. Everything the core declared has to
become something the supervisor holds or performs — the timer it asked for, and the commands
the decision described (§5.18).

The timer is the substance of these assertions. It is measured on the host's monotonic clock
because that is the only clock that can measure chunks *ceasing to arrive* (§5.1), and it
must not fire twice for one wait: the ticket's own acceptance criterion is that cancelling or
rearming produces no duplicate judgment. `FakeClock` moves by assignment, so nothing here
sleeps.
"""

from __future__ import annotations

import unittest

from harness import (
    EXTERNAL_END,
    IDLE_TIMEOUT,
    STEP_DEADLINE,
    STEPS,
    FakeClock,
    opening_state,
)

from edge_runtime.judgment import ReasonCode, Verdict
from edge_runtime.judgment.model import (
    Decision,
    EvidenceSpan,
    HostInstant,
    HostLiveness,
    Lifecycle,
    Ordering,
    Violation,
)
from edge_runtime.stream_health import StreamFact, StreamHealthEvent
from edge_runtime.supervisor.commands import (
    ClipEvidence,
    CloseInstance,
    EvidenceMargins,
    LatchViolation,
    RecordDecision,
)
from edge_runtime.supervisor.inputs import (
    ActionRecognized,
    ExternalSignal,
    StreamHealthObserved,
    TimeAlignment,
)
from edge_runtime.supervisor.station import Reaction, StationSupervisor

MARGINS = EvidenceMargins(leading=5.0, trailing=5.0)
ANCHOR = 1_700_000_000.0


def station(
    ordering: Ordering = Ordering.UNORDERED,
    *,
    clock: FakeClock | None = None,
    end_signals: tuple[str, ...] = (),
) -> tuple[StationSupervisor, FakeClock]:
    """A supervisor for one station, with the clock the test will move."""
    moving = clock if clock is not None else FakeClock()
    return (
        StationSupervisor(
            state=opening_state(ordering, end_signals=end_signals),
            margins=MARGINS,
            clock=moving,
        ),
        moving,
    )


def action(signal: str, at: float, *, anchor: float = ANCHOR) -> ActionRecognized:
    return ActionRecognized(signal=signal, at=HostInstant(at), source_time=at, source_anchor=anchor)


class EveryInputReachesTheCoreAndComesBackAsCommandsTest(unittest.TestCase):
    def test_an_action_that_decides_nothing_yields_no_command(self) -> None:
        supervisor, _ = station()

        reaction = supervisor.receive(action(STEPS[0], at=100.0))

        self.assertEqual(
            Reaction(commands=(), wake_at=HostInstant(160.0)),
            reaction,
            "one step of five decides nothing yet, but the pass is now in flight and the "
            "core wants waking at the nearer of the two limits",
        )

    def test_a_complete_pass_comes_back_as_the_whole_command_sequence(self) -> None:
        supervisor, _ = station()
        for second, signal in enumerate(STEPS[:-1], start=100):
            supervisor.receive(action(signal, at=float(second)))

        reaction = supervisor.receive(action(STEPS[4], at=104.0))

        self.assertEqual(
            Reaction(
                commands=(
                    RecordDecision(
                        decision=Decision(
                            instance_id=1,
                            verdict=Verdict.PASS,
                            reasons=(),
                            violations=(),
                            lifecycle=Lifecycle.CLOSED_BY_COMPLETE_SET,
                            evidence=EvidenceSpan.at(HostInstant(104.0)),
                        )
                    ),
                    ClipEvidence(
                        instance_id=1,
                        anchor=HostInstant(104.0),
                        start=HostInstant(99.0),
                        end=HostInstant(109.0),
                    ),
                    CloseInstance(instance_id=1, lifecycle=Lifecycle.CLOSED_BY_COMPLETE_SET),
                ),
                wake_at=None,
            ),
            reaction,
        )

    def test_a_violation_comes_back_as_a_latch_command(self) -> None:
        supervisor, _ = station(Ordering.ORDERED)
        supervisor.receive(action(STEPS[0], at=100.0))

        reaction = supervisor.receive(action(STEPS[2], at=101.0))

        self.assertEqual(
            (
                LatchViolation(
                    instance_id=1,
                    violation=Violation(
                        reason=ReasonCode.WRONG_STEP,
                        steps=(STEPS[2],),
                        evidence=EvidenceSpan.at(HostInstant(101.0)),
                    ),
                ),
                LatchViolation(
                    instance_id=1,
                    violation=Violation(
                        reason=ReasonCode.MISSED_STEP,
                        steps=(STEPS[1],),
                        evidence=EvidenceSpan.at(HostInstant(101.0)),
                    ),
                ),
            ),
            tuple(c for c in reaction.commands if isinstance(c, LatchViolation)),
            "the jumped step is reported on arrival, not at close: that is what makes "
            "error-proofing real-time for the most common violation kind (§5.1)",
        )

    def test_one_input_producing_several_decisions_yields_all_of_their_commands(
        self,
    ) -> None:
        supervisor, _ = station(end_signals=(EXTERNAL_END,))
        supervisor.receive(action(STEPS[0], at=100.0))

        reaction = supervisor.receive(
            ExternalSignal(
                signal=EXTERNAL_END,
                at=HostInstant(120.0),
                alignment=TimeAlignment.UNALIGNED,
            )
        )

        self.assertEqual(
            (
                RecordDecision(
                    decision=Decision(
                        instance_id=1,
                        verdict=Verdict.INDETERMINATE,
                        reasons=(ReasonCode.IO_TIME_UNALIGNED,),
                        violations=(),
                        lifecycle=Lifecycle.CLOSED_BY_END_SIGNAL,
                        evidence=EvidenceSpan.at(HostInstant(120.0)),
                    )
                ),
                ClipEvidence(
                    instance_id=1,
                    anchor=HostInstant(120.0),
                    start=HostInstant(115.0),
                    end=HostInstant(125.0),
                ),
                CloseInstance(instance_id=1, lifecycle=Lifecycle.CLOSED_BY_END_SIGNAL),
            ),
            reaction.commands,
            "§5.8: the end signal could not be placed on the video timeline, so this pass "
            "closes indeterminate rather than running the set comparison on it",
        )

    def test_a_health_event_alone_produces_no_command(self) -> None:
        supervisor, _ = station()
        supervisor.receive(action(STEPS[0], at=100.0))

        reaction = supervisor.receive(
            StreamHealthObserved(
                event=StreamHealthEvent(
                    fact=StreamFact.SOURCE_ERROR, at_monotonic=110.0, source_anchor=ANCHOR
                )
            )
        )

        self.assertEqual(
            Reaction(commands=(), wake_at=HostInstant(160.0)),
            reaction,
            "losing sight is not a conclusion. It goes on the instance's record and is "
            "read when something does conclude (§5.1)",
        )

    def test_the_state_it_carries_is_what_the_next_ticket_persists(self) -> None:
        supervisor, _ = station()

        supervisor.receive(action(STEPS[0], at=100.0))

        instance = supervisor.state.instance
        assert instance is not None
        self.assertEqual(1, instance.instance_id)


class TheTimerIsArmedFromWhatTheCoreDeclaredTest(unittest.TestCase):
    def test_nothing_is_armed_before_a_pass_is_in_flight(self) -> None:
        supervisor, _ = station()

        self.assertIsNone(supervisor.wake_at)
        self.assertIsNone(supervisor.timeout())

    def test_the_wait_is_the_declared_instant_less_the_clock(self) -> None:
        supervisor, clock = station()
        supervisor.receive(action(STEPS[0], at=100.0))

        clock.now = 130.0

        self.assertEqual(HostInstant(160.0), supervisor.wake_at)
        self.assertEqual(30.0, supervisor.timeout())

    def test_a_deadline_already_passed_asks_the_loop_not_to_wait(self) -> None:
        supervisor, clock = station()
        supervisor.receive(action(STEPS[0], at=100.0))

        clock.now = 400.0

        self.assertEqual(
            0.0,
            supervisor.timeout(),
            "never negative: the loop would read that as a poll with no bound, and the "
            "wake-up is already owed",
        )

    def test_an_arriving_observation_rearms_to_the_new_wait(self) -> None:
        supervisor, _ = station()
        supervisor.receive(action(STEPS[0], at=100.0))

        supervisor.receive(action(STEPS[1], at=140.0))

        self.assertEqual(HostInstant(200.0), supervisor.wake_at)

    def test_closing_the_pass_disarms_it(self) -> None:
        supervisor, _ = station(end_signals=(EXTERNAL_END,))
        supervisor.receive(action(STEPS[0], at=100.0))

        supervisor.receive(
            ExternalSignal(
                signal=EXTERNAL_END, at=HostInstant(120.0), alignment=TimeAlignment.ALIGNED
            )
        )

        self.assertIsNone(supervisor.wake_at)
        self.assertIsNone(supervisor.timeout())


class RearmingProducesNoDuplicateJudgmentTest(unittest.TestCase):
    """The ticket's own criterion: a replaced or cancelled timer must not still fire.

    The supervisor holds a deadline rather than a scheduled callback, and a firing is only
    honoured when the clock has actually reached it. So a stale wake-up cannot decide
    anything — there is no armed callback left over to cancel, and nothing to race.
    """

    def test_a_wake_up_before_the_declared_instant_decides_nothing(self) -> None:
        supervisor, clock = station()
        supervisor.receive(action(STEPS[0], at=100.0))

        clock.now = 159.0
        reaction = supervisor.wake(host=HostLiveness.ALIVE)

        self.assertEqual(Reaction(commands=(), wake_at=HostInstant(160.0)), reaction)

    def test_the_instant_a_superseded_timer_named_decides_nothing(self) -> None:
        supervisor, clock = station()
        supervisor.receive(action(STEPS[0], at=100.0))
        supervisor.receive(action(STEPS[1], at=140.0))

        clock.now = 160.0
        reaction = supervisor.wake(host=HostLiveness.ALIVE)

        self.assertEqual(
            Reaction(commands=(), wake_at=HostInstant(200.0)),
            reaction,
            "160.0 was the deadline for a wait that a new observation ended. Deciding on "
            "it would report a step as late while the operator was doing the next one",
        )

    def test_one_wait_is_reported_once_however_often_the_loop_wakes(self) -> None:
        supervisor, clock = station()
        supervisor.receive(action(STEPS[0], at=100.0))

        clock.now = 160.0
        first = supervisor.wake(host=HostLiveness.ALIVE)
        clock.now = 170.0
        second = supervisor.wake(host=HostLiveness.ALIVE)

        self.assertEqual(
            (ReasonCode.DEADLINE_EXCEEDED,),
            tuple(c.decision.reasons[0] for c in first.commands if isinstance(c, RecordDecision)),
        )
        self.assertEqual(
            Reaction(commands=(), wake_at=HostInstant(400.0)),
            second,
            "the same wait is one fact. Having reported it, the next thing that can happen "
            "by time alone is the idle close",
        )

    def test_a_wake_up_with_nothing_in_flight_decides_nothing(self) -> None:
        supervisor, clock = station()

        clock.now = 500.0

        self.assertEqual(
            Reaction(commands=(), wake_at=None), supervisor.wake(host=HostLiveness.ALIVE)
        )


class WhatTheSupervisorFoundAtTheFiringTest(unittest.TestCase):
    """§5.18: the core declares the instant, the supervisor reports the facts it found.

    Both findings ride on one firing rather than on two timers, so the core has no branch
    for "idle close" against "process gone" — it reads them off the event.
    """

    def test_the_firing_carries_the_instant_the_clock_actually_read(self) -> None:
        supervisor, clock = station()
        supervisor.receive(action(STEPS[0], at=100.0))

        clock.now = 175.0
        reaction = supervisor.wake(host=HostLiveness.ALIVE)

        self.assertEqual(
            (
                RecordDecision(
                    decision=Decision(
                        instance_id=1,
                        verdict=Verdict.FAIL,
                        reasons=(ReasonCode.DEADLINE_EXCEEDED,),
                        violations=(
                            Violation(
                                reason=ReasonCode.DEADLINE_EXCEEDED,
                                # Every step still outstanding: an unordered template waits
                                # on all of them at once, so the late wait is about the set
                                # rather than about one next step.
                                steps=STEPS[1:],
                                evidence=EvidenceSpan.spanning(
                                    HostInstant(175.0), since=HostInstant(100.0)
                                ),
                            ),
                        ),
                        lifecycle=Lifecycle.STAYS_OPEN,
                        evidence=EvidenceSpan.at(HostInstant(175.0)),
                    )
                ),
                ClipEvidence(
                    instance_id=1,
                    anchor=HostInstant(175.0),
                    # The whole wait, widened by the margin — not the instant the limit was
                    # crossed. A reviewer has to see how long it actually waited (story 18).
                    start=HostInstant(95.0),
                    end=HostInstant(180.0),
                ),
            ),
            tuple(c for c in reaction.commands if isinstance(c, RecordDecision | ClipEvidence)),
            "175.0 rather than 160.0: a busy loop may reach the firing late, and the "
            "silence really did last that long",
        )

    def test_a_dead_backend_makes_the_idle_close_indeterminate(self) -> None:
        supervisor, clock = station()
        supervisor.receive(action(STEPS[0], at=100.0))

        clock.now = 100.0 + IDLE_TIMEOUT
        reaction = supervisor.wake(host=HostLiveness.DOWN)

        self.assertEqual(
            (
                RecordDecision(
                    decision=Decision(
                        instance_id=1,
                        verdict=Verdict.INDETERMINATE,
                        reasons=(ReasonCode.INFERENCE_HOST_DOWN,),
                        violations=(),
                        lifecycle=Lifecycle.CLOSED_BY_IDLE_TIMEOUT,
                        evidence=EvidenceSpan.at(HostInstant(400.0)),
                    )
                ),
            ),
            tuple(c for c in reaction.commands if isinstance(c, RecordDecision)),
            "process death cannot announce itself, so continued silence is the signal "
            "(§5.11). The four steps not seen are not reported as missed",
        )

    def test_a_lost_stream_makes_the_idle_close_indeterminate(self) -> None:
        supervisor, clock = station()
        supervisor.receive(action(STEPS[0], at=100.0))
        supervisor.receive(
            StreamHealthObserved(
                event=StreamHealthEvent(
                    fact=StreamFact.SOURCE_ERROR, at_monotonic=110.0, source_anchor=ANCHOR
                )
            )
        )

        clock.now = 100.0 + IDLE_TIMEOUT
        reaction = supervisor.wake(host=HostLiveness.ALIVE)

        self.assertEqual(
            (
                RecordDecision(
                    decision=Decision(
                        instance_id=1,
                        verdict=Verdict.INDETERMINATE,
                        reasons=(ReasonCode.STREAM_LOST,),
                        violations=(),
                        lifecycle=Lifecycle.CLOSED_BY_IDLE_TIMEOUT,
                        evidence=EvidenceSpan.at(HostInstant(400.0)),
                    )
                ),
            ),
            tuple(c for c in reaction.commands if isinstance(c, RecordDecision)),
            "§2.1, the measurement this whole design defends: the stream cut out, so the "
            "steps were not observed. That is not the operator failing to do them",
        )

    def test_the_stream_state_at_the_firing_is_the_one_last_reported(self) -> None:
        supervisor, clock = station()
        supervisor.receive(action(STEPS[0], at=100.0))
        for fact in (StreamFact.SOURCE_ERROR, StreamFact.DELIVERING):
            supervisor.receive(
                StreamHealthObserved(
                    event=StreamHealthEvent(fact=fact, at_monotonic=110.0, source_anchor=ANCHOR)
                )
            )

        clock.now = 100.0 + IDLE_TIMEOUT
        reaction = supervisor.wake(host=HostLiveness.ALIVE)

        decisions = [c.decision for c in reaction.commands if isinstance(c, RecordDecision)]
        self.assertEqual(
            [Verdict.INDETERMINATE],
            [decision.verdict for decision in decisions],
            "the stream recovered before the firing, so it is not lost now — but this "
            "pass was unobservable for part of its length and stays unconcludable (§5.1)",
        )
        self.assertEqual(
            [(ReasonCode.STREAM_LOST,)],
            [decision.reasons for decision in decisions],
        )

    def test_a_pass_that_opens_while_the_stream_is_already_down_is_impaired(self) -> None:
        supervisor, clock = station()
        supervisor.receive(
            StreamHealthObserved(
                event=StreamHealthEvent(
                    fact=StreamFact.SOURCE_ERROR, at_monotonic=1.0, source_anchor=ANCHOR
                )
            )
        )

        supervisor.receive(action(STEPS[0], at=100.0))
        clock.now = 100.0 + IDLE_TIMEOUT
        reaction = supervisor.wake(host=HostLiveness.ALIVE)

        self.assertEqual(
            [(Verdict.INDETERMINATE, (ReasonCode.STREAM_LOST,))],
            [
                (c.decision.verdict, c.decision.reasons)
                for c in reaction.commands
                if isinstance(c, RecordDecision)
            ],
            "the impairment was in force before this pass existed, so the pass carries it "
            "from the moment it opens: an instance that began blind was never observable",
        )


class ARunEndingConcludesWhatWasInFlightTest(unittest.TestCase):
    """§5.2 `RUN_INTERRUPTED`: one maintenance action must not manufacture a violation."""

    def test_an_interruption_closes_the_pass_at_the_clock_it_read(self) -> None:
        supervisor, clock = station()
        supervisor.receive(action(STEPS[0], at=100.0))

        clock.now = 250.0
        reaction = supervisor.interrupt()

        self.assertEqual(
            Reaction(
                commands=(
                    RecordDecision(
                        decision=Decision(
                            instance_id=1,
                            verdict=Verdict.INDETERMINATE,
                            reasons=(ReasonCode.RUN_INTERRUPTED,),
                            violations=(),
                            lifecycle=Lifecycle.CLOSED_BY_RUN_INTERRUPTION,
                            evidence=EvidenceSpan.at(HostInstant(250.0)),
                        )
                    ),
                    ClipEvidence(
                        instance_id=1,
                        anchor=HostInstant(250.0),
                        start=HostInstant(245.0),
                        end=HostInstant(255.0),
                    ),
                    CloseInstance(instance_id=1, lifecycle=Lifecycle.CLOSED_BY_RUN_INTERRUPTION),
                ),
                wake_at=None,
            ),
            reaction,
        )

    def test_an_interruption_with_nothing_in_flight_concludes_nothing(self) -> None:
        supervisor, clock = station()

        clock.now = 250.0

        self.assertEqual(Reaction(commands=(), wake_at=None), supervisor.interrupt())


class OnlyTheMonotonicClockIsReadAndOnlyWhereNoInstantArrivedTest(unittest.TestCase):
    def test_an_arriving_input_is_stamped_by_its_own_instant_not_the_clock(self) -> None:
        supervisor, clock = station()
        clock.now = 9_999.0

        supervisor.receive(action(STEPS[0], at=100.0))

        self.assertEqual(
            HostInstant(100.0 + STEP_DEADLINE),
            supervisor.wake_at,
            "the observation carries the instant it was observed at. Restamping it with "
            "the clock would measure the supervisor's own lag as the operator's pace",
        )


if __name__ == "__main__":
    unittest.main()
