"""#206 Center 引用保护：真实 PostgreSQL + 主机签名 HTTP 的删除资格、认领与反馈边界。

这里证明的是真实边界：容器里的 PostgreSQL、真实 FastAPI 应用与真实主机签名、真实 owner 仓储
seam（evidence 的引用/复核/再切片/播放/认领、monitor 的闭合与锁存违规、device 的当前配置身份、
retention 自己的策略行）。判定规则的穷举在 ``tests/unit/test_retention_evidence_deletion``。

不声称的边界：这里不删除任何媒体字节（媒体由来源推理机持有，ADR-0012），也不覆盖 Edge 侧删前
校验与文件系统；现场/长稳验证归 linked validation。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session as DatabaseSession
from test_auth_http import settings
from test_evidence_access import _post_as_host
from test_retention_impact_postgres import _decision, _instance
from test_runtime_observation_http import RuntimeTopology, _host_headers
from test_runtime_observation_http import runtime_topology as runtime_topology

from factory_sop.app import API_PREFIX, create_app
from factory_sop.auth.api import Caller
from factory_sop.auth.model import User, UserStatus
from factory_sop.auth.permissions import Permission
from factory_sop.device.adapters.repository import (
    PostgresInferenceHostRepository,
    PostgresStationRepository,
)
from factory_sop.device.api import DeviceEvidenceGateway
from factory_sop.device.usecases.retention import (
    RepositoryDeviceRetentionSources,
    RepositoryDeviceStationFacts,
)
from factory_sop.evidence.adapters.repository import (
    PostgresEvidenceDeletionClaimRepository,
    PostgresEvidenceReclipRequestRepository,
    PostgresEvidenceRepository,
    PostgresEvidenceReviewRepository,
)
from factory_sop.evidence.deletion import RepositoryEvidenceDeletionGateway
from factory_sop.evidence.errors import EvidenceRefusedError
from factory_sop.evidence.model import (
    EvidenceKind,
    EvidenceOrigin,
    EvidenceReclipRequest,
    EvidenceReference,
    EvidenceReferenceLock,
    EvidenceRegistration,
    EvidenceReview,
    EvidenceStatus,
    ReclipRequestStatus,
    ReviewConclusion,
)
from factory_sop.evidence.playback import EvidenceAccessRefusedError, issue_access_context
from factory_sop.evidence.reclip import EvidenceReclipRefusedError, create_reclip_request
from factory_sop.evidence.reviews import EvidenceReviewRefusedError, begin_review, save_review
from factory_sop.evidence.usecases import register_evidence
from factory_sop.identifiers import new_id
from factory_sop.monitor.adapters.repository import (
    PostgresMonitorImpactRepository,
    PostgresMonitorRepository,
)
from factory_sop.monitor.model import MirroredViolation
from factory_sop.monitor.retention import RepositoryMonitorImpact
from factory_sop.persistence import session_factory
from factory_sop.retention.adapters.repository import (
    PostgresRetentionPolicyRepository,
    PostgresStationRetentionOverrideRepository,
)
from factory_sop.retention.api import (
    DEFAULT_RETENTION_POLICY,
    DeletionAuthorityBusyError,
    DeletionAuthorityFence,
    EvidenceDeletionGateway,
    EvidenceDeletionReason,
    RetentionMode,
    RetentionPolicyState,
    RetentionRule,
    update_policy,
)
from factory_sop.retention.deletion import (
    EvidenceDeletionRefusedError,
    claim_evidence_deletion,
    settle_evidence_deletion,
    verify_evidence_deletion,
)
from nvsop_contracts import (
    EvidenceDeletionCandidate,
    EvidenceDeletionDecision,
    EvidenceDeletionOutcome,
    EvidenceDeletionResponse,
    EvidenceDeletionVerifyResponse,
    ReportEvidence,
    ReportViolation,
    configuration_from_wire,
)

CLAIMS_PATH = f"{API_PREFIX}/retention/evidence-deletion-claims"
REGISTRATIONS_PATH = f"{API_PREFIX}/evidence/registrations"
TTL_SECONDS = 300
CONFIG_SHA = "c" * 64
SECRET = "integration-evidence-deletion-secret"  # pragma: allowlist secret
DAY = 24 * 60 * 60
# 证据登记时刻取足够久以前：默认通过类保留 90 天，HTTP 路径的年龄以真实中心时钟为准。
RECEIVED_AT = datetime(2026, 1, 5, 1, 0, tzinfo=UTC)
NOW = RECEIVED_AT + timedelta(days=200)
DIGEST = "a" * 64
SIZE = 10
REFERENCE = "artifact/camera-a.mp4"
MATERIAL = {"sha256": DIGEST, "size": SIZE, "reference": REFERENCE}


class _Owner:
    """registration 归属校验使用的最小 device seam：只认这台主机拥有该工位。"""

    def __init__(self, topology: RuntimeTopology) -> None:
        self._topology = topology

    def owns_station(self, *, host_id: UUID, station_id: UUID) -> bool:
        return host_id == self._topology.host.id and station_id == self._topology.station.id


def _caller() -> Caller:
    """机器端点不使用人类权限；这里用于策略写入与复核/再切片写入用例。"""
    return Caller(
        user=User(
            new_id(),
            "retention-machine-operator",
            "Retention Machine Operator",
            "argon2-encoded",  # pragma: allowlist secret
            UserStatus.ACTIVE,
        ),
        granted=frozenset(
            {
                Permission.RETENTION_POLICY_EDIT,
                Permission.EVIDENCE_VIEW,
                Permission.EVIDENCE_REVIEW_EDIT,
            }
        ),
    )


def _registration(
    topology: RuntimeTopology, evidence_id: str, *, instance_id: int
) -> EvidenceRegistration:
    return EvidenceRegistration(
        evidence_id=evidence_id,
        host_id=str(topology.host.id),
        station_id=str(topology.station.id),
        instance_id=instance_id,
        violation_id=None,
        kind=EvidenceKind.CLIP,
        origin=EvidenceOrigin.AUTOMATIC,
        anchor=1.0,
        window_start=0.0,
        window_end=2.0,
        generation="original",
        sha256=DIGEST,
        size=SIZE,
        reference=REFERENCE,
    )


def _seed_instance(
    engine: Engine,
    topology: RuntimeTopology,
    *,
    instance_id: int,
    closed: bool = True,
    verdict: str | None = "pass",
    latched: bool = False,
) -> None:
    """按真实镜像事实播种一个实例：闭合与判定来自 monitor owner 的行，不是替身。"""
    host, station = str(topology.host.id), str(topology.station.id)
    with session_factory(engine)() as session:
        monitor = PostgresMonitorRepository(session)
        monitor.upsert_instance(
            _instance(
                station,
                host_id=host,
                instance_id=instance_id,
                received_at=RECEIVED_AT,
                closed=closed,
            )
        )
        if verdict is not None:
            monitor.upsert_decision(
                _decision(
                    station,
                    host_id=host,
                    instance_id=instance_id,
                    received_at=RECEIVED_AT,
                    verdict=verdict,
                )
            )
        if latched:
            monitor.upsert_violation(
                MirroredViolation(
                    event_id=f"violation/{host}/{instance_id}",
                    decision_event_id=f"decision/{host}/{instance_id}",
                    host_id=host,
                    station_id=station,
                    instance_id=instance_id,
                    report=ReportViolation(
                        reason_code="MISSED_STEP",
                        detail=None,
                        step_ids=(),
                        evidence=ReportEvidence(None, None, None),
                    ),
                    decision_reported_at="2026-01-01T00:00:00Z",
                    received_at=RECEIVED_AT,
                )
            )
        session.commit()


def _seed_evidence(
    engine: Engine, topology: RuntimeTopology, evidence_id: str, *, instance_id: int
) -> None:
    reference = EvidenceReference(
        _registration(topology, evidence_id, instance_id=instance_id),
        EvidenceStatus.AVAILABLE,
        None,
        RECEIVED_AT,
    )
    with session_factory(engine)() as session:
        assert PostgresEvidenceRepository(session).insert(reference)
        session.commit()


def _seed_review(engine: Engine, evidence_id: str) -> None:
    """删除前已有的人工结论：删除后必须仍然可查，不能被抹掉。"""
    with session_factory(engine)() as session:
        assert PostgresEvidenceReviewRepository(session).insert(
            EvidenceReview(
                review_id=uuid4(),
                evidence_id=evidence_id,
                generation="original",
                reviewer_id=new_id(),
                reviewer_name="集成复核人",
                reviewed_at=RECEIVED_AT,
                conclusion=ReviewConclusion.CONFIRMED,
            )
        )
        session.commit()


def _configure_current(
    topology: RuntimeTopology, engine: Engine, *, digest: str = CONFIG_SHA
) -> int:
    """把该主机当前的配置身份写进 device 拥有的行，返回其分配修订。"""
    with session_factory(engine)() as session:
        revision = PostgresInferenceHostRepository(session).next_configuration_revision(
            host_id=topology.host.id, content_sha256=digest
        )
        session.commit()
    return revision


@dataclass(frozen=True, slots=True)
class Seams:
    """一次删除资格判定的 owner seam 组合；全部落在同一个真实事务里。"""

    evidence: EvidenceDeletionGateway
    monitor: RepositoryMonitorImpact
    stations: RepositoryDeviceStationFacts
    sources: RepositoryDeviceRetentionSources
    policies: PostgresRetentionPolicyRepository
    overrides: PostgresStationRetentionOverrideRepository


def _deletion_gateway(
    session: DatabaseSession, *, evidence: EvidenceDeletionGateway | None = None
) -> EvidenceDeletionGateway:
    if evidence is not None:
        return evidence
    return RepositoryEvidenceDeletionGateway(
        evidence=PostgresEvidenceRepository(session),
        reviews=PostgresEvidenceReviewRepository(session),
        reclips=PostgresEvidenceReclipRequestRepository(session),
        claims=PostgresEvidenceDeletionClaimRepository(session),
    )


def _seams(
    session: DatabaseSession,
    *,
    evidence: EvidenceDeletionGateway | None = None,
    policies: PostgresRetentionPolicyRepository | None = None,
) -> Seams:
    return Seams(
        evidence=_deletion_gateway(session, evidence=evidence),
        monitor=RepositoryMonitorImpact(repository=PostgresMonitorImpactRepository(session)),
        stations=RepositoryDeviceStationFacts(stations=PostgresStationRepository(session)),
        sources=RepositoryDeviceRetentionSources(hosts=PostgresInferenceHostRepository(session)),
        policies=PostgresRetentionPolicyRepository(session) if policies is None else policies,
        overrides=PostgresStationRetentionOverrideRepository(session),
    )


def _candidate(evidence_id: str, **overrides: object) -> EvidenceDeletionCandidate:
    values: dict[str, object] = {"generation": "original", **MATERIAL}
    values.update(overrides)
    return EvidenceDeletionCandidate(evidence_id=evidence_id, **values)  # type: ignore[arg-type]


def _claim(
    seams: Seams,
    topology: RuntimeTopology,
    *,
    candidates: tuple[EvidenceDeletionCandidate, ...],
    config_revision: int,
    digest: str = CONFIG_SHA,
    now: datetime = NOW,
) -> EvidenceDeletionResponse:
    return claim_evidence_deletion(
        host_id=topology.host.id,
        config_revision=config_revision,
        effective_sha256=digest,
        candidates=candidates,
        evidence=seams.evidence,
        monitor=seams.monitor,
        stations=seams.stations,
        sources=seams.sources,
        policies=seams.policies,
        overrides=seams.overrides,
        now=now,
        ticket_ttl_seconds=TTL_SECONDS,
        secret=SECRET,
    )


def _verify(
    seams: Seams,
    topology: RuntimeTopology,
    *,
    ticket: str,
    evidence_id: str,
    config_revision: int,
    digest: str = CONFIG_SHA,
    now: datetime = NOW,
    ttl_seconds: int | None = TTL_SECONDS,
) -> EvidenceDeletionVerifyResponse:
    return verify_evidence_deletion(
        host_id=topology.host.id,
        ticket=ticket,
        config_revision=config_revision,
        effective_sha256=digest,
        candidate=_candidate(evidence_id),
        evidence=seams.evidence,
        monitor=seams.monitor,
        stations=seams.stations,
        sources=seams.sources,
        policies=seams.policies,
        overrides=seams.overrides,
        now=now,
        ticket_ttl_seconds=ttl_seconds,
        secret=SECRET,
    )


def _clear(engine: Engine, *evidence_ids: str) -> None:
    with engine.begin() as connection:
        if evidence_ids:
            connection.execute(
                text("DELETE FROM evidence_evidence WHERE evidence_id = ANY(:ids)"),
                {"ids": list(evidence_ids)},
            )
        connection.execute(text("DELETE FROM retention_policy"))


def _unreached_gateway() -> DeviceEvidenceGateway:
    """认领冲突在读取来源主机窗口之前就拒绝，因此该 seam 在这条路径上不会被调用。"""
    return cast(DeviceEvidenceGateway, None)


def test_signed_claim_verify_and_deleted_feedback_keep_history(
    engine: Engine, runtime_topology: RuntimeTopology
) -> None:
    """主机签名 HTTP 上走完 claim → verify → result(deleted)，并保留身份与复核历史。"""
    topology = runtime_topology
    host_id = str(topology.host.id)
    evidence_id = f"{host_id}/deletion-loop.mp4"
    revision = _configure_current(topology, engine)
    _seed_instance(engine, topology, instance_id=1)
    _seed_evidence(engine, topology, evidence_id, instance_id=1)
    _seed_review(engine, evidence_id)
    app = create_app(
        settings().model_copy(update={"evidence_deletion_ticket_ttl_seconds": TTL_SECONDS}),
        session_factory=session_factory(engine),
    )
    body: dict[str, object] = {
        "host_id": host_id,
        "config_revision": revision,
        "effective_sha256": CONFIG_SHA,
        "candidates": [{"evidence_id": evidence_id, "generation": "original", **MATERIAL}],
    }
    result_body: dict[str, object] = {
        "host_id": host_id,
        "claim_id": "",
        "evidence_id": evidence_id,
        "outcome": "deleted",
        "reason_code": "retention_expired",
        "detail": None,
    }
    try:
        with TestClient(app, base_url="https://testserver") as host:
            claimed = _post_as_host(host, topology, path=CLAIMS_PATH, body=body)
            assert claimed.status_code == 200, claimed.text
            (entry,) = claimed.json()["entries"]
            assert entry["decision"] == "eligible", entry
            assert entry["reason"] == "expired"
            assert entry["category"] == "pass"
            assert entry["threshold_seconds"] == 90 * DAY
            assert entry["station_id"] == str(topology.station.id)
            assert entry["instance_id"] == 1
            assert entry["age_seconds"] >= 200 * DAY - 1
            assert entry["received_at"] == RECEIVED_AT.isoformat()
            assert entry["ticket"]
            assert entry["claim_id"]

            verify_path = f"{CLAIMS_PATH}/verify"
            verify_body = {
                "host_id": host_id,
                "ticket": entry["ticket"],
                "config_revision": revision,
                "effective_sha256": CONFIG_SHA,
                "evidence_id": evidence_id,
                "generation": "original",
                **MATERIAL,
            }
            verified = _post_as_host(host, topology, path=verify_path, body=verify_body)
            assert verified.status_code == 200, verified.text
            assert verified.json()["decision"] == "eligible"
            assert verified.json()["reason"] == "expired"

            result_path = f"{CLAIMS_PATH}/result"
            result_body["claim_id"] = entry["claim_id"]
            reported = _post_as_host(host, topology, path=result_path, body=result_body)
            assert reported.status_code == 200, reported.text
            assert reported.json() == {
                "claim_id": entry["claim_id"],
                "evidence_id": evidence_id,
                "status": "deleted",
                "reason_code": "retention_expired",
            }
            # 同一反馈重放按同一状态幂等返回；相反结论的反馈被拒绝，终局不可改写。
            replayed = _post_as_host(host, topology, path=result_path, body=result_body)
            assert replayed.status_code == 200
            assert replayed.json() == reported.json()
            conflicting = _post_as_host(
                host,
                topology,
                path=result_path,
                body=result_body | {"outcome": "retained", "reason_code": "operator_kept"},
            )
            assert conflicting.status_code == 409
            assert conflicting.json()["detail"] == "claim_already_settled"

            again = _post_as_host(host, topology, path=CLAIMS_PATH, body=body)
            assert again.status_code == 200
            (second,) = again.json()["entries"]
            assert (second["decision"], second["reason"]) == ("unknown", "evidence_media_deleted")

            # 已确认删除的媒体不得被重新登记复活。
            resurrection = _post_as_host(
                host,
                topology,
                path=REGISTRATIONS_PATH,
                body={
                    "evidence_id": evidence_id,
                    "host_id": host_id,
                    "station_id": str(topology.station.id),
                    "instance_id": 1,
                    "violation_id": None,
                    "kind": "clip",
                    "origin": "automatic",
                    "anchor": 1.0,
                    "window_start": 0.0,
                    "window_end": 2.0,
                    "generation": "original",
                    **MATERIAL,
                },
            )
            assert resurrection.status_code == 409
            assert resurrection.json()["detail"] == "media_deleted"

            # 被改动的票据不是合法签名凭据：拒绝而不是继续判定。
            forged = _post_as_host(
                host,
                topology,
                path=verify_path,
                body=verify_body | {"ticket": f"{entry['ticket']}ff"},
            )
            assert forged.status_code == 403
            assert forged.json()["detail"] == "ticket_invalid"
        with session_factory(engine)() as session:
            stored = PostgresEvidenceRepository(session).find(evidence_id)
            assert stored is not None
            assert stored.status is EvidenceStatus.DELETED
            # 引用身份与复核历史保留下来：删除只把媒体标记为不可用。
            assert stored.registration.material() == (DIGEST, SIZE, REFERENCE)
            history = PostgresEvidenceReviewRepository(session).list_for_evidence(evidence_id)
            assert [row.conclusion for row in history] == [ReviewConclusion.CONFIRMED]
            assert (
                PostgresEvidenceDeletionClaimRepository(session).find_pending(evidence_id) is None
            )
            assert (
                PostgresEvidenceReviewRepository(session).active_reviewers(evidence_id=evidence_id)
                == frozenset()
            )
        # 已删除媒体在复核、再切片与播放入口上都是"不可用"，而不是静默成功。
        with DatabaseSession(engine) as session:
            evidence = PostgresEvidenceRepository(session)
            with pytest.raises(EvidenceReviewRefusedError) as review_refusal:
                begin_review(
                    _caller(),
                    review_id=uuid4(),
                    evidence_id=evidence_id,
                    generation="original",
                    now=NOW,
                    evidence=evidence,
                    reviews=PostgresEvidenceReviewRepository(session),
                )
            assert (review_refusal.value.status, review_refusal.value.code) == (
                410,
                "evidence_unavailable",
            )
            with pytest.raises(EvidenceReclipRefusedError) as reclip_refusal:
                create_reclip_request(
                    _caller(),
                    request_id=uuid4(),
                    evidence_id=evidence_id,
                    generation="original",
                    before_seconds=1.0,
                    after_seconds=1.0,
                    now=NOW,
                    evidence=evidence,
                    requests=PostgresEvidenceReclipRequestRepository(session),
                    gateway=_unreached_gateway(),
                )
            assert (reclip_refusal.value.status, reclip_refusal.value.code) == (
                410,
                "evidence_unavailable",
            )
            with pytest.raises(EvidenceAccessRefusedError) as playback_refusal:
                issue_access_context(
                    _caller(),
                    evidence_id=evidence_id,
                    fingerprint="integration-fingerprint",
                    evidence=evidence,
                    registered_hosts=(topology.host.id,),
                    origins={},
                    secret=SECRET,
                    now=NOW.timestamp(),
                )
            assert (playback_refusal.value.status, playback_refusal.value.code) == (
                410,
                "reference_unavailable",
            )
    finally:
        _clear(engine, evidence_id)


def test_each_protection_source_blocks_a_claim_for_an_expired_reference(
    engine: Engine, runtime_topology: RuntimeTopology
) -> None:
    """保护与年龄无关：每个 owner 事实单独成立时都不放行，并给出自己的机器原因。"""
    topology = runtime_topology
    host_id = str(topology.host.id)
    revision = _configure_current(topology, engine)
    instances = {
        "open": 21,
        "latched": 22,
        "review": 23,
        "reclip": 24,
        "playback": 25,
        "no_instance_fact": 26,
        "unknown_verdict": 27,
        "clean": 28,
    }
    _seed_instance(engine, topology, instance_id=instances["open"], closed=False)
    _seed_instance(engine, topology, instance_id=instances["latched"], latched=True, verdict="fail")
    for name in ("review", "reclip", "playback", "clean"):
        _seed_instance(engine, topology, instance_id=instances[name])
    # 该实例只有闭合事实、没有判定镜像：保留类别未知。
    _seed_instance(engine, topology, instance_id=instances["unknown_verdict"], verdict=None)
    identifiers = {name: f"{host_id}/protection-{name}.mp4" for name in instances}
    for name, instance_id in instances.items():
        if name != "no_instance_fact":
            _seed_evidence(engine, topology, identifiers[name], instance_id=instance_id)
    # 该实例没有任何镜像事实：monitor 的 seam 按未结案回答，因此仍受保护。
    _seed_evidence(
        engine,
        topology,
        identifiers["no_instance_fact"],
        instance_id=instances["no_instance_fact"],
    )
    try:
        with DatabaseSession(engine) as session:
            begin_review(
                _caller(),
                review_id=uuid4(),
                evidence_id=identifiers["review"],
                generation="original",
                now=NOW,
                evidence=PostgresEvidenceRepository(session),
                reviews=PostgresEvidenceReviewRepository(session),
            )
            assert PostgresEvidenceReclipRequestRepository(session).insert(
                EvidenceReclipRequest(
                    request_id=uuid4(),
                    evidence_id=identifiers["reclip"],
                    generation="original",
                    host_id=topology.host.id,
                    anchor=1.0,
                    before_seconds=1.0,
                    after_seconds=1.0,
                    window_start=0.0,
                    window_end=2.0,
                    requested_by=new_id(),
                    requested_at=RECEIVED_AT,
                    command_id=new_id(),
                    status=ReclipRequestStatus.PENDING,
                )
            )
            leased = PostgresEvidenceRepository(session).lock_playback(
                identifiers["playback"], expires_at=NOW + timedelta(seconds=120)
            )
            assert leased.reference is not None
            session.commit()
        with DatabaseSession(engine) as session:
            entries = {
                entry.evidence_id: entry
                for entry in _claim(
                    _seams(session),
                    topology,
                    candidates=tuple(_candidate(value) for value in identifiers.values()),
                    config_revision=revision,
                ).entries
            }
        expected = {
            "open": (EvidenceDeletionDecision.PROTECTED, EvidenceDeletionReason.INSTANCE_OPEN),
            "latched": (
                EvidenceDeletionDecision.PROTECTED,
                EvidenceDeletionReason.LATCHED_VIOLATION,
            ),
            "review": (
                EvidenceDeletionDecision.PROTECTED,
                EvidenceDeletionReason.REVIEW_IN_PROGRESS,
            ),
            "reclip": (EvidenceDeletionDecision.PROTECTED, EvidenceDeletionReason.RECLIP_PENDING),
            "playback": (
                EvidenceDeletionDecision.PROTECTED,
                EvidenceDeletionReason.PLAYBACK_IN_PROGRESS,
            ),
            "no_instance_fact": (
                EvidenceDeletionDecision.PROTECTED,
                EvidenceDeletionReason.INSTANCE_OPEN,
            ),
            "unknown_verdict": (
                EvidenceDeletionDecision.UNKNOWN,
                EvidenceDeletionReason.INSTANCE_CATEGORY_UNKNOWN,
            ),
            "clean": (EvidenceDeletionDecision.ELIGIBLE, EvidenceDeletionReason.EXPIRED),
        }
        for name, (decision, reason) in expected.items():
            entry = entries[identifiers[name]]
            assert (entry.decision, entry.reason) == (decision.value, reason.value), name
        # 只有干净的那条拿到票据；其它条目一律不带票据与认领身份。
        assert entries[identifiers["clean"]].ticket is not None
        for name, entry in entries.items():
            if name != identifiers["clean"]:
                assert entry.ticket is None, name
                assert entry.claim_id is None, name
        # 材料身份不符的候选即便引用可用也不放行：不是同一个对象。
        with DatabaseSession(engine) as session:
            (mismatched,) = _claim(
                _seams(session),
                topology,
                candidates=(_candidate(identifiers["clean"], sha256="b" * 64),),
                config_revision=revision,
            ).entries
        assert (mismatched.decision, mismatched.reason) == ("unknown", "material_mismatch")
    finally:
        _clear(engine, *identifiers.values())


def test_pending_claim_conflicts_with_reference_writers_and_expiry_does_not_release_it(
    engine: Engine, runtime_topology: RuntimeTopology
) -> None:
    """未结案认领让引用写入给出可解释冲突；票据过期只让票据失效，不释放认领。"""
    topology = runtime_topology
    host_id = str(topology.host.id)
    revision = _configure_current(topology, engine)
    instance_id = 31
    evidence_id = f"{host_id}/claim-conflict.mp4"
    _seed_instance(engine, topology, instance_id=instance_id)
    _seed_evidence(engine, topology, evidence_id, instance_id=instance_id)
    try:
        with DatabaseSession(engine) as session:
            (claimed,) = _claim(
                _seams(session),
                topology,
                candidates=(_candidate(evidence_id),),
                config_revision=revision,
            ).entries
            assert claimed.decision is EvidenceDeletionDecision.ELIGIBLE
            assert claimed.ticket is not None
            assert claimed.claim_id is not None
            ticket, claim_id = claimed.ticket, UUID(claimed.claim_id)
            session.commit()
        caller = _caller()
        with DatabaseSession(engine) as session:
            evidence = PostgresEvidenceRepository(session)
            reviews = PostgresEvidenceReviewRepository(session)
            with pytest.raises(EvidenceReviewRefusedError) as hold_refusal:
                begin_review(
                    caller,
                    review_id=uuid4(),
                    evidence_id=evidence_id,
                    generation="original",
                    now=NOW,
                    evidence=evidence,
                    reviews=reviews,
                )
            assert (hold_refusal.value.status, hold_refusal.value.code) == (
                409,
                "evidence_deletion_claimed",
            )
            with pytest.raises(EvidenceReviewRefusedError) as conclusion_refusal:
                save_review(
                    caller,
                    review_id=uuid4(),
                    evidence_id=evidence_id,
                    generation="original",
                    conclusion=ReviewConclusion.CONFIRMED,
                    reviewed_at=NOW,
                    evidence=evidence,
                    reviews=reviews,
                )
            assert (conclusion_refusal.value.status, conclusion_refusal.value.code) == (
                409,
                "evidence_deletion_claimed",
            )
            with pytest.raises(EvidenceReclipRefusedError) as reclip_refusal:
                create_reclip_request(
                    caller,
                    request_id=uuid4(),
                    evidence_id=evidence_id,
                    generation="original",
                    before_seconds=1.0,
                    after_seconds=1.0,
                    now=NOW,
                    evidence=evidence,
                    requests=PostgresEvidenceReclipRequestRepository(session),
                    gateway=_unreached_gateway(),
                )
            assert (reclip_refusal.value.status, reclip_refusal.value.code) == (
                409,
                "evidence_deletion_claimed",
            )
            with pytest.raises(EvidenceAccessRefusedError) as playback_refusal:
                issue_access_context(
                    caller,
                    evidence_id=evidence_id,
                    fingerprint="integration-fingerprint",
                    evidence=evidence,
                    registered_hosts=(topology.host.id,),
                    origins={},
                    secret=SECRET,
                    now=NOW.timestamp(),
                )
            assert (playback_refusal.value.status, playback_refusal.value.code) == (
                409,
                "media_deletion_pending",
            )
            with pytest.raises(EvidenceRefusedError) as registration_refusal:
                register_evidence(
                    _registration(topology, evidence_id, instance_id=instance_id),
                    received_at=NOW,
                    evidence=evidence,
                    host_gateway=_Owner(topology),
                )
            assert registration_refusal.value.code.value == "deletion_claim_pending"
        # 票据过期只让这张票据失效：认领仍在，引用继续被保护。
        with DatabaseSession(engine) as session:
            expired = _verify(
                _seams(session),
                topology,
                ticket=ticket,
                evidence_id=evidence_id,
                config_revision=revision,
                now=NOW + timedelta(seconds=TTL_SECONDS + 1),
            )
            assert (expired.decision.value, expired.reason) == ("unknown", "ticket_expired")
            assert (
                PostgresEvidenceDeletionClaimRepository(session).find_pending(evidence_id)
                is not None
            )
        with DatabaseSession(engine) as session:
            with pytest.raises(EvidenceReviewRefusedError) as still_claimed:
                begin_review(
                    caller,
                    review_id=uuid4(),
                    evidence_id=evidence_id,
                    generation="original",
                    now=NOW + timedelta(seconds=TTL_SECONDS + 2),
                    evidence=PostgresEvidenceRepository(session),
                    reviews=PostgresEvidenceReviewRepository(session),
                )
            assert still_claimed.value.code == "evidence_deletion_claimed"
        # 只有来源机的终局反馈释放认领：取消后引用写入恢复。
        with DatabaseSession(engine) as session:
            settled = settle_evidence_deletion(
                host_id=topology.host.id,
                claim_id=claim_id,
                evidence_id=evidence_id,
                outcome=EvidenceDeletionOutcome.CANCELLED,
                reason_code="delete_cancelled",
                detail="ticket expired before unlink",
                evidence=_seams(session).evidence,
                now=NOW + timedelta(seconds=TTL_SECONDS + 3),
            )
            assert settled.status.value == "cancelled"
            session.commit()
        with DatabaseSession(engine) as session:
            assert (
                PostgresEvidenceDeletionClaimRepository(session).find_pending(evidence_id) is None
            )
            hold = begin_review(
                caller,
                review_id=uuid4(),
                evidence_id=evidence_id,
                generation="original",
                now=NOW,
                evidence=PostgresEvidenceRepository(session),
                reviews=PostgresEvidenceReviewRepository(session),
            )
            assert hold.evidence_id == evidence_id
            session.commit()
    finally:
        _clear(engine, evidence_id)


class _BarrierEvidence(PostgresEvidenceRepository):
    """在取引用行锁之前对齐两个真实事务，使并发真的发生在锁上。"""

    def __init__(self, session: DatabaseSession, barrier: Barrier) -> None:
        super().__init__(session)
        self._barrier = barrier

    def lock(self, evidence_id: str) -> EvidenceReferenceLock:
        self._barrier.wait(timeout=15)
        return super().lock(evidence_id)


def test_claim_and_review_begin_cannot_both_win_on_one_reference(
    engine: Engine, runtime_topology: RuntimeTopology
) -> None:
    """两个独立 PostgreSQL 会话竞争同一条引用：认领与在审复核只有一个能成立。"""
    topology = runtime_topology
    host_id = str(topology.host.id)
    revision = _configure_current(topology, engine)
    instance_id = 41
    evidence_id = f"{host_id}/claim-race.mp4"
    _seed_instance(engine, topology, instance_id=instance_id)
    _seed_evidence(engine, topology, evidence_id, instance_id=instance_id)
    caller = _caller()
    barrier = Barrier(2)
    try:

        def claim_side() -> tuple[str, str, str]:
            with DatabaseSession(engine) as session:
                try:
                    (entry,) = _claim(
                        _seams(
                            session,
                            evidence=RepositoryEvidenceDeletionGateway(
                                evidence=_BarrierEvidence(session, barrier),
                                reviews=PostgresEvidenceReviewRepository(session),
                                reclips=PostgresEvidenceReclipRequestRepository(session),
                                claims=PostgresEvidenceDeletionClaimRepository(session),
                            ),
                        ),
                        topology,
                        candidates=(_candidate(evidence_id),),
                        config_revision=revision,
                    ).entries
                    session.commit()
                    return ("claim", entry.decision.value, entry.reason)
                except Exception as error:
                    session.rollback()
                    return ("claim-error", type(error).__name__, str(error))

        def review_side() -> tuple[str, str, str]:
            with DatabaseSession(engine) as session:
                try:
                    hold = begin_review(
                        caller,
                        review_id=uuid4(),
                        evidence_id=evidence_id,
                        generation="original",
                        now=NOW,
                        evidence=_BarrierEvidence(session, barrier),
                        reviews=PostgresEvidenceReviewRepository(session),
                    )
                    session.commit()
                    return ("review", str(hold.review_id), "")
                except EvidenceReviewRefusedError as error:
                    session.rollback()
                    return ("review-refused", error.code, "")

        with ThreadPoolExecutor(max_workers=2) as workers:
            claim_future = workers.submit(claim_side)
            review_future = workers.submit(review_side)
            outcomes = (claim_future.result(timeout=60), review_future.result(timeout=60))
        claim_outcome = next(item for item in outcomes if item[0].startswith("claim"))
        review_outcome = next(item for item in outcomes if item[0].startswith("review"))
        with DatabaseSession(engine) as session:
            pending = PostgresEvidenceDeletionClaimRepository(session).find_pending(evidence_id)
            holders = PostgresEvidenceReviewRepository(session).active_reviewers(
                evidence_id=evidence_id
            )
        assert holders is not None
        assert claim_outcome[0] == "claim", outcomes
        if claim_outcome[1] == "eligible":
            # 认领先到：在审复核被解释性拒绝，数据库里只有未结案认领。
            assert review_outcome == ("review-refused", "evidence_deletion_claimed", "")
            assert pending is not None
            assert holders == frozenset()
        else:
            # 复核先到：认领不被登记，判定必须落在受保护一侧。
            assert (claim_outcome[1], claim_outcome[2]) in {
                ("protected", "review_in_progress"),
                ("protected", "deletion_claim_pending"),
            }, outcomes
            assert review_outcome[0] == "review", outcomes
            assert pending is None
            assert holders != frozenset()
    finally:
        _clear(engine, evidence_id)


class _PausingPolicies(PostgresRetentionPolicyRepository):
    """在取得共享锁之后暂停，让并发的策略写入真的撞上这把锁。"""

    def __init__(
        self, session: DatabaseSession, holding: threading.Event, release: threading.Event
    ) -> None:
        super().__init__(session)
        self._holding, self._release = holding, release

    def lock_current(self) -> RetentionPolicyState | None:
        state = super().lock_current()
        self._holding.set()
        assert self._release.wait(timeout=15), "policy holder was never released"
        return state


def test_claim_creation_blocks_a_concurrent_policy_write(
    engine: Engine, runtime_topology: RuntimeTopology
) -> None:
    """认领登记持有策略行的共享锁：并发策略写入真实等待，而不是"先计数再写入"。"""
    topology = runtime_topology
    host_id = str(topology.host.id)
    revision = _configure_current(topology, engine)
    instance_id = 51
    evidence_id = f"{host_id}/policy-lock.mp4"
    _seed_instance(engine, topology, instance_id=instance_id)
    _seed_evidence(engine, topology, evidence_id, instance_id=instance_id)
    with session_factory(engine)() as session:
        update_policy(
            _caller(),
            DEFAULT_RETENTION_POLICY,
            expected_revision=0,
            repository=PostgresRetentionPolicyRepository(session),
        )
        session.commit()
    holding, release = threading.Event(), threading.Event()
    try:

        def claim_side() -> str:
            with DatabaseSession(engine) as session:
                response = claim_evidence_deletion(
                    host_id=topology.host.id,
                    config_revision=revision,
                    effective_sha256=CONFIG_SHA,
                    candidates=(_candidate(evidence_id),),
                    evidence=_deletion_gateway(session),
                    monitor=RepositoryMonitorImpact(
                        repository=PostgresMonitorImpactRepository(session)
                    ),
                    stations=RepositoryDeviceStationFacts(
                        stations=PostgresStationRepository(session)
                    ),
                    sources=RepositoryDeviceRetentionSources(
                        hosts=PostgresInferenceHostRepository(session)
                    ),
                    policies=_PausingPolicies(session, holding, release),
                    overrides=PostgresStationRetentionOverrideRepository(session),
                    now=NOW,
                    ticket_ttl_seconds=TTL_SECONDS,
                    secret=SECRET,
                )
                session.commit()
                return response.entries[0].decision.value

        with ThreadPoolExecutor(max_workers=1) as workers:
            pending_claim = workers.submit(claim_side)
            assert holding.wait(timeout=15), "claim never took the shared policy lock"
            try:
                with DatabaseSession(engine) as session:
                    session.execute(text("SET LOCAL lock_timeout = '300ms'"))
                    with pytest.raises(OperationalError) as blocked:
                        update_policy(
                            _caller(),
                            DEFAULT_RETENTION_POLICY,
                            expected_revision=1,
                            repository=PostgresRetentionPolicyRepository(session),
                        )
                    assert "timeout" in str(blocked.value).lower()
                    session.rollback()
            finally:
                release.set()
            assert pending_claim.result(timeout=30) == "eligible"
    finally:
        _clear(engine, evidence_id)


def test_stale_configuration_and_policy_bindings_stop_a_verified_deletion(
    engine: Engine, runtime_topology: RuntimeTopology
) -> None:
    """认领绑定来源配置身份与策略版本：期间任一变化都让删前校验不再放行。"""
    topology = runtime_topology
    host_id = str(topology.host.id)
    instance_id = 61
    evidence_id = f"{host_id}/stale-binding.mp4"
    _seed_instance(engine, topology, instance_id=instance_id)
    _seed_evidence(engine, topology, evidence_id, instance_id=instance_id)
    revision = _configure_current(topology, engine)
    try:
        with DatabaseSession(engine) as session:
            (claimed,) = _claim(
                _seams(session),
                topology,
                candidates=(_candidate(evidence_id),),
                config_revision=revision,
            ).entries
            assert claimed.decision is EvidenceDeletionDecision.ELIGIBLE
            assert claimed.ticket is not None
            ticket = claimed.ticket
            session.commit()
        # 当前配置前进：认领记录的旧身份不再等于来源机现在的已确认配置。
        next_revision = _configure_current(topology, engine, digest="d" * 64)
        assert next_revision != revision
        with DatabaseSession(engine) as session:
            stale_config = _verify(
                _seams(session),
                topology,
                ticket=ticket,
                evidence_id=evidence_id,
                config_revision=next_revision,
                digest="d" * 64,
            )
            assert (stale_config.decision.value, stale_config.reason) == (
                "unknown",
                "config_changed",
            )
            # 提交一份更旧的身份：来源机与中心对"当前配置"不一致，整批拒绝。
            with pytest.raises(EvidenceDeletionRefusedError) as stale:
                _verify(
                    _seams(session),
                    topology,
                    ticket=ticket,
                    evidence_id=evidence_id,
                    config_revision=revision,
                )
            assert (stale.value.status, stale.value.reason) == (
                409,
                EvidenceDeletionReason.CONFIG_NOT_CURRENT,
            )
        # 延长通过类保留期：旧认领记录的策略版本不再等于当前版本。
        with session_factory(engine)() as session:
            update_policy(
                _caller(),
                DEFAULT_RETENTION_POLICY,
                expected_revision=0,
                repository=PostgresRetentionPolicyRepository(session),
            )
            session.commit()
        with session_factory(engine)() as session:
            update_policy(
                _caller(),
                replace(
                    DEFAULT_RETENTION_POLICY,
                    evidence_pass=RetentionRule(RetentionMode.DURATION, 365 * DAY),
                ),
                expected_revision=1,
                repository=PostgresRetentionPolicyRepository(session),
            )
            session.commit()
        with DatabaseSession(engine) as session:
            (fresh,) = _claim(
                _seams(session),
                topology,
                candidates=(_candidate(evidence_id),),
                config_revision=next_revision,
                digest="d" * 64,
            ).entries
            # 旧认领在票据过期后仍然存在：新的判定必须落在受保护一侧，且原因指向未结案认领。
            assert (fresh.decision.value, fresh.reason) == (
                "protected",
                "deletion_claim_pending",
            )
            assert fresh.ticket is None
            assert (
                PostgresEvidenceDeletionClaimRepository(session).find_pending(evidence_id)
                is not None
            )
    finally:
        _clear(engine, evidence_id)


def test_authorized_deletion_fences_policy_mutation_until_source_result(
    engine: Engine, runtime_topology: RuntimeTopology
) -> None:
    """verify 成功后到来源机终局反馈前，策略写入必须被拒绝；结案后恢复。"""
    topology = runtime_topology
    host_id = str(topology.host.id)
    revision = _configure_current(topology, engine)
    instance_id = 71
    evidence_id = f"{host_id}/authorized-fence.mp4"
    _seed_instance(engine, topology, instance_id=instance_id)
    _seed_evidence(engine, topology, evidence_id, instance_id=instance_id)
    try:
        with DatabaseSession(engine) as session:
            (claimed,) = _claim(
                _seams(session),
                topology,
                candidates=(_candidate(evidence_id),),
                config_revision=revision,
            ).entries
            assert claimed.decision is EvidenceDeletionDecision.ELIGIBLE
            assert claimed.ticket is not None
            verified = _verify(
                _seams(session),
                topology,
                ticket=claimed.ticket,
                evidence_id=evidence_id,
                config_revision=revision,
            )
            assert verified.decision is EvidenceDeletionDecision.ELIGIBLE
            claim_id = UUID(claimed.claim_id or "")
            session.commit()
        # 已授权：全局策略写入必须在同一事务内被权威锁拒绝。
        with DatabaseSession(engine) as session:
            fence = cast(DeletionAuthorityFence, _seams(session).evidence)
            with pytest.raises(DeletionAuthorityBusyError):
                update_policy(
                    _caller(),
                    replace(
                        DEFAULT_RETENTION_POLICY,
                        evidence_pass=RetentionRule(RetentionMode.DURATION, 999 * DAY),
                    ),
                    expected_revision=0,
                    repository=PostgresRetentionPolicyRepository(session),
                    fence=fence,
                )
            session.rollback()
        # 来源机取消后认领不再是未结案授权：策略写入恢复。
        with DatabaseSession(engine) as session:
            settled = settle_evidence_deletion(
                host_id=topology.host.id,
                claim_id=claim_id,
                evidence_id=evidence_id,
                outcome=EvidenceDeletionOutcome.CANCELLED,
                reason_code="delete_cancelled",
                detail="fence test release",
                evidence=_seams(session).evidence,
                now=NOW,
            )
            assert settled.status.value == "cancelled"
            session.commit()
        with DatabaseSession(engine) as session:
            fence = cast(DeletionAuthorityFence, _seams(session).evidence)
            state = update_policy(
                _caller(),
                DEFAULT_RETENTION_POLICY,
                expected_revision=0,
                repository=PostgresRetentionPolicyRepository(session),
                fence=fence,
            )
            assert state.revision == 1
            session.commit()
    finally:
        _clear(engine, evidence_id)


def test_unverified_pending_claim_does_not_block_policy_mutation(
    engine: Engine, runtime_topology: RuntimeTopology
) -> None:
    """只有 claim 而未 verify 的未结案认领不阻断权威变更：普通 pending 仍给出既有 stale 原因。"""
    topology = runtime_topology
    host_id = str(topology.host.id)
    revision = _configure_current(topology, engine)
    instance_id = 72
    evidence_id = f"{host_id}/pending-not-authorized.mp4"
    _seed_instance(engine, topology, instance_id=instance_id)
    _seed_evidence(engine, topology, evidence_id, instance_id=instance_id)
    try:
        with DatabaseSession(engine) as session:
            (claimed,) = _claim(
                _seams(session),
                topology,
                candidates=(_candidate(evidence_id),),
                config_revision=revision,
            ).entries
            assert claimed.decision is EvidenceDeletionDecision.ELIGIBLE
            session.commit()
        with DatabaseSession(engine) as session:
            fence = cast(DeletionAuthorityFence, _seams(session).evidence)
            state = update_policy(
                _caller(),
                DEFAULT_RETENTION_POLICY,
                expected_revision=0,
                repository=PostgresRetentionPolicyRepository(session),
                fence=fence,
            )
            assert state.revision == 1
            session.commit()
        with DatabaseSession(engine) as session:
            (again,) = _claim(
                _seams(session),
                topology,
                candidates=(_candidate(evidence_id),),
                config_revision=revision,
            ).entries
            # 未授权认领仍按 pending 保护，且给出既有的认领原因；策略版本变化不影响该表达。
            assert (again.decision.value, again.reason) == ("protected", "deletion_claim_pending")
    finally:
        _clear(engine, evidence_id)


def test_verify_without_configured_ticket_ttl_refuses_new_authorization(
    engine: Engine, runtime_topology: RuntimeTopology
) -> None:
    """缺失删除票据有效期时 verify 不产生新授权；已持有认领的终局反馈仍可结案。"""
    topology = runtime_topology
    host_id = str(topology.host.id)
    revision = _configure_current(topology, engine)
    instance_id = 73
    evidence_id = f"{host_id}/ttl-disabled.mp4"
    _seed_instance(engine, topology, instance_id=instance_id)
    _seed_evidence(engine, topology, evidence_id, instance_id=instance_id)
    try:
        with DatabaseSession(engine) as session:
            (claimed,) = _claim(
                _seams(session),
                topology,
                candidates=(_candidate(evidence_id),),
                config_revision=revision,
            ).entries
            assert claimed.ticket is not None
            with pytest.raises(EvidenceDeletionRefusedError) as refused:
                _verify(
                    _seams(session),
                    topology,
                    ticket=claimed.ticket,
                    evidence_id=evidence_id,
                    config_revision=revision,
                    ttl_seconds=None,
                )
            assert (refused.value.status, refused.value.reason.value) == (
                409,
                "ticket_ttl_unconfigured",
            )
            # 未授权认领仍存在；result 不受有效期配置影响，可正常结案清理。
            settled = settle_evidence_deletion(
                host_id=topology.host.id,
                claim_id=UUID(claimed.claim_id or ""),
                evidence_id=evidence_id,
                outcome=EvidenceDeletionOutcome.CANCELLED,
                reason_code="ttl_disabled",
                detail=None,
                evidence=_seams(session).evidence,
                now=NOW,
            )
            assert settled.status.value == "cancelled"
            session.commit()
    finally:
        _clear(engine, evidence_id)


@pytest.mark.parametrize(
    "candidates",
    [
        [None],
        [1],
        ["evidence-id"],
        [{"evidence_id": 1, "generation": "original", **MATERIAL}],
        [{"evidence_id": "a/b.mp4", "unknown": 1}],
    ],
)
def test_malformed_deletion_candidates_are_rejected_as_422(
    candidates: list[object], engine: Engine, runtime_topology: RuntimeTopology
) -> None:
    """候选元素不是严格对象、字段缺失或含未知字段时必须 422，不能 500。"""
    topology = runtime_topology
    host_id = str(topology.host.id)
    revision = _configure_current(topology, engine)
    app = create_app(
        settings().model_copy(update={"evidence_deletion_ticket_ttl_seconds": TTL_SECONDS}),
        session_factory=session_factory(engine),
    )
    body: dict[str, object] = {
        "host_id": host_id,
        "config_revision": revision,
        "effective_sha256": CONFIG_SHA,
        "candidates": candidates,
    }
    with TestClient(app, base_url="https://testserver") as host:
        response = _post_as_host(host, topology, path=CLAIMS_PATH, body=body)
    assert response.status_code == 422, response.text


def test_unchanged_configuration_pull_is_allowed_while_authorized_deletion_pending(
    engine: Engine, runtime_topology: RuntimeTopology
) -> None:
    """未变的签名配置拉取/租约续期不被在途授权删除阻断；真正改变绑定的配置被原子拒绝。"""
    topology = runtime_topology
    host_id = str(topology.host.id)
    instance_id = 81
    evidence_id = f"{host_id}/unchanged-pull.mp4"
    config_path = f"{API_PREFIX}/inference-hosts/{host_id}/configuration"
    app = create_app(
        settings().model_copy(update={"evidence_deletion_ticket_ttl_seconds": TTL_SECONDS}),
        session_factory=session_factory(engine),
    )
    try:
        with TestClient(app, base_url="https://testserver") as base:
            pulled = base.get(
                config_path, headers=_host_headers(topology, method="GET", path=config_path)
            )
            assert pulled.status_code == 200, pulled.text
            bundle = configuration_from_wire(pulled.json())
            _seed_instance(engine, topology, instance_id=instance_id)
            _seed_evidence(engine, topology, evidence_id, instance_id=instance_id)
            with DatabaseSession(engine) as session:
                (claimed,) = _claim(
                    _seams(session),
                    topology,
                    candidates=(_candidate(evidence_id),),
                    config_revision=bundle.config_revision,
                    digest=bundle.effective_sha256,
                ).entries
                assert claimed.decision is EvidenceDeletionDecision.ELIGIBLE
                assert claimed.ticket is not None
                verified = _verify(
                    _seams(session),
                    topology,
                    ticket=claimed.ticket,
                    evidence_id=evidence_id,
                    config_revision=bundle.config_revision,
                    digest=bundle.effective_sha256,
                )
                assert verified.decision is EvidenceDeletionDecision.ELIGIBLE
                claim_id = UUID(claimed.claim_id or "")
                session.commit()
            # 有效配置未变（含仅租约续期的签名 GET）：不得被在途授权删除拒绝。
            unchanged = base.get(
                config_path, headers=_host_headers(topology, method="GET", path=config_path)
            )
            assert unchanged.status_code == 200, unchanged.text
            unchanged_bundle = configuration_from_wire(unchanged.json())
            assert (
                unchanged_bundle.config_revision,
                unchanged_bundle.effective_sha256,
            ) == (bundle.config_revision, bundle.effective_sha256)
            # 真正改变删除所依据的绑定配置：必须被原子拒绝。
            with engine.begin() as connection:
                connection.execute(
                    text("UPDATE device_station SET revision = revision + 1 WHERE id = :id"),
                    {"id": topology.station.id},
                )
            changed = base.get(
                config_path, headers=_host_headers(topology, method="GET", path=config_path)
            )
            assert changed.status_code == 409, changed.text
            assert changed.json()["error_code"] == "STATE_CONFLICT"
            # 来源机终局反馈后权威释放，新配置可继续签发。
            with DatabaseSession(engine) as session:
                settle_evidence_deletion(
                    host_id=topology.host.id,
                    claim_id=claim_id,
                    evidence_id=evidence_id,
                    outcome=EvidenceDeletionOutcome.CANCELLED,
                    reason_code="delete_cancelled",
                    detail="release fence",
                    evidence=_seams(session).evidence,
                    now=NOW,
                )
                session.commit()
            restored = base.get(
                config_path, headers=_host_headers(topology, method="GET", path=config_path)
            )
            assert restored.status_code == 200, restored.text
    finally:
        _clear(engine, evidence_id)


def test_claim_replay_is_idempotent_for_same_binding_and_refuses_changed_binding(
    engine: Engine, runtime_topology: RuntimeTopology
) -> None:
    """崩溃丢失 HTTP 响应后，同一来源/同一绑定可幂等重放自己的未结案认领；绑定变化则不放行。"""
    topology = runtime_topology
    host_id = str(topology.host.id)
    revision = _configure_current(topology, engine)
    instance_id = 91
    evidence_id = f"{host_id}/claim-replay.mp4"
    _seed_instance(engine, topology, instance_id=instance_id)
    _seed_evidence(engine, topology, evidence_id, instance_id=instance_id)
    try:
        with DatabaseSession(engine) as session:
            first = _claim(
                _seams(session),
                topology,
                candidates=(_candidate(evidence_id),),
                config_revision=revision,
            ).entries[0]
            assert first.decision is EvidenceDeletionDecision.ELIGIBLE
            assert first.ticket is not None
            assert first.claim_id is not None
            session.commit()
        # 同一绑定重放：返回同一条认领与新的短期票据，不新建第二条认领。
        with DatabaseSession(engine) as session:
            replay = _claim(
                _seams(session),
                topology,
                candidates=(_candidate(evidence_id),),
                config_revision=revision,
            ).entries[0]
            assert replay.decision is EvidenceDeletionDecision.ELIGIBLE
            assert replay.claim_id == first.claim_id
            assert replay.ticket is not None
            assert (
                PostgresEvidenceDeletionClaimRepository(session).find_pending(evidence_id)
                is not None
            )
            session.commit()
        # 绑定配置变化后不得刷新旧认领：仍按未结案认领受保护。
        next_revision = _configure_current(topology, engine, digest="e" * 64)
        with DatabaseSession(engine) as session:
            changed = _claim(
                _seams(session),
                topology,
                candidates=(_candidate(evidence_id),),
                config_revision=next_revision,
                digest="e" * 64,
            ).entries[0]
            assert (changed.decision.value, changed.reason) == (
                "protected",
                "deletion_claim_pending",
            )
            assert changed.ticket is None
            pending_after = PostgresEvidenceDeletionClaimRepository(session).find_pending(
                evidence_id
            )
            assert pending_after is not None
            assert pending_after.claim_id == UUID(first.claim_id)
    finally:
        _clear(engine, evidence_id)
