"""驱动一个工位的核心, 持有计时器, 并在返回前原子提交一次反应。

一次输入可产生多个核心事件, 但状态、判定、锁存及两个队列的效果只提交一次。
计时器只持有到点时刻, 不注册回调; 过早或重复唤醒不会重复判定。
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from time import monotonic

from edge_runtime.judgment.core import advance
from edge_runtime.judgment.evidence import EvidenceMargins
from edge_runtime.judgment.model import (
    Decision,
    Event,
    HostInstant,
    HostLiveness,
    Instance,
    JudgmentState,
    RunInterrupted,
    TimerFired,
)
from edge_runtime.local_state.queues import BackendReportContext
from edge_runtime.local_state.store import ReactionStore
from edge_runtime.supervisor.evidence import clips_for
from edge_runtime.supervisor.inputs import Normalizer, SupervisorInput


@dataclass(frozen=True, slots=True)
class Reaction:
    """已提交的判定和下一次唤醒时刻; 调用方不得再次提交。"""

    decisions: tuple[Decision, ...]
    wake_at: HostInstant | None
    closed_instances: tuple[Instance, ...] = ()
    """本次反应已提交的闭合实例完整快照, 不是待执行的持久化任务。"""


class StationSupervisor:
    """The judgment core plus the state and the clock it deliberately does not hold.

    One per station, because a station is the unit an SOP instance belongs to: judgment and
    violations hang on the station, not on a camera or a person (CONTEXT.md).

    The clock is read at exactly three points, all of them places where no instant arrived
    with the input: computing how long the loop should wait, stamping a timer firing, and
    stamping a run interruption. An arriving observation keeps its own instant — restamping
    it here would measure the supervisor's own lag as the operator's pace.
    """

    def __init__(
        self,
        *,
        state: JudgmentState,
        store: ReactionStore,
        margins: EvidenceMargins,
        clock: Callable[[], float] = monotonic,
        initial_report_provenance: tuple[BackendReportContext, ...] | None = (),
    ) -> None:
        self._state = state
        self._store = store
        self._margins = margins
        self._clock = clock
        self._normalizer = Normalizer()
        self._deadline: HostInstant | None = None
        self._report_provenance: dict[int, dict[str, BackendReportContext] | None] = {}
        if state.instance is not None:
            self._report_provenance[state.instance.instance_id] = (
                None
                if initial_report_provenance is None
                else {item.backend_id: item for item in initial_report_provenance}
            )

    @property
    def state(self) -> JudgmentState:
        """最近一次成功提交的核心状态。"""
        return self._state

    @property
    def wake_at(self) -> HostInstant | None:
        """The instant the core asked to be woken at, or None when nothing is in flight."""
        return self._deadline

    def timeout(self) -> float | None:
        """How long the run loop may wait, or None when it may wait indefinitely.

        Never negative: a loop would read that as an unbounded poll, and a deadline already
        passed means the wake-up is owed now.
        """
        if self._deadline is None:
            return None
        return max(0.0, self._deadline.seconds - self._clock())

    def receive(
        self,
        arriving: SupervisorInput,
        *,
        report_provenance: BackendReportContext | None = None,
    ) -> Reaction:
        """One thing that arrived from outside; reporting provenance stays outside judgment core."""
        normalizer = deepcopy(self._normalizer)
        reaction = self._advance(
            normalizer.events_for(arriving), report_provenance=report_provenance
        )
        self._normalizer = normalizer
        return reaction

    def wake(self, *, host: HostLiveness) -> Reaction:
        """The timer the core asked for, if it is in fact due.

        `host` is passed in rather than probed here because the run loop is what holds that
        knowledge — it is watching the inference request, so it is the thing that finds out
        the process stopped answering. The stream's state comes from the health events this
        supervisor already consumed, which is the first-hand record (§2.4).

        Not due yet, or nothing in flight: nothing is decided and the deadline stands.
        """
        if self._deadline is None:
            return Reaction(decisions=(), wake_at=None)
        now = self._clock()
        if now < self._deadline.seconds:
            return Reaction(decisions=(), wake_at=self._deadline)
        return self._advance(
            (
                TimerFired(
                    at=HostInstant(now),
                    host=host,
                    stream=self._normalizer.stream_health,
                ),
            )
        )

    def interrupt(self) -> Reaction:
        """A configuration switch or a shutdown ended this run.

        The pass in flight is concluded as indeterminate rather than carried across, so one
        maintenance action does not manufacture a violation (§5.2).
        """
        return self._advance((RunInterrupted(at=HostInstant(self._clock())),))

    def _advance(
        self,
        events: tuple[Event, ...],
        *,
        report_provenance: BackendReportContext | None = None,
    ) -> Reaction:
        """先收集全部事件的结果, 一次提交成功后才发布新状态、provenance 和计时器。"""
        state, deadline = self._state, self._deadline
        decisions: list[Decision] = []
        closed_instances: list[Instance] = []
        touched_ids: set[int] = set()
        if state.instance is not None:
            touched_ids.add(state.instance.instance_id)
        for event in events:
            before = state.instance
            outcome = advance(state, event)
            state, deadline = outcome.state, outcome.wake_at
            if before is not None:
                touched_ids.add(before.instance_id)
            if state.instance is not None:
                touched_ids.add(state.instance.instance_id)
            touched_ids.update(instance.instance_id for instance in outcome.closed_instances)
            touched_ids.update(decision.instance_id for decision in outcome.decisions)
            closed_instances.extend(outcome.closed_instances)
            decisions.extend(outcome.decisions)
        if events:
            for instance_id in touched_ids:
                existing = self._report_provenance.setdefault(instance_id, {})
                if existing is not None and report_provenance is not None:
                    existing[report_provenance.backend_id] = report_provenance
        committed_provenance: dict[int, tuple[BackendReportContext, ...] | None] = {}
        for instance_id in touched_ids:
            values = self._report_provenance.get(instance_id)
            committed_provenance[instance_id] = (
                None if values is None else tuple(values[key] for key in sorted(values))
            )
        self._store.commit(
            state=state,
            decisions=tuple(decisions),
            evidence=tuple(
                clip
                for decision in decisions
                for clip in clips_for(decision, margins=self._margins)
            ),
            closed_instances=tuple(closed_instances),
            report_provenance=committed_provenance,
        )
        for instance in closed_instances:
            self._report_provenance.pop(instance.instance_id, None)
        self._state, self._deadline = state, deadline
        return Reaction(
            decisions=tuple(decisions),
            wake_at=deadline,
            closed_instances=tuple(closed_instances),
        )
