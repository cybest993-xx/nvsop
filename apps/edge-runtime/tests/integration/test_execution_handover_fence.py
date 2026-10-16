"""正常交权停写 fence(#214): 本地唯一执行权事实上的持久禁写.

真实 SQLite 迁移 + 真实 ``LocalExecutionLeaseStore``/``StationOutputWriteGate``. fence 是
交权确认的本机安全事实: 先持久禁写再停机, 进程崩溃或重启都不得复活物理写入; 同一 ``grant_id``
的续期/配置重报不得清除 fence, 只有后继新 grant(#215 负责签发)才恢复写权.
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

from nvsop_contracts import ExecutionLease

from edge_runtime.connectors.port import Refused, WriteRefusal
from edge_runtime.connectors.writes import WriteRequest
from edge_runtime.judgment.model import HostInstant
from edge_runtime.local_state.execution import (
    ExecutionAuthority,
    ExecutionLeaseFact,
    LocalExecutionLeaseStore,
)
from edge_runtime.local_state.schema import migrate
from edge_runtime.runtime import StationOutputWriteGate, StationWriteLifecycle

STATION = "station-a"
CONNECTOR = "connector-a"
POINT_ADDRESS = "2"


def _epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _lease(grant_id: str, expires_at: str = "2099-01-01T00:00:00Z") -> ExecutionLease:
    return ExecutionLease(
        station_id=STATION,
        grant_id=grant_id,
        holder_host_id="host-a",
        lease_expires_at=expires_at,
    )


def _request() -> WriteRequest:
    return WriteRequest(
        point=__import__("edge_runtime.connectors.port", fromlist=["OutputPoint"]).OutputPoint(
            label="停线联锁", address=POINT_ADDRESS
        ),
        state=__import__("edge_runtime.connectors.port", fromlist=["PointState"]).PointState.ACTIVE,
        key="station-a:disposal-1",
        actor="supervisor",
        timeout=1.0,
        capability_budget=0.5,
        station_id=STATION,
        connector_id=CONNECTOR,
        attempt_at=HostInstant(1.0),
        lease_seconds=5.0,
    )


class ExecutionHandoverFenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.state_path = Path(self._directory.name) / "state.sqlite"
        self.connection = self._open()
        self.leases = LocalExecutionLeaseStore(self.connection)
        self.lifecycle = StationWriteLifecycle()

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.state_path), isolation_level=None, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        migrate(connection)
        self.addCleanup(connection.close)
        return connection

    def _gate(self) -> StationOutputWriteGate:
        return StationOutputWriteGate(
            station_id=STATION,
            connector_id=CONNECTOR,
            output_points=(
                __import__("edge_runtime.connectors.port", fromlist=["OutputPoint"]).OutputPoint(
                    label="停线联锁", address=POINT_ADDRESS
                ),
            ),
            leases=self.leases,
            lifecycle=self.lifecycle,
            clock=lambda: _epoch("2030-01-01T00:00:00Z"),
        )

    def test_fence_blocks_a_live_lease_and_survives_a_same_grant_renewal(self) -> None:
        self.leases.apply([_lease("grant-a")], observed_at=1.0)
        self.assertIsNone(self._gate().refusal(_request()))

        self.leases.stop_writes(STATION, grant_id="grant-a", stopped_at=100.0)

        refused = self._gate().refusal(_request())
        self.assertIsInstance(refused, Refused)
        assert isinstance(refused, Refused)
        self.assertIs(WriteRefusal.WRITE_STOPPED, refused.reason)

        # 同一 grant 的续期/配置重报不清 fence.
        self.leases.apply(
            [_lease("grant-a", "2031-01-01T00:00:00Z")],
            observed_at=2.0,
        )
        refused = self._gate().refusal(_request())
        self.assertIsInstance(refused, Refused)
        assert isinstance(refused, Refused)
        self.assertIs(WriteRefusal.WRITE_STOPPED, refused.reason)

    def test_fence_survives_restart_and_yields_to_a_new_grant(self) -> None:
        self.leases.apply([_lease("grant-a")], observed_at=1.0)
        self.leases.stop_writes(STATION, grant_id="grant-a", stopped_at=100.0)

        # 进程重启: 重开真实文件数据库, fence 仍在.
        self.connection.close()
        self.connection = self._open()
        self.leases = LocalExecutionLeaseStore(self.connection)
        refused = self._gate().refusal(_request())
        self.assertIsInstance(refused, Refused)

        # 后继新 grant 才恢复写权; 旧 grant 重放不清新 fence.
        self.leases.apply([_lease("grant-b")], observed_at=3.0)
        self.assertIsNone(self._gate().refusal(_request()))
        self.leases.apply([_lease("grant-a")], observed_at=4.0)
        self.assertIsNone(self._gate().refusal(_request()))

    def test_stopped_writes_are_visible_to_authority_reporting(self) -> None:
        self.leases.apply([_lease("grant-a")], observed_at=1.0)
        self.leases.stop_writes(STATION, grant_id="grant-a", stopped_at=100.0)

        authority = self.leases.status(STATION, now=_epoch("2030-01-01T00:00:00Z"))

        self.assertFalse(authority.authorized)
        self.assertTrue(authority.write_stopped)
        self.assertIsNotNone(authority.fact)
        # lease 本身没有被销毁: 后继 grant 恢复语义需要原事实可查.
        assert authority.fact is not None
        self.assertEqual("grant-a", authority.fact.grant_id)

    def test_status_does_not_mix_an_old_lease_with_a_new_grants_cleared_fence(self) -> None:
        shared_lock = threading.RLock()
        leases = LocalExecutionLeaseStore(self.connection, lock=shared_lock)
        leases.apply([_lease("grant-a")], observed_at=1.0)
        leases.stop_writes(STATION, grant_id="grant-a", stopped_at=2.0)
        entered = threading.Event()
        resume = threading.Event()
        replaced = threading.Event()
        results: list[ExecutionAuthority] = []

        class PausingStore(LocalExecutionLeaseStore):
            def fact(self, station_id: str) -> ExecutionLeaseFact | None:
                fact = super().fact(station_id)
                entered.set()
                resume.wait(timeout=2)
                return fact

        reader = PausingStore(self.connection, lock=shared_lock)
        sampling = threading.Thread(
            target=lambda: results.append(
                reader.status(STATION, now=_epoch("2030-01-01T00:00:00Z"))
            )
        )

        def replace_grant() -> None:
            leases.apply([_lease("grant-b")], observed_at=3.0)
            replaced.set()

        writer = threading.Thread(target=replace_grant)
        sampling.start()
        self.assertTrue(entered.wait(timeout=2))
        writer.start()
        try:
            replaced.wait(timeout=0.1)
        finally:
            resume.set()
            sampling.join(timeout=3)
            writer.join(timeout=3)
        self.assertFalse(sampling.is_alive())
        self.assertFalse(writer.is_alive())
        self.assertEqual(1, len(results))
        self.assertTrue(results[0].write_stopped)
        self.assertFalse(results[0].authorized)

    def test_stopping_twice_keeps_the_frozen_grant_identity(self) -> None:
        self.leases.apply([_lease("grant-a")], observed_at=1.0)
        self.leases.stop_writes(STATION, grant_id="grant-a", stopped_at=100.0)
        self.leases.stop_writes(STATION, grant_id="grant-a", stopped_at=200.0)

        row = self.connection.execute(
            "SELECT grant_id, stopped_at FROM local_execution_write_fence WHERE station_id = ?",
            (STATION,),
        ).fetchone()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual("grant-a", row["grant_id"])
        self.assertEqual(100.0, row["stopped_at"])
        self.assertFalse(
            self.leases.status(STATION, now=_epoch("2030-01-01T00:00:00Z")).write_stopped is False
        )


if __name__ == "__main__":
    unittest.main()
