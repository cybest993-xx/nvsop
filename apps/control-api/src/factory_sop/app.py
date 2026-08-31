"""The center backend's composition root.

`create_app` is where the resolved `Settings`, the diagnostic-logging middleware and the
routers meet. It is the only place allowed to know all of them: a module's use cases take
what they need as arguments, so they stay callable from an ARQ worker or a smoke script and
not only from an HTTP request (§5.15).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import APIRouter, FastAPI, Request, Response

from factory_sop.observability import (
    correlation_scope,
    get_logger,
    new_correlation_id,
)
from factory_sop.settings import Settings

# The literal prefix every control-plane path sits under. Not a version axis: there will be
# no `/api/v2` (ADR-0003).
API_PREFIX = "/api/v1"

# Nginx and each inference host forward this header. Carrying an inbound value rather than
# minting a fresh one is what lets one operator action be followed across processes.
CORRELATION_ID_HEADER = "x-correlation-id"

_logger = get_logger("http")

liveness_router = APIRouter()


@liveness_router.get("/liveness")
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
    app = FastAPI(title="SOP compliance center backend", docs_url=f"{API_PREFIX}/docs")
    app.state.settings = settings
    app.include_router(liveness_router, prefix=API_PREFIX)

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

    return app
