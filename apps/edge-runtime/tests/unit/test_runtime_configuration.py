from __future__ import annotations

import hashlib
import json
import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from nvsop_contracts import (
    ConfigurationArtifact,
    ConfigurationBundle,
    ConfigurationTemplate,
    ConfiguredConnector,
    ConfiguredPoint,
    ConfiguredStation,
    EdgePreservation,
    Measured,
    Polled,
    Pushed,
    ResolvedRuntimeParameters,
    Sequencing,
    TimestampSource,
    Unverified,
)

from edge_runtime.configuration import EdgeRuntimeConfiguration, LocalIsapiConnectorConfiguration
from edge_runtime.configuration_sync import ConfigurationPullError
from edge_runtime.connectors.hikvision import CANDIDATE_PROFILE
from edge_runtime.judgment.evidence import EvidenceMargins
from edge_runtime.judgment.model import Ordering, RuntimeParameters, Template
from edge_runtime.local_state.schema import migrate
from edge_runtime.local_state.store import LocalState
from edge_runtime.runtime import _synchronize_runtime_configuration
from edge_runtime.runtime_configuration import (
    RuntimeConfigurationError,
    bootstrap_runtime_configuration,
    confirmed_runtime_configuration,
)
from edge_runtime.station_runtime import StationRuntimeConfiguration

HOST_ID = "host-a"
STATION_ID = "station-a"
BACKEND_ID = "backend-a"
CONNECTOR_ID = "connector-a"


def local_station() -> StationRuntimeConfiguration:
    return StationRuntimeConfiguration(
        station_id=STATION_ID,
        backend_id=BACKEND_ID,
        inference_url="http://inference.example/v1/chat/completions",
        request_body={"stream": True, "messages": []},
        template=Template(
            steps=("(1) old",),
            ordering=Ordering.ORDERED,
            start_signal="(1) old",
        ),
        parameters=RuntimeParameters(idle_timeout=99.0, step_deadline=88.0),
        margins=EvidenceMargins(leading=1.0, trailing=2.0),
        model_ids=("stale-local-model",),
    )


def local_connector() -> LocalIsapiConnectorConfiguration:
    return LocalIsapiConnectorConfiguration(
        connector_id=CONNECTOR_ID,
        revision=1,
        credentials_configured=False,
        base_url="http://camera.example:80",
        username="",
        password="",
        profile=CANDIDATE_PROFILE,
        capability=Unverified(),
    )


