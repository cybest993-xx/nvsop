"""中心物理执行权租约的业务入口。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import NoReturn
from uuid import UUID

from factory_sop.auth.api import Caller, HandoverAuthority, Permission, authorize
from factory_sop.device.api import DeviceExecutionHandoverCommandGateway, InferenceHostIdentity
from factory_sop.execution.api import ExecutionGrantState, ExecutionGrantView
from factory_sop.execution.errors import ExecutionRefusalCode, ExecutionRefusedError
from factory_sop.execution.model import (
    HANDOVER_RISK_STATEMENT,
    HandoverConfirmation,
    NormalHandover,
    NormalHandoverStatus,
    StationGrant,
)
from factory_sop.execution.repository import (
    ExecutionGrantRepository,
    HandoverRepository,
    NormalHandoverCommandGateway,
    NormalHandoverRepository,
)
from factory_sop.identifiers import new_id
from factory_sop.observability import get_logger

LEASE_TTL = timedelta(days=7)

# 强制改绑双人确认的权限。建立与第二确认都要求“此刻”持有它，不接受请求开始时的快照。
HANDOVER_PERMISSION = Permission.HANDOVER_EDIT

_logger = get_logger("execution")


class RepositoryExecutionGrantViewGateway:
    """执行权 owner 内集中判定 Center 租约当前状态；读取不加写锁。"""

    def __init__(self, grants: ExecutionGrantRepository) -> None:
        self._grants = grants

    def station_views(
        self, *, station_ids: tuple[UUID, ...], now: datetime
    ) -> tuple[ExecutionGrantView, ...]:
        grants = {grant.station_id: grant for grant in self._grants.for_stations(station_ids)}
        values: list[ExecutionGrantView] = []
        for station_id in station_ids:
            grant = grants.get(station_id)
            if grant is None:
                state = ExecutionGrantState.MISSING
            elif grant.lease_expires_at <= now:
                state = ExecutionGrantState.EXPIRED
            else:
                state = ExecutionGrantState.ACTIVE
            values.append(ExecutionGrantView(station_id=station_id, state=state, grant=grant))
        return tuple(values)


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


def create_normal_handover(
    *,
    caller: Caller,
    station_id: UUID,
    from_host_id: UUID,
    to_host_id: UUID,
    idempotency_key: str,
    now: datetime,
    handovers: NormalHandoverRepository,
    grants: ExecutionGrantRepository,
    commands: NormalHandoverCommandGateway,
) -> NormalHandover:
    """建立正常交权请求并入队旧机停机命令，不创建新机执行权。"""
    authorize(caller, HANDOVER_PERMISSION)
    current = grants.for_station(station_id)
    existing = handovers.by_idempotency_key(idempotency_key)
    if existing is not None:
        _check_normal_identity(
            existing,
            station_id=station_id,
            from_host_id=from_host_id,
            to_host_id=to_host_id,
            caller=caller,
        )
        return existing
    if from_host_id == to_host_id:
        _refuse_handover(
            ExecutionRefusalCode.HANDOVER_SAME_HOST,
            caller=caller,
            station_id=station_id,
        )
    if current is None:
        _refuse_handover(
            ExecutionRefusalCode.HANDOVER_TARGET_NOT_FOUND,
            caller=caller,
            station_id=station_id,
        )
    if current.holder_host_id != from_host_id:
        _refuse_handover(
            ExecutionRefusalCode.HANDOVER_SOURCE_MISMATCH,
            caller=caller,
            station_id=station_id,
            from_host_id=from_host_id,
        )
    if current.lease_expires_at <= now:
        _refuse_handover(
            ExecutionRefusalCode.NORMAL_HANDOVER_GRANT_EXPIRED,
            caller=caller,
            station_id=station_id,
        )
    pending = handovers.pending_for_station(station_id)
    if pending is not None:
        pending = _expire_normal_handover(pending, now=now, handovers=handovers)
        if pending.status is NormalHandoverStatus.CONFIRMED and (
            pending.grant_id != current.grant_id or pending.from_host_id != current.holder_host_id
        ):
            pending = handovers.save(
                replace(
                    pending,
                    status=NormalHandoverStatus.FAILED,
                    failed_at=now,
                    failure_code=ExecutionRefusalCode.NORMAL_HANDOVER_STALE_CONFIRMATION.value,
                    failure_detail="the frozen execution grant is no longer current",
                )
            )
    if pending is not None and pending.status in {
        NormalHandoverStatus.PENDING,
        NormalHandoverStatus.CONFIRMED,
    }:
        _refuse_handover(
            ExecutionRefusalCode.NORMAL_HANDOVER_PENDING,
            caller=caller,
            station_id=station_id,
        )
    record = NormalHandover(
        handover_id=new_id(),
        station_id=station_id,
        from_host_id=from_host_id,
        to_host_id=to_host_id,
        grant_id=current.grant_id,
        command_id=new_id(),
        idempotency_key=idempotency_key,
        requested_by=caller.user.id,
        requested_at=now,
        expires_at=now + timedelta(seconds=300),
        status=NormalHandoverStatus.PENDING,
    )
    stored, created = handovers.add_or_get(record)
    if not created:
        _check_normal_identity(
            stored,
            station_id=station_id,
            from_host_id=from_host_id,
            to_host_id=to_host_id,
            caller=caller,
        )
        return stored
    commands.enqueue(
        command_id=record.command_id,
        handover_id=record.handover_id,
        host_id=record.from_host_id,
        requested_by=record.requested_by,
        now=now,
    )
    _logger.info(
        "execution.normal_handover.created",
        handover_id=str(record.handover_id),
        command_id=str(record.command_id),
        station_id=str(record.station_id),
        from_host_id=str(record.from_host_id),
        to_host_id=str(record.to_host_id),
        grant_id=str(record.grant_id),
        requested_by=str(record.requested_by),
    )
    return record


def grant_normal_handover(
    *,
    caller: Caller,
    handover_id: UUID,
    now: datetime,
    handovers: NormalHandoverRepository,
    grants: ExecutionGrantRepository,
) -> NormalHandover:
    """消费当前停写确认，在同一事务内替换旧权并冻结授予结果。"""
    authorize(caller, HANDOVER_PERMISSION)
    observed = handovers.by_identifier(handover_id)
    if observed is None:
        raise ExecutionRefusedError(ExecutionRefusalCode.NORMAL_HANDOVER_NOT_FOUND)
    # 与命令结案保持 grant -> handover 锁序；并发授予只能消费一次确认。
    current = grants.for_station(observed.station_id)
    record = handovers.by_identifier_for_update(handover_id)
    if record is None:
        raise ExecutionRefusedError(ExecutionRefusalCode.NORMAL_HANDOVER_NOT_FOUND)
    if record.new_grant_id is not None:
        return record
    active = handovers.pending_for_station(record.station_id)
    if (
        record.status is not NormalHandoverStatus.CONFIRMED
        or record.confirmed_at is None
        or active is None
        or active.handover_id != handover_id
    ):
        _refuse_handover(
            ExecutionRefusalCode.NORMAL_HANDOVER_NOT_CONFIRMED,
            caller=caller,
            handover_id=handover_id,
        )
    # 请求记时可能早于等待行锁期间提交的确认；授予及租约从已锁定的确认之后起算。
    now = max(now, record.confirmed_at)
    if (
        current is None
        or current.grant_id != record.grant_id
        or current.holder_host_id != record.from_host_id
        or current.lease_expires_at <= now
    ):
        _refuse_handover(
            ExecutionRefusalCode.NORMAL_HANDOVER_STALE_CONFIRMATION,
            caller=caller,
            handover_id=handover_id,
        )
    granted = grants.transfer_if_current(
        previous_grant_id=record.grant_id,
        previous_holder_id=record.from_host_id,
        value=StationGrant(
            grant_id=new_id(),
            station_id=record.station_id,
            holder_host_id=record.to_host_id,
            renewed_at=now,
            lease_expires_at=now + LEASE_TTL,
            request_id=record.handover_id,
        ),
    )
    if granted is None:
        _refuse_handover(
            ExecutionRefusalCode.NORMAL_HANDOVER_STALE_CONFIRMATION,
            caller=caller,
            handover_id=handover_id,
        )
    result = handovers.save(
        replace(
            record,
            new_grant_id=granted.grant_id,
            granted_at=now,
            granted_by=caller.user.id,
        )
    )
    _logger.info(
        "execution.normal_handover.granted",
        handover_id=str(handover_id),
        station_id=str(record.station_id),
        from_host_id=str(record.from_host_id),
        to_host_id=str(record.to_host_id),
        old_grant_id=str(record.grant_id),
        new_grant_id=str(granted.grant_id),
        actor_id=str(caller.user.id),
    )
    return result


def read_claimed_normal_handover(
    *,
    handover_id: UUID,
    command_id: UUID,
    from_host_id: UUID,
    now: datetime,
    handovers: NormalHandoverRepository,
) -> NormalHandover | None:
    """在已认证命令的行锁之后锁定请求；过期先持久落盘，取消与领取不能交错。"""
    record = handovers.by_identifier_for_update(handover_id)
    if record is None or record.command_id != command_id or record.from_host_id != from_host_id:
        return None
    record = _expire_normal_handover(record, now=now, handovers=handovers)
    return record if record.status is NormalHandoverStatus.PENDING else None


def read_normal_handover(
    *, caller: Caller, handover_id: UUID, now: datetime, handovers: NormalHandoverRepository
) -> NormalHandover:
    """读取请求并在观察到截止时刻后持久记录超时。"""
    authorize(caller, HANDOVER_PERMISSION)
    record = handovers.by_identifier_for_update(handover_id)
    if record is None:
        raise ExecutionRefusedError(ExecutionRefusalCode.NORMAL_HANDOVER_NOT_FOUND)
    return _expire_normal_handover(record, now=now, handovers=handovers)


def cancel_normal_handover(
    *,
    caller: Caller,
    handover_id: UUID,
    now: datetime,
    handovers: NormalHandoverRepository,
    commands: DeviceExecutionHandoverCommandGateway,
) -> NormalHandover:
    """只取消从未领取的请求；领取后旧机可能已停写，不能再承诺取消。"""
    authorize(caller, HANDOVER_PERMISSION)
    observed = handovers.by_identifier(handover_id)
    if observed is None:
        raise ExecutionRefusedError(ExecutionRefusalCode.NORMAL_HANDOVER_NOT_FOUND)
    was_claimed = commands.lock_claim(command_id=observed.command_id, now=now)
    record = read_normal_handover(
        caller=caller, handover_id=handover_id, now=now, handovers=handovers
    )
    if record.status is NormalHandoverStatus.PENDING:
        if was_claimed:
            _refuse_handover(
                ExecutionRefusalCode.NORMAL_HANDOVER_ALREADY_CLAIMED,
                caller=caller,
                handover_id=handover_id,
            )
        return handovers.save(
            replace(record, status=NormalHandoverStatus.CANCELLED, cancelled_at=now)
        )
    if record.status in {NormalHandoverStatus.CANCELLED, NormalHandoverStatus.TIMED_OUT}:
        return record
    _refuse_handover(
        ExecutionRefusalCode.NORMAL_HANDOVER_ALREADY_TERMINAL,
        caller=caller,
        handover_id=handover_id,
    )


def settle_normal_handover_command(
    *,
    handover_id: UUID,
    command_id: UUID,
    station_id: UUID | None,
    from_host_id: UUID,
    to_host_id: UUID | None,
    grant_id: UUID,
    host: InferenceHostIdentity,
    claim_token: str,
    outcome: str,
    failure_code: str | None,
    detail: str | None,
    now: datetime,
    handovers: NormalHandoverRepository,
    grants: ExecutionGrantRepository,
    commands: DeviceExecutionHandoverCommandGateway,
    authenticated: bool = False,
) -> tuple[NormalHandover | None, bool]:
    """在同一事务中复核冻结请求/租约、写状态并结案命令。

    返回 ``accepted`` 表示旧机确认已被本请求接受；陈旧、取消或身份不符的回报只结案命令，
    不改变后继请求，也不制造执行权。
    """
    # 已入队命令统一按 command -> grant(如需) -> handover 加锁；创建只入队全新的命令。
    commands.lock_claim(command_id=command_id, host=None if authenticated else host, now=now)
    authenticated = True
    observed = handovers.by_identifier(handover_id)
    current = None if observed is None else grants.for_station(observed.station_id)
    record = handovers.by_identifier_for_update(handover_id)
    if record is None:
        commands.complete(
            command_id=command_id,
            host=host,
            claim_token=claim_token,
            now=now,
            outcome="rejected",
            failure_code="NORMAL_HANDOVER_NOT_FOUND",
            detail="normal handover request no longer exists",
            authenticated=authenticated,
        )
        return None, False
    # 已被同一结果结案的重复回报直接幂等接受: 不重入陈旧拒绝, 也不改写已冻结终态。
    already_confirmed = (
        record.status is NormalHandoverStatus.CONFIRMED
        and outcome == "confirmed"
        and record.command_id == command_id
        and record.grant_id == grant_id
        and record.from_host_id == from_host_id
        and host.host_id == from_host_id
    )
    if already_confirmed:
        commands.complete(
            command_id=command_id,
            host=host,
            claim_token=claim_token,
            now=now,
            outcome="confirmed",
            authenticated=authenticated,
        )
        return record, False
    identity_matches = (
        record.handover_id == handover_id
        and record.command_id == command_id
        # station_id/to_host_id 为 None 表示由冻结请求提供, 不信任客户端自报身份。
        and (station_id is None or record.station_id == station_id)
        and record.from_host_id == from_host_id
        and (to_host_id is None or record.to_host_id == to_host_id)
        and record.grant_id == grant_id
        and host.host_id == from_host_id
    )
    if not identity_matches:
        commands.complete(
            command_id=command_id,
            host=host,
            claim_token=claim_token,
            now=now,
            outcome="rejected",
            failure_code="NORMAL_HANDOVER_IDENTITY_MISMATCH",
            detail="command result does not match the frozen request",
            authenticated=authenticated,
        )
        return record, False
    record = _expire_normal_handover(record, now=now, handovers=handovers)
    if (
        record.status is NormalHandoverStatus.FAILED
        and outcome == "failed"
        and record.failure_code == failure_code
        and record.failure_detail == detail
    ):
        commands.complete(
            command_id=command_id,
            host=host,
            claim_token=claim_token,
            now=now,
            outcome=outcome,
            failure_code=failure_code,
            detail=detail,
            authenticated=authenticated,
        )
        return record, False
    if record.status is not NormalHandoverStatus.PENDING:
        commands.complete(
            command_id=command_id,
            host=host,
            claim_token=claim_token,
            now=now,
            outcome="rejected",
            failure_code="NORMAL_HANDOVER_STALE_CONFIRMATION",
            detail=(
                record.failure_detail
                if record.failure_code == "NORMAL_HANDOVER_STALE_CONFIRMATION"
                else "normal handover request is no longer pending"
            ),
            authenticated=authenticated,
        )
        return record, False
    if (
        current is None
        or current.grant_id != record.grant_id
        or current.holder_host_id != record.from_host_id
        or current.lease_expires_at <= now
    ):
        record = handovers.save(
            replace(
                record,
                status=NormalHandoverStatus.FAILED,
                failed_at=now,
                failure_code="NORMAL_HANDOVER_STALE_CONFIRMATION",
                failure_detail="the frozen execution grant is no longer current",
            )
        )
        commands.complete(
            command_id=command_id,
            host=host,
            claim_token=claim_token,
            now=now,
            outcome="rejected",
            failure_code="NORMAL_HANDOVER_STALE_CONFIRMATION",
            detail="the frozen execution grant is no longer current",
            authenticated=authenticated,
        )
        return record, False
    if outcome == "confirmed":
        if failure_code is not None or detail is not None:
            raise ValueError("a confirmed handover result cannot carry a failure")
        confirmed = handovers.save(
            replace(record, status=NormalHandoverStatus.CONFIRMED, confirmed_at=now)
        )
        commands.complete(
            command_id=command_id,
            host=host,
            claim_token=claim_token,
            now=now,
            outcome="confirmed",
            authenticated=authenticated,
        )
        _logger.info(
            "execution.normal_handover.confirmed",
            handover_id=str(handover_id),
            station_id=str(station_id),
            from_host_id=str(from_host_id),
            to_host_id=str(to_host_id),
            grant_id=str(grant_id),
        )
        return confirmed, True
    if outcome != "failed" or not failure_code:
        raise ValueError("a failed handover result must carry a failure code")
    failed = handovers.save(
        replace(
            record,
            status=NormalHandoverStatus.FAILED,
            failed_at=now,
            failure_code=failure_code,
            failure_detail=detail,
        )
    )
    commands.complete(
        command_id=command_id,
        host=host,
        claim_token=claim_token,
        now=now,
        outcome="failed",
        failure_code=failure_code,
        detail=detail,
        authenticated=authenticated,
    )
    _logger.info(
        "execution.normal_handover.failed",
        handover_id=str(handover_id),
        station_id=str(station_id),
        failure_code=failure_code,
    )
    return failed, False


def _expire_normal_handover(
    record: NormalHandover,
    *,
    now: datetime,
    handovers: NormalHandoverRepository,
) -> NormalHandover:
    if record.status is not NormalHandoverStatus.PENDING or now < record.expires_at:
        return record
    return handovers.save(replace(record, status=NormalHandoverStatus.TIMED_OUT, timed_out_at=now))


def _check_normal_identity(
    record: NormalHandover,
    *,
    station_id: UUID,
    from_host_id: UUID,
    to_host_id: UUID,
    caller: Caller,
) -> None:
    if (record.station_id, record.from_host_id, record.to_host_id) != (
        station_id,
        from_host_id,
        to_host_id,
    ):
        _refuse_handover(
            ExecutionRefusalCode.NORMAL_HANDOVER_IDENTITY_MISMATCH,
            caller=caller,
            handover_id=record.handover_id,
        )


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
    "cancel_normal_handover",
    "confirm_handover",
    "create_handover",
    "create_normal_handover",
    "read_claimed_normal_handover",
    "read_handover",
    "read_handover_risk",
    "read_normal_handover",
    "settle_normal_handover_command",
]
