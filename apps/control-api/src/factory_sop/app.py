"""The center backend's composition root.

`create_app` is where the resolved `Settings`, the diagnostic-logging middleware, the CSRF
check, the `problem+json` handlers and the routers meet. It is the only place allowed to know
all of them: a module's use cases take what they need as arguments, so they stay callable from
an ARQ worker or a smoke script and not only from an HTTP request (§5.15).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError

from factory_sop.auth.adapters import role_administration as auth_role_administration
from factory_sop.auth.adapters import routes as auth_routes
from factory_sop.auth.adapters import user_administration as auth_user_administration
from factory_sop.auth.adapters.cookies import CSRF_HEADER
from factory_sop.auth.adapters.dependencies import presented_token
from factory_sop.auth.authorization import AuthorizationRefusedError
from factory_sop.auth.csrf import verify_csrf_token
from factory_sop.auth.errors import (
    AdministrationRefusedError,
    AuthenticationRefusedError,
    administration_problem,
    refusal_problem,
)
from factory_sop.auth.model import SessionPolicy
from factory_sop.device.adapters.routes_backends import router as inference_backends_router
from factory_sop.device.adapters.routes_cameras import router as cameras_router
from factory_sop.device.adapters.routes_commands import (
    connector_router as connector_commands_router,
)
from factory_sop.device.adapters.routes_commands import router as device_commands_router
from factory_sop.device.adapters.routes_connectors import router as connectors_router
from factory_sop.device.adapters.routes_hosts import router as inference_hosts_router
from factory_sop.device.adapters.routes_points import (
    binding_validation_router,
)
from factory_sop.device.adapters.routes_points import (
    router as points_router,
)
from factory_sop.device.adapters.routes_stations import router as stations_router
from factory_sop.device.errors import DeviceRefusedError
from factory_sop.device.errors import refusal_problem as device_refusal_problem
from factory_sop.observability import (
    correlation_scope,
    get_logger,
    new_correlation_id,
)
from factory_sop.problem import (
    PROBLEM_MEDIA_TYPE,
    ApiErrorCode,
    FieldError,
    problem_openapi_response,
    problem_response,
)
from factory_sop.settings import Settings

# The literal prefix every control-plane path sits under. Not a version axis: there will be
# no `/api/v2` (ADR-0003).
API_PREFIX = "/api/v1"

# Nginx and each inference host forward this header. Carrying an inbound value rather than
# minting a fresh one is what lets one operator action be followed across processes.
CORRELATION_ID_HEADER = "x-correlation-id"

# Methods that change something, and therefore need the CSRF token. `GET`, `HEAD` and
# `OPTIONS` are the safe ones; `TRACE` is not in that list because nothing serves it.
MODIFYING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Opening a session is the one modifying request that cannot carry a CSRF token: there is no
# session to derive one from yet, and the credential in the body is what authorizes it. Keyed by
# method **and** path — the session resource answers `DELETE` on this same path, and that one
# must be checked, so a path-only exemption would silently cover the logout as well.
CSRF_EXEMPT_REQUESTS = frozenset({("POST", f"{API_PREFIX}/auth/session")})

_logger = get_logger("http")

liveness_router = APIRouter()


@liveness_router.get("/liveness", operation_id="readLiveness")
async def liveness() -> dict[str, str]:
    """Report that the process is up. Deliberately touches no dependency.

    Readiness — "can this process serve traffic" — needs the database and arrives with it
    (C3). A liveness probe that checked PostgreSQL would have the container restarted for a
    fault that is not the container's.
    """
    return {"status": "alive"}


def create_app(settings: Settings) -> FastAPI:
    """Build the application from already-resolved settings.

    Settings are a parameter, not read here: the entrypoint resolves them once from the
    process environment and start-up fails there, before an application exists to serve a
    request with a half-valid configuration.
    """
    app = FastAPI(
        title="SOP compliance center backend",
        docs_url=f"{API_PREFIX}/docs",
        responses={500: problem_openapi_response("Internal server error")},
    )
    app.state.settings = settings
    # Built here rather than by `Settings`, which sits below the domain in the layering and so
    # carries the configured minutes rather than the type made from them.
    app.state.session_policy = SessionPolicy(
        idle_timeout=timedelta(minutes=settings.session_idle_timeout_minutes),
        absolute_lifetime=timedelta(minutes=settings.session_absolute_lifetime_minutes),
    )
    app.include_router(liveness_router, prefix=API_PREFIX)
    app.include_router(auth_routes.router, prefix=API_PREFIX)
    app.include_router(inference_hosts_router, prefix=API_PREFIX)
    app.include_router(inference_backends_router, prefix=API_PREFIX)
    app.include_router(device_commands_router, prefix=API_PREFIX)
    app.include_router(stations_router, prefix=API_PREFIX)
    app.include_router(cameras_router, prefix=API_PREFIX)
    app.include_router(connectors_router, prefix=API_PREFIX)
    app.include_router(connector_commands_router, prefix=API_PREFIX)
    app.include_router(points_router, prefix=API_PREFIX)
    app.include_router(binding_validation_router, prefix=API_PREFIX)
    app.include_router(auth_role_administration.router, prefix=API_PREFIX)
    app.include_router(auth_user_administration.router, prefix=API_PREFIX)

    @app.exception_handler(AuthenticationRefusedError)
    async def refused(request: Request, error: AuthenticationRefusedError) -> Response:
        """Report an `auth` refusal as `problem+json` (§5.15).

        Every protected route answers an anonymous caller through this one handler, so the
        shape does not depend on which route was asked for. The status and title are the
        refusal's own (`errors.refusal_problem`), so a new code cannot arrive without them.
        """
        status, title = refusal_problem(error.code)
        return problem_response(
            status=status,
            title=title,
            error_code=ApiErrorCode(error.code.value),
        )

    @app.exception_handler(AuthorizationRefusedError)
    async def denied(request: Request, error: AuthorizationRefusedError) -> Response:
        """Report a permission denial as 403 `problem+json`.

        403 and never 401: the caller is authenticated, and answering 401 would send the Web
        shell to the login page, where signing in again would change nothing. The permission
        that was missing is deliberately not in the response — it is in the diagnostic line
        (`auth/authorization.py`), because naming it tells an unauthorized caller which
        permission guards the resource.
        """
        return problem_response(
            status=403,
            title="没有执行该操作的权限",
            error_code=ApiErrorCode(error.code.value),
        )

    @app.exception_handler(AdministrationRefusedError)
    async def unprocessable(request: Request, error: AdministrationRefusedError) -> Response:
        """Report an administration refusal, carrying the reason the operator needs.

        `detail` comes from the use case rather than being composed here, because the useful
        part is the specific value — which login name is taken, which permission is
        unregistered — and only the use case that refused knows it. The status and title are
        the code's own (`errors.administration_problem`), so a new code cannot arrive without
        them.
        """
        status, title = administration_problem(error.code)
        return problem_response(
            status=status,
            title=title,
            error_code=ApiErrorCode(error.code.value),
            detail=error.detail,
        )

    @app.exception_handler(DeviceRefusedError)
    async def device_refused(request: Request, error: DeviceRefusedError) -> Response:
        """Report a `device` refusal as `problem+json` (§5.15).

        The same one handler per module as `auth`'s: the status and title are the refusal's
        own (`device.errors.refusal_problem`), and `error_code` is the module's code spelled
        into the shared wire enumeration.
        """
        status, title = device_refusal_problem(error.code)
        return problem_response(
            status=status,
            title=title,
            error_code=ApiErrorCode(error.code.value),
            field_errors=[
                FieldError(field=item.field, message=item.message) for item in error.field_errors
            ],
        )

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, error: RequestValidationError) -> Response:
        """Report a rejected body as `problem+json` with `field_errors[]`.

        FastAPI's own handler answers with its own JSON shape, which would make malformed input
        the one failure a client has to parse differently from every other.
        """
        return problem_response(
            status=422,
            title="提交的内容不合要求",
            error_code=ApiErrorCode.REQUEST_INVALID,
            field_errors=[
                FieldError(
                    # `loc` starts with the source (`body`, `query`); the client cares about the
                    # field path within it.
                    field=".".join(str(part) for part in item["loc"][1:]) or "body",
                    message=item["msg"],
                )
                for item in error.errors()
            ],
        )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, error: Exception) -> Response:
        """Answer an unexpected failure as `problem+json`, saying nothing about its cause.

        The traceback goes to the diagnostic log under the request's correlation id; the
        response carries a stable code and no detail, because a message assembled from an
        exception is how a table name or a connection string reaches a browser.
        """
        _logger.exception("http.request.failed", path=request.url.path)
        return problem_response(
            status=500,
            title="服务器内部错误",
            error_code=ApiErrorCode.INTERNAL_ERROR,
        )

    @app.middleware("http")
    async def check_csrf_token(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Require the CSRF header on every modifying request that carries a session cookie.

        Middleware rather than a dependency each route declares: a route that forgot the
        dependency would be unprotected and nothing would say so. Here, a new route is covered
        the moment it exists, and an exemption has to be written down in
        `CSRF_EXEMPT_REQUESTS`.

        A request with no session cookie is not checked. There is nothing to protect — it is
        anonymous, and it will be refused by `Authenticated` if the route needs an identity.

        Registered **before** `bind_correlation_id`, because the last-registered middleware is
        the outermost and a CSRF rejection must happen inside the correlation scope: it is the
        one security-relevant refusal, and the id is how its investigation starts (§5.15).
        """
        exempt = (request.method, request.url.path) in CSRF_EXEMPT_REQUESTS
        if request.method not in MODIFYING_METHODS or exempt:
            return await call_next(request)
        token = presented_token(request)
        if token is None:
            return await call_next(request)
        presented = request.headers.get(CSRF_HEADER)
        if not verify_csrf_token(
            presented,
            session=token,
            secret=settings.csrf_secret.get_secret_value(),
        ):
            _logger.warning(
                "http.request.csrf_rejected",
                method=request.method,
                path=request.url.path,
                # Whether the header was there at all separates "the page did not send one"
                # from "it sent one that did not match", which are different bugs. The value
                # itself is not logged: it is derived from the session token.
                header_present=presented is not None,
            )
            return problem_response(
                status=403,
                title="请求校验失败，请刷新页面后重试",
                error_code=ApiErrorCode.CSRF_TOKEN_INVALID,
            )
        return await call_next(request)

    @app.middleware("http")
    async def bind_correlation_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        correlation_id = request.headers.get(CORRELATION_ID_HEADER) or new_correlation_id()
        with correlation_scope(correlation_id):
            response = await call_next(request)
            _logger.info(
                "http.request.completed",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
            )
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        return response

    default_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        """Keep every documented problem response on RFC 9457's media type only."""
        if app.openapi_schema is None:
            schema = default_openapi()
            for path_item in schema.get("paths", {}).values():
                if not isinstance(path_item, dict):
                    continue
                for operation in path_item.values():
                    if not isinstance(operation, dict):
                        continue
                    responses = operation.get("responses", {})
                    if not isinstance(responses, dict):
                        continue
                    for response_code, response in list(responses.items()):
                        if not isinstance(response, dict):
                            continue
                        if str(response_code) == "422":
                            responses[response_code] = {
                                "description": response.get("description", "Request invalid"),
                                "content": {
                                    PROBLEM_MEDIA_TYPE: {
                                        "schema": {
                                            "$ref": "#/components/schemas/ProblemDocument",
                                        }
                                    }
                                },
                            }
                            continue
                        content = response.get("content", {})
                        if isinstance(content, dict) and PROBLEM_MEDIA_TYPE in content:
                            content.pop("application/json", None)

            # Replacing FastAPI's default response removes its references from operations. Do
            # not retain an orphaned HTTPValidationError component in the published contract.
            validation_ref = "#/components/schemas/HTTPValidationError"
            if validation_ref not in json.dumps(schema):
                components = schema.get("components")
                if isinstance(components, dict):
                    schemas = components.get("schemas")
                    if isinstance(schemas, dict):
                        schemas.pop("HTTPValidationError", None)
            app.openapi_schema = schema
        return app.openapi_schema

    # FastAPI documents replacing this instance method, but its type declares the method as
    # non-assignable. This is the one justified local type escape; keep the rest of the
    # composition root under strict checking.
    app.openapi = openapi  # type: ignore[method-assign]
    return app
