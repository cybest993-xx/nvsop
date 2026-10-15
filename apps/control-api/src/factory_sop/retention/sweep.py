"""回收执行的纯规则、范围解析与结果账本形状（#207 / §5.19）。

retention 只拥有策略/资格与 sweep 编排：本模块把生效保留组解析成各数据面的绝对截止时刻，
定义周期幂等身份与可持久化的执行结果，不触碰任何表，也不删除任何对象。数据面的实际删除由
各 owner 经自己的公开接缝执行（monitor 记录与处置归 `monitor`；Edge 媒体复用 #206 的本机
删除执行器，中心绝不删媒体字节，ADR-0012）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from factory_sop.retention.model import RetentionMode, RetentionPolicy, RetentionRule

if TYPE_CHECKING:
    from factory_sop.retention.usecases import StationRetentionRecord

# 一次回收任务每张候选表读取与删除的有界行数；批次大小是编排配置，不是保留时长。
DEFAULT_SWEEP_BATCH_LIMIT = 256
_SWEEP_CYCLE_NAMESPACE = UUID("8f0b1c2d-0000-4000-8000-00c0de207000")


class RetentionSweepStatus(StrEnum):
    """一次回收任务的账本状态；``running`` 崩溃残留可被安全重跑覆盖。"""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RetentionSkip:
    """一类跳过原因及其计数；原因码是稳定的机器字符串。"""

    reason: str
    count: int

    def to_wire(self) -> dict[str, object]:
        return {"reason": self.reason, "count": self.count}


@dataclass(frozen=True, slots=True)
class Cutoff:
    """一个范围（``station_id=None`` 表示整组跟随全局默认的全部工位）的绝对截止时刻。

    ``moment`` 为 ``None`` 表示该范围在生效策略下永不暴露（永久），不产生任何删除候选。
    """

    station_id: str | None
    moment: datetime | None


@dataclass(frozen=True, slots=True)
class RetentionCutoffs:
    """一个保留字段在全局与各自定义工位上的绝对截止时刻。"""

    cutoffs: tuple[Cutoff, ...]

    def for_station(self, station_id: str) -> datetime | None:
        """该工位的截止时刻；没有自定义记录即跟随全局默认。"""
        for value in self.cutoffs:
            if value.station_id == station_id:
                return value.moment
        return self.cutoffs[0].moment if self.cutoffs else None


def rule_cutoff(rule: RetentionRule, *, now: datetime) -> datetime | None:
    """把一条保留规则解析成绝对截止时刻；永久（或未设置）返回 ``None``。"""
    if rule.mode is RetentionMode.DURATION:
        assert rule.seconds is not None
        return now - timedelta(seconds=rule.seconds)
    if rule.mode is RetentionMode.DISCARD:
        return now
    return None


def retention_cutoffs(
    *,
    default: RetentionPolicy,
    overrides: Iterable[StationRetentionRecord],
    now: datetime,
    field: str,
) -> RetentionCutoffs:
    """按当前生效策略解析各范围的截止时刻；更新策略与执行删除是分开的动作。

    全局默认作用于整组跟随全局的工位；带自定义整组覆盖的工位用自己的生效值。永久不产生截止。
    """
    global_cutoff = rule_cutoff(_field_rule(default, field), now=now)
    entries = [Cutoff(None, global_cutoff)]
    for record in overrides:
        effective = default if record.overrides is None else record.overrides
        entries.append(
            Cutoff(str(record.station_id), rule_cutoff(_field_rule(effective, field), now=now))
        )
    return RetentionCutoffs(cutoffs=tuple(entries))


def _field_rule(policy: RetentionPolicy, field: str) -> RetentionRule:
    value: RetentionRule = getattr(policy, field)
    return value


def sweep_cycle_id(now: datetime, *, interval_seconds: int) -> UUID:
    """按调度周期取整的确定性周期身份：并发 worker 对同一周期只会登记一个任务。"""
    cycle = int(now.timestamp()) // interval_seconds
    return uuid5(_SWEEP_CYCLE_NAMESPACE, f"retention-sweep/{cycle}")


@dataclass(frozen=True, slots=True)
class RecordRetirement:
    """monitor 明细记录的回收结果：删除计数、聚合归档实例数与保护跳过。"""

    deleted: int
    archived_buckets: int
    earliest: datetime | None
    latest: datetime | None
    skips: tuple[RetentionSkip, ...] = ()

    def to_wire(self) -> dict[str, object]:
        return {
            "deleted": self.deleted,
            "archived_buckets": self.archived_buckets,
            "earliest": None if self.earliest is None else self.earliest.isoformat(),
            "latest": None if self.latest is None else self.latest.isoformat(),
            "skips": [value.to_wire() for value in self.skips],
        }


@dataclass(frozen=True, slots=True)
class DisposalRetirement:
    """处置与违规归档的回收结果；默认永久策略下恒为零。"""

    deleted_disposals: int
    deleted_violations: int
    earliest: datetime | None
    latest: datetime | None
    skips: tuple[RetentionSkip, ...] = ()

    def to_wire(self) -> dict[str, object]:
        return {
            "deleted_disposals": self.deleted_disposals,
            "deleted_violations": self.deleted_violations,
            "earliest": None if self.earliest is None else self.earliest.isoformat(),
            "latest": None if self.latest is None else self.latest.isoformat(),
            "skips": [value.to_wire() for value in self.skips],
        }


@dataclass(frozen=True, slots=True)
class RetentionSweepScope:
    """一次回收任务的执行范围：各字段截止、批次上限与依据的策略版本。"""

    record_cutoffs: RetentionCutoffs
    disposal_cutoffs: RetentionCutoffs
    batch_limit: int
    default_revision: int
    station_revisions: tuple[tuple[str, int], ...]

    def to_wire(self) -> dict[str, object]:
        return {
            "record_cutoffs": [
                {
                    "station_id": value.station_id,
                    "moment": None if value.moment is None else value.moment.isoformat(),
                }
                for value in self.record_cutoffs.cutoffs
            ],
            "disposal_cutoffs": [
                {
                    "station_id": value.station_id,
                    "moment": None if value.moment is None else value.moment.isoformat(),
                }
                for value in self.disposal_cutoffs.cutoffs
            ],
            "batch_limit": self.batch_limit,
            "default_revision": self.default_revision,
            "station_revisions": [
                {"station_id": station, "revision": revision}
                for station, revision in self.station_revisions
            ],
        }


@dataclass(frozen=True, slots=True)
class RetentionSweepResult:
    """一次回收任务的结果账本内容；部分失败只记失败段，绝不冒充全量成功。"""

    scope: RetentionSweepScope
    records: RecordRetirement | None = None
    disposals: DisposalRetirement | None = None
    failure_code: str | None = None

    @property
    def completed(self) -> bool:
        """两个数据面都执行完毕（无论删了多少）才算本轮完成。"""
        return self.records is not None and self.disposals is not None and self.failure_code is None

    def to_wire(self) -> dict[str, object]:
        return {
            "scope": self.scope.to_wire(),
            "records": None if self.records is None else self.records.to_wire(),
            "disposals": None if self.disposals is None else self.disposals.to_wire(),
            "failure_code": self.failure_code,
            "completed": self.completed,
        }


def merge_skips(*groups: Iterable[RetentionSkip]) -> tuple[RetentionSkip, ...]:
    """把多段跳过按原因合并计数，输出按原因码排序的稳定序列。"""
    counts: dict[str, int] = {}
    for group in groups:
        for value in group:
            counts[value.reason] = counts.get(value.reason, 0) + value.count
    return tuple(RetentionSkip(reason=reason, count=counts[reason]) for reason in sorted(counts))


def _window(
    moments: Iterable[datetime | None],
) -> tuple[datetime | None, datetime | None]:
    present = [value for value in moments if value is not None]
    if not present:
        return None, None
    return min(present), max(present)


def window_of(
    *groups: Iterable[datetime | None],
) -> tuple[datetime | None, datetime | None]:
    """合并多段的最早/最晚时刻；全部为空时返回 ``(None, None)``。"""
    merged: list[datetime | None] = []
    for group in groups:
        merged.extend(group)
    return _window(merged)


def cutoff_windows(
    cutoffs: RetentionCutoffs,
) -> tuple[datetime | None, datetime | None]:
    """全部生效截止的最早/最晚时刻，作为账本里的范围描述。"""
    return _window(value.moment for value in cutoffs.cutoffs if value.moment is not None)


def assert_cutoffs_cover(cutoffs: RetentionCutoffs, stations: Mapping[str, object]) -> None:
    """调试断言：每个自定义工位都必须有显式截止条目，不能静默落入全局默认。"""
    known = {value.station_id for value in cutoffs.cutoffs}
    missing = sorted(set(stations) - known)
    if missing:
        raise ValueError(f"retention cutoffs missing custom stations: {missing}")


__all__ = [
    "DEFAULT_SWEEP_BATCH_LIMIT",
    "Cutoff",
    "DisposalRetirement",
    "RecordRetirement",
    "RetentionCutoffs",
    "RetentionSkip",
    "RetentionSweepResult",
    "RetentionSweepScope",
    "RetentionSweepStatus",
    "assert_cutoffs_cover",
    "cutoff_windows",
    "merge_skips",
    "retention_cutoffs",
    "rule_cutoff",
    "sweep_cycle_id",
    "window_of",
]
