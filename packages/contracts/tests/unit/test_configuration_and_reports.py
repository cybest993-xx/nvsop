from __future__ import annotations

import hashlib
import unittest

from nvsop_contracts import (
    ConfigurationArtifact,
    ConfigurationBundle,
    ConfigurationTemplate,
    ConfiguredConnector,
    ConfiguredPoint,
    ConfiguredStation,
    Measured,
    Polled,
    ResolvedRuntimeParameters,
    Unverified,
    configuration_from_wire,
    configuration_to_wire,
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
                ),
            ),
        )

        wire = configuration_to_wire(bundle)
        self.assertEqual(configuration_from_wire(wire), bundle)
        self.assertEqual(wire["sha256"], bundle.sha256)

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
        self.assertEqual(configuration_from_wire(configuration_to_wire(bundle)), bundle)
        invalid = configuration_to_wire(bundle)
        connector = invalid["stations"][0]["connectors"][0]  # type: ignore[index]
        connector["password"] = "not-allowed"  # pragma: allowlist secret
        with self.assertRaises(ValueError):
            configuration_from_wire(invalid)