def confirmed_bundle() -> ConfigurationBundle:
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
    ).encode("utf-8")
    artifact_values = (
        ("actions.json", "application/json", b"{}"),
        ("vlm_prompts.txt", "text/plain", b"prompt\n"),
        ("template.json", "application/json", content),
    )
    artifacts = tuple(
        ConfigurationArtifact(
            name, media_type, artifact_content, hashlib.sha256(artifact_content).hexdigest()
        )
        for name, media_type, artifact_content in artifact_values
    )
    manifest = json.dumps(
        {
            "artifacts": [
                {
                    "byte_length": len(artifact.content),
                    "media_type": artifact.media_type,
                    "name": artifact.name,
                    "sha256": artifact.sha256,
                }
                for artifact in artifacts
            ],
            "format_version": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    measured = Measured(
        delivery=Polled(interval=0.5),
        max_delivery_delay=0.5,
        sequencing=Sequencing.SEQUENCED,
        edges=EdgePreservation.PRESERVED,
        timestamps=TimestampSource.HOST_RECEIPT,
    )
    return ConfigurationBundle(
        host_id=HOST_ID,
        config_revision=7,
        generated_at="2026-09-14T00:00:00Z",
        stations=(
            ConfiguredStation(
                station_id=STATION_ID,
                backend_id=BACKEND_ID,
                code="S-A",
                name="Station A",
                revision=4,
                runtime_parameters=ResolvedRuntimeParameters(7.0, 3.0, "stop"),
                connectors=(
                    ConfiguredConnector(
                        connector_id=CONNECTOR_ID,
                        name="Camera IO",
                        connector_type="hikvision_isapi",
                        revision=6,
                        address="camera.example",
                        port=80,
                        capability=measured,
                    ),
                ),
                points=(
                    ConfiguredPoint(
                        point_id="point-in",
                        name="工件到位",
                        direction="input",
                        connector_id=CONNECTOR_ID,
                        role="start_signal",
                        address="1",
                    ),
                    ConfiguredPoint(
                        point_id="point-out",
                        name="停线联锁",
                        direction="output",
                        connector_id=CONNECTOR_ID,
                        role="safety_output",
                        address="2",
                    ),
                ),
                template=ConfigurationTemplate(
                    version_id="version-a",
                    version_sha256=hashlib.sha256(manifest).hexdigest(),
                    artifacts=(
                        *artifacts,
                        ConfigurationArtifact(
                            "manifest.json",
                            "application/json",
                            manifest,
                            hashlib.sha256(manifest).hexdigest(),
                        ),
                    ),
                ),
                model_ids=("reported-model", "reported-model-2"),
            ),
        ),
    )


class ConfirmedRuntimeConfigurationTests(unittest.TestCase):
    def test_bootstrap_keeps_local_connectors_out_of_station_topology(self) -> None:
        second = replace(local_station(), station_id="station-b", backend_id="backend-b")

        result = bootstrap_runtime_configuration(
            stations=(local_station(), second),
            connectors=(local_connector(),),
        )

        self.assertEqual(
            ("station-a", "station-b"),
            tuple(item.configuration.station_id for item in result.stations),
        )
        self.assertEqual((), result.stations[0].connector_ids)
        self.assertEqual((), result.stations[0].input_points)
        self.assertEqual((), result.stations[0].output_points)
        self.assertEqual((CONNECTOR_ID,), tuple(item.connector_id for item in result.connectors))

    def test_confirmed_values_replace_local_template_and_timing_and_bind_points(self) -> None:
        bundle = confirmed_bundle()
        configured_template = bundle.stations[0].template
        assert configured_template is not None
        result = confirmed_runtime_configuration(
            bundle=bundle,
            bootstrap_stations=(local_station(),),
            local_connectors=(local_connector(),),
        )

        station = result.stations[0]
        expected_configuration = replace(
            local_station(),
            template=Template(
                steps=("(1) confirmed",),
                ordering=Ordering.ORDERED,
                start_signal="(1) confirmed",
            ),
            parameters=RuntimeParameters(idle_timeout=7.0, step_deadline=3.0),
            backend_id=BACKEND_ID,
            template_version_id="version-a",
            template_sha256=configured_template.version_sha256,
            model_ids=("reported-model", "reported-model-2"),
            disposition_policy="stop",
        )
        self.assertEqual(station.configuration, expected_configuration)
        self.assertEqual(station.input_points_for(CONNECTOR_ID)[0].address, "1")
        self.assertEqual(station.output_points_for(CONNECTOR_ID)[0].address, "2")
        self.assertEqual(result.connectors[0].revision, 6)
        self.assertIsInstance(result.connectors[0].capability, Measured)

    def test_multiple_backend_slices_share_one_station_state_and_connector_set(self) -> None:
        bundle = confirmed_bundle()
        second_backend = replace(bundle.stations[0], backend_id="backend-b")
        local_backend = replace(local_station(), backend_id="backend-b")

        result = confirmed_runtime_configuration(
            bundle=replace(bundle, stations=(bundle.stations[0], second_backend)),
            bootstrap_stations=(local_station(), local_backend),
            local_connectors=(local_connector(),),
        )

        self.assertEqual(1, len(result.stations))
        self.assertEqual(2, len(result.stations[0].configurations))
        self.assertEqual((CONNECTOR_ID,), result.stations[0].connector_ids)
        self.assertEqual(1, len(result.stations[0].input_points_for(CONNECTOR_ID)))

    def test_pushed_confirmed_connector_is_rejected_before_confirmation(self) -> None:
        bundle = confirmed_bundle()
        connector = replace(
            bundle.stations[0].connectors[0],
            capability=Measured(
                delivery=Pushed(),
                max_delivery_delay=0.5,
                sequencing=Sequencing.SEQUENCED,
                edges=EdgePreservation.PRESERVED,
                timestamps=TimestampSource.HOST_RECEIPT,
            ),
        )
        station = replace(bundle.stations[0], connectors=(connector,))

        with self.assertRaisesRegex(RuntimeConfigurationError, "pushed delivery"):
            confirmed_runtime_configuration(
                bundle=replace(bundle, stations=(station,)),
                bootstrap_stations=(local_station(),),
                local_connectors=(local_connector(),),
            )

    def test_confirmed_topology_needs_the_local_credential_boundary(self) -> None:
        with self.assertRaisesRegex(RuntimeConfigurationError, "no local credential"):
            confirmed_runtime_configuration(
                bundle=confirmed_bundle(),
                bootstrap_stations=(local_station(),),
                local_connectors=(),
            )


class RuntimeSynchronizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        migrate(self.connection)
        self.state = LocalState(self.connection)
        self.config = EdgeRuntimeConfiguration(
            center_url="https://center.example",
            host_id=HOST_ID,
            host_private_key="private-key",  # pragma: allowlist secret
            command_timeout=1.0,
            command_poll_interval=1.0,
            connectors=(),
            ssl_context=None,
            local_state_path=Path("state.sqlite"),
            stations=(),
        )

    def tearDown(self) -> None:
        self.state.close()

    def test_first_start_uses_bootstrap_when_pull_fails_without_confirmation(self) -> None:
        with patch(
            "edge_runtime.runtime.HttpConfigurationPuller.pull",
            side_effect=ConfigurationPullError("center_unreachable", "center unavailable"),
        ):
            result = _synchronize_runtime_configuration(self.config, self.state)

        self.assertIsNone(result.confirmed)
        self.assertEqual((), result.stations)
        failure = self.state.configuration().failure()
        self.assertIsNotNone(failure)
        assert failure is not None
        self.assertEqual("center_unreachable", failure.code)

    def test_pull_failure_keeps_the_last_confirmed_bundle_active(self) -> None:
        bundle = ConfigurationBundle(
            host_id=HOST_ID,
            config_revision=1,
            generated_at="2026-09-13T00:00:00Z",
            stations=(),
        )
        self.state.configuration().confirm(bundle, confirmed_at=1.0)

        with patch(
            "edge_runtime.runtime.HttpConfigurationPuller.pull",
            side_effect=ConfigurationPullError("center_unreachable", "center unavailable"),
        ):
            result = _synchronize_runtime_configuration(self.config, self.state)

        self.assertEqual(bundle, result.confirmed)
        self.assertEqual((), result.stations)


if __name__ == "__main__":
    unittest.main()
