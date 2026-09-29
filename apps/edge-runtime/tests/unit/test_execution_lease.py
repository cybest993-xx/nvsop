"""本机工位物理执行权事实: 持久、按绝对到期判定, 且不被重复/延迟租约复活。

S053 要求到期后立即停写、重启与重复/延迟响应不能复活失效授权、中心离线不延长本地期限。
这些性质都由 ``LocalExecutionLeaseStore`` 只保存中心签发的绝对到期时刻、并在每次查询时按本机
墙钟重新判定来保证, 所以它们在这里以真实 SQLite 持久化证明。
"""

from __future__ import annotations

import sqlite3
import unittest
from dataclasses import replace
from datetime import datetime
from unittest.mock import patch

from nvsop_contracts import ConfigurationBundle, ExecutionLease

from edge_runtime.local_state.execution import (
    ExecutionLeaseState,
    LocalExecutionLeaseStore,
)
from edge_runtime.local_state.schema import migrate
from edge_runtime.local_state.store import LocalState


def lease(
    station_id: str = "station-a",
    *,
    grant_id: str = "grant-a",
    expires_at: str = "2030-01-01T00:00:00Z",
) -> ExecutionLease:
    return ExecutionLease(
        station_id=station_id,
        grant_id=grant_id,
        holder_host_id="host-a",
        lease_expires_at=expires_at,
    )


def epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class ExecutionLeaseFactTest(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        migrate(self.connection)
        self.store = LocalExecutionLeaseStore(self.connection)

    def tearDown(self) -> None:
        self.connection.close()

    def test_a_confirmed_lease_authorizes_before_its_expiry(self) -> None:
        self.store.apply([lease()], observed_at=1.0)

        authority = self.store.status("station-a", now=epoch("2029-12-31T23:59:59Z"))

        self.assertTrue(authority.authorized)
        self.assertIs(ExecutionLeaseState.ACTIVE, authority.state)
        assert authority.fact is not None
        self.assertEqual("grant-a", authority.fact.grant_id)

    def test_expiry_is_evaluated_at_read_time_not_at_apply_time(self) -> None:
        self.store.apply([lease()], observed_at=1.0)

        authority = self.store.status("station-a", now=epoch("2030-01-01T00:00:00Z"))

        self.assertFalse(authority.authorized)
        self.assertIs(ExecutionLeaseState.EXPIRED, authority.state)
        self.assertEqual("物理执行权租约已于 2030-01-01T00:00:00Z 到期", authority.detail)

    def test_a_station_without_a_confirmed_lease_has_no_authority(self) -> None:
        authority = self.store.status("station-a", now=1.0)

        self.assertFalse(authority.authorized)
        self.assertIs(ExecutionLeaseState.MISSING, authority.state)

    def test_an_authority_set_replaces_stations_that_are_no_longer_granted(self) -> None:
        self.store.apply(
            [lease("station-a"), lease("station-b", grant_id="grant-b")], observed_at=1.0
        )

        self.store.apply([lease("station-b", grant_id="grant-b")], observed_at=2.0)

        self.assertFalse(self.store.status("station-a", now=2.0).authorized)
        self.assertTrue(self.store.status("station-b", now=2.0).authorized)

    def test_a_renewal_extends_an_unexpired_lease(self) -> None:
        self.store.apply([lease(expires_at="2030-01-01T00:00:00Z")], observed_at=1.0)

        self.store.apply([lease(expires_at="2031-01-01T00:00:00Z")], observed_at=2.0)

        self.assertTrue(
            self.store.status("station-a", now=epoch("2030-06-01T00:00:00Z")).authorized
        )

    def test_redelivering_an_expired_lease_does_not_revive_it(self) -> None:
        self.store.apply([lease(expires_at="2030-01-01T00:00:00Z")], observed_at=1.0)
        now = epoch("2030-06-01T00:00:00Z")

        # 重复或延迟到达的同一租约: 绝对到期时刻不变, 因此仍然失效。
        self.store.apply([lease(expires_at="2030-01-01T00:00:00Z")], observed_at=now)

        self.assertFalse(self.store.status("station-a", now=now).authorized)

    def test_a_restart_reads_the_same_absolute_deadline(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.sqlite"
            first = sqlite3.connect(str(path), isolation_level=None)
            first.row_factory = sqlite3.Row
            migrate(first)
            LocalExecutionLeaseStore(first).apply(
                [lease(expires_at="2030-01-01T00:00:00Z")], observed_at=1.0
            )
            first.close()

            restarted = sqlite3.connect(str(path), isolation_level=None)
            restarted.row_factory = sqlite3.Row
            try:
                store = LocalExecutionLeaseStore(restarted)
                self.assertTrue(
                    store.status("station-a", now=epoch("2029-01-01T00:00:00Z")).authorized
                )
                self.assertFalse(
                    store.status("station-a", now=epoch("2030-06-01T00:00:00Z")).authorized
                )
            finally:
                restarted.close()

    def test_duplicate_station_leases_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.apply([lease("station-a"), lease("station-a")], observed_at=1.0)

    def test_a_backward_clock_step_revives_an_expired_lease_is_the_documented_residual(
        self,
    ) -> None:
        self.store.apply([lease(expires_at="2030-01-01T00:00:00Z")], observed_at=1.0)
        self.assertFalse(
            self.store.status("station-a", now=epoch("2030-06-01T00:00:00Z")).authorized
        )

        # 墙钟回拨到到期前: 期限按本机墙钟比较, 因此重新放行。这是 #174 Outcome 要求核对的
        # 实际影响, 也是信任本机时钟的残余风险 (见 execution.py 模块文档与 §5.17)。
        authority = self.store.status("station-a", now=epoch("2029-06-01T00:00:00Z"))

        self.assertTrue(authority.authorized)


class ConfirmedConfigurationCarriesTheLeaseTest(unittest.TestCase):
    """确认 bundle 与它携带的租约事实必须一起生效, 不出现半个状态。"""

    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        migrate(self.connection)
        self.state = LocalState(self.connection)

    def tearDown(self) -> None:
        self.state.close()

    def test_confirming_a_bundle_records_its_execution_leases(self) -> None:
        bundle = ConfigurationBundle(
            host_id="host-a",
            config_revision=1,
            generated_at="2026-09-13T00:00:00Z",
            stations=(),
            execution_grants=(lease(),),
        )

        self.state.configuration().confirm(bundle, confirmed_at=1.0)

        authority = self.state.execution_leases().status(
            "station-a", now=epoch("2029-01-01T00:00:00Z")
        )
        self.assertTrue(authority.authorized)

    def test_a_failed_lease_apply_rolls_back_the_configuration(self) -> None:
        good = ConfigurationBundle(
            host_id="host-a",
            config_revision=1,
            generated_at="2026-09-13T00:00:00Z",
            stations=(),
            execution_grants=(lease(),),
        )
        self.state.configuration().confirm(good, confirmed_at=1.0)
        with (
            patch.object(LocalExecutionLeaseStore, "apply", side_effect=RuntimeError("boom")),
            self.assertRaises(RuntimeError),
        ):
            self.state.configuration().confirm(replace(good, config_revision=2), confirmed_at=2.0)

        confirmed = self.state.configuration().confirmed()
        assert confirmed is not None
        self.assertEqual(1, confirmed.config_revision)


if __name__ == "__main__":
    unittest.main()
