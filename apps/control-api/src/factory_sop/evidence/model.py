"""中心证据引用；媒体字节始终留在来源推理机，登记不确认媒体已复制（ADR-0012）。"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum


class EvidenceKind(StrEnum):
    """证据媒体的切片形态：片段或锚点关键帧（§5.20）。"""

    CLIP = "clip"
    KEYFRAME = "keyframe"


class EvidenceOrigin(StrEnum):
    """本机引用是首次自动切片，还是复核期再切片追加（§5.20）。"""

    AUTOMATIC = "automatic"
    RECLIP = "reclip"


class EvidenceStatus(StrEnum):
    """中心引用的登记状态；没有“媒体已上传中心”这一类状态（ADR-0012）。"""

    PENDING = "pending"
    AVAILABLE = "available"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class EvidenceRegistration:
    """推理机对一条已落盘证据的登记事实；摘要在切片后才可知，故可暂缺。

    缺省时中心只登记身份等待对账（``PENDING``），不声称媒体可用。
    """

    evidence_id: str
    host_id: str
    station_id: str
    instance_id: int
    violation_id: str | None
    kind: EvidenceKind
    origin: EvidenceOrigin
    anchor: float
    window_start: float
    window_end: float
    generation: str
    sha256: str | None = None
    size: int | None = None
    reference: str | None = None

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (self.evidence_id, self.host_id, self.station_id, self.generation)
        ):
            raise ValueError("evidence identity strings must be non-empty")
        if self.violation_id is not None and not self.violation_id:
            raise ValueError("evidence violation_id must be non-empty or null")
        if isinstance(self.instance_id, bool) or self.instance_id < 0:
            raise ValueError("evidence instance_id must be a non-negative integer")
        if not all(
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(float(value))
            for value in (self.anchor, self.window_start, self.window_end)
        ):
            raise ValueError("evidence anchor and window must be finite numbers")
        if self.window_end < self.window_start:
            raise ValueError("evidence window_end must not precede window_start")
        material = (self.sha256, self.size, self.reference)
        if any(item is not None for item in material) and not all(
            item is not None for item in material
        ):
            raise ValueError("evidence sha256/size/reference must be supplied together")
        if self.sha256 is not None:
            if not self._is_sha256(self.sha256):
                raise ValueError("evidence sha256 must be 64 hexadecimal characters")
            if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0:
                raise ValueError("evidence size must be a non-negative integer")
            if not isinstance(self.reference, str) or not self.reference:
                raise ValueError("evidence reference must be a non-empty string")

    def identity(self) -> tuple[object, ...]:
        """不可变登记身份；再次切片追加新引用，不覆盖已有身份。"""
        return (
            self.evidence_id,
            self.host_id,
            self.station_id,
            self.instance_id,
            self.violation_id,
            self.kind,
            self.origin,
            self.anchor,
            self.window_start,
            self.window_end,
            self.generation,
        )

    def material(self) -> tuple[object, ...]:
        """本机媒体身份三元组；中心只登记，不复制字节，也不确认对象存在。"""
        return (self.sha256, self.size, self.reference)

    def without_material(self) -> EvidenceRegistration:
        """丢掉摘要/大小/引用，用于登记未通过受控引用校验时的失败记录。"""
        return replace(self, sha256=None, size=None, reference=None)

    @staticmethod
    def _is_sha256(value: str) -> bool:
        return len(value) == 64 and all(
            character in "0123456789abcdefABCDEF" for character in value
        )


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    """中心登记的一条证据引用及其真实登记状态。"""

    registration: EvidenceRegistration
    status: EvidenceStatus
    failure_reason: str | None
    received_at: datetime

    def __post_init__(self) -> None:
        if self.status is EvidenceStatus.FAILED:
            if not isinstance(self.failure_reason, str) or not self.failure_reason:
                raise ValueError("failed evidence must carry a failure reason")
        elif self.failure_reason is not None:
            raise ValueError("only failed evidence may carry a failure reason")

    @property
    def evidence_id(self) -> str:
        return self.registration.evidence_id


__all__ = [
    "EvidenceKind",
    "EvidenceOrigin",
    "EvidenceReference",
    "EvidenceRegistration",
    "EvidenceStatus",
]
