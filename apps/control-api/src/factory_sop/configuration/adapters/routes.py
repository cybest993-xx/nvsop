"""推理机配置拉取 HTTP 适配器。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from factory_sop.configuration.adapters import dependencies
from factory_sop.configuration.composition import (
    ConfigurationAssemblyError,
    configuration_for_host,
    register_confirmed_configuration,
)
from factory_sop.device.api import DeviceConfigurationGateway, host_identity_from_headers
from factory_sop.template.api import TemplateConfigurationGateway
from nvsop_contracts import (
    DECISION_REPORT_CONTRACT_VERSION,
    REPORT_CAPABILITIES_HEADER,
    SOP_INSTANCE_REPORT_CAPABILITY,
    SOP_INSTANCE_REPORT_CONTRACT_VERSION,
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
    device: Annotated[DeviceConfigurationGateway, Depends(dependencies.device_gateway)],
    templates: Annotated[TemplateConfigurationGateway, Depends(dependencies.template_gateway)],
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
        device.authenticate(host=identity, now=datetime.now(UTC))
        bundle = configuration_for_host(
            host_id=host_id,
            generated_at=datetime.now(UTC),
            device=device,
            templates=templates,
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
    device: Annotated[DeviceConfigurationGateway, Depends(dependencies.device_gateway)],
    inference_host_id: Annotated[str | None, Header(alias="X-Inference-Host-ID")] = None,
    inference_host_timestamp: Annotated[
        str | None, Header(alias="X-Inference-Host-Timestamp")
    ] = None,
    inference_host_nonce: Annotated[str | None, Header(alias="X-Inference-Host-Nonce")] = None,
    inference_host_signature: Annotated[
        str | None, Header(alias="X-Inference-Host-Signature")
    ] = None,
    report_capabilities: Annotated[str | None, Header(alias=REPORT_CAPABILITIES_HEADER)] = None,
) -> dict[str, object]:
    """签名确认已下发 bundle，并协商当前 Edge 明确请求的 report capability。"""
    if inference_host_id != str(host_id):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="host identity mismatch"
        )
    identity = host_identity_from_headers(
        host_id=host_id,
        method=request.method,
        path=request.url.path,
        body=body,
        timestamp=inference_host_timestamp,
        nonce=inference_host_nonce,
        signature=inference_host_signature,
    )
    device.authenticate(host=identity, now=datetime.now(UTC))
    try:
        bundle = configuration_from_wire(body)
        register_confirmed_configuration(
            host_id=host_id,
            bundle=bundle,
            device=device,
        )
    except ConfigurationAssemblyError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error
    response: dict[str, object] = {
        "decision_report_contract_version": DECISION_REPORT_CONTRACT_VERSION
    }
    if report_capabilities == SOP_INSTANCE_REPORT_CAPABILITY:
        response["sop_instance_report_contract_version"] = SOP_INSTANCE_REPORT_CONTRACT_VERSION
    return response


__all__ = ["router"]
