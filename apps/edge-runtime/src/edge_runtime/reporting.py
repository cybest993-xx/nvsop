"""把本地判定至少一次转换为中心上报事件。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from nvsop_contracts import (
    DECISION_REPORT_CONTRACT_VERSION,
    ConfigurationBundle,
    ReportBackendProvenance,
    ReportedDecision,
    ReportedSopInstance,
    ReportEvidence,
    ReportViolation,
    configuration_from_wire,
)

from edge_runtime.judgment.model import Decision, HostInstant, Lifecycle
from edge_runtime.local_state import (
    PendingReport,
    PendingSopInstanceReport,
    ReportContext,
    ReportStore,
)


class DecisionReportTransport(Protocol):
    def send_decision(
        self,
        report: ReportedDecision,
        *,
        configuration: ConfigurationBundle | None,
    ) -> None: ...

    def send_instance(
        self,
        report: ReportedSopInstance,
        *,
        configuration: ConfigurationBundle | None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ReportAttempt:
    queue_id: int
    sent: bool
    event_id: str
    error: str | None = None


class HostReportReconciler:
    """排空本机结构化事实,但绝不阻塞判定持久化。"""

    def __init__(
        self,
        *,
        reports: ReportStore,
        transport: DecisionReportTransport,
    ) -> None:
        self._reports = reports
        self._transport = transport

    def flush(
        self, *, now: HostInstant, reported_at: str, limit: int | None = None
    ) -> tuple[ReportAttempt, ...]:
        attempts: list[ReportAttempt] = []
        for pending in self._reports.pending_items(limit=limit):
            if isinstance(pending, PendingSopInstanceReport):
                event_id = (
                    f"{pending.context.host_id}:{pending.context.station_id}:"
                    f"instance:{pending.instance_id}"
                )
                try:
                    stable_reported_at = pending.reported_at
                    if stable_reported_at is None:
                        stable_reported_at = self._reports.freeze_reported_at(
                            pending.queue_id, candidate=reported_at
                        )
                    opening_report = reported_open_instance_from_pending(
                        pending, reported_at=stable_reported_at
                    )
                    self._transport.send_instance(
                        opening_report,
                        configuration=_configuration_from_context(pending.context),
                    )
                except Exception as error:
                    message = f"{type(error).__name__}: {error}"[:255]
                    self._reports.record_report_failure(pending.queue_id, at=now, error=message)
                    attempts.append(
                        ReportAttempt(
                            queue_id=pending.queue_id,
                            sent=False,
                            event_id=event_id,
                            error=message,
                        )
                    )
                    continue
                self._reports.mark_reported(pending.queue_id, at=now)
                attempts.append(
                    ReportAttempt(queue_id=pending.queue_id, sent=True, event_id=event_id)
                )
                continue
            event_id = (
                f"{pending.context.host_id}:{pending.queue_id}"
                if pending.context is not None
                else f"legacy-pending:{pending.queue_id}"
            )
            try:
                stable_reported_at = pending.reported_at
                if stable_reported_at is None and pending.context is not None:
                    stable_reported_at = self._reports.freeze_reported_at(
                        pending.queue_id, candidate=reported_at
                    )
                decision_report = reported_decision_from_pending(
                    pending,
                    reported_at=stable_reported_at or reported_at,
                )
            except Exception as error:
                message = f"{type(error).__name__}: {error}"[:255]
                self._reports.record_report_failure(pending.queue_id, at=now, error=message)
                attempts.append(
                    ReportAttempt(
                        queue_id=pending.queue_id,
                        sent=False,
                        event_id=event_id,
                        error=message,
                    )
                )
                continue
            try:
                self._transport.send_decision(
                    decision_report,
                    configuration=_configuration_from_context(pending.context),
                )
                instance_report = reported_instance_from_pending(
                    pending, reported_at=stable_reported_at or reported_at
                )
                if instance_report is not None:
                    self._transport.send_instance(
                        instance_report,
                        configuration=_configuration_from_context(pending.context),
                    )
            except Exception as error:
                message = f"{type(error).__name__}: {error}"[:255]
                self._reports.record_report_failure(pending.queue_id, at=now, error=message)
                attempts.append(
                    ReportAttempt(
                        queue_id=pending.queue_id,
                        sent=False,
                        event_id=decision_report.event_id,
                        error=message,
                    )
                )
                continue
            self._reports.mark_reported(pending.queue_id, at=now)
            attempts.append(
                ReportAttempt(
                    queue_id=pending.queue_id,
                    sent=True,
                    event_id=decision_report.event_id,
                )
            )
        return tuple(attempts)


def reported_decision_from_pending(
    pending: PendingReport,
    *,
    reported_at: str,
) -> ReportedDecision:
    """映射精确的本地持久判定;中心不重新判定。"""
    decision: Decision = pending.decision
    context = pending.context
    if context is None:
        raise ValueError("legacy pending report has no event-time report context")
    event_id = f"{context.host_id}:{pending.queue_id}"
    violations = tuple(
        ReportViolation(
            reason_code=violation.reason.value,
            detail=None,
            step_ids=tuple(violation.steps),
            evidence=ReportEvidence(
                anchor=violation.evidence.anchor.seconds,
                start=violation.evidence.required_from.seconds,
                end=violation.evidence.required_to.seconds,
            ),
        )
        for violation in decision.violations
    )
    evidence = ReportEvidence(
        anchor=decision.evidence.anchor.seconds,
        start=decision.evidence.required_from.seconds,
        end=decision.evidence.required_to.seconds,
    )
    if context.configuration_revision is None:
        if len(context.backends) != 1:
            raise ValueError("unconfirmed report requires exactly one backend provenance for v1")
        backend = context.backends[0]
        return ReportedDecision(
            event_id=event_id,
            trace_id=event_id,
            host_id=context.host_id,
            station_id=context.station_id,
            backend_id=backend.backend_id,
            instance_id=decision.instance_id,
            verdict=decision.verdict.value,
            reason_codes=tuple(reason.value for reason in decision.reasons),
            violations=violations,
            lifecycle=decision.lifecycle.value,
            evidence=evidence,
            template_version_id=context.template_version_id,
            template_sha256=context.template_sha256,
            model_ids=backend.model_ids,
            reported_at=reported_at,
        )
    return ReportedDecision(
        event_id=event_id,
        trace_id=event_id,
        host_id=context.host_id,
        station_id=context.station_id,
        backend_id=None,
        instance_id=decision.instance_id,
        verdict=decision.verdict.value,
        reason_codes=tuple(reason.value for reason in decision.reasons),
        violations=violations,
        lifecycle=decision.lifecycle.value,
        evidence=evidence,
        template_version_id=context.template_version_id,
        template_sha256=context.template_sha256,
        model_ids=(),
        reported_at=reported_at,
        backend_provenance=tuple(
            ReportBackendProvenance(backend_id=item.backend_id, model_ids=item.model_ids)
            for item in context.backends
        ),
        configuration_revision=context.configuration_revision,
        configuration_sha256=context.configuration_sha256,
        contract_version=DECISION_REPORT_CONTRACT_VERSION,
    )


def _configuration_from_context(context: ReportContext | None) -> ConfigurationBundle | None:
    if context is None or context.configuration_revision is None:
        return None
    raw = context.configuration_json
    if raw is None:
        raise ValueError("historical report has no frozen confirmed configuration")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("frozen confirmed configuration is invalid JSON") from error
    if not isinstance(value, Mapping):
        raise ValueError("frozen confirmed configuration is not an object")
    bundle = configuration_from_wire(value)
    if (
        bundle.host_id != context.host_id
        or bundle.config_revision != context.configuration_revision
        or bundle.effective_sha256 != context.configuration_sha256
    ):
        raise ValueError("frozen confirmed configuration does not match report proof")
    return bundle


def reported_instance_from_pending(
    pending: PendingReport, *, reported_at: str
) -> ReportedSopInstance | None:
    context = pending.context
    if (
        context is None
        or context.configuration_revision is None
        or pending.closed_at is None
        or pending.decision.lifecycle is Lifecycle.STAYS_OPEN
    ):
        return None
    if pending.opened_at is None or context.configuration_sha256 is None:
        raise ValueError("pending report has incomplete SOP instance provenance")
    event_id = f"{context.host_id}:{context.station_id}:instance:{pending.decision.instance_id}"
    return ReportedSopInstance(
        event_id=event_id,
        trace_id=event_id,
        host_id=context.host_id,
        station_id=context.station_id,
        instance_id=pending.decision.instance_id,
        opened_at=pending.opened_at,
        closed_at=pending.closed_at,
        close_reason=pending.close_reason,
        open_boundary_signal=pending.open_boundary_signal,
        close_boundary_signal=pending.close_boundary_signal,
        template_version_id=context.template_version_id,
        template_sha256=context.template_sha256,
        backend_provenance=tuple(
            ReportBackendProvenance(backend_id=item.backend_id, model_ids=item.model_ids)
            for item in context.backends
        ),
        configuration_revision=context.configuration_revision,
        configuration_sha256=context.configuration_sha256,
        reported_at=reported_at,
    )


def reported_open_instance_from_pending(
    pending: PendingSopInstanceReport, *, reported_at: str
) -> ReportedSopInstance:
    context = pending.context
    if context.configuration_revision is None or context.configuration_sha256 is None:
        raise ValueError("pending instance report has no confirmed configuration provenance")
    event_id = f"{context.host_id}:{context.station_id}:instance:{pending.instance_id}"
    return ReportedSopInstance(
        event_id=event_id,
        trace_id=event_id,
        host_id=context.host_id,
        station_id=context.station_id,
        instance_id=pending.instance_id,
        opened_at=pending.opened_at,
        closed_at=None,
        close_reason=None,
        open_boundary_signal=pending.open_boundary_signal,
        close_boundary_signal=None,
        template_version_id=context.template_version_id,
        template_sha256=context.template_sha256,
        backend_provenance=tuple(
            ReportBackendProvenance(backend_id=item.backend_id, model_ids=item.model_ids)
            for item in context.backends
        ),
        configuration_revision=context.configuration_revision,
        configuration_sha256=context.configuration_sha256,
        reported_at=reported_at,
    )


__all__ = [
    "DecisionReportTransport",
    "HostReportReconciler",
    "ReportAttempt",
    "reported_decision_from_pending",
    "reported_instance_from_pending",
    "reported_open_instance_from_pending",
]
