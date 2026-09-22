"""Writing an output point: the idempotency key, the timeout, and the structured outcome.

Driving a 停线联锁 relay is the highest-impact thing this system does, and it happens while
the center may be unreachable (§5.7). So the three properties asserted here are the ones that
make a physical write safe to retry after a restart: the same key never produces a second
physical write intent, every refusal states a machine-readable reason instead of guessing, and
every attempt leaves a diagnostic event naming the operator and the target point (Q37).
"""

from __future__ import annotations

import unittest

from harness import measured_capability
from nvsop_contracts import Capability, Unverified

from edge_runtime.connectors.port import (
    ConnectorHealth,
    Failed,
    InputPoint,
    OutputPoint,
    PointState,
    Reachability,
    ReadResult,
    Refused,
    TimedOut,
    Unreachable,
    WriteOutcome,
    WriteRefusal,
    Written,
)
from edge_runtime.connectors.writes import (
    InMemoryWriteLedger,
    OutputDispatcher,
    WriteAttempted,
    WriteRequest,
)
from edge_runtime.judgment.model import HostInstant

INTERLOCK = OutputPoint(label="停线联锁", address="1")

ACCEPTED = Written(at=HostInstant(10.0))


def request(key: str = "disposal-7", *, state: PointState = PointState.ACTIVE) -> WriteRequest:
    return WriteRequest(
        point=INTERLOCK,
        state=state,
        key=key,
        actor="supervisor:station-3",
        timeout=2.0,
        capability_budget=0.2,
    )


class RecordingConnector:
    """A connector double that records what it was asked to drive.

    A double at the adapter seam rather than a mock through the call chain (harness §4): what
    is under test is the dispatcher's own behavior, and what matters is whether a physical
    write intent reached the adapter at all.
    """

    def __init__(
        self,
        *,
        capability: Capability | None = None,
        outcome: WriteOutcome = ACCEPTED,
    ) -> None:
        self.capability: Capability = measured_capability() if capability is None else capability
        self.writes: list[tuple[OutputPoint, PointState]] = []
        self._outcome = outcome

    def read(self, point: InputPoint, /, *, timeout: float) -> ReadResult:
        return Unreachable(detail="not under test")

    def probe(self, /, *, timeout: float) -> ConnectorHealth:
        return ConnectorHealth(reachability=Reachability.REACHABLE)

    def write(self, point: OutputPoint, state: PointState, /, *, timeout: float) -> WriteOutcome:
        self.writes.append((point, state))
        return self._outcome


class RaisingConnector(RecordingConnector):
    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self._error = error

    def write(self, point: OutputPoint, state: PointState, /, *, timeout: float) -> WriteOutcome:
        self.writes.append((point, state))
        raise self._error


def dispatcher(
    connector: RecordingConnector,
) -> tuple[OutputDispatcher, InMemoryWriteLedger, list[WriteAttempted]]:
    ledger = InMemoryWriteLedger()
    events: list[WriteAttempted] = []
    return (
        OutputDispatcher(connector=connector, ledger=ledger, diagnostics=events.append),
        ledger,
        events,
    )


class TheSameKeyDrivesTheDeviceOnceTest(unittest.TestCase):
    """#41: 相同幂等键重试不会重复产生物理写入意图.

    The retry is not hypothetical: reporting is idempotent-upsert and at-least-once (§5.7), so
    a disposal that was dispatched before a restart is dispatched again after it.
    """

    def test_the_first_write_reaches_the_device(self) -> None:
        connector = RecordingConnector()
        dispatch, _, _ = dispatcher(connector)

        self.assertEqual(ACCEPTED, dispatch.write(request()))
        self.assertEqual([(INTERLOCK, PointState.ACTIVE)], connector.writes)

    def test_the_same_key_returns_the_recorded_outcome_without_writing_again(self) -> None:
        connector = RecordingConnector()
        dispatch, _, _ = dispatcher(connector)
        dispatch.write(request())

        self.assertEqual(ACCEPTED, dispatch.write(request()))
        self.assertEqual(
            [(INTERLOCK, PointState.ACTIVE)],
            connector.writes,
            "one physical write intent, and the caller gets the same answer it got before",
        )

    def test_a_different_key_is_a_different_action(self) -> None:
        connector = RecordingConnector()
        dispatch, _, _ = dispatcher(connector)
        dispatch.write(request("disposal-7"))

        dispatch.write(request("disposal-8"))

        self.assertEqual(
            [(INTERLOCK, PointState.ACTIVE), (INTERLOCK, PointState.ACTIVE)], connector.writes
        )

    def test_a_failed_write_may_be_retried(self) -> None:
        connector = RecordingConnector(outcome=Failed(detail="401 unauthorized"))
        dispatch, _, _ = dispatcher(connector)
        dispatch.write(request())

        dispatch.write(request())

        self.assertEqual(
            2,
            len(connector.writes),
            "the device answered with a rejection, so nothing physical happened. Suppressing "
            "the retry would leave the interlock unwritten on the strength of a failure",
        )

    def test_a_timed_out_write_may_be_retried(self) -> None:
        connector = RecordingConnector(outcome=TimedOut(after=2.0))
        dispatch, _, _ = dispatcher(connector)
        dispatch.write(request())

        dispatch.write(request())

        self.assertEqual(
            2,
            len(connector.writes),
            "the physical outcome is unknown, and a point write assigns a level rather than "
            "emitting a pulse — repeating it converges on the state that was asked for, "
            "while not repeating it may leave a 停线联锁 unasserted",
        )


