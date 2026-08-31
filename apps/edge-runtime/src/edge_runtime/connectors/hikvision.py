"""The Hikvision ISAPI adapter: the first implementation of the connector seam.

A Hikvision camera carries alarm input/output terminals, and ISAPI exposes these endpoints
(§5.8, verified to exist):

```
/ISAPI/System/IO/inputs/<ID>          读写告警输入口配置
/ISAPI/System/IO/inputs/<ID>/status   读告警输入状态
/ISAPI/System/IO/outputs/<ID>/status  读告警输出状态
```

**This module holds no wire shape of its own.** Paths, request body, state tokens and element
names arrive as an `IsapiProfile`, because §5.21 forbids writing a measured value into the code
as a default and the validation matrix forbids treating alarm I/O, push endpoints or the ISAPI
request body as universal before a model reports its own capability. The write endpoint in
particular is absent from the list above. `CANDIDATE_PROFILE` is a starting point for gate I1 to
confirm or correct — a different device costs a profile, not a code change.

Delivery is polling with host-receipt timestamps, which is what can be built while a push
endpoint remains unmeasured (§5.8 待实测). That choice is visible in the capability declaration
rather than assumed here: the declaration is configuration, and while it is `Unverified` the
binding check and the output dispatcher already refuse every role and every write (§5.21).

**This adapter reports; it decides nothing.** A read that did not succeed comes back as
`Unreachable` rather than as a level, because the optimistic reading is what turns a cut cable
into a passing pass (§5.2). A write that never left the host comes back as `Refused` and one
whose answer never arrived as `TimedOut`, because those two have different physical
consequences and the retry rule reads exactly that difference (`writes.py`).

The capability gate is deliberately **not** here: a connection test must be able to drive an
unverified connector — that is how it stops being unverified — and a second copy of the rule the
dispatcher already enforces is what harness §1 calls a defect.

Standard library only (edge-autonomy.md §5.11).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Protocol
from xml.etree import ElementTree

from edge_runtime.connectors.capability import Capability
from edge_runtime.connectors.port import (
    ConnectorHealth,
    Failed,
    InputPoint,
    OutputPoint,
    PointState,
    Reachability,
    Reading,
    ReadResult,
    Refused,
    TimedOut,
    Unreachable,
    WriteOutcome,
    WriteRefusal,
    Written,
)
from edge_runtime.judgment.model import HostInstant
from edge_runtime.supervisor.inputs import TimeAlignment

BODY_LIMIT = 64 * 1024
"""How much of a response body is parsed at all.

