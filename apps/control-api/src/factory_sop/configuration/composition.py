"""机器配置 transport 对真实 owner 投影的纯组合。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from factory_sop.device.api import DeviceConfigurationError, DeviceConfigurationGateway
from factory_sop.template.api import (
    TemplateConfigurationError,
    TemplateConfigurationGateway,
)
from nvsop_contracts import ConfigurationBundle


class ConfigurationAssemblyError(ValueError):
    """配置组合无法从 owner 投影构造完整 bundle。"""


def register_confirmed_configuration(
    *,
    host_id: UUID,
    bundle: ConfigurationBundle,
    device: DeviceConfigurationGateway,
) -> None:
    """把已确认 bundle 交回 device owner 校验签发证明并记录历史。"""
    try:
        device.confirm_configuration(host_id=host_id, bundle=bundle)
    except DeviceConfigurationError as error:
        raise ConfigurationAssemblyError(str(error)) from error


def configuration_for_host(
    *,
    host_id: UUID,
    generated_at: datetime,
    device: DeviceConfigurationGateway,
    templates: TemplateConfigurationGateway,
) -> ConfigurationBundle:
    """只组合 device/template owner 提供的公开最小投影。"""
    if generated_at.tzinfo is None or generated_at.utcoffset() != UTC.utcoffset(generated_at):
        raise ValueError("configuration generated_at must be UTC")

    try:
        topology = device.topology_for_host(host_id)
        stations = []
        for target in topology.targets:
            template = templates.for_station(target.station_id)
            station = device.station_configuration(
                host_id=host_id,
                target=target,
                runtime_defaults=template.runtime_defaults,
            )
            stations.append(
                replace(
                    station,
                    revision=max(station.revision, template.revision),
                    template=template.template,
                )
            )

        candidate = ConfigurationBundle(
            host_id=str(topology.host_id),
            config_revision=1,
            generated_at=generated_at.isoformat().replace("+00:00", "Z"),
            stations=tuple(sorted(stations, key=lambda item: (item.station_id, item.backend_id))),
        )
        revision_floor = max(
            topology.host_revision,
            *topology.backend_revisions,
            *(station.revision for station in stations),
            0,
        )
        return device.finalize_configuration(candidate, minimum_revision=revision_floor)
    except (DeviceConfigurationError, TemplateConfigurationError) as error:
        raise ConfigurationAssemblyError(str(error)) from error


__all__ = [
    "ConfigurationAssemblyError",
    "configuration_for_host",
    "register_confirmed_configuration",
]
