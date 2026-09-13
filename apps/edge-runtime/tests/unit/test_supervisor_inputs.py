"""Normalizing what arrives into the core's events, so the core never learns a source.

The core consumes normalized observations and knows nothing of chunks, cameras or
connectors (harness §1). This is where that ignorance is produced: an action number from
the inference service, a point-level signal from a connector, and a synthetic health event
from the base's pipeline callback all leave here as core events.

Two of the fourteen reason codes are derived here rather than reported by a caller, because
only this side holds what they are read from: `TIMESTAMP_DISCONTINUITY` from the source
anchor changing underneath us, and `IO_TIME_UNALIGNED` from an external signal that could
not be placed on the video timeline.
"""

from __future__ import annotations

import unittest

from harness import EXTERNAL_START, STEPS

from edge_runtime.judgment import ReasonCode
from edge_runtime.judgment.model import (
    HostInstant,
    Observation,
    StreamHealth,
    ValidityImpaired,
    ValidityRestored,
)
from edge_runtime.stream_health import StreamFact, StreamHealthEvent
from edge_runtime.supervisor.inputs import (
    ActionRecognized,
    ExternalSignal,
    Normalizer,
    StreamHealthObserved,
    TimeAlignment,
    Validity,
    ValidityChanged,
)

ANCHOR = 1_700_000_000.0
"""The base's `first_timestamp`: a wall-clock identity to compare, never an interval to
measure (§2.13). Any value works; what matters is whether it changed."""


def health(
    fact: StreamFact | str, *, at: float = 10.0, anchor: float | None = ANCHOR
) -> StreamHealthObserved:
    return StreamHealthObserved(
        event=StreamHealthEvent(fact=fact, at_monotonic=at, source_anchor=anchor)
    )


class AnActionBecomesAnObservationTest(unittest.TestCase):
    def test_the_action_arrives_with_both_of_its_clocks(self) -> None:
        normalizer = Normalizer()

        events = normalizer.events_for(
            ActionRecognized(
                signal=STEPS[0], at=HostInstant(900.0), source_time=42.0, source_anchor=ANCHOR
            )
        )

        self.assertEqual(
            (Observation(signal=STEPS[0], at=HostInstant(900.0), source_time=42.0),),
            events,
            "the monotonic instant is what the core measures the timeouts on; the source "
            "time rides along for the supervisor's own latency accounting (§5.6)",
        )


class AnExternalSignalTakesTheSamePathAsAnActionTest(unittest.TestCase):
    """§5.8: judgment must not branch on whether a connector is configured.

    A station with no connector and a station with three run the same code, so an external
    signal leaves here as the same event kind an action number does. What differs is only
    that it has no chunk timeline of its own.
    """

    def test_an_aligned_signal_becomes_an_observation_with_no_source_time(self) -> None:
        normalizer = Normalizer()

        events = normalizer.events_for(
            ExternalSignal(
                signal=EXTERNAL_START, at=HostInstant(50.0), alignment=TimeAlignment.ALIGNED
            )
        )

        self.assertEqual(
            (Observation(signal=EXTERNAL_START, at=HostInstant(50.0), source_time=None),),
            events,
            "a point-level signal is a host-clock fact; it sits on no stream's timeline",
        )

    def test_an_unaligned_signal_impairs_the_pass_it_lands_in(self) -> None:
        normalizer = Normalizer()

        events = normalizer.events_for(
            ExternalSignal(
                signal=EXTERNAL_START, at=HostInstant(50.0), alignment=TimeAlignment.UNALIGNED
            )
        )

        self.assertEqual(
            (
                # Before the observation, not after: the observation may be the end signal
                # that closes the instance, and the closing gate has to see this first
                # (§5.8 — an unaligned signal must never produce a failing verdict).
                ValidityImpaired(reason=ReasonCode.IO_TIME_UNALIGNED),
                Observation(signal=EXTERNAL_START, at=HostInstant(50.0), source_time=None),
                # Momentary rather than lasting: this signal was unaligned, which taints
                # the pass it landed in. Drift that persists re-reports itself on the next
                # signal, so a later pass is not condemned by an earlier one.
                ValidityRestored(reason=ReasonCode.IO_TIME_UNALIGNED),
            ),
            events,
        )


