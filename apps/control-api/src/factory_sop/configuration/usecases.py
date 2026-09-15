"""组装一台推理机专属且不含凭据的配置 bundle。"""

from __future__ import annotations

from datetime import UTC, datetime
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
    InferenceHostRepository,
    PointRepository,
    StationRepository,
)
from factory_sop.template.model import TemplateVersion, TemplateVersionArtifact
from factory_sop.template.repository import TemplateRepository
from nvsop_contracts import (
    ConfigurationArtifact,
    ConfigurationBundle,
    ConfigurationTemplate,
    ConfiguredConnector,
    ConfiguredPoint,
    ConfiguredStation,
    ResolvedRuntimeParameters,
)


class ConfigurationAssemblyError(ValueError):
    """中心无法为该推理机构造完整且安全的 bundle。"""


def configuration_for_host(
    *,
    host_id: UUID,
    generated_at: datetime,
    hosts: InferenceHostRepository,
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
    station_ids = {camera.station_id for camera in host_cameras}
    station_values: list[ConfiguredStation] = []
    revision_values = [host.revision]
    for backend in backend_values:
        if backend.status.value != "active":
            continue
        backend_cameras = [
            camera
            for camera in host_cameras
            if camera.backend_id == backend.id and camera.station_id in station_ids
        ]
        for station_id in sorted({camera.station_id for camera in backend_cameras}, key=str):
            station = stations.by_id(station_id)
            if station is None or station.status.value != "active":
                continue
            station_bundle = _station_bundle(
                host=host,
                backend=backend,
                station=station,
                backend_cameras=backend_cameras,
                connectors=connectors,
                points=points,
                templates=templates,
            )
            station_values.append(station_bundle)
            revision_values.extend(
                [
                    backend.revision,
                    station.revision,
                    station.runtime_parameters_revision,
                    station_bundle.revision,
                    *(camera.revision for camera in backend_cameras),
                ]
            )

    return ConfigurationBundle(
        host_id=str(host.id),
        config_revision=max(revision_values),
        generated_at=generated_at.isoformat().replace("+00:00", "Z"),
        stations=tuple(sorted(station_values, key=lambda item: (item.station_id, item.backend_id))),
    )


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
    del host  # device repository 查询已经完成归属校验
    binding = templates.binding_by_station(station.id)
    template: ConfigurationTemplate | None = None
    defaults: StationRuntimeParameters | None = None
    configuration_revision = station.revision
    if binding is not None:
        version = templates.version_by_id(binding.desired_version_id)
        if version is None:
            raise ConfigurationAssemblyError("station binding refers to a missing template version")
        if version.sha256 != binding.desired_sha256:
            raise ConfigurationAssemblyError("station binding digest does not match its version")
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

    station_connectors = {
        connector.id: connector
        for connector in connectors.for_station(station.id)
        if connector.host_id == backend.host_id and connector.status.value == "active"
    }
    station_points = [
        point
        for point in points.for_station(station.id)
        if point.connector_id in station_connectors and point.status.value == "active"
    ]
    for camera in backend_cameras:
        if camera.host_id != backend.host_id or camera.backend_id != backend.id:
            raise ConfigurationAssemblyError("host topology contains a foreign camera")
    configuration_revision = max(
        configuration_revision,
        station.runtime_parameters_revision,
        *(connector.revision for connector in station_connectors.values()),
        *(point.revision for point in station_points),
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
        model_ids=backend.self_reported_model_ids,
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
