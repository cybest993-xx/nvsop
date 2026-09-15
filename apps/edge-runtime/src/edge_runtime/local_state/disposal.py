"""由 ``local_state`` 拥有的持久连接器处置账本。

连接器适配器可以在这个小存储接缝上转换结构化结果,但不能拥有第二份幂等表。主键是工位和调用方
拥有的幂等键,跨进程重启和中心离线保持不变。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from threading import RLock


@dataclass(frozen=True, slots=True)
class DisposalIntent:
    station_id: str
    idempotency_key: str
    connector_id: str
    point_id: str
    actor: str
    requested_state: str

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
"""已确认的物理写入; 同一幂等键进入终态。"""

DISPOSAL_RESULT_TIMED_OUT = "timed_out"
"""适配器超时且物理结果未知; 同一幂等键不能隐式重放。"""

DISPOSAL_RESULT_UNKNOWN = "unknown"
"""租约恢复时无法确认物理结果; 同一幂等键不能隐式重放。"""

_TERMINAL_RESULT_KINDS = frozenset(
    {DISPOSAL_RESULT_WRITTEN, DISPOSAL_RESULT_TIMED_OUT, DISPOSAL_RESULT_UNKNOWN}
)

_DEFAULT_LEDGER_LOCK = RLock()
"""串行化共享同一 SQLite 连接的调用方, 除非所有者提供自己的锁。"""


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
                (station_id, idempotency_key, connector_id, point_id, actor, requested_state)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (station_id, idempotency_key) DO NOTHING
            """,
            (
                intent.station_id,
                intent.idempotency_key,
                intent.connector_id,
                intent.point_id,
                intent.actor,
                intent.requested_state,
            ),
        )
        row = self._connection.execute(
            """
            SELECT connector_id, point_id, actor, requested_state
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
        ):
            raise ValueError("idempotency key was reused for a different disposal intent")

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

        ``failed`` 可以重试; ``written``、``timed_out`` 和租约过期写入的 ``unknown`` 都是终态。
        物理结果不可安全重建, 账本不能静默重复操作。
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
                # 只有明确可重试的失败结果会清除旧结果; 整个过程在写事务内完成,
                # 下面的 UPDATE 会原子地创建新租约。
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
    "DISPOSAL_RESULT_TIMED_OUT",
    "DISPOSAL_RESULT_UNKNOWN",
    "DISPOSAL_RESULT_WRITTEN",
    "DisposalClaim",
    "DisposalIntent",
    "LocalDisposalLedger",
    "StoredDisposalResult",
]
