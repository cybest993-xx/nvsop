"""S029: 输出点位写入前的连接、目标归属、生命周期与执行权检查。

在拥有者边界使用真实基础设施: 真实 ``LocalExecutionLeaseStore``/``LocalDisposalLedger`` (SQLite)、
真实 ``IsapiConnector`` + ``UrllibIsapiTransport`` 与 loopback HTTP 设备。拒绝不发送物理写 PUT
(探测只发送安全 GET), 按本机事实判定而不信任请求自报的 ``station_id``。
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from collections.abc import Callable
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NamedTuple, cast

from nvsop_contracts import (
    EdgePreservation,
    ExecutionLease,
    Measured,
    Polled,
    Sequencing,
    TimestampSource,
    Unverified,
)

from edge_runtime.connectors.hikvision import CANDIDATE_PROFILE, IsapiConnector
from edge_runtime.connectors.port import OutputPoint, PointState, Refused, WriteRefusal, Written
from edge_runtime.connectors.transport import UrllibIsapiTransport
from edge_runtime.connectors.writes import OutputDispatcher, WriteAttempted, WriteRequest
from edge_runtime.judgment.model import HostInstant
from edge_runtime.local_state.disposal import DISPOSAL_RESULT_UNKNOWN, LocalDisposalLedger
from edge_runtime.local_state.execution import LocalExecutionLeaseStore
from edge_runtime.local_state.schema import migrate
from edge_runtime.runtime import SQLiteWriteLedger, StationOutputWriteGate, StationWriteLifecycle

STATION = "station-a"
CONNECTOR = "connector-a"
INTERLOCK = OutputPoint(label="停线联锁", address="2")
VALID_UNTIL = "2099-01-01T00:00:00Z"
TRIGGER_PATH = "/ISAPI/System/IO/outputs/2/trigger"


def _epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _lease(expires_at: str) -> ExecutionLease:
    return ExecutionLease(
        station_id=STATION,
        grant_id="grant-a",
        holder_host_id="host-a",
        lease_expires_at=expires_at,
    )


def _request(*, key: str = "station-a:disposal-1", station_id: str = STATION) -> WriteRequest:
    return WriteRequest(
        point=INTERLOCK,
        state=PointState.ACTIVE,
        key=key,
        actor="supervisor",
        timeout=1.0,
        capability_budget=1.0,
        station_id=station_id,
        connector_id=CONNECTOR,
        attempt_at=HostInstant(1.0),
        lease_seconds=5.0,
    )


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _RefusalCase(NamedTuple):
    name: str
    leases: tuple[ExecutionLease, ...]
    reason: WriteRefusal
    capability: Measured | Unverified | None = None
    output_points: tuple[OutputPoint, ...] = (INTERLOCK,)
    request_station_id: str = STATION
    device_info_ok: bool = True
    clock: _Clock | None = None


class _IsapiLoopbackHandler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # keep the test output clean
        return None

    def do_GET(self) -> None:
        server = cast("IsapiLoopbackServer", self.server)
        server.requests.append(("GET", self.path))
        # 让测试在探测处理期间改变本机授权事实, 以证明实际发送前的重新判定 (S029 AC2)。
        if server.probe_hook is not None:
            server.probe_hook()
        self._answer(server.device_info_ok)

    def do_PUT(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        server = cast("IsapiLoopbackServer", self.server)
        server.requests.append(("PUT", self.path))
        self._answer(server.write_ok)

    def _answer(self, ok: bool) -> None:
        self.send_response(200 if ok else 500)
        self.send_header("Content-Type", "application/xml")
        self.end_headers()
        if ok:
            self.wfile.write(b"<ok/>")


class IsapiLoopbackServer(ThreadingHTTPServer):
    """A real loopback ISAPI device. A 200 answer is what the adapter must actually receive."""

    def __init__(self, *, device_info_ok: bool = True, write_ok: bool = True) -> None:
        super().__init__(("127.0.0.1", 0), _IsapiLoopbackHandler)
        self.device_info_ok = device_info_ok
        self.write_ok = write_ok
        self.requests: list[tuple[str, str]] = []
        self.probe_hook: Callable[[], None] | None = None
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    @property
    def puts(self) -> list[str]:
        return [path for method, path in self.requests if method == "PUT"]

    def __enter__(self) -> IsapiLoopbackServer:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self.shutdown()
        self._thread.join(timeout=5)
        self.server_close()


class OutputSafetyIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.state_path = Path(self._directory.name) / "state.sqlite"
        self.connection = self._open(self.state_path)
        self.leases = LocalExecutionLeaseStore(self.connection)
        self.lifecycle = StationWriteLifecycle()

    def _open(self, path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(str(path), isolation_level=None)
        connection.row_factory = sqlite3.Row
        migrate(connection)
        self.addCleanup(connection.close)
        return connection

    def _dispatcher(
        self,
        base_url: str,
        *,
        connection: sqlite3.Connection | None = None,
        capability: Measured | Unverified | None = None,
        output_points: tuple[OutputPoint, ...] = (INTERLOCK,),
        lifecycle: StationWriteLifecycle | None = None,
        clock: _Clock | None = None,
    ) -> tuple[OutputDispatcher, list[WriteAttempted]]:
        connector = IsapiConnector(
            transport=UrllibIsapiTransport(base_url=base_url, username="operator", password="x"),
            profile=CANDIDATE_PROFILE,
            capability=capability
            or Measured(
                delivery=Polled(interval=0.1),
                max_delivery_delay=0.1,
                sequencing=Sequencing.SEQUENCED,
                edges=EdgePreservation.PRESERVED,
                timestamps=TimestampSource.HOST_RECEIPT,
            ),
        )
        gate = StationOutputWriteGate(
            station_id=STATION,
            connector_id=CONNECTOR,
            output_points=output_points,
            leases=self.leases,
            lifecycle=self.lifecycle if lifecycle is None else lifecycle,
            clock=clock or _Clock(1.0),
        )
        events: list[WriteAttempted] = []
        dispatch = OutputDispatcher(
            connector=connector,
            ledger=SQLiteWriteLedger(
                LocalDisposalLedger(self.connection if connection is None else connection)
            ),
            diagnostics=events.append,
            gate=gate,
        )
        return dispatch, events

    def _assert_refused(self, outcome: object, reason: WriteRefusal) -> None:
        self.assertIsInstance(outcome, Refused)
        assert isinstance(outcome, Refused)
        self.assertIs(reason, outcome.reason)

    def _stored(self, key: str, *, connection: sqlite3.Connection | None = None) -> sqlite3.Row:
        source = self.connection if connection is None else connection
        row = source.execute(
            "SELECT result_kind, lease_until FROM local_disposal"
            " WHERE station_id = ? AND idempotency_key = ?",
            (STATION, key),
        ).fetchone()
        self.assertIsNotNone(row)
        assert row is not None
        return cast(sqlite3.Row, row)

    def test_each_refusal_precedes_the_device_and_never_sends_a_put(self) -> None:
        cases = (
            _RefusalCase(
                "unverified",
                (_lease(VALID_UNTIL),),
                WriteRefusal.CAPABILITY_UNVERIFIED,
                capability=Unverified(),
            ),
            _RefusalCase(
                "target",
                (_lease(VALID_UNTIL),),
                WriteRefusal.TARGET_NOT_IN_STATION,
                output_points=(OutputPoint(label="其他点位", address="9"),),
            ),
            _RefusalCase(
                "expired",
                (_lease("2000-01-01T00:00:00Z"),),
                WriteRefusal.EXECUTION_LEASE_EXPIRED,
                clock=_Clock(_epoch("2020-01-01T00:00:00Z")),
            ),
            _RefusalCase("missing", (), WriteRefusal.EXECUTION_LEASE_MISSING),
            _RefusalCase(
                "disconnected",
                (_lease(VALID_UNTIL),),
                WriteRefusal.CONNECTOR_UNREACHABLE,
                device_info_ok=False,
            ),
            _RefusalCase(
                "valid_authority_but_request_names_another_station",
                (_lease(VALID_UNTIL),),
                WriteRefusal.TARGET_NOT_IN_STATION,
                request_station_id="station-b",
            ),
        )
        for case in cases:
            with (
                self.subTest(name=case.name),
                IsapiLoopbackServer(device_info_ok=case.device_info_ok) as loopback,
            ):
                self.leases.apply(case.leases, observed_at=1.0)
                dispatch, events = self._dispatcher(
                    loopback.base_url,
                    capability=case.capability,
                    output_points=case.output_points,
                    clock=case.clock,
                )

                self._assert_refused(
                    dispatch.write(_request(station_id=case.request_station_id)), case.reason
                )
                self.assertEqual([], loopback.puts)
                self.assertEqual(INTERLOCK, events[-1].point)

    def test_a_pre_claim_refusal_does_not_occupy_the_key_and_retries(self) -> None:
        with IsapiLoopbackServer() as loopback:
            self.leases.apply([_lease("2000-01-01T00:00:00Z")], observed_at=1.0)
            dispatch, events = self._dispatcher(
                loopback.base_url, clock=_Clock(_epoch("2020-01-01T00:00:00Z"))
            )

            refused = dispatch.write(_request())

            self._assert_refused(refused, WriteRefusal.EXECUTION_LEASE_EXPIRED)
            self.assertEqual([], loopback.requests)
            self.assertEqual(refused, events[-1].outcome)
            self.assertIsNone(self._stored("station-a:disposal-1")["result_kind"])

            self.leases.apply([_lease(VALID_UNTIL)], observed_at=2.0)

            self.assertIsInstance(dispatch.write(_request()), Written)
            self.assertEqual([TRIGGER_PATH], loopback.puts)

    def test_authority_lost_during_the_probe_is_released_and_retryable(self) -> None:
        cases = (
            ("lease_expiry", "2030-01-01T00:00:00Z", "2031-01-01T00:00:00Z", False),
            ("lifecycle_stop", VALID_UNTIL, "2029-01-01T00:00:00Z", True),
        )
        for name, expires, after, stopped in cases:
            with self.subTest(name=name), IsapiLoopbackServer() as loopback:
                key = f"station-a:{name}"
                clock = _Clock(_epoch("2029-01-01T00:00:00Z"))
                lifecycle = StationWriteLifecycle()
                self.leases.apply([_lease(expires)], observed_at=1.0)
                dispatch, events = self._dispatcher(
                    loopback.base_url, lifecycle=lifecycle, clock=clock
                )

                def lose_authority(
                    _stopped: bool = stopped,
                    _lifecycle: StationWriteLifecycle = lifecycle,
                    _clock: _Clock = clock,
                    _after: str = after,
                ) -> None:
                    if _stopped:
                        _lifecycle.stop()
                    else:
                        _clock.now = _epoch(_after)

                loopback.probe_hook = lose_authority
                refused = dispatch.write(_request(key=key))
                loopback.probe_hook = None

                reason = (
                    WriteRefusal.WRITE_STOPPED if stopped else WriteRefusal.EXECUTION_LEASE_EXPIRED
                )
                self._assert_refused(refused, reason)
                self.assertEqual(
                    [], loopback.puts, "the post-probe re-check stopped the physical write"
                )
                self.assertEqual(refused, events[-1].outcome)

                # The release is durable and never becomes `unknown`; a fresh attempt on the same
                # key is free to proceed once authority is back.
                self.connection.close()
                self.connection = self._open(self.state_path)
                self.leases = LocalExecutionLeaseStore(self.connection)
                stored = self._stored(key)
                self.assertEqual(f"refused:{reason.value}", stored["result_kind"])
                self.assertNotEqual(DISPOSAL_RESULT_UNKNOWN, stored["result_kind"])
                self.assertIsNone(
                    stored["lease_until"], "the claim lease is released, not left to expire"
                )

                clock.now = _epoch("2029-01-01T00:00:00Z")
                retry, _ = self._dispatcher(
                    loopback.base_url, lifecycle=StationWriteLifecycle(), clock=clock
                )
                self.assertIsInstance(retry.write(_request(key=key)), Written)
                self.assertEqual([TRIGGER_PATH], loopback.puts)


if __name__ == "__main__":
    unittest.main()
