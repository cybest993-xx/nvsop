"""把本地判定至少一次转换为中心上报事件。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from nvsop_contracts import (
    ReportedDecision,
    ReportEvidence,
    ReportViolation,
)

from edge_runtime.judgment.model import Decision, HostInstant
from edge_runtime.local_state.queues import PendingReport, ReportContext, StationQueues


class DecisionReportTransport(Protocol):
    def send_decision(self, report: ReportedDecision) -> None: ...


@dataclass(frozen=True, slots=True)
class ReportAttempt:
    queue_id: int
    sent: bool
    event_id: str
    error: str | None = None


class DecisionReporter:
    """排空一个工位的持久队列,但绝不阻塞判定持久化。"""

    def __init__(
        self,
        *,
        queues: StationQueues,
        transport: DecisionReportTransport,
    ) -> None:
        self._queues = queues
        self._transport = transport

    def flush(
        self, *, now: HostInstant, reported_at: str, limit: int | None = None
    ) -> tuple[ReportAttempt, ...]:
        attempts: list[ReportAttempt] = []
        for pending in self._queues.pending_reports(limit=limit):
            event_id = (
                f"{pending.context.host_id}:{pending.queue_id}"
                if pending.context is not None
                else f"legacy-pending:{pending.queue_id}"
            )
            try:
                stable_reported_at = pending.reported_at
                if stable_reported_at is None and pending.context is not None:
                    stable_reported_at = self._queues.freeze_reported_at(
                        pending.queue_id, candidate=reported_at
                    )
                report = reported_decision_from_pending(
                    pending,
                    reported_at=stable_reported_at or reported_at,
                )
            except Exception as error:
                message = f"{type(error).__name__}: {error}"[:255]
                self._queues.record_report_failure(pending.queue_id, at=now, error=message)
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
                self._transport.send_decision(report)
            except Exception as error:
                message = f"{type(error).__name__}: {error}"[:255]
                self._queues.record_report_failure(pending.queue_id, at=now, error=message)
                attempts.append(
                    ReportAttempt(
                        queue_id=pending.queue_id,
                        sent=False,
                        event_id=report.event_id,
                        error=message,
                    )
                )
                continue
            self._queues.mark_reported(pending.queue_id, at=now)
            attempts.append(
                ReportAttempt(queue_id=pending.queue_id, sent=True, event_id=report.event_id)
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
    return ReportedDecision(
        event_id=event_id,
        trace_id=event_id,
        host_id=context.host_id,
        station_id=context.station_id,
        backend_id=context.backend_id,
        instance_id=decision.instance_id,
        verdict=decision.verdict.value,
        reason_codes=tuple(reason.value for reason in decision.reasons),
        violations=tuple(
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
        ),
        lifecycle=decision.lifecycle.value,
        evidence=ReportEvidence(
            anchor=decision.evidence.anchor.seconds,
            start=decision.evidence.required_from.seconds,
            end=decision.evidence.required_to.seconds,
        ),
        template_version_id=context.template_version_id,
        template_sha256=context.template_sha256,
        model_ids=context.model_ids,
        reported_at=reported_at,
        configuration_revision=context.configuration_revision,
        configuration_sha256=context.configuration_sha256,
    )


__all__ = [
    "DecisionReportTransport",
    "DecisionReporter",
    "ReportAttempt",
    "ReportContext",
    "reported_decision_from_pending",
]
