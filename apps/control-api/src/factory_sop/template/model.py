"""`template` 持有的草稿、步骤和规范化文档导入记录。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import assert_never
from uuid import UUID

from factory_sop.template.errors import TemplateFieldError

_ACTION_PATTERN = re.compile(r"^\((\d+)\).+")


def action_description_number(value: str) -> int | None:
    """返回基座动作描述中的步骤号；格式不符时返回 `None`。"""
    match = _ACTION_PATTERN.match(value)
    return int(match.group(1)) if match is not None else None


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label}结构无效")
    return value


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


class TemplateSignalKind(StrEnum):
    """声明式边界信号的种类。"""

    ACTION = "action"
    EXTERNAL = "external"


@dataclass(frozen=True, slots=True)
class TemplateSignal:
    """一个动作编号或外部信号语义标签。"""

    kind: TemplateSignalKind
    value: int | str

    def __post_init__(self) -> None:
        match self.kind:
            case TemplateSignalKind.ACTION:
                if (
                    isinstance(self.value, bool)
                    or not isinstance(self.value, int)
                    or self.value <= 0
                ):
                    raise ValueError("动作边界信号必须是正整数")
            case TemplateSignalKind.EXTERNAL:
                if not isinstance(self.value, str) or not self.value.strip():
                    raise ValueError("外部边界信号语义标签不能为空")
            case _:
                assert_never(self.kind)

    def to_wire(self) -> dict[str, object]:
        match self.kind:
            case TemplateSignalKind.ACTION:
                return {"kind": self.kind.value, "action_number": self.value}
            case TemplateSignalKind.EXTERNAL:
                return {"kind": self.kind.value, "semantic_label": self.value}
            case _:
                assert_never(self.kind)

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> TemplateSignal:
        kind_value = value.get("kind")
        if not isinstance(kind_value, str):
            raise ValueError("边界信号种类无效")
        try:
            kind = TemplateSignalKind(kind_value)
        except ValueError as error:
            raise ValueError("边界信号种类无效") from error
        match kind:
            case TemplateSignalKind.ACTION:
                if set(value) != {"kind", "action_number"}:
                    raise ValueError("动作边界信号字段无效")
                action_number = value["action_number"]
                if isinstance(action_number, bool) or not isinstance(action_number, int):
                    raise ValueError("动作边界信号必须是整数")
                return cls(kind=kind, value=action_number)
            case TemplateSignalKind.EXTERNAL:
                if set(value) != {"kind", "semantic_label"}:
                    raise ValueError("外部边界信号字段无效")
                semantic_label = value["semantic_label"]
                if not isinstance(semantic_label, str):
                    raise ValueError("外部边界信号语义标签必须是字符串")
                return cls(kind=kind, value=semantic_label.strip())
            case _:
                assert_never(kind)


@dataclass(frozen=True, slots=True)
class TemplateBoundaryDraft:
    """草稿的声明式边界；空值表示尚未完成声明，空元组表示明确没有结束信号。"""

    start_signal: TemplateSignal | None
    end_signals: tuple[TemplateSignal, ...] | None

    def to_wire(self) -> dict[str, object]:
        return {
            "start_signal": self.start_signal.to_wire() if self.start_signal is not None else None,
            "end_signals": (
                None
                if self.end_signals is None
                else [signal.to_wire() for signal in self.end_signals]
            ),
        }

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> TemplateBoundaryDraft:
        if set(value) != {"start_signal", "end_signals"}:
            raise ValueError("模板边界字段无效")
        start = value["start_signal"]
        end = value["end_signals"]
        start_signal = (
            None if start is None else TemplateSignal.from_wire(_mapping(start, "开始信号"))
        )
        end_signals: tuple[TemplateSignal, ...] | None
        if end is None:
            end_signals = None
        elif isinstance(end, list):
            end_signals = tuple(
                TemplateSignal.from_wire(_mapping(item, "结束信号")) for item in end
            )
        else:
            raise ValueError("结束信号列表结构无效")
        return cls(start_signal=start_signal, end_signals=end_signals)


@dataclass(frozen=True, slots=True)
class KeepTemplateBoundary:
    """表示编辑请求未提供边界字段，应保留当前声明。"""


@dataclass(frozen=True, slots=True)
class KeepTemplateBoundaryPart:
    """表示编辑请求未提供边界字段，应保留该字段。"""


TemplateBoundaryPart = TemplateSignal | None | KeepTemplateBoundaryPart


@dataclass(frozen=True, slots=True)
class PatchTemplateBoundary:
    """表示编辑请求只替换已提交的边界字段。"""

    start_signal: TemplateBoundaryPart = KeepTemplateBoundaryPart()
    end_signals: tuple[TemplateSignal, ...] | KeepTemplateBoundaryPart | None = (
        KeepTemplateBoundaryPart()
    )


TemplateBoundaryUpdate = KeepTemplateBoundary | PatchTemplateBoundary
KEEP_TEMPLATE_BOUNDARY = KeepTemplateBoundary()


class TemplateArtifactName(StrEnum):
    """模板版本允许下载的确定性制品名称。"""

    ACTIONS = "actions.json"
    VLM_PROMPTS = "vlm_prompts.txt"
    TEMPLATE = "template.json"
    MANIFEST = "manifest.json"


TEMPLATE_ARTIFACT_FORMAT_VERSION = 1
TEMPLATE_ARTIFACT_ORDER: tuple[TemplateArtifactName, ...] = (
    TemplateArtifactName.ACTIONS,
    TemplateArtifactName.VLM_PROMPTS,
    TemplateArtifactName.TEMPLATE,
    TemplateArtifactName.MANIFEST,
)


@dataclass(frozen=True, slots=True)
class TemplateVersionArtifact:
    """模板版本中的一个已保存制品。"""

    name: TemplateArtifactName
    media_type: str
    content: bytes
    sha256: str

    def __post_init__(self) -> None:
        if hashlib.sha256(self.content).hexdigest() != self.sha256:
            raise ValueError("制品摘要与内容不一致")

    @property
    def byte_length(self) -> int:
        return len(self.content)


@dataclass(frozen=True, slots=True)
class TemplateVersion:
    """发布后不可变的模板版本及其可复验制品。"""

    id: UUID
    template_id: UUID
    source_import_id: UUID
    source_draft_id: UUID
    source_draft_revision: int
    steps: tuple[TemplateStep, ...]
    ordering: OrderingMode
    boundary: TemplateBoundaryDraft
    runtime_defaults: TemplateRuntimeDefaults
    artifacts: tuple[TemplateVersionArtifact, ...]
    sha256: str
    published_by: UUID
    published_at: datetime

    def __post_init__(self) -> None:
        if self.published_at.tzinfo is None or self.published_at.utcoffset() != timedelta(0):
            raise ValueError("发布时间必须是 UTC")
        if tuple(artifact.name for artifact in self.artifacts) != TEMPLATE_ARTIFACT_ORDER:
            raise ValueError("版本制品集合或顺序无效")
        manifest = self.artifacts[-1]
        if hashlib.sha256(manifest.content).hexdigest() != self.sha256:
            raise ValueError("版本摘要与清单内容不一致")
        try:
            manifest_document = json.loads(manifest.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("版本清单内容无效") from error
        expected_artifacts = [
            {
                "byte_length": artifact.byte_length,
                "media_type": artifact.media_type,
                "name": artifact.name.value,
                "sha256": artifact.sha256,
            }
            for artifact in self.artifacts[:-1]
        ]
        if manifest_document != {
            "artifacts": expected_artifacts,
            "format_version": TEMPLATE_ARTIFACT_FORMAT_VERSION,
        }:
            raise ValueError("版本清单与制品不一致")


@dataclass(frozen=True, slots=True)
class TemplateVersionWriteResult:
    """版本写入结果；重复发布返回已有版本且 `created` 为假。"""

    version: TemplateVersion
    created: bool


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
    boundary: TemplateBoundaryDraft | None = None


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
