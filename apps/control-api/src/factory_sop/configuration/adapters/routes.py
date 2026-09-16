"""推理机配置拉取 HTTP 适配器。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, status

from factory_sop.configuration.usecases import (
    ConfigurationAssemblyError,
    ConfigurationHostRepository,
    configuration_for_host,
    register_confirmed_configuration,
)
from factory_sop.device.adapters import dependencies as device_dependencies
from factory_sop.device.api import authenticate_host, host_identity_from_headers
from factory_sop.persistence import RequestSession
from factory_sop.template.adapters import dependencies as template_dependencies
from nvsop_contracts import (
    DECISION_REPORT_CONTRACT_VERSION,
    configuration_from_wire,
    configuration_to_wire,
)

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
    inference_host_id: Annotated[str | None, Header(alias="X-Inference-Host-ID")] = None,
    inference_host_timestamp: Annotated[
        str | None, Header(alias="X-Inference-Host-Timestamp")
    ] = None,
    inference_host_nonce: Annotated[str | None, Header(alias="X-Inference-Host-Nonce")] = None,
    inference_host_signature: Annotated[
        str | None, Header(alias="X-Inference-Host-Signature")
    ] = None,
) -> dict[str, object]:
    """先认证主机，再只组装该主机的拓扑和模板。"""
    if inference_host_id != str(host_id):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="host identity mismatch"
        )
    hosts = device_dependencies.hosts(session)
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
        authenticate_host(host=identity, now=datetime.now(UTC), hosts=hosts)
        bundle = configuration_for_host(
            host_id=host_id,
            generated_at=datetime.now(UTC),
            hosts=cast(ConfigurationHostRepository, hosts),
            backends=device_dependencies.backends(session),
            stations=device_dependencies.stations(session),
            cameras=device_dependencies.cameras(session),
            connectors=device_dependencies.connectors(session),
            points=device_dependencies.points(session),
            templates=template_dependencies.templates(session),
        )
    except ConfigurationAssemblyError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return configuration_to_wire(bundle)


@router.post(
    "/{host_id}/confirmed-configuration",
    operation_id="confirmInferenceHostConfigurationHistory",
    response_model=dict[str, object],
)
def confirm_inference_host_configuration_history(
    request: Request,
    host_id: UUID,
    body: dict[str, object],
    session: RequestSession,
    inference_host_id: Annotated[str | None, Header(alias="X-Inference-Host-ID")] = None,
    inference_host_timestamp: Annotated[
        str | None, Header(alias="X-Inference-Host-Timestamp")
    ] = None,
    inference_host_nonce: Annotated[str | None, Header(alias="X-Inference-Host-Nonce")] = None,
    inference_host_signature: Annotated[
        str | None, Header(alias="X-Inference-Host-Signature")
    ] = None,
) -> dict[str, object]:
    """签名确认 Edge 保存的已下发 bundle，并协商 historical decision report v2。"""
    if inference_host_id != str(host_id):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="host identity mismatch"
        )
    hosts = device_dependencies.hosts(session)
    identity = host_identity_from_headers(
        host_id=host_id,
        method=request.method,
        path=request.url.path,
        body=body,
        timestamp=inference_host_timestamp,
        nonce=inference_host_nonce,
        signature=inference_host_signature,
    )
    authenticate_host(host=identity, now=datetime.now(UTC), hosts=hosts)
    try:
        bundle = configuration_from_wire(body)
        register_confirmed_configuration(
            host_id=host_id,
            bundle=bundle,
            hosts=cast(ConfigurationHostRepository, hosts),
        )
    except ConfigurationAssemblyError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error
    return {"decision_report_contract_version": DECISION_REPORT_CONTRACT_VERSION}


__all__ = ["router"]
