"""授权点位 CRUD 与绑定前能力校验。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import assert_never
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.device.errors import DeviceRefusalCode
from factory_sop.device.model import (
    BindingReason,
    BindingReasonCode,
    BindingValidation,
    Connector,
    DeviceStatus,
    Point,
    PointDirection,
    Station,
)
from factory_sop.device.repository import ConnectorRepository, PointRepository, StationRepository
from factory_sop.device.usecases._transitions import refuse
from factory_sop.identifiers import new_id
from factory_sop.observability import get_logger
from nvsop_contracts import Measured, PointRole, Unfitness, unfit_for

_logger = get_logger("device")
_REFUSAL_EVENT = "device.point.refused"


def create_point(
    *,
    station_id: UUID,
    connector_id: UUID,
    direction: PointDirection,
    identifier: str,
    semantic_label: str,
    caller: Caller,
    now: datetime,
    stations: StationRepository,
    connectors: ConnectorRepository,
    points: PointRepository,
) -> Point:
    """在活动且拓扑一致的工位和连接器上创建点位。"""
    authorize(caller, Permission.POINT_EDIT)
    _validate_placement(station_id, connector_id, stations=stations, connectors=connectors)
    point = Point(
        id=new_id(),
        station_id=station_id,
        connector_id=connector_id,
        direction=direction,
        identifier=identifier,
        semantic_label=semantic_label,
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=caller.user.id,
        updated_by=caller.user.id,
        created_at=now,
        updated_at=now,
    )
    points.add(point)
    _logger.info("device.point.created", point_id=str(point.id), actor_id=str(caller.user.id))
    return point


def edit_point(
    *,
    point_id: UUID,
    station_id: UUID,
    connector_id: UUID,
    direction: PointDirection,
    identifier: str,
    semantic_label: str,
    expected_revision: int,
    caller: Caller,
    now: datetime,
    stations: StationRepository,
    connectors: ConnectorRepository,
    points: PointRepository,
) -> Point:
    """整体替换点位配置；迁移父级时要求目标仍可用。"""
    authorize(caller, Permission.POINT_EDIT)
    point = _existing(point_id, points)
    _require_revision(point, expected_revision)
    if (point.station_id, point.connector_id) != (station_id, connector_id):
        _validate_placement(station_id, connector_id, stations=stations, connectors=connectors)
    else:
        _existing_station(station_id, stations)
        _existing_connector(connector_id, connectors)
    edited = replace(
        point,
        station_id=station_id,
        connector_id=connector_id,
        direction=direction,
        identifier=identifier,
        semantic_label=semantic_label,
        revision=point.revision + 1,
        updated_by=caller.user.id,
        updated_at=now,
    )
    points.save(edited, expected_revision=expected_revision)
    _logger.info("device.point.updated", point_id=str(point.id), actor_id=str(caller.user.id))
    return edited


def set_point_status(
    *,
    point_id: UUID,
    requested_status: DeviceStatus,
    expected_revision: int,
    caller: Caller,
    now: datetime,
    points: PointRepository,
) -> Point:
    """停用或恢复点位，不级联修改连接器。"""
    authorize(caller, Permission.POINT_EDIT)
    point = _existing(point_id, points)
    _require_revision(point, expected_revision)
    if point.status is requested_status:
        return point
    match requested_status:
        case DeviceStatus.DEACTIVATED:
            event = "device.point.deactivated"
        case DeviceStatus.ACTIVE:
            event = "device.point.restored"
        case _:
            assert_never(requested_status)
    changed = replace(
        point,
        status=requested_status,
        revision=point.revision + 1,
        updated_by=caller.user.id,
        updated_at=now,
    )
    points.save(changed, expected_revision=expected_revision)
    _logger.info(event, point_id=str(point.id), actor_id=str(caller.user.id))
    return changed


def delete_point(
    *, point_id: UUID, expected_revision: int, caller: Caller, points: PointRepository
) -> None:
    """按调用方读取的版本永久删除点位。"""
    authorize(caller, Permission.POINT_DELETE)
    point = _existing(point_id, points)
    _require_revision(point, expected_revision)
    if not points.remove(point_id, expected_revision=expected_revision):
        refuse(_REFUSAL_EVENT, DeviceRefusalCode.POINT_NOT_FOUND, point_id=str(point_id))
    _logger.info("device.point.deleted", point_id=str(point_id), actor_id=str(caller.user.id))


def point_by_identifier(*, point_id: UUID, caller: Caller, points: PointRepository) -> Point:
    """按 UUID 读取一个点位。"""
    authorize(caller, Permission.POINT_VIEW)
    return _existing(point_id, points)


def list_points(
    *,
    caller: Caller,
    points: PointRepository,
    page: int,
    page_size: int,
    station_id: UUID | None,
    connector_id: UUID | None,
) -> tuple[list[Point], int]:
    """列出点位，可独立按工位或连接器筛选。"""
    authorize(caller, Permission.POINT_VIEW)
    return points.page_of(
        page=page,
        page_size=page_size,
        station_id=station_id,
        connector_id=connector_id,
    )


def validate_binding(
    *,
    station_id: UUID,
    point_id: UUID | None,
    role: PointRole,
    budget: float,
    caller: Caller,
    stations: StationRepository,
    points: PointRepository,
    connectors: ConnectorRepository,
) -> BindingValidation:
    """校验候选来源是否可绑定；本接口不创建模板或绑定。"""
    authorize(caller, Permission.POINT_VIEW)
    station = _existing_station(station_id, stations)
    if station.status is DeviceStatus.DEACTIVATED:
        refuse(_REFUSAL_EVENT, DeviceRefusalCode.STATION_DEACTIVATED, station_id=str(station_id))
    if point_id is None:
        match role:
            case PointRole.SAFETY_OUTPUT:
                return _rejected(
                    BindingReasonCode.POINT_REQUIRED,
                    "point_id",
                    "安全输出必须选择一个输出点位",
                )
            case (
                PointRole.START_SIGNAL
                | PointRole.END_SIGNAL
                | PointRole.ORDERED_STEP
                | PointRole.UNORDERED_STEP
            ):
                return BindingValidation(accepted=True, reasons=())
            case _:
                assert_never(role)

    point = points.by_id(point_id)
    if point is None:
        return _rejected(BindingReasonCode.POINT_NOT_FOUND, "point_id", "所选点位不存在")
    if point.station_id != station_id:
        return _rejected(
            BindingReasonCode.POINT_STATION_MISMATCH,
            "station_id",
            "所选点位属于其他工位",
        )
    if point.status is DeviceStatus.DEACTIVATED:
        return _rejected(BindingReasonCode.POINT_DEACTIVATED, "status", "所选点位已停用")

    connector = connectors.by_id(point.connector_id)
    if connector is None:
        return _rejected(
            BindingReasonCode.CONNECTOR_NOT_FOUND,
            "connector_id",
            "点位所属连接器不存在",
        )
    if connector.status is DeviceStatus.DEACTIVATED:
        return _rejected(
            BindingReasonCode.CONNECTOR_DEACTIVATED,
            "connector_id",
            "点位所属连接器已停用",
        )

    match role:
        case (
            PointRole.START_SIGNAL
            | PointRole.END_SIGNAL
            | PointRole.ORDERED_STEP
            | PointRole.UNORDERED_STEP
        ):
            expected = PointDirection.INPUT
        case PointRole.SAFETY_OUTPUT:
            expected = PointDirection.OUTPUT
        case _:
            assert_never(role)
    if point.direction is not expected:
        role_name = "安全输出" if role is PointRole.SAFETY_OUTPUT else "信号或步骤"
        return _rejected(
            BindingReasonCode.WRONG_DIRECTION,
            "direction",
            f"{role_name}需要{_direction_name(expected)}点位，当前点位为"
            f"{_direction_name(point.direction)}",
        )

    reasons = tuple(
        _capability_reason(reason, connector=connector, budget=budget)
        for reason in unfit_for(connector.capability, role=role, budget=budget)
    )
    return BindingValidation(accepted=not reasons, reasons=reasons)


def _validate_placement(
    station_id: UUID,
    connector_id: UUID,
    *,
    stations: StationRepository,
    connectors: ConnectorRepository,
) -> None:
    station = _existing_station(station_id, stations)
    connector = _existing_connector(connector_id, connectors)
    if station.status is DeviceStatus.DEACTIVATED:
        refuse(_REFUSAL_EVENT, DeviceRefusalCode.STATION_DEACTIVATED, station_id=str(station_id))
    if connector.status is DeviceStatus.DEACTIVATED:
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.CONNECTOR_DEACTIVATED,
            connector_id=str(connector_id),
        )
    if connector.station_id != station_id:
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.POINT_CONNECTOR_STATION_MISMATCH,
            station_id=str(station_id),
            connector_id=str(connector_id),
        )


def _capability_reason(reason: Unfitness, *, connector: Connector, budget: float) -> BindingReason:
    match reason:
        case Unfitness.CAPABILITY_UNVERIFIED:
            return BindingReason(
                BindingReasonCode.CAPABILITY_UNVERIFIED,
                "capability.verification",
                "连接器能力尚未实测验证",
            )
        case Unfitness.MAY_DROP_EDGES:
            return BindingReason(
                BindingReasonCode.MAY_DROP_EDGES,
                "capability.edge_preservation",
                "连接器可能丢失瞬时边沿",
            )
        case Unfitness.NOT_SEQUENCED:
            return BindingReason(
                BindingReasonCode.NOT_SEQUENCED,
                "capability.sequencing",
                "连接器不能保证点位变化顺序",
            )
        case Unfitness.DELIVERY_TOO_SLOW:
            if not isinstance(connector.capability, Measured):
                raise AssertionError("只有实测能力可能超过延迟预算")
            return BindingReason(
                BindingReasonCode.DELIVERY_TOO_SLOW,
                "capability.max_delivery_delay_seconds",
                f"连接器最大投递延迟 {connector.capability.max_delivery_delay:g} 秒"
                f"超出角色预算 {budget:g} 秒",
            )
        case _:
            assert_never(reason)


def _direction_name(direction: PointDirection) -> str:
    match direction:
        case PointDirection.INPUT:
            return "输入"
        case PointDirection.OUTPUT:
            return "输出"
        case _:
            assert_never(direction)


def _rejected(code: BindingReasonCode, field: str, message: str) -> BindingValidation:
    return BindingValidation(
        accepted=False,
        reasons=(BindingReason(code=code, field=field, message=message),),
    )


def _existing(point_id: UUID, points: PointRepository) -> Point:
    point = points.by_id(point_id)
    if point is None:
        refuse(_REFUSAL_EVENT, DeviceRefusalCode.POINT_NOT_FOUND, point_id=str(point_id))
    return point


def _existing_station(station_id: UUID, stations: StationRepository) -> Station:
    station = stations.by_id(station_id)
    if station is None:
        refuse(_REFUSAL_EVENT, DeviceRefusalCode.STATION_NOT_FOUND, station_id=str(station_id))
    return station


def _existing_connector(connector_id: UUID, connectors: ConnectorRepository) -> Connector:
    connector = connectors.by_id(connector_id)
    if connector is None:
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.CONNECTOR_NOT_FOUND,
            connector_id=str(connector_id),
        )
    return connector


def _require_revision(point: Point, expected_revision: int) -> None:
    if point.revision != expected_revision:
        refuse(
            _REFUSAL_EVENT,
            DeviceRefusalCode.STALE_REVISION,
            point_id=str(point.id),
            expected_revision=str(expected_revision),
            actual_revision=str(point.revision),
        )
