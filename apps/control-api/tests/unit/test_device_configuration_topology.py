"""Machine configuration reads complete device-owner topology, not UI pages."""

from __future__ import annotations

from uuid import UUID

from device_fakes import (
    FakeCameras,
    FakeConnectors,
    FakeInferenceBackends,
    FakeInferenceHosts,
    FakeInferenceStations,
    FakePoints,
)

from factory_sop.device.api import DeviceConfigurationTarget
from factory_sop.device.model import InferenceBackend
from factory_sop.device.usecases.configuration import RepositoryDeviceConfigurationGateway
from nvsop_contracts import ConfigurationBundle


class ConfigurationHosts(FakeInferenceHosts):
    def next_configuration_revision(
        self, *, host_id: UUID, content_sha256: str, minimum_revision: int = 0
    ) -> int:
        del content_sha256
        host = self.by_id(host_id)
        assert host is not None
        return max(host.configuration_revision, minimum_revision) + 1

    def configuration_was_issued(
        self,
        *,
        host_id: UUID,
        configuration_revision: int,
        configuration_sha256: str,
    ) -> bool:
        host = self.by_id(host_id)
        return (
            host is not None
            and host.configuration_revision == configuration_revision
            and host.configuration_sha256 == configuration_sha256
        )

    def record_configuration_assignments(self, bundle: ConfigurationBundle) -> None:
        del bundle


class EmptyUiPageBackends(FakeInferenceBackends):
    """Expose a deliberately incomplete UI page while preserving the owner-complete query."""

    def page_of(
        self, *, page: int, page_size: int, host_id: UUID | None
    ) -> tuple[list[InferenceBackend], int]:
        del page, page_size
        total = len(self.for_host(host_id)) if host_id is not None else len(self.rows)
        return [], total


def test_machine_topology_does_not_depend_on_backend_ui_pagination() -> None:
    hosts = ConfigurationHosts()
    backends = EmptyUiPageBackends()
    stations = FakeInferenceStations()
    cameras = FakeCameras()
    host = hosts.register(name="推理机-完整拓扑")
    backend = backends.register(host_id=host.id, base_url="http://10.0.8.20:8000")
    station = stations.register(code="CFG-001", name="机器配置工位")
    cameras.register(station_id=station.id, host_id=host.id, backend_id=backend.id)
    gateway = RepositoryDeviceConfigurationGateway(
        hosts=hosts,
        backends=backends,
        stations=stations,
        cameras=cameras,
        connectors=FakeConnectors(),
        points=FakePoints(),
    )

    topology = gateway.topology_for_host(host.id)

    assert topology.backend_revisions == (backend.revision,)
    assert topology.targets == (
        DeviceConfigurationTarget(station_id=station.id, backend_id=backend.id),
    )
