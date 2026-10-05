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
    # 建立请求时工位/旧机/目标机引用不存在的真实记录，或该工位没有当前执行权归属。
    HANDOVER_TARGET_NOT_FOUND = "HANDOVER_TARGET_NOT_FOUND"
    # 请求的旧机不是该工位当前的物理执行权持有者；关系在建立与第二确认时都复核。
    HANDOVER_SOURCE_MISMATCH = "HANDOVER_SOURCE_MISMATCH"
    # 旧机与目标机不能是同一台推理机。
    HANDOVER_SAME_HOST = "HANDOVER_SAME_HOST"


class ExecutionRefusedError(Exception):
    """execution 拒绝一次授权变更。"""

    def __init__(self, code: ExecutionRefusalCode) -> None:
        super().__init__(code.value)
        self.code = code


def refusal_problem(code: ExecutionRefusalCode) -> tuple[int, str]:
    """返回执行权拒绝对应的 HTTP 状态和简体中文标题。"""
    match code:
        case ExecutionRefusalCode.ACTIVE_GRANT_EXISTS:
            return 409, "该工位仍持有未到期的物理执行权租约"
        case ExecutionRefusalCode.GRANT_NOT_RENEWABLE:
            return 409, "该物理执行权租约已不可续期"
        case ExecutionRefusalCode.HANDOVER_NOT_FOUND:
            return 404, "强制改绑请求不存在"
        case ExecutionRefusalCode.HANDOVER_ALREADY_CONFIRMED:
            return 409, "该强制改绑请求已完成两人确认"
        case ExecutionRefusalCode.HANDOVER_SAME_OPERATOR:
            return 409, "必须由另一名具备强制改绑权限的用户确认"
        case ExecutionRefusalCode.HANDOVER_CONTENT_MISMATCH:
            return 409, "确认内容与请求记录不一致，请重新读取后再确认"
        case ExecutionRefusalCode.HANDOVER_RISK_NOT_ACKNOWLEDGED:
            return 422, "必须确认已阅读强制改绑风险原文"
        case ExecutionRefusalCode.HANDOVER_NOT_ELIGIBLE:
            return 403, "当前账户不具备强制改绑权限"
        case ExecutionRefusalCode.HANDOVER_TARGET_NOT_FOUND:
            return 404, "工位、旧机或目标机不存在，或该工位没有当前执行权归属"
        case ExecutionRefusalCode.HANDOVER_SOURCE_MISMATCH:
            return 409, "请求的旧机不是该工位当前的物理执行权持有者"
        case ExecutionRefusalCode.HANDOVER_SAME_HOST:
            return 422, "旧机与目标机不能是同一台推理机"


__all__ = ["ExecutionRefusalCode", "ExecutionRefusedError", "refusal_problem"]
