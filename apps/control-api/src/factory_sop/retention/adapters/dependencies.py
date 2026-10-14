"""retention HTTP 与配置组合适配器的注入点；真实 wiring 只在组合根。"""

from __future__ import annotations

from factory_sop.persistence import RequestSession
from factory_sop.retention.adapters.repository import (
    PostgresRetentionPolicyRepository,
    PostgresStationRetentionOverrideRepository,
    RepositoryStationRetentionProjectionGateway,
)
from factory_sop.retention.api import (
    RetentionPolicyRepository,
    StationFactsGateway,
    StationRetentionOverrideRepository,
    StationRetentionProjectionGateway,
)


def policy_repository(session: RequestSession) -> RetentionPolicyRepository:
    """请求事务中的 `retention_policy`。"""
    return PostgresRetentionPolicyRepository(session)


def station_overrides(session: RequestSession) -> StationRetentionOverrideRepository:
    """请求事务中的 `retention_station_policy`。"""
    return PostgresStationRetentionOverrideRepository(session)


def station_facts() -> StationFactsGateway:
    """由组合根注入 `device` 的工位存在/启用事实 seam。"""
    raise RuntimeError("retention station facts dependency was not wired")


def configuration_gateway(session: RequestSession) -> StationRetentionProjectionGateway:
    """机器配置 transport 使用的 retention owner seam。"""
    return RepositoryStationRetentionProjectionGateway(
        PostgresRetentionPolicyRepository(session),
        PostgresStationRetentionOverrideRepository(session),
    )


__all__ = ["configuration_gateway", "policy_repository", "station_facts", "station_overrides"]
