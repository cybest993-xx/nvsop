"""The inference backend HTTP surface: placement, lifecycle, and connection testing.

Routes are thin adapters. They validate the wire shape, resolve the caller and repositories,
then cross the device use-case seam. A connection test is a synchronous standard-library
request executed by FastAPI's threadpool; it reports the endpoint's real result and never
blocks configuration from being saved offline.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response, status
from pydantic import BaseModel, Field, field_validator

from factory_sop.auth.api import Authorized, Permission, needs
from factory_sop.device.adapters.dependencies import backends, hosts, probe
from factory_sop.device.model import (
    ConnectionState,
    DeviceStatus,
    InferenceBackend,
    carries_userinfo,
)
from factory_sop.device.probing import ConnectionProbe
from factory_sop.device.repository import InferenceBackendRepository, InferenceHostRepository
from factory_sop.device.usecases.backends import (
    backend_by_identifier,
    create_backend,
    deactivate_backend,
    delete_backend,
    edit_backend,
    list_backends,
    restore_backend,
)
from factory_sop.device.usecases.connection import test_backend_connection
from factory_sop.problem import problem_openapi_response
from factory_sop.responses import DEFAULT_PAGE_SIZE, MAXIMUM_PAGE_SIZE, ItemPage

router = APIRouter(prefix="/inference-backends", tags=["device"])
ProblemResponses = dict[int | str, dict[str, Any]]

_UNAUTHORIZED: ProblemResponses = {
    401: problem_openapi_response("Authentication required or session invalid"),
    403: problem_openapi_response("Permission denied or CSRF token invalid"),
}
_VALIDATION: ProblemResponses = {422: problem_openapi_response("Request invalid")}


class BackendPlacement(BaseModel):
    """The complete placement of one process endpoint."""

    host_id: UUID
    base_url: str = Field(min_length=1, max_length=255, pattern=r"^https?://")

    @field_validator("base_url")
    @classmethod
    def _no_credentials_in_url(cls, value: str) -> str:
        if carries_userinfo(value):
            raise ValueError("不能携带用户名或密码")
        return value


class BackendStatus(BaseModel):
    """The active/deactivated value of the backend status subresource."""

    status: DeviceStatus


class ConnectionView(BaseModel):
    """The last real connection observation, including the endpoint's model identities."""

    state: ConnectionState
    checked_at: datetime | None
    detail: str | None
    self_reported_model_ids: list[str]
    self_reported_at: datetime | None


class InferenceBackendView(BaseModel):
    """One process endpoint and its single reserved template-binding slot."""

    id: UUID
    host_id: UUID
    base_url: str
    template_version_id: UUID | None
    status: DeviceStatus
    connection: ConnectionView
    revision: int
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime


def _view(backend: InferenceBackend) -> InferenceBackendView:
    return InferenceBackendView(
        id=backend.id,
        host_id=backend.host_id,
        base_url=backend.base_url,
        template_version_id=backend.template_version_id,
        status=backend.status,
        connection=ConnectionView(
            state=backend.connection_state,
            checked_at=backend.connection_checked_at,
            detail=backend.connection_detail,
            self_reported_model_ids=list(backend.self_reported_model_ids),
            self_reported_at=backend.self_reported_at,
        ),
        revision=backend.revision,
        created_by=backend.created_by,
        updated_by=backend.updated_by,
        created_at=backend.created_at,
        updated_at=backend.updated_at,
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    operation_id="createInferenceBackend",
    openapi_extra=needs(Permission.INFERENCE_BACKEND_EDIT),
    responses=_UNAUTHORIZED
    | _VALIDATION
    | {
        404: problem_openapi_response("Host not found"),
        409: problem_openapi_response("Host deactivated, or the endpoint is taken"),
    },
)
def create_a_backend(
    placement: BackendPlacement,
    caller: Authorized,
    hosts: Annotated[InferenceHostRepository, Depends(hosts)],
    backends: Annotated[InferenceBackendRepository, Depends(backends)],
) -> InferenceBackendView:
    """Register an endpoint on an existing active host."""
    backend = create_backend(
        host_id=placement.host_id,
        base_url=placement.base_url,
        caller=caller,
        now=datetime.now(UTC),
        hosts=hosts,
        backends=backends,
    )
    return _view(backend)


