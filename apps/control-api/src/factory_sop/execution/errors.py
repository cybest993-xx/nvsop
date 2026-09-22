"""execution 用例的稳定拒绝原因。"""

from __future__ import annotations

from enum import StrEnum


class ExecutionRefusalCode(StrEnum):
    """租约操作无法满足中心排他不变量的原因。"""

    ACTIVE_GRANT_EXISTS = "ACTIVE_GRANT_EXISTS"
    GRANT_NOT_RENEWABLE = "GRANT_NOT_RENEWABLE"


class ExecutionRefusedError(Exception):
    """execution 拒绝一次授权变更。"""

    def __init__(self, code: ExecutionRefusalCode) -> None:
        super().__init__(code.value)
        self.code = code


__all__ = ["ExecutionRefusalCode", "ExecutionRefusedError"]
