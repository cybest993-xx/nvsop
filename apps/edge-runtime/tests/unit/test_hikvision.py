"""The Hikvision ISAPI adapter, against a protocol double.

The first adapter targets Hikvision ISAPI (§5.8). What is asserted here is the translation the
adapter owns — an HTTP response into a `Reading`, a transport failure into the conservative
answer, a point write into a bounded request — and nothing about a real camera. A real model
only adds release verification (#40).

**No wire shape is asserted as fact.** Paths, request body and state tokens reach the adapter as
an `IsapiProfile`, because §5.21 keeps a measured value out of the code and the validation matrix
refuses to treat ISAPI endpoints or bodies as universal before a model reports its own. So every
test states the profile it is exercising, and one of them states a deliberately different one:
what is under test is that the adapter uses whatever it was given.

The double stands at the transport seam, which is one HTTP exchange. Replacing an adapter at
its seam is what harness §4 asks for; mocking `urllib` internals would pin the implementation
in place of the behavior.
"""

from __future__ import annotations

import unittest

from harness import measured_capability
from nvsop_contracts import Capability, Unverified

from edge_runtime.connectors.hikvision import (
    CANDIDATE_PROFILE,
    Exchange,
    IsapiConnector,
    IsapiProfile,
    Response,
    TransportFailed,
    TransportRefused,
    TransportTimedOut,
)
from edge_runtime.connectors.port import (
    ConnectorHealth,
    Failed,
    InputPoint,
    OutputPoint,
    PointState,
    Reachability,
    Reading,
    Refused,
    TimedOut,
    Unreachable,
    WriteRefusal,
    Written,
)
from edge_runtime.judgment.model import HostInstant
from edge_runtime.supervisor.inputs import TimeAlignment

ARRIVAL = InputPoint(label="工件到位", address="1")
INTERLOCK = OutputPoint(label="停线联锁", address="1")

NOW = 10.0
TIMEOUT = 2.0

ACTIVE_STATUS = """<?xml version="1.0" encoding="UTF-8"?>
<IOPortStatus xmlns="http://www.hikvision.com/ver20/XMLSchema" version="2.0">
<ioPortID>1</ioPortID>
<ioState>active</ioState>
</IOPortStatus>
"""
"""A response in the shape `CANDIDATE_PROFILE` describes, namespaced as ISAPI documents are.

Synthetic and minimal (harness §4). A real model's body is what gate I1 records; what this
fixture pins is that the adapter reads the profile's element out of a namespaced document.
"""

INACTIVE_STATUS = ACTIVE_STATUS.replace(">active<", ">inactive<")

DEVICE_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<DeviceInfo xmlns="http://www.hikvision.com/ver20/XMLSchema" version="2.0">
<deviceName>camera</deviceName>
</DeviceInfo>
"""

OTHER_FIRMWARE = IsapiProfile(
    input_status_path="/ISAPI/IO/inputs/{address}",
    output_trigger_path="/ISAPI/IO/outputs/{address}/state",
    output_body="<IOState>{state}</IOState>",
    device_info_path="/ISAPI/System/status",
    input_state_element="portState",
    input_tokens=(("high", PointState.ACTIVE), ("low", PointState.INACTIVE)),
    output_tokens=((PointState.ACTIVE, "high"), (PointState.INACTIVE, "low")),
)
"""A second, deliberately different wire shape.

