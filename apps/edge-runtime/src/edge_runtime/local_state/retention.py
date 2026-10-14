"""本地已上报记录的只读影响事实;不处理媒体保留或执行任何裁剪。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from nvsop_contracts.retention_impact import ReportedImpactQuery

from edge_runtime.local_state.configuration import LocalConfigurationStore


class ReportedImpactUnavailableError(ValueError):
    """当前事实不足以估算;调用方必须保留原因而不能按零放行。"""


@dataclass(frozen=True, slots=True)
class ReportedAggregate:
    """序列化记录的逻辑字节,不是 SQLite 物理缩减量或预测回收空间。"""

    objects: int = 0
    known_bytes: int = 0
    earliest: datetime | None = None
    latest: datetime | None = None

    def include(self, row: dict[str, object], *, acknowledged_at: datetime) -> ReportedAggregate:
        size = len(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        )
        return ReportedAggregate(
            objects=self.objects + 1,
            known_bytes=self.known_bytes + size,
            earliest=acknowledged_at
            if self.earliest is None
            else min(self.earliest, acknowledged_at),
            latest=acknowledged_at if self.latest is None else max(self.latest, acknowledged_at),
        )


@dataclass(frozen=True, slots=True)
class ReportedImpact:
    host_id: str
    configuration_revision: int
    configuration_sha256: str
    exposed: ReportedAggregate
    protected: ReportedAggregate
    bytes_basis: str = "serialized_rows"


# 固定 owner 表与实例关联表达式,不接收客户端 SQL 标识符。
_REPORTED_TABLES = (
    (
        "local_report_queue",
        "coalesce(q.instance_id, (SELECT d.instance_id FROM local_decision d "
        "WHERE d.decision_id=q.decision_id))",
    ),
    ("local_observation_queue", "q.instance_id"),
    ("local_health_queue", None),
    ("local_disposal", "q.violation_instance_id"),
)


def read_reported_impact(path: Path, *, query: ReportedImpactQuery) -> ReportedImpact:
    """在一致的只读快照统计四类已上报记录;未知确认年龄、配置或来源明确拒绝。"""
    connection = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        bundle = LocalConfigurationStore(connection).confirmed()
        if (
            bundle is None
            or bundle.host_id != query.host_id
            or bundle.config_revision != query.configuration_revision
            or bundle.effective_sha256 != query.configuration_sha256
        ):
            raise ReportedImpactUnavailableError("configuration_mismatch")
        stations = {
            station.station_id: station.retention
            for station in bundle.stations
            if station.station_id not in query.excluded_stations
            and (query.station_id is None or station.station_id == query.station_id)
        }
        if query.station_id is not None and query.station_id not in stations:
            raise ReportedImpactUnavailableError("station_scope_unknown")
        for group in stations.values():
            if group is None:
                raise ReportedImpactUnavailableError("retention_configuration_unknown")
            rule = group.local_reported
            baseline = (
                rule.seconds if rule.mode == "duration" else (0 if rule.mode == "discard" else None)
            )
            if baseline != query.baseline_seconds:
                raise ReportedImpactUnavailableError("baseline_mismatch")
        exposed = ReportedAggregate()
        protected = ReportedAggregate()
        for table, instance in _REPORTED_TABLES:
            conditions = ["q.sent_at IS NOT NULL"]
            arguments: list[object] = []
            if query.station_id is not None:
                conditions.append("q.station_id = ?")
                arguments.append(query.station_id)
            if query.excluded_stations:
                conditions.append(
                    "q.station_id NOT IN (" + ",".join("?" for _ in query.excluded_stations) + ")"
                )
                arguments.extend(query.excluded_stations)
            age_conditions = ["julianday(q.acknowledged_at) <= julianday(?)"]
            arguments.append(
                (query.reference_time - timedelta(seconds=query.proposed_seconds)).isoformat()
            )
            if query.baseline_seconds is not None:
                age_conditions.append("julianday(q.acknowledged_at) > julianday(?)")
                arguments.append(
                    (query.reference_time - timedelta(seconds=query.baseline_seconds)).isoformat()
                )
            conditions.append(
                "(q.acknowledged_at IS NULL OR julianday(q.acknowledged_at) IS NULL OR ("
                + " AND ".join(age_conditions)
                + "))"
            )
            protection = (
                "0"
                if instance is None
                else f"""(
                NOT EXISTS (SELECT 1 FROM local_sop_instance i
                    WHERE i.station_id=q.station_id AND i.instance_id={instance}
                    AND i.closed_at IS NOT NULL)
                OR EXISTS (SELECT 1 FROM local_violation v
                    WHERE v.station_id=q.station_id AND v.instance_id={instance})
                OR EXISTS (SELECT 1 FROM local_evidence_queue e
                    WHERE e.station_id=q.station_id AND e.instance_id={instance})
            )"""
            )
            if table == "local_disposal":
                protection = f"(q.violation_ref IS NOT NULL OR {protection})"
            rows = connection.execute(
                f"SELECT q.*, {protection} AS retention_protected FROM {table} q WHERE "
                + " AND ".join(conditions),
                arguments,
            )
            for item in rows:
                row = dict(item)
                is_protected = bool(row.pop("retention_protected"))
                if row["station_id"] not in stations:
                    raise ReportedImpactUnavailableError("station_scope_unknown")
                if row["report_host_id"] != query.host_id:
                    raise ReportedImpactUnavailableError("report_source_unknown")
                acknowledged = row["acknowledged_at"]
                try:
                    if not isinstance(acknowledged, str):
                        raise ValueError
                    at = datetime.fromisoformat(acknowledged)
                    if at.tzinfo is None:
                        raise ValueError
                except ValueError as error:
                    raise ReportedImpactUnavailableError("acknowledgement_utc_unknown") from error
                if is_protected:
                    protected = protected.include(row, acknowledged_at=at)
                else:
                    exposed = exposed.include(row, acknowledged_at=at)
        return ReportedImpact(
            host_id=bundle.host_id,
            configuration_revision=bundle.config_revision,
            configuration_sha256=bundle.effective_sha256,
            exposed=exposed,
            protected=protected,
        )
    finally:
        connection.close()
