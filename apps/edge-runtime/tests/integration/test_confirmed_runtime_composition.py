from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from random import Random
from time import monotonic
from unittest.mock import patch

from nvsop_contracts import (
    Capability,
    ConfigurationBundle,
    HostIdentityKeyPair,
    Unverified,
    capability_to_wire,
    generate_host_identity_key_pair,
)

from edge_runtime.configuration import LocalIsapiConnectorConfiguration
from edge_runtime.connectors.hikvision import CANDIDATE_PROFILE
from edge_runtime.connectors.port import (
    ConnectorHealth,
    InputPoint,
    OutputPoint,
    PointState,
    Reachability,
    ReadResult,
    Refused,
    TimedOut,
    Unreachable,
    WriteOutcome,
    WriteRefusal,
    Written,
)
from edge_runtime.connectors.writes import WriteRequest
from edge_runtime.judgment.model import HostInstant
from edge_runtime.local_state.store import open_local_state
from edge_runtime.runtime import AutonomousRuntime, build_autonomous_runtime_from_file

# This module is intentionally self-contained when invoked by the repository's unittest target.

DEFAULT_WRITE_OUTCOME = Written(at=HostInstant(1.5))


class RecordingConnector:
    """在连接器适配器接缝记录物理写入, 不修改具体 ISAPI 实现。"""

    def __init__(
        self,
        capability: Capability,
        outcome: WriteOutcome = DEFAULT_WRITE_OUTCOME,
    ) -> None:
        self.capability = capability
        self.writes: list[tuple[OutputPoint, PointState]] = []
        self._outcome = outcome

    def read(self, point: InputPoint, /, *, timeout: float) -> ReadResult:
        return Unreachable(detail="not under test")

    def probe(self, /, *, timeout: float) -> ConnectorHealth:
        return ConnectorHealth(reachability=Reachability.REACHABLE)

    def write(self, point: OutputPoint, state: PointState, /, *, timeout: float) -> WriteOutcome:
        self.writes.append((point, state))
        return self._outcome


def connector_factory_for(
    created: dict[str, RecordingConnector],
    *,
    outcome: WriteOutcome = DEFAULT_WRITE_OUTCOME,
) -> Callable[[LocalIsapiConnectorConfiguration], RecordingConnector]:
    def factory(configuration: LocalIsapiConnectorConfiguration) -> RecordingConnector:
        connector_id = configuration.connector_id
        capability = configuration.capability
        connector = created.get(connector_id)
        if connector is None:
            connector = RecordingConnector(capability, outcome=outcome)
            created[connector_id] = connector
        return connector

    return factory


def _fixture_host_identity(seed: int) -> HostIdentityKeyPair:
    random = Random(seed)
    with (
        patch("nvsop_contracts.host_identity.secrets.randbits", side_effect=random.getrandbits),
        patch("nvsop_contracts.host_identity.secrets.randbelow", side_effect=random.randrange),
    ):
        return generate_host_identity_key_pair()


