from __future__ import annotations

import unittest
from collections.abc import Callable
from time import sleep
from typing import cast

from nvsop_contracts import ConfigurationBundle

from edge_runtime.configuration_sync import ConfigurationSynchronizer, ConfigurationSyncResult
from edge_runtime.connectors.runtime import ConnectorRuntimeSet
from edge_runtime.local_state.store import LocalState
from edge_runtime.runtime import (
    AutonomousRuntime,
    AutonomousStation,
    ConnectionTestCommandLoop,
    RuntimeComposition,
)
from edge_runtime.runtime_configuration import RuntimeConfiguration


class _CommandLoop:
    def run_forever(self, *, should_stop: Callable[[], bool]) -> None:
        while not should_stop():
            sleep(0.001)


class _State:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Station:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Synchronizer:
    def __init__(
        self,
        *,
        candidate: ConfigurationBundle,
        confirmed: ConfigurationBundle,
        confirm_error: Exception | None = None,
    ) -> None:
        self.candidate = candidate
        self.confirmed_bundle = confirmed
        self.confirm_error = confirm_error
        self.runtime: AutonomousRuntime | None = None
        self.confirmation_completed = False
        self.rejected = False
        self.confirm_saw_applied_runtime = False

    def synchronize(self, *, observed_at: float) -> ConfigurationSyncResult:
        return ConfigurationSyncResult(
            candidate=self.candidate,
            confirmed=self.confirmed_bundle,
            failure=None,
        )

    def confirm(self, bundle: ConfigurationBundle, *, confirmed_at: float) -> None:
        assert self.runtime is not None
        current = self.runtime.configuration
        self.confirm_saw_applied_runtime = current is not None and current.confirmed == bundle
        if self.confirm_error is not None:
            raise self.confirm_error
        self.confirmation_completed = True

    def reject_application(self, *, detail: str, observed_at: float) -> None:
        self.rejected = True


class RuntimeConfigurationSwitchTest(unittest.TestCase):
    def test_runtime_switch_precedes_durable_confirmation(self) -> None:
        old = _bundle(1)
        candidate = _bundle(2)
        synchronizer = _Synchronizer(candidate=candidate, confirmed=old)
        state = _State()
        candidate_runtime = RuntimeConfiguration(stations=(), connectors=(), confirmed=candidate)
        composition = RuntimeComposition(
            configuration=candidate_runtime,
            stations=(),
            connector_runtimes=cast(ConnectorRuntimeSet, object()),
            output_dispatchers={},
        )
        old_connector_runtimes = cast(ConnectorRuntimeSet, object())
        runtime = AutonomousRuntime(
            command_loop=cast(ConnectionTestCommandLoop, _CommandLoop()),
            stations=(),
            state=cast(LocalState, state),
            configuration_sync=cast(ConfigurationSynchronizer, synchronizer),
            maintenance_interval=0.001,
            configuration=RuntimeConfiguration(stations=(), connectors=(), confirmed=old),
            configuration_factory=lambda bundle: composition,
            connector_runtimes=old_connector_runtimes,
        )
        synchronizer.runtime = runtime

        runtime.run_forever(should_stop=lambda: synchronizer.confirmation_completed)

        self.assertTrue(synchronizer.confirm_saw_applied_runtime)
        self.assertEqual(runtime.configuration, candidate_runtime)
        self.assertTrue(state.closed)

    def test_confirmation_failure_rolls_back_candidate_runtime_state(self) -> None:
        old = _bundle(1)
        candidate = _bundle(2)
        synchronizer = _Synchronizer(
            candidate=candidate,
            confirmed=old,
            confirm_error=OSError("local confirmation failed"),
        )
        state = _State()
        old_runtime = RuntimeConfiguration(stations=(), connectors=(), confirmed=old)
        candidate_runtime = RuntimeConfiguration(stations=(), connectors=(), confirmed=candidate)
        candidate_station = _Station()
        composition = RuntimeComposition(
            configuration=candidate_runtime,
            stations=(cast(AutonomousStation, candidate_station),),
            connector_runtimes=cast(ConnectorRuntimeSet, object()),
            output_dispatchers={},
        )
        runtime = AutonomousRuntime(
            command_loop=cast(ConnectionTestCommandLoop, _CommandLoop()),
            stations=(),
            state=cast(LocalState, state),
            configuration_sync=cast(ConfigurationSynchronizer, synchronizer),
            maintenance_interval=0.001,
            configuration=old_runtime,
            configuration_factory=lambda bundle: composition,
            connector_runtimes=cast(ConnectorRuntimeSet, object()),
        )
        synchronizer.runtime = runtime

        try:
            with self.assertRaisesRegex(OSError, "local confirmation failed"):
                runtime.run_forever(should_stop=lambda: False)
            self.assertEqual(runtime.configuration, old_runtime)
            self.assertTrue(candidate_station.closed)
            self.assertFalse(synchronizer.confirmation_completed)
        finally:
            runtime.close()

    def test_composition_failure_keeps_old_runtime_unconfirmed(self) -> None:
        old = _bundle(1)
        candidate = _bundle(2)
        synchronizer = _Synchronizer(candidate=candidate, confirmed=old)
        state = _State()
        old_runtime = RuntimeConfiguration(stations=(), connectors=(), confirmed=old)

        def reject_candidate(bundle: ConfigurationBundle) -> RuntimeComposition:
            raise ValueError("candidate cannot be composed")

        runtime = AutonomousRuntime(
            command_loop=cast(ConnectionTestCommandLoop, _CommandLoop()),
            stations=(),
            state=cast(LocalState, state),
            configuration_sync=cast(ConfigurationSynchronizer, synchronizer),
            maintenance_interval=0.001,
            configuration=old_runtime,
            configuration_factory=reject_candidate,
            connector_runtimes=cast(ConnectorRuntimeSet, object()),
        )
        synchronizer.runtime = runtime

        runtime.run_forever(should_stop=lambda: synchronizer.rejected)

        self.assertFalse(synchronizer.confirmation_completed)
        self.assertEqual(runtime.configuration, old_runtime)
        self.assertTrue(state.closed)


def _bundle(revision: int) -> ConfigurationBundle:
    return ConfigurationBundle(
        host_id="host-a",
        config_revision=revision,
        generated_at=f"2026-09-13T00:00:0{revision}Z",
        stations=(),
    )


if __name__ == "__main__":
    unittest.main()
