from __future__ import annotations

import sqlite3
import threading
import unittest

from edge_runtime.local_state.disposal import (
    DISPOSAL_RESULT_TIMED_OUT,
    DISPOSAL_RESULT_UNKNOWN,
    DisposalClaim,
    DisposalIntent,
    LocalDisposalLedger,
    StoredDisposalResult,
)
from edge_runtime.local_state.schema import migrate


class DisposalLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        migrate(self.connection)
        self.ledger = LocalDisposalLedger(self.connection)
        self.intent = DisposalIntent(
            station_id="station-a",
            idempotency_key="disposal-1",
            connector_id="connector-a",
            point_id="relay-1",
            actor="supervisor",
            requested_state="active",
        )

    def test_result_survives_a_new_ledger_object_and_same_key_is_not_claimed(self) -> None:
        self.ledger.ensure_intent(self.intent)
        claim = self.ledger.claim(self.intent, now=1.0, lease_seconds=5.0)
        self.assertTrue(claim.claimed)
        self.ledger.record_result(
            self.intent,
            result=StoredDisposalResult(kind="written", detail=None, at=2.0),
        )

        restarted = LocalDisposalLedger(self.connection)
        held = restarted.claim(self.intent, now=3.0, lease_seconds=5.0)
        self.assertFalse(held.claimed)
        self.assertIsNotNone(held.result)
        assert held.result is not None
        self.assertEqual(held.result.kind, "written")

    def test_timed_out_result_is_a_terminal_result_after_restart(self) -> None:
        self.ledger.claim(self.intent, now=1.0, lease_seconds=5.0)
        self.ledger.record_result(
            self.intent,
            result=StoredDisposalResult(
                kind=DISPOSAL_RESULT_TIMED_OUT,
                detail="2.0",
                at=2.0,
            ),
        )

        restarted = LocalDisposalLedger(self.connection)
        held = restarted.claim(self.intent, now=3.0, lease_seconds=5.0)

        self.assertFalse(held.claimed)
        self.assertEqual(DISPOSAL_RESULT_TIMED_OUT, held.result.kind if held.result else None)
        self.assertEqual("already_recorded", held.reason)

    def test_expired_lease_closes_unknown_instead_of_replaying_physical_write(self) -> None:
        claim = self.ledger.claim(self.intent, now=1.0, lease_seconds=5.0)
        self.assertTrue(claim.claimed)
        expired = self.ledger.claim(self.intent, now=7.0, lease_seconds=5.0)
        self.assertFalse(expired.claimed)
        self.assertEqual(expired.reason, "lease_expired")
        self.assertIsNotNone(expired.result)
        assert expired.result is not None
        self.assertEqual(expired.result.kind, DISPOSAL_RESULT_UNKNOWN)

    def test_late_physical_result_cannot_replace_an_expired_unknown_result(self) -> None:
        claim = self.ledger.claim(self.intent, now=1.0, lease_seconds=5.0)
        self.assertTrue(claim.claimed)
        self.ledger.claim(self.intent, now=7.0, lease_seconds=5.0)

        self.ledger.record_result(
            self.intent,
            result=StoredDisposalResult(kind="written", detail=None, at=8.0),
        )

        stored = self.ledger.result_for("station-a", "disposal-1")
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.kind, DISPOSAL_RESULT_UNKNOWN)

    def test_key_reuse_for_another_target_is_rejected(self) -> None:
        self.ledger.ensure_intent(self.intent)
        with self.assertRaises(ValueError):
            self.ledger.ensure_intent(
                DisposalIntent(
                    station_id="station-a",
                    idempotency_key="disposal-1",
                    connector_id="connector-b",
                    point_id="relay-2",
                    actor="operator",
                    requested_state="inactive",
                )
            )

    def test_the_same_key_is_independent_between_stations(self) -> None:
        self.ledger.ensure_intent(self.intent)
        self.ledger.claim(self.intent, now=1.0, lease_seconds=5.0)
        self.ledger.record_result(
            self.intent,
            result=StoredDisposalResult(kind="written", detail=None, at=2.0),
        )

        other_station = DisposalIntent(
            station_id="station-b",
            idempotency_key="disposal-1",
            connector_id="connector-b",
            point_id="relay-2",
            actor="supervisor",
            requested_state="active",
        )
        claim = self.ledger.claim(other_station, now=3.0, lease_seconds=5.0)

        self.assertTrue(claim.claimed)
        self.assertIsNone(claim.result)

    def test_concurrent_claims_reserve_one_physical_attempt(self) -> None:
        connection = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        migrate(connection)
        first = LocalDisposalLedger(connection)
        second = LocalDisposalLedger(connection)
        barrier = threading.Barrier(2)
        results: list[DisposalClaim] = []

        def claim(ledger: LocalDisposalLedger) -> None:
            barrier.wait()
            results.append(ledger.claim(self.intent, now=1.0, lease_seconds=5.0))

        threads = [
            threading.Thread(target=claim, args=(first,)),
            threading.Thread(target=claim, args=(second,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(2, len(results))
        self.assertEqual(1, sum(result.claimed for result in results))
        self.assertEqual(
            {"lease_active"},
            {result.reason for result in results if not result.claimed},
        )
        connection.close()


if __name__ == "__main__":
    unittest.main()
