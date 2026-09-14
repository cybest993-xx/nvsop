from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from nvsop_contracts import ConfigurationBundle

from edge_runtime.configuration_sync import (
    ConfigurationPullError,
    ConfigurationSynchronizer,
    HttpConfigurationPuller,
)
from edge_runtime.local_state.schema import migrate
from edge_runtime.local_state.store import LocalState


class BrokenResponse:
    status = 200

    def read(self) -> bytes:
        raise OSError("password=must-not-leak")

    def close(self) -> None:
        pass


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
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(local_config)")}
        self.assertNotIn("sha256", columns)

    def tearDown(self) -> None:
        self.state.close()

    def test_older_revision_keeps_last_confirmed_bundle(self) -> None:
        first = ConfigurationBundle(
            host_id="host-a",
            config_revision=2,
            generated_at="2026-09-13T00:00:00Z",
            stations=(),
        )
        older = ConfigurationBundle(
            host_id="host-a",
            config_revision=1,
            generated_at="2026-09-12T00:00:00Z",
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
            generated_at="2026-09-13T00:00:00Z",
            stations=(),
        )
        conflicting = ConfigurationBundle(
            host_id="host-a",
            config_revision=1,
            generated_at="2026-09-12T00:00:00Z",
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
        self.assertEqual(first.stable_content_wire(), repeated.stable_content_wire())

    def test_invalid_runtime_view_is_not_confirmed(self) -> None:
        candidate = ConfigurationBundle(
            host_id="host-a",
            config_revision=1,
            generated_at="2026-09-13T00:00:00Z",
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
            generated_at="2026-09-13T00:00:00Z",
            stations=(),
        )
        synchronizer = ConfigurationSynchronizer(
            puller=ScriptedPuller(
                [first, ConfigurationPullError("contract_invalid", "invalid configuration")]
            ),
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
        self.assertEqual(failed.failure.code, "contract_invalid")
        self.assertEqual(self.state.configuration().confirmed(), first)

    def test_host_scope_change_keeps_the_last_confirmed_bundle(self) -> None:
        first = ConfigurationBundle(
            host_id="host-a",
            config_revision=1,
            generated_at="2026-09-13T00:00:00Z",
            stations=(),
        )
        foreign = ConfigurationBundle(
            host_id="host-b",
            config_revision=2,
            generated_at="2026-09-13T00:00:01Z",
            stations=(),
        )
        synchronizer = ConfigurationSynchronizer(
            puller=ScriptedPuller([first, foreign]),
            store=self.state.configuration(),
        )

        synchronizer.synchronize(observed_at=1.0)
        result = synchronizer.synchronize(observed_at=2.0)

        self.assertEqual(result.active, first)
        assert result.failure is not None
        self.assertEqual(result.failure.code, "host_scope_mismatch")

    def test_corrupt_local_metadata_is_not_treated_as_confirmed(self) -> None:
        value = ConfigurationBundle(
            host_id="host-a",
            config_revision=1,
            generated_at="2026-09-13T00:00:00Z",
            stations=(),
        )
        store = self.state.configuration()
        store.confirm(value, confirmed_at=1.0)
        self.connection.execute("UPDATE local_config SET host_id = 'host-b' WHERE slot = 1")

        with self.assertRaisesRegex(ValueError, "metadata"):
            store.confirmed()

    def test_http_read_and_connection_failures_are_generic_and_do_not_leak_details(self) -> None:
        puller = HttpConfigurationPuller(
            center_url="https://center.example",
            host_id="host-a",
            host_private_key="unused",
            timeout=1.0,
        )
        with (
            patch(
                "edge_runtime.configuration_sync.sign_host_identity_request",
                return_value="signature",
            ),
            patch(
                "edge_runtime.configuration_sync.urllib.request.urlopen",
                return_value=BrokenResponse(),
            ),
            self.assertRaises(ConfigurationPullError) as raised,
        ):
            puller.pull()
        self.assertEqual(raised.exception.code, "center_unreachable")
        self.assertNotIn("password", str(raised.exception))

        with (
            patch(
                "edge_runtime.configuration_sync.sign_host_identity_request",
                return_value="signature",
            ),
            patch(
                "edge_runtime.configuration_sync.urllib.request.urlopen",
                side_effect=OSError("password=must-not-leak"),
            ),
            self.assertRaises(ConfigurationPullError) as raised,
        ):
            puller.pull()
        self.assertEqual(raised.exception.code, "center_unreachable")
        self.assertNotIn("password", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
