"""In-memory stand-ins for the phase-one `device` repository seams.

They replace the adapters at the public seam (harness §4). The backend stand-in is deliberately
only a history reference: backend CRUD and connection/probe behavior are phase two.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from factory_sop.device.errors import DeviceRefusalCode, DeviceRefusedError
from factory_sop.device.model import (
    ConnectionState,
    DeviceStatus,
    InferenceBackend,
    InferenceHost,
)
from factory_sop.identifiers import new_id

FAKE_ACTOR = new_id()
FAKE_NOW = datetime(2026, 9, 5, 8, 0, tzinfo=UTC)


@dataclass
class FakeInferenceHosts:
    """An `InferenceHostRepository` over a dict."""

    rows: dict[UUID, InferenceHost] = field(default_factory=dict)

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
        )
        self.add(host)
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
        return True

    def page_of(self, *, page: int, page_size: int) -> tuple[list[InferenceHost], int]:
        ordered = sorted(
            self.rows.values(), key=lambda item: (item.created_at, item.id), reverse=True
        )
        start = (page - 1) * page_size
        return ordered[start : start + page_size], len(ordered)


@dataclass
class FakeInferenceBackends:
    """The backend-history reference needed by host deletion tests."""

    rows: dict[UUID, InferenceBackend] = field(default_factory=dict)

    def register(
        self,
        *,
        host_id: UUID,
        base_url: str,
        status: DeviceStatus = DeviceStatus.ACTIVE,
        template_version_id: UUID | None = None,
        created_at: datetime = FAKE_NOW,
    ) -> InferenceBackend:
        backend = InferenceBackend(
            id=new_id(),
            host_id=host_id,
            base_url=base_url,
            template_version_id=template_version_id,
            status=status,
            connection_state=ConnectionState.UNVERIFIED,
            connection_checked_at=None,
            connection_detail=None,
            self_reported_model_ids=(),
            self_reported_at=None,
            revision=1,
            created_by=FAKE_ACTOR,
            updated_by=FAKE_ACTOR,
            created_at=created_at,
            updated_at=created_at,
        )
        self.rows[backend.id] = backend
        return backend

    def any_for_host(self, host_id: UUID) -> bool:
        return any(stored.host_id == host_id for stored in self.rows.values())
