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

    def renew_host_leases(
        self,
        *,
        host_id: UUID,
        now: datetime,
        request_id: UUID,
    ) -> tuple[StationGrant, ...]:
        """在主机成功拉取配置的边界续期其持有且仍可续期的租约。

        只读取该主机持有的租约；已到期、已交权或更新的续期候选不被延长，原样返回当前事实，
        不伪造成功期限，也不为其他主机建立资源。
        """
        leases: list[StationGrant] = []
        for current in self._grants.for_holder(host_id):
            if current.lease_expires_at <= now:
                leases.append(current)
                continue
            candidate = StationGrant(
                grant_id=current.grant_id,
                station_id=current.station_id,
                holder_host_id=current.holder_host_id,
                lease_expires_at=now + LEASE_TTL,
                renewed_at=now,
                request_id=request_id,
            )
            renewed = self._grants.renew_if_current(candidate)
            leases.append(renewed if renewed is not None else current)
        return tuple(leases)


__all__ = ["LEASE_TTL", "RepositoryExecutionLeaseGateway"]
