"""中心到来源机的只读已上报记录估算请求;不授权删除或传输媒体。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class ReportedImpactQuery:
    """绑定来源、已签发配置、策略基准/目标、工位范围和参考时刻。"""

    host_id: str
    configuration_revision: int
    configuration_sha256: str
    station_id: str | None
    excluded_stations: tuple[str, ...]
    baseline_seconds: int | None
    proposed_seconds: int
    reference_time: datetime

    def to_wire(self) -> dict[str, object]:
        return {
            "host_id": self.host_id,
            "configuration_revision": self.configuration_revision,
            "configuration_sha256": self.configuration_sha256,
            "station_id": self.station_id,
            "excluded_stations": list(self.excluded_stations),
            "baseline_seconds": self.baseline_seconds,
            "proposed_seconds": self.proposed_seconds,
            "reference_time": self.reference_time.isoformat(),
        }

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> ReportedImpactQuery:
        if set(value) != {
            "host_id",
            "configuration_revision",
            "configuration_sha256",
            "station_id",
            "excluded_stations",
            "baseline_seconds",
            "proposed_seconds",
            "reference_time",
        }:
            raise ValueError("invalid reported impact query fields")
        host = value["host_id"]
        revision = value["configuration_revision"]
        digest = value["configuration_sha256"]
        station = value["station_id"]
        excluded = value["excluded_stations"]
        baseline = value["baseline_seconds"]
        proposed = value["proposed_seconds"]
        reference = value["reference_time"]
        if not isinstance(host, str) or not host:
            raise ValueError("invalid reported impact host")
        if type(revision) is not int or revision < 1:
            raise ValueError("invalid reported impact revision")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("invalid reported impact configuration digest")
        if station is not None and (not isinstance(station, str) or not station):
            raise ValueError("invalid reported impact station")
        if not isinstance(excluded, list) or any(
            not isinstance(item, str) or not item for item in excluded
        ):
            raise ValueError("invalid reported impact exclusions")
        # 不可信请求在此校验,不补全或推测工位范围。
        exclusions = tuple(str(item) for item in excluded)
        if len(set(exclusions)) != len(exclusions) or station in exclusions:
            raise ValueError("invalid reported impact scope")
        if baseline is not None and (type(baseline) is not int or baseline < 0):
            raise ValueError("invalid reported impact baseline")
        if (
            type(proposed) is not int
            or proposed < 0
            or (baseline is not None and proposed >= baseline)
        ):
            raise ValueError("reported impact query must shorten the baseline")
        if not isinstance(reference, str):
            raise ValueError("invalid reported impact reference time")
        at = datetime.fromisoformat(reference)
        if at.tzinfo is None:
            raise ValueError("reported impact reference time must have a UTC offset")
        return cls(
            host_id=host,
            configuration_revision=revision,
            configuration_sha256=digest,
            station_id=station,
            excluded_stations=exclusions,
            baseline_seconds=baseline,
            proposed_seconds=proposed,
            reference_time=at.astimezone(UTC),
        )
