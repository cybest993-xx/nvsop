"""分页读取和枚举计数的共享纯函数。"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

_PAGE_SIZE = 1000


def all_pages[T](
    fetch: Callable[[int, int], tuple[Sequence[T], int]],
) -> tuple[tuple[T, ...], int]:
    """读取稳定总数对应的全部分页，并拒绝不完整或自相矛盾的结果。"""
    page = 1
    expected_total: int | None = None
    values: list[T] = []
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
    """按稳定字符串值统计枚举或原始状态，未知值原样保留。"""
    counts: dict[str, int] = {}
    for value in values:
        raw = getattr(value, "value", value)
        key = "unknown" if raw is None else str(raw)
        counts[key] = counts.get(key, 0) + 1
    return counts


__all__ = ["all_pages", "enum_counts"]
