from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from factory_sop.configuration.composition import (
    ConfigurationAssemblyError,
    configuration_for_host,
    host_configuration_pull,
    register_confirmed_configuration,
)
from factory_sop.device.api import (
    DeviceConfigurationError,
    DeviceConfigurationTarget,
    DeviceConfigurationTopology,
)
from factory_sop.execution.api import StationGrant
from factory_sop.template.api import TemplateConfigurationProjection
from nvsop_contracts import (
    DISPOSITION_SOUND_LIGHT_OUTPUT_CAPABILITY,
    DISPOSITION_STOP_OUTPUT_CAPABILITY,
    EXECUTION_LEASE_WRITE_GATE_CAPABILITY,
    SOUND_LIGHT_OUTPUT_SEMANTIC_LABEL,
    ConfigurationBundle,
    ConfiguredConnector,
    ConfiguredPoint,
    ConfiguredStation,
    ExecutionLease,
    ResolvedRuntimeParameters,
    Unverified,
)

HOST_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f101")
STATION_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f103")
BACKEND_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f104")
GRANT_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f105")


class DeviceGateway:
    def __init__(self) -> None:
        self.minimum_revision: int | None = None
        self.confirmed: list[ConfigurationBundle] = []
        self.station_error: DeviceConfigurationError | None = None
        self.confirm_error: DeviceConfigurationError | None = None
        self.connectors: tuple[ConfiguredConnector, ...] = ()
        self.points: tuple[ConfiguredPoint, ...] = ()

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
            connectors=self.connectors,
            points=self.points,
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
    def __init__(self, *, disposition_policy: str = "stop") -> None:
        self.disposition_policy = disposition_policy

    def for_station(self, station_id: UUID) -> TemplateConfigurationProjection:
        assert station_id == STATION_ID
        return TemplateConfigurationProjection(
            template=None,
            runtime_defaults=ResolvedRuntimeParameters(
                idle_timeout_seconds=10.0,
                step_deadline_seconds=2.0,
                disposition_policy=self.disposition_policy,
            ),
            revision=9,
        )


class ExecutionGateway:
    def __init__(self) -> None:
        self.host_id: UUID | None = None
        self.now: datetime | None = None
        self.request_id: UUID | None = None

    def acquire(
        self,
        *,
        station_id: UUID,
        holder_host_id: UUID,
        request_id: UUID,
        now: datetime,
    ) -> StationGrant:
        raise AssertionError("host configuration pull must not acquire leases")

    def renew(
        self,
        *,
        station_id: UUID,
        grant_id: UUID,
        holder_host_id: UUID,
        request_id: UUID,
        now: datetime,
    ) -> StationGrant:
        raise AssertionError("host configuration pull must not renew a single lease")

    def renew_host_leases(
        self, *, host_id: UUID, now: datetime, request_id: UUID
    ) -> tuple[StationGrant, ...]:
        self.host_id = host_id
        self.now = now
        self.request_id = request_id
        return (
            StationGrant(
                grant_id=GRANT_ID,
                station_id=STATION_ID,
                holder_host_id=HOST_ID,
                lease_expires_at=now + timedelta(days=7),
                renewed_at=now,
                request_id=request_id,
            ),
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
    assert bundle.required_capabilities == (
        DISPOSITION_STOP_OUTPUT_CAPABILITY,
        EXECUTION_LEASE_WRITE_GATE_CAPABILITY,
    )


def test_record_configuration_does_not_require_stop_output_capability() -> None:
    bundle = configuration_for_host(
        host_id=HOST_ID,
        generated_at=datetime(2026, 9, 13, tzinfo=UTC),
        device=DeviceGateway(),
        templates=TemplateGateway(disposition_policy="record"),
    )

    assert bundle.required_capabilities == (EXECUTION_LEASE_WRITE_GATE_CAPABILITY,)


def test_sound_light_output_declares_its_behavior_capability() -> None:
    device = DeviceGateway()
    device.connectors = (
        ConfiguredConnector(
            connector_id="connector-a",
            name="I/O",
            connector_type="hikvision_isapi",
            revision=1,
            address="camera.example",
            port=80,
            capability=Unverified(),
        ),
    )
    device.points = (
        ConfiguredPoint(
            point_id="sound-light",
            name=SOUND_LIGHT_OUTPUT_SEMANTIC_LABEL,
            direction="output",
            connector_id="connector-a",
            role="output",
            address="3",
        ),
    )

    bundle = configuration_for_host(
        host_id=HOST_ID,
        generated_at=datetime(2026, 9, 13, tzinfo=UTC),
        device=device,
        templates=TemplateGateway(disposition_policy="record"),
    )

    assert bundle.required_capabilities == (
        DISPOSITION_SOUND_LIGHT_OUTPUT_CAPABILITY,
        EXECUTION_LEASE_WRITE_GATE_CAPABILITY,
    )


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


def test_host_configuration_pull_renews_only_the_hosts_leases() -> None:
    device = DeviceGateway()
    execution = ExecutionGateway()
    now = datetime(2026, 9, 13, tzinfo=UTC)

    bundle = host_configuration_pull(
        host_id=HOST_ID,
        now=now,
        device=device,
        templates=TemplateGateway(),
        execution=execution,
    )

    assert execution.host_id == HOST_ID
    assert execution.now == now
    assert execution.request_id is not None
    assert bundle.execution_grants == (
        ExecutionLease(
            station_id=str(STATION_ID),
            grant_id=str(GRANT_ID),
            holder_host_id=str(HOST_ID),
            lease_expires_at=(now + timedelta(days=7)).isoformat().replace("+00:00", "Z"),
        ),
    )
    without_grants = configuration_for_host(
        host_id=HOST_ID,
        generated_at=now,
        device=DeviceGateway(),
        templates=TemplateGateway(),
    )
    assert bundle.effective_sha256 == without_grants.effective_sha256
    assert bundle.stable_content_wire() == without_grants.stable_content_wire()
