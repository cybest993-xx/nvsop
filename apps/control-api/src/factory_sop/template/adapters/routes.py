"""模板导入和可编辑草稿的 HTTP 路由。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal, assert_never, cast
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_serializer,
    field_validator,
    model_validator,
)

from factory_sop.auth.api import Authorized, Permission, needs
from factory_sop.device.api import (
    INFERENCE_HOST_ID_HEADER,
    INFERENCE_HOST_NONCE_HEADER,
    INFERENCE_HOST_SIGNATURE_HEADER,
    INFERENCE_HOST_TIMESTAMP_HEADER,
    DeviceHostGateway,
    DeviceTemplateBindingGateway,
    RuntimeParameterMode,
    StationCodeLookup,
    StationRuntimeParameters,
    host_identity_from_headers,
)
from factory_sop.problem import ApiErrorCode, FieldError, problem_openapi_response, problem_response
from factory_sop.responses import DEFAULT_PAGE_SIZE, MAXIMUM_PAGE_SIZE, ItemPage
from factory_sop.template.adapters import dependencies as template_dependencies
from factory_sop.template.adapters.dependencies import templates as template_dependency
from factory_sop.template.model import (
    ImportStatus,
    KeepTemplateBoundary,
    KeepTemplateBoundaryPart,
    OrderingMode,
    PatchTemplateBoundary,
    TemplateArtifactName,
    TemplateBindingStatus,
    TemplateBoundaryUpdate,
    TemplateDraft,
    TemplateDraftDocument,
    TemplateImport,
    TemplateRuntimeDefaults,
    TemplateSignal,
    TemplateSignalKind,
    TemplateStep,
    TemplateVersion,
)
from factory_sop.template.repository import TemplateRepository
from factory_sop.template.usecases.bindings import (
    StationTemplateConfiguration,
    TemplateBindingPreview,
    bind_template_version,
    preview_template_binding,
    read_station_configuration,
    report_template_configuration,
    update_station_runtime_parameters,
)
from factory_sop.template.usecases.drafts import (
    edit_template_draft,
    import_template_draft,
    list_template_drafts,
    list_template_imports,
    read_template_draft,
    read_template_import,
)
from factory_sop.template.usecases.versions import (
    download_template_version_artifact,
    list_template_versions,
    publish_template_version,
    read_template_version,
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
_VERSION_ARTIFACT_DOWNLOAD: dict[int, dict[str, object]] = {
    200: {
        "description": "已保存的模板版本制品",
        "content": {
            "application/json": {"schema": {"type": "string", "format": "binary"}},
            "text/plain": {"schema": {"type": "string", "format": "binary"}},
        },
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


class ActionSignalInput(BaseModel):
    """动作编号边界信号。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["action"]
    action_number: StrictInt = Field(gt=0)


