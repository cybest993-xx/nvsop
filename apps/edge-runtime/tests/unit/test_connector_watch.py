"""Input points becoming observations, on the same path an action number takes.

§5.8's hard constraint: 判定逻辑不因某工位有无某类连接器而改变代码路径. This is where that is
produced, so the assertions here are about what leaves for the supervisor — an
`ExternalSignal` carrying a semantic label, indistinguishable in kind from a chunk's action
number — and about the two facts a connector holds first-hand and must report rather than
guess: a point it cannot reach, and a reading it cannot place on the video timeline.

The other two conditions §5.8 names — an unverified declaration and one that may drop edges —
are refused at template binding by `capability.unfit_for`, not re-decided here. A second
copy of that rule on this path is what harness §1 calls a defect.
"""

from __future__ import annotations

import unittest

from harness import EXTERNAL_END, EXTERNAL_START, FakeClock, opening_state

from edge_runtime.connectors.port import InputPoint, PointState, Reading, ReadResult, Unreachable
from edge_runtime.connectors.watch import ConnectorPoller, PointWatch
from edge_runtime.judgment.model import HostInstant, Ordering
from edge_runtime.judgment.reasons import ReasonCode
from edge_runtime.supervisor.commands import EvidenceMargins
from edge_runtime.supervisor.inputs import (
    ActionRecognized,
    ExternalSignal,
    TimeAlignment,
    Validity,
    ValidityChanged,
)
from edge_runtime.supervisor.station import StationSupervisor

POINT = InputPoint(label=EXTERNAL_START, address="1")
"""A template references a point by semantic label, never by device address (§5.8). The
address is the adapter's business; the label is what reaches judgment."""


def reading(
    state: PointState, *, at: float, alignment: TimeAlignment = TimeAlignment.ALIGNED
) -> Reading:
    return Reading(state=state, at=HostInstant(at), alignment=alignment)


def signal(at: float, alignment: TimeAlignment = TimeAlignment.ALIGNED) -> ExternalSignal:
    return ExternalSignal(signal=EXTERNAL_START, at=HostInstant(at), alignment=alignment)


def lost(now: Validity) -> ValidityChanged:
    return ValidityChanged(reason=ReasonCode.IO_SIGNAL_LOST, now=now)


class ScriptedInput:
    """A connector double answering from a script, one answer per point per poll.

    A double at the adapter seam (harness §4): what is under test is the runtime above it, and
    a script is the smallest thing that can stage "this point answered and that one did not".
    A point whose script runs out keeps returning its last answer, so a test states only the
    polls it is about.
    """

    def __init__(self, answers: dict[str, list[ReadResult]]) -> None:
        self._answers = {address: list(results) for address, results in answers.items()}
        self.reads: list[tuple[str, float]] = []

    def read(self, point: InputPoint, /, *, timeout: float) -> ReadResult:
        self.reads.append((point.address, timeout))
        staged = self._answers[point.address]
        return staged.pop(0) if len(staged) > 1 else staged[0]


class ARisingEdgeIsTheObservationTest(unittest.TestCase):
    """A point's *change* is the fact, not its level.

    An observation is a statement that something happened at a time (CONTEXT.md). A point
    reading ACTIVE on every poll of a five-second press did not happen five times.
    """

    def test_the_first_active_reading_produces_the_signal(self) -> None:
        watch = PointWatch(point=POINT)

        self.assertEqual((signal(at=10.0),), watch.inputs_for(reading(PointState.ACTIVE, at=10.0)))

    def test_staying_active_produces_nothing_further(self) -> None:
        watch = PointWatch(point=POINT)
        watch.inputs_for(reading(PointState.ACTIVE, at=10.0))

        self.assertEqual((), watch.inputs_for(reading(PointState.ACTIVE, at=11.0)))

    def test_returning_to_active_after_release_is_a_second_signal(self) -> None:
        watch = PointWatch(point=POINT)
        watch.inputs_for(reading(PointState.ACTIVE, at=10.0))
        watch.inputs_for(reading(PointState.INACTIVE, at=11.0))

        self.assertEqual((signal(at=12.0),), watch.inputs_for(reading(PointState.ACTIVE, at=12.0)))

    def test_a_release_is_not_an_observation(self) -> None:
        watch = PointWatch(point=POINT)
        watch.inputs_for(reading(PointState.ACTIVE, at=10.0))

        self.assertEqual(
            (),
            watch.inputs_for(reading(PointState.INACTIVE, at=11.0)),
            "the template references one semantic label, and 工件到位 is the arrival. A "
            "falling edge would arrive at judgment as that same label a second time",
        )

    def test_the_first_reading_being_inactive_says_nothing(self) -> None:
        watch = PointWatch(point=POINT)

        self.assertEqual(
            (),
            watch.inputs_for(reading(PointState.INACTIVE, at=10.0)),
            "a point resting at inactive when the runtime starts has not changed; treating "
            "the first poll as an edge would manufacture one per restart",
        )


