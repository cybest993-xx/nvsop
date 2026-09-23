"""主机上报入口和仅供看板使用的 SSE 投影。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from factory_sop.auth.api import Authorized, Permission, needs
from factory_sop.device.api import (
    DeviceHistoricalAssignmentGateway,
    DeviceHostGateway,
    host_identity_from_headers,
)
from factory_sop.monitor.adapters import dependencies
from factory_sop.monitor.errors import MonitorRefusedError
from factory_sop.monitor.repository import MonitorRepository, MonitorStreamSource
from factory_sop.monitor.usecases import (
    list_instances,
    mirror_decision,
    mirror_health,
    mirror_instance,
    sse_snapshot_state,
    sse_stream,
)
from factory_sop.responses import DEFAULT_PAGE_SIZE, MAXIMUM_PAGE_SIZE, ItemPage
from nvsop_contracts import (
    ReportedDecision,
    ReportedHealth,
    ReportedSopInstance,
    reported_decision_from_wire,
    reported_health_from_wire,
    reported_sop_instance_from_wire,
    reported_sop_instance_to_wire,
)

router = APIRouter(prefix="/monitor", tags=["monitor"])


@router.post("/reported-decisions", operation_id="reportMonitorDecision")
def report_monitor_decision(
    request: Request,
    body: dict[str, object],
    monitor: Annotated[MonitorRepository, Depends(dependencies.monitor)],
    host_gateway: Annotated[DeviceHostGateway, Depends(dependencies.host_gateway)],
    assignment_gateway: Annotated[
        DeviceHistoricalAssignmentGateway,
        Depends(dependencies.historical_assignment_gateway),
    ],
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
        host_gateway=host_gateway,
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
            host_gateway=host_gateway,
            assignment_gateway=assignment_gateway,
        )
    except MonitorRefusedError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return {"accepted": True, "duplicate": not inserted, "event_id": report.event_id}


@router.post("/health", operation_id="reportMonitorHealth")
def report_monitor_health(
    request: Request,
    body: dict[str, object],
    monitor: Annotated[MonitorRepository, Depends(dependencies.monitor)],
    host_gateway: Annotated[DeviceHostGateway, Depends(dependencies.host_gateway)],
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
        host_gateway=host_gateway,
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
            host_gateway=host_gateway,
        )
    except MonitorRefusedError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return {"accepted": True, "duplicate": not inserted, "event_id": report.event_id}


@router.post("/reported-instances", operation_id="reportMonitorSopInstance")
def report_monitor_instance(
    request: Request,
    body: dict[str, object],
    monitor: Annotated[MonitorRepository, Depends(dependencies.monitor)],
    host_gateway: Annotated[DeviceHostGateway, Depends(dependencies.host_gateway)],
    assignment_gateway: Annotated[
        DeviceHistoricalAssignmentGateway, Depends(dependencies.historical_assignment_gateway)
    ],
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
        report: ReportedSopInstance = reported_sop_instance_from_wire(body)
        host_id = UUID(report.host_id)
    except (ValueError, TypeError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error
    _authenticate_report_host(
        request=request,
        host_gateway=host_gateway,
        body=body,
        host_id=host_id,
        inference_host_id=inference_host_id,
        inference_host_timestamp=inference_host_timestamp,
        inference_host_nonce=inference_host_nonce,
        inference_host_signature=inference_host_signature,
    )
    try:
        inserted = mirror_instance(
            report,
            received_at=datetime.now(UTC),
            monitor=monitor,
            assignment_gateway=assignment_gateway,
        )
    except MonitorRefusedError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return {"accepted": True, "duplicate": not inserted, "event_id": report.event_id}


@router.get(
    "/instances",
    operation_id="listMonitorSopInstances",
    openapi_extra=needs(Permission.MONITOR_VIEW),
)
def list_monitor_instances(
    caller: Authorized,
    monitor: Annotated[MonitorRepository, Depends(dependencies.monitor)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAXIMUM_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ItemPage[dict[str, object]]:
    items, total = list_instances(monitor, caller=caller, page=page, page_size=page_size)
    return ItemPage(
        items=[reported_sop_instance_to_wire(item.report) for item in items],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get(
    "/stream",
    operation_id="streamMonitorEvents",
    openapi_extra=needs(Permission.MONITOR_VIEW),
)
def stream_monitor_events(
    caller: Authorized,
    monitor: Annotated[MonitorRepository, Depends(dependencies.monitor)],
    source: Annotated[
        MonitorStreamSource,
        Depends(dependencies.monitor_stream_source, scope="request"),
    ],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    once: bool = Query(default=False),
) -> StreamingResponse:
    snapshot = sse_snapshot_state(monitor, caller=caller, last_event_id=last_event_id)

    def events() -> Iterator[str]:
        yield from snapshot.frames
        if not once:
            yield from sse_stream(
                source,
                caller=caller,
                decision_sequence=snapshot.decision_sequence,
                health_sequence=snapshot.health_sequence,
            )

    return StreamingResponse(events(), media_type="text/event-stream")


def _authenticate_report_host(
    *,
    request: Request,
    host_gateway: DeviceHostGateway,
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
    host_gateway.authenticate(host=identity, now=datetime.now(UTC))


__all__ = ["router"]
