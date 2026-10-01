"""retention 拥有的保留策略值与纯规则；默认值只在这里定义一次（§5.19）。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class RetentionCategory(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"


class RetentionMode(StrEnum):
    UNSET = "unset"
    DURATION = "duration"
    PERMANENT = "permanent"
    DISCARD = "discard"


@dataclass(frozen=True, slots=True)
class RetentionRule:
    """单条保留规则：有界时长、永久或显式不保留。"""

    mode: RetentionMode
    seconds: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, RetentionMode):
            raise ValueError("retention rule mode is unsupported")
        if self.mode is RetentionMode.DURATION:
            if (
                isinstance(self.seconds, bool)
                or not isinstance(self.seconds, int)
                or self.seconds <= 0
            ):
                raise ValueError("duration retention requires a positive number of seconds")
        elif self.seconds is not None:
            raise ValueError("only duration retention carries seconds")

    def to_wire(self) -> dict[str, object]:
        return {"mode": self.mode.value, "seconds": self.seconds}

    @classmethod
    def from_wire(cls, value: object) -> RetentionRule:
        if not isinstance(value, Mapping) or set(value) != {"mode", "seconds"}:
            raise ValueError("retention rule must carry exactly mode and seconds")
        mode, seconds = value["mode"], value["seconds"]
        if not isinstance(mode, str):
            raise ValueError("retention rule mode must be a string")
        try:
            parsed = RetentionMode(mode)
        except ValueError:
            raise ValueError("retention rule mode is unsupported") from None
        if seconds is not None and (isinstance(seconds, bool) or not isinstance(seconds, int)):
            raise ValueError("retention rule seconds must be an integer or null")
        return cls(parsed, seconds)


POLICY_FIELDS = (
    "evidence_pass",
    "evidence_fail",
    "evidence_indeterminate",
    "record_detail",
    "record_compression_age",
    "record_aggregate_age",
    "disposal",
    "local_reported",
)


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """完整一组保留参数；工位覆盖（#204）将整组替换（§5.19）。

    录像滚动窗口与原始素材窗口不在这里——它们分别归 `device` 与边缘媒体配置。
    """

    evidence_pass: RetentionRule
    evidence_fail: RetentionRule
    evidence_indeterminate: RetentionRule
    record_detail: RetentionRule
    record_compression_age: RetentionRule
    record_aggregate_age: RetentionRule
    disposal: RetentionRule
    local_reported: RetentionRule

    def __post_init__(self) -> None:
        for name in POLICY_FIELDS:
            if getattr(self, name).mode is RetentionMode.UNSET:
                raise ValueError(f"{name} must be set; a complete policy has no unset field")
        for name in ("evidence_fail", "evidence_indeterminate", "disposal", "local_reported"):
            if getattr(self, name).mode is RetentionMode.DISCARD:
                raise ValueError(f"{name} must not be discard; discard is only for pass")
        for name in ("record_detail", "record_compression_age", "record_aggregate_age"):
            if getattr(self, name).mode is not RetentionMode.DURATION:
                raise ValueError(f"{name} must be a bounded duration")
        detail, compression = self.record_detail.seconds, self.record_compression_age.seconds
        if detail is None or compression is None:
            raise ValueError("record retention fields must be bounded durations")
        if detail < compression:
            raise ValueError("record detail retention must not be shorter than compression age")

    def to_wire(self) -> dict[str, object]:
        return {name: getattr(self, name).to_wire() for name in POLICY_FIELDS}

    @classmethod
    def from_wire(cls, value: object) -> RetentionPolicy:
        if not isinstance(value, Mapping) or set(value) != set(POLICY_FIELDS):
            raise ValueError("retention policy must be a complete group")
        return cls(**{name: RetentionRule.from_wire(value[name]) for name in POLICY_FIELDS})


@dataclass(frozen=True, slots=True)
class RetentionPolicyState:
    policy: RetentionPolicy
    revision: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 0
        ):
            raise ValueError("retention policy revision must be a non-negative integer")


def category_for(verdict: str, *, has_latched_violation: bool) -> RetentionCategory:
    """解析实例保留类别；锁存违规恒按不通过类保留。`has_latched_violation` 由调用方传入。"""
    if has_latched_violation:
        return RetentionCategory.FAIL
    try:
        return RetentionCategory(verdict)
    except ValueError:
        raise ValueError(f"unsupported judgment verdict: {verdict}") from None


def check_reslice_window(expected_seconds: int, *, recording_window_seconds: int) -> None:
    """再切片期望不能超过录像滚动窗口；窗口值由拥有者传入（§5.20）。"""
    _positive_int(expected_seconds, "expected reslice window")
    _positive_int(recording_window_seconds, "recording window")
    if expected_seconds > recording_window_seconds:
        raise ValueError("expected reslice window must not exceed the recording window")


def _positive_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


_DAY = 24 * 60 * 60

DEFAULT_RETENTION_POLICY = RetentionPolicy(
    evidence_pass=RetentionRule(RetentionMode.DURATION, 90 * _DAY),
    evidence_fail=RetentionRule(RetentionMode.PERMANENT),
    evidence_indeterminate=RetentionRule(RetentionMode.DURATION, 90 * _DAY),
    record_detail=RetentionRule(RetentionMode.DURATION, 90 * _DAY),
    record_compression_age=RetentionRule(RetentionMode.DURATION, 7 * _DAY),
    record_aggregate_age=RetentionRule(RetentionMode.DURATION, 90 * _DAY),
    disposal=RetentionRule(RetentionMode.PERMANENT),
    local_reported=RetentionRule(RetentionMode.DURATION, 14 * _DAY),
)


__all__ = [
    "DEFAULT_RETENTION_POLICY",
    "RetentionCategory",
    "RetentionMode",
    "RetentionPolicy",
    "RetentionPolicyState",
    "RetentionRule",
    "category_for",
    "check_reslice_window",
]
