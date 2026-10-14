"""evidence 用例访问中心登记表的唯一仓储 seam；方法都不提交事务。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from factory_sop.evidence.model import (
    EvidenceReclipRequest,
    EvidenceReference,
    EvidenceReview,
    EvidenceStatus,
)
from factory_sop.retention.api import EvidenceInstanceUsage


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


class EvidenceImpactRepository(Protocol):
    """保留变更影响估算所需的证据引用只读计数 seam；不读取媒体字节（ADR-0012）。"""

    def evidence_usage(
        self,
        *,
        station_id: str | None,
        exposed_from: datetime | None,
        exposed_to: datetime,
    ) -> tuple[EvidenceInstanceUsage, ...]:
        """按 ``(host_id, station_id, instance_id)`` 返回接收时刻落在 ``(exposed_from, exposed_to]`` 的计数。

        ``linked`` 是带 ``violation_id``（已锁存违规）的引用，``unlinked`` 是其余引用；两者不重叠。
        """
        ...


__all__ = [
    "EvidenceImpactRepository",
    "EvidenceReclipRequestRepository",
    "EvidenceRepository",
    "EvidenceReviewRepository",
]
