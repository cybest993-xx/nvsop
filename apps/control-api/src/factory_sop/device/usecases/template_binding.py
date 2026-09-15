"""模板绑定所需的设备拓扑与工位运行参数适配。

模板模块只依赖 `device.api` 定义的窄接口；本文件把该接口接到设备模块已有的仓储 seam。
绑定校验在写入前和实际写入时都重新读取拓扑，避免把一次旧的预校验当作通行证。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID

from factory_sop.device.api import (
    BindingSignal,
    BindingSignalKind,
    BindingValidationIssue,
    DeviceTemplateBindingGateway,
    TemplateBindingSpecification,
    TemplateBindingValidation,
    authenticate_host,
    capability_unfitness,
)
from factory_sop.device.errors import (
    DeviceFieldError,
    DeviceRefusalCode,
    DeviceRefusedError,
)
from factory_sop.device.model import (
    Camera,
    Connector,
    DeviceStatus,
    InferenceBackend,
    InferenceHost,
    InferenceHostIdentity,
    Point,
    PointDirection,
    RuntimeParameterMode,
    Station,
    StationRuntimeConfiguration,
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
from nvsop_contracts import PointRole, Unfitness

TEMPLATE_EXTERNAL_SIGNAL_BUDGET_SECONDS = 0.5


@dataclass(frozen=True, slots=True)
class _Topology:
    station: Station
    cameras: tuple[Camera, ...]
    backends: tuple[InferenceBackend, ...]
    hosts: tuple[InferenceHost, ...]
    connectors: tuple[Connector, ...]
    points: tuple[Point, ...]


class RepositoryDeviceTemplateBindingGateway(DeviceTemplateBindingGateway):
    """把模板绑定 seam 接到同一请求事务中的 device 仓储。"""

    def __init__(
        self,
        *,
        stations: StationRepository,
        hosts: InferenceHostRepository,
        backends: InferenceBackendRepository,
        cameras: CameraRepository,
        connectors: ConnectorRepository,
        points: PointRepository,
    ) -> None:
        self._stations = stations
        self._hosts = hosts
        self._backends = backends
        self._cameras = cameras
        self._connectors = connectors
        self._points = points

    def validate_template_binding(
        self, specification: TemplateBindingSpecification
    ) -> TemplateBindingValidation:
        """重新读取设备拓扑，返回所有能在提交前修复的绑定问题。"""
        topology = self._topology(specification.station_id)
        if topology is None:
            return TemplateBindingValidation(
                accepted=False,
                reasons=(
                    BindingValidationIssue(
                        code="station_not_found",
                        field="station_id",
                        message="工位不存在",
                    ),
                ),
            )

        issues: list[BindingValidationIssue] = list(
            self._runtime_issues(specification.runtime_mode, specification.runtime_overrides)
        )
        if topology.station.status is DeviceStatus.DEACTIVATED:
            issues.append(
                BindingValidationIssue(
                    code="station_deactivated",
                    field="station_id",
                    message="工位已停用，恢复后才能绑定模板",
                )
            )

        if not topology.cameras:
            issues.append(
                BindingValidationIssue(
                    code="camera_required",
                    field="cameras",
                    message="工位至少需要一台已关联相机才能确定推理后端",
                )
            )

        backend_by_id = {backend.id: backend for backend in topology.backends}
        host_by_id = {host.id: host for host in topology.hosts}
        connector_by_id = {connector.id: connector for connector in topology.connectors}
        backend_ids: list[UUID] = []
        camera_host_ids: set[UUID] = set()

        for camera in topology.cameras:
            if camera.status is DeviceStatus.DEACTIVATED:
                issues.append(
                    BindingValidationIssue(
                        code="camera_deactivated",
                        field=f"cameras[{camera.id}].status",
                        message="参与绑定的相机已停用",
                    )
                )
                # 停用相机不属于当前运行拓扑；它仍使绑定失败，但不能影响参与后端集合。
                continue
            camera_host_ids.add(camera.host_id)
            backend = backend_by_id.get(camera.backend_id)
            if backend is None:
                issues.append(
                    BindingValidationIssue(
                        code="backend_not_found",
                        field=f"cameras[{camera.id}].backend_id",
                        message="相机引用的推理后端不存在",
                    )
                )
                continue
            if backend.id not in backend_ids:
                backend_ids.append(backend.id)
            if backend.host_id != camera.host_id:
                issues.append(
                    BindingValidationIssue(
                        code="camera_host_backend_mismatch",
                        field=f"cameras[{camera.id}]",
                        message="相机的推理机必须与推理后端所属推理机一致",
                    )
                )
            if backend.status is DeviceStatus.DEACTIVATED:
                issues.append(
                    BindingValidationIssue(
                        code="backend_deactivated",
                        field=f"backends[{backend.id}].status",
                        message="参与绑定的推理后端已停用",
                    )
                )
            host = host_by_id.get(camera.host_id)
            if host is None:
                issues.append(
                    BindingValidationIssue(
                        code="host_not_found",
                        field=f"cameras[{camera.id}].host_id",
                        message="相机引用的推理机不存在",
                    )
                )
            elif host.status is DeviceStatus.DEACTIVATED:
                issues.append(
                    BindingValidationIssue(
                        code="host_deactivated",
                        field=f"hosts[{host.id}].status",
                        message="参与绑定的推理机已停用",
                    )
                )

        if len(camera_host_ids) > 1:
            issues.append(
                BindingValidationIssue(
                    code="camera_station_host_conflict",
                    field="cameras.host_id",
                    message="同一工位的相机必须属于同一推理机",
                )
            )
        if not backend_ids:
            issues.append(
                BindingValidationIssue(
                    code="backend_required",
                    field="backends",
                    message="工位没有可确定的推理后端",
                )
            )

        for connector in topology.connectors:
            if connector.status is DeviceStatus.DEACTIVATED:
                issues.append(
                    BindingValidationIssue(
                        code="connector_deactivated",
                        field=f"connectors[{connector.id}].status",
                        message="绑定所需连接器已停用",
                    )
                )
            if camera_host_ids and connector.host_id not in camera_host_ids:
                issues.append(
                    BindingValidationIssue(
                        code="connector_station_host_conflict",
                        field=f"connectors[{connector.id}].host_id",
                        message="连接器必须使用工位相同的推理机",
                    )
                )

        for backend_id in backend_ids:
            backend = backend_by_id[backend_id]
            if (
                backend.template_version_id is not None
                and backend.template_version_id != specification.template_version_id
                and self._cameras.any_for_backend_outside_station(
                    backend_id, specification.station_id
                )
            ):
                issues.append(
                    BindingValidationIssue(
                        code="backend_template_conflict",
                        field=f"backends[{backend_id}].template_version_id",
                        message="该推理后端仍被其他工位使用，不能切换到不同模板",
                    )
                )

        signals: list[tuple[str, BindingSignal | None, PointRole]] = [
            ("start_signal", specification.start_signal, PointRole.START_SIGNAL)
        ]
        signals.extend(
            (f"end_signals[{index}]", signal, PointRole.END_SIGNAL)
            for index, signal in enumerate(specification.end_signals)
        )
        for field, signal, role in signals:
            if signal is None:
                issues.append(
                    BindingValidationIssue(
                        code="boundary_signal_required",
                        field=field,
                        message="必须声明边界信号",
                    )
                )
                continue
            if signal.kind is BindingSignalKind.ACTION:
                if (
                    isinstance(signal.value, bool)
                    or not isinstance(signal.value, int)
                    or signal.value <= 0
                ):
                    issues.append(
                        BindingValidationIssue(
                            code="boundary_signal_invalid",
                            field=field,
                            message="动作边界信号必须是正整数",
                        )
                    )
                continue
            if signal.kind is not BindingSignalKind.EXTERNAL or not isinstance(signal.value, str):
                issues.append(
                    BindingValidationIssue(
                        code="boundary_signal_invalid",
                        field=field,
                        message="边界信号结构无效",
                    )
                )
                continue
            matches = [point for point in topology.points if point.semantic_label == signal.value]
            if not matches:
                issues.append(
                    BindingValidationIssue(
                        code="point_not_found",
                        field=field,
                        message=f"找不到语义标签为“{signal.value}”的输入点位",
                    )
                )
                continue
            if len(matches) > 1:
                issues.append(
                    BindingValidationIssue(
                        code="point_ambiguous",
                        field=field,
                        message=f"语义标签“{signal.value}”对应多个点位，不能自动选择",
                    )
                )
                continue
            point = matches[0]
            point_connector = connector_by_id.get(point.connector_id)
            if point.station_id != specification.station_id:
                issues.append(
                    BindingValidationIssue(
                        code="point_station_mismatch",
                        field=field,
                        message="边界点位必须属于目标工位",
                    )
                )
            if point.status is DeviceStatus.DEACTIVATED:
                issues.append(
                    BindingValidationIssue(
                        code="point_deactivated",
                        field=field,
                        message="边界点位已停用",
                    )
                )
            if point.direction is not PointDirection.INPUT:
                issues.append(
                    BindingValidationIssue(
                        code="wrong_direction",
                        field=field,
                        message="开始和结束边界必须引用输入点位",
                    )
                )
            if point_connector is None:
                issues.append(
                    BindingValidationIssue(
                        code="connector_not_found",
                        field=field,
                        message="边界点位所属连接器不存在",
                    )
                )
                continue
            if point_connector.status is DeviceStatus.DEACTIVATED:
                issues.append(
                    BindingValidationIssue(
                        code="connector_deactivated",
                        field=field,
                        message="边界点位所属连接器已停用",
                    )
                )
            if camera_host_ids and point_connector.host_id not in camera_host_ids:
                issues.append(
                    BindingValidationIssue(
                        code="connector_station_host_conflict",
                        field=field,
                        message="边界点位连接器必须与工位使用相同推理机",
                    )
                )
            for reason in capability_unfitness(
                point_connector.capability,
                role=role,
                budget_seconds=TEMPLATE_EXTERNAL_SIGNAL_BUDGET_SECONDS,
            ):
                issues.append(self._capability_issue(field, reason))

        return TemplateBindingValidation(
            accepted=not issues,
            reasons=tuple(issues),
            station_revision=topology.station.revision,
            runtime_parameters_revision=topology.station.runtime_parameters_revision,
            backend_ids=tuple(backend_ids),
        )

    def apply_template_binding(
        self,
        specification: TemplateBindingSpecification,
        *,
        expected_station_revision: int,
        now: datetime,
    ) -> TemplateBindingValidation:
        """再次校验后条件写入 station 和参与后端的模板槽位。"""
        validation = self.validate_template_binding(specification)
        if not validation.accepted:
            raise DeviceRefusedError(
                DeviceRefusalCode.TEMPLATE_BINDING_INVALID,
                field_errors=tuple(
                    DeviceFieldError(reason.field, reason.message) for reason in validation.reasons
                ),
            )
        mode = specification.runtime_mode
        if mode is None:
            raise DeviceRefusedError(DeviceRefusalCode.STATION_RUNTIME_PARAMETERS_INVALID)
        self._validate_runtime_mode(mode, specification.runtime_overrides)

        self._backends.lock_template_binding_topology(validation.backend_ids)
        validation = self.validate_template_binding(specification)
        if not validation.accepted:
            raise DeviceRefusedError(
                DeviceRefusalCode.TEMPLATE_BINDING_INVALID,
                field_errors=tuple(
                    DeviceFieldError(reason.field, reason.message) for reason in validation.reasons
                ),
            )
        topology = self._topology(specification.station_id)
        if topology is None:
            raise DeviceRefusedError(DeviceRefusalCode.STATION_NOT_FOUND)
        if topology.station.revision != expected_station_revision:
            raise DeviceRefusedError(DeviceRefusalCode.STALE_REVISION)

        actor_id = specification.actor_id or topology.station.updated_by
        updated_station = replace(
            topology.station,
            revision=topology.station.revision + 1,
            updated_by=actor_id,
            updated_at=now,
            runtime_parameter_mode=mode,
            runtime_parameter_overrides=specification.runtime_overrides,
            runtime_parameters_revision=topology.station.runtime_parameters_revision + 1,
        )
        expected_revisions = {
            backend.id: backend.revision
            for backend in topology.backends
            if backend.id in validation.backend_ids
        }
        # 相机和后端拓扑写入统一采用后端→工位的锁序；先切换后端，再写工位运行参数，
        # 避免正式绑定先持有工位锁而与相机写入反向等待。整个请求事务仍保持原子性。
        self._backends.assign_template_version(
            backend_ids=validation.backend_ids,
            template_version_id=specification.template_version_id,
            actor_id=actor_id,
            now=now,
            expected_revisions=expected_revisions,
        )
        self._stations.save(updated_station, expected_revision=expected_station_revision)
        return TemplateBindingValidation(
            accepted=True,
            reasons=(),
            station_revision=updated_station.revision,
            runtime_parameters_revision=updated_station.runtime_parameters_revision,
            backend_ids=validation.backend_ids,
        )

    def read_station_runtime_parameters(
        self,
        station_id: UUID,
        *,
        defaults: StationRuntimeParameters | None,
    ) -> StationRuntimeConfiguration:
        topology = self._topology(station_id)
        if topology is None:
            raise DeviceRefusedError(DeviceRefusalCode.STATION_NOT_FOUND)
        station = topology.station
        return StationRuntimeConfiguration(
            station_id=station.id,
            station_revision=station.revision,
            runtime_parameters_revision=station.runtime_parameters_revision,
            mode=station.runtime_parameter_mode,
            defaults=defaults,
            overrides=station.runtime_parameter_overrides,
            effective=station.runtime_parameters_for(defaults),
        )

    def update_station_runtime_parameters(
        self,
        station_id: UUID,
        *,
        mode: RuntimeParameterMode,
        overrides: StationRuntimeParameters | None,
        expected_station_revision: int,
        actor_id: UUID,
        now: datetime,
        defaults: StationRuntimeParameters | None,
    ) -> StationRuntimeConfiguration:
        """以工位版本条件更新完整运行参数组，并推进配置修订。"""
        topology = self._topology(station_id)
        if topology is None:
            raise DeviceRefusedError(DeviceRefusalCode.STATION_NOT_FOUND)
        station = topology.station
        if station.status is DeviceStatus.DEACTIVATED:
            raise DeviceRefusedError(DeviceRefusalCode.STATION_DEACTIVATED)
        if station.revision != expected_station_revision:
            raise DeviceRefusedError(DeviceRefusalCode.STALE_REVISION)
        self._validate_runtime_mode(mode, overrides)
        updated = replace(
            station,
            revision=station.revision + 1,
            updated_by=actor_id,
            updated_at=now,
            runtime_parameter_mode=mode,
            runtime_parameter_overrides=overrides,
            runtime_parameters_revision=station.runtime_parameters_revision + 1,
        )
        self._stations.save(updated, expected_revision=expected_station_revision)
        return StationRuntimeConfiguration(
            station_id=updated.id,
            station_revision=updated.revision,
            runtime_parameters_revision=updated.runtime_parameters_revision,
            mode=updated.runtime_parameter_mode,
            defaults=defaults,
            overrides=updated.runtime_parameter_overrides,
            effective=updated.runtime_parameters_for(defaults),
        )

    def participating_backend_ids(self, station_id: UUID) -> tuple[UUID, ...]:
        """返回当前工位活动相机参与的去重后端集合。"""
        topology = self._topology(station_id)
        if topology is None:
            return ()
        result: list[UUID] = []
        for camera in topology.cameras:
            if camera.status is DeviceStatus.ACTIVE and camera.backend_id not in result:
                result.append(camera.backend_id)
        return tuple(result)

    def owns_station(self, *, host_id: UUID, station_id: UUID) -> bool:
        """判断认证主机是否拥有该工位上的活动相机。"""
        topology = self._topology(station_id)
        if topology is None or topology.station.status is DeviceStatus.DEACTIVATED:
            return False
        hosts = {host.id: host for host in topology.hosts}
        return any(
            camera.status is DeviceStatus.ACTIVE
            and camera.host_id == host_id
            and (host := hosts.get(camera.host_id)) is not None
            and host.status is DeviceStatus.ACTIVE
            for camera in topology.cameras
        )

    def owns_station_backend(self, *, host_id: UUID, station_id: UUID, backend_id: UUID) -> bool:
        """判断认证主机是否拥有该工位上的活动相机和活动推理后端。"""
        topology = self._topology(station_id)
        if topology is None or topology.station.status is DeviceStatus.DEACTIVATED:
            return False
        hosts = {host.id: host for host in topology.hosts}
        backends = {backend.id: backend for backend in topology.backends}
        for camera in topology.cameras:
            host = hosts.get(camera.host_id)
            backend = backends.get(camera.backend_id)
            if (
                camera.status is DeviceStatus.ACTIVE
                and camera.host_id == host_id
                and camera.backend_id == backend_id
                and host is not None
                and host.status is DeviceStatus.ACTIVE
                and backend is not None
                and backend.status is DeviceStatus.ACTIVE
            ):
                return True
        return False

    def authenticate(self, *, host: InferenceHostIdentity, now: datetime) -> None:
        authenticate_host(host=host, now=now, hosts=self._hosts)

    def _topology(self, station_id: UUID) -> _Topology | None:
        station = self._stations.by_id(station_id)
        if station is None:
            return None
        cameras = tuple(self._cameras.for_station(station_id))
        backend_ids: list[UUID] = []
        for camera in cameras:
            if camera.backend_id not in backend_ids:
                backend_ids.append(camera.backend_id)
        backends = tuple(
            backend
            for backend_id in backend_ids
            if (backend := self._backends.by_id(backend_id)) is not None
        )
        host_ids: list[UUID] = []
        for camera in cameras:
            if camera.host_id not in host_ids:
                host_ids.append(camera.host_id)
        hosts = tuple(
            host for host_id in host_ids if (host := self._hosts.by_id(host_id)) is not None
        )
        connectors = tuple(self._connectors.for_station(station_id))
        points = tuple(self._points.for_station(station_id))
        return _Topology(
            station=station,
            cameras=cameras,
            backends=backends,
            hosts=hosts,
            connectors=connectors,
            points=points,
        )

    @staticmethod
    def _runtime_issues(
        mode: RuntimeParameterMode | None,
        overrides: StationRuntimeParameters | None,
    ) -> tuple[BindingValidationIssue, ...]:
        if mode is None:
            return (
                BindingValidationIssue(
                    code="runtime_mode_required",
                    field="runtime_parameter_mode",
                    message="必须明确选择跟随模板或工位自定义",
                ),
            )
        if mode is RuntimeParameterMode.FOLLOW_TEMPLATE and overrides is not None:
            return (
                BindingValidationIssue(
                    code="runtime_overrides_forbidden",
                    field="runtime_parameters",
                    message="跟随模板时不能保存工位覆盖组",
                ),
            )
        if mode is RuntimeParameterMode.CUSTOM and overrides is None:
            return (
                BindingValidationIssue(
                    code="runtime_overrides_required",
                    field="runtime_parameters",
                    message="工位自定义时必须完整提供运行参数组",
                ),
            )
        return ()

    @classmethod
    def _validate_runtime_mode(
        cls, mode: RuntimeParameterMode, overrides: StationRuntimeParameters | None
    ) -> None:
        issues = cls._runtime_issues(mode, overrides)
        if issues:
            raise DeviceRefusedError(
                DeviceRefusalCode.STATION_RUNTIME_PARAMETERS_INVALID,
                field_errors=tuple(
                    DeviceFieldError(issue.field, issue.message) for issue in issues
                ),
            )
        if overrides is not None:
            try:
                StationRuntimeParameters.from_wire(overrides.to_wire())
            except (TypeError, ValueError) as error:
                raise DeviceRefusedError(
                    DeviceRefusalCode.STATION_RUNTIME_PARAMETERS_INVALID
                ) from error

    @staticmethod
    def _capability_issue(field: str, reason: Unfitness) -> BindingValidationIssue:
        messages = {
            Unfitness.CAPABILITY_UNVERIFIED: "连接器能力尚未实测验证",
            Unfitness.MAY_DROP_EDGES: "连接器可能丢失瞬时边沿",
            Unfitness.NOT_SEQUENCED: "连接器不能保证点位变化顺序",
            Unfitness.DELIVERY_TOO_SLOW: "连接器最大投递延迟超过 500 ms 产品预算",
        }
        return BindingValidationIssue(
            code=reason.value,
            field=f"{field}.capability",
            message=messages[reason],
        )


__all__ = [
    "TEMPLATE_EXTERNAL_SIGNAL_BUDGET_SECONDS",
    "RepositoryDeviceTemplateBindingGateway",
]
