"""The stream-health channel: what the hook inside `vendor/` puts on the wire (§5.11).

The base's pipeline callback sees source errors, reconnects and end-of-stream and throws
all of it away (§2.4). E4 appends one call at that callback so those facts reach the
supervisor over the SSE the base already serves. This file pins both halves of that wire
shape, because the producer runs in the base container and the consumer runs in the
supervisor: two processes that upgrade independently, so the shape is a contract even
though one module owns both ends.

No pyservicemaker here. The hook duck-types the message it is handed, so a test hands it a
stand-in with the same attributes — which is also the honest test, since what the hook
depends on is those attributes and not the base's class identities.
"""

from __future__ import annotations

import queue
import unittest
from dataclasses import dataclass
from typing import Any

from edge_runtime.stream_health import (
    STREAM_HEALTH_KEY,
    StreamFact,
    StreamHealthEvent,
    decode,
    note_pipeline_message,
)


@dataclass(frozen=True)
class FakeState:
    """Stands in for `pyservicemaker.PipelineState`, whose members compare by name."""

    name: str


@dataclass(frozen=True)
class FakeStateTransitionMessage:
    """Stands in for `StateTransitionMessage`: what the hook reads is `new_state`."""

    new_state: FakeState

    def __str__(self) -> str:
        return f"StateTransitionMessage({self.new_state.name})"


class FakeEOSMessage:
    """Stands in for `EOSMessage`, which carries no state at all.

    Named to mirror the base's class exactly. The hook recognizes end-of-stream by the
    class name, so a fake spelled differently would test a different thing — and the base
    importing that name is pinned by the contract suite (§5.9).
    """


