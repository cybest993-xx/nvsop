"""中心证据引用；媒体字节始终留在来源推理机，登记不确认媒体已复制（ADR-0012）。"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID


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
    # 来源机在入队事务里冻结的录像墙钟映射与核心必需跨度（HostInstant 单调秒）。旧登记没有
    # 这组事实，缺失即"截止未知、不可再切片"，绝不用 received_at 或当前时钟反推（§5.20）。
    wall_offset: float | None = None
    required_from: float | None = None
    required_to: float | None = None

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
        frozen = (self.wall_offset, self.required_from, self.required_to)
        if any(item is not None for item in frozen) and not all(
            item is not None for item in frozen
        ):
            raise ValueError("evidence frozen time metadata must be supplied together")
        if self.wall_offset is not None:
            for name, value in (
                ("wall_offset", self.wall_offset),
                ("required_from", self.required_from),
                ("required_to", self.required_to),
            ):
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                ):
                    raise ValueError(f"evidence {name} must be a finite number")
            if not self.required_from <= self.anchor <= self.required_to:
                raise ValueError("evidence required span must contain the anchor")
        if self.sha256 is not None:
            if not self._is_sha256(self.sha256):
                raise ValueError("evidence sha256 must be 64 hexadecimal characters")
            if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0:
                raise ValueError("evidence size must be a non-negative integer")
            if not isinstance(self.reference, str) or not self.reference:
                raise ValueError("evidence reference must be a non-empty string")

    def identity(self) -> tuple[object, ...]:
        """不可变登记身份；再次切片追加新引用，不覆盖已有身份。

        冻结时间元数据不进身份：它可随来源机升级在重复登记时补齐，旧登记缺失时仍合法。
        """
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


class ReviewConclusion(StrEnum):
    """人工对一条证据的复核结论：确认或否定；不改写原判定（§六）。"""

    CONFIRMED = "confirmed"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class EvidenceReview:
    """一条只追加的人工复核结论；不改机器事实、证据或他人记录。

    ``review_id`` 是客户端一次提交的稳定幂等键；``reviewer_name`` 是提交时的显示名快照，
    复核人改名不改变已存结论，也不使同一意图重放冲突。
    """

    review_id: UUID
    evidence_id: str
    generation: str
    reviewer_id: UUID
    reviewer_name: str
    reviewed_at: datetime
    conclusion: ReviewConclusion

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_id, str) or not self.evidence_id:
            raise ValueError("review evidence_id must be a non-empty string")
        if not isinstance(self.generation, str) or not self.generation:
            raise ValueError("review generation must be a non-empty string")
        if not isinstance(self.reviewer_name, str) or not self.reviewer_name:
            raise ValueError("review reviewer_name must be a non-empty string")


class ReclipRequestStatus(StrEnum):
    """再切片委托的生命周期状态；本票只创建``PENDING``，失败由后续执行票写入。"""

    PENDING = "pending"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class EvidenceReclipRequest:
    """一次再切片请求：锚点与来源主机不可改，新余量叠加核心必需跨度形成新窗口。

    命令交付归``device``（稳定幂等键 + 来源主机），请求与状态归``evidence``；本行不覆盖原证据。
    """

    request_id: UUID
    evidence_id: str
    generation: str
    host_id: UUID
    anchor: float
    before_seconds: float
    after_seconds: float
    window_start: float
    window_end: float
    requested_by: UUID
    requested_at: datetime
    command_id: UUID
    status: ReclipRequestStatus
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_id, str) or not self.evidence_id:
            raise ValueError("reclip evidence_id must be a non-empty string")
        if not isinstance(self.generation, str) or not self.generation:
            raise ValueError("reclip generation must be a non-empty string")
        for name, value in (
            ("anchor", self.anchor),
            ("before_seconds", self.before_seconds),
            ("after_seconds", self.after_seconds),
            ("window_start", self.window_start),
            ("window_end", self.window_end),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"reclip {name} must be a finite number")
        if self.before_seconds < 0 or self.after_seconds < 0:
            raise ValueError("reclip margins must not be negative")
        if self.window_end < self.window_start:
            raise ValueError("reclip window_end must not precede window_start")
        if self.status is ReclipRequestStatus.FAILED:
            if not isinstance(self.failure_reason, str) or not self.failure_reason:
                raise ValueError("failed reclip request must carry a failure reason")
        elif self.failure_reason is not None:
            raise ValueError("only failed reclip requests may carry a failure reason")

    def matches(
        self,
        *,
        evidence_id: str,
        generation: str,
        before_seconds: float,
        after_seconds: float,
        requested_by: UUID,
    ) -> bool:
        """同一稳定``request_id``的重复意图比较；不同内容不得覆盖已存请求。"""
        return (
            self.evidence_id == evidence_id
            and self.generation == generation
            and self.before_seconds == before_seconds
            and self.after_seconds == after_seconds
            and self.requested_by == requested_by
        )


__all__ = [
    "EvidenceKind",
    "EvidenceOrigin",
    "EvidenceReclipRequest",
    "EvidenceReference",
    "EvidenceRegistration",
    "EvidenceReview",
    "EvidenceStatus",
    "ReclipRequestStatus",
    "ReviewConclusion",
]
