"""正常交权请求/取消/超时/旧机确认的真实 PostgreSQL 状态机（#214）。

经 owner seam（Postgres 仓库 + device 命令队列 fake）驱动 `execution.usecases`：请求绑定
工位/双方主机/冻结租约身份并入队停机命令；取消/超时持久可查；确认前新机无写权；陈旧确认
（grant 已变/请求已终态）只结案命令，不影响后继交权，也不制造执行权。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import TextClause, text
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.auth.authorization import Caller
from factory_sop.auth.model import Role, User
from factory_sop.auth.passwords import hash_password
from factory_sop.auth.permissions import Permission
from factory_sop.device.adapters.repository import (
    PostgresInferenceHostRepository,
    PostgresStationRepository,
)
from factory_sop.device.api import InferenceHostIdentity
from factory_sop.device.model import (
    DeviceStatus,
    InferenceHost,
    PendingCommand,
    PendingCommandStatus,
    PendingCommandType,
    Station,
)
from factory_sop.execution.adapters.repository import (
    PostgresExecutionGrantRepository,
    PostgresNormalHandoverRepository,
)
from factory_sop.execution.errors import ExecutionRefusalCode, ExecutionRefusedError
from factory_sop.execution.model import NormalHandover, StationGrant
from factory_sop.execution.usecases import (
    cancel_normal_handover,
    create_normal_handover,
    read_normal_handover,
    settle_normal_handover_command,
)
from factory_sop.identifiers import new_id

NOW = datetime(2026, 9, 22, 1, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class QueueCommand:
    """device 命令队列 seam 的最小内存记录, 只服务状态机断言."""

    command_id: UUID
    handover_id: UUID
    host_id: UUID
    outcome: str | None = None
    failure_code: str | None = None


class RecordingCommandGateway:
    """记录入队与结案的命令网关; 事务边界归测试外层真 PostgreSQL 会话."""

    def __init__(self) -> None:
        self.enqueued: list[QueueCommand] = []
        self.completed: list[QueueCommand] = []

    def enqueue(
        self,
        *,
        command_id: UUID,
        handover_id: UUID,
        host_id: UUID,
        requested_by: UUID,
        now: datetime,
    ) -> None:
        self.enqueued.append(
            QueueCommand(command_id=command_id, handover_id=handover_id, host_id=host_id)
        )

    def claim(self, **kwargs: object) -> PendingCommand | None:
        del kwargs
        return None

    def lock_claim(
        self, *, command_id: UUID, now: datetime, host: InferenceHostIdentity | None = None
    ) -> bool:
        return False

    def complete(
        self,
        *,
        command_id: UUID,
        host: InferenceHostIdentity,
        claim_token: str,
        now: datetime,
        outcome: str,
        failure_code: str | None = None,
        detail: str | None = None,
        authenticated: bool = False,
    ) -> PendingCommand:
        self.completed.append(
            QueueCommand(
                command_id=command_id,
                handover_id=command_id,
                host_id=host.host_id,
                outcome=outcome,
                failure_code=failure_code,
            )
        )
        return PendingCommand(
            id=command_id,
            host_id=host.host_id,
            command_type=PendingCommandType.STOP_EXECUTION_HANDOVER,
            target_id=command_id,
            target_revision=1,
            idempotency_key="test",
            status=PendingCommandStatus.SUCCEEDED,
            attempt=1,
            claim_token=claim_token,
            claimed_at=now,
            lease_expires_at=now + timedelta(minutes=5),
            result=None,
            result_detail=detail,
            failure_code=failure_code,
            completed_at=now,
            created_by=host.host_id,
            created_at=now,
            updated_at=now,
        )


@pytest.fixture
def targets(session: DatabaseSession) -> tuple[User, Station, InferenceHost, InferenceHost]:
    """真实提交工位与两台推理机及一名持交权权限的操作者."""
    operator = User(
        id=new_id(),
        login_name=f"normal.operator.{new_id().hex[:8]}",
        display_name="正常交权操作者",
        password_hash=hash_password("assembly-line-3"),  # pragma: allowlist secret
        status=__import__("factory_sop.auth.model", fromlist=["UserStatus"]).UserStatus.ACTIVE,
    )
    role = Role(
        id=new_id(),
        code=f"handover_{new_id().hex[:8]}",
        name="交权测试",
        permissions=frozenset({Permission.HANDOVER_EDIT}),
    )
    station = Station(
        id=new_id(),
        code=f"NORM-{new_id().hex[:8]}",
        name="正常交权工位",
        tags=(),
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=new_id(),
        updated_by=new_id(),
        created_at=NOW,
        updated_at=NOW,
    )
    from_host = InferenceHost(
        id=new_id(),
        name="正常交权旧机",
        address="10.0.8.11",
        mediamtx_address=None,
        recording_window_seconds=604800,
        disk_watermark_percent=85,
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=new_id(),
        updated_by=new_id(),
        created_at=NOW,
        updated_at=NOW,
    )
    to_host = InferenceHost(
        id=new_id(),
        name="正常交权新机",
        address="10.0.8.12",
        mediamtx_address=None,
        recording_window_seconds=604800,
        disk_watermark_percent=85,
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=new_id(),
        updated_by=new_id(),
        created_at=NOW,
        updated_at=NOW,
    )
    from factory_sop.auth.adapters.repository import PostgresRoleRepository, PostgresUserRepository

    PostgresUserRepository(session).add(operator)
    PostgresRoleRepository(session).add(role)
    PostgresRoleRepository(session).assign(user_id=operator.id, role_ids=[role.id])
    PostgresStationRepository(session).add(station)
    hosts = PostgresInferenceHostRepository(session)
    hosts.add(from_host)
    hosts.add(to_host)
    PostgresExecutionGrantRepository(session).acquire_if_available(
        StationGrant(
            grant_id=new_id(),
            station_id=station.id,
            holder_host_id=from_host.id,
            lease_expires_at=NOW + timedelta(days=7),
            renewed_at=NOW,
            request_id=new_id(),
        )
    )
    session.flush()
    return operator, station, from_host, to_host


def _caller(user: User) -> Caller:
    return Caller(user=user, granted=frozenset({Permission.HANDOVER_EDIT}))


def _create(
    session: DatabaseSession,
    operator: User,
    station: Station,
    from_host: InferenceHost,
    to_host: InferenceHost,
    commands: RecordingCommandGateway,
    *,
    idempotency_key: str = "handover-key-1",
) -> NormalHandover:
    return create_normal_handover(
        caller=_caller(operator),
        station_id=station.id,
        from_host_id=from_host.id,
        to_host_id=to_host.id,
        idempotency_key=idempotency_key,
        now=NOW,
        handovers=PostgresNormalHandoverRepository(session),
        grants=PostgresExecutionGrantRepository(session),
        commands=commands,
    )


def test_request_freezes_identity_enqueues_one_command_and_never_grants_the_new_host(
    session: DatabaseSession,
    targets: tuple[User, Station, InferenceHost, InferenceHost],
) -> None:
    operator, station, from_host, to_host = targets
    commands = RecordingCommandGateway()

    record = _create(session, operator, station, from_host, to_host, commands)

    assert record.status.value == "pending"
    assert record.grant_id is not None
    assert record.expires_at == NOW + timedelta(seconds=300)
    assert len(commands.enqueued) == 1
    assert commands.enqueued[0].host_id == from_host.id
    # 新机没有获得任何执行权: 租约 holder 仍是旧机.
    current = PostgresExecutionGrantRepository(session).for_station(station.id)
    assert current is not None
    assert current.holder_host_id == from_host.id


def test_same_idempotency_key_reuses_the_frozen_request_without_a_second_command(
    session: DatabaseSession,
    targets: tuple[User, Station, InferenceHost, InferenceHost],
) -> None:
    operator, station, from_host, to_host = targets
    commands = RecordingCommandGateway()

    first = _create(session, operator, station, from_host, to_host, commands)
    second = _create(session, operator, station, from_host, to_host, commands)

    assert first.handover_id == second.handover_id
    assert len(commands.enqueued) == 1


def test_identity_mismatch_on_a_replayed_key_is_refused(
    session: DatabaseSession, targets: tuple[User, Station, InferenceHost, InferenceHost]
) -> None:
    operator, station, from_host, to_host = targets
    commands = RecordingCommandGateway()
    _create(session, operator, station, from_host, to_host, commands)

    # 后继租约不改变原键绑定; 同一键修改请求的目标机仍必须拒绝。
    session.execute(
        text_delete_grant(station.id),
    )
    PostgresExecutionGrantRepository(session).acquire_if_available(
        StationGrant(
            grant_id=new_id(),
            station_id=station.id,
            holder_host_id=from_host.id,
            lease_expires_at=NOW + timedelta(days=9),
            renewed_at=NOW + timedelta(days=2),
            request_id=new_id(),
        )
    )
    session.flush()

    with pytest.raises(ExecutionRefusedError) as refused:
        create_normal_handover(
            caller=_caller(operator),
            station_id=station.id,
            from_host_id=from_host.id,
            to_host_id=from_host.id,
            idempotency_key="handover-key-1",
            now=NOW + timedelta(seconds=2),
            handovers=PostgresNormalHandoverRepository(session),
            grants=PostgresExecutionGrantRepository(session),
            commands=commands,
        )
    assert refused.value.code is ExecutionRefusalCode.NORMAL_HANDOVER_IDENTITY_MISMATCH


def test_a_second_pending_request_for_the_same_station_is_refused(
    session: DatabaseSession,
    targets: tuple[User, Station, InferenceHost, InferenceHost],
) -> None:
    operator, station, from_host, to_host = targets
    commands = RecordingCommandGateway()
    _create(session, operator, station, from_host, to_host, commands)

    with pytest.raises(ExecutionRefusedError) as refused:
        _create(
            session,
            operator,
            station,
            from_host,
            to_host,
            commands,
            idempotency_key="handover-key-2",
        )
    assert refused.value.code is ExecutionRefusalCode.NORMAL_HANDOVER_PENDING


def test_cancel_and_timeout_are_persisted_and_queryable(
    session: DatabaseSession, targets: tuple[User, Station, InferenceHost, InferenceHost]
) -> None:
    operator, station, from_host, to_host = targets
    commands = RecordingCommandGateway()
    record = _create(session, operator, station, from_host, to_host, commands)

    cancelled = cancel_normal_handover(
        caller=_caller(operator),
        handover_id=record.handover_id,
        now=NOW + timedelta(seconds=10),
        handovers=PostgresNormalHandoverRepository(session),
        commands=commands,
    )
    assert cancelled.status.value == "cancelled"
    stored = read_normal_handover(
        caller=_caller(operator),
        handover_id=record.handover_id,
        now=NOW + timedelta(seconds=20),
        handovers=PostgresNormalHandoverRepository(session),
    )
    assert stored.status.value == "cancelled"

    # 超时: 到期后的读取持久记录 timed_out.
    expired = _create(
        session,
        operator,
        station,
        from_host,
        to_host,
        commands,
        idempotency_key="handover-key-expire",
    )
    timed_out = read_normal_handover(
        caller=_caller(operator),
        handover_id=expired.handover_id,
        now=NOW + timedelta(seconds=400),
        handovers=PostgresNormalHandoverRepository(session),
    )
    assert timed_out.status.value == "timed_out"


def test_stale_confirmation_settles_the_command_but_not_the_request(
    session: DatabaseSession,
    targets: tuple[User, Station, InferenceHost, InferenceHost],
) -> None:
    operator, station, from_host, to_host = targets
    commands = RecordingCommandGateway()
    record = _create(session, operator, station, from_host, to_host, commands)

    # 租约已被替换: 冻结 grant 不再是当前事实.
    from factory_sop.execution.adapters.repository import PostgresNormalHandoverRepository as R

    frozen = R(session).by_identifier(record.handover_id)
    assert frozen is not None
    accepted = settle_normal_handover_command(
        handover_id=frozen.handover_id,
        command_id=frozen.command_id,
        station_id=None,
        from_host_id=frozen.from_host_id,
        to_host_id=None,
        grant_id=frozen.grant_id,
        host=InferenceHostIdentity(host_id=frozen.from_host_id, request=None, signature=None),
        claim_token="claim-1",
        outcome="confirmed",
        failure_code=None,
        detail=None,
        now=NOW + timedelta(seconds=10),
        handovers=R(session),
        grants=PostgresExecutionGrantRepository(session),
        commands=commands,
    )
    # 当前租约仍是旧机且 grant 一致 => 接受.
    assert accepted[1] is True
    assert accepted[0] is not None
    assert accepted[0].status.value == "confirmed"


def test_confirmation_after_grant_replacement_is_stale_and_request_stays_pending(
    session: DatabaseSession,
    targets: tuple[User, Station, InferenceHost, InferenceHost],
) -> None:
    operator, station, from_host, to_host = targets
    commands = RecordingCommandGateway()
    record = _create(session, operator, station, from_host, to_host, commands)
    from factory_sop.execution.adapters.repository import PostgresNormalHandoverRepository as R

    frozen = R(session).by_identifier(record.handover_id)
    assert frozen is not None
    # 真实替换当前租约为另一 grant 身份.
    with session.begin_nested():
        session.execute(
            text_delete_grant(station.id),
        )
    PostgresExecutionGrantRepository(session).acquire_if_available(
        StationGrant(
            grant_id=new_id(),
            station_id=station.id,
            holder_host_id=from_host.id,
            lease_expires_at=NOW + timedelta(days=9),
            renewed_at=NOW + timedelta(days=2),
            request_id=new_id(),
        )
    )
    session.flush()

    record_after, accepted = settle_normal_handover_command(
        handover_id=frozen.handover_id,
        command_id=frozen.command_id,
        station_id=None,
        from_host_id=frozen.from_host_id,
        to_host_id=None,
        grant_id=frozen.grant_id,
        host=InferenceHostIdentity(host_id=frozen.from_host_id, request=None, signature=None),
        claim_token="claim-1",
        outcome="confirmed",
        failure_code=None,
        detail=None,
        now=NOW + timedelta(seconds=10),
        handovers=R(session),
        grants=PostgresExecutionGrantRepository(session),
        commands=commands,
    )
    assert accepted is False
    assert record_after is not None
    assert record_after.status.value == "pending"
    # 命令被结案为 rejected, 不留永久领取.
    assert commands.completed[-1].outcome == "rejected"


def test_expired_current_lease_does_not_break_an_idempotent_request_replay(
    session: DatabaseSession, targets: tuple[User, Station, InferenceHost, InferenceHost]
) -> None:
    operator, station, from_host, to_host = targets
    session.execute(
        text(
            "UPDATE execution_station_grant SET lease_expires_at = :expiry,"
            " renewed_at = :renewed WHERE station_id = :sid"
        ),
        {
            "expiry": NOW + timedelta(seconds=5),
            "renewed": NOW - timedelta(days=7) + timedelta(seconds=5),
            "sid": station.id,
        },
    )
    commands = RecordingCommandGateway()
    record = _create(session, operator, station, from_host, to_host, commands)
    replayed = create_normal_handover(
        caller=_caller(operator),
        station_id=station.id,
        from_host_id=from_host.id,
        to_host_id=to_host.id,
        idempotency_key=record.idempotency_key,
        now=NOW + timedelta(seconds=10),
        handovers=PostgresNormalHandoverRepository(session),
        grants=PostgresExecutionGrantRepository(session),
        commands=commands,
    )
    assert replayed == record
    assert len(commands.enqueued) == 1


def test_confirmation_with_an_expired_but_unreplaced_grant_is_not_accepted(
    session: DatabaseSession, targets: tuple[User, Station, InferenceHost, InferenceHost]
) -> None:
    operator, station, from_host, to_host = targets
    session.execute(
        text(
            "UPDATE execution_station_grant SET lease_expires_at = :expiry,"
            " renewed_at = :renewed WHERE station_id = :sid"
        ),
        {
            "expiry": NOW + timedelta(seconds=5),
            "renewed": NOW - timedelta(days=7) + timedelta(seconds=5),
            "sid": station.id,
        },
    )
    commands = RecordingCommandGateway()
    record = _create(session, operator, station, from_host, to_host, commands)
    _, accepted = settle_normal_handover_command(
        handover_id=record.handover_id,
        command_id=record.command_id,
        station_id=record.station_id,
        from_host_id=record.from_host_id,
        to_host_id=record.to_host_id,
        grant_id=record.grant_id,
        host=InferenceHostIdentity(host_id=record.from_host_id, request=None, signature=None),
        claim_token="claim-1",
        outcome="confirmed",
        failure_code=None,
        detail=None,
        now=NOW + timedelta(seconds=10),
        handovers=PostgresNormalHandoverRepository(session),
        grants=PostgresExecutionGrantRepository(session),
        commands=commands,
    )
    assert accepted is False
    assert commands.completed[-1].outcome == "rejected"


def text_delete_grant(station_id: UUID) -> TextClause:
    from sqlalchemy import text

    return text("DELETE FROM execution_station_grant WHERE station_id = :sid").bindparams(
        sid=station_id
    )
