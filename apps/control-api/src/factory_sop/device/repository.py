"""device 用例访问持久化的接缝。

使用 `Protocol` 而不是基类：PostgreSQL 适配器位于本模块的 `adapters/`，用例测试在同一接缝传入
内存替身（harness §4）。方法不提交事务；每个请求的事务由 HTTP 适配层开启和提交（ADR-0002）。自然键
由真实唯一约束保护, 因此并发创建和编辑冲突都必须拒绝。保存时比较调用方读取的版本, 不能静默后写覆盖
先写。查询名称说明匹配内容：`by_id` 匹配行 UUID（§5.15），`page_of` 是仓储提供的分页列表。
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from factory_sop.device.model import (
    Camera,
    Connector,
    InferenceBackend,
    InferenceHost,
    PendingCommand,
    PendingCommandCompletion,
    Point,
    Station,
)


class InferenceHostRepository(Protocol):
    """`device_inference_host`."""

    def add(self, host: InferenceHost) -> None:
        """插入推理机；名称冲突由真实唯一约束拒绝，避免并发创建同时成功。"""
        ...

    def save(self, host: InferenceHost, *, expected_revision: int) -> None:
        """仅在存储版本仍为 `expected_revision` 时写入推理机。
        版本过期、记录不存在或名称冲突都拒绝。
        """
        ...

    def by_id(self, host_id: UUID) -> InferenceHost | None:
        """按行 UUID 返回推理机；不存在时返回 `None`。"""
        ...

    def consume_identity_nonce(self, *, host_id: UUID, nonce: str, seen_at: datetime) -> bool:
        """登记一次主机签名随机数；返回 `False` 表示请求可能被重放。"""
        ...

    def remove(self, host_id: UUID, *, expected_revision: int) -> bool:
        """仅在版本仍为 `expected_revision` 时删除推理机。
        版本冲突、记录不存在或仍有后端关联时拒绝。
        """
        ...

    def page_of(self, *, page: int, page_size: int) -> tuple[list[InferenceHost], int]:
        """按最新优先返回一页推理机及未分页总数（§5.15）。"""
        ...


class InferenceBackendRepository(Protocol):
    """`device_inference_backend`."""

    def add(self, backend: InferenceBackend) -> None:
        """插入推理后端；拒绝重复的推理机/端点组合及不存在的推理机。"""
        ...

    def save(self, backend: InferenceBackend, *, expected_revision: int) -> None:
        """仅在存储版本仍为 `expected_revision` 时写入推理后端。"""
        ...

    def by_id(self, backend_id: UUID) -> InferenceBackend | None:
        """按公开 UUID 返回推理后端；不存在时返回 `None`。"""
        ...

    def remove(self, backend_id: UUID, *, expected_revision: int) -> bool:
        """仅按调用方读取的版本删除一个推理后端。"""
        ...

    def any_for_host(self, host_id: UUID) -> bool:
        """报告推理机是否仍承载后端。"""
        ...

    def page_of(
        self, *, page: int, page_size: int, host_id: UUID | None
    ) -> tuple[list[InferenceBackend], int]:
        """按最新优先返回一页推理后端，可按推理机筛选。"""
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

    def by_code(self, code: str) -> Station | None:
        """按自然编码返回一个工位，供跨模块导入匹配。"""
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


class PendingCommandRepository(Protocol):
    """`device_pending_command`，保存中心委托给推理机的设备命令。"""

    def by_id(self, command_id: UUID) -> PendingCommand | None:
        """按公开命令 UUID 读取状态，供操作员查看结果。"""
        ...

    def by_id_for_update(self, command_id: UUID) -> PendingCommand | None:
        """锁定一条命令直到当前请求事务结束，供完成回报串行化。"""
        ...

    def by_idempotency_key(self, key: str) -> PendingCommand | None:
        """按幂等键读取已有命令，重复请求不得再创建一条。"""
        ...

    def add(self, command: PendingCommand) -> None:
        """持久化新命令；供非幂等的内部安排使用。"""
        ...

    def add_or_get(self, command: PendingCommand) -> PendingCommand:
        """原子插入幂等命令，并返回并发竞争中的唯一已持久化命令。"""
        ...

    def claim_next(
        self,
        *,
        host_id: UUID,
        claim_token: str,
        claimed_at: datetime,
        lease_expires_at: datetime,
    ) -> PendingCommand | None:
        """原子领取该推理机的一条命令，并回收已过期的旧领取。"""
        ...

    def complete(self, completion: PendingCommandCompletion) -> PendingCommand:
        """以完整完成上下文回报一次结果，拒绝迟到或他机回报。"""
        ...
