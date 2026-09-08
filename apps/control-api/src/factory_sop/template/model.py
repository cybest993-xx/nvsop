"""`template` 持有的草稿、步骤和规范化文档导入记录。"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from factory_sop.template.errors import TemplateFieldError

_ACTION_PATTERN = re.compile(r"^\((\d+)\).+")


def action_description_number(value: str) -> int | None:
    """返回基座动作描述中的步骤号；格式不符时返回 `None`。"""
    match = _ACTION_PATTERN.match(value)
    return int(match.group(1)) if match is not None else None


def _duration_from_wire(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("运行参数时限类型无效")
    return float(value)


class OrderingMode(StrEnum):
    """模板是否要求动作严格按步骤顺序出现。"""

    STRICT = "strict"
    UNORDERED = "unordered"


class ImportStatus(StrEnum):
    """一次规范化文档导入是否创建了草稿。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TemplateRuntimeDefaults:
    """模板版本可继承的三项运行参数默认值；草稿阶段允许尚未填写。"""

    idle_timeout_seconds: float | None = None
    step_deadline_seconds: float | None = None
    disposition_policy: str | None = None

    def to_wire(self) -> dict[str, object]:
        return {
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "step_deadline_seconds": self.step_deadline_seconds,
            "disposition_policy": self.disposition_policy,
        }

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> TemplateRuntimeDefaults:
        allowed = {"idle_timeout_seconds", "step_deadline_seconds", "disposition_policy"}
        if set(value) - allowed:
            raise ValueError("运行参数包含不支持的字段")
        idle_duration = _duration_from_wire(value.get("idle_timeout_seconds"))
        deadline_duration = _duration_from_wire(value.get("step_deadline_seconds"))
        policy = value.get("disposition_policy")
        if policy is not None and not isinstance(policy, str):
            raise ValueError("处置策略类型无效")
        return cls(
            idle_timeout_seconds=idle_duration,
            step_deadline_seconds=deadline_duration,
            disposition_policy=policy,
        )

    def __post_init__(self) -> None:
        for value in (self.idle_timeout_seconds, self.step_deadline_seconds):
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError("运行参数时限必须是有限且大于零的数字")
        if self.disposition_policy is not None and not self.disposition_policy.strip():
            raise ValueError("处置策略不能为空")


@dataclass(frozen=True, slots=True)
class TemplateStep:
    """一个草稿步骤，`description` 保留基座要求的动作编码。"""

    number: int
    name: str
    description: str

    def to_wire(self) -> dict[str, object]:
        return {"number": self.number, "name": self.name, "description": self.description}

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> TemplateStep:
        number = value.get("number")
        name = value.get("name")
        description = value.get("description")
        if (
            not isinstance(number, int)
            or isinstance(number, bool)
            or not isinstance(name, str)
            or not isinstance(description, str)
        ):
            raise ValueError("步骤结构无效")
        return cls(number=number, name=name, description=description)

    def __post_init__(self) -> None:
        if self.number <= 0:
            raise ValueError("步骤号必须大于零")
        if not self.name.strip():
            raise ValueError("步骤名称不能为空")
        if not self.description.strip():
            raise ValueError("步骤描述不能为空")
        encoded_number = action_description_number(self.description)
        if encoded_number is None:
            raise ValueError("步骤描述必须符合基座动作编码格式")
        if encoded_number != self.number:
            raise ValueError("步骤描述中的步骤号必须与步骤号一致")


@dataclass(frozen=True, slots=True)
class SopTemplate:
    """一个由规范化文档导入出的 SOP 模板身份。"""

    id: UUID
    station_id: UUID
    station_code: str
    station_name: str
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TemplateDraft:
    """可编辑的模板权威记录；发布前可继续替换整组内容。"""

    id: UUID
    template_id: UUID
    source_import_id: UUID
    steps: tuple[TemplateStep, ...]
    ordering: OrderingMode
    runtime_defaults: TemplateRuntimeDefaults
    revision: int
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TemplateDraftDocument:
    """草稿及其模板身份的一个读取快照。"""

    template: SopTemplate
    draft: TemplateDraft


@dataclass(frozen=True, slots=True)
class TemplateImport:
    """保留原始 Excel 字节和导入结果，供追溯和失败排障。"""

    id: UUID
    filename: str
    content_type: str
    original_document: bytes
    sha256: str
    status: ImportStatus
    errors: tuple[TemplateFieldError, ...]
    imported_by: UUID
    imported_at: datetime
