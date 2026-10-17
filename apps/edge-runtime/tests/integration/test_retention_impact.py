"""S042:SQLite 以真实中心确认 UTC 作为已上报保留期的年龄事实。"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx2

from edge_runtime.center_client import CenterClient
from edge_runtime.judgment.model import HostInstant
from edge_runtime.local_state import ReportContext, open_local_state
from edge_runtime.local_state.schema import MIGRATIONS, apply_migrations
from edge_runtime.reporting import HostReportReconciler
from edge_runtime.reporting_transport import HttpDecisionReportTransport

ACK = "2026-10-14T09:00:00+00:00"
LATER = "2026-10-15T09:00:00+00:00"


def context() -> ReportContext:
    return ReportContext(
        host_id="host-a",
        station_id="station-a",
        backends=(),
        template_version_id=None,
        template_sha256=None,
        configuration_revision=None,
        configuration_sha256=None,
        configuration_json=None,
    )


class RetentionAcknowledgementTests(unittest.TestCase):
    def test_first_acknowledgement_utc_survives_restart_without_touching_pending(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            state = open_local_state(str(path))
            try:
                owner = state.station("station-a", report_context=context())
                for index in range(2):
                    owner.enqueue_health(
                        stream_id=f"stream-{index}",
                        status="healthy",
                        reason_code=None,
                        detail=None,
                        occurred_at="2026-10-01T00:00:00+00:00",
                        source_anchor=None,
                        anchor_offset=None,
                    )
                reports = state.reports()
                acknowledged, pending = reports.pending_health_ids()
                reports.mark_health_reported(acknowledged, at=HostInstant(900), acknowledged_at=ACK)
                reports.mark_health_reported(
                    acknowledged, at=HostInstant(901), acknowledged_at=LATER
                )
                self.assertEqual((pending,), reports.pending_health_ids())
            finally:
                state.close()
            reopened = open_local_state(str(path))
            try:
                self.assertEqual((pending,), reopened.reports().pending_health_ids())
                with sqlite3.connect(path) as connection:
                    rows = connection.execute(
                        "SELECT queue_id, acknowledged_at FROM local_health_queue ORDER BY queue_id"
                    ).fetchall()
                self.assertEqual([(acknowledged, ACK), (pending, None)], rows)
            finally:
                reopened.close()

    def test_v19_upgrade_does_not_guess_acknowledgement_utc(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            with sqlite3.connect(path, isolation_level=None) as connection:
                self.assertEqual(apply_migrations(connection, MIGRATIONS[:19]), 19)
                connection.execute(
                    """INSERT INTO local_health_queue
                       (station_id, status, occurred_at, report_host_id, sent_at)
                       VALUES ('station-a', 'healthy',
                               '2026-10-01T00:00:00+00:00', 'host-a', 777)"""
                )
            state = open_local_state(str(path))
            state.close()
            with sqlite3.connect(path) as connection:
                # V22 (#214) 追加后, 打开即完成全部迁移; 本测试只关心 acknowledged_at 不被猜测。
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 22)
                self.assertEqual(
                    connection.execute(
                        "SELECT sent_at, acknowledged_at FROM local_health_queue"
                    ).fetchall(),
                    [(777.0, None)],
                )
                for table in (
                    "local_report_queue",
                    "local_observation_queue",
                    "local_health_queue",
                    "local_disposal",
                ):
                    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                    self.assertIn("acknowledged_at", columns)

    def test_production_http_success_reads_ack_clock_only_after_response(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            state = open_local_state(str(path))
            self.addCleanup(state.close)
            state.station("station-a", report_context=context()).enqueue_health(
                stream_id="stream-a",
                status="healthy",
                reason_code=None,
                detail=None,
                occurred_at="2026-10-01T00:00:00+00:00",
                source_anchor=None,
                anchor_offset=None,
            )
            status = 503
            events: list[str] = []

            def respond(request: httpx2.Request) -> httpx2.Response:
                self.assertEqual(request.url.path, "/api/v1/monitor/health")
                events.append(f"response:{status}")
                return httpx2.Response(status, json={})

            def acknowledged_now() -> str:
                events.append("ack-clock")
                return ACK

            with patch(
                "edge_runtime.center_client.sign_host_identity_request", return_value="signature"
            ):
                client = CenterClient(
                    center_url="http://center.example",
                    host_id="host-a",
                    host_private_key="unused",  # pragma: allowlist secret
                    timeout=1.0,
                    transport=httpx2.MockTransport(respond),
                )
                reconciler = HostReportReconciler(
                    reports=state.reports(),
                    transport=HttpDecisionReportTransport(client=client, host_id="host-a"),
                    utc_now=acknowledged_now,
                )
                first = reconciler.flush(now=HostInstant(900), reported_at=LATER)
                self.assertFalse(first[0].sent)
                with sqlite3.connect(path) as connection:
                    self.assertEqual(
                        connection.execute(
                            "SELECT sent_at, acknowledged_at FROM local_health_queue"
                        ).fetchone(),
                        (None, None),
                    )
                self.assertEqual(events, ["response:503"])
                status = 200
                second = reconciler.flush(now=HostInstant(901), reported_at=LATER)
                self.assertTrue(second[0].sent)
                self.assertEqual(events, ["response:503", "response:200", "ack-clock"])
                with sqlite3.connect(path) as connection:
                    self.assertEqual(
                        connection.execute(
                            "SELECT sent_at, acknowledged_at, report_reported_at "
                            "FROM local_health_queue"
                        ).fetchone(),
                        (901.0, ACK, LATER),
                    )
                self.assertEqual(reconciler.flush(now=HostInstant(902), reported_at=LATER), ())
                self.assertEqual(events, ["response:503", "response:200", "ack-clock"])
