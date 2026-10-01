"""强制改绑双人确认在真实 PostgreSQL 与真实 auth 权限上的定向不变量。

覆盖 AC1/AC2/AC3 的后端闭环：两人成功且租约不变、同一人重复、内容不符、撤权后拒绝、
操作者失权/停用后拒绝，以及两连接真实并发：confirm 与撤权在同一把管理锁上串行、
两次 confirm 竞争同一把锁恰一人赢。
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session as DatabaseSession
from sqlalchemy.orm import sessionmaker

from factory_sop.auth.adapters.dependencies import handover_authority
from factory_sop.auth.adapters.repository import PostgresRoleRepository, PostgresUserRepository
from factory_sop.auth.authorization import Caller
from factory_sop.auth.model import Role, User, UserStatus
from factory_sop.auth.passwords import hash_password
from factory_sop.auth.permissions import Permission
from factory_sop.auth.usecases.roles import edit_role
from factory_sop.device.adapters.repository import (
    PostgresInferenceHostRepository,
    PostgresStationRepository,
)
from factory_sop.device.model import DeviceStatus, InferenceHost, Station
from factory_sop.execution.adapters.dependencies import lease_gateway
from factory_sop.execution.adapters.repository import (
    PostgresExecutionGrantRepository,
    PostgresHandoverRepository,
)
from factory_sop.execution.errors import ExecutionRefusalCode, ExecutionRefusedError
from factory_sop.execution.model import HANDOVER_RISK_STATEMENT, HandoverConfirmation
from factory_sop.execution.usecases import confirm_handover, create_handover, read_handover
from factory_sop.identifiers import new_id

NOW = datetime(2026, 9, 22, 1, 0, tzinfo=UTC)
PASSWORD = "assembly-line-3"  # pragma: allowlist secret


def _user(login_name: str) -> User:
    return User(
        id=new_id(),
        login_name=login_name,
        display_name=login_name,
        password_hash=hash_password(PASSWORD),
        status=UserStatus.ACTIVE,
    )


def _caller(user: User, *granted: Permission) -> Caller:
    return Caller(user=user, granted=frozenset(granted))


def _host(name: str) -> InferenceHost:
    return InferenceHost(
        id=new_id(),
        name=name,
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


def _station() -> Station:
    return Station(
        id=new_id(),
        code=f"HAND-{new_id().hex[:8]}",
        name="强制改绑测试工位",
        tags=(),
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=new_id(),
        updated_by=new_id(),
        created_at=NOW,
        updated_at=NOW,
    )


def _arrange(
    session: DatabaseSession,
) -> tuple[User, User, Role, Station, InferenceHost, InferenceHost]:
    operator, second = _user("handover.operator"), _user("handover.second")
    users = PostgresUserRepository(session)
    users.add(operator)
    users.add(second)
    role = Role(
        id=new_id(),
        code=f"device_admin_{new_id().hex[:6]}",
        name="设备管理员",
        permissions=frozenset({Permission.HANDOVER_EDIT}),
    )
    roles = PostgresRoleRepository(session)
    roles.add(role)
    roles.assign(user_id=operator.id, role_ids=[role.id])
    roles.assign(user_id=second.id, role_ids=[role.id])
    station = _station()
    PostgresStationRepository(session).add(station)
    hosts = PostgresInferenceHostRepository(session)
    host_from, host_to = _host("旧机"), _host("新机")
    hosts.add(host_from)
    hosts.add(host_to)
    session.flush()
    return operator, second, role, station, host_from, host_to


def _create(
    session: DatabaseSession,
    operator: User,
    station: Station,
    from_host: InferenceHost,
    to_host: InferenceHost,
    *,
    now: datetime = NOW,
) -> HandoverConfirmation:
    return create_handover(
        caller=_caller(operator, Permission.HANDOVER_EDIT),
        station_id=station.id,
        from_host_id=from_host.id,
        to_host_id=to_host.id,
        risk_acknowledgement=HANDOVER_RISK_STATEMENT,
        now=now,
        handovers=PostgresHandoverRepository(session),
        authority=handover_authority(session),
    )


def _confirm(
    session: DatabaseSession,
    second: User,
    record: HandoverConfirmation,
    station: Station,
    from_host: InferenceHost,
    to_host: InferenceHost,
    *,
    now: datetime = NOW,
) -> HandoverConfirmation:
    return confirm_handover(
        caller=_caller(second, Permission.HANDOVER_EDIT),
        handover_id=record.handover_id,
        station_id=station.id,
        from_host_id=from_host.id,
        to_host_id=to_host.id,
        risk_acknowledgement=HANDOVER_RISK_STATEMENT,
        now=now,
        handovers=PostgresHandoverRepository(session),
        authority=handover_authority(session),
    )


def test_two_users_confirm_same_request_and_grant_is_untouched(session: DatabaseSession) -> None:
    operator, second, _role, station, from_host, to_host = _arrange(session)
    grant = lease_gateway(session).acquire(
        station_id=station.id, holder_host_id=from_host.id, request_id=new_id(), now=NOW
    )
    session.flush()
    record = _create(session, operator, station, from_host, to_host)
    assert (record.operator_id, record.operator_confirmed_at) == (operator.id, NOW)
    assert record.operator_risk_shown is True
    assert record.second_operator_id is None
    assert (
        read_handover(
            caller=_caller(operator, Permission.HANDOVER_EDIT),
            handover_id=record.handover_id,
            handovers=PostgresHandoverRepository(session),
        )
        == record
    )

    confirmed = _confirm(
        session, second, record, station, from_host, to_host, now=NOW + timedelta(minutes=5)
    )
    assert confirmed.second_operator_id == second.id
    assert confirmed.second_confirmed_at == NOW + timedelta(minutes=5)
    assert confirmed.second_risk_shown is True
    assert confirmed.operator_id == operator.id
    assert PostgresHandoverRepository(session).by_identifier(record.handover_id) == confirmed
    # AC3：审批满足不激活新输出权，也不改物理执行权租约。
    assert PostgresExecutionGrantRepository(session).for_holder(from_host.id) == (grant,)


def test_operator_cannot_second_own_request(session: DatabaseSession) -> None:
    operator, _second, _role, station, from_host, to_host = _arrange(session)
    record = _create(session, operator, station, from_host, to_host)
    with pytest.raises(ExecutionRefusedError) as refused:
        _confirm(session, operator, record, station, from_host, to_host)
    assert refused.value.code is ExecutionRefusalCode.HANDOVER_SAME_OPERATOR
    stored = PostgresHandoverRepository(session).by_identifier(record.handover_id)
    assert stored is not None
    assert stored.second_operator_id is None


def test_second_confirmation_of_different_content_is_refused(session: DatabaseSession) -> None:
    operator, second, _role, station, from_host, to_host = _arrange(session)
    record = _create(session, operator, station, from_host, to_host)
    other = _host("第三个目标机")
    PostgresInferenceHostRepository(session).add(other)
    session.flush()
    with pytest.raises(ExecutionRefusedError) as refused:
        _confirm(session, second, record, station, from_host, other)
    assert refused.value.code is ExecutionRefusalCode.HANDOVER_CONTENT_MISMATCH
    stored = PostgresHandoverRepository(session).by_identifier(record.handover_id)
    assert stored is not None
    assert stored.second_operator_id is None


def test_confirming_after_revocation_is_refused(session: DatabaseSession) -> None:
    operator, second, _role, station, from_host, to_host = _arrange(session)
    record = _create(session, operator, station, from_host, to_host)
    PostgresRoleRepository(session).assign(user_id=second.id, role_ids=[])
    session.flush()
    with pytest.raises(ExecutionRefusedError) as refused:
        _confirm(session, second, record, station, from_host, to_host)
    assert refused.value.code is ExecutionRefusalCode.HANDOVER_NOT_ELIGIBLE
    stored = PostgresHandoverRepository(session).by_identifier(record.handover_id)
    assert stored is not None
    assert stored.second_operator_id is None


@pytest.mark.parametrize("loss", ["permission_revoked", "operator_deactivated"])
def test_confirm_is_refused_when_operator_loses_eligibility(
    session: DatabaseSession, loss: str
) -> None:
    """建立后操作者失去权限或被停用，第二人不能凑足两人。"""
    operator, second, _role, station, from_host, to_host = _arrange(session)
    record = _create(session, operator, station, from_host, to_host)
    if loss == "permission_revoked":
        PostgresRoleRepository(session).assign(user_id=operator.id, role_ids=[])
    else:
        PostgresUserRepository(session).update(replace(operator, status=UserStatus.DEACTIVATED))
    session.flush()
    with pytest.raises(ExecutionRefusedError) as refused:
        _confirm(session, second, record, station, from_host, to_host)
    assert refused.value.code is ExecutionRefusalCode.HANDOVER_NOT_ELIGIBLE
    stored = PostgresHandoverRepository(session).by_identifier(record.handover_id)
    assert stored is not None
    assert stored.second_operator_id is None


def test_concurrent_second_confirmations_have_exactly_one_winner(engine: Engine) -> None:
    """两个真实连接同时做第二确认：管理锁串行化，恰一人赢，另一人得已确认。

    第一个事务完成条件更新后持锁不提交，第二个事务在 `pg_advisory_xact_lock` 上真实等待
    （用 `pg_locks` 的 ungranted advisory 锁证明确在竞争）；释放后第二个事务读到已确认事实。
    """
    setup = sessionmaker(bind=engine)()
    operator, second, role, station, from_host, to_host = _arrange(setup)
    second_b = _user("handover.second_b")
    PostgresUserRepository(setup).add(second_b)
    PostgresRoleRepository(setup).assign(user_id=second_b.id, role_ids=[role.id])
    record = _create(setup, operator, station, from_host, to_host)
    setup.commit()
    setup.close()

    winner_holding_lock = threading.Event()
    release_winner = threading.Event()
    outcomes: dict[str, object] = {}

    def first() -> None:
        connection = engine.connect()
        transaction = connection.begin()
        session = sessionmaker(bind=connection)()
        try:
            outcomes["first"] = _confirm(session, second, record, station, from_host, to_host)
            winner_holding_lock.set()
            if not release_winner.wait(timeout=30):
                raise AssertionError("winning confirmation was never released")
            transaction.commit()
        finally:
            session.close()
            connection.close()

    def contender() -> None:
        connection = engine.connect()
        transaction = connection.begin()
        session = sessionmaker(bind=connection)()
        try:
            try:
                _confirm(session, second_b, record, station, from_host, to_host)
            except ExecutionRefusedError as refused:
                outcomes["contender"] = refused.code
            finally:
                transaction.rollback()
        finally:
            session.close()
            connection.close()

    first_thread = threading.Thread(target=first)
    contender_thread = threading.Thread(target=contender)
    try:
        first_thread.start()
        assert winner_holding_lock.wait(timeout=30)
        contender_thread.start()
        _wait_for_blocked_administration_lock(engine)
        release_winner.set()
        first_thread.join(timeout=30)
        contender_thread.join(timeout=30)

        winner = outcomes["first"]
        assert isinstance(winner, HandoverConfirmation)
        assert winner.second_operator_id == second.id
        assert outcomes["contender"] is ExecutionRefusalCode.HANDOVER_ALREADY_CONFIRMED

        verification = sessionmaker(bind=engine)()
        try:
            stored = PostgresHandoverRepository(verification).by_identifier(record.handover_id)
        finally:
            verification.close()
        assert stored is not None
        assert stored.second_operator_id == second.id
    finally:
        release_winner.set()
        first_thread.join(timeout=30)
        contender_thread.join(timeout=30)
        _cleanup(
            engine,
            handover_id=record.handover_id,
            user_ids=(operator.id, second.id, second_b.id),
            role_id=role.id,
            station_id=station.id,
            host_ids=(from_host.id, to_host.id),
        )


def _wait_for_blocked_administration_lock(engine: Engine) -> None:
    """等到第二个事务真实阻塞在管理 advisory 锁上，证明两个事务在竞争。"""
    deadline = time.monotonic() + 30
    with engine.connect() as connection:
        while time.monotonic() < deadline:
            waiting = connection.execute(
                text(
                    "SELECT count(*) FROM pg_locks l "
                    "JOIN pg_database d ON d.oid = l.database "
                    "WHERE l.locktype = 'advisory' AND NOT l.granted "
                    "AND d.datname = current_database()"
                )
            ).scalar_one()
            if waiting:
                return
            time.sleep(0.05)
    raise AssertionError("no second confirmation contended for the administration lock")


def test_confirm_and_revocation_serialize_on_the_administration_lock(engine: Engine) -> None:
    setup = sessionmaker(bind=engine)()
    operator, second, role, station, from_host, to_host = _arrange(setup)
    record = _create(setup, operator, station, from_host, to_host)
    setup.commit()
    setup.close()
    admin = _caller(_user("handover.admin"), Permission.ROLE_EDIT)
    try:
        # A 拿管理锁完成第二确认但不提交：撤权事务 B 必须先等它。
        connection_a = engine.connect()
        transaction_a = connection_a.begin()
        session_a = sessionmaker(bind=connection_a)()
        confirmed = _confirm(session_a, second, record, station, from_host, to_host)
        assert confirmed.second_operator_id == second.id

        connection_b = engine.connect()
        transaction_b = connection_b.begin()
        connection_b.execute(text("SET LOCAL lock_timeout = '200ms'"))
        session_b = sessionmaker(bind=connection_b)()
        with pytest.raises(OperationalError):
            edit_role(
                caller=admin,
                role_id=role.id,
                name=role.name,
                permissions=(),
                roles=PostgresRoleRepository(session_b),
            )
        transaction_b.rollback()
        session_b.close()
        connection_b.close()

        transaction_a.rollback()
        session_a.close()
        connection_a.close()

        retry = sessionmaker(bind=engine)()
        edit_role(
            caller=admin,
            role_id=role.id,
            name=role.name,
            permissions=(),
            roles=PostgresRoleRepository(retry),
        )
        retry.commit()
        retry.close()

        later = sessionmaker(bind=engine)()
        with pytest.raises(ExecutionRefusedError) as refused:
            _confirm(later, second, record, station, from_host, to_host)
        assert refused.value.code is ExecutionRefusalCode.HANDOVER_NOT_ELIGIBLE
        later.rollback()
        later.close()
    finally:
        _cleanup(
            engine,
            handover_id=record.handover_id,
            user_ids=(operator.id, second.id),
            role_id=role.id,
            station_id=station.id,
            host_ids=(from_host.id, to_host.id),
        )


def _cleanup(
    engine: Engine,
    *,
    handover_id: UUID,
    user_ids: tuple[UUID, ...],
    role_id: UUID,
    station_id: UUID,
    host_ids: tuple[UUID, ...],
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM execution_handover WHERE handover_id = :handover_id"),
            {"handover_id": handover_id},
        )
        connection.execute(
            text("DELETE FROM auth_user WHERE id = ANY(:ids)"), {"ids": list(user_ids)}
        )
        connection.execute(text("DELETE FROM auth_role WHERE id = :role_id"), {"role_id": role_id})
        connection.execute(
            text("DELETE FROM device_station WHERE id = :station_id"), {"station_id": station_id}
        )
        connection.execute(
            text("DELETE FROM device_inference_host WHERE id = ANY(:ids)"),
            {"ids": list(host_ids)},
        )