@router.get(
    "",
    operation_id="listInferenceBackends",
    openapi_extra=needs(Permission.INFERENCE_BACKEND_VIEW),
    responses=_UNAUTHORIZED | _VALIDATION,
)
def list_the_backends(
    caller: Authorized,
    backends: Annotated[InferenceBackendRepository, Depends(backends)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAXIMUM_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    host_id: Annotated[UUID | None, Query()] = None,
) -> ItemPage[InferenceBackendView]:
    """List endpoints, optionally filtered to one host, under the common page envelope."""
    items, total = list_backends(
        caller=caller,
        backends=backends,
        page=page,
        page_size=page_size,
        host_id=host_id,
    )
    return ItemPage(
        items=[_view(backend) for backend in items],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get(
    "/{backend_id}",
    operation_id="readInferenceBackend",
    openapi_extra=needs(Permission.INFERENCE_BACKEND_VIEW),
    responses=_UNAUTHORIZED | _VALIDATION | {404: problem_openapi_response("Backend not found")},
)
def read_a_backend(
    backend_id: UUID,
    caller: Authorized,
    backends: Annotated[InferenceBackendRepository, Depends(backends)],
) -> InferenceBackendView:
    """Read one endpoint, including connection and provenance facts."""
    return _view(backend_by_identifier(backend_id=backend_id, caller=caller, backends=backends))


@router.patch(
    "/{backend_id}",
    operation_id="editInferenceBackend",
    openapi_extra=needs(Permission.INFERENCE_BACKEND_EDIT),
    responses=_UNAUTHORIZED
    | _VALIDATION
    | {
        404: problem_openapi_response("Backend or host not found"),
        409: problem_openapi_response(
            "Revision moved (STALE_REVISION), host deactivated, or endpoint taken"
        ),
    },
)
def edit_a_backend(
    backend_id: UUID,
    placement: BackendPlacement,
    caller: Authorized,
    hosts: Annotated[InferenceHostRepository, Depends(hosts)],
    backends: Annotated[InferenceBackendRepository, Depends(backends)],
    if_match: Annotated[int, Header(alias="If-Match")],
) -> InferenceBackendView:
    """Replace placement at the revision the caller read."""
    edited = edit_backend(
        backend_id=backend_id,
        host_id=placement.host_id,
        base_url=placement.base_url,
        expected_revision=if_match,
        caller=caller,
        now=datetime.now(UTC),
        hosts=hosts,
        backends=backends,
    )
    return _view(edited)


@router.put(
    "/{backend_id}/status",
    operation_id="setInferenceBackendStatus",
    openapi_extra=needs(Permission.INFERENCE_BACKEND_EDIT),
    responses=_UNAUTHORIZED
    | _VALIDATION
    | {
        404: problem_openapi_response("Backend not found"),
        409: problem_openapi_response("Revision moved (STALE_REVISION)"),
    },
)
def set_the_backend_status(
    backend_id: UUID,
    requested: BackendStatus,
    caller: Authorized,
    backends: Annotated[InferenceBackendRepository, Depends(backends)],
    if_match: Annotated[int, Header(alias="If-Match")],
) -> InferenceBackendView:
    """Set the reversible active/deactivated status."""
    match requested.status:
        case DeviceStatus.DEACTIVATED:
            backend = deactivate_backend(
                backend_id=backend_id,
                expected_revision=if_match,
                caller=caller,
                now=datetime.now(UTC),
                backends=backends,
            )
        case DeviceStatus.ACTIVE:
            backend = restore_backend(
                backend_id=backend_id,
                expected_revision=if_match,
                caller=caller,
                now=datetime.now(UTC),
                backends=backends,
            )
    return _view(backend)


@router.post(
    "/{backend_id}/connection-test",
    operation_id="testInferenceBackendConnection",
    openapi_extra=needs(Permission.INFERENCE_BACKEND_EDIT),
    responses=_UNAUTHORIZED
    | _VALIDATION
    | {
        404: problem_openapi_response("Backend not found"),
        409: problem_openapi_response("Revision moved (STALE_REVISION)"),
    },
)
def test_a_backend_connection(
    backend_id: UUID,
    caller: Authorized,
    backends: Annotated[InferenceBackendRepository, Depends(backends)],
    connection_probe: Annotated[ConnectionProbe, Depends(probe)],
    if_match: Annotated[int, Header(alias="If-Match")],
) -> InferenceBackendView:
    """Ask the endpoint once and persist the actual connection result."""
    tested = test_backend_connection(
        backend_id=backend_id,
        expected_revision=if_match,
        caller=caller,
        now=datetime.now(UTC),
        probe=connection_probe,
        backends=backends,
    )
    return _view(tested)


@router.delete(
    "/{backend_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="deleteInferenceBackend",
    openapi_extra=needs(Permission.INFERENCE_BACKEND_DELETE),
    responses=_UNAUTHORIZED
    | _VALIDATION
    | {
        404: problem_openapi_response("Backend not found"),
        409: problem_openapi_response("Revision moved (STALE_REVISION)"),
    },
)
def delete_a_backend(
    backend_id: UUID,
    caller: Authorized,
    backends: Annotated[InferenceBackendRepository, Depends(backends)],
    if_match: Annotated[int, Header(alias="If-Match")],
) -> Response:
    """Delete one endpoint outright; deactivation is the reversible alternative."""
    delete_backend(
        backend_id=backend_id,
        expected_revision=if_match,
        caller=caller,
        backends=backends,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
