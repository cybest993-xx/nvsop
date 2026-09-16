"""What a `device` route declares to reach the module's seams.

The repositories come from the request's one transaction (ADR-0002), the probe from the
process. Authentication is `auth`'s `Authenticated` dependency; authorization itself is a
use-case-layer decision, not a route dependency (§5.15).
"""

from __future__ import annotations

from factory_sop.device.adapters.command_repository import PostgresPendingCommandRepository
from factory_sop.device.adapters.probe import UrllibConnectionProbe
from factory_sop.device.adapters.repository import (
    PostgresCameraRepository,
    PostgresConnectorRepository,
    PostgresInferenceBackendRepository,
    PostgresInferenceHostRepository,
    PostgresPointRepository,
    PostgresStationRepository,
)
from factory_sop.device.api import (
    DeviceHistoricalAssignmentGateway,
    DeviceHostGateway,
    DeviceTemplateBindingGateway,
)
from factory_sop.device.probing import ConnectionProbe
from factory_sop.device.repository import (
    CameraRepository,
    ConnectorRepository,
    InferenceBackendRepository,
    InferenceHostRepository,
    PendingCommandRepository,
    PointRepository,
    StationRepository,
)
from factory_sop.device.usecases.template_binding import RepositoryDeviceTemplateBindingGateway
from factory_sop.persistence import RequestSession


def hosts(session: RequestSession) -> InferenceHostRepository:
    """`device_inference_host` on the request's transaction."""
    return PostgresInferenceHostRepository(session)


def backends(
    session: RequestSession,
) -> InferenceBackendRepository:
    """`device_inference_backend` on the request's transaction."""
    return PostgresInferenceBackendRepository(session)


def stations(
    session: RequestSession,
) -> StationRepository:
    """请求事务中的 `device_station`。"""
    return PostgresStationRepository(session)


def cameras(
    session: RequestSession,
) -> CameraRepository:
    """请求事务中的 `device_camera`。"""
    return PostgresCameraRepository(session)


def connectors(
    session: RequestSession,
) -> ConnectorRepository:
    """请求事务中的 `device_connector`。"""
    return PostgresConnectorRepository(session)


def points(
    session: RequestSession,
) -> PointRepository:
    """请求事务中的 `device_point`。"""
    return PostgresPointRepository(session)


def probe() -> ConnectionProbe:
    """The real network probe. Route tests replace it with a scripted stand-in."""
    return UrllibConnectionProbe()


def pending_commands(
    session: RequestSession,
) -> PendingCommandRepository:
    """`device_pending_command` on the request's transaction."""
    return PostgresPendingCommandRepository(session)


def template_binding(session: RequestSession) -> DeviceTemplateBindingGateway:
    """模板模块使用的设备拓扑与工位运行参数 seam。"""
    return RepositoryDeviceTemplateBindingGateway(
        stations=PostgresStationRepository(session),
        hosts=PostgresInferenceHostRepository(session),
        backends=PostgresInferenceBackendRepository(session),
        cameras=PostgresCameraRepository(session),
        connectors=PostgresConnectorRepository(session),
        points=PostgresPointRepository(session),
    )


def historical_assignments(session: RequestSession) -> DeviceHistoricalAssignmentGateway:
    """监控上报入口使用的历史配置归属验证接缝。"""
    return PostgresInferenceHostRepository(session)


def host_gateway(session: RequestSession) -> DeviceHostGateway:
    """模板上报使用的主机认证与归属 seam。"""
    return RepositoryDeviceTemplateBindingGateway(
        stations=PostgresStationRepository(session),
        hosts=PostgresInferenceHostRepository(session),
        backends=PostgresInferenceBackendRepository(session),
        cameras=PostgresCameraRepository(session),
        connectors=PostgresConnectorRepository(session),
        points=PostgresPointRepository(session),
    )
