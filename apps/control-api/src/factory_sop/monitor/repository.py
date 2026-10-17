"""Persistence seam for the monitor mirror."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from factory_sop.monitor.model import (
    MirroredDecision,
    MirroredExecutionAuthority,
    MirroredHealth,
    MirroredObservation,
    MirroredSopInstance,
    MirroredViolation,
)
from factory_sop.retention.api import InstanceRetentionFact, StationUsage
from nvsop_contracts import ReportedDisposal


class MonitorRepository(Protocol):
    def upsert_decision(self, value: MirroredDecision) -> bool:
        """只插入一次；相同重试返回 False。"""
        ...

    def upsert_disposal(self, report: ReportedDisposal, *, received_at: datetime) -> bool: ...

    def upsert_execution_authority(self, value: MirroredExecutionAuthority) -> bool:
        """按 host/station 保存最新 Edge 写入门禁状态。"""
        ...

    def upsert_health(self, value: MirroredHealth) -> bool:
        """只插入一次；相同重试返回 False。"""
        ...

    def upsert_instance(self, value: MirroredSopInstance) -> bool: ...

    def upsert_violation(self, value: MirroredViolation) -> bool: ...

    def upsert_observation(self, value: MirroredObservation) -> bool:
        """只插入一次；相同重试返回 False。"""
        ...

    def page_instances(
        self, *, page: int, page_size: int
    ) -> tuple[tuple[MirroredSopInstance, ...], int]: ...

    def page_violations(
        self, *, page: int, page_size: int
    ) -> tuple[tuple[MirroredViolation, ...], int]: ...

    def page_observations(
        self,
        *,
        page: int,
        page_size: int,
        station_id: str | None = None,
        instance_id: int | None = None,
    ) -> tuple[tuple[MirroredObservation, ...], int]: ...

    def page_disposals(
        self, *, page: int, page_size: int
    ) -> tuple[tuple[ReportedDisposal, ...], int]: ...

    def recent_decisions(self, *, limit: int) -> tuple[MirroredDecision, ...]: ...

    def recent_health(self, *, limit: int) -> tuple[MirroredHealth, ...]: ...

    def execution_authority_for_stations(
        self, *, station_ids: tuple[str, ...]
    ) -> tuple[MirroredExecutionAuthority, ...]: ...

    def health_for_station(self, *, station_id: str) -> tuple[MirroredHealth, ...]:
        """返回工位全部健康事实；展示 limit 不得改变业务分类。"""
        ...

    def last_report_at_by_host(self) -> tuple[tuple[str, datetime], ...]: ...

    def decisions_after_sequence(
        self, *, after_sequence: int, limit: int
    ) -> tuple[MirroredDecision, ...]: ...

    def health_after_sequence(
        self, *, after_sequence: int, limit: int
    ) -> tuple[MirroredHealth, ...]: ...

    def decision_sequence_for_event(self, event_id: str) -> int | None: ...

    def health_sequence_for_event(self, event_id: str) -> int | None: ...

    def runtime_projection(self) -> tuple[dict[str, object], ...]: ...


class MonitorStreamSource(Protocol):
    """SSE 每轮短读和独立唤醒资源的 seam。"""

    def read_after_sequences(
        self,
        *,
        decision_sequence: int,
        health_sequence: int,
        limit: int,
    ) -> tuple[tuple[MirroredDecision, ...], tuple[MirroredHealth, ...]]: ...

    def read_runtime_projection(
        self, *, reserve_listener: bool = True
    ) -> tuple[dict[str, object], ...]: ...

    def wait_for_wakeup(self, *, timeout: float) -> bool:
        """等待提交后提示；False 只表示本次等待超时。"""
        ...


class MonitorImpactRepository(Protocol):
    """保留变更影响估算所需的 monitor 镜像事实只读计数 seam；不读 evidence/retention 表。"""

    def instance_retention_facts(
        self, *, instance_ids: tuple[tuple[UUID, UUID, int], ...]
    ) -> tuple[InstanceRetentionFact, ...]:
        """返回这些实例的判定、锁存违规与真实结案事实。"""
        ...

    def record_impact(
        self,
        *,
        station_id: str | None,
        exposed_from: datetime | None,
        exposed_to: datetime,
    ) -> tuple[StationUsage, ...]:
        """按工位返回落在该时段的判定/健康/观测记录计数。

        属于未结案实例（没有真实 ``closed_at``）的记录，以及实例未知的记录，都归入受保护一类。
        """
        ...

    def disposal_impact(
        self,
        *,
        station_id: str | None,
        exposed_from: datetime | None,
        exposed_to: datetime,
    ) -> tuple[StationUsage, ...]:
        """按工位返回落在该时段的处置记录计数；无明确结案事实的已锁存违规单列为受保护。"""
        ...
