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
"""单次裁剪每表最多检查的逻辑行数;运行时按既有上报节奏以更小预算周期调用。"""


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
    """一个逻辑队列本轮有界页的实际范围、删除/跳过计数与原因。

    ``queue_id_range`` 是本轮检查页的 ``queue_id`` 闭区间;它描述逻辑行范围,
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


def _instance_protection_conditions(
    *, station_expression: str, instance_expression: str
) -> tuple[tuple[str, str], ...]:
    """#206 媒体删除与 #208 队列回收共用的实例保护条件; 规则只在这里定义一次。"""
    return (
        (
            "latched_violation",
            "EXISTS (SELECT 1 FROM local_violation v "
            f"WHERE v.station_id={station_expression} AND v.instance_id={instance_expression})",
        ),
        (
            "evidence_pending",
            "EXISTS (SELECT 1 FROM local_evidence_queue e "
            f"WHERE e.station_id={station_expression} AND e.instance_id={instance_expression} "
            "AND e.registered_at IS NULL)",
        ),
    )


def retention_protection_predicate(
    *, instance_expression: str, station_expression: str = "q.station_id"
) -> str:
    """共享保留保护谓词: 未知/未结案实例、已锁存违规、未登记证据引用。

    影响估算、已上报回收与 #206 媒体删除共用本谓词; 未知或失配状态按受保护处理。
    """
    conditions = " OR ".join(
        condition
        for _, condition in _instance_protection_conditions(
            station_expression=station_expression, instance_expression=instance_expression
        )
    )
    return (
        "(NOT EXISTS (SELECT 1 FROM local_sop_instance i "
        f"WHERE i.station_id={station_expression} AND i.instance_id={instance_expression} "
        "AND i.closed_at IS NOT NULL) "
        f"OR {conditions})"
    )


def instance_protection_reason(
    connection: sqlite3.Connection, *, station_id: str, instance_id: int
) -> str | None:
    """按共享规则返回一条实例的保护原因; ``None`` 表示共享规则不保护它。

    这是 #206 媒体删除与 #208 队列回收共用的唯一未结案/锁存违规/未登记证据判定。
    """
    instance = connection.execute(
        "SELECT closed_at FROM local_sop_instance WHERE station_id = ? AND instance_id = ?",
        (station_id, instance_id),
    ).fetchone()
    if instance is None:
        return "instance_unknown"
    if instance[0] is None:
        return "instance_open"
    for reason, condition in _instance_protection_conditions(
        station_expression="?", instance_expression="?"
    ):
        found = connection.execute(
            f"SELECT 1 WHERE {condition}", (station_id, instance_id)
        ).fetchone()
        if found is not None:
            return reason
    return None


def _protection_expression(instance_expression: str | None, *, disposal: bool) -> str:
    """返回“仍受未结实例/已锁存违规/未登记证据引用保护”的 SQL 谓词。

    影响估算与回收共用同一含义;``q`` 是 owner 表别名。未知或失配状态按受保护处理。
    """
    if instance_expression is None:
        base = "0"
    else:
        base = retention_protection_predicate(instance_expression=instance_expression)
    if disposal:
        return f"(q.violation_ref IS NOT NULL OR {base})"
    return base


def _acknowledged_utc(value: object) -> datetime | None:
    """解析中心确认的 UTC 时刻;缺失、无时区或非法返回 None,由调用方决定拒绝还是跳过。

    这是确认时刻的唯一权威:删除资格只按本函数结果判定,SQL 只负责主键页定位。
    """
    if not isinstance(value, str):
        return None
    try:
        acknowledged = datetime.fromisoformat(value)
    except ValueError:
        return None
    if acknowledged.tzinfo is None:
        return None
    return acknowledged


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
    connection: sqlite3.Connection,
    *,
    now: datetime,
    limit: int,
    positions: Mapping[str, int],
) -> tuple[ReportedReclamation, dict[str, int]]:
    """在调用方持有的写事务内按最后确认配置有界裁剪三类已上报队列。

    每表只读取一个 ``queue_id`` 主键 keyset 页(最多 ``limit`` 行, 含待上报行);按 Python
    侧的唯一确认时刻/保护权威逐行判定, 删除过期且非最高 ``queue_id`` 的行。返回下一轮各表
    起始位置: 页未满即本轮已到表尾并回绕到 0。调用方只能在事务成功 ``COMMIT`` 后采用新位置;
    失败时不得采纳, 以便重试仍在同一页。
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
    rows: list[ReclaimedQueue] = []
    next_positions: dict[str, int] = {}
    for name, table, instance, prune in _REPORTED_TABLES:
        if not prune:
            continue
        reclaimed, next_position = _reclaim_queue(
            connection,
            name=name,
            table=table,
            instance_expression=instance,
            groups=groups,
            host_id=host_id,
            now=now,
            limit=limit,
            position=int(positions.get(name, 0)),
        )
        rows.append(reclaimed)
        next_positions[name] = next_position
    return ReportedReclamation(rows=tuple(rows)), next_positions


def _reclaim_skip_reason(
    candidate: sqlite3.Row,
    *,
    group: ResolvedRetentionPolicy | None,
    host_id: str | None,
    now: datetime,
    highest: int | None,
) -> str | None:
    """返回该候选的保留原因;``None`` 表示按当前配置可删除。"""
    if candidate["sent_at"] is None:
        return "pending"
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
    position: int,
) -> tuple[ReclaimedQueue, int]:
    row = connection.execute(f"SELECT max(queue_id) AS highest FROM {table}").fetchone()
    highest = None if row is None or row["highest"] is None else int(row["highest"])
    protection = _protection_expression(instance_expression, disposal=False)
    page = connection.execute(
        f"""
        SELECT q.queue_id        AS queue_id,
               q.station_id      AS station_id,
               q.report_host_id  AS report_host_id,
               q.acknowledged_at AS acknowledged_at,
               q.sent_at         AS sent_at,
               {protection} AS retention_protected
          FROM {table} q
         WHERE q.queue_id > ?
         ORDER BY q.queue_id
         LIMIT ?
        """,
        (position, limit),
    ).fetchall()
    deleted: list[int] = []
    skip_reasons: Counter[str] = Counter()
    for candidate in page:
        reason = _reclaim_skip_reason(
            candidate,
            group=groups.get(str(candidate["station_id"])),
            host_id=host_id,
            now=now,
            highest=highest,
        )
        if reason is None:
            deleted.append(int(candidate["queue_id"]))
        else:
            skip_reasons[reason] += 1
    if deleted:
        placeholders = ",".join("?" for _ in deleted)
        connection.execute(f"DELETE FROM {table} WHERE queue_id IN ({placeholders})", deleted)
    # 页未满说明本轮已到表尾: 下一轮从最小 queue_id 重新开始; 否则从本页末继续。
    next_position = 0 if len(page) < limit else int(page[-1]["queue_id"])
    return (
        ReclaimedQueue(
            queue=name,
            deleted=len(deleted),
            skipped=sum(skip_reasons.values()),
            skip_reasons=tuple(sorted(skip_reasons)),
            queue_id_range=(int(page[0]["queue_id"]), int(page[-1]["queue_id"])) if page else None,
        ),
        next_position,
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
