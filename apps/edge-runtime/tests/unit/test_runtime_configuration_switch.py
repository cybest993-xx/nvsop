from __future__ import annotations

import unittest
from collections.abc import Callable
from threading import Event, Thread
from time import sleep
from typing import cast

from nvsop_contracts import ConfigurationBundle

from edge_runtime.configuration_sync import ConfigurationSynchronizer, ConfigurationSyncResult
from edge_runtime.connectors.runtime import ConnectorRuntimeSet
from edge_runtime.local_state.store import LocalState
from edge_runtime.media import MediaRuntime
from edge_runtime.reporting import HostReportReconciler
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
    def __init__(self, *, close_error: BaseException | None = None) -> None:
        self.closed = False
        self.stopped = False
        self.close_error = close_error

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error

    def run_forever(self, *, should_stop: Callable[[], bool]) -> None:
        while not should_stop():
            sleep(0.001)
        self.stopped = True


class _CoordinatedStation(_Station):
    def __init__(self, *, close_started: Event, wait_for_close_started: Event) -> None:
        super().__init__()
        self._close_started = close_started
        self._wait_for_close_started = wait_for_close_started

    def close(self) -> None:
        self._close_started.set()
        if not self._wait_for_close_started.wait(0.2):
            raise RuntimeError("station close was serialized")
        super().close()


class _Media:
    def __init__(self) -> None:
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True


