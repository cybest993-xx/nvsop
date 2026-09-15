"""overview 模块对 HTTP 组合适配器和测试暴露的最小接口。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from factory_sop.auth.api import Caller
from factory_sop.overview.usecases.summary import (
    OverviewSection,
    OverviewUnavailableError,
    compose_overview,
)
from factory_sop.persistence import RequestSession


class SummaryReader(Protocol):
    """组合根向 overview 提供一个模块摘要读取器。"""

    def __call__(self, caller: Caller, session: RequestSession) -> dict[str, object]:
        """在同一请求事务中读取一个权限裁剪摘要。"""
        ...


@dataclass(frozen=True, slots=True)
class OverviewSources:
    """HTTP 组合适配器读取四个所有者摘要的 source。"""

    device: SummaryReader
    template: SummaryReader
    dataset: SummaryReader
    monitor: SummaryReader


def build_overview(
    *,
    device: Callable[[], OverviewSection | Mapping[str, object]],
    template: Callable[[], OverviewSection | Mapping[str, object]],
    dataset: Callable[[], OverviewSection | Mapping[str, object]],
    monitor: Callable[[], OverviewSection | Mapping[str, object]],
) -> dict[str, object]:
    """组合四个模块的权限裁剪摘要，并保留可用模块的真实结果。"""
    return compose_overview(
        device=device,
        template=template,
        dataset=dataset,
        monitor=monitor,
    )


__all__ = [
    "OverviewSection",
    "OverviewSources",
    "OverviewUnavailableError",
    "SummaryReader",
    "build_overview",
]
