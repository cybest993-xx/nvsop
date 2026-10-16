"""正常交权旧机停机执行(#214): 真实 SSE 流源、真实 SQLite fence 与真实输出门禁.

fence 先持久禁写(崩溃复活被拒), 再等待在途判定/写入收敛并关闭资源(SSE reader 有界 join,
卡死显式失败不吞异常), 全部成功才回报 ``confirmed``. 停机后 ``AutonomousStation.write_output``
经真实 ``OutputDispatcher``/``StationOutputWriteGate`` 拒绝物理写: 租约仍有效也发不出 PUT.
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

from nvsop_contracts import (
    EdgePreservation,
    ExecutionHandoverCommand,
    ExecutionHandoverOutcome,
    Measured,
    Polled,
    Sequencing,
    TimestampSource,
)
from store_harness import STATION, FakeClock, opening_state, supervisor

from edge_runtime.connectors.hikvision import CANDIDATE_PROFILE, IsapiConnector
from edge_runtime.connectors.port import OutputPoint, PointState, Refused, WriteRefusal
from edge_runtime.connectors.transport import UrllibIsapiTransport
from edge_runtime.connectors.writes import OutputDispatcher, WriteRequest
from edge_runtime.judgment.model import HostInstant
from edge_runtime.local_state.execution import LocalExecutionLeaseStore
from edge_runtime.local_state.schema import migrate
from edge_runtime.runtime import (
    AutonomousStation,
    SQLiteWriteLedger,
    StationOutputWriteGate,
    StationWriteLifecycle,
)
from edge_runtime.station_runtime import SseStationInputSource
from edge_runtime.supervisor.execution_handover import (
    AutonomousStationStopper,
    ExecutionHandoverCommandRunner,
)

INTERLOCK = OutputPoint(label="停线联锁", address="2")
CONNECTOR = "connector-a"
VALID_UNTIL = "2099-01-01T00:00:00Z"
EPOCH = datetime.fromisoformat(VALID_UNTIL.replace("Z", "+00:00")).timestamp()


def _command(grant_id: str = "grant-a") -> ExecutionHandoverCommand:
    return ExecutionHandoverCommand(
        command_id="command-1",
        handover_id="handover-1",
        station_id=STATION,
        from_host_id="host-a",
        to_host_id="host-b",
        grant_id=grant_id,
    )


class _StuckSseHandler(BaseHTTPRequestHandler):
    """持续滴流不发换行: readline 永不返回也永不超时, reader 无法在限时内退出."""

    release: ClassVar[threading.Event] = threading.Event()

    def do_POST(self) -> None:
        import time

        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while not _StuckSseHandler.release.is_set():
                self.wfile.write(b":")  # 无换行: readline 阻塞在半行上
                self.wfile.flush()
                time.sleep(0.2)
        except OSError:
            pass

    def log_message(self, *args: object, **kwargs: object) -> None:
        return None


class _IsapiHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[tuple[str, str]]] = []
    write_started: ClassVar[threading.Event] = threading.Event()
    release_write: ClassVar[threading.Event | None] = None

    def do_GET(self) -> None:
        _IsapiHandler.requests.append(("GET", self.path))
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.end_headers()
        self.wfile.write(b"<ok/>")

    def do_PUT(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        _IsapiHandler.requests.append(("PUT", self.path))
        _IsapiHandler.write_started.set()
        if _IsapiHandler.release_write is not None:
            _IsapiHandler.release_write.wait(timeout=5)
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.end_headers()
        self.wfile.write(b"<ok/>")

    def log_message(self, *args: object, **kwargs: object) -> None:
        return None


@contextmanager
def _stuck_sse_server() -> Iterator[str]:
    _StuckSseHandler.release.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StuckSseHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    finally:
        _StuckSseHandler.release.set()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@contextmanager
def _isapi_device() -> Iterator[str]:
    del _IsapiHandler.requests[:]
    _IsapiHandler.write_started.clear()
    _IsapiHandler.release_write = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), _IsapiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


class ExecutionHandoverStationStopTest(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.state_path = Path(self._directory.name) / "state.sqlite"
        self.connection = self._open()
        self.leases = LocalExecutionLeaseStore(self.connection)
        self.leases.apply(
            [
                __import__("nvsop_contracts", fromlist=["ExecutionLease"]).ExecutionLease(
                    station_id=STATION,
                    grant_id="grant-a",
                    holder_host_id="host-a",
                    lease_expires_at=VALID_UNTIL,
                )
            ],
            observed_at=1.0,
        )
        self.clock = FakeClock(1_000.0)

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.state_path), isolation_level=None, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        migrate(connection)
        self.addCleanup(connection.close)
        return connection

    def _dispatcher(self, lifecycle: StationWriteLifecycle, base_url: str = "") -> OutputDispatcher:
        return OutputDispatcher(
            connector=IsapiConnector(
                transport=UrllibIsapiTransport(
                    base_url=base_url or "http://127.0.0.1:1",
                    username="operator",
                    password="x",
                ),
                profile=CANDIDATE_PROFILE,
                capability=Measured(
                    delivery=Polled(interval=0.1),
                    max_delivery_delay=0.1,
                    sequencing=Sequencing.SEQUENCED,
                    edges=EdgePreservation.PRESERVED,
                    timestamps=TimestampSource.HOST_RECEIPT,
                ),
            ),
            ledger=SQLiteWriteLedger(
                __import__(
                    "edge_runtime.local_state.disposal", fromlist=["LocalDisposalLedger"]
                ).LocalDisposalLedger(self.connection)
            ),
            diagnostics=lambda event: None,
            gate=StationOutputWriteGate(
                station_id=STATION,
                connector_id=CONNECTOR,
                output_points=(INTERLOCK,),
                leases=self.leases,
                lifecycle=lifecycle,
                clock=lambda: EPOCH - 1,
            ),
        )

    def _runner(self, stations: dict[str, AutonomousStation]) -> ExecutionHandoverCommandRunner:
        return ExecutionHandoverCommandRunner(
            stopper=AutonomousStationStopper(stations, self.leases),
            leases=self.leases,
            clock=self.clock,
        )

    def test_stop_persists_the_fence_closes_the_stream_and_refuses_late_writes(self) -> None:
        with _isapi_device() as device_url:
            _IsapiHandler.requests.clear()
            lifecycle = StationWriteLifecycle()
            dispatcher = self._dispatcher(lifecycle, device_url)
            from edge_runtime.local_state.store import StationStore
            from edge_runtime.runtime import AutonomousStation

            connection = sqlite3.connect(str(self.state_path), isolation_level=None)
            connection.row_factory = sqlite3.Row
            migrate(connection)
            self.addCleanup(connection.close)
            store = StationStore(connection, station_id=STATION, lock=threading.RLock())
            station = AutonomousStation(
                station_id=STATION,
                supervisor=supervisor(opening_state(), self.clock, store),
                source=_QuietSource(),
                output_dispatchers={CONNECTOR: dispatcher},
                output_points={CONNECTOR: (INTERLOCK,)},
                write_lifecycle=lifecycle,
            )

            result = self._runner({STATION: station}).execute(_command())

            self.assertIs(ExecutionHandoverOutcome.CONFIRMED, result.outcome)
            self.assertIsNone(result.failure_code)
            # fence 持久化且冻结 grant 身份.
            row = self.connection.execute(
                "SELECT grant_id FROM local_execution_write_fence WHERE station_id = ?",
                (STATION,),
            ).fetchone()
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual("grant-a", row["grant_id"])
            # 生命周期停止: 再写点位被真实门禁拒绝, 设备连探测都没有收到.
            outcome = station.write_output(
                WriteRequest(
                    point=INTERLOCK,
                    state=PointState.ACTIVE,
                    key="station-a:late",
                    actor="test",
                    timeout=1.0,
                    capability_budget=0.5,
                    station_id=STATION,
                    connector_id=CONNECTOR,
                    attempt_at=HostInstant(2.0),
                    lease_seconds=5.0,
                )
            )
            self.assertIsInstance(outcome, Refused)
            assert isinstance(outcome, Refused)
            self.assertIs(WriteRefusal.WRITE_STOPPED, outcome.reason)
            self.assertEqual([], [r for r in _IsapiHandler.requests if r[0] == "PUT"])

    def test_restart_refuses_writes_with_a_fresh_gate(self) -> None:
        with _isapi_device() as device_url:
            lifecycle = StationWriteLifecycle()
            station = self._station_with_source(lifecycle, device_url)
            self._runner({STATION: station}).execute(_command())

            # 进程重启: 重开真实文件数据库与全新门禁, fence 仍拒绝写入.
            self.connection.close()
            self.connection = self._open()
            self.leases = LocalExecutionLeaseStore(self.connection)
            fresh_lifecycle = StationWriteLifecycle()
            gate = StationOutputWriteGate(
                station_id=STATION,
                connector_id=CONNECTOR,
                output_points=(INTERLOCK,),
                leases=self.leases,
                lifecycle=fresh_lifecycle,
                clock=lambda: EPOCH,
            )
            refusal = gate.refusal(
                WriteRequest(
                    point=INTERLOCK,
                    state=PointState.ACTIVE,
                    key="station-a:restarted",
                    actor="test",
                    timeout=1.0,
                    capability_budget=0.5,
                    station_id=STATION,
                    connector_id=CONNECTOR,
                    attempt_at=HostInstant(3.0),
                    lease_seconds=5.0,
                )
            )
            self.assertIsInstance(refusal, Refused)
            assert isinstance(refusal, Refused)
            self.assertIs(WriteRefusal.WRITE_STOPPED, refusal.reason)

    def test_stale_grant_fails_without_stopping_the_station(self) -> None:
        lifecycle = StationWriteLifecycle()
        station = self._station_with_source(lifecycle, None)
        result = self._runner({STATION: station}).execute(_command(grant_id="grant-old"))

        self.assertIs(ExecutionHandoverOutcome.FAILED, result.outcome)
        self.assertEqual("COMMAND_GRANT_STALE", result.failure_code)
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM local_execution_write_fence WHERE station_id = ?", (STATION,)
            ).fetchone()
        )
        self.assertFalse(lifecycle.stopped)

    def test_a_stuck_stream_fails_instead_of_pretending_success(self) -> None:
        with _stuck_sse_server() as sse_url:
            lifecycle = StationWriteLifecycle()
            station = self._station_with_source(lifecycle, None, sse_url)
            # 生产中工位循环在持续消费输入; 先真实启动 SSE reader 再停机.
            consumer = threading.Thread(
                target=lambda: station._source.next_input(timeout=None), daemon=True
            )
            consumer.start()
            time.sleep(0.5)  # 等待惰性启动的真实 reader 进入阻塞 readline
            result = self._runner({STATION: station}).execute(_command())

            self.assertIs(ExecutionHandoverOutcome.FAILED, result.outcome)
            self.assertEqual("COMMAND_STOP_FAILED", result.failure_code)
            self.assertTrue(lifecycle.stopped)

    def test_confirmation_waits_for_an_inflight_physical_write(self) -> None:
        with _isapi_device() as device_url:
            lifecycle = StationWriteLifecycle()
            station = self._station_with_source(lifecycle, device_url)
            released = threading.Event()
            _IsapiHandler.release_write = released
            finished = threading.Event()
            results = []
            writer = threading.Thread(
                target=lambda: station.write_output(
                    WriteRequest(
                        point=INTERLOCK,
                        state=PointState.ACTIVE,
                        key="inflight-output",
                        actor="test",
                        timeout=3.0,
                        capability_budget=0.5,
                        station_id=STATION,
                        connector_id=CONNECTOR,
                        attempt_at=HostInstant(2.0),
                        lease_seconds=7.0,
                    )
                )
            )

            def handover() -> None:
                results.append(self._runner({STATION: station}).execute(_command()))
                finished.set()

            closer = threading.Thread(target=handover)
            writer.start()
            try:
                self.assertTrue(_IsapiHandler.write_started.wait(timeout=2))
                closer.start()
                self.assertFalse(finished.wait(timeout=0.1), "confirmed before the PUT completed")
            finally:
                released.set()
                writer.join(timeout=5)
                if closer.ident is not None:
                    closer.join(timeout=5)
            self.assertFalse(writer.is_alive())
            self.assertFalse(closer.is_alive())
            self.assertIs(ExecutionHandoverOutcome.CONFIRMED, results[0].outcome)

    def _station_with_source(
        self,
        lifecycle: StationWriteLifecycle,
        device_url: str | None,
        sse_url: str | None = None,
    ) -> AutonomousStation:
        from edge_runtime.local_state.store import StationStore

        connection = sqlite3.connect(
            str(self.state_path), isolation_level=None, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        migrate(connection)
        self.addCleanup(connection.close)
        store = StationStore(connection, station_id=STATION, lock=threading.RLock())
        return AutonomousStation(
            station_id=STATION,
            supervisor=supervisor(opening_state(), self.clock, store),
            source=SseStationInputSource(
                inference_url=sse_url or f"http://127.0.0.1:{_free_port()}",
                request_body={"stream": True},
                timeout=1.0,
            ),
            output_dispatchers=(
                {CONNECTOR: self._dispatcher(lifecycle, device_url)} if device_url else {}
            ),
            output_points={CONNECTOR: (INTERLOCK,)},
            write_lifecycle=lifecycle,
        )


class _QuietSource:
    """不启动网络的输入源; 停机断言聚焦 fence 与门禁, 不重复 SSE 关闭语义."""

    def __init__(self) -> None:
        self.ended = False

    def next_input(self, *, timeout: float | None) -> None:
        return None

    def close(self) -> None:
        self.ended = True


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


if __name__ == "__main__":
    unittest.main()