class StreamHealthBecomesTheValidityRecordTest(unittest.TestCase):
    """§2.4: these facts exist in the pipeline callback and the base discards them.

    E4 gave them a channel. Here they become the record the closing gate reads, which is
    what keeps "we could not see" from being reported as "the operator did not do it".
    """

    def test_a_source_error_impairs_until_something_says_otherwise(self) -> None:
        normalizer = Normalizer()

        events = normalizer.events_for(health(StreamFact.SOURCE_ERROR))

        self.assertEqual((ValidityImpaired(reason=ReasonCode.STREAM_LOST),), events)
        self.assertEqual(StreamHealth.LOST, normalizer.stream_health)

    def test_inference_timeout_preserves_its_specific_reason_until_recovery(self) -> None:
        normalizer = Normalizer()

        impaired = normalizer.events_for(health(StreamFact.INFERENCE_TIMEOUT))
        restored = normalizer.events_for(health(StreamFact.DELIVERING))

        self.assertEqual(
            (ValidityImpaired(reason=ReasonCode.INFERENCE_TIMEOUT),),
            impaired,
        )
        self.assertEqual(
            (ValidityRestored(reason=ReasonCode.INFERENCE_TIMEOUT),),
            restored,
        )
        self.assertEqual(StreamHealth.HEALTHY, normalizer.stream_health)

    def test_delivering_after_an_error_restores_observation(self) -> None:
        normalizer = Normalizer()
        normalizer.events_for(health(StreamFact.SOURCE_ERROR))

        events = normalizer.events_for(health(StreamFact.DELIVERING))

        self.assertEqual(
            (ValidityRestored(reason=ReasonCode.STREAM_LOST),),
            events,
            "recovery reaches us as SOURCE_ERROR followed by DELIVERING (§5.11). That is a "
            "premise about the base's bus messages, verified by fault injection in P4",
        )
        self.assertEqual(StreamHealth.HEALTHY, normalizer.stream_health)

    def test_delivering_with_nothing_wrong_changes_nothing(self) -> None:
        normalizer = Normalizer()

        self.assertEqual((), normalizer.events_for(health(StreamFact.DELIVERING)))
        self.assertEqual(StreamHealth.HEALTHY, normalizer.stream_health)

    def test_a_repeated_source_error_is_reported_once(self) -> None:
        normalizer = Normalizer()
        normalizer.events_for(health(StreamFact.SOURCE_ERROR))

        self.assertEqual((), normalizer.events_for(health(StreamFact.SOURCE_ERROR)))

    def test_the_stream_ending_impairs_it(self) -> None:
        normalizer = Normalizer()

        events = normalizer.events_for(health(StreamFact.STREAM_ENDED))

        self.assertEqual((ValidityImpaired(reason=ReasonCode.STREAM_LOST),), events)

    def test_a_fact_this_build_cannot_classify_impairs_it(self) -> None:
        normalizer = Normalizer()

        events = normalizer.events_for(health("camera_lens_obstructed"))

        self.assertEqual(
            (ValidityImpaired(reason=ReasonCode.STREAM_LOST),),
            events,
            "an unknown fact from a newer producer is read conservatively (ADR-0003, "
            "§5.21): reading it as healthy would let an unobservable pass close as passing",
        )
        self.assertEqual(StreamHealth.LOST, normalizer.stream_health)

    def test_observation_alone_does_not_clear_stream_loss(self) -> None:
        normalizer = Normalizer()
        normalizer.events_for(health(StreamFact.SOURCE_ERROR))

        normalizer.events_for(
            ActionRecognized(
                signal=STEPS[0], at=HostInstant(20.0), source_time=1.0, source_anchor=ANCHOR
            )
        )

        self.assertEqual(
            StreamHealth.LOST,
            normalizer.stream_health,
            "a chunk arriving is not proof the window was observable: treating it as "
            "recovery is the optimistic reading §5.2 forbids, and it would make an "
            "impairment unable to survive the very chunks that arrive while degraded",
        )


