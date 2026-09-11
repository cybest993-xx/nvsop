"""标注控制面与基座兼容路径；兼容路径不进入公开 OpenAPI。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_serializer

from factory_sop.auth.api import Authorized, Permission, needs
from factory_sop.dataset import model
from factory_sop.dataset.adapters import dependencies
from factory_sop.dataset.errors import DatasetRefusalCode, DatasetRefusedError
from factory_sop.dataset.repository import DatasetRepository
from factory_sop.dataset.usecases.annotation import (
    AnnotationGatewayResource,
    AnnotationGatewayResourceKind,
    AnnotationMediaKind,
    AnnotationRefusedError,
    AnnotationSubmissionResult,
    authorize_annotation_gateway_resource,
    authorize_annotation_media_resource,
    create_annotation_context,
    encode_annotation_context_token,
    list_action_lists,
    list_annotation_submissions,
    read_action_list,
    read_annotation_context,
    read_annotation_execution,
    read_prepared_annotation_context,
    read_scoped_annotation_submission,
    register_action_list,
    retry_annotation,
    submit_annotation,
)
from factory_sop.job.api import AnnotationJobQueue, ApplicationJob
from factory_sop.problem import problem_openapi_response

router = APIRouter(prefix="/training-datasets", tags=["dataset-annotation"])
context_router = APIRouter(prefix="/annotation-contexts", tags=["dataset-annotation"])
media_router = APIRouter(prefix="/annotation/media", tags=["dataset-annotation"])
gateway_router = APIRouter(prefix="/annotation", tags=["dataset-annotation"])
compatibility_router = APIRouter(prefix="/api/annotation", tags=["annotation-compatibility"])
_CONTROL_API_PREFIX = "/api/v1"


@gateway_router.get("/gateway-authorize", include_in_schema=False)
def authorize_annotation_gateway(
    request: Request,
    caller: Authorized,
    datasets: Annotated[DatasetRepository, Depends(dependencies.datasets)],
) -> Response:
    """供 Nginx 预检请求；HTTP 目标先在此解析，再交给标注用例授权。"""
    original_method = request.headers.get("x-original-method", "GET").upper()
    original_uri = request.headers.get("x-original-uri", "")
    permission = _gateway_permission(original_uri, original_method)
    resource = _gateway_resource_from_uri(original_uri)
    try:
        if permission is not None:
            authorize_annotation_gateway_resource(
                resource=resource,
                permission=permission,
                caller=caller,
                now=datetime.now(UTC),
                datasets=datasets,
                secret=request.app.state.settings.csrf_secret.get_secret_value(),
            )
    except DatasetRefusedError:
        # auth_request 只把 2xx/401/403 当作正常结果；资源拒绝统一隐藏为 403。
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _gateway_permission(original_uri: str, method: str) -> Permission | None:
    """为标注和数据集路径选择前置权限；其他控制面请求只在此处校验会话。"""
    path = urlsplit(original_uri).path.rstrip("/")
    dataset_root = f"{_CONTROL_API_PREFIX}/training-datasets"
    if path == dataset_root or path.startswith(dataset_root + "/"):
        if method == "POST" and _is_dataset_import_path(path, dataset_root):
            return Permission.DATASET_IMPORT
        return (
            Permission.DATASET_EDIT
            if method in {"POST", "PUT", "PATCH", "DELETE"}
            else Permission.DATASET_VIEW
        )
    if (
        path.startswith(f"{_CONTROL_API_PREFIX}/annotation-contexts/")
        or path.startswith(f"{_CONTROL_API_PREFIX}/annotation/")
        or path.startswith("/api/annotation/")
    ):
        return (
            Permission.DATASET_EDIT
            if method in {"POST", "PUT", "PATCH", "DELETE"}
            else Permission.DATASET_VIEW
        )
    return None


def _is_dataset_import_path(path: str, dataset_root: str) -> bool:
    if path == dataset_root:
        return True
    parts = path.split("/")
    if len(parts) == 6 and parts[5] == "members":
        return True
    return len(parts) == 8 and parts[5] == "members" and parts[7] in {"confirm", "retry"}


def _gateway_resource_from_uri(original_uri: str) -> AnnotationGatewayResource:
    path = urlsplit(original_uri).path
    if path.startswith("/api/v1/training-datasets/"):
        parts = path.split("/")
        dataset_id = _parse_gateway_uuid(parts, 4)
        if dataset_id is None:
            return AnnotationGatewayResource(AnnotationGatewayResourceKind.INVALID)
        if len(parts) < 7 or parts[5] != "members" or not parts[6]:
            return AnnotationGatewayResource(
                AnnotationGatewayResourceKind.DATASET,
                dataset_id=dataset_id,
            )
        member_id = _parse_gateway_uuid(parts, 6)
        if member_id is None:
            return AnnotationGatewayResource(AnnotationGatewayResourceKind.INVALID)
        if len(parts) < 9 or parts[7] != "annotations" or not parts[8]:
            return AnnotationGatewayResource(
                AnnotationGatewayResourceKind.MEMBER,
                dataset_id=dataset_id,
                member_id=member_id,
            )
        submission_id = _parse_gateway_uuid(parts, 8)
        if submission_id is None:
            return AnnotationGatewayResource(AnnotationGatewayResourceKind.INVALID)
        return AnnotationGatewayResource(
            AnnotationGatewayResourceKind.SUBMISSION,
            dataset_id=dataset_id,
            member_id=member_id,
            submission_id=submission_id,
        )
    elif path.startswith("/api/v1/annotation-contexts/"):
        parts = path.split("/")
        return AnnotationGatewayResource(
            AnnotationGatewayResourceKind.CONTEXT
            if len(parts) >= 5 and parts[4]
            else AnnotationGatewayResourceKind.INVALID,
            context_token=parts[4] if len(parts) >= 5 and parts[4] else None,
        )
    elif path.startswith("/api/annotation/api/v1/videos/"):
        parts = path.split("/")
        return AnnotationGatewayResource(
            AnnotationGatewayResourceKind.COMPATIBILITY_VIDEO
            if len(parts) >= 7 and parts[6]
            else AnnotationGatewayResourceKind.INVALID,
            context_token=parts[6] if len(parts) >= 7 and parts[6] else None,
        )
    elif path.startswith("/api/annotation/api/v1/annotation-submissions/"):
        parts = path.split("/")
        submission_id = _parse_gateway_uuid(parts, 6)
        if submission_id is None:
            return AnnotationGatewayResource(AnnotationGatewayResourceKind.INVALID)
        if len(parts) < 9 or parts[7] != "executions" or not parts[8]:
            return AnnotationGatewayResource(
                AnnotationGatewayResourceKind.SUBMISSION,
                submission_id=submission_id,
            )
        execution_id = _parse_gateway_uuid(parts, 8)
        if execution_id is None:
            return AnnotationGatewayResource(AnnotationGatewayResourceKind.INVALID)
        clip_index = None
        if len(parts) >= 11 and parts[9] == "clips" and parts[10]:
            try:
                clip_index = int(parts[10])
            except ValueError:
                return AnnotationGatewayResource(AnnotationGatewayResourceKind.INVALID)
        return AnnotationGatewayResource(
            AnnotationGatewayResourceKind.SUBMISSION,
            submission_id=submission_id,
            execution_id=execution_id,
            clip_index=clip_index,
        )
    return AnnotationGatewayResource(AnnotationGatewayResourceKind.UNSCOPED)


def _parse_gateway_uuid(parts: list[str], index: int) -> UUID | None:
    if len(parts) <= index or not parts[index]:
        return None
    try:
        return UUID(parts[index])
    except ValueError:
        return None


ProblemResponses: dict[int | str, dict[str, Any]] = {
    401: problem_openapi_response("需要认证或会话无效"),
    403: problem_openapi_response("权限不足或 CSRF 校验失败"),
    404: problem_openapi_response("训练数据集、视频或标注提交不存在"),
    409: problem_openapi_response("标注当前状态不允许该操作"),
    422: problem_openapi_response("标注输入无效"),
    503: problem_openapi_response("标注基座或对象存储暂时不可用"),
}


class ActionListInput(BaseModel):
    """新增动作清单修订。"""

    model_config = ConfigDict(extra="forbid")

    actions: list[StrictStr] = Field(min_length=1, max_length=500)


class ActionListView(BaseModel):
    dataset_id: UUID
    revision: int
    actions: list[str]
    created_by: UUID
    created_at: datetime

    @field_serializer("created_at")
    def serialize_created_at(self, value: datetime) -> str:
        return _utc(value)


class ActionListHistoryView(BaseModel):
    items: list[ActionListView]


class AnnotationContextInput(BaseModel):
    """选择动作清单修订；省略则使用最新修订。"""

    model_config = ConfigDict(extra="forbid")

    action_list_revision: int | None = Field(default=None, ge=1)


class AnnotationSegmentInput(BaseModel):
    """控制面提交的动作时间段。"""

    model_config = ConfigDict(extra="forbid")

    start: float = Field(ge=0)
    end: float = Field(gt=0)
    action_index: int = Field(ge=0)
    action_description: str = ""


class AnnotationSubmissionInput(BaseModel):
    """控制面提交的完整标注内容。"""

    model_config = ConfigDict(extra="forbid")

    context_token: StrictStr = Field(min_length=1)
    mode: model.AnnotationMode
    segments: list[AnnotationSegmentInput] = Field(min_length=1)


class AnnotationJobView(BaseModel):
    id: UUID
    job_type: str
    status: str
    member_id: UUID
    attempt_id: UUID
    failure_code: str | None
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def serialize_datetime(self, value: datetime) -> str:
        return _utc(value)


class AnnotationExecutionView(BaseModel):
    id: UUID
    submission_id: UUID
    generation: int
    job_id: UUID | None
    status: str
    clips: list[dict[str, Any]]
    failure_code: str | None
    failure_detail: str | None
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def serialize_datetime(self, value: datetime) -> str:
        return _utc(value)


class AnnotationSubmissionView(BaseModel):
    id: UUID
    dataset_id: UUID
    member_id: UUID
    context_id: UUID
    revision: int
    action_list_revision: int
    source_object_version_id: str
    source_sha256: str
    idempotency_key: str
    mode: str
    segments: list[AnnotationSegmentInput]
    raw_segments: list[dict[str, Any]]
    created_by: UUID
    created_at: datetime
    executions: list[AnnotationExecutionView]

    @field_serializer("created_at")
    def serialize_created_at(self, value: datetime) -> str:
        return _utc(value)


class AnnotationAcceptedView(BaseModel):
    submission: AnnotationSubmissionView
    execution: AnnotationExecutionView
    job: AnnotationJobView


class AnnotationContextView(BaseModel):
    context_token: str
    dataset_id: UUID
    member_id: UUID
    action_list_revision: int
    annotation_revision: int
    source_object_version_id: str
    source_sha256: str
    derived_video_size: int | None
    derived_video_sha256: str | None
    derived_video_duration_seconds: float | None
    preparation_job_id: UUID | None
    preparation_status: str
    preparation_failure_code: str | None
    preparation_failure_detail: str | None
    original_filename: str
    source: str
    duration_seconds: float | None
    actions: list[str]
    video_url: str
    initial_timestamps: list[AnnotationSegmentInput]
    two_operator_mode: bool
    expires_at: datetime
    latest_submission: AnnotationSubmissionView | None

    @field_serializer("expires_at")
    def serialize_expires_at(self, value: datetime) -> str:
        return _utc(value)


class AnnotationHistoryView(BaseModel):
    items: list[AnnotationSubmissionView]


class LegacySplitInput(BaseModel):
    """基座 React 控件现有的 camelCase 请求体。"""

    model_config = ConfigDict(extra="forbid")

    timestamps: list[dict[str, Any]] = Field(min_length=1)
    two_operator_mode: bool = Field(alias="twoOperatorMode")


@router.post(
    "/{dataset_id}/action-list",
    response_model=ActionListView,
    status_code=status.HTTP_201_CREATED,
    operation_id="registerDatasetActionList",
    openapi_extra=needs(Permission.DATASET_EDIT),
    responses=ProblemResponses,
)
def register_a_dataset_action_list(
    dataset_id: UUID,
    submitted: ActionListInput,
    caller: Authorized,
    datasets: Annotated[DatasetRepository, Depends(dependencies.datasets)],
) -> ActionListView:
    value = register_action_list(
        dataset_id=dataset_id,
        actions=submitted.actions,
        caller=caller,
        now=datetime.now(UTC),
        datasets=datasets,
    )
    return _action_list_view(value)


@router.get(
    "/{dataset_id}/action-list",
    response_model=ActionListView,
    operation_id="readDatasetActionList",
    openapi_extra=needs(Permission.DATASET_VIEW),
    responses=ProblemResponses,
)
def read_a_dataset_action_list(
    dataset_id: UUID,
    caller: Authorized,
    datasets: Annotated[DatasetRepository, Depends(dependencies.datasets)],
) -> ActionListView:
    return _action_list_view(
        read_action_list(dataset_id=dataset_id, caller=caller, datasets=datasets)
    )


@router.get(
    "/{dataset_id}/action-list/versions",
    response_model=ActionListHistoryView,
    operation_id="listDatasetActionListVersions",
    openapi_extra=needs(Permission.DATASET_VIEW),
    responses=ProblemResponses,
)
def list_dataset_action_list_versions(
    dataset_id: UUID,
    caller: Authorized,
    datasets: Annotated[DatasetRepository, Depends(dependencies.datasets)],
) -> ActionListHistoryView:
    return ActionListHistoryView(
        items=[
            _action_list_view(value)
            for value in list_action_lists(
                dataset_id=dataset_id,
                caller=caller,
                datasets=datasets,
            )
        ]
    )


@router.post(
    "/{dataset_id}/members/{member_id}/annotation-context",
    response_model=AnnotationContextView,
    status_code=status.HTTP_201_CREATED,
    operation_id="createAnnotationContext",
    openapi_extra=needs(Permission.DATASET_VIEW, Permission.DATASET_EDIT),
    responses=ProblemResponses,
)
def create_a_context(
    dataset_id: UUID,
    member_id: UUID,
    submitted: AnnotationContextInput,
    caller: Authorized,
    request: Request,
    datasets: Annotated[DatasetRepository, Depends(dependencies.datasets)],
    jobs: Annotated[AnnotationJobQueue, Depends(dependencies.annotation_jobs)],
) -> AnnotationContextView:
    settings = request.app.state.settings
    context = create_annotation_context(
        dataset_id=dataset_id,
        member_id=member_id,
        action_list_revision=submitted.action_list_revision,
        caller=caller,
        now=datetime.now(UTC),
        datasets=datasets,
        secret=settings.csrf_secret.get_secret_value(),
        ttl_seconds=settings.annotation_context_ttl_seconds,
    )
    job = jobs.get_or_create_annotation_preparation(
        member_id=member_id,
        attempt_id=context.id,
        now=datetime.now(UTC),
    )
    context = replace(context, preparation_job_id=job.id)
    datasets.save_annotation_context(context)
    return _context_view(context=context, token=context.token, request=request, datasets=datasets)


@context_router.get(
    "/{context_token}",
    response_model=AnnotationContextView,
    operation_id="readAnnotationContext",
    openapi_extra=needs(Permission.DATASET_VIEW),
    responses=ProblemResponses,
)
def read_a_context(
    context_token: str,
    caller: Authorized,
    request: Request,
    datasets: Annotated[DatasetRepository, Depends(dependencies.datasets)],
) -> AnnotationContextView:
    context = read_annotation_context(
        token=context_token,
        caller=caller,
        now=datetime.now(UTC),
        datasets=datasets,
        secret=request.app.state.settings.csrf_secret.get_secret_value(),
    )
    return _context_view(context=context, token=context_token, request=request, datasets=datasets)


@router.post(
    "/{dataset_id}/members/{member_id}/annotations",
    response_model=AnnotationAcceptedView,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="submitAnnotation",
    openapi_extra=needs(Permission.DATASET_EDIT),
    responses=ProblemResponses,
)
def submit_an_annotation(
    dataset_id: UUID,
    member_id: UUID,
    submitted: AnnotationSubmissionInput,
    caller: Authorized,
    request: Request,
    datasets: Annotated[DatasetRepository, Depends(dependencies.datasets)],
    jobs: Annotated[AnnotationJobQueue, Depends(dependencies.annotation_jobs)],
    if_match: Annotated[int, Header(alias="If-Match")],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> AnnotationAcceptedView:
    parsed = submitted
    if idempotency_key is None:
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_IDEMPOTENCY_CONFLICT,
            detail="标注提交必须携带 Idempotency-Key",
        )
    result = submit_annotation(
        dataset_id=dataset_id,
        member_id=member_id,
        context_token=parsed.context_token,
        raw_segments=[item.model_dump(mode="python") for item in parsed.segments],
        mode=parsed.mode,
        idempotency_key=idempotency_key,
        caller=caller,
        now=datetime.now(UTC),
        datasets=datasets,
        jobs=jobs,
        secret=request.app.state.settings.csrf_secret.get_secret_value(),
        expected_revision=if_match,
    )
    return _accepted_view(result, datasets=datasets)


@router.get(
    "/{dataset_id}/members/{member_id}/annotations",
    response_model=AnnotationHistoryView,
    operation_id="listAnnotations",
    openapi_extra=needs(Permission.DATASET_VIEW),
    responses=ProblemResponses,
)
def list_the_annotations(
    dataset_id: UUID,
    member_id: UUID,
    caller: Authorized,
    datasets: Annotated[DatasetRepository, Depends(dependencies.datasets)],
) -> AnnotationHistoryView:
    return AnnotationHistoryView(
        items=[
            _submission_view(value, datasets=datasets)
            for value in list_annotation_submissions(
                dataset_id=dataset_id,
                member_id=member_id,
                caller=caller,
                datasets=datasets,
            )
        ]
    )


@router.get(
    "/{dataset_id}/members/{member_id}/annotations/{submission_id}",
    response_model=AnnotationSubmissionView,
    operation_id="readAnnotation",
    openapi_extra=needs(Permission.DATASET_VIEW),
    responses=ProblemResponses,
)
def read_an_annotation(
    dataset_id: UUID,
    member_id: UUID,
    submission_id: UUID,
    caller: Authorized,
    datasets: Annotated[DatasetRepository, Depends(dependencies.datasets)],
) -> AnnotationSubmissionView:
    value = read_scoped_annotation_submission(
        dataset_id=dataset_id,
        member_id=member_id,
        submission_id=submission_id,
        caller=caller,
        datasets=datasets,
    )
    return _submission_view(value, datasets=datasets)


@router.post(
    "/{dataset_id}/members/{member_id}/annotations/{submission_id}/retry",
    response_model=AnnotationAcceptedView,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="retryAnnotation",
    openapi_extra=needs(Permission.DATASET_EDIT),
    responses=ProblemResponses,
)
def retry_an_annotation(
    dataset_id: UUID,
    member_id: UUID,
    submission_id: UUID,
    caller: Authorized,
    datasets: Annotated[DatasetRepository, Depends(dependencies.datasets)],
    jobs: Annotated[AnnotationJobQueue, Depends(dependencies.annotation_jobs)],
) -> AnnotationAcceptedView:
    result = retry_annotation(
        submission_id=submission_id,
        caller=caller,
        now=datetime.now(UTC),
        datasets=datasets,
        jobs=jobs,
        dataset_id=dataset_id,
        member_id=member_id,
    )
    return _accepted_view(result, datasets=datasets)


@compatibility_router.get("/api/v1/videos/{context_token}", include_in_schema=False)
def compatibility_video_metadata(
    context_token: str,
    caller: Authorized,
    request: Request,
    datasets: Annotated[DatasetRepository, Depends(dependencies.datasets)],
) -> dict[str, Any]:
    prepared = read_prepared_annotation_context(
        token=context_token,
        caller=caller,
        now=datetime.now(UTC),
        datasets=datasets,
        secret=request.app.state.settings.csrf_secret.get_secret_value(),
    )
    context, member, actions = prepared.context, prepared.member, prepared.actions
    return {
        "id": context_token,
        "filename": member.original_filename,
        "file_path": "annotation-resource",
        "file_size": context.upstream_video_size or member.actual_size or member.declared_size,
        "upload_time": member.created_at,
        "mime_type": "video/mp4",
        "action_list_revision": actions.revision,
    }


@compatibility_router.get("/api/v1/videos/{context_token}/download", include_in_schema=False)
def compatibility_video_download(
    context_token: str,
    caller: Authorized,
    request: Request,
    datasets: Annotated[DatasetRepository, Depends(dependencies.datasets)],
) -> RedirectResponse:
    read_prepared_annotation_context(
        token=context_token,
        caller=caller,
        now=datetime.now(UTC),
        datasets=datasets,
        secret=request.app.state.settings.csrf_secret.get_secret_value(),
    )
    return RedirectResponse(
        url=_media_url(request, context_token),
        status_code=status.HTTP_302_FOUND,
    )


@compatibility_router.get(
    "/api/v1/annotation-submissions/{submission_id}/executions/{execution_id}/clips/{clip_index}/download",
    include_in_schema=False,
)
def compatibility_clip_download(
    submission_id: UUID,
    execution_id: UUID,
    clip_index: int,
    caller: Authorized,
    request: Request,
    datasets: Annotated[DatasetRepository, Depends(dependencies.datasets)],
) -> RedirectResponse:
    authorize_annotation_media_resource(
        kind=AnnotationMediaKind.CLIP,
        resource=f"{submission_id}:{execution_id}:{clip_index}",
        caller=caller,
        now=datetime.now(UTC),
        datasets=datasets,
        secret=request.app.state.settings.csrf_secret.get_secret_value(),
    )
    return RedirectResponse(
        url=_clip_media_url(
            request,
            submission_id=submission_id,
            execution_id=execution_id,
            clip_index=clip_index,
        ),
        status_code=status.HTTP_302_FOUND,
    )


@compatibility_router.get(
    "/api/v1/annotation-submissions/{submission_id}/executions/{execution_id}/download-all",
    include_in_schema=False,
)
def compatibility_download_all(
    submission_id: UUID,
    execution_id: UUID,
    caller: Authorized,
    request: Request,
    datasets: Annotated[DatasetRepository, Depends(dependencies.datasets)],
) -> RedirectResponse:
    authorize_annotation_media_resource(
        kind=AnnotationMediaKind.ARCHIVE,
        resource=f"{submission_id}:{execution_id}",
        caller=caller,
        now=datetime.now(UTC),
        datasets=datasets,
        secret=request.app.state.settings.csrf_secret.get_secret_value(),
    )
    return RedirectResponse(
        url=_archive_media_url(
            request,
            submission_id=submission_id,
            execution_id=execution_id,
        ),
        status_code=status.HTTP_302_FOUND,
    )


@compatibility_router.get(
    "/api/v1/annotation-submissions/{submission_id}/executions/{execution_id}",
    include_in_schema=False,
)
def compatibility_execution_status(
    submission_id: UUID,
    execution_id: UUID,
    caller: Authorized,
    datasets: Annotated[DatasetRepository, Depends(dependencies.datasets)],
) -> dict[str, Any]:
    execution = read_annotation_execution(
        submission_id=submission_id,
        execution_id=execution_id,
        caller=caller,
        datasets=datasets,
    )
    return {
        "status": execution.status.value,
        "clips": list(execution.clips),
        "failure_code": execution.failure_code,
        "failure_detail": execution.failure_detail,
    }


@compatibility_router.post(
    "/api/v1/videos/{context_token}/split",
    include_in_schema=False,
    openapi_extra=needs(Permission.DATASET_EDIT),
)
def compatibility_split(
    context_token: str,
    submitted: LegacySplitInput,
    caller: Authorized,
    request: Request,
    datasets: Annotated[DatasetRepository, Depends(dependencies.datasets)],
    jobs: Annotated[AnnotationJobQueue, Depends(dependencies.annotation_jobs)],
) -> JSONResponse:
    context = read_annotation_context(
        token=context_token,
        caller=caller,
        now=datetime.now(UTC),
        datasets=datasets,
        secret=request.app.state.settings.csrf_secret.get_secret_value(),
    )
    # 旧控件没有幂等头；对同一上下文和完整请求生成稳定键，仍落入同一个 dataset 用例。
    key = _legacy_idempotency_key(context_token, submitted.model_dump(by_alias=True))
    result = submit_annotation(
        dataset_id=context.dataset_id,
        member_id=context.member_id,
        context_token=context_token,
        raw_segments=submitted.timestamps,
        mode=(
            model.AnnotationMode.TWO_OPERATOR
            if submitted.two_operator_mode
            else model.AnnotationMode.SINGLE_OPERATOR
        ),
        idempotency_key=key,
        caller=caller,
        now=datetime.now(UTC),
        datasets=datasets,
        jobs=jobs,
        secret=request.app.state.settings.csrf_secret.get_secret_value(),
    )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "status": "accepted",
            "submission_id": str(result.submission.id),
            "execution_id": str(result.execution.id),
            "job_id": str(result.job.id),
            "generation": result.execution.generation,
            "clips": [],
            "poll_url": (
                "/api/annotation/api/v1/annotation-submissions/"
                f"{result.submission.id}/executions/{result.execution.id}"
            ),
        },
    )


@compatibility_router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
    openapi_extra=needs(Permission.DATASET_EDIT),
    responses=ProblemResponses,
)
def reject_unsupported_compatibility_operation(path: str, caller: Authorized) -> Response:
    """拒绝基座上传、清空、删除和训练等未开放操作。"""
    del path, caller
    raise AnnotationRefusedError(
        DatasetRefusalCode.ANNOTATION_OPERATION_NOT_ALLOWED,
        detail="该标注基座操作未通过产品数据集接口开放",
    )


@media_router.get("/authorize", include_in_schema=False)
def authorize_annotation_media(
    kind: Literal["video", "clip", "archive"],
    resource: str,
    caller: Authorized,
    request: Request,
    datasets: Annotated[DatasetRepository, Depends(dependencies.datasets)],
) -> Response:
    try:
        media_kind = AnnotationMediaKind(kind)
    except ValueError as error:
        raise AnnotationRefusedError(DatasetRefusalCode.RESOURCE_MISMATCH) from error
    authorized = authorize_annotation_media_resource(
        kind=media_kind,
        resource=resource,
        caller=caller,
        now=datetime.now(UTC),
        datasets=datasets,
        secret=request.app.state.settings.csrf_secret.get_secret_value(),
    )
    headers = {"X-Annotation-Upstream-Video-ID": authorized.upstream_video_id}
    if authorized.upstream_clip_id is not None:
        headers["X-Annotation-Upstream-Clip-ID"] = authorized.upstream_clip_id
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers=headers)


@media_router.get("/gateway-authorize", include_in_schema=False)
def authorize_annotation_media_gateway(
    kind: Literal["video", "clip", "archive"],
    resource: str,
    caller: Authorized,
    request: Request,
    datasets: Annotated[DatasetRepository, Depends(dependencies.datasets)],
) -> Response:
    """返回 Nginx auth_request 可识别的媒体拒绝状态。"""
    try:
        return authorize_annotation_media(
            kind=kind,
            resource=resource,
            caller=caller,
            request=request,
            datasets=datasets,
        )
    except DatasetRefusedError:
        return Response(status_code=status.HTTP_403_FORBIDDEN)


def _context_view(
    *,
    context: model.AnnotationContext,
    token: str,
    request: Request,
    datasets: DatasetRepository,
) -> AnnotationContextView:
    member = datasets.member_by_id(context.member_id)
    actions = datasets.action_list_by_revision(
        dataset_id=context.dataset_id,
        revision=context.action_list_revision,
    )
    if member is None or actions is None:
        raise AnnotationRefusedError(DatasetRefusalCode.ANNOTATION_CONTEXT_INVALID)
    submissions = datasets.list_annotation_submissions(
        dataset_id=context.dataset_id,
        member_id=context.member_id,
    )
    submission = max(submissions, key=lambda item: item.revision, default=None)
    effective_token = token or encode_annotation_context_token(
        context,
        secret=request.app.state.settings.csrf_secret.get_secret_value(),
    )
    initial = (
        []
        if submission is None or submission.action_list_revision != context.action_list_revision
        else [
            AnnotationSegmentInput(
                start=item.start,
                end=item.end,
                action_index=item.action_index,
                action_description=item.action_description,
            )
            for item in submission.segments
        ]
    )
    return AnnotationContextView(
        context_token=effective_token,
        dataset_id=context.dataset_id,
        member_id=context.member_id,
        action_list_revision=actions.revision,
        annotation_revision=context.annotation_revision,
        source_object_version_id=context.source_object_version_id,
        source_sha256=context.source_sha256,
        derived_video_size=context.upstream_video_size,
        derived_video_sha256=context.upstream_video_sha256,
        derived_video_duration_seconds=context.upstream_video_duration_seconds,
        preparation_job_id=context.preparation_job_id,
        preparation_status=context.preparation_status,
        preparation_failure_code=context.preparation_failure_code,
        preparation_failure_detail=context.preparation_failure_detail,
        original_filename=member.original_filename,
        source=member.source,
        duration_seconds=member.duration_seconds,
        actions=list(actions.actions),
        video_url=_media_url(request, effective_token),
        initial_timestamps=initial,
        two_operator_mode=submission is not None
        and submission.mode is model.AnnotationMode.TWO_OPERATOR,
        expires_at=context.expires_at,
        latest_submission=(
            _submission_view(submission, datasets=datasets) if submission is not None else None
        ),
    )


def _accepted_view(
    value: AnnotationSubmissionResult, *, datasets: DatasetRepository
) -> AnnotationAcceptedView:
    return AnnotationAcceptedView(
        submission=_submission_view(value.submission, datasets=datasets),
        execution=_execution_view(value.execution),
        job=_job_view(value.job),
    )


def _submission_view(
    value: model.AnnotationSubmission, *, datasets: DatasetRepository
) -> AnnotationSubmissionView:
    return AnnotationSubmissionView(
        id=value.id,
        dataset_id=value.dataset_id,
        member_id=value.member_id,
        context_id=value.context_id,
        revision=value.revision,
        action_list_revision=value.action_list_revision,
        source_object_version_id=value.source_object_version_id,
        source_sha256=value.source_sha256,
        idempotency_key=value.idempotency_key,
        mode=value.mode.value,
        segments=[
            AnnotationSegmentInput(
                start=item.start,
                end=item.end,
                action_index=item.action_index,
                action_description=item.action_description,
            )
            for item in value.segments
        ],
        raw_segments=list(value.raw_segments),
        created_by=value.created_by,
        created_at=value.created_at,
        executions=[
            _execution_view(item) for item in datasets.list_annotation_executions(value.id)
        ],
    )


def _execution_view(value: model.AnnotationExecution) -> AnnotationExecutionView:
    return AnnotationExecutionView(
        id=value.id,
        submission_id=value.submission_id,
        generation=value.generation,
        job_id=value.job_id,
        status=value.status,
        clips=list(value.clips),
        failure_code=value.failure_code,
        failure_detail=value.failure_detail,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


def _job_view(value: ApplicationJob) -> AnnotationJobView:
    return AnnotationJobView(
        id=value.id,
        job_type=value.job_type.value,
        status=value.status,
        member_id=value.member_id,
        attempt_id=value.attempt_id,
        failure_code=value.failure_code,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


def _action_list_view(value: model.ActionListRevision) -> ActionListView:
    return ActionListView(
        dataset_id=value.dataset_id,
        revision=value.revision,
        actions=list(value.actions),
        created_by=value.created_by,
        created_at=value.created_at,
    )


def _legacy_idempotency_key(context_token: str, value: dict[str, Any]) -> str:
    import hashlib
    import json

    encoded = json.dumps(
        {"context": context_token, "request": value},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "legacy-" + hashlib.sha256(encoded).hexdigest()


def _annotation_media_origin(request: Request) -> str:
    origin = request.app.state.settings.annotation_media_origin
    if not isinstance(origin, str) or not origin:
        raise AnnotationRefusedError(
            DatasetRefusalCode.ANNOTATION_BACKEND_UNAVAILABLE,
            detail="标注媒体端点尚未配置",
        )
    return origin.rstrip("/")


def _media_url(request: Request, token: str) -> str:
    return (
        f"{_annotation_media_origin(request)}/annotation/media/videos/"
        f"{quote(token, safe='')}/download"
    )


def _clip_media_url(
    request: Request,
    *,
    submission_id: UUID,
    execution_id: UUID,
    clip_index: int,
) -> str:
    return (
        f"{_annotation_media_origin(request)}/annotation/media/clips/"
        f"{submission_id}/{execution_id}/{clip_index}/download"
    )


def _archive_media_url(request: Request, *, submission_id: UUID, execution_id: UUID) -> str:
    return (
        f"{_annotation_media_origin(request)}/annotation/media/archives/"
        f"{submission_id}/{execution_id}/download"
    )


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