class AnUnverifiedConnectorIsNotDrivenTest(unittest.TestCase):
    """§5.21 refuses an unmeasured capability conservatively rather than optimistically.

    This is the least affordable place for optimism: 保守拒绝。不乐观放行.
    """

    def test_the_write_is_refused_before_the_device_is_touched(self) -> None:
        connector = RecordingConnector(capability=Unverified())
        dispatch, _, _ = dispatcher(connector)

        self.assertEqual(
            Refused(
                reason=WriteRefusal.CAPABILITY_UNVERIFIED,
                detail="连接器能力声明未验证。不驱动物理执行器",
            ),
            dispatch.write(request()),
        )
        self.assertEqual([], connector.writes)

    def test_a_refusal_is_not_remembered_as_the_action_having_happened(self) -> None:
        connector = RecordingConnector(capability=Unverified())
        dispatch, ledger, _ = dispatcher(connector)
        dispatch.write(request())

        self.assertIsNone(
            ledger.outcome_for("disposal-7"),
            "nothing was attempted against the device, so a later retry — after the "
            "capability was measured — must be free to proceed",
        )


class ASlowConnectorIsNotUsedForSafetyOutputTest(unittest.TestCase):
    def test_the_shared_capability_rule_refuses_before_the_device_is_touched(self) -> None:
        connector = RecordingConnector(capability=measured_capability(max_delivery_delay=0.21))
        dispatch, _, _ = dispatcher(connector)

        self.assertEqual(
            Refused(
                reason=WriteRefusal.DELIVERY_TOO_SLOW,
                detail="连接器最大投递延迟超出安全输出预算。不驱动物理执行器",
            ),
            dispatch.write(request()),
        )
        self.assertEqual([], connector.writes)


class TheAdaptersOwnRefusalIsPassedThroughTest(unittest.TestCase):
    """§5.8: 断线…被保守拒绝并给出结构化原因.

    The adapter answers this one, because only it knows whether the request left the host: a
    connection refused before sending means nothing physical happened, and that is a refusal
    rather than a failure.
    """

    def test_an_unreachable_point_comes_back_as_a_structured_refusal(self) -> None:
        connector = RecordingConnector(
            outcome=Refused(reason=WriteRefusal.POINT_UNREACHABLE, detail="connection refused")
        )
        dispatch, _, _ = dispatcher(connector)

        self.assertEqual(
            Refused(reason=WriteRefusal.POINT_UNREACHABLE, detail="connection refused"),
            dispatch.write(request()),
        )


