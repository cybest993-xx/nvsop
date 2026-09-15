"""推理机配置拉取 HTTP 适配器。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from factory_sop.configuration.adapters import dependencies
from factory_sop.configuration.api import (
    ConfigurationAssemblyError,
    ConfigurationSources,
    configuration_for_host,
)
from factory_sop.device.api import host_identity_from_headers
from factory_sop.persistence import RequestSession
from nvsop_contracts import configuration_to_wire

router = APIRouter(prefix="/inference-hosts", tags=["inference-host"])


@router.get(
    "/{host_id}/configuration",
    operation_id="pullInferenceHostConfiguration",
    response_model=dict[str, object],
)
def pull_inference_host_configuration(
    request: Request,
    host_id: UUID,
    session: RequestSession,
    sources: Annotated[ConfigurationSources, Depends(dependencies.sources)],
    inference_host_id: Annotated[str | None, Header(alias="X-Inference-Host-ID")] = None,
    inference_host_timestamp: Annotated[
        str | None, Header(alias="X-Inference-Host-Timestamp")
    ] = None,
    inference_host_nonce: Annotated[str | None, Header(alias="X-Inference-Host-Nonce")] = None,
    inference_host_signature: Annotated[
        str | None, Header(alias="X-Inference-Host-Signature")
    ] = None,
) -> dict[str, object]:
    """先认证主机, 再只组装该主机的拓扑和模板。"""
    if inference_host_id != str(host_id):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="host identity mismatch"
        )
    identity = host_identity_from_headers(
        host_id=host_id,
        method=request.method,
        path=request.url.path,
        body=None,
        timestamp=inference_host_timestamp,
        nonce=inference_host_nonce,
        signature=inference_host_signature,
    )
    try:
        sources.authenticate(host=identity, now=datetime.now(UTC), session=session)
        bundle = configuration_for_host(
            host_id=host_id,
            generated_at=datetime.now(UTC),
            hosts=sources.hosts(session),
            backends=sources.backends(session),
            stations=sources.stations(session),
            cameras=sources.cameras(session),
            connectors=sources.connectors(session),
            points=sources.points(session),
            templates=sources.templates(session),
        )
    except ConfigurationAssemblyError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return configuration_to_wire(bundle)


__all__ = ["router"]
