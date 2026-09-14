"""Permission-scoped composition helpers for the overview HTTP adapter."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass

from factory_sop.auth.api import Caller
from factory_sop.observability import get_logger

_logger = get_logger("overview")


class OverviewUnavailableError(RuntimeError):
    """An owner summary could not produce a safe snapshot for this request."""


@dataclass(frozen=True, slots=True)
class OverviewSection:
    """The internal section envelope used by the HTTP composition seam."""

    status: str
    data: Mapping[str, object]
    detail: str | None = None

    def to_wire(self) -> dict[str, object]:
        return {key: value for key, value in asdict(self).items() if value is not None}


def build_overview(
    *,
    caller: Caller,
    device: Callable[[], OverviewSection | Mapping[str, object]],
    template: Callable[[], OverviewSection | Mapping[str, object]],
    dataset: Callable[[], OverviewSection | Mapping[str, object]],
    monitor: Callable[[], OverviewSection | Mapping[str, object]],
) -> dict[str, object]:
    """Compose owner summaries while keeping one failed section from hiding the others.

    A failed owner is ``partial`` when at least one other owner returned a usable response.
    If every owner failed, each section is ``unavailable``. ``not_permitted`` and ``no_data``
    are returned by the owner use cases and are never rewritten as failures here.
    """
    del caller
    providers = {
        "device": device,
        "template": template,
        "dataset": dataset,
        "monitor": monitor,
    }
    unavailable_detail = {
        "device": "设备摘要暂时不可用",
        "template": "模板摘要暂时不可用",
        "dataset": "训练数据摘要暂时不可用",
        "monitor": "运行观测摘要暂时不可用",
    }
    result: dict[str, object] = {}
    failed: list[str] = []
    for key, provider in providers.items():
        try:
            result[key] = _section(provider()).to_wire()
        except OverviewUnavailableError as error:
            failed.append(key)
            _logger.warning(
                "overview.section.unavailable",
                section=key,
                error_type=type(error).__name__,
            )

    if failed:
        failure_status = "partial" if len(failed) < len(providers) else "unavailable"
        for key in failed:
            detail = unavailable_detail[key]
            if failure_status == "partial":
                detail = f"{detail}；其他模块仍返回真实摘要"
            result[key] = OverviewSection(
                status=failure_status,
                data={},
                detail=detail,
            ).to_wire()
    return result


def _section(value: OverviewSection | Mapping[str, object]) -> OverviewSection:
    if isinstance(value, OverviewSection):
        return value
    status = value.get("status")
    data = value.get("data")
    detail = value.get("detail")
    if not isinstance(status, str) or not isinstance(data, Mapping):
        raise OverviewUnavailableError("owner summary has an invalid envelope")
    if detail is not None and not isinstance(detail, str):
        raise OverviewUnavailableError("owner summary has an invalid detail")
    return OverviewSection(status=status, data=data, detail=detail)


__all__ = ["OverviewSection", "OverviewUnavailableError", "build_overview"]
