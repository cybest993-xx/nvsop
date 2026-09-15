from __future__ import annotations

from types import SimpleNamespace

from factory_sop.device.usecases.summary import summary as device_summary
from factory_sop.overview.api import (
    OverviewSection,
    OverviewUnavailableError,
    build_overview,
)


class Caller:
    def holds(self, permission: object) -> bool:
        return False


class AllPermissionsCaller:
    def holds(self, permission: object) -> bool:
        return True


class PagedRepository:
    def page_of(self, *, page: int, page_size: int, **kwargs: object) -> tuple[list[object], int]:
        del page_size, kwargs
        status = "active" if page == 1 else "deactivated"
        values: list[object] = [
            SimpleNamespace(
                status=status,
                reachability=SimpleNamespace(value="unverified"),
            )
            for _ in range(1000 if page == 1 else 1)
        ]
        return values, 1001


def test_overview_reports_partial_owner_failure_without_fabricating_status() -> None:
    result = build_overview(
        device=lambda: OverviewSection(status="available", data={"hosts": {"total": 1}}),
        template=lambda: (_ for _ in ()).throw(OverviewUnavailableError("template")),
        dataset=lambda: OverviewSection(status="not_permitted", data={}),
        monitor=lambda: OverviewSection(
            status="available", data={"runtime_status": "reported_observations_only"}
        ),
    )

    assert result["device"] == {"status": "available", "data": {"hosts": {"total": 1}}}
    assert result["template"]["status"] == "partial"  # type: ignore[index]
    assert result["template"]["detail"] == "模板摘要暂时不可用；其他模块仍返回真实摘要"  # type: ignore[index]
    assert result["dataset"] == {"status": "not_permitted", "data": {}}
    assert result["monitor"]["data"]["runtime_status"] == "reported_observations_only"  # type: ignore[index]


def test_device_summary_counts_statuses_across_all_pages() -> None:
    repository = PagedRepository()
    result = device_summary(
        caller=AllPermissionsCaller(),  # type: ignore[arg-type]
        hosts=repository,  # type: ignore[arg-type]
        backends=repository,  # type: ignore[arg-type]
        stations=repository,  # type: ignore[arg-type]
        cameras=repository,  # type: ignore[arg-type]
        connectors=repository,  # type: ignore[arg-type]
        points=repository,  # type: ignore[arg-type]
    )

    assert result["status"] == "available"
    data = result["data"]
    assert isinstance(data, dict)
    hosts = data["inference_hosts"]
    assert isinstance(hosts, dict)
    assert hosts["total"] == 1001
    assert hosts["active"] == 1000
    assert hosts["deactivated"] == 1
    assert hosts["unknown"] == 0
    assert hosts["by_status"] == {"active": 1000, "deactivated": 1}
    connectors = data["connectors"]
    assert isinstance(connectors, dict)
    assert connectors["total"] == 1001
    assert connectors["verified"] == 0
    assert connectors["unverified"] == 1001


def test_overview_reports_unavailable_when_every_owner_fails() -> None:
    def failed() -> OverviewSection:
        raise OverviewUnavailableError("store unavailable")

    result = build_overview(
        device=failed,
        template=failed,
        dataset=failed,
        monitor=failed,
    )

    statuses: list[object] = []
    for section in result.values():
        assert isinstance(section, dict)
        statuses.append(section["status"])
    assert set(statuses) == {"unavailable"}
