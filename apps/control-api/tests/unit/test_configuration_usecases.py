from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from factory_sop.configuration.usecases import configuration_for_host
from nvsop_contracts import (
    ConfigurationArtifact,
    ConfigurationBundle,
    ConfigurationTemplate,
    ConfiguredConnector,
    ConfiguredPoint,
    ConfiguredStation,
    ResolvedRuntimeParameters,
    Unverified,
)

HOST_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f101")
FOREIGN_HOST_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f102")
STATION_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f103")
BACKEND_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f104")
CONNECTOR_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f105")
FOREIGN_CONNECTOR_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f106")
POINT_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f107")


class Repo:
    def __init__(self) -> None:
        self.host = SimpleNamespace(id=HOST_ID, revision=4)
        self.backend = SimpleNamespace(
            id=BACKEND_ID,
            host_id=HOST_ID,
            status=SimpleNamespace(value="active"),
            self_reported_model_ids=("reported-model", "reported-model-2"),
            revision=3,
        )
        self.station = SimpleNamespace(
            id=STATION_ID,
            code="S-A",
            name="Station A",
            status=SimpleNamespace(value="active"),
            revision=2,
            runtime_parameters_revision=1,
        )
        self.station.runtime_parameters_for = lambda defaults: SimpleNamespace(
            idle_timeout_seconds=defaults.idle_timeout_seconds,
            step_deadline_seconds=defaults.step_deadline_seconds,
            disposition_policy=defaults.disposition_policy,
        )
        content = b"{}"
        self.version = SimpleNamespace(
            id=UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f108"),
            sha256="a" * 64,
            source_draft_revision=1,
            runtime_defaults=SimpleNamespace(
                idle_timeout_seconds=10.0,
                step_deadline_seconds=2.0,
                disposition_policy="stop",
            ),
            artifacts=(
                SimpleNamespace(
                    name=SimpleNamespace(value="template.json"),
                    media_type="application/json",
                    content=content,
                    sha256=hashlib.sha256(content).hexdigest(),
                ),
            ),
        )

    def by_id(self, value: UUID) -> object | None:
        if value == HOST_ID:
            return self.host
        if value == STATION_ID:
            return self.station
        return None

    def page_of(
        self, *, page: int, page_size: int, host_id: UUID | None = None
    ) -> tuple[list[object], int]:
        return ([self.backend], 1) if host_id == HOST_ID else ([], 0)

    def for_host(self, value: UUID) -> list[object]:
        if value != HOST_ID:
            return []
        return [
            SimpleNamespace(
                station_id=STATION_ID,
                backend_id=BACKEND_ID,
                host_id=HOST_ID,
                status=SimpleNamespace(value="active"),
                revision=1,
            )
        ]

    def for_station(self, value: UUID) -> list[object]:
        if value == STATION_ID:
            return [
                SimpleNamespace(
                    id=CONNECTOR_ID,
                    host_id=HOST_ID,
                    status=SimpleNamespace(value="active"),
                    name="PLC",
                    connector_type=SimpleNamespace(value="modbus"),
                    revision=8,
                    configuration=SimpleNamespace(address="plc.local", port=502),
                    capability=Unverified(),
                ),
                SimpleNamespace(
                    id=FOREIGN_CONNECTOR_ID,
                    host_id=FOREIGN_HOST_ID,
                    status=SimpleNamespace(value="active"),
                    name="foreign",
                    connector_type=SimpleNamespace(value="modbus"),
                    revision=1,
                    configuration=SimpleNamespace(address="foreign.local", port=502),
                    capability=Unverified(),
                ),
            ]
        return []

    def points_for_station(self, value: UUID) -> list[object]:
        return (
            [
                SimpleNamespace(
                    id=POINT_ID,
                    connector_id=CONNECTOR_ID,
                    revision=9,
                    status=SimpleNamespace(value="active"),
                    semantic_label="start",
                    identifier="DI-01",
                    direction=SimpleNamespace(value="input"),
                )
            ]
            if value == STATION_ID
            else []
        )

    def binding_by_station(self, value: UUID) -> object | None:
        return SimpleNamespace(
            desired_version_id=self.version.id,
            desired_sha256=self.version.sha256,
            desired_config_revision=1,
        )

    def version_by_id(self, value: UUID) -> object | None:
        return self.version if value == self.version.id else None


def test_configuration_for_host_excludes_foreign_connector_and_emits_effective_values() -> None:
    repo = Repo()
    bundle = configuration_for_host(
        host_id=HOST_ID,
        generated_at=datetime(2026, 9, 13, tzinfo=UTC),
        hosts=repo,  # type: ignore[arg-type]
        backends=repo,  # type: ignore[arg-type]
        stations=repo,  # type: ignore[arg-type]
        cameras=repo,  # type: ignore[arg-type]
        connectors=repo,  # type: ignore[arg-type]
        points=SimpleNamespace(for_station=repo.points_for_station),
        templates=repo,  # type: ignore[arg-type]
    )

    expected = ConfigurationBundle(
        host_id=str(HOST_ID),
        config_revision=9,
        generated_at="2026-09-13T00:00:00Z",
        stations=(
            ConfiguredStation(
                station_id=str(STATION_ID),
                backend_id=str(BACKEND_ID),
                code="S-A",
                name="Station A",
                revision=9,
                runtime_parameters=ResolvedRuntimeParameters(10.0, 2.0, "stop"),
                connectors=(
                    ConfiguredConnector(
                        connector_id=str(CONNECTOR_ID),
                        name="PLC",
                        connector_type="modbus",
                        revision=8,
                        address="plc.local",
                        port=502,
                        capability=Unverified(),
                    ),
                ),
                points=(
                    ConfiguredPoint(
                        point_id=str(POINT_ID),
                        name="start",
                        direction="input",
                        connector_id=str(CONNECTOR_ID),
                        role="input",
                        address="DI-01",
                    ),
                ),
                template=ConfigurationTemplate(
                    version_id=str(repo.version.id),
                    version_sha256="a" * 64,
                    artifacts=(
                        ConfigurationArtifact(
                            name="template.json",
                            media_type="application/json",
                            content=b"{}",
                            sha256=hashlib.sha256(b"{}").hexdigest(),
                        ),
                    ),
                ),
                model_ids=("reported-model", "reported-model-2"),
            ),
        ),
    )
    assert bundle == expected
