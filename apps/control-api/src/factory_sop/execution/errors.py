"""execution 用例的稳定拒绝原因。"""

from __future__ import annotations

from enum import StrEnum


class ExecutionRefusalCode(StrEnum):
    """租约操作无法满足中心排他不变量的原因。"""

    ACTIVE_GRANT_EXISTS = "ACTIVE_GRANT_EXISTS"
    GRANT_NOT_RENEWABLE = "GRANT_NOT_RENEWABLE"
    HANDOVER_NOT_FOUND = "HANDOVER_NOT_FOUND"
    HANDOVER_ALREADY_CONFIRMED = "HANDOVER_ALREADY_CONFIRMED"
    HANDOVER_SAME_OPERATOR = "HANDOVER_SAME_OPERATOR"
    HANDOVER_CONTENT_MISMATCH = "HANDOVER_CONTENT_MISMATCH"
    HANDOVER_RISK_NOT_ACKNOWLEDGED = "HANDOVER_RISK_NOT_ACKNOWLEDGED"
    HANDOVER_NOT_ELIGIBLE = "HANDOVER_NOT_ELIGIBLE"


class ExecutionRefusedError(Exception):
    """execution 拒绝一次授权变更。"""

    def __init__(self, code: ExecutionRefusalCode) -> None:
        super().__init__(code.value)
        self.code = code


__all__ = ["ExecutionRefusalCode", "ExecutionRefusedError"]
