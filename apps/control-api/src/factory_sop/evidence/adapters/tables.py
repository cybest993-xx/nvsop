"""evidence 中心引用登记表的 SQLAlchemy 行模型。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from factory_sop.evidence.model import (
    EvidenceDeletionClaim,
    EvidenceKind,
    EvidenceOrigin,
    EvidenceReclipRequest,
    EvidenceReference,
    EvidenceRegistration,
    EvidenceReview,
    EvidenceReviewHold,
    EvidenceStatus,
    ReclipRequestStatus,
    ReviewConclusion,
)
from factory_sop.persistence import Table
from nvsop_contracts import EvidenceDeletionClaimStatus


class EvidenceReferenceRow(Table):
    """一条中心证据引用：只存身份/元数据，不存媒体字节或对象存储键。"""

    __tablename__ = "evidence_evidence"

    evidence_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    host_id: Mapped[str] = mapped_column(String(128), index=True)
    station_id: Mapped[str] = mapped_column(String(128), index=True)
    instance_id: Mapped[int] = mapped_column(BigInteger(), index=True)
    violation_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    kind: Mapped[str] = mapped_column(String(32))
    origin: Mapped[str] = mapped_column(String(32))
    anchor: Mapped[float]
    window_start: Mapped[float]
    window_end: Mapped[float]
    generation: Mapped[str] = mapped_column(String(128))
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    reference: Mapped[str | None] = mapped_column(Text(), nullable=True)
    # 来源机冻结的录像墙钟映射与核心必需跨度；旧登记为 NULL（截止未知，不可再切片）。
    wall_offset: Mapped[float | None] = mapped_column(nullable=True)
    required_from: Mapped[float | None] = mapped_column(nullable=True)
    required_to: Mapped[float | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    failure_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    review_state_known: Mapped[bool] = mapped_column(default=True, nullable=False)

    def to_domain(self) -> EvidenceReference:
        return EvidenceReference(
            registration=EvidenceRegistration(
                evidence_id=self.evidence_id,
                host_id=self.host_id,
                station_id=self.station_id,
                instance_id=self.instance_id,
                violation_id=self.violation_id,
                kind=EvidenceKind(self.kind),
                origin=EvidenceOrigin(self.origin),
                anchor=self.anchor,
                window_start=self.window_start,
                window_end=self.window_end,
                generation=self.generation,
                sha256=self.sha256,
                size=self.size,
                reference=self.reference,
                wall_offset=self.wall_offset,
                required_from=self.required_from,
                required_to=self.required_to,
            ),
            status=EvidenceStatus(self.status),
            failure_reason=self.failure_reason,
            received_at=self.received_at,
        )

    @classmethod
    def from_domain(cls, value: EvidenceReference) -> EvidenceReferenceRow:
        registration = value.registration
        return cls(
            evidence_id=registration.evidence_id,
            host_id=registration.host_id,
            station_id=registration.station_id,
            instance_id=registration.instance_id,
            violation_id=registration.violation_id,
            kind=registration.kind.value,
            origin=registration.origin.value,
            anchor=registration.anchor,
            window_start=registration.window_start,
            window_end=registration.window_end,
            generation=registration.generation,
            sha256=registration.sha256,
            size=registration.size,
            reference=registration.reference,
            wall_offset=registration.wall_offset,
            required_from=registration.required_from,
            required_to=registration.required_to,
            status=value.status.value,
            failure_reason=value.failure_reason,
            received_at=value.received_at,
            review_state_known=True,
        )


class EvidenceReviewRow(Table):
    """一条人工复核结论：只追加，不存媒体字节，也不直接引用 auth/monitor 表。"""

    __tablename__ = "evidence_review"

    review_id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("evidence_evidence.evidence_id", ondelete="CASCADE"), index=True
    )
    generation: Mapped[str] = mapped_column(String(128))
    reviewer_id: Mapped[UUID] = mapped_column(Uuid())
    reviewer_name: Mapped[str] = mapped_column(String(255))
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    conclusion: Mapped[str] = mapped_column(String(32))

    def to_domain(self) -> EvidenceReview:
        return EvidenceReview(
            review_id=self.review_id,
            evidence_id=self.evidence_id,
            generation=self.generation,
            reviewer_id=self.reviewer_id,
            reviewer_name=self.reviewer_name,
            reviewed_at=self.reviewed_at,
            conclusion=ReviewConclusion(self.conclusion),
        )

    @classmethod
    def from_domain(cls, value: EvidenceReview) -> EvidenceReviewRow:
        return cls(
            review_id=value.review_id,
            evidence_id=value.evidence_id,
            generation=value.generation,
            reviewer_id=value.reviewer_id,
            reviewer_name=value.reviewer_name,
            reviewed_at=value.reviewed_at,
            conclusion=value.conclusion.value,
        )


class EvidenceReviewSessionRow(Table):
    """evidence 的复核生命周期占用；结论仍单独保存在只追加表。"""

    __tablename__ = "evidence_review_session"

    review_id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("evidence_evidence.evidence_id", ondelete="CASCADE"), index=True
    )
    generation: Mapped[str] = mapped_column(String(128))
    reviewer_id: Mapped[UUID] = mapped_column(Uuid(), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_domain(self) -> EvidenceReviewHold:
        return EvidenceReviewHold(
            review_id=self.review_id,
            evidence_id=self.evidence_id,
            generation=self.generation,
            reviewer_id=self.reviewer_id,
            started_at=self.started_at,
            ended_at=self.ended_at,
        )


class EvidenceDeletionClaimRow(Table):
    """一条删除认领：未结案期间该引用受保护，只有来源机的终局反馈才释放它。

    认领绑定来源分配（``config_revision``/``effective_sha256``）与保留策略版本
    （``default_revision``/``station_revision``），使校验阶段能发现期间发生的策略或配置变化。
    """

    __tablename__ = "evidence_deletion_claim"

    claim_id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("evidence_evidence.evidence_id", ondelete="CASCADE"), index=True
    )
    host_id: Mapped[UUID] = mapped_column(Uuid(), index=True)
    config_revision: Mapped[int] = mapped_column(Integer())
    effective_sha256: Mapped[str] = mapped_column(String(64))
    default_revision: Mapped[int] = mapped_column(Integer())
    station_revision: Mapped[int] = mapped_column(Integer())
    status: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[str | None] = mapped_column(String(255), nullable=True)

    def to_domain(self) -> EvidenceDeletionClaim:
        return EvidenceDeletionClaim(
            claim_id=self.claim_id,
            evidence_id=self.evidence_id,
            host_id=self.host_id,
            config_revision=self.config_revision,
            effective_sha256=self.effective_sha256,
            default_revision=self.default_revision,
            station_revision=self.station_revision,
            status=EvidenceDeletionClaimStatus(self.status),
            created_at=self.created_at,
            expires_at=self.expires_at,
            settled_at=self.settled_at,
            authorized_at=self.authorized_at,
            reason_code=self.reason_code,
            detail=self.detail,
        )

    @classmethod
    def from_domain(cls, value: EvidenceDeletionClaim) -> EvidenceDeletionClaimRow:
        return cls(
            claim_id=value.claim_id,
            evidence_id=value.evidence_id,
            host_id=value.host_id,
            config_revision=value.config_revision,
            effective_sha256=value.effective_sha256,
            default_revision=value.default_revision,
            station_revision=value.station_revision,
            status=value.status.value,
            created_at=value.created_at,
            expires_at=value.expires_at,
            settled_at=value.settled_at,
            authorized_at=value.authorized_at,
            reason_code=value.reason_code,
            detail=value.detail,
        )


class EvidencePlaybackLeaseRow(Table):
    """一条引用上的短期播放占用；``expires_at`` 之前该证据不得被删除（§5.19 引用保护）。"""

    __tablename__ = "evidence_playback_lease"

    evidence_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("evidence_evidence.evidence_id", ondelete="CASCADE"),
        primary_key=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvidenceReclipRequestRow(Table):
    """一条再切片请求：锚点与来源主机冻结，命令交付归 device，本表不覆盖原证据。"""

    __tablename__ = "evidence_reclip_request"

    request_id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("evidence_evidence.evidence_id", ondelete="CASCADE"), index=True
    )
    generation: Mapped[str] = mapped_column(String(128))
    host_id: Mapped[UUID] = mapped_column(Uuid(), index=True)
    anchor: Mapped[float]
    before_seconds: Mapped[float]
    after_seconds: Mapped[float]
    window_start: Mapped[float]
    window_end: Mapped[float]
    requested_by: Mapped[UUID] = mapped_column(Uuid())
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    command_id: Mapped[UUID] = mapped_column(Uuid(), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    failure_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 成功追加后记录的新引用身份；未成功为 NULL。
    result_evidence_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    def to_domain(self) -> EvidenceReclipRequest:
        return EvidenceReclipRequest(
            request_id=self.request_id,
            evidence_id=self.evidence_id,
            generation=self.generation,
            host_id=self.host_id,
            anchor=self.anchor,
            before_seconds=self.before_seconds,
            after_seconds=self.after_seconds,
            window_start=self.window_start,
            window_end=self.window_end,
            requested_by=self.requested_by,
            requested_at=self.requested_at,
            command_id=self.command_id,
            status=ReclipRequestStatus(self.status),
            failure_reason=self.failure_reason,
            result_evidence_id=self.result_evidence_id,
        )

    @classmethod
    def from_domain(cls, value: EvidenceReclipRequest) -> EvidenceReclipRequestRow:
        return cls(
            request_id=value.request_id,
            evidence_id=value.evidence_id,
            generation=value.generation,
            host_id=value.host_id,
            anchor=value.anchor,
            before_seconds=value.before_seconds,
            after_seconds=value.after_seconds,
            window_start=value.window_start,
            window_end=value.window_end,
            requested_by=value.requested_by,
            requested_at=value.requested_at,
            command_id=value.command_id,
            status=value.status.value,
            failure_reason=value.failure_reason,
            result_evidence_id=value.result_evidence_id,
        )


__all__ = [
    "EvidenceDeletionClaimRow",
    "EvidencePlaybackLeaseRow",
    "EvidenceReclipRequestRow",
    "EvidenceReferenceRow",
    "EvidenceReviewRow",
    "EvidenceReviewSessionRow",
]
