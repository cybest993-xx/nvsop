"""只读本地保留估算的机器边界,不接受失配或不完整请求。"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from nvsop_contracts.retention_impact import ReportedImpactQuery


class ReportedImpactQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.query = ReportedImpactQuery(
            host_id="host-a",
            configuration_revision=7,
            configuration_sha256="a" * 64,
            station_id=None,
            excluded_stations=("station-b",),
            baseline_seconds=14 * 86400,
            proposed_seconds=86400,
            reference_time=datetime(2026, 10, 14, 12, tzinfo=UTC),
        )

    def test_round_trip_binds_every_source_scope_and_window_field(self) -> None:
        self.assertEqual(ReportedImpactQuery.from_wire(self.query.to_wire()), self.query)
        wire = self.query.to_wire()
        wire["baseline_seconds"] = None
        self.assertIsNone(ReportedImpactQuery.from_wire(wire).baseline_seconds)

    def test_invalid_or_incomplete_queries_are_not_repaired(self) -> None:
        for name, value in (
            ("host_id", ""),
            ("configuration_revision", True),
            ("configuration_revision", 0),
            ("configuration_sha256", "g" * 64),
            ("baseline_seconds", True),
            ("proposed_seconds", True),
            ("proposed_seconds", -1),
            ("proposed_seconds", 14 * 86400),
            ("reference_time", "2026-10-14T12:00:00"),
            ("excluded_stations", ["station-b", "station-b"]),
            ("station_id", "station-b"),
        ):
            with self.subTest(name=name, value=value):
                wire = self.query.to_wire()
                wire[name] = value
                with self.assertRaises(ValueError):
                    ReportedImpactQuery.from_wire(wire)
        extra = self.query.to_wire() | {"reference": "private/path"}
        with self.assertRaises(ValueError):
            ReportedImpactQuery.from_wire(extra)
        missing = self.query.to_wire()
        del missing["baseline_seconds"]
        with self.assertRaises(ValueError):
            ReportedImpactQuery.from_wire(missing)
