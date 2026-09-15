"""中心只读摘要共用的分页和状态计数规则。"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

_PAGE_SIZE = 1000
PageFetcher = Callable[[int, int], tuple[Sequence[object], int]]


def all_pages(fetch: PageFetcher) -> tuple[tuple[object, ...], int]:
    """读取稳定总数下的全部分页，拒绝不完整或变化中的结果。"""
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


def enum_counts(values: Iterable[object]) -> dict[str, int]:
    """按枚举值或字符串值统计摘要状态，并将空值归入 unknown。"""
    counts: dict[str, int] = {}
    for value in values:
        raw = getattr(value, "value", value)
        key = "unknown" if raw is None else str(raw)
        counts[key] = counts.get(key, 0) + 1
    return counts


__all__ = ["PageFetcher", "all_pages", "enum_counts"]
