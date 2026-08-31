"""The store the supervisor writes through: one reaction, one transaction.

The supervisor holds the core's state and performs the commands; this is where both land so
they survive a restart and an unreachable center (§5.7). It is the authority for decisions
and latched violations — the center's `monitor` holds a mirror written by idempotent upsert,
not the original.

**One reaction is one transaction.** `commit` takes the supervisor and the reaction it just
returned, and writes both or neither. That is the ticket's first acceptance criterion.

It takes the supervisor rather than a state for a reason found by making the mistake: with a
`commit(state, commands)` signature, the natural call site is
`commit(driver.state, driver.receive(x).commands)` — and Python evaluates `driver.state`
*before* `receive` runs, so the store persists the state from before the input and writes no
instance row at all. Nothing raises; the loss only surfaces much later, as a restart with no
in-flight pass to conclude. Reading the state inside this method instead makes the argument
order irrelevant, so the trap is closed rather than documented.

**The remaining contract**: commit once per reaction, before the next input reaches the
supervisor. Two consequences, and they are not equally guarded:

- An instance that both opens and closes inside one reaction never appears as an open row, so
  its opening instant is taken from the closing decision's anchor — correct exactly when the
  two are one reaction.
- Committing the *same* reaction twice writes a second decision and a second report event.
  Latched violations and clips are idempotent by construction (uniqueness on the deviation,
  upsert on the anchor), but a decision has no natural key: one instance may legitimately
  reach two decisions at one anchor with one verdict. So this half of the contract is
  unguarded, and a caller that wraps `commit` in a retry must not retry a transaction that
  already committed. Making it structural needs a reaction identity the supervisor does not
  currently produce; #46 is where the centre-side event id is composed and is where to settle
  that if the run loop turns out to need it.

**No clock.** Like the core, this module reads none: every instant it stores arrived from a
caller holding one. A store that stamped its own rows would date them by when the write
happened rather than when the thing happened, and after a restart the monotonic clock those
instants live on has restarted too.

Standard library only (edge-autonomy.md §5.11).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import assert_never

from edge_runtime.judgment.model import (
    Decision,
    HostInstant,
    Instance,
    JudgmentState,
    RuntimeParameters,
    Template,
    Violation,
)
from edge_runtime.local_state.codec import (
    dump_reasons,
    dump_settled,
    dump_signal_set,
    dump_steps,
    load_reasons,
    load_settled,
    load_signal_set,
)
from edge_runtime.local_state.codec import violation as decode_violation
from edge_runtime.local_state.queues import StationQueues
from edge_runtime.local_state.schema import migrate
from edge_runtime.supervisor.commands import (
    ClipEvidence,
    CloseInstance,
    Command,
    LatchViolation,
    RecordDecision,
)
from edge_runtime.supervisor.station import Reaction, StationSupervisor


class StationStore(StationQueues):
    """One station's rows inside the host's database.

    A station rather than a host, because a station is the unit an instance, a decision and a
    violation belong to (CONTEXT.md), and one host runs several. Every statement is scoped by
    the station id, so one station's reads never see another's rows.

    It is a `StationQueues` because the queues are written on the judgment path and drained
    off it: `commit` enqueues in the same transaction as the decision, while the sender and the
    uploader read through the same handle later. One class would mix two lifetimes; two
    unrelated classes would let a decision be written without its report event.
    """

    def commit(self, supervisor: StationSupervisor, reaction: Reaction) -> None:
        """Persist one reaction: the state the core reached and the commands it produced.

        All of it or none. A decision without its report event would be a decision the center
        never hears about; a latched violation without its decision would be a deviation with
        nothing explaining it.

        The state is read from the supervisor here rather than passed in, so no call site can
        hand over a state from before the reaction (see this module's docstring).
        """
        state = supervisor.state
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            if state.instance is not None:
                self._write_instance(state.instance)
            self._perform(reaction.commands)
        except Exception:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")

    def _perform(self, commands: Sequence[Command]) -> None:
        """Each command in the order the supervisor issued it.

        `commands_for` emits one decision's commands contiguously, `RecordDecision` first, and
        the violations, clips and close that follow belong to it. That ordering is relied on
        here to attribute a violation to the decision that reported it, so a command arriving
        without its decision raises rather than being attributed to whichever came before.
        """
        decision_id: int | None = None
        anchor: HostInstant | None = None
        for command in commands:
            match command:
                case RecordDecision():
                    decision_id = self._write_decision(command.decision)
                    anchor = command.decision.evidence.anchor
                    self._enqueue_report(decision_id)
                case LatchViolation():
                    if decision_id is None:
                        raise ValueError(
                            "a violation to latch arrived before the decision that reported "
                            "it; commands must reach the store in the order they were issued"
                        )
                    self._latch(command.instance_id, decision_id, command.violation)
                case ClipEvidence():
                    self._enqueue_evidence(command)
                case CloseInstance():
                    if anchor is None:
                        raise ValueError(
                            "an instance to close arrived before the decision that closed it; "
                            "commands must reach the store in the order they were issued"
                        )
                    self._close_instance(command, at=anchor)
                case _:
                    assert_never(command)

    def _write_instance(self, instance: Instance) -> None:
        """The in-flight instance as the core holds it, replacing the previous snapshot.

        The whole record rather than the fields that changed: the core returns a new
        `Instance` each transition, and writing a subset would make this row a second model of
        the same thing, free to disagree with it.
        """
        self._connection.execute(
            """
            INSERT INTO local_sop_instance (
                station_id, instance_id, opened_at, last_observation_at,
                seen, expected_index, impairments, settled
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (station_id, instance_id) DO UPDATE SET
                last_observation_at = excluded.last_observation_at,
                seen                = excluded.seen,
                expected_index      = excluded.expected_index,
                impairments         = excluded.impairments,
                settled             = excluded.settled
            """,
            (
                self._station_id,
                instance.instance_id,
                instance.opened_at.seconds,
                instance.last_observation_at.seconds,
                dump_signal_set(instance.seen),
                instance.expected_index,
                dump_reasons(instance.impairments),
                dump_settled(instance.settled),
            ),
        )

    def _write_decision(self, decision: Decision) -> int:
        cursor = self._connection.execute(
            """
            INSERT INTO local_decision (
                station_id, instance_id, verdict, reasons, lifecycle,
                evidence_anchor, evidence_from, evidence_to
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._station_id,
                decision.instance_id,
                decision.verdict.value,
                dump_reasons(decision.reasons),
                decision.lifecycle.value,
                decision.evidence.anchor.seconds,
                decision.evidence.required_from.seconds,
                decision.evidence.required_to.seconds,
            ),
        )
        return int(cursor.lastrowid or 0)

    def _latch(self, instance_id: int, decision_id: int, violation: Violation) -> None:
        """Insert-only. One deviation in one instance is one row, re-reported or not.

        `DO NOTHING` rather than an update: the row already there is the same fact, and the
        first record of when it was confirmed is the one worth keeping (§5.2).
        """
        self._connection.execute(
            """
            INSERT INTO local_violation (
                station_id, instance_id, decision_id, reason, steps,
                evidence_anchor, evidence_from, evidence_to
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (station_id, instance_id, reason, steps) DO NOTHING
            """,
            (
                self._station_id,
                instance_id,
                decision_id,
                violation.reason.value,
                dump_steps(violation.steps),
                violation.evidence.anchor.seconds,
                violation.evidence.required_from.seconds,
                violation.evidence.required_to.seconds,
            ),
        )

    def _enqueue_report(self, decision_id: int) -> None:
        self._connection.execute(
            "INSERT INTO local_report_queue (station_id, decision_id) VALUES (?, ?)",
            (self._station_id, decision_id),
        )

    def _enqueue_evidence(self, clip: ClipEvidence) -> None:
        """One clip per anchor, widening the window when a second conclusion needs more.

        Widening only, never shortening — the same rule the supervisor applies when it adds
        margins (§5.20). Two conclusions in one instance anchored at one instant are one clip
        covering both, because cutting the same seconds twice is waste no reviewer sees.
        """
        self._connection.execute(
            """
            INSERT INTO local_evidence_queue (
                station_id, instance_id, anchor, window_from, window_to
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (station_id, instance_id, anchor) DO UPDATE SET
                window_from = min(window_from, excluded.window_from),
                window_to   = max(window_to,   excluded.window_to)
            """,
            (
                self._station_id,
                clip.instance_id,
                clip.anchor.seconds,
                clip.start.seconds,
                clip.end.seconds,
            ),
        )

    def _close_instance(self, close: CloseInstance, *, at: HostInstant) -> None:
        """Mark the instance concluded by the named condition.

        The insert branch is reachable only for an instance that opened and closed inside one
        reaction and so never appeared as an open row. Its opening instant is the closing
        decision's anchor, which is that same instant — what "commit once per reaction" buys.
        """
        self._connection.execute(
            """
            INSERT INTO local_sop_instance (
                station_id, instance_id, opened_at, last_observation_at,
                seen, expected_index, impairments, settled, closed_at, lifecycle
            ) VALUES (?, ?, ?, ?, '[]', 0, '[]', '[]', ?, ?)
            ON CONFLICT (station_id, instance_id) DO UPDATE SET
                closed_at = excluded.closed_at,
                lifecycle = excluded.lifecycle
            """,
            (
                self._station_id,
                close.instance_id,
                at.seconds,
                at.seconds,
                at.seconds,
                close.lifecycle.value,
            ),
        )

    def latched_violations(self, *, instance_id: int) -> tuple[Violation, ...]:
        """Every deviation confirmed for this instance, in the order they were confirmed.

        Independent of what the instance finally concluded: an instance may be indeterminate
        and still carry these (§5.2). One says the pass's conclusion is unreliable, the other
        says this deviation happened.
        """
        rows = self._connection.execute(
            """
            SELECT reason, steps, evidence_anchor, evidence_from, evidence_to
              FROM local_violation
             WHERE station_id = ? AND instance_id = ?
             ORDER BY violation_id
            """,
            (self._station_id, instance_id),
        ).fetchall()
        return tuple(decode_violation(row) for row in rows)

    def resume(self, template: Template, parameters: RuntimeParameters) -> JudgmentState:
        """The core's state as this station left it, for the run loop now starting.

        The template and the resolved parameters arrive from the caller rather than from this
        database: they are the last confirmed configuration, which the center owns and the host
        caches (§5.3). Reconstructed here is only what this store alone knows — the instance
        that was in flight, and how far instance numbering has gone.

        `active_impairments` is deliberately not restored. Stream health and backend
        reachability are facts about a run, and this is a new run: the supervisor learns them
        again from the health channel it is about to consume (§5.11). Carrying them across
        would leave a station that died while its stream was down permanently indeterminate,
        with no event able to clear a fact nothing will re-report.

        `next_instance_id` is derived rather than stored; `_next_instance_id` carries what that
        rests on.
        """
        return JudgmentState(
            template=template,
            parameters=parameters,
            instance=self._in_flight(),
            next_instance_id=self._next_instance_id(),
        )

    def _in_flight(self) -> Instance | None:
        row = self._connection.execute(
            """
            SELECT instance_id, opened_at, last_observation_at, seen, expected_index,
                   impairments, settled
              FROM local_sop_instance
             WHERE station_id = ? AND closed_at IS NULL
             ORDER BY instance_id DESC
             LIMIT 1
            """,
            (self._station_id,),
        ).fetchone()
        if row is None:
            return None
        return Instance(
            instance_id=row["instance_id"],
            opened_at=HostInstant(row["opened_at"]),
            last_observation_at=HostInstant(row["last_observation_at"]),
            seen=load_signal_set(row["seen"]),
            expected_index=row["expected_index"],
            impairments=load_reasons(row["impairments"]),
            settled=load_settled(row["settled"]),
        )

    def _next_instance_id(self) -> int:
        """One past the highest instance this station ever opened.

        Derived rather than stored, because the core advances its counter only when it opens
        an instance — so the highest row, plus one, is that value.

        **This depends on the module's commit contract**, and is the second place that
        contract carries weight. An instance the core opened but that was never committed
        leaves no row, so its number is handed out again after a restart, and two different
        passes end up sharing one identity in the center's mirror. Nothing detects it: unlike
        an out-of-order command, a reused number raises nothing. The contract holds today
        because the only caller commits every reaction (`resume_station`); a run loop that
        batches reactions to save writes would break this, and would have to store the
        counter instead of deriving it.
        """
        highest = self._connection.execute(
            "SELECT max(instance_id) FROM local_sop_instance WHERE station_id = ?",
            (self._station_id,),
        ).fetchone()[0]
        return 1 if highest is None else int(highest) + 1


class LocalState:
    """The inference host's database: every station it runs, in one SQLite file."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def station(self, station_id: str) -> StationStore:
        """This station's rows. Cheap: a scope on the same connection, not a new one."""
        return StationStore(self._connection, station_id)

    def close(self) -> None:
        self._connection.close()


def open_local_state(path: str) -> LocalState:
    """Open or create the host's local state, migrated to the current schema.

    `:memory:` is accepted, and is what a test uses when it is not asserting about a restart.

    The connection is in autocommit mode, so `commit` states its transaction explicitly and a
    single-statement update on a queue needs no ceremony. Foreign keys are enabled per
    connection because SQLite defaults them off, and the schema's references are what keep a
    decision from outliving its instance.
    """
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    # Durability over speed: this database is the authority for latched violations and for
    # what the center has not yet been told (§5.7), and it is the only copy of both.
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    migrate(connection)
    return LocalState(connection)
