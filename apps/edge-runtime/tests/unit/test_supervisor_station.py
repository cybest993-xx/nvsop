"""supervisor 的公开驱动接口: 输入到达后提交完整反应, 并返回判定和计时器。

持久化替身只记录接缝数据; SQLite 原子性由集成测试证明。可移动时钟验证取消、
重排和重复唤醒不会重复判定, 不使用 sleep。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from harness import (
    EXTERNAL_END,
    IDLE_TIMEOUT,
    STEP_DEADLINE,
    STEPS,
    FakeClock,
    MemoryReactionStore,
    opening_state,
)

from edge_runtime.judgment import ReasonCode, Verdict
from edge_runtime.judgment.evidence import EvidenceMargins
from edge_runtime.judgment.model import (
    Decision,
    EvidenceSpan,
    HostInstant,
    HostLiveness,
    Instance,
    Lifecycle,
    Ordering,
    Violation,
)
from edge_runtime.local_state import BackendReportContext
from edge_runtime.stream_health import StreamFact, StreamHealthEvent
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
            store=MemoryReactionStore(),
            state=opening_state(ordering, end_signals=end_signals),
            margins=MARGINS,
            clock=moving,
        ),
        moving,
    )


def action(signal: str, at: float, *, anchor: float = ANCHOR) -> ActionRecognized:
    return ActionRecognized(signal=signal, at=HostInstant(at), source_time=at, source_anchor=anchor)


def backend(name: str) -> BackendReportContext:
    return BackendReportContext(name, (f"model-{name}",))


class EveryInputReachesTheCoreAndCommitsDecisionsTest(unittest.TestCase):
    def test_an_action_that_decides_nothing_yields_no_decision(self) -> None:
        supervisor, _ = station()

        reaction = supervisor.receive(action(STEPS[0], at=100.0))

        self.assertEqual(
            Reaction(decisions=(), wake_at=HostInstant(160.0)),
            reaction,
            "one step of five decides nothing yet, but the pass is now in flight and the "
            "core wants waking at the nearer of the two limits",
        )

    def test_a_complete_pass_returns_the_decision_and_closed_instance(self) -> None:
        supervisor, _ = station()
        for second, signal in enumerate(STEPS[:-1], start=100):
            supervisor.receive(action(signal, at=float(second)))

        reaction = supervisor.receive(action(STEPS[4], at=104.0))

        self.assertEqual(
            Reaction(
                decisions=(
                    Decision(
                        instance_id=1,
                        verdict=Verdict.PASS,
                        reasons=(),
                        violations=(),
                        lifecycle=Lifecycle.CLOSED_BY_COMPLETE_SET,
                        evidence=EvidenceSpan.at(HostInstant(104.0)),
                    ),
                ),
                wake_at=None,
                closed_instances=(
                    Instance(
                        instance_id=1,
                        opened_at=HostInstant(100.0),
                        last_observation_at=HostInstant(104.0),
                        seen=frozenset(STEPS),
                        open_boundary_signal=STEPS[0],
                    ),
                ),
            ),
            reaction,
        )

    def test_a_violation_is_carried_by_its_decision(self) -> None:
        supervisor, _ = station(Ordering.ORDERED)
        supervisor.receive(action(STEPS[0], at=100.0))

        reaction = supervisor.receive(action(STEPS[2], at=101.0))

        self.assertEqual(
            (
                Violation(
                    reason=ReasonCode.WRONG_STEP,
                    steps=(STEPS[2],),
                    evidence=EvidenceSpan.at(HostInstant(101.0)),
                ),
                Violation(
                    reason=ReasonCode.MISSED_STEP,
                    steps=(STEPS[1],),
                    evidence=EvidenceSpan.at(HostInstant(101.0)),
                ),
            ),
            tuple(v for d in reaction.decisions for v in d.violations),
            "the jumped step is reported on arrival, not at close: that is what makes "
            "error-proofing real-time for the most common violation kind (§5.1)",
        )

    def test_multiple_normalized_events_return_the_closing_decision(
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
                Decision(
                    instance_id=1,
                    verdict=Verdict.INDETERMINATE,
                    reasons=(ReasonCode.IO_TIME_UNALIGNED,),
                    violations=(),
                    lifecycle=Lifecycle.CLOSED_BY_END_SIGNAL,
                    evidence=EvidenceSpan.at(HostInstant(120.0)),
                ),
            ),
            reaction.decisions,
            "§5.8: the end signal could not be placed on the video timeline, so this pass "
            "closes indeterminate rather than running the set comparison on it",
        )

    def test_a_health_event_alone_produces_no_decision(self) -> None:
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
            Reaction(decisions=(), wake_at=HostInstant(160.0)),
            reaction,
            "losing sight is not a conclusion. It goes on the instance's record and is "
            "read when something does conclude (§5.1)",
        )

    def test_the_returned_state_is_already_committed_once(self) -> None:
        store = MemoryReactionStore()
        supervisor = StationSupervisor(
            state=opening_state(Ordering.UNORDERED),
            store=store,
            margins=MARGINS,
            clock=FakeClock(),
        )
        supervisor.receive(action(STEPS[0], at=100.0))
        self.assertEqual(store.reactions, [(supervisor.state, (), (), ())])
        instance = supervisor.state.instance
        assert instance is not None
        self.assertEqual(1, instance.instance_id)


class MultipleBackendSourcesStayIsolatedTest(unittest.TestCase):
    def test_one_backend_recovery_does_not_clear_another_backend_failure(self) -> None:
        supervisor, _ = station(Ordering.ORDERED)
        failed = backend("a")
        healthy = backend("b")
        supervisor.receive(
            StreamHealthObserved(
                event=StreamHealthEvent(
                    fact=StreamFact.SOURCE_ERROR,
                    at_monotonic=90.0,
                    source_anchor=ANCHOR,
                )
            ),
            report_provenance=failed,
        )
        supervisor.receive(
            StreamHealthObserved(
                event=StreamHealthEvent(
                    fact=StreamFact.DELIVERING,
                    at_monotonic=91.0,
                    source_anchor=ANCHOR + 100.0,
                )
            ),
            report_provenance=healthy,
        )
        supervisor.receive(
            action(STEPS[0], at=100.0, anchor=ANCHOR + 100.0),
            report_provenance=healthy,
        )

        reaction = supervisor.receive(
            action(STEPS[2], at=101.0, anchor=ANCHOR + 100.0),
            report_provenance=healthy,
        )

        self.assertEqual(1, len(reaction.decisions))
        self.assertIs(Verdict.INDETERMINATE, reaction.decisions[0].verdict)
        self.assertEqual((ReasonCode.STREAM_LOST,), reaction.decisions[0].reasons)

    def test_source_anchors_are_compared_only_within_the_same_backend(self) -> None:
        supervisor, _ = station()
        source_a = backend("a")
        source_b = backend("b")
        supervisor.receive(
            action(STEPS[0], at=100.0, anchor=ANCHOR),
            report_provenance=source_a,
        )
        supervisor.receive(
            action(STEPS[1], at=101.0, anchor=ANCHOR + 100.0),
            report_provenance=source_b,
        )
        instance = supervisor.state.instance
        assert instance is not None
        self.assertNotIn(ReasonCode.TIMESTAMP_DISCONTINUITY, instance.impairments)

        supervisor.receive(
            action(STEPS[2], at=102.0, anchor=ANCHOR + 1.0),
            report_provenance=source_a,
        )

        instance = supervisor.state.instance
        assert instance is not None
        self.assertIn(ReasonCode.TIMESTAMP_DISCONTINUITY, instance.impairments)

    def test_failed_commit_does_not_publish_source_health_state(self) -> None:
        store = MemoryReactionStore()
        supervisor = StationSupervisor(
            state=opening_state(Ordering.UNORDERED),
            store=store,
            margins=MARGINS,
            clock=FakeClock(),
        )
        source = backend("a")
        arriving = StreamHealthObserved(
            event=StreamHealthEvent(
                fact=StreamFact.SOURCE_ERROR,
                at_monotonic=90.0,
                source_anchor=ANCHOR,
            )
        )

        with patch.object(store, "commit", side_effect=(RuntimeError("commit failed"), None)):
            with self.assertRaisesRegex(RuntimeError, "commit failed"):
                supervisor.receive(arriving, report_provenance=source)
            supervisor.receive(arriving, report_provenance=source)

        self.assertIn(ReasonCode.STREAM_LOST, supervisor.state.active_impairments)

    def test_failed_commit_does_not_publish_report_provenance(self) -> None:
        store = MemoryReactionStore()
        supervisor = StationSupervisor(
            state=opening_state(Ordering.UNORDERED),
            store=store,
            margins=MARGINS,
            clock=FakeClock(),
        )
        source_a = backend("a")
        source_b = backend("b")

        with (
            patch.object(store, "commit", side_effect=RuntimeError("commit failed")),
            self.assertRaisesRegex(RuntimeError, "commit failed"),
        ):
            supervisor.receive(action(STEPS[0], at=100.0), report_provenance=source_a)

        supervisor.receive(action(STEPS[0], at=101.0), report_provenance=source_b)

        self.assertEqual({1: (source_b,)}, store.report_provenance[-1])


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

        self.assertEqual(Reaction(decisions=(), wake_at=HostInstant(160.0)), reaction)

    def test_the_instant_a_superseded_timer_named_decides_nothing(self) -> None:
        supervisor, clock = station()
        supervisor.receive(action(STEPS[0], at=100.0))
        supervisor.receive(action(STEPS[1], at=140.0))

        clock.now = 160.0
        reaction = supervisor.wake(host=HostLiveness.ALIVE)

        self.assertEqual(
            Reaction(decisions=(), wake_at=HostInstant(200.0)),
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
            tuple(d.reasons[0] for d in first.decisions),
        )
        self.assertEqual(
            Reaction(decisions=(), wake_at=HostInstant(400.0)),
            second,
            "the same wait is one fact. Having reported it, the next thing that can happen "
            "by time alone is the idle close",
        )

    def test_a_wake_up_with_nothing_in_flight_decides_nothing(self) -> None:
        supervisor, clock = station()

        clock.now = 500.0

        self.assertEqual(
            Reaction(decisions=(), wake_at=None), supervisor.wake(host=HostLiveness.ALIVE)
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
                Decision(
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
                ),
            ),
            reaction.decisions,
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
                Decision(
                    instance_id=1,
                    verdict=Verdict.INDETERMINATE,
                    reasons=(ReasonCode.INFERENCE_HOST_DOWN,),
                    violations=(),
                    lifecycle=Lifecycle.CLOSED_BY_IDLE_TIMEOUT,
                    evidence=EvidenceSpan.at(HostInstant(400.0)),
                ),
            ),
            reaction.decisions,
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
                Decision(
                    instance_id=1,
                    verdict=Verdict.INDETERMINATE,
                    reasons=(ReasonCode.STREAM_LOST,),
                    violations=(),
                    lifecycle=Lifecycle.CLOSED_BY_IDLE_TIMEOUT,
                    evidence=EvidenceSpan.at(HostInstant(400.0)),
                ),
            ),
            reaction.decisions,
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

        decisions = list(reaction.decisions)
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
            [(d.verdict, d.reasons) for d in reaction.decisions],
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
                decisions=(
                    Decision(
                        instance_id=1,
                        verdict=Verdict.INDETERMINATE,
                        reasons=(ReasonCode.RUN_INTERRUPTED,),
                        violations=(),
                        lifecycle=Lifecycle.CLOSED_BY_RUN_INTERRUPTION,
                        evidence=EvidenceSpan.at(HostInstant(250.0)),
                    ),
                ),
                wake_at=None,
                closed_instances=(
                    Instance(
                        instance_id=1,
                        opened_at=HostInstant(100.0),
                        last_observation_at=HostInstant(100.0),
                        seen=frozenset({STEPS[0]}),
                        impairments=frozenset({ReasonCode.RUN_INTERRUPTED}),
                        open_boundary_signal=STEPS[0],
                    ),
                ),
            ),
            reaction,
        )

    def test_an_interruption_with_nothing_in_flight_concludes_nothing(self) -> None:
        supervisor, clock = station()

        clock.now = 250.0

        self.assertEqual(Reaction(decisions=(), wake_at=None), supervisor.interrupt())


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