class ConfirmedRuntimeCompositionIntegrationTest(unittest.TestCase):
    def test_unconfirmed_bootstrap_keeps_local_connectors_in_command_seam_only(self) -> None:
        identity = _fixture_host_identity(43)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            private_key_file = directory / "host-private-key"
            private_key_file.write_text(identity.private_key, encoding="utf-8")
            local_config = _local_config(directory, private_key_file)
            local_stations = local_config["stations"]
            assert isinstance(local_stations, list)
            local_stations.append(
                {
                    **local_stations[0],
                    "station_id": "station-b",
                    "backend_id": "backend-b",
                }
            )
            config_path = directory / "edge.json"
            config_path.write_text(json.dumps(local_config), encoding="utf-8")
            created: dict[str, RecordingConnector] = {}

            runtime = build_autonomous_runtime_from_file(
                config_path,
                connector_factory=connector_factory_for(created),
            )
            try:
                self.assertEqual(2, len(runtime.stations))
                self.assertTrue(all(not station.connector_runtimes for station in runtime.stations))
                self.assertTrue(all(not station.output_points for station in runtime.stations))
                self.assertEqual({}, runtime.output_dispatchers)
                connector_runtimes = runtime.connector_runtimes
                self.assertIsNotNone(connector_runtimes)
                assert connector_runtimes is not None
                self.assertEqual(0, len(connector_runtimes.runtimes))
                self.assertEqual(("connector-a",), tuple(created))
                started = monotonic()
                runtime.run_forever(should_stop=lambda: monotonic() - started >= 0.2)
            finally:
                runtime.close()

    def test_builder_uses_one_station_runtime_for_multiple_backend_slices(self) -> None:
        bundle = _bundle()
        second_slice = replace(bundle.stations[0], backend_id="backend-b")
        bundle = replace(bundle, stations=(bundle.stations[0], second_slice))
        identity = _fixture_host_identity(45)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state_path = directory / "state.sqlite"
            state = open_local_state(str(state_path))
            state.configuration().confirm(bundle, confirmed_at=1.0)
            state.close()

            private_key_file = directory / "host-private-key"
            private_key_file.write_text(identity.private_key, encoding="utf-8")
            local_config = _local_config(directory, private_key_file)
            local_stations = local_config["stations"]
            assert isinstance(local_stations, list)
            local_stations.append(
                {
                    **local_stations[0],
                    "backend_id": "backend-b",
                    "inference_url": "http://inference-b.example/v1/chat/completions",
                }
            )
            config_path = directory / "edge.json"
            config_path.write_text(json.dumps(local_config), encoding="utf-8")

            runtime = build_autonomous_runtime_from_file(config_path)
            try:
                self.assertEqual(1, len(runtime.stations))
                self.assertEqual(1, len(runtime.stations[0].connector_runtimes))
                connector_runtimes = runtime.connector_runtimes
                self.assertIsNotNone(connector_runtimes)
                assert connector_runtimes is not None
                self.assertEqual(1, len(connector_runtimes.runtimes))
            finally:
                runtime.close()

    def test_builder_uses_last_confirmed_station_and_composes_real_connector_seams(self) -> None:
        bundle = _bundle()
        identity = _fixture_host_identity(44)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state_path = directory / "state.sqlite"
            state = open_local_state(str(state_path))
            state.configuration().confirm(bundle, confirmed_at=1.0)
            state.close()

            private_key_file = directory / "host-private-key"
            private_key_file.write_text(identity.private_key, encoding="utf-8")
            config_path = directory / "edge.json"
            config_path.write_text(
                json.dumps(_local_config(directory, private_key_file)), encoding="utf-8"
            )

            created: dict[str, RecordingConnector] = {}
            runtime = build_autonomous_runtime_from_file(
                config_path,
                connector_factory=connector_factory_for(created),
            )
            self.assertIsInstance(runtime, AutonomousRuntime)
            try:
                self.assertEqual(
                    ("(1) confirmed",), runtime.stations[0].supervisor.state.template.steps
                )
                self.assertEqual(7.0, runtime.stations[0].supervisor.state.parameters.idle_timeout)
                self.assertEqual(3.0, runtime.stations[0].supervisor.state.parameters.step_deadline)
                self.assertIsNotNone(runtime.connector_runtimes)
                assert runtime.connector_runtimes is not None
                self.assertEqual(1, len(runtime.connector_runtimes.runtimes))
                self.assertEqual(0.5, runtime.connector_runtimes.runtimes[0].polling_interval)
                self.assertIn("connector-a", runtime.output_dispatchers)
                self.assertEqual("2", runtime.stations[0].output_points["connector-a"][0].address)
                request = WriteRequest(
                    point=OutputPoint(label="停线联锁", address="2"),
                    state=PointState.ACTIVE,
                    key="station-a:disposal-1",
                    actor="supervisor",
                    timeout=1.0,
                    capability_budget=1.0,
                    station_id="station-a",
                    connector_id="connector-a",
                    attempt_at=HostInstant(1.0),
                    lease_seconds=5.0,
                )
                self.assertEqual(
                    Written(at=HostInstant(1.5)), runtime.stations[0].write_output(request)
                )
                self.assertEqual(
                    [(OutputPoint(label="停线联锁", address="2"), PointState.ACTIVE)],
                    created["connector-a"].writes,
                )
                for invalid_request in (
                    replace(request, station_id="station-b"),
                    replace(request, connector_id="connector-b"),
                    replace(request, point=OutputPoint(label="未知输出", address="99")),
                ):
                    self.assertEqual(
                        Refused(
                            reason=WriteRefusal.OUTPUT_NOT_CONFIGURED,
                            detail=(
                                "输出请求不属于当前工位的已确认 topology"
                                if invalid_request.station_id != "station-a"
                                else "输出点不属于当前工位的已确认 output topology"
                            ),
                        ),
                        runtime.stations[0].write_output(invalid_request),
                    )
                self.assertEqual(
                    [(OutputPoint(label="停线联锁", address="2"), PointState.ACTIVE)],
                    created["connector-a"].writes,
                )
            finally:
                runtime.close()

            restarted_created: dict[str, RecordingConnector] = {}
            restarted = build_autonomous_runtime_from_file(
                config_path,
                connector_factory=connector_factory_for(restarted_created),
            )
            try:
                self.assertEqual(
                    Written(at=HostInstant(1.5)),
                    restarted.stations[0].write_output(
                        replace(request, attempt_at=HostInstant(2.0))
                    ),
                )
                self.assertEqual([], restarted_created["connector-a"].writes)
            finally:
                restarted.close()

    def test_timed_out_output_is_replayed_from_ledger_without_touching_adapter(self) -> None:
        bundle = _bundle()
        identity = _fixture_host_identity(46)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state_path = directory / "state.sqlite"
            state = open_local_state(str(state_path))
            state.configuration().confirm(bundle, confirmed_at=1.0)
            state.close()
            private_key_file = directory / "host-private-key"
            private_key_file.write_text(identity.private_key, encoding="utf-8")
            config_path = directory / "edge.json"
            config_path.write_text(
                json.dumps(_local_config(directory, private_key_file)), encoding="utf-8"
            )
            request = WriteRequest(
                point=OutputPoint(label="停线联锁", address="2"),
                state=PointState.ACTIVE,
                key="station-a:timed-out",
                actor="supervisor",
                timeout=1.0,
                capability_budget=1.0,
                station_id="station-a",
                connector_id="connector-a",
                attempt_at=HostInstant(1.0),
                lease_seconds=5.0,
            )
            first_created: dict[str, RecordingConnector] = {}
            first = build_autonomous_runtime_from_file(
                config_path,
                connector_factory=connector_factory_for(
                    first_created,
                    outcome=TimedOut(after=1.0),
                ),
            )
            try:
                expected = TimedOut(after=1.0)
                self.assertEqual(expected, first.stations[0].write_output(request))
                self.assertEqual(1, len(first_created["connector-a"].writes))
            finally:
                first.close()
            second_created: dict[str, RecordingConnector] = {}
            second = build_autonomous_runtime_from_file(
                config_path,
                connector_factory=connector_factory_for(second_created),
            )
            try:
                expected = TimedOut(after=1.0)
                self.assertEqual(
                    expected,
                    second.stations[0].write_output(replace(request, attempt_at=HostInstant(2.0))),
                )
                self.assertEqual([], second_created["connector-a"].writes)
            finally:
                second.close()


