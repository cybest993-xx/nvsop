"""retention HTTP 与配置组合适配器的注入点；真实 wiring 只在组合根。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Protocol
from uuid import UUID

from fastapi import Depends, Request

from factory_sop.auth.api import Authorized, CurrentSessionFingerprint, SessionRefresh
from factory_sop.persistence import RequestSession
from factory_sop.retention.adapters.edge_impact import HttpEdgeImpactGateway
from factory_sop.retention.adapters.repository import (
    PostgresRetentionPolicyRepository,
    PostgresStationRetentionOverrideRepository,
    RepositoryStationRetentionProjectionGateway,
)
from factory_sop.retention.api import (
    EdgeImpactGateway,
    EdgeImpactSourcesGateway,
    EvidenceImpactGateway,
    MonitorImpactGateway,
    RetentionPolicyRepository,
    StationFactsGateway,
    StationRetentionOverrideRepository,
    StationRetentionProjectionGateway,
)
from factory_sop.settings import Settings


def estimate_ttl_seconds(request: Request) -> int | None:
    """读取中心部署显式提供的确认时限；None 表示缩短确认尚未配置。"""
    settings: Settings = request.app.state.settings
    return settings.retention_estimate_ttl_seconds


def policy_repository(session: RequestSession) -> RetentionPolicyRepository:
    """请求事务中的 `retention_policy`。"""
    return PostgresRetentionPolicyRepository(session)


def station_overrides(session: RequestSession) -> StationRetentionOverrideRepository:
    """请求事务中的 `retention_station_policy`。"""
    return PostgresStationRetentionOverrideRepository(session)


def station_facts() -> StationFactsGateway:
    """由组合根注入 `device` 的工位存在/启用事实 seam。"""
    raise RuntimeError("retention station facts dependency was not wired")


def evidence_impact() -> EvidenceImpactGateway:
    """由组合根注入 `evidence` 的保留影响计数 seam。"""
    raise RuntimeError("retention evidence impact dependency was not wired")


def monitor_impact() -> MonitorImpactGateway:
    """由组合根注入 `monitor` 的保留影响计数 seam。"""
    raise RuntimeError("retention monitor impact dependency was not wired")


def edge_sources() -> EdgeImpactSourcesGateway:
    """组合根接到 device 的只读来源配置投影。"""
    raise RuntimeError("retention edge sources dependency was not wired")


class RetentionHostAuthenticator(Protocol):
    """统计回查 transport 所需的窄机器认证接口；device 身份转换由组合根负责。"""

    def __call__(
        self, *, request: Request, host_id: UUID, body: dict[str, object], now: datetime
    ) -> None: ...


def host_authenticator() -> RetentionHostAuthenticator:
    """组合根接到现有主机签名认证。"""
    raise RuntimeError("retention host authenticator dependency was not wired")


def edge_impact(
    request: Request,
    caller: Authorized,
    fingerprint: CurrentSessionFingerprint,
    session: RequestSession,
    refresh: SessionRefresh,
    sources: Annotated[EdgeImpactSourcesGateway, Depends(edge_sources)],
) -> EdgeImpactGateway:
    settings: Settings = request.app.state.settings
    return HttpEdgeImpactGateway(
        sources=sources,
        release_connection=session.rollback,
        refresh_caller=refresh,
        origins=settings.evidence_host_origins,
        caller=caller,
        fingerprint=fingerprint,
        secret=settings.csrf_secret.get_secret_value(),
        ttl_seconds=settings.retention_estimate_ttl_seconds,
        timeout_seconds=settings.retention_impact_timeout_seconds,
    )


def configuration_gateway(session: RequestSession) -> StationRetentionProjectionGateway:
    """机器配置 transport 使用的 retention owner seam。"""
    return RepositoryStationRetentionProjectionGateway(
        PostgresRetentionPolicyRepository(session),
        PostgresStationRetentionOverrideRepository(session),
    )


__all__ = [
    "RetentionHostAuthenticator",
    "configuration_gateway",
    "edge_impact",
    "edge_sources",
    "estimate_ttl_seconds",
    "evidence_impact",
    "host_authenticator",
    "monitor_impact",
    "policy_repository",
    "station_facts",
    "station_overrides",
]
