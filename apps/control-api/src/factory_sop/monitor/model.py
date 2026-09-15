"""monitor 拥有的镜像值；它们是观测，不是判定权威。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from nvsop_contracts import ReportedDecision, ReportedHealth


@dataclass(frozen=True, slots=True)
class MirroredDecision:
    report: ReportedDecision
    received_at: datetime
    stream_sequence: int | None = None


@dataclass(frozen=True, slots=True)
class MirroredHealth:
    report: ReportedHealth
    received_at: datetime
    stream_sequence: int | None = None


__all__ = ["MirroredDecision", "MirroredHealth"]
