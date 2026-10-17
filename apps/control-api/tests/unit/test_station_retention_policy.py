"""S041 retention 侧工位保留覆盖：记录校验、整组解析、真实 CAS 与授权。"""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest
from auth_fakes import caller_holding
from device_fakes import FAKE_ACTOR, FAKE_NOW, FakeInferenceStations

from factory_sop.auth.authorization import AuthorizationRefusedError
from factory_sop.auth.permissions import Permission
from factory_sop.device.model import DeviceStatus, Station
from factory_sop.device.usecases.retention import RepositoryDeviceStationFacts
from factory_sop.identifiers import new_id
from factory_sop.retention.api import (
    DEFAULT_RETENTION_POLICY,
    RetentionMode,
    RetentionPolicy,
    RetentionPolicyConflictError,
    RetentionPolicyMode,
    RetentionPolicyState,
    RetentionRule,
    RetentionStationPresence,
    StationRetentionRecord,
    read_station_retention,
    update_station_retention,
)
from factory_sop.retention.errors import RetentionRefusalCode, RetentionRefusedError


def custom_group() -> RetentionPolicy:
    return replace(
        DEFAULT_RETENTION_POLICY,
        evidence_pass=RetentionRule(RetentionMode.DISCARD),
        evidence_indeterminate=RetentionRule(RetentionMode.DURATION, 3600),
    )


def follow_global_record(station_id: UUID, revision: int) -> StationRetentionRecord:
    return StationRetentionRecord(
        station_id=station_id,
        mode=RetentionPolicyMode.FOLLOW_GLOBAL,
        overrides=None,
        revision=revision,
    )


def custom_record(station_id: UUID, revision: int) -> StationRetentionRecord:
    return StationRetentionRecord(
        station_id=station_id,
        mode=RetentionPolicyMode.CUSTOM,
        overrides=custom_group(),
        revision=revision,
    )


def _station(station_id: UUID) -> Station:
    return Station(
        id=station_id,
        code=f"S-{station_id.hex}",
        name="工位",
        tags=(),
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=FAKE_ACTOR,
        updated_by=FAKE_ACTOR,
        created_at=FAKE_NOW,
        updated_at=FAKE_NOW,
    )


def facts_for(*station_ids: UUID) -> RepositoryDeviceStationFacts:
    store = FakeInferenceStations()
    for station_id in station_ids:
        store.add(_station(station_id))
    return RepositoryDeviceStationFacts(stations=store)


class FakePolicies:
    def __init__(self) -> None:
        self.state: RetentionPolicyState | None = None

    def get_global(self) -> RetentionPolicyState | None:
        return self.state

    def lock_current(self) -> RetentionPolicyState | None:
        return self.state

    def replace_if_current(self, *, expected_revision: int, value: RetentionPolicyState) -> bool:
        current = 0 if self.state is None else self.state.revision
        if current != expected_revision:
            return False
        self.state = value
        return True


class FakeOverrides:
    """内存覆盖记录，语义与 PostgreSQL 适配器一致：首写只在无记录时成立。"""

    def __init__(self) -> None:
        self.records: dict[UUID, StationRetentionRecord] = {}
        self.writes = 0

    def get(self, station_id: UUID) -> StationRetentionRecord | None:
        return self.records.get(station_id)

    def list_all(self) -> tuple[StationRetentionRecord, ...]:
        return tuple(self.records.values())

    def lock_current(self, station_id: UUID) -> StationRetentionRecord | None:
        return self.records.get(station_id)

    def insert_if_absent(self, record: StationRetentionRecord) -> bool:
        self.writes += 1
        if record.station_id in self.records:
            return False
        self.records[record.station_id] = record
        return True

    def replace_if_current(self, *, expected_revision: int, record: StationRetentionRecord) -> bool:
        self.writes += 1
        current = self.records.get(record.station_id)
        if current is None or current.revision != expected_revision:
            return False
        self.records[record.station_id] = record
        return True


def test_station_record_requires_a_paired_whole_group_and_a_positive_revision() -> None:
    station_id = new_id()
    with pytest.raises(ValueError, match="must not carry a custom retention group"):
        replace(custom_record(station_id, 1), mode=RetentionPolicyMode.FOLLOW_GLOBAL)
    with pytest.raises(ValueError, match="requires a complete override group"):
        replace(custom_record(station_id, 1), overrides=None)
    with pytest.raises(ValueError, match="revision must be a positive integer"):
        replace(custom_record(station_id, 1), revision=0)


def test_reading_a_station_without_a_record_follows_the_global_default() -> None:
    station_id = new_id()
    policies = FakePolicies()
    overrides = FakeOverrides()

    view = read_station_retention(
        caller_holding(Permission.RETENTION_POLICY_VIEW),
        station_id,
        policies=policies,
        overrides=overrides,
        stations=facts_for(station_id),
    )

    assert view.revision == 0
    assert view.mode is RetentionPolicyMode.FOLLOW_GLOBAL
    assert view.overrides is None
    assert view.default == DEFAULT_RETENTION_POLICY
    assert view.effective == DEFAULT_RETENTION_POLICY
    assert overrides.writes == 0


