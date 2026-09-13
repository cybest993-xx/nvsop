"""device PostgreSQL 适配器共用的拒绝转换。"""

from __future__ import annotations

from typing import NoReturn

from sqlalchemy.exc import DatabaseError

from factory_sop.device.errors import DeviceRefusalCode, DeviceRefusedError

# 唯一约束和数据库触发器的名称是迁移与适配器共享的稳定边界。
_CONSTRAINT_REFUSALS: dict[str, DeviceRefusalCode] = {
    "uq_device_inference_host_name": DeviceRefusalCode.INFERENCE_HOST_NAME_TAKEN,
    "uq_device_inference_backend_host_id_base_url": (
        DeviceRefusalCode.INFERENCE_BACKEND_ENDPOINT_TAKEN
    ),
    "uq_device_station_code": DeviceRefusalCode.STATION_CODE_TAKEN,
    "uq_device_connector_station_id_name": DeviceRefusalCode.CONNECTOR_NAME_TAKEN,
    "ck_device_connector_configuration_safe": DeviceRefusalCode.CONNECTOR_CONFIGURATION_SECRET,
    "uq_device_point_station_id_semantic_label": (DeviceRefusalCode.POINT_SEMANTIC_LABEL_TAKEN),
    "uq_device_point_connector_id_direction_identifier": (DeviceRefusalCode.POINT_IDENTITY_TAKEN),
}
_HOST_FOREIGN_KEY = "fk_device_inference_backend_host_id_device_inference_host"
_TRIGGERED_REFUSALS = {
    "device_backend_host_deactivated": DeviceRefusalCode.INFERENCE_HOST_DEACTIVATED,
    "device_camera_host_backend_mismatch": DeviceRefusalCode.CAMERA_HOST_BACKEND_MISMATCH,
    "device_camera_host_deactivated": DeviceRefusalCode.INFERENCE_HOST_DEACTIVATED,
    "device_camera_backend_deactivated": DeviceRefusalCode.INFERENCE_BACKEND_DEACTIVATED,
    "device_camera_station_deactivated": DeviceRefusalCode.STATION_DEACTIVATED,
    "device_camera_station_host_conflict": DeviceRefusalCode.CAMERA_STATION_HOST_CONFLICT,
    "device_camera_station_template_conflict": DeviceRefusalCode.CAMERA_STATION_TEMPLATE_CONFLICT,
    "device_connector_station_deactivated": DeviceRefusalCode.STATION_DEACTIVATED,
    "device_connector_host_deactivated": DeviceRefusalCode.INFERENCE_HOST_DEACTIVATED,
    "device_connector_station_host_conflict": DeviceRefusalCode.CONNECTOR_STATION_HOST_CONFLICT,
    "device_connector_has_points": DeviceRefusalCode.CONNECTOR_HAS_POINTS,
    "device_point_connector_station_mismatch": (DeviceRefusalCode.POINT_CONNECTOR_STATION_MISMATCH),
    "device_point_connector_deactivated": DeviceRefusalCode.CONNECTOR_DEACTIVATED,
    "device_point_station_deactivated": DeviceRefusalCode.STATION_DEACTIVATED,
}


