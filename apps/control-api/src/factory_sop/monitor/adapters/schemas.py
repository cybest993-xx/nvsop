"""monitor 上报 HTTP 使用的显式 wire schema。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic.dataclasses import dataclass

from nvsop_contracts import ReportedDecision, ReportedHealth, ReportEvidence, ReportViolation


@dataclass(frozen=True, config=ConfigDict(extra="forbid"))
class MonitorReportEvidence(ReportEvidence):
    """复用 contracts 中判定证据的字段和校验。"""


@dataclass(frozen=True, config=ConfigDict(extra="forbid"))
class MonitorReportViolation(ReportViolation):
    """复用 contracts 中违规观测的字段和校验。"""

    evidence: MonitorReportEvidence


@dataclass(frozen=True, config=ConfigDict(extra="forbid"))
class MonitorReportedDecision(ReportedDecision):
    """复用 contracts 中判定上报的 wire 字段和校验。"""

    violations: tuple[MonitorReportViolation, ...]
    evidence: MonitorReportEvidence
    contract_version: int


@dataclass(frozen=True, config=ConfigDict(extra="forbid"))
class MonitorReportedHealth(ReportedHealth):
    """复用 contracts 中健康上报的 wire 字段和校验。"""

    contract_version: int


class MonitorReportAccepted(BaseModel):
    """monitor 上报接受结果。"""

    model_config = ConfigDict(extra="forbid")

    accepted: Literal[True]
    duplicate: bool
    event_id: str


__all__ = [
    "MonitorReportAccepted",
    "MonitorReportEvidence",
    "MonitorReportViolation",
    "MonitorReportedDecision",
    "MonitorReportedHealth",
]