class APointWeCannotReachIsReportedNotGuessedTest(unittest.TestCase):
    """§5.8 through `IO_SIGNAL_LOST`: 模板依赖的输入点位不可达 is indeterminate, never a pass.

    The connector runtime holds this fact first-hand, so it states it (`inputs.py`). What it
    must not do is hold the last known level and keep answering with it.
    """

    def test_losing_the_point_impairs_observation(self) -> None:
        watch = PointWatch(point=POINT)

        self.assertEqual(
            (lost(Validity.IMPAIRED),), watch.inputs_for(Unreachable(detail="connection refused"))
        )

    def test_staying_unreachable_is_reported_once(self) -> None:
        watch = PointWatch(point=POINT)
        watch.inputs_for(Unreachable(detail="connection refused"))

        self.assertEqual(
            (),
            watch.inputs_for(Unreachable(detail="connection refused")),
            "one impairment lasts until a reading restores it; one per failed poll would be "
            "one per period for as long as the cable is out",
        )

    def test_a_reading_restores_it(self) -> None:
        watch = PointWatch(point=POINT)
        watch.inputs_for(Unreachable(detail="connection refused"))

        self.assertEqual(
            (lost(Validity.RESTORED),), watch.inputs_for(reading(PointState.INACTIVE, at=11.0))
        )

    def test_an_edge_across_a_gap_is_reported_after_the_restoration(self) -> None:
        watch = PointWatch(point=POINT)
        watch.inputs_for(reading(PointState.INACTIVE, at=10.0))
        watch.inputs_for(Unreachable(detail="connection refused"))

        self.assertEqual(
            (lost(Validity.RESTORED), signal(at=12.0)),
            watch.inputs_for(reading(PointState.ACTIVE, at=12.0)),
            "the restoration comes first so the pass in flight keeps the impairment on its "
            "record: the change may have happened while we were blind, and the instant we "
            "report is when we saw it rather than when it happened",
        )

    def test_the_level_before_a_gap_is_not_carried_across_it(self) -> None:
        watch = PointWatch(point=POINT)
        watch.inputs_for(reading(PointState.ACTIVE, at=10.0))
        watch.inputs_for(Unreachable(detail="cable out"))

        self.assertEqual(
            (lost(Validity.RESTORED), signal(at=30.0)),
            watch.inputs_for(reading(PointState.ACTIVE, at=30.0)),
            "active-then-blind-then-active may be one press or two. Suppressing the second "
            "would decide it was one, which is the guess §5.8 refuses; reporting it while "
            "the pass already carries the impairment is the honest reading",
        )


class ASignalWeCannotPlaceOnTheTimelineMayNotFailAPassTest(unittest.TestCase):
    """§5.8: an unusable or drifted offset is indeterminate with `IO_TIME_UNALIGNED`.

    不得输出不通过 — it may never produce a failing verdict.
    """

    def test_the_alignment_the_adapter_reported_rides_along(self) -> None:
        watch = PointWatch(point=POINT)

        self.assertEqual(
            (signal(at=10.0, alignment=TimeAlignment.UNALIGNED),),
            watch.inputs_for(
                reading(PointState.ACTIVE, at=10.0, alignment=TimeAlignment.UNALIGNED)
            ),
            "the adapter holds the device clock and the offset, so it answers this; the "
            "runtime carries the answer to judgment without re-deciding it",
        )


