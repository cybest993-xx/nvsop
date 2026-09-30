"""evidence 用例访问中心登记表的唯一仓储 seam；方法都不提交事务。"""

from __future__ import annotations

from typing import Protocol

from factory_sop.evidence.model import EvidenceReference, EvidenceStatus


class EvidenceRepository(Protocol):
    """中心证据引用的读写 seam。"""

    def find(self, evidence_id: str) -> EvidenceReference | None:
        """按稳定证据 ID 读取一条登记，未登记时返回 ``None``。"""
        ...

    def insert(self, value: EvidenceReference) -> bool:
        """仅在证据 ID 尚未登记时插入；已有行时返回 ``False``，不覆盖。"""
        ...

    def replace(self, value: EvidenceReference) -> None:
        """覆盖同证据 ID 的登记状态或补齐媒体身份；身份本身不在此改变。"""
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


__all__ = ["EvidenceRepository"]
