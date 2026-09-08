"""supervisor 的本地状态持久化接缝: 一次反应对应一个事务。

状态、效果和队列在此一起落盘, 即使中心不可达或进程重启也能恢复 (§5.7)。该数据库是判定和已锁定
违规的权威, 中心只保存幂等镜像。持久化接口只接收 judgment 的领域状态、效果和闭合实例快照, 不依赖
编排这些数据的 supervisor。

一次反应的状态和效果必须全部写入或全部回滚。闭合实例的父行先于判定写入, 使 SQLite 外键在实例于同一
反应内打开并闭合时仍然成立。每次反应必须在下一次输入前提交一次; 同一反应重复提交会产生重复判定和
报告, 已提交事务不能盲目重试。模块不读取时钟, 所有时刻都由调用方传入。标准库实现, 见
edge-autonomy.md §5.11。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import AbstractContextManager
from threading import RLock
from typing import assert_never

from edge_runtime.judgment.effects import (
    ClipEvidence,
    CloseInstance,
    Command,
    LatchViolation,
    RecordDecision,
)
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


class StationStore(StationQueues):
    """推理机数据库中的一个工位作用域。

    工位是实例、判定和违例所属的单元, 一台推理机可运行多个工位 (CONTEXT.md)。所有语句都带工位 ID,
    不会读取其他工位的数据。它同时实现 `StationQueues`, 使判定和对应报告在同一事务中写入。
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        station_id: str,
        lock: AbstractContextManager[object],
    ) -> None:
        super().__init__(connection, station_id)
        self._lock = lock

    def commit(
        self,
        *,
        state: JudgmentState,
        commands: Sequence[Command],
        closed_instances: Sequence[Instance] = (),
    ) -> None:
        """持久化一次反应, 并串行化共享 SQLite 连接上的工位线程。"""
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                if state.instance is not None:
                    self._write_instance(state.instance)
                for instance in closed_instances:
                    self._write_instance(instance)
                self._perform(commands)
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    def _perform(self, commands: Sequence[Command]) -> None:
        """按 supervisor 发出的顺序执行命令; 违例、证据和关闭命令必须跟随对应判定。"""
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
        """用核心当前持有的完整实例快照替换旧记录, 避免数据库形成第二套不一致模型。"""
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
        """只插入实例违例; 重复报告同一事实时保留首次确认时刻 (§5.2)。"""
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
        """每个实例锚点一条证据; 第二次判定需要更大窗口时只扩大、不缩小 (§5.20)。"""
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
        """按指定生命周期关闭实例; 同一反应内开闭的实例以关闭判定锚点作为开始时刻。"""
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
        """按确认顺序返回实例已锁定的全部违例, 与实例最终生命周期无关 (§5.2)。"""
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
        """恢复工位启动时的核心状态。

        模板和参数由调用方传入, 数据库只恢复未结束实例和实例编号。运行期的流健康与后端可达性不跨进程
        恢复, 新的 supervisor 会从健康通道重新学习 (§5.11)。实例编号由历史记录推导。
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
        """返回本工位历史最大实例编号加一; 该推导依赖每次反应都及时提交。"""
        highest = self._connection.execute(
            "SELECT max(instance_id) FROM local_sop_instance WHERE station_id = ?",
            (self._station_id,),
        ).fetchone()[0]
        return 1 if highest is None else int(highest) + 1


class LocalState:
    """推理机的本地数据库; 多个工位共享一个 SQLite 文件。"""

    def __init__(
        self,
        connection: sqlite3.Connection,
        lock: AbstractContextManager[object] | None = None,
    ) -> None:
        self._connection = connection
        self._lock = lock or RLock()

    def station(self, station_id: str) -> StationStore:
        """返回一个工位的行作用域; 所有工位共享连接和写入锁。"""
        return StationStore(self._connection, station_id, self._lock)

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def open_local_state(path: str) -> LocalState:
    """打开或创建推理机本地状态, 并迁移到当前 SQLite 模式。

    连接使用自动提交, `commit` 显式声明反应事务; 启用外键以保证判定记录不会脱离实例。
    """
    # 工位循环在独立线程中运行; LocalState 的锁串行化共享连接上的写入。
    connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    # 优先保证持久性: 这里是已锁定违例和待上报判定的唯一副本 (§5.7)。
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    migrate(connection)
    return LocalState(connection)