class OneConnectorsPointsArePolledTogetherTest(unittest.TestCase):
    """A pass over every configured input point, with the cadence left to the run loop (#45).

    工位可以没有任何连接器 (§5.8), and it can equally have one with two points. What this asserts
    is that a failure on one does not silence the other: the station loses the judgment that
    depends on the failed point, not all of them.
    """

    def test_each_points_edges_are_reported_under_its_own_label(self) -> None:
        departure = InputPoint(label=EXTERNAL_END, address="2")
        connector = ScriptedInput(
            {
                POINT.address: [reading(PointState.ACTIVE, at=10.0)],
                departure.address: [reading(PointState.ACTIVE, at=10.0)],
            }
        )
        poller = ConnectorPoller(connector=connector, points=(POINT, departure), timeout=1.0)

        self.assertEqual(
            (
                signal(at=10.0),
                ExternalSignal(
                    signal=EXTERNAL_END, at=HostInstant(10.0), alignment=TimeAlignment.ALIGNED
                ),
            ),
            poller.poll(),
        )

    def test_the_configured_timeout_bounds_every_read(self) -> None:
        connector = ScriptedInput({POINT.address: [reading(PointState.INACTIVE, at=10.0)]})
        poller = ConnectorPoller(connector=connector, points=(POINT,), timeout=0.5)

        poller.poll()

        self.assertEqual(
            [(POINT.address, 0.5)],
            connector.reads,
            "a poll that blocks past its own period stops being a poll, so the timeout is "
            "configured rather than defaulted (§5.19)",
        )

    def test_one_unreachable_point_does_not_silence_the_other(self) -> None:
        departure = InputPoint(label=EXTERNAL_END, address="2")
        connector = ScriptedInput(
            {
                POINT.address: [Unreachable(detail="cable out")],
                departure.address: [reading(PointState.ACTIVE, at=10.0)],
            }
        )
        poller = ConnectorPoller(connector=connector, points=(POINT, departure), timeout=1.0)

        self.assertEqual(
            (
                lost(Validity.IMPAIRED),
                ExternalSignal(
                    signal=EXTERNAL_END, at=HostInstant(10.0), alignment=TimeAlignment.ALIGNED
                ),
            ),
            poller.poll(),
        )

    def test_a_point_at_rest_produces_nothing(self) -> None:
        connector = ScriptedInput({POINT.address: [reading(PointState.INACTIVE, at=10.0)]})
        poller = ConnectorPoller(connector=connector, points=(POINT,), timeout=1.0)

        self.assertEqual((), poller.poll())


class AConnectorSignalRunsTheSameJudgmentCodeAnActionNumberDoesTest(unittest.TestCase):
    """§5.8's hard constraint, end to end: 输入点位按语义标签进入同一判定事件路径.

    The two supervisors here are given the same template and reach the same declared start
    signal from the two different sources — one through the connector poller, one as a
    recognized action off a chunk. Everything from `StationSupervisor.receive` inward is one
    code path, and this is the assertion that would fail if a `if 配了连接器` branch appeared.

    Only the source varies. Comparing an external start signal against `STEPS[0]` instead
    would compare two *templates*: an action number that is also a step lands in the seen set,
    which is a template fact rather than a connector one.
    """

    def _supervisor(self) -> StationSupervisor:
        return StationSupervisor(
            state=opening_state(Ordering.ORDERED, start_signal=EXTERNAL_START),
            margins=EvidenceMargins(leading=0.0, trailing=0.0),
            clock=FakeClock(10.0),
        )

    def test_a_point_signal_opens_an_instance_exactly_as_an_action_does(self) -> None:
        by_connector = self._supervisor()
        connector = ScriptedInput({POINT.address: [reading(PointState.ACTIVE, at=10.0)]})
        poller = ConnectorPoller(connector=connector, points=(POINT,), timeout=1.0)

        for arriving in poller.poll():
            by_connector.receive(arriving)

        by_action = self._supervisor()
        by_action.receive(
            ActionRecognized(
                signal=EXTERNAL_START,
                at=HostInstant(10.0),
                source_time=1.0,
                source_anchor=1.0,
            )
        )

        self.assertEqual(
            by_action.state.instance,
            by_connector.state.instance,
            "same instance id, same opening instant, same empty impairment record. The core "
            "cannot tell the two apart, which is what §5.8 requires of it",
        )

    def test_the_signal_it_carries_is_the_semantic_label_not_the_address(self) -> None:
        connector = ScriptedInput({POINT.address: [reading(PointState.ACTIVE, at=10.0)]})
        poller = ConnectorPoller(connector=connector, points=(POINT,), timeout=1.0)

        self.assertEqual(
            (signal(at=10.0),),
            poller.poll(),
            "a template references a point by semantic label and never by device address "
            "(§5.8): this one declared 工件到位 as its start signal and never named ISAPI input 1",
        )


if __name__ == "__main__":
    unittest.main()