An alarm-port status document is a few hundred bytes. The cap is a bound on what an
XML parser is asked to chew: `ElementTree` does not resolve external entities, but it does
expand internal ones, and a camera on the factory network is not a trusted author of
unbounded input.
"""


@dataclass(frozen=True, slots=True)
class IsapiProfile:
    """One device family's actual wire shape: paths, body template, and state tokens.

    **This is where a measurement lands.** §5.21 forbids writing a measured value into the code
    as a default, and the validation matrix forbids treating alarm I/O, push endpoints or the
    ISAPI request body as universal capabilities before a model reports its own. So none of
    these fields has a default: a deployment fills them from what gate I1 found on the model in
    front of it, and filling them is how that finding gets recorded.

    Why the paths are here and not only the body: the matrix's rule covers the endpoints too,
    and the write endpoint in particular is not in §5.8's verified list. Firmware families also
    disagree about the state tokens — `active`/`inactive` against `high`/`low` — which is a data
    difference of exactly the kind §5.21 says must not become a code branch.
    """

    input_status_path: str
    """Read one alarm input's level. `{address}` is substituted with the point's address."""

    output_trigger_path: str
    """Write one alarm output. `{address}` is substituted the same way."""

    output_body: str
    """The write body, with `{state}` substituted by the token below.

    A literal template rather than a serialized document: what a device accepts is byte-exact
    in ways an XML serializer would not preserve.
    """

    device_info_path: str
    """What a connection test asks for — the lightest endpoint that proves credentials and
    reachability together, which is what `device_connector.健康状态` records (§5.7)."""

    input_state_element: str
    """The element carrying an input's level, matched on its local name.

    Configured for the same reason the paths are: it is wire shape. Matched without its
    namespace because that has varied across ISAPI schema revisions while element names have
    not, and a device is not obliged to speak the revision this build was written against.
    """

    input_tokens: tuple[tuple[str, PointState], ...]
    """How this family spells an input level, lowercased. A token outside this set is reported
    as unreadable rather than mapped to the nearer of the two (§5.21).

    A tuple of pairs rather than a mapping so the profile stays hashable and comparable as one
    object, which is what lets a test assert on it whole.
    """

    output_tokens: tuple[tuple[PointState, str], ...]
    """How this family spells an output level. Separate from `input_tokens` because a device is
    not obliged to use one vocabulary for both directions, and because the write direction is
    the one whose token has physical consequences."""

    def __post_init__(self) -> None:
        if "{address}" not in self.input_status_path:
            raise ValueError("input_status_path must carry {address}")
        if "{address}" not in self.output_trigger_path:
            raise ValueError("output_trigger_path must carry {address}")
        if "{state}" not in self.output_body:
            raise ValueError("output_body must carry {state}")
        written = {state for state, _ in self.output_tokens}
        if written != set(PointState):
            raise ValueError(
                f"output_tokens must spell every point state, missing "
                f"{sorted(state.value for state in set(PointState) - written)}"
            )

    def input_state(self, token: str) -> PointState | None:
        """The state this token names, or None when this profile does not know it."""
        for known, state in self.input_tokens:
            if known == token:
                return state
        return None

    def output_token(self, state: PointState) -> str:
        """How to spell this state on the wire. Total by the invariant above."""
        for known, token in self.output_tokens:
            if known is state:
                return token
        raise AssertionError(f"output_tokens is missing {state}, which __post_init__ forbids")


CANDIDATE_PROFILE = IsapiProfile(
    input_status_path="/ISAPI/System/IO/inputs/{address}/status",
    output_trigger_path="/ISAPI/System/IO/outputs/{address}/trigger",
    output_body=(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<IOPortData version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">'
        "<outputState>{state}</outputState></IOPortData>"
    ),
    device_info_path="/ISAPI/System/deviceInfo",
    input_state_element="ioState",
    input_tokens=(("active", PointState.ACTIVE), ("inactive", PointState.INACTIVE)),
    output_tokens=((PointState.ACTIVE, "active"), (PointState.INACTIVE, "inactive")),
)
"""A starting point for gate I1 to confirm or correct. **Not a default, and not a measurement.**

