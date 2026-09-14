"""dataset 模块拥有的权限裁剪配置摘要。"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from factory_sop.auth.api import Caller, Permission
from factory_sop.dataset.repository import DatasetRepository
from factory_sop.summary_support import all_pages, enum_counts

Summary = dict[str, object]


def summary(*, caller: Caller, datasets: DatasetRepository) -> Summary:
    """返回调用方可见的真实数据集和已登记视频事实。"""
    if not caller.holds(Permission.DATASET_VIEW):
        return {"status": "not_permitted", "data": {}}

    dataset_values, dataset_total = all_pages(
        lambda page, size: datasets.page_datasets(page=page, page_size=size)
    )
    member_values: list[object] = []
    member_total = 0
    for dataset in dataset_values:
        dataset_id = getattr(dataset, "id", None)
        if not isinstance(dataset_id, UUID):
            raise ValueError("dataset summary encountered a dataset without an id")
        bound_dataset_id: UUID = dataset_id

        def fetch_members(
            page: int, page_size: int, dataset_id: UUID = bound_dataset_id
        ) -> tuple[Sequence[object], int]:
            return datasets.page_members(dataset_id=dataset_id, page=page, page_size=page_size)

        members, total = all_pages(fetch_members)
        member_values.extend(members)
        member_total += total

    data = {
        "datasets": {"total": dataset_total},
        "members": {
            "total": member_total,
            "by_status": enum_counts(getattr(value, "status", None) for value in member_values),
        },
    }
    status = "available" if dataset_total else "no_data"
    return {"status": status, "data": data}


__all__ = ["summary"]
