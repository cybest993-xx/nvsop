"""录像压缩控制的固定机器契约; 旧命令领取端点不会消费此类型。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from math import isfinite
from typing import Literal, cast

CompressionAction = Literal["inspect", "pause", "resume", "abort"]
ACTIONS = ("inspect", "pause", "resume", "abort")


@dataclass(frozen=True, slots=True)
class CompressionClaim:
    """一条来源主机专属的控制命令及其领取租约。"""

    command_id: str
    action: CompressionAction
    claim_token: str
    lease_expires_at: float


@dataclass(frozen=True, slots=True)
class CompressionResult:
    """来源机观测快照; 请求接受态保留为独立状态, 不伪造暂停/中止成功。"""

    state: str
    requested: str
    segment: str | None
    pid: int | None
    readrate: float | None
    failure_code: str | None = None
    detail: str | None = None

    def to_wire(self) -> dict[str, object]:
        document = asdict(self)
        if self.readrate is not None:
            document["readrate"] = float(self.readrate)
        return document


def compression_claim_from_wire(value: Mapping[str, object]) -> CompressionClaim:
    if set(value) != {"command_type", "command_id", "action", "claim_token", "lease_expires_at"}:
        raise ValueError("invalid compression claim fields")
    if value["command_type"] != "control_recording_compression" or value["action"] not in ACTIONS:
        raise ValueError("invalid compression command")
    for key in ("command_id", "claim_token"):
        if not isinstance(value[key], str) or not value[key]:
            raise ValueError("invalid compression claim identity")
    expires = value["lease_expires_at"]
    if isinstance(expires, bool) or not isinstance(expires, int | float) or not isfinite(expires):
        raise ValueError("invalid compression lease")
    return CompressionClaim(
        cast(str, value["command_id"]),
        cast(CompressionAction, value["action"]),
        cast(str, value["claim_token"]),
        float(expires),
    )


def compression_result_from_wire(value: Mapping[str, object]) -> CompressionResult:
    if set(value) != set(CompressionResult.__dataclass_fields__):
        raise ValueError("invalid compression result fields")
    if value["state"] not in (
        "idle",
        "running",
        "pausing",
        "paused",
        "resuming",
        "aborting",
        "aborted",
        "busy",
        "unavailable",
    ) or value["requested"] not in ("run", "pause", "abort", "unavailable"):
        raise ValueError("invalid compression state")
    for key in ("segment", "failure_code", "detail"):
        if value[key] is not None and not isinstance(value[key], str):
            raise ValueError("invalid compression result text")
    pid, rate = value["pid"], value["readrate"]
    if pid is not None and (type(pid) is not int or pid <= 0):
        raise ValueError("invalid compression process identity")
    if rate is not None and (
        isinstance(rate, bool)
        or not isinstance(rate, int | float)
        or not isfinite(rate)
        or rate <= 0
    ):
        raise ValueError("invalid compression readrate")
    return CompressionResult(
        cast(str, value["state"]),
        cast(str, value["requested"]),
        cast(str | None, value["segment"]),
        pid,
        float(rate) if rate is not None else None,
        cast(str | None, value["failure_code"]),
        cast(str | None, value["detail"]),
    )


def compression_action_confirmed(action: CompressionAction, state: str) -> bool:
    """接受意图不代表完成; 两端共用同一实际状态确认规则。"""
    return (
        action == "inspect"
        or state
        in {
            "pause": ("paused",),
            "resume": ("idle", "running"),
            "abort": ("aborted",),
        }[action]
    )
