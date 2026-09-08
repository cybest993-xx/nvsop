"""The inference host's local state: what survives a restart and what still owes a send.

These are integration tests, not unit tests, and the distinction is not bureaucratic: SQLite
is this store's real infrastructure rather than a stand-in for it (harness §4), so every
assertion here runs against a real database, a real migration, and a real
`StationSupervisor` producing the commands. They stay in `make check` because SQLite needs
no container and no GPU — what `make check-integration` exists to keep out is Docker, not
integration itself.

What these assertions are about, in the ticket's own terms:

- a migration list runs on a database that already holds rows, and a migration that fails
  leaves neither half a table nor a version claiming it ran;
- one transaction carries a decision, the violations it latched, and the event owed to the
  center — never a subset of the three;
- a latched violation survives rework, an indeterminate close, and an unreachable center;
- a restart concludes what was in flight instead of resuming it (`RUN_INTERRUPTED`);
- both queues retry, and a failed retry never drops the only copy we hold;
- a stored clip keeps the whole span its conclusion required, widened by the margins.
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from store_harness import (
    ANCHOR,
    MARGINS,
    OTHER_STATION,
    STATION,
    STEP_DEADLINE,
    STEPS,
    FakeClock,
    action,
    decision_of,
    lost_stream,
    opening_state,
    supervisor,
)

from edge_runtime.judgment.model import (
    Decision,
    EvidenceSpan,
    HostInstant,
    HostLiveness,
    Lifecycle,
)
from edge_runtime.judgment.reasons import ReasonCode, Verdict
from edge_runtime.local_state import open_local_state
from edge_runtime.local_state.schema import apply_migrations
from edge_runtime.local_state.store import StationStore
from edge_runtime.supervisor.inputs import StreamHealthObserved
from edge_runtime.supervisor.startup import resume_station
from edge_runtime.supervisor.station import Reaction, StationSupervisor


def commit_reaction(station: StationStore, driver: StationSupervisor, reaction: Reaction) -> None:
    """通过持久化 seam 提交 supervisor 产生的领域状态和效果。"""
    station.commit(
        state=driver.state,
        commands=reaction.commands,
        closed_instances=reaction.closed_instances,
    )


class OneTransactionTest(unittest.TestCase):
    """A decision, its violations and its report event are one write or none.

    The center's mirror is written from the queue, so a decision persisted without its
    report event is a decision the center never hears about, and a report event persisted
    without its decision is a send with nothing to send. Neither may be reachable.
    """

    def test_decision_violations_and_report_event_are_stored_together(self) -> None:
        clock = FakeClock()
        state = open_local_state(":memory:")
        station = state.station(STATION)
        driver = supervisor(opening_state(), clock)

        commit_reaction(station, driver, driver.receive(action(STEPS[0], at=ANCHOR)))

        reaction = driver.receive(action(STEPS[2], at=ANCHOR + 1.0))
        commit_reaction(station, driver, reaction)

        decision = decision_of(reaction)
        (pending,) = station.pending_reports()
        self.assertEqual(pending.decision, decision)
        self.assertEqual(
            station.latched_violations(instance_id=decision.instance_id), decision.violations
        )

    def test_a_reaction_that_fails_part_way_through_leaves_none_of_itself_behind(self) -> None:
        """The other half of the same claim: not a subset, and not the empty subset either.

        The fault is a real one rather than a patched-in failure — `_perform` rejects commands
        that reach it out of the order the supervisor issued them, because a violation whose
        decision has not been written cannot be attributed to it. Reversing the tuple is how
        this test reaches that path with commands the supervisor actually produced.

        What the fault path had already written when it raised is the instance row and the
        clip, both of which must be gone; what it never reached is the decision and its report
        event, which must be absent for the other reason. The assertions do not distinguish
        the two, and should not: the claim is that nothing from this reaction is in the
        database, not which statement got how far.
        """
        clock = FakeClock()
        state = open_local_state(":memory:")
        station = state.station(STATION)
        driver = supervisor(opening_state(), clock)

        # Not committed: this reaction opens the instance in memory only, so the reaction that
        # fails below is the one that would have written the instance row for the first time.
        driver.receive(action(STEPS[0], at=ANCHOR))
        reaction = driver.receive(action(STEPS[2], at=ANCHOR + 1.0))
        decision = decision_of(reaction)
        self.assertTrue(decision.violations)

        scrambled = Reaction(commands=tuple(reversed(reaction.commands)), wake_at=reaction.wake_at)
        with self.assertRaises(ValueError):
            commit_reaction(station, driver, scrambled)

        self.assertEqual(station.pending_reports(), ())
        self.assertEqual(station.pending_evidence(), ())
        self.assertEqual(station.latched_violations(instance_id=decision.instance_id), ())
        # `resume` is where the instance row is read on the path that matters, and
        # `next_instance_id` is derived from the highest row — so 1 is the store saying it
        # holds no instance at all, not merely none in flight.
        opening = opening_state()
        resumed = station.resume(opening.template, opening.parameters)
        self.assertIsNone(resumed.instance)
        self.assertEqual(resumed.next_instance_id, 1)


class LatchSurvivesTest(unittest.TestCase):
    """A confirmed deviation never disappears, whatever happens after it.

    Two of the three afterwards this ticket's second criterion names are driven here: the
    operator goes back and does the step (§2.2's rework), and the center cannot be reached to
    be told. The indeterminate close in between is not one of the three — it is how this test
    reaches the rework, since sight was lost first — but it carries §5.2's other half, that a
    pass whose conclusion is unreliable still carries the deviations confirmed while it was
    not.

    **The third, disposal failure, is structural and has no assertion here.** This store
    exposes no `UPDATE` and no `DELETE` for `local_violation` — `_latch` inserts and
    `latched_violations` reads, and there is nothing else. Cross-module access goes through the
    owner's interface rather than the table (harness §1), so the disposal module has no path to
    a latched row whether its dispatch succeeded or failed. An assertion that a module cannot
    do something it has no method for would pin this test to the absence of code rather than to
    behaviour; the regression that belongs with disposal lands with disposal, in #50/#51.
    """

    def test_rework_indeterminate_close_and_failed_reports_all_leave_it_latched(self) -> None:
        clock = FakeClock()
        state = open_local_state(":memory:")
        station = state.station(STATION)
        driver = supervisor(opening_state(), clock)

        for arriving in (action(STEPS[0], at=ANCHOR), action(STEPS[2], at=ANCHOR + 1.0)):
            commit_reaction(station, driver, driver.receive(arriving))
        confirmed = station.latched_violations(instance_id=1)
        self.assertEqual(
            {(violation.reason, violation.steps) for violation in confirmed},
            {
                (ReasonCode.WRONG_STEP, (STEPS[2],)),
                (ReasonCode.MISSED_STEP, (STEPS[1],)),
            },
        )

        # Sight is lost, and then the operator goes back and does the step that was missed.
        # The pass therefore closes indeterminate — we could not see all of it — while the
        # deviations confirmed while we could see stay confirmed (§5.2).
        commit_reaction(
            station,
            driver,
            driver.receive(StreamHealthObserved(event=lost_stream(at=ANCHOR + 2.0))),
        )
        closing = driver.receive(action(STEPS[1], at=ANCHOR + 3.0))
        commit_reaction(station, driver, closing)

        decision = decision_of(closing)
        self.assertEqual(decision.verdict, Verdict.INDETERMINATE)
        self.assertEqual(decision.violations, ())
        self.assertEqual(station.latched_violations(instance_id=1), confirmed)

        # The center cannot be reached. Every send fails; nothing is dropped for it.
        for attempt, pending in enumerate(station.pending_reports(), start=1):
            station.record_report_failure(
                pending.queue_id,
                at=HostInstant(ANCHOR + 10.0 + attempt),
                error="center unreachable",
            )
        self.assertEqual(station.latched_violations(instance_id=1), confirmed)
        self.assertEqual(
            [pending.attempts for pending in station.pending_reports()],
            [1] * len(station.pending_reports()),
        )


class RestartTest(unittest.TestCase):
    """A restart is a run boundary: the pass in flight is concluded, never resumed.

    Both halves matter and they are different claims. Concluding it means the pass that was
    open ends as indeterminate with `RUN_INTERRUPTED` — nobody was watching for part of it, so
    it cannot be concluded on (§5.2). Not resuming it means the next observation opens a *new*
    instance: stitching the two together would present one continuous observation where there
    was a gap, which is exactly the false conclusion the invariant forbids.

    This is the one assertion here that needs a real file. `:memory:` dies with its
    connection, and what is under test is precisely what survives one.
    """

    def test_in_flight_instance_closes_as_run_interrupted_and_the_next_pass_is_new(self) -> None:
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "local-state.sqlite3")

            first = open_local_state(path)
            driver = supervisor(opening_state(), FakeClock())
            first_station = first.station(STATION)
            commit_reaction(first_station, driver, driver.receive(action(STEPS[0], at=ANCHOR)))
            self.assertEqual(driver.state.instance.instance_id if driver.state.instance else 0, 1)
            first.close()

            # The process restarts. Nothing else happened in between.
            second = open_local_state(path)
            station = second.station(STATION)
            clock = FakeClock(now=ANCHOR + 60.0)
            resumed = resume_station(
                station,
                template=opening_state().template,
                parameters=opening_state().parameters,
                margins=MARGINS,
                clock=clock,
            )

            self.assertIsNone(resumed.state.instance)
            interruption = station.pending_reports()[-1].decision
            self.assertEqual(
                interruption,
                Decision(
                    instance_id=1,
                    verdict=Verdict.INDETERMINATE,
                    reasons=(ReasonCode.RUN_INTERRUPTED,),
                    violations=(),
                    lifecycle=Lifecycle.CLOSED_BY_RUN_INTERRUPTION,
                    evidence=EvidenceSpan.at(HostInstant(ANCHOR + 60.0)),
                ),
            )

            # The next pass is a new instance, not a continuation of the interrupted one.
            commit_reaction(station, resumed, resumed.receive(action(STEPS[0], at=ANCHOR + 61.0)))
            self.assertEqual(resumed.state.instance.instance_id if resumed.state.instance else 0, 2)
            second.close()


class QueueRetryTest(unittest.TestCase):
    """Both queues are at-least-once, and a failure may only ever cost an attempt.

    The evidence queue carries the sharper claim. A report that is never delivered costs the
    center a row it could have mirrored; a clip that is dropped is gone, because until it is
    uploaded this host holds the only copy (§5.19). So the failure path may not remove a row,
    and the success path may not be reachable without proof that a second copy exists.
    """

    def setUp(self) -> None:
        self.state = open_local_state(":memory:")
        self.station = self.state.station(STATION)
        driver = supervisor(opening_state(), FakeClock())
        commit_reaction(self.station, driver, driver.receive(action(STEPS[0], at=ANCHOR)))
        commit_reaction(self.station, driver, driver.receive(action(STEPS[2], at=ANCHOR + 1.0)))

    def test_repeated_failures_keep_both_queues_owed_and_only_count_attempts(self) -> None:
        (report,) = self.station.pending_reports()
        for attempt in range(1, 4):
            self.station.record_report_failure(
                report.queue_id, at=HostInstant(ANCHOR + attempt), error=f"attempt {attempt}"
            )
            (still_owed,) = self.station.pending_reports()
            self.assertEqual(still_owed.attempts, attempt)
            self.assertEqual(still_owed.last_error, f"attempt {attempt}")

        clips = self.station.pending_evidence()
        self.assertTrue(clips)
        for clip in clips:
            self.station.record_evidence_failure(
                clip.queue_id, at=HostInstant(ANCHOR + 9.0), error="object store unreachable"
            )
        self.assertEqual(
            {clip.queue_id for clip in self.station.pending_evidence()},
            {clip.queue_id for clip in clips},
        )

    def test_a_report_leaves_the_queue_only_once_the_center_has_acknowledged_it(self) -> None:
        (report,) = self.station.pending_reports()
        self.station.record_report_failure(
            report.queue_id, at=HostInstant(ANCHOR + 1.0), error="center unreachable"
        )
        self.assertEqual(len(self.station.pending_reports()), 1)

        self.station.mark_reported(report.queue_id, at=HostInstant(ANCHOR + 2.0))
        self.assertEqual(self.station.pending_reports(), ())

    def test_evidence_cannot_be_marked_uploaded_without_naming_the_remote_copy(self) -> None:
        (clip, *_) = self.station.pending_evidence()
        with self.assertRaises(ValueError):
            self.station.mark_evidence_uploaded(
                clip.queue_id, at=HostInstant(ANCHOR + 2.0), remote_reference=""
            )
        self.assertIn(clip.queue_id, {owed.queue_id for owed in self.station.pending_evidence()})

        self.station.mark_evidence_uploaded(
            clip.queue_id, at=HostInstant(ANCHOR + 3.0), remote_reference="s3://evidence/abc"
        )
        self.assertNotIn(clip.queue_id, {owed.queue_id for owed in self.station.pending_evidence()})

    def test_one_stations_queues_are_not_visible_to_another_on_the_same_host(self) -> None:
        """One database per host, several stations in it (§5.7) — so scoping is load-bearing.

        Without it a station would drain another's queue, report another's decisions to the
        center under its own identity, and hold a lease on evidence it never produced.
        """
        other = self.state.station(OTHER_STATION)
        self.assertEqual(other.pending_reports(), ())
        self.assertEqual(other.pending_evidence(), ())
        self.assertEqual(other.latched_violations(instance_id=1), ())


class EvidenceWindowTest(unittest.TestCase):
    """A stored clip keeps the whole span its conclusion required, plus the margins.

    The deadline is the case that distinguishes this from "the anchored instant": what a
    reviewer has to see is the entire wait, not the moment it grew too long (§5.20, story 18).
    So the row must carry the union of the decision's anchor and the violation's span, and the
    margins must widen that union rather than replace it.
    """

    def test_a_deadline_clip_spans_the_whole_wait_widened_by_the_margins(self) -> None:
        clock = FakeClock()
        station = open_local_state(":memory:").station(STATION)
        driver = supervisor(opening_state(), clock)
        commit_reaction(station, driver, driver.receive(action(STEPS[0], at=ANCHOR)))

        clock.now = ANCHOR + STEP_DEADLINE + 1.0
        commit_reaction(station, driver, driver.wake(host=HostLiveness.ALIVE))

        (clip,) = station.pending_evidence()
        self.assertEqual(
            (clip.anchor, clip.start, clip.end),
            (
                HostInstant(ANCHOR + STEP_DEADLINE + 1.0),
                # From the last observation minus the leading margin: the wait started there.
                HostInstant(ANCHOR - MARGINS.leading),
                HostInstant(ANCHOR + STEP_DEADLINE + 1.0 + MARGINS.trailing),
            ),
        )


class MigrationTest(unittest.TestCase):
    """The migration takes a database to the current schema, and running again does nothing.

    This is creation, over the real list: a host restarts far more often than it upgrades, so
    opening an already-current database is the common path and must be a no-op rather than an
    error. `MigrationMechanismTest` carries evolution, which this list cannot show while it is
    one entry long.
    """

    def test_opening_an_already_current_database_changes_nothing(self) -> None:
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "local-state.sqlite3")

            first = open_local_state(path)
            first.close()
            before = _schema_of(path)
            self.assertIn("local_violation", before)

            second = open_local_state(path)
            second.close()
            self.assertEqual(_schema_of(path), before)


class MigrationMechanismTest(unittest.TestCase):
    """Evolution: a second migration reaches a database that already holds rows, or reaches
    none of it.

    The real `MIGRATIONS` is one entry long, so running it can only ever demonstrate creation
    — which is why these assertions drive `apply_migrations` over a list this test owns. What
    is under test is the mechanism every later table arrives through (`local_config`,
    `local_template_version`, `local_disposal` are one entry each), not these two statements.

    The failing case is the one worth having. A host upgrades unattended, in a factory, with
    the only copy of what it has judged and not yet reported (§5.7). A migration that half
    applied would leave that database in a state no code expects: a table that exists with the
    wrong shape, or a `user_version` claiming work that did not happen and so will never be
    retried.
    """

    _FIRST: tuple[str, ...] = ("CREATE TABLE evolving (id INTEGER PRIMARY KEY, kept TEXT)",)
    """A table a host has been running on, holding rows before the upgrade arrives."""

    _SECOND: tuple[str, ...] = (
        "ALTER TABLE evolving ADD COLUMN added TEXT",
        "CREATE TABLE arrived (id INTEGER PRIMARY KEY)",
    )
    """Two statements, because the failing case needs a migration that can get part-way."""

    _SECOND_FAILING: tuple[str, ...] = (
        "CREATE TABLE arrived (id INTEGER PRIMARY KEY)",
        "CREATE TABLE arrived (id INTEGER PRIMARY KEY)",
    )
    """The same first statement, then one that cannot succeed — the table now exists. A real
    migration fails on a constraint or a typo; what matters is that it fails after a statement
    already took effect, which is the only way to reach a half-applied database."""

    def setUp(self) -> None:
        # `isolation_level=None` for the same reason the store opens its connection that way:
        # `apply_migrations` states `BEGIN IMMEDIATE` itself, and sqlite3's implicit
        # transaction would already have opened one.
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(self.connection.close)
        self.assertEqual(apply_migrations(self.connection, (self._FIRST,)), 1)
        self.connection.execute("INSERT INTO evolving (kept) VALUES ('written before upgrade')")

    def test_a_later_migration_runs_on_a_database_that_already_holds_rows(self) -> None:
        self.assertEqual(apply_migrations(self.connection, (self._FIRST, self._SECOND)), 2)

        self.assertEqual(self._version(), 2)
        self.assertIn("arrived", self._tables())
        self.assertEqual(
            self.connection.execute("SELECT kept, added FROM evolving").fetchall(),
            [("written before upgrade", None)],
        )

        # And running the same list again is the restart path, not a second upgrade.
        self.assertEqual(apply_migrations(self.connection, (self._FIRST, self._SECOND)), 2)
        self.assertEqual(self._version(), 2)

    def test_a_failing_migration_leaves_neither_half_a_table_nor_a_version_claiming_it_ran(
        self,
    ) -> None:
        with self.assertRaises(sqlite3.OperationalError):
            apply_migrations(self.connection, (self._FIRST, self._SECOND_FAILING))

        # Nothing of the failed migration survived: not the table its first statement created,
        # and not a version bump that would stop it being retried.
        self.assertNotIn("arrived", self._tables())
        self.assertEqual(self._version(), 1)
        self.assertEqual(
            self.connection.execute("SELECT kept FROM evolving").fetchall(),
            [("written before upgrade",)],
        )

        # Still owed, therefore. The fixed migration reaches this database on the next start.
        self.assertEqual(apply_migrations(self.connection, (self._FIRST, self._SECOND)), 2)
        self.assertIn("arrived", self._tables())

    def _version(self) -> int:
        applied: int = self.connection.execute("PRAGMA user_version").fetchone()[0]
        return applied

    def _tables(self) -> set[str]:
        return {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }


def _schema_of(path: str) -> dict[str, str]:
    """Every table and index this database holds, by name.

    Read with a plain `sqlite3` connection rather than through the store, because what is
    under test is the schema itself — asserting it through the interface that created it would
    only prove the interface agrees with itself.
    """
    connection = sqlite3.connect(path)
    try:
        return {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
            )
        }
    finally:
        connection.close()


if __name__ == "__main__":
    unittest.main()
