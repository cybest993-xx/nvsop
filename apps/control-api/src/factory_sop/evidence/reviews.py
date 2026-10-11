"""人工复核结论的登记与查询：只追加，不改写机器判定、证据或他人记录（§六）。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.evidence.model import EvidenceReview, EvidenceStatus, ReviewConclusion
from factory_sop.evidence.repository import EvidenceRepository, EvidenceReviewRepository


class EvidenceReviewRefusedError(ValueError):
    """复核请求被拒绝；被拒绝的请求不产生任何持久化事实。"""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


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
    时间；不同内容或作者返回 409，不覆盖已存结论；不同 ID 一律追加。
    """
    authorize(caller, Permission.EVIDENCE_VIEW)
    authorize(caller, Permission.EVIDENCE_REVIEW_EDIT)
    reference = evidence.find(evidence_id)
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
        return existing
    raise EvidenceReviewRefusedError(409, "review_conflict")


def list_reviews(
    caller: Caller, *, evidence_id: str, reviews: EvidenceReviewRepository
) -> tuple[EvidenceReview, ...]:
    """返回该证据的全部复核结论；查询复用查看权限，不隐含编辑。"""
    authorize(caller, Permission.EVIDENCE_VIEW)
    return reviews.list_for_evidence(evidence_id)


__all__ = ["EvidenceReviewRefusedError", "list_reviews", "save_review"]
