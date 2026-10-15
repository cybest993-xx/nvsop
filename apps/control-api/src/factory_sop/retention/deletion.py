"""逐证据删除资格：retention 拥有判定、票据与保护事实的编排（#206）。

三个主机签名动作都在这里实现：

1. ``claim_evidence_deletion``：校验来源机的当前已确认配置，逐条按 owner 事实判定
   ``eligible/protected/unknown``，并为 ``eligible`` 的引用登记未结案删除认领与短期票据；
2. ``verify_evidence_deletion``：删除前用票据重新校验，来源配置、保留策略版本、认领状态与保护
   事实任一变旧即不再放行；
3. ``settle_evidence_deletion``：把来源机的终局反馈结案为认领状态。

判定规则是 ``model.evidence_deletion_decision`` 的纯规则；本模块只负责取事实、发票据与编排，不删除
任何媒体字节，也不跨模块读表（事实都经各 owner 的公开 seam）。票据是不透明的 HMAC 短票据，只
指向一条持久认领：票据过期不释放认领，只有来源机的终局反馈才释放保护。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from factory_sop.retention.model import (
    DEFAULT_RETENTION_POLICY,
    EvidenceDeletionFacts,
    EvidenceDeletionReason,
    RetentionCategory,
    RetentionPolicy,
    RetentionPolicyMode,
    category_for,
    evidence_deletion_decision,
    evidence_retention_rule,
    resolve_retention_policy,
)
from factory_sop.retention.usecases import (
    EdgeImpactSourcesGateway,
    EvidenceDeletionClaimState,
    EvidenceDeletionGateway,
    EvidenceDeletionReference,
    MonitorImpactGateway,
    RetentionPolicyRepository,
    RetentionStationPresence,
    StationFactsGateway,
    StationRetentionOverrideRepository,
)
from nvsop_contracts import (
    EvidenceDeletionCandidate,
    EvidenceDeletionClaimStatus,
    EvidenceDeletionDecision,
    EvidenceDeletionEntry,
    EvidenceDeletionOutcome,
    EvidenceDeletionResponse,
    EvidenceDeletionVerifyResponse,
)
from nvsop_contracts import EvidenceDeletionResultResponse as EvidenceDeletionResult

EDGE_DELETION_TICKET_SCOPE = b"retention-evidence-deletion\0"
_TICKET_LIMIT = 4096
_OUTCOME_STATUS: dict[EvidenceDeletionOutcome, EvidenceDeletionClaimStatus] = {
    EvidenceDeletionOutcome.DELETED: EvidenceDeletionClaimStatus.DELETED,
    EvidenceDeletionOutcome.RETAINED: EvidenceDeletionClaimStatus.RETAINED,
    EvidenceDeletionOutcome.CANCELLED: EvidenceDeletionClaimStatus.CANCELLED,
}


class EvidenceDeletionRefusedError(ValueError):
    """删除资格请求被拒绝；``reason`` 是稳定的机器原因码，不携带媒体内容或秘密。"""

    def __init__(self, status: int, reason: EvidenceDeletionReason) -> None:
        super().__init__(reason.value)
        self.status = status
        self.reason = reason


@dataclass(frozen=True, slots=True)
class DeletionTicket:
    """一条删除认领的短期凭据；它只指向认领，过期即失效，但不释放认领。"""

    claim_id: UUID
    evidence_id: str
    host_id: UUID
    expires_at: datetime


def issue_deletion_ticket(
    *,
    claim_id: UUID,
    evidence_id: str,
    host_id: UUID,
    secret: str,
    expires_at: datetime,
) -> str:
    """签发绑定认领、证据与来源主机的 HMAC 短票据。"""
    payload = json.dumps(
        {
            "claim_id": str(claim_id),
            "evidence_id": evidence_id,
            "host_id": str(host_id),
            "expires_at": expires_at.isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(
        secret.encode(), EDGE_DELETION_TICKET_SCOPE + encoded.encode(), hashlib.sha256
    ).hexdigest()
    ticket = encoded + "." + signature
    if len(ticket) > _TICKET_LIMIT:
        raise EvidenceDeletionRefusedError(409, EvidenceDeletionReason.TICKET_INVALID)
    return ticket


def read_deletion_ticket(*, ticket: str, host_id: UUID, secret: str) -> DeletionTicket:
    """校验票据签名与来源主机；过期不在此拒绝，由调用方按 ``ticket_expired`` 解释。"""
    try:
        if len(ticket) > _TICKET_LIMIT:
            raise ValueError
        encoded, signature = ticket.split(".")
        expected = hmac.new(
            secret.encode(), EDGE_DELETION_TICKET_SCOPE + encoded.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if not isinstance(claims, dict) or set(claims) != {
            "claim_id",
            "evidence_id",
            "host_id",
            "expires_at",
        }:
            raise ValueError
        evidence_id = claims["evidence_id"]
        expires = claims["expires_at"]
        if not isinstance(evidence_id, str) or not evidence_id or not isinstance(expires, str):
            raise ValueError
        moment = datetime.fromisoformat(expires)
        if moment.tzinfo is None:
            raise ValueError
        parsed = DeletionTicket(
            claim_id=UUID(str(claims["claim_id"])),
            evidence_id=evidence_id,
            host_id=UUID(str(claims["host_id"])),
            expires_at=moment,
        )
        if parsed.host_id != host_id:
            raise ValueError
    except (ValueError, TypeError, UnicodeError) as error:
        raise EvidenceDeletionRefusedError(403, EvidenceDeletionReason.TICKET_INVALID) from error
    return parsed


def claim_evidence_deletion(
    *,
    host_id: UUID,
    config_revision: int,
    effective_sha256: str,
    candidates: tuple[EvidenceDeletionCandidate, ...],
    evidence: EvidenceDeletionGateway,
    monitor: MonitorImpactGateway,
    stations: StationFactsGateway,
    sources: EdgeImpactSourcesGateway,
    policies: RetentionPolicyRepository,
    overrides: StationRetentionOverrideRepository,
    now: datetime,
    ticket_ttl_seconds: int | None,
    secret: str,
) -> EvidenceDeletionResponse:
    """逐条判定删除资格，并为 ``eligible`` 的引用登记删除认领与短期票据；不删除任何媒体。"""
    if ticket_ttl_seconds is None or ticket_ttl_seconds <= 0:
        # 没有显式配置的票据有效期就没有删除资格：中心不提供业务默认值（§5.19）。
        raise EvidenceDeletionRefusedError(409, EvidenceDeletionReason.TICKET_TTL_UNCONFIGURED)
    _require_current_configuration(
        host_id=host_id,
        config_revision=config_revision,
        effective_sha256=effective_sha256,
        sources=sources,
    )
    references = {
        value.evidence_id: value
        for value in evidence.references(
            evidence_ids=tuple(candidate.evidence_id for candidate in candidates), now=now
        )
    }
    # 策略行的共享锁与策略写入真实互斥：判定所依据的策略版本不会在登记认领之前改变。
    default_state = policies.lock_current()
    default_policy = DEFAULT_RETENTION_POLICY if default_state is None else default_state.policy
    default_revision = 0 if default_state is None else default_state.revision
    for station_id in sorted({value.station_id for value in references.values()}):
        overrides.lock_current(station_id)
    scope = _DeletionScope(
        host_id=host_id,
        now=now,
        evidence=evidence,
        monitor=monitor,
        stations=stations,
        overrides=overrides,
        default_policy=default_policy,
        default_revision=default_revision,
        references=tuple(references.values()),
    )
    deadline = now + timedelta(seconds=ticket_ttl_seconds)
    entries: dict[str, EvidenceDeletionEntry] = {}
    # 锁定引用行的顺序固定为按证据 ID 升序：并发批次因此不会互相持锁等待。
    for candidate in sorted(candidates, key=lambda value: value.evidence_id):
        reference = references.get(candidate.evidence_id)
        if reference is None:
            entries[candidate.evidence_id] = _entry(
                candidate=candidate,
                decision=EvidenceDeletionDecision.UNKNOWN,
                reason=EvidenceDeletionReason.EVIDENCE_UNREGISTERED,
            )
            continue
        policy, station_revision = scope.station_policy(reference.station_id)
        decision, reason = scope.decide(candidate, reference, policy=policy)
        if decision is not EvidenceDeletionDecision.ELIGIBLE:
            entries[candidate.evidence_id] = scope.entry(candidate, reference, decision, reason)
            continue
        claim_id = evidence.claim(
            evidence_id=candidate.evidence_id,
            host_id=host_id,
            config_revision=config_revision,
            effective_sha256=effective_sha256,
            default_revision=default_revision,
            station_revision=station_revision,
            created_at=now,
            expires_at=deadline,
        )
        if claim_id is None:
            # 认领被拒（并发认领或锁内新出现的保护）：用最新事实重新判定并给出原因。
            decision, reason = scope.recheck(candidate)
            entries[candidate.evidence_id] = scope.entry(candidate, reference, decision, reason)
            continue
        entries[candidate.evidence_id] = scope.entry(
            candidate,
            reference,
            EvidenceDeletionDecision.ELIGIBLE,
            reason,
            claim_id=claim_id,
            ticket=issue_deletion_ticket(
                claim_id=claim_id,
                evidence_id=candidate.evidence_id,
                host_id=host_id,
                secret=secret,
                expires_at=deadline,
            ),
            expires_at=deadline,
        )
    return EvidenceDeletionResponse(
        host_id=str(host_id),
        config_revision=config_revision,
        effective_sha256=effective_sha256.lower(),
        ticket_ttl_seconds=ticket_ttl_seconds,
        entries=tuple(entries[candidate.evidence_id] for candidate in candidates),
    )


def verify_evidence_deletion(
    *,
    host_id: UUID,
    ticket: str,
    config_revision: int,
    effective_sha256: str,
    candidate: EvidenceDeletionCandidate,
    evidence: EvidenceDeletionGateway,
    monitor: MonitorImpactGateway,
    stations: StationFactsGateway,
    sources: EdgeImpactSourcesGateway,
    policies: RetentionPolicyRepository,
    overrides: StationRetentionOverrideRepository,
    now: datetime,
    ticket_ttl_seconds: int | None,
    secret: str,
) -> EvidenceDeletionVerifyResponse:
    """删除前重新校验同一条引用，并把仍在的事实推进到已授权阶段。

    权威锁最先取且与策略/工位/配置写入共用：一旦授权提交，权威变更会被 ``ensure_mutable`` 拒绝，
    直到来源机终局反馈。没有配置删除票据有效期时不产生新的授权；已授权认领仍可复核以完成已有
    删除意图（``result`` 始终接受）。
    """
    parsed = read_deletion_ticket(ticket=ticket, host_id=host_id, secret=secret)
    if parsed.evidence_id != candidate.evidence_id:
        raise EvidenceDeletionRefusedError(403, EvidenceDeletionReason.TICKET_INVALID)
    # 先取权威锁再读判定事实：并发的策略/工位/签发写入因此与“读判定 + 写授权”真串行。
    evidence.lock_authority()
    state = evidence.claim_state(parsed.claim_id, now=now)
    if state is None:
        raise EvidenceDeletionRefusedError(404, EvidenceDeletionReason.CLAIM_UNKNOWN)
    if state.host_id != host_id:
        raise EvidenceDeletionRefusedError(403, EvidenceDeletionReason.CLAIM_HOST_MISMATCH)
    if state.evidence_id != candidate.evidence_id:
        raise EvidenceDeletionRefusedError(409, EvidenceDeletionReason.CLAIM_IDENTITY_MISMATCH)
    if ticket_ttl_seconds is None and state.authorized_at is None:
        # 缺失有效期就不再产生新的删除授权；已授权的旧清理仍可复核并结案。
        raise EvidenceDeletionRefusedError(409, EvidenceDeletionReason.TICKET_TTL_UNCONFIGURED)
    _require_current_configuration(
        host_id=host_id,
        config_revision=config_revision,
        effective_sha256=effective_sha256,
        sources=sources,
    )
    if state.status is not EvidenceDeletionClaimStatus.PENDING:
        return _verify_response(
            state, EvidenceDeletionDecision.UNKNOWN, EvidenceDeletionReason.CLAIM_SETTLED
        )
    default_state = policies.lock_current()
    default_policy = DEFAULT_RETENTION_POLICY if default_state is None else default_state.policy
    default_revision = 0 if default_state is None else default_state.revision
    overrides.lock_current(state.reference.station_id)
    scope = _DeletionScope(
        host_id=host_id,
        now=now,
        evidence=evidence,
        monitor=monitor,
        stations=stations,
        overrides=overrides,
        default_policy=default_policy,
        default_revision=default_revision,
        references=(state.reference,),
        verifying_claim_id=state.claim_id,
    )
    policy, station_revision = scope.station_policy(state.reference.station_id)
    if (
        state.config_revision != config_revision
        or state.effective_sha256.lower() != effective_sha256.lower()
    ):
        return _verify_response(
            state, EvidenceDeletionDecision.UNKNOWN, EvidenceDeletionReason.CONFIG_CHANGED
        )
    if state.default_revision != default_revision or state.station_revision != station_revision:
        return _verify_response(
            state, EvidenceDeletionDecision.UNKNOWN, EvidenceDeletionReason.POLICY_CHANGED
        )
    if now >= parsed.expires_at:
        # 票据过期只让这张票据无效：认领仍在，引用继续受保护，也不会自动允许新的引用。
        return _verify_response(
            state, EvidenceDeletionDecision.UNKNOWN, EvidenceDeletionReason.TICKET_EXPIRED
        )
    decision, reason = scope.decide(candidate, state.reference, policy=policy)
    if decision is EvidenceDeletionDecision.ELIGIBLE and not evidence.authorize(
        parsed.claim_id, at=now
    ):
        # 并发终局把认领结掉了：不得再放行删除，按已结案解释。
        current = evidence.claim_state(parsed.claim_id, now=now)
        return _verify_response(
            current if current is not None else state,
            EvidenceDeletionDecision.UNKNOWN,
            EvidenceDeletionReason.CLAIM_SETTLED,
        )
    return _verify_response(state, decision, reason)


def settle_evidence_deletion(
    *,
    host_id: UUID,
    claim_id: UUID,
    evidence_id: str,
    outcome: EvidenceDeletionOutcome,
    reason_code: str,
    detail: str | None,
    evidence: EvidenceDeletionGateway,
    now: datetime,
) -> EvidenceDeletionResult:
    """把来源机的终局反馈结案为认领状态；重复同一反馈按同一状态幂等返回。"""
    state = evidence.claim_state(claim_id, now=now)
    if state is None:
        raise EvidenceDeletionRefusedError(404, EvidenceDeletionReason.CLAIM_UNKNOWN)
    if state.host_id != host_id:
        raise EvidenceDeletionRefusedError(403, EvidenceDeletionReason.CLAIM_HOST_MISMATCH)
    if state.evidence_id != evidence_id:
        raise EvidenceDeletionRefusedError(409, EvidenceDeletionReason.CLAIM_IDENTITY_MISMATCH)
    target = _OUTCOME_STATUS[outcome]
    if state.status is target:
        return _result(state, reason_code)
    if state.status is not EvidenceDeletionClaimStatus.PENDING:
        raise EvidenceDeletionRefusedError(409, EvidenceDeletionReason.CLAIM_ALREADY_SETTLED)
    settled = evidence.settle(
        claim_id=claim_id,
        status=target,
        reason_code=reason_code,
        detail=detail,
        settled_at=now,
    )
    if settled is None:
        current = evidence.claim_state(claim_id, now=now)
        if current is not None and current.status is target:
            return _result(current, reason_code)
        raise EvidenceDeletionRefusedError(409, EvidenceDeletionReason.CLAIM_ALREADY_SETTLED)
    return _result(settled, reason_code)


class _DeletionScope:
    """一次资格判定的事实读取与缓存；只读 owner seam，不写任何状态。"""

    def __init__(
        self,
        *,
        host_id: UUID,
        now: datetime,
        evidence: EvidenceDeletionGateway,
        monitor: MonitorImpactGateway,
        stations: StationFactsGateway,
        overrides: StationRetentionOverrideRepository,
        default_policy: RetentionPolicy,
        default_revision: int,
        references: tuple[EvidenceDeletionReference, ...],
        verifying_claim_id: UUID | None = None,
    ) -> None:
        self._host_id = host_id
        self._now = now
        # 正在被校验的认领不是竞争认领：它自己不该让本次判定变成"有未结案认领"。
        self._verifying_claim_id = verifying_claim_id
        self._evidence = evidence
        self._stations = stations
        self._overrides = overrides
        self._default_policy = default_policy
        self._default_revision = default_revision
        self._presence: dict[UUID, RetentionStationPresence] = {}
        self._policies: dict[UUID, tuple[RetentionPolicy, int]] = {}
        wanted = tuple(
            sorted((value.host_id, value.station_id, value.instance_id) for value in references)
        )
        self._instances = {
            (value.host_id, value.station_id, value.instance_id): value
            for value in (() if not wanted else monitor.instance_facts(instance_ids=wanted))
        }

    def station_presence(self, station_id: UUID) -> RetentionStationPresence:
        if station_id not in self._presence:
            self._presence[station_id] = self._stations.station_presence(station_id)
        return self._presence[station_id]

    def station_policy(self, station_id: UUID) -> tuple[RetentionPolicy, int]:
        if station_id not in self._policies:
            record = self._overrides.get(station_id)
            mode = RetentionPolicyMode.FOLLOW_GLOBAL if record is None else record.mode
            custom = None if record is None else record.overrides
            self._policies[station_id] = (
                resolve_retention_policy(self._default_policy, mode=mode, overrides=custom),
                0 if record is None else record.revision,
            )
        return self._policies[station_id]

    def facts(
        self, candidate: EvidenceDeletionCandidate, reference: EvidenceDeletionReference
    ) -> EvidenceDeletionFacts:
        """按 owner 事实组装一条引用的资格事实；任何缺项都表达为未知。"""
        instance = self._instances.get(
            (reference.host_id, reference.station_id, reference.instance_id)
        )
        latched = instance is not None and instance.has_latched_violation
        category: RetentionCategory | None = None
        if instance is not None and not latched and instance.verdict is not None:
            try:
                category = category_for(instance.verdict, has_latched_violation=False)
            except ValueError:
                category = None
        presence = self.station_presence(reference.station_id)
        age = (
            None
            if reference.received_at is None
            else max(0.0, (self._now - reference.received_at).total_seconds())
        )
        return EvidenceDeletionFacts(
            registered=True,
            media_state=reference.media_state,
            source_matches=reference.host_id == self._host_id,
            generation_matches=reference.generation == candidate.generation,
            material_matches=(
                reference.sha256 is not None
                and reference.sha256.lower() == candidate.sha256.lower()
                and reference.size == candidate.size
                and reference.reference == candidate.reference
            ),
            review_state_known=reference.review_state_known,
            review_active=reference.review_active,
            reclip_pending=reference.reclip_pending,
            playback_active=reference.playback_active,
            claim_pending=(
                reference.pending_claim_id is not None
                and reference.pending_claim_id != self._verifying_claim_id
            ),
            station_known=presence is not RetentionStationPresence.MISSING,
            station_active=presence is RetentionStationPresence.ACTIVE,
            instance_known=instance is not None,
            instance_closed=instance is not None and instance.closed,
            latched_violation=latched,
            category=category,
            age_seconds=age,
        )

    def decide(
        self,
        candidate: EvidenceDeletionCandidate,
        reference: EvidenceDeletionReference,
        *,
        policy: RetentionPolicy,
    ) -> tuple[EvidenceDeletionDecision, EvidenceDeletionReason]:
        facts = self.facts(candidate, reference)
        return evidence_deletion_decision(
            facts, rule=evidence_retention_rule(policy, facts.category)
        )

    def recheck(
        self, candidate: EvidenceDeletionCandidate
    ) -> tuple[EvidenceDeletionDecision, EvidenceDeletionReason]:
        """认领被拒后用最新事实重新判定；仍可删时说明是并发认领先到。"""
        fresh = self._evidence.references(evidence_ids=(candidate.evidence_id,), now=self._now)
        if not fresh:
            return EvidenceDeletionDecision.UNKNOWN, EvidenceDeletionReason.EVIDENCE_UNREGISTERED
        reference = fresh[0]
        policy, _ = self.station_policy(reference.station_id)
        decision, reason = self.decide(candidate, reference, policy=policy)
        if decision is EvidenceDeletionDecision.ELIGIBLE:
            return (
                EvidenceDeletionDecision.PROTECTED,
                EvidenceDeletionReason.DELETION_CLAIM_PENDING,
            )
        return decision, reason

    def entry(
        self,
        candidate: EvidenceDeletionCandidate,
        reference: EvidenceDeletionReference,
        decision: EvidenceDeletionDecision,
        reason: EvidenceDeletionReason,
        *,
        claim_id: UUID | None = None,
        ticket: str | None = None,
        expires_at: datetime | None = None,
    ) -> EvidenceDeletionEntry:
        policy, _ = self.station_policy(reference.station_id)
        facts = self.facts(candidate, reference)
        rule = evidence_retention_rule(policy, facts.category)
        return _entry(
            candidate=candidate,
            decision=decision,
            reason=reason,
            station_id=reference.station_id,
            instance_id=reference.instance_id,
            category=facts.category,
            threshold_seconds=None if rule is None else rule.seconds,
            received_at=reference.received_at,
            age_seconds=facts.age_seconds,
            claim_id=claim_id,
            ticket=ticket,
            expires_at=expires_at,
        )


def _result(state: EvidenceDeletionClaimState, reason_code: str) -> EvidenceDeletionResult:
    return EvidenceDeletionResult(
        claim_id=str(state.claim_id),
        evidence_id=state.evidence_id,
        status=state.status,
        reason_code=state.reason_code or reason_code,
    )


def _verify_response(
    state: EvidenceDeletionClaimState,
    decision: EvidenceDeletionDecision,
    reason: EvidenceDeletionReason,
) -> EvidenceDeletionVerifyResponse:
    return EvidenceDeletionVerifyResponse(
        claim_id=str(state.claim_id),
        evidence_id=state.evidence_id,
        decision=decision,
        reason=reason.value,
        expires_at=None if decision is not EvidenceDeletionDecision.ELIGIBLE else state.expires_at,
    )


def _entry(
    *,
    candidate: EvidenceDeletionCandidate,
    decision: EvidenceDeletionDecision,
    reason: EvidenceDeletionReason,
    station_id: UUID | None = None,
    instance_id: int | None = None,
    category: RetentionCategory | None = None,
    threshold_seconds: int | None = None,
    received_at: datetime | None = None,
    age_seconds: float | None = None,
    claim_id: UUID | None = None,
    ticket: str | None = None,
    expires_at: datetime | None = None,
) -> EvidenceDeletionEntry:
    return EvidenceDeletionEntry(
        evidence_id=candidate.evidence_id,
        station_id=None if station_id is None else str(station_id),
        instance_id=instance_id,
        generation=candidate.generation,
        decision=decision,
        reason=reason.value,
        category=None if category is None else category.value,
        ticket=ticket,
        claim_id=None if claim_id is None else str(claim_id),
        expires_at=expires_at,
        received_at=received_at,
        age_seconds=age_seconds,
        threshold_seconds=threshold_seconds,
    )


def _require_current_configuration(
    *,
    host_id: UUID,
    config_revision: int,
    effective_sha256: str,
    sources: EdgeImpactSourcesGateway,
) -> None:
    """来源机提交的配置身份必须等于中心当前记录的配置，而不是某次历史签发。

    读取前先取该主机行的共享锁：配置分配修订推进与删除认领登记因此真正互斥，判定所依据的配置
    身份不会被并发改写。锁序固定为"主机行 → 策略行 → 引用行"，认领与校验都按同一顺序取锁。
    """
    if not sources.lock_configuration(host_id=host_id):
        raise EvidenceDeletionRefusedError(403, EvidenceDeletionReason.SOURCE_HOST_UNREGISTERED)
    for source in sources.sources(station_id=None):
        if source.host_id != host_id:
            continue
        if (
            source.configuration_revision is None
            or source.configuration_sha256 is None
            or source.configuration_revision != config_revision
            or source.configuration_sha256.lower() != effective_sha256.lower()
        ):
            raise EvidenceDeletionRefusedError(409, EvidenceDeletionReason.CONFIG_NOT_CURRENT)
        return
    raise EvidenceDeletionRefusedError(403, EvidenceDeletionReason.SOURCE_HOST_UNREGISTERED)


__all__ = [
    "EDGE_DELETION_TICKET_SCOPE",
    "DeletionTicket",
    "EvidenceDeletionRefusedError",
    "EvidenceDeletionResult",
    "claim_evidence_deletion",
    "issue_deletion_ticket",
    "read_deletion_ticket",
    "settle_evidence_deletion",
    "verify_evidence_deletion",
]
