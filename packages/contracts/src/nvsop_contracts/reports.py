"""判定与健康上报的至少一次契约。

这些对象是推理机观测。中心可以为操作员镜像它们，但不会重新判定，也不会成为 edge 判定路径的依赖。
原因码保持字符串，以便新 edge 向旧中心报告未知代码。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import cast

REPORT_CONTRACT_VERSION = 1
DECISION_REPORT_CONTRACT_VERSION = 2
SOP_INSTANCE_REPORT_CONTRACT_VERSION = 1
HEALTH_REPORT_CONTRACT_VERSION = 2
OBSERVATION_REPORT_CONTRACT_VERSION = 1
DISPOSAL_REPORT_CONTRACT_VERSION = 1
EXECUTION_AUTHORITY_REPORT_CONTRACT_VERSION = 1
REPORT_CAPABILITIES_HEADER = "X-NVSOP-Report-Capabilities"
SOP_INSTANCE_REPORT_CAPABILITY = "sop-instance-report-v1"
HEALTH_REPORT_CAPABILITY = "stream-health-report-v2"

OBSERVATION_SOURCE_ACTION = "action"
"""观测来自动作识别（VLM 输出的动作编号）。"""

OBSERVATION_SOURCE_EXTERNAL_SIGNAL = "external_signal"
"""观测来自连接器输入点位的外部信号。"""

OBSERVATION_SOURCES = frozenset({OBSERVATION_SOURCE_ACTION, OBSERVATION_SOURCE_EXTERNAL_SIGNAL})


@dataclass(frozen=True, slots=True)
class ReportEvidence:
    """随判定携带的可选本地证据区间。"""

    anchor: float | None
    start: float | None
    end: float | None

    def __post_init__(self) -> None:
        values = (self.anchor, self.start, self.end)
        for label, value in zip(("anchor", "start", "end"), values, strict=True):
            if value is not None:
                _finite_number(value, f"report evidence {label}")
        if (self.anchor is None) != (self.start is None) or (self.anchor is None) != (
            self.end is None
        ):
            raise ValueError("report evidence must be complete or absent")
        if self.start is not None and self.end is not None and self.end < self.start:
            raise ValueError("report evidence end must not precede its start")

    def to_wire(self) -> dict[str, object]:
        return {"anchor": self.anchor, "start": self.start, "end": self.end}

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> ReportEvidence:
        _require_keys(value, {"anchor", "start", "end"}, "report evidence")
        return cls(
            anchor=_optional_number(value["anchor"], "evidence anchor"),
            start=_optional_number(value["start"], "evidence start"),
            end=_optional_number(value["end"], "evidence end"),
        )


@dataclass(frozen=True, slots=True)
class ReportViolation:
    """edge 观测到的一项违规；原因码在此边界不设封闭枚举。"""

    reason_code: str
    detail: str | None
    step_ids: tuple[str, ...]
    evidence: ReportEvidence

    def __post_init__(self) -> None:
        if not self.reason_code:
            raise ValueError("violation reason_code must not be empty")
        if self.detail is not None and not isinstance(self.detail, str):
            raise ValueError("violation detail must be a string or null")
        if any(not step_id for step_id in self.step_ids):
            raise ValueError("violation step ids must not be empty")

    def to_wire(self) -> dict[str, object]:
        return {
            "reason_code": self.reason_code,
            "detail": self.detail,
            "step_ids": list(self.step_ids),
            "evidence": self.evidence.to_wire(),
        }

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> ReportViolation:
        _require_keys(value, {"reason_code", "detail", "step_ids", "evidence"}, "report violation")
        raw_steps = _array(value["step_ids"], "violation step_ids")
        if any(not isinstance(step_id, str) or not step_id for step_id in raw_steps):
            raise ValueError("violation step_ids are invalid")
        detail = value["detail"]
        if detail is not None and not isinstance(detail, str):
            raise ValueError("violation detail is invalid")
        return cls(
            reason_code=_string(value["reason_code"], "violation reason_code"),
            detail=detail,
            step_ids=tuple(cast(str, step_id) for step_id in raw_steps),
            evidence=ReportEvidence.from_wire(_object(value["evidence"], "violation evidence")),
        )


@dataclass(frozen=True, slots=True)
class ReportBackendProvenance:
    """实际参与该 SOP 实例的一个推理后端及其事件时模型集合。"""

    backend_id: str
    model_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.backend_id:
            raise ValueError("backend provenance id must not be empty")
        if any(not model_id for model_id in self.model_ids):
            raise ValueError("backend provenance model ids must not be empty")

    def to_wire(self) -> dict[str, object]:
        return {"backend_id": self.backend_id, "model_ids": list(self.model_ids)}

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> ReportBackendProvenance:
        _require_keys(value, {"backend_id", "model_ids"}, "backend provenance")
        return cls(
            backend_id=_string(value["backend_id"], "backend provenance id"),
            model_ids=_strings(value["model_ids"], "backend provenance model_ids"),
        )


@dataclass(frozen=True, slots=True)
class ReportedDecision:
    """一项不可变的 edge 判定事件；v1/v2 wire shape 都严格且互不混用。"""

    event_id: str
    trace_id: str
    host_id: str
    station_id: str
    backend_id: str | None
    instance_id: int
    verdict: str
    reason_codes: tuple[str, ...]
    violations: tuple[ReportViolation, ...]
    lifecycle: str
    evidence: ReportEvidence
    template_version_id: str | None
    template_sha256: str | None
    model_ids: tuple[str, ...]
    reported_at: str
    backend_provenance: tuple[ReportBackendProvenance, ...] = ()
    configuration_revision: int | None = None
    configuration_sha256: str | None = None
    contract_version: int = REPORT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name, value in (
            ("event_id", self.event_id),
            ("trace_id", self.trace_id),
            ("host_id", self.host_id),
            ("station_id", self.station_id),
            ("lifecycle", self.lifecycle),
            ("reported_at", self.reported_at),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must not be empty")
        if self.contract_version not in {REPORT_CONTRACT_VERSION, DECISION_REPORT_CONTRACT_VERSION}:
            raise ValueError("reported decision contract version is unsupported")
        if self.instance_id < 0:
            raise ValueError("reported decision instance_id must not be negative")
        if self.verdict not in {"pass", "fail", "indeterminate"}:
            raise ValueError("reported decision verdict is unsupported")
        if any(not reason for reason in self.reason_codes):
            raise ValueError("reported decision reason codes must not be empty")
        if self.verdict != "pass" and not self.reason_codes:
            raise ValueError("a non-passing decision must preserve a reason code")
        if any(not model_id for model_id in self.model_ids):
            raise ValueError("model ids must not be empty")
        if (self.template_version_id is None) != (self.template_sha256 is None):
            raise ValueError("template version and digest must be supplied together")
        if self.template_sha256 is not None and not _is_sha256(self.template_sha256):
            raise ValueError("template_sha256 is invalid")
        if (self.configuration_revision is None) != (self.configuration_sha256 is None):
            raise ValueError("configuration revision and digest must be supplied together")
        if self.configuration_revision is not None and self.configuration_revision < 1:
            raise ValueError("configuration_revision must be positive")
        if self.configuration_sha256 is not None and not _is_sha256(self.configuration_sha256):
            raise ValueError("configuration_sha256 is invalid")
        if self.contract_version == REPORT_CONTRACT_VERSION:
            if not isinstance(self.backend_id, str) or not self.backend_id:
                raise ValueError("v1 reported decision requires backend_id")
            if self.backend_provenance:
                raise ValueError("v1 reported decision cannot carry backend provenance")
            if self.configuration_revision is not None:
                raise ValueError("v1 reported decision cannot carry configuration proof")
            return
        if self.backend_id is not None or self.model_ids:
            raise ValueError(
                "v2 reported decision uses backend_provenance instead of backend_id/model_ids"
            )
        if self.configuration_revision is None:
            raise ValueError("v2 reported decision requires configuration proof")
        backend_ids = tuple(item.backend_id for item in self.backend_provenance)
        if len(set(backend_ids)) != len(backend_ids):
            raise ValueError("v2 reported decision backend provenance must be unique")
        if backend_ids != tuple(sorted(backend_ids)):
            raise ValueError("v2 reported decision backend provenance must be sorted")

    def to_wire(self) -> dict[str, object]:
        result: dict[str, object] = {
            "contract_version": self.contract_version,
            "event_id": self.event_id,
            "trace_id": self.trace_id,
            "host_id": self.host_id,
            "station_id": self.station_id,
            "instance_id": self.instance_id,
            "verdict": self.verdict,
            "reason_codes": list(self.reason_codes),
            "violations": [violation.to_wire() for violation in self.violations],
            "lifecycle": self.lifecycle,
            "evidence": self.evidence.to_wire(),
            "template_version_id": self.template_version_id,
            "template_sha256": self.template_sha256,
            "reported_at": self.reported_at,
        }
        if self.contract_version == REPORT_CONTRACT_VERSION:
            result["backend_id"] = self.backend_id
            result["model_ids"] = list(self.model_ids)
            return result
        result["backend_provenance"] = [item.to_wire() for item in self.backend_provenance]
        result["configuration_revision"] = self.configuration_revision
        result["configuration_sha256"] = self.configuration_sha256
        return result

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> ReportedDecision:
        contract_version = _positive_int(value.get("contract_version"), "contract_version")
        common = {
            "contract_version",
            "event_id",
            "trace_id",
            "host_id",
            "station_id",
            "instance_id",
            "verdict",
            "reason_codes",
            "violations",
            "lifecycle",
            "evidence",
            "template_version_id",
            "template_sha256",
            "reported_at",
        }
        if contract_version == REPORT_CONTRACT_VERSION:
            required = common | {"backend_id", "model_ids"}
        elif contract_version == DECISION_REPORT_CONTRACT_VERSION:
            required = common | {
                "backend_provenance",
                "configuration_revision",
                "configuration_sha256",
            }
        else:
            raise ValueError("reported decision contract version is unsupported")
        _require_keys(value, required, "reported decision")
        reasons = _strings(value["reason_codes"], "reason_codes", require_nonempty=False)
        raw_violations = _array(value["violations"], "violations")
        template_id = value["template_version_id"]
        template_sha = value["template_sha256"]
        if template_id is not None and not isinstance(template_id, str):
            raise ValueError("template_version_id is invalid")
        if template_sha is not None and not isinstance(template_sha, str):
            raise ValueError("template_sha256 is invalid")
        backend_id: str | None = None
        model_ids: tuple[str, ...] = ()
        backend_provenance: tuple[ReportBackendProvenance, ...] = ()
        configuration_revision: int | None = None
        configuration_sha256: str | None = None
        if contract_version == REPORT_CONTRACT_VERSION:
            backend_id = _string(value["backend_id"], "backend_id")
            model_ids = _strings(value["model_ids"], "model_ids")
        else:
            backend_provenance = tuple(
                ReportBackendProvenance.from_wire(_object(item, "backend provenance"))
                for item in _array(value["backend_provenance"], "backend_provenance")
            )
            configuration_revision = _positive_int(
                value["configuration_revision"], "configuration_revision"
            )
            configuration_sha256 = _string(value["configuration_sha256"], "configuration_sha256")
        return cls(
            event_id=_string(value["event_id"], "event_id"),
            trace_id=_string(value["trace_id"], "trace_id"),
            host_id=_string(value["host_id"], "host_id"),
            station_id=_string(value["station_id"], "station_id"),
            backend_id=backend_id,
            instance_id=_nonnegative_int(value["instance_id"], "instance_id"),
            verdict=_string(value["verdict"], "verdict"),
            reason_codes=reasons,
            violations=tuple(
                ReportViolation.from_wire(_object(item, "report violation"))
                for item in raw_violations
            ),
            lifecycle=_string(value["lifecycle"], "lifecycle"),
            evidence=ReportEvidence.from_wire(_object(value["evidence"], "report evidence")),
            template_version_id=template_id,
            template_sha256=template_sha,
            model_ids=model_ids,
            reported_at=_string(value["reported_at"], "reported_at"),
            backend_provenance=backend_provenance,
            configuration_revision=configuration_revision,
            configuration_sha256=configuration_sha256,
            contract_version=contract_version,
        )


@dataclass(frozen=True, slots=True)
class ReportedDisposal:
    """edge 本地处置账本的一条不可变结果镜像；中心不得据此再次执行。"""

    event_id: str
    host_id: str
    station_id: str
    instance_id: int
    idempotency_key: str
    violation_ref: str
    action_kind: str
    actor: str
    source: str
    result_kind: str
    result_detail: str | None
    result_at: float
    attempts: int
    reported_at: str
    contract_version: int = DISPOSAL_REPORT_CONTRACT_VERSION

    def to_wire(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "event_id": self.event_id,
            "host_id": self.host_id,
            "station_id": self.station_id,
            "instance_id": self.instance_id,
            "idempotency_key": self.idempotency_key,
            "violation_ref": self.violation_ref,
            "action_kind": self.action_kind,
            "actor": self.actor,
            "source": self.source,
            "result_kind": self.result_kind,
            "result_detail": self.result_detail,
            "result_at": self.result_at,
            "attempts": self.attempts,
            "reported_at": self.reported_at,
        }

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> ReportedDisposal:
        _require_keys(
            value,
            {
                "contract_version",
                "event_id",
                "host_id",
                "station_id",
                "instance_id",
                "idempotency_key",
                "violation_ref",
                "action_kind",
                "actor",
                "source",
                "result_kind",
                "result_detail",
                "result_at",
                "attempts",
                "reported_at",
            },
            "reported disposal",
        )
        contract_version = _positive_int(value["contract_version"], "contract_version")
        if contract_version != DISPOSAL_REPORT_CONTRACT_VERSION:
            raise ValueError("reported disposal contract version is unsupported")
        return cls(
            event_id=_string(value["event_id"], "event_id"),
            host_id=_string(value["host_id"], "host_id"),
            station_id=_string(value["station_id"], "station_id"),
            instance_id=_nonnegative_int(value["instance_id"], "instance_id"),
            idempotency_key=_string(value["idempotency_key"], "idempotency_key"),
            violation_ref=_string(value["violation_ref"], "violation_ref"),
            action_kind=_string(value["action_kind"], "action_kind"),
            actor=_string(value["actor"], "actor"),
            source=_string(value["source"], "source"),
            result_kind=_string(value["result_kind"], "result_kind"),
            result_detail=(
                None
                if value["result_detail"] is None
                else _string(value["result_detail"], "result_detail")
            ),
            result_at=_finite_number(value["result_at"], "result_at"),
            attempts=_positive_int(value["attempts"], "attempts"),
            reported_at=_string(value["reported_at"], "reported_at"),
            contract_version=contract_version,
        )


@dataclass(frozen=True, slots=True)
class ReportedExecutionAuthority:
    """edge 本机写入门禁的当前状态快照；中心只镜像，不据此授权。"""

    host_id: str
    station_id: str
    authority_state: str
    write_state: str
    reason_code: str | None
    detail: str | None
    grant_id: str | None
    holder_host_id: str | None
    lease_expires_at: str | None
    renewed_at: str | None
    reported_at: str
    contract_version: int = EXECUTION_AUTHORITY_REPORT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name, value in (
            ("host_id", self.host_id),
            ("station_id", self.station_id),
            ("authority_state", self.authority_state),
            ("write_state", self.write_state),
            ("reported_at", self.reported_at),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must not be empty")
        for name, optional_value in (
            ("reason_code", self.reason_code),
            ("detail", self.detail),
            ("grant_id", self.grant_id),
            ("holder_host_id", self.holder_host_id),
            ("lease_expires_at", self.lease_expires_at),
            ("renewed_at", self.renewed_at),
        ):
            if optional_value is not None and (
                not isinstance(optional_value, str) or not optional_value
            ):
                raise ValueError(f"{name} must be a non-empty string or null")
        try:
            reported_at = datetime.fromisoformat(self.reported_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("reported_at must be an ISO-8601 timestamp") from error
        if reported_at.tzinfo is None:
            raise ValueError("reported_at must include a timezone")

    def to_wire(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "host_id": self.host_id,
            "station_id": self.station_id,
            "authority_state": self.authority_state,
            "write_state": self.write_state,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "grant_id": self.grant_id,
            "holder_host_id": self.holder_host_id,
            "lease_expires_at": self.lease_expires_at,
            "renewed_at": self.renewed_at,
            "reported_at": self.reported_at,
        }

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> ReportedExecutionAuthority:
        _require_keys(
            value,
            {
                "contract_version",
                "host_id",
                "station_id",
                "authority_state",
                "write_state",
                "reason_code",
                "detail",
                "grant_id",
                "holder_host_id",
                "lease_expires_at",
                "renewed_at",
                "reported_at",
            },
            "reported execution authority",
        )
        contract_version = _positive_int(value["contract_version"], "contract_version")
        if contract_version != EXECUTION_AUTHORITY_REPORT_CONTRACT_VERSION:
            raise ValueError("reported execution authority contract version is unsupported")
        optional: dict[str, str | None] = {}
        for name in (
            "reason_code",
            "detail",
            "grant_id",
            "holder_host_id",
            "lease_expires_at",
            "renewed_at",
        ):
            raw = value[name]
            optional[name] = None if raw is None else _string(raw, name)
        return cls(
            host_id=_string(value["host_id"], "host_id"),
            station_id=_string(value["station_id"], "station_id"),
            authority_state=_string(value["authority_state"], "authority_state"),
            write_state=_string(value["write_state"], "write_state"),
            reason_code=optional["reason_code"],
            detail=optional["detail"],
            grant_id=optional["grant_id"],
            holder_host_id=optional["holder_host_id"],
            lease_expires_at=optional["lease_expires_at"],
            renewed_at=optional["renewed_at"],
            reported_at=_string(value["reported_at"], "reported_at"),
            contract_version=contract_version,
        )


def reported_execution_authority_to_wire(
    report: ReportedExecutionAuthority,
) -> dict[str, object]:
    return report.to_wire()


def reported_execution_authority_from_wire(
    value: Mapping[str, object],
) -> ReportedExecutionAuthority:
    return ReportedExecutionAuthority.from_wire(value)


@dataclass(frozen=True, slots=True)
class ReportedHealth:
    """一路流健康事实的至少一次上报；它是观测有效性，不是观测，也不是判定。

    每路流用自己的稳定事件身份上报，携带事实发生时间、工位/流身份和源时间锚及偏移。
    中心只按事件 id 幂等归档，不据此重新判定，也不反写 edge。
    """

    event_id: str
    trace_id: str
    host_id: str
    station_id: str
    stream_id: str | None
    status: str
    reason_code: str | None
    detail: str | None
    occurred_at: str
    source_anchor: float | None
    anchor_offset: float | None
    reported_at: str
    contract_version: int = HEALTH_REPORT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name, value in (
            ("event_id", self.event_id),
            ("trace_id", self.trace_id),
            ("host_id", self.host_id),
            ("station_id", self.station_id),
            ("status", self.status),
            ("occurred_at", self.occurred_at),
            ("reported_at", self.reported_at),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must not be empty")
        if self.stream_id is not None and not self.stream_id:
            raise ValueError("stream_id must be non-empty or null")
        if self.reason_code is not None and not self.reason_code:
            raise ValueError("reason_code must be non-empty or null")
        if self.detail is not None and not isinstance(self.detail, str):
            raise ValueError("health detail must be a string or null")
        if self.contract_version != HEALTH_REPORT_CONTRACT_VERSION:
            raise ValueError("reported health contract version is unsupported")
        for label, numeric in (
            ("source_anchor", self.source_anchor),
            ("anchor_offset", self.anchor_offset),
        ):
            if numeric is not None:
                _finite_number(numeric, f"reported health {label}")

    def to_wire(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "event_id": self.event_id,
            "trace_id": self.trace_id,
            "host_id": self.host_id,
            "station_id": self.station_id,
            "stream_id": self.stream_id,
            "status": self.status,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "occurred_at": self.occurred_at,
            "source_anchor": self.source_anchor,
            "anchor_offset": self.anchor_offset,
            "reported_at": self.reported_at,
        }

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> ReportedHealth:
        _require_keys(
            value,
            {
                "contract_version",
                "event_id",
                "trace_id",
                "host_id",
                "station_id",
                "stream_id",
                "status",
                "reason_code",
                "detail",
                "occurred_at",
                "source_anchor",
                "anchor_offset",
                "reported_at",
            },
            "reported health",
        )
        reason = value["reason_code"]
        detail = value["detail"]
        stream_id = value["stream_id"]
        if reason is not None and not isinstance(reason, str):
            raise ValueError("health reason_code is invalid")
        if detail is not None and not isinstance(detail, str):
            raise ValueError("health detail is invalid")
        if stream_id is not None and not isinstance(stream_id, str):
            raise ValueError("health stream_id is invalid")
        return cls(
            event_id=_string(value["event_id"], "event_id"),
            trace_id=_string(value["trace_id"], "trace_id"),
            host_id=_string(value["host_id"], "host_id"),
            station_id=_string(value["station_id"], "station_id"),
            stream_id=stream_id,
            status=_string(value["status"], "status"),
            reason_code=reason,
            detail=detail,
            occurred_at=_string(value["occurred_at"], "occurred_at"),
            source_anchor=_optional_number(value["source_anchor"], "source_anchor"),
            anchor_offset=_optional_number(value["anchor_offset"], "anchor_offset"),
            reported_at=_string(value["reported_at"], "reported_at"),
            contract_version=_positive_int(value["contract_version"], "contract_version"),
        )


@dataclass(frozen=True, slots=True)
class ReportedSopInstance:
    """edge SOP 实例生命周期镜像；中心只保存，不重建边界。"""

    event_id: str
    trace_id: str
    host_id: str
    station_id: str
    instance_id: int
    opened_at: float
    closed_at: float | None
    close_reason: str | None
    open_boundary_signal: str | None
    close_boundary_signal: str | None
    template_version_id: str | None
    template_sha256: str | None
    backend_provenance: tuple[ReportBackendProvenance, ...]
    configuration_revision: int
    configuration_sha256: str
    reported_at: str
    contract_version: int = SOP_INSTANCE_REPORT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name, value in (
            ("event_id", self.event_id),
            ("trace_id", self.trace_id),
            ("host_id", self.host_id),
            ("station_id", self.station_id),
            ("configuration_sha256", self.configuration_sha256),
            ("reported_at", self.reported_at),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must not be empty")
        if (
            self.contract_version != SOP_INSTANCE_REPORT_CONTRACT_VERSION
            or self.instance_id < 0
            or self.configuration_revision < 1
        ):
            raise ValueError("reported SOP instance identity is invalid")
        _finite_number(self.opened_at, "instance opened_at")
        if self.closed_at is not None:
            _finite_number(self.closed_at, "instance closed_at")
            if self.closed_at < self.opened_at:
                raise ValueError("instance closed_at must not precede opened_at")
        if (self.closed_at is None) != (self.close_reason is None):
            raise ValueError("instance close time and reason must be supplied together")
        for name, signal in (
            ("open_boundary_signal", self.open_boundary_signal),
            ("close_boundary_signal", self.close_boundary_signal),
        ):
            if signal is not None and (not isinstance(signal, str) or not signal):
                raise ValueError(f"{name} must be non-empty or null")
        if self.closed_at is None and self.close_boundary_signal is not None:
            raise ValueError("open instance cannot carry a close boundary signal")
        if self.close_boundary_signal is not None and self.close_reason != "closed_by_end_signal":
            raise ValueError("close boundary signal requires end-signal closure")
        if (self.template_version_id is None) != (self.template_sha256 is None):
            raise ValueError("template version and digest must be supplied together")
        if self.template_sha256 is not None and not _is_sha256(self.template_sha256):
            raise ValueError("template_sha256 is invalid")
        if not _is_sha256(self.configuration_sha256):
            raise ValueError("configuration_sha256 is invalid")
        ids = tuple(item.backend_id for item in self.backend_provenance)
        if ids != tuple(sorted(ids)) or len(set(ids)) != len(ids):
            raise ValueError("instance backend provenance must be unique and sorted")

    def to_wire(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "event_id": self.event_id,
            "trace_id": self.trace_id,
            "host_id": self.host_id,
            "station_id": self.station_id,
            "instance_id": self.instance_id,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "close_reason": self.close_reason,
            "open_boundary_signal": self.open_boundary_signal,
            "close_boundary_signal": self.close_boundary_signal,
            "template_version_id": self.template_version_id,
            "template_sha256": self.template_sha256,
            "backend_provenance": [item.to_wire() for item in self.backend_provenance],
            "configuration_revision": self.configuration_revision,
            "configuration_sha256": self.configuration_sha256,
            "reported_at": self.reported_at,
        }

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> ReportedSopInstance:
        _require_keys(
            value,
            {
                "contract_version",
                "event_id",
                "trace_id",
                "host_id",
                "station_id",
                "instance_id",
                "opened_at",
                "closed_at",
                "close_reason",
                "open_boundary_signal",
                "close_boundary_signal",
                "template_version_id",
                "template_sha256",
                "backend_provenance",
                "configuration_revision",
                "configuration_sha256",
                "reported_at",
            },
            "reported SOP instance",
        )
        close_reason = value["close_reason"]
        open_boundary_signal = value["open_boundary_signal"]
        close_boundary_signal = value["close_boundary_signal"]
        template_id = value["template_version_id"]
        template_sha = value["template_sha256"]
        if close_reason is not None and not isinstance(close_reason, str):
            raise ValueError("instance close_reason is invalid")
        if open_boundary_signal is not None and not isinstance(open_boundary_signal, str):
            raise ValueError("instance open_boundary_signal is invalid")
        if close_boundary_signal is not None and not isinstance(close_boundary_signal, str):
            raise ValueError("instance close_boundary_signal is invalid")
        if template_id is not None and not isinstance(template_id, str):
            raise ValueError("instance template_version_id is invalid")
        if template_sha is not None and not isinstance(template_sha, str):
            raise ValueError("instance template_sha256 is invalid")
        return cls(
            event_id=_string(value["event_id"], "event_id"),
            trace_id=_string(value["trace_id"], "trace_id"),
            host_id=_string(value["host_id"], "host_id"),
            station_id=_string(value["station_id"], "station_id"),
            instance_id=_nonnegative_int(value["instance_id"], "instance_id"),
            opened_at=_finite_number(value["opened_at"], "opened_at"),
            closed_at=_optional_number(value["closed_at"], "closed_at"),
            close_reason=close_reason,
            open_boundary_signal=open_boundary_signal,
            close_boundary_signal=close_boundary_signal,
            template_version_id=template_id,
            template_sha256=template_sha,
            backend_provenance=tuple(
                ReportBackendProvenance.from_wire(_object(item, "backend provenance"))
                for item in _array(value["backend_provenance"], "backend_provenance")
            ),
            configuration_revision=_positive_int(
                value["configuration_revision"], "configuration_revision"
            ),
            configuration_sha256=_string(value["configuration_sha256"], "configuration_sha256"),
            reported_at=_string(value["reported_at"], "reported_at"),
            contract_version=_positive_int(value["contract_version"], "contract_version"),
        )


@dataclass(frozen=True, slots=True)
class ReportedObservation:
    """一条归一化观测的至少一次镜像上报；它是观测，不是流健康，也不是判定。

    观测关联稳定事件 ID、所属实例、来源和产生时模板/模型身份，自带源时间与主机锚。
    中心按事件 id 幂等归档产生时内容，不据此重新判定，也不反写 edge。
    它不携带配置证明：模板/模型身份是产生时的冻结事实，中心只保存，不重新归属。
    """

    event_id: str
    trace_id: str
    host_id: str
    station_id: str
    instance_id: int
    source: str
    signal: str
    source_time: float | None
    source_anchor: float | None
    observed_at: float
    template_version_id: str | None
    template_sha256: str | None
    backend: ReportBackendProvenance | None
    reported_at: str
    contract_version: int = OBSERVATION_REPORT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name, value in (
            ("event_id", self.event_id),
            ("trace_id", self.trace_id),
            ("host_id", self.host_id),
            ("station_id", self.station_id),
            ("source", self.source),
            ("signal", self.signal),
            ("reported_at", self.reported_at),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must not be empty")
        if self.source not in OBSERVATION_SOURCES:
            raise ValueError("reported observation source is unsupported")
        if self.contract_version != OBSERVATION_REPORT_CONTRACT_VERSION:
            raise ValueError("reported observation contract version is unsupported")
        if self.instance_id < 0:
            raise ValueError("reported observation instance_id must not be negative")
        _finite_number(self.observed_at, "observed_at")
        for label, amount in (
            ("source_time", self.source_time),
            ("source_anchor", self.source_anchor),
        ):
            if amount is not None:
                _finite_number(amount, f"reported observation {label}")
        if (self.source_time is None) != (self.source_anchor is None):
            raise ValueError("observation source time and source anchor must be supplied together")
        if (self.template_version_id is None) != (self.template_sha256 is None):
            raise ValueError("observation template version and digest must be supplied together")
        if self.template_sha256 is not None and not _is_sha256(self.template_sha256):
            raise ValueError("observation template_sha256 is invalid")
        if self.backend is not None and not isinstance(self.backend, ReportBackendProvenance):
            raise ValueError("observation backend must be backend provenance or null")

    def to_wire(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "event_id": self.event_id,
            "trace_id": self.trace_id,
            "host_id": self.host_id,
            "station_id": self.station_id,
            "instance_id": self.instance_id,
            "source": self.source,
            "signal": self.signal,
            "source_time": self.source_time,
            "source_anchor": self.source_anchor,
            "observed_at": self.observed_at,
            "template_version_id": self.template_version_id,
            "template_sha256": self.template_sha256,
            "backend": None if self.backend is None else self.backend.to_wire(),
            "reported_at": self.reported_at,
        }

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> ReportedObservation:
        _require_keys(
            value,
            {
                "contract_version",
                "event_id",
                "trace_id",
                "host_id",
                "station_id",
                "instance_id",
                "source",
                "signal",
                "source_time",
                "source_anchor",
                "observed_at",
                "template_version_id",
                "template_sha256",
                "backend",
                "reported_at",
            },
            "reported observation",
        )
        template_id = value["template_version_id"]
        template_sha = value["template_sha256"]
        if template_id is not None and not isinstance(template_id, str):
            raise ValueError("observation template_version_id is invalid")
        if template_sha is not None and not isinstance(template_sha, str):
            raise ValueError("observation template_sha256 is invalid")
        backend_raw = value["backend"]
        backend: ReportBackendProvenance | None
        if backend_raw is None:
            backend = None
        else:
            backend = ReportBackendProvenance.from_wire(_object(backend_raw, "observation backend"))
        return cls(
            event_id=_string(value["event_id"], "event_id"),
            trace_id=_string(value["trace_id"], "trace_id"),
            host_id=_string(value["host_id"], "host_id"),
            station_id=_string(value["station_id"], "station_id"),
            instance_id=_nonnegative_int(value["instance_id"], "instance_id"),
            source=_string(value["source"], "source"),
            signal=_string(value["signal"], "signal"),
            source_time=_optional_number(value["source_time"], "source_time"),
            source_anchor=_optional_number(value["source_anchor"], "source_anchor"),
            observed_at=_finite_number(value["observed_at"], "observed_at"),
            template_version_id=template_id,
            template_sha256=template_sha,
            backend=backend,
            reported_at=_string(value["reported_at"], "reported_at"),
            contract_version=_positive_int(value["contract_version"], "contract_version"),
        )


def reported_decision_to_wire(report: ReportedDecision) -> dict[str, object]:
    return report.to_wire()


def reported_decision_from_wire(value: Mapping[str, object]) -> ReportedDecision:
    return ReportedDecision.from_wire(value)


def reported_health_to_wire(report: ReportedHealth) -> dict[str, object]:
    return report.to_wire()


def reported_health_from_wire(value: Mapping[str, object]) -> ReportedHealth:
    return ReportedHealth.from_wire(value)


def reported_sop_instance_to_wire(report: ReportedSopInstance) -> dict[str, object]:
    return report.to_wire()


def reported_sop_instance_from_wire(value: Mapping[str, object]) -> ReportedSopInstance:
    return ReportedSopInstance.from_wire(value)


def reported_observation_to_wire(report: ReportedObservation) -> dict[str, object]:
    return report.to_wire()


def reported_observation_from_wire(value: Mapping[str, object]) -> ReportedObservation:
    return ReportedObservation.from_wire(value)


def _require_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{label} has unsupported or missing fields")


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object")
    return dict(cast(Mapping[str, object], value))


def _array(value: object, label: str) -> tuple[object, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be an array")
    return tuple(value)


def _strings(value: object, label: str, *, require_nonempty: bool = False) -> tuple[str, ...]:
    raw = _array(value, label)
    if any(not isinstance(item, str) or (require_nonempty and not item) for item in raw):
        raise ValueError(f"{label} contains an invalid string")
    return tuple(cast(str, item) for item in raw)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _optional_number(value: object, label: str) -> float | None:
    if value is None:
        return None
    return _finite_number(value, label)


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric or null")
    try:
        result = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{label} must be finite or null") from error
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite or null")
    return result


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdefABCDEF" for character in value)


__all__ = [
    "DECISION_REPORT_CONTRACT_VERSION",
    "DISPOSAL_REPORT_CONTRACT_VERSION",
    "EXECUTION_AUTHORITY_REPORT_CONTRACT_VERSION",
    "HEALTH_REPORT_CAPABILITY",
    "HEALTH_REPORT_CONTRACT_VERSION",
    "OBSERVATION_REPORT_CONTRACT_VERSION",
    "OBSERVATION_SOURCES",
    "OBSERVATION_SOURCE_ACTION",
    "OBSERVATION_SOURCE_EXTERNAL_SIGNAL",
    "REPORT_CONTRACT_VERSION",
    "SOP_INSTANCE_REPORT_CAPABILITY",
    "SOP_INSTANCE_REPORT_CONTRACT_VERSION",
    "ReportBackendProvenance",
    "ReportEvidence",
    "ReportViolation",
    "ReportedDecision",
    "ReportedDisposal",
    "ReportedExecutionAuthority",
    "ReportedHealth",
    "ReportedObservation",
    "ReportedSopInstance",
    "reported_decision_from_wire",
    "reported_decision_to_wire",
    "reported_execution_authority_from_wire",
    "reported_execution_authority_to_wire",
    "reported_health_from_wire",
    "reported_health_to_wire",
    "reported_observation_from_wire",
    "reported_observation_to_wire",
    "reported_sop_instance_from_wire",
    "reported_sop_instance_to_wire",
]
