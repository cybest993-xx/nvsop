"""In-memory stand-ins for the phase-one `device` repository seams.

They sit at the same seams the PostgreSQL adapters and the urllib probe do (harness §4:
replace the adapter at the seam, do not mock through the call chain). The conflict and
concurrency behavior mirrors what the real adapter gets from PostgreSQL — a duplicate natural
key and a lost revision race raise the module's own refusals here, so a test cannot pass against
behavior the database would refuse.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from uuid import UUID

from factory_sop.device.errors import DeviceRefusalCode, DeviceRefusedError
from factory_sop.device.host_credentials import fingerprint
from factory_sop.device.model import (
    Camera,
    ConnectionState,
    Connector,
    ConnectorConfiguration,
    ConnectorReachability,
    ConnectorType,
    DeviceStatus,
    InferenceBackend,
    InferenceHost,
    PendingCommand,
    PendingCommandCompletion,
    PendingCommandStatus,
    Point,
    PointDirection,
    Station,
)
from factory_sop.device.probing import ProbeReport
from factory_sop.device.repository import PendingCommandRepository
from factory_sop.identifiers import new_id
from nvsop_contracts import Capability, Unverified

FAKE_ACTOR = new_id()
FAKE_NOW = datetime(2026, 9, 5, 8, 0, tzinfo=UTC)


@dataclass
class FakeInferenceHosts:
    """An `InferenceHostRepository` over a dict."""

    rows: dict[UUID, InferenceHost] = field(default_factory=dict)
    credentials: dict[UUID, str] = field(default_factory=dict)

    def register(
        self,
        *,
        name: str,
        address: str = "10.0.0.1",
        mediamtx_address: str | None = None,
        recording_window_seconds: int = 7 * 24 * 3600,
        disk_watermark_percent: int = 85,
        status: DeviceStatus = DeviceStatus.ACTIVE,
        created_at: datetime = FAKE_NOW,
    ) -> InferenceHost:
        """Build a host for a use-case test and store it."""
        host_credential = f"test-host-credential-{len(self.rows) + 1}"
        host = InferenceHost(
            id=new_id(),
            name=name,
            address=address,
            mediamtx_address=mediamtx_address,
            recording_window_seconds=recording_window_seconds,
            disk_watermark_percent=disk_watermark_percent,
            status=status,
            revision=1,
            created_by=FAKE_ACTOR,
            updated_by=FAKE_ACTOR,
            created_at=created_at,
            updated_at=created_at,
            credential_hash=fingerprint(credential=host_credential),
        )
        self.add(host)
        self.credentials[host.id] = host_credential
        return host

    def add(self, host: InferenceHost) -> None:
        if any(stored.name == host.name for stored in self.rows.values()):
            raise DeviceRefusedError(DeviceRefusalCode.INFERENCE_HOST_NAME_TAKEN)
        self.rows[host.id] = host

    def save(self, host: InferenceHost, *, expected_revision: int) -> None:
        stored = self.rows.get(host.id)
        if stored is None:
            raise DeviceRefusedError(DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND)
        if stored.revision != expected_revision:
            raise DeviceRefusedError(DeviceRefusalCode.STALE_REVISION)
        if any(other.name == host.name and other.id != host.id for other in self.rows.values()):
            raise DeviceRefusedError(DeviceRefusalCode.INFERENCE_HOST_NAME_TAKEN)
        self.rows[host.id] = host

    def by_id(self, host_id: UUID) -> InferenceHost | None:
        return self.rows.get(host_id)

    def remove(self, host_id: UUID, *, expected_revision: int) -> bool:
        stored = self.rows.get(host_id)
        if stored is None:
            raise DeviceRefusedError(DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND)
        if stored.revision != expected_revision:
            raise DeviceRefusedError(DeviceRefusalCode.STALE_REVISION)
        del self.rows[host_id]
        self.credentials.pop(host_id, None)
        return True

    def credential_for(self, host_id: UUID) -> str:
        return self.credentials[host_id]

    def page_of(self, *, page: int, page_size: int) -> tuple[list[InferenceHost], int]:
        ordered = sorted(
            self.rows.values(), key=lambda item: (item.created_at, item.id), reverse=True
        )
        start = (page - 1) * page_size
        return ordered[start : start + page_size], len(ordered)


@dataclass
class FakePendingCommands(PendingCommandRepository):
    """命令仓储接缝的内存实现，包含租约回收和原子幂等插入。"""

    rows: dict[UUID, PendingCommand] = field(default_factory=dict)

    def by_id(self, command_id: UUID) -> PendingCommand | None:
        return self.rows.get(command_id)

    def by_idempotency_key(self, key: str) -> PendingCommand | None:
        return next((item for item in self.rows.values() if item.idempotency_key == key), None)

    def add(self, command: PendingCommand) -> None:
        if self.by_idempotency_key(command.idempotency_key) is not None:
            raise DeviceRefusedError(DeviceRefusalCode.COMMAND_IDEMPOTENCY_CONFLICT)
        self.rows[command.id] = command

    def add_or_get(self, command: PendingCommand) -> PendingCommand:
        existing = self.by_idempotency_key(command.idempotency_key)
        if existing is not None:
            return existing
        self.rows[command.id] = command
        return command

    def claim_next(
        self,
        *,
        host_id: UUID,
        claim_token: str,
        claimed_at: datetime,
        lease_expires_at: datetime,
    ) -> PendingCommand | None:
        for command in self.rows.values():
            if (
                command.status is PendingCommandStatus.CLAIMED
                and command.lease_expires_at is not None
                and command.lease_expires_at <= claimed_at
            ):
                self.rows[command.id] = replace(
                    command,
                    status=PendingCommandStatus.PENDING,
                    claim_token=None,
                    claimed_at=None,
                    lease_expires_at=None,
                    updated_at=claimed_at,
                )
        pending = sorted(
            (
                command
                for command in self.rows.values()
                if command.host_id == host_id and command.status is PendingCommandStatus.PENDING
            ),
            key=lambda item: (item.created_at, item.id),
        )
        if not pending:
            return None
        command = pending[0]
        claimed = replace(
            command,
            status=PendingCommandStatus.CLAIMED,
            attempt=command.attempt + 1,
            claim_token=claim_token,
            claimed_at=claimed_at,
            lease_expires_at=lease_expires_at,
            updated_at=claimed_at,
        )
        self.rows[command.id] = claimed
        return claimed

    def complete(self, completion: PendingCommandCompletion) -> PendingCommand:
        command = self.rows.get(completion.command_id)
        if command is None:
            raise DeviceRefusedError(DeviceRefusalCode.COMMAND_NOT_FOUND)
        if command.host_id != completion.host_id:
            raise DeviceRefusedError(DeviceRefusalCode.COMMAND_HOST_MISMATCH)
        if command.status in {
            PendingCommandStatus.SUCCEEDED,
            PendingCommandStatus.FAILED,
            PendingCommandStatus.REJECTED,
        }:
            if (
                command.claim_token == completion.claim_token
                and command.result == completion.result
                and command.result_detail == completion.result_detail
                and command.failure_code == completion.failure_code
            ):
                return command
            raise DeviceRefusedError(DeviceRefusalCode.COMMAND_ALREADY_COMPLETED)
        if command.status is not PendingCommandStatus.CLAIMED:
            raise DeviceRefusedError(DeviceRefusalCode.COMMAND_CLAIM_REQUIRED)
        if command.claim_token != completion.claim_token:
            raise DeviceRefusedError(DeviceRefusalCode.COMMAND_CLAIM_TOKEN_INVALID)
        if command.lease_expires_at is None or completion.completed_at >= command.lease_expires_at:
            raise DeviceRefusedError(DeviceRefusalCode.COMMAND_CLAIM_EXPIRED)
        completed = replace(
            command,
            status=completion.status,
            result=completion.result,
            result_detail=completion.result_detail,
            failure_code=completion.failure_code,
            completed_at=completion.completed_at,
            updated_at=completion.completed_at,
        )
        self.rows[command.id] = completed
        return completed


@dataclass
class FakeInferenceBackends:
    """An `InferenceBackendRepository` over a dict."""

    rows: dict[UUID, InferenceBackend] = field(default_factory=dict)

    def register(
        self,
        *,
        host_id: UUID,
        base_url: str,
        status: DeviceStatus = DeviceStatus.ACTIVE,
        template_version_id: UUID | None = None,
        connection_state: ConnectionState = ConnectionState.UNVERIFIED,
        connection_checked_at: datetime | None = None,
        connection_detail: str | None = None,
        self_reported_model_ids: tuple[str, ...] = (),
        self_reported_at: datetime | None = None,
        created_at: datetime = FAKE_NOW,
    ) -> InferenceBackend:
        backend = InferenceBackend(
            id=new_id(),
            host_id=host_id,
            base_url=base_url,
            template_version_id=template_version_id,
            status=status,
            connection_state=connection_state,
            connection_checked_at=connection_checked_at,
            connection_detail=connection_detail,
            self_reported_model_ids=self_reported_model_ids,
            self_reported_at=self_reported_at,
            revision=1,
            created_by=FAKE_ACTOR,
            updated_by=FAKE_ACTOR,
            created_at=created_at,
            updated_at=created_at,
        )
        self.add(backend)
        return backend

    def add(self, backend: InferenceBackend) -> None:
        if any(
            stored.host_id == backend.host_id and stored.base_url == backend.base_url
            for stored in self.rows.values()
        ):
            raise DeviceRefusedError(DeviceRefusalCode.INFERENCE_BACKEND_ENDPOINT_TAKEN)
        self.rows[backend.id] = backend

    def save(self, backend: InferenceBackend, *, expected_revision: int) -> None:
        stored = self.rows.get(backend.id)
        if stored is None:
            raise DeviceRefusedError(DeviceRefusalCode.INFERENCE_BACKEND_NOT_FOUND)
        if stored.revision != expected_revision:
            raise DeviceRefusedError(DeviceRefusalCode.STALE_REVISION)
        if any(
            other.host_id == backend.host_id
            and other.base_url == backend.base_url
            and other.id != backend.id
            for other in self.rows.values()
        ):
            raise DeviceRefusedError(DeviceRefusalCode.INFERENCE_BACKEND_ENDPOINT_TAKEN)
        self.rows[backend.id] = backend

    def by_id(self, backend_id: UUID) -> InferenceBackend | None:
        return self.rows.get(backend_id)

    def remove(self, backend_id: UUID, *, expected_revision: int) -> bool:
        stored = self.rows.get(backend_id)
        if stored is None:
            return False
        if stored.revision != expected_revision:
            raise DeviceRefusedError(DeviceRefusalCode.STALE_REVISION)
        del self.rows[backend_id]
        return True

    def any_for_host(self, host_id: UUID) -> bool:
        return any(stored.host_id == host_id for stored in self.rows.values())

    def page_of(
        self, *, page: int, page_size: int, host_id: UUID | None
    ) -> tuple[list[InferenceBackend], int]:
        candidates = [
            backend
            for backend in self.rows.values()
            if host_id is None or backend.host_id == host_id
        ]
        ordered = sorted(candidates, key=lambda item: (item.created_at, item.id), reverse=True)
        start = (page - 1) * page_size
        return ordered[start : start + page_size], len(ordered)


@dataclass
class FakeInferenceStations:
    """内存中的 `StationRepository`。"""

    rows: dict[UUID, Station] = field(default_factory=dict)

    def register(
        self,
        *,
        code: str,
        name: str,
        tags: tuple[str, ...] = (),
        status: DeviceStatus = DeviceStatus.ACTIVE,
        created_at: datetime = FAKE_NOW,
    ) -> Station:
        station = Station(
            id=new_id(),
            code=code,
            name=name,
            tags=tags,
            status=status,
            revision=1,
            created_by=FAKE_ACTOR,
            updated_by=FAKE_ACTOR,
            created_at=created_at,
            updated_at=created_at,
        )
        self.add(station)
        return station

    def add(self, station: Station) -> None:
        if any(stored.code == station.code for stored in self.rows.values()):
            raise DeviceRefusedError(DeviceRefusalCode.STATION_CODE_TAKEN)
        self.rows[station.id] = station

    def save(self, station: Station, *, expected_revision: int) -> None:
        stored = self.rows.get(station.id)
        if stored is None:
            raise DeviceRefusedError(DeviceRefusalCode.STATION_NOT_FOUND)
        if stored.revision != expected_revision:
            raise DeviceRefusedError(DeviceRefusalCode.STALE_REVISION)
        if any(
            other.code == station.code and other.id != station.id for other in self.rows.values()
        ):
            raise DeviceRefusedError(DeviceRefusalCode.STATION_CODE_TAKEN)
        self.rows[station.id] = station

    def by_id(self, station_id: UUID) -> Station | None:
        return self.rows.get(station_id)

    def remove(self, station_id: UUID, *, expected_revision: int) -> bool:
        stored = self.rows.get(station_id)
        if stored is None:
            raise DeviceRefusedError(DeviceRefusalCode.STATION_NOT_FOUND)
        if stored.revision != expected_revision:
            raise DeviceRefusedError(DeviceRefusalCode.STALE_REVISION)
        del self.rows[station_id]
        return True

    def page_of(self, *, page: int, page_size: int) -> tuple[list[Station], int]:
        ordered = sorted(
            self.rows.values(), key=lambda item: (item.created_at, item.id), reverse=True
        )
        start = (page - 1) * page_size
        return ordered[start : start + page_size], len(ordered)


@dataclass
class FakeCameras:
    """相机用例接口处的内存 `CameraRepository`。"""

    rows: dict[UUID, Camera] = field(default_factory=dict)

    def register(
        self,
        *,
        station_id: UUID,
        host_id: UUID,
        backend_id: UUID,
        name: str = "相机1",
        address: str = "10.0.8.21",
        main_stream_path: str = "/Streaming/Channels/101",
        sub_stream_path: str = "/Streaming/Channels/102",
        credentials_configured: bool = False,
        status: DeviceStatus = DeviceStatus.ACTIVE,
        created_at: datetime = FAKE_NOW,
    ) -> Camera:
        camera = Camera(
            id=new_id(),
            name=name,
            address=address,
            main_stream_path=main_stream_path,
            sub_stream_path=sub_stream_path,
            credentials_configured=credentials_configured,
            station_id=station_id,
            host_id=host_id,
            backend_id=backend_id,
            status=status,
            revision=1,
            created_by=FAKE_ACTOR,
            updated_by=FAKE_ACTOR,
            created_at=created_at,
            updated_at=created_at,
        )
        self.add(camera)
        return camera

    def add(self, camera: Camera) -> None:
        self.rows[camera.id] = camera

    def save(self, camera: Camera, *, expected_revision: int) -> None:
        stored = self.rows.get(camera.id)
        if stored is None:
            raise DeviceRefusedError(DeviceRefusalCode.CAMERA_NOT_FOUND)
        if stored.revision != expected_revision:
            raise DeviceRefusedError(DeviceRefusalCode.STALE_REVISION)
        self.rows[camera.id] = camera

    def by_id(self, camera_id: UUID) -> Camera | None:
        return self.rows.get(camera_id)

    def remove(self, camera_id: UUID, *, expected_revision: int) -> bool:
        stored = self.rows.get(camera_id)
        if stored is None:
            return False
        if stored.revision != expected_revision:
            raise DeviceRefusedError(DeviceRefusalCode.STALE_REVISION)
        del self.rows[camera_id]
        return True

    def any_for_station(self, station_id: UUID) -> bool:
        return any(camera.station_id == station_id for camera in self.rows.values())

    def for_station(self, station_id: UUID) -> list[Camera]:
        return [camera for camera in self.rows.values() if camera.station_id == station_id]

    def page_of(
        self, *, page: int, page_size: int, station_id: UUID | None
    ) -> tuple[list[Camera], int]:
        candidates = [
            camera
            for camera in self.rows.values()
            if station_id is None or camera.station_id == station_id
        ]
        ordered = sorted(candidates, key=lambda item: (item.created_at, item.id), reverse=True)
        start = (page - 1) * page_size
        return ordered[start : start + page_size], len(ordered)


@dataclass
class FakeConnectors:
    """`device_connector` 的内存仓储适配器。"""

    rows: dict[UUID, Connector] = field(default_factory=dict)

    def register(
        self,
        *,
        station_id: UUID,
        host_id: UUID,
        name: str = "一号连接器",
        connector_type: ConnectorType = ConnectorType.HIKVISION_ISAPI,
        configuration: dict[str, object] | None = None,
        status: DeviceStatus = DeviceStatus.ACTIVE,
        capability: Capability | None = None,
        created_at: datetime = FAKE_NOW,
    ) -> Connector:
        connector = Connector(
            id=new_id(),
            station_id=station_id,
            host_id=host_id,
            name=name,
            connector_type=connector_type,
            configuration=ConnectorConfiguration.from_wire(
                configuration or {"address": "10.0.8.21"}
            ),
            credentials_configured=False,
            reachability=ConnectorReachability.UNVERIFIED,
            health_detail=None,
            capability=Unverified() if capability is None else capability,
            status=status,
            revision=1,
            created_by=FAKE_ACTOR,
            updated_by=FAKE_ACTOR,
            created_at=created_at,
            updated_at=created_at,
        )
        self.add(connector)
        return connector

    def add(self, connector: Connector) -> None:
        if any(
            item.station_id == connector.station_id and item.name == connector.name
            for item in self.rows.values()
        ):
            raise DeviceRefusedError(DeviceRefusalCode.CONNECTOR_NAME_TAKEN)
        self.rows[connector.id] = connector

    def save(self, connector: Connector, *, expected_revision: int) -> None:
        stored = self.rows.get(connector.id)
        if stored is None:
            raise DeviceRefusedError(DeviceRefusalCode.CONNECTOR_NOT_FOUND)
        if stored.revision != expected_revision:
            raise DeviceRefusedError(DeviceRefusalCode.STALE_REVISION)
        if any(
            item.id != connector.id
            and item.station_id == connector.station_id
            and item.name == connector.name
            for item in self.rows.values()
        ):
            raise DeviceRefusedError(DeviceRefusalCode.CONNECTOR_NAME_TAKEN)
        self.rows[connector.id] = connector

    def by_id(self, connector_id: UUID) -> Connector | None:
        return self.rows.get(connector_id)

    def remove(self, connector_id: UUID, *, expected_revision: int) -> bool:
        stored = self.rows.get(connector_id)
        if stored is None:
            return False
        if stored.revision != expected_revision:
            raise DeviceRefusedError(DeviceRefusalCode.STALE_REVISION)
        del self.rows[connector_id]
        return True

    def any_for_station(self, station_id: UUID) -> bool:
        return any(item.station_id == station_id for item in self.rows.values())

    def any_for_host(self, host_id: UUID) -> bool:
        return any(item.host_id == host_id for item in self.rows.values())

    def for_station(self, station_id: UUID) -> list[Connector]:
        return [item for item in self.rows.values() if item.station_id == station_id]

    def page_of(
        self, *, page: int, page_size: int, station_id: UUID | None
    ) -> tuple[list[Connector], int]:
        candidates = [
            item
            for item in self.rows.values()
            if station_id is None or item.station_id == station_id
        ]
        ordered = sorted(candidates, key=lambda item: (item.created_at, item.id), reverse=True)
        start = (page - 1) * page_size
        return ordered[start : start + page_size], len(ordered)


@dataclass
class FakePoints:
    """`device_point` 的内存仓储适配器。"""

    rows: dict[UUID, Point] = field(default_factory=dict)

    def register(
        self,
        *,
        station_id: UUID,
        connector_id: UUID,
        direction: PointDirection,
        identifier: str,
        semantic_label: str,
        status: DeviceStatus = DeviceStatus.ACTIVE,
        created_at: datetime = FAKE_NOW,
    ) -> Point:
        point = Point(
            id=new_id(),
            station_id=station_id,
            connector_id=connector_id,
            direction=direction,
            identifier=identifier,
            semantic_label=semantic_label,
            status=status,
            revision=1,
            created_by=FAKE_ACTOR,
            updated_by=FAKE_ACTOR,
            created_at=created_at,
            updated_at=created_at,
        )
        self.add(point)
        return point

    def add(self, point: Point) -> None:
        self._require_unique(point)
        self.rows[point.id] = point

    def save(self, point: Point, *, expected_revision: int) -> None:
        stored = self.rows.get(point.id)
        if stored is None:
            raise DeviceRefusedError(DeviceRefusalCode.POINT_NOT_FOUND)
        if stored.revision != expected_revision:
            raise DeviceRefusedError(DeviceRefusalCode.STALE_REVISION)
        self._require_unique(point)
        self.rows[point.id] = point

    def by_id(self, point_id: UUID) -> Point | None:
        return self.rows.get(point_id)

    def remove(self, point_id: UUID, *, expected_revision: int) -> bool:
        stored = self.rows.get(point_id)
        if stored is None:
            return False
        if stored.revision != expected_revision:
            raise DeviceRefusedError(DeviceRefusalCode.STALE_REVISION)
        del self.rows[point_id]
        return True

    def any_for_station(self, station_id: UUID) -> bool:
        return any(point.station_id == station_id for point in self.rows.values())

    def any_for_connector(self, connector_id: UUID) -> bool:
        return any(point.connector_id == connector_id for point in self.rows.values())

    def page_of(
        self,
        *,
        page: int,
        page_size: int,
        station_id: UUID | None,
        connector_id: UUID | None,
    ) -> tuple[list[Point], int]:
        candidates = [
            point
            for point in self.rows.values()
            if (station_id is None or point.station_id == station_id)
            and (connector_id is None or point.connector_id == connector_id)
        ]
        ordered = sorted(candidates, key=lambda item: (item.created_at, item.id), reverse=True)
        start = (page - 1) * page_size
        return ordered[start : start + page_size], len(ordered)

    def _require_unique(self, point: Point) -> None:
        for stored in self.rows.values():
            if stored.id == point.id:
                continue
            if (stored.station_id, stored.semantic_label) == (
                point.station_id,
                point.semantic_label,
            ):
                raise DeviceRefusedError(DeviceRefusalCode.POINT_SEMANTIC_LABEL_TAKEN)
            if (stored.connector_id, stored.direction, stored.identifier) == (
                point.connector_id,
                point.direction,
                point.identifier,
            ):
                raise DeviceRefusedError(DeviceRefusalCode.POINT_IDENTITY_TAKEN)


@dataclass
class FakeProbe:
    """A `ConnectionProbe` whose outcome the test scripts."""

    report: ProbeReport = field(
        default_factory=lambda: ProbeReport(status=ConnectionState.SUCCESS, model_ids=("m",))
    )
    asked_for: list[str] = field(default_factory=list)

    def probe(self, *, base_url: str) -> ProbeReport:
        self.asked_for.append(base_url)
        return self.report
