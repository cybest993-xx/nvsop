from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from factory_sop.configuration.composition import (
    ConfigurationAssemblyError,
    configuration_for_host,
    register_confirmed_configuration,
)
from factory_sop.device.api import (
    DeviceConfigurationError,
    DeviceConfigurationTarget,
    DeviceConfigurationTopology,
)
from factory_sop.template.api import TemplateConfigurationProjection
from nvsop_contracts import ConfigurationBundle, ConfiguredStation, ResolvedRuntimeParameters

HOST_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f101")
STATION_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f103")
BACKEND_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f104")


class DeviceGateway:
    def __init__(self) -> None:
        self.minimum_revision: int | None = None
        self.confirmed: list[ConfigurationBundle] = []
        self.station_error: DeviceConfigurationError | None = None
        self.confirm_error: DeviceConfigurationError | None = None

    def authenticate(self, *, host: object, now: datetime) -> None:
        del host, now

    def topology_for_host(self, host_id: UUID) -> DeviceConfigurationTopology:
        assert host_id == HOST_ID
        return DeviceConfigurationTopology(
            host_id=HOST_ID,
            host_revision=4,
            backend_revisions=(5,),
            targets=(
                DeviceConfigurationTarget(
                    station_id=STATION_ID,
                    backend_id=BACKEND_ID,
                ),
            ),
        )

    def station_configuration(
        self,
        *,
        host_id: UUID,
        target: DeviceConfigurationTarget,
        runtime_defaults: ResolvedRuntimeParameters | None,
    ) -> ConfiguredStation:
        assert host_id == HOST_ID
        assert target.station_id == STATION_ID
        assert target.backend_id == BACKEND_ID
        if self.station_error is not None:
            raise self.station_error
        assert runtime_defaults is not None
        return ConfiguredStation(
            station_id=str(STATION_ID),
            backend_id=str(BACKEND_ID),
            code="S-A",
            name="Station A",
            revision=7,
            runtime_parameters=runtime_defaults,
            connectors=(),
            points=(),
            template=None,
            model_ids=("reported-model",),
        )

    def finalize_configuration(
        self, candidate: ConfigurationBundle, *, minimum_revision: int
    ) -> ConfigurationBundle:
        self.minimum_revision = minimum_revision
        return replace(candidate, config_revision=minimum_revision + 1)

    def confirm_configuration(self, *, host_id: UUID, bundle: ConfigurationBundle) -> None:
        assert host_id == HOST_ID
        if self.confirm_error is not None:
            raise self.confirm_error
        self.confirmed.append(bundle)


class TemplateGateway:
    def for_station(self, station_id: UUID) -> TemplateConfigurationProjection:
        assert station_id == STATION_ID
        return TemplateConfigurationProjection(
            template=None,
            runtime_defaults=ResolvedRuntimeParameters(
                idle_timeout_seconds=10.0,
                step_deadline_seconds=2.0,
                disposition_policy="stop",
            ),
            revision=9,
        )


def test_configuration_composition_uses_only_owner_projections() -> None:
    device = DeviceGateway()

    bundle = configuration_for_host(
        host_id=HOST_ID,
        generated_at=datetime(2026, 9, 13, tzinfo=UTC),
        device=device,
        templates=TemplateGateway(),
    )

    assert device.minimum_revision == 9
    assert bundle.config_revision == 10
    assert bundle.host_id == str(HOST_ID)
    assert len(bundle.stations) == 1
    station = bundle.stations[0]
    assert station.station_id == str(STATION_ID)
    assert station.backend_id == str(BACKEND_ID)
    assert station.revision == 9
    assert station.runtime_parameters.idle_timeout_seconds == 10.0
    assert station.model_ids == ("reported-model",)


def test_configuration_composition_translates_owner_errors() -> None:
    device = DeviceGateway()
    device.station_error = DeviceConfigurationError("host topology contains a foreign camera")

    with pytest.raises(ConfigurationAssemblyError, match="foreign camera"):
        configuration_for_host(
            host_id=HOST_ID,
            generated_at=datetime(2026, 9, 13, tzinfo=UTC),
            device=device,
            templates=TemplateGateway(),
        )


def test_confirmed_configuration_stays_behind_device_owner_seam() -> None:
    device = DeviceGateway()
    bundle = configuration_for_host(
        host_id=HOST_ID,
        generated_at=datetime(2026, 9, 13, tzinfo=UTC),
        device=device,
        templates=TemplateGateway(),
    )

    register_confirmed_configuration(host_id=HOST_ID, bundle=bundle, device=device)
    assert device.confirmed == [bundle]

    device.confirm_error = DeviceConfigurationError(
        "confirmed configuration was not issued by this Center"
    )
    with pytest.raises(ConfigurationAssemblyError, match="not issued"):
        register_confirmed_configuration(host_id=HOST_ID, bundle=bundle, device=device)
