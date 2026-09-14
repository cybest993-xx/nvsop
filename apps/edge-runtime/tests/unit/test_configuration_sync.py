from __future__ import annotations

import sqlite3
import unittest

from nvsop_contracts import ConfigurationBundle

from edge_runtime.configuration_sync import (
    ConfigurationPullError,
    ConfigurationSynchronizer,
)
from edge_runtime.local_state.schema import migrate
from edge_runtime.local_state.store import LocalState


class ScriptedPuller:
    def __init__(self, values: list[object]) -> None:
        self.values = values

    def pull(self) -> ConfigurationBundle:
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        assert isinstance(value, ConfigurationBundle)
        return value


class ConfigurationSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        migrate(self.connection)
        self.state = LocalState(self.connection)

    def tearDown(self) -> None:
        self.state.close()

    def test_older_revision_keeps_last_confirmed_bundle(self) -> None:
        first = ConfigurationBundle(
            host_id="host-a",
            config_revision=2,
            generated_at="now",
            stations=(),
        )
        older = ConfigurationBundle(
            host_id="host-a",
            config_revision=1,
            generated_at="older",
            stations=(),
        )
        synchronizer = ConfigurationSynchronizer(
            puller=ScriptedPuller([first, older]),
            store=self.state.configuration(),
        )
        synchronizer.synchronize(observed_at=1.0)
        result = synchronizer.synchronize(observed_at=2.0)
        self.assertFalse(result.applied)
        self.assertEqual(result.active, first)
        assert result.failure is not None
        self.assertEqual(result.failure.code, "stale_revision")

    def test_conflicting_revision_records_a_distinct_failure_reason(self) -> None:
        first = ConfigurationBundle(
            host_id="host-a",
            config_revision=1,
            generated_at="now",
            stations=(),
        )
        conflicting = ConfigurationBundle(
            host_id="host-a",
            config_revision=1,
            generated_at="different",
            stations=(),
        )
        synchronizer = ConfigurationSynchronizer(
            puller=ScriptedPuller([first, conflicting]),
            store=self.state.configuration(),
        )

        synchronizer.synchronize(observed_at=1.0)
        result = synchronizer.synchronize(observed_at=2.0)
        self.assertFalse(result.applied)
        self.assertEqual(first, result.active)
        assert result.failure is not None
        self.assertEqual(result.failure.code, "conflicting_confirmation")

    def test_repeated_pull_with_new_generated_at_is_not_a_conflict(self) -> None:
        first = ConfigurationBundle(
            host_id="host-a",
            config_revision=1,
            generated_at="2026-09-13T00:00:00Z",
            stations=(),
        )
        repeated = ConfigurationBundle(
            host_id="host-a",
            config_revision=1,
            generated_at="2026-09-13T00:00:01Z",
            stations=(),
        )
        synchronizer = ConfigurationSynchronizer(
            puller=ScriptedPuller([first, repeated]),
            store=self.state.configuration(),
        )

        synchronizer.synchronize(observed_at=1.0)
        result = synchronizer.synchronize(observed_at=2.0)

        self.assertTrue(result.applied)
        self.assertEqual(result.active, repeated)
        self.assertIsNone(result.failure)
        self.assertEqual(first.effective_sha256, repeated.effective_sha256)

    def test_invalid_runtime_view_is_not_confirmed(self) -> None:
        candidate = ConfigurationBundle(
            host_id="host-a",
            config_revision=1,
            generated_at="now",
            stations=(),
        )
        synchronizer = ConfigurationSynchronizer(
            puller=ScriptedPuller([candidate]),
            store=self.state.configuration(),
            validator=lambda bundle: (_ for _ in ()).throw(
                ValueError(f"station {bundle.host_id} cannot be composed")
            ),
        )

        result = synchronizer.synchronize(observed_at=1.0)

        self.assertFalse(result.applied)
        self.assertIsNone(result.active)
        self.assertIsNotNone(result.failure)
        assert result.failure is not None
        self.assertEqual(result.failure.code, "runtime_configuration_invalid")

    def test_invalid_pull_keeps_last_confirmed_bundle_and_records_reason(self) -> None:
        first = ConfigurationBundle(
            host_id="host-a",
            config_revision=1,
            generated_at="now",
            stations=(),
        )
        synchronizer = ConfigurationSynchronizer(
            puller=ScriptedPuller([first, ConfigurationPullError("digest_mismatch", "bad digest")]),
            store=self.state.configuration(),
        )

        confirmed = synchronizer.synchronize(observed_at=1.0)
        self.assertTrue(confirmed.applied)
        self.assertEqual(confirmed.active, first)
        failed = synchronizer.synchronize(observed_at=2.0)
        self.assertFalse(failed.applied)
        self.assertEqual(failed.active, first)
        self.assertIsNotNone(failed.failure)
        assert failed.failure is not None
        self.assertEqual(failed.failure.code, "digest_mismatch")
        self.assertEqual(self.state.configuration().confirmed(), first)


if __name__ == "__main__":
    unittest.main()
