"""overview 模块对 HTTP 组合适配器暴露的最小接口。

角色：组合根通过 ``OverviewSources`` 注入四个 owner summary；调用方必须是已认证的
``Caller``，并由各 owner usecase 按自己的查看权限裁剪数据。overview 不重新解释 owner
事实，也不直接依赖其他模块的 repository 或 adapter。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from factory_sop.auth.api import Caller
from factory_sop.overview.usecases.summary import (
    OverviewFailedError,
    OverviewSection,
    OverviewUnavailableError,
    compose_overview,
)
from factory_sop.persistence import RequestSession


class SummaryReader(Protocol):
    """组合根向 overview 提供一个模块摘要读取器。"""

    def __call__(self, caller: Caller, session: RequestSession) -> dict[str, object]:
        """使用请求级 session 读取按 caller 权限裁剪的 owner 摘要。"""
        ...


@dataclass(frozen=True, slots=True)
class OverviewSources:
    """概览路由的四个归属模块数据源; 不承载仓储实现。"""

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
    """组合四个归属模块摘要; 调用方契约已在各归属模块用例中执行。"""
    return compose_overview(
        device=device,
        template=template,
        dataset=dataset,
        monitor=monitor,
    )


__all__ = [
    "OverviewFailedError",
    "OverviewSection",
    "OverviewSources",
    "OverviewUnavailableError",
    "SummaryReader",
    "build_overview",
]
