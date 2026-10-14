"""S042:真实 SQLite 已上报记录的只读窗口、保护、范围与未知事实。"""

from __future__ import annotations

import json
import sqlite3
import threading
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from edge_runtime.center_client import CenterClient, CenterResponse
from edge_runtime.evidence_access import create_server
from edge_runtime.local_state import open_local_state
from edge_runtime.local_state.retention import (
    ReportedImpactUnavailableError,
    read_reported_impact,
)
from nvsop_contracts import (
    RETENTION_STATION_POLICY_CAPABILITY,
    ResolvedRetentionPolicy,
    ResolvedRetentionRule,
)
from nvsop_contracts.retention_impact import ReportedImpactQuery
from test_confirmed_runtime_composition import _bundle


class LocalRetentionImpactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.sqlite"
        state = open_local_state(str(self.path))
        self.group = ResolvedRetentionPolicy(
            evidence_pass=ResolvedRetentionRule("duration", 90 * 86400),
            evidence_fail=ResolvedRetentionRule("permanent"),
            evidence_indeterminate=ResolvedRetentionRule("duration", 90 * 86400),
            record_detail=ResolvedRetentionRule("duration", 90 * 86400),
            record_compression_age=ResolvedRetentionRule("duration", 7 * 86400),
            record_aggregate_age=ResolvedRetentionRule("duration", 90 * 86400),
            disposal=ResolvedRetentionRule("permanent"),
            local_reported=ResolvedRetentionRule("duration", 14 * 86400),
        )
        base = _bundle()
        first = replace(base.stations[0], retention=self.group)
        self.bundle = replace(
            base,
            required_capabilities=tuple(
                sorted((*base.required_capabilities, RETENTION_STATION_POLICY_CAPABILITY))
            ),
            stations=(first, replace(first, station_id="station-b")),
        )
        state.configuration().confirm(self.bundle, confirmed_at=1.0)
        state.close()
        self.query = ReportedImpactQuery(
            host_id="host-a",
            configuration_revision=self.bundle.config_revision,
            configuration_sha256=self.bundle.effective_sha256,
            station_id=None,
            excluded_stations=("station-b",),
            baseline_seconds=14 * 86400,
            proposed_seconds=86400,
            reference_time=datetime(2026, 10, 14, 12, tzinfo=UTC),
        )

    def _health(
        self,
        connection: sqlite3.Connection,
        *,
        station: str = "station-a",
        ack: str | None,
        sent: float | None = 10.0,
    ) -> None:
        connection.execute(
            """INSERT INTO local_health_queue
               (station_id,status,occurred_at,report_host_id,sent_at,acknowledged_at)
               VALUES (?, 'healthy', '2026-09-01T00:00:00Z', 'host-a', ?, ?)""",
            (station, sent, ack),
        )

    def test_counts_only_acknowledged_window_and_keeps_protected_groups_disjoint(self) -> None:
        with sqlite3.connect(self.path) as connection:
            self._health(connection, ack="2026-10-10T12:00:00Z")
            self._health(connection, station="station-b", ack="2026-10-10T12:00:00Z")
            self._health(connection, ack="2026-10-14T12:00:00Z")
            self._health(connection, ack="2026-09-01T12:00:00Z")
            self._health(connection, ack=None, sent=None)
            connection.execute(
                """INSERT INTO local_observation_queue
                   (station_id,instance_id,source,signal,observed_at,report_host_id,
                    sent_at,acknowledged_at)
                   VALUES ('station-a', 123, 'fixture', 'action', 1, 'host-a', 10,
                           '2026-10-10T12:00:00Z')"""
            )
            connection.row_factory = sqlite3.Row
            health = connection.execute(
                "SELECT * FROM local_health_queue WHERE queue_id=1"
            ).fetchone()
            expected_bytes = len(
                json.dumps(
                    dict(health), ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
            )
            before = connection.execute("SELECT COUNT(*) FROM local_health_queue").fetchone()[0]
        result = read_reported_impact(self.path, query=self.query)
        self.assertEqual(result.exposed.objects, 1)
        self.assertEqual(result.exposed.known_bytes, expected_bytes)
        self.assertEqual(result.protected.objects, 1)
        self.assertGreater(result.protected.known_bytes, 0)
        self.assertEqual(result.exposed.earliest, datetime(2026, 10, 10, 12, tzinfo=UTC))
        self.assertEqual(result.exposed.latest, result.exposed.earliest)
        self.assertEqual(result.configuration_sha256, self.bundle.effective_sha256)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM local_health_queue").fetchone()[0], before
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM local_observation_queue").fetchone()[0], 1
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM local_health_queue WHERE sent_at IS NULL"
                ).fetchone()[0],
                1,
            )

    def test_only_pending_evidence_registration_protects_reported_structured_rows(self) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """INSERT INTO local_sop_instance
                (station_id,instance_id,opened_at,last_observation_at,seen,expected_index,
                 impairments,settled,closed_at,lifecycle)
                VALUES ('station-a',123,1,2,'[]',0,'[]','[]',2,'closed')"""
            )
            connection.execute(
                """INSERT INTO local_observation_queue
                (station_id,instance_id,source,signal,observed_at,report_host_id,
                 sent_at,acknowledged_at)
                VALUES ('station-a',123,'fixture','action',1,'host-a',10,
                        '2026-10-10T12:00:00Z')"""
            )
            connection.execute(
                """INSERT INTO local_evidence_queue
                (station_id,instance_id,anchor,window_from,window_to)
                VALUES ('station-a',123,2,1,3)"""
            )
        before = read_reported_impact(self.path, query=self.query)
        self.assertEqual((before.exposed.objects, before.protected.objects), (0, 1))
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE local_evidence_queue SET registered_at=20")
        media = self.path.parent / "registered-evidence.mp4"
        media.write_bytes(b"synthetic registered evidence remains authoritative locally")
        database_before = self.path.read_bytes()
        after = read_reported_impact(self.path, query=self.query)
        self.assertEqual((after.exposed.objects, after.protected.objects), (1, 0))
        self.assertEqual(after.exposed.known_bytes, before.protected.known_bytes)
        self.assertEqual(self.path.read_bytes(), database_before)
        self.assertEqual(
            media.read_bytes(), b"synthetic registered evidence remains authoritative locally"
        )

    def test_unknown_acknowledgement_and_stale_configuration_are_refusals_not_zero(self) -> None:
        with sqlite3.connect(self.path) as connection:
            self._health(connection, ack=None)
        with self.assertRaisesRegex(ReportedImpactUnavailableError, "acknowledgement_utc_unknown"):
            read_reported_impact(self.path, query=self.query)
        with self.assertRaisesRegex(ReportedImpactUnavailableError, "configuration_mismatch"):
            read_reported_impact(
                self.path,
                query=replace(self.query, configuration_revision=self.bundle.config_revision + 1),
            )
        with self.assertRaisesRegex(ReportedImpactUnavailableError, "baseline_mismatch"):
            read_reported_impact(self.path, query=replace(self.query, baseline_seconds=7 * 86400))

    def test_http_impact_rechecks_a_separate_scope_and_preserves_database_and_media(self) -> None:
        with sqlite3.connect(self.path) as connection:
            self._health(connection, ack="2026-10-10T12:00:00Z")
        media = self.path.parent / "evidence.mp4"
        media.write_bytes(b"synthetic evidence, not a recording inventory")
        before_database = self.path.read_bytes()
        before_media = media.read_bytes()
        calls: list[tuple[str, dict[str, object]]] = []
        query = self.query

        class Client:
            status = 200
            grant: object = query.to_wire()

            def post(self, path: str, body: dict[str, object]) -> CenterResponse:
                calls.append((path, body))
                return CenterResponse(self.status, json.dumps(self.grant).encode())

        client = Client()
        server = create_server(
            address=("127.0.0.1", 0),
            client=cast(CenterClient, client),
            directory=self.path.parent,
            web_origin="http://web.local",
            local_state_path=self.path,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(context: str) -> tuple[int, dict[str, str], dict[str, object]]:
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request("GET", f"/retention/reported-impact?context={context}")
                response = connection.getresponse()
                return response.status, dict(response.getheaders()), json.loads(response.read())
            finally:
                connection.close()

        try:
            status, headers, body = request("retention-ticket")
            self.assertEqual(status, 200)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(body["query"], query.to_wire())
            self.assertEqual(body["bytes_basis"], "serialized_rows")
            self.assertEqual(cast(dict[str, object], body["exposed"])["objects"], 1)
            self.assertEqual(
                calls,
                [
                    (
                        "/api/v1/retention/reported-impact/access-check",
                        {"context": "retention-ticket"},
                    )
                ],
            )
            client.status, client.grant = 403, {"detail": "access_denied"}
            self.assertEqual(request("media-ticket")[0], 403)
            client.status = 410
            client.grant = {"detail": "context_expired"}
            self.assertEqual(request("expired-ticket")[0], 410)
            client.status, client.grant = (
                200,
                {"reference": "evidence.mp4", "size": len(before_media)},
            )
            self.assertEqual(request("wrong-grant")[0], 503)
            client.grant = replace(
                query, configuration_revision=query.configuration_revision + 1
            ).to_wire()
            self.assertEqual(request("stale-config")[2]["error"], "configuration_mismatch")
            self.assertEqual(self.path.read_bytes(), before_database)
            client.grant = query.to_wire()
            with sqlite3.connect(self.path) as connection:
                self._health(connection, ack=None)
            self.assertEqual(request("unknown-age")[2]["error"], "acknowledgement_utc_unknown")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual(media.read_bytes(), before_media)
        # 只有上述显式夹具插入可改变持久化数据。
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM local_health_queue").fetchone()[0], 2
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM local_health_queue WHERE sent_at IS NULL"
                ).fetchone()[0],
                0,
            )
        self.assertTrue(before_database)
