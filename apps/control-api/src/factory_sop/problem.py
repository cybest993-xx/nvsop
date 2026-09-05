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
    INTERNAL_ERROR = "INTERNAL_ERROR"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    REQUEST_INVALID = "REQUEST_INVALID"
    SESSION_INVALID = "SESSION_INVALID"


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
