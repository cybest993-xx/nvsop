"""主机上报入口和仅供看板使用的 SSE 投影。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from factory_sop.auth.api import Authorized, Permission, authorize, needs
from factory_sop.device.adapters import dependencies as device_dependencies
from factory_sop.device.api import authenticate_host, host_identity_from_headers
from factory_sop.monitor.adapters import dependencies
from factory_sop.monitor.errors import MonitorRefusedError
from factory_sop.monitor.repository import MonitorRepository
from factory_sop.monitor.usecases import (
    mirror_decision,
    mirror_health,
    sse_snapshot_state,
    sse_stream,
)
from factory_sop.persistence import RequestSession
from nvsop_contracts import (
    ReportedDecision,
    ReportedHealth,
    reported_decision_from_wire,
    reported_health_from_wire,
)

router = APIRouter(prefix="/monitor", tags=["monitor"])


@router.post("/reported-decisions", operation_id="reportMonitorDecision")
def report_monitor_decision(
    request: Request,
    body: dict[str, object],
    session: RequestSession,
    monitor: Annotated[MonitorRepository, Depends(dependencies.monitor)],
    inference_host_id: Annotated[str | None, Header(alias="X-Inference-Host-ID")] = None,
    inference_host_timestamp: Annotated[
        str | None, Header(alias="X-Inference-Host-Timestamp")
    ] = None,
    inference_host_nonce: Annotated[str | None, Header(alias="X-Inference-Host-Nonce")] = None,
    inference_host_signature: Annotated[
        str | None, Header(alias="X-Inference-Host-Signature")
    ] = None,
) -> dict[str, object]:
    try:
        report: ReportedDecision = reported_decision_from_wire(body)
        host_id = UUID(report.host_id)
    except (ValueError, TypeError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error
    _authenticate_report_host(
        request=request,
        session=session,
        body=body,
        host_id=host_id,
        inference_host_id=inference_host_id,
        inference_host_timestamp=inference_host_timestamp,
        inference_host_nonce=inference_host_nonce,
        inference_host_signature=inference_host_signature,
    )
    try:
        inserted = mirror_decision(
            report,
            received_at=datetime.now(UTC),
            monitor=monitor,
            host_gateway=device_dependencies.host_gateway(session),
        )
    except MonitorRefusedError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return {"accepted": True, "duplicate": not inserted, "event_id": report.event_id}


@router.post("/health", operation_id="reportMonitorHealth")
def report_monitor_health(
    request: Request,
    body: dict[str, object],
    session: RequestSession,
    monitor: Annotated[MonitorRepository, Depends(dependencies.monitor)],
    inference_host_id: Annotated[str | None, Header(alias="X-Inference-Host-ID")] = None,
    inference_host_timestamp: Annotated[
        str | None, Header(alias="X-Inference-Host-Timestamp")
    ] = None,
    inference_host_nonce: Annotated[str | None, Header(alias="X-Inference-Host-Nonce")] = None,
    inference_host_signature: Annotated[
        str | None, Header(alias="X-Inference-Host-Signature")
    ] = None,
) -> dict[str, object]:
    try:
        report: ReportedHealth = reported_health_from_wire(body)
        host_id = UUID(report.host_id)
    except (ValueError, TypeError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error
    _authenticate_report_host(
        request=request,
        session=session,
        body=body,
        host_id=host_id,
        inference_host_id=inference_host_id,
        inference_host_timestamp=inference_host_timestamp,
        inference_host_nonce=inference_host_nonce,
        inference_host_signature=inference_host_signature,
    )
    try:
        inserted = mirror_health(
            report,
            received_at=datetime.now(UTC),
            monitor=monitor,
            host_gateway=device_dependencies.host_gateway(session),
        )
    except MonitorRefusedError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return {"accepted": True, "duplicate": not inserted, "event_id": report.event_id}


@router.get(
    "/stream",
    operation_id="streamMonitorEvents",
    openapi_extra=needs(Permission.MONITOR_VIEW),
)
def stream_monitor_events(
    caller: Authorized,
    monitor: Annotated[MonitorRepository, Depends(dependencies.streaming_monitor)],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    once: bool = Query(default=False),
) -> StreamingResponse:
    authorize(caller, Permission.MONITOR_VIEW)
    snapshot = sse_snapshot_state(monitor, last_event_id=last_event_id)

    def events() -> Iterator[str]:
        yield from snapshot.frames
        if not once:
            yield from sse_stream(
                monitor,
                after=snapshot.decision_after,
                decision_event_id=snapshot.decision_event_id,
                health_after=snapshot.health_after,
                health_event_id=snapshot.health_event_id,
                decision_sequence=snapshot.decision_sequence,
                health_sequence=snapshot.health_sequence,
            )

    return StreamingResponse(events(), media_type="text/event-stream")


def _authenticate_report_host(
    *,
    request: Request,
    session: RequestSession,
    body: Mapping[str, object],
    host_id: UUID,
    inference_host_id: str | None,
    inference_host_timestamp: str | None,
    inference_host_nonce: str | None,
    inference_host_signature: str | None,
) -> None:
    """统一认证两类推理机上报，避免报告字段和签名身份分叉。"""
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
    authenticate_host(
        host=identity,
        now=datetime.now(UTC),
        hosts=device_dependencies.hosts(session),
    )


__all__ = ["router"]
