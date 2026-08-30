"""The stream-health channel: the one thing the hook inside `vendor/` is allowed to call.

The base's pipeline callback already sees the facts that decide whether we could observe at
all — the source failed, the pipeline is playing again, the stream ended — and discards
every one of them (§2.4). E4 appends one call at that callback so they reach the supervisor
over the SSE the base already serves, as a synthetic chunk marked by one explicit key
(§5.11). Nothing on the base's computation path changes: this module only adds output.

Both halves of that wire shape live here, because the producer runs inside the base
container and the consumer runs in the supervisor. They upgrade independently, so an
unknown fact must survive decoding as itself rather than be dropped (ADR-0003), and a fact
this module cannot classify counts as impairing rather than healthy.

Standard library only, and this file is bound to that harder than the judgment core is: it
executes inside the DeepStream container, whose interpreter the base image sets (§5.11).
It also imports nothing from `edge_runtime` — the hook in `vendor/` reaches exactly this
module and no further, which is what keeps the patch surface at one call.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from time import monotonic
from typing import Any, Protocol

STREAM_HEALTH_KEY = "stream_health"
"""The key that marks a synthetic chunk as ours.

A key rather than a sentinel value: a consumer branches on whether the key is present,
which is safer than recognizing a magic number and does not collide with the base's own
`chunk_idx=-1` end-of-stream chunk (§5.11).
"""

DETAIL_LIMIT = 200
"""How much of the base's message text rides along. It is triage detail on one SSE frame,
not a payload, and its length is not ours to trust."""


class StreamFact(Enum):
    """What the base's pipeline callback can actually observe about one stream.

    Only observable facts are named. "Reconnecting" is deliberately absent: the base sets
    `init-rtsp-reconnect-interval` on the source and DeepStream retries inside the element
    without announcing it on the bus (§2.4), so a `RECONNECTING` member would be invented
    rather than observed. Recovery reaches the supervisor as `SOURCE_ERROR` followed by
    `DELIVERING`, which is the same information without the fiction.

    Timeline re-zeroing is not a member either, for the opposite reason: it is not an event
    but a change in `source_anchor`, which every event carries, so the supervisor reads it
    by comparing anchors rather than by being told.
    """

    SOURCE_ERROR = "source_error"
    DELIVERING = "delivering"
    STREAM_ENDED = "stream_ended"
    """Best-effort, unlike the other two.

    The base's VLM thread puts its own `None` sentinel on the same queue when it finishes
    (`ds_sop_process.py:1175`), and the dispatch loop stops forwarding at that sentinel. An
    end-of-stream event racing behind it is dropped. Nothing is lost by that: the SSE
    response itself ends, which the supervisor sees directly, and stream termination is
    already the chunk-silence timer's job (§5.11). `SOURCE_ERROR` and `DELIVERING` arrive
    mid-stream, well before any sentinel, so the facts judgment depends on do not race.
    """

    @property
    def impairs_observation(self) -> bool:
        """Whether this fact says the observation window was not usable.

        Classification lives with the fact rather than in the supervisor, so a member
        cannot be added without deciding what it means for judgment — the same reason
        `ReasonCode` carries its own verdict class.
        """
        return self is not StreamFact.DELIVERING


class HealthSink(Protocol):
    """Where the event goes: `_vlm_response_queue`, the one usable sink of three (§5.11).

    A protocol rather than `queue.Queue`, so this module states what it needs — one
    `put` — instead of naming a type it never constructs. `_boundary_queue` is unusable
    because `clip_post_process` unpacks its items positionally, and `_chunk_queue` is
    unusable because a VLM inference would be run against the event.
    """

    def put(self, item: dict[str, Any], /) -> None: ...


@dataclass(frozen=True, slots=True)
class StreamHealthEvent:
    """One statement about whether a stream could be observed.

    Not an observation: an observation states what happened at the station, this states
    whether we could see it at all (CONTEXT.md). It therefore never enters the missing-step
    comparison and never reaches the observation storage or reporting path.
    """

    fact: StreamFact | str
    """The fact, or the raw wire string when this build does not know it.

    A `str` here means the producer is newer than this consumer. Use
    `isinstance(event.fact, StreamFact)` to tell them apart; `impairs_observation` already
    reads an unknown fact conservatively, so most callers do not need to.
    """

    at_monotonic: float | None
    """When the callback saw it, on the host's monotonic clock.

    The same clock the judgment core measures the idle timeout and step deadline on. Taken
    to be comparable across the two processes because Linux's `CLOCK_MONOTONIC` counts from
    boot rather than per process — a **premise, not a measured fact**, registered in
    measured-facts.md §2.13 along with the one thing that would break it (a container time
    namespace) and the fallback if it does. None only when a malformed event arrived, which
    is itself a reason to distrust what we are seeing.
    """

    source_anchor: float | None = None
    """The base's `first_timestamp`: the wall-clock moment it anchored the source timeline.

    Wall clock, not monotonic — the base reads `time.time()` for it — so it is an identity
    to compare, never an interval to measure. The base re-anchors it after a reconnect, and
    every chunk carries the same field, which is how the supervisor detects that the source
    timeline went back to zero underneath it.
    """

    stream_id: str = ""
    detail: str = ""

    @property
    def impairs_observation(self) -> bool:
        """Whether this event says the observation window was not usable."""
        if isinstance(self.fact, StreamFact):
            return self.fact.impairs_observation
        # A fact this build cannot classify. Conservative rather than optimistic (§5.21):
        # reading it as healthy would let an unobservable pass close as passing.
        return True

    def as_chunk(self) -> dict[str, Any]:
        """The synthetic chunk that goes on the queue, carrying only our own key."""
        fact = self.fact.value if isinstance(self.fact, StreamFact) else self.fact
        return {
            STREAM_HEALTH_KEY: {
                "fact": fact,
                "stream_id": self.stream_id,
                "at_monotonic": self.at_monotonic,
                "source_anchor": self.source_anchor,
                "detail": self.detail,
            }
        }


def note_pipeline_message(
    message: object,
    *,
    sink: HealthSink,
    stream_id: str,
    source_anchor: float,
    clock: Callable[[], float] = monotonic,
) -> StreamHealthEvent | None:
    """The hook's entry point: turn one pipeline message into an event, or into nothing.

    Called from the base's `on_message` after its own branches, once per message. Returns
    the event that was queued, or None when the message says nothing about health — the
    base's pre-roll state transitions are most messages, and reporting them would mark a
    healthy stream impaired every time a pipeline starts.

    The message is duck-typed. Importing `pyservicemaker` to name its classes would tie
    this module to the DeepStream container and make it untestable on a bare CPU, and what
    the hook depends on is the attributes rather than the class identities. That dependency
    is pinned from the other side, by the contract suite asserting the base still imports
    those message types (§5.9).
    """
    fact = _classify(message)
    if fact is None:
        return None
    event = StreamHealthEvent(
        fact=fact,
        at_monotonic=clock(),
        source_anchor=source_anchor,
        stream_id=stream_id,
        detail=_detail(message),
    )
    sink.put(event.as_chunk())
    return event


def _classify(message: object) -> StreamFact | None:
    """Which fact this message carries, if any.

    Read off the message's shape rather than its type: a state transition carries
    `new_state`, and end-of-stream carries nothing but its name. The base's own callback
    branches on `isinstance` against the classes it imported, which this module cannot do
    without importing the container's world.
    """
    state = getattr(message, "new_state", None)
    if state is not None:
        return _STATE_FACTS.get(getattr(state, "name", ""))

    name = type(message).__name__
    if "EOS" in name:
        return StreamFact.STREAM_ENDED
    if "Error" in name or getattr(message, "error", None) is not None:
        return StreamFact.SOURCE_ERROR
    return None


_STATE_FACTS = {
    # `INVALID` is how the base's own callback learns the pipeline failed: it sets
    # `_started_event` there so a caller waiting to start is released (`ds_sop_process.py`).
    "INVALID": StreamFact.SOURCE_ERROR,
    "PLAYING": StreamFact.DELIVERING,
}
"""Pipeline states that are health facts. `READY`, `PAUSED` and `NULL` are the base's
start-up and teardown bookkeeping, and are not."""


def _detail(message: object) -> str:
    """The base's message text, bounded, and never at the cost of the event.

    A message whose `__str__` raises must still produce its fact: losing the event means
    the supervisor concludes on a stream it could not see, which is the one outcome §5.2
    forbids. The detail is triage convenience, so it is what gets dropped.
    """
    try:
        return str(message)[:DETAIL_LIMIT]
    except Exception:
        return ""


def decode(chunk: Mapping[str, Any]) -> StreamHealthEvent | None:
    """Read one SSE chunk as a health event, or None when it is an ordinary chunk.

    The supervisor calls this on everything the stream delivers, so a real chunk of work
    must come back as "not one of ours" rather than as a malformed event. A chunk that
    *is* ours but does not parse comes back as a source error: producer and consumer
    disagreeing is itself a reason to distrust what we are seeing, and raising here would
    put a decoding failure into the supervisor's event loop.
    """
    if STREAM_HEALTH_KEY not in chunk:
        return None
    payload = chunk[STREAM_HEALTH_KEY]
    if not isinstance(payload, Mapping):
        return StreamHealthEvent(fact=StreamFact.SOURCE_ERROR, at_monotonic=None)
    return StreamHealthEvent(
        fact=_decode_fact(payload.get("fact")),
        at_monotonic=_number(payload.get("at_monotonic")),
        source_anchor=_number(payload.get("source_anchor")),
        stream_id=str(payload.get("stream_id", "")),
        detail=str(payload.get("detail", ""))[:DETAIL_LIMIT],
    )


def _decode_fact(raw: object) -> StreamFact | str:
    """A known fact as its member, an unknown one as itself (ADR-0003)."""
    if not isinstance(raw, str) or not raw:
        return StreamFact.SOURCE_ERROR
    try:
        return StreamFact(raw)
    except ValueError:
        return raw


def _number(raw: object) -> float | None:
    return float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None
