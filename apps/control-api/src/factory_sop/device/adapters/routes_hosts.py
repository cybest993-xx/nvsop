"""推理机资源的配置 CRUD 和可恢复停用接口。

路由只负责校验请求体，并把已授权调用方交给用例；相对用例新增的只是 HTTP 外壳、If-Match 前置条件和
`problem+json` 声明。每条路由用 `needs()` 在 OpenAPI 中声明用例实际执行的权限（§5.15），权限声明
与用例由路由测试共同校验。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response, status
from pydantic import BaseModel, Field, field_validator

from factory_sop.auth.api import Authorized, Permission, needs
from factory_sop.device.adapters.dependencies import backends, connectors, hosts
from factory_sop.device.model import DeviceStatus, InferenceHost, carries_userinfo
from factory_sop.device.repository import (
    ConnectorRepository,
    InferenceBackendRepository,
    InferenceHostRepository,
)
from factory_sop.device.usecases.hosts import (
    create_host,
    deactivate_host,
    delete_host,
    edit_host,
    host_by_identifier,
    list_hosts,
    register_host_identity_key,
    restore_host,
    retire_host_credential,
)
from factory_sop.problem import problem_openapi_response
from factory_sop.responses import DEFAULT_PAGE_SIZE, MAXIMUM_PAGE_SIZE, ItemPage
from nvsop_contracts import validate_host_identity_public_key

router = APIRouter(prefix="/inference-hosts", tags=["device"])

# FastAPI 的 responses 声明类型；下面的共享字典与之保持一致。
ProblemResponses = dict[int | str, dict[str, Any]]

# 未认证或无权限调用方的统一响应。
_UNAUTHORIZED: ProblemResponses = {
    401: problem_openapi_response("Authentication required or session invalid"),
    403: problem_openapi_response("Permission denied or CSRF token invalid"),
}
_VALIDATION: ProblemResponses = {422: problem_openapi_response("Request invalid")}


class HostConfiguration(BaseModel):
    """推理机的完整可编辑配置，与编辑表单提交的形状一致。

    §5.19 的时长和阈值必须有效；带有 `user:password` 的 URL 按 ADR-0008 在契约层拒绝，不能存储
    或回显。
    """

    name: str = Field(min_length=1, max_length=128)
    address: str = Field(min_length=1, max_length=255)
    mediamtx_address: str | None = Field(
        default=None, min_length=1, max_length=255, pattern=r"^https?://"
    )
    recording_window_seconds: int = Field(gt=0)
    disk_watermark_percent: int = Field(ge=1, le=99)

    @field_validator("mediamtx_address")
    @classmethod
    def _no_credentials_in_urls(cls, value: str | None) -> str | None:
        if value is not None and carries_userinfo(value):
            raise ValueError("不能携带用户名或密码")
        return value


class HostStatus(BaseModel):
    """停用和恢复共用的状态子资源。"""

    status: DeviceStatus


class HostIdentityKeyConfiguration(BaseModel):
    """推理机公钥配置；对应的私钥只留在推理机本地。"""

    public_key: str = Field(min_length=1, max_length=4096)

    @field_validator("public_key")
    @classmethod
    def _valid_public_key(cls, value: str) -> str:
        validate_host_identity_public_key(value)
        return value


class InferenceHostCredentialView(BaseModel):
    """仅为保留旧 OpenAPI 响应形状；路径总是返回 410，处理逻辑不会构造凭据。"""

    credential: str
    revision: int


class InferenceHostIdentityKeyView(BaseModel):
    """公钥登记结果；不回显任何私钥或凭据。"""

    revision: int


class InferenceHostView(BaseModel):
    """API 返回的一台推理机；`revision` 用于 If-Match 乐观锁。"""

    id: UUID
    name: str
    address: str
    mediamtx_address: str | None
    recording_window_seconds: int
    disk_watermark_percent: int
    status: DeviceStatus
    revision: int
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime


def _view(host: InferenceHost) -> InferenceHostView:
    return InferenceHostView(
        id=host.id,
        name=host.name,
        address=host.address,
        mediamtx_address=host.mediamtx_address,
        recording_window_seconds=host.recording_window_seconds,
        disk_watermark_percent=host.disk_watermark_percent,
        status=host.status,
        revision=host.revision,
        created_by=host.created_by,
        updated_by=host.updated_by,
        created_at=host.created_at,
        updated_at=host.updated_at,
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    operation_id="createInferenceHost",
    openapi_extra=needs(Permission.INFERENCE_HOST_EDIT),
    responses=_UNAUTHORIZED
    | _VALIDATION
    | {409: problem_openapi_response("Host name already taken")},
)
def create_a_host(
    configuration: HostConfiguration,
    caller: Authorized,
    hosts: Annotated[InferenceHostRepository, Depends(hosts)],
) -> InferenceHostView:
    """登记一台物理推理机。"""
    host = create_host(
        name=configuration.name,
        address=configuration.address,
        mediamtx_address=configuration.mediamtx_address,
        recording_window_seconds=configuration.recording_window_seconds,
        disk_watermark_percent=configuration.disk_watermark_percent,
        caller=caller,
        now=datetime.now(UTC),
        hosts=hosts,
    )
    return _view(host)


@router.get(
    "",
    operation_id="listInferenceHosts",
    openapi_extra=needs(Permission.INFERENCE_HOST_VIEW),
    responses=_UNAUTHORIZED | _VALIDATION,
)
def list_the_hosts(
    caller: Authorized,
    hosts: Annotated[InferenceHostRepository, Depends(hosts)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAXIMUM_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ItemPage[InferenceHostView]:
    """按最新优先返回一页推理机，并使用 §5.15 的分页外壳。"""
    items, total = list_hosts(caller=caller, hosts=hosts, page=page, page_size=page_size)
    return ItemPage(
        items=[_view(host) for host in items], page=page, page_size=page_size, total=total
    )


@router.get(
    "/{host_id}",
    operation_id="readInferenceHost",
    openapi_extra=needs(Permission.INFERENCE_HOST_VIEW),
    responses=_UNAUTHORIZED | _VALIDATION | {404: problem_openapi_response("Host not found")},
)
def read_a_host(
    host_id: UUID,
    caller: Authorized,
    hosts: Annotated[InferenceHostRepository, Depends(hosts)],
) -> InferenceHostView:
    """读取一台推理机。"""
    return _view(host_by_identifier(host_id=host_id, caller=caller, hosts=hosts))


@router.patch(
    "/{host_id}",
    operation_id="editInferenceHost",
    openapi_extra=needs(Permission.INFERENCE_HOST_EDIT),
    responses=_UNAUTHORIZED
    | _VALIDATION
    | {
        404: problem_openapi_response("Host not found"),
        409: problem_openapi_response("Revision moved (STALE_REVISION), or the name is taken"),
    },
)
def edit_a_host(
    host_id: UUID,
    configuration: HostConfiguration,
    caller: Authorized,
    hosts: Annotated[InferenceHostRepository, Depends(hosts)],
    if_match: Annotated[int, Header(alias="If-Match")],
) -> InferenceHostView:
    """替换推理机的完整配置。

    请求必须携带调用方读取的 `If-Match: <revision>`；缺少前置版本就拒绝，不能盲写覆盖。
    """
    edited = edit_host(
        host_id=host_id,
        name=configuration.name,
        address=configuration.address,
        mediamtx_address=configuration.mediamtx_address,
        recording_window_seconds=configuration.recording_window_seconds,
        disk_watermark_percent=configuration.disk_watermark_percent,
        expected_revision=if_match,
        caller=caller,
        now=datetime.now(UTC),
        hosts=hosts,
    )
    return _view(edited)


@router.post(
    "/{host_id}/credential",
    operation_id="rotateInferenceHostCredential",
    deprecated=True,
    openapi_extra=needs(Permission.INFERENCE_HOST_EDIT),
    responses=_UNAUTHORIZED
    | _VALIDATION
    | {
        404: problem_openapi_response("Host not found"),
        409: problem_openapi_response("Revision moved (STALE_REVISION)"),
        410: problem_openapi_response("Host bearer credentials are no longer issued"),
    },
)
def retired_a_host_credential(
    host_id: UUID,
    caller: Authorized,
    hosts: Annotated[InferenceHostRepository, Depends(hosts)],
    if_match: Annotated[int, Header(alias="If-Match")],
) -> InferenceHostCredentialView:
    """保留旧路径以避免静默改写；不再签发或存储 bearer 凭据。"""
    del hosts, if_match
    retire_host_credential(host_id=host_id, caller=caller)


@router.post(
    "/{host_id}/identity-key",
    operation_id="registerInferenceHostIdentityKey",
    openapi_extra=needs(Permission.INFERENCE_HOST_EDIT),
    responses=_UNAUTHORIZED
    | _VALIDATION
    | {
        404: problem_openapi_response("Host not found"),
        409: problem_openapi_response("Revision moved (STALE_REVISION)"),
    },
)
def register_a_host_identity_key(
    host_id: UUID,
    configuration: HostIdentityKeyConfiguration,
    caller: Authorized,
    hosts: Annotated[InferenceHostRepository, Depends(hosts)],
    if_match: Annotated[int, Header(alias="If-Match")],
) -> InferenceHostIdentityKeyView:
    """登记主机公钥并递增配置修订号。"""
    registered = register_host_identity_key(
        host_id=host_id,
        public_key=configuration.public_key,
        expected_revision=if_match,
        caller=caller,
        now=datetime.now(UTC),
        hosts=hosts,
    )
    return InferenceHostIdentityKeyView(revision=registered.revision)


@router.put(
    "/{host_id}/status",
    operation_id="setInferenceHostStatus",
    openapi_extra=needs(Permission.INFERENCE_HOST_EDIT),
    responses=_UNAUTHORIZED
    | _VALIDATION
    | {
        404: problem_openapi_response("Host not found"),
        409: problem_openapi_response("Revision moved (STALE_REVISION)"),
    },
)
def set_the_host_status(
    host_id: UUID,
    requested: HostStatus,
    caller: Authorized,
    hosts: Annotated[InferenceHostRepository, Depends(hosts)],
    if_match: Annotated[int, Header(alias="If-Match")],
) -> InferenceHostView:
    """在一个状态子资源中停用或恢复推理机；两个方向都可恢复且幂等。"""
    match requested.status:
        case DeviceStatus.DEACTIVATED:
            host = deactivate_host(
                host_id=host_id,
                expected_revision=if_match,
                caller=caller,
                now=datetime.now(UTC),
                hosts=hosts,
            )
        case DeviceStatus.ACTIVE:
            host = restore_host(
                host_id=host_id,
                expected_revision=if_match,
                caller=caller,
                now=datetime.now(UTC),
                hosts=hosts,
            )
    return _view(host)


@router.delete(
    "/{host_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="deleteInferenceHost",
    openapi_extra=needs(Permission.INFERENCE_HOST_DELETE),
    responses=_UNAUTHORIZED
    | _VALIDATION
    | {
        404: problem_openapi_response("Host not found"),
        409: problem_openapi_response(
            "Revision moved (STALE_REVISION), or the host still carries backends, connectors, "
            "pending commands, or template history"
        ),
    },
)
def delete_a_host(
    host_id: UUID,
    caller: Authorized,
    hosts: Annotated[InferenceHostRepository, Depends(hosts)],
    backends: Annotated[InferenceBackendRepository, Depends(backends)],
    connector_store: Annotated[ConnectorRepository, Depends(connectors)],
    if_match: Annotated[int, Header(alias="If-Match")],
) -> Response:
    """直接删除推理机；不可逆操作应优先使用可恢复的停用。"""
    delete_host(
        host_id=host_id,
        expected_revision=if_match,
        caller=caller,
        hosts=hosts,
        backends=backends,
        connectors=connector_store,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
