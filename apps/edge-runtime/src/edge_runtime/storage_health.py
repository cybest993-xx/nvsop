"""按真实写入路径采样磁盘, 以高水位/恢复边界维持失效状态。

这里只探测本模块拥有的临时文件, 不回收录像、证据或任何待上报队列。
"""

from __future__ import annotations

import math
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DiskHealthConfiguration:
    high_watermark_percent: float = 90
    recovery_watermark_percent: float = 81
    check_interval_seconds: float = 5

    def __post_init__(self) -> None:
        high, recovery, interval = (
            self.high_watermark_percent,
            self.recovery_watermark_percent,
            self.check_interval_seconds,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in (high, recovery, interval)
        ):
            raise ValueError("disk health values must be numbers")
        if not all(math.isfinite(value) for value in (high, recovery, interval)):
            raise ValueError("disk health values must be finite")
        if not 0 <= recovery < high < 100:
            raise ValueError("disk recovery must be below high watermark, both within 0..100")
        if interval <= 0:
            raise ValueError("disk check interval must be positive")


def load_disk_health_configuration(value: object) -> DiskHealthConfiguration:
    """旧部署使用默认组; 显式组必须完整且拒绝未知键。"""
    if value is None:
        return DiskHealthConfiguration()
    keys = {"high_watermark_percent", "recovery_watermark_percent", "check_interval_seconds"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("disk_health must contain high/recovery watermark and check interval")
    return DiskHealthConfiguration(
        value["high_watermark_percent"],
        value["recovery_watermark_percent"],
        value["check_interval_seconds"],
    )


@dataclass(frozen=True, slots=True)
class DiskUsage:
    total_bytes: int
    available_bytes: int

    def __post_init__(self) -> None:
        if self.total_bytes <= 0 or not 0 <= self.available_bytes <= self.total_bytes:
            raise ValueError("disk usage is invalid")

    @property
    def used_percent(self) -> float:
        """使用当前进程可用空间, 不把文件系统保留块当作可写余量。"""
        return 100 * (self.total_bytes - self.available_bytes) / self.total_bytes


@dataclass(frozen=True, slots=True)
class StorageHealthEvent:
    role: str
    path: str
    impaired: bool
    used_percent: float | None
    total_bytes: int | None
    available_bytes: int | None
    high_watermark_percent: float
    recovery_watermark_percent: float
    error: str | None


def inspect_disk(path: Path) -> DiskUsage:
    """测量真实目标文件系统, 并用本进程创建/写入/fsync 的文件验证可写。"""
    stat = os.statvfs(path)
    usage = DiskUsage(stat.f_blocks * stat.f_frsize, stat.f_bavail * stat.f_frsize)
    with tempfile.NamedTemporaryFile(prefix=".nvsop-disk-probe-", dir=path) as probe:
        probe.write(b"nvsop disk health\n")
        probe.flush()
        os.fsync(probe.fileno())
    return usage


class DiskHealthWatch:
    """每个目标独立滞回; 只发布初始事实和状态/整百分点变化, 不制造静态积压。"""

    def __init__(
        self,
        *,
        paths: Mapping[str, Path],
        configuration: DiskHealthConfiguration,
        sample: Callable[[Path], DiskUsage] = inspect_disk,
        initially_impaired: bool = False,
    ) -> None:
        self._paths = dict(paths)
        self._configuration = configuration
        self._sample = sample
        self._impaired = dict.fromkeys(paths, initially_impaired)
        self._published: dict[str, tuple[bool, int | None, str | None]] = {}
        self._next_poll = 0.0

    def timeout(self, *, now: float) -> float:
        return max(0, self._next_poll - now)

    def poll(self, *, now: float) -> tuple[StorageHealthEvent, ...]:
        if now < self._next_poll:
            return ()
        self._next_poll = now + self._configuration.check_interval_seconds
        result: list[StorageHealthEvent] = []
        high = self._configuration.high_watermark_percent
        recovery = self._configuration.recovery_watermark_percent
        for role, path in self._paths.items():
            usage = None
            error = None
            try:
                usage = self._sample(path)
            except (OSError, ValueError) as failure:
                error = f"{type(failure).__name__}: {failure}"
            impaired = (
                True
                if usage is None
                else usage.used_percent > recovery
                if self._impaired[role]
                else usage.used_percent >= high
            )
            self._impaired[role] = impaired
            signature = (impaired, None if usage is None else int(usage.used_percent), error)
            if self._published.get(role) == signature:
                continue
            self._published[role] = signature
            result.append(
                StorageHealthEvent(
                    role=role,
                    path=str(path),
                    impaired=impaired,
                    used_percent=None if usage is None else usage.used_percent,
                    total_bytes=None if usage is None else usage.total_bytes,
                    available_bytes=None if usage is None else usage.available_bytes,
                    high_watermark_percent=high,
                    recovery_watermark_percent=recovery,
                    error=error,
                )
            )
        return tuple(result)
