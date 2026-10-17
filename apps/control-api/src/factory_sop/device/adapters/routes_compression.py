"""录像压缩的操作员入口与独立主机签名领取/回报入口。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, Self, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, model_validator

from factory_sop.auth.api import Authorized, Permission, needs
from factory_sop.device.adapters.dependencies import hosts, pending_commands
from factory_sop.device.adapters.routes_commands import _EDGE_RESPONSES, _OPERATOR_RESPONSES
from factory_sop.device.api import (
    COMMAND_CLAIM_TOKEN_HEADER,
    COMMAND_LEASE_DURATION,
    INFERENCE_HOST_ID_HEADER,
    INFERENCE_HOST_NONCE_HEADER,
    INFERENCE_HOST_SIGNATURE_HEADER,
    INFERENCE_HOST_TIMESTAMP_HEADER,
    host_identity_from_headers,
)
from factory_sop.device.model import (
    InferenceHostIdentity,
    PendingCommand,
    PendingCommandStatus,
    PendingCommandType,
)
from factory_sop.device.repository import InferenceHostRepository, PendingCommandRepository
from factory_sop.device.usecases.commands import claim_next_command
from factory_sop.device.usecases.compression import (
    complete_compression,
    enqueue_compression,
    read_compression_command,
)
from nvsop_contracts.compression import CompressionAction, compression_result_from_wire

router = APIRouter(tags=["device"])


class CompressionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: CompressionAction


class CompressionObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: str
    requested: str
    segment: str | None
    pid: int | None
    readrate: float | None
    failure_code: str | None
    detail: str | None

    @model_validator(mode="after")
    def shared_contract(self) -> Self:
        compression_result_from_wire(self.model_dump())
        return self


class CompressionCommandView(BaseModel):
    id: UUID
    host_id: UUID
    action: CompressionAction
    status: PendingCommandStatus
    observation: CompressionObservation | None
    observed_at: datetime | None
    failure_code: str | None


class CompressionClaimView(BaseModel):
    command_type: Literal["control_recording_compression"] = "control_recording_compression"
    command_id: str
    action: CompressionAction
    claim_token: str
    lease_expires_at: float


def _view(command: PendingCommand) -> CompressionCommandView:
    return CompressionCommandView(
        id=command.id,
        host_id=command.host_id,
        action=cast(CompressionAction, command.compression_action),
        status=command.status,
        observation=(
            CompressionObservation.model_validate(command.compression_result)
            if command.compression_result is not None
            else None
        ),
        observed_at=command.completed_at,
        failure_code=command.failure_code,
    )


@router.post(
    "/inference-hosts/{host_id}/recording-compression/commands",
    status_code=202,
    operation_id="enqueueRecordingCompression",
    openapi_extra=needs(Permission.INFERENCE_HOST_VIEW),
    responses=_OPERATOR_RESPONSES,
)
def enqueue(
    host_id: UUID,
    body: CompressionInput,
    caller: Authorized,
    host_store: Annotated[InferenceHostRepository, Depends(hosts)],
    command_store: Annotated[PendingCommandRepository, Depends(pending_commands)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> CompressionCommandView:
    return _view(
        enqueue_compression(
            host_id=host_id,
            action=body.action,
            idempotency_key=idempotency_key,
            caller=caller,
            now=datetime.now(UTC),
            hosts=host_store,
            commands=command_store,
        )
    )


@router.get(
    "/inference-hosts/{host_id}/recording-compression/commands/{command_id}",
    operation_id="readRecordingCompressionCommand",
    openapi_extra=needs(Permission.INFERENCE_HOST_VIEW),
    responses=_OPERATOR_RESPONSES,
)
def read_command(
    host_id: UUID,
    command_id: UUID,
    caller: Authorized,
    command_store: Annotated[PendingCommandRepository, Depends(pending_commands)],
) -> CompressionCommandView:
    return _view(
        read_compression_command(
            host_id=host_id,
            command_id=command_id,
            caller=caller,
            commands=command_store,
        )
    )


def _identity(
    request: Request, host_id: UUID, body: dict[str, object] | None
) -> InferenceHostIdentity:
    return host_identity_from_headers(
        host_id=host_id,
        method=request.method,
        path=request.url.path,
        body=body,
        timestamp=request.headers.get(INFERENCE_HOST_TIMESTAMP_HEADER),
        nonce=request.headers.get(INFERENCE_HOST_NONCE_HEADER),
        signature=request.headers.get(INFERENCE_HOST_SIGNATURE_HEADER),
    )


@router.get(
    "/device-commands/compression/next",
    operation_id="claimRecordingCompression",
    response_model=CompressionClaimView,
    responses=_EDGE_RESPONSES | {204: {"description": "No pending command"}},
)
def claim(
    request: Request,
    host_id: Annotated[UUID, Header(alias=INFERENCE_HOST_ID_HEADER)],
    host_store: Annotated[InferenceHostRepository, Depends(hosts)],
    command_store: Annotated[PendingCommandRepository, Depends(pending_commands)],
) -> CompressionClaimView | Response:
    command = claim_next_command(
        host=_identity(request, host_id, None),
        command_type=PendingCommandType.CONTROL_RECORDING_COMPRESSION,
        now=datetime.now(UTC),
        lease_duration=COMMAND_LEASE_DURATION,
        hosts=host_store,
        commands=command_store,
    )
    if command is None:
        return Response(status_code=204)
    assert command.claim_token is not None
    assert command.lease_expires_at is not None
    return CompressionClaimView(
        command_id=str(command.id),
        action=cast(CompressionAction, command.compression_action),
        claim_token=command.claim_token,
        lease_expires_at=command.lease_expires_at.timestamp(),
    )


@router.post(
    "/device-commands/compression/{command_id}/result",
    operation_id="completeRecordingCompression",
    responses=_EDGE_RESPONSES,
)
def complete(
    request: Request,
    command_id: UUID,
    body: CompressionObservation,
    host_id: Annotated[UUID, Header(alias=INFERENCE_HOST_ID_HEADER)],
    claim_token: Annotated[
        str, Header(alias=COMMAND_CLAIM_TOKEN_HEADER, min_length=1, max_length=128)
    ],
    host_store: Annotated[InferenceHostRepository, Depends(hosts)],
    command_store: Annotated[PendingCommandRepository, Depends(pending_commands)],
) -> CompressionCommandView:
    return _view(
        complete_compression(
            command_id=command_id,
            host=_identity(request, host_id, body.model_dump()),
            claim_token=claim_token,
            result=compression_result_from_wire(body.model_dump()),
            now=datetime.now(UTC),
            hosts=host_store,
            commands=command_store,
        )
    )
