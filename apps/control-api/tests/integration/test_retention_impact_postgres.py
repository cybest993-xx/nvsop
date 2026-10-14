"""真实 PostgreSQL 验证 retention 变更影响估算的引用/计数 seam 与组合（#205）。

evidence 与 monitor 计数各经其 owner 的只读 seam，不跨表；估算经真实仓储与真实用例，
证明“受影响对象/空间/最早最晚 + 引用保护跳过”来自真实数据而不是固定数字替身。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.orm import Session

from factory_sop.auth.authorization import Caller
from factory_sop.auth.model import User, UserStatus
from factory_sop.auth.permissions import Permission
from factory_sop.evidence.adapters.repository import (
    PostgresEvidenceImpactRepository,
    PostgresEvidenceRepository,
)
from factory_sop.evidence.model import (
    EvidenceKind,
    EvidenceOrigin,
    EvidenceReference,
    EvidenceRegistration,
    EvidenceStatus,
)
from factory_sop.evidence.retention import RepositoryEvidenceImpact
from factory_sop.identifiers import new_id
from factory_sop.monitor.adapters.repository import (
    PostgresMonitorImpactRepository,
    PostgresMonitorRepository,
)
from factory_sop.monitor.model import (
    MirroredDecision,
    MirroredHealth,
    MirroredObservation,
    MirroredSopInstance,
)
from factory_sop.monitor.retention import RepositoryMonitorImpact
from factory_sop.retention.adapters.repository import (
    PostgresRetentionPolicyRepository,
    PostgresStationRetentionOverrideRepository,
)
from factory_sop.retention.api import (
    DEFAULT_RETENTION_POLICY,
    EDGE_LOCAL_USAGE_UNAVAILABLE,
    EdgeImpactResult,
    RetentionImpactField,
    RetentionMode,
    RetentionRule,
    estimate_global_policy_change,
)
from nvsop_contracts import (
    ReportBackendProvenance,
    ReportedDecision,
    ReportedDisposal,
    ReportedHealth,
    ReportedObservation,
    ReportedSopInstance,
    ReportEvidence,
)

HOST = "00000000-0000-4000-8000-000000000001"
NOW = datetime(2026, 10, 14, tzinfo=UTC)
_DAY = 24 * 60 * 60
# 缩短 evidence_pass 90d→30d 后，接收时刻处在此开区间内的引用是新暴露的对象。
BAND_FROM = NOW - timedelta(days=90)
BAND_TO = NOW - timedelta(days=30)


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


def _evidence(
    *,
    station_id: str,
    host_id: str = HOST,
    instance_id: int,
    violation_id: str | None,
    received_at: datetime,
    size: int | None,
) -> EvidenceReference:
    material = size is not None
    registration = EvidenceRegistration(
        evidence_id=f"evidence/{new_id()}",
        host_id=host_id,
        station_id=station_id,
        instance_id=instance_id,
        violation_id=violation_id,
        kind=EvidenceKind.CLIP,
        origin=EvidenceOrigin.AUTOMATIC,
        anchor=1.0,
        window_start=0.0,
        window_end=2.0,
        generation="original",
        sha256=("a" * 64) if material else None,
        size=size,
        reference="evidence/clip.mp4" if material else None,
    )
    status = EvidenceStatus.AVAILABLE if material else EvidenceStatus.PENDING
    return EvidenceReference(registration, status, None, received_at)


def _instance(
    station_id: str, *, instance_id: int, received_at: datetime, closed: bool, host_id: str = HOST
) -> MirroredSopInstance:
    report = ReportedSopInstance(
        event_id=f"instance/{host_id}/{instance_id}",
        trace_id=f"instance/{instance_id}",
        host_id=host_id,
        station_id=station_id,
        instance_id=instance_id,
        opened_at=1.0,
        closed_at=2.0 if closed else None,
        close_reason="closed_by_idle_timeout" if closed else None,
        open_boundary_signal="start",
        close_boundary_signal=None,
        template_version_id=None,
        template_sha256=None,
        backend_provenance=(ReportBackendProvenance(str(new_id()), ("model",)),),
        configuration_revision=1,
        configuration_sha256="b" * 64,
        reported_at="2026-10-01T00:00:00Z",
    )
    return MirroredSopInstance(report=report, received_at=received_at)


def _decision(
    station_id: str, *, instance_id: int, received_at: datetime, verdict: str, host_id: str = HOST
) -> MirroredDecision:
    reasons = () if verdict == "pass" else ("MISSED_STEP",)
    return MirroredDecision(
        report=ReportedDecision(
            event_id=f"decision/{new_id()}",
            trace_id="trace",
            host_id=host_id,
            station_id=station_id,
            backend_id=str(new_id()),
            instance_id=instance_id,
            verdict=verdict,
            reason_codes=reasons,
            violations=(),
            lifecycle="closed",
            evidence=ReportEvidence(None, None, None),
            template_version_id=None,
            template_sha256=None,
            model_ids=("model",),
            reported_at="2026-10-01T00:00:00Z",
        ),
        received_at=received_at,
    )


def _observation(
    station_id: str, *, instance_id: int, received_at: datetime, host_id: str = HOST
) -> MirroredObservation:
    return MirroredObservation(
        report=ReportedObservation(
            event_id=f"observation/{new_id()}",
            trace_id="trace",
            host_id=host_id,
            station_id=station_id,
            instance_id=instance_id,
            source="action",
            signal="(1) action",
            source_time=1.0,
            source_anchor=10.0,
            observed_at=2.0,
            template_version_id=None,
            template_sha256=None,
            backend=None,
            reported_at="2026-10-01T00:00:00Z",
        ),
        received_at=received_at,
    )


def _health(station_id: str, *, received_at: datetime) -> MirroredHealth:
    return MirroredHealth(
        report=ReportedHealth(
            event_id=f"health/{new_id()}",
            trace_id="trace",
            host_id=str(new_id()),
            station_id=station_id,
            stream_id="stream",
            status="delivering",
            reason_code=None,
            detail=None,
            occurred_at="2026-10-01T00:00:00Z",
            source_anchor=1.0,
            anchor_offset=0.0,
            reported_at="2026-10-01T00:00:00Z",
        ),
        received_at=received_at,
    )


def test_evidence_usage_counts_only_the_window_with_disjoint_reference_groups(
    session: Session,
) -> None:
    station = str(new_id())
    evidence = PostgresEvidenceRepository(session)
    in_band = NOW - timedelta(days=45)
    for instance, linked, received, size in (
        (1, None, in_band, 1000),
        (2, "violation-1", in_band, 2000),
        (3, None, in_band, None),
        (4, None, NOW - timedelta(days=200), 4000),
        (5, None, NOW - timedelta(days=5), 5000),
    ):
        evidence.insert(
            _evidence(
                station_id=station,
                instance_id=instance,
                violation_id=linked,
                received_at=received,
                size=size,
            )
        )
    session.flush()
    usage = RepositoryEvidenceImpact(
        repository=PostgresEvidenceImpactRepository(session)
    ).evidence_usage(station_id=UUID(station), exposed_from=BAND_FROM, exposed_to=BAND_TO)
    assert {row.instance_id for row in usage} == {1, 2, 3}
    assert sum(row.unlinked.objects for row in usage) == 2
    unlinked_bytes = 0
    linked_bytes = 0
    for row in usage:
        assert row.unlinked.known_bytes is not None
        assert row.linked.known_bytes is not None
        unlinked_bytes += row.unlinked.known_bytes
        linked_bytes += row.linked.known_bytes
    assert unlinked_bytes == 1000
    assert sum(row.unlinked.unknown_size_objects for row in usage) == 1
    assert sum(row.linked.objects for row in usage) == 1
    assert linked_bytes == 2000
    assert all(row.unlinked.earliest == in_band for row in usage if row.unlinked.objects)
    assert all(row.unlinked.latest == in_band for row in usage if row.unlinked.objects)
    assert all(str(row.host_id) == HOST for row in usage)


def test_monitor_usage_protects_unclosed_records_and_referenced_disposal(session: Session) -> None:
    station = str(new_id())
    monitor = PostgresMonitorRepository(session)
    received = NOW - timedelta(days=45)
    monitor.upsert_instance(_instance(station, instance_id=1, received_at=received, closed=True))
    monitor.upsert_instance(_instance(station, instance_id=2, received_at=received, closed=False))
    monitor.upsert_decision(_decision(station, instance_id=1, received_at=received, verdict="pass"))
    monitor.upsert_decision(_decision(station, instance_id=2, received_at=received, verdict="fail"))
    monitor.upsert_observation(_observation(station, instance_id=2, received_at=received))
    monitor.upsert_health(_health(station, received_at=received))
    monitor.upsert_disposal(
        ReportedDisposal(
            event_id=f"disposal/{new_id()}",
            host_id=HOST,
            station_id=station,
            instance_id=1,
            idempotency_key=str(new_id()),
            violation_ref="violation-1",
            action_kind="sound_light",
            actor="edge",
            source="local",
            result_kind="written",
            result_detail=None,
            result_at=1.0,
            attempts=1,
            reported_at="2026-10-01T00:00:00Z",
        ),
        received_at=received,
    )
    session.flush()
    gateway = RepositoryMonitorImpact(repository=PostgresMonitorImpactRepository(session))
    (records,) = gateway.record_usage(
        station_id=UUID(station), exposed_from=BAND_FROM, exposed_to=BAND_TO
    )
    (disposal,) = gateway.disposal_usage(
        station_id=UUID(station), exposed_from=BAND_FROM, exposed_to=BAND_TO
    )
    assert records.exposed.objects == 2  # 已结案判定 + 无实例引用的流健康
    assert records.protected.objects == 2  # 未结案判定/观测
    assert records.exposed.known_bytes is not None
    assert records.exposed.known_bytes > 0
    assert records.exposed.earliest == records.exposed.latest == received
    assert disposal.exposed.objects == 0
    assert disposal.protected.objects == 1  # 处置成功不解除锁存违规引用


def test_global_estimate_uses_real_categories_and_refuses_unknown_review_state(
    session: Session,
) -> None:
    station = str(new_id())
    received = NOW - timedelta(days=45)
    evidence = PostgresEvidenceRepository(session)
    monitor = PostgresMonitorRepository(session)
    for instance, verdict, linked, size in ((1, "pass", None, 1000), (2, "fail", "v-2", 2000)):
        evidence.insert(
            _evidence(
                station_id=station,
                instance_id=instance,
                violation_id=linked,
                received_at=received,
                size=size,
            )
        )
        monitor.upsert_instance(
            _instance(station, instance_id=instance, received_at=received, closed=True)
        )
        monitor.upsert_decision(
            _decision(station, instance_id=instance, received_at=received, verdict=verdict)
        )
    session.flush()
    proposed = replace(
        DEFAULT_RETENTION_POLICY,
        evidence_pass=RetentionRule(RetentionMode.DURATION, 30 * _DAY),
        evidence_fail=RetentionRule(RetentionMode.DURATION, 30 * _DAY),
    )
    policies = PostgresRetentionPolicyRepository(session)
    before = policies.get_global()
    estimate = estimate_global_policy_change(
        _caller(Permission.RETENTION_POLICY_EDIT),
        proposed,
        expected_revision=0,
        policies=policies,
        records=PostgresStationRetentionOverrideRepository(session),
        evidence_impact=RepositoryEvidenceImpact(
            repository=PostgresEvidenceImpactRepository(session)
        ),
        monitor_impact=RepositoryMonitorImpact(repository=PostgresMonitorImpactRepository(session)),
        now=NOW,
        edge_impact=FakeEdgeImpact(),
    )
    by_field = {value.field: value for value in estimate.fields}
    fail = by_field[RetentionImpactField.EVIDENCE_FAIL]
    assert fail.breakdown is not None
    assert fail.breakdown.exposed.objects == 0
    assert fail.breakdown.protected.objects == 1
    assert fail.breakdown.protected.known_bytes == 2000
    assert fail.breakdown.protected.earliest == fail.breakdown.protected.latest == received
    assert fail.exposed_from is None  # 永久 -> 有限
    assert fail.exposed_to == BAND_TO
    passing = by_field[RetentionImpactField.EVIDENCE_PASS]
    assert passing.exposed_from == BAND_FROM
    assert passing.exposed_to == BAND_TO
    assert passing.unavailable_reason == "evidence_review_state_unknown"
    assert passing.breakdown is None
    assert not estimate.complete
    assert estimate.incomplete_reasons == ("evidence_pass:evidence_review_state_unknown",)
    assert policies.get_global() == before  # 只读估算不写策略、不删除任何对象
    raw = RepositoryEvidenceImpact(
        repository=PostgresEvidenceImpactRepository(session)
    ).evidence_usage(station_id=UUID(station), exposed_from=BAND_FROM, exposed_to=BAND_TO)
    assert sum(row.unlinked.objects + row.linked.objects for row in raw) == 2


def test_evidence_usage_keeps_producing_host_in_the_instance_identity(session: Session) -> None:
    station = str(new_id())
    owner = PostgresEvidenceRepository(session)
    references = tuple(
        _evidence(
            station_id=station,
            host_id=str(new_id()),
            instance_id=1,
            violation_id=None,
            received_at=NOW - timedelta(days=45),
            size=100,
        )
        for _ in range(2)
    )
    for reference in references:
        owner.insert(reference)
    session.flush()
    usage = RepositoryEvidenceImpact(
        repository=PostgresEvidenceImpactRepository(session)
    ).evidence_usage(station_id=UUID(station), exposed_from=BAND_FROM, exposed_to=BAND_TO)
    assert len(usage) == 2
    assert {str(value.host_id) for value in usage} == {
        value.registration.host_id for value in references
    }
    assert all(value.unlinked.objects == 1 for value in usage)


def test_another_host_closure_does_not_unprotect_or_reclassify_a_record(session: Session) -> None:
    station = str(new_id())
    host_a, host_b = str(new_id()), str(new_id())
    owner = PostgresMonitorRepository(session)
    received = NOW - timedelta(days=45)
    owner.upsert_instance(
        _instance(station, host_id=host_a, instance_id=1, received_at=received, closed=True)
    )
    owner.upsert_decision(
        _decision(station, host_id=host_a, instance_id=1, received_at=received, verdict="fail")
    )
    owner.upsert_decision(
        _decision(station, host_id=host_b, instance_id=1, received_at=received, verdict="pass")
    )
    session.flush()
    gateway = RepositoryMonitorImpact(repository=PostgresMonitorImpactRepository(session))
    (usage,) = gateway.record_usage(
        station_id=UUID(station), exposed_from=BAND_FROM, exposed_to=BAND_TO
    )
    assert usage.exposed.objects == 1
    assert usage.protected.objects == 1
    facts = gateway.instance_facts(
        instance_ids=((UUID(host_a), UUID(station), 1), (UUID(host_b), UUID(station), 1))
    )
    by_host = {str(value.host_id): value for value in facts}
    assert by_host[host_a].closed is True
    assert by_host[host_a].verdict == "fail"
    assert by_host[host_b].closed is False
    assert by_host[host_b].verdict == "pass"
    assert all(value.active_review is None for value in facts)


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
