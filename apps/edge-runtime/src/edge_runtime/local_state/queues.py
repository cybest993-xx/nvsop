"""The two things this host still owes the center: a report, and a clip.

Separate from the store's write path because the callers and their lifetimes differ. The run
loop writes through `commit` on the judgment path, where latency is budgeted (§5.6); these
are drained by the sender (#46) and the evidence uploader (#52), which retry on their own
schedule and must never block a judgment.

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
from dataclasses import dataclass

from edge_runtime.judgment.model import Decision, HostInstant, Lifecycle, Violation
from edge_runtime.judgment.reasons import ReasonCode, Verdict
from edge_runtime.local_state.codec import span
from edge_runtime.local_state.codec import violation as decode_violation


@dataclass(frozen=True, slots=True)
class PendingReport:
    """One decision the center has not acknowledged, with what retrying has cost so far.

    `queue_id` is the local half of the report event's identity. The wire-level event id the
    center upserts by is composed from the reporting host's identity and this value (#46), so
    it is stable across every retry of one event without this module inventing a second
    identifier for what the primary key already names.
    """

    queue_id: int
    decision: Decision
    attempts: int
    last_error: str | None


@dataclass(frozen=True, slots=True)
class PendingEvidence:
    """One clip that exists nowhere but this host.

    The window is already widened by the station's margins — the supervisor did that when it
    turned the decision into a command (§5.20) — so an uploader takes these instants as they
    stand rather than re-deriving them.
    """

    queue_id: int
    instance_id: int
    anchor: HostInstant
    start: HostInstant
    end: HostInstant
    attempts: int
    last_error: str | None


class StationQueues:
    """One station's outstanding work, on the host's database."""

    def __init__(self, connection: sqlite3.Connection, station_id: str) -> None:
        self._connection = connection
        self._station_id = station_id

    def pending_reports(self, *, limit: int | None = None) -> tuple[PendingReport, ...]:
        """Decisions the center has not acknowledged, oldest first.

        Oldest first because the mirror reads in the order things happened, and `queue_id` is
        that order.
        """
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
        """The center acknowledged this event. It stops being owed and stays on record.

        Marked rather than deleted: the center's idempotent upsert needs the local half of
        the event's identity to stay stable across retries (#46), and "what has this host
        already reported" is the question retention answers when it trims reported data
        (§5.19). Trimming a settled row is retention's, and only ever one carrying `sent_at`.
        """
        self._connection.execute(
            "UPDATE local_report_queue SET sent_at = ? WHERE station_id = ? AND queue_id = ?",
            (at.seconds, self._station_id, queue_id),
        )

    def record_report_failure(self, queue_id: int, *, at: HostInstant, error: str) -> None:
        """A send failed. The row stays owed and counts one more attempt."""
        self._connection.execute(
            """
            UPDATE local_report_queue
               SET attempts = attempts + 1, last_attempt_at = ?, last_error = ?
             WHERE station_id = ? AND queue_id = ?
            """,
            (at.seconds, error, self._station_id, queue_id),
        )

    def pending_evidence(self, *, limit: int | None = None) -> tuple[PendingEvidence, ...]:
        """Clips that exist only on this host, oldest first."""
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
        """A remote copy now exists, and this names where it is.

        `remote_reference` is required rather than optional because it *is* the evidence that
        a second copy exists. Until a row carries one, the local file is the only copy there
        is and nothing may release it (§5.19).
        """
        if not remote_reference:
            raise ValueError(
                "uploaded evidence must name where the remote copy is; without it there is "
                "no proof of a second copy and the local one cannot be released"
            )
        self._connection.execute(
            """
            UPDATE local_evidence_queue
               SET uploaded_at = ?, remote_reference = ?
             WHERE station_id = ? AND queue_id = ?
            """,
            (at.seconds, remote_reference, self._station_id, queue_id),
        )

    def record_evidence_failure(self, queue_id: int, *, at: HostInstant, error: str) -> None:
        """An upload failed. The row stays owed, and the local copy stays where it is."""
        self._connection.execute(
            """
            UPDATE local_evidence_queue
               SET attempts = attempts + 1, last_attempt_at = ?, last_error = ?
             WHERE station_id = ? AND queue_id = ?
            """,
            (at.seconds, error, self._station_id, queue_id),
        )
