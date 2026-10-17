"""#207 回收执行纯规则：截止解析、周期身份与跳过合并的形状约束。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from factory_sop.retention.api import (
    DEFAULT_RETENTION_POLICY,
    DisposalRetirement,
    RetentionMode,
    RetentionRule,
    retention_cutoffs,
    rule_cutoff,
    sweep_cycle_id,
)
from factory_sop.retention.model import RetentionPolicyMode
from factory_sop.retention.sweep import RetentionSkip, merge_skips
from factory_sop.retention.usecases import StationRetentionRecord

_NOW = datetime(2026, 10, 14, tzinfo=UTC)
_DAY = 24 * 60 * 60


def _station(id_text: str) -> object:
    from uuid import UUID

    return UUID(f"00000000-0000-4000-8000-{id_text.zfill(12)}")


def _custom_record(station_id: object, policy: object) -> StationRetentionRecord:
    return StationRetentionRecord(
        station_id=station_id,  # type: ignore[arg-type]
        mode=RetentionPolicyMode.CUSTOM,
        overrides=policy,  # type: ignore[arg-type]
        revision=2,
    )


def test_rule_cutoff_maps_duration_permanent_and_discard() -> None:
    assert rule_cutoff(RetentionRule(RetentionMode.DURATION, _DAY), now=_NOW) == _NOW - timedelta(
        seconds=_DAY
    )
    assert rule_cutoff(RetentionRule(RetentionMode.PERMANENT), now=_NOW) is None
    assert rule_cutoff(RetentionRule(RetentionMode.DISCARD), now=_NOW) == _NOW


def test_retention_cutoffs_respects_custom_station_groups() -> None:
    custom = _station("000000000001")
    custom_policy = replace(
        DEFAULT_RETENTION_POLICY,
        record_detail=RetentionRule(RetentionMode.DURATION, _DAY),
        record_compression_age=RetentionRule(RetentionMode.DURATION, _DAY),
    )
    cutoffs = retention_cutoffs(
        default=DEFAULT_RETENTION_POLICY,
        overrides=(_custom_record(custom, custom_policy),),
        now=_NOW,
        field="record_detail",
    )
    # 第一条是无工位身份的全局默认；自定义工位用自己的生效值。
    assert cutoffs.cutoffs[0].station_id is None
    assert cutoffs.cutoffs[0].moment == _NOW - timedelta(seconds=90 * _DAY)
    assert cutoffs.for_station(str(custom)) == _NOW - timedelta(seconds=_DAY)
    other = _station("000000000002")
    assert cutoffs.for_station(str(other)) == _NOW - timedelta(seconds=90 * _DAY)
    # 处置字段默认永久：不产生任何截止。
    disposal = retention_cutoffs(
        default=DEFAULT_RETENTION_POLICY,
        overrides=(_custom_record(custom, custom_policy),),
        now=_NOW,
        field="disposal",
    )
    assert all(value.moment is None for value in disposal.cutoffs)


def test_sweep_cycle_identity_is_stable_within_one_interval() -> None:
    first = sweep_cycle_id(_NOW, interval_seconds=600)
    again = sweep_cycle_id(_NOW + timedelta(seconds=1), interval_seconds=600)
    next_cycle = sweep_cycle_id(_NOW + timedelta(seconds=601), interval_seconds=600)
    assert first == again
    assert first != next_cycle


def test_merge_skips_combines_counts_and_sorts_by_reason() -> None:
    merged = merge_skips(
        (RetentionSkip("b", 1),),
        DisposalRetirement(0, 0, None, None, (RetentionSkip("a", 2),)).skips,
    )
    assert [(value.reason, value.count) for value in merged] == [("a", 2), ("b", 1)]
    assert merge_skips() == ()