class Recorder:
    """A sink that records instead of feeding the base's queue."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def put(self, item: dict[str, Any], /) -> None:
        self.items.append(item)


def transition(state: str) -> FakeStateTransitionMessage:
    return FakeStateTransitionMessage(new_state=FakeState(name=state))


def note(
    message: object,
    *,
    sink: Recorder | None = None,
    stream_id: str = "camera-1",
    source_anchor: float = 1_700_000_000.5,
    now: float = 42.0,
) -> tuple[Recorder, StreamHealthEvent | None]:
    """Hand one message to the hook, with the clock stated rather than read."""
    recorder = sink if sink is not None else Recorder()
    event = note_pipeline_message(
        message,
        sink=recorder,
        stream_id=stream_id,
        source_anchor=source_anchor,
        clock=lambda: now,
    )
    return recorder, event


class FactsTheCallbackCanSeeTest(unittest.TestCase):
    """Which pipeline messages become which health fact.

    Only what the callback actually observes is named. "Reconnecting" is deliberately
    absent: the base sets `init-rtsp-reconnect-interval` on the source (§2.4) and
    DeepStream retries inside the element without a bus message, so a fact called
    "reconnecting" would be invented rather than observed. What the supervisor sees
    instead is `SOURCE_ERROR` followed by `DELIVERING`, which is the recovery.
    """

    def test_an_invalid_pipeline_is_a_source_error(self) -> None:
        _, event = note(transition("INVALID"))

        self.assertIsNotNone(event)
        assert event is not None
        self.assertIs(StreamFact.SOURCE_ERROR, event.fact)

    def test_playing_is_the_stream_delivering(self) -> None:
        _, event = note(transition("PLAYING"))

        assert event is not None
        self.assertIs(StreamFact.DELIVERING, event.fact)

    def test_end_of_stream_is_its_own_fact(self) -> None:
        _, event = note(FakeEOSMessage())

        assert event is not None
        self.assertIs(StreamFact.STREAM_ENDED, event.fact)

    def test_pre_roll_states_are_not_health_facts(self) -> None:
        # READY and PAUSED are the base's own start-up bookkeeping. Reporting them would
        # make the supervisor mark a healthy stream impaired every time a pipeline starts.
        for state in ("READY", "PAUSED", "NULL"):
            with self.subTest(state=state):
                recorder, event = note(transition(state))

                self.assertIsNone(event)
                self.assertEqual([], recorder.items)

    def test_an_unrecognized_message_puts_nothing_on_the_wire(self) -> None:
        recorder, event = note(object())

        self.assertIsNone(event)
        self.assertEqual([], recorder.items)


class WireShapeTest(unittest.TestCase):
    """What the supervisor receives, asserted as a whole object.

    A synthetic chunk carrying exactly one key. The base's own chunks never carry it, and
    the base's consumers reach for their keys with `.get()`, so the event rides the SSE
    without any of them reading it as a chunk of work (§5.11).
    """

    def test_the_event_is_one_explicitly_keyed_synthetic_chunk(self) -> None:
        recorder, _ = note(transition("INVALID"), stream_id="camera-7", now=931.25)

        self.assertEqual(
            [
                {
                    STREAM_HEALTH_KEY: {
                        "fact": "source_error",
                        "stream_id": "camera-7",
                        "at_monotonic": 931.25,
                        "source_anchor": 1_700_000_000.5,
                        "detail": "StateTransitionMessage(INVALID)",
                    }
                }
            ],
            recorder.items,
        )

    def test_no_sentinel_value_marks_the_event(self) -> None:
        # §5.11: the marker is a key, not a magic number. `chunk_idx=-1` is the base's own
        # end-of-stream sentinel, and reusing it would make our event indistinguishable
        # from that one.
        recorder, _ = note(transition("INVALID"))

        self.assertEqual({STREAM_HEALTH_KEY}, set(recorder.items[0]))

    def test_the_base_queue_satisfies_the_sink(self) -> None:
        # The real sink is `self._vlm_response_queue`, a `queue.Queue`. Asserted here so
        # the protocol the hook declares is checked against the actual type it is handed,
        # rather than only against this file's recorder.
        sink: queue.Queue[dict[str, Any]] = queue.Queue()

        event = note_pipeline_message(
            FakeEOSMessage(),
            sink=sink,
            stream_id="camera-1",
            source_anchor=0.0,
            clock=lambda: 1.0,
        )

        assert event is not None
        self.assertEqual(event.as_chunk(), sink.get_nowait())

    def test_a_message_detail_is_carried_but_bounded(self) -> None:
        # Whatever the base's message stringifies to is useful for triage and is not ours
        # to trust: it rides one SSE frame, so it is capped.
        class Chatty:
            def __init__(self) -> None:
                self.error = "x" * 500

            def __str__(self) -> str:
                return "y" * 500

        recorder, event = note(Chatty())

        assert event is not None
        self.assertIs(StreamFact.SOURCE_ERROR, event.fact)
        self.assertEqual(200, len(event.detail))
        self.assertEqual("y" * 200, recorder.items[0][STREAM_HEALTH_KEY]["detail"])

    def test_a_message_that_cannot_be_stringified_still_reports_its_fact(self) -> None:
        # The detail is a convenience; the fact is the contract. A message whose `__str__`
        # raises must not cost us the event, because losing it means the supervisor
        # concludes on a stream it could not see.
        class Hostile:
            error = "source error"

            def __str__(self) -> str:
                raise RuntimeError("no")

        _, event = note(Hostile())

        assert event is not None
        self.assertIs(StreamFact.SOURCE_ERROR, event.fact)
        self.assertEqual("", event.detail)


class DecodeTest(unittest.TestCase):
    """The supervisor's half. Same module, so the two halves cannot drift apart."""

    def test_a_health_event_round_trips(self) -> None:
        recorder, event = note(transition("INVALID"))

        self.assertEqual(event, decode(recorder.items[0]))

    def test_an_ordinary_chunk_is_not_a_health_event(self) -> None:
        # The supervisor calls this on every chunk the SSE delivers, so the base's real
        # chunks must come back as "not one of ours" rather than as a malformed one.
        self.assertIsNone(
            decode({"chunk_idx": 3, "start_time": 6.0, "end_time": 9.0, "response": "(1) step 1"})
        )

    def test_the_base_end_of_stream_sentinel_is_not_a_health_event(self) -> None:
        self.assertIsNone(decode({"chunk_idx": -1, "start_time": 0, "end_time": 0}))

    def test_an_unknown_fact_survives_decoding_as_its_raw_value(self) -> None:
        # The base container and the supervisor upgrade independently, so a fact this
        # supervisor has never heard of must arrive as itself (ADR-0003). The alternative
        # — dropping it — would turn a stream we cannot judge into a silent pass.
        decoded = decode({STREAM_HEALTH_KEY: {"fact": "sensor_on_fire", "at_monotonic": 5.0}})

        assert decoded is not None
        self.assertEqual("sensor_on_fire", decoded.fact)
        self.assertNotIsInstance(decoded.fact, StreamFact)

    def test_a_malformed_event_is_reported_as_a_source_error(self) -> None:
        # Missing or non-numeric fields mean the producer and this decoder disagree, which
        # is itself a reason not to trust what we are seeing. Read conservatively rather
        # than raising into the supervisor's event loop.
        decoded = decode({STREAM_HEALTH_KEY: {"at_monotonic": "not a number"}})

        assert decoded is not None
        self.assertIs(StreamFact.SOURCE_ERROR, decoded.fact)
        self.assertIsNone(decoded.at_monotonic)

    def test_a_non_mapping_payload_is_reported_as_a_source_error(self) -> None:
        decoded = decode({STREAM_HEALTH_KEY: "lost"})

        assert decoded is not None
        self.assertIs(StreamFact.SOURCE_ERROR, decoded.fact)


class ImpairmentTest(unittest.TestCase):
    """Which facts say the observation window was not usable.

    The judgment core takes an impairment as a reason code (`ValidityImpaired`), and this
    is where a fact becomes one. It lives beside the wire shape rather than in the
    supervisor, so adding a fact forces the same change to classify it — the same reason
    `ReasonCode` carries its own verdict class.
    """

    def test_a_source_error_impairs_observation(self) -> None:
        self.assertTrue(StreamFact.SOURCE_ERROR.impairs_observation)
        self.assertTrue(StreamFact.STREAM_ENDED.impairs_observation)

    def test_delivering_does_not(self) -> None:
        self.assertFalse(StreamFact.DELIVERING.impairs_observation)

    def test_an_unknown_fact_is_treated_as_impairing(self) -> None:
        # Conservative by construction (§5.21): a fact we cannot classify must not be
        # read as "the stream is fine".
        decoded = decode({STREAM_HEALTH_KEY: {"fact": "sensor_on_fire", "at_monotonic": 5.0}})

        assert decoded is not None
        self.assertTrue(decoded.impairs_observation)


if __name__ == "__main__":
    unittest.main()
