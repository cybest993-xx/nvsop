"""Why `device` refused, in the vocabulary the HTTP adapter turns into `problem+json`.

§5.15 makes `error_code` a stable SCREAMING_SNAKE enumeration, a different type from a
judgment's `reason_code`. An exception rather than a result union, as in `auth`: every
refusal here has exactly one caller behavior — do not proceed, report this code.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never


class DeviceRefusalCode(StrEnum):
    """`device` 产生的稳定 `error_code` 值。"""

    INFERENCE_HOST_NOT_FOUND = "INFERENCE_HOST_NOT_FOUND"
    INFERENCE_HOST_NAME_TAKEN = "INFERENCE_HOST_NAME_TAKEN"
    INFERENCE_HOST_DEACTIVATED = "INFERENCE_HOST_DEACTIVATED"
    INFERENCE_HOST_HAS_BACKENDS = "INFERENCE_HOST_HAS_BACKENDS"
    INFERENCE_BACKEND_NOT_FOUND = "INFERENCE_BACKEND_NOT_FOUND"
    INFERENCE_BACKEND_DEACTIVATED = "INFERENCE_BACKEND_DEACTIVATED"
    INFERENCE_BACKEND_ENDPOINT_TAKEN = "INFERENCE_BACKEND_ENDPOINT_TAKEN"
    INFERENCE_BACKEND_HAS_CAMERAS = "INFERENCE_BACKEND_HAS_CAMERAS"
    STATION_NOT_FOUND = "STATION_NOT_FOUND"
    STATION_CODE_TAKEN = "STATION_CODE_TAKEN"
    STATION_DEACTIVATED = "STATION_DEACTIVATED"
    STATION_HAS_CAMERAS = "STATION_HAS_CAMERAS"
    CAMERA_NOT_FOUND = "CAMERA_NOT_FOUND"
    CAMERA_HOST_BACKEND_MISMATCH = "CAMERA_HOST_BACKEND_MISMATCH"
    CAMERA_STATION_HOST_CONFLICT = "CAMERA_STATION_HOST_CONFLICT"
    CAMERA_STATION_TEMPLATE_CONFLICT = "CAMERA_STATION_TEMPLATE_CONFLICT"
    STALE_REVISION = "STALE_REVISION"


@dataclass(frozen=True, slots=True)
class DeviceFieldError:
    """拓扑拒绝所涉及的一个输入字段。"""

    field: str
    message: str


_DEFAULT_FIELD_ERRORS: dict[DeviceRefusalCode, tuple[DeviceFieldError, ...]] = {
    DeviceRefusalCode.CAMERA_HOST_BACKEND_MISMATCH: (
        DeviceFieldError("host_id", "必须与推理后端所属推理机一致"),
        DeviceFieldError("backend_id", "必须属于所选推理机"),
    ),
    DeviceRefusalCode.CAMERA_STATION_HOST_CONFLICT: (
        DeviceFieldError("host_id", "同一工位的相机必须属于同一推理机"),
    ),
    DeviceRefusalCode.CAMERA_STATION_TEMPLATE_CONFLICT: (
        DeviceFieldError("backend_id", "该推理后端的模板必须与工位已有相机一致"),
    ),
}


class DeviceRefusedError(Exception):
    """`device` 拒绝了操作，并可标出有问题的字段。"""

    def __init__(
        self,
        code: DeviceRefusalCode,
        *,
        field_errors: tuple[DeviceFieldError, ...] | None = None,
    ) -> None:
        super().__init__(code.value)
        self.code = code
        self.field_errors = (
            _DEFAULT_FIELD_ERRORS.get(code, ()) if field_errors is None else field_errors
        )


def refusal_problem(code: DeviceRefusalCode) -> tuple[int, str]:
    """返回设备拒绝对应的 HTTP 状态和简体中文标题。"""
    match code:
        case DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND:
            return 404, "推理机不存在"
        case DeviceRefusalCode.INFERENCE_HOST_NAME_TAKEN:
            return 409, "已存在同名推理机"
        case DeviceRefusalCode.INFERENCE_HOST_DEACTIVATED:
            return 409, "推理机已停用，恢复后才能继续"
        case DeviceRefusalCode.INFERENCE_HOST_HAS_BACKENDS:
            return 409, "该推理机仍承载推理后端，请先删除它们"
        case DeviceRefusalCode.INFERENCE_BACKEND_NOT_FOUND:
            return 404, "推理后端不存在"
        case DeviceRefusalCode.INFERENCE_BACKEND_DEACTIVATED:
            return 409, "推理后端已停用，恢复后才能继续"
        case DeviceRefusalCode.INFERENCE_BACKEND_ENDPOINT_TAKEN:
            return 409, "该推理机上已存在同地址的推理后端"
        case DeviceRefusalCode.INFERENCE_BACKEND_HAS_CAMERAS:
            return 409, "该推理后端仍被相机引用"
        case DeviceRefusalCode.STATION_NOT_FOUND:
            return 404, "工位不存在"
        case DeviceRefusalCode.STATION_CODE_TAKEN:
            return 409, "工位编码已被占用"
        case DeviceRefusalCode.STATION_DEACTIVATED:
            return 409, "工位已停用，恢复后才能继续"
        case DeviceRefusalCode.STATION_HAS_CAMERAS:
            return 409, "该工位仍有关联相机"
        case DeviceRefusalCode.CAMERA_NOT_FOUND:
            return 404, "相机不存在"
        case DeviceRefusalCode.CAMERA_HOST_BACKEND_MISMATCH:
            return 409, "相机的推理机与推理后端归属不一致"
        case DeviceRefusalCode.CAMERA_STATION_HOST_CONFLICT:
            return 409, "同一工位的相机不能跨推理机"
        case DeviceRefusalCode.CAMERA_STATION_TEMPLATE_CONFLICT:
            return 409, "同一工位的相机不能使用不同模板"
        case DeviceRefusalCode.STALE_REVISION:
            return 409, "内容已被他人修改，请刷新后重试"
        case _:
            assert_never(code)
