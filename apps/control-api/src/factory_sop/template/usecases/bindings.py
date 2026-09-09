"""模板版本绑定、工位运行参数和推理机确认对账用例。

本模块拥有模板绑定与 desired/reported 事实；设备拓扑和主机认证只能通过
``device.api`` 的窄 seam 访问。所有仓储方法都运行在请求级事务中，不在这里提交。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.device.api import (
    BindingSignal,
    BindingSignalKind,
    BindingValidationIssue,
    DeviceHostGateway,
    DeviceTemplateBindingGateway,
    TemplateBindingSpecification,
)
from factory_sop.device.model import (
    InferenceHostIdentity,
    RuntimeParameterMode,
    StationRuntimeConfiguration,
    StationRuntimeParameters,
)
from factory_sop.identifiers import new_id
from factory_sop.observability import get_logger
from factory_sop.template.errors import (
    TemplateFieldError,
    TemplateRefusalCode,
    TemplateRefusedError,
)
from factory_sop.template.model import (
    TemplateBindingStatus,
    TemplateConfigurationReport,
    TemplateReportRejectionCode,
    TemplateRuntimeDefaults,
    TemplateStationBinding,
    TemplateVersion,
)
from factory_sop.template.repository import TemplateBindingRepository

_logger = get_logger("template")


@dataclass(frozen=True, slots=True)
class TemplateBindingPreview:
    """绑定预校验的完整结果；它不是正式绑定凭证。"""

    version: TemplateVersion
    runtime: StationRuntimeConfiguration
    requested_mode: RuntimeParameterMode
    requested_overrides: StationRuntimeParameters | None
    validation_issues: tuple[BindingValidationIssue, ...]
    accepted: bool


@dataclass(frozen=True, slots=True)
class BackendConfigurationStatus:
    """一个参与后端的 reported 事实及其相对 desired 的状态。"""

    backend_id: UUID
    host_id: UUID | None
    status: TemplateBindingStatus
    reported_version_id: UUID | None
    reported_sha256: str | None
    reported_config_revision: int | None
    reported_at: datetime | None
    rejection_code: str | None
    rejection_detail: str | None
    rejection_at: datetime | None


@dataclass(frozen=True, slots=True)
class StationTemplateConfiguration:
    """工位绑定、解析后的运行参数和逐后端对账读取结果。"""

    station_id: UUID
    station_revision: int
    runtime: StationRuntimeConfiguration
    binding: TemplateStationBinding | None
    version: TemplateVersion | None
    status: TemplateBindingStatus
    status_detail: str | None
    topology_issues: tuple[BindingValidationIssue, ...]
    backends: tuple[BackendConfigurationStatus, ...]


@dataclass(frozen=True, slots=True)
class TemplateConfigurationReportResult:
    """主机上报的结果；语义拒绝已写入最近拒绝事实。"""

    accepted: bool
    report: TemplateConfigurationReport | None
    rejection_code: TemplateReportRejectionCode | None = None
    rejection_detail: str | None = None


def preview_template_binding(
    *,
    station_id: UUID,
    version_id: UUID,
    runtime_mode: RuntimeParameterMode | None,
    runtime_overrides: StationRuntimeParameters | None,
    caller: Caller,
    now: datetime,
    templates: TemplateBindingRepository,
    device: DeviceTemplateBindingGateway,
) -> TemplateBindingPreview:
    """只读校验当前拓扑；提交时必须重新调用设备网关。"""
    del now
    authorize(caller, Permission.STATION_VIEW)
    version = _version_for_station(
        station_id=station_id,
        version_id=version_id,
        templates=templates,
    )
    current = device.read_station_runtime_parameters(
        station_id,
        defaults=_device_runtime_defaults(version.runtime_defaults),
    )
    mode, overrides = _requested_runtime(
        current=current,
        mode=runtime_mode,
        overrides=runtime_overrides,
    )
    specification = _specification(
        station_id=station_id,
        version=version,
        runtime_mode=mode,
        runtime_overrides=overrides,
        actor_id=caller.user.id,
    )
    validation = device.validate_template_binding(specification)
    return TemplateBindingPreview(
        version=version,
        runtime=current,
        requested_mode=mode,
        requested_overrides=overrides,
        validation_issues=validation.reasons,
        accepted=validation.accepted,
    )


def bind_template_version(
    *,
    station_id: UUID,
    version_id: UUID,
    runtime_mode: RuntimeParameterMode | None,
    runtime_overrides: StationRuntimeParameters | None,
    expected_station_revision: int,
    caller: Caller,
    now: datetime,
    templates: TemplateBindingRepository,
    device: DeviceTemplateBindingGateway,
) -> StationTemplateConfiguration:
    """正式绑定一个不可变版本，并在同一事务中更新 device 拓扑槽位。"""
    authorize(caller, Permission.STATION_EDIT)
    version = _version_for_station(
        station_id=station_id,
        version_id=version_id,
        templates=templates,
    )
    current = device.read_station_runtime_parameters(
        station_id,
        defaults=_device_runtime_defaults(version.runtime_defaults),
    )
    mode, overrides = _requested_runtime(
        current=current,
        mode=runtime_mode,
        overrides=runtime_overrides,
    )
    specification = _specification(
        station_id=station_id,
        version=version,
        runtime_mode=mode,
        runtime_overrides=overrides,
        actor_id=caller.user.id,
    )

    # 这是预检和正式提交之间的第一次复核；`apply_template_binding` 还会再读一次。
    validation = device.validate_template_binding(specification)
    if not validation.accepted:
        _raise_binding_invalid(validation.reasons)
    if validation.station_revision != expected_station_revision:
        _raise_stale_revision(
            station_id=station_id,
            expected=expected_station_revision,
            actual=validation.station_revision,
        )

    existing = templates.binding_by_station(station_id)
    if (
        existing is not None
        and existing.desired_version_id == version.id
        and existing.desired_sha256 == version.sha256
        and current.mode is mode
        and current.overrides == overrides
    ):
        # 相同期望配置的重试不推进 revision，也不重新写后端。
        return _read_configuration(
            station_id=station_id,
            caller=caller,
            templates=templates,
            device=device,
            binding=existing,
            version=version,
            runtime=current,
            topology_issues=validation.reasons,
        )

    applied = device.apply_template_binding(
        specification,
        expected_station_revision=expected_station_revision,
        now=now,
    )
    if not applied.accepted or applied.runtime_parameters_revision is None:
        _raise_binding_invalid(applied.reasons)
    binding = TemplateStationBinding(
        id=existing.id if existing is not None else new_id(),
        station_id=station_id,
        desired_version_id=version.id,
        desired_sha256=version.sha256,
        desired_config_revision=applied.runtime_parameters_revision,
        revision=existing.revision + 1 if existing is not None else 1,
        created_by=existing.created_by if existing is not None else caller.user.id,
        updated_by=caller.user.id,
        created_at=existing.created_at if existing is not None else now,
        updated_at=now,
    )
    templates.save_binding(
        binding,
        expected_revision=existing.revision if existing is not None else None,
    )
    runtime = device.read_station_runtime_parameters(
        station_id,
        defaults=_device_runtime_defaults(version.runtime_defaults),
    )
    _logger.info(
        "template.station_binding.succeeded",
        station_id=str(station_id),
        version_id=str(version.id),
        sha256=version.sha256,
        config_revision=str(binding.desired_config_revision),
        actor_id=str(caller.user.id),
    )
    return _read_configuration(
        station_id=station_id,
        caller=caller,
        templates=templates,
        device=device,
        binding=binding,
        version=version,
        runtime=runtime,
        topology_issues=(),
    )


def read_station_configuration(
    *,
    station_id: UUID,
    caller: Caller,
    templates: TemplateBindingRepository,
    device: DeviceTemplateBindingGateway,
) -> StationTemplateConfiguration:
    """读取 desired、解析参数和每个参与后端的 reported 对账状态。"""
    authorize(caller, Permission.STATION_VIEW)
    binding = templates.binding_by_station(station_id)
    if binding is None:
        runtime = device.read_station_runtime_parameters(station_id, defaults=None)
        return StationTemplateConfiguration(
            station_id=station_id,
            station_revision=runtime.station_revision,
            runtime=runtime,
            binding=None,
            version=None,
            status=TemplateBindingStatus.UNBOUND,
            status_detail="工位尚未绑定模板版本",
            topology_issues=(),
            backends=(),
        )
    version = templates.version_by_id(binding.desired_version_id)
    if version is None:
        runtime = device.read_station_runtime_parameters(station_id, defaults=None)
        return StationTemplateConfiguration(
            station_id=station_id,
            station_revision=runtime.station_revision,
            runtime=runtime,
            binding=binding,
            version=None,
            status=TemplateBindingStatus.TOPOLOGY_INVALID,
            status_detail="期望模板版本不存在，无法解析工位配置",
            topology_issues=(),
            backends=(),
        )
    runtime = device.read_station_runtime_parameters(
        station_id,
        defaults=_device_runtime_defaults(version.runtime_defaults),
    )
    specification = _specification(
        station_id=station_id,
        version=version,
        runtime_mode=runtime.mode,
        runtime_overrides=runtime.overrides,
        actor_id=caller.user.id,
    )
    validation = device.validate_template_binding(specification)
    return _read_configuration(
        station_id=station_id,
        caller=caller,
        templates=templates,
        device=device,
        binding=binding,
        version=version,
        runtime=runtime,
        topology_issues=validation.reasons if not validation.accepted else (),
    )


def update_station_runtime_parameters(
    *,
    station_id: UUID,
    mode: RuntimeParameterMode,
    overrides: StationRuntimeParameters | None,
    expected_station_revision: int,
    caller: Caller,
    now: datetime,
    templates: TemplateBindingRepository,
    device: DeviceTemplateBindingGateway,
) -> StationTemplateConfiguration:
    """显式替换工位运行参数整组；存在绑定时同步推进 desired revision。"""
    authorize(caller, Permission.STATION_EDIT)
    binding = templates.binding_by_station(station_id)
    version = None if binding is None else templates.version_by_id(binding.desired_version_id)
    defaults = None if version is None else _device_runtime_defaults(version.runtime_defaults)
    current = device.read_station_runtime_parameters(station_id, defaults=defaults)
    if current.station_revision != expected_station_revision:
        _raise_stale_revision(
            station_id=station_id,
            expected=expected_station_revision,
            actual=current.station_revision,
        )
    if current.mode is mode and current.overrides == overrides:
        return _read_configuration(
            station_id=station_id,
            caller=caller,
            templates=templates,
            device=device,
            binding=binding,
            version=version,
            runtime=current,
            topology_issues=(),
        )
    updated = device.update_station_runtime_parameters(
        station_id,
        mode=mode,
        overrides=overrides,
        expected_station_revision=expected_station_revision,
        actor_id=caller.user.id,
        now=now,
        defaults=defaults,
    )
    if binding is not None:
        desired = TemplateStationBinding(
            id=binding.id,
            station_id=binding.station_id,
            desired_version_id=binding.desired_version_id,
            desired_sha256=binding.desired_sha256,
            desired_config_revision=updated.runtime_parameters_revision,
            revision=binding.revision + 1,
            created_by=binding.created_by,
            updated_by=caller.user.id,
            created_at=binding.created_at,
            updated_at=now,
        )
        templates.save_binding(desired, expected_revision=binding.revision)
        binding = desired
    _logger.info(
        "template.station_runtime_parameters.succeeded",
        station_id=str(station_id),
        mode=mode.value,
        config_revision=str(updated.runtime_parameters_revision),
        actor_id=str(caller.user.id),
    )
    return _read_configuration(
        station_id=station_id,
        caller=caller,
        templates=templates,
        device=device,
        binding=binding,
        version=version,
        runtime=updated,
        topology_issues=(),
    )


def report_template_configuration(
    *,
    host: InferenceHostIdentity,
    station_id: UUID,
    backend_id: UUID,
    reported_version_id: UUID,
    reported_sha256: str,
    reported_config_revision: int,
    now: datetime,
    templates: TemplateBindingRepository,
    host_gateway: DeviceHostGateway,
) -> TemplateConfigurationReportResult:
    """接收一次已认证主机确认，保留最后有效事实并记录语义拒绝。"""
    host_gateway.authenticate(host=host, now=now)
    if not host_gateway.owns_station_backend(
        host_id=host.host_id,
        station_id=station_id,
        backend_id=backend_id,
    ):
        _logger.warning(
            "template.configuration_report.rejected",
            error_code=TemplateRefusalCode.REPORT_HOST_NOT_ALLOWED.value,
            host_id=str(host.host_id),
            station_id=str(station_id),
            backend_id=str(backend_id),
        )
        raise TemplateRefusedError(TemplateRefusalCode.REPORT_HOST_NOT_ALLOWED)

    binding = templates.binding_by_station(station_id)
    if binding is None:
        _logger.warning(
            "template.configuration_report.rejected",
            error_code=TemplateRefusalCode.REPORT_STATION_UNBOUND.value,
            host_id=str(host.host_id),
            station_id=str(station_id),
            backend_id=str(backend_id),
        )
        raise TemplateRefusedError(TemplateRefusalCode.REPORT_STATION_UNBOUND)
    existing = templates.report_by_backend(station_id=station_id, backend_id=backend_id)

    version = templates.version_by_id(reported_version_id)
    if version is None:
        return _save_rejection(
            binding=binding,
            existing=existing,
            host_id=host.host_id,
            station_id=station_id,
            backend_id=backend_id,
            code=TemplateReportRejectionCode.UNKNOWN_VERSION,
            detail="上报的模板版本不存在",
            now=now,
            templates=templates,
        )
    template = templates.template_by_id(version.template_id)
    if template is None or template.station_id != station_id:
        return _save_rejection(
            binding=binding,
            existing=existing,
            host_id=host.host_id,
            station_id=station_id,
            backend_id=backend_id,
            code=TemplateReportRejectionCode.VERSION_STATION_MISMATCH,
            detail="上报模板版本不属于该工位",
            now=now,
            templates=templates,
        )
    if version.sha256 != reported_sha256:
        return _save_rejection(
            binding=binding,
            existing=existing,
            host_id=host.host_id,
            station_id=station_id,
            backend_id=backend_id,
            code=TemplateReportRejectionCode.DIGEST_MISMATCH,
            detail="上报摘要与不可变模板版本不一致",
            now=now,
            templates=templates,
        )
    if reported_config_revision > binding.desired_config_revision:
        return _save_rejection(
            binding=binding,
            existing=existing,
            host_id=host.host_id,
            station_id=station_id,
            backend_id=backend_id,
            code=TemplateReportRejectionCode.FUTURE_REVISION,
            detail="上报配置修订高于中心期望修订",
            now=now,
            templates=templates,
        )
    if existing is not None and existing.reported_config_revision is not None:
        assert existing.reported_version_id is not None
        assert existing.reported_sha256 is not None
        if reported_config_revision < existing.reported_config_revision:
            return _save_rejection(
                binding=binding,
                existing=existing,
                host_id=host.host_id,
                station_id=station_id,
                backend_id=backend_id,
                code=TemplateReportRejectionCode.STALE_REVISION,
                detail="上报配置修订落后于该后端最近有效确认",
                now=now,
                templates=templates,
            )
        if reported_config_revision == existing.reported_config_revision and (
            reported_version_id != existing.reported_version_id
            or reported_sha256 != existing.reported_sha256
        ):
            return _save_rejection(
                binding=binding,
                existing=existing,
                host_id=host.host_id,
                station_id=station_id,
                backend_id=backend_id,
                code=TemplateReportRejectionCode.CONFLICTING_CONFIRMATION,
                detail="同一配置修订已有不同的版本或摘要确认",
                now=now,
                templates=templates,
            )
    if (
        reported_config_revision == binding.desired_config_revision
        and reported_version_id != binding.desired_version_id
    ):
        return _save_rejection(
            binding=binding,
            existing=existing,
            host_id=host.host_id,
            station_id=station_id,
            backend_id=backend_id,
            code=TemplateReportRejectionCode.VERSION_ID_MISMATCH,
            detail="上报版本身份与当前期望版本不一致",
            now=now,
            templates=templates,
        )

    if (
        existing is not None
        and existing.last_rejection_code is None
        and existing.reported_version_id == reported_version_id
        and existing.reported_sha256 == reported_sha256
        and existing.reported_config_revision == reported_config_revision
    ):
        # 相同确认是安全重试，不更新接收时间或配置事实。
        return TemplateConfigurationReportResult(accepted=True, report=existing)

    report = TemplateConfigurationReport(
        station_id=station_id,
        backend_id=backend_id,
        host_id=host.host_id,
        reported_version_id=reported_version_id,
        reported_sha256=reported_sha256,
        reported_config_revision=reported_config_revision,
        reported_at=now,
        last_rejection_code=None,
        last_rejection_detail=None,
        last_rejection_at=None,
        created_by=host.host_id,
        updated_by=host.host_id,
        created_at=now,
        updated_at=now,
    )
    templates.save_report(report, expected=existing)
    _logger.info(
        "template.configuration_report.accepted",
        host_id=str(host.host_id),
        station_id=str(station_id),
        backend_id=str(backend_id),
        version_id=str(reported_version_id),
        sha256=reported_sha256,
        config_revision=str(reported_config_revision),
    )
    return TemplateConfigurationReportResult(accepted=True, report=report)


def _version_for_station(
    *, station_id: UUID, version_id: UUID, templates: TemplateBindingRepository
) -> TemplateVersion:
    version = templates.version_by_id(version_id)
    if version is None:
        raise TemplateRefusedError(TemplateRefusalCode.VERSION_NOT_FOUND)
    template = templates.template_by_id(version.template_id)
    if template is None or template.station_id != station_id:
        raise TemplateRefusedError(
            TemplateRefusalCode.VERSION_STATION_MISMATCH,
            field_errors=(
                TemplateFieldError(
                    "工位",
                    None,
                    "station_id",
                    "模板版本不属于目标工位",
                ),
            ),
        )
    if version.boundary.start_signal is None or version.boundary.end_signals is None:
        raise TemplateRefusedError(
            TemplateRefusalCode.VERSION_INVALID,
            field_errors=(TemplateFieldError("版本", None, "boundary", "模板版本边界声明未完成"),),
        )
    return version


def _device_runtime_defaults(
    defaults: TemplateRuntimeDefaults,
) -> StationRuntimeParameters | None:
    if (
        defaults.idle_timeout_seconds is None
        or defaults.step_deadline_seconds is None
        or defaults.disposition_policy is None
    ):
        return None
    return StationRuntimeParameters(
        idle_timeout_seconds=defaults.idle_timeout_seconds,
        step_deadline_seconds=defaults.step_deadline_seconds,
        disposition_policy=defaults.disposition_policy,
    )


def _requested_runtime(
    *,
    current: StationRuntimeConfiguration,
    mode: RuntimeParameterMode | None,
    overrides: StationRuntimeParameters | None,
) -> tuple[RuntimeParameterMode, StationRuntimeParameters | None]:
    selected = current.mode if mode is None else mode
    if selected is RuntimeParameterMode.FOLLOW_TEMPLATE:
        return selected, None
    return selected, current.overrides if mode is None and overrides is None else overrides


def _specification(
    *,
    station_id: UUID,
    version: TemplateVersion,
    runtime_mode: RuntimeParameterMode,
    runtime_overrides: StationRuntimeParameters | None,
    actor_id: UUID,
) -> TemplateBindingSpecification:
    assert version.boundary.start_signal is not None
    assert version.boundary.end_signals is not None
    return TemplateBindingSpecification(
        station_id=station_id,
        template_version_id=version.id,
        template_version_sha256=version.sha256,
        start_signal=_signal(
            version.boundary.start_signal.kind.value,
            version.boundary.start_signal.value,
        ),
        end_signals=tuple(
            _signal(signal.kind.value, signal.value) for signal in version.boundary.end_signals
        ),
        runtime_mode=runtime_mode,
        runtime_overrides=runtime_overrides,
        actor_id=actor_id,
    )


def _signal(kind: str, value: int | str) -> BindingSignal:
    return BindingSignal(kind=BindingSignalKind(kind), value=value)


def _raise_binding_invalid(reasons: tuple[BindingValidationIssue, ...]) -> NoReturn:
    raise TemplateRefusedError(
        TemplateRefusalCode.BINDING_INVALID,
        field_errors=tuple(
            TemplateFieldError("设备", None, reason.field, reason.message) for reason in reasons
        ),
    )


def _raise_stale_revision(*, station_id: UUID, expected: int, actual: int | None) -> NoReturn:
    raise TemplateRefusedError(
        TemplateRefusalCode.STALE_REVISION,
        field_errors=(
            TemplateFieldError(
                "工位",
                None,
                "station_revision",
                f"工位配置已变化（期望 {expected}，当前 {actual}）",
            ),
        ),
    )


def _read_configuration(
    *,
    station_id: UUID,
    caller: Caller,
    templates: TemplateBindingRepository,
    device: DeviceTemplateBindingGateway,
    binding: TemplateStationBinding | None,
    version: TemplateVersion | None,
    runtime: StationRuntimeConfiguration,
    topology_issues: tuple[BindingValidationIssue, ...],
) -> StationTemplateConfiguration:
    reports = {report.backend_id: report for report in templates.reports_for_station(station_id)}
    backend_ids = list(device.participating_backend_ids(station_id))
    for backend_id in reports:
        if backend_id not in backend_ids:
            backend_ids.append(backend_id)
    if binding is None or version is None:
        return StationTemplateConfiguration(
            station_id=station_id,
            station_revision=runtime.station_revision,
            runtime=runtime,
            binding=binding,
            version=version,
            status=(
                TemplateBindingStatus.UNBOUND
                if binding is None
                else TemplateBindingStatus.TOPOLOGY_INVALID
            ),
            status_detail=("工位尚未绑定模板版本" if binding is None else "期望模板版本不可读取"),
            topology_issues=topology_issues,
            backends=(),
        )

    backend_views = tuple(
        _backend_status(
            backend_id=backend_id,
            report=reports.get(backend_id),
            binding=binding,
        )
        for backend_id in backend_ids
    )
    status, detail = _overall_status(
        topology_issues=topology_issues,
        backend_views=backend_views,
    )
    _logger.info(
        "template.station_configuration.read",
        station_id=str(station_id),
        status=status.value,
        config_revision=str(binding.desired_config_revision),
        actor_id=str(caller.user.id),
    )
    return StationTemplateConfiguration(
        station_id=station_id,
        station_revision=runtime.station_revision,
        runtime=runtime,
        binding=binding,
        version=version,
        status=status,
        status_detail=detail,
        topology_issues=topology_issues,
        backends=backend_views,
    )


def _backend_status(
    *,
    backend_id: UUID,
    report: TemplateConfigurationReport | None,
    binding: TemplateStationBinding,
) -> BackendConfigurationStatus:
    if report is None:
        return BackendConfigurationStatus(
            backend_id=backend_id,
            host_id=None,
            status=TemplateBindingStatus.NOT_CONFIRMED,
            reported_version_id=None,
            reported_sha256=None,
            reported_config_revision=None,
            reported_at=None,
            rejection_code=None,
            rejection_detail=None,
            rejection_at=None,
        )
    if report.last_rejection_code is not None:
        status = TemplateBindingStatus.REJECTED
    elif (
        report.reported_sha256 is not None
        and report.reported_version_id == binding.desired_version_id
    ):
        status = (
            TemplateBindingStatus.CONFIRMED
            if report.reported_sha256 == binding.desired_sha256
            and report.reported_config_revision == binding.desired_config_revision
            else TemplateBindingStatus.DIGEST_MISMATCH
            if report.reported_sha256 != binding.desired_sha256
            else TemplateBindingStatus.WAITING
        )
    else:
        status = TemplateBindingStatus.WAITING
    return BackendConfigurationStatus(
        backend_id=backend_id,
        host_id=report.host_id,
        status=status,
        reported_version_id=report.reported_version_id,
        reported_sha256=report.reported_sha256,
        reported_config_revision=report.reported_config_revision,
        reported_at=report.reported_at,
        rejection_code=report.last_rejection_code,
        rejection_detail=report.last_rejection_detail,
        rejection_at=report.last_rejection_at,
    )


def _overall_status(
    *,
    topology_issues: tuple[BindingValidationIssue, ...],
    backend_views: tuple[BackendConfigurationStatus, ...],
) -> tuple[TemplateBindingStatus, str]:
    if topology_issues:
        return TemplateBindingStatus.TOPOLOGY_INVALID, "当前设备拓扑或边界已不再满足模板绑定要求"
    if any(item.status is TemplateBindingStatus.REJECTED for item in backend_views):
        return TemplateBindingStatus.REJECTED, "最近一次推理机上报被拒绝"
    if any(item.status is TemplateBindingStatus.DIGEST_MISMATCH for item in backend_views):
        return TemplateBindingStatus.DIGEST_MISMATCH, "推理机上报摘要与期望内容不一致"
    if backend_views and all(
        item.status is TemplateBindingStatus.CONFIRMED for item in backend_views
    ):
        return TemplateBindingStatus.CONFIRMED, "所有参与推理后端均已确认当前期望配置"
    return TemplateBindingStatus.WAITING, "等待所有参与推理后端确认当前期望配置"


def _save_rejection(
    *,
    binding: TemplateStationBinding,
    existing: TemplateConfigurationReport | None,
    host_id: UUID,
    station_id: UUID,
    backend_id: UUID,
    code: TemplateReportRejectionCode,
    detail: str,
    now: datetime,
    templates: TemplateBindingRepository,
) -> TemplateConfigurationReportResult:
    report = TemplateConfigurationReport(
        station_id=station_id,
        backend_id=backend_id,
        host_id=host_id,
        reported_version_id=None if existing is None else existing.reported_version_id,
        reported_sha256=None if existing is None else existing.reported_sha256,
        reported_config_revision=(None if existing is None else existing.reported_config_revision),
        reported_at=None if existing is None else existing.reported_at,
        last_rejection_code=code.value,
        last_rejection_detail=detail,
        last_rejection_at=now,
        created_by=existing.created_by if existing is not None else host_id,
        updated_by=host_id,
        created_at=existing.created_at if existing is not None else now,
        updated_at=now,
    )
    templates.save_report(report, expected=existing)
    _logger.warning(
        "template.configuration_report.rejected",
        error_code=code.value,
        host_id=str(host_id),
        station_id=str(station_id),
        backend_id=str(backend_id),
        desired_version_id=str(binding.desired_version_id),
        desired_config_revision=str(binding.desired_config_revision),
    )
    return TemplateConfigurationReportResult(
        accepted=False,
        report=report,
        rejection_code=code,
        rejection_detail=detail,
    )


__all__ = [
    "BackendConfigurationStatus",
    "StationTemplateConfiguration",
    "TemplateBindingPreview",
    "TemplateConfigurationReportResult",
    "bind_template_version",
    "preview_template_binding",
    "read_station_configuration",
    "report_template_configuration",
    "update_station_runtime_parameters",
]
