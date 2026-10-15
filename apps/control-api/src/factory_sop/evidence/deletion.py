"""evidence 对删除资格的 owning 实现：引用与保护事实、删除认领与来源机反馈（#206）。

retention 拥有"这条引用现在是否可以删除"的判定与保留策略；这里只提供事实、登记认领，并把来源机
的终局反馈结案。所有写入都在同一请求事务内完成，锁序固定为"先 ``evidence_evidence`` 行，再认领
表"，与引用写入者一致，因此认领创建与复核/再切片/播放/重新登记只会串行。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from factory_sop.evidence.model import (
    EvidenceDeletionClaim,
    EvidenceReference,
    EvidenceStatus,
    ReclipRequestStatus,
)
from factory_sop.evidence.repository import (
    EvidenceDeletionClaimRepository,
    EvidenceReclipRequestRepository,
    EvidenceRepository,
    EvidenceReviewRepository,
)
from factory_sop.identifiers import new_id
from factory_sop.retention.api import (
    DeletionAuthorityBusyError,
    DeletionAuthorityFence,
    EvidenceDeletionClaimState,
    EvidenceDeletionGateway,
    EvidenceDeletionReference,
    EvidenceMediaState,
)
from nvsop_contracts import EvidenceDeletionClaimStatus

_MEDIA_STATE: dict[EvidenceStatus, EvidenceMediaState] = {
    EvidenceStatus.AVAILABLE: EvidenceMediaState.AVAILABLE,
    EvidenceStatus.PENDING: EvidenceMediaState.PENDING,
    EvidenceStatus.FAILED: EvidenceMediaState.FAILED,
    EvidenceStatus.DELETED: EvidenceMediaState.DELETED,
}


class RepositoryEvidenceDeletionGateway(EvidenceDeletionGateway, DeletionAuthorityFence):
    """把 evidence 的引用、复核、再切片、播放与认领事实接到 retention 的定义；不读 retention 表。"""

    def __init__(
        self,
        *,
        evidence: EvidenceRepository,
        reviews: EvidenceReviewRepository,
        reclips: EvidenceReclipRequestRepository,
        claims: EvidenceDeletionClaimRepository,
    ) -> None:
        self._evidence = evidence
        self._reviews = reviews
        self._reclips = reclips
        self._claims = claims

    def references(
        self, *, evidence_ids: tuple[str, ...], now: datetime
    ) -> tuple[EvidenceDeletionReference, ...]:
        values: list[EvidenceDeletionReference] = []
        for evidence_id in evidence_ids:
            reference = self._evidence.find(evidence_id)
            if reference is None:
                continue
            values.append(self._fact(reference, now=now))
        return tuple(values)

    def claim(
        self,
        *,
        evidence_id: str,
        host_id: UUID,
        config_revision: int,
        effective_sha256: str,
        default_revision: int,
        station_revision: int,
        created_at: datetime,
        expires_at: datetime,
    ) -> UUID | None:
        # 锁引用行后再插入认领：未结案认领按证据唯一，插入冲突同样返回 None。
        locked = self._evidence.lock(evidence_id)
        reference = locked.reference
        if reference is None or locked.pending_claim:
            return None
        if self._protected(reference, now=created_at):
            # 锁内出现新的 evidence 保护事实（复核、再切片、播放或媒体已不可用）。
            return None
        value = EvidenceDeletionClaim(
            claim_id=new_id(),
            evidence_id=evidence_id,
            host_id=host_id,
            config_revision=config_revision,
            effective_sha256=effective_sha256.lower(),
            default_revision=default_revision,
            station_revision=station_revision,
            status=EvidenceDeletionClaimStatus.PENDING,
            created_at=created_at,
            expires_at=expires_at,
        )
        if not self._claims.insert_if_no_pending(value):
            return None
        return value.claim_id

    def claim_state(self, claim_id: UUID, *, now: datetime) -> EvidenceDeletionClaimState | None:
        claim = self._claims.find(claim_id)
        if claim is None:
            return None
        reference = self._evidence.find(claim.evidence_id)
        if reference is None:
            return None
        return EvidenceDeletionClaimState(
            claim_id=claim.claim_id,
            evidence_id=claim.evidence_id,
            host_id=claim.host_id,
            config_revision=claim.config_revision,
            effective_sha256=claim.effective_sha256,
            default_revision=claim.default_revision,
            station_revision=claim.station_revision,
            status=claim.status,
            created_at=claim.created_at,
            expires_at=claim.expires_at,
            settled_at=claim.settled_at,
            authorized_at=claim.authorized_at,
            reason_code=claim.reason_code,
            reference=self._fact(reference, now=now),
        )

    def lock_authority(self) -> None:
        """把事务级权威锁接到 evidence 自己的认领仓储；调用方在锁内读判定事实。"""
        self._claims.lock_authority()

    def authorize(self, claim_id: UUID, *, at: datetime) -> bool:
        return self._claims.authorize(claim_id, at=at)

    def replay_pending(
        self,
        *,
        evidence_id: str,
        host_id: UUID,
        config_revision: int,
        effective_sha256: str,
        default_revision: int,
        station_revision: int,
        expires_at: datetime,
        now: datetime,
    ) -> UUID | None:
        """同一来源、同一绑定的本机未结案认领幂等重放；守卫后再确认一次保护事实。"""
        locked = self._evidence.lock(evidence_id)
        reference = locked.reference
        if reference is None:
            return None
        if self._protected(reference, now=now):
            return None
        pending = self._claims.find_pending(evidence_id)
        if pending is None:
            return None
        if (
            pending.host_id != host_id
            or pending.config_revision != config_revision
            or pending.effective_sha256.lower() != effective_sha256.lower()
            or pending.default_revision != default_revision
            or pending.station_revision != station_revision
        ):
            return None
        if not self._claims.refresh_deadline(pending.claim_id, expires_at=expires_at):
            return None
        return pending.claim_id

    def ensure_mutable(self) -> None:
        """取权威锁并拒绝在已授权未结案删除期间改写策略/工位/签发事实。"""
        self._claims.lock_authority()
        if self._claims.has_authorized_pending():
            raise DeletionAuthorityBusyError(
                "an authorized evidence deletion is awaiting the source host result"
            )

    def settle(
        self,
        *,
        claim_id: UUID,
        status: EvidenceDeletionClaimStatus,
        reason_code: str,
        detail: str | None,
        settled_at: datetime,
    ) -> EvidenceDeletionClaimState | None:
        claim = self._claims.find(claim_id)
        if claim is None or claim.status is not EvidenceDeletionClaimStatus.PENDING:
            return None
        # 与认领创建同序：先锁引用行，再结案认领；删除反馈在同一事务内标记媒体不可用。
        locked = self._evidence.lock(claim.evidence_id)
        if locked.reference is None:
            return None
        if status is EvidenceDeletionClaimStatus.DELETED and not self._evidence.mark_media_deleted(
            claim.evidence_id
        ):
            return None
        if status is EvidenceDeletionClaimStatus.MISSING and not self._evidence.mark_media_missing(
            claim.evidence_id
        ):
            # 无法把媒体落为不可用（未知证据）：不得把认领结案成可播放的假象。
            return None
        if not self._claims.settle(
            claim_id,
            status=status,
            reason_code=reason_code,
            detail=detail,
            settled_at=settled_at,
        ):
            return None
        return self.claim_state(claim_id, now=settled_at)

    def _fact(self, reference: EvidenceReference, *, now: datetime) -> EvidenceDeletionReference:
        """组装一条引用的当前事实；复核状态未知表达为 ``review_state_known=False``。"""
        registration = reference.registration
        reviewers = self._reviews.active_reviewers(evidence_id=reference.evidence_id)
        pending_claim = self._claims.find_pending(reference.evidence_id)
        reclips = self._reclips.list_for_evidence(reference.evidence_id)
        return EvidenceDeletionReference(
            evidence_id=reference.evidence_id,
            host_id=UUID(registration.host_id),
            station_id=UUID(registration.station_id),
            instance_id=registration.instance_id,
            generation=registration.generation,
            sha256=registration.sha256,
            size=registration.size,
            reference=registration.reference,
            media_state=_MEDIA_STATE.get(reference.status, EvidenceMediaState.UNKNOWN),
            received_at=reference.received_at,
            review_state_known=reviewers is not None,
            review_active=bool(reviewers),
            reclip_pending=any(value.status is ReclipRequestStatus.PENDING for value in reclips),
            playback_active=self._evidence.playback_active(reference.evidence_id, now=now),
            pending_claim_id=None if pending_claim is None else pending_claim.claim_id,
        )

    def _protected(self, reference: EvidenceReference, *, now: datetime) -> bool:
        """evidence 自己拥有的保护事实是否已经成立；成立即不登记新的删除认领。"""
        fact = self._fact(reference, now=now)
        return (
            fact.media_state is not EvidenceMediaState.AVAILABLE
            or not fact.review_state_known
            or fact.review_active
            or fact.reclip_pending
            or fact.playback_active
        )


__all__ = ["RepositoryEvidenceDeletionGateway"]
