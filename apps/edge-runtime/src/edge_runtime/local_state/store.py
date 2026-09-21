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

import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from threading import RLock
from typing import Protocol

from edge_runtime.judgment.evidence import EvidenceClip
from edge_runtime.judgment.model import (
    Decision,
    HostInstant,
    Instance,
    JudgmentState,
    Lifecycle,
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
from edge_runtime.local_state.configuration import LocalConfigurationStore
from edge_runtime.local_state.disposal import LocalDisposalLedger
from edge_runtime.local_state.queues import BackendReportContext, ReportContext, StationQueues
from edge_runtime.local_state.schema import migrate


class ReactionStore(Protocol):
    """supervisor 提交一次完整反应的接缝; 实现必须全部提交或全部回滚。"""

    def commit(
        self,
        *,
        state: JudgmentState,
        decisions: Sequence[Decision],
        evidence: Sequence[EvidenceClip],
        closed_instances: Sequence[Instance],
        report_provenance: Mapping[int, tuple[BackendReportContext, ...] | None],
    ) -> None: ...


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
        report_context: ReportContext | None = None,
    ) -> None:
        super().__init__(connection, station_id, lock)
        self._lock = lock
        self._report_context = report_context

    def commit(
        self,
        *,
        state: JudgmentState,
        decisions: Sequence[Decision],
        evidence: Sequence[EvidenceClip],
        closed_instances: Sequence[Instance],
        report_provenance: Mapping[int, tuple[BackendReportContext, ...] | None],
    ) -> None:
        """持久化一次反应, 并串行化共享 SQLite 连接上的工位线程。"""
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                if state.instance is not None:
                    provenance = report_provenance.get(state.instance.instance_id)
                    if self._write_instance(state.instance, provenance):
                        self._enqueue_instance_open_report(state.instance.instance_id, provenance)
                for instance in closed_instances:
                    provenance = report_provenance.get(instance.instance_id)
                    if self._write_instance(instance, provenance):
                        self._enqueue_instance_open_report(instance.instance_id, provenance)
                for decision in decisions:
                    decision_id = self._write_decision(decision)
                    self._enqueue_report(
                        decision_id,
                        report_provenance.get(decision.instance_id),
                    )
                    for violation in decision.violations:
                        self._latch(decision.instance_id, decision_id, violation)
                    if decision.lifecycle is not Lifecycle.STAYS_OPEN:
                        self._close_instance(decision)
                        self._supersede_instance_open_report(
                            decision.instance_id, at=decision.evidence.anchor
                        )
                for clip in evidence:
                    self._enqueue_evidence(clip)
                self._connection.execute("COMMIT")
            except Exception:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def _write_instance(
        self,
        instance: Instance,
        provenance: tuple[BackendReportContext, ...] | None,
    ) -> bool:
        """保存核心实例和独立 reporting provenance; 返回是否为首次落盘。"""
        created = (
            self._connection.execute(
                """
                SELECT 1
                  FROM local_sop_instance
                 WHERE station_id = ? AND instance_id = ?
                """,
                (self._station_id, instance.instance_id),
            ).fetchone()
            is None
        )
        encoded_provenance = self._encode_provenance(provenance)
        self._connection.execute(
            """
            INSERT INTO local_sop_instance (
                station_id, instance_id, opened_at, last_observation_at,
                seen, expected_index, impairments, settled, report_backend_provenance
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (station_id, instance_id) DO UPDATE SET
                last_observation_at        = excluded.last_observation_at,
                seen                       = excluded.seen,
                expected_index             = excluded.expected_index,
                impairments                = excluded.impairments,
                settled                    = excluded.settled,
                report_backend_provenance  = excluded.report_backend_provenance
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
                encoded_provenance,
            ),
        )
        return created

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

    def _enqueue_report(
        self,
        decision_id: int,
        provenance: tuple[BackendReportContext, ...] | None,
    ) -> None:
        if self._report_context is None:
            self._connection.execute(
                """
                INSERT INTO local_report_queue (station_id, decision_id, report_kind)
                VALUES (?, ?, 'decision')
                """,
                (self._station_id, decision_id),
            )
            return
        context = self._report_context
        if provenance is not None:
            configured = {backend.backend_id: backend for backend in context.backends}
            for backend in provenance:
                if configured.get(backend.backend_id) != backend:
                    raise ValueError(
                        "report provenance is outside the event-time station configuration"
                    )
        self._connection.execute(
            """
            INSERT INTO local_report_queue (
                station_id, decision_id, report_kind, report_host_id,
                report_template_version_id, report_template_sha256,
                report_backend_provenance, report_configuration,
                configuration_revision, configuration_sha256
            ) VALUES (?, ?, 'decision', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._station_id,
                decision_id,
                context.host_id,
                context.template_version_id,
                context.template_sha256,
                self._encode_provenance(provenance),
                context.configuration_json,
                context.configuration_revision,
                context.configuration_sha256,
            ),
        )

    def _enqueue_instance_open_report(
        self,
        instance_id: int,
        provenance: tuple[BackendReportContext, ...] | None,
    ) -> None:
        context = self._report_context
        if context is None or context.configuration_revision is None or provenance is None:
            return
        configured = {backend.backend_id: backend for backend in context.backends}
        for backend in provenance:
            if configured.get(backend.backend_id) != backend:
                raise ValueError(
                    "instance provenance is outside the event-time station configuration"
                )
        self._connection.execute(
            """
            INSERT INTO local_report_queue (
                station_id, instance_id, report_kind, report_host_id,
                report_template_version_id, report_template_sha256,
                report_backend_provenance, report_configuration,
                configuration_revision, configuration_sha256
            ) VALUES (?, ?, 'instance', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._station_id,
                instance_id,
                context.host_id,
                context.template_version_id,
                context.template_sha256,
                self._encode_provenance(provenance),
                context.configuration_json,
                context.configuration_revision,
                context.configuration_sha256,
            ),
        )

    def _supersede_instance_open_report(self, instance_id: int, *, at: HostInstant) -> None:
        """未送达的开放快照由同实例的闭合快照取代;已确认开放快照保留。"""
        self._connection.execute(
            """
            UPDATE local_report_queue
               SET superseded_at = ?
             WHERE station_id = ?
               AND report_kind = 'instance'
               AND instance_id = ?
               AND sent_at IS NULL
               AND superseded_at IS NULL
            """,
            (at.seconds, self._station_id, instance_id),
        )

    @staticmethod
    def _encode_provenance(provenance: tuple[BackendReportContext, ...] | None) -> str | None:
        if provenance is None:
            return None
        return json.dumps(
            [
                {"backend_id": backend.backend_id, "model_ids": list(backend.model_ids)}
                for backend in provenance
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _enqueue_evidence(self, clip: EvidenceClip) -> None:
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

    def _close_instance(self, decision: Decision) -> None:
        """按判定的生命周期关闭实例, 不要求调用方额外发送关闭命令。"""
        at = decision.evidence.anchor
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
                decision.instance_id,
                at.seconds,
                at.seconds,
                at.seconds,
                decision.lifecycle.value,
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

    def resume_report_provenance(self) -> tuple[BackendReportContext, ...] | None:
        """恢复未结实例的 backend provenance; NULL 表示实例来自旧 schema, 来源不可证明。"""
        row = self._connection.execute(
            """
            SELECT report_backend_provenance
              FROM local_sop_instance
             WHERE station_id = ? AND closed_at IS NULL
             ORDER BY instance_id DESC
             LIMIT 1
            """,
            (self._station_id,),
        ).fetchone()
        if row is None or row["report_backend_provenance"] is None:
            return None
        decoded = json.loads(row["report_backend_provenance"])
        if not isinstance(decoded, list):
            raise ValueError("stored backend report provenance is invalid")
        values: list[BackendReportContext] = []
        for item in decoded:
            if not isinstance(item, dict) or set(item) != {"backend_id", "model_ids"}:
                raise ValueError("stored backend report provenance is invalid")
            backend_id = item["backend_id"]
            model_ids = item["model_ids"]
            if (
                not isinstance(backend_id, str)
                or not isinstance(model_ids, list)
                or any(not isinstance(model_id, str) for model_id in model_ids)
            ):
                raise ValueError("stored backend report provenance is invalid")
            values.append(BackendReportContext(backend_id=backend_id, model_ids=tuple(model_ids)))
        result = tuple(values)
        if tuple(backend.backend_id for backend in result) != tuple(
            sorted(backend.backend_id for backend in result)
        ):
            raise ValueError("stored backend report provenance is not sorted")
        return result

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

    def station(
        self, station_id: str, *, report_context: ReportContext | None = None
    ) -> StationStore:
        """返回一个工位的行作用域; 所有工位共享连接和写入锁。"""
        return StationStore(self._connection, station_id, self._lock, report_context)

    def pending_report_station_ids(self) -> tuple[str, ...]:
        """返回仍欠 Center 报告的工位, 包括已从当前配置移除的历史工位。"""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT DISTINCT station_id
                  FROM local_report_queue
                 WHERE sent_at IS NULL AND superseded_at IS NULL
                 ORDER BY station_id
                """
            ).fetchall()
        return tuple(str(row["station_id"]) for row in rows)

    def disposal(self) -> LocalDisposalLedger:
        """返回该主机唯一的持久连接器写入账本。"""
        return LocalDisposalLedger(self._connection, self._lock)

    def configuration(self) -> LocalConfigurationStore:
        """返回该主机的原子最后确认配置存储。"""
        return LocalConfigurationStore(self._connection, self._lock)

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
