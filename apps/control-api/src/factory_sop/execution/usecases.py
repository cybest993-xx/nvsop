"""中心物理执行权租约的业务入口。"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from factory_sop.execution.errors import ExecutionRefusalCode, ExecutionRefusedError
from factory_sop.execution.model import StationGrant
from factory_sop.execution.repository import ExecutionGrantRepository
from factory_sop.identifiers import new_id

LEASE_TTL = timedelta(days=7)


class RepositoryExecutionLeaseGateway:
    """把七天租约规则封装在 execution owner 内。"""

    def __init__(self, grants: ExecutionGrantRepository) -> None:
        self._grants = grants

    def acquire(
        self,
        *,
        station_id: UUID,
        holder_host_id: UUID,
        request_id: UUID,
        now: datetime,
    ) -> StationGrant:
        candidate = StationGrant(
            grant_id=new_id(),
            station_id=station_id,
            holder_host_id=holder_host_id,
            lease_expires_at=now + LEASE_TTL,
            renewed_at=now,
            request_id=request_id,
        )
        granted = self._grants.acquire_if_available(candidate)
        if granted is None:
            raise ExecutionRefusedError(ExecutionRefusalCode.ACTIVE_GRANT_EXISTS)
        return granted

    def renew(
        self,
        *,
        station_id: UUID,
        grant_id: UUID,
        holder_host_id: UUID,
        request_id: UUID,
        now: datetime,
    ) -> StationGrant:
        candidate = StationGrant(
            grant_id=grant_id,
            station_id=station_id,
            holder_host_id=holder_host_id,
            lease_expires_at=now + LEASE_TTL,
            renewed_at=now,
            request_id=request_id,
        )
        renewed = self._grants.renew_if_current(candidate)
        if renewed is None:
            raise ExecutionRefusedError(ExecutionRefusalCode.GRANT_NOT_RENEWABLE)
        return renewed


__all__ = ["LEASE_TTL", "RepositoryExecutionLeaseGateway"]
