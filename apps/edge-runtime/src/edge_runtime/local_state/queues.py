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
class PendingReport:
    """中心尚未确认的一项判定,以及重试成本。

    ``queue_id`` 是上报事件身份的本地半边;wire event id 由主机身份和它组成,因此同一事件的每次重试
    都稳定,不需要为已有主键再发明第二个标识。
    """

    queue_id: int
    decision: Decision
    attempts: int
    last_error: str | None


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
                SELECT q.queue_id, q.attempts, q.last_error, d.decision_id, d.instance_id,
                       d.verdict, d.reasons, d.lifecycle,
                       d.evidence_anchor, d.evidence_from, d.evidence_to
                  FROM local_report_queue q
                  JOIN local_decision d ON d.decision_id = q.decision_id
                 WHERE q.station_id = ? AND q.sent_at IS NULL
                 ORDER BY q.queue_id
                 LIMIT ?
                """,
                (self._station_id, -1 if limit is None else limit),
            ).fetchall()
            return tuple(
                PendingReport(
                    queue_id=row["queue_id"],
                    decision=self._decision_of(row),
                    attempts=row["attempts"],
                    last_error=row["last_error"],
                )
                for row in rows
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
