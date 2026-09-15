from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace

from nvsop_contracts import (
    ConfigurationArtifact,
    ConfigurationBundle,
    ConfigurationTemplate,
    ConfiguredConnector,
    ConfiguredPoint,
    ConfiguredStation,
    Measured,
    Polled,
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


class ConfigurationContractTests(unittest.TestCase):
    def test_round_trip_and_digest_cover_host_scoped_content(self) -> None:
        content = b'{"steps":[]}'
        artifact = ConfigurationArtifact(
            name="template.json",
            media_type="application/json",
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
        )
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
                    template=ConfigurationTemplate(
                        version_id="version-a",
                        version_sha256="a" * 64,
                        artifacts=(artifact,),
                    ),
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

        legacy_with_ids = configuration_to_wire(replace(bundle, contract_version=1))
        self.assertEqual(
            configuration_from_wire(legacy_with_ids), replace(bundle, contract_version=1)
        )

        legacy_without_ids = configuration_to_wire(
            replace(
                bundle,
                contract_version=1,
                stations=(replace(bundle.stations[0], model_ids=()),),
            )
        )
        old_stations = legacy_without_ids["stations"]
        assert isinstance(old_stations, list)
        assert isinstance(old_stations[0], dict)
        self.assertNotIn("model_ids", old_stations[0])
        self.assertEqual(
            configuration_from_wire(legacy_without_ids),
            replace(
                bundle,
                contract_version=1,
                stations=(replace(bundle.stations[0], model_ids=()),),
            ),
        )

        legacy_explicit_empty = dict(legacy_without_ids)
        explicit_stations = [dict(station) for station in old_stations]
        explicit_stations[0]["model_ids"] = []
        legacy_explicit_empty["stations"] = explicit_stations
        legacy_explicit_empty.pop("sha256")
        legacy_explicit_empty["sha256"] = hashlib.sha256(
            canonical_json(legacy_explicit_empty).encode("utf-8")
        ).hexdigest()
        decoded_explicit_empty = configuration_from_wire(legacy_explicit_empty)
        self.assertEqual(configuration_to_wire(decoded_explicit_empty), legacy_explicit_empty)
        self.assertEqual(decoded_explicit_empty.sha256, legacy_explicit_empty["sha256"])

        tampered = dict(wire)
        tampered["host_id"] = "host-b"
        with self.assertRaises(ValueError):
            configuration_from_wire(tampered)

    def test_template_manifest_digest_must_match_the_template_version(self) -> None:
        content = b'{"format_version":1}'
        artifact = ConfigurationArtifact(
            name="template.json",
            media_type="application/json",
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
        )
        manifest_content = b"{}"
        manifest = ConfigurationArtifact(
            name="manifest.json",
            media_type="application/json",
            content=manifest_content,
            sha256=hashlib.sha256(manifest_content).hexdigest(),
        )

        with self.assertRaises(ValueError):
            ConfigurationTemplate(
                version_id="version-a",
                version_sha256="a" * 64,
                artifacts=(artifact, manifest),
            )

    def test_unverified_capability_is_wireable_but_secret_fields_are_not(self) -> None:
        bundle = ConfigurationBundle(
            host_id="host-a",
            config_revision=1,
            generated_at="now",
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
        self.assertNotIn("model_ids", stations[0])
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