def refuse_constraint_violation(
    error: DatabaseError,
    *,
    foreign_key_to_host: DeviceRefusalCode | None = None,
    foreign_key_to_station: DeviceRefusalCode | None = None,
    foreign_key_to_backend: DeviceRefusalCode | None = None,
    foreign_key_to_connector: DeviceRefusalCode | None = None,
    point_station_refusal: DeviceRefusalCode | None = None,
    point_connector_refusal: DeviceRefusalCode | None = None,
    station_template_refusal: DeviceRefusalCode | None = None,
    station_binding_refusal: DeviceRefusalCode | None = None,
    station_report_refusal: DeviceRefusalCode | None = None,
    backend_report_refusal: DeviceRefusalCode | None = None,
    host_report_refusal: DeviceRefusalCode | None = None,
    pending_command_refusal: DeviceRefusalCode | None = None,
) -> NoReturn:
    """将数据库约束或触发器拒绝转换为 device 错误。"""
    diagnostics = getattr(error.orig, "diag", None)
    constraint_name = getattr(diagnostics, "constraint_name", None)
    sqlstate = getattr(error.orig, "sqlstate", None)
    message = getattr(diagnostics, "message_primary", None)
    if sqlstate == "P0001" and message in _TRIGGERED_REFUSALS:
        raise DeviceRefusedError(_TRIGGERED_REFUSALS[message]) from error
    if constraint_name == "fk_template_sop_template_station_id_device_station":
        if station_template_refusal is None:
            raise error
        raise DeviceRefusedError(station_template_refusal) from error
    if constraint_name == "fk_template_station_binding_station_id_device_station":
        if station_binding_refusal is None:
            raise error
        raise DeviceRefusedError(station_binding_refusal) from error
    if constraint_name == "fk_template_configuration_report_station_id_device_station":
        if station_report_refusal is None:
            raise error
        raise DeviceRefusedError(station_report_refusal) from error
    if constraint_name in {
        "fk_template_configuration_report_backend_id_device_inference_backend",
        "fk_template_configuration_report_backend_id_device_infe_c4c5",
    }:
        if backend_report_refusal is None:
            raise error
        raise DeviceRefusedError(backend_report_refusal) from error
    if constraint_name == "fk_template_configuration_report_host_id_device_inference_host":
        if host_report_refusal is None:
            raise error
        raise DeviceRefusedError(host_report_refusal) from error
    if constraint_name == "fk_device_pending_command_host_id_device_inference_host":
        if pending_command_refusal is None:
            raise error
        raise DeviceRefusedError(pending_command_refusal) from error
    if constraint_name == _HOST_FOREIGN_KEY:
        if foreign_key_to_host is None:
            raise error
        raise DeviceRefusedError(foreign_key_to_host) from error
    if constraint_name == "fk_device_camera_station_id_device_station":
        if foreign_key_to_station is None:
            raise error
        raise DeviceRefusedError(foreign_key_to_station) from error
    if constraint_name == "fk_device_connector_station_id_device_station":
        refusal = foreign_key_to_connector or foreign_key_to_station
        if refusal is None:
            raise error
        raise DeviceRefusedError(refusal) from error
    if constraint_name == "fk_device_camera_host_id_device_inference_host":
        if foreign_key_to_host is None:
            raise error
        raise DeviceRefusedError(foreign_key_to_host) from error
    if constraint_name == "fk_device_connector_host_id_device_inference_host":
        refusal = foreign_key_to_connector or foreign_key_to_host
        if refusal is None:
            raise error
        raise DeviceRefusedError(refusal) from error
    if constraint_name == "fk_device_camera_backend_id_device_inference_backend":
        if foreign_key_to_backend is None:
            raise error
        raise DeviceRefusedError(foreign_key_to_backend) from error
    if constraint_name == "fk_device_point_station_id_device_station":
        if point_station_refusal is None:
            raise error
        raise DeviceRefusedError(point_station_refusal) from error
    if constraint_name == "fk_device_point_connector_id_device_connector":
        if point_connector_refusal is None:
            raise error
        raise DeviceRefusedError(point_connector_refusal) from error
    refusal = _CONSTRAINT_REFUSALS.get(constraint_name or "")
    if refusal is None:
        raise error
    raise DeviceRefusedError(refusal) from error


def refuse_lost_race(
    *,
    missing_refusal: DeviceRefusalCode,
    present_refusal: DeviceRefusalCode,
    row: object | None,
) -> NoReturn:
    """根据条件写入后的再查询区分资源消失和版本竞争。"""
    if row is None:
        raise DeviceRefusedError(missing_refusal)
    raise DeviceRefusedError(present_refusal)


__all__ = ["refuse_constraint_violation", "refuse_lost_race"]
