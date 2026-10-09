from __future__ import annotations

import hashlib
import json
import sqlite3
import unittest
from collections.abc import Callable
from unittest.mock import patch

import httpx2
from nvsop_contracts import (
    DISPOSITION_SOUND_LIGHT_OUTPUT_CAPABILITY,
    DISPOSITION_STOP_OUTPUT_CAPABILITY,
    ConfigurationBundle,
    ConfiguredStation,
    ResolvedRuntimeParameters,
    canonical_json,
    configuration_to_wire,
)

from edge_runtime.center_client import CenterClient
from edge_runtime.configuration_sync import (
    ConfigurationPullError,
    ConfigurationSynchronizer,
    HttpConfigurationPuller,
)
from edge_runtime.local_state.schema import migrate
from edge_runtime.local_state.store import LocalState


def http_puller(respond: Callable[[httpx2.Request], httpx2.Response]) -> HttpConfigurationPuller:
    client = CenterClient(
        center_url="https://center.example",
        host_id="host-a",
        host_private_key="unused",  # pragma: allowlist secret
        timeout=1.0,
        transport=httpx2.MockTransport(respond),
    )
    return HttpConfigurationPuller(client=client, host_id="host-a")


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
        self.assertIn("sha256", columns)

    def tearDown(self) -> None:
        self.state.close()

    def test_historical_v1_payload_keeps_model_ids_after_confirmation(self) -> None:
        current = ConfigurationBundle(
            host_id="host-a",
            config_revision=1,
            generated_at="2026-09-13T00:00:00Z",
            stations=(
                ConfiguredStation(
                    station_id="station-a",
                    backend_id="backend-a",
                    code="S-A",
                    name="Station A",
                    revision=1,
                    runtime_parameters=ResolvedRuntimeParameters(1, 1, "stop"),
                    connectors=(),
                    points=(),
                    template=None,
                    model_ids=("reported-model",),
                ),
            ),
        )
        wire = configuration_to_wire(current)
        wire["contract_version"] = 1
        wire.pop("sha256")
        wire["sha256"] = hashlib.sha256(canonical_json(wire).encode("utf-8")).hexdigest()
        payload = json.dumps(wire, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.connection.execute(
            """
            INSERT INTO local_config
                (slot, host_id, config_revision, sha256, confirmed_at, payload)
            VALUES (1, ?, ?, ?, ?, ?)
            """,
            (current.host_id, current.config_revision, wire["sha256"], 1.0, payload),
        )

        self.assertEqual(self.state.configuration().confirmed(), current)
        self.state.configuration().confirm(current, confirmed_at=2.0)
        self.assertEqual(self.state.configuration().confirmed(), current)

    def test_historical_v1_missing_collections_restore_as_empty_only(self) -> None:
        current = ConfigurationBundle(
            host_id="host-a",
            config_revision=1,
            generated_at="2026-09-13T00:00:00Z",
            stations=(
                ConfiguredStation(
                    station_id="station-a",
                    backend_id="backend-a",
                    code="S-A",
                    name="Station A",
                    revision=1,
                    runtime_parameters=ResolvedRuntimeParameters(1, 1, "stop"),
                    connectors=(),
                    points=(),
                    template=None,
                    model_ids=(),
                ),
            ),
        )
        wire = configuration_to_wire(current)
        wire["contract_version"] = 1
        raw_stations = wire["stations"]
        assert isinstance(raw_stations, list)
        raw_station = raw_stations[0]
        assert isinstance(raw_station, dict)
        raw_station.pop("cameras")
        raw_station.pop("model_ids")
        wire.pop("sha256")
        wire["sha256"] = hashlib.sha256(canonical_json(wire).encode("utf-8")).hexdigest()
        payload = json.dumps(wire, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.connection.execute(
            """
            INSERT INTO local_config
                (slot, host_id, config_revision, sha256, confirmed_at, payload)
            VALUES (1, ?, ?, ?, ?, ?)
            """,
            (current.host_id, current.config_revision, wire["sha256"], 1.0, payload),
        )

        restored = self.state.configuration().confirmed()
        assert restored is not None
        self.assertEqual(restored.stations[0].cameras, ())
        self.assertEqual(restored.stations[0].model_ids, ())
        self.assertEqual(restored.stations[0].backend_id, "backend-a")

    def test_historical_v2_missing_core_collections_is_rejected(self) -> None:
        current = ConfigurationBundle(
            host_id="host-a",
            config_revision=1,
            generated_at="2026-09-13T00:00:00Z",
            stations=(
                ConfiguredStation(
                    station_id="station-a",
                    backend_id="backend-a",
                    code="S-A",
                    name="Station A",
                    revision=1,
                    runtime_parameters=ResolvedRuntimeParameters(1, 1, "stop"),
                    connectors=(),
                    points=(),
                    template=None,
                ),
            ),
        )
        wire = configuration_to_wire(current)
        raw_stations = wire["stations"]
        assert isinstance(raw_stations, list)
        raw_station = raw_stations[0]
        assert isinstance(raw_station, dict)
        raw_station.pop("cameras")
        raw_station.pop("model_ids")
        wire.pop("sha256")
        wire["sha256"] = hashlib.sha256(canonical_json(wire).encode("utf-8")).hexdigest()
        payload = json.dumps(wire, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.connection.execute(
            """
            INSERT INTO local_config
                (slot, host_id, config_revision, sha256, confirmed_at, payload)
            VALUES (1, ?, ?, ?, ?, ?)
            """,
            (current.host_id, current.config_revision, wire["sha256"], 1.0, payload),
        )

        with self.assertRaisesRegex(ValueError, "unsupported or missing fields"):
            self.state.configuration().confirmed()

    def test_new_bundle_after_center_removes_highest_revision_object_confirms(self) -> None:
        old_configuration = ConfigurationBundle(
            host_id="host-a",
            config_revision=99,
            generated_at="2026-09-13T00:00:00Z",
            stations=(),
        )
        new_configuration = ConfigurationBundle(
            host_id="host-a",
            config_revision=100,
            generated_at="2026-09-12T00:00:00Z",
            stations=(),
        )
        synchronizer = ConfigurationSynchronizer(
            puller=ScriptedPuller([old_configuration, new_configuration]),
            store=self.state.configuration(),
        )

        first = synchronizer.synchronize(observed_at=1.0)
        self.assertEqual(first.candidate, old_configuration)
        self.assertIsNone(self.state.configuration().confirmed())
        synchronizer.confirm(old_configuration, confirmed_at=1.0)

        result = synchronizer.synchronize(observed_at=2.0)
        self.assertEqual(result.candidate, new_configuration)
        self.assertEqual(result.confirmed, old_configuration)
        synchronizer.confirm(new_configuration, confirmed_at=2.0)
        self.assertEqual(self.state.configuration().confirmed(), new_configuration)

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
        accepted = synchronizer.synchronize(observed_at=1.0)
        assert accepted.candidate is not None
        synchronizer.confirm(accepted.candidate, confirmed_at=1.0)
        result = synchronizer.synchronize(observed_at=2.0)
        self.assertIsNone(result.candidate)
        self.assertEqual(result.confirmed, first)
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
            stations=(
                ConfiguredStation(
                    station_id="station-a",
                    backend_id="backend-a",
                    code="S-A",
                    name="Station A",
                    revision=1,
                    runtime_parameters=ResolvedRuntimeParameters(1, 1, "stop"),
                    connectors=(),
                    points=(),
                    template=None,
                ),
            ),
        )
        synchronizer = ConfigurationSynchronizer(
            puller=ScriptedPuller([first, conflicting]),
            store=self.state.configuration(),
        )

        accepted = synchronizer.synchronize(observed_at=1.0)
        assert accepted.candidate is not None
        synchronizer.confirm(accepted.candidate, confirmed_at=1.0)
        result = synchronizer.synchronize(observed_at=2.0)
        self.assertIsNone(result.candidate)
        self.assertEqual(first, result.confirmed)
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

        accepted = synchronizer.synchronize(observed_at=1.0)
        assert accepted.candidate is not None
        synchronizer.confirm(accepted.candidate, confirmed_at=1.0)
        result = synchronizer.synchronize(observed_at=2.0)

        self.assertEqual(result.candidate, repeated)
        self.assertEqual(result.confirmed, first)
        self.assertIsNone(result.failure)
        self.assertEqual(first.stable_content_wire(), repeated.stable_content_wire())
        synchronizer.confirm(repeated, confirmed_at=2.0)
        self.assertEqual(self.state.configuration().confirmed(), repeated)

    def test_synchronizer_accepts_sound_light_output_capability(self) -> None:
        candidate = ConfigurationBundle(
            host_id="host-a",
            config_revision=1,
            generated_at="2026-09-13T00:00:00Z",
            stations=(),
            required_capabilities=(DISPOSITION_SOUND_LIGHT_OUTPUT_CAPABILITY,),
        )
        synchronizer = ConfigurationSynchronizer(
            puller=ScriptedPuller([candidate]),
            store=self.state.configuration(),
        )

        result = synchronizer.synchronize(observed_at=1.0)

        self.assertEqual(candidate, result.candidate)
        self.assertIsNone(result.failure)

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

        self.assertIsNone(result.candidate)
        self.assertIsNone(result.confirmed)
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

        accepted = synchronizer.synchronize(observed_at=1.0)
        self.assertEqual(accepted.candidate, first)
        synchronizer.confirm(first, confirmed_at=1.0)
        failed = synchronizer.synchronize(observed_at=2.0)
        self.assertIsNone(failed.candidate)
        self.assertEqual(failed.confirmed, first)
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

        accepted = synchronizer.synchronize(observed_at=1.0)
        assert accepted.candidate is not None
        synchronizer.confirm(accepted.candidate, confirmed_at=1.0)
        result = synchronizer.synchronize(observed_at=2.0)

        self.assertIsNone(result.candidate)
        self.assertEqual(result.confirmed, first)
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

    def test_tampered_http_digest_is_rejected_before_local_confirmation(self) -> None:
        candidate = configuration_to_wire(
            ConfigurationBundle(
                host_id="host-a",
                config_revision=1,
                generated_at="2026-09-13T00:00:00Z",
                stations=(),
            )
        )
        candidate["sha256"] = "0" * 64
        puller = http_puller(lambda _: httpx2.Response(200, json=candidate))
        with (
            patch("edge_runtime.center_client.sign_host_identity_request", return_value="sig"),
            self.assertRaises(ConfigurationPullError) as raised,
        ):
            puller.pull()
        self.assertEqual(raised.exception.code, "digest_mismatch")
        self.assertIsNone(self.state.configuration().confirmed())

    def test_synchronizer_accepts_stop_output_capability(self) -> None:
        candidate = ConfigurationBundle(
            host_id="host-a",
            config_revision=1,
            generated_at="2026-09-13T00:00:00Z",
            stations=(),
            required_capabilities=(DISPOSITION_STOP_OUTPUT_CAPABILITY,),
        )
        synchronizer = ConfigurationSynchronizer(
            puller=ScriptedPuller([candidate]),
            store=self.state.configuration(),
        )

        result = synchronizer.synchronize(observed_at=1.0)

        self.assertEqual(candidate, result.candidate)
        self.assertIsNone(result.failure)

    def test_synchronizer_rejects_unsupported_required_capability(self) -> None:
        candidate = ConfigurationBundle(
            host_id="host-a",
            config_revision=1,
            generated_at="2026-09-13T00:00:00Z",
            stations=(),
            required_capabilities=("future.behavior",),
        )
        synchronizer = ConfigurationSynchronizer(
            puller=ScriptedPuller([candidate]),
            store=self.state.configuration(),
        )

        result = synchronizer.synchronize(observed_at=1.0)

        self.assertIsNone(result.candidate)
        self.assertIsNone(result.confirmed)
        assert result.failure is not None
        self.assertEqual(result.failure.code, "unsupported_capability")
        self.assertIn("future.behavior", result.failure.detail)

    def test_http_read_and_connection_failures_are_generic_and_do_not_leak_details(self) -> None:
        for failure in (
            httpx2.ReadError("password=must-not-leak"),
            httpx2.ConnectError("password=must-not-leak"),
        ):

            def fail(_: httpx2.Request, failure: Exception = failure) -> httpx2.Response:
                raise failure

            with (
                self.subTest(failure=type(failure).__name__),
                patch("edge_runtime.center_client.sign_host_identity_request", return_value="sig"),
                self.assertRaises(ConfigurationPullError) as raised,
            ):
                http_puller(fail).pull()
            self.assertEqual(raised.exception.code, "center_unreachable")
            self.assertNotIn("password", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
