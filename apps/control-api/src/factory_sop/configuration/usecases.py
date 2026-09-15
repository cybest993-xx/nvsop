"""组装一台推理机专属且不含凭据的配置 bundle。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from factory_sop.device.model import (
    Camera,
    Connector,
    InferenceBackend,
    InferenceHost,
    Point,
    Station,
    StationRuntimeParameters,
)
from factory_sop.device.repository import (
    CameraRepository,
    ConnectorRepository,
    InferenceBackendRepository,
    PointRepository,
    StationRepository,
)
from factory_sop.template.model import TemplateVersion, TemplateVersionArtifact
from factory_sop.template.repository import TemplateRepository
from nvsop_contracts import (
    ConfigurationArtifact,
    ConfigurationBundle,
    ConfigurationTemplate,
    ConfiguredCamera,
    ConfiguredConnector,
    ConfiguredPoint,
    ConfiguredStation,
    ResolvedRuntimeParameters,
)


class ConfigurationAssemblyError(ValueError):
    """中心无法为该推理机构造完整且安全的 bundle。"""


class ConfigurationHostRepository(Protocol):
    """配置组装所需的主机最小接缝。"""

    def by_id(self, host_id: UUID) -> InferenceHost | None: ...

    def next_configuration_revision(
        self, *, host_id: UUID, content_sha256: str, minimum_revision: int = 0
    ) -> int: ...


def configuration_for_host(
    *,
    host_id: UUID,
    generated_at: datetime,
    hosts: ConfigurationHostRepository,
    backends: InferenceBackendRepository,
    stations: StationRepository,
    cameras: CameraRepository,
    connectors: ConnectorRepository,
    points: PointRepository,
    templates: TemplateRepository,
) -> ConfigurationBundle:
    """只通过模块 repository 读取，并准确返回该主机的拓扑。"""
    host = hosts.by_id(host_id)
    if host is None:
        raise ConfigurationAssemblyError("inference host was not found")
    if generated_at.tzinfo is None or generated_at.utcoffset() != UTC.utcoffset(generated_at):
        raise ValueError("configuration generated_at must be UTC")

    backend_values, _ = backends.page_of(page=1, page_size=10_000, host_id=host_id)
    host_cameras = [
        camera for camera in cameras.for_host(host_id) if camera.status.value == "active"
    ]
    station_values: list[ConfiguredStation] = []
    for backend in backend_values:
        if backend.status.value != "active":
            continue
        if backend.host_id != host.id:
            raise ConfigurationAssemblyError("host topology contains a foreign inference backend")
        backend_cameras = [camera for camera in host_cameras if camera.backend_id == backend.id]
        for station_id in sorted({camera.station_id for camera in backend_cameras}, key=str):
            station = stations.by_id(station_id)
            if station is None or station.status.value != "active":
                continue
            station_cameras = [
                camera for camera in backend_cameras if camera.station_id == station.id
            ]
            station_bundle = _station_bundle(
                host=host,
                backend=backend,
                station=station,
                backend_cameras=station_cameras,
                connectors=connectors,
                points=points,
                templates=templates,
            )
            station_values.append(station_bundle)

    candidate = ConfigurationBundle(
        host_id=str(host.id),
        config_revision=1,
        generated_at=generated_at.isoformat().replace("+00:00", "Z"),
        stations=tuple(sorted(station_values, key=lambda item: (item.station_id, item.backend_id))),
    )
    legacy_revision_floor = max(
        [
            host.revision,
            *(backend.revision for backend in backend_values if backend.status.value == "active"),
            *(station.revision for station in station_values),
            0,
        ]
    )
    config_revision = hosts.next_configuration_revision(
        host_id=host.id,
        content_sha256=candidate.effective_sha256,
        minimum_revision=legacy_revision_floor,
    )
    return replace(candidate, config_revision=config_revision)


def _station_bundle(
    *,
    host: InferenceHost,
    backend: InferenceBackend,
    station: Station,
    backend_cameras: list[Camera],
    connectors: ConnectorRepository,
    points: PointRepository,
    templates: TemplateRepository,
) -> ConfiguredStation:
    binding = templates.binding_by_station(station.id)
    template: ConfigurationTemplate | None = None
    defaults: StationRuntimeParameters | None = None
    configuration_revision = station.revision
    if binding is not None:
        version = templates.version_by_id(binding.desired_version_id)
        if version is None:
            raise ConfigurationAssemblyError("station binding refers to a missing template version")
        template_owner = templates.template_by_id(version.template_id)
        if template_owner is None or template_owner.station_id != station.id:
            raise ConfigurationAssemblyError("station binding refers to a foreign template version")
        template = _template(version)
        defaults = _runtime_defaults(version)
        configuration_revision = max(
            configuration_revision,
            binding.desired_config_revision,
            version.source_draft_revision,
        )
    effective = station.runtime_parameters_for(defaults)
    if effective is None:
        raise ConfigurationAssemblyError("station has no complete resolved runtime parameters")

    raw_connectors = connectors.for_station(station.id)
    for connector in raw_connectors:
        if connector.station_id != station.id:
            raise ConfigurationAssemblyError("host topology contains a foreign connector")
    station_connectors = {
        connector.id: connector
        for connector in raw_connectors
        if connector.host_id == host.id and connector.status.value == "active"
    }
    raw_points = points.for_station(station.id)
    for point in raw_points:
        if point.station_id != station.id:
            raise ConfigurationAssemblyError("host topology contains a foreign point")
    station_points = [
        point
        for point in raw_points
        if point.connector_id in station_connectors and point.status.value == "active"
    ]
    for camera in backend_cameras:
        if (
            camera.host_id != host.id
            or camera.backend_id != backend.id
            or camera.station_id != station.id
        ):
            raise ConfigurationAssemblyError("host topology contains a foreign camera")
    configuration_revision = max(
        configuration_revision,
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
        revision=configuration_revision,
        runtime_parameters=ResolvedRuntimeParameters(
            idle_timeout_seconds=effective.idle_timeout_seconds,
            step_deadline_seconds=effective.step_deadline_seconds,
            disposition_policy=effective.disposition_policy,
        ),
        connectors=tuple(
            sorted(
                (_connector(value) for value in station_connectors.values()),
                key=lambda item: item.connector_id,
            )
        ),
        points=tuple(
            sorted((_point(value) for value in station_points), key=lambda item: item.point_id)
        ),
        template=template,
        cameras=tuple(
            sorted((_camera(value) for value in backend_cameras), key=lambda item: item.camera_id)
        ),
    )


def _connector(value: Connector) -> ConfiguredConnector:
    return ConfiguredConnector(
        connector_id=str(value.id),
        name=value.name,
        connector_type=value.connector_type.value,
        revision=value.revision,
        address=value.configuration.address,
        port=value.configuration.port,
        capability=value.capability,
    )


def _camera(value: Camera) -> ConfiguredCamera:
    return ConfiguredCamera(
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


def _point(value: Point) -> ConfiguredPoint:
    return ConfiguredPoint(
        point_id=str(value.id),
        name=value.semantic_label,
        direction=value.direction.value,
        connector_id=str(value.connector_id),
        role=value.direction.value,
        address=value.identifier,
    )


def _template(version: TemplateVersion) -> ConfigurationTemplate:
    return ConfigurationTemplate(
        version_id=str(version.id),
        version_sha256=version.sha256,
        artifacts=tuple(_artifact(artifact) for artifact in version.artifacts),
    )


def _artifact(value: TemplateVersionArtifact) -> ConfigurationArtifact:
    return ConfigurationArtifact(
        name=value.name.value,
        media_type=value.media_type,
        content=value.content,
        sha256=value.sha256,
    )


def _runtime_defaults(version: TemplateVersion) -> StationRuntimeParameters:
    defaults = version.runtime_defaults
    idle_timeout = defaults.idle_timeout_seconds
    step_deadline = defaults.step_deadline_seconds
    disposition_policy = defaults.disposition_policy
    if idle_timeout is None or step_deadline is None or disposition_policy is None:
        raise ConfigurationAssemblyError("published template has incomplete runtime defaults")
    return StationRuntimeParameters(
        idle_timeout_seconds=idle_timeout,
        step_deadline_seconds=step_deadline,
        disposition_policy=disposition_policy,
    )


__all__ = ["ConfigurationAssemblyError", "configuration_for_host"]
