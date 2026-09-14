from __future__ import annotations

from types import SimpleNamespace

from factory_sop.overview.usecases import (
    OverviewSection,
    OverviewUnavailableError,
    build_overview,
    device_summary,
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
        caller=Caller(),  # type: ignore[arg-type]
        device=lambda: OverviewSection(status="available", data={"hosts": {"total": 1}}),
        template=lambda: (_ for _ in ()).throw(OverviewUnavailableError("template")),
        dataset=lambda: OverviewSection(status="not_permitted", data={}),
        monitor=lambda: OverviewSection(
            status="available", data={"runtime_status": "reported_observations_only"}
        ),
    )

    assert result["device"] == {"status": "available", "data": {"hosts": {"total": 1}}}
    assert result["template"]["status"] == "unavailable"  # type: ignore[index]
    assert result["template"]["detail"] == "模板摘要暂时不可用"  # type: ignore[index]
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

    hosts = result.data["inference_hosts"]
    assert isinstance(hosts, dict)
    assert hosts == {"total": 1001, "active": 1000, "deactivated": 1, "unknown": 0}
    connectors = result.data["connectors"]
    assert isinstance(connectors, dict)
    assert connectors["total"] == 1001
    assert connectors["verified"] == 0
    assert connectors["unverified"] == 1001
