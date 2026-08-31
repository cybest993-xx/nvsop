"""The inference host's local state: what survives a restart and what still owes a send.

These are integration tests, not unit tests, and the distinction is not bureaucratic: SQLite
is this store's real infrastructure rather than a stand-in for it (harness §4), so every
assertion here runs against a real database, a real migration, and a real
`StationSupervisor` producing the commands. They stay in `make check` because SQLite needs
no container and no GPU — what `make check-integration` exists to keep out is Docker, not
integration itself.

What these assertions are about, in the ticket's own terms:

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
from edge_runtime.local_state import open_local_state, resume_station
from edge_runtime.supervisor.inputs import StreamHealthObserved


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

        station.commit(driver, driver.receive(action(STEPS[0], at=ANCHOR)))

        reaction = driver.receive(action(STEPS[2], at=ANCHOR + 1.0))
        station.commit(driver, reaction)

        decision = decision_of(reaction)
        (pending,) = station.pending_reports()
        self.assertEqual(pending.decision, decision)
        self.assertEqual(
            station.latched_violations(instance_id=decision.instance_id), decision.violations
        )


class LatchSurvivesTest(unittest.TestCase):
    """A confirmed deviation never disappears, whatever happens after it.

    Three afterwards, all of them real: the operator goes back and does the step (§2.2's
    rework), the pass ends up indeterminate because sight was lost before it closed, and the
    center cannot be reached to be told. None of the three may remove a row.
    """

    def test_rework_indeterminate_close_and_failed_reports_all_leave_it_latched(self) -> None:
        clock = FakeClock()
        state = open_local_state(":memory:")
        station = state.station(STATION)
        driver = supervisor(opening_state(), clock)

        for arriving in (action(STEPS[0], at=ANCHOR), action(STEPS[2], at=ANCHOR + 1.0)):
            station.commit(driver, driver.receive(arriving))
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
        station.commit(
            driver, driver.receive(StreamHealthObserved(event=lost_stream(at=ANCHOR + 2.0)))
        )
        closing = driver.receive(action(STEPS[1], at=ANCHOR + 3.0))
        station.commit(driver, closing)

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
            first.station(STATION).commit(driver, driver.receive(action(STEPS[0], at=ANCHOR)))
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
            station.commit(resumed, resumed.receive(action(STEPS[0], at=ANCHOR + 61.0)))
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
        self.station.commit(driver, driver.receive(action(STEPS[0], at=ANCHOR)))
        self.station.commit(driver, driver.receive(action(STEPS[2], at=ANCHOR + 1.0)))

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
        station.commit(driver, driver.receive(action(STEPS[0], at=ANCHOR)))

        clock.now = ANCHOR + STEP_DEADLINE + 1.0
        station.commit(driver, driver.wake(host=HostLiveness.ALIVE))

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

    This is the half of "migrations can create and evolve all autonomous state" that is
    reachable today: a host restarts far more often than it upgrades, so opening an
    already-current database is the common path and must be a no-op rather than an error.
    Evolution itself is `PRAGMA user_version` plus an append-only list — the tables this
    ticket does not create (`local_config`, `local_template_version`, `local_disposal`) are
    each one further entry in it.
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