class ExternalSignalInput(BaseModel):
    """外部信号语义标签边界信号。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["external"]
    semantic_label: StrictStr = Field(min_length=1, max_length=128)

    @field_validator("semantic_label")
    @classmethod
    def non_blank_label(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("外部信号语义标签不能为空")
        return value.strip()


TemplateSignalInput = Annotated[
    ActionSignalInput | ExternalSignalInput,
    Field(discriminator="kind"),
]


class TemplateDraftConfiguration(BaseModel):
    steps: list[TemplateStepInput] = Field(min_length=1)
    ordering: OrderingMode
    runtime_defaults: TemplateRuntimeDefaultsInput
    start_signal: TemplateSignalInput | None = None
    end_signals: list[TemplateSignalInput] | None = None

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
    start_signal: TemplateSignalInput | None = None
    end_signals: list[TemplateSignalInput] | None = None
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


class TemplateArtifactView(BaseModel):
    name: TemplateArtifactName
    media_type: str
    byte_length: int
    sha256: str


class TemplateVersionView(BaseModel):
    id: UUID
    template_id: UUID
    source_import_id: UUID
    source_draft_id: UUID
    source_draft_revision: int
    steps: list[TemplateStepView]
    ordering: OrderingMode
    runtime_defaults: TemplateRuntimeDefaultsView
    start_signal: TemplateSignalInput
    end_signals: list[TemplateSignalInput]
    sha256: str
    published_by: UUID
    published_at: datetime = Field(json_schema_extra={"format": "date-time"})
    artifacts: list[TemplateArtifactView]

    @field_serializer("published_at")
    def serialize_published_at(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _signal_input(signal: TemplateSignal) -> TemplateSignalInput:
    match signal.kind:
        case TemplateSignalKind.ACTION:
            assert isinstance(signal.value, int)
            return ActionSignalInput(kind="action", action_number=signal.value)
        case TemplateSignalKind.EXTERNAL:
            assert isinstance(signal.value, str)
            return ExternalSignalInput(kind="external", semantic_label=signal.value)
        case _:
            assert_never(signal.kind)


def _signal_domain(signal: TemplateSignalInput) -> TemplateSignal:
    if isinstance(signal, ActionSignalInput):
        return TemplateSignal(TemplateSignalKind.ACTION, signal.action_number)
    if isinstance(signal, ExternalSignalInput):
        return TemplateSignal(TemplateSignalKind.EXTERNAL, signal.semantic_label)
    assert_never(signal)


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
        start_signal=(
            _signal_input(draft.boundary.start_signal)
            if draft.boundary is not None and draft.boundary.start_signal is not None
            else None
        ),
        end_signals=(
            None
            if draft.boundary is None or draft.boundary.end_signals is None
            else [_signal_input(signal) for signal in draft.boundary.end_signals]
        ),
        revision=draft.revision,
        created_by=draft.created_by,
        updated_by=draft.updated_by,
        created_at=draft.created_at,
        updated_at=draft.updated_at,
    )


def _version_view(version: TemplateVersion) -> TemplateVersionView:
    assert version.boundary.start_signal is not None
    assert version.boundary.end_signals is not None
    return TemplateVersionView(
        id=version.id,
        template_id=version.template_id,
        source_import_id=version.source_import_id,
        source_draft_id=version.source_draft_id,
        source_draft_revision=version.source_draft_revision,
        steps=[
            TemplateStepView(number=step.number, name=step.name, description=step.description)
            for step in version.steps
        ],
        ordering=version.ordering,
        runtime_defaults=TemplateRuntimeDefaultsView(
            idle_timeout_seconds=version.runtime_defaults.idle_timeout_seconds,
            step_deadline_seconds=version.runtime_defaults.step_deadline_seconds,
            disposition_policy=version.runtime_defaults.disposition_policy,
        ),
        start_signal=_signal_input(version.boundary.start_signal),
        end_signals=[_signal_input(signal) for signal in version.boundary.end_signals],
        sha256=version.sha256,
        published_by=version.published_by,
        published_at=version.published_at,
        artifacts=[
            TemplateArtifactView(
                name=artifact.name,
                media_type=artifact.media_type,
                byte_length=artifact.byte_length,
                sha256=artifact.sha256,
            )
            for artifact in version.artifacts
        ],
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
    fields = configuration.model_fields_set
    boundary: TemplateBoundaryUpdate
    if "start_signal" not in fields and "end_signals" not in fields:
        boundary = KeepTemplateBoundary()
    else:
        boundary = PatchTemplateBoundary(
            start_signal=(
                _signal_domain(configuration.start_signal)
                if "start_signal" in fields and configuration.start_signal is not None
                else None
                if "start_signal" in fields
                else KeepTemplateBoundaryPart()
            ),
            end_signals=(
                tuple(_signal_domain(signal) for signal in configuration.end_signals or ())
                if "end_signals" in fields and configuration.end_signals is not None
                else None
                if "end_signals" in fields
                else KeepTemplateBoundaryPart()
            ),
        )
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
        boundary=boundary,
    )
    return _draft_view(edited)


@router.post(
    "/drafts/{draft_id}/publish",
    status_code=status.HTTP_201_CREATED,
    response_model=TemplateVersionView,
    operation_id="publishTemplateVersion",
    openapi_extra=needs(Permission.TEMPLATE_DRAFT_EDIT),
    responses=_UNAUTHORIZED
    | _VALIDATION
    | {
        200: {"description": "已发布的模板版本", "model": TemplateVersionView},
        404: problem_openapi_response("模板草稿不存在"),
        409: problem_openapi_response("草稿修订号已变化（STALE_REVISION）"),
    },
)
def publish_a_template_version(
    draft_id: UUID,
    caller: Authorized,
    templates: Annotated[TemplateRepository, Depends(template_dependency)],
    if_match: Annotated[int, Header(alias="If-Match")],
) -> TemplateVersionView | JSONResponse:
    """发布指定草稿修订；成功只保存中心版本，不改变工位绑定。"""
    result = publish_template_version(
        draft_id=draft_id,
        expected_revision=if_match,
        caller=caller,
        now=datetime.now(UTC),
        templates=templates,
    )
    view = _version_view(result.version)
    if result.created:
        return view
    return JSONResponse(status_code=status.HTTP_200_OK, content=view.model_dump(mode="json"))


@router.get(
    "/versions",
    operation_id="listTemplateVersions",
    openapi_extra=needs(Permission.TEMPLATE_DRAFT_VIEW),
    responses=_UNAUTHORIZED | _VALIDATION,
)
def list_the_template_versions(
    caller: Authorized,
    templates: Annotated[TemplateRepository, Depends(template_dependency)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAXIMUM_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ItemPage[TemplateVersionView]:
    items, total = list_template_versions(
        caller=caller, templates=templates, page=page, page_size=page_size
    )
    return ItemPage(
        items=[_version_view(item) for item in items],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get(
    "/versions/{version_id}",
    operation_id="readTemplateVersion",
    response_model=TemplateVersionView,
    openapi_extra=needs(Permission.TEMPLATE_DRAFT_VIEW),
    responses=_UNAUTHORIZED | _VALIDATION | {404: problem_openapi_response("模板版本不存在")},
)
def read_a_template_version(
    version_id: UUID,
    caller: Authorized,
    templates: Annotated[TemplateRepository, Depends(template_dependency)],
) -> TemplateVersionView:
    return _version_view(
        read_template_version(version_id=version_id, caller=caller, templates=templates)
    )


@router.get(
    "/versions/{version_id}/artifacts/{name}",
    operation_id="downloadTemplateVersionArtifact",
    response_class=Response,
    openapi_extra=needs(Permission.TEMPLATE_DRAFT_VIEW),
    responses=_UNAUTHORIZED
    | _VALIDATION
    | _VERSION_ARTIFACT_DOWNLOAD
    | {404: problem_openapi_response("模板版本或制品不存在")},
)
def download_a_template_version_artifact(
    version_id: UUID,
    name: TemplateArtifactName,
    caller: Authorized,
    templates: Annotated[TemplateRepository, Depends(template_dependency)],
) -> Response:
    artifact = download_template_version_artifact(
        version_id=version_id,
        name=name,
        caller=caller,
        templates=templates,
    )
    return Response(content=artifact.content, media_type=artifact.media_type)


class RuntimeParametersInput(BaseModel):
    """一整组工位运行参数；禁止逐字段补丁和未知字段。"""

    model_config = ConfigDict(extra="forbid")

    idle_timeout_seconds: StrictInt | StrictFloat = Field(gt=0)
    step_deadline_seconds: StrictInt | StrictFloat = Field(gt=0)
    disposition_policy: StrictStr = Field(min_length=1, max_length=64)

    @field_validator("disposition_policy")
    @classmethod
    def _policy_is_non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("处置策略不能为空")
        return value

    def to_domain(self) -> StationRuntimeParameters:
        return StationRuntimeParameters(
            idle_timeout_seconds=float(self.idle_timeout_seconds),
            step_deadline_seconds=float(self.step_deadline_seconds),
            disposition_policy=self.disposition_policy,
        )


class TemplateBindingInput(BaseModel):
    """正式绑定请求；省略运行参数字段表示保留工位当前整组来源。"""

    model_config = ConfigDict(extra="forbid")

    station_id: UUID
    version_id: UUID
    runtime_parameter_mode: RuntimeParameterMode | None = None
    runtime_parameters: RuntimeParametersInput | None = None

    @model_validator(mode="after")
    def _mode_and_group_match(self) -> TemplateBindingInput:
        if self.runtime_parameter_mode is RuntimeParameterMode.FOLLOW_TEMPLATE:
            if self.runtime_parameters is not None:
                raise ValueError("跟随模板时不能提交工位覆盖组")
        elif self.runtime_parameter_mode is RuntimeParameterMode.CUSTOM:
            if self.runtime_parameters is None:
                raise ValueError("工位自定义时必须提交完整运行参数组")
        elif self.runtime_parameters is not None:
            raise ValueError("提交运行参数时必须同时声明配置模式")
        return self


class RuntimeParametersUpdateInput(BaseModel):
    """运行参数切换必须明确提交模式和完整覆盖组。"""

    model_config = ConfigDict(extra="forbid")

    mode: RuntimeParameterMode
    parameters: RuntimeParametersInput | None = None

    @model_validator(mode="after")
    def _mode_and_group_match(self) -> RuntimeParametersUpdateInput:
        if self.mode is RuntimeParameterMode.FOLLOW_TEMPLATE and self.parameters is not None:
            raise ValueError("跟随模板时不能提交工位覆盖组")
        if self.mode is RuntimeParameterMode.CUSTOM and self.parameters is None:
            raise ValueError("工位自定义时必须提交完整运行参数组")
        return self


class RuntimeParametersView(BaseModel):
    idle_timeout_seconds: float
    step_deadline_seconds: float
    disposition_policy: str


def _runtime_view(parameters: StationRuntimeParameters | None) -> RuntimeParametersView | None:
    if parameters is None:
        return None
    return RuntimeParametersView(
        idle_timeout_seconds=float(parameters.idle_timeout_seconds),
        step_deadline_seconds=float(parameters.step_deadline_seconds),
        disposition_policy=parameters.disposition_policy,
    )


def _optional_runtime_view(
    parameters: StationRuntimeParameters | None,
) -> RuntimeParametersView | None:
    return _runtime_view(parameters)


class BindingValidationIssueView(BaseModel):
    code: str
    field: str
    message: str


class DesiredTemplateBindingView(BaseModel):
    id: UUID
    version_id: UUID
    sha256: str
    config_revision: int
    revision: int
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime


class BackendConfigurationStatusView(BaseModel):
    backend_id: UUID
    host_id: UUID | None
    status: TemplateBindingStatus
    reported_version_id: UUID | None
    reported_sha256: str | None
    reported_config_revision: int | None
    reported_at: datetime | None
    rejection_code: str | None
    rejection_detail: str | None
    rejection_at: datetime | None


class StationTemplateConfigurationView(BaseModel):
    station_id: UUID
    station_revision: int
    runtime_parameters_revision: int
    runtime_parameter_mode: RuntimeParameterMode
    template_defaults: RuntimeParametersView | None
    runtime_overrides: RuntimeParametersView | None
    effective_runtime_parameters: RuntimeParametersView | None
    desired: DesiredTemplateBindingView | None
    version: TemplateVersionView | None
    status: TemplateBindingStatus
    status_detail: str | None
    topology_issues: list[BindingValidationIssueView]
    backends: list[BackendConfigurationStatusView]


def _configuration_view(
    configuration: StationTemplateConfiguration,
) -> StationTemplateConfigurationView:
    binding = configuration.binding
    return StationTemplateConfigurationView(
        station_id=configuration.station_id,
        station_revision=configuration.station_revision,
        runtime_parameters_revision=configuration.runtime.runtime_parameters_revision,
        runtime_parameter_mode=configuration.runtime.mode,
        template_defaults=_runtime_view(configuration.runtime.defaults),
        runtime_overrides=_runtime_view(configuration.runtime.overrides),
        effective_runtime_parameters=_runtime_view(configuration.runtime.effective),
        desired=(
            None
            if binding is None
            else DesiredTemplateBindingView(
                id=binding.id,
                version_id=binding.desired_version_id,
                sha256=binding.desired_sha256,
                config_revision=binding.desired_config_revision,
                revision=binding.revision,
                created_by=binding.created_by,
                updated_by=binding.updated_by,
                created_at=binding.created_at,
                updated_at=binding.updated_at,
            )
        ),
        version=None if configuration.version is None else _version_view(configuration.version),
        status=configuration.status,
        status_detail=configuration.status_detail,
        topology_issues=[
            BindingValidationIssueView(
                code=issue.code,
                field=issue.field,
                message=issue.message,
            )
            for issue in configuration.topology_issues
        ],
        backends=[
            BackendConfigurationStatusView(
                backend_id=backend.backend_id,
                host_id=backend.host_id,
                status=backend.status,
                reported_version_id=backend.reported_version_id,
                reported_sha256=backend.reported_sha256,
                reported_config_revision=backend.reported_config_revision,
                reported_at=backend.reported_at,
                rejection_code=backend.rejection_code,
                rejection_detail=backend.rejection_detail,
                rejection_at=backend.rejection_at,
            )
            for backend in configuration.backends
        ],
    )


class TemplateBindingPreviewView(BaseModel):
    version: TemplateVersionView
    station_revision: int
    current_mode: RuntimeParameterMode
    current_defaults: RuntimeParametersView | None
    current_overrides: RuntimeParametersView | None
    requested_mode: RuntimeParameterMode
    requested_overrides: RuntimeParametersView | None
    accepted: bool
    reasons: list[BindingValidationIssueView]


def _preview_view(preview: TemplateBindingPreview) -> TemplateBindingPreviewView:
    return TemplateBindingPreviewView(
        version=_version_view(preview.version),
        station_revision=preview.runtime.station_revision,
        current_mode=preview.runtime.mode,
        current_defaults=_runtime_view(preview.runtime.defaults),
        current_overrides=_runtime_view(preview.runtime.overrides),
        requested_mode=preview.requested_mode,
        requested_overrides=_runtime_view(preview.requested_overrides),
        accepted=preview.accepted,
        reasons=[
            BindingValidationIssueView(
                code=issue.code,
                field=issue.field,
                message=issue.message,
            )
            for issue in preview.validation_issues
        ],
    )


@router.post(
    "/bindings/validate",
    operation_id="validateTemplateBinding",
    openapi_extra=needs(Permission.STATION_VIEW),
    responses=_UNAUTHORIZED | _VALIDATION | {404: problem_openapi_response("模板版本或工位不存在")},
)
def validate_a_template_binding(
    request: TemplateBindingInput,
    caller: Authorized,
    templates: Annotated[TemplateRepository, Depends(template_dependency)],
    device: Annotated[DeviceTemplateBindingGateway, Depends(template_dependencies.binding_gateway)],
) -> TemplateBindingPreviewView:
    preview = preview_template_binding(
        station_id=request.station_id,
        version_id=request.version_id,
        runtime_mode=request.runtime_parameter_mode,
        runtime_overrides=(
            None if request.runtime_parameters is None else request.runtime_parameters.to_domain()
        ),
        caller=caller,
        now=datetime.now(UTC),
        templates=templates,
        device=device,
    )
    return _preview_view(preview)


@router.post(
    "/bindings",
    operation_id="bindTemplateVersion",
    openapi_extra=needs(Permission.STATION_EDIT),
    responses=_UNAUTHORIZED
    | _VALIDATION
    | {
        404: problem_openapi_response("模板版本或工位不存在"),
        409: problem_openapi_response("工位配置已变化或共享设备拓扑冲突"),
        422: problem_openapi_response("模板版本、边界或设备拓扑不符合要求"),
    },
)
def bind_a_template_version(
    request: TemplateBindingInput,
    caller: Authorized,
    templates: Annotated[TemplateRepository, Depends(template_dependency)],
    device: Annotated[DeviceTemplateBindingGateway, Depends(template_dependencies.binding_gateway)],
    if_match: Annotated[int, Header(alias="If-Match")],
) -> StationTemplateConfigurationView:
    configuration = bind_template_version(
        station_id=request.station_id,
        version_id=request.version_id,
        runtime_mode=request.runtime_parameter_mode,
        runtime_overrides=(
            None if request.runtime_parameters is None else request.runtime_parameters.to_domain()
        ),
        expected_station_revision=if_match,
        caller=caller,
        now=datetime.now(UTC),
        templates=templates,
        device=device,
    )
    return _configuration_view(configuration)


@router.get(
    "/stations/{station_id}/configuration",
    operation_id="readStationTemplateConfiguration",
    openapi_extra=needs(Permission.STATION_VIEW),
    responses=_UNAUTHORIZED | {404: problem_openapi_response("工位不存在")},
)
def read_a_station_template_configuration(
    station_id: UUID,
    caller: Authorized,
    templates: Annotated[TemplateRepository, Depends(template_dependency)],
    device: Annotated[DeviceTemplateBindingGateway, Depends(template_dependencies.binding_gateway)],
) -> StationTemplateConfigurationView:
    return _configuration_view(
        read_station_configuration(
            station_id=station_id,
            caller=caller,
            templates=templates,
            device=device,
        )
    )


@router.put(
    "/stations/{station_id}/runtime-parameters",
    operation_id="updateStationRuntimeParameters",
    openapi_extra=needs(Permission.STATION_EDIT),
    responses=_UNAUTHORIZED
    | _VALIDATION
    | {
        404: problem_openapi_response("工位不存在"),
        409: problem_openapi_response("工位配置已变化"),
    },
)
def update_a_station_runtime_parameters(
    station_id: UUID,
    request: RuntimeParametersUpdateInput,
    caller: Authorized,
    templates: Annotated[TemplateRepository, Depends(template_dependency)],
    device: Annotated[DeviceTemplateBindingGateway, Depends(template_dependencies.binding_gateway)],
    if_match: Annotated[int, Header(alias="If-Match")],
) -> StationTemplateConfigurationView:
    return _configuration_view(
        update_station_runtime_parameters(
            station_id=station_id,
            mode=request.mode,
            overrides=(None if request.parameters is None else request.parameters.to_domain()),
            expected_station_revision=if_match,
            caller=caller,
            now=datetime.now(UTC),
            templates=templates,
            device=device,
        )
    )


class TemplateConfigurationReportInput(BaseModel):
    """推理机已应用配置的最小事实；请求体也参与主机签名。"""

    model_config = ConfigDict(extra="forbid")

    station_id: UUID
    backend_id: UUID
    version_id: UUID
    sha256: StrictStr = Field(min_length=64, max_length=64)
    config_revision: StrictInt = Field(gt=0)


class TemplateConfigurationReportView(BaseModel):
    accepted: bool
    report: BackendConfigurationStatusView | None
    rejection_code: str | None
    rejection_detail: str | None


@router.post(
    "/configuration-reports",
    operation_id="reportTemplateConfiguration",
    responses={
        200: {
            "description": "主机确认或可持久化的拒绝事实",
            "model": TemplateConfigurationReportView,
        },
        401: problem_openapi_response("推理机身份认证失败"),
        403: problem_openapi_response("推理机不拥有该工位后端"),
        409: problem_openapi_response("工位尚未绑定或现场事实发生冲突"),
        422: problem_openapi_response("上报内容无效"),
    },
)
def report_a_template_configuration(
    request: Request,
    report: TemplateConfigurationReportInput,
    templates: Annotated[TemplateRepository, Depends(template_dependency)],
    host_gateway: Annotated[DeviceHostGateway, Depends(template_dependencies.host_gateway)],
    host_id: Annotated[UUID, Header(alias=INFERENCE_HOST_ID_HEADER)],
    host_timestamp: Annotated[
        str | None, Header(alias=INFERENCE_HOST_TIMESTAMP_HEADER, min_length=1, max_length=32)
    ] = None,
    host_nonce: Annotated[
        str | None, Header(alias=INFERENCE_HOST_NONCE_HEADER, min_length=1, max_length=128)
    ] = None,
    host_signature: Annotated[
        str | None, Header(alias=INFERENCE_HOST_SIGNATURE_HEADER, min_length=1, max_length=4096)
    ] = None,
) -> TemplateConfigurationReportView:
    body = cast(dict[str, object], report.model_dump(mode="json"))
    identity = host_identity_from_headers(
        host_id=host_id,
        method=request.method,
        path=request.url.path,
        body=body,
        timestamp=host_timestamp,
        nonce=host_nonce,
        signature=host_signature,
    )
    result = report_template_configuration(
        host=identity,
        station_id=report.station_id,
        backend_id=report.backend_id,
        reported_version_id=report.version_id,
        reported_sha256=report.sha256,
        reported_config_revision=report.config_revision,
        now=datetime.now(UTC),
        templates=templates,
        host_gateway=host_gateway,
    )
    report_view = None
    if result.report is not None:
        binding = templates.binding_by_station(report.station_id)
        confirmed = (
            result.accepted
            and binding is not None
            and result.report.reported_version_id == binding.desired_version_id
            and result.report.reported_sha256 == binding.desired_sha256
            and result.report.reported_config_revision == binding.desired_config_revision
        )
        report_view = BackendConfigurationStatusView(
            backend_id=result.report.backend_id,
            host_id=result.report.host_id,
            status=(
                TemplateBindingStatus.CONFIRMED
                if confirmed
                else (
                    TemplateBindingStatus.REJECTED
                    if not result.accepted or result.report.last_rejection_code is not None
                    else TemplateBindingStatus.WAITING
                )
            ),
            reported_version_id=result.report.reported_version_id,
            reported_sha256=result.report.reported_sha256,
            reported_config_revision=result.report.reported_config_revision,
            reported_at=result.report.reported_at,
            rejection_code=result.report.last_rejection_code,
            rejection_detail=result.report.last_rejection_detail,
            rejection_at=result.report.last_rejection_at,
        )
    return TemplateConfigurationReportView(
        accepted=result.accepted,
        report=report_view,
        rejection_code=(None if result.rejection_code is None else result.rejection_code.value),
        rejection_detail=result.rejection_detail,
    )