Only `input_status_path` rests on §5.8's verified endpoint list. The write path, the body and
both token sets are guesses at the common ISAPI shape, and the validation matrix says not to
treat them as universal before a model reports its own. Nothing constructs this implicitly: a
deployment names it deliberately, and its capability declaration stays `Unverified` — which
already refuses every judgment role and every point write (§5.21) — until I1 has run.
"""


@dataclass(frozen=True, slots=True)
class Response:
    """The device answered, with this body."""

    body: str


@dataclass(frozen=True, slots=True)
class TransportRefused:
    """The request never left the host: no route, connection refused, DNS failure.

    Separate from `TransportFailed` because for a write it is the difference between "no relay
    moved" and "the device said no" — only the first is safely a refusal.
    """

    detail: str


@dataclass(frozen=True, slots=True)
class TransportTimedOut:
    """The request went out and no answer came back inside the timeout."""

    after: float


@dataclass(frozen=True, slots=True)
class TransportFailed:
    """The device answered, and the answer was an error status."""

    detail: str


Exchange = Response | TransportRefused | TransportTimedOut | TransportFailed
"""One HTTP exchange's result. The four cases exist because the adapter above maps them to
four different point-write outcomes, and collapsing any two would lose a physical distinction."""


class IsapiTransport(Protocol):
    """One HTTP exchange with one camera, including its credentials.

    A seam so the adapter's translation is testable on a bare CPU: everything above this line
    is pure. Credentials live behind it and never above it: they are entered and encrypted on
    the inference host itself (ADR-0008), and nothing here logs a URL or a header.
    """

    def exchange(
        self, method: str, path: str, /, *, body: str | None = None, timeout: float
    ) -> Exchange: ...


class IsapiConnector:
    """One camera's alarm I/O, behind the connector seam.

    Two things arrive from configuration rather than from this class, for the same reason. The
    `capability` declaration is a measured value and not the adapter author's promise (§5.8),
    and the `profile` is the device family's actual wire shape, which §5.21 says must be
    absorbed as data. Neither has a default: a value guessed here would be indistinguishable
    from one that had been measured.
    """

    def __init__(
        self,
        *,
        transport: IsapiTransport,
        profile: IsapiProfile,
        capability: Capability,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._transport = transport
        self._profile = profile
        self._capability = capability
        self._clock = clock

    @property
    def capability(self) -> Capability:
        return self._capability

    def read(self, point: InputPoint, /, *, timeout: float) -> ReadResult:
        """This input point's level, or the fact that we could not get one.

        The instant is read after the answer arrives, which is what a host-receipt timestamp
        source means: the alignment holds by construction inside the declared delivery delay,
        and the polling period is the floor on that delay (§5.8).
        """
        answer = self._transport.exchange(
            "GET",
            self._profile.input_status_path.format(address=point.address),
            timeout=timeout,
        )
        match answer:
            case Response():
                return self._level(answer.body)
            case TransportRefused() | TransportFailed():
                return Unreachable(detail=answer.detail)
            case TransportTimedOut():
                return Unreachable(detail=f"ISAPI 读取超时 {answer.after}s")

    def write(self, point: OutputPoint, state: PointState, /, *, timeout: float) -> WriteOutcome:
        """Drive this output point, reporting what the device did about it.

        Never raises: the caller persists this result alongside the violation it belongs to
        (#19), and an exception crossing that boundary would lose the record of the attempt.
        """
        answer = self._transport.exchange(
            "PUT",
            self._profile.output_trigger_path.format(address=point.address),
            body=self._profile.output_body.format(state=self._profile.output_token(state)),
            timeout=timeout,
        )
        match answer:
            case Response():
                return Written(at=HostInstant(self._clock()))
            case TransportRefused():
                # Nothing was sent, so no relay can have moved. That is what makes this a
                # refusal rather than a failure, and it is what lets a retry proceed.
                return Refused(reason=WriteRefusal.POINT_UNREACHABLE, detail=answer.detail)
            case TransportTimedOut():
                return TimedOut(after=answer.after)
            case TransportFailed():
                return Failed(detail=answer.detail)

    def probe(self, /, *, timeout: float) -> ConnectorHealth:
        """Whether this device is answering, for the center's device page (§5.7).

        A connection test performs a real request and never returns a simulated success
        (control-plane.md §5.3). `UNVERIFIED` is never produced here — it is the state of a
        connector nobody has probed, which is the absence of this call rather than a result.
        """
        answer = self._transport.exchange("GET", self._profile.device_info_path, timeout=timeout)
        match answer:
            case Response():
                return ConnectorHealth(reachability=Reachability.REACHABLE)
            case TransportRefused() | TransportFailed():
                return ConnectorHealth(reachability=Reachability.UNREACHABLE, detail=answer.detail)
            case TransportTimedOut():
                return ConnectorHealth(
                    reachability=Reachability.UNREACHABLE,
                    detail=f"ISAPI 探测超时 {answer.after}s",
                )

    def _level(self, body: str) -> ReadResult:
        """The port state in this document, or why it could not be read.

        The instant is taken here rather than before the exchange, so it is when the answer
        was in hand. A document this build cannot read is `Unreachable`: a firmware answering
        in an unfamiliar shape is a reason to distrust the reading, not to guess a level.
        """
        state = _port_state(body, self._profile)
        if isinstance(state, str):
            return Unreachable(detail=state)
        return Reading(state=state, at=HostInstant(self._clock()), alignment=TimeAlignment.ALIGNED)


def _port_state(body: str, profile: IsapiProfile) -> PointState | str:
    """Parse one status document. Returns the state, or the reason it could not be had.

    The element name and the token vocabulary both come from the profile, because both are
    what a firmware family decides rather than what this code may assume. Namespaces are
    stripped: they have varied across ISAPI schema revisions while element names have not.
    An unknown token is not tolerated — it is reported, never mapped to the nearer state.
    """
    if len(body) > BODY_LIMIT:
        return f"ISAPI 响应超过 {BODY_LIMIT} 字节上限"
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return "ISAPI 响应无法解析为点位状态"

    for element in root.iter():
        if _local(element.tag) != profile.input_state_element:
            continue
        token = (element.text or "").strip().lower()
        state = profile.input_state(token)
        if state is None:
            return f"ISAPI 未知点位状态 {token}"
        return state
    return "ISAPI 响应无法解析为点位状态"


def _local(tag: str) -> str:
    """An element's name without its namespace."""
    return tag.rsplit("}", 1)[-1]