def test_usecases_authorize_and_reject_a_mismatched_selection_before_writing() -> None:
    station_id = new_id()
    policies = FakePolicies()
    overrides = FakeOverrides()
    facts = facts_for(station_id)

    with pytest.raises(AuthorizationRefusedError):
        read_station_retention(
            caller_holding(), station_id, policies=policies, overrides=overrides, stations=facts
        )

    edit = caller_holding(Permission.RETENTION_POLICY_EDIT)
    with pytest.raises(AuthorizationRefusedError):
        update_station_retention(
            caller_holding(Permission.RETENTION_POLICY_VIEW),
            station_id,
            mode=RetentionPolicyMode.CUSTOM,
            overrides=custom_group(),
            expected_revision=0,
            policies=policies,
            records=overrides,
            stations=facts,
        )
    assert overrides.writes == 0

    with pytest.raises(RetentionRefusedError, match="RETENTION_POLICY_INVALID"):
        update_station_retention(
            edit,
            station_id,
            mode=RetentionPolicyMode.FOLLOW_GLOBAL,
            overrides=custom_group(),
            expected_revision=0,
            policies=policies,
            records=overrides,
            stations=facts,
        )
    with pytest.raises(RetentionRefusedError, match="RETENTION_POLICY_INVALID"):
        update_station_retention(
            edit,
            station_id,
            mode=RetentionPolicyMode.CUSTOM,
            overrides=None,
            expected_revision=0,
            policies=policies,
            records=overrides,
            stations=facts,
        )
    assert overrides.writes == 0

    created = update_station_retention(
        edit,
        station_id,
        mode=RetentionPolicyMode.CUSTOM,
        overrides=custom_group(),
        expected_revision=0,
        policies=policies,
        records=overrides,
        stations=facts,
    )

    assert created.revision == 1
    assert created.mode is RetentionPolicyMode.CUSTOM
    assert created.overrides == custom_group()
    assert created.effective == custom_group()
    assert created.default == DEFAULT_RETENTION_POLICY

    # 第二次“首次建立”必须冲突，且不覆盖已建立的记录。
    with pytest.raises(RetentionPolicyConflictError):
        update_station_retention(
            edit,
            station_id,
            mode=RetentionPolicyMode.FOLLOW_GLOBAL,
            overrides=None,
            expected_revision=0,
            policies=policies,
            records=overrides,
            stations=facts,
        )
    assert overrides.writes == 2
    assert overrides.get(station_id) == custom_record(station_id, 1)

    updated = update_station_retention(
        edit,
        station_id,
        mode=RetentionPolicyMode.FOLLOW_GLOBAL,
        overrides=None,
        expected_revision=1,
        policies=policies,
        records=overrides,
        stations=facts,
    )
    assert updated.revision == 2
    assert updated.mode is RetentionPolicyMode.FOLLOW_GLOBAL
    assert updated.overrides is None
    assert updated.effective == DEFAULT_RETENTION_POLICY
    assert overrides.get(station_id) == follow_global_record(station_id, 2)

    # 条件更新防丢失：同一个旧版本再写一次被拒。
    with pytest.raises(RetentionPolicyConflictError):
        update_station_retention(
            edit,
            station_id,
            mode=RetentionPolicyMode.CUSTOM,
            overrides=custom_group(),
            expected_revision=1,
            policies=policies,
            records=overrides,
            stations=facts,
        )
    assert overrides.get(station_id) == follow_global_record(station_id, 2)


def test_a_custom_override_keeps_its_own_group_when_the_global_default_changes() -> None:
    station_id = new_id()
    policies = FakePolicies()
    overrides = FakeOverrides()
    facts = facts_for(station_id)
    edit = caller_holding(Permission.RETENTION_POLICY_EDIT)
    view_holder = caller_holding(Permission.RETENTION_POLICY_VIEW)

    update_station_retention(
        edit,
        station_id,
        mode=RetentionPolicyMode.CUSTOM,
        overrides=custom_group(),
        expected_revision=0,
        policies=policies,
        records=overrides,
        stations=facts,
    )
    changed_default = replace(
        DEFAULT_RETENTION_POLICY, disposal=RetentionRule(RetentionMode.DURATION, 3600)
    )
    policies.state = RetentionPolicyState(changed_default, 1)

    reread = read_station_retention(
        view_holder, station_id, policies=policies, overrides=overrides, stations=facts
    )

    assert reread.default == changed_default
    assert reread.overrides == custom_group()
    assert reread.effective == custom_group()
    assert reread.default != reread.effective


def test_station_facts_gate_missing_stations_on_read_and_write() -> None:
    policies = FakePolicies()
    overrides = FakeOverrides()
    edit = caller_holding(Permission.RETENTION_POLICY_EDIT)
    view_holder = caller_holding(Permission.RETENTION_POLICY_VIEW)
    facts = facts_for()

    with pytest.raises(RetentionRefusedError) as missing:
        read_station_retention(
            view_holder, new_id(), policies=policies, overrides=overrides, stations=facts
        )
    assert missing.value.code is RetentionRefusalCode.STATION_NOT_FOUND

    with pytest.raises(RetentionRefusedError) as absent:
        update_station_retention(
            edit,
            new_id(),
            mode=RetentionPolicyMode.FOLLOW_GLOBAL,
            overrides=None,
            expected_revision=0,
            policies=policies,
            records=overrides,
            stations=facts,
        )
    assert absent.value.code is RetentionRefusalCode.STATION_NOT_FOUND
    assert overrides.writes == 0


def test_device_station_facts_reports_missing_active_and_deactivated() -> None:
    station_id = new_id()
    store = FakeInferenceStations()
    store.add(_station(station_id))
    gateway = RepositoryDeviceStationFacts(stations=store)

    assert gateway.station_presence(station_id) is RetentionStationPresence.ACTIVE
    assert gateway.station_presence(new_id()) is RetentionStationPresence.MISSING

    store.save(replace(_station(station_id), status=DeviceStatus.DEACTIVATED), expected_revision=1)

    assert gateway.station_presence(station_id) is RetentionStationPresence.DEACTIVATED
