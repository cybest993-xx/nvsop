"""RFC 9457 `application/problem+json`: the one shape every refusal is reported in.

§5.15 fixes the contract, including the extension members `error_code` and `field_errors[]`.
`error_code` is a stable SCREAMING_SNAKE enumeration and is deliberately a different type from
`reason_code`: that one is a judgment's domain reason, and sharing a field name would let the
front end handle 不可判定 as an HTTP failure.

Shared infrastructure, like `observability`. It owns no domain behavior and imports no domain
module; each module's adapter raises its own refusal, and the handlers installed here turn it
into this shape.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from fastapi.responses import JSONResponse
from pydantic import BaseModel

PROBLEM_MEDIA_TYPE = "application/problem+json"

# RFC 9457 §4.2.1: `about:blank` says the status code carries the whole of the semantics, with
# no further type-specific meaning. That is accurate here — `error_code` is what a client
# branches on, and it is a member of the object rather than a URI we would have to host and
# keep resolvable on an air-gapped network.
BLANK_PROBLEM_TYPE: Literal["about:blank"] = "about:blank"


class ApiErrorCode(StrEnum):
    """The stable wire-level failure codes currently emitted by the control plane."""

    # `auth`'s administration refusals. Each names why an operation could not be carried out:
    # the target is gone, the natural key is taken, the request names an unregistered
    # permission, or the operation would leave the system unadministrable.
    ADMINISTRATION_WOULD_BE_LOST = "ADMINISTRATION_WOULD_BE_LOST"
    LOGIN_NAME_TAKEN = "LOGIN_NAME_TAKEN"
    PASSWORD_TOO_SHORT = "PASSWORD_TOO_SHORT"  # pragma: allowlist secret
    PERMISSION_UNREGISTERED = "PERMISSION_UNREGISTERED"
    ROLE_CODE_TAKEN = "ROLE_CODE_TAKEN"
    ROLE_NOT_FOUND = "ROLE_NOT_FOUND"
    USER_NOT_FOUND = "USER_NOT_FOUND"
    ACCOUNT_DEACTIVATED = "ACCOUNT_DEACTIVATED"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    CREDENTIALS_REJECTED = "CREDENTIALS_REJECTED"
    CSRF_TOKEN_INVALID = "CSRF_TOKEN_INVALID"
    INFERENCE_BACKEND_ENDPOINT_TAKEN = "INFERENCE_BACKEND_ENDPOINT_TAKEN"
    INFERENCE_BACKEND_NOT_FOUND = "INFERENCE_BACKEND_NOT_FOUND"
    INFERENCE_BACKEND_DEACTIVATED = "INFERENCE_BACKEND_DEACTIVATED"
    INFERENCE_BACKEND_HAS_CAMERAS = "INFERENCE_BACKEND_HAS_CAMERAS"
    INFERENCE_HOST_DEACTIVATED = "INFERENCE_HOST_DEACTIVATED"
    INFERENCE_HOST_AUTHENTICATION_FAILED = "INFERENCE_HOST_AUTHENTICATION_FAILED"
    INFERENCE_HOST_CREDENTIALS_REMOVED = "INFERENCE_HOST_CREDENTIALS_REMOVED"
    INFERENCE_HOST_HAS_BACKENDS = "INFERENCE_HOST_HAS_BACKENDS"
    INFERENCE_HOST_HAS_PENDING_COMMANDS = "INFERENCE_HOST_HAS_PENDING_COMMANDS"
    INFERENCE_HOST_NAME_TAKEN = "INFERENCE_HOST_NAME_TAKEN"
    INFERENCE_HOST_NOT_FOUND = "INFERENCE_HOST_NOT_FOUND"
    STATION_NOT_FOUND = "STATION_NOT_FOUND"
    STATION_CODE_TAKEN = "STATION_CODE_TAKEN"
    STATION_DEACTIVATED = "STATION_DEACTIVATED"
    STATION_HAS_CAMERAS = "STATION_HAS_CAMERAS"
    CAMERA_NOT_FOUND = "CAMERA_NOT_FOUND"
    CAMERA_HOST_BACKEND_MISMATCH = "CAMERA_HOST_BACKEND_MISMATCH"
    CAMERA_STATION_HOST_CONFLICT = "CAMERA_STATION_HOST_CONFLICT"
    CAMERA_STATION_TEMPLATE_CONFLICT = "CAMERA_STATION_TEMPLATE_CONFLICT"
    CONNECTOR_NOT_FOUND = "CONNECTOR_NOT_FOUND"
    CONNECTOR_NAME_TAKEN = "CONNECTOR_NAME_TAKEN"
    CONNECTOR_DEACTIVATED = "CONNECTOR_DEACTIVATED"
    CONNECTOR_STATION_HOST_CONFLICT = "CONNECTOR_STATION_HOST_CONFLICT"
    CONNECTOR_HAS_POINTS = "CONNECTOR_HAS_POINTS"
    POINT_NOT_FOUND = "POINT_NOT_FOUND"
    POINT_SEMANTIC_LABEL_TAKEN = "POINT_SEMANTIC_LABEL_TAKEN"
    POINT_IDENTITY_TAKEN = "POINT_IDENTITY_TAKEN"
    POINT_CONNECTOR_STATION_MISMATCH = "POINT_CONNECTOR_STATION_MISMATCH"
    STATION_HAS_POINTS = "STATION_HAS_POINTS"
    # 仅错误码标识。
    CONNECTOR_CONFIGURATION_SECRET = "CONNECTOR_CONFIGURATION_SECRET"  # pragma: allowlist secret
    STATION_HAS_CONNECTORS = "STATION_HAS_CONNECTORS"
    INFERENCE_HOST_HAS_CONNECTORS = "INFERENCE_HOST_HAS_CONNECTORS"
    INFERENCE_HOST_HAS_CONFIGURATION_REPORT = "INFERENCE_HOST_HAS_CONFIGURATION_REPORT"
    INFERENCE_BACKEND_HAS_CONFIGURATION_REPORT = "INFERENCE_BACKEND_HAS_CONFIGURATION_REPORT"
    STATION_HAS_TEMPLATES = "STATION_HAS_TEMPLATES"
    STATION_HAS_TEMPLATE_BINDING = "STATION_HAS_TEMPLATE_BINDING"
    STATION_HAS_CONFIGURATION_REPORT = "STATION_HAS_CONFIGURATION_REPORT"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    REQUEST_INVALID = "REQUEST_INVALID"
    SESSION_INVALID = "SESSION_INVALID"
    STALE_REVISION = "STALE_REVISION"
    COMMAND_NOT_FOUND = "COMMAND_NOT_FOUND"
    COMMAND_HOST_MISMATCH = "COMMAND_HOST_MISMATCH"
    COMMAND_CLAIM_REQUIRED = "COMMAND_CLAIM_REQUIRED"
    COMMAND_CLAIM_EXPIRED = "COMMAND_CLAIM_EXPIRED"
    COMMAND_CLAIM_TOKEN_INVALID = "COMMAND_CLAIM_TOKEN_INVALID"
    COMMAND_ALREADY_COMPLETED = "COMMAND_ALREADY_COMPLETED"
    COMMAND_IDEMPOTENCY_CONFLICT = "COMMAND_IDEMPOTENCY_CONFLICT"
    COMMAND_CONFIGURATION_CHANGED = "COMMAND_CONFIGURATION_CHANGED"
    COMMAND_TARGET_NOT_FOUND = "COMMAND_TARGET_NOT_FOUND"
    COMMAND_TARGET_DEACTIVATED = "COMMAND_TARGET_DEACTIVATED"
    TEMPLATE_IMPORT_INVALID = "TEMPLATE_IMPORT_INVALID"
    TEMPLATE_IMPORT_NOT_FOUND = "TEMPLATE_IMPORT_NOT_FOUND"
    TEMPLATE_DRAFT_NOT_FOUND = "TEMPLATE_DRAFT_NOT_FOUND"
    TEMPLATE_DRAFT_INVALID = "TEMPLATE_DRAFT_INVALID"
    TEMPLATE_VERSION_NOT_FOUND = "TEMPLATE_VERSION_NOT_FOUND"
    TEMPLATE_VERSION_INVALID = "TEMPLATE_VERSION_INVALID"
    TEMPLATE_VERSION_ARTIFACT_NOT_FOUND = "TEMPLATE_VERSION_ARTIFACT_NOT_FOUND"
    TEMPLATE_VERSION_STATION_MISMATCH = "TEMPLATE_VERSION_STATION_MISMATCH"
    TEMPLATE_BINDING_INVALID = "TEMPLATE_BINDING_INVALID"
    TEMPLATE_BINDING_NOT_FOUND = "TEMPLATE_BINDING_NOT_FOUND"
    TEMPLATE_BINDING_STATION_TAKEN = "TEMPLATE_BINDING_STATION_TAKEN"
    TEMPLATE_REPORT_HOST_NOT_ALLOWED = "TEMPLATE_REPORT_HOST_NOT_ALLOWED"
    TEMPLATE_REPORT_STATION_UNBOUND = "TEMPLATE_REPORT_STATION_UNBOUND"
    TEMPLATE_REPORT_CONFLICT = "TEMPLATE_REPORT_CONFLICT"


class FieldError(BaseModel):
    """One rejected input, named so the Web form can put the message beside it."""

    field: str
    message: str


class ProblemDocument(BaseModel):
    """RFC 9457 plus the stable control-plane extensions from §5.15."""

    type: Literal["about:blank"] = BLANK_PROBLEM_TYPE
    title: str
    status: int
    error_code: ApiErrorCode
    detail: str | None = None
    field_errors: list[FieldError] | None = None


def problem_openapi_response(description: str) -> dict[str, Any]:
    """Describe one RFC 9457 response without repeating its wire shape at every route."""
    return {
        "description": description,
        # `model` registers the reusable ProblemDocument schema. `create_app` removes the
        # response class's default `application/json` entry after FastAPI builds OpenAPI;
        # keeping the registration here avoids duplicating the schema in each response.
        "model": ProblemDocument,
        "content": {
            PROBLEM_MEDIA_TYPE: {
                "schema": {"$ref": "#/components/schemas/ProblemDocument"},
            }
        },
    }


def problem_response(
    *,
    status: int,
    title: str,
    error_code: ApiErrorCode,
    detail: str | None = None,
    field_errors: list[FieldError] | None = None,
) -> JSONResponse:
    """Build the response for a refusal.

    `title` is the human-readable summary, in Simplified Chinese (Q32) — the front end
    displays it verbatim when it does not recognize `error_code`, which is what §5.15's
    unknown-value fallback requires of every client.
    """
    document = ProblemDocument(
        title=title,
        status=status,
        error_code=error_code,
        detail=detail,
        field_errors=field_errors or None,
    )
    # Absent members are omitted rather than sent as null: RFC 9457 makes them optional, and a
    # null would ask a client to tell "no detail" from "detail is null".
    return JSONResponse(
        status_code=status,
        content=document.model_dump(mode="json", exclude_none=True),
        media_type=PROBLEM_MEDIA_TYPE,
    )
