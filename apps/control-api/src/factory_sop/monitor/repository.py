"""Persistence seam for the monitor mirror."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from factory_sop.monitor.model import (
    MirroredDecision,
    MirroredHealth,
    MirroredObservation,
    MirroredSopInstance,
    MirroredViolation,
)


class MonitorRepository(Protocol):
    def upsert_decision(self, value: MirroredDecision) -> bool:
        """只插入一次；相同重试返回 False。"""
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

    def recent_decisions(self, *, limit: int) -> tuple[MirroredDecision, ...]: ...

    def recent_health(self, *, limit: int) -> tuple[MirroredHealth, ...]: ...

    def recent_health_for_station(
        self, *, station_id: str, limit: int
    ) -> tuple[MirroredHealth, ...]: ...

    def last_report_at_by_host(self) -> tuple[tuple[str, datetime], ...]: ...

    def decisions_after_sequence(
        self, *, after_sequence: int, limit: int
    ) -> tuple[MirroredDecision, ...]: ...

    def health_after_sequence(
        self, *, after_sequence: int, limit: int
    ) -> tuple[MirroredHealth, ...]: ...

    def decision_sequence_for_event(self, event_id: str) -> int | None: ...

    def health_sequence_for_event(self, event_id: str) -> int | None: ...


class MonitorStreamSource(Protocol):
    """SSE 每轮短读和独立唤醒资源的 seam。"""

    def read_after_sequences(
        self,
        *,
        decision_sequence: int,
        health_sequence: int,
        limit: int,
    ) -> tuple[tuple[MirroredDecision, ...], tuple[MirroredHealth, ...]]: ...

    def wait_for_wakeup(self, *, timeout: float) -> bool:
        """等待提交后提示；False 只表示本次等待超时。"""
        ...
