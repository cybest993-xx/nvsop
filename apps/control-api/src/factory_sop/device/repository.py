"""The seams `device`'s use cases reach persistence through.

`Protocol`s rather than base classes: the PostgreSQL adapter arrives in this module's
`adapters/`, and the use-case tests pass in-memory stand-ins at this same seam (harness §4).

No method commits — one request is one transaction, opened and committed by the HTTP adapter
layer (ADR-0002). A duplicate natural key refuses at `add` and at `save` (an edit can collide
too, by renaming or by moving an endpoint), and a save whose expected revision no longer
matches refuses rather than overwriting, so two concurrent administrators get one
deterministic outcome instead of a silent last-write-wins.

The lookup names say what they match: `by_id` is the row's UUID (§5.15's URL identity), and
`page_of` is the one paged listing a repository serves.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from factory_sop.device.model import (
    Camera,
    Connector,
    InferenceBackend,
    InferenceHost,
    Point,
    Station,
)


class InferenceHostRepository(Protocol):
    """`device_inference_host`."""

    def add(self, host: InferenceHost) -> None:
        """Insert a host.

        Raises `DeviceRefusedError` with `INFERENCE_HOST_NAME_TAKEN` on a name that is
        already taken: that is a real unique constraint rather than a check the caller makes
        first, so two concurrent creations cannot both succeed.
        """
        ...

    def save(self, host: InferenceHost, *, expected_revision: int) -> None:
        """Write `host`'s fields over the stored row, but only if it still sits at
        `expected_revision`. Raises `STALE_REVISION` when another write moved the row first,
        `INFERENCE_HOST_NOT_FOUND` when the row is gone entirely (§5.15), and
        `INFERENCE_HOST_NAME_TAKEN` when the edit took a name another row already holds.
        """
        ...

    def by_id(self, host_id: UUID) -> InferenceHost | None:
        """Return the host whose row identity is `host_id`, if it still exists."""
        ...

    def remove(self, host_id: UUID, *, expected_revision: int) -> bool:
        """Delete the host only if it still has `expected_revision`.

        Raises `STALE_REVISION` when another write moved the row, `INFERENCE_HOST_NOT_FOUND`
        when the row is gone, and `INFERENCE_HOST_HAS_BACKENDS` if a backend still references
        this host — the foreign key refuses the delete, and the use case refuses it first with
        the same code.
        """
        ...

    def page_of(self, *, page: int, page_size: int) -> tuple[list[InferenceHost], int]:
        """One page of hosts, newest first, with the unpaginated total (§5.15's envelope)."""
        ...


class InferenceBackendRepository(Protocol):
    """`device_inference_backend`."""

    def add(self, backend: InferenceBackend) -> None:
        """Insert a backend, refusing a duplicate host/endpoint pair or missing host."""
        ...

    def save(self, backend: InferenceBackend, *, expected_revision: int) -> None:
        """Write a backend only if its stored revision still equals `expected_revision`."""
        ...

    def by_id(self, backend_id: UUID) -> InferenceBackend | None:
        """Return one backend by its public UUID, if it exists."""
        ...

    def remove(self, backend_id: UUID, *, expected_revision: int) -> bool:
        """Delete one backend only at the revision the caller read."""
        ...

    def any_for_host(self, host_id: UUID) -> bool:
        """Report whether any backend still hangs off a host."""
        ...

    def page_of(
        self, *, page: int, page_size: int, host_id: UUID | None
    ) -> tuple[list[InferenceBackend], int]:
        """Return one newest-first page, optionally filtered to one host."""
        ...


class StationRepository(Protocol):
    """`device_station`，表示独立的 SOP 工作空间。"""

    def add(self, station: Station) -> None:
        """插入工位；重复的自然编码应被拒绝。"""
        ...

    def save(self, station: Station, *, expected_revision: int) -> None:
        """仅在调用方读取的版本号上替换工位。"""
        ...

    def by_id(self, station_id: UUID) -> Station | None:
        """按公开 UUID 返回一个工位；不存在时返回 `None`。"""
        ...

    def remove(self, station_id: UUID, *, expected_revision: int) -> bool:
        """删除工位；仍有相机关联时应拒绝。"""
        ...

    def page_of(self, *, page: int, page_size: int) -> tuple[list[Station], int]:
        """返回按最新优先的一页及总数。"""
        ...


class CameraRepository(Protocol):
    """`device_camera`，包含其工位和推理拓扑。"""

    def add(self, camera: Camera) -> None:
        """插入相机，同时保持数据库拓扑约束。"""
        ...

    def save(self, camera: Camera, *, expected_revision: int) -> None:
        """仅在调用方读取的版本号上替换相机。"""
        ...

    def by_id(self, camera_id: UUID) -> Camera | None:
        """按公开 UUID 返回一个相机；不存在时返回 `None`。"""
        ...

    def remove(self, camera_id: UUID, *, expected_revision: int) -> bool:
        """按调用方读取的版本号删除一个相机。"""
        ...

    def any_for_station(self, station_id: UUID) -> bool:
        """报告工位是否仍有相机历史记录。"""
        ...

    def for_station(self, station_id: UUID) -> list[Camera]:
        """返回分配给工位的全部相机，供拓扑校验使用。"""
        ...

    def page_of(
        self, *, page: int, page_size: int, station_id: UUID | None
    ) -> tuple[list[Camera], int]:
        """返回按最新优先的一页，可选按工位过滤。"""
        ...


class PointRepository(Protocol):
    """`device_point`，保存授权输入/输出点位。"""

    def add(self, point: Point) -> None:
        """插入点位，同时由数据库唯一约束保护语义和物理身份。"""
        ...

    def save(self, point: Point, *, expected_revision: int) -> None:
        """按乐观锁版本替换点位。"""
        ...

    def by_id(self, point_id: UUID) -> Point | None:
        """按公开 UUID 读取点位。"""
        ...

    def remove(self, point_id: UUID, *, expected_revision: int) -> bool:
        """按乐观锁版本删除点位。"""
        ...

    def any_for_station(self, station_id: UUID) -> bool:
        """报告工位是否仍有点位。"""
        ...

    def any_for_connector(self, connector_id: UUID) -> bool:
        """报告连接器是否仍有点位。"""
        ...

    def page_of(
        self,
        *,
        page: int,
        page_size: int,
        station_id: UUID | None,
        connector_id: UUID | None,
    ) -> tuple[list[Point], int]:
        """按创建时间倒序列出点位，可按工位和连接器筛选。"""
        ...


class ConnectorRepository(Protocol):
    """`device_connector`，只保存中心允许的非秘密配置。"""

    def add(self, connector: Connector) -> None:
        """插入连接器；工位内名称必须唯一。"""
        ...

    def save(self, connector: Connector, *, expected_revision: int) -> None:
        """按乐观锁版本替换连接器。"""
        ...

    def by_id(self, connector_id: UUID) -> Connector | None:
        """按公开 UUID 读取连接器。"""
        ...

    def remove(self, connector_id: UUID, *, expected_revision: int) -> bool:
        """按乐观锁版本删除连接器。"""
        ...

    def any_for_station(self, station_id: UUID) -> bool:
        """报告工位是否仍有关联连接器，供工位删除保护使用。"""
        ...

    def any_for_host(self, host_id: UUID) -> bool:
        """报告推理机是否仍承载连接器，供推理机删除保护使用。"""
        ...

    def for_station(self, station_id: UUID) -> list[Connector]:
        """返回工位上的连接器，供相机和连接器拓扑校验使用。"""
        ...

    def page_of(
        self, *, page: int, page_size: int, station_id: UUID | None
    ) -> tuple[list[Connector], int]:
        """按创建时间倒序列出连接器，可按工位筛选。"""
        ...
