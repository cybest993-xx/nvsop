"""monitor 的回收接缝；组合根注入共享 UoW，周期编排负责阶段隔离。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy.orm import Session

from factory_sop.monitor.adapters.repository import PostgresMonitorRetirementRepository
from factory_sop.retention.api import DisposalRetirement, RecordRetirement, RetentionCutoffs


class RepositoryMonitorRetirement:
    """组合根注入共享 UoW 及 evidence 的引用保护查询；本 owner 不跨模块读表。"""

    def __init__(
        self,
        *,
        session: Session,
        evidence_protected: Callable[[str, str, int], bool],
    ) -> None:
        self._repository = PostgresMonitorRetirementRepository(session, evidence_protected)

    def retire_records(
        self,
        *,
        cutoffs: RetentionCutoffs,
        limit: int,
        now: datetime,
    ) -> RecordRetirement:
        return self._repository.retire_records(cutoffs=cutoffs, limit=limit, now=now)

    def retire_disposals(
        self,
        *,
        cutoffs: RetentionCutoffs,
        limit: int,
        now: datetime,
    ) -> DisposalRetirement:
        return self._repository.retire_disposals(cutoffs=cutoffs, limit=limit, now=now)
