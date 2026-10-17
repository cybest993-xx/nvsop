"""人工复核结论的登记与查询：只追加，不改写机器判定、证据或他人记录（§六）。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.evidence.model import (
    EvidenceReview,
    EvidenceReviewHold,
    EvidenceStatus,
    ReviewConclusion,
)
from factory_sop.evidence.repository import EvidenceRepository, EvidenceReviewRepository


class EvidenceReviewRefusedError(ValueError):
    """复核请求被拒绝；被拒绝的请求不产生任何持久化事实。"""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def begin_review(
    caller: Caller,
    *,
    review_id: UUID,
    evidence_id: str,
    generation: str,
    now: datetime,
    evidence: EvidenceRepository,
    reviews: EvidenceReviewRepository,
) -> EvidenceReviewHold:
    """开始明确复核；认领冲突、不可用或历史复核状态未知都 fail-closed。

    先锁定该引用行再读取认领与复核事实：锁序与删除认领的创建一致（先引用行，再保护状态），
    因此"开始复核"与"登记删除认领"只会串行，不会交错。
    """
    authorize(caller, Permission.EVIDENCE_VIEW)
    authorize(caller, Permission.EVIDENCE_REVIEW_EDIT)
    locked = evidence.lock(evidence_id)
    if locked.pending_claim:
        raise EvidenceReviewRefusedError(409, "evidence_deletion_claimed")
    reference = locked.reference
    if reference is None:
        raise EvidenceReviewRefusedError(404, "evidence_unregistered")
    if reference.registration.generation != generation:
        raise EvidenceReviewRefusedError(409, "evidence_generation_mismatch")
    if reference.status is not EvidenceStatus.AVAILABLE:
        raise EvidenceReviewRefusedError(410, "evidence_unavailable")
    # 历史行 review_state_known=false 只约束删除资格（未知即不删），不能阻止用户开启一次
    # 明确的新复核；保留既有结论也不受影响。
    hold = EvidenceReviewHold(review_id, evidence_id, generation, caller.user.id, now)
    if reviews.begin(hold):
        return hold
    existing = reviews.find_hold(review_id)
    if (
        existing is not None
        and existing.evidence_id == evidence_id
        and existing.generation == generation
        and existing.reviewer_id == caller.user.id
        and existing.ended_at is None
    ):
        # 同一 review_id 的重放返回原 hold 与原时刻，不产生第二个占用或新时间戳。
        return existing
    raise EvidenceReviewRefusedError(409, "review_conflict")


def cancel_review(
    caller: Caller, *, review_id: UUID, now: datetime, reviews: EvidenceReviewRepository
) -> None:
    """仅取消当前人的复核占用；不解除其他 reviewer 的占用，也不按时间自动释放。"""
    authorize(caller, Permission.EVIDENCE_VIEW)
    authorize(caller, Permission.EVIDENCE_REVIEW_EDIT)
    if not reviews.cancel(review_id=review_id, reviewer_id=caller.user.id, at=now):
        raise EvidenceReviewRefusedError(409, "review_not_owned_or_closed")


def save_review(
    caller: Caller,
    *,
    review_id: UUID,
    evidence_id: str,
    generation: str,
    conclusion: ReviewConclusion,
    reviewed_at: datetime,
    evidence: EvidenceRepository,
    reviews: EvidenceReviewRepository,
) -> EvidenceReview:
    """登记一条人工复核结论；按 ``review_id`` 幂等，冲突不覆盖。

    只接受已登记、代次一致且可用的证据。同 ID 同证据/代次/作者/结论的重放返回原记录及原
    时间；不同内容或作者返回 409，不覆盖已存结论；不同 ID 一律追加。提交结论同时结案提交者
    本人在这条证据上的在审占用，其他 reviewer 的占用不受影响，也不按时间自动释放。
    """
    authorize(caller, Permission.EVIDENCE_VIEW)
    authorize(caller, Permission.EVIDENCE_REVIEW_EDIT)
    locked = evidence.lock(evidence_id)
    if locked.pending_claim:
        raise EvidenceReviewRefusedError(409, "evidence_deletion_claimed")
    reference = locked.reference
    if reference is None:
        raise EvidenceReviewRefusedError(404, "evidence_unregistered")
    if reference.registration.generation != generation:
        raise EvidenceReviewRefusedError(409, "evidence_generation_mismatch")
    if reference.status is not EvidenceStatus.AVAILABLE:
        raise EvidenceReviewRefusedError(410, "evidence_unavailable")
    incoming = EvidenceReview(
        review_id=review_id,
        evidence_id=evidence_id,
        generation=generation,
        reviewer_id=caller.user.id,
        reviewer_name=caller.user.display_name,
        reviewed_at=reviewed_at,
        conclusion=conclusion,
    )
    if reviews.insert(incoming):
        reviews.conclude_own(evidence_id=evidence_id, reviewer_id=caller.user.id, at=reviewed_at)
        return incoming
    existing = reviews.find(review_id)
    if existing is None:
        raise RuntimeError("review insert conflicted without a visible row")
    if (
        existing.evidence_id == evidence_id
        and existing.generation == generation
        and existing.reviewer_id == caller.user.id
        and existing.conclusion is conclusion
    ):
        # 同一意图重放同样结案本人占用：重复提交不留残余在审状态。
        reviews.conclude_own(evidence_id=evidence_id, reviewer_id=caller.user.id, at=reviewed_at)
        return existing
    raise EvidenceReviewRefusedError(409, "review_conflict")


def list_reviews(
    caller: Caller, *, evidence_id: str, reviews: EvidenceReviewRepository
) -> tuple[EvidenceReview, ...]:
    """返回该证据的全部复核结论；查询复用查看权限，不隐含编辑。"""
    authorize(caller, Permission.EVIDENCE_VIEW)
    return reviews.list_for_evidence(evidence_id)


__all__ = [
    "EvidenceReviewRefusedError",
    "begin_review",
    "cancel_review",
    "list_reviews",
    "save_review",
]
