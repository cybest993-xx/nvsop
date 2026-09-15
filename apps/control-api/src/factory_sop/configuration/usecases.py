"""组装一台推理机专属且不含凭据的配置 bundle。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from factory_sop.configuration.repository import (
    ArtifactSnapshot,
    BackendReader,
    BackendSnapshot,
    CameraReader,
    CameraSnapshot,
    ConnectorReader,
    ConnectorSnapshot,
    HostReader,
    PointReader,
    PointSnapshot,
    RuntimeParametersSnapshot,
    StationReader,
    StationSnapshot,
    TemplateReader,
    TemplateVersionSnapshot,
)
from factory_sop.pagination import all_pages
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


@dataclass(frozen=True, slots=True)
class _ResolvedParameters:
    """配置 bundle 所需的完整运行参数快照。"""

    idle_timeout_seconds: float
    step_deadline_seconds: float
    disposition_policy: str


def configuration_for_host(
    *,
    host_id: UUID,
    generated_at: datetime,
    hosts: HostReader,
    backends: BackendReader,
    stations: StationReader,
    cameras: CameraReader,
    connectors: ConnectorReader,
    points: PointReader,
    templates: TemplateReader,
) -> ConfigurationBundle:
    """只通过配置模块读取接缝组装该主机 bundle。

    调用方 contract：``host_id`` 必须来自已认证的推理机请求，``generated_at`` 必须为 UTC；
    reader 只返回各 owner 的 snapshot，usecase 负责校验归属、状态、模板 digest 和完整运行参数。
    """
    host = hosts.by_id(host_id)
    if host is None:
        raise ConfigurationAssemblyError("inference host was not found")
    if generated_at.tzinfo is None or generated_at.utcoffset() != UTC.utcoffset(generated_at):
        raise ValueError("configuration generated_at must be UTC")

    backend_values, _ = all_pages(
        lambda page, size: backends.page_of(page=page, page_size=size, host_id=host_id)
    )
    host_cameras = [
        camera for camera in cameras.for_host(host_id) if camera.status.value == "active"
    ]
    station_ids = {camera.station_id for camera in host_cameras}
    station_values: list[ConfiguredStation] = []
    revision_values = [host.revision]
    for backend in backend_values:
        if backend.host_id != host.id:
            raise ConfigurationAssemblyError("host topology contains a foreign backend")
        if backend.status.value != "active":
            continue
        for station_id in sorted(
            {
                camera.station_id
                for camera in host_cameras
                if camera.backend_id == backend.id and camera.station_id in station_ids
            },
            key=str,
        ):
            station = stations.by_id(station_id)
            if station is None or station.status.value != "active":
                continue
            backend_cameras = [
                camera
                for camera in host_cameras
                if camera.backend_id == backend.id and camera.station_id == station_id
            ]
            station_bundle = _station_bundle(
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
    backend: BackendSnapshot,
    station: StationSnapshot,
    backend_cameras: list[CameraSnapshot],
    connectors: ConnectorReader,
    points: PointReader,
    templates: TemplateReader,
) -> ConfiguredStation:
    binding = templates.binding_by_station(station.id)
    template: ConfigurationTemplate | None = None
    defaults: _ResolvedParameters | None = None
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
    effective = _effective_parameters(station, defaults)
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
    )


def _effective_parameters(
    station: StationSnapshot, defaults: _ResolvedParameters | None
) -> RuntimeParametersSnapshot | _ResolvedParameters | None:
    """按工位的整组模式选择模板默认值或工位覆盖组。"""
    if station.runtime_parameter_mode.value == "custom":
        return station.runtime_parameter_overrides
    return defaults


def _connector(value: ConnectorSnapshot) -> ConfiguredConnector:
    return ConfiguredConnector(
        connector_id=str(value.id),
        name=value.name,
        connector_type=value.connector_type.value,
        revision=value.revision,
        address=value.configuration.address,
        port=value.configuration.port,
        capability=value.capability,
    )


def _point(value: PointSnapshot) -> ConfiguredPoint:
    return ConfiguredPoint(
        point_id=str(value.id),
        name=value.semantic_label,
        direction=value.direction.value,
        connector_id=str(value.connector_id),
        role=value.direction.value,
        address=value.identifier,
    )


def _template(version: TemplateVersionSnapshot) -> ConfigurationTemplate:
    return ConfigurationTemplate(
        version_id=str(version.id),
        version_sha256=version.sha256,
        artifacts=tuple(_artifact(artifact) for artifact in version.artifacts),
    )


def _artifact(value: ArtifactSnapshot) -> ConfigurationArtifact:
    return ConfigurationArtifact(
        name=value.name.value,
        media_type=value.media_type,
        content=value.content,
        sha256=value.sha256,
    )


def _runtime_defaults(version: TemplateVersionSnapshot) -> _ResolvedParameters:
    defaults = version.runtime_defaults
    idle_timeout = defaults.idle_timeout_seconds
    step_deadline = defaults.step_deadline_seconds
    disposition_policy = defaults.disposition_policy
    if idle_timeout is None or step_deadline is None or disposition_policy is None:
        raise ConfigurationAssemblyError("published template has incomplete runtime defaults")
    return _ResolvedParameters(
        idle_timeout_seconds=idle_timeout,
        step_deadline_seconds=step_deadline,
        disposition_policy=disposition_policy,
    )


__all__ = ["ConfigurationAssemblyError", "configuration_for_host"]
