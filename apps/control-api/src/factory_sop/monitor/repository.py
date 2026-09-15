"""monitor 镜像的持久化接缝。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from factory_sop.monitor.model import MirroredDecision, MirroredHealth


class MonitorRepository(Protocol):
    """monitor 镜像写入、摘要读取和 SSE 序号读取的最小接缝。"""

    def upsert_decision(self, value: MirroredDecision) -> bool:
        """只插入一次；相同重试返回 False。"""
        ...

    def upsert_health(self, value: MirroredHealth) -> bool:
        """只插入一次；相同重试返回 False。"""
        ...

    def recent_decisions(
        self, *, limit: int, through_sequence: int | None = None
    ) -> tuple[MirroredDecision, ...]:
        """按最新接收顺序返回有限的判定观测，可限定在一个序号高水位内。"""
        ...

    def recent_health(
        self, *, limit: int, through_sequence: int | None = None
    ) -> tuple[MirroredHealth, ...]:
        """按最新接收顺序返回有限的健康观测，可限定在一个序号高水位内。"""
        ...

    def decisions_after_sequence(
        self, *, after_sequence: int, limit: int
    ) -> tuple[MirroredDecision, ...]:
        """返回序号高于游标的判定观测。"""
        ...

    def health_after_sequence(
        self, *, after_sequence: int, limit: int
    ) -> tuple[MirroredHealth, ...]:
        """返回序号高于游标的健康观测。"""
        ...

    def event_cursor_for(self, event_id: str) -> tuple[datetime, str] | None:
        """按事件 ID 查询跨判定和健康流共用的时间游标。"""
        ...

    def decisions_after_cursor(
        self,
        *,
        after: tuple[datetime, str],
        limit: int | None,
        through_sequence: int | None = None,
    ) -> tuple[MirroredDecision, ...]:
        """返回游标之后的全部判定观测；重连补发时可不设上限。"""
        ...

    def health_after_cursor(
        self,
        *,
        after: tuple[datetime, str],
        limit: int | None,
        through_sequence: int | None = None,
    ) -> tuple[MirroredHealth, ...]:
        """返回游标之后的全部健康观测；重连补发时可不设上限。"""
        ...

    def stream_watermarks(self) -> tuple[int, int]:
        """返回判定流和健康流当前各自的最高序号。"""
        ...
