"""The inference host as one resource: configuration CRUD plus the reversible 停用.

The routes are thin: validate the body, hand the use case the caller it authorizes with.
What they add over the use cases is only the HTTP vocabulary — the envelope, the If-Match
precondition, and the `problem+json` declarations. Each route declares the permission its use
case enforces in the OpenAPI extension `needs()` — metadata, never enforcement (§5.15); the
declaration and the use case are held together by behaviour in the route suite.
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
    restore_host,
)
from factory_sop.problem import problem_openapi_response
from factory_sop.responses import DEFAULT_PAGE_SIZE, MAXIMUM_PAGE_SIZE, ItemPage

router = APIRouter(prefix="/inference-hosts", tags=["device"])

# The shape FastAPI's `responses` declares, so the shared dictionaries below satisfy it.
ProblemResponses = dict[int | str, dict[str, Any]]

# What every route here answers an unauthenticated or unauthorized caller with.
_UNAUTHORIZED: ProblemResponses = {
    401: problem_openapi_response("Authentication required or session invalid"),
    403: problem_openapi_response("Permission denied or CSRF token invalid"),
}
_VALIDATION: ProblemResponses = {422: problem_openapi_response("Request invalid")}


class HostConfiguration(BaseModel):
    """The host's whole editable configuration, exactly as the edit form submits it.

    §5.19: durations and thresholds are configured values, and 0 or negative — or a
    watermark of 0 or 100 — is a configuration error refused at save time. A URL with an
    embedded `user:password` is refused at the contract (ADR-0008), before it can be stored
    or echoed back.
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
    """The one field 停用 and 恢复 toggle, as the two values of one subresource."""

    status: DeviceStatus


class InferenceHostView(BaseModel):
    """One host as the API carries it. `revision` is what If-Match echoes."""

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
    """Register a physical inference machine."""
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
    """One page of hosts, newest first, under §5.15's envelope."""
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
    """Read one host."""
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
    """Replace the host's whole configuration.

    The request must carry `If-Match: <revision>` — the revision the caller read. A body
    without it cannot say what it believed it was editing, so it is refused rather than
    allowed to overwrite blind.
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
    """停用 or 恢复 the host — the two values of one field, on one subresource.

    Both directions are reversible and idempotent, which is why they share a route; which use
    case runs is the requested value's, and the match is exhaustive over the enum.
    """
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
            "Revision moved (STALE_REVISION), or the host still carries backends"
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
    """Delete the host outright — the irreversible operation 停用 exists to avoid."""
    delete_host(
        host_id=host_id,
        expected_revision=if_match,
        caller=caller,
        hosts=hosts,
        backends=backends,
        connectors=connector_store,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
