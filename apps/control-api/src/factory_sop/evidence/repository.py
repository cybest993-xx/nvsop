"""evidence 用例访问中心登记表的唯一仓储 seam；方法都不提交事务。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from factory_sop.evidence.model import (
    EvidenceDeletionClaim,
    EvidenceInstanceCount,
    EvidenceReclipRequest,
    EvidenceReference,
    EvidenceReferenceLock,
    EvidenceReview,
    EvidenceReviewHold,
    EvidenceReviewUsageCount,
    EvidenceStatus,
)
from nvsop_contracts import EvidenceDeletionClaimStatus


class EvidenceRepository(Protocol):
    """中心证据引用的读写 seam。"""

    def find(self, evidence_id: str) -> EvidenceReference | None:
        """按稳定证据 ID 读取一条登记，未登记时返回 ``None``。"""
        ...

    def insert(self, value: EvidenceReference) -> bool:
        """仅在证据 ID 尚未登记时插入；已有行时返回 ``False``，不覆盖。"""
        ...

    def replace_if_current(self, *, expected: EvidenceReference, value: EvidenceReference) -> bool:
        """仅在状态/材料仍等于 expected 时原子更新；CAS 失败返回 ``False``。"""
        ...

    def lock(self, evidence_id: str) -> EvidenceReferenceLock:
        """锁定该引用行（``SELECT ... FOR UPDATE``）并读取未结案删除认领事实。

        行不存在时引用为 ``None``；认领事实必须与行锁在同一事务内读取，引用写入者据此拒绝与
        删除认领冲突的写入（§5.19）。
        """
        ...

    def lock_playback(self, evidence_id: str, *, expires_at: datetime) -> EvidenceReferenceLock:
        """与 ``lock`` 同序锁定引用行，并在引用可用且无未结案认领时登记/续期播放占用。

        播放占用是一条引用上的短期保护事实：占用未到期时该证据不得被删除。
        """
        ...

    def playback_active(self, evidence_id: str, *, now: datetime) -> bool:
        """该引用当前是否存在未到期的播放占用；占用存在时它不是可删除候选。"""
        ...

    def mark_media_deleted(self, evidence_id: str) -> bool:
        """把一条已登记的引用标记为媒体不可用；保留身份、材料与复核历史。

        未知证据返回 ``False``；已是该状态时重放返回 ``True``，不产生第二个状态。
        """
        ...

    def mark_media_missing(self, evidence_id: str) -> bool:
        """把一条可用引用标记为“源文件确证不存在”；不是已证明的成功删除。

        未知证据返回 ``False``；已删除的行保持已删除并按不可用处理。
        """
        ...

    def page(
        self,
        *,
        page: int,
        page_size: int,
        status: EvidenceStatus | None = None,
        station_id: str | None = None,
        instance_id: int | None = None,
    ) -> tuple[tuple[EvidenceReference, ...], int]:
        """返回按登记时刻倒序的一页引用和总数。"""
        ...


class EvidenceReclipRequestRepository(Protocol):
    """再切片请求的只追加 seam；状态由 evidence 拥有，不提供删除。"""

    def insert(self, value: EvidenceReclipRequest) -> bool:
        """仅在 ``request_id`` 尚未存在时插入；已有行时返回 ``False``，不覆盖。"""
        ...

    def find(self, request_id: UUID) -> EvidenceReclipRequest | None:
        """按稳定请求 ID 读取一条请求，不存在时返回 ``None``。"""
        ...

    def find_by_command_id(self, command_id: UUID) -> EvidenceReclipRequest | None:
        """按委托命令 ID 读取对应请求，不存在时返回 ``None``。"""
        ...

    def list_for_evidence(self, evidence_id: str) -> tuple[EvidenceReclipRequest, ...]:
        """返回该证据的全部再切片请求，按请求时刻升序。"""
        ...

    def has_for(
        self,
        *,
        host_id: UUID,
        station_id: str,
        instance_id: int,
        anchor: float,
        window_start: float,
        window_end: float,
    ) -> bool:
        """是否存在该来源主机/工位/实例/锚点/窗口的真实再切片请求，用于登记来源关联校验。"""
        ...

    def mark_succeeded(self, request_id: UUID, *, result_evidence_id: str) -> bool:
        """仅在仍为 ``PENDING`` 时原子转为 ``SUCCEEDED`` 并记录新引用；CAS 失败返回 ``False``。"""
        ...

    def mark_failed(self, request_id: UUID, *, failure_reason: str) -> bool:
        """仅在仍为 ``PENDING`` 时原子转为 ``FAILED`` 并记录可查询原因；CAS 失败返回 ``False``。

        失败结案不覆盖已成功或已失败的终态, 也不触碰原证据与复核记录。
        """
        ...


class EvidenceReviewRepository(Protocol):
    """人工复核结论与在审生命周期的 evidence owning seam。"""

    def begin(self, value: EvidenceReviewHold) -> bool:
        """锁定证据身份后登记本人在审 hold；已存在相同 review_id 时不覆盖。"""
        ...

    def find_hold(self, review_id: UUID) -> EvidenceReviewHold | None:
        """按 review_id 读取在审 hold；不存在时返回 ``None``。"""
        ...

    def cancel(self, *, review_id: UUID, reviewer_id: UUID, at: datetime) -> bool:
        """仅结束指定 reviewer 自己的 hold；不存在/已结束/非本人返回 False。"""
        ...

    def active_reviewers(self, *, evidence_id: str) -> frozenset[UUID] | None:
        """返回活动 reviewer；历史状态未知返回 None，不能当作空集合。"""
        ...

    def insert(self, value: EvidenceReview) -> bool:
        """仅在 ``review_id`` 尚未存在时插入；已有行时返回 ``False``，不覆盖。"""
        ...

    def find(self, review_id: UUID) -> EvidenceReview | None:
        """按稳定复核 ID 读取一条结论，不存在时返回 ``None``。"""
        ...

    def list_for_evidence(self, evidence_id: str) -> tuple[EvidenceReview, ...]:
        """返回该证据的全部复核结论，按复核时刻升序；不删除他人记录。"""
        ...

    def conclude_own(self, *, evidence_id: str, reviewer_id: UUID, at: datetime) -> int:
        """结案该 reviewer 在这条证据上的全部在审占用并返回条数；不触碰他人占用。"""
        ...


class EvidenceDeletionClaimRepository(Protocol):
    """删除认领的 evidence owning seam；未结案认领是持久保护状态，不随票据过期自动释放。"""

    def find(self, claim_id: UUID) -> EvidenceDeletionClaim | None:
        """按稳定认领 ID 读取一条认领，不存在时返回 ``None``。"""
        ...

    def find_pending(self, evidence_id: str) -> EvidenceDeletionClaim | None:
        """该证据当前未结案的删除认领；没有时返回 ``None``。"""
        ...

    def insert_if_no_pending(self, value: EvidenceDeletionClaim) -> bool:
        """仅在该证据尚无未结案认领时插入；已有未结案认领时返回 ``False``，不覆盖。"""
        ...

    def lock_authority(self) -> None:
        """取本事务的权威互斥锁（``pg_advisory_xact_lock``）；与授权和权威变更共用。"""
        ...

    def has_authorized_pending(self) -> bool:
        """是否存在已授权且未结案的删除认领；存在时权威不得变更。"""
        ...

    def authorize(self, claim_id: UUID, *, at: datetime) -> bool:
        """把未结案认领推进到已授权阶段并保留首次时刻；已结案/不存在返回 ``False``。"""
        ...

    def refresh_deadline(self, claim_id: UUID, *, expires_at: datetime) -> bool:
        """仅在仍为未结案时刷新认领票据截止；已结案/不存在返回 ``False``。"""
        ...

    def settle(
        self,
        claim_id: UUID,
        *,
        status: EvidenceDeletionClaimStatus,
        reason_code: str,
        detail: str | None,
        settled_at: datetime,
    ) -> bool:
        """仅在仍为 ``PENDING`` 时终局结案；CAS 失败返回 ``False``，不覆盖既有终局。"""
        ...


class EvidenceImpactRepository(Protocol):
    """保留变更影响估算所需的证据引用只读计数 seam；不读取媒体字节（ADR-0012）。"""

    def review_usage(
        self,
        *,
        station_id: str | None,
        exposed_from: datetime | None,
        exposed_to: datetime,
    ) -> tuple[EvidenceReviewUsageCount, ...]:
        """按 ``(host_id, station_id, instance_id)`` 返回窗口内引用的复核状态聚合。

        ``known`` 是该实例窗口内全部引用的复核状态是否都已知，``active`` 表示其中存在未结案的
        在审复核。复核状态归 evidence 所有，聚合由本 owner 完成，调用方不读表。
        """
        ...

    def evidence_usage(
        self,
        *,
        station_id: str | None,
        exposed_from: datetime | None,
        exposed_to: datetime,
    ) -> tuple[EvidenceInstanceCount, ...]:
        """按 ``(host_id, station_id, instance_id)`` 返回计数。

        接收时刻落在 ``(exposed_from, exposed_to]`` 内。

        ``linked`` 是带 ``violation_id``（已锁存违规）的引用，``unlinked`` 是其余引用；两者不重叠。
        """
        ...


__all__ = [
    "EvidenceDeletionClaimRepository",
    "EvidenceImpactRepository",
    "EvidenceReclipRequestRepository",
    "EvidenceRepository",
    "EvidenceReviewRepository",
]
