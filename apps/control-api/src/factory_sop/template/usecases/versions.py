"""模板版本发布、历史读取和制品下载用例。"""

from __future__ import annotations

import math
from datetime import datetime
from typing import NoReturn, assert_never
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.identifiers import new_id
from factory_sop.observability import get_logger
from factory_sop.template.artifacts import build_template_artifacts
from factory_sop.template.errors import (
    TemplateFieldError,
    TemplateRefusalCode,
    TemplateRefusedError,
)
from factory_sop.template.model import (
    TemplateArtifactName,
    TemplateSignal,
    TemplateSignalKind,
    TemplateVersion,
    TemplateVersionArtifact,
    TemplateVersionWriteResult,
    action_description_number,
)
from factory_sop.template.repository import (
    TemplateVersionPublishRepository,
    TemplateVersionRepository,
)

_logger = get_logger("template")


def publish_template_version(
    *,
    draft_id: UUID,
    expected_revision: int,
    caller: Caller,
    now: datetime,
    templates: TemplateVersionPublishRepository,
) -> TemplateVersionWriteResult:
    """校验指定草稿修订并原子保存不可变版本，重复请求返回原版本。"""
    authorize(caller, Permission.TEMPLATE_DRAFT_EDIT)

    replay = templates.version_by_source(draft_id=draft_id, revision=expected_revision)
    if replay is not None:
        _logger.info(
            "template.version.publish.replayed",
            version_id=str(replay.id),
            draft_id=str(draft_id),
            source_revision=str(expected_revision),
            actor_id=str(caller.user.id),
            sha256=replay.sha256,
        )
        return TemplateVersionWriteResult(version=replay, created=False)

    document = templates.draft_for_publish(draft_id)
    if document is None:
        _refuse(TemplateRefusalCode.DRAFT_NOT_FOUND, draft_id=str(draft_id))
    if document.draft.revision != expected_revision:
        _refuse(
            TemplateRefusalCode.STALE_REVISION,
            draft_id=str(draft_id),
            expected_revision=str(expected_revision),
            actual_revision=str(document.draft.revision),
        )

    try:
        draft = document.draft
        errors: list[TemplateFieldError] = []
        numbers = tuple(step.number for step in draft.steps)
        if not numbers:
            errors.append(TemplateFieldError("草稿", None, "steps", "至少需要一个步骤"))
        if numbers != tuple(range(1, len(numbers) + 1)):
            errors.append(TemplateFieldError("草稿", None, "steps", "步骤号必须从 1 开始连续排列"))
        for position, step in enumerate(draft.steps):
            encoded_number = action_description_number(step.description)
            if not step.name.strip():
                errors.append(TemplateFieldError("草稿", position + 1, "步骤名称", "必填"))
            if not step.description.strip() or encoded_number is None:
                errors.append(
                    TemplateFieldError("草稿", position + 1, "步骤描述", "必须符合基座动作编码格式")
                )
            elif encoded_number != step.number:
                errors.append(
                    TemplateFieldError(
                        "草稿", position + 1, "步骤描述", "编码中的步骤号必须与步骤号一致"
                    )
                )

        boundary = draft.boundary
        if boundary is None or boundary.start_signal is None:
            errors.append(TemplateFieldError("草稿", None, "start_signal", "必须声明开始信号"))
        if boundary is None or boundary.end_signals is None:
            errors.append(
                TemplateFieldError(
                    "草稿", None, "end_signals", "必须明确声明结束信号列表，可以为空"
                )
            )
        if boundary is not None:
            if boundary.start_signal is not None:
                _validate_signal_reference(
                    boundary.start_signal,
                    numbers=numbers,
                    field="start_signal",
                    errors=errors,
                )
            if boundary.end_signals is not None:
                seen: set[tuple[TemplateSignalKind, int | str]] = set()
                for index, signal in enumerate(boundary.end_signals):
                    _validate_signal_reference(
                        signal,
                        numbers=numbers,
                        field=f"end_signals[{index}]",
                        errors=errors,
                    )
                    identity = (signal.kind, signal.value)
                    if identity in seen:
                        errors.append(
                            TemplateFieldError(
                                "草稿", None, f"end_signals[{index}]", "结束信号不能重复"
                            )
                        )
                    seen.add(identity)

        runtime = draft.runtime_defaults
        if runtime.idle_timeout_seconds is None:
            errors.append(TemplateFieldError("草稿", None, "idle_timeout_seconds", "必须填写"))
        if runtime.step_deadline_seconds is None:
            errors.append(TemplateFieldError("草稿", None, "step_deadline_seconds", "必须填写"))
        if runtime.disposition_policy is None or not runtime.disposition_policy.strip():
            errors.append(TemplateFieldError("草稿", None, "disposition_policy", "必须填写"))
        for field, value in (
            ("idle_timeout_seconds", runtime.idle_timeout_seconds),
            ("step_deadline_seconds", runtime.step_deadline_seconds),
        ):
            if value is not None and (not math.isfinite(value) or value <= 0):
                errors.append(TemplateFieldError("草稿", None, field, "必须是有限且大于零的数字"))

        if errors:
            raise TemplateRefusedError(
                TemplateRefusalCode.VERSION_INVALID, field_errors=tuple(errors)
            )
    except TemplateRefusedError as error:
        _logger.info(
            "template.version.publish.rejected",
            error_code=error.code.value,
            draft_id=str(draft_id),
            source_revision=str(expected_revision),
            actor_id=str(caller.user.id),
        )
        raise

    assert document.draft.boundary is not None
    built = build_template_artifacts(
        steps=document.draft.steps,
        ordering=document.draft.ordering,
        boundary=document.draft.boundary,
        runtime_defaults=document.draft.runtime_defaults,
    )
    version = TemplateVersion(
        id=new_id(),
        template_id=document.draft.template_id,
        source_import_id=document.draft.source_import_id,
        source_draft_id=document.draft.id,
        source_draft_revision=document.draft.revision,
        steps=document.draft.steps,
        ordering=document.draft.ordering,
        boundary=document.draft.boundary,
        runtime_defaults=document.draft.runtime_defaults,
        artifacts=built.artifacts,
        sha256=built.sha256,
        published_by=caller.user.id,
        published_at=now,
    )
    result = templates.add_version(version)
    if not result.created:
        _logger.info(
            "template.version.publish.replayed",
            version_id=str(result.version.id),
            draft_id=str(draft_id),
            source_revision=str(expected_revision),
            actor_id=str(caller.user.id),
            sha256=result.version.sha256,
        )
    else:
        _logger.info(
            "template.version.publish.succeeded",
            version_id=str(result.version.id),
            draft_id=str(draft_id),
            source_revision=str(expected_revision),
            actor_id=str(caller.user.id),
            sha256=result.version.sha256,
        )
    return result


