"""#207 Edge 删除调度: 运行时线程驱动 #206 执行器周期 drain, 失败不影响判定循环。"""

from __future__ import annotations

import sqlite3
import threading
import unittest
from typing import cast

from edge_runtime.evidence_deletion import EvidenceDeletionExecution
from edge_runtime.runtime import AutonomousRuntime, _RuntimeCycleRunner


class _CycleStub:
    """可编程的 cycle 停止事件: 第 N 次 wait 后停止, 模拟配置切换。"""

    def __init__(self, *, stop_after_waits: int) -> None:
        self.stopped = threading.Event()
        self.waited: list[float] = []
        self._stop_after = stop_after_waits

    def is_set(self) -> bool:
        return self.stopped.is_set()

    def set(self) -> None:
        self.stopped.set()

    def wait(self, timeout: float) -> None:
        self.waited.append(timeout)
        if len(self.waited) >= self._stop_after:
            self.stopped.set()


class _DeletionStub:
    def __init__(self, *, fail: bool) -> None:
        self.calls = 0
        self._fail = fail

    def drain(self, *, limit: int) -> tuple[EvidenceDeletionExecution, ...]:
        assert 0 < limit <= 100
        self.calls += 1
        if self._fail:
            raise sqlite3.OperationalError("injected state failure")
        return (EvidenceDeletionExecution("evidence/a", "deleted", "retention_expired"),)


class _RuntimeStub:
    def __init__(self, executor: object) -> None:
        self._evidence_deletion = executor


def _runner(executor: object, *, stop_after_waits: int) -> _RuntimeCycleRunner:
    runtime = cast(AutonomousRuntime, _RuntimeStub(executor))
    cycle_stop = _CycleStub(stop_after_waits=stop_after_waits)
    runner = _RuntimeCycleRunner.__new__(_RuntimeCycleRunner)
    runner._runtime = runtime
    runner._cycle_stop = cycle_stop  # type: ignore[assignment]
    runner._stop_requested = cycle_stop.is_set  # type: ignore[method-assign]
    return runner


class EvidenceDeletionSchedulingTests(unittest.TestCase):
    def test_drain_results_are_recorded_each_cycle(self) -> None:
        executor = _DeletionStub(fail=False)
        with self.assertLogs("edge_runtime", level="INFO") as logs:
            _runner(executor, stop_after_waits=2)._run_evidence_deletion()
        self.assertEqual(executor.calls, 2)
        self.assertTrue(all("scope=('evidence/a',)" in line for line in logs.output))
        self.assertTrue(
            all("started_at=" in line and "finished_at=" in line for line in logs.output)
        )

    def test_unknown_state_stops_deletion_without_retrying(self) -> None:
        executor = _DeletionStub(fail=True)
        with self.assertLogs("edge_runtime", level="ERROR") as logs:
            _runner(executor, stop_after_waits=2)._run_evidence_deletion()
        self.assertEqual(executor.calls, 1)
        self.assertIn("status=stopped", logs.output[0])
        self.assertIn("deleted=unknown", logs.output[0])
        self.assertIn("scope=registered_evidence", logs.output[0])
        self.assertIn("started_at=", logs.output[0])
        self.assertIn("finished_at=", logs.output[0])


if __name__ == "__main__":
    unittest.main()
