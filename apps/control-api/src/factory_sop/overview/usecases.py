"""在 HTTP/应用边界组合概览摘要。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass

from factory_sop.auth.api import Caller, Permission
from factory_sop.dataset.repository import DatasetRepository
from factory_sop.device.repository import (
    CameraRepository,
    ConnectorRepository,
    InferenceBackendRepository,
    InferenceHostRepository,
    PointRepository,
    StationRepository,
)
from factory_sop.monitor.repository import MonitorRepository
from factory_sop.observability import get_logger
from factory_sop.template.repository import TemplateRepository

_PAGE_SIZE = 1000
_logger = get_logger("overview")


class OverviewUnavailableError(RuntimeError):
    """概览分页或数据层在同一请求事务内无法形成完整快照。"""


@dataclass(frozen=True, slots=True)
class OverviewSection:
    status: str
    data: Mapping[str, object]
    detail: str | None = None

    def to_wire(self) -> dict[str, object]:
        return {key: value for key, value in asdict(self).items() if value is not None}


def build_overview(
    *,
    caller: Caller,
    device: Callable[[], OverviewSection],
    template: Callable[[], OverviewSection],
    dataset: Callable[[], OverviewSection],
    monitor: Callable[[], OverviewSection],
) -> dict[str, object]:
    """在一个请求事务中运行各 owner 摘要,并隔离已知的数据层故障。"""
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
    for key, provider in providers.items():
        try:
            result[key] = provider().to_wire()
        except OverviewUnavailableError as error:
            _logger.warning(
                "overview.section.unavailable",
                section=key,
                error_type=type(error).__name__,
            )
            result[key] = OverviewSection(
                status="unavailable",
                data={},
                detail=unavailable_detail[key],
            ).to_wire()
    return result


def device_summary(
    *,
    caller: Caller,
    hosts: InferenceHostRepository,
    backends: InferenceBackendRepository,
    stations: StationRepository,
    cameras: CameraRepository,
    connectors: ConnectorRepository,
    points: PointRepository,
) -> OverviewSection:
    """只返回调用者有权查看的设备资源,并完整遍历分页快照。"""
    data: dict[str, object] = {}
    visible = False
    if caller.holds(Permission.INFERENCE_HOST_VIEW):
        host_values, total = _all_pages(lambda page, size: hosts.page_of(page=page, page_size=size))
        data["inference_hosts"] = _count_status(host_values, total)
        visible = True
    if caller.holds(Permission.INFERENCE_BACKEND_VIEW):
        backend_values, total = _all_pages(
            lambda page, size: backends.page_of(page=page, page_size=size, host_id=None)
        )
        data["inference_backends"] = _count_status(backend_values, total)
        visible = True
    if caller.holds(Permission.STATION_VIEW):
        station_values, total = _all_pages(
            lambda page, size: stations.page_of(page=page, page_size=size)
        )
        data["stations"] = _count_status(station_values, total)
        visible = True
    if caller.holds(Permission.CAMERA_VIEW):
        camera_values, total = _all_pages(
            lambda page, size: cameras.page_of(page=page, page_size=size, station_id=None)
        )
        data["cameras"] = _count_status(camera_values, total)
        visible = True
    if caller.holds(Permission.CONNECTOR_VIEW):
        connector_values, total = _all_pages(
            lambda page, size: connectors.page_of(page=page, page_size=size, station_id=None)
        )
        reachability = _enum_counts(connector_values, "reachability")
        data["connectors"] = {
            **_count_status(connector_values, total),
            "reachability": reachability,
            "verified": sum(
                getattr(
                    getattr(value, "reachability", None),
                    "value",
                    getattr(value, "reachability", None),
                )
                in {"reachable", "unreachable"}
                for value in connector_values
            ),
            "unverified": reachability.get("unverified", 0),
        }
        visible = True
    if caller.holds(Permission.POINT_VIEW):
        point_values, total = _all_pages(
            lambda page, size: points.page_of(
                page=page, page_size=size, station_id=None, connector_id=None
            )
        )
        data["points"] = _count_status(point_values, total)
        visible = True
    if not visible:
        return OverviewSection(status="not_permitted", data={})
    return OverviewSection(status="available", data=data)


def template_summary(*, caller: Caller, templates: TemplateRepository) -> OverviewSection:
    if not caller.holds(Permission.TEMPLATE_DRAFT_VIEW):
        return OverviewSection(status="not_permitted", data={})
    _drafts, draft_total = _all_pages(
        lambda page, size: templates.page_drafts(page=page, page_size=size)
    )
    versions, version_total = _all_pages(
        lambda page, size: templates.page_versions(page=page, page_size=size)
    )
    return OverviewSection(
        status="available",
        data={
            "drafts": {"total": draft_total},
            "published_versions": {
                "total": version_total,
                "sha256_verified": sum(
                    bool(getattr(version, "sha256", None)) for version in versions
                ),
            },
        },
    )


def dataset_summary(*, caller: Caller, datasets: DatasetRepository) -> OverviewSection:
    if not caller.holds(Permission.DATASET_VIEW):
        return OverviewSection(status="not_permitted", data={})
    _values, total = datasets.page_datasets(page=1, page_size=1)
    return OverviewSection(status="available", data={"datasets": {"total": total}})


def monitor_summary(*, caller: Caller, monitor: MonitorRepository) -> OverviewSection:
    if not caller.holds(Permission.MONITOR_VIEW):
        return OverviewSection(status="not_permitted", data={})
    return OverviewSection(
        status="available",
        data={
            "recent_decisions": len(monitor.recent_decisions(limit=100)),
            "recent_health": len(monitor.recent_health(limit=100)),
            "runtime_status": "reported_observations_only",
        },
    )


def _all_pages(
    fetch: Callable[[int, int], tuple[Sequence[object], int]],
) -> tuple[tuple[object, ...], int]:
    """在请求事务内读取完整分页,避免状态分布只代表第一页。"""
    page = 1
    expected_total: int | None = None
    values: list[object] = []
    while expected_total is None or len(values) < expected_total:
        page_values, total = fetch(page, _PAGE_SIZE)
        if total < 0:
            raise OverviewUnavailableError("分页总数为负数")
        if expected_total is None:
            expected_total = total
        elif expected_total != total:
            raise OverviewUnavailableError("分页总数在请求事务内发生变化")
        values.extend(page_values)
        if len(values) >= expected_total:
            break
        if not page_values:
            raise OverviewUnavailableError("分页在总数达到前提前结束")
        page += 1
    assert expected_total is not None
    return tuple(values[:expected_total]), expected_total


def _count_status(values: Sequence[object], total: int) -> dict[str, object]:
    active = sum(
        getattr(getattr(value, "status", None), "value", getattr(value, "status", None)) == "active"
        for value in values
    )
    deactivated = sum(
        getattr(getattr(value, "status", None), "value", getattr(value, "status", None))
        == "deactivated"
        for value in values
    )
    return {
        "total": total,
        "active": active,
        "deactivated": deactivated,
        "unknown": sum(
            getattr(getattr(value, "status", None), "value", getattr(value, "status", None))
            not in {"active", "deactivated"}
            for value in values
        ),
    }


def _enum_counts(values: Sequence[object], field: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        raw = getattr(getattr(value, field), "value", getattr(value, field))
        result[raw] = result.get(raw, 0) + 1
    return result


__all__ = [
    "OverviewSection",
    "build_overview",
    "dataset_summary",
    "device_summary",
    "monitor_summary",
    "template_summary",
]
