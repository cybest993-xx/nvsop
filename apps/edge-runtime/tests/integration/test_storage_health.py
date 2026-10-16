"""真实 SQLite/supervisor 接缝上的存储失效、滞回与安全数据保护。"""

import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from store_harness import ANCHOR, STATION, STEPS, FakeClock, action, opening_state, supervisor

from edge_runtime.judgment.reasons import ReasonCode, Verdict
from edge_runtime.local_state import LocalState, ReportContext, open_local_state
from edge_runtime.runtime import AutonomousStation
from edge_runtime.storage_health import (
    DiskHealthConfiguration,
    DiskHealthWatch,
    StorageHealthEvent,
    inspect_disk,
)
from edge_runtime.supervisor.inputs import StorageHealthObserved


def disk(role: str, *, impaired: bool) -> StorageHealthObserved:
    return StorageHealthObserved(
        StorageHealthEvent(
            role,
            f"/synthetic/{role}",
            impaired,
            91 if impaired else 75,
            100,
            9 if impaired else 25,
            90,
            80,
            None,
        )
    )


def context() -> ReportContext:
    return ReportContext("host-a", STATION, (), None, None, None, None, None)


class StorageHealthIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_failure_is_indeterminate_and_recovery_never_retracts_violations(self) -> None:
        state = open_local_state(str(self.root / "state.sqlite"))
        self.addCleanup(state.close)
        owner = state.station(STATION, report_context=context())
        driver = supervisor(opening_state(), FakeClock(), owner)
        driver.receive(action(STEPS[0], ANCHOR))
        driver.receive(action(STEPS[2], ANCHOR + 1))
        violations = owner.latched_violations(instance_id=1)
        self.assertTrue(violations)
        pending = {item.queue_id for item in owner.pending_evidence()}
        driver.receive(disk("recording", impaired=True))
        driver.receive(disk("evidence", impaired=True))
        driver.receive(disk("recording", impaired=False))
        self.assertIn(ReasonCode.STORAGE_UNAVAILABLE, driver.state.active_impairments)
        driver.receive(action(STEPS[1], ANCHOR + 2))
        self.assertIs(Verdict.INDETERMINATE, owner.pending_reports()[-1].decision.verdict)
        driver.receive(disk("evidence", impaired=False))
        self.assertNotIn(ReasonCode.STORAGE_UNAVAILABLE, driver.state.active_impairments)
        self.assertEqual(violations, owner.latched_violations(instance_id=1))
        self.assertTrue(pending <= {item.queue_id for item in owner.pending_evidence()})
        facts = owner.pending_health_reports()
        self.assertEqual(4, len(facts))
        self.assertEqual("STORAGE_UNAVAILABLE", facts[0].reason_code)
        self.assertEqual(90, json.loads(facts[0].detail or "{}")["high_watermark_percent"])
        self.assertEqual("storage_healthy", facts[-1].status)

    def test_runtime_stops_before_consuming_actions_when_sqlite_is_read_only(self) -> None:
        path = self.root / "state.sqlite"
        initialized = open_local_state(str(path))
        initialized.close()
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)
        connection.row_factory = sqlite3.Row
        state = LocalState(connection)
        self.addCleanup(state.close)

        class Source:
            ended = False
            read = False
            closed = False

            def next_input(self, timeout: float | None) -> None:
                self.read = True
                raise AssertionError("consumed an action despite unavailable local persistence")

            def close(self) -> None:
                self.closed = True

        source = Source()
        driver = supervisor(
            opening_state(), FakeClock(), state.station(STATION, report_context=context())
        )
        station = AutonomousStation(
            station_id=STATION,
            supervisor=driver,
            source=source,
            connector_runtimes=(),
            disk_health=DiskHealthWatch(
                paths={"local_state": self.root}, configuration=DiskHealthConfiguration()
            ),
        )
        with (
            self.assertLogs("edge_runtime", level="ERROR") as logs,
            self.assertRaises(sqlite3.OperationalError),
        ):
            station.run_forever(should_stop=lambda: False)
        self.assertFalse(source.read)
        self.assertTrue(source.closed)
        self.assertIn("edge.storage.write_failed", "\n".join(logs.output))

    def test_real_probe_leaves_all_existing_media_and_files_untouched(self) -> None:
        protected = self.root / "pending-evidence.mp4"
        protected.write_bytes(b"synthetic protected media")
        before = list(self.root.iterdir())
        usage = inspect_disk(self.root)
        self.assertTrue(0 <= usage.used_percent <= 100)
        self.assertEqual(before, list(self.root.iterdir()))
        self.assertEqual(b"synthetic protected media", protected.read_bytes())
        with self.assertRaises(OSError):
            inspect_disk(self.root / "not-mounted")
        # 真实目录权限失败, 不是替换 os.statvfs 或 SQLite 的假成功。
        readonly = self.root / "read-only-evidence"
        readonly.mkdir(mode=0o500)
        try:
            with self.assertRaises(PermissionError):
                inspect_disk(readonly)
        finally:
            readonly.chmod(0o700)

    def test_real_sqlite_write_failure_cannot_publish_pass(self) -> None:
        for failure in ("readonly", "full"):
            with self.subTest(failure=failure):
                path = self.root / f"{failure}.sqlite"
                initialized = open_local_state(str(path))
                initialized.close()
                connection = sqlite3.connect(
                    f"file:{path}?mode={'ro' if failure == 'readonly' else 'rw'}",
                    uri=True,
                    isolation_level=None,
                )
                connection.row_factory = sqlite3.Row
                if failure == "full":
                    pages = connection.execute("PRAGMA page_count").fetchone()[0]
                    connection.execute(f"PRAGMA max_page_count={pages}")
                state = LocalState(connection)
                try:
                    owner = state.station(STATION, report_context=context())
                    driver = supervisor(opening_state(), FakeClock(), owner)
                    event = disk("local_state", impaired=True)
                    if failure == "full":
                        event = StorageHealthObserved(
                            replace(event.event, error="synthetic I/O error " * 8192)
                        )
                    with self.assertRaises(sqlite3.Error) as raised:
                        driver.receive(event)
                    self.assertEqual(
                        sqlite3.SQLITE_READONLY if failure == "readonly" else sqlite3.SQLITE_FULL,
                        raised.exception.sqlite_errorcode,
                    )
                    self.assertEqual((), owner.pending_reports())
                    self.assertIsNone(driver.state.instance)
                finally:
                    state.close()
