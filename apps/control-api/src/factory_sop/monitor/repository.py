"""Persistence seam for the monitor mirror."""

from __future__ import annotations

from typing import Protocol

from factory_sop.monitor.model import MirroredDecision, MirroredHealth, MirroredSopInstance


class MonitorRepository(Protocol):
    def upsert_decision(self, value: MirroredDecision) -> bool:
        """只插入一次；相同重试返回 False。"""
        ...

    def upsert_health(self, value: MirroredHealth) -> bool:
        """只插入一次；相同重试返回 False。"""
        ...

    def upsert_instance(self, value: MirroredSopInstance) -> bool: ...

    def page_instances(
        self, *, page: int, page_size: int
    ) -> tuple[tuple[MirroredSopInstance, ...], int]: ...

    def recent_decisions(self, *, limit: int) -> tuple[MirroredDecision, ...]: ...

    def recent_health(self, *, limit: int) -> tuple[MirroredHealth, ...]: ...

    def decisions_after_sequence(
        self, *, after_sequence: int, limit: int
    ) -> tuple[MirroredDecision, ...]: ...

    def health_after_sequence(
        self, *, after_sequence: int, limit: int
    ) -> tuple[MirroredHealth, ...]: ...

    def decision_sequence_for_event(self, event_id: str) -> int | None: ...

    def health_sequence_for_event(self, event_id: str) -> int | None: ...
