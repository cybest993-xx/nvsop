from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from random import Random
from unittest.mock import patch

from nvsop_contracts import (
    ConfigurationBundle,
    HostIdentityKeyPair,
    Unverified,
    capability_to_wire,
    configuration_to_wire,
    generate_host_identity_key_pair,
)

from edge_runtime.connectors.hikvision import CANDIDATE_PROFILE
from edge_runtime.connectors.port import OutputPoint, PointState, Written
from edge_runtime.connectors.writes import WriteRequest
from edge_runtime.judgment.model import (
    Decision,
    EvidenceSpan,
    HostInstant,
    Instance,
    JudgmentState,
    Lifecycle,
    Ordering,
    RuntimeParameters,
    Template,
)
from edge_runtime.judgment.reasons import Verdict
from edge_runtime.local_state.queues import BackendReportContext, ReportContext
from edge_runtime.local_state.store import open_local_state
from edge_runtime.runtime import AutonomousRuntime, build_autonomous_runtime_from_file

# This module is intentionally self-contained when invoked by the repository's unittest target.


def _fixture_host_identity(seed: int) -> HostIdentityKeyPair:
    random = Random(seed)
    with (
        patch("nvsop_contracts.host_identity.secrets.randbits", side_effect=random.getrandbits),
        patch("nvsop_contracts.host_identity.secrets.randbelow", side_effect=random.randrange),
    ):
        return generate_host_identity_key_pair()