class EveryAttemptLeavesADiagnosticEventTest(unittest.TestCase):
    """Q37: 写输出点位必产生事件并记操作人与目标点位.

    High-impact physical operations, so the event is unconditional — including for the
    attempts that were suppressed or refused, which are the ones an operator asking "why did
    the line not stop" needs to see.
    """

    def test_the_event_names_the_point_the_actor_and_the_outcome(self) -> None:
        connector = RecordingConnector()
        dispatch, _, events = dispatcher(connector)

        dispatch.write(request())

        self.assertEqual(
            [
                WriteAttempted(
                    point=INTERLOCK,
                    state=PointState.ACTIVE,
                    key="disposal-7",
                    actor="supervisor:station-3",
                    outcome=ACCEPTED,
                    replayed=False,
                )
            ],
            events,
        )

    def test_a_suppressed_replay_is_still_recorded_as_one(self) -> None:
        connector = RecordingConnector()
        dispatch, _, events = dispatcher(connector)
        dispatch.write(request())

        dispatch.write(request())

        self.assertEqual(
            WriteAttempted(
                point=INTERLOCK,
                state=PointState.ACTIVE,
                key="disposal-7",
                actor="supervisor:station-3",
                outcome=ACCEPTED,
                replayed=True,
            ),
            events[-1],
            "silence here would make a suppressed disposal indistinguishable from one that "
            "was never dispatched, which is the question the log exists to answer",
        )

    def test_a_refusal_is_recorded_too(self) -> None:
        connector = RecordingConnector(capability=Unverified())
        dispatch, _, events = dispatcher(connector)

        dispatch.write(request())

        self.assertEqual(
            [
                WriteAttempted(
                    point=INTERLOCK,
                    state=PointState.ACTIVE,
                    key="disposal-7",
                    actor="supervisor:station-3",
                    outcome=Refused(
                        reason=WriteRefusal.CAPABILITY_UNVERIFIED,
                        detail="连接器能力声明未验证。不驱动物理执行器",
                    ),
                    replayed=False,
                )
            ],
            events,
            "an operator asking why the line did not stop has to find the refusal, not an "
            "absence they cannot tell from a disposal that was never dispatched",
        )


