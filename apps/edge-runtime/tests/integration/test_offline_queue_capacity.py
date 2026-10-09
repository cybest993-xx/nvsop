"""SQLite pending capacity must fail closed without discarding its unique offline copies."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from store_harness import ANCHOR, STATION, STEPS, FakeClock, action, opening_state, supervisor

from edge_runtime.judgment.model import HostInstant
from edge_runtime.local_state import (
    QueueCapacity,
    QueueCapacityError,
    ReportContext,
    open_local_state,
)


class OfflineQueueCapacityTests(unittest.TestCase):
    def test_report_limit_preserves_violation_and_releases_only_after_ack(self) -> None:
        with TemporaryDirectory() as directory:
            database = open_local_state(
                str(Path(directory) / "state.sqlite"),
                queue_capacity=QueueCapacity(reports=1, evidence=10, observations=10, health=10),
            )
            try:
                owner = database.station(STATION)
                driver = supervisor(opening_state(), FakeClock(now=ANCHOR + 2.0), owner)
                driver.receive(action(STEPS[0], at=ANCHOR))
                driver.receive(action(STEPS[2], at=ANCHOR + 1.0))
                before = driver.state
                (report,) = owner.pending_reports()
                original_violations = owner.latched_violations(instance_id=1)
                owner.record_report_failure(
                    report.queue_id, at=HostInstant(ANCHOR + 1.0), error="unsupported protocol"
                )
                status = {entry.name: entry for entry in database.queue_status()}
                self.assertEqual(
                    (1, report.queue_id, "unsupported protocol", 1),
                    (
                        status["report"].depth,
                        status["report"].oldest_queue_id,
                        status["report"].last_failure,
                        status["report"].limit,
                    ),
                )

                with (
                    self.assertLogs("edge_runtime", level="ERROR") as logs,
                    self.assertRaisesRegex(QueueCapacityError, "report"),
                ):
                    driver.terminate_instance()
                self.assertTrue(
                    any("edge.local_queue.capacity_exhausted" in x for x in logs.output)
                )
                self.assertEqual(before, driver.state)
                self.assertEqual(
                    (report.queue_id,), tuple(x.queue_id for x in owner.pending_reports())
                )
                self.assertEqual(original_violations, owner.latched_violations(instance_id=1))

                owner.mark_reported(report.queue_id, at=HostInstant(ANCHOR + 2.0))
                self.assertEqual(0, {x.name: x for x in database.queue_status()}["report"].depth)
                ended = driver.terminate_instance()
                self.assertEqual(1, len(ended.decisions))
                self.assertEqual(1, len(owner.pending_reports()))
            finally:
                database.close()

    def test_evidence_remote_copy_is_not_registration_ack(self) -> None:
        with TemporaryDirectory() as directory:
            database = open_local_state(
                str(Path(directory) / "state.sqlite"),
                queue_capacity=QueueCapacity(reports=10, evidence=1, observations=10, health=10),
            )
            try:
                owner = database.station(STATION)
                driver = supervisor(opening_state(), FakeClock(now=ANCHOR + 2.0), owner)
                driver.receive(action(STEPS[0], at=ANCHOR))
                driver.receive(action(STEPS[2], at=ANCHOR + 1.0))
                (item,) = owner.pending_evidence()
                owner.mark_evidence_uploaded(
                    item.queue_id, at=HostInstant(ANCHOR + 1.0), remote_reference="remote-copy"
                )
                self.assertEqual(
                    1, {row.name: row for row in database.queue_status()}["evidence"].depth
                )
                owner.record_evidence_slice(
                    item.queue_id,
                    at=HostInstant(ANCHOR + 2.0),
                    media_results="[]",
                    covered_from=item.start.seconds,
                    covered_to=item.end.seconds,
                )
                self.assertTrue(
                    owner.mark_evidence_registered(
                        item.queue_id,
                        at=HostInstant(ANCHOR + 3.0),
                        expected_media_results="[]",
                    )
                )
                self.assertEqual(
                    0, {row.name: row for row in database.queue_status()}["evidence"].depth
                )
            finally:
                database.close()

    def test_observation_and_health_limits_preserve_failed_pending_until_ack(self) -> None:
        context = ReportContext(
            host_id="host-a",
            station_id=STATION,
            backends=(),
            template_version_id=None,
            template_sha256=None,
            configuration_revision=None,
            configuration_sha256=None,
            configuration_json=None,
        )
        state = open_local_state(
            ":memory:",
            queue_capacity=QueueCapacity(reports=10, evidence=10, observations=1, health=1),
        )
        self.addCleanup(state.close)
        owner = state.station(STATION, report_context=context)
        owner.enqueue_observation(
            instance_id=1,
            source="external_signal",
            signal="part_arrived",
            source_time=None,
            source_anchor=None,
            observed_at=ANCHOR,
            backend=None,
        )
        (observation,) = owner.pending_observation_reports()
        owner.record_observation_failure(
            observation.queue_id, at=HostInstant(ANCHOR), error="incompatible report version"
        )
        with (
            self.assertLogs("edge_runtime", level="ERROR"),
            self.assertRaisesRegex(QueueCapacityError, "observation"),
        ):
            owner.enqueue_observation(
                instance_id=1,
                source="external_signal",
                signal="part_left",
                source_time=None,
                source_anchor=None,
                observed_at=ANCHOR + 1,
                backend=None,
            )
        self.assertEqual(
            (observation.queue_id,),
            tuple(item.queue_id for item in owner.pending_observation_reports()),
        )
        owner.enqueue_health(
            stream_id="stream-a",
            status="source_error",
            reason_code=None,
            detail=None,
            occurred_at="2026-10-10T00:00:00Z",
            source_anchor=None,
            anchor_offset=None,
        )
        (health,) = owner.pending_health_reports()
        owner.record_health_failure(health.queue_id, at=HostInstant(ANCHOR), error="center offline")
        with (
            self.assertLogs("edge_runtime", level="ERROR"),
            self.assertRaisesRegex(QueueCapacityError, "health"),
        ):
            owner.enqueue_health(
                stream_id="stream-b",
                status="delivering",
                reason_code=None,
                detail=None,
                occurred_at="2026-10-10T00:00:01Z",
                source_anchor=None,
                anchor_offset=None,
            )
        statuses = {entry.name: entry for entry in state.queue_status()}
        self.assertEqual(
            (1, observation.queue_id, "incompatible report version"),
            (
                statuses["observation"].depth,
                statuses["observation"].oldest_queue_id,
                statuses["observation"].last_failure,
            ),
        )
        self.assertEqual(
            (1, health.queue_id, "center offline"),
            (
                statuses["health"].depth,
                statuses["health"].oldest_queue_id,
                statuses["health"].last_failure,
            ),
        )
        owner.mark_observation_reported(observation.queue_id, at=HostInstant(ANCHOR + 2))
        owner.mark_health_reported(health.queue_id, at=HostInstant(ANCHOR + 2))
        self.assertEqual(
            (0, 0),
            tuple(
                stat.depth
                for stat in state.queue_status()
                if stat.name in {"observation", "health"}
            ),
        )
        self.assertEqual(
            1,
            state._connection.execute("SELECT count(*) FROM local_observation_queue").fetchone()[0],
        )
        self.assertEqual(
            1, state._connection.execute("SELECT count(*) FROM local_health_queue").fetchone()[0]
        )

    def test_evidence_capacity_rollback_retains_original_clip_and_report(self) -> None:
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.sqlite")
            database = open_local_state(
                path,
                queue_capacity=QueueCapacity(reports=10, evidence=1, observations=10, health=10),
            )
            try:
                owner = database.station(STATION)
                driver = supervisor(opening_state(), FakeClock(now=ANCHOR + 2.0), owner)
                driver.receive(action(STEPS[0], at=ANCHOR))
                driver.receive(action(STEPS[2], at=ANCHOR + 1.0))
                before = driver.state
                prior_reports = owner.pending_reports()
                prior_evidence = owner.pending_evidence()
                self.assertEqual(1, len(prior_evidence))

                with (
                    self.assertLogs("edge_runtime", level="ERROR"),
                    self.assertRaisesRegex(QueueCapacityError, "evidence"),
                ):
                    driver.terminate_instance()
                self.assertEqual(before, driver.state)
                self.assertEqual(prior_reports, owner.pending_reports())
                self.assertEqual(prior_evidence, owner.pending_evidence())
                stats = {entry.name: entry for entry in database.queue_status()}
                self.assertEqual(1, stats["evidence"].depth)
                self.assertEqual(prior_evidence[0].queue_id, stats["evidence"].oldest_queue_id)
            finally:
                database.close()

            restarted = open_local_state(path)
            try:
                self.assertEqual(prior_reports, restarted.station(STATION).pending_reports())
                self.assertEqual(prior_evidence, restarted.station(STATION).pending_evidence())
            finally:
                restarted.close()
