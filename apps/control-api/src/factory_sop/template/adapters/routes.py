"""模板导入和可编辑草稿的 HTTP 路由。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, Query, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator

from factory_sop.auth.api import Authorized, Permission, needs
from factory_sop.device.api import StationCodeLookup
from factory_sop.problem import ApiErrorCode, FieldError, problem_openapi_response, problem_response
from factory_sop.responses import DEFAULT_PAGE_SIZE, MAXIMUM_PAGE_SIZE, ItemPage
from factory_sop.template.adapters import dependencies as template_dependencies
from factory_sop.template.adapters.dependencies import templates as template_dependency
from factory_sop.template.model import (
    ImportStatus,
    OrderingMode,
    TemplateDraft,
    TemplateDraftDocument,
    TemplateImport,
    TemplateRuntimeDefaults,
    TemplateStep,
)
from factory_sop.template.repository import TemplateRepository
from factory_sop.template.usecases.drafts import (
    edit_template_draft,
    import_template_draft,
    list_template_drafts,
    list_template_imports,
    read_template_draft,
    read_template_import,
)

router = APIRouter(prefix="/templates", tags=["template"])
ProblemResponses = dict[int | str, dict[str, Any]]
_UNAUTHORIZED: ProblemResponses = {
    401: problem_openapi_response("需要认证或会话无效"),
    403: problem_openapi_response("权限不足或 CSRF 校验失败"),
}
_VALIDATION: ProblemResponses = {422: problem_openapi_response("请求无效")}
_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_BINARY_DOWNLOAD: dict[int, dict[str, object]] = {
    200: {
        "description": "保留的原始工作簿",
        "content": {_XLSX_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}},
    }
}


class TemplateFieldErrorView(BaseModel):
    """导入错误保留工作表和行号，不把定位信息压平。"""

    sheet: str
    row: int | None
    field: str
    message: str


class TemplateStepInput(BaseModel):
    number: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def matches_base_action(self) -> TemplateStepInput:
        TemplateStep(number=self.number, name=self.name, description=self.description)
        return self


class TemplateRuntimeDefaultsInput(BaseModel):
    idle_timeout_seconds: float | None = Field(default=None, gt=0)
    step_deadline_seconds: float | None = Field(default=None, gt=0)
    disposition_policy: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("disposition_policy")
    @classmethod
    def non_blank_policy(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("处置策略不能为空")
        return value.strip() if value is not None else None


class TemplateDraftConfiguration(BaseModel):
    steps: list[TemplateStepInput] = Field(min_length=1)
    ordering: OrderingMode
    runtime_defaults: TemplateRuntimeDefaultsInput

    @model_validator(mode="after")
    def has_contiguous_step_numbers(self) -> TemplateDraftConfiguration:
        if [step.number for step in self.steps] != list(range(1, len(self.steps) + 1)):
            raise ValueError("步骤号必须从 1 开始连续排列")
        return self


class TemplateStepView(BaseModel):
    number: int
    name: str
    description: str


class TemplateRuntimeDefaultsView(BaseModel):
    idle_timeout_seconds: float | None
    step_deadline_seconds: float | None
    disposition_policy: str | None


class TemplateDraftView(BaseModel):
    id: UUID
    template_id: UUID
    source_import_id: UUID
    station_id: UUID
    station_code: str
    station_name: str
    steps: list[TemplateStepView]
    ordering: OrderingMode
    runtime_defaults: TemplateRuntimeDefaultsView
    revision: int
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime


class TemplateImportView(BaseModel):
    id: UUID
    filename: str
    content_type: str
    sha256: str
    status: ImportStatus
    errors: list[TemplateFieldErrorView]
    imported_by: UUID
    imported_at: datetime


class TemplateImportResultView(BaseModel):
    import_record: TemplateImportView
    draft: TemplateDraftView | None


def _draft_view(document: TemplateDraftDocument) -> TemplateDraftView:
    draft: TemplateDraft = document.draft
    return TemplateDraftView(
        id=draft.id,
        template_id=draft.template_id,
        source_import_id=draft.source_import_id,
        station_id=document.template.station_id,
        station_code=document.template.station_code,
        station_name=document.template.station_name,
        steps=[
            TemplateStepView(number=step.number, name=step.name, description=step.description)
            for step in draft.steps
        ],
        ordering=draft.ordering,
        runtime_defaults=TemplateRuntimeDefaultsView(
            idle_timeout_seconds=draft.runtime_defaults.idle_timeout_seconds,
            step_deadline_seconds=draft.runtime_defaults.step_deadline_seconds,
            disposition_policy=draft.runtime_defaults.disposition_policy,
        ),
        revision=draft.revision,
        created_by=draft.created_by,
        updated_by=draft.updated_by,
        created_at=draft.created_at,
        updated_at=draft.updated_at,
    )


def _import_view(record: TemplateImport) -> TemplateImportView:
    return TemplateImportView(
        id=record.id,
        filename=record.filename,
        content_type=record.content_type,
        sha256=record.sha256,
        status=record.status,
        errors=[
            TemplateFieldErrorView(
                sheet=error.sheet,
                row=error.row,
                field=error.field,
                message=error.message,
            )
            for error in record.errors
        ],
        imported_by=record.imported_by,
        imported_at=record.imported_at,
    )


@router.post(
    "/imports",
    status_code=status.HTTP_201_CREATED,
    response_model=TemplateImportResultView,
    operation_id="importTemplateDraft",
    openapi_extra=needs(Permission.TEMPLATE_DRAFT_EDIT),
    responses=_UNAUTHORIZED | _VALIDATION,
)
def import_a_template_draft(
    document: Annotated[bytes, Body(media_type=_XLSX_MEDIA_TYPE)],
    filename: Annotated[str, Query(min_length=1, max_length=255)],
    caller: Authorized,
    stations: Annotated[StationCodeLookup, Depends(template_dependencies.stations)],
    templates: Annotated[TemplateRepository, Depends(template_dependency)],
    content_type: Annotated[str | None, Header(alias="Content-Type")] = None,
) -> TemplateImportResultView | Response:
    """导入原始 xlsx 字节；校验失败时仍保留原文。"""
    result = import_template_draft(
        document=document,
        filename=filename,
        content_type=_XLSX_MEDIA_TYPE,
        caller=caller,
        now=datetime.now(UTC),
        stations=stations,
        templates=templates,
    )
    if result.draft is None:
        # 先让请求事务提交失败记录，再返回 422；不能通过抛异常触发事务回滚。
        return problem_response(
            status=422,
            title="导入的工作簿不符合规范",
            error_code=ApiErrorCode.TEMPLATE_IMPORT_INVALID,
            field_errors=[
                FieldError(
                    field=(
                        f"{item.sheet}[{item.row}].{item.field}"
                        if item.row is not None
                        else f"{item.sheet}.{item.field}"
                    ),
                    message=item.message,
                )
                for item in result.errors
            ],
        )
    return TemplateImportResultView(
        import_record=_import_view(result.import_record),
        draft=_draft_view(result.draft),
    )


@router.get(
    "/imports",
    operation_id="listTemplateImports",
    openapi_extra=needs(Permission.TEMPLATE_DRAFT_VIEW),
    responses=_UNAUTHORIZED | _VALIDATION,
)
def list_the_template_imports(
    caller: Authorized,
    templates: Annotated[TemplateRepository, Depends(template_dependency)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAXIMUM_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ItemPage[TemplateImportView]:
    items, total = list_template_imports(
        caller=caller, templates=templates, page=page, page_size=page_size
    )
    return ItemPage(
        items=[_import_view(item) for item in items], page=page, page_size=page_size, total=total
    )


@router.get(
    "/imports/{import_id}/document",
    operation_id="downloadTemplateImport",
    response_class=Response,
    openapi_extra=needs(Permission.TEMPLATE_DRAFT_VIEW),
    responses=_UNAUTHORIZED
    | _VALIDATION
    | _BINARY_DOWNLOAD
    | {404: problem_openapi_response("导入记录不存在")},
)
def download_an_import(
    import_id: UUID,
    caller: Authorized,
    templates: Annotated[TemplateRepository, Depends(template_dependency)],
) -> Response:
    """返回服务端保留的原始工作簿。"""
    record = read_template_import(import_id=import_id, caller=caller, templates=templates)
    return Response(content=record.original_document, media_type=_XLSX_MEDIA_TYPE)


@router.get(
    "/imports/{import_id}",
    operation_id="readTemplateImport",
    openapi_extra=needs(Permission.TEMPLATE_DRAFT_VIEW),
    responses=_UNAUTHORIZED | _VALIDATION | {404: problem_openapi_response("导入记录不存在")},
)
def read_an_import(
    import_id: UUID,
    caller: Authorized,
    templates: Annotated[TemplateRepository, Depends(template_dependency)],
) -> TemplateImportView:
    return _import_view(
        read_template_import(import_id=import_id, caller=caller, templates=templates)
    )


@router.get(
    "/drafts",
    operation_id="listTemplateDrafts",
    openapi_extra=needs(Permission.TEMPLATE_DRAFT_VIEW),
    responses=_UNAUTHORIZED | _VALIDATION,
)
def list_the_template_drafts(
    caller: Authorized,
    templates: Annotated[TemplateRepository, Depends(template_dependency)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAXIMUM_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ItemPage[TemplateDraftView]:
    items, total = list_template_drafts(
        caller=caller, templates=templates, page=page, page_size=page_size
    )
    return ItemPage(
        items=[_draft_view(item) for item in items], page=page, page_size=page_size, total=total
    )


@router.get(
    "/drafts/{draft_id}",
    operation_id="readTemplateDraft",
    openapi_extra=needs(Permission.TEMPLATE_DRAFT_VIEW),
    responses=_UNAUTHORIZED | _VALIDATION | {404: problem_openapi_response("模板草稿不存在")},
)
def read_a_template_draft(
    draft_id: UUID,
    caller: Authorized,
    templates: Annotated[TemplateRepository, Depends(template_dependency)],
) -> TemplateDraftView:
    return _draft_view(read_template_draft(draft_id=draft_id, caller=caller, templates=templates))


@router.patch(
    "/drafts/{draft_id}",
    operation_id="editTemplateDraft",
    openapi_extra=needs(Permission.TEMPLATE_DRAFT_EDIT),
    responses=_UNAUTHORIZED
    | _VALIDATION
    | {
        404: problem_openapi_response("模板草稿不存在"),
        409: problem_openapi_response("草稿修订号已变化（STALE_REVISION）"),
    },
)
def edit_a_template_draft(
    draft_id: UUID,
    configuration: TemplateDraftConfiguration,
    caller: Authorized,
    templates: Annotated[TemplateRepository, Depends(template_dependency)],
    if_match: Annotated[int, Header(alias="If-Match")],
) -> TemplateDraftView:
    edited = edit_template_draft(
        draft_id=draft_id,
        steps=tuple(
            TemplateStep(number=step.number, name=step.name, description=step.description)
            for step in configuration.steps
        ),
        ordering=configuration.ordering,
        runtime_defaults=TemplateRuntimeDefaults(
            idle_timeout_seconds=configuration.runtime_defaults.idle_timeout_seconds,
            step_deadline_seconds=configuration.runtime_defaults.step_deadline_seconds,
            disposition_policy=configuration.runtime_defaults.disposition_policy,
        ),
        expected_revision=if_match,
        caller=caller,
        now=datetime.now(UTC),
        templates=templates,
    )
    return _draft_view(edited)
