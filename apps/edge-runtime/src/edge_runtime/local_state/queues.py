"""The two things this host still owes the center: a report, and a clip.

写入与消费的调用方、生命周期不同: supervisor 在判定路径一次提交 (§5.6),
发送方 (#46) 和证据上传方 (#52) 独立重试消费, 不得阻塞判定。

**At-least-once, and what that forbids.** A row leaves a queue only when the far side has
confirmed it. Every failure path here may cost an attempt and nothing more: there is no
method on either queue that removes a pending row, because these are the only copies this
host holds (§5.7, §5.19). For evidence that is stricter still — a row cannot be recorded as
uploaded without naming where the remote copy is, so no failure can reach the state that
would let the local file go.

Neither queue reads a clock: an attempt's instant arrives from the caller that made the
attempt.

Standard library only (edge-autonomy.md §5.11).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
from dataclasses import dataclass
from threading import RLock

from edge_runtime.judgment.model import Decision, HostInstant, Lifecycle, Violation
from edge_runtime.judgment.reasons import ReasonCode, Verdict
from edge_runtime.local_state.codec import span
from edge_runtime.local_state.codec import violation as decode_violation


@dataclass(frozen=True, slots=True)
class BackendReportContext:
    """判定实例实际使用的一个 backend 及其事件时模型集合。"""

    backend_id: str
    model_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.backend_id:
            raise ValueError("backend report context identity must not be empty")
        if any(not model_id for model_id in self.model_ids):
            raise ValueError("backend report context model ids must not be empty")


@dataclass(frozen=True, slots=True)
class ReportContext:
    """与判定 outbox 行同时冻结的事件时配置和真实 backend provenance。"""

    host_id: str
    station_id: str
    backends: tuple[BackendReportContext, ...]
    template_version_id: str | None
    template_sha256: str | None
    configuration_revision: int | None
    configuration_sha256: str | None
    configuration_json: str | None

    def __post_init__(self) -> None:
        if not self.host_id or not self.station_id:
            raise ValueError("report context identity must not be empty")
        backend_ids = tuple(backend.backend_id for backend in self.backends)
        if len(set(backend_ids)) != len(backend_ids) or backend_ids != tuple(sorted(backend_ids)):
            raise ValueError("report context backends must be unique and sorted")
        if (self.configuration_revision is None) != (self.configuration_sha256 is None):
            raise ValueError(
                "report context configuration revision and digest must be supplied together"
            )
        if self.configuration_revision is not None and self.configuration_revision < 1:
            raise ValueError("report context configuration revision must be positive")
        if self.configuration_sha256 is not None and (
            len(self.configuration_sha256) != 64
            or any(
                character not in "0123456789abcdefABCDEF" for character in self.configuration_sha256
            )
        ):
            raise ValueError("report context configuration digest must be SHA-256")
        if (self.configuration_revision is None) != (self.configuration_json is None):
            raise ValueError("confirmed report context must freeze its configuration bundle")
        if (self.template_version_id is None) != (self.template_sha256 is None):
            raise ValueError("report context template version and digest must be supplied together")
        if self.template_sha256 is not None and (
            len(self.template_sha256) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in self.template_sha256)
        ):
            raise ValueError("report context template digest must be SHA-256")


@dataclass(frozen=True, slots=True)
class PendingReport:
    """中心尚未确认的一项判定,以及重试成本。

    ``queue_id`` 是上报事件身份的本地半边;wire event id 由主机身份和它组成,因此同一事件的每次重试
    都稳定,不需要为已有主键再发明第二个标识。
    """

    queue_id: int
    decision: Decision
    attempts: int
    last_error: str | None
    reported_at: str | None = None
    context: ReportContext | None = None
    opened_at: float | None = None
    closed_at: float | None = None
    close_reason: str | None = None
    open_boundary_signal: str | None = None
    close_boundary_signal: str | None = None


@dataclass(frozen=True, slots=True)
class PendingSopInstanceReport:
    """现有 report outbox 中尚未确认的实例开放快照。"""

    queue_id: int
    instance_id: int
    opened_at: float
    open_boundary_signal: str | None
    attempts: int
    last_error: str | None
    reported_at: str | None
    context: ReportContext


@dataclass(frozen=True, slots=True)
class PendingEvidence:
    """One clip that exists nowhere but this host.

    窗口已由 supervisor 按工位余量加宽 (§5.20), 上传方直接使用, 不再次推导。
    """

    queue_id: int
    instance_id: int
    anchor: HostInstant
    start: HostInstant
    end: HostInstant
    attempts: int
    last_error: str | None


class StationQueues:
    """主机数据库中一个工位尚未完成的工作。"""

    def __init__(
        self,
        connection: sqlite3.Connection,
        station_id: str,
        lock: AbstractContextManager[object] | None = None,
    ) -> None:
        self._connection = connection
        self._station_id = station_id
        self._lock = lock or RLock()

    def pending_reports(self, *, limit: int | None = None) -> tuple[PendingReport, ...]:
        """中心尚未确认的判定,按最早优先返回。

        镜像按事件发生顺序读取,``queue_id`` 正好表达该顺序。
        """
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT q.queue_id, q.attempts, q.last_error,
                       q.report_host_id, q.report_backend_id, q.report_template_version_id,
                       q.report_template_sha256, q.report_model_ids, q.report_reported_at,
                       q.report_backend_provenance,
                       q.report_configuration,
                       q.configuration_revision, q.configuration_sha256,
                       d.decision_id, d.instance_id, d.verdict, d.reasons, d.lifecycle,
                       d.evidence_anchor, d.evidence_from, d.evidence_to,
                       i.opened_at, i.closed_at, i.lifecycle AS instance_lifecycle,
                       i.open_boundary_signal, i.close_boundary_signal
                  FROM local_report_queue q
                  JOIN local_decision d ON d.decision_id = q.decision_id
                  JOIN local_sop_instance i
                    ON i.station_id = q.station_id AND i.instance_id = d.instance_id
                 WHERE q.station_id = ?
                   AND q.report_kind = 'decision'
                   AND q.sent_at IS NULL
                   AND q.superseded_at IS NULL
                 ORDER BY q.queue_id
                 LIMIT ?
                """,
                (self._station_id, -1 if limit is None else limit),
            ).fetchall()
            return tuple(self._pending_report_from_row(row) for row in rows)

    def pending_report(self, queue_id: int) -> PendingReport:
        """按队列身份读取一个仍待发送的判定。"""
        with self._lock:
            row = self._connection.execute(
                """
                SELECT q.queue_id, q.attempts, q.last_error,
                       q.report_host_id, q.report_backend_id, q.report_template_version_id,
                       q.report_template_sha256, q.report_model_ids, q.report_reported_at,
                       q.report_backend_provenance,
                       q.report_configuration,
                       q.configuration_revision, q.configuration_sha256,
                       d.decision_id, d.instance_id, d.verdict, d.reasons, d.lifecycle,
                       d.evidence_anchor, d.evidence_from, d.evidence_to,
                       i.opened_at, i.closed_at, i.lifecycle AS instance_lifecycle,
                       i.open_boundary_signal, i.close_boundary_signal
                  FROM local_report_queue q
                  JOIN local_decision d ON d.decision_id = q.decision_id
                  JOIN local_sop_instance i
                    ON i.station_id = q.station_id AND i.instance_id = d.instance_id
                 WHERE q.station_id = ?
                   AND q.queue_id = ?
                   AND q.report_kind = 'decision'
                   AND q.sent_at IS NULL
                   AND q.superseded_at IS NULL
                """,
                (self._station_id, queue_id),
            ).fetchone()
            if row is None:
                raise ValueError("pending decision report does not exist")
            return self._pending_report_from_row(row)

    def _pending_report_from_row(self, row: sqlite3.Row) -> PendingReport:
        return PendingReport(
            queue_id=row["queue_id"],
            decision=self._decision_of(row),
            attempts=row["attempts"],
            last_error=row["last_error"],
            reported_at=row["report_reported_at"],
            context=self._report_context_of(row),
            opened_at=row["opened_at"] if row["closed_at"] is not None else None,
            closed_at=row["closed_at"],
            close_reason=row["instance_lifecycle"] if row["closed_at"] is not None else None,
            open_boundary_signal=row["open_boundary_signal"],
            close_boundary_signal=row["close_boundary_signal"],
        )

    def pending_instance_reports(
        self, *, limit: int | None = None
    ) -> tuple[PendingSopInstanceReport, ...]:
        """返回同一 report outbox 中仍开放且尚未确认的实例快照。"""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT q.queue_id, q.attempts, q.last_error, q.report_reported_at,
                       q.report_host_id, q.report_template_version_id,
                       q.report_template_sha256, q.report_backend_provenance,
                       q.report_configuration, q.configuration_revision,
                       q.configuration_sha256, q.instance_id, i.opened_at,
                       i.open_boundary_signal
                  FROM local_report_queue q
                  JOIN local_sop_instance i
                    ON i.station_id = q.station_id AND i.instance_id = q.instance_id
                 WHERE q.station_id = ?
                   AND q.report_kind = 'instance'
                   AND q.sent_at IS NULL
                   AND q.superseded_at IS NULL
                   AND i.closed_at IS NULL
                 ORDER BY q.queue_id
                 LIMIT ?
                """,
                (self._station_id, -1 if limit is None else limit),
            ).fetchall()
            return tuple(self._pending_instance_report_from_row(row) for row in rows)

    def pending_instance_report(self, queue_id: int) -> PendingSopInstanceReport:
        """按队列身份读取一个仍待发送的实例开放快照。"""
        with self._lock:
            row = self._connection.execute(
                """
                SELECT q.queue_id, q.attempts, q.last_error, q.report_reported_at,
                       q.report_host_id, q.report_template_version_id,
                       q.report_template_sha256, q.report_backend_provenance,
                       q.report_configuration, q.configuration_revision,
                       q.configuration_sha256, q.instance_id, i.opened_at,
                       i.open_boundary_signal
                  FROM local_report_queue q
                  JOIN local_sop_instance i
                    ON i.station_id = q.station_id AND i.instance_id = q.instance_id
                 WHERE q.station_id = ?
                   AND q.queue_id = ?
                   AND q.report_kind = 'instance'
                   AND q.sent_at IS NULL
                   AND q.superseded_at IS NULL
                   AND i.closed_at IS NULL
                """,
                (self._station_id, queue_id),
            ).fetchone()
            if row is None:
                raise ValueError("pending instance report does not exist")
            return self._pending_instance_report_from_row(row)

    def _pending_instance_report_from_row(self, row: sqlite3.Row) -> PendingSopInstanceReport:
        context = self._report_context_of(row)
        if context is None:
            raise ValueError("pending instance report has no event-time report context")
        return PendingSopInstanceReport(
            queue_id=int(row["queue_id"]),
            instance_id=int(row["instance_id"]),
            opened_at=float(row["opened_at"]),
            open_boundary_signal=row["open_boundary_signal"],
            attempts=int(row["attempts"]),
            last_error=row["last_error"],
            reported_at=row["report_reported_at"],
            context=context,
        )

    def _report_context_of(self, row: sqlite3.Row) -> ReportContext | None:
        raw_provenance = row["report_backend_provenance"]
        if raw_provenance is None:
            # v4/v5 rows predate exact backend provenance. Keeping them unreportable is safer than
            # relabelling a historical decision from current configuration or the old
            # first-backend slot.
            return None
        if row["report_host_id"] is None:
            raise ValueError("pending report has incomplete event-time report context")
        decoded = json.loads(raw_provenance)
        if not isinstance(decoded, list):
            raise ValueError("pending report backend provenance is invalid")
        backends: list[BackendReportContext] = []
        for item in decoded:
            if not isinstance(item, dict) or set(item) != {"backend_id", "model_ids"}:
                raise ValueError("pending report backend provenance is invalid")
            backend_id = item["backend_id"]
            model_ids = item["model_ids"]
            if (
                not isinstance(backend_id, str)
                or not isinstance(model_ids, list)
                or any(not isinstance(model_id, str) for model_id in model_ids)
            ):
                raise ValueError("pending report backend provenance is invalid")
            backends.append(BackendReportContext(backend_id=backend_id, model_ids=tuple(model_ids)))
        template_version_id = row["report_template_version_id"]
        template_sha256 = row["report_template_sha256"]
        if (template_version_id is None) != (template_sha256 is None):
            raise ValueError("pending report has incomplete template context")
        return ReportContext(
            host_id=str(row["report_host_id"]),
            station_id=self._station_id,
            backends=tuple(backends),
            template_version_id=template_version_id,
            template_sha256=template_sha256,
            configuration_json=(
                None if row["report_configuration"] is None else str(row["report_configuration"])
            ),
            configuration_revision=(
                None
                if row["configuration_revision"] is None
                else int(row["configuration_revision"])
            ),
            configuration_sha256=(
                None if row["configuration_sha256"] is None else str(row["configuration_sha256"])
            ),
        )

    def _decision_of(self, row: sqlite3.Row) -> Decision:
        return Decision(
            instance_id=row["instance_id"],
            verdict=Verdict(row["verdict"]),
            reasons=tuple(ReasonCode.from_wire(value) for value in json.loads(row["reasons"])),
            violations=self._violations_of(row["decision_id"]),
            lifecycle=Lifecycle(row["lifecycle"]),
            evidence=span(row["evidence_anchor"], row["evidence_from"], row["evidence_to"]),
        )

    def _violations_of(self, decision_id: int) -> tuple[Violation, ...]:
        rows = self._connection.execute(
            """
            SELECT reason, steps, evidence_anchor, evidence_from, evidence_to
              FROM local_violation
             WHERE station_id = ? AND decision_id = ?
             ORDER BY violation_id
            """,
            (self._station_id, decision_id),
        ).fetchall()
        return tuple(decode_violation(row) for row in rows)

    def freeze_reported_at(self, queue_id: int, *, candidate: str) -> str:
        """持久化首个 wire 上报时刻, 保证同一事件重试保持完全相同的 payload。"""
        if not candidate:
            raise ValueError("reported_at candidate must not be empty")
        with self._lock:
            self._connection.execute(
                """
                UPDATE local_report_queue
                   SET report_reported_at = ?
                 WHERE station_id = ? AND queue_id = ? AND report_reported_at IS NULL
                """,
                (candidate, self._station_id, queue_id),
            )
            row = self._connection.execute(
                """
                SELECT report_reported_at
                  FROM local_report_queue
                 WHERE station_id = ?
                   AND queue_id = ?
                   AND sent_at IS NULL
                   AND superseded_at IS NULL
                """,
                (self._station_id, queue_id),
            ).fetchone()
        if row is None or row["report_reported_at"] is None:
            raise ValueError("pending report could not freeze its wire timestamp")
        return str(row["report_reported_at"])

    def mark_reported(self, queue_id: int, *, at: HostInstant) -> None:
        """中心已确认该事件;它不再待发送,但继续保留记录。

        只标记不删除,以保持重试时的本地事件身份稳定,并让保留策略知道主机已经上报过什么。
        """
        with self._lock:
            self._connection.execute(
                "UPDATE local_report_queue SET sent_at = ? WHERE station_id = ? AND queue_id = ?",
                (at.seconds, self._station_id, queue_id),
            )

    def record_report_failure(self, queue_id: int, *, at: HostInstant, error: str) -> None:
        """发送失败;行继续待发送并增加一次尝试。"""
        with self._lock:
            self._connection.execute(
                """
                UPDATE local_report_queue
                   SET attempts = attempts + 1, last_attempt_at = ?, last_error = ?
                 WHERE station_id = ? AND queue_id = ?
                """,
                (at.seconds, error, self._station_id, queue_id),
            )

    def pending_evidence(self, *, limit: int | None = None) -> tuple[PendingEvidence, ...]:
        """只存在于本机的证据片段,按最早优先返回。"""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT queue_id, instance_id, anchor, window_from, window_to, attempts, last_error
                  FROM local_evidence_queue
                 WHERE station_id = ? AND uploaded_at IS NULL
                 ORDER BY queue_id
                 LIMIT ?
                """,
                (self._station_id, -1 if limit is None else limit),
            ).fetchall()
            return tuple(
                PendingEvidence(
                    queue_id=row["queue_id"],
                    instance_id=row["instance_id"],
                    anchor=HostInstant(row["anchor"]),
                    start=HostInstant(row["window_from"]),
                    end=HostInstant(row["window_to"]),
                    attempts=row["attempts"],
                    last_error=row["last_error"],
                )
                for row in rows
            )

    def mark_evidence_uploaded(
        self, queue_id: int, *, at: HostInstant, remote_reference: str
    ) -> None:
        """远端副本已经存在,并记录其位置。

        ``remote_reference`` 是远端副本存在的证据;没有它时本地文件仍是唯一副本,不能释放。
        """
        if not remote_reference:
            raise ValueError(
                "uploaded evidence must name where the remote copy is; without it there is "
                "no proof of a second copy and the local one cannot be released"
            )
        with self._lock:
            self._connection.execute(
                """
                UPDATE local_evidence_queue
                   SET uploaded_at = ?, remote_reference = ?
                 WHERE station_id = ? AND queue_id = ?
                """,
                (at.seconds, remote_reference, self._station_id, queue_id),
            )

    def record_evidence_failure(self, queue_id: int, *, at: HostInstant, error: str) -> None:
        """上传失败;行继续待上传,本地副本继续保留。"""
        with self._lock:
            self._connection.execute(
                """
                UPDATE local_evidence_queue
                   SET attempts = attempts + 1, last_attempt_at = ?, last_error = ?
                 WHERE station_id = ? AND queue_id = ?
                """,
                (at.seconds, error, self._station_id, queue_id),
            )
