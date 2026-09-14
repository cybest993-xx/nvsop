"""Permission-scoped configuration summary owned by the template module."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

from factory_sop.auth.api import Caller, Permission
from factory_sop.template.repository import TemplateRepository

_PAGE_SIZE = 1000
Summary = dict[str, object]


def summary(*, caller: Caller, templates: TemplateRepository) -> Summary:
    """Return real draft, import and published-version facts for the caller."""
    if not caller.holds(Permission.TEMPLATE_DRAFT_VIEW):
        return {"status": "not_permitted", "data": {}}

    drafts, draft_total = _all_pages(
        lambda page, size: templates.page_drafts(page=page, page_size=size)
    )
    imports, import_total = _all_pages(
        lambda page, size: templates.page_imports(page=page, page_size=size)
    )
    versions, version_total = _all_pages(
        lambda page, size: templates.page_versions(page=page, page_size=size)
    )
    del drafts
    data = {
        "drafts": {"total": draft_total},
        "imports": {
            "total": import_total,
            "by_status": _enum_counts(getattr(value, "status", None) for value in imports),
        },
        "published_versions": {
            "total": version_total,
            "sha256_verified": sum(bool(getattr(value, "sha256", None)) for value in versions),
            "sha256_unverified": sum(
                not bool(getattr(value, "sha256", None)) for value in versions
            ),
        },
    }
    status = "available" if draft_total or import_total or version_total else "no_data"
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
