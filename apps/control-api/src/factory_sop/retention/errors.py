"""retention 拒绝的稳定错误码与 `problem+json` 映射。"""

from __future__ import annotations

from enum import StrEnum
from typing import assert_never


class RetentionRefusalCode(StrEnum):
    """`retention` 产生的稳定 `error_code` 值。

    工位不存在/已停用沿用与其他模块相同的线格式：含义相同，客户端按同一个码处理；保留策略冲突与
    不合规则由 retention 自己命名，避免把独立覆盖记录的 CAS 冲突伪装成设备工位修订冲突。
    """

    STATION_NOT_FOUND = "STATION_NOT_FOUND"
    STATION_DEACTIVATED = "STATION_DEACTIVATED"
    POLICY_CONFLICT = "RETENTION_POLICY_CONFLICT"
    POLICY_INVALID = "RETENTION_POLICY_INVALID"


class RetentionRefusedError(Exception):
    """`retention` 拒绝了操作。"""

    def __init__(self, code: RetentionRefusalCode) -> None:
        super().__init__(code.value)
        self.code = code


def refusal_problem(code: RetentionRefusalCode) -> tuple[int, str]:
    """返回保留拒绝对应的 HTTP 状态和简体中文标题。"""
    match code:
        case RetentionRefusalCode.STATION_NOT_FOUND:
            return 404, "工位不存在"
        case RetentionRefusalCode.STATION_DEACTIVATED:
            return 409, "工位已停用，恢复后才能继续"
        case RetentionRefusalCode.POLICY_CONFLICT:
            return 409, "保留策略已被他人修改，请刷新后重试"
        case RetentionRefusalCode.POLICY_INVALID:
            return 422, "保留策略必须是完整且有效的一组"
        case _:
            assert_never(code)


__all__ = ["RetentionRefusalCode", "RetentionRefusedError", "refusal_problem"]
