"""Permission-scoped configuration summary owned by the dataset module."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from uuid import UUID

from factory_sop.auth.api import Caller, Permission
from factory_sop.dataset.repository import DatasetRepository

_PAGE_SIZE = 1000
Summary = dict[str, object]


def summary(*, caller: Caller, datasets: DatasetRepository) -> Summary:
    """Return real dataset and registered-video facts visible to the caller."""
    if not caller.holds(Permission.DATASET_VIEW):
        return {"status": "not_permitted", "data": {}}

    dataset_values, dataset_total = _all_pages(
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

        members, total = _all_pages(fetch_members)
        member_values.extend(members)
        member_total += total

    data = {
        "datasets": {"total": dataset_total},
        "members": {
            "total": member_total,
            "by_status": _enum_counts(getattr(value, "status", None) for value in member_values),
        },
    }
    status = "available" if dataset_total else "no_data"
    return {"status": status, "data": data}


def _all_pages(
    fetch: Callable[[int, int], tuple[Sequence[object], int]],
) -> tuple[tuple[object, ...], int]:
    page = 1
    expected_total: int | None = None
    values: list[object] = []
    while expected_total is None or len(values) < expected_total:
        page_values, total = fetch(page, _PAGE_SIZE)
        if total < 0:
            raise ValueError("summary pagination total must not be negative")
        if expected_total is None:
            expected_total = total
        elif expected_total != total:
            raise ValueError("summary pagination total changed during the request")
        values.extend(page_values)
        if len(values) >= expected_total:
            break
        if not page_values:
            raise ValueError("summary pagination ended before reaching its total")
        page += 1
    assert expected_total is not None
    return tuple(values[:expected_total]), expected_total


def _enum_counts(values: Iterable[object]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        raw = getattr(value, "value", value)
        key = "unknown" if raw is None else str(raw)
        counts[key] = counts.get(key, 0) + 1
    return counts


__all__ = ["summary"]
