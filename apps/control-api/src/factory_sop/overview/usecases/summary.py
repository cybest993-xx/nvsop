"""overview 模块的权限裁剪摘要组合用例。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Literal

from factory_sop.observability import get_logger

_logger = get_logger("overview")

OverviewStatus = Literal[
    "available",
    "no_data",
    "not_permitted",
    "unavailable",
    "failed",
    "partial",
]
_OWNER_STATUSES = frozenset({"available", "no_data", "not_permitted", "unavailable", "failed"})


class OverviewUnavailableError(RuntimeError):
    """某个归属模块暂时无法为本次请求产生摘要。"""


class OverviewFailedError(RuntimeError):
    """某个归属模块返回了无法解释的摘要结果。"""


@dataclass(frozen=True, slots=True)
class OverviewSection:
    """HTTP 组合接缝使用的内部分区信封。"""

    status: str
    data: Mapping[str, object]
    detail: str | None = None

    def to_wire(self) -> dict[str, object]:
        """把摘要分区转换为 HTTP 可编码的对象。"""
        return {key: value for key, value in asdict(self).items() if value is not None}


def compose_overview(
    *,
    device: Callable[[], OverviewSection | Mapping[str, object]],
    template: Callable[[], OverviewSection | Mapping[str, object]],
    dataset: Callable[[], OverviewSection | Mapping[str, object]],
    monitor: Callable[[], OverviewSection | Mapping[str, object]],
) -> dict[str, object]:
    """组合归属模块摘要，并保持 ``partial`` 的明确含义。

    只有暂时不可用的读取在同时存在可用真实摘要时才标为 ``partial``；没有可用真实摘要
    时标为 ``unavailable``。所有者明确返回的 ``no_data``、``not_permitted``、``failed``
    和 ``unavailable`` 不会互相改写。
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
    unavailable: dict[str, OverviewUnavailableError] = {}
    for key, provider in providers.items():
        try:
            result[key] = _section(provider()).to_wire()
        except OverviewUnavailableError as error:
            unavailable[key] = error
            _logger.warning(
                "overview.section.unavailable",
                section=key,
                error_type=type(error).__name__,
            )
        except OverviewFailedError as error:
            result[key] = OverviewSection(
                status="failed",
                data={},
                detail=str(error),
            ).to_wire()
            _logger.warning(
                "overview.section.failed",
                section=key,
                error_type=type(error).__name__,
            )

    has_available = any(
        isinstance(section, Mapping) and section.get("status") == "available"
        for section in result.values()
    )
    for key in unavailable:
        failure_status: OverviewStatus = "partial" if has_available else "unavailable"
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
    """校验所有者摘要封装，不把未知状态改写成已知状态。"""
    if isinstance(value, OverviewSection):
        section = value
    elif isinstance(value, Mapping):
        status = value.get("status")
        data = value.get("data")
        detail = value.get("detail")
        if not isinstance(status, str) or not isinstance(data, Mapping):
            raise OverviewFailedError("owner summary has an invalid envelope")
        if detail is not None and not isinstance(detail, str):
            raise OverviewFailedError("owner summary has an invalid detail")
        section = OverviewSection(status=status, data=data, detail=detail)
    else:
        raise OverviewFailedError("owner summary has an invalid envelope")

    if section.status not in _OWNER_STATUSES:
        raise OverviewFailedError(f"owner summary returned unknown status: {section.status!r}")
    return section


__all__ = [
    "OverviewFailedError",
    "OverviewSection",
    "OverviewStatus",
    "OverviewUnavailableError",
    "compose_overview",
]