class _Synchronizer:
    def __init__(
        self,
        *,
        candidate: ConfigurationBundle,
        confirmed: ConfigurationBundle,
        confirm_error: Exception | None = None,
        candidate_sequence: tuple[ConfigurationBundle, ...] | None = None,
    ) -> None:
        self._candidate_sequence = candidate_sequence or (candidate,)
        self.confirmed_bundle = confirmed
        self.confirm_error = confirm_error
        self.runtime: AutonomousRuntime | None = None
        self.confirmation_completed = False
        self.confirmation = Event()
        self.rejected = False
        self.rejection_count = 0
        self.synchronize_calls = 0
        self.confirm_saw_applied_runtime = False

    def synchronize(self, *, observed_at: float) -> ConfigurationSyncResult:
        candidate = self._candidate_sequence[
            min(self.synchronize_calls, len(self._candidate_sequence) - 1)
        ]
        self.synchronize_calls += 1
        return ConfigurationSyncResult(
            candidate=candidate,
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
        self.confirmation.set()

    def reject_application(self, *, detail: str, observed_at: float) -> None:
        self.rejected = True
        self.rejection_count += 1


class _BlockingReportReconciler:
    def __init__(self, *, release: Event) -> None:
        self.started = Event()
        self.release = release
        self.limits: list[int | None] = []

    def flush(
        self, *, now: object, reported_at: str, limit: int | None = None
    ) -> tuple[object, ...]:
        del now, reported_at
        self.limits.append(limit)
        self.started.set()
        if not self.release.wait(0.5):
            raise RuntimeError("report flush was not released")
        return ()


class _CountingReportReconciler:
    def __init__(self) -> None:
        self.first = Event()
        self.second = Event()
        self.calls = 0

    def flush(
        self, *, now: object, reported_at: str, limit: int | None = None
    ) -> tuple[object, ...]:
        del now, reported_at, limit
        self.calls += 1
        if self.calls == 1:
            self.first.set()
        elif self.calls == 2:
            self.second.set()
        return ()


class RuntimeConfigurationSwitchTest(unittest.TestCase):
    def test_blocked_report_flush_does_not_block_configuration_activation(self) -> None:
        old = _bundle(1)
        candidate = _bundle(2)
        release = Event()
        reporter = _BlockingReportReconciler(release=release)
        synchronizer = _Synchronizer(candidate=candidate, confirmed=old)
        stop = Event()
        activated = Event()
        state = _State()
        errors: list[BaseException] = []
        candidate_runtime = RuntimeConfiguration(stations=(), connectors=(), confirmed=candidate)

        def compose(runtime_configuration: RuntimeConfiguration) -> RuntimeComposition:
            self.assertIs(runtime_configuration, candidate_runtime)
            activated.set()
            return RuntimeComposition(
                configuration=candidate_runtime,
                stations=(),
                connector_runtimes=cast(ConnectorRuntimeSet, object()),
                output_dispatchers={},
                report_reconciler=cast(HostReportReconciler, reporter),
            )

        runtime = AutonomousRuntime(
            command_loop=cast(ConnectionTestCommandLoop, _CommandLoop()),
            stations=(),
            state=cast(LocalState, state),
            report_reconciler=cast(HostReportReconciler, reporter),
            configuration_sync=cast(ConfigurationSynchronizer, synchronizer),
            maintenance_interval=30.0,
            configuration=RuntimeConfiguration(stations=(), connectors=(), confirmed=old),
            configuration_resolver=lambda bundle: candidate_runtime,
            configuration_factory=compose,
            connector_runtimes=cast(ConnectorRuntimeSet, object()),
        )
        synchronizer.runtime = runtime

        def run_runtime() -> None:
            try:
                runtime.run_forever(should_stop=stop.is_set)
            except BaseException as error:
                errors.append(error)

        thread = Thread(target=run_runtime)
        thread.start()
        try:
            self.assertTrue(reporter.started.wait(0.2))
            self.assertTrue(activated.wait(0.2))
            self.assertEqual(reporter.limits, [1])
            self.assertTrue(synchronizer.confirmation.wait(0.2))
        finally:
            stop.set()
            release.set()
            thread.join(0.5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(state.closed)

    def test_report_wake_interrupts_retry_wait_and_stop_terminates_loop(self) -> None:
        reporter = _CountingReportReconciler()
        report_wake = Event()
        stop = Event()
        state = _State()
        errors: list[BaseException] = []
        runtime = AutonomousRuntime(
            command_loop=cast(ConnectionTestCommandLoop, _CommandLoop()),
            stations=(),
            state=cast(LocalState, state),
            report_reconciler=cast(HostReportReconciler, reporter),
            report_wake=report_wake,
        )

        def run_runtime() -> None:
            try:
                runtime.run_forever(should_stop=stop.is_set)
            except BaseException as error:
                errors.append(error)

        thread = Thread(target=run_runtime)
        thread.start()
        try:
            self.assertTrue(reporter.first.wait(0.2))
            report_wake.set()
            self.assertTrue(reporter.second.wait(0.2))
        finally:
            stop.set()
            thread.join(0.5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(state.closed)

    def test_close_finishes_runtime_cleanup_before_propagating_station_failure(self) -> None:
        failing = _Station(close_error=RuntimeError("synthetic station close failure"))
        later = _Station()
        state = _State()
        media = _Media()
        runtime = AutonomousRuntime(
            command_loop=cast(ConnectionTestCommandLoop, _CommandLoop()),
            stations=(
                cast(AutonomousStation, failing),
                cast(AutonomousStation, later),
            ),
            state=cast(LocalState, state),
            media=cast(MediaRuntime, media),
        )

        with self.assertRaisesRegex(RuntimeError, "synthetic station close failure"):
            runtime.close()

        self.assertTrue(failing.closed)
        self.assertTrue(later.closed)
        self.assertTrue(media.closed)
        self.assertTrue(state.closed)

    def test_close_starts_all_station_shutdowns_before_waiting(self) -> None:
        first_started = Event()
        second_started = Event()
        first = _CoordinatedStation(
            close_started=first_started,
            wait_for_close_started=second_started,
        )
        second = _CoordinatedStation(
            close_started=second_started,
            wait_for_close_started=first_started,
        )
        state = _State()
        runtime = AutonomousRuntime(
            command_loop=cast(ConnectionTestCommandLoop, _CommandLoop()),
            stations=(
                cast(AutonomousStation, first),
                cast(AutonomousStation, second),
            ),
            state=cast(LocalState, state),
        )

        runtime.close()

        self.assertTrue(first.closed)
        self.assertTrue(second.closed)
        self.assertTrue(state.closed)

    def test_configuration_switch_close_failure_cleans_runtime_before_propagating(self) -> None:
        old = _bundle(1)
        candidate = _bundle(2)
        synchronizer = _Synchronizer(candidate=candidate, confirmed=old)
        failing = _Station(close_error=RuntimeError("synthetic station close failure"))
        later = _Station()
        state = _State()
        media = _Media()
        candidate_runtime = RuntimeConfiguration(stations=(), connectors=(), confirmed=candidate)
        composition_called = False

        def compose(runtime_configuration: RuntimeConfiguration) -> RuntimeComposition:
            nonlocal composition_called
            composition_called = True
            self.assertIs(runtime_configuration, candidate_runtime)
            return RuntimeComposition(
                configuration=candidate_runtime,
                stations=(),
                connector_runtimes=cast(ConnectorRuntimeSet, object()),
                output_dispatchers={},
            )

        runtime = AutonomousRuntime(
            command_loop=cast(ConnectionTestCommandLoop, _CommandLoop()),
            stations=(
                cast(AutonomousStation, failing),
                cast(AutonomousStation, later),
            ),
            state=cast(LocalState, state),
            media=cast(MediaRuntime, media),
            configuration_sync=cast(ConfigurationSynchronizer, synchronizer),
            maintenance_interval=0.001,
            configuration=RuntimeConfiguration(stations=(), connectors=(), confirmed=old),
            configuration_resolver=lambda bundle: candidate_runtime,
            configuration_factory=compose,
            connector_runtimes=cast(ConnectorRuntimeSet, object()),
        )
        synchronizer.runtime = runtime

        with self.assertRaisesRegex(RuntimeError, "synthetic station close failure"):
            runtime.run_forever(should_stop=lambda: False)

        self.assertFalse(composition_called)
        self.assertTrue(failing.closed)
        self.assertTrue(later.closed)
        self.assertTrue(media.closed)
        self.assertTrue(state.closed)

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
        old_station = _Station()
        old_connector_runtimes = cast(ConnectorRuntimeSet, object())

        def compose(runtime_configuration: RuntimeConfiguration) -> RuntimeComposition:
            self.assertIs(runtime_configuration, candidate_runtime)
            self.assertTrue(old_station.stopped)
            return composition

        runtime = AutonomousRuntime(
            command_loop=cast(ConnectionTestCommandLoop, _CommandLoop()),
            stations=(cast(AutonomousStation, old_station),),
            state=cast(LocalState, state),
            configuration_sync=cast(ConfigurationSynchronizer, synchronizer),
            maintenance_interval=0.001,
            configuration=RuntimeConfiguration(stations=(), connectors=(), confirmed=old),
            configuration_resolver=lambda bundle: candidate_runtime,
            configuration_factory=compose,
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
        rollback_runtime = RuntimeConfiguration(stations=(), connectors=(), confirmed=old)
        composition = RuntimeComposition(
            configuration=candidate_runtime,
            stations=(cast(AutonomousStation, candidate_station),),
            connector_runtimes=cast(ConnectorRuntimeSet, object()),
            output_dispatchers={},
        )
        rollback = RuntimeComposition(
            configuration=rollback_runtime,
            stations=(),
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
            configuration_resolver=lambda bundle: candidate_runtime,
            configuration_factory=lambda value: (
                composition if value is candidate_runtime else rollback
            ),
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

    def test_composition_failure_quarantines_candidate_until_identity_changes(self) -> None:
        old = _bundle(1)
        rejected = _bundle(2)
        replacement = _bundle(3)
        synchronizer = _Synchronizer(
            candidate=rejected,
            confirmed=old,
            candidate_sequence=(rejected, rejected, replacement),
        )
        state = _State()
        old_runtime = RuntimeConfiguration(stations=(), connectors=(), confirmed=old)
        rejected_runtime = RuntimeConfiguration(stations=(), connectors=(), confirmed=rejected)
        replacement_runtime = RuntimeConfiguration(
            stations=(), connectors=(), confirmed=replacement
        )
        rollback = RuntimeComposition(
            configuration=old_runtime,
            stations=(),
            connector_runtimes=cast(ConnectorRuntimeSet, object()),
            output_dispatchers={},
        )
        replacement_composition = RuntimeComposition(
            configuration=replacement_runtime,
            stations=(),
            connector_runtimes=cast(ConnectorRuntimeSet, object()),
            output_dispatchers={},
        )
        composition_attempts: list[int] = []

        def resolve(bundle: ConfigurationBundle) -> RuntimeConfiguration:
            return rejected_runtime if bundle is rejected else replacement_runtime

        def compose(runtime_configuration: RuntimeConfiguration) -> RuntimeComposition:
            confirmed = runtime_configuration.confirmed
            assert confirmed is not None
            composition_attempts.append(confirmed.config_revision)
            if runtime_configuration is rejected_runtime:
                raise ValueError("candidate cannot be composed")
            if runtime_configuration is replacement_runtime:
                return replacement_composition
            return rollback

        runtime = AutonomousRuntime(
            command_loop=cast(ConnectionTestCommandLoop, _CommandLoop()),
            stations=(),
            state=cast(LocalState, state),
            configuration_sync=cast(ConfigurationSynchronizer, synchronizer),
            maintenance_interval=0.001,
            configuration=old_runtime,
            configuration_resolver=resolve,
            configuration_factory=compose,
            connector_runtimes=cast(ConnectorRuntimeSet, object()),
        )
        synchronizer.runtime = runtime

        runtime.run_forever(should_stop=lambda: synchronizer.confirmation_completed)

        self.assertEqual(synchronizer.rejection_count, 1)
        self.assertGreaterEqual(synchronizer.synchronize_calls, 3)
        self.assertEqual(composition_attempts, [2, 1, 3])
        self.assertEqual(runtime.configuration, replacement_runtime)
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
