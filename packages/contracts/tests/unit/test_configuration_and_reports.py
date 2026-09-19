from __future__ import annotations

import copy
import hashlib
import json
import unittest
from dataclasses import replace

from nvsop_contracts import (
    DECISION_REPORT_CONTRACT_VERSION,
    ConfigurationArtifact,
    ConfigurationBundle,
    ConfigurationTemplate,
    ConfiguredConnector,
    ConfiguredPoint,
    ConfiguredStation,
    Measured,
    Polled,
    ReportBackendProvenance,
    ReportedDecision,
    ReportedHealth,
    ReportEvidence,
    ResolvedRuntimeParameters,
    Unverified,
    canonical_json,
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


class ConfigurationContractTests(unittest.TestCase):
    def test_round_trip_and_digest_cover_host_scoped_content(self) -> None:
        template_value = template()
        bundle = ConfigurationBundle(
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
                            capability=Measured(
                                delivery=Polled(interval=0.5),
                                max_delivery_delay=0.5,
                                sequencing=__import__("nvsop_contracts").Sequencing.SEQUENCED,
                                edges=__import__("nvsop_contracts").EdgePreservation.PRESERVED,
                                timestamps=__import__(
                                    "nvsop_contracts"
                                ).TimestampSource.HOST_RECEIPT,
                            ),
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
                    template=template_value,
                    model_ids=("reported-model", "reported-model-2"),
                ),
            ),
        )

        wire = configuration_to_wire(bundle)
        self.assertEqual(configuration_from_wire(wire), bundle)
        self.assertEqual(wire["sha256"], bundle.sha256)
        stations = wire["stations"]
        assert isinstance(stations, list)
        assert isinstance(stations[0], dict)
        self.assertEqual(stations[0]["model_ids"], ["reported-model", "reported-model-2"])

        metadata_wire = copy.deepcopy(wire)
        metadata_wire["producer"] = "control-api@2026.09"
        metadata_wire.pop("sha256")
        metadata_wire["sha256"] = hashlib.sha256(
            canonical_json(metadata_wire).encode("utf-8")
        ).hexdigest()
        with_metadata = configuration_from_wire(metadata_wire)
        self.assertEqual(with_metadata.producer, "control-api@2026.09")
        self.assertEqual(with_metadata.effective_sha256, bundle.effective_sha256)
        self.assertEqual(with_metadata.stable_content_wire(), bundle.stable_content_wire())

        capability_wire = copy.deepcopy(wire)
        capability_wire["required_capabilities"] = ["future.behavior"]
        capability_wire.pop("sha256")
        capability_wire["sha256"] = hashlib.sha256(
            canonical_json(capability_wire).encode("utf-8")
        ).hexdigest()
        with_capability = configuration_from_wire(capability_wire)
        self.assertEqual(with_capability.required_capabilities, ("future.behavior",))
        self.assertNotEqual(with_capability.effective_sha256, bundle.effective_sha256)

        legacy_wire = copy.deepcopy(wire)
        legacy_wire["contract_version"] = 1
        legacy_wire.pop("sha256")
        legacy_wire["sha256"] = hashlib.sha256(
            canonical_json(legacy_wire).encode("utf-8")
        ).hexdigest()
        with self.assertRaisesRegex(ValueError, "contract version"):
            configuration_from_wire(legacy_wire)

        missing_core_field = copy.deepcopy(wire)
        missing_stations = missing_core_field["stations"]
        assert isinstance(missing_stations, list)
        assert isinstance(missing_stations[0], dict)
        missing_stations[0].pop("model_ids")
        missing_core_field.pop("sha256")
        missing_core_field["sha256"] = hashlib.sha256(
            canonical_json(missing_core_field).encode("utf-8")
        ).hexdigest()
        with self.assertRaisesRegex(ValueError, "unsupported or missing fields"):
            configuration_from_wire(missing_core_field)

        unknown_field = copy.deepcopy(wire)
        unknown_field["extensions"] = {}
        unknown_field.pop("sha256")
        unknown_field["sha256"] = hashlib.sha256(
            canonical_json(unknown_field).encode("utf-8")
        ).hexdigest()
        with self.assertRaisesRegex(ValueError, "unsupported or missing fields"):
            configuration_from_wire(unknown_field)

        tampered = dict(wire)
        tampered["host_id"] = "host-b"
        with self.assertRaises(ValueError):
            configuration_from_wire(tampered)

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
        assert isinstance(stations, list)
        assert isinstance(stations[0], dict)
        self.assertEqual(stations[0]["model_ids"], [])
        invalid = wire
        connector = invalid["stations"][0]["connectors"][0]  # type: ignore[index]
        connector["password"] = "not-allowed"  # pragma: allowlist secret
        with self.assertRaises(ValueError):
            configuration_from_wire(invalid)


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

    def test_v2_configuration_proof_and_backend_provenance_are_strict(self) -> None:
        report = ReportedDecision(
            event_id="host-a:proof",
            trace_id="trace-proof",
            host_id="host-a",
            station_id="station-a",
            backend_id=None,
            instance_id=1,
            verdict="pass",
            reason_codes=(),
            violations=(),
            lifecycle="closed",
            evidence=ReportEvidence(None, None, None),
            template_version_id="template-a",
            template_sha256="a" * 64,
            model_ids=(),
            reported_at="2026-09-13T00:00:00Z",
            backend_provenance=(
                ReportBackendProvenance("backend-a", ("model-a",)),
                ReportBackendProvenance("backend-b", ("model-b",)),
            ),
            configuration_revision=7,
            configuration_sha256="b" * 64,
            contract_version=DECISION_REPORT_CONTRACT_VERSION,
        )
        wire = reported_decision_to_wire(report)
        self.assertEqual(wire["configuration_revision"], 7)
        self.assertEqual(wire["configuration_sha256"], "b" * 64)
        self.assertNotIn("backend_id", wire)
        self.assertNotIn("model_ids", wire)
        self.assertEqual(reported_decision_from_wire(wire), report)

        missing_digest = reported_decision_to_wire(report)
        del missing_digest["configuration_sha256"]
        with self.assertRaises(ValueError):
            reported_decision_from_wire(missing_digest)

        invalid_revision = reported_decision_to_wire(report)
        invalid_revision["configuration_revision"] = 0
        with self.assertRaises(ValueError):
            reported_decision_from_wire(invalid_revision)

        invalid_digest = reported_decision_to_wire(report)
        invalid_digest["configuration_sha256"] = "not-a-sha256"
        with self.assertRaises(ValueError):
            reported_decision_from_wire(invalid_digest)

        mixed_v1_field = reported_decision_to_wire(report)
        mixed_v1_field["backend_id"] = "backend-a"
        with self.assertRaises(ValueError):
            reported_decision_from_wire(mixed_v1_field)

        no_backend_input = replace(report, backend_provenance=())
        self.assertEqual(
            reported_decision_from_wire(reported_decision_to_wire(no_backend_input)),
            no_backend_input,
        )

    def test_legacy_report_omits_configuration_proof_fields(self) -> None:
        report = ReportedDecision(
            event_id="host-a:legacy",
            trace_id="trace-legacy",
            host_id="host-a",
            station_id="station-a",
            backend_id="backend-a",
            instance_id=2,
            verdict="pass",
            reason_codes=(),
            violations=(),
            lifecycle="closed",
            evidence=ReportEvidence(None, None, None),
            template_version_id=None,
            template_sha256=None,
            model_ids=(),
            reported_at="2026-09-13T00:00:00Z",
        )
        wire = reported_decision_to_wire(report)
        self.assertNotIn("configuration_revision", wire)
        self.assertNotIn("configuration_sha256", wire)
        self.assertEqual(reported_decision_from_wire(wire), report)

    def test_legacy_report_wire_shape_remains_strict_v1(self) -> None:
        report = ReportedDecision(
            event_id="host-a:legacy-shape",
            trace_id="trace-legacy-shape",
            host_id="host-a",
            station_id="station-a",
            backend_id="backend-a",
            instance_id=3,
            verdict="pass",
            reason_codes=(),
            violations=(),
            lifecycle="closed",
            evidence=ReportEvidence(None, None, None),
            template_version_id=None,
            template_sha256=None,
            model_ids=(),
            reported_at="2026-09-13T00:00:00Z",
        )
        wire = reported_decision_to_wire(report)
        legacy_keys = {
            "contract_version",
            "event_id",
            "trace_id",
            "host_id",
            "station_id",
            "backend_id",
            "instance_id",
            "verdict",
            "reason_codes",
            "violations",
            "lifecycle",
            "evidence",
            "template_version_id",
            "template_sha256",
            "model_ids",
            "reported_at",
        }
        self.assertEqual(set(wire), legacy_keys)
        incompatible = dict(wire)
        incompatible["configuration_revision"] = 1
        with self.assertRaises(ValueError):
            reported_decision_from_wire(incompatible)

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
