from __future__ import annotations

import hashlib
import json
import unittest

from nvsop_contracts import (
    ConfigurationArtifact,
    ConfigurationBundle,
    ConfigurationTemplate,
    ConfiguredCamera,
    ConfiguredConnector,
    ConfiguredPoint,
    ConfiguredStation,
    EdgePreservation,
    Measured,
    Polled,
    ReportedDecision,
    ReportedHealth,
    ReportEvidence,
    ResolvedRuntimeParameters,
    Sequencing,
    TimestampSource,
    Unverified,
    configuration_from_wire,
    configuration_to_wire,
    reported_decision_from_wire,
    reported_decision_to_wire,
)


def template() -> ConfigurationTemplate:
    artifacts = (
        ConfigurationArtifact(
            "actions.json", "application/json", b"{}", hashlib.sha256(b"{}").hexdigest()
        ),
        ConfigurationArtifact(
            "vlm_prompts.txt",
            "text/plain",
            b"prompt\n",
            hashlib.sha256(b"prompt\n").hexdigest(),
        ),
        ConfigurationArtifact(
            "template.json", "application/json", b"{}", hashlib.sha256(b"{}").hexdigest()
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
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return ConfigurationTemplate(
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
    )


def bundle() -> ConfigurationBundle:
    measured = Measured(
        delivery=Polled(interval=0.5),
        max_delivery_delay=0.5,
        sequencing=Sequencing.SEQUENCED,
        edges=EdgePreservation.PRESERVED,
        timestamps=TimestampSource.HOST_RECEIPT,
    )
    return ConfigurationBundle(
        host_id="host-a",
        config_revision=7,
        generated_at="2026-09-13T00:00:00Z",
        stations=(
            ConfiguredStation(
                station_id="station-a",
                backend_id="backend-a",
                code="S-A",
                name="Station A",
                revision=3,
                runtime_parameters=ResolvedRuntimeParameters(10, 4, "stop"),
                connectors=(
                    ConfiguredConnector(
                        connector_id="connector-a",
                        name="PLC",
                        connector_type="modbus",
                        revision=2,
                        address="plc.local",
                        port=502,
                        capability=measured,
                    ),
                ),
                points=(
                    ConfiguredPoint(
                        point_id="point-a",
                        name="start",
                        direction="input",
                        connector_id="connector-a",
                        role="start_signal",
                        address="DI-01",
                    ),
                ),
                template=template(),
                cameras=(
                    ConfiguredCamera(
                        camera_id="camera-a",
                        name="Camera A",
                        address="camera.local",
                        main_stream_path="/main",
                        sub_stream_path="/sub",
                        credentials_configured=True,
                        revision=1,
                        media_path_mode="passthrough",
                        recording_mode="continuous",
                    ),
                ),
            ),
        ),
    )


class ConfigurationContractTests(unittest.TestCase):
    def test_round_trip_and_wire_preserves_sha256_and_excludes_credentials(self) -> None:
        value = bundle()
        wire = configuration_to_wire(value)

        self.assertEqual(configuration_from_wire(wire), value)
        serialized = json.dumps(wire, ensure_ascii=False)
        self.assertIn(value.sha256, serialized)
        assert value.stations[0].template is not None
        self.assertIn(value.stations[0].template.version_sha256, serialized)
        self.assertNotIn("password", serialized)
        self.assertTrue(wire["stations"][0]["cameras"][0]["credentials_configured"])  # type: ignore[index]

        tampered = dict(wire)
        tampered["sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            configuration_from_wire(tampered)

    def test_legacy_contract_version_remains_decodable(self) -> None:
        value = ConfigurationBundle(
            host_id="host-a",
            config_revision=1,
            generated_at="2026-09-13T00:00:00Z",
            stations=(),
            contract_version=1,
        )
        self.assertEqual(value.contract_version, 1)

        with self.assertRaisesRegex(ValueError, "contract version"):
            ConfigurationBundle(
                host_id="host-a",
                config_revision=1,
                generated_at="2026-09-13T00:00:00Z",
                stations=(),
                contract_version=3,
            )

    def test_template_manifest_and_artifact_shape_are_strict(self) -> None:
        value = template()
        manifest = json.dumps(
            {
                "artifacts": [],
                "format_version": 1,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        invalid = (
            *value.artifacts[:-1],
            ConfigurationArtifact(
                "manifest.json",
                "application/json",
                manifest,
                hashlib.sha256(manifest).hexdigest(),
            ),
        )

        with self.assertRaisesRegex(ValueError, "manifest"):
            ConfigurationTemplate(
                version_id=value.version_id,
                version_sha256=hashlib.sha256(manifest).hexdigest(),
                artifacts=invalid,
            )

        with self.assertRaises(ValueError):
            ConfigurationArtifact(
                "template.json", "text/plain", b"{}", hashlib.sha256(b"{}").hexdigest()
            )
        with self.assertRaisesRegex(ValueError, "version sha256"):
            ConfigurationTemplate(
                version_id=value.version_id,
                version_sha256="0" * 64,
                artifacts=value.artifacts,
            )

    def test_unverified_capability_is_wireable_but_secret_fields_are_not(self) -> None:
        value = bundle()
        self.assertIsInstance(value.stations[0].connectors[0].capability, Measured)
        invalid = configuration_to_wire(value)
        connector = invalid["stations"][0]["connectors"][0]  # type: ignore[index]
        connector["password"] = "not-allowed"  # pragma: allowlist secret
        with self.assertRaises(ValueError):
            configuration_from_wire(invalid)

        without_template = ConfigurationBundle(
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
                    connectors=(
                        ConfiguredConnector(
                            connector_id="connector-a",
                            name="PLC",
                            connector_type="modbus",
                            revision=1,
                            address="plc.local",
                            port=None,
                            capability=Unverified(),
                        ),
                    ),
                    points=(),
                    template=None,
                ),
            ),
        )
        self.assertEqual(
            configuration_from_wire(configuration_to_wire(without_template)), without_template
        )


class ReportContractTests(unittest.TestCase):
    def test_unknown_reason_code_survives_round_trip(self) -> None:
        report = ReportedDecision(
            event_id="host-a:42",
            trace_id="trace-42",
            host_id="host-a",
            station_id="station-a",
            backend_id="backend-a",
            instance_id=42,
            verdict="indeterminate",
            reason_codes=("FUTURE_REASON",),
            violations=(),
            lifecycle="closed",
            evidence=ReportEvidence(None, None, None),
            template_version_id=None,
            template_sha256=None,
            model_ids=("model-v1",),
            reported_at="2026-09-13T00:00:00Z",
        )
        self.assertEqual(reported_decision_from_wire(reported_decision_to_wire(report)), report)

    def test_non_finite_evidence_is_rejected_at_the_wire_boundary(self) -> None:
        report = ReportedDecision(
            event_id="host-a:43",
            trace_id="trace-43",
            host_id="host-a",
            station_id="station-a",
            backend_id="backend-a",
            instance_id=43,
            verdict="indeterminate",
            reason_codes=("FUTURE_REASON",),
            violations=(),
            lifecycle="closed",
            evidence=ReportEvidence(1.0, 0.5, 1.5),
            template_version_id=None,
            template_sha256=None,
            model_ids=(),
            reported_at="2026-09-13T00:00:00Z",
        )
        wire = reported_decision_to_wire(report)
        wire["evidence"] = {"anchor": float("nan"), "start": 0.5, "end": 1.5}
        with self.assertRaises(ValueError):
            reported_decision_from_wire(wire)

    def test_health_keeps_raw_status_and_reason(self) -> None:
        health = ReportedHealth(
            event_id="host-a:health:1",
            trace_id="trace-health-1",
            host_id="host-a",
            station_id="station-a",
            status="future_status",
            reason_code="FUTURE_REASON",
            detail="preserve me",
            reported_at="2026-09-13T00:00:00Z",
        )
        self.assertEqual(ReportedHealth.from_wire(health.to_wire()), health)


if __name__ == "__main__":
    unittest.main()