Invented, and that is the point: it exists to show the adapter carries no shape of its own. If
gate I1 finds the real device disagrees with `CANDIDATE_PROFILE`, the repair is a profile like
this one and no code change (§5.21: 硬件差异由数据吸收).
"""

ACCEPTED_EMPTY = Response(body="")
"""A device answering with no body, which a successful trigger does."""


class FakeTransport:
    """One HTTP exchange with the camera, recorded rather than performed."""

    def __init__(self, *, answer: Exchange = ACCEPTED_EMPTY) -> None:
        self._answer = answer
        self.calls: list[tuple[str, str, str | None, float]] = []

    def exchange(
        self, method: str, path: str, /, *, body: str | None = None, timeout: float
    ) -> Exchange:
        self.calls.append((method, path, body, timeout))
        return self._answer


def connector(
    transport: FakeTransport,
    *,
    capability: Capability | None = None,
    profile: IsapiProfile = CANDIDATE_PROFILE,
) -> IsapiConnector:
    return IsapiConnector(
        transport=transport,
        profile=profile,
        capability=measured_capability() if capability is None else capability,
        clock=lambda: NOW,
    )


class ReadingAnInputPointTest(unittest.TestCase):
    """Reading an alarm input's level over whatever path the profile names.

    §5.8 lists the input status endpoint as verified to exist, so `CANDIDATE_PROFILE` starts
    from it. The adapter still reads it off the profile rather than holding it.
    """

    def test_the_path_comes_from_the_profile_with_the_address_substituted(self) -> None:
        transport = FakeTransport(answer=Response(body=ACTIVE_STATUS))

        connector(transport).read(ARRIVAL, timeout=TIMEOUT)

        self.assertEqual(
            [("GET", "/ISAPI/System/IO/inputs/1/status", None, TIMEOUT)],
            transport.calls,
            "the address is the device's own input ID; the semantic label never reaches the "
            "wire: a template references a point by semantic label and never by device "
            "address (§5.8)",
        )

    def test_an_active_port_reads_as_active_on_the_hosts_clock(self) -> None:
        transport = FakeTransport(answer=Response(body=ACTIVE_STATUS))

        self.assertEqual(
            Reading(state=PointState.ACTIVE, at=HostInstant(NOW), alignment=TimeAlignment.ALIGNED),
            connector(transport).read(ARRIVAL, timeout=TIMEOUT),
            "host receipt is this adapter's declared timestamp source, so the instant is the "
            "monotonic reading taken when the answer arrived, and it is aligned by "
            "construction within the declared delivery delay (§5.8)",
        )

    def test_an_inactive_port_reads_as_inactive(self) -> None:
        transport = FakeTransport(answer=Response(body=INACTIVE_STATUS))

        self.assertEqual(
            Reading(
                state=PointState.INACTIVE, at=HostInstant(NOW), alignment=TimeAlignment.ALIGNED
            ),
            connector(transport).read(ARRIVAL, timeout=TIMEOUT),
        )


class AReadThatDidNotSucceedIsNeverALevelTest(unittest.TestCase):
    """§5.2: the optimistic reading is what turns a cut cable into a passing pass.

    Every way a read can fail comes back as `Unreachable`, which reaches judgment as
    `IO_SIGNAL_LOST`. The adapter reports; it does not fill in.
    """

    def test_a_refused_connection_is_unreachable(self) -> None:
        transport = FakeTransport(answer=TransportRefused(detail="connection refused"))

        self.assertEqual(
            Unreachable(detail="connection refused"),
            connector(transport).read(ARRIVAL, timeout=TIMEOUT),
        )

    def test_a_timeout_is_unreachable(self) -> None:
        transport = FakeTransport(answer=TransportTimedOut(after=TIMEOUT))

        self.assertEqual(
            Unreachable(detail="ISAPI 读取超时 2.0s"),
            connector(transport).read(ARRIVAL, timeout=TIMEOUT),
            "a read has no unknown-outcome case the way a write does: either an answer "
            "arrived or none did, and none did",
        )

    def test_an_error_response_is_unreachable(self) -> None:
        transport = FakeTransport(answer=TransportFailed(detail="401 Unauthorized"))

        self.assertEqual(
            Unreachable(detail="401 Unauthorized"),
            connector(transport).read(ARRIVAL, timeout=TIMEOUT),
        )

    def test_a_body_this_adapter_cannot_parse_is_unreachable(self) -> None:
        transport = FakeTransport(answer=Response(body="<IOPortStatus><ioPortID>1</ioPortID>"))

        self.assertEqual(
            Unreachable(detail="ISAPI 响应无法解析为点位状态"),
            connector(transport).read(ARRIVAL, timeout=TIMEOUT),
            "a firmware answering in a shape this build does not know is a reason to "
            "distrust the reading, not to guess at a level (§5.21)",
        )

    def test_a_state_this_adapter_does_not_know_is_unreachable(self) -> None:
        transport = FakeTransport(answer=Response(body=ACTIVE_STATUS.replace("active", "pulse")))

        self.assertEqual(
            Unreachable(detail="ISAPI 未知点位状态 pulse"),
            connector(transport).read(ARRIVAL, timeout=TIMEOUT),
        )


class WritingAnOutputPointTest(unittest.TestCase):
    """Driving an alarm output over whatever path and body the profile names.

    Unlike the read path, no write endpoint appears in §5.8's verified list. That is exactly why
    it is profile data: `CANDIDATE_PROFILE` carries a guess for gate I1 to confirm or correct,
    and this test asserts the adapter sends what it was given rather than that the guess is right.
    """

    def test_the_request_carries_the_state_and_the_timeout(self) -> None:
        transport = FakeTransport(answer=ACCEPTED_EMPTY)

        connector(transport).write(INTERLOCK, PointState.ACTIVE, timeout=TIMEOUT)

        self.assertEqual(
            [
                (
                    "PUT",
                    "/ISAPI/System/IO/outputs/1/trigger",
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<IOPortData version="2.0" '
                    'xmlns="http://www.hikvision.com/ver20/XMLSchema">'
                    "<outputState>active</outputState></IOPortData>",
                    TIMEOUT,
                )
            ],
            transport.calls,
            "this asserts the profile reached the wire unaltered, not that the shape is "
            "correct for any real model — gate I1 answers that, and a different answer is a "
            "different profile rather than a code change",
        )

    def test_an_accepted_write_is_stamped_on_the_hosts_clock(self) -> None:
        transport = FakeTransport(answer=ACCEPTED_EMPTY)

        self.assertEqual(
            Written(at=HostInstant(NOW)),
            connector(transport).write(INTERLOCK, PointState.ACTIVE, timeout=TIMEOUT),
        )

    def test_a_refused_connection_is_a_refusal_because_nothing_was_sent(self) -> None:
        transport = FakeTransport(answer=TransportRefused(detail="connection refused"))

        self.assertEqual(
            Refused(reason=WriteRefusal.POINT_UNREACHABLE, detail="connection refused"),
            connector(transport).write(INTERLOCK, PointState.ACTIVE, timeout=TIMEOUT),
            "the request never left the host, so no relay can have moved; that is what "
            "separates a refusal from a failure",
        )

    def test_a_timeout_stays_a_timeout(self) -> None:
        transport = FakeTransport(answer=TransportTimedOut(after=TIMEOUT))

        self.assertEqual(
            TimedOut(after=TIMEOUT),
            connector(transport).write(INTERLOCK, PointState.ACTIVE, timeout=TIMEOUT),
            "the physical outcome is unknown and must stay distinguishable from a rejection: "
            "the retry rule reads exactly this difference (`writes.py`)",
        )

    def test_a_rejection_by_the_device_is_a_failure(self) -> None:
        transport = FakeTransport(answer=TransportFailed(detail="403 Forbidden"))

        self.assertEqual(
            Failed(detail="403 Forbidden"),
            connector(transport).write(INTERLOCK, PointState.ACTIVE, timeout=TIMEOUT),
        )

    def test_an_unverified_connector_still_reaches_the_device_from_here(self) -> None:
        transport = FakeTransport(answer=ACCEPTED_EMPTY)

        connector(transport, capability=Unverified()).write(
            INTERLOCK, PointState.ACTIVE, timeout=TIMEOUT
        )

        self.assertEqual(
            1,
            len(transport.calls),
            "the capability gate is the dispatcher's (`writes.py`), not the adapter's. Two "
            "copies of one rule is what harness §1 calls a defect, and the adapter is the "
            "wrong place for it: a connection test has to be able to drive an unverified "
            "connector, which is how it stops being unverified",
        )


class ProbingTheConnectorTest(unittest.TestCase):
    """`device_connector.健康状态` is device-level reachability, reported by the host (§5.7).

    A connection test performs a real request and returns success, failure or unverified —
    never a simulated success (control-plane.md §5.3).
    """

    def test_a_device_that_answers_is_reachable(self) -> None:
        transport = FakeTransport(answer=Response(body=DEVICE_INFO))

        self.assertEqual(
            ConnectorHealth(reachability=Reachability.REACHABLE),
            connector(transport).probe(timeout=TIMEOUT),
        )
        self.assertEqual(
            [("GET", CANDIDATE_PROFILE.device_info_path, None, TIMEOUT)], transport.calls
        )

    def test_a_device_that_does_not_answer_is_unreachable_with_the_reason(self) -> None:
        transport = FakeTransport(answer=TransportFailed(detail="401 Unauthorized"))

        self.assertEqual(
            ConnectorHealth(reachability=Reachability.UNREACHABLE, detail="401 Unauthorized"),
            connector(transport).probe(timeout=TIMEOUT),
            "the failure detail is what makes the device page actionable: a credential fault "
            "and an unplugged camera are the same reachability and different repairs",
        )


class TheAdapterHoldsNoWireShapeOfItsOwnTest(unittest.TestCase):
    """§5.21 absorbs a hardware difference as data rather than as a code branch.

    The measured value belongs in the profile, not in this module. These assertions are what
    make that claim checkable: the same adapter, given a different firmware family's shape,
    speaks that shape — so gate I1 finding a different device costs a configuration row and
    no code change, and `CANDIDATE_PROFILE` cannot be mistaken for a fact.
    """

    def test_a_different_firmwares_read_path_and_tokens_are_honoured(self) -> None:
        transport = FakeTransport(
            answer=Response(body="<IOPortStatus><portState>high</portState></IOPortStatus>")
        )

        result = connector(transport, profile=OTHER_FIRMWARE).read(ARRIVAL, timeout=TIMEOUT)

        self.assertEqual(
            Reading(state=PointState.ACTIVE, at=HostInstant(NOW), alignment=TimeAlignment.ALIGNED),
            result,
        )
        self.assertEqual([("GET", "/ISAPI/IO/inputs/1", None, TIMEOUT)], transport.calls)

    def test_a_different_firmwares_write_shape_is_honoured(self) -> None:
        transport = FakeTransport(answer=ACCEPTED_EMPTY)

        connector(transport, profile=OTHER_FIRMWARE).write(
            INTERLOCK, PointState.ACTIVE, timeout=TIMEOUT
        )

        self.assertEqual(
            [("PUT", "/ISAPI/IO/outputs/1/state", "<IOState>high</IOState>", TIMEOUT)],
            transport.calls,
            "the token that reaches a relay is the profile's, because `high` against `active` "
            "is a data difference between firmware families and not a branch",
        )

    def test_the_candidate_profiles_tokens_are_not_read_by_another_profile(self) -> None:
        transport = FakeTransport(answer=Response(body=ACTIVE_STATUS))

        self.assertEqual(
            Unreachable(detail="ISAPI 响应无法解析为点位状态"),
            connector(transport, profile=OTHER_FIRMWARE).read(ARRIVAL, timeout=TIMEOUT),
            "this profile names `portState`, and a document carrying only `ioState` says "
            "nothing it can read — reported rather than guessed at (§5.21)",
        )


class AProfileThatCouldNotWorkIsRefusedWhenBuiltTest(unittest.TestCase):
    """构造期拒绝, because the alternative is discovering it against a live relay.

    A path missing its substitution or a state with no token spelled would fail at the moment
    a 停线联锁 was being driven. Constructing the profile is where a deployment can still fix it.
    """

    def test_a_read_path_without_an_address_placeholder_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            IsapiProfile(
                input_status_path="/ISAPI/System/IO/inputs/status",
                output_trigger_path=CANDIDATE_PROFILE.output_trigger_path,
                output_body=CANDIDATE_PROFILE.output_body,
                device_info_path=CANDIDATE_PROFILE.device_info_path,
                input_state_element=CANDIDATE_PROFILE.input_state_element,
                input_tokens=CANDIDATE_PROFILE.input_tokens,
                output_tokens=CANDIDATE_PROFILE.output_tokens,
            )

    def test_a_write_body_without_a_state_placeholder_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            IsapiProfile(
                input_status_path=CANDIDATE_PROFILE.input_status_path,
                output_trigger_path=CANDIDATE_PROFILE.output_trigger_path,
                output_body="<IOPortData><outputState>active</outputState></IOPortData>",
                device_info_path=CANDIDATE_PROFILE.device_info_path,
                input_state_element=CANDIDATE_PROFILE.input_state_element,
                input_tokens=CANDIDATE_PROFILE.input_tokens,
                output_tokens=CANDIDATE_PROFILE.output_tokens,
            )

    def test_a_state_with_no_token_to_spell_it_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            IsapiProfile(
                input_status_path=CANDIDATE_PROFILE.input_status_path,
                output_trigger_path=CANDIDATE_PROFILE.output_trigger_path,
                output_body=CANDIDATE_PROFILE.output_body,
                device_info_path=CANDIDATE_PROFILE.device_info_path,
                input_state_element=CANDIDATE_PROFILE.input_state_element,
                input_tokens=CANDIDATE_PROFILE.input_tokens,
                output_tokens=((PointState.ACTIVE, "active"),),
            )


if __name__ == "__main__":
    unittest.main()
