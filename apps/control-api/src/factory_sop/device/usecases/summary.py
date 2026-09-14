"""Permission-scoped configuration summary owned by the device module."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

from factory_sop.auth.api import Caller, Permission
from factory_sop.device.repository import (
    CameraRepository,
    ConnectorRepository,
    InferenceBackendRepository,
    InferenceHostRepository,
    PointRepository,
    StationRepository,
)

_PAGE_SIZE = 1000
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
    """Return only real device configuration facts visible to ``caller``.

    This use case deliberately reports configuration and measured connection facts only. It
    never turns a missing observation into an online/healthy value. An authorized but empty
    resource set is ``no_data``; a caller without any device view permission is
    ``not_permitted``.
    """
    data: dict[str, object] = {}
    resource_summaries: list[dict[str, object]] = []

    if caller.holds(Permission.INFERENCE_HOST_VIEW):
        values, total = _all_pages(lambda page, size: hosts.page_of(page=page, page_size=size))
        item = _resource_counts(values, total)
        data["inference_hosts"] = item
        resource_summaries.append(item)

    if caller.holds(Permission.INFERENCE_BACKEND_VIEW):
        values, total = _all_pages(
            lambda page, size: backends.page_of(page=page, page_size=size, host_id=None)
        )
        item = _backend_counts(values, total)
        data["inference_backends"] = item
        resource_summaries.append(item)

    if caller.holds(Permission.STATION_VIEW):
        values, total = _all_pages(lambda page, size: stations.page_of(page=page, page_size=size))
        item = _resource_counts(values, total)
        data["stations"] = item
        resource_summaries.append(item)

    if caller.holds(Permission.CAMERA_VIEW):
        values, total = _all_pages(
            lambda page, size: cameras.page_of(page=page, page_size=size, station_id=None)
        )
        item = _camera_counts(values, total)
        data["cameras"] = item
        resource_summaries.append(item)

    if caller.holds(Permission.CONNECTOR_VIEW):
        values, total = _all_pages(
            lambda page, size: connectors.page_of(page=page, page_size=size, station_id=None)
        )
        item = _connector_counts(values, total)
        data["connectors"] = item
        resource_summaries.append(item)

    if caller.holds(Permission.POINT_VIEW):
        values, total = _all_pages(
            lambda page, size: points.page_of(
                page=page, page_size=size, station_id=None, connector_id=None
            )
        )
        item = _resource_counts(values, total)
        data["points"] = item
        resource_summaries.append(item)

    if not resource_summaries:
        return {"status": "not_permitted", "data": {}}
    status = "available" if any(item["total"] for item in resource_summaries) else "no_data"
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


def _resource_counts(values: Sequence[object], total: int) -> dict[str, object]:
    by_status = _enum_counts(getattr(value, "status", None) for value in values)
    return {
        "total": total,
        "active": by_status.get("active", 0),
        "deactivated": by_status.get("deactivated", 0),
        "unknown": sum(
            count for key, count in by_status.items() if key not in {"active", "deactivated"}
        ),
        "by_status": by_status,
    }


def _backend_counts(values: Sequence[object], total: int) -> dict[str, object]:
    result = _resource_counts(values, total)
    connection_states = _enum_counts(getattr(value, "connection_state", None) for value in values)
    result.update(
        {
            "connection_states": connection_states,
            "verified": connection_states.get("success", 0) + connection_states.get("failure", 0),
            "unverified": connection_states.get("unverified", 0),
        }
    )
    return result


def _camera_counts(values: Sequence[object], total: int) -> dict[str, object]:
    result = _resource_counts(values, total)
    credentials = _enum_counts(
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


def _connector_counts(values: Sequence[object], total: int) -> dict[str, object]:
    result = _resource_counts(values, total)
    reachability = _enum_counts(getattr(value, "reachability", None) for value in values)
    result.update(
        {
            "reachability": reachability,
            "verified": reachability.get("reachable", 0) + reachability.get("unreachable", 0),
            "unverified": reachability.get("unverified", 0),
        }
    )
    return result


def _enum_counts(values: Iterable[object]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        raw = getattr(value, "value", value)
        key = "unknown" if raw is None else str(raw)
        counts[key] = counts.get(key, 0) + 1
    return counts


__all__ = ["summary"]
