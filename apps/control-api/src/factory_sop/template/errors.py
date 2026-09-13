"""`template` 的错误、字段定位和 HTTP 适配所需的稳定代码。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never


@dataclass(frozen=True, slots=True)
class TemplateFieldError:
    """一个可定位到工作表、行和字段的模板输入错误。"""

    sheet: str
    row: int | None
    field: str
    message: str

    def to_wire(self) -> dict[str, object]:
        return {"sheet": self.sheet, "row": self.row, "field": self.field, "message": self.message}

    @classmethod
    def from_wire(cls, value: dict[str, object]) -> TemplateFieldError:
        sheet = value.get("sheet")
        row = value.get("row")
        field = value.get("field")
        message = value.get("message")
        if not isinstance(sheet, str) or not isinstance(field, str) or not isinstance(message, str):
            raise ValueError("模板字段错误结构无效")
        if row is not None and (not isinstance(row, int) or isinstance(row, bool)):
            raise ValueError("模板字段错误行号类型无效")
        return cls(sheet=sheet, row=row, field=field, message=message)


class TemplateRefusalCode(StrEnum):
    """`template` 产生的稳定 `error_code` 值。"""

    IMPORT_INVALID = "TEMPLATE_IMPORT_INVALID"
    IMPORT_NOT_FOUND = "TEMPLATE_IMPORT_NOT_FOUND"
    DRAFT_NOT_FOUND = "TEMPLATE_DRAFT_NOT_FOUND"
    DRAFT_INVALID = "TEMPLATE_DRAFT_INVALID"
    VERSION_NOT_FOUND = "TEMPLATE_VERSION_NOT_FOUND"
    VERSION_INVALID = "TEMPLATE_VERSION_INVALID"
    VERSION_ARTIFACT_NOT_FOUND = "TEMPLATE_VERSION_ARTIFACT_NOT_FOUND"
    VERSION_STATION_MISMATCH = "TEMPLATE_VERSION_STATION_MISMATCH"
    BINDING_INVALID = "TEMPLATE_BINDING_INVALID"
    BINDING_NOT_FOUND = "TEMPLATE_BINDING_NOT_FOUND"
    BINDING_STATION_TAKEN = "TEMPLATE_BINDING_STATION_TAKEN"
    REPORT_HOST_NOT_ALLOWED = "TEMPLATE_REPORT_HOST_NOT_ALLOWED"
    REPORT_STATION_UNBOUND = "TEMPLATE_REPORT_STATION_UNBOUND"
    REPORT_CONFLICT = "TEMPLATE_REPORT_CONFLICT"
    STALE_REVISION = "STALE_REVISION"


class TemplateRefusedError(Exception):
    """草稿读写被拒绝，并携带可能需要展示的字段错误。"""

    def __init__(
        self,
        code: TemplateRefusalCode,
        *,
        field_errors: tuple[TemplateFieldError, ...] = (),
    ) -> None:
        super().__init__(code.value)
        self.code = code
        self.field_errors = field_errors


def refusal_problem(code: TemplateRefusalCode) -> tuple[int, str]:
    """返回模板拒绝对应的 HTTP 状态和简体中文标题。"""
    match code:
        case TemplateRefusalCode.IMPORT_INVALID:
            return 422, "Excel 工作簿校验失败"
        case TemplateRefusalCode.IMPORT_NOT_FOUND:
            return 404, "导入记录不存在"
        case TemplateRefusalCode.DRAFT_NOT_FOUND:
            return 404, "模板草稿不存在"
        case TemplateRefusalCode.DRAFT_INVALID:
            return 422, "模板草稿内容不符合规范"
        case TemplateRefusalCode.VERSION_NOT_FOUND:
            return 404, "模板版本不存在"
        case TemplateRefusalCode.VERSION_INVALID:
            return 422, "模板版本发布校验失败"
        case TemplateRefusalCode.VERSION_ARTIFACT_NOT_FOUND:
            return 404, "模板版本制品不存在"
        case TemplateRefusalCode.VERSION_STATION_MISMATCH:
            return 409, "模板版本不属于目标工位"
        case TemplateRefusalCode.BINDING_INVALID:
            return 422, "模板绑定前置条件不满足"
        case TemplateRefusalCode.BINDING_NOT_FOUND:
            return 404, "模板工位绑定不存在"
        case TemplateRefusalCode.BINDING_STATION_TAKEN:
            return 409, "该工位已有模板绑定"
        case TemplateRefusalCode.REPORT_HOST_NOT_ALLOWED:
            return 403, "推理机不拥有该工位后端"
        case TemplateRefusalCode.REPORT_STATION_UNBOUND:
            return 409, "工位尚未绑定模板版本"
        case TemplateRefusalCode.REPORT_CONFLICT:
            return 409, "配置确认与较新的现场事实冲突"
        case TemplateRefusalCode.STALE_REVISION:
            return 409, "模板草稿已被他人修改，请刷新后重试"
        case _:
            assert_never(code)
