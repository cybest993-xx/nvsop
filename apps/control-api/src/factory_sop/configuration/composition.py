"""机器配置 transport 对真实 owner 投影的纯组合。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from factory_sop.device.api import DeviceConfigurationError, DeviceConfigurationGateway
from factory_sop.execution.api import ExecutionLeaseGateway, StationGrant
from factory_sop.identifiers import new_id
from factory_sop.template.api import (
    TemplateConfigurationError,
    TemplateConfigurationGateway,
)
from nvsop_contracts import (
    DISPOSITION_POLICY_STOP,
    DISPOSITION_STOP_OUTPUT_CAPABILITY,
    EXECUTION_LEASE_WRITE_GATE_CAPABILITY,
    ConfigurationBundle,
    ExecutionLease,
)


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

        required_capabilities = {EXECUTION_LEASE_WRITE_GATE_CAPABILITY}
        if any(
            station.runtime_parameters.disposition_policy == DISPOSITION_POLICY_STOP
            for station in stations
        ):
            required_capabilities.add(DISPOSITION_STOP_OUTPUT_CAPABILITY)

        candidate = ConfigurationBundle(
            host_id=str(topology.host_id),
            config_revision=1,
            generated_at=generated_at.isoformat().replace("+00:00", "Z"),
            stations=tuple(sorted(stations, key=lambda item: (item.station_id, item.backend_id))),
            # 物理写入门禁是行为扩展: 声明能力门禁, 不支持该能力的旧 Edge 显式拒绝整个候选,
            # 而不是忽略 execution_grants 继续无门禁写入 (machine-contract-evolution.md)。
            required_capabilities=tuple(sorted(required_capabilities)),
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


def host_configuration_pull(
    *,
    host_id: UUID,
    now: datetime,
    device: DeviceConfigurationGateway,
    templates: TemplateConfigurationGateway,
    execution: ExecutionLeaseGateway,
) -> ConfigurationBundle:
    """主机成功拉取配置的边界：组装主机切片并在同一请求内续期其执行权租约。

    续期只作用于认证主机自己持有的租约；租约随同一信封下发，但不进入配置稳定内容或运行
    语义身份。组装失败时组合抛错，请求事务回滚，不留下未随成功响应返回的续期。
    """
    bundle = configuration_for_host(
        host_id=host_id,
        generated_at=now,
        device=device,
        templates=templates,
    )
    leases = execution.renew_host_leases(
        host_id=host_id,
        now=now,
        request_id=new_id(),
    )
    return replace(
        bundle,
        execution_grants=tuple(_execution_lease(lease) for lease in leases),
    )


def _execution_lease(grant: StationGrant) -> ExecutionLease:
    return ExecutionLease(
        station_id=str(grant.station_id),
        grant_id=str(grant.grant_id),
        holder_host_id=str(grant.holder_host_id),
        lease_expires_at=grant.lease_expires_at.isoformat().replace("+00:00", "Z"),
    )


__all__ = [
    "ConfigurationAssemblyError",
    "configuration_for_host",
    "host_configuration_pull",
    "register_confirmed_configuration",
]
