"""由 ``local_state`` 拥有的唯一持久处置账本。

连接器适配器可以在这个小存储接缝上转换结构化结果,但不能拥有第二份幂等表。主键是工位和调用方
拥有的幂等键,跨进程重启和中心离线保持不变。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from threading import RLock

from nvsop_contracts import ReportedDisposal


@dataclass(frozen=True, slots=True)
class DisposalIntent:
    station_id: str
    idempotency_key: str
    connector_id: str
    point_id: str
    actor: str
    requested_state: str
    violation_ref: str | None = None
    violation_instance_id: int | None = None
    source: str = "connector"
    report_host_id: str | None = None

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value
            for value in (
                self.station_id,
                self.idempotency_key,
                self.connector_id,
                self.point_id,
                self.actor,
                self.requested_state,
            )
        ):
            raise ValueError("a disposal intent needs complete identity and target fields")
        if self.report_host_id is not None:
            if not self.report_host_id:
                raise ValueError("reported disposal host id must not be empty")
            if not self.violation_ref or not self.source:
                raise ValueError("a reported physical disposal needs violation and source")
            if (
                isinstance(self.violation_instance_id, bool)
                or not isinstance(self.violation_instance_id, int)
                or self.violation_instance_id < 0
            ):
                raise ValueError("a reported physical disposal needs an instance id")


DISPOSAL_ACTION_RECORD = "record"
DISPOSAL_ACTION_FRONTEND_ALERT = "frontend_alert"
DISPOSAL_ACTION_WRITE_OUTPUT = "write_output"


@dataclass(frozen=True, slots=True)
class LocalDisposalRequest:
    idempotency_key: str
    violation_ref: str
    instance_id: int
    action_kind: str
    actor: str
    source: str


@dataclass(frozen=True, slots=True)
class LocalDisposalIntent(LocalDisposalRequest):
    station_id: str


@dataclass(frozen=True, slots=True)
class StoredDisposalResult:
    kind: str
    detail: str | None
    at: float


@dataclass(frozen=True, slots=True)
class DisposalClaim:
    """为一次物理尝试预留处置意图后的结果。"""

    claimed: bool
    result: StoredDisposalResult | None
    reason: str | None = None


DISPOSAL_RESULT_WRITTEN = "written"
"""A confirmed physical write; the idempotency key is terminal."""

DISPOSAL_RESULT_UNKNOWN = "unknown"
"""A lease expired without a durable physical outcome; never replay implicitly."""

_TERMINAL_RESULT_KINDS = frozenset({DISPOSAL_RESULT_WRITTEN, DISPOSAL_RESULT_UNKNOWN})

_DEFAULT_LEDGER_LOCK = RLock()
"""Serialise users sharing one sqlite connection unless the owner supplies its lock."""


class LocalDisposalLedger:
    """本地唯一写入去重权威的 SQLite 实现。

    每次读改写都由 SQLite immediate 事务保护。进程锁用于通常的推理机布局:工位线程和连接器线程共享
    一个 ``sqlite3.Connection``;SQLite 数据库锁继续承担跨进程保护。
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        lock: AbstractContextManager[object] | None = None,
    ) -> None:
        self._connection = connection
        self._lock = lock or _DEFAULT_LEDGER_LOCK

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        with self._lock:
            owns_transaction = not self._connection.in_transaction
            if owns_transaction:
                self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                if owns_transaction:
                    self._connection.execute("ROLLBACK")
                raise
            else:
                if owns_transaction:
                    self._connection.execute("COMMIT")

    def ensure_intent(self, intent: DisposalIntent) -> None:
        """插入意图,或拒绝把同一幂等键复用于不同目标。"""
        with self._write_transaction():
            self._ensure_intent_locked(intent)

    def _ensure_intent_locked(self, intent: DisposalIntent) -> None:
        self._connection.execute(
            """
            INSERT INTO local_disposal
                (station_id, idempotency_key, connector_id, point_id, actor, requested_state,
                 action_kind, violation_ref, violation_instance_id, source, report_host_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (station_id, idempotency_key) DO NOTHING
            """,
            (
                intent.station_id,
                intent.idempotency_key,
                intent.connector_id,
                intent.point_id,
                intent.actor,
                intent.requested_state,
                DISPOSAL_ACTION_WRITE_OUTPUT,
                intent.violation_ref,
                intent.violation_instance_id,
                intent.source,
                intent.report_host_id,
            ),
        )
        row = self._connection.execute(
            """
            SELECT connector_id, point_id, actor, requested_state, action_kind,
                   violation_ref, violation_instance_id, source, report_host_id
              FROM local_disposal
             WHERE station_id = ? AND idempotency_key = ?
            """,
            (intent.station_id, intent.idempotency_key),
        ).fetchone()
        if row is None:
            raise RuntimeError("disposal intent disappeared after insert")
        if tuple(row) != (
            intent.connector_id,
            intent.point_id,
            intent.actor,
            intent.requested_state,
            DISPOSAL_ACTION_WRITE_OUTPUT,
            intent.violation_ref,
            intent.violation_instance_id,
            intent.source,
            intent.report_host_id,
        ):
            raise ValueError("idempotency key was reused for a different disposal intent")

    def ensure_local(
        self, station_id: str, request: LocalDisposalRequest, *, host_id: str | None
    ) -> LocalDisposalIntent | None:
        intent = LocalDisposalIntent(
            request.idempotency_key,
            request.violation_ref,
            request.instance_id,
            request.action_kind,
            request.actor,
            request.source,
            station_id,
        )
        with self._write_transaction():
            inserted = (
                self._connection.execute(
                    """
                INSERT INTO local_disposal
                    (station_id,idempotency_key,action_kind,violation_ref,violation_instance_id,
                     source,actor,report_host_id)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT (station_id,idempotency_key) DO NOTHING
                """,
                    (
                        station_id,
                        request.idempotency_key,
                        request.action_kind,
                        request.violation_ref,
                        request.instance_id,
                        request.source,
                        request.actor,
                        host_id,
                    ),
                ).rowcount
                == 1
            )
            row = self._connection.execute(
                """SELECT action_kind,violation_ref,violation_instance_id FROM local_disposal
                     WHERE station_id=? AND idempotency_key=?""",
                (station_id, request.idempotency_key),
            ).fetchone()
            if row is None or tuple(row) != (
                request.action_kind,
                request.violation_ref,
                request.instance_id,
            ):
                raise ValueError("idempotency key was reused for a different local disposal")
        return intent if inserted else None

    def pending_local(self, station_id: str) -> tuple[LocalDisposalIntent, ...]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT station_id,idempotency_key,violation_ref,violation_instance_id,
                          action_kind,actor,source FROM local_disposal
                     WHERE station_id=? AND action_kind!='write_output' AND result_kind IS NULL
                     ORDER BY disposal_id""",
                (station_id,),
            ).fetchall()
        return tuple(_local_intent(row) for row in rows)

    def claim_local(self, intent: LocalDisposalIntent, *, now: float) -> bool:
        with self._write_transaction():
            return (
                self._connection.execute(
                    """UPDATE local_disposal SET attempts=attempts+1,last_attempt_at=?
                     WHERE station_id=? AND idempotency_key=? AND result_kind IS NULL""",
                    (now, intent.station_id, intent.idempotency_key),
                ).rowcount
                == 1
            )

    def record_local_result(
        self, intent: LocalDisposalIntent, result: StoredDisposalResult
    ) -> bool:
        with self._write_transaction():
            return (
                self._connection.execute(
                    """UPDATE local_disposal SET result_kind=?,result_detail=?,result_at=?
                     WHERE station_id=? AND idempotency_key=? AND result_kind IS NULL""",
                    (
                        result.kind,
                        result.detail,
                        result.at,
                        intent.station_id,
                        intent.idempotency_key,
                    ),
                ).rowcount
                == 1
            )

    def pending_report_ids(self, *, limit: int | None = None) -> tuple[int, ...]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT disposal_id FROM local_disposal
                     WHERE report_host_id IS NOT NULL AND result_kind IS NOT NULL
                       AND sent_at IS NULL ORDER BY disposal_id LIMIT ?""",
                (-1 if limit is None else limit,),
            ).fetchall()
        return tuple(int(row[0]) for row in rows)

    def report(self, disposal_id: int, *, reported_at: str) -> ReportedDisposal:
        with self._write_transaction():
            self._connection.execute(
                """UPDATE local_disposal SET report_reported_at=?
                     WHERE disposal_id=? AND report_reported_at IS NULL AND sent_at IS NULL""",
                (reported_at, disposal_id),
            )
            row = self._connection.execute(
                """SELECT station_id,idempotency_key,violation_ref,violation_instance_id,
                          action_kind,actor,source,result_kind,result_detail,result_at,attempts,
                          report_host_id,report_reported_at FROM local_disposal
                     WHERE disposal_id=? AND report_host_id IS NOT NULL
                       AND result_kind IS NOT NULL AND sent_at IS NULL""",
                (disposal_id,),
            ).fetchone()
        if row is None:
            raise ValueError("pending disposal report does not exist")
        event_id = f"{row['report_host_id']}:disposal:{disposal_id}"
        return ReportedDisposal(
            event_id=event_id,
            host_id=str(row["report_host_id"]),
            station_id=str(row["station_id"]),
            instance_id=int(row["violation_instance_id"]),
            idempotency_key=str(row["idempotency_key"]),
            violation_ref=str(row["violation_ref"]),
            action_kind=str(row["action_kind"]),
            actor=str(row["actor"]),
            source=str(row["source"]),
            result_kind=str(row["result_kind"]),
            result_detail=row["result_detail"],
            result_at=float(row["result_at"]),
            attempts=int(row["attempts"]),
            reported_at=str(row["report_reported_at"]),
        )

    def mark_reported(self, disposal_id: int, *, at: float) -> None:
        with self._write_transaction():
            self._connection.execute(
                "UPDATE local_disposal SET sent_at=? WHERE disposal_id=?", (at, disposal_id)
            )

    def record_report_failure(self, disposal_id: int, *, at: float, error: str) -> None:
        with self._write_transaction():
            self._connection.execute(
                """UPDATE local_disposal SET report_attempts=report_attempts+1,
                       report_last_attempt_at=?,report_last_error=? WHERE disposal_id=?""",
                (at, error, disposal_id),
            )

    def result_for(self, station_id: str, idempotency_key: str) -> StoredDisposalResult | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT result_kind, result_detail, result_at
                  FROM local_disposal
                 WHERE station_id = ? AND idempotency_key = ?
                """,
                (station_id, idempotency_key),
            ).fetchone()
        return _stored_result(row)

    def claim(self, intent: DisposalIntent, *, now: float, lease_seconds: float) -> DisposalClaim:
        """预留一次物理尝试,或返回已经知道的持久结果。

        ``failed`` 和 ``timed_out`` 可以重试; ``written`` 是终态。租约过期会写入 ``unknown``,
        此时物理结果不可知, 账本不能静默重复操作。
        """
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self._write_transaction():
            self._ensure_intent_locked(intent)
            row = self._connection.execute(
                """
                SELECT result_kind, result_detail, result_at, lease_until
                  FROM local_disposal
                 WHERE station_id = ? AND idempotency_key = ?
                """,
                (intent.station_id, intent.idempotency_key),
            ).fetchone()
            if row is None:
                raise RuntimeError("disposal intent disappeared before claim")

            result_kind, result_detail, result_at, lease_until = row
            if result_kind is not None:
                if result_kind in _TERMINAL_RESULT_KINDS:
                    return DisposalClaim(
                        claimed=False,
                        result=StoredDisposalResult(
                            kind=result_kind,
                            detail=result_detail,
                            at=result_at,
                        ),
                        reason="already_recorded",
                    )
                # A failed or timed-out adapter call did not establish a successful physical
                # write.  Clear only that result while holding the write transaction; the next
                # UPDATE below creates the new lease atomically.
                self._connection.execute(
                    """
                    UPDATE local_disposal
                       SET result_kind = NULL,
                           result_detail = NULL,
                           result_at = NULL,
                           lease_until = NULL
                     WHERE station_id = ? AND idempotency_key = ?
                    """,
                    (intent.station_id, intent.idempotency_key),
                )
                lease_until = None

            if lease_until is not None:
                if lease_until > now:
                    return DisposalClaim(claimed=False, result=None, reason="lease_active")
                expired = self._connection.execute(
                    """
                    UPDATE local_disposal
                       SET result_kind = ?,
                           result_detail = ?,
                           result_at = ?,
                           lease_until = NULL
                     WHERE station_id = ? AND idempotency_key = ? AND result_kind IS NULL
                    """,
                    (
                        DISPOSAL_RESULT_UNKNOWN,
                        "disposal attempt lease expired; physical outcome is unknown",
                        now,
                        intent.station_id,
                        intent.idempotency_key,
                    ),
                )
                if expired.rowcount != 1:
                    current = self._connection.execute(
                        """
                        SELECT result_kind, result_detail, result_at
                          FROM local_disposal
                         WHERE station_id = ? AND idempotency_key = ?
                        """,
                        (intent.station_id, intent.idempotency_key),
                    ).fetchone()
                    stored = _stored_result(current)
                    if stored is not None and stored.kind in _TERMINAL_RESULT_KINDS:
                        return DisposalClaim(
                            claimed=False,
                            result=stored,
                            reason="already_recorded",
                        )
                    raise RuntimeError("expired disposal lease could not be closed")
                return DisposalClaim(
                    claimed=False,
                    result=StoredDisposalResult(
                        kind=DISPOSAL_RESULT_UNKNOWN,
                        detail="disposal attempt lease expired; physical outcome is unknown",
                        at=now,
                    ),
                    reason="lease_expired",
                )

            updated = self._connection.execute(
                """
                UPDATE local_disposal
                   SET attempts = attempts + 1, last_attempt_at = ?, lease_until = ?
                 WHERE station_id = ? AND idempotency_key = ? AND result_kind IS NULL
                """,
                (now, now + lease_seconds, intent.station_id, intent.idempotency_key),
            )
            if updated.rowcount != 1:
                raise RuntimeError("disposal claim was lost before lease reservation")
            return DisposalClaim(claimed=True, result=None)

    def record_result(self, intent: DisposalIntent, *, result: StoredDisposalResult) -> None:
        """记录第一次结构化结果并关闭活动租约。"""
        with self._write_transaction():
            self._ensure_intent_locked(intent)
            updated = self._connection.execute(
                """
                UPDATE local_disposal
                   SET result_kind = ?, result_detail = ?, result_at = ?, lease_until = NULL
                 WHERE station_id = ? AND idempotency_key = ? AND result_kind IS NULL
                """,
                (
                    result.kind,
                    result.detail,
                    result.at,
                    intent.station_id,
                    intent.idempotency_key,
                ),
            )
            if updated.rowcount == 1:
                return
            current = self._connection.execute(
                """
                SELECT result_kind, result_detail, result_at
                  FROM local_disposal
                 WHERE station_id = ? AND idempotency_key = ?
                """,
                (intent.station_id, intent.idempotency_key),
            ).fetchone()
            stored = _stored_result(current)
            if stored is not None and stored.kind in _TERMINAL_RESULT_KINDS:
                return
            raise RuntimeError("disposal result was already replaced by another attempt")


def _local_intent(row: sqlite3.Row) -> LocalDisposalIntent:
    return LocalDisposalIntent(
        idempotency_key=str(row["idempotency_key"]),
        violation_ref=str(row["violation_ref"]),
        instance_id=int(row["violation_instance_id"]),
        action_kind=str(row["action_kind"]),
        actor=str(row["actor"]),
        source=str(row["source"]),
        station_id=str(row["station_id"]),
    )


def _stored_result(row: sqlite3.Row | tuple[object, ...] | None) -> StoredDisposalResult | None:
    if row is None:
        return None
    raw_kind: object = row[0]
    if raw_kind is None:
        return None
    raw_detail: object = row[1]
    detail = raw_detail if raw_detail is None or isinstance(raw_detail, str) else str(raw_detail)
    raw_at: object = row[2]
    if raw_at is None:
        raise RuntimeError("stored disposal result has no timestamp")
    return StoredDisposalResult(kind=str(raw_kind), detail=detail, at=float(str(raw_at)))


__all__ = [
    "DISPOSAL_ACTION_FRONTEND_ALERT",
    "DISPOSAL_ACTION_RECORD",
    "DISPOSAL_RESULT_UNKNOWN",
    "DISPOSAL_RESULT_WRITTEN",
    "DisposalClaim",
    "DisposalIntent",
    "LocalDisposalIntent",
    "LocalDisposalLedger",
    "LocalDisposalRequest",
    "StoredDisposalResult",
]
