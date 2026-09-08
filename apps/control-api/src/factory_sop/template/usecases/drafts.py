"""模板草稿导入、读取和编辑用例。"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import NoReturn
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.device.api import StationCodeLookup, station_by_code
from factory_sop.identifiers import new_id
from factory_sop.observability import get_logger
from factory_sop.template.errors import (
    TemplateFieldError,
    TemplateRefusalCode,
    TemplateRefusedError,
)
from factory_sop.template.model import (
    KEEP_TEMPLATE_BOUNDARY,
    ImportStatus,
    KeepTemplateBoundaryPart,
    OrderingMode,
    PatchTemplateBoundary,
    SopTemplate,
    TemplateBoundaryDraft,
    TemplateBoundaryUpdate,
    TemplateDraft,
    TemplateDraftDocument,
    TemplateImport,
    TemplateRuntimeDefaults,
    TemplateStep,
)
from factory_sop.template.parser import WorkbookValidationError, parse_workbook
from factory_sop.template.repository import TemplateDraftRepository

_logger = get_logger("template")


@dataclass(frozen=True, slots=True)
class TemplateImportResult:
    """一次导入的持久记录，以及成功时创建的草稿。"""

    import_record: TemplateImport
    draft: TemplateDraftDocument | None
    errors: tuple[TemplateFieldError, ...]


def import_template_draft(
    *,
    document: bytes,
    filename: str,
    content_type: str,
    caller: Caller,
    now: datetime,
    stations: StationCodeLookup,
    templates: TemplateDraftRepository,
) -> TemplateImportResult:
    """授权导入工作簿，保留原文，并在成功时追加而非覆盖一个草稿。"""
    authorize(caller, Permission.TEMPLATE_DRAFT_EDIT)
    import_id = new_id()
    try:
        parsed = parse_workbook(document)
    except WorkbookValidationError as error:
        record = _import_record(
            import_id=import_id,
            document=document,
            filename=filename,
            content_type=content_type,
            status=ImportStatus.FAILED,
            errors=error.errors,
            caller=caller,
            now=now,
        )
        templates.add_import(record)
        _log_import_failure(record=record, caller=caller)
        return TemplateImportResult(record, None, record.errors)

    station = station_by_code(code=parsed.station_code, stations=stations)
    if station is None:
        errors = (
            TemplateFieldError(
                sheet="工位表",
                row=parsed.station_row,
                field="工位号",
                message="工位不存在，请先在设备管理中创建该工位",
            ),
        )
        record = _import_record(
            import_id=import_id,
            document=document,
            filename=filename,
            content_type=content_type,
            status=ImportStatus.FAILED,
            errors=errors,
            caller=caller,
            now=now,
        )
        templates.add_import(record)
        _log_import_failure(record=record, caller=caller)
        return TemplateImportResult(record, None, errors)

    template = SopTemplate(
        id=new_id(),
        station_id=station.id,
        station_code=station.code,
        station_name=station.name,
        created_by=caller.user.id,
        updated_by=caller.user.id,
        created_at=now,
        updated_at=now,
    )
    draft = TemplateDraft(
        id=new_id(),
        template_id=template.id,
        source_import_id=import_id,
        steps=tuple(
            TemplateStep(number=step.number, name=step.name, description=step.description)
            for step in parsed.steps
        ),
        ordering=OrderingMode.STRICT,
        runtime_defaults=TemplateRuntimeDefaults(),
        revision=1,
        created_by=caller.user.id,
        updated_by=caller.user.id,
        created_at=now,
        updated_at=now,
    )
    record = _import_record(
        import_id=import_id,
        document=document,
        filename=filename,
        content_type=content_type,
        status=ImportStatus.SUCCEEDED,
        errors=(),
        caller=caller,
        now=now,
    )
    templates.add_import(record)
    templates.add_template(template)
    templates.add_draft(draft)
    result = TemplateDraftDocument(template=template, draft=draft)
    _logger.info(
        "template.import.succeeded",
        import_id=str(record.id),
        draft_id=str(draft.id),
        template_id=str(template.id),
        actor_id=str(caller.user.id),
    )
    return TemplateImportResult(record, result, ())


def list_template_drafts(
    *,
    caller: Caller,
    templates: TemplateDraftRepository,
    page: int,
    page_size: int,
) -> tuple[list[TemplateDraftDocument], int]:
    authorize(caller, Permission.TEMPLATE_DRAFT_VIEW)
    return templates.page_drafts(page=page, page_size=page_size)


def read_template_draft(
    *,
    draft_id: UUID,
    caller: Caller,
    templates: TemplateDraftRepository,
) -> TemplateDraftDocument:
    """读取一个草稿及其工位身份。"""
    authorize(caller, Permission.TEMPLATE_DRAFT_VIEW)
    document = templates.draft_by_id(draft_id)
    if document is None:
        _refuse(TemplateRefusalCode.DRAFT_NOT_FOUND, draft_id=str(draft_id))
    return document


def edit_template_draft(
    *,
    draft_id: UUID,
    steps: Sequence[TemplateStep],
    ordering: OrderingMode,
    runtime_defaults: TemplateRuntimeDefaults,
    expected_revision: int,
    caller: Caller,
    now: datetime,
    templates: TemplateDraftRepository,
    boundary: TemplateBoundaryUpdate = KEEP_TEMPLATE_BOUNDARY,
) -> TemplateDraftDocument:
    """在 If-Match 对应的 revision 上替换草稿内容。"""
    authorize(caller, Permission.TEMPLATE_DRAFT_EDIT)
    current = templates.draft_by_id(draft_id)
    if current is None:
        _refuse(TemplateRefusalCode.DRAFT_NOT_FOUND, draft_id=str(draft_id))
    if current.draft.revision != expected_revision:
        _refuse(
            TemplateRefusalCode.STALE_REVISION,
            draft_id=str(draft_id),
            expected_revision=str(expected_revision),
            actual_revision=str(current.draft.revision),
        )
    if tuple(step.number for step in steps) != tuple(range(1, len(steps) + 1)):
        _refuse(
            TemplateRefusalCode.DRAFT_INVALID,
            field_errors=(
                TemplateFieldError(
                    sheet="草稿",
                    row=None,
                    field="步骤号",
                    message="步骤号必须从 1 开始连续排列",
                ),
            ),
        )
    next_boundary = current.draft.boundary
    if isinstance(boundary, PatchTemplateBoundary):
        current_boundary = current.draft.boundary
        current_start = current_boundary.start_signal if current_boundary is not None else None
        current_end = current_boundary.end_signals if current_boundary is not None else None
        next_start = (
            current_start
            if isinstance(boundary.start_signal, KeepTemplateBoundaryPart)
            else boundary.start_signal
        )
        next_end = (
            current_end
            if isinstance(boundary.end_signals, KeepTemplateBoundaryPart)
            else boundary.end_signals
        )
        next_boundary = TemplateBoundaryDraft(start_signal=next_start, end_signals=next_end)
    edited = replace(
        current.draft,
        steps=tuple(steps),
        ordering=ordering,
        runtime_defaults=runtime_defaults,
        revision=expected_revision + 1,
        updated_by=caller.user.id,
        updated_at=now,
        boundary=next_boundary,
    )
    templates.save_draft(edited, expected_revision=expected_revision)
    _logger.info(
        "template.draft.updated",
        draft_id=str(edited.id),
        template_id=str(edited.template_id),
        actor_id=str(caller.user.id),
    )
    return TemplateDraftDocument(template=current.template, draft=edited)


def list_template_imports(
    *,
    caller: Caller,
    templates: TemplateDraftRepository,
    page: int,
    page_size: int,
) -> tuple[list[TemplateImport], int]:
    """列出导入记录及其成功或失败状态。"""
    authorize(caller, Permission.TEMPLATE_DRAFT_VIEW)
    return templates.page_imports(page=page, page_size=page_size)


def read_template_import(
    *,
    import_id: UUID,
    caller: Caller,
    templates: TemplateDraftRepository,
) -> TemplateImport:
    """读取一条导入记录；原始字节通过独立下载契约返回。"""
    authorize(caller, Permission.TEMPLATE_DRAFT_VIEW)
    record = templates.import_by_id(import_id)
    if record is None:
        _refuse(TemplateRefusalCode.IMPORT_NOT_FOUND, import_id=str(import_id))
    return record


def _import_record(
    *,
    import_id: UUID,
    document: bytes,
    filename: str,
    content_type: str,
    status: ImportStatus,
    errors: tuple[TemplateFieldError, ...],
    caller: Caller,
    now: datetime,
) -> TemplateImport:
    return TemplateImport(
        id=import_id,
        filename=filename or "template.xlsx",
        content_type=content_type,
        original_document=document,
        sha256=hashlib.sha256(document).hexdigest(),
        status=status,
        errors=errors,
        imported_by=caller.user.id,
        imported_at=now,
    )


def _log_import_failure(*, record: TemplateImport, caller: Caller) -> None:
    _logger.info(
        "template.import.failed",
        import_id=str(record.id),
        actor_id=str(caller.user.id),
        error_count=str(len(record.errors)),
    )


def _refuse(
    code: TemplateRefusalCode,
    *,
    field_errors: tuple[TemplateFieldError, ...] = (),
    **context: str,
) -> NoReturn:
    _logger.info("template.draft.refused", error_code=code.value, **context)
    raise TemplateRefusedError(code, field_errors=field_errors)