class ConfirmedRuntimeCompositionIntegrationTest(unittest.TestCase):
    def test_builder_uses_one_station_runtime_for_multiple_backend_slices(self) -> None:
        bundle = _bundle()
        second_slice = replace(bundle.stations[0], backend_id="backend-b", model_ids=("model-b",))
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

    def test_removed_station_keeps_outbox_only_reporter_until_historical_report_is_sent(
        self,
    ) -> None:
        bundle_n = _bundle()
        bundle_n1 = replace(
            bundle_n,
            config_revision=bundle_n.config_revision + 1,
            generated_at="2026-09-14T00:05:00Z",
            stations=(),
        )
        identity = _fixture_host_identity(46)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state_path = directory / "state.sqlite"
            state = open_local_state(str(state_path))
            station_n = bundle_n.stations[0]
            assert station_n.template is not None
            assert station_n.backend_id is not None
            provenance = BackendReportContext(station_n.backend_id, station_n.model_ids)
            station = state.station(
                station_n.station_id,
                report_context=ReportContext(
                    host_id=bundle_n.host_id,
                    station_id=station_n.station_id,
                    backends=(provenance,),
                    template_version_id=station_n.template.version_id,
                    template_sha256=station_n.template.version_sha256,
                    configuration_revision=bundle_n.config_revision,
                    configuration_sha256=bundle_n.effective_sha256,
                    configuration_json=json.dumps(
                        configuration_to_wire(bundle_n),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                ),
            )
            instance = Instance(
                instance_id=1,
                opened_at=HostInstant(1.0),
                last_observation_at=HostInstant(2.0),
            )
            station.commit(
                state=JudgmentState(
                    template=Template(
                        steps=("(1) confirmed",),
                        ordering=Ordering.ORDERED,
                        start_signal="(1) confirmed",
                    ),
                    parameters=RuntimeParameters(idle_timeout=7.0, step_deadline=3.0),
                    next_instance_id=2,
                ),
                decisions=(
                    Decision(
                        instance_id=1,
                        verdict=Verdict.PASS,
                        reasons=(),
                        violations=(),
                        lifecycle=Lifecycle.CLOSED_BY_END_SIGNAL,
                        evidence=EvidenceSpan.at(HostInstant(2.0)),
                    ),
                ),
                evidence=(),
                closed_instances=(instance,),
                report_provenance={1: (provenance,)},
            )
            self.assertEqual((station_n.station_id,), state.pending_report_station_ids())
            state.configuration().confirm(bundle_n1, confirmed_at=3.0)
            state.close()

            private_key_file = directory / "host-private-key"
            private_key_file.write_text(identity.private_key, encoding="utf-8")
            config_path = directory / "edge.json"
            config_path.write_text(
                json.dumps(_local_config(directory, private_key_file)), encoding="utf-8"
            )

            runtime = build_autonomous_runtime_from_file(config_path)
            try:
                self.assertEqual((), runtime.stations)
                self.assertEqual(1, len(runtime._reporters))
                with (
                    patch(
                        "edge_runtime.reporting_transport.HttpDecisionReportTransport.send_decision"
                    ) as send_decision,
                    patch(
                        "edge_runtime.reporting_transport.HttpDecisionReportTransport.send_instance"
                    ) as send_instance,
                ):
                    attempts = runtime._reporters[0].flush(
                        now=HostInstant(4.0),
                        reported_at="2026-09-14T00:10:00Z",
                    )
                self.assertEqual(1, len(attempts))
                self.assertTrue(attempts[0].sent)
                send_decision.assert_called_once()
                send_instance.assert_called_once()

                inspection = open_local_state(str(state_path))
                try:
                    self.assertEqual((), inspection.pending_report_station_ids())
                finally:
                    inspection.close()
            finally:
                runtime.close()

    def test_startup_assembly_failure_does_not_advance_durable_confirmation(self) -> None:
        confirmed = _bundle()
        candidate = replace(
            confirmed,
            config_revision=confirmed.config_revision + 1,
            generated_at="2026-09-14T00:05:00Z",
        )
        identity = _fixture_host_identity(47)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state_path = directory / "state.sqlite"
            state = open_local_state(str(state_path))
            state.configuration().confirm(confirmed, confirmed_at=1.0)
            state.close()

            private_key_file = directory / "host-private-key"
            private_key_file.write_text(identity.private_key, encoding="utf-8")
            config_path = directory / "edge.json"
            config_path.write_text(
                json.dumps(_local_config(directory, private_key_file)), encoding="utf-8"
            )

            with (
                patch(
                    "edge_runtime.runtime.HttpConfigurationPuller.pull",
                    return_value=candidate,
                ),
                patch(
                    "edge_runtime.runtime.build_connection_test_loop",
                    side_effect=ValueError("command runtime assembly failed"),
                ),
                self.assertRaisesRegex(ValueError, "command runtime assembly failed"),
            ):
                build_autonomous_runtime_from_file(config_path)

            inspection = open_local_state(str(state_path))
            try:
                self.assertEqual(confirmed, inspection.configuration().confirmed())
            finally:
                inspection.close()

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

            runtime = build_autonomous_runtime_from_file(config_path)
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
                with patch(
                    "edge_runtime.connectors.hikvision.IsapiConnector.write",
                    return_value=Written(at=HostInstant(1.5)),
                ) as physical_write:
                    self.assertEqual(
                        Written(at=HostInstant(1.5)),
                        runtime.stations[0].write_output(request),
                    )
                    physical_write.assert_called_once()
            finally:
                runtime.close()

            restarted = build_autonomous_runtime_from_file(config_path)
            try:
                with patch(
                    "edge_runtime.connectors.hikvision.IsapiConnector.write",
                    return_value=Written(at=HostInstant(2.5)),
                ) as physical_write:
                    self.assertEqual(
                        Written(at=HostInstant(1.5)),
                        restarted.stations[0].write_output(
                            replace(request, attempt_at=HostInstant(2.0))
                        ),
                    )
                    physical_write.assert_not_called()
            finally:
                restarted.close()


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
    artifacts = (
        ConfigurationArtifact("actions.json", "application/json", b"{}", sha256(b"{}").hexdigest()),
        ConfigurationArtifact(
            "vlm_prompts.txt", "text/plain", b"prompt\\n", sha256(b"prompt\\n").hexdigest()
        ),
        ConfigurationArtifact(
            "template.json", "application/json", content, sha256(content).hexdigest()
        ),
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
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    template = ConfigurationTemplate(
        version_id="version-a",
        version_sha256=sha256(manifest).hexdigest(),
        artifacts=(
            *artifacts,
            ConfigurationArtifact(
                "manifest.json", "application/json", manifest, sha256(manifest).hexdigest()
            ),
        ),
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
                template=template,
            ),
        ),
    )


if __name__ == "__main__":
    unittest.main()