def _local_config(directory: Path, private_key_file: Path) -> dict[str, object]:
    profile = {
        "input_status_path": CANDIDATE_PROFILE.input_status_path,
        "output_trigger_path": CANDIDATE_PROFILE.output_trigger_path,
        "output_body": CANDIDATE_PROFILE.output_body,
        "device_info_path": CANDIDATE_PROFILE.device_info_path,
        "input_state_element": CANDIDATE_PROFILE.input_state_element,
        "input_tokens": [
            {"token": token, "state": state.value}
            for token, state in CANDIDATE_PROFILE.input_tokens
        ],
        "output_tokens": [
            {"state": state.value, "token": token}
            for state, token in CANDIDATE_PROFILE.output_tokens
        ],
    }
    return {
        "center_url": "https://center.example",
        "host_id": "host-a",
        "host_private_key_file": str(private_key_file),
        "command_timeout_seconds": 1.0,
        "command_poll_interval_seconds": 0.1,
        "connectors": [
            {
                "connector_id": "connector-a",
                "revision": 1,
                "credentials_configured": False,
                "base_url": "http://camera.example:80",
                "profile": profile,
                "capability": capability_to_wire(Unverified()),
            }
        ],
        "local_state_path": str(directory / "state.sqlite"),
        "stations": [
            {
                "station_id": "station-a",
                "backend_id": "backend-a",
                "inference_url": "http://inference.example/v1/chat/completions",
                "request": {"stream": True, "messages": []},
                "template": {
                    "steps": ["(1) local"],
                    "ordering": "ordered",
                    "start_signal": "(1) local",
                    "end_signals": [],
                },
                "parameters": {"idle_timeout": 99, "step_deadline": 88},
                "margins": {"leading": 0, "trailing": 0},
            }
        ],
    }


