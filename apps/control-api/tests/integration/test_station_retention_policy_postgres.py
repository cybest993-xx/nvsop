"""真实 PostgreSQL 验证 retention 拥有的工位覆盖记录、CAS 与全局隔离。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from threading import Barrier

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from factory_sop.auth.authorization import Caller
from factory_sop.auth.model import User, UserStatus
from factory_sop.auth.permissions import Permission
from factory_sop.device.adapters.repository import PostgresStationRepository
from factory_sop.device.model import DeviceStatus, Station
from factory_sop.device.usecases.retention import RepositoryDeviceStationFacts
from factory_sop.identifiers import new_id
from factory_sop.retention.adapters.repository import (
    PostgresRetentionPolicyRepository,
    PostgresStationRetentionOverrideRepository,
    RepositoryStationRetentionProjectionGateway,
)
from factory_sop.retention.api import (
    DEFAULT_RETENTION_POLICY,
    RetentionMode,
    RetentionPolicy,
    RetentionPolicyConflictError,
    RetentionPolicyMode,
    RetentionRule,
    RetentionStationPresence,
    StationRetentionRecord,
    read_station_retention,
    update_policy,
    update_station_retention,
)

NOW = datetime(2026, 10, 14, tzinfo=UTC)


def _caller(*permissions: Permission) -> Caller:
    return Caller(
        user=User(
            new_id(),
            "retention-operator",
            "Retention Operator",
            "argon2-encoded",  # pragma: allowlist secret
            UserStatus.ACTIVE,
        ),
        granted=frozenset(permissions),
    )


def _custom_group() -> RetentionPolicy:
    return replace(
        DEFAULT_RETENTION_POLICY,
        evidence_pass=RetentionRule(RetentionMode.DISCARD),
        evidence_indeterminate=RetentionRule(RetentionMode.DURATION, 3600),
    )


def _changed_global() -> RetentionPolicy:
    return replace(DEFAULT_RETENTION_POLICY, disposal=RetentionRule(RetentionMode.DURATION, 3600))


def _station() -> Station:
    actor = new_id()
    station_id = new_id()
    return Station(
        id=station_id,
        code=f"S-{station_id.hex}",
        name="保留覆盖工位",
        tags=(),
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=actor,
        updated_by=actor,
        created_at=NOW,
        updated_at=NOW,
    )


def test_retention_owns_the_only_station_override_table(engine: Engine) -> None:
    with engine.connect() as connection:
        columns = set(
            connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'retention_station_policy'"
                )
            ).scalars()
        )
        station_columns = set(
            connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'device_station'"
                )
            ).scalars()
        )

    assert columns == {"station_id", "mode", "overrides", "revision"}
    # 工位表不保留任何保留配置列：覆盖记录只有 retention 一个持有者，无双权威。
    assert not {name for name in station_columns if name.startswith("retention_")}


def test_first_write_is_cas_and_only_the_current_version_can_replace(session: Session) -> None:
    stations = PostgresStationRepository(session)
    facts = RepositoryDeviceStationFacts(stations=stations)
    records = PostgresStationRetentionOverrideRepository(session)
    policies = PostgresRetentionPolicyRepository(session)
    edit = _caller(Permission.RETENTION_POLICY_EDIT)
    view = _caller(Permission.RETENTION_POLICY_VIEW)
    station = _station()
    stations.add(station)
    custom = _custom_group()

    created = update_station_retention(
        edit,
        station.id,
        mode=RetentionPolicyMode.CUSTOM,
        overrides=custom,
        expected_revision=0,
        policies=policies,
        records=records,
        stations=facts,
    )

    assert created.revision == 1
    assert created.mode is RetentionPolicyMode.CUSTOM
    assert created.overrides == custom
    assert created.effective == custom
    assert created.default == DEFAULT_RETENTION_POLICY
    assert session.execute(text("SELECT count(*) FROM retention_station_policy")).scalar_one() == 1

    # 首写 CAS：并发下第二个 expected_revision=0 被唯一键拒绝，不覆盖已建立的记录。
    with pytest.raises(RetentionPolicyConflictError):
        update_station_retention(
            edit,
            station.id,
            mode=RetentionPolicyMode.FOLLOW_GLOBAL,
            overrides=None,
            expected_revision=0,
            policies=policies,
            records=records,
            stations=facts,
        )
    persisted = records.get(station.id)
    assert persisted is not None
    assert persisted.revision == 1
    assert persisted.overrides == custom

    updated = update_station_retention(
        edit,
        station.id,
        mode=RetentionPolicyMode.CUSTOM,
        overrides=custom,
        expected_revision=1,
        policies=policies,
        records=records,
        stations=facts,
    )

    assert updated.revision == 2

    # 条件更新防丢失：陈旧版本被拒且不落库。
    with pytest.raises(RetentionPolicyConflictError):
        update_station_retention(
            edit,
            station.id,
            mode=RetentionPolicyMode.FOLLOW_GLOBAL,
            overrides=None,
            expected_revision=1,
            policies=policies,
            records=records,
            stations=facts,
        )
    assert records.get(station.id) == replace(persisted, revision=2)
    assert session.execute(text("SELECT count(*) FROM retention_station_policy")).scalar_one() == 1

    # 读回时权限与生效解析都走同一条 owner 用例。
    reread = read_station_retention(
        view, station.id, policies=policies, overrides=records, stations=facts
    )
    assert reread.revision == 2
    assert reread.effective == custom


def test_a_global_change_does_not_touch_a_custom_station_group(session: Session) -> None:
    stations = PostgresStationRepository(session)
    facts = RepositoryDeviceStationFacts(stations=stations)
    records = PostgresStationRetentionOverrideRepository(session)
    policies = PostgresRetentionPolicyRepository(session)
    edit = _caller(Permission.RETENTION_POLICY_EDIT)
    view = _caller(Permission.RETENTION_POLICY_VIEW)
    customised = _station()
    following = _station()
    stations.add(customised)
    stations.add(following)

    update_station_retention(
        edit,
        customised.id,
        mode=RetentionPolicyMode.CUSTOM,
        overrides=_custom_group(),
        expected_revision=0,
        policies=policies,
        records=records,
        stations=facts,
    )

    # 没有记录的第二台工位：跟随全局默认，且不会因为读取而建立记录。
    untouched = read_station_retention(
        view, following.id, policies=policies, overrides=records, stations=facts
    )
    assert untouched.revision == 0
    assert untouched.mode is RetentionPolicyMode.FOLLOW_GLOBAL
    assert untouched.effective == DEFAULT_RETENTION_POLICY
    assert records.get(following.id) is None

    changed = update_policy(edit, _changed_global(), expected_revision=0, repository=policies)

    after = read_station_retention(
        view, customised.id, policies=policies, overrides=records, stations=facts
    )
    assert after.default == changed.policy
    assert after.overrides == _custom_group()
    assert after.effective == _custom_group()
    assert records.get(customised.id) is not None
    assert records.get(customised.id).revision == 1  # type: ignore[union-attr]

    # 配置组合投影读的是同一个 owner 结论：生效组与记录版本一起给出。
    projection = RepositoryStationRetentionProjectionGateway(policies, records)
    customised_projection = projection.for_station(customised.id)
    follow_projection = projection.for_station(following.id)

    assert customised_projection.revision == 1
    assert customised_projection.effective.to_wire() == _custom_group().to_wire()
    assert follow_projection.revision == 0
    assert follow_projection.effective.to_wire() == _changed_global().to_wire()


def test_station_facts_do_not_expose_retention_configuration(session: Session) -> None:
    stations = PostgresStationRepository(session)
    facts = RepositoryDeviceStationFacts(stations=stations)
    station = _station()
    stations.add(station)

    assert facts.station_presence(station.id) is RetentionStationPresence.ACTIVE
    assert facts.station_presence(new_id()) is RetentionStationPresence.MISSING

    stations.save(
        replace(station, status=DeviceStatus.DEACTIVATED), expected_revision=station.revision
    )
    assert facts.station_presence(station.id) is RetentionStationPresence.DEACTIVATED


def test_concurrent_first_writes_and_replacements_have_one_winner(engine: Engine) -> None:
    station_id = new_id()
    contenders = (
        StationRetentionRecord(station_id, RetentionPolicyMode.FOLLOW_GLOBAL, None, 1),
        StationRetentionRecord(station_id, RetentionPolicyMode.CUSTOM, _custom_group(), 1),
    )

    def compete(expected_revision: int) -> list[StationRetentionRecord]:
        ready = Barrier(2)

        def write(candidate: StationRetentionRecord) -> tuple[bool, StationRetentionRecord]:
            with Session(engine) as opened, opened.begin():
                repository = PostgresStationRetentionOverrideRepository(opened)
                current = repository.get(station_id)
                assert (0 if current is None else current.revision) == expected_revision
                ready.wait(timeout=10)
                written = (
                    repository.insert_if_absent(candidate)
                    if expected_revision == 0
                    else repository.replace_if_current(
                        expected_revision=expected_revision, record=candidate
                    )
                )
                return written, candidate

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(write, replace(record, revision=expected_revision + 1))
                for record in contenders
            ]
            results = [future.result(timeout=30) for future in futures]
        return [record for written, record in results if written]

    try:
        for expected_revision in (0, 1):
            winners = compete(expected_revision)
            assert len(winners) == 1
            with Session(engine) as opened:
                assert (
                    PostgresStationRetentionOverrideRepository(opened).get(station_id) == winners[0]
                )
    finally:
        # 本场景必须提交两个真实事务；只清理本场景创建的唯一 owner 记录。
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM retention_station_policy WHERE station_id = :station_id"),
                {"station_id": station_id},
            )