class DurableLocalDisposalLedgerTest(unittest.TestCase):
    def test_failed_result_is_retryable_but_success_closes_the_key(self) -> None:
        import sqlite3
        from dataclasses import replace

        from edge_runtime.local_state.disposal import LocalDisposalLedger
        from edge_runtime.local_state.schema import migrate
        from edge_runtime.runtime import SQLiteWriteLedger

        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.row_factory = sqlite3.Row
        migrate(connection)
        first_connector = RecordingConnector(outcome=Failed(detail="temporary failure"))
        first_request = replace(
            request(),
            station_id="station-retry",
            connector_id="connector-a",
            attempt_at=HostInstant(1.0),
            lease_seconds=5.0,
        )
        first_dispatch = OutputDispatcher(
            connector=first_connector,
            ledger=SQLiteWriteLedger(LocalDisposalLedger(connection)),
            diagnostics=lambda event: None,
        )
        self.assertEqual(Failed(detail="temporary failure"), first_dispatch.write(first_request))
        self.assertEqual(1, len(first_connector.writes))

        second_connector = RecordingConnector()
        second_dispatch = OutputDispatcher(
            connector=second_connector,
            ledger=SQLiteWriteLedger(LocalDisposalLedger(connection)),
            diagnostics=lambda event: None,
        )
        self.assertEqual(
            ACCEPTED,
            second_dispatch.write(replace(first_request, attempt_at=HostInstant(2.0))),
        )
        self.assertEqual(1, len(second_connector.writes))
        connection.close()

    def test_adapter_exception_closes_unknown_and_is_not_replayed_after_restart(self) -> None:
        import sqlite3
        from dataclasses import replace

        from edge_runtime.connectors.writes import PERSISTENT_UNKNOWN_DETAIL
        from edge_runtime.local_state.disposal import DISPOSAL_RESULT_UNKNOWN, LocalDisposalLedger
        from edge_runtime.local_state.schema import migrate
        from edge_runtime.runtime import SQLiteWriteLedger

        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.row_factory = sqlite3.Row
        migrate(connection)
        durable_request = replace(
            request(),
            station_id="station-unknown",
            connector_id="connector-a",
            attempt_at=HostInstant(1.0),
            lease_seconds=5.0,
        )
        connector = RaisingConnector(RuntimeError("adapter crashed after send"))
        events: list[WriteAttempted] = []
        dispatch = OutputDispatcher(
            connector=connector,
            ledger=SQLiteWriteLedger(LocalDisposalLedger(connection)),
            diagnostics=events.append,
        )

        expected = Failed(detail=PERSISTENT_UNKNOWN_DETAIL)
        with self.assertLogs("edge_runtime", level="ERROR") as logs:
            self.assertEqual(expected, dispatch.write(durable_request))
        self.assertIn("RuntimeError: adapter crashed after send", logs.output[0])
        stored = LocalDisposalLedger(connection).result_for("station-unknown", "disposal-7")
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(DISPOSAL_RESULT_UNKNOWN, stored.kind)
        self.assertEqual(
            expected, dispatch.write(replace(durable_request, attempt_at=HostInstant(2.0)))
        )
        self.assertEqual(1, len(connector.writes))
        self.assertEqual(
            WriteAttempted(
                point=INTERLOCK,
                state=PointState.ACTIVE,
                key="disposal-7",
                actor="supervisor:station-3",
                outcome=expected,
                replayed=False,
            ),
            events[0],
        )

        restarted_connector = RecordingConnector()
        restarted = OutputDispatcher(
            connector=restarted_connector,
            ledger=SQLiteWriteLedger(LocalDisposalLedger(connection)),
            diagnostics=lambda event: None,
        )
        self.assertEqual(
            expected,
            restarted.write(replace(durable_request, attempt_at=HostInstant(3.0))),
        )
        self.assertEqual([], restarted_connector.writes)
        connection.close()

    def test_adapter_exception_is_logged_even_when_unknown_persistence_fails(self) -> None:
        class FailingRecordLedger(InMemoryWriteLedger):
            def record(self, key: str, outcome: WriteOutcome, /) -> None:
                raise RuntimeError("ledger unavailable")

        dispatch = OutputDispatcher(
            connector=RaisingConnector(RuntimeError("adapter crashed after send")),
            ledger=FailingRecordLedger(),
            diagnostics=lambda event: self.fail(f"unexpected diagnostic: {event!r}"),
        )

        with (
            self.assertLogs("edge_runtime", level="ERROR") as logs,
            self.assertRaisesRegex(RuntimeError, "ledger unavailable"),
        ):
            dispatch.write(request())
        self.assertIn("RuntimeError: adapter crashed after send", logs.output[0])

    def test_control_flow_exit_is_not_recorded_as_unknown(self) -> None:
        import sqlite3
        from dataclasses import replace

        from edge_runtime.local_state.disposal import LocalDisposalLedger
        from edge_runtime.local_state.schema import migrate
        from edge_runtime.runtime import SQLiteWriteLedger

        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.row_factory = sqlite3.Row
        migrate(connection)
        durable_request = replace(
            request(),
            station_id="station-cancelled",
            connector_id="connector-a",
            attempt_at=HostInstant(1.0),
            lease_seconds=5.0,
        )
        ledger = LocalDisposalLedger(connection)
        dispatch = OutputDispatcher(
            connector=RaisingConnector(KeyboardInterrupt()),
            ledger=SQLiteWriteLedger(ledger),
            diagnostics=lambda event: self.fail(f"unexpected diagnostic: {event!r}"),
        )

        with self.assertRaises(KeyboardInterrupt):
            dispatch.write(durable_request)
        self.assertIsNone(ledger.result_for("station-cancelled", "disposal-7"))
        connection.close()

    def test_written_result_survives_restart_and_expired_attempt_is_not_replayed(self) -> None:
        import sqlite3
        from dataclasses import replace

        from edge_runtime.local_state.disposal import LocalDisposalLedger
        from edge_runtime.local_state.schema import migrate
        from edge_runtime.runtime import SQLiteWriteLedger

        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.row_factory = sqlite3.Row
        migrate(connection)
        local_ledger = LocalDisposalLedger(connection)
        first_connector = RecordingConnector()
        first_request = replace(
            request(),
            station_id="station-3",
            connector_id="connector-a",
            attempt_at=HostInstant(1.0),
            lease_seconds=5.0,
        )
        first_dispatch = OutputDispatcher(
            connector=first_connector,
            ledger=SQLiteWriteLedger(local_ledger),
            diagnostics=lambda event: None,
        )
        self.assertEqual(ACCEPTED, first_dispatch.write(first_request))
        self.assertEqual(1, len(first_connector.writes))

        restarted_connector = RecordingConnector()
        restarted_dispatch = OutputDispatcher(
            connector=restarted_connector,
            ledger=SQLiteWriteLedger(LocalDisposalLedger(connection)),
            diagnostics=lambda event: None,
        )
        self.assertEqual(
            ACCEPTED,
            restarted_dispatch.write(replace(first_request, attempt_at=HostInstant(2.0))),
        )
        self.assertEqual([], restarted_connector.writes)

        abandoned = replace(first_request, key="disposal-abandoned")
        abandoned_ledger = SQLiteWriteLedger(LocalDisposalLedger(connection))
        abandoned_ledger.prepare(abandoned)
        self.assertIsNone(abandoned_ledger.claim(abandoned))
        no_duplicate_connector = RecordingConnector()
        no_duplicate_dispatch = OutputDispatcher(
            connector=no_duplicate_connector,
            ledger=SQLiteWriteLedger(LocalDisposalLedger(connection)),
            diagnostics=lambda event: None,
        )
        outcome = no_duplicate_dispatch.write(replace(abandoned, attempt_at=HostInstant(7.0)))
        self.assertIsInstance(outcome, Failed)
        self.assertEqual([], no_duplicate_connector.writes)
        connection.close()


if __name__ == "__main__":
    unittest.main()
