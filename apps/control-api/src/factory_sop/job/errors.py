"""`job` 查询的稳定错误。"""

from __future__ import annotations

from enum import StrEnum
from typing import NoReturn


class JobRefusalCode(StrEnum):
    """异步任务查询拒绝码。"""

    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_RESOURCE_NOT_FOUND = "JOB_RESOURCE_NOT_FOUND"


class JobRefusedError(Exception):
    """任务不存在或关联资源已经不可读。"""

    def __init__(self, code: JobRefusalCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def refusal_problem(code: JobRefusalCode) -> tuple[int, str]:
    """返回任务查询的 HTTP 问题状态。"""
    match code:
        case JobRefusalCode.JOB_NOT_FOUND | JobRefusalCode.JOB_RESOURCE_NOT_FOUND:
            return 404, "异步任务不存在"
        case _:
            return _unexpected(code)


def _unexpected(code: JobRefusalCode) -> NoReturn:
    raise AssertionError(f"未处理的 job 错误码：{code}")
