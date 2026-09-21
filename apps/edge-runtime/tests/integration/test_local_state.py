"""推理机本地状态的重启、事务与待发送队列回归。

SQLite 是该接缝的真实基础设施, 测试使用真实迁移和 supervisor 反应提交。
无需容器或 GPU, 因此集成用例仍由 make check 执行 (harness §4)。

保留的行为场景:
- 已有数据上的迁移保持原数据, 失败时不留下半张表或虚假的版本号;
- 一个事务提交实例、判定、锁存和两个队列, 失败时全部回滚;
- 返工、不可判定结案和中心不可达不会清除已锁存违规;
- 重启以 RUN_INTERRUPTED 结案, 不续接上次实例;
- 两个队列的失败重试都保留本地唯一副本;
- 证据余量只加宽核心所需跨度, 不截断。
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from nvsop_contracts import (
    ConfigurationBundle,
    ReportBackendProvenance,
    ReportedDecision,
    configuration_to_wire,
)
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
from edge_runtime.local_state import BackendReportContext, ReportContext, open_local_state
from edge_runtime.local_state.schema import MIGRATIONS, apply_migrations, migrate
from edge_runtime.local_state.store import LocalState
from edge_runtime.reporting import HostReportReconciler
from edge_runtime.supervisor.inputs import StreamHealthObserved, Validity, ValidityChanged
from edge_runtime.supervisor.startup import resume_station


class OneTransactionTest(unittest.TestCase):
    """A decision, its violations and its report event are one write or none.

    The center's mirror is written from the queue, so a decision persisted without its
    report event is a decision the center never hears about, and a report event persisted
    without its decision is a send with nothing to send. Neither may be reachable.
    """

    def test_receive_persists_without_a_second_caller_commit(self) -> None:
        state = open_local_state(":memory:")
        self.addCleanup(state.close)
        station = state.station(STATION)
        driver = resume_station(
            station,
            template=opening_state().template,
            parameters=opening_state().parameters,
            margins=MARGINS,
            clock=FakeClock(),
        )
        driver.receive(action(STEPS[0], at=ANCHOR))
        reaction = driver.receive(action(STEPS[2], at=ANCHOR + 1.0))
        self.assertEqual(
            tuple(report.decision for report in station.pending_reports()),
            (decision_of(reaction),),
        )

    def test_decision_violations_and_report_event_are_stored_together(self) -> None:
        clock = FakeClock()
        state = open_local_state(":memory:")
        station = state.station(STATION)
        driver = supervisor(opening_state(), clock, station)

        driver.receive(action(STEPS[0], at=ANCHOR))

        reaction = driver.receive(action(STEPS[2], at=ANCHOR + 1.0))

        decision = decision_of(reaction)
        (pending,) = station.pending_reports()
        self.assertEqual(pending.decision, decision)
        self.assertEqual(
            station.latched_violations(instance_id=decision.instance_id), decision.violations
        )

    def test_a_reaction_that_fails_part_way_through_leaves_none_of_itself_behind(self) -> None:
        """证据队列写入失败时, 已写入的实例、判定、锁存和报告全部回滚。"""
        connection, state = self._fault_database()
        station = state.station(STATION)
        driver = supervisor(opening_state(), FakeClock(), station)
        driver.receive(action(STEPS[0], at=ANCHOR))
        before, deadline = driver.state, driver.wake_at
        arriving = action(STEPS[2], at=ANCHOR + 1.0)

        with self.assertRaises(sqlite3.IntegrityError):
            driver.receive(arriving)

        self.assertEqual((driver.state, driver.wake_at), (before, deadline))
        self.assertEqual(station.resume(before.template, before.parameters), before)
        self.assertEqual(station.pending_reports(), ())
        self.assertEqual(station.pending_evidence(), ())
        self.assertEqual(station.latched_violations(instance_id=1), ())

        connection.execute("DROP TRIGGER fail_evidence")
        reaction = driver.receive(arriving)
        self.assertEqual(
            tuple(report.decision for report in station.pending_reports()), reaction.decisions
        )
        self.assertEqual(
            station.latched_violations(instance_id=1), reaction.decisions[0].violations
        )
        self.assertTrue(reaction.decisions[0].violations)

    def test_failed_reanchoring_is_still_seen_when_the_input_is_retried(self) -> None:
        """重锚的多事件反应失败后, 不得先消费归一化状态而让重试变成误判。"""
        connection, state = self._fault_database()
        station = state.station(STATION)
        driver = supervisor(opening_state(end_signals=("end",)), FakeClock(), station)
        driver.receive(action(STEPS[0], at=ANCHOR))
        before, deadline = driver.state, driver.wake_at
        arriving = replace(action("end", at=ANCHOR + 1.0), source_anchor=ANCHOR + 2.0)
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        with self.assertRaises(sqlite3.IntegrityError):
            driver.receive(arriving)
        self.assertEqual((driver.state, driver.wake_at), (before, deadline))
        self.assertEqual(station.resume(before.template, before.parameters), before)
        self.assertEqual(station.pending_reports(), ())
        self.assertEqual(station.pending_evidence(), ())
        self.assertEqual(statements.count("BEGIN IMMEDIATE"), 1)
        self.assertEqual(statements.count("ROLLBACK"), 1)

        connection.execute("DROP TRIGGER fail_evidence")
        statements.clear()
        reaction = driver.receive(arriving)
        self.assertEqual(
            reaction.decisions,
            (
                Decision(
                    instance_id=1,
                    verdict=Verdict.INDETERMINATE,
                    reasons=(ReasonCode.TIMESTAMP_DISCONTINUITY,),
                    violations=(),
                    lifecycle=Lifecycle.CLOSED_BY_END_SIGNAL,
                    evidence=EvidenceSpan.at(HostInstant(ANCHOR + 1.0)),
                ),
            ),
        )
        self.assertEqual(tuple(p.decision for p in station.pending_reports()), reaction.decisions)
        self.assertEqual(statements.count("BEGIN IMMEDIATE"), 1)
        self.assertEqual(statements.count("COMMIT"), 1)

    def _fault_database(self) -> tuple[sqlite3.Connection, LocalState]:
        """在真实 SQLite 事务的最后一个队列写入上制造约束失败。"""
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        migrate(connection)
        connection.execute(
            "CREATE TRIGGER fail_evidence BEFORE INSERT ON local_evidence_queue "
            "BEGIN SELECT RAISE(ABORT, 'evidence unavailable'); END"
        )
        state = LocalState(connection)
        self.addCleanup(state.close)
        return connection, state


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
        driver = supervisor(opening_state(), clock, station)

        for arriving in (action(STEPS[0], at=ANCHOR), action(STEPS[2], at=ANCHOR + 1.0)):
            driver.receive(arriving)
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
        driver.receive(StreamHealthObserved(event=lost_stream(at=ANCHOR + 2.0)))
        closing = driver.receive(action(STEPS[1], at=ANCHOR + 3.0))

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
            first_station = first.station(STATION)
            driver = supervisor(opening_state(), FakeClock(), first_station)
            driver.receive(action(STEPS[0], at=ANCHOR))
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
                    evidence=EvidenceSpan.at(HostInstant(ANCHOR)),
                ),
            )
            self.assertEqual(station.pending_reports()[-1].closed_at, ANCHOR)

            # 重复启动和中断不能再次结案或重复入队。
            pending = station.pending_reports(), station.pending_evidence()
            resumed = resume_station(
                station,
                template=opening_state().template,
                parameters=opening_state().parameters,
                margins=MARGINS,
                clock=clock,
            )
            resumed.interrupt()
            self.assertEqual((station.pending_reports(), station.pending_evidence()), pending)

            # The next pass is a new instance, not a continuation of the interrupted one.
            resumed.receive(action(STEPS[0], at=ANCHOR + 61.0))
            self.assertEqual(resumed.state.instance.instance_id if resumed.state.instance else 0, 2)
            second.close()

    def test_host_reboot_clock_reset_closes_at_last_comparable_instant(self) -> None:
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "local-state.sqlite3")
            first = open_local_state(path)
            driver = supervisor(opening_state(), FakeClock(), first.station(STATION))
            driver.receive(action(STEPS[0], at=ANCHOR))
            first.close()

            second = open_local_state(path)
            station = second.station(STATION)
            resumed = resume_station(
                station,
                template=opening_state().template,
                parameters=opening_state().parameters,
                margins=MARGINS,
                clock=FakeClock(now=1.0),
            )
            self.addCleanup(second.close)

            self.assertIsNone(resumed.state.instance)
            (pending,) = station.pending_reports()
            self.assertEqual(pending.opened_at, ANCHOR)
            self.assertEqual(pending.closed_at, ANCHOR)
            self.assertEqual(pending.decision.evidence, EvidenceSpan.at(HostInstant(ANCHOR)))


class HistoricalReportContextTest(unittest.TestCase):
    def context(self, *, revision: int, backend_id: str) -> ReportContext:
        bundle = ConfigurationBundle(
            host_id="host-a",
            config_revision=revision,
            generated_at=f"2026-09-{revision:02d}T00:00:00Z",
            stations=(),
        )
        return ReportContext(
            host_id="host-a",
            station_id=STATION,
            backends=(BackendReportContext(backend_id, (f"model-{revision}",)),),
            template_version_id="template-a",
            template_sha256="a" * 64,
            configuration_revision=revision,
            configuration_sha256=bundle.effective_sha256,
            configuration_json=json.dumps(
                configuration_to_wire(bundle), ensure_ascii=False, separators=(",", ":")
            ),
        )

    def test_report_context_is_frozen_across_reconfiguration_and_sqlite_restart(self) -> None:
        context_n = self.context(revision=7, backend_id="backend-old")
        context_n1 = self.context(revision=8, backend_id="backend-new")
        with TemporaryDirectory() as temporary:
            database = str(Path(temporary) / "state.sqlite")
            first = open_local_state(database)
            station = first.station(STATION, report_context=context_n)
            driver = supervisor(opening_state(), FakeClock(), station)
            provenance = context_n.backends[0]
            driver.receive(action(STEPS[0], at=ANCHOR), report_provenance=provenance)
            driver.receive(action(STEPS[2], at=ANCHOR + 1.0), report_provenance=provenance)
            (pending,) = station.pending_reports()
            self.assertEqual(pending.context, context_n)
            first.close()

            second = open_local_state(database)
            rebound = second.station(STATION, report_context=context_n1)
            (after_reconfigure,) = rebound.pending_reports()
            self.assertEqual(after_reconfigure.context, context_n)
            second.close()

            third = open_local_state(database)
            restarted = third.station(STATION, report_context=context_n1)
            (after_restart,) = restarted.pending_reports()
            self.assertEqual(after_restart.context, context_n)
            third.close()

    def test_open_instance_uses_existing_report_outbox_and_close_supersedes_unsent_open(
        self,
    ) -> None:
        context = self.context(revision=7, backend_id="backend-old")
        state = open_local_state(":memory:")
        self.addCleanup(state.close)
        station = state.station(STATION, report_context=context)
        driver = supervisor(opening_state(end_signals=("end-a", "end-b")), FakeClock(), station)
        provenance = context.backends[0]

        driver.receive(action(STEPS[0], at=ANCHOR), report_provenance=provenance)
        (opening,) = station.pending_instance_reports()
        self.assertEqual(opening.instance_id, 1)
        self.assertEqual(opening.open_boundary_signal, STEPS[0])
        self.assertEqual(opening.context, context)

        driver.receive(action("end-b", at=ANCHOR + 1.0), report_provenance=provenance)
        self.assertEqual(station.pending_instance_reports(), ())
        (closed,) = station.pending_reports()
        self.assertEqual(closed.open_boundary_signal, STEPS[0])
        self.assertEqual(closed.close_boundary_signal, "end-b")

    def test_pre_instance_impaired_backend_provenance_reaches_the_report_outbox(self) -> None:
        bundle = ConfigurationBundle(
            host_id="host-a",
            config_revision=9,
            generated_at="2026-09-16T00:00:00Z",
            stations=(),
        )
        backend_a = BackendReportContext("backend-a", ("model-a",))
        backend_b = BackendReportContext("backend-b", ("model-b",))
        context = ReportContext(
            host_id="host-a",
            station_id=STATION,
            backends=(backend_a, backend_b),
            template_version_id="template-a",
            template_sha256="a" * 64,
            configuration_revision=bundle.config_revision,
            configuration_sha256=bundle.effective_sha256,
            configuration_json=json.dumps(
                configuration_to_wire(bundle), ensure_ascii=False, separators=(",", ":")
            ),
        )
        state = open_local_state(":memory:")
        self.addCleanup(state.close)
        station = state.station(STATION, report_context=context)
        driver = supervisor(opening_state(), FakeClock(), station)

        driver.receive(
            ValidityChanged(
                reason=ReasonCode.INFERENCE_BACKEND_UNREACHABLE,
                now=Validity.IMPAIRED,
            ),
            report_provenance=backend_a,
        )
        self.assertIsNone(driver.state.instance)
        driver.receive(action(STEPS[0], at=ANCHOR), report_provenance=backend_b)
        driver.receive(action(STEPS[2], at=ANCHOR + 1.0), report_provenance=backend_b)

        (pending,) = station.pending_reports()
        assert pending.context is not None
        self.assertEqual(pending.context.backends, (backend_a, backend_b))
        self.assertEqual(pending.decision.verdict, Verdict.INDETERMINATE)
        self.assertIn(ReasonCode.INFERENCE_BACKEND_UNREACHABLE, pending.decision.reasons)

    def test_pre_instance_impaired_backend_provenance_reaches_one_step_report_outbox(self) -> None:
        bundle = ConfigurationBundle(
            host_id="host-a",
            config_revision=9,
            generated_at="2026-09-16T00:00:00Z",
            stations=(),
        )
        backend_a = BackendReportContext("backend-a", ("model-a",))
        backend_b = BackendReportContext("backend-b", ("model-b",))
        context = ReportContext(
            host_id="host-a",
            station_id=STATION,
            backends=(backend_a, backend_b),
            template_version_id="template-a",
            template_sha256="a" * 64,
            configuration_revision=bundle.config_revision,
            configuration_sha256=bundle.effective_sha256,
            configuration_json=json.dumps(
                configuration_to_wire(bundle), ensure_ascii=False, separators=(",", ":")
            ),
        )
        state = open_local_state(":memory:")
        self.addCleanup(state.close)
        station = state.station(STATION, report_context=context)
        driver = supervisor(opening_state(steps=(STEPS[0],)), FakeClock(), station)

        driver.receive(
            ValidityChanged(
                reason=ReasonCode.INFERENCE_BACKEND_UNREACHABLE,
                now=Validity.IMPAIRED,
            ),
            report_provenance=backend_a,
        )
        driver.receive(action(STEPS[0], at=ANCHOR), report_provenance=backend_b)

        (pending,) = station.pending_reports()
        assert pending.context is not None
        self.assertEqual(pending.context.backends, (backend_a, backend_b))
        self.assertEqual(pending.decision.verdict, Verdict.INDETERMINATE)
        self.assertIn(ReasonCode.INFERENCE_BACKEND_UNREACHABLE, pending.decision.reasons)

    def test_recovered_backend_is_not_seeded_into_a_later_instance(self) -> None:
        bundle = ConfigurationBundle(
            host_id="host-a",
            config_revision=9,
            generated_at="2026-09-16T00:00:00Z",
            stations=(),
        )
        backend_a = BackendReportContext("backend-a", ("model-a",))
        backend_b = BackendReportContext("backend-b", ("model-b",))
        backend_c = BackendReportContext("backend-c", ("model-c",))
        context = ReportContext(
            host_id="host-a",
            station_id=STATION,
            backends=(backend_a, backend_b, backend_c),
            template_version_id="template-a",
            template_sha256="a" * 64,
            configuration_revision=bundle.config_revision,
            configuration_sha256=bundle.effective_sha256,
            configuration_json=json.dumps(
                configuration_to_wire(bundle), ensure_ascii=False, separators=(",", ":")
            ),
        )
        state = open_local_state(":memory:")
        self.addCleanup(state.close)
        station = state.station(STATION, report_context=context)
        driver = supervisor(opening_state(), FakeClock(), station)

        driver.receive(
            ValidityChanged(
                reason=ReasonCode.INFERENCE_BACKEND_UNREACHABLE,
                now=Validity.IMPAIRED,
            ),
            report_provenance=backend_a,
        )
        driver.receive(
            ValidityChanged(
                reason=ReasonCode.INFERENCE_BACKEND_UNREACHABLE,
                now=Validity.IMPAIRED,
            ),
            report_provenance=backend_b,
        )
        driver.receive(
            ValidityChanged(
                reason=ReasonCode.INFERENCE_BACKEND_UNREACHABLE,
                now=Validity.RESTORED,
            ),
            report_provenance=backend_a,
        )
        driver.receive(action(STEPS[0], at=ANCHOR), report_provenance=backend_c)
        driver.receive(action(STEPS[2], at=ANCHOR + 1.0), report_provenance=backend_c)

        (pending,) = station.pending_reports()
        assert pending.context is not None
        self.assertEqual(pending.context.backends, (backend_b, backend_c))
        self.assertEqual(pending.decision.verdict, Verdict.INDETERMINATE)
        self.assertIn(ReasonCode.INFERENCE_BACKEND_UNREACHABLE, pending.decision.reasons)

    def test_second_impaired_backend_is_recorded_for_an_open_instance(self) -> None:
        bundle = ConfigurationBundle(
            host_id="host-a",
            config_revision=9,
            generated_at="2026-09-16T00:00:00Z",
            stations=(),
        )
        backend_a = BackendReportContext("backend-a", ("model-a",))
        backend_b = BackendReportContext("backend-b", ("model-b",))
        context = ReportContext(
            host_id="host-a",
            station_id=STATION,
            backends=(backend_a, backend_b),
            template_version_id="template-a",
            template_sha256="a" * 64,
            configuration_revision=bundle.config_revision,
            configuration_sha256=bundle.effective_sha256,
            configuration_json=json.dumps(
                configuration_to_wire(bundle), ensure_ascii=False, separators=(",", ":")
            ),
        )
        state = open_local_state(":memory:")
        self.addCleanup(state.close)
        station = state.station(STATION, report_context=context)
        driver = supervisor(opening_state(), FakeClock(), station)

        driver.receive(action(STEPS[0], at=ANCHOR), report_provenance=backend_a)
        driver.receive(
            ValidityChanged(
                reason=ReasonCode.INFERENCE_BACKEND_UNREACHABLE,
                now=Validity.IMPAIRED,
            ),
            report_provenance=backend_a,
        )
        driver.receive(
            ValidityChanged(
                reason=ReasonCode.INFERENCE_BACKEND_UNREACHABLE,
                now=Validity.IMPAIRED,
            ),
            report_provenance=backend_b,
        )
        driver.receive(action(STEPS[2], at=ANCHOR + 1.0), report_provenance=backend_a)

        (pending,) = station.pending_reports()
        assert pending.context is not None
        self.assertEqual(pending.context.backends, (backend_a, backend_b))
        self.assertEqual(pending.decision.verdict, Verdict.INDETERMINATE)
        self.assertIn(ReasonCode.INFERENCE_BACKEND_UNREACHABLE, pending.decision.reasons)

    def test_non_first_backend_provenance_reaches_the_report_outbox(self) -> None:
        bundle = ConfigurationBundle(
            host_id="host-a",
            config_revision=9,
            generated_at="2026-09-16T00:00:00Z",
            stations=(),
        )
        backend_a = BackendReportContext("backend-a", ("model-a",))
        backend_b = BackendReportContext("backend-b", ("model-b",))
        context = ReportContext(
            host_id="host-a",
            station_id=STATION,
            backends=(backend_a, backend_b),
            template_version_id="template-a",
            template_sha256="a" * 64,
            configuration_revision=bundle.config_revision,
            configuration_sha256=bundle.effective_sha256,
            configuration_json=json.dumps(
                configuration_to_wire(bundle), ensure_ascii=False, separators=(",", ":")
            ),
        )
        state = open_local_state(":memory:")
        self.addCleanup(state.close)
        station = state.station(STATION, report_context=context)
        driver = supervisor(opening_state(), FakeClock(), station)
        driver.receive(action(STEPS[0], at=ANCHOR), report_provenance=backend_b)
        driver.receive(action(STEPS[2], at=ANCHOR + 1.0), report_provenance=backend_b)
        (pending,) = station.pending_reports()
        assert pending.context is not None
        self.assertEqual(pending.context.backends, (backend_b,))

        class Transport:
            def __init__(self) -> None:
                self.sent: list[ReportedDecision] = []

            def send_decision(
                self,
                report: ReportedDecision,
                *,
                configuration: ConfigurationBundle | None,
            ) -> None:
                self.assert_configuration(configuration)
                self.sent.append(report)

            @staticmethod
            def assert_configuration(configuration: ConfigurationBundle | None) -> None:
                if configuration is None:
                    raise AssertionError("v2 report must carry the frozen configuration")

            def send_instance(
                self, report: object, *, configuration: ConfigurationBundle | None
            ) -> None:
                del report, configuration

        transport = Transport()
        attempts = HostReportReconciler(reports=state.reports(), transport=transport).flush(
            now=HostInstant(ANCHOR + 2.0),
            reported_at="2026-09-16T00:00:00Z",
        )
        self.assertTrue(attempts[0].sent)
        self.assertEqual(
            transport.sent[0].backend_provenance,
            (ReportBackendProvenance("backend-b", ("model-b",)),),
        )

    def test_confirmed_connector_only_decision_reports_empty_backend_provenance(self) -> None:
        context = self.context(revision=10, backend_id="backend-configured")
        state = open_local_state(":memory:")
        self.addCleanup(state.close)
        station = state.station(STATION, report_context=context)
        driver = supervisor(opening_state(), FakeClock(), station)
        driver.receive(action(STEPS[0], at=ANCHOR))
        driver.receive(action(STEPS[2], at=ANCHOR + 1.0))
        (pending,) = station.pending_reports()
        assert pending.context is not None
        self.assertEqual(pending.context.backends, ())

        class Transport:
            def __init__(self) -> None:
                self.sent: list[ReportedDecision] = []

            def send_decision(
                self,
                report: ReportedDecision,
                *,
                configuration: ConfigurationBundle | None,
            ) -> None:
                if configuration is None:
                    raise AssertionError("confirmed decision must carry its frozen configuration")
                self.sent.append(report)

            def send_instance(
                self, report: object, *, configuration: ConfigurationBundle | None
            ) -> None:
                del report, configuration

        transport = Transport()
        attempts = HostReportReconciler(reports=state.reports(), transport=transport).flush(
            now=HostInstant(ANCHOR + 2.0),
            reported_at="2026-09-16T00:00:00Z",
        )
        self.assertTrue(attempts[0].sent)
        self.assertEqual(transport.sent[0].backend_provenance, ())

    def test_event_time_context_without_history_is_not_confused_with_legacy_pending(self) -> None:
        unproven = ReportContext(
            host_id="host-a",
            station_id=STATION,
            backends=(BackendReportContext("backend-bootstrap", ()),),
            template_version_id="template-bootstrap",
            template_sha256="e" * 64,
            configuration_revision=None,
            configuration_sha256=None,
            configuration_json=None,
        )
        with TemporaryDirectory() as temporary:
            database = str(Path(temporary) / "bootstrap.sqlite")
            first = open_local_state(database)
            station = first.station(STATION, report_context=unproven)
            driver = supervisor(opening_state(), FakeClock(), station)
            provenance = unproven.backends[0]
            driver.receive(action(STEPS[0], at=ANCHOR), report_provenance=provenance)
            driver.receive(action(STEPS[2], at=ANCHOR + 1.0), report_provenance=provenance)
            (pending,) = station.pending_reports()
            self.assertEqual(pending.context, unproven)
            first.close()

            second = open_local_state(database)
            (reopened,) = second.station(STATION).pending_reports()
            self.assertEqual(reopened.context, unproven)
            second.close()

    def test_legacy_pending_is_retained_and_never_sent_with_current_context(self) -> None:
        state = open_local_state(":memory:")
        self.addCleanup(state.close)
        station = state.station(STATION)
        driver = supervisor(opening_state(), FakeClock(), station)
        driver.receive(action(STEPS[0], at=ANCHOR))
        driver.receive(action(STEPS[2], at=ANCHOR + 1.0))

        class Transport:
            def __init__(self) -> None:
                self.sent: list[object] = []

            def send_decision(
                self,
                report: ReportedDecision,
                *,
                configuration: ConfigurationBundle | None,
            ) -> None:
                del configuration
                self.sent.append(report)

            def send_instance(
                self, report: object, *, configuration: ConfigurationBundle | None
            ) -> None:
                del report, configuration

        transport = Transport()
        attempts = HostReportReconciler(reports=state.reports(), transport=transport).flush(
            now=HostInstant(ANCHOR + 2.0),
            reported_at="2026-09-16T00:00:00Z",
        )
        self.assertEqual(transport.sent, [])
        self.assertEqual(len(attempts), 1)
        self.assertFalse(attempts[0].sent)
        self.assertIn("event-time report context", attempts[0].error or "")
        (still_pending,) = station.pending_reports()
        self.assertEqual(still_pending.attempts, 1)
        self.assertIsNone(still_pending.context)

    def test_v4_pending_reopens_as_unproven_legacy_after_v6_migration(self) -> None:
        context = self.context(revision=8, backend_id="backend-new")
        with TemporaryDirectory() as temporary:
            database = str(Path(temporary) / "legacy.sqlite")
            connection = sqlite3.connect(database)
            apply_migrations(connection, MIGRATIONS[:4])
            connection.execute(
                """
                INSERT INTO local_sop_instance (
                    station_id, instance_id, opened_at, last_observation_at, seen,
                    expected_index, impairments, settled, closed_at, lifecycle
                ) VALUES (?, 1, 1.0, 2.0, '[]', 0, '[]', '[]', 2.0, ?)
                """,
                (STATION, Lifecycle.CLOSED_BY_END_SIGNAL.value),
            )
            connection.execute(
                """
                INSERT INTO local_decision (
                    decision_id, station_id, instance_id, verdict, reasons, lifecycle,
                    evidence_anchor, evidence_from, evidence_to
                ) VALUES (1, ?, 1, ?, '[]', ?, 2.0, 2.0, 2.0)
                """,
                (STATION, Verdict.PASS.value, Lifecycle.CLOSED_BY_END_SIGNAL.value),
            )
            connection.execute(
                "INSERT INTO local_report_queue (station_id, decision_id) VALUES (?, 1)",
                (STATION,),
            )
            connection.commit()
            connection.close()

            state = open_local_state(database)
            (pending,) = state.station(STATION, report_context=context).pending_reports()
            self.assertIsNone(pending.context)
            self.assertEqual(pending.attempts, 0)
            state.close()

            reopened = sqlite3.connect(database)
            try:
                self.assertEqual(
                    reopened.execute("PRAGMA user_version").fetchone()[0], len(MIGRATIONS)
                )
            finally:
                reopened.close()

    def test_lost_ack_retry_reuses_the_exact_report_payload(self) -> None:
        context = self.context(revision=7, backend_id="backend-old")
        state = open_local_state(":memory:")
        self.addCleanup(state.close)
        station = state.station(STATION, report_context=context)
        driver = supervisor(opening_state(), FakeClock(), station)
        provenance = context.backends[0]
        driver.receive(action(STEPS[0], at=ANCHOR), report_provenance=provenance)
        driver.receive(action(STEPS[2], at=ANCHOR + 1.0), report_provenance=provenance)

        class LostAckTransport:
            def __init__(self) -> None:
                self.sent: list[object] = []

            def send_decision(
                self,
                report: ReportedDecision,
                *,
                configuration: ConfigurationBundle | None,
            ) -> None:
                self.sent.append((report, configuration))
                if len(self.sent) == 1:
                    raise OSError("center committed but acknowledgement was lost")

            def send_instance(
                self, report: object, *, configuration: ConfigurationBundle | None
            ) -> None:
                del report, configuration

        transport = LostAckTransport()
        first = HostReportReconciler(reports=state.reports(), transport=transport).flush(
            now=HostInstant(ANCHOR + 2.0),
            reported_at="2026-09-16T00:00:00Z",
        )
        self.assertEqual(len(first), 2)
        self.assertTrue(first[0].sent)
        self.assertFalse(first[1].sent)
        self.assertEqual(station.pending_instance_reports(), ())
        (pending_after_loss,) = station.pending_reports()
        self.assertEqual(pending_after_loss.reported_at, "2026-09-16T00:00:00Z")

        second = HostReportReconciler(reports=state.reports(), transport=transport).flush(
            now=HostInstant(ANCHOR + 3.0),
            reported_at="2026-09-16T00:05:00Z",
        )
        self.assertTrue(second[0].sent)
        self.assertEqual(transport.sent[0], transport.sent[1])
        self.assertEqual(station.pending_reports(), ())

    def test_host_reconciler_continues_after_one_station_report_fails(self) -> None:
        context = self.context(revision=7, backend_id="backend-old")
        other_context = replace(context, station_id=OTHER_STATION)
        state = open_local_state(":memory:")
        self.addCleanup(state.close)
        stations = (
            state.station(STATION, report_context=context),
            state.station(OTHER_STATION, report_context=other_context),
        )
        for station, report_context in zip(stations, (context, other_context), strict=True):
            driver = supervisor(opening_state(), FakeClock(), station)
            provenance = report_context.backends[0]
            driver.receive(action(STEPS[0], at=ANCHOR), report_provenance=provenance)
            driver.receive(action(STEPS[2], at=ANCHOR + 1.0), report_provenance=provenance)

        class Transport:
            def __init__(self) -> None:
                self.attempted_decisions: list[str] = []

            def send_decision(
                self,
                report: ReportedDecision,
                *,
                configuration: ConfigurationBundle | None,
            ) -> None:
                del configuration
                self.attempted_decisions.append(report.station_id)
                if report.station_id == STATION:
                    raise OSError("station report unavailable")

            def send_instance(
                self, report: object, *, configuration: ConfigurationBundle | None
            ) -> None:
                del report, configuration

        transport = Transport()
        attempts = HostReportReconciler(reports=state.reports(), transport=transport).flush(
            now=HostInstant(ANCHOR + 2.0),
            reported_at="2026-09-16T00:00:00Z",
        )

        self.assertEqual(transport.attempted_decisions, [STATION, OTHER_STATION])
        self.assertTrue(any(not attempt.sent for attempt in attempts))
        self.assertEqual(len(stations[0].pending_reports()), 1)
        self.assertEqual(stations[1].pending_reports(), ())

    def test_successful_flush_marks_only_the_outbox_row_reported(self) -> None:
        context = self.context(revision=7, backend_id="backend-old")
        state = open_local_state(":memory:")
        self.addCleanup(state.close)
        station = state.station(STATION, report_context=context)
        driver = supervisor(opening_state(), FakeClock(), station)
        provenance = context.backends[0]
        driver.receive(action(STEPS[0], at=ANCHOR), report_provenance=provenance)
        driver.receive(action(STEPS[2], at=ANCHOR + 1.0), report_provenance=provenance)
        (pending,) = station.pending_reports()

        class Transport:
            def __init__(self) -> None:
                self.sent: list[object] = []

            def send_decision(
                self,
                report: ReportedDecision,
                *,
                configuration: ConfigurationBundle | None,
            ) -> None:
                self.sent.append((report, configuration))

            def send_instance(
                self, report: object, *, configuration: ConfigurationBundle | None
            ) -> None:
                del report, configuration

        transport = Transport()
        attempts = HostReportReconciler(reports=state.reports(), transport=transport).flush(
            now=HostInstant(ANCHOR + 2.0),
            reported_at="2026-09-16T00:00:00Z",
        )
        self.assertEqual(len(transport.sent), 1)
        self.assertTrue(attempts[0].sent)
        self.assertEqual(station.pending_reports(), ())
        self.assertEqual(
            station.latched_violations(instance_id=pending.decision.instance_id),
            pending.decision.violations,
        )


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
        driver = supervisor(opening_state(), FakeClock(), self.station)
        driver.receive(action(STEPS[0], at=ANCHOR))
        driver.receive(action(STEPS[2], at=ANCHOR + 1.0))

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
        driver = supervisor(opening_state(), clock, station)
        driver.receive(action(STEPS[0], at=ANCHOR))

        clock.now = ANCHOR + STEP_DEADLINE + 1.0
        driver.wake(host=HostLiveness.ALIVE)

        pending = station.pending_reports(), station.pending_evidence()
        driver.wake(host=HostLiveness.ALIVE)
        self.assertEqual((station.pending_reports(), station.pending_evidence()), pending)
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

    def test_digest_survives_the_legacy_table_rebuild(self) -> None:
        connection = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(connection.close)
        self.assertEqual(apply_migrations(connection, MIGRATIONS[:3]), 3)
        digest = "a" * 64
        connection.execute(
            """
            INSERT INTO local_config
                (slot, host_id, config_revision, sha256, confirmed_at, payload)
            VALUES (1, 'host-a', 4, ?, 1.0, '{}')
            """,
            (digest,),
        )

        self.assertEqual(apply_migrations(connection, MIGRATIONS), len(MIGRATIONS))
        self.assertEqual(
            connection.execute("SELECT sha256 FROM local_config WHERE slot = 1").fetchone()[0],
            digest,
        )

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
