"""中心物理执行权租约的业务入口。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import NoReturn
from uuid import UUID

from factory_sop.auth.api import Caller, HandoverAuthority, Permission, authorize
from factory_sop.execution.errors import ExecutionRefusalCode, ExecutionRefusedError
from factory_sop.execution.model import (
    HANDOVER_RISK_STATEMENT,
    HandoverConfirmation,
    StationGrant,
)
from factory_sop.execution.repository import ExecutionGrantRepository, HandoverRepository
from factory_sop.identifiers import new_id
from factory_sop.observability import get_logger

LEASE_TTL = timedelta(days=7)

# 强制改绑双人确认的权限。建立与第二确认都要求“此刻”持有它，不接受请求开始时的快照。
HANDOVER_PERMISSION = Permission.HANDOVER_EDIT

_logger = get_logger("execution")


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


def create_handover(
    *,
    caller: Caller,
    station_id: UUID,
    from_host_id: UUID,
    to_host_id: UUID,
    risk_acknowledgement: str,
    now: datetime,
    handovers: HandoverRepository,
    grants: ExecutionGrantRepository,
    authority: HandoverAuthority,
) -> HandoverConfirmation:
    """建立强制改绑请求；操作者同时完成第一确认。

    内容（工位、旧机、目标机）在此冻结，之后只能读或由另一人第二确认；改内容必须新建请求。
    权限在既有 auth 管理锁内按“此刻”复核，因此已被撤权的 stale 身份不能完成首确认。
    执行权关系也在此复核：旧机必须是该工位当前归属，建立与第二确认不得只查其一。
    """
    authorize(caller, HANDOVER_PERMISSION)
    holders = authority.lock_and_read_holders()
    if caller.user.id not in holders:
        _refuse_handover(
            ExecutionRefusalCode.HANDOVER_NOT_ELIGIBLE, caller=caller, station_id=station_id
        )
    if risk_acknowledgement != HANDOVER_RISK_STATEMENT:
        _refuse_handover(
            ExecutionRefusalCode.HANDOVER_RISK_NOT_ACKNOWLEDGED,
            caller=caller,
            station_id=station_id,
        )
    _require_current_holder(
        caller=caller,
        station_id=station_id,
        from_host_id=from_host_id,
        to_host_id=to_host_id,
        grants=grants,
    )
    record = HandoverConfirmation(
        handover_id=new_id(),
        station_id=station_id,
        from_host_id=from_host_id,
        to_host_id=to_host_id,
        operator_id=caller.user.id,
        operator_confirmed_at=now,
        operator_risk_shown=True,
    )
    handovers.add(record)
    _logger.info(
        "execution.handover.created",
        handover_id=str(record.handover_id),
        station_id=str(station_id),
        from_host_id=str(from_host_id),
        to_host_id=str(to_host_id),
        operator_id=str(caller.user.id),
        risk_statement=HANDOVER_RISK_STATEMENT,
    )
    return record


def read_handover(
    *, caller: Caller, handover_id: UUID, handovers: HandoverRepository
) -> HandoverConfirmation:
    """读取一条请求，供第二名当前有权的用户查看内容与风险原文。"""
    authorize(caller, HANDOVER_PERMISSION)
    record = handovers.by_identifier(handover_id)
    if record is None:
        _refuse_handover(
            ExecutionRefusalCode.HANDOVER_NOT_FOUND, caller=caller, handover_id=handover_id
        )
    return record


def read_handover_risk(*, caller: Caller) -> str:
    """返回服务器权威的强制改绑风险原文，供前端展示。

    前端不自拟第二段风险文本：只有本函数返回的原文可作为确认输入。
    """
    authorize(caller, HANDOVER_PERMISSION)
    return HANDOVER_RISK_STATEMENT


def confirm_handover(
    *,
    caller: Caller,
    handover_id: UUID,
    station_id: UUID,
    from_host_id: UUID,
    to_host_id: UUID,
    risk_acknowledgement: str,
    now: datetime,
    handovers: HandoverRepository,
    grants: ExecutionGrantRepository,
    authority: HandoverAuthority,
) -> HandoverConfirmation:
    """由另一名当前有权的用户确认被展示的同一请求内容。

    第二人绑定请求 id 与展示的内容：内容不符不确认旧内容。双方“此刻”的有效状态与权限在同一
    管理锁内重读，`Caller` 的旧快照不足；条件更新保证并发重复只有一个赢家。
    执行权关系在此再次复核：建立与第二确认之间租约被替换时，不能确认旧归属。
    """
    authorize(caller, HANDOVER_PERMISSION)
    holders = authority.lock_and_read_holders()
    record = handovers.by_identifier(handover_id)
    if record is None:
        _refuse_handover(
            ExecutionRefusalCode.HANDOVER_NOT_FOUND, caller=caller, handover_id=handover_id
        )
    if (record.station_id, record.from_host_id, record.to_host_id) != (
        station_id,
        from_host_id,
        to_host_id,
    ):
        _refuse_handover(
            ExecutionRefusalCode.HANDOVER_CONTENT_MISMATCH, caller=caller, handover_id=handover_id
        )
    if record.second_operator_id is not None:
        _refuse_handover(
            ExecutionRefusalCode.HANDOVER_ALREADY_CONFIRMED,
            caller=caller,
            handover_id=handover_id,
        )
    if record.operator_id == caller.user.id:
        _refuse_handover(
            ExecutionRefusalCode.HANDOVER_SAME_OPERATOR, caller=caller, handover_id=handover_id
        )
    if risk_acknowledgement != HANDOVER_RISK_STATEMENT:
        _refuse_handover(
            ExecutionRefusalCode.HANDOVER_RISK_NOT_ACKNOWLEDGED,
            caller=caller,
            handover_id=handover_id,
        )
    if record.operator_id not in holders or caller.user.id not in holders:
        _refuse_handover(
            ExecutionRefusalCode.HANDOVER_NOT_ELIGIBLE, caller=caller, handover_id=handover_id
        )
    _require_current_holder(
        caller=caller,
        station_id=record.station_id,
        from_host_id=record.from_host_id,
        to_host_id=record.to_host_id,
        grants=grants,
    )
    confirmed = handovers.confirm_second(
        HandoverConfirmation(
            handover_id=record.handover_id,
            station_id=record.station_id,
            from_host_id=record.from_host_id,
            to_host_id=record.to_host_id,
            operator_id=record.operator_id,
            operator_confirmed_at=record.operator_confirmed_at,
            operator_risk_shown=record.operator_risk_shown,
            second_operator_id=caller.user.id,
            second_confirmed_at=now,
            second_risk_shown=True,
        )
    )
    if confirmed is None:
        _refuse_handover(
            ExecutionRefusalCode.HANDOVER_ALREADY_CONFIRMED,
            caller=caller,
            handover_id=handover_id,
        )
    _logger.info(
        "execution.handover.confirmed",
        handover_id=str(confirmed.handover_id),
        station_id=str(confirmed.station_id),
        from_host_id=str(confirmed.from_host_id),
        to_host_id=str(confirmed.to_host_id),
        operator_id=str(confirmed.operator_id),
        second_operator_id=str(caller.user.id),
        risk_statement=HANDOVER_RISK_STATEMENT,
    )
    return confirmed


def _require_current_holder(
    *,
    caller: Caller,
    station_id: UUID,
    from_host_id: UUID,
    to_host_id: UUID,
    grants: ExecutionGrantRepository,
) -> None:
    """建立与第二确认共用的执行权关系复核。

    以 execution 自己的当前租约记录为权威：请求的旧机必须是该工位此刻的 holder。到期但未被
    替换的记录仍是当前归属事实，不额外要求未过期，也不要求旧机在线或 active；不从相机拓扑猜
    关系。无当前归属（含未知工位）沿用 404，holder 不符为 409，同一台机为 422。
    """
    if from_host_id == to_host_id:
        _refuse_handover(
            ExecutionRefusalCode.HANDOVER_SAME_HOST,
            caller=caller,
            station_id=station_id,
            from_host_id=from_host_id,
        )
    grant = grants.for_station(station_id)
    if grant is None:
        _refuse_handover(
            ExecutionRefusalCode.HANDOVER_TARGET_NOT_FOUND, caller=caller, station_id=station_id
        )
    if grant.holder_host_id != from_host_id:
        _refuse_handover(
            ExecutionRefusalCode.HANDOVER_SOURCE_MISMATCH,
            caller=caller,
            station_id=station_id,
            from_host_id=from_host_id,
        )


def _refuse_handover(code: ExecutionRefusalCode, *, caller: Caller, **target: UUID) -> NoReturn:
    """把一次强制改绑拒绝写成稳定诊断事件，再抛出。"""
    _logger.info(
        "execution.handover.refused",
        error_code=code.value,
        actor_id=str(caller.user.id),
        **{key: str(value) for key, value in target.items()},
    )
    raise ExecutionRefusedError(code)


__all__ = [
    "LEASE_TTL",
    "RepositoryExecutionLeaseGateway",
    "confirm_handover",
    "create_handover",
    "read_handover",
    "read_handover_risk",
]
