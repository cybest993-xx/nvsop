"""evidence 用例访问中心登记表的唯一仓储 seam；方法都不提交事务。"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from factory_sop.evidence.model import (
    EvidenceReclipRequest,
    EvidenceReference,
    EvidenceReview,
    EvidenceStatus,
)


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

    def list_for_evidence(self, evidence_id: str) -> tuple[EvidenceReclipRequest, ...]:
        """返回该证据的全部再切片请求，按请求时刻升序。"""
        ...


class EvidenceReviewRepository(Protocol):
    """人工复核结论的只追加 seam；不提供更新或删除。"""

    def insert(self, value: EvidenceReview) -> bool:
        """仅在 ``review_id`` 尚未存在时插入；已有行时返回 ``False``，不覆盖。"""
        ...

    def find(self, review_id: UUID) -> EvidenceReview | None:
        """按稳定复核 ID 读取一条结论，不存在时返回 ``None``。"""
        ...

    def list_for_evidence(self, evidence_id: str) -> tuple[EvidenceReview, ...]:
        """返回该证据的全部复核结论，按复核时刻升序；不删除他人记录。"""
        ...


__all__ = [
    "EvidenceReclipRequestRepository",
    "EvidenceRepository",
    "EvidenceReviewRepository",
]
