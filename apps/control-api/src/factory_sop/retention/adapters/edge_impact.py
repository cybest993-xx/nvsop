"""中心直读来源机结构化统计;不代理媒体、不缓存、不重试、不接受部分汇总。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from typing import Literal
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.retention.access import (
    ReportedImpactAccessRefusedError,
    issue_reported_impact_context,
)
from factory_sop.retention.api import (
    EdgeImpactGateway,
    EdgeImpactResult,
    EdgeImpactSource,
    EdgeImpactSourcesGateway,
    ImpactAggregate,
    RetentionImpactBreakdown,
    empty_aggregate,
)
from nvsop_contracts.retention_impact import ReportedImpactQuery


class _Aggregate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    objects: int = Field(ge=0)
    known_bytes: int = Field(ge=0)
    earliest: datetime | None
    latest: datetime | None

    @model_validator(mode="after")
    def complete_times(self) -> _Aggregate:
        if self.objects == 0:
            if self.known_bytes != 0 or self.earliest is not None or self.latest is not None:
                raise ValueError("empty aggregate has nonempty facts")
        elif (
            self.earliest is None
            or self.latest is None
            or self.earliest.tzinfo is None
            or self.latest.tzinfo is None
            or self.earliest > self.latest
        ):
            raise ValueError("aggregate timestamps are incomplete")
        return self

    def to_domain(self) -> ImpactAggregate:
        return ImpactAggregate(
            objects=self.objects,
            known_bytes=self.known_bytes,
            unknown_size_objects=0,
            earliest=self.earliest,
            latest=self.latest,
        )


class _Response(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    query: dict[str, object]
    bytes_basis: Literal["serialized_rows"]
    exposed: _Aggregate
    protected: _Aggregate


class HttpEdgeImpactGateway(EdgeImpactGateway):
    """请求生命周期内逐个读取 device 给出的真实来源;一个来源失败即拒绝对应字段。"""

    def __init__(
        self,
        *,
        sources: EdgeImpactSourcesGateway,
        release_connection: Callable[[], None],
        refresh_caller: Callable[[], Caller],
        origins: Mapping[str, str],
        caller: Caller,
        fingerprint: str,
        secret: str,
        ttl_seconds: int | None,
        timeout_seconds: float | None,
    ) -> None:
        self._sources, self._origins = sources, origins
        self._release_connection, self._refresh_caller = release_connection, refresh_caller
        self._caller, self._fingerprint, self._secret = caller, fingerprint, secret
        self._ttl, self._timeout = ttl_seconds, timeout_seconds

    def reported_impact(
        self,
        *,
        station_id: UUID | None,
        excluded_stations: frozenset[UUID],
        baseline_seconds: int | None,
        proposed_seconds: int,
        reference_time: datetime,
    ) -> EdgeImpactResult:
        authorize(self._caller, Permission.RETENTION_POLICY_EDIT)
        sources = self._sources.sources(station_id=station_id)
        if not sources:
            return EdgeImpactResult(None, "edge_sources_unknown", sources)
        if self._ttl is None or self._timeout is None:
            return EdgeImpactResult(None, "edge_estimate_transport_unconfigured", sources)
        for source in sources:
            if str(source.host_id) not in self._origins:
                return EdgeImpactResult(None, "edge_source_unconfigured", sources)
            if (
                source.configuration_revision is None
                or source.configuration_revision < 1
                or source.configuration_sha256 is None
            ):
                return EdgeImpactResult(None, "edge_configuration_unknown", sources)
        # 来源已复制为不可变值；等待远端签名回查时不占用请求连接或会话行锁。
        self._release_connection()
        try:
            result = self._read_sources(
                sources=sources,
                station_id=station_id,
                excluded_stations=excluded_stations,
                baseline_seconds=baseline_seconds,
                proposed_seconds=proposed_seconds,
                reference_time=reference_time,
            )
        finally:
            # rollback 也撤销了最初的会话续期；成功/不完整响应都须重新验证并续期。
            self._caller = self._refresh_caller()
            authorize(self._caller, Permission.RETENTION_POLICY_EDIT)
        if self._sources.sources(station_id=station_id) != sources:
            return EdgeImpactResult(None, "edge_sources_changed", sources)
        return result

    def _read_sources(
        self,
        *,
        sources: tuple[EdgeImpactSource, ...],
        station_id: UUID | None,
        excluded_stations: frozenset[UUID],
        baseline_seconds: int | None,
        proposed_seconds: int,
        reference_time: datetime,
    ) -> EdgeImpactResult:
        exposed = empty_aggregate(measurable_bytes=True)
        protected = empty_aggregate(measurable_bytes=True)
        for source in sources:
            assert source.configuration_revision is not None
            assert source.configuration_sha256 is not None
            query = ReportedImpactQuery(
                host_id=str(source.host_id),
                configuration_revision=source.configuration_revision,
                configuration_sha256=source.configuration_sha256,
                station_id=None if station_id is None else str(station_id),
                excluded_stations=tuple(sorted(str(value) for value in excluded_stations)),
                baseline_seconds=baseline_seconds,
                proposed_seconds=proposed_seconds,
                reference_time=reference_time,
            )
            try:
                response = self._read(origin=self._origins[str(source.host_id)], query=query)
            except ReportedImpactAccessRefusedError as error:
                return EdgeImpactResult(None, error.code, sources)
            exposed = exposed.plus(response.exposed.to_domain())
            protected = protected.plus(response.protected.to_domain())
        return EdgeImpactResult(RetentionImpactBreakdown(exposed, protected), None, sources)

    def _read(self, *, origin: str, query: ReportedImpactQuery) -> _Response:
        assert self._ttl is not None
        assert self._timeout is not None
        context = issue_reported_impact_context(
            self._caller,
            query=query,
            fingerprint=self._fingerprint,
            secret=self._secret,
            now=datetime.now(UTC).timestamp(),
            ttl_seconds=self._ttl,
        )
        parsed = urlsplit(origin)
        connection_type = HTTPSConnection if parsed.scheme == "https" else HTTPConnection
        assert parsed.hostname is not None
        connection = connection_type(parsed.hostname, parsed.port, timeout=self._timeout)
        try:
            connection.request(
                "GET", "/retention/reported-impact?" + urlencode({"context": context})
            )
            incoming = connection.getresponse()
            payload = incoming.read()
            if incoming.status != 200:
                raise ReportedImpactAccessRefusedError(409, "edge_estimate_refused")
        except (OSError, HTTPException) as error:
            raise ReportedImpactAccessRefusedError(503, "edge_unreachable") from error
        finally:
            connection.close()
        try:
            response = _Response.model_validate_json(payload)
            if ReportedImpactQuery.from_wire(response.query) != query:
                raise ValueError("source query mismatch")
            start = (
                None
                if query.baseline_seconds is None
                else (query.reference_time - timedelta(seconds=query.baseline_seconds))
            )
            end = query.reference_time - timedelta(seconds=query.proposed_seconds)
            for group in (response.exposed, response.protected):
                if group.earliest is not None and (
                    (start is not None and group.earliest <= start)
                    or group.latest is None
                    or group.latest > end
                ):
                    raise ValueError("source aggregate outside query window")
        except ValueError as error:
            raise ReportedImpactAccessRefusedError(503, "edge_estimate_invalid") from error
        return response