def _bundle() -> ConfigurationBundle:
    # Reuse the exact contract shape without depending on another test file's import path.
    from hashlib import sha256

    from nvsop_contracts import (
        ConfigurationArtifact,
        ConfigurationTemplate,
        ConfiguredConnector,
        ConfiguredPoint,
        ConfiguredStation,
        EdgePreservation,
        Measured,
        Polled,
        ResolvedRuntimeParameters,
        Sequencing,
        TimestampSource,
    )

    content = json.dumps(
        {
            "boundary": {
                "end_signals": [],
                "start_signal": {"action_number": 1, "kind": "action"},
            },
            "format_version": 1,
            "ordering": "strict",
            "runtime_defaults": {
                "disposition_policy": "stop",
                "idle_timeout_seconds": 1,
                "step_deadline_seconds": 1,
            },
            "steps": [{"description": "(1) confirmed", "name": "Confirmed", "number": 1}],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    artifact = ConfigurationArtifact(
        name="template.json",
        media_type="application/json",
        content=content,
        sha256=sha256(content).hexdigest(),
    )
    return ConfigurationBundle(
        host_id="host-a",
        config_revision=7,
        generated_at="2026-09-14T00:00:00Z",
        stations=(
            ConfiguredStation(
                station_id="station-a",
                backend_id="backend-a",
                code="S-A",
                name="Station A",
                revision=4,
                runtime_parameters=ResolvedRuntimeParameters(7, 3, "stop"),
                connectors=(
                    ConfiguredConnector(
                        connector_id="connector-a",
                        name="Camera IO",
                        connector_type="hikvision_isapi",
                        revision=6,
                        address="camera.example",
                        port=80,
                        capability=Measured(
                            delivery=Polled(interval=0.5),
                            max_delivery_delay=0.5,
                            sequencing=Sequencing.SEQUENCED,
                            edges=EdgePreservation.PRESERVED,
                            timestamps=TimestampSource.HOST_RECEIPT,
                        ),
                    ),
                ),
                points=(
                    ConfiguredPoint(
                        point_id="point-in",
                        name="工件到位",
                        direction="input",
                        connector_id="connector-a",
                        role="start_signal",
                        address="1",
                    ),
                    ConfiguredPoint(
                        point_id="point-out",
                        name="停线联锁",
                        direction="output",
                        connector_id="connector-a",
                        role="safety_output",
                        address="2",
                    ),
                ),
                template=ConfigurationTemplate(
                    version_id="version-a",
                    version_sha256="a" * 64,
                    artifacts=(artifact,),
                ),
            ),
        ),
    )


if __name__ == "__main__":
    unittest.main()
