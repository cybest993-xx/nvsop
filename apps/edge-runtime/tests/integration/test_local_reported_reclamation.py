"""S045 (#208): 本机已上报队列裁剪只按最后确认的 local_reported 删除逻辑行。

真实 SQLite + 临时文件: 断言公开接缝 `LocalState.reclaim_reported` 的删除/跳过范围与原因、
安全重试、待上报/待登记/已登记证据媒体保留, 以及队列 INTEGER PRIMARY KEY 事件身份不被复用。
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from nvsop_contracts import (
    RETENTION_STATION_POLICY_CAPABILITY,
    ConfigurationBundle,
    ResolvedRetentionPolicy,
    ResolvedRetentionRule,
)
from test_confirmed_runtime_composition import _bundle

from edge_runtime.local_state import (
    LocalState,
    ReclaimedQueue,
    ReportContext,
    ReportedReclamation,
    open_local_state,
)
from edge_runtime.local_state.disposal import DisposalIntent, StoredDisposalResult
from edge_runtime.local_state.schema import migrate

STATION = "station-a"
OTHER_STATION = "station-b"
HOST = "host-a"
NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
EXPIRED_ACK = "2026-09-01T12:00:00Z"
FRESH_ACK = "2026-10-14T11:00:00Z"
NAIVE_ACK = "2020-01-01T00:00:00"
"""无时区的本地时刻: SQLite julianday 可解析, 但本机权威确认语义只接受 UTC。"""


def _retention_group(*, local_reported_seconds: int = 14 * 86400) -> ResolvedRetentionPolicy:
    return ResolvedRetentionPolicy(
        evidence_pass=ResolvedRetentionRule("duration", 90 * 86400),
        evidence_fail=ResolvedRetentionRule("permanent"),
        evidence_indeterminate=ResolvedRetentionRule("duration", 90 * 86400),
        record_detail=ResolvedRetentionRule("duration", 90 * 86400),
        record_compression_age=ResolvedRetentionRule("duration", 7 * 86400),
        record_aggregate_age=ResolvedRetentionRule("duration", 90 * 86400),
        disposal=ResolvedRetentionRule("permanent"),
        local_reported=ResolvedRetentionRule("duration", local_reported_seconds),
    )


def _bundle_with_retention(*, stations: tuple[str, ...] = (STATION,)) -> ConfigurationBundle:
    group = _retention_group()
    base = _bundle()
    replacements = []
    for station_id in stations:
        replacements.append(replace(base.stations[0], station_id=station_id, retention=group))
    return replace(
        base,
        required_capabilities=tuple(
            sorted((*base.required_capabilities, RETENTION_STATION_POLICY_CAPABILITY))
        ),
        stations=tuple(replacements),
    )


def _instance(connection: sqlite3.Connection, instance_id: int, *, closed: bool) -> None:
    connection.execute(
        """
        INSERT INTO local_sop_instance
            (station_id, instance_id, opened_at, last_observation_at, seen,
             expected_index, impairments, settled, closed_at, lifecycle)
        VALUES (?, ?, 1, 2, '[]', 0, '[]', '[]', ?, ?)
        """,
        (STATION, instance_id, 2 if closed else None, "closed" if closed else None),
    )


def _decision(connection: sqlite3.Connection, decision_id: int, instance_id: int) -> None:
    connection.execute(
        """
        INSERT INTO local_decision
            (decision_id, station_id, instance_id, verdict, reasons, lifecycle,
             evidence_anchor, evidence_from, evidence_to)
        VALUES (?, ?, ?, 'pass', '[]', 'stays_open', 1, 1, 1)
        """,
        (decision_id, STATION, instance_id),
    )


def _report_row(
    connection: sqlite3.Connection,
    queue_id: int,
    decision_id: int,
    *,
    sent: float | None,
    acknowledged_at: str | None,
) -> None:
    connection.execute(
        """
        INSERT INTO local_report_queue
            (queue_id, station_id, decision_id, report_kind, report_host_id, sent_at,
             acknowledged_at)
        VALUES (?, ?, ?, 'decision', ?, ?, ?)
        """,
        (queue_id, STATION, decision_id, HOST, sent, acknowledged_at),
    )


def _observation_row(
    connection: sqlite3.Connection,
    queue_id: int,
    instance_id: int,
    *,
    sent: float | None,
    acknowledged_at: str | None,
) -> None:
    connection.execute(
        """
        INSERT INTO local_observation_queue
            (queue_id, station_id, instance_id, source, signal, observed_at,
             report_host_id, sent_at, acknowledged_at)
        VALUES (?, ?, ?, 'fixture', 'action', 1, ?, ?, ?)
        """,
        (queue_id, STATION, instance_id, HOST, sent, acknowledged_at),
    )


def _health_row(
    connection: sqlite3.Connection,
    queue_id: int,
    *,
    station: str = STATION,
    sent: float | None,
    acknowledged_at: str | None,
) -> None:
    connection.execute(
        """
        INSERT INTO local_health_queue
            (queue_id, station_id, status, occurred_at, report_host_id, sent_at,
             acknowledged_at)
        VALUES (?, ?, 'healthy', '2026-09-01T00:00:00Z', ?, ?, ?)
        """,
        (queue_id, station, HOST, sent, acknowledged_at),
    )


class LocalReportedReclamationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "state.sqlite"
        self.state = open_local_state(str(self.path))

    def tearDown(self) -> None:
        self.state.close()

    def _seed_structured_rows(self) -> None:
        with sqlite3.connect(self.path) as connection:
            for instance_id, closed in (
                (1, True),
                (2, True),
                (3, False),
                (4, True),
                (5, True),
                (6, True),
                (8, True),
            ):
                _instance(connection, instance_id, closed=closed)
            for decision_id, instance_id in ((1, 1), (2, 2), (3, 3), (4, 4), (5, 5), (6, 6)):
                _decision(connection, decision_id, instance_id)
            # report: 10 删除, 11 待上报, 12 开放实例保护, 13 未知确认时刻, 14 删除,
            #         15 最高 queue_id 身份保留
            _report_row(connection, 10, 1, sent=10.0, acknowledged_at=EXPIRED_ACK)
            _report_row(connection, 11, 2, sent=None, acknowledged_at=None)
            _report_row(connection, 12, 3, sent=10.0, acknowledged_at=EXPIRED_ACK)
            _report_row(connection, 13, 4, sent=10.0, acknowledged_at=None)
            _report_row(connection, 14, 5, sent=10.0, acknowledged_at=EXPIRED_ACK)
            _report_row(connection, 15, 6, sent=10.0, acknowledged_at=EXPIRED_ACK)
            # observation: 20 删除, 21 待上报, 22 待登记证据引用保护, 23 最高 queue_id 身份保留
            _observation_row(connection, 20, 1, sent=10.0, acknowledged_at=EXPIRED_ACK)
            _observation_row(connection, 21, 1, sent=None, acknowledged_at=None)
            _observation_row(connection, 22, 8, sent=10.0, acknowledged_at=EXPIRED_ACK)
            _observation_row(connection, 23, 1, sent=10.0, acknowledged_at=EXPIRED_ACK)
            # health: 30 删除, 31 最高 queue_id 身份保留
            _health_row(connection, 30, sent=10.0, acknowledged_at=EXPIRED_ACK)
            _health_row(connection, 31, sent=10.0, acknowledged_at=EXPIRED_ACK)
            # 证据: 40 待登记保护实例 8 的结构化行; 41 已登记不触发结构化保护
            connection.execute(
                """
                INSERT INTO local_evidence_queue
                    (queue_id, station_id, instance_id, anchor, window_from, window_to,
                     registered_at)
                VALUES (40, ?, 8, 2, 1, 3, NULL)
                """,
                (STATION,),
            )
            connection.execute(
                """
                INSERT INTO local_evidence_queue
                    (queue_id, station_id, instance_id, anchor, window_from, window_to,
                     registered_at)
                VALUES (41, ?, 1, 2, 1, 3, 20)
                """,
                (STATION,),
            )

    def _seed_acked_disposal_tombstones(self) -> None:
        ledger = self.state.disposal()
        for key in ("disposal-key-1", "disposal-key-2"):
            intent = DisposalIntent(
                station_id=STATION,
                idempotency_key=key,
                connector_id="connector-a",
                point_id="point-out",
                actor="operator",
                requested_state="active",
                violation_ref="violation-1",
                violation_instance_id=1,
                source="connector",
                report_host_id=HOST,
            )
            ledger.ensure_intent(intent)
            ledger.record_result(
                intent, result=StoredDisposalResult(kind="written", detail=None, at=1.0)
            )
            (disposal_id,) = ledger.pending_report_ids()
            ledger.report(disposal_id, reported_at="2026-10-01T00:00:00Z")
            ledger.mark_reported(disposal_id, at=10.0, acknowledged_at=EXPIRED_ACK)

    def test_deletes_only_expired_acknowledged_rows_and_keeps_every_promise(self) -> None:
        self.state.configuration().confirm(_bundle_with_retention(), confirmed_at=1.0)
        self._seed_structured_rows()
        self._seed_acked_disposal_tombstones()
        evidence = self.root / "evidence"
        evidence.mkdir()
        registered_media = evidence / "registered.mp4"
        registered_media.write_bytes(b"synthetic registered evidence bytes")
        pending_media = evidence / "pending.mp4"
        pending_media.write_bytes(b"synthetic pending evidence bytes")

        result = self.state.reclaim_reported(now=NOW)

        self.assertEqual(4, result.deleted)
        self.assertEqual(
            {"report": 2, "observation": 1, "health": 1},
            {row.queue: row.deleted for row in result.rows},
        )
        self.assertIn("retention_protected", result.skip_reasons)
        self.assertIn("acknowledgement_unknown", result.skip_reasons)
        self.assertIn("identity_high_water", result.skip_reasons)
        # 待上报事实仍在公开接缝上可领取。
        self.assertIn(11, self.state.reports().pending_ids())
        self.assertIn(21, self.state.reports().pending_observation_ids())
        # 待登记证据工位与两份媒体文件字节都不受影响。
        self.assertIn(STATION, self.state.pending_evidence_stations())
        self.assertEqual(b"synthetic registered evidence bytes", registered_media.read_bytes())
        self.assertEqual(b"synthetic pending evidence bytes", pending_media.read_bytes())
        # 处置去重账本即使在已上报并确认后也不因裁剪消失 (包括非最高 disposal_id)。
        self.assertIsNotNone(self.state.disposal().result_for(STATION, "disposal-key-1"))
        self.assertIsNotNone(self.state.disposal().result_for(STATION, "disposal-key-2"))
        # 安全重试: 第二次没有任何可删除行。
        self.assertEqual(0, self.state.reclaim_reported(now=NOW).deleted)

    def test_limit_is_a_keyset_page_that_progresses_past_pending_and_protected_rows(self) -> None:
        self.state.configuration().confirm(_bundle_with_retention(), confirmed_at=1.0)
        with sqlite3.connect(self.path) as connection:
            _health_row(connection, 1, sent=None, acknowledged_at=None)  # 待上报
            _health_row(connection, 2, sent=10.0, acknowledged_at=NAIVE_ACK)  # 非法确认时刻
            _health_row(connection, 3, sent=10.0, acknowledged_at=EXPIRED_ACK)  # 可删
            _health_row(connection, 4, sent=10.0, acknowledged_at=EXPIRED_ACK)  # 最高 queue_id

        def health(result: ReportedReclamation) -> ReclaimedQueue:
            return next(row for row in result.rows if row.queue == "health")

        # 每次至多一页 limit 行, 从最小 queue_id 起按主键推进;
        # 待上报与非法确认行只占本页 examined 预算, 不会被跳过也不永久挡住后面的可删行。
        first = health(self.state.reclaim_reported(now=NOW, limit=1))
        self.assertEqual((1, 1), first.queue_id_range)
        self.assertEqual(0, first.deleted)
        self.assertEqual(1, first.skipped)
        self.assertLessEqual(first.deleted + first.skipped, 1)
        self.assertIn("pending", first.skip_reasons)

        second = health(self.state.reclaim_reported(now=NOW, limit=1))
        self.assertEqual((2, 2), second.queue_id_range)
        self.assertEqual(0, second.deleted)
        self.assertLessEqual(second.deleted + second.skipped, 1)
        self.assertIn("acknowledgement_unknown", second.skip_reasons)

        deleted = 0
        for _ in range(3):
            page = health(self.state.reclaim_reported(now=NOW, limit=1))
            self.assertLessEqual(page.deleted + page.skipped, 1)
            deleted += page.deleted
        self.assertEqual(1, deleted)

    def test_invalid_acknowledgement_is_never_deleted_but_valid_utc_still_is(self) -> None:
        self.state.configuration().confirm(_bundle_with_retention(), confirmed_at=1.0)
        with sqlite3.connect(self.path) as connection:
            _health_row(connection, 1, sent=10.0, acknowledged_at=NAIVE_ACK)
            _health_row(connection, 2, sent=10.0, acknowledged_at="not-a-timestamp")
            _health_row(connection, 3, sent=10.0, acknowledged_at=None)
            _health_row(connection, 4, sent=10.0, acknowledged_at=EXPIRED_ACK)
            _health_row(connection, 5, sent=10.0, acknowledged_at=EXPIRED_ACK)

        result = self.state.reclaim_reported(now=NOW)

        # 只有能被权威 UTC 语义解析、过期且非最高 queue_id 的行可删; 缺失/无时区/无法解析一律不删。
        self.assertEqual(1, result.deleted)
        self.assertIn("acknowledgement_unknown", result.skip_reasons)
        self.assertIn("identity_high_water", result.skip_reasons)

    def test_new_queue_rows_keep_strictly_increasing_event_identity(self) -> None:
        self.state.configuration().confirm(_bundle_with_retention(), confirmed_at=1.0)
        self._seed_structured_rows()
        self.state.reclaim_reported(now=NOW)

        context = ReportContext(
            host_id=HOST,
            station_id=STATION,
            backends=(),
            template_version_id=None,
            template_sha256=None,
            configuration_revision=None,
            configuration_sha256=None,
            configuration_json=None,
        )
        station = self.state.station(STATION, report_context=context)
        station.enqueue_health(
            stream_id=None,
            status="healthy",
            reason_code=None,
            detail=None,
            occurred_at="2026-10-14T11:00:00Z",
            source_anchor=None,
            anchor_offset=None,
        )
        station.enqueue_observation(
            instance_id=1,
            source="fixture",
            signal="action",
            source_time=1.0,
            source_anchor=1.0,
            observed_at=1.0,
            backend=None,
        )

        # 最高 queue_id 是身份高水位而被保留, 新事件必须严格更大, 不能复用已上报身份。
        self.assertGreater(max(self.state.reports().pending_health_ids()), 31)
        self.assertGreater(max(self.state.reports().pending_observation_ids()), 23)

    def test_unknown_or_stale_protection_state_skips_without_deleting(self) -> None:
        with sqlite3.connect(self.path) as connection:
            # 确认时刻未知: 无论配置是否已知都不能删。
            _health_row(connection, 1, sent=10.0, acknowledged_at=None)

        # 尚无最后确认配置: 保留期未知, 一行都不能删。
        unconfirmed = self.state.reclaim_reported(now=NOW)
        self.assertEqual(0, unconfirmed.deleted)
        self.assertIn("retention_unknown", unconfirmed.skip_reasons)

        self.state.configuration().confirm(_bundle_with_retention(), confirmed_at=1.0)
        with sqlite3.connect(self.path) as connection:
            _health_row(
                connection, 2, station=OTHER_STATION, sent=10.0, acknowledged_at=EXPIRED_ACK
            )
            _health_row(connection, 3, sent=10.0, acknowledged_at=FRESH_ACK)
            # 最高 queue_id 即使已过期也作为身份高水位保留。
            _health_row(connection, 4, sent=10.0, acknowledged_at=EXPIRED_ACK)

        stale = self.state.reclaim_reported(now=NOW)
        self.assertEqual(0, stale.deleted)
        self.assertEqual(
            {
                "retention_unknown",
                "acknowledgement_unknown",
                "not_expired",
                "identity_high_water",
            },
            set(stale.skip_reasons),
        )
        self.assertEqual(0, self.state.reclaim_reported(now=NOW).deleted)


class ReclamationTransactionFailureTests(unittest.TestCase):
    """#208: COMMIT 基础设施失败必须回滚且不推进进度, 重试能在同一页完成。"""

    def test_commit_failure_rolls_back_and_retry_deletes_the_same_page(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.sqlite"
            seeding = open_local_state(str(path))
            seeding.configuration().confirm(_bundle_with_retention(), confirmed_at=1.0)
            seeding.close()
            with sqlite3.connect(path) as connection:
                _health_row(connection, 1, sent=10.0, acknowledged_at=EXPIRED_ACK)
                _health_row(connection, 2, sent=10.0, acknowledged_at=EXPIRED_ACK)

            connection = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            migrate(connection)
            state = LocalState(connection)
            deny_commit = True

            def authorize(action: int, *arguments: object) -> int:
                if (
                    deny_commit
                    and action == sqlite3.SQLITE_TRANSACTION
                    and arguments[0] == "COMMIT"
                ):
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            try:
                connection.set_authorizer(authorize)
                with self.assertRaises(sqlite3.DatabaseError):
                    state.reclaim_reported(now=NOW)
                deny_commit = False
                connection.set_authorizer(None)
                # 失败事务未留下活动事务, 也未推进进度: 重试在同一页删除同一可删行。
                retry = state.reclaim_reported(now=NOW)
                self.assertEqual(1, retry.deleted)
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
