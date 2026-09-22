"""device 两条仓储接缝的 PostgreSQL 适配器。

方法不提交事务；每个请求的事务由 HTTP 适配层开启和提交（ADR-0002）。条件写入和 PostgreSQL 约束错误
在此转换为确定的模块拒绝，而不是 500，便于并发配置变更得到稳定结果。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import (
    CursorResult,
    and_,
    case,
    delete,
    exists,
    func,
    insert,
    inspect,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.device.adapters.repository_support import (
    refuse_constraint_violation as _refuse_constraint_violation,
)
from factory_sop.device.adapters.repository_support import (
    refuse_lost_race as _refuse_lost_race,
)
from factory_sop.device.adapters.tables import (
    CameraRow,
    ConfigurationAssignmentRow,
    ConfigurationIssueRow,
    ConnectorRow,
    InferenceBackendRow,
    InferenceHostIdentityNonceRow,
    InferenceHostRow,
    PointRow,
    StationRow,
)
from factory_sop.device.errors import DeviceRefusalCode, DeviceRefusedError
from factory_sop.device.model import (
    Camera,
    Connector,
    InferenceBackend,
    InferenceHost,
    Point,
    Station,
)
from nvsop_contracts import ConfigurationBundle, capability_to_wire

__all__ = [
    "PostgresCameraRepository",
    "PostgresConnectorRepository",
    "PostgresInferenceBackendRepository",
    "PostgresInferenceHostRepository",
    "PostgresPointRepository",
    "PostgresStationRepository",
]


_HOST_BASE_COLUMNS = (
    "id",
    "name",
    "address",
    "mediamtx_address",
    "recording_window_seconds",
    "disk_watermark_percent",
    "status",
    "revision",
    "created_by",
    "updated_by",
    "created_at",
    "updated_at",
)


def _host_values(
    host: InferenceHost,
    *,
    include_identity: bool,
    include_media: bool,
    include_configuration: bool,
) -> dict[str, object]:
    values: dict[str, object] = {
        "id": host.id,
        "name": host.name,
        "address": host.address,
        "mediamtx_address": host.mediamtx_address,
        "recording_window_seconds": host.recording_window_seconds,
        "disk_watermark_percent": host.disk_watermark_percent,
        "status": host.status,
        "revision": host.revision,
        "created_by": host.created_by,
        "updated_by": host.updated_by,
        "created_at": host.created_at,
        "updated_at": host.updated_at,
    }
    if include_identity:
        values["identity_public_key"] = host.identity_public_key
    if include_media:
        values["mediamtx_playback_address"] = host.mediamtx_playback_address
    if include_configuration:
        values["configuration_revision"] = host.configuration_revision
        values["configuration_sha256"] = host.configuration_sha256
    return values


def _host_from_values(values: Mapping[str, Any]) -> InferenceHost:
    return InferenceHost(
        id=values["id"],
        name=values["name"],
        address=values["address"],
        mediamtx_address=values["mediamtx_address"],
        mediamtx_playback_address=values.get("mediamtx_playback_address"),
        recording_window_seconds=values["recording_window_seconds"],
        disk_watermark_percent=values["disk_watermark_percent"],
        status=values["status"],
        revision=values["revision"],
        configuration_revision=values.get("configuration_revision", 0),
        configuration_sha256=values.get("configuration_sha256"),
        created_by=values["created_by"],
        updated_by=values["updated_by"],
        created_at=values["created_at"],
        updated_at=values["updated_at"],
        identity_public_key=values.get("identity_public_key"),
    )


class PostgresInferenceHostRepository:
    """通过请求级会话访问 `device_inference_host`。"""

    def __init__(self, session: DatabaseSession) -> None:
        self._session = session
        self._has_identity_column: bool | None = None
        self._has_media_column: bool | None = None
        self._has_configuration_columns: bool | None = None

    def add(self, host: InferenceHost) -> None:
        if (
            self._supports_host_identity()
            and self._supports_host_media()
            and self._supports_host_configuration()
        ):
            self._session.add(InferenceHostRow.from_domain(host))
        else:
            if host.identity_public_key is not None and not self._supports_host_identity():
                raise ValueError("旧版主机表不支持公钥身份")
            self._session.execute(
                insert(cast("Any", InferenceHostRow.__table__)).values(
                    **_host_values(
                        host,
                        include_identity=self._supports_host_identity(),
                        include_media=self._supports_host_media(),
                        include_configuration=self._supports_host_configuration(),
                    )
                )
            )
        try:
            self._session.flush()
        except DatabaseError as error:
            _refuse_constraint_violation(error)

    def save(self, host: InferenceHost, *, expected_revision: int) -> None:
        values = _host_values(
            host,
            include_identity=self._supports_host_identity(),
            include_media=self._supports_host_media(),
            include_configuration=self._supports_host_configuration(),
        )
        try:
            result = cast(
                "CursorResult[Any]",
                self._session.execute(
                    update(InferenceHostRow)
                    .where(
                        InferenceHostRow.id == host.id,
                        InferenceHostRow.revision == expected_revision,
                    )
                    .values(**values)
                ),
            )
        except DatabaseError as error:
            _refuse_constraint_violation(error)
        if result.rowcount == 0:
            _refuse_lost_race(
                missing_refusal=DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND,
                present_refusal=DeviceRefusalCode.STALE_REVISION,
                row=self._session.scalar(
                    select(InferenceHostRow.id).where(InferenceHostRow.id == host.id)
                ),
            )

    def by_id(self, host_id: UUID) -> InferenceHost | None:
        if (
            self._supports_host_identity()
            and self._supports_host_media()
            and self._supports_host_configuration()
        ):
            row = self._session.get(InferenceHostRow, host_id)
            return row.to_domain() if row is not None else None
        values = self._legacy_host_by_id(host_id)
        return _host_from_values(values) if values is not None else None

    def _supports_host_identity(self) -> bool:
        if self._has_identity_column is None:
            self._has_identity_column = any(
                column["name"] == "identity_public_key"
                for column in inspect(self._session.connection()).get_columns(
                    InferenceHostRow.__tablename__
                )
            )
        return self._has_identity_column

    def _supports_host_media(self) -> bool:
        if self._has_media_column is None:
            self._has_media_column = any(
                column["name"] == "mediamtx_playback_address"
                for column in inspect(self._session.connection()).get_columns(
                    InferenceHostRow.__tablename__
                )
            )
        return self._has_media_column

    def _supports_host_configuration(self) -> bool:
        if self._has_configuration_columns is None:
            columns = {
                column["name"]
                for column in inspect(self._session.connection()).get_columns(
                    InferenceHostRow.__tablename__
                )
            }
            self._has_configuration_columns = {
                "configuration_revision",
                "configuration_sha256",
            }.issubset(columns)
        return self._has_configuration_columns

    def _host_columns(self) -> list[Any]:
        names = list(_HOST_BASE_COLUMNS)
        if self._supports_host_media():
            names.insert(names.index("mediamtx_address") + 1, "mediamtx_playback_address")
        if self._supports_host_identity():
            names.append("identity_public_key")
        if self._supports_host_configuration():
            names.extend(("configuration_revision", "configuration_sha256"))
        return [getattr(InferenceHostRow, name) for name in names]

    def _legacy_host_by_id(self, host_id: UUID) -> Mapping[str, Any] | None:
        columns = self._host_columns()
        return cast(
            "Mapping[str, Any] | None",
            self._session.execute(select(*columns).where(InferenceHostRow.id == host_id))
            .mappings()
            .one_or_none(),
        )

    def next_configuration_revision(
        self,
        *,
        host_id: UUID,
        content_sha256: str,
        minimum_revision: int = 0,
    ) -> int:
        """按配置内容分配持久化、单调且并发安全的主机版本。"""
        if minimum_revision < 0:
            raise ValueError("minimum_revision must not be negative")
        if not self._supports_host_configuration():
            raise ValueError("主机表不支持持久化配置版本")
        row = self._session.execute(
            select(
                InferenceHostRow.configuration_revision,
                InferenceHostRow.configuration_sha256,
            )
            .where(InferenceHostRow.id == host_id)
            .with_for_update()
        ).one_or_none()
        if row is None:
            raise DeviceRefusedError(DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND)
        if row[1] == content_sha256:
            return int(row[0])
        revision = max(1, int(row[0]) + 1)
        if row[1] is None:
            revision = max(revision, minimum_revision + 1)
        self._session.execute(
            update(InferenceHostRow)
            .where(InferenceHostRow.id == host_id)
            .values(configuration_revision=revision, configuration_sha256=content_sha256)
        )
        return revision

    def configuration_was_issued(
        self,
        *,
        host_id: UUID,
        configuration_revision: int,
        configuration_sha256: str,
    ) -> bool:
        """验证 host/revision/effective-digest 是否由 Center 实际生成过。"""
        return bool(
            self._session.scalar(
                select(
                    exists().where(
                        ConfigurationIssueRow.host_id == host_id,
                        ConfigurationIssueRow.configuration_revision == configuration_revision,
                        ConfigurationIssueRow.configuration_sha256 == configuration_sha256,
                    )
                )
            )
        )

    def _record_configuration_issue(self, bundle: ConfigurationBundle) -> None:
        key = {
            "host_id": UUID(bundle.host_id),
            "configuration_revision": bundle.config_revision,
        }
        existing = self._session.scalar(
            select(ConfigurationIssueRow).where(
                ConfigurationIssueRow.host_id == key["host_id"],
                ConfigurationIssueRow.configuration_revision == key["configuration_revision"],
            )
        )
        if existing is None:
            self._session.add(
                ConfigurationIssueRow(
                    **key,
                    configuration_sha256=bundle.effective_sha256,
                )
            )
            self._session.flush()
            return
        if existing.configuration_sha256 != bundle.effective_sha256:
            raise ValueError("configuration revision conflicts with immutable issued history")

    def record_configuration_assignments(self, bundle: ConfigurationBundle) -> None:
        """只追加 Center 实际组装并下发过的配置归属；同 revision 内容不得改写。"""
        self._record_configuration_issue(bundle)
        host_id = UUID(bundle.host_id)
        for station in bundle.stations:
            key = {
                "host_id": host_id,
                "configuration_revision": bundle.config_revision,
                "station_id": UUID(station.station_id),
                "backend_id": UUID(station.backend_id),
            }
            expected = {
                "configuration_sha256": bundle.effective_sha256,
                "template_version_id": (
                    None if station.template is None else UUID(station.template.version_id)
                ),
                "template_sha256": (
                    None if station.template is None else station.template.version_sha256
                ),
                "model_ids": list(station.model_ids),
            }
            existing = self._session.scalar(
                select(ConfigurationAssignmentRow).where(
                    ConfigurationAssignmentRow.host_id == key["host_id"],
                    ConfigurationAssignmentRow.configuration_revision
                    == key["configuration_revision"],
                    ConfigurationAssignmentRow.station_id == key["station_id"],
                    ConfigurationAssignmentRow.backend_id == key["backend_id"],
                )
            )
            if existing is None:
                self._session.add(ConfigurationAssignmentRow(**key, **expected))
                self._session.flush()
                continue
            actual = {
                "configuration_sha256": existing.configuration_sha256,
                "template_version_id": existing.template_version_id,
                "template_sha256": existing.template_sha256,
                "model_ids": existing.model_ids,
            }
            if actual != expected:
                raise ValueError(
                    "configuration assignment revision conflicts with immutable history"
                )

    def has_configuration_station(
        self,
        *,
        host_id: UUID,
        configuration_revision: int,
        configuration_sha256: str,
        station_id: UUID,
        template_version_id: str | None,
        template_sha256: str | None,
    ) -> bool:
        """验证该 station/template 精确存在于 Center 已固化的历史配置。"""
        rows = self._session.scalars(
            select(ConfigurationAssignmentRow).where(
                ConfigurationAssignmentRow.host_id == host_id,
                ConfigurationAssignmentRow.configuration_revision == configuration_revision,
                ConfigurationAssignmentRow.configuration_sha256 == configuration_sha256,
                ConfigurationAssignmentRow.station_id == station_id,
            )
        ).all()
        return any(
            (None if row.template_version_id is None else str(row.template_version_id))
            == template_version_id
            and row.template_sha256 == template_sha256
            for row in rows
        )

    def has_configuration_assignment(
        self,
        *,
        host_id: UUID,
        configuration_revision: int,
        configuration_sha256: str,
        station_id: UUID,
        backend_id: UUID,
        template_version_id: str | None,
        template_sha256: str | None,
        model_ids: tuple[str, ...],
    ) -> bool:
        """验证报告完整上下文是否精确匹配 Center 已下发的历史 assignment。"""
        row = self._session.scalar(
            select(ConfigurationAssignmentRow).where(
                ConfigurationAssignmentRow.host_id == host_id,
                ConfigurationAssignmentRow.configuration_revision == configuration_revision,
                ConfigurationAssignmentRow.station_id == station_id,
                ConfigurationAssignmentRow.backend_id == backend_id,
            )
        )
        if row is None:
            return False
        return (
            row.configuration_sha256 == configuration_sha256
            and (None if row.template_version_id is None else str(row.template_version_id))
            == template_version_id
            and row.template_sha256 == template_sha256
            and tuple(row.model_ids) == model_ids
        )

    def consume_identity_nonce(self, *, host_id: UUID, nonce: str, seen_at: datetime) -> bool:
        """原子登记随机数；重复请求在事务内被拒绝。"""
        self._session.execute(
            delete(InferenceHostIdentityNonceRow).where(
                InferenceHostIdentityNonceRow.host_id == host_id,
                InferenceHostIdentityNonceRow.seen_at < seen_at - timedelta(minutes=10),
            )
        )
        inserted = self._session.execute(
            postgres_insert(InferenceHostIdentityNonceRow)
            .values(host_id=host_id, nonce=nonce, seen_at=seen_at)
            .on_conflict_do_nothing(
                index_elements=[
                    InferenceHostIdentityNonceRow.host_id,
                    InferenceHostIdentityNonceRow.nonce,
                ]
            )
            .returning(InferenceHostIdentityNonceRow.nonce)
        ).scalar_one_or_none()
        return inserted is not None

    def remove(self, host_id: UUID, *, expected_revision: int) -> bool:
        try:
            result = cast(
                "CursorResult[Any]",
                self._session.execute(
                    delete(InferenceHostRow).where(
                        InferenceHostRow.id == host_id,
                        InferenceHostRow.revision == expected_revision,
                    )
                ),
            )
        except DatabaseError as error:
            # 删除前由用例检查关联，这里保留外键拒绝作为数据库后备保护。
            _refuse_constraint_violation(
                error,
                foreign_key_to_host=DeviceRefusalCode.INFERENCE_HOST_HAS_BACKENDS,
                foreign_key_to_connector=DeviceRefusalCode.INFERENCE_HOST_HAS_CONNECTORS,
                host_report_refusal=DeviceRefusalCode.INFERENCE_HOST_HAS_CONFIGURATION_REPORT,
                pending_command_refusal=DeviceRefusalCode.INFERENCE_HOST_HAS_PENDING_COMMANDS,
                active_execution_grant_refusal=(
                    DeviceRefusalCode.INFERENCE_HOST_HAS_ACTIVE_EXECUTION_GRANT
                ),
            )
        if result.rowcount == 0:
            _refuse_lost_race(
                missing_refusal=DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND,
                present_refusal=DeviceRefusalCode.STALE_REVISION,
                row=self._session.scalar(
                    select(InferenceHostRow.id).where(InferenceHostRow.id == host_id)
                ),
            )
        return True

    def page_of(self, *, page: int, page_size: int) -> tuple[list[InferenceHost], int]:
        total = cast(
            "int", self._session.scalar(select(func.count()).select_from(InferenceHostRow))
        )
        if (
            self._supports_host_identity()
            and self._supports_host_media()
            and self._supports_host_configuration()
        ):
            rows = self._session.scalars(
                select(InferenceHostRow)
                .order_by(InferenceHostRow.created_at.desc(), InferenceHostRow.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            return [row.to_domain() for row in rows], total

        columns = self._host_columns()
        legacy_rows = (
            self._session.execute(
                select(*columns)
                .order_by(InferenceHostRow.created_at.desc(), InferenceHostRow.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            .mappings()
            .all()
        )
        return [_host_from_values(cast("Mapping[str, Any]", row)) for row in legacy_rows], total


class PostgresInferenceBackendRepository:
    """`device_inference_backend` through the request's session."""

    def __init__(self, session: DatabaseSession) -> None:
        self._session = session

    def add(self, backend: InferenceBackend) -> None:
        self._session.add(InferenceBackendRow.from_domain(backend))
        try:
            self._session.flush()
        except DatabaseError as error:
            _refuse_constraint_violation(
                error,
                foreign_key_to_host=DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND,
            )

    def save(self, backend: InferenceBackend, *, expected_revision: int) -> None:
        values = {
            "host_id": backend.host_id,
            "base_url": backend.base_url,
            "template_version_id": backend.template_version_id,
            "status": backend.status,
            "connection_state": backend.connection_state,
            "connection_checked_at": backend.connection_checked_at,
            "connection_detail": backend.connection_detail,
            "self_reported_model_ids": list(backend.self_reported_model_ids),
            "self_reported_at": backend.self_reported_at,
            "revision": backend.revision,
            "updated_by": backend.updated_by,
            "updated_at": backend.updated_at,
        }
        try:
            result = cast(
                "CursorResult[Any]",
                self._session.execute(
                    update(InferenceBackendRow)
                    .where(
                        InferenceBackendRow.id == backend.id,
                        InferenceBackendRow.revision == expected_revision,
                    )
                    .values(**values)
                ),
            )
        except DatabaseError as error:
            _refuse_constraint_violation(error)
        if result.rowcount == 0:
            _refuse_lost_race(
                missing_refusal=DeviceRefusalCode.INFERENCE_BACKEND_NOT_FOUND,
                present_refusal=DeviceRefusalCode.STALE_REVISION,
                row=self._session.get(InferenceBackendRow, backend.id),
            )

    def by_id(self, backend_id: UUID) -> InferenceBackend | None:
        row = self._session.get(InferenceBackendRow, backend_id)
        return row.to_domain() if row is not None else None

    def assign_template_version(
        self,
        *,
        backend_ids: tuple[UUID, ...],
        template_version_id: UUID,
        actor_id: UUID,
        now: datetime,
        expected_revisions: dict[UUID, int],
    ) -> tuple[InferenceBackend, ...]:
        """在一个条件 UPDATE 中更新参与后端的单一模板配置槽位。"""
        if set(backend_ids) != set(expected_revisions):
            raise ValueError("backend revisions must cover exactly the participating backends")
        if not backend_ids:
            return ()
        self.lock_template_binding_topology(backend_ids)
        conditions = tuple(
            and_(InferenceBackendRow.id == backend_id, InferenceBackendRow.revision == revision)
            for backend_id, revision in expected_revisions.items()
        )
        revision_case = case(
            {backend_id: expected_revisions[backend_id] + 1 for backend_id in backend_ids},
            value=InferenceBackendRow.id,
        )
        try:
            result = cast(
                "CursorResult[Any]",
                self._session.execute(
                    update(InferenceBackendRow)
                    .where(or_(*conditions))
                    .values(
                        template_version_id=template_version_id,
                        revision=revision_case,
                        updated_by=actor_id,
                        updated_at=now,
                    )
                ),
            )
        except DatabaseError as error:
            _refuse_constraint_violation(error)
        if result.rowcount != len(backend_ids):
            missing = next(
                (
                    backend_id
                    for backend_id in backend_ids
                    if self._session.get(InferenceBackendRow, backend_id) is None
                ),
                None,
            )
            if missing is not None:
                raise DeviceRefusedError(DeviceRefusalCode.INFERENCE_BACKEND_NOT_FOUND)
            raise DeviceRefusedError(DeviceRefusalCode.STALE_REVISION)
        rows = self._session.scalars(
            select(InferenceBackendRow).where(InferenceBackendRow.id.in_(backend_ids))
        ).all()
        by_id = {row.id: row.to_domain() for row in rows}
        try:
            return tuple(by_id[backend_id] for backend_id in backend_ids)
        except KeyError as error:
            raise RuntimeError("updated inference backend disappeared") from error

    def lock_template_binding_topology(self, backend_ids: tuple[UUID, ...]) -> None:
        """以 backend→host→station→camera 顺序锁住批量模板切换的拓扑。"""
        ordered_backend_ids = tuple(sorted(set(backend_ids), key=str))
        host_ids: list[UUID] = []
        for backend_id in ordered_backend_ids:
            host_id = self._session.execute(
                select(InferenceBackendRow.host_id)
                .where(InferenceBackendRow.id == backend_id)
                .with_for_update()
            ).scalar_one_or_none()
            if host_id is not None and host_id not in host_ids:
                host_ids.append(host_id)

        for host_id in sorted(host_ids, key=str):
            self._session.execute(
                select(InferenceHostRow.id).where(InferenceHostRow.id == host_id).with_for_update()
            ).scalar_one_or_none()

        station_ids = self._session.scalars(
            select(CameraRow.station_id)
            .where(CameraRow.backend_id.in_(ordered_backend_ids))
            .distinct()
            .order_by(CameraRow.station_id)
        ).all()
        for station_id in station_ids:
            self._session.execute(
                select(StationRow.id).where(StationRow.id == station_id).with_for_update()
            ).scalar_one_or_none()

        camera_ids = self._session.scalars(
            select(CameraRow.id)
            .where(CameraRow.backend_id.in_(ordered_backend_ids))
            .order_by(CameraRow.id)
        ).all()
        for camera_id in camera_ids:
            self._session.execute(
                select(CameraRow.id).where(CameraRow.id == camera_id).with_for_update()
            ).scalar_one_or_none()

    def remove(self, backend_id: UUID, *, expected_revision: int) -> bool:
        try:
            result = cast(
                "CursorResult[Any]",
                self._session.execute(
                    delete(InferenceBackendRow).where(
                        InferenceBackendRow.id == backend_id,
                        InferenceBackendRow.revision == expected_revision,
                    )
                ),
            )
        except DatabaseError as error:
            _refuse_constraint_violation(
                error,
                foreign_key_to_backend=DeviceRefusalCode.INFERENCE_BACKEND_HAS_CAMERAS,
                backend_report_refusal=DeviceRefusalCode.INFERENCE_BACKEND_HAS_CONFIGURATION_REPORT,
            )
        if result.rowcount == 0:
            _refuse_lost_race(
                missing_refusal=DeviceRefusalCode.INFERENCE_BACKEND_NOT_FOUND,
                present_refusal=DeviceRefusalCode.STALE_REVISION,
                row=self._session.get(InferenceBackendRow, backend_id),
            )
        return True

    def any_for_host(self, host_id: UUID) -> bool:
        return bool(
            self._session.scalar(select(exists().where(InferenceBackendRow.host_id == host_id)))
        )

    def page_of(
        self, *, page: int, page_size: int, host_id: UUID | None
    ) -> tuple[list[InferenceBackend], int]:
        query = select(func.count()).select_from(InferenceBackendRow)
        if host_id is not None:
            query = query.where(InferenceBackendRow.host_id == host_id)
        total = cast("int", self._session.scalar(query))

        listing = select(InferenceBackendRow)
        if host_id is not None:
            listing = listing.where(InferenceBackendRow.host_id == host_id)
        rows = self._session.scalars(
            listing.order_by(InferenceBackendRow.created_at.desc(), InferenceBackendRow.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return [row.to_domain() for row in rows], total


class PostgresStationRepository:
    def __init__(self, session: DatabaseSession) -> None:
        self._session = session

    def add(self, station: Station) -> None:
        self._session.add(StationRow.from_domain(station))
        try:
            self._session.flush()
        except DatabaseError as error:
            _refuse_constraint_violation(error)

    def save(self, station: Station, *, expected_revision: int) -> None:
        try:
            result = cast(
                "CursorResult[Any]",
                self._session.execute(
                    update(StationRow)
                    .where(StationRow.id == station.id, StationRow.revision == expected_revision)
                    .values(
                        code=station.code,
                        name=station.name,
                        tags=list(station.tags),
                        status=station.status,
                        runtime_parameter_mode=station.runtime_parameter_mode,
                        runtime_parameter_overrides=(
                            None
                            if station.runtime_parameter_overrides is None
                            else station.runtime_parameter_overrides.to_wire()
                        ),
                        runtime_parameters_revision=station.runtime_parameters_revision,
                        revision=station.revision,
                        updated_by=station.updated_by,
                        updated_at=station.updated_at,
                    )
                ),
            )
        except DatabaseError as error:
            _refuse_constraint_violation(error)
        if result.rowcount == 0:
            _refuse_lost_race(
                missing_refusal=DeviceRefusalCode.STATION_NOT_FOUND,
                present_refusal=DeviceRefusalCode.STALE_REVISION,
                row=self._session.get(StationRow, station.id),
            )

    def by_id(self, station_id: UUID) -> Station | None:
        row = self._session.get(StationRow, station_id)
        return row.to_domain() if row is not None else None

    def by_code(self, code: str) -> Station | None:
        row = self._session.scalar(select(StationRow).where(StationRow.code == code))
        return row.to_domain() if row is not None else None

    def remove(self, station_id: UUID, *, expected_revision: int) -> bool:
        try:
            result = cast(
                "CursorResult[Any]",
                self._session.execute(
                    delete(StationRow).where(
                        StationRow.id == station_id,
                        StationRow.revision == expected_revision,
                    )
                ),
            )
        except DatabaseError as error:
            _refuse_constraint_violation(
                error,
                foreign_key_to_station=DeviceRefusalCode.STATION_HAS_CAMERAS,
                foreign_key_to_connector=DeviceRefusalCode.STATION_HAS_CONNECTORS,
                point_station_refusal=DeviceRefusalCode.STATION_HAS_POINTS,
                station_template_refusal=DeviceRefusalCode.STATION_HAS_TEMPLATES,
                station_binding_refusal=DeviceRefusalCode.STATION_HAS_TEMPLATE_BINDING,
                station_report_refusal=DeviceRefusalCode.STATION_HAS_CONFIGURATION_REPORT,
                active_execution_grant_refusal=DeviceRefusalCode.STATION_HAS_ACTIVE_EXECUTION_GRANT,
            )
        if result.rowcount == 0:
            _refuse_lost_race(
                missing_refusal=DeviceRefusalCode.STATION_NOT_FOUND,
                present_refusal=DeviceRefusalCode.STALE_REVISION,
                row=self._session.get(StationRow, station_id),
            )
        return True

    def page_of(self, *, page: int, page_size: int) -> tuple[list[Station], int]:
        total = cast("int", self._session.scalar(select(func.count()).select_from(StationRow)))
        rows = self._session.scalars(
            select(StationRow)
            .order_by(StationRow.created_at.desc(), StationRow.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return [row.to_domain() for row in rows], total


class PostgresCameraRepository:
    def __init__(self, session: DatabaseSession) -> None:
        self._session = session

    def add(self, camera: Camera) -> None:
        self._session.add(CameraRow.from_domain(camera))
        try:
            self._session.flush()
        except DatabaseError as error:
            _refuse_constraint_violation(
                error,
                foreign_key_to_host=DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND,
                foreign_key_to_station=DeviceRefusalCode.STATION_NOT_FOUND,
                foreign_key_to_backend=DeviceRefusalCode.INFERENCE_BACKEND_NOT_FOUND,
            )

    def lock_topology(self, camera_id: UUID) -> Camera | None:
        """按 backend→host→station→camera 顺序锁定并刷新相机。"""
        placement = self._session.execute(
            select(CameraRow.backend_id, CameraRow.host_id, CameraRow.station_id).where(
                CameraRow.id == camera_id
            )
        ).one_or_none()
        if placement is None:
            return None
        backend_id, host_id, station_id = placement
        self._session.execute(
            select(InferenceBackendRow.id)
            .where(InferenceBackendRow.id == backend_id)
            .with_for_update()
        ).scalar_one_or_none()
        self._session.execute(
            select(InferenceHostRow.id).where(InferenceHostRow.id == host_id).with_for_update()
        ).scalar_one_or_none()
        self._session.execute(
            select(StationRow.id).where(StationRow.id == station_id).with_for_update()
        ).scalar_one_or_none()
        row = self._session.scalar(
            select(CameraRow)
            .where(CameraRow.id == camera_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return row.to_domain() if row is not None else None

    def save(self, camera: Camera, *, expected_revision: int) -> None:
        try:
            result = cast(
                "CursorResult[Any]",
                self._session.execute(
                    update(CameraRow)
                    .where(CameraRow.id == camera.id, CameraRow.revision == expected_revision)
                    .values(
                        name=camera.name,
                        address=camera.address,
                        main_stream_path=camera.main_stream_path,
                        sub_stream_path=camera.sub_stream_path,
                        media_path_mode=camera.media_path_mode,
                        recording_mode=camera.recording_mode,
                        credentials_configured=camera.credentials_configured,
                        station_id=camera.station_id,
                        host_id=camera.host_id,
                        backend_id=camera.backend_id,
                        status=camera.status,
                        revision=camera.revision,
                        updated_by=camera.updated_by,
                        updated_at=camera.updated_at,
                    )
                ),
            )
        except DatabaseError as error:
            _refuse_constraint_violation(
                error,
                foreign_key_to_host=DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND,
                foreign_key_to_station=DeviceRefusalCode.STATION_NOT_FOUND,
                foreign_key_to_backend=DeviceRefusalCode.INFERENCE_BACKEND_NOT_FOUND,
            )
        if result.rowcount == 0:
            _refuse_lost_race(
                missing_refusal=DeviceRefusalCode.CAMERA_NOT_FOUND,
                present_refusal=DeviceRefusalCode.STALE_REVISION,
                row=self._session.get(CameraRow, camera.id),
            )

    def by_id(self, camera_id: UUID) -> Camera | None:
        row = self._session.get(CameraRow, camera_id)
        return row.to_domain() if row is not None else None

    def remove(self, camera_id: UUID, *, expected_revision: int) -> bool:
        self.lock_topology(camera_id)
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                delete(CameraRow).where(
                    CameraRow.id == camera_id,
                    CameraRow.revision == expected_revision,
                )
            ),
        )
        if result.rowcount == 0:
            _refuse_lost_race(
                missing_refusal=DeviceRefusalCode.CAMERA_NOT_FOUND,
                present_refusal=DeviceRefusalCode.STALE_REVISION,
                row=self._session.get(CameraRow, camera_id),
            )
        return True

    def any_for_station(self, station_id: UUID) -> bool:
        return bool(
            self._session.scalar(select(exists().where(CameraRow.station_id == station_id)))
        )

    def for_station(self, station_id: UUID) -> list[Camera]:
        rows = self._session.scalars(
            select(CameraRow)
            .where(CameraRow.station_id == station_id)
            .order_by(CameraRow.created_at, CameraRow.id)
        ).all()
        return [row.to_domain() for row in rows]

    def for_host(self, host_id: UUID) -> list[Camera]:
        rows = self._session.scalars(
            select(CameraRow)
            .where(CameraRow.host_id == host_id)
            .order_by(CameraRow.created_at, CameraRow.id)
        ).all()
        return [row.to_domain() for row in rows]

    def any_for_backend_outside_station(self, backend_id: UUID, station_id: UUID) -> bool:
        return bool(
            self._session.scalar(
                select(
                    exists().where(
                        CameraRow.backend_id == backend_id,
                        CameraRow.station_id != station_id,
                    )
                )
            )
        )

    def page_of(
        self, *, page: int, page_size: int, station_id: UUID | None
    ) -> tuple[list[Camera], int]:
        query = select(func.count()).select_from(CameraRow)
        listing = select(CameraRow)
        if station_id is not None:
            query = query.where(CameraRow.station_id == station_id)
            listing = listing.where(CameraRow.station_id == station_id)
        total = cast("int", self._session.scalar(query))
        rows = self._session.scalars(
            listing.order_by(CameraRow.created_at.desc(), CameraRow.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return [row.to_domain() for row in rows], total


class PostgresPointRepository:
    """通过请求事务保存 device_point。"""

    def __init__(self, session: DatabaseSession) -> None:
        self._session = session

    def add(self, point: Point) -> None:
        self._session.add(PointRow.from_domain(point))
        try:
            self._session.flush()
        except DatabaseError as error:
            _refuse_constraint_violation(
                error,
                point_station_refusal=DeviceRefusalCode.STATION_NOT_FOUND,
                point_connector_refusal=DeviceRefusalCode.CONNECTOR_NOT_FOUND,
            )

    def save(self, point: Point, *, expected_revision: int) -> None:
        try:
            result = cast(
                "CursorResult[Any]",
                self._session.execute(
                    update(PointRow)
                    .where(PointRow.id == point.id, PointRow.revision == expected_revision)
                    .values(
                        station_id=point.station_id,
                        connector_id=point.connector_id,
                        direction=point.direction,
                        identifier=point.identifier,
                        semantic_label=point.semantic_label,
                        status=point.status,
                        revision=point.revision,
                        updated_by=point.updated_by,
                        updated_at=point.updated_at,
                    )
                ),
            )
        except DatabaseError as error:
            _refuse_constraint_violation(
                error,
                point_station_refusal=DeviceRefusalCode.STATION_NOT_FOUND,
                point_connector_refusal=DeviceRefusalCode.CONNECTOR_NOT_FOUND,
            )
        if result.rowcount == 0:
            _refuse_lost_race(
                missing_refusal=DeviceRefusalCode.POINT_NOT_FOUND,
                present_refusal=DeviceRefusalCode.STALE_REVISION,
                row=self._session.scalar(
                    select(PointRow)
                    .where(PointRow.id == point.id)
                    .execution_options(populate_existing=True)
                ),
            )

    def by_id(self, point_id: UUID) -> Point | None:
        row = self._session.get(PointRow, point_id)
        return row.to_domain() if row is not None else None

    def for_station(self, station_id: UUID) -> list[Point]:
        rows = self._session.scalars(
            select(PointRow)
            .where(PointRow.station_id == station_id)
            .order_by(PointRow.created_at, PointRow.id)
        ).all()
        return [row.to_domain() for row in rows]

    def remove(self, point_id: UUID, *, expected_revision: int) -> bool:
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                delete(PointRow).where(
                    PointRow.id == point_id,
                    PointRow.revision == expected_revision,
                )
            ),
        )
        if result.rowcount == 0:
            _refuse_lost_race(
                missing_refusal=DeviceRefusalCode.POINT_NOT_FOUND,
                present_refusal=DeviceRefusalCode.STALE_REVISION,
                row=self._session.get(PointRow, point_id),
            )
        return True

    def any_for_station(self, station_id: UUID) -> bool:
        return bool(self._session.scalar(select(exists().where(PointRow.station_id == station_id))))

    def any_for_connector(self, connector_id: UUID) -> bool:
        return bool(
            self._session.scalar(select(exists().where(PointRow.connector_id == connector_id)))
        )

    def page_of(
        self,
        *,
        page: int,
        page_size: int,
        station_id: UUID | None,
        connector_id: UUID | None,
    ) -> tuple[list[Point], int]:
        count = select(func.count()).select_from(PointRow)
        listing = select(PointRow)
        if station_id is not None:
            count = count.where(PointRow.station_id == station_id)
            listing = listing.where(PointRow.station_id == station_id)
        if connector_id is not None:
            count = count.where(PointRow.connector_id == connector_id)
            listing = listing.where(PointRow.connector_id == connector_id)
        total = cast("int", self._session.scalar(count))
        rows = self._session.scalars(
            listing.order_by(PointRow.created_at.desc(), PointRow.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return [row.to_domain() for row in rows], total


class PostgresConnectorRepository:
    """`device_connector` through the request's session."""

    def __init__(self, session: DatabaseSession) -> None:
        self._session = session

    def add(self, connector: Connector) -> None:
        self._session.add(ConnectorRow.from_domain(connector))
        try:
            self._session.flush()
        except DatabaseError as error:
            _refuse_constraint_violation(
                error,
                foreign_key_to_station=DeviceRefusalCode.STATION_NOT_FOUND,
                foreign_key_to_host=DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND,
            )

    def save(self, connector: Connector, *, expected_revision: int) -> None:
        try:
            result = cast(
                "CursorResult[Any]",
                self._session.execute(
                    update(ConnectorRow)
                    .where(
                        ConnectorRow.id == connector.id,
                        ConnectorRow.revision == expected_revision,
                    )
                    .values(
                        station_id=connector.station_id,
                        host_id=connector.host_id,
                        name=connector.name,
                        connector_type=connector.connector_type,
                        configuration=connector.configuration.to_wire(),
                        credentials_configured=connector.credentials_configured,
                        reachability=connector.reachability,
                        health_detail=connector.health_detail,
                        capability=capability_to_wire(connector.capability),
                        status=connector.status,
                        revision=connector.revision,
                        updated_by=connector.updated_by,
                        updated_at=connector.updated_at,
                    )
                ),
            )
        except DatabaseError as error:
            _refuse_constraint_violation(
                error,
                foreign_key_to_station=DeviceRefusalCode.STATION_NOT_FOUND,
                foreign_key_to_host=DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND,
            )
        if result.rowcount == 0:
            _refuse_lost_race(
                missing_refusal=DeviceRefusalCode.CONNECTOR_NOT_FOUND,
                present_refusal=DeviceRefusalCode.STALE_REVISION,
                row=self._session.get(ConnectorRow, connector.id),
            )

    def by_id(self, connector_id: UUID) -> Connector | None:
        row = self._session.get(ConnectorRow, connector_id)
        return row.to_domain() if row is not None else None

    def remove(self, connector_id: UUID, *, expected_revision: int) -> bool:
        try:
            result = cast(
                "CursorResult[Any]",
                self._session.execute(
                    delete(ConnectorRow).where(
                        ConnectorRow.id == connector_id,
                        ConnectorRow.revision == expected_revision,
                    )
                ),
            )
        except DatabaseError as error:
            _refuse_constraint_violation(
                error,
                point_connector_refusal=DeviceRefusalCode.CONNECTOR_HAS_POINTS,
            )
        if result.rowcount == 0:
            _refuse_lost_race(
                missing_refusal=DeviceRefusalCode.CONNECTOR_NOT_FOUND,
                present_refusal=DeviceRefusalCode.STALE_REVISION,
                row=self._session.get(ConnectorRow, connector_id),
            )
        return True

    def any_for_station(self, station_id: UUID) -> bool:
        return bool(
            self._session.scalar(select(exists().where(ConnectorRow.station_id == station_id)))
        )

    def any_for_host(self, host_id: UUID) -> bool:
        return bool(self._session.scalar(select(exists().where(ConnectorRow.host_id == host_id))))

    def for_station(self, station_id: UUID) -> list[Connector]:
        rows = self._session.scalars(
            select(ConnectorRow)
            .where(ConnectorRow.station_id == station_id)
            .order_by(ConnectorRow.created_at, ConnectorRow.id)
        ).all()
        return [row.to_domain() for row in rows]

    def page_of(
        self, *, page: int, page_size: int, station_id: UUID | None
    ) -> tuple[list[Connector], int]:
        query = select(func.count()).select_from(ConnectorRow)
        listing = select(ConnectorRow)
        if station_id is not None:
            query = query.where(ConnectorRow.station_id == station_id)
            listing = listing.where(ConnectorRow.station_id == station_id)
        total = cast("int", self._session.scalar(query))
        rows = self._session.scalars(
            listing.order_by(ConnectorRow.created_at.desc(), ConnectorRow.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return [row.to_domain() for row in rows], total
