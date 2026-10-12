"""中心与推理机共同解释的委托连接测试与再切片命令线契约。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import cast


class ConnectionTestOutcome(StrEnum):
    """推理机真实测试的三种可观察结果。"""

    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ConnectionTestCommand:
    """推理机可执行的命令; 只携带非秘密配置和配置修订号。"""

    command_id: str
    connector_id: str
    connector_revision: int
    connector_type: str
    configuration: dict[str, str | int]

    def __post_init__(self) -> None:
        if not self.command_id or not self.connector_id or not self.connector_type:
            raise ValueError("connection test command identities must not be empty")
        if self.connector_revision < 1:
            raise ValueError("connection test command revision must be positive")
        _validate_configuration(self.configuration)


@dataclass(frozen=True, slots=True)
class ConnectionTestClaim:
    """推理机领取后的命令信封, 包含仅用于回报的领取令牌和租约截止时刻。"""

    command: ConnectionTestCommand
    claim_token: str
    lease_expires_at: str

    def __post_init__(self) -> None:
        if not self.claim_token or not self.lease_expires_at:
            raise ValueError("connection test claim lease fields must not be empty")


@dataclass(frozen=True, slots=True)
class ConnectionTestResult:
    """推理机回报的真实状态; 拒绝时携带机器可读原因, 不伪造不可达。"""

    outcome: ConnectionTestOutcome
    detail: str | None = None
    credentials_configured: bool | None = None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if self.outcome is ConnectionTestOutcome.REJECTED and not self.failure_code:
            raise ValueError("a rejected connection test must carry a failure code")
        if (
            self.outcome is not ConnectionTestOutcome.REJECTED
            and self.credentials_configured is not True
        ):
            raise ValueError("a non-rejected connection test must confirm configured credentials")


def connection_test_command_to_wire(command: ConnectionTestCommand) -> dict[str, object]:
    """把连接测试命令编码为中心和推理机共用的严格 JSON 对象。"""
    return {
        "command_type": "test_connector_connection",
        "command_id": command.command_id,
        "connector_id": command.connector_id,
        "connector_revision": command.connector_revision,
        "connector_type": command.connector_type,
        "configuration": dict(command.configuration),
    }


def connection_test_command_from_wire(value: Mapping[str, object]) -> ConnectionTestCommand:
    """解析命令并拒绝未知键, 秘密键或不完整配置。"""
    required = {
        "command_type",
        "command_id",
        "connector_id",
        "connector_revision",
        "connector_type",
        "configuration",
    }
    if set(value) != required or value.get("command_type") != "test_connector_connection":
        raise ValueError("connection test command has unsupported fields")
    command_id = value["command_id"]
    connector_id = value["connector_id"]
    connector_revision = value["connector_revision"]
    connector_type = value["connector_type"]
    configuration = value["configuration"]
    if (
        not isinstance(command_id, str)
        or not command_id
        or not isinstance(connector_id, str)
        or not connector_id
        or not isinstance(connector_type, str)
        or not connector_type
    ):
        raise ValueError("connection test command has invalid identity")
    if isinstance(connector_revision, bool) or not isinstance(connector_revision, int):
        raise ValueError("connection test command has an invalid revision")
    if not isinstance(configuration, Mapping):
        raise ValueError("connection test command has an invalid configuration")
    if not all(isinstance(key, str) for key in configuration):
        raise ValueError("connection test command has an invalid configuration key")
    normalized = dict(cast("Mapping[str, object]", configuration))
    _validate_configuration(normalized)
    return ConnectionTestCommand(
        command_id=command_id,
        connector_id=connector_id,
        connector_revision=connector_revision,
        connector_type=connector_type,
        configuration={key: cast("str | int", item) for key, item in normalized.items()},
    )


def connection_test_claim_to_wire(claim: ConnectionTestClaim) -> dict[str, object]:
    """把领取信封编码为严格对象; 领取令牌不与连接器凭据混淆。"""
    return {
        "command": connection_test_command_to_wire(claim.command),
        "claim_token": claim.claim_token,
        "lease_expires_at": claim.lease_expires_at,
    }


def connection_test_claim_from_wire(value: Mapping[str, object]) -> ConnectionTestClaim:
    """解析领取信封并拒绝秘密或未声明字段。"""
    required = {"command", "claim_token", "lease_expires_at"}
    if set(value) != required:
        raise ValueError("connection test claim has unsupported fields")
    command = value["command"]
    claim_token = value["claim_token"]
    lease_expires_at = value["lease_expires_at"]
    if not isinstance(command, Mapping):
        raise ValueError("connection test claim has an invalid command")
    if not isinstance(claim_token, str) or not claim_token:
        raise ValueError("connection test claim has an invalid claim token")
    if not isinstance(lease_expires_at, str) or not lease_expires_at:
        raise ValueError("connection test claim has an invalid lease expiry")
    return ConnectionTestClaim(
        command=connection_test_command_from_wire(command),
        claim_token=claim_token,
        lease_expires_at=lease_expires_at,
    )


def connection_test_result_to_wire(result: ConnectionTestResult) -> dict[str, object]:
    """把推理机结果编码为可持久化的 JSON 对象。"""
    return {
        "outcome": result.outcome.value,
        "detail": result.detail,
        "credentials_configured": result.credentials_configured,
        "failure_code": result.failure_code,
    }


def connection_test_result_from_wire(value: Mapping[str, object]) -> ConnectionTestResult:
    """解析结果, 保留拒绝原因而不把未知结构猜成失败。"""
    required = {"outcome", "detail", "credentials_configured", "failure_code"}
    if set(value) != required:
        raise ValueError("connection test result has unsupported fields")
    raw_outcome = value["outcome"]
    if not isinstance(raw_outcome, str):
        raise ValueError("connection test result outcome is unsupported")
    try:
        outcome = ConnectionTestOutcome(raw_outcome)
    except ValueError as error:
        raise ValueError("connection test result outcome is unsupported") from error
    detail = value["detail"]
    credentials_configured = value["credentials_configured"]
    failure_code = value["failure_code"]
    if detail is not None and not isinstance(detail, str):
        raise ValueError("connection test result detail is invalid")
    if credentials_configured is not None and not isinstance(credentials_configured, bool):
        raise ValueError("connection test result credentials flag is invalid")
    if failure_code is not None and not isinstance(failure_code, str):
        raise ValueError("connection test result failure code is invalid")
    return ConnectionTestResult(
        outcome=outcome,
        detail=detail,
        credentials_configured=credentials_configured,
        failure_code=failure_code,
    )


@dataclass(frozen=True, slots=True)
class ReclipEvidenceCommand:
    """目标推理机可执行的再切片命令: 原锚点固定, 只给并集窗口与新请求身份。"""

    command_id: str
    request_id: str
    station_id: str
    instance_id: int
    anchor: float
    window_start: float
    window_end: float

    def __post_init__(self) -> None:
        if not self.command_id or not self.request_id or not self.station_id:
            raise ValueError("reclip command identities must not be empty")
        if isinstance(self.instance_id, bool) or self.instance_id < 0:
            raise ValueError("reclip command instance_id must be a non-negative integer")
        for name, value in (
            ("anchor", self.anchor),
            ("window_start", self.window_start),
            ("window_end", self.window_end),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"reclip command {name} must be a finite number")
            if not math.isfinite(float(value)):
                raise ValueError(f"reclip command {name} must be a finite number")
        if self.window_end < self.window_start:
            raise ValueError("reclip command window_end must not precede window_start")


@dataclass(frozen=True, slots=True)
class ReclipEvidenceClaim:
    """推理机领取后的再切片命令信封, 含仅用于回报的领取令牌和租约截止时刻。"""

    command: ReclipEvidenceCommand
    claim_token: str
    lease_expires_at: str

    def __post_init__(self) -> None:
        if not self.claim_token or not self.lease_expires_at:
            raise ValueError("reclip claim lease fields must not be empty")


@dataclass(frozen=True, slots=True)
class ReclipEvidenceResult:
    """再切片成功回报: 只声明中心已追加的新证据引用身份, 不复制媒体字节。"""

    evidence_id: str

    def __post_init__(self) -> None:
        if not self.evidence_id:
            raise ValueError("reclip result evidence_id must not be empty")


def reclip_evidence_command_to_wire(command: ReclipEvidenceCommand) -> dict[str, object]:
    """把再切片命令编码为中心和推理机共用的严格 JSON 对象。"""
    return {
        "command_type": "reclip_evidence",
        "command_id": command.command_id,
        "request_id": command.request_id,
        "station_id": command.station_id,
        "instance_id": command.instance_id,
        "anchor": command.anchor,
        "window_start": command.window_start,
        "window_end": command.window_end,
    }


def reclip_evidence_command_from_wire(value: Mapping[str, object]) -> ReclipEvidenceCommand:
    """解析再切片命令并拒绝未知键或非数字窗口。"""
    required = {
        "command_type",
        "command_id",
        "request_id",
        "station_id",
        "instance_id",
        "anchor",
        "window_start",
        "window_end",
    }
    if set(value) != required or value.get("command_type") != "reclip_evidence":
        raise ValueError("reclip command has unsupported fields")
    command_id = value["command_id"]
    request_id = value["request_id"]
    station_id = value["station_id"]
    instance_id = value["instance_id"]
    if (
        not isinstance(command_id, str)
        or not command_id
        or not isinstance(request_id, str)
        or not request_id
        or not isinstance(station_id, str)
        or not station_id
    ):
        raise ValueError("reclip command has invalid identity")
    if isinstance(instance_id, bool) or not isinstance(instance_id, int):
        raise ValueError("reclip command has an invalid instance_id")
    numbers: dict[str, float] = {}
    for name in ("anchor", "window_start", "window_end"):
        raw = value[name]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"reclip command has an invalid {name}")
        numbers[name] = float(raw)
    return ReclipEvidenceCommand(
        command_id=command_id,
        request_id=request_id,
        station_id=station_id,
        instance_id=instance_id,
        anchor=numbers["anchor"],
        window_start=numbers["window_start"],
        window_end=numbers["window_end"],
    )


def reclip_evidence_claim_to_wire(claim: ReclipEvidenceClaim) -> dict[str, object]:
    """把再切片领取信封编码为严格对象。"""
    return {
        "command": reclip_evidence_command_to_wire(claim.command),
        "claim_token": claim.claim_token,
        "lease_expires_at": claim.lease_expires_at,
    }


def reclip_evidence_claim_from_wire(value: Mapping[str, object]) -> ReclipEvidenceClaim:
    """解析再切片领取信封并拒绝未声明字段。"""
    required = {"command", "claim_token", "lease_expires_at"}
    if set(value) != required:
        raise ValueError("reclip claim has unsupported fields")
    command = value["command"]
    claim_token = value["claim_token"]
    lease_expires_at = value["lease_expires_at"]
    if not isinstance(command, Mapping):
        raise ValueError("reclip claim has an invalid command")
    if not isinstance(claim_token, str) or not claim_token:
        raise ValueError("reclip claim has an invalid claim token")
    if not isinstance(lease_expires_at, str) or not lease_expires_at:
        raise ValueError("reclip claim has an invalid lease expiry")
    return ReclipEvidenceClaim(
        command=reclip_evidence_command_from_wire(command),
        claim_token=claim_token,
        lease_expires_at=lease_expires_at,
    )


def reclip_evidence_result_to_wire(result: ReclipEvidenceResult) -> dict[str, object]:
    """把再切片成功回报编码为严格对象。"""
    return {"evidence_id": result.evidence_id}


def reclip_evidence_result_from_wire(value: Mapping[str, object]) -> ReclipEvidenceResult:
    """解析再切片成功回报并拒绝未声明字段。"""
    if set(value) != {"evidence_id"}:
        raise ValueError("reclip result has unsupported fields")
    evidence_id = value["evidence_id"]
    if not isinstance(evidence_id, str) or not evidence_id:
        raise ValueError("reclip result has an invalid evidence_id")
    return ReclipEvidenceResult(evidence_id=evidence_id)


def _validate_configuration(value: Mapping[str, object]) -> None:
    """命令配置只允许中心已经定义的非秘密字段。"""
    if set(value) - {"address", "port"} or "address" not in value:
        raise ValueError("connection test configuration contains unsupported fields")
    address = value["address"]
    port = value.get("port")
    if not isinstance(address, str) or not address:
        raise ValueError("connection test configuration has an invalid address")
    if port is not None and (
        isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
    ):
        raise ValueError("connection test configuration has an invalid port")