class TheSourceTimelineGoingBackToZeroIsDetectedByComparingAnchorsTest(unittest.TestCase):
    """§5.11: re-anchoring is not an event, it is a change in a field every event carries.

    The base re-reads `first_timestamp` after a reconnect, so the chunk timeline restarts
    underneath us. Nothing announces it; the supervisor notices by comparing.
    """

    def test_the_first_anchor_seen_is_not_a_discontinuity(self) -> None:
        normalizer = Normalizer()

        events = normalizer.events_for(
            ActionRecognized(
                signal=STEPS[0], at=HostInstant(10.0), source_time=1.0, source_anchor=ANCHOR
            )
        )

        self.assertEqual(
            (Observation(signal=STEPS[0], at=HostInstant(10.0), source_time=1.0),), events
        )

    def test_a_changed_anchor_impairs_the_pass_in_flight(self) -> None:
        normalizer = Normalizer()
        normalizer.events_for(
            ActionRecognized(
                signal=STEPS[0], at=HostInstant(10.0), source_time=1.0, source_anchor=ANCHOR
            )
        )

        events = normalizer.events_for(
            ActionRecognized(
                signal=STEPS[1],
                at=HostInstant(20.0),
                source_time=1.0,
                source_anchor=ANCHOR + 30.0,
            )
        )

        self.assertEqual(
            (
                ValidityImpaired(reason=ReasonCode.TIMESTAMP_DISCONTINUITY),
                Observation(signal=STEPS[1], at=HostInstant(20.0), source_time=1.0),
                ValidityRestored(reason=ReasonCode.TIMESTAMP_DISCONTINUITY),
            ),
            events,
            "the pass that straddles the re-anchoring cannot be concluded on, but the one "
            "after it starts on the new timeline and is fine",
        )

    def test_the_new_anchor_becomes_the_one_compared_against(self) -> None:
        normalizer = Normalizer()
        for anchor in (ANCHOR, ANCHOR + 30.0):
            normalizer.events_for(
                ActionRecognized(
                    signal=STEPS[0], at=HostInstant(10.0), source_time=1.0, source_anchor=anchor
                )
            )

        events = normalizer.events_for(
            ActionRecognized(
                signal=STEPS[1], at=HostInstant(30.0), source_time=2.0, source_anchor=ANCHOR + 30.0
            )
        )

        self.assertEqual(
            (Observation(signal=STEPS[1], at=HostInstant(30.0), source_time=2.0),),
            events,
            "one reconnect is one discontinuity, not one per chunk that follows it",
        )

    def test_a_health_event_re_anchoring_reports_both_facts(self) -> None:
        normalizer = Normalizer()
        normalizer.events_for(health(StreamFact.SOURCE_ERROR))

        events = normalizer.events_for(health(StreamFact.DELIVERING, anchor=ANCHOR + 30.0))

        self.assertEqual(
            (
                ValidityImpaired(reason=ReasonCode.TIMESTAMP_DISCONTINUITY),
                ValidityRestored(reason=ReasonCode.TIMESTAMP_DISCONTINUITY),
                ValidityRestored(reason=ReasonCode.STREAM_LOST),
            ),
            events,
            "a reconnect both restores the stream and moves the timeline; the pass in "
            "flight keeps the discontinuity on its record either way",
        )

    def test_a_health_event_without_an_anchor_compares_nothing(self) -> None:
        normalizer = Normalizer()
        normalizer.events_for(health(StreamFact.DELIVERING, anchor=None))

        events = normalizer.events_for(health(StreamFact.SOURCE_ERROR))

        self.assertEqual(
            (ValidityImpaired(reason=ReasonCode.STREAM_LOST),),
            events,
            "a malformed event carries no anchor, and there is nothing to compare against",
        )


class WhatTheCallerKnowsFirstHandItReportsTest(unittest.TestCase):
    """The reachability and load facts the supervisor's own machinery holds.

    The SSE loop knows the backend stopped answering; the connector runtime knows a point
    went unreachable. They pass the fact through here so that the core's event types stay
    behind one seam rather than being constructed in every part of the runtime.
    """

    def test_an_impairment_the_caller_reports_reaches_the_core_as_one(self) -> None:
        normalizer = Normalizer()

        events = normalizer.events_for(
            ValidityChanged(reason=ReasonCode.INFERENCE_BACKEND_UNREACHABLE, now=Validity.IMPAIRED)
        )

        self.assertEqual(
            (ValidityImpaired(reason=ReasonCode.INFERENCE_BACKEND_UNREACHABLE),), events
        )

    def test_a_restoration_the_caller_reports_reaches_the_core_as_one(self) -> None:
        normalizer = Normalizer()

        events = normalizer.events_for(
            ValidityChanged(reason=ReasonCode.IO_SIGNAL_LOST, now=Validity.RESTORED)
        )

        self.assertEqual((ValidityRestored(reason=ReasonCode.IO_SIGNAL_LOST),), events)

    def test_the_caller_cannot_report_a_violation_as_an_impairment(self) -> None:
        normalizer = Normalizer()

        with self.assertRaises(ValueError):
            normalizer.events_for(
                ValidityChanged(reason=ReasonCode.MISSED_STEP, now=Validity.IMPAIRED)
            )


if __name__ == "__main__":
    unittest.main()
