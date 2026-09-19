"""device 拥有的机器配置拓扑、运行参数和签发历史。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Protocol
from uuid import UUID

from factory_sop.device.api import (
    DeviceConfigurationError,
    DeviceConfigurationGateway,
    DeviceConfigurationTarget,
    DeviceConfigurationTopology,
)
from factory_sop.device.model import InferenceHostIdentity, StationRuntimeParameters
from factory_sop.device.repository import (
    CameraRepository,
    ConnectorRepository,
    InferenceBackendRepository,
    InferenceHostRepository,
    PointRepository,
    StationRepository,
)
from factory_sop.device.usecases.commands import authenticate_command_host
from nvsop_contracts import (
    ConfigurationBundle,
    ConfiguredCamera,
    ConfiguredConnector,
    ConfiguredPoint,
    ConfiguredStation,
    ResolvedRuntimeParameters,
)


class ConfigurationHostRepository(InferenceHostRepository, Protocol):
    def next_configuration_revision(
        self, *, host_id: UUID, content_sha256: str, minimum_revision: int = 0
    ) -> int: ...

    def configuration_was_issued(
        self,
        *,
        host_id: UUID,
        configuration_revision: int,
        configuration_sha256: str,
    ) -> bool: ...

    def record_configuration_assignments(self, bundle: ConfigurationBundle) -> None: ...


class RepositoryDeviceConfigurationGateway(DeviceConfigurationGateway):
    """在 request UoW 内读取并维护 device 配置事实。"""

    def __init__(
        self,
        *,
        hosts: ConfigurationHostRepository,
        backends: InferenceBackendRepository,
        stations: StationRepository,
        cameras: CameraRepository,
        connectors: ConnectorRepository,
        points: PointRepository,
    ) -> None:
        self._hosts = hosts
        self._backends = backends
        self._stations = stations
        self._cameras = cameras
        self._connectors = connectors
        self._points = points

    def authenticate(self, *, host: InferenceHostIdentity, now: datetime) -> None:
        authenticate_command_host(host=host, now=now, hosts=self._hosts)

    def topology_for_host(self, host_id: UUID) -> DeviceConfigurationTopology:
        host = self._hosts.by_id(host_id)
        if host is None:
            raise DeviceConfigurationError("inference host was not found")
        backend_values, _ = self._backends.page_of(page=1, page_size=10_000, host_id=host_id)
        active_backends = []
        for backend in backend_values:
            if backend.status.value != "active":
                continue
            if backend.host_id != host.id:
                raise DeviceConfigurationError("host topology contains a foreign inference backend")
            active_backends.append(backend)
        active_backend_ids = {backend.id for backend in active_backends}
        targets: set[DeviceConfigurationTarget] = set()
        for camera in self._cameras.for_host(host_id):
            if camera.status.value != "active" or camera.backend_id not in active_backend_ids:
                continue
            station = self._stations.by_id(camera.station_id)
            if station is None or station.status.value != "active":
                continue
            targets.add(
                DeviceConfigurationTarget(
                    station_id=camera.station_id,
                    backend_id=camera.backend_id,
                )
            )
        return DeviceConfigurationTopology(
            host_id=host.id,
            host_revision=host.revision,
            backend_revisions=tuple(backend.revision for backend in active_backends),
            targets=tuple(
                sorted(targets, key=lambda item: (str(item.station_id), str(item.backend_id)))
            ),
        )

    def station_configuration(
        self,
        *,
        host_id: UUID,
        target: DeviceConfigurationTarget,
        runtime_defaults: ResolvedRuntimeParameters | None,
    ) -> ConfiguredStation:
        host = self._hosts.by_id(host_id)
        backend = self._backends.by_id(target.backend_id)
        station = self._stations.by_id(target.station_id)
        if host is None:
            raise DeviceConfigurationError("inference host was not found")
        if backend is None or backend.status.value != "active" or backend.host_id != host.id:
            raise DeviceConfigurationError("host topology contains a foreign inference backend")
        if station is None or station.status.value != "active":
            raise DeviceConfigurationError("configuration target refers to an inactive station")

        defaults = (
            None
            if runtime_defaults is None
            else StationRuntimeParameters(
                idle_timeout_seconds=runtime_defaults.idle_timeout_seconds,
                step_deadline_seconds=runtime_defaults.step_deadline_seconds,
                disposition_policy=runtime_defaults.disposition_policy,
            )
        )
        effective = station.runtime_parameters_for(defaults)
        if effective is None:
            raise DeviceConfigurationError("station has no complete resolved runtime parameters")

        raw_connectors = self._connectors.for_station(station.id)
        for connector in raw_connectors:
            if connector.station_id != station.id:
                raise DeviceConfigurationError("host topology contains a foreign connector")
        station_connectors = {
            connector.id: connector
            for connector in raw_connectors
            if connector.host_id == host.id and connector.status.value == "active"
        }
        raw_points = self._points.for_station(station.id)
        for point in raw_points:
            if point.station_id != station.id:
                raise DeviceConfigurationError("host topology contains a foreign point")
        station_points = [
            point
            for point in raw_points
            if point.connector_id in station_connectors and point.status.value == "active"
        ]
        backend_cameras = [
            camera
            for camera in self._cameras.for_host(host_id)
            if camera.status.value == "active"
            and camera.backend_id == backend.id
            and camera.station_id == station.id
        ]
        for camera in backend_cameras:
            if (
                camera.host_id != host.id
                or camera.backend_id != backend.id
                or camera.station_id != station.id
            ):
                raise DeviceConfigurationError("host topology contains a foreign camera")

        revision = max(
            station.revision,
            station.runtime_parameters_revision,
            *(connector.revision for connector in station_connectors.values()),
            *(point.revision for point in station_points),
            *(camera.revision for camera in backend_cameras),
        )
        return ConfiguredStation(
            station_id=str(station.id),
            backend_id=str(backend.id),
            code=station.code,
            name=station.name,
            revision=revision,
            runtime_parameters=ResolvedRuntimeParameters(
                idle_timeout_seconds=effective.idle_timeout_seconds,
                step_deadline_seconds=effective.step_deadline_seconds,
                disposition_policy=effective.disposition_policy,
            ),
            connectors=tuple(
                sorted(
                    (
                        ConfiguredConnector(
                            connector_id=str(value.id),
                            name=value.name,
                            connector_type=value.connector_type.value,
                            revision=value.revision,
                            address=value.configuration.address,
                            port=value.configuration.port,
                            capability=value.capability,
                        )
                        for value in station_connectors.values()
                    ),
                    key=lambda item: item.connector_id,
                )
            ),
            points=tuple(
                sorted(
                    (
                        ConfiguredPoint(
                            point_id=str(value.id),
                            name=value.semantic_label,
                            direction=value.direction.value,
                            connector_id=str(value.connector_id),
                            role=value.direction.value,
                            address=value.identifier,
                        )
                        for value in station_points
                    ),
                    key=lambda item: item.point_id,
                )
            ),
            template=None,
            cameras=tuple(
                sorted(
                    (
                        ConfiguredCamera(
                            camera_id=str(value.id),
                            name=value.name,
                            address=value.address,
                            main_stream_path=value.main_stream_path,
                            sub_stream_path=value.sub_stream_path,
                            credentials_configured=value.credentials_configured,
                            revision=value.revision,
                            media_path_mode=value.media_path_mode.value,
                            recording_mode=value.recording_mode.value,
                        )
                        for value in backend_cameras
                    ),
                    key=lambda item: item.camera_id,
                )
            ),
            model_ids=backend.self_reported_model_ids,
        )

    def finalize_configuration(
        self, candidate: ConfigurationBundle, *, minimum_revision: int
    ) -> ConfigurationBundle:
        revision = self._hosts.next_configuration_revision(
            host_id=UUID(candidate.host_id),
            content_sha256=candidate.effective_sha256,
            minimum_revision=minimum_revision,
        )
        bundle = replace(candidate, config_revision=revision)
        self._hosts.record_configuration_assignments(bundle)
        return bundle

    def confirm_configuration(self, *, host_id: UUID, bundle: ConfigurationBundle) -> None:
        if bundle.host_id != str(host_id):
            raise DeviceConfigurationError("confirmed configuration belongs to a different host")
        if not self._hosts.configuration_was_issued(
            host_id=host_id,
            configuration_revision=bundle.config_revision,
            configuration_sha256=bundle.effective_sha256,
        ):
            raise DeviceConfigurationError("confirmed configuration was not issued by this Center")
        self._hosts.record_configuration_assignments(bundle)


__all__ = ["RepositoryDeviceConfigurationGateway"]
