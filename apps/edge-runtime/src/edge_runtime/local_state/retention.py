"""本地已上报记录的只读影响事实与有界逻辑裁剪;不处理媒体保留或删除媒体。

影响估算与回收共享同一保护含义(未结实例/已锁存违规/未登记证据引用),避免规则漂移;
裁剪只按最后确认配置的每工位 ``local_reported`` 删除 ``local_report_queue``/
``local_observation_queue``/``local_health_queue`` 的逻辑行,绝不触碰证据队列/媒体、
实例/判定/违规身份或处置去重账本。
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nvsop_contracts import ResolvedRetentionPolicy
from nvsop_contracts.retention_impact import ReportedImpactQuery

from edge_runtime.local_state.configuration import LocalConfigurationStore

DEFAULT_RECLAIM_LIMIT = 256
"""单次裁剪最多检查的逻辑行数;运行时按既有上报节奏以更小预算周期调用。"""


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


@dataclass(frozen=True, slots=True)
class ReclaimedQueue:
    """一个逻辑队列本轮有界裁剪的实际范围、删除/跳过计数与原因。

    ``queue_id_range`` 是本轮检查过的 ``queue_id`` 闭区间;它描述逻辑行范围,
    不代表 SQLite 物理回收量或磁盘节省。
    """

    queue: str
    deleted: int
    skipped: int
    skip_reasons: tuple[str, ...]
    queue_id_range: tuple[int, int] | None


@dataclass(frozen=True, slots=True)
class ReportedReclamation:
    """已上报队列的逻辑裁剪结果;不声称物理磁盘回收,也不统计未知媒体文件。"""

    rows: tuple[ReclaimedQueue, ...]

    @property
    def deleted(self) -> int:
        return sum(row.deleted for row in self.rows)

    @property
    def skipped(self) -> int:
        return sum(row.skipped for row in self.rows)

    @property
    def skip_reasons(self) -> tuple[str, ...]:
        return tuple(sorted({reason for row in self.rows for reason in row.skip_reasons}))


# 固定 owner 表、实例关联表达式与是否参与已上报队列裁剪;不接收客户端 SQL 标识符。
_REPORTED_TABLES = (
    (
        "report",
        "local_report_queue",
        "coalesce(q.instance_id, (SELECT d.instance_id FROM local_decision d "
        "WHERE d.decision_id=q.decision_id))",
        True,
    ),
    ("observation", "local_observation_queue", "q.instance_id", True),
    ("health", "local_health_queue", None, True),
    ("disposal", "local_disposal", "q.violation_instance_id", False),
)


def _protection_expression(instance_expression: str | None, *, disposal: bool) -> str:
    """返回“仍受未结实例/已锁存违规/未登记证据引用保护”的 SQL 谓词。

    影响估算与回收共用同一含义;``q`` 是 owner 表别名。未知或失配状态按受保护处理。
    """
    if instance_expression is None:
        base = "0"
    else:
        base = f"""(
                NOT EXISTS (SELECT 1 FROM local_sop_instance i
                    WHERE i.station_id=q.station_id AND i.instance_id={instance_expression}
                    AND i.closed_at IS NOT NULL)
                OR EXISTS (SELECT 1 FROM local_violation v
                    WHERE v.station_id=q.station_id AND v.instance_id={instance_expression})
                OR EXISTS (SELECT 1 FROM local_evidence_queue e
                    WHERE e.station_id=q.station_id AND e.instance_id={instance_expression}
                    AND e.registered_at IS NULL)
            )"""
    if disposal:
        return f"(q.violation_ref IS NOT NULL OR {base})"
    return base


def _acknowledged_utc(value: object) -> datetime | None:
    """解析中心确认的 UTC 时刻;缺失或非法返回 None,由调用方决定拒绝还是跳过。"""
    if not isinstance(value, str):
        return None
    try:
        acknowledged = datetime.fromisoformat(value)
    except ValueError:
        return None
    if acknowledged.tzinfo is None:
        return None
    return acknowledged


def _reclaim_eligible_expression(
    *,
    durations: Mapping[str, int],
    host_id: str | None,
    highest: int | None,
    protection: str,
    reference: str,
) -> tuple[str, list[object]]:
    """返回“按当前配置与年龄可删”的 0/1 SQL 谓词与绑定参数。

    WHERE 用它分两路(可删 / 不可删)并在主键顺序上扫描,使最旧的受保护行不占用有界预算,
    也不会为了排序而扫描整段已上报历史。
    """
    if host_id is None or not durations:
        return "0", []
    station_ids = tuple(durations)
    placeholders = ",".join("?" for _ in station_ids)
    case = "CASE q.station_id " + " ".join("WHEN ? THEN ?" for _ in station_ids) + " ELSE 0 END"
    case_arguments: list[object] = []
    for station_id in station_ids:
        case_arguments.extend((station_id, durations[station_id]))
    highest_clause = "" if highest is None else "q.queue_id <> ? AND "
    expression = (
        "q.acknowledged_at IS NOT NULL AND "
        f"COALESCE(q.report_host_id, '') = ? AND {highest_clause}"
        f"q.station_id IN ({placeholders}) AND "
        f"julianday(q.acknowledged_at) <= julianday(?) - ({case} / 86400.0) AND "
        f"NOT {protection}"
    )
    arguments: list[object] = [host_id]
    if highest is not None:
        arguments.append(highest)
    arguments.extend(station_ids)
    arguments.append(reference)
    arguments.extend(case_arguments)
    return expression, arguments


def _reclaim_skip_reason(
    candidate: sqlite3.Row,
    *,
    groups: Mapping[str, ResolvedRetentionPolicy | None],
    host_id: str | None,
    now: datetime,
    highest: int | None,
) -> str | None:
    """返回该候选的保留原因;``None`` 表示按当前配置可删除。"""
    group = groups.get(str(candidate["station_id"]))
    if group is None or host_id is None:
        return "retention_unknown"
    if group.local_reported.mode == "permanent":
        return "retention_permanent"
    if group.local_reported.mode != "duration":
        return "retention_unknown"
    if candidate["report_host_id"] != host_id:
        return "report_source_unknown"
    acknowledged = _acknowledged_utc(candidate["acknowledged_at"])
    rule = group.local_reported
    if acknowledged is None:
        return "acknowledgement_unknown"
    if rule.seconds is None or acknowledged > now - timedelta(seconds=rule.seconds):
        return "not_expired"
    if bool(candidate["retention_protected"]):
        return "retention_protected"
    if highest is not None and int(candidate["queue_id"]) == highest:
        return "identity_high_water"
    return None


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
        for _name, table, instance, _prune in _REPORTED_TABLES:
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
            protection = _protection_expression(instance, disposal=(table == "local_disposal"))
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
                acknowledged = _acknowledged_utc(row["acknowledged_at"])
                if acknowledged is None:
                    raise ReportedImpactUnavailableError("acknowledgement_utc_unknown")
                if is_protected:
                    protected = protected.include(row, acknowledged_at=acknowledged)
                else:
                    exposed = exposed.include(row, acknowledged_at=acknowledged)
        return ReportedImpact(
            host_id=bundle.host_id,
            configuration_revision=bundle.config_revision,
            configuration_sha256=bundle.effective_sha256,
            exposed=exposed,
            protected=protected,
        )
    finally:
        connection.close()


def reclaim_reported_reports(
    connection: sqlite3.Connection, *, now: datetime, limit: int
) -> ReportedReclamation:
    """在调用方持有的写事务内按最后确认配置有界裁剪三类已上报队列。

    只删除 ``sent_at`` 有中心确认时刻(``acknowledged_at``)且年龄超过该工位
    ``local_reported`` 的逻辑行;未知策略/来源/确认时刻、仍受引用保护、以及各表最高
    ``queue_id`` 一律保留并记录原因。每表最多检查 ``limit`` 行,``limit`` 必须为正。
    处置账本、证据队列与媒体、实例/判定/违规身份不在本函数所有权内。
    """
    if limit <= 0:
        raise ValueError("reclaim limit must be positive")
    if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
        raise ValueError("reclaim reference time must be UTC")
    bundle = LocalConfigurationStore(connection).confirmed()
    groups: Mapping[str, ResolvedRetentionPolicy | None] = (
        {}
        if bundle is None
        else {station.station_id: station.retention for station in bundle.stations}
    )
    host_id = None if bundle is None else bundle.host_id
    return ReportedReclamation(
        rows=tuple(
            _reclaim_queue(
                connection,
                name=name,
                table=table,
                instance_expression=instance,
                groups=groups,
                host_id=host_id,
                now=now,
                limit=limit,
            )
            for name, table, instance, prune in _REPORTED_TABLES
            if prune
        )
    )


def _reclaim_queue(
    connection: sqlite3.Connection,
    *,
    name: str,
    table: str,
    instance_expression: str | None,
    groups: Mapping[str, ResolvedRetentionPolicy | None],
    host_id: str | None,
    now: datetime,
    limit: int,
) -> ReclaimedQueue:
    row = connection.execute(f"SELECT max(queue_id) AS highest FROM {table}").fetchone()
    highest = None if row is None or row["highest"] is None else int(row["highest"])
    protection = _protection_expression(instance_expression, disposal=False)
    durations: dict[str, int] = {}
    for station_id, group in groups.items():
        if group is None or group.local_reported.mode != "duration":
            continue
        seconds = group.local_reported.seconds
        if seconds is not None:
            durations[station_id] = seconds
    eligible_sql, eligible_arguments = _reclaim_eligible_expression(
        durations=durations,
        host_id=host_id,
        highest=highest,
        protection=protection,
        reference=now.isoformat(),
    )
    # 先扫描不可删行给出跳过原因(主键倒序, 恒含最高 queue_id), 再扫描可删行; 两者都按主键
    # 受限推进, 不排序全部已上报历史, 受保护的最旧行不占用删除预算。
    skipped_rows = connection.execute(
        f"""
        SELECT q.queue_id        AS queue_id,
               q.station_id      AS station_id,
               q.report_host_id  AS report_host_id,
               q.acknowledged_at AS acknowledged_at,
               {protection} AS retention_protected
          FROM {table} q
         WHERE q.sent_at IS NOT NULL AND COALESCE(({eligible_sql}), 0) = 0
         ORDER BY q.queue_id DESC
         LIMIT ?
        """,
        [*eligible_arguments, limit],
    ).fetchall()
    eligible_rows = connection.execute(
        f"""
        SELECT q.queue_id AS queue_id
          FROM {table} q
         WHERE q.sent_at IS NOT NULL AND COALESCE(({eligible_sql}), 0) = 1
         ORDER BY q.queue_id
         LIMIT ?
        """,
        [*eligible_arguments, limit],
    ).fetchall()
    deleted: list[int] = []
    skip_reasons: Counter[str] = Counter()
    examined: list[int] = []
    for candidate in skipped_rows:
        queue_id = int(candidate["queue_id"])
        examined.append(queue_id)
        reason = _reclaim_skip_reason(
            candidate, groups=groups, host_id=host_id, now=now, highest=highest
        )
        if reason is None:
            deleted.append(queue_id)
        else:
            skip_reasons[reason] += 1
    for candidate in eligible_rows:
        queue_id = int(candidate["queue_id"])
        examined.append(queue_id)
        deleted.append(queue_id)
    if deleted:
        placeholders = ",".join("?" for _ in deleted)
        connection.execute(f"DELETE FROM {table} WHERE queue_id IN ({placeholders})", deleted)
    return ReclaimedQueue(
        queue=name,
        deleted=len(deleted),
        skipped=sum(skip_reasons.values()),
        skip_reasons=tuple(sorted(skip_reasons)),
        queue_id_range=(min(examined), max(examined)) if examined else None,
    )


__all__ = [
    "DEFAULT_RECLAIM_LIMIT",
    "ReclaimedQueue",
    "ReportedAggregate",
    "ReportedImpact",
    "ReportedImpactUnavailableError",
    "ReportedReclamation",
    "read_reported_impact",
    "reclaim_reported_reports",
]
