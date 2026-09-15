from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace

from nvsop_contracts import (
    ConfigurationArtifact,
    ConfigurationBundle,
    ConfigurationTemplate,
    ConfiguredCamera,
    ConfiguredConnector,
    ConfiguredStation,
    ReportedDecision,
    ReportedHealth,
    ReportEvidence,
    ResolvedRuntimeParameters,
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
            b"prompt\\n",
            hashlib.sha256(b"prompt\\n").hexdigest(),
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
                "manifest.json", "application/json", manifest, hashlib.sha256(manifest).hexdigest()
            ),
        ),
    )


def bundle() -> ConfigurationBundle:
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
                revision=1,
                runtime_parameters=ResolvedRuntimeParameters(1, 1, "stop"),
                connectors=(),
                points=(),
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
                model_ids=("reported-model", "reported-model-2"),
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
        template_value = template()
        manifest = json.dumps(
            {"artifacts": [], "format_version": 1}, separators=(",", ":")
        ).encode()
        invalid = ConfigurationArtifact(
            "manifest.json", "application/json", manifest, hashlib.sha256(manifest).hexdigest()
        )
        with self.assertRaises(ValueError):
            ConfigurationTemplate(
                version_id=template_value.version_id,
                version_sha256=template_value.version_sha256,
                artifacts=(*template_value.artifacts[:-1], invalid),
            )

        bundle_value = bundle()
        wire = configuration_to_wire(bundle_value)
        self.assertEqual(configuration_from_wire(wire), bundle_value)
        self.assertEqual(wire["sha256"], bundle_value.sha256)
        stations = wire["stations"]
        assert isinstance(stations, list) and isinstance(stations[0], dict)
        self.assertEqual(stations[0]["model_ids"], ["reported-model", "reported-model-2"])
        legacy = replace(
            bundle_value,
            contract_version=1,
            stations=(replace(bundle_value.stations[0], model_ids=()),),
        )
        legacy_wire = configuration_to_wire(legacy)
        legacy_stations = legacy_wire["stations"]
        assert isinstance(legacy_stations, list) and isinstance(legacy_stations[0], dict)
        station = legacy_stations[0]
        self.assertNotIn("model_ids", station)
        self.assertEqual(configuration_from_wire(legacy_wire), legacy)

    def test_template_manifest_digest_must_match_the_template_version(self) -> None:
        value = template()
        manifest_content = b"{}"
        manifest = ConfigurationArtifact(
            "manifest.json",
            "application/json",
            manifest_content,
            hashlib.sha256(manifest_content).hexdigest(),
        )

        with self.assertRaises(ValueError):
            ConfigurationTemplate(
                version_id=value.version_id,
                version_sha256=hashlib.sha256(manifest_content).hexdigest(),
                artifacts=(*value.artifacts[:-1], manifest),
            )

    def test_unverified_capability_is_wireable_but_secret_fields_are_not(self) -> None:
        bundle = ConfigurationBundle(
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
        wire = configuration_to_wire(bundle)
        self.assertEqual(configuration_from_wire(wire), bundle)
        stations = wire["stations"]
        assert isinstance(stations, list) and isinstance(stations[0], dict)
        self.assertEqual(stations[0]["model_ids"], [])
        stations[0]["password"] = "not-allowed"  # pragma: allowlist secret
        with self.assertRaises(ValueError):
            configuration_from_wire(wire)


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