def list_template_versions(
    *,
    caller: Caller,
    templates: TemplateVersionRepository,
    page: int,
    page_size: int,
) -> tuple[list[TemplateVersion], int]:
    """按发布时间倒序读取模板版本历史。"""
    authorize(caller, Permission.TEMPLATE_DRAFT_VIEW)
    versions, total = templates.page_versions(page=page, page_size=page_size)
    _logger.info(
        "template.version.list.succeeded",
        page=page,
        page_size=page_size,
        total=total,
        actor_id=str(caller.user.id),
    )
    return versions, total


def read_template_version(
    *,
    version_id: UUID,
    caller: Caller,
    templates: TemplateVersionRepository,
) -> TemplateVersion:
    """读取一个不可变版本及其完整制品元数据。"""
    authorize(caller, Permission.TEMPLATE_DRAFT_VIEW)
    version = templates.version_by_id(version_id)
    if version is None:
        _refuse(
            TemplateRefusalCode.VERSION_NOT_FOUND,
            event="template.version.read.rejected",
            version_id=str(version_id),
        )
    _logger.info(
        "template.version.read.succeeded",
        version_id=str(version.id),
        sha256=version.sha256,
        actor_id=str(caller.user.id),
    )
    return version


def download_template_version_artifact(
    *,
    version_id: UUID,
    name: TemplateArtifactName,
    caller: Caller,
    templates: TemplateVersionRepository,
) -> TemplateVersionArtifact:
    """按版本身份和固定制品名称返回发布时保存的字节。"""
    authorize(caller, Permission.TEMPLATE_DRAFT_VIEW)
    version = templates.version_by_id(version_id)
    if version is None:
        _refuse(
            TemplateRefusalCode.VERSION_NOT_FOUND,
            event="template.version.artifact.download.rejected",
            version_id=str(version_id),
        )
    artifact = templates.artifact_by_name(version_id=version_id, name=name)
    if artifact is None:
        _refuse(
            TemplateRefusalCode.VERSION_ARTIFACT_NOT_FOUND,
            event="template.version.artifact.download.rejected",
            version_id=str(version_id),
            artifact=name.value,
        )
    _logger.info(
        "template.version.artifact.download.succeeded",
        version_id=str(version.id),
        artifact=artifact.name.value,
        byte_length=artifact.byte_length,
        sha256=artifact.sha256,
        actor_id=str(caller.user.id),
    )
    return artifact


def _validate_signal_reference(
    signal: TemplateSignal,
    *,
    numbers: tuple[int, ...],
    field: str,
    errors: list[TemplateFieldError],
) -> None:
    kind = signal.kind
    value = signal.value
    match kind:
        case TemplateSignalKind.ACTION:
            if value not in numbers:
                errors.append(
                    TemplateFieldError("草稿", None, field, "动作边界信号必须引用现有步骤")
                )
        case TemplateSignalKind.EXTERNAL:
            if not isinstance(value, str) or not value.strip():
                errors.append(TemplateFieldError("草稿", None, field, "外部信号语义标签不能为空"))
        case _:
            assert_never(kind)


def _refuse(
    code: TemplateRefusalCode,
    *,
    event: str = "template.version.publish.rejected",
    **context: str,
) -> NoReturn:
    _logger.info(event, error_code=code.value, **context)
    raise TemplateRefusedError(code)
