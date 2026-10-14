"""#205 retention 变更影响估算：逐字段/逐类别、范围与不完整拒绝。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from auth_fakes import caller_holding
from device_fakes import FAKE_ACTOR, FAKE_NOW, FakeInferenceStations

from factory_sop.auth.api import Caller
from factory_sop.auth.authorization import AuthorizationRefusedError
from factory_sop.auth.permissions import Permission
from factory_sop.device.model import DeviceStatus, Station
from factory_sop.device.usecases.retention import RepositoryDeviceStationFacts
from factory_sop.identifiers import new_id
from factory_sop.retention.api import (
    DEFAULT_RETENTION_POLICY,
    EDGE_LOCAL_USAGE_UNAVAILABLE,
    EVIDENCE_BYTES_UNKNOWN,
    INSTANCE_CATEGORY_UNKNOWN,
    EdgeImpactResult,
    EvidenceInstanceUsage,
    ImpactAggregate,
    InstanceRetentionFact,
    MonitorImpactGateway,
    RetentionImpactField,
    RetentionPolicy,
    RetentionPolicyMode,
    RetentionPolicyState,
    RetentionRule,
    StationRetentionRecord,
    StationUsage,
    estimate_global_policy_change,
    estimate_station_policy_change,
    impact_digest,
)
from factory_sop.retention.model import RetentionMode

HOST = UUID("00000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 10, 14, tzinfo=UTC)
_DAY = 24 * 60 * 60


def aggregate(
    *,
    objects: int = 0,
    known_bytes: int | None = 0,
    unknown: int = 0,
    earliest: datetime | None = None,
    latest: datetime | None = None,
) -> ImpactAggregate:
    return ImpactAggregate(
        objects=objects,
        known_bytes=known_bytes,
        unknown_size_objects=unknown,
        earliest=earliest,
        latest=latest,
    )


class FakeEdgeImpact:
    def reported_impact(
        self,
        *,
        station_id: UUID | None,
        excluded_stations: frozenset[UUID],
        baseline_seconds: int | None,
        proposed_seconds: int,
        reference_time: datetime,
    ) -> EdgeImpactResult:
        return EdgeImpactResult(
            breakdown=None,
            unavailable_reason=EDGE_LOCAL_USAGE_UNAVAILABLE,
            sources=(),
        )


class FakePolicies:
    def __init__(self, state: RetentionPolicyState | None = None) -> None:
        self.state = state
        self.writes = 0

    def get_global(self) -> RetentionPolicyState | None:
        return self.state

    def replace_if_current(self, *, expected_revision: int, value: RetentionPolicyState) -> bool:
        self.writes += 1
        current = 0 if self.state is None else self.state.revision
        if current != expected_revision:
            return False
        self.state = value
        return True


class FakeOverrides:
    def __init__(self) -> None:
        self.records: dict[UUID, StationRetentionRecord] = {}
        self.writes = 0

    def get(self, station_id: UUID) -> StationRetentionRecord | None:
        return self.records.get(station_id)

    def list_all(self) -> tuple[StationRetentionRecord, ...]:
        return tuple(self.records.values())

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


class FakeEvidenceImpact:
    """按外部注入的逐实例用法回答；记录调用以便断言 owner 确实被查询。"""

    def __init__(self, usage: tuple[EvidenceInstanceUsage, ...] = ()) -> None:
        self.calls: list[tuple[UUID | None, datetime | None, datetime]] = []
        self._usage = usage

    def evidence_usage(
        self, *, station_id: UUID | None, exposed_from: datetime | None, exposed_to: datetime
    ) -> tuple[EvidenceInstanceUsage, ...]:
        self.calls.append((station_id, exposed_from, exposed_to))
        return self._usage


class FakeMonitorImpact(MonitorImpactGateway):
    def __init__(
        self,
        *,
        facts: tuple[InstanceRetentionFact, ...] = (),
        records: tuple[StationUsage, ...] = (),
        disposals: tuple[StationUsage, ...] = (),
    ) -> None:
        self._facts = facts
        self._records = records
        self._disposals = disposals
        self.fact_calls: list[tuple[tuple[UUID, UUID, int], ...]] = []
        self.record_calls: list[tuple[UUID | None, datetime | None, datetime]] = []
        self.disposal_calls: list[tuple[UUID | None, datetime | None, datetime]] = []
        self.fail_records = False

    def instance_facts(
        self, *, instance_ids: tuple[tuple[UUID, UUID, int], ...]
    ) -> tuple[InstanceRetentionFact, ...]:
        self.fact_calls.append(instance_ids)
        return self._facts

    def record_usage(
        self, *, station_id: UUID | None, exposed_from: datetime | None, exposed_to: datetime
    ) -> tuple[StationUsage, ...]:
        if self.fail_records:
            raise RuntimeError("monitor record usage failed")
        self.record_calls.append((station_id, exposed_from, exposed_to))
        return self._records

    def disposal_usage(
        self, *, station_id: UUID | None, exposed_from: datetime | None, exposed_to: datetime
    ) -> tuple[StationUsage, ...]:
        self.disposal_calls.append((station_id, exposed_from, exposed_to))
        return self._disposals


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


def usage(
    station_id: UUID, instance_id: int, *, unlinked: ImpactAggregate, linked: ImpactAggregate
) -> EvidenceInstanceUsage:
    return EvidenceInstanceUsage(
        host_id=HOST,
        station_id=station_id,
        instance_id=instance_id,
        unlinked=unlinked,
        linked=linked,
    )


def fact(
    station_id: UUID,
    instance_id: int,
    *,
    verdict: str | None,
    latched: bool = False,
    closed: bool = True,
    active_review: bool | None = False,
) -> InstanceRetentionFact:
    return InstanceRetentionFact(
        host_id=HOST,
        station_id=station_id,
        instance_id=instance_id,
        verdict=verdict,
        has_latched_violation=latched,
        closed=closed,
        active_review=active_review,
    )


def edit() -> Caller:
    return caller_holding(Permission.RETENTION_POLICY_EDIT)


def test_shortening_a_non_minimum_evidence_class_still_queries_the_evidence_owner() -> None:
    # pass/indeterminate 保持 90 天，fail 由永久缩到 120 天：整类必须被单独估算，不能被跳过。
    evidence = FakeEvidenceImpact()
    proposed = replace(
        DEFAULT_RETENTION_POLICY,
        evidence_fail=RetentionRule(RetentionMode.DURATION, 120 * _DAY),
    )

    estimate = estimate_global_policy_change(
        edit(),
        proposed,
        expected_revision=0,
        policies=FakePolicies(),
        records=FakeOverrides(),
        evidence_impact=evidence,
        monitor_impact=FakeMonitorImpact(),
        now=NOW,
        edge_impact=FakeEdgeImpact(),
    )

    assert evidence.calls, (
        "permanent -> 120 days was silently omitted; evidence owner was never queried"
    )
    fields = {value.field for value in estimate.fields}
    assert fields == {RetentionImpactField.EVIDENCE_FAIL}
    result = estimate.fields[0]
    assert result.category is not None
    assert result.category.value == "fail"
    assert result.exposed_from is None  # 基准永久：下界为空
    assert result.exposed_to == NOW - timedelta(days=120)


def test_evidence_categories_are_counted_separately_with_latched_protection() -> None:
    station = new_id()
    evidence = FakeEvidenceImpact(
        (
            usage(
                station,
                1,
                unlinked=aggregate(objects=2, known_bytes=100, earliest=NOW),
                linked=aggregate(),
            ),
            usage(
                station,
                2,
                unlinked=aggregate(),
                linked=aggregate(objects=1, known_bytes=500, earliest=NOW),
            ),
        )
    )
    monitor = FakeMonitorImpact(
        facts=(
            fact(station, 1, verdict="pass", closed=False),
            fact(station, 2, verdict="fail", latched=True),
        )
    )
    proposed = replace(
        DEFAULT_RETENTION_POLICY,
        evidence_pass=RetentionRule(RetentionMode.DURATION, 10 * _DAY),
        evidence_fail=RetentionRule(RetentionMode.DURATION, 10 * _DAY),
    )

    estimate = estimate_global_policy_change(
        edit(),
        proposed,
        expected_revision=0,
        policies=FakePolicies(),
        records=FakeOverrides(),
        evidence_impact=evidence,
        monitor_impact=monitor,
        now=NOW,
        edge_impact=FakeEdgeImpact(),
    )

    by_field = {value.field: value for value in estimate.fields}
    pass_impact = by_field[RetentionImpactField.EVIDENCE_PASS]
    assert pass_impact.breakdown is not None
    assert pass_impact.breakdown.exposed.objects == 0
    # 实例未结案：通过类引用也继续受保护，不因“通过”就放行。
    assert pass_impact.breakdown.protected.objects == 2
    fail_impact = by_field[RetentionImpactField.EVIDENCE_FAIL]
    assert fail_impact.breakdown is not None
    assert fail_impact.breakdown.exposed.objects == 0
    assert fail_impact.breakdown.protected.objects == 1
    assert estimate.complete
    # 只为窗口内出现的实例取分类事实。
    assert monitor.fact_calls
    assert monitor.fact_calls[0]


def test_unknown_instance_category_makes_the_estimate_incomplete() -> None:
    station = new_id()
    evidence = FakeEvidenceImpact(
        (usage(station, 9, unlinked=aggregate(objects=1, known_bytes=10), linked=aggregate()),)
    )
    proposed = replace(
        DEFAULT_RETENTION_POLICY,
        evidence_pass=RetentionRule(RetentionMode.DURATION, 10 * _DAY),
    )

    estimate = estimate_global_policy_change(
        edit(),
        proposed,
        expected_revision=0,
        policies=FakePolicies(),
        records=FakeOverrides(),
        evidence_impact=evidence,
        monitor_impact=FakeMonitorImpact(facts=()),
        now=NOW,
        edge_impact=FakeEdgeImpact(),
    )

    assert not estimate.complete
    assert estimate.incomplete_reasons == (f"evidence_pass:{INSTANCE_CATEGORY_UNKNOWN}",)
    assert estimate.fields[0].breakdown is None


def test_unknown_evidence_bytes_makes_the_estimate_incomplete() -> None:
    station = new_id()
    evidence = FakeEvidenceImpact(
        (
            usage(
                station,
                1,
                unlinked=aggregate(objects=1, known_bytes=0, unknown=1),
                linked=aggregate(),
            ),
        )
    )
    proposed = replace(
        DEFAULT_RETENTION_POLICY,
        evidence_pass=RetentionRule(RetentionMode.DURATION, 10 * _DAY),
    )

    estimate = estimate_global_policy_change(
        edit(),
        proposed,
        expected_revision=0,
        policies=FakePolicies(),
        records=FakeOverrides(),
        evidence_impact=evidence,
        monitor_impact=FakeMonitorImpact(facts=(fact(station, 1, verdict="pass"),)),
        now=NOW,
        edge_impact=FakeEdgeImpact(),
    )

    assert estimate.incomplete_reasons == (f"evidence_pass:{EVIDENCE_BYTES_UNKNOWN}",)


def test_record_compression_and_aggregate_have_measurable_impact_with_unknown_savings() -> None:
    station = new_id()
    monitor = FakeMonitorImpact(
        records=(
            StationUsage(
                station_id=station,
                exposed=aggregate(objects=5, known_bytes=900, earliest=NOW),
                protected=aggregate(objects=1, known_bytes=100),
            ),
        )
    )
    proposed = replace(
        DEFAULT_RETENTION_POLICY,
        record_compression_age=RetentionRule(RetentionMode.DURATION, _DAY),
        record_aggregate_age=RetentionRule(RetentionMode.DURATION, 30 * _DAY),
    )

    estimate = estimate_global_policy_change(
        edit(),
        proposed,
        expected_revision=0,
        policies=FakePolicies(),
        records=FakeOverrides(),
        evidence_impact=FakeEvidenceImpact(),
        monitor_impact=monitor,
        now=NOW,
        edge_impact=FakeEdgeImpact(),
    )

    by_field = {value.field: value for value in estimate.fields}
    assert set(by_field) == {
        RetentionImpactField.RECORD_COMPRESSION_AGE,
        RetentionImpactField.RECORD_AGGREGATE_AGE,
    }
    compression = by_field[RetentionImpactField.RECORD_COMPRESSION_AGE]
    assert compression.removes_objects is False
    assert compression.savings_unknown is True
    assert compression.breakdown is not None
    assert compression.breakdown.exposed.objects == 5
    assert compression.breakdown.exposed.known_bytes == 900
    assert compression.breakdown.protected.objects == 1
    assert estimate.complete


def test_disposal_usage_is_queried_for_the_disposal_field() -> None:
    station = new_id()
    monitor = FakeMonitorImpact(
        disposals=(
            StationUsage(
                station_id=station,
                exposed=aggregate(objects=3, known_bytes=300),
                protected=aggregate(objects=2, known_bytes=200),
            ),
        )
    )
    proposed = replace(
        DEFAULT_RETENTION_POLICY,
        disposal=RetentionRule(RetentionMode.DURATION, 30 * _DAY),
    )

    estimate = estimate_global_policy_change(
        edit(),
        proposed,
        expected_revision=0,
        policies=FakePolicies(),
        records=FakeOverrides(),
        evidence_impact=FakeEvidenceImpact(),
        monitor_impact=monitor,
        now=NOW,
        edge_impact=FakeEdgeImpact(),
    )

    assert monitor.disposal_calls == [(None, None, NOW - timedelta(days=30))]
    assert monitor.record_calls == []
    impact = estimate.fields[0]
    assert impact.field is RetentionImpactField.DISPOSAL
    assert impact.breakdown is not None
    assert impact.breakdown.exposed.objects == 3
    assert impact.breakdown.protected.objects == 2


def test_local_reported_is_unavailable_and_blocks_confirmation() -> None:
    proposed = replace(
        DEFAULT_RETENTION_POLICY,
        local_reported=RetentionRule(RetentionMode.DURATION, _DAY),
    )

    estimate = estimate_global_policy_change(
        edit(),
        proposed,
        expected_revision=0,
        policies=FakePolicies(),
        records=FakeOverrides(),
        evidence_impact=FakeEvidenceImpact(),
        monitor_impact=FakeMonitorImpact(),
        now=NOW,
        edge_impact=FakeEdgeImpact(),
    )

    assert not estimate.complete
    assert estimate.incomplete_reasons == (f"local_reported:{EDGE_LOCAL_USAGE_UNAVAILABLE}",)
    assert estimate.fields[0].breakdown is None


def test_a_global_change_excludes_stations_with_a_custom_group() -> None:
    following = new_id()
    customised = new_id()
    records = FakeOverrides()
    records.records[customised] = StationRetentionRecord(
        station_id=customised,
        mode=RetentionPolicyMode.CUSTOM,
        overrides=DEFAULT_RETENTION_POLICY,
        revision=1,
    )
    evidence = FakeEvidenceImpact(
        (
            usage(
                following,
                1,
                unlinked=aggregate(objects=1, known_bytes=10),
                linked=aggregate(),
            ),
            usage(
                customised,
                1,
                unlinked=aggregate(objects=1, known_bytes=20),
                linked=aggregate(),
            ),
        )
    )
    monitor = FakeMonitorImpact(
        facts=(
            fact(following, 1, verdict="pass"),
            fact(customised, 1, verdict="pass"),
        ),
        records=(
            StationUsage(
                station_id=following,
                exposed=aggregate(objects=1, known_bytes=10),
                protected=aggregate(),
            ),
            StationUsage(
                station_id=customised,
                exposed=aggregate(objects=1, known_bytes=20),
                protected=aggregate(),
            ),
        ),
    )
    proposed = replace(
        DEFAULT_RETENTION_POLICY,
        evidence_pass=RetentionRule(RetentionMode.DURATION, 10 * _DAY),
        record_detail=RetentionRule(RetentionMode.DURATION, 40 * _DAY),
    )

    estimate = estimate_global_policy_change(
        edit(),
        proposed,
        expected_revision=0,
        policies=FakePolicies(),
        records=records,
        evidence_impact=evidence,
        monitor_impact=monitor,
        now=NOW,
        edge_impact=FakeEdgeImpact(),
    )

    by_field = {value.field: value for value in estimate.fields}
    evidence_impact = by_field[RetentionImpactField.EVIDENCE_PASS]
    assert evidence_impact.breakdown is not None
    # 自定义工位的对象不算进全局影响。
    assert evidence_impact.breakdown.exposed.objects == 1
    assert evidence_impact.breakdown.exposed.known_bytes == 10
    records_impact = by_field[RetentionImpactField.RECORD_DETAIL]
    assert records_impact.breakdown is not None
    assert records_impact.breakdown.exposed.objects == 1


def test_estimate_requires_edit_permission_and_a_valid_revision() -> None:
    with pytest.raises(AuthorizationRefusedError):
        estimate_global_policy_change(
            caller_holding(Permission.RETENTION_POLICY_VIEW),
            replace(
                DEFAULT_RETENTION_POLICY,
                record_detail=RetentionRule(RetentionMode.DURATION, 10 * _DAY),
            ),
            expected_revision=0,
            policies=FakePolicies(),
            records=FakeOverrides(),
            evidence_impact=FakeEvidenceImpact(),
            monitor_impact=FakeMonitorImpact(),
            now=NOW,
            edge_impact=FakeEdgeImpact(),
        )

    with pytest.raises(ValueError, match="expected_revision"):
        estimate_global_policy_change(
            edit(),
            replace(
                DEFAULT_RETENTION_POLICY,
                record_detail=RetentionRule(RetentionMode.DURATION, 10 * _DAY),
            ),
            expected_revision=-1,
            policies=FakePolicies(),
            records=FakeOverrides(),
            evidence_impact=FakeEvidenceImpact(),
            monitor_impact=FakeMonitorImpact(),
            now=NOW,
            edge_impact=FakeEdgeImpact(),
        )


def test_an_owner_failure_propagates_instead_of_zero_impact() -> None:
    monitor = FakeMonitorImpact()
    monitor.fail_records = True
    proposed = replace(
        DEFAULT_RETENTION_POLICY,
        record_detail=RetentionRule(RetentionMode.DURATION, 40 * _DAY),
    )

    with pytest.raises(RuntimeError, match="monitor record usage failed"):
        estimate_global_policy_change(
            edit(),
            proposed,
            expected_revision=0,
            policies=FakePolicies(),
            records=FakeOverrides(),
            evidence_impact=FakeEvidenceImpact(),
            monitor_impact=monitor,
            now=NOW,
            edge_impact=FakeEdgeImpact(),
        )


def test_a_field_without_a_shortening_is_not_queried() -> None:
    evidence = FakeEvidenceImpact()
    monitor = FakeMonitorImpact()
    longer = replace(
        DEFAULT_RETENTION_POLICY,
        evidence_pass=RetentionRule(RetentionMode.DURATION, 200 * _DAY),
    )

    estimate = estimate_global_policy_change(
        edit(),
        longer,
        expected_revision=0,
        policies=FakePolicies(),
        records=FakeOverrides(),
        evidence_impact=evidence,
        monitor_impact=monitor,
        now=NOW,
        edge_impact=FakeEdgeImpact(),
    )

    assert estimate.fields == ()
    assert estimate.complete
    assert evidence.calls == []
    assert monitor.record_calls == []


def test_impact_digest_binds_scope_revision_target_and_counts() -> None:
    station = new_id()
    monitor = FakeMonitorImpact(
        records=(
            StationUsage(
                station_id=station,
                exposed=aggregate(objects=1, known_bytes=10),
                protected=aggregate(),
            ),
        )
    )
    proposed = replace(
        DEFAULT_RETENTION_POLICY,
        record_detail=RetentionRule(RetentionMode.DURATION, 40 * _DAY),
    )

    def run(*, revision: int, target: RetentionPolicy, records: FakeOverrides) -> str:
        estimate = estimate_global_policy_change(
            edit(),
            target,
            expected_revision=revision,
            policies=FakePolicies(),
            records=records,
            evidence_impact=FakeEvidenceImpact(),
            monitor_impact=monitor,
            now=NOW,
            edge_impact=FakeEdgeImpact(),
        )
        return impact_digest(estimate)

    base = run(revision=0, target=proposed, records=FakeOverrides())
    assert base == run(revision=0, target=proposed, records=FakeOverrides())
    assert base != run(revision=7, target=proposed, records=FakeOverrides())
    changed = replace(proposed, record_detail=RetentionRule(RetentionMode.DURATION, 30 * _DAY))
    assert base != run(revision=0, target=changed, records=FakeOverrides())

    # 范围也进摘要：同一目标在“工位范围”下的估算与全局不同。
    station_estimate = estimate_station_policy_change(
        edit(),
        station,
        mode=RetentionPolicyMode.CUSTOM,
        overrides=proposed,
        expected_revision=0,
        policies=FakePolicies(),
        records=FakeOverrides(),
        stations=facts_for(station),
        evidence_impact=FakeEvidenceImpact(),
        monitor_impact=monitor,
        now=NOW,
        edge_impact=FakeEdgeImpact(),
    )
    assert impact_digest(station_estimate) != base


def test_station_estimate_uses_the_station_effective_group_as_baseline() -> None:
    station_id = new_id()
    policies = FakePolicies()
    records = FakeOverrides()
    custom = replace(
        DEFAULT_RETENTION_POLICY,
        record_detail=RetentionRule(RetentionMode.DURATION, 60 * _DAY),
    )
    records.records[station_id] = StationRetentionRecord(
        station_id=station_id,
        mode=RetentionPolicyMode.CUSTOM,
        overrides=custom,
        revision=3,
    )
    monitor = FakeMonitorImpact(
        records=(
            StationUsage(
                station_id=station_id,
                exposed=aggregate(objects=2, known_bytes=20),
                protected=aggregate(),
            ),
        )
    )
    proposed = replace(custom, record_detail=RetentionRule(RetentionMode.DURATION, 10 * _DAY))

    estimate = estimate_station_policy_change(
        edit(),
        station_id,
        mode=RetentionPolicyMode.CUSTOM,
        overrides=proposed,
        expected_revision=3,
        policies=policies,
        records=records,
        stations=facts_for(station_id),
        evidence_impact=FakeEvidenceImpact(),
        monitor_impact=monitor,
        now=NOW,
        edge_impact=FakeEdgeImpact(),
    )

    assert estimate.baseline_revision == 3
    assert estimate.station_id == station_id
    assert monitor.record_calls == [
        (station_id, NOW - timedelta(days=60), NOW - timedelta(days=10))
    ]
    assert records.writes == 0


def test_impact_aggregate_rejects_impossible_counts() -> None:
    with pytest.raises(ValueError, match="must not exceed"):
        ImpactAggregate(
            objects=1, known_bytes=0, unknown_size_objects=2, earliest=None, latest=None
        )
    with pytest.raises(ValueError, match="non-negative integer"):
        ImpactAggregate(
            objects=-1, known_bytes=0, unknown_size_objects=0, earliest=None, latest=None
        )


def test_closed_instance_without_review_fact_is_not_assumed_unprotected() -> None:
    station = new_id()
    evidence = FakeEvidenceImpact(
        (usage(station, 1, unlinked=aggregate(objects=1, known_bytes=10), linked=aggregate()),)
    )
    monitor = FakeMonitorImpact(
        facts=(
            InstanceRetentionFact(
                host_id=HOST,
                station_id=station,
                instance_id=1,
                verdict="pass",
                has_latched_violation=False,
                closed=True,
            ),
        )
    )
    proposed = replace(
        DEFAULT_RETENTION_POLICY, evidence_pass=RetentionRule(RetentionMode.DURATION, 10 * _DAY)
    )
    estimate = estimate_global_policy_change(
        edit(),
        proposed,
        expected_revision=0,
        policies=FakePolicies(),
        records=FakeOverrides(),
        evidence_impact=evidence,
        monitor_impact=monitor,
        now=NOW,
        edge_impact=FakeEdgeImpact(),
    )
    assert not estimate.complete
    assert estimate.incomplete_reasons == ("evidence_pass:evidence_review_state_unknown",)


def test_empty_estimate_digest_still_binds_baseline_and_global_scope() -> None:
    target = replace(
        DEFAULT_RETENTION_POLICY, evidence_pass=RetentionRule(RetentionMode.DURATION, 200 * _DAY)
    )

    def run(policies: FakePolicies, records: FakeOverrides) -> str:
        return impact_digest(
            estimate_global_policy_change(
                edit(),
                target,
                expected_revision=0,
                policies=policies,
                records=records,
                evidence_impact=FakeEvidenceImpact(),
                monitor_impact=FakeMonitorImpact(),
                now=NOW,
                edge_impact=FakeEdgeImpact(),
            )
        )

    first = run(FakePolicies(), FakeOverrides())
    altered = replace(
        DEFAULT_RETENTION_POLICY, evidence_pass=RetentionRule(RetentionMode.DURATION, 60 * _DAY)
    )
    assert first != run(FakePolicies(RetentionPolicyState(altered, 1)), FakeOverrides())
    scope = FakeOverrides()
    station = new_id()
    scope.records[station] = StationRetentionRecord(
        station, RetentionPolicyMode.CUSTOM, DEFAULT_RETENTION_POLICY, 1
    )
    assert first != run(FakePolicies(), scope)


def test_empty_station_estimate_digest_binds_selection_mode_and_default_revision() -> None:
    station = new_id()

    def run(mode: RetentionPolicyMode, policies: FakePolicies) -> str:
        return impact_digest(
            estimate_station_policy_change(
                edit(),
                station,
                mode=mode,
                overrides=DEFAULT_RETENTION_POLICY if mode is RetentionPolicyMode.CUSTOM else None,
                expected_revision=0,
                policies=policies,
                records=FakeOverrides(),
                stations=facts_for(station),
                evidence_impact=FakeEvidenceImpact(),
                monitor_impact=FakeMonitorImpact(),
                now=NOW,
                edge_impact=FakeEdgeImpact(),
            )
        )

    first = run(RetentionPolicyMode.FOLLOW_GLOBAL, FakePolicies())
    assert first != run(RetentionPolicyMode.CUSTOM, FakePolicies())
    assert first != run(
        RetentionPolicyMode.FOLLOW_GLOBAL,
        FakePolicies(RetentionPolicyState(DEFAULT_RETENTION_POLICY, 1)),
    )


def test_local_reported_uses_edge_owner_and_binds_sources_even_when_empty() -> None:
    from factory_sop.retention.api import (
        EdgeImpactResult,
        EdgeImpactSource,
        RetentionImpactBreakdown,
    )

    excluded = UUID("00000000-0000-4000-8000-000000000002")
    records = FakeOverrides()
    records.records[excluded] = StationRetentionRecord(
        station_id=excluded,
        mode=RetentionPolicyMode.CUSTOM,
        overrides=DEFAULT_RETENTION_POLICY,
        revision=1,
    )
    proposed = replace(
        DEFAULT_RETENTION_POLICY, local_reported=RetentionRule(RetentionMode.DURATION, _DAY)
    )
    calls: list[dict[str, object]] = []

    class Edge:
        revision = 7

        def reported_impact(
            self,
            *,
            station_id: UUID | None,
            excluded_stations: frozenset[UUID],
            baseline_seconds: int | None,
            proposed_seconds: int,
            reference_time: datetime,
        ) -> EdgeImpactResult:
            calls.append(
                {
                    "station_id": station_id,
                    "excluded": excluded_stations,
                    "baseline": baseline_seconds,
                    "proposed": proposed_seconds,
                    "reference": reference_time,
                }
            )
            return EdgeImpactResult(
                breakdown=RetentionImpactBreakdown(exposed=aggregate(), protected=aggregate()),
                unavailable_reason=None,
                sources=(EdgeImpactSource(HOST, self.revision, "a" * 64),),
            )

    edge = Edge()
    policies = FakePolicies()
    first = estimate_global_policy_change(
        caller_holding(Permission.RETENTION_POLICY_EDIT),
        proposed,
        expected_revision=0,
        policies=policies,
        records=records,
        evidence_impact=FakeEvidenceImpact(),
        monitor_impact=FakeMonitorImpact(),
        edge_impact=edge,
        now=NOW,
    )
    assert first.complete
    assert len(first.fields) == 1
    assert first.fields[0].field is RetentionImpactField.LOCAL_REPORTED
    assert first.fields[0].bytes_basis == "serialized_rows"
    assert calls == [
        {
            "station_id": None,
            "excluded": frozenset({excluded}),
            "baseline": DEFAULT_RETENTION_POLICY.local_reported.seconds,
            "proposed": _DAY,
            "reference": NOW,
        }
    ]
    edge.revision += 1
    second = estimate_global_policy_change(
        caller_holding(Permission.RETENTION_POLICY_EDIT),
        proposed,
        expected_revision=0,
        policies=policies,
        records=records,
        evidence_impact=FakeEvidenceImpact(),
        monitor_impact=FakeMonitorImpact(),
        edge_impact=edge,
        now=NOW,
    )
    assert impact_digest(first) != impact_digest(second)
    assert policies.writes == 0
    assert records.writes == 0
