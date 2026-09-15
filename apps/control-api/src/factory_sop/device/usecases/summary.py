"""device 模块拥有的权限范围配置摘要。"""

from __future__ import annotations

from factory_sop.auth.api import Caller, Permission
from factory_sop.device.repository import (
    CameraRepository,
    ConnectorRepository,
    InferenceBackendRepository,
    InferenceHostRepository,
    PointRepository,
    StationRepository,
)
from factory_sop.pagination import all_pages, enum_counts

Summary = dict[str, object]


def summary(
    *,
    caller: Caller,
    hosts: InferenceHostRepository,
    backends: InferenceBackendRepository,
    stations: StationRepository,
    cameras: CameraRepository,
    connectors: ConnectorRepository,
    points: PointRepository,
) -> Summary:
    """只返回 ``caller`` 可见的真实设备配置事实。

    本用例只报告配置和实测连接事实，不把缺失观测转换为在线或健康。已授权但资源为空时
    返回 ``no_data``；没有任何设备查看权限时返回 ``not_permitted``。
    """
    data: dict[str, object] = {}
    resource_summaries: list[dict[str, object]] = []

    if caller.holds(Permission.INFERENCE_HOST_VIEW):
        host_values, total = all_pages(lambda page, size: hosts.page_of(page=page, page_size=size))
        item = _resource_counts(host_values, total)
        data["inference_hosts"] = item
        resource_summaries.append(item)

    if caller.holds(Permission.INFERENCE_BACKEND_VIEW):
        backend_values, total = all_pages(
            lambda page, size: backends.page_of(page=page, page_size=size, host_id=None)
        )
        item = _backend_counts(backend_values, total)
        data["inference_backends"] = item
        resource_summaries.append(item)

    if caller.holds(Permission.STATION_VIEW):
        station_values, total = all_pages(
            lambda page, size: stations.page_of(page=page, page_size=size)
        )
        item = _resource_counts(station_values, total)
        data["stations"] = item
        resource_summaries.append(item)

    if caller.holds(Permission.CAMERA_VIEW):
        camera_values, total = all_pages(
            lambda page, size: cameras.page_of(page=page, page_size=size, station_id=None)
        )
        item = _camera_counts(camera_values, total)
        data["cameras"] = item
        resource_summaries.append(item)

    if caller.holds(Permission.CONNECTOR_VIEW):
        connector_values, total = all_pages(
            lambda page, size: connectors.page_of(page=page, page_size=size, station_id=None)
        )
        item = _connector_counts(connector_values, total)
        data["connectors"] = item
        resource_summaries.append(item)

    if caller.holds(Permission.POINT_VIEW):
        point_values, total = all_pages(
            lambda page, size: points.page_of(
                page=page, page_size=size, station_id=None, connector_id=None
            )
        )
        item = _resource_counts(point_values, total)
        data["points"] = item
        resource_summaries.append(item)

    if not resource_summaries:
        return {"status": "not_permitted", "data": {}}
    status = "available" if any(item["total"] for item in resource_summaries) else "no_data"
    return {"status": status, "data": data}


def _resource_counts(values: tuple[object, ...], total: int) -> dict[str, object]:
    by_status = enum_counts(getattr(value, "status", None) for value in values)
    return {
        "total": total,
        "active": by_status.get("active", 0),
        "deactivated": by_status.get("deactivated", 0),
        "unknown": sum(
            count for key, count in by_status.items() if key not in {"active", "deactivated"}
        ),
        "by_status": by_status,
    }


def _backend_counts(values: tuple[object, ...], total: int) -> dict[str, object]:
    result = _resource_counts(values, total)
    connection_states = enum_counts(getattr(value, "connection_state", None) for value in values)
    result.update(
        {
            "connection_states": connection_states,
            "verified": connection_states.get("success", 0) + connection_states.get("failure", 0),
            "unverified": connection_states.get("unverified", 0),
        }
    )
    return result


def _camera_counts(values: tuple[object, ...], total: int) -> dict[str, object]:
    result = _resource_counts(values, total)
    credentials = enum_counts(
        "configured" if bool(getattr(value, "credentials_configured", False)) else "not_configured"
        for value in values
    )
    result.update(
        {
            "credentials_configured": credentials.get("configured", 0),
            "credentials_not_configured": credentials.get("not_configured", 0),
        }
    )
    return result


def _connector_counts(values: tuple[object, ...], total: int) -> dict[str, object]:
    result = _resource_counts(values, total)
    reachability = enum_counts(getattr(value, "reachability", None) for value in values)
    result.update(
        {
            "reachability": reachability,
            "verified": reachability.get("reachable", 0) + reachability.get("unreachable", 0),
            "unverified": reachability.get("unverified", 0),
        }
    )
    return result


__all__ = ["summary"]
