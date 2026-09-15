"""overview 模块的权限裁剪摘要组合用例。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass

from factory_sop.observability import get_logger

_logger = get_logger("overview")


class OverviewUnavailableError(RuntimeError):
    """某个所有者摘要无法为本次请求生成安全快照。"""


@dataclass(frozen=True, slots=True)
class OverviewSection:
    """HTTP 组合接缝使用的内部分段封装。"""

    status: str
    data: Mapping[str, object]
    detail: str | None = None

    def to_wire(self) -> dict[str, object]:
        return {key: value for key, value in asdict(self).items() if value is not None}


def compose_overview(
    *,
    device: Callable[[], OverviewSection | Mapping[str, object]],
    template: Callable[[], OverviewSection | Mapping[str, object]],
    dataset: Callable[[], OverviewSection | Mapping[str, object]],
    monitor: Callable[[], OverviewSection | Mapping[str, object]],
) -> dict[str, object]:
    """组合所有者摘要，避免一个失败分段遮蔽其他真实结果。

    至少一个其他模块返回可用结果时，失败模块状态为 ``partial``；全部模块失败时，
    每个分段状态为 ``unavailable``。所有者用例返回的 ``not_permitted`` 和 ``no_data``
    会原样保留，不会在这里改写成失败。
    """
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


__all__ = ["OverviewSection", "OverviewUnavailableError", "compose_overview"]
