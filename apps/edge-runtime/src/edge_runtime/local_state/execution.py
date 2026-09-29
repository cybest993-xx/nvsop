"""推理机本地的工位物理执行权事实与写入门禁依据。

中心在每次成功拉取配置时于同一请求事务内续期本机持有的租约, 随 host-scoped bundle 下发
(edge-autonomy.md §5.17)。本模块把该事实持久化为本机唯一权威: 工位 -> 中心签发的
``grant_id`` / ``holder_host_id`` / 绝对到期时刻。

它只保存中心签发的绝对到期时刻, 不维护本地倒计时或第二份有效期, 因此:

- 到期由写入边界按本机墙钟实时比较, 不依赖下一次中心请求 (AC1);
- 重启后同一绝对时刻仍然有效/失效, 重复或延迟到达的同一租约不会把它复活 (AC2);
- 中心离线不会延长本地期限, 因为期限来自中心而不是本机的在线时长 (AC2)。

S029 的能力/连接/执行权检查与写入门禁都读这一份事实, 不另建有效期。

时钟影响的核对结论 (#174 Outcome 要求): 重启、重复/延迟响应和中心离线都不会复活或延长授权;
但墙钟回拨 (NTP 阶跃校正、手工改时间) 会把比较点移回到期前, 从而重新放行。本机墙钟被信任为
单调可靠 (与 §5.17 “主机时钟的绝对时刻”一致), 该残余风险由部署时钟保证, 本模块不引入额外时钟
服务; `test_a_backward_clock_step_revives_an_expired_lease_is_the_documented_residual`
固化这一核对结果。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from threading import RLock

from nvsop_contracts import ExecutionLease


@dataclass(frozen=True, slots=True)
class ExecutionLeaseFact:
    """本机某个工位当前持有的中心确认执行权租约事实。"""

    station_id: str
    grant_id: str
    holder_host_id: str
    lease_expires_at: float
    """中心签发的绝对到期时刻, Unix 纪元秒 (UTC)。"""

    renewed_at: float
    """记录该事实时的本机墙钟, 供诊断与过期展示。"""


class ExecutionLeaseState(Enum):
    """一次写入前查到的执行权状态。"""

    ACTIVE = "active"
    EXPIRED = "expired"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class ExecutionAuthority:
    """写入边界与展示层共用的执行权事实视图。"""

    state: ExecutionLeaseState
    detail: str
    fact: ExecutionLeaseFact | None = None

    @property
    def authorized(self) -> bool:
        return self.state is ExecutionLeaseState.ACTIVE


_DEFAULT_LOCK = RLock()
"""通常的推理机布局里工位线程共享一个连接, 因此串行化本机读改写。"""


class LocalExecutionLeaseStore:
    """拥有 ``local_execution_lease`` 的 SQLite 实现, 只保存中心签发的租约事实。"""

    def __init__(
        self,
        connection: sqlite3.Connection,
        lock: AbstractContextManager[object] | None = None,
    ) -> None:
        self._connection = connection
        self._lock = lock or _DEFAULT_LOCK

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """立即事务; 已在外层事务中时加入它, 使配置确认与租约替换原子提交。"""
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

    def apply(self, leases: Sequence[ExecutionLease], *, observed_at: float) -> None:
        """用一次中心确认的 host-scoped 租约集合替换本机事实。

        集合本身就是权威: 未出现在集合中的工位立即失去本地执行权 (交权或移除)。本方法只记录
        中心签发的绝对到期时刻, 不读取时钟, 也不延长任何期限。
        """
        with self._transaction():
            self._apply_locked(leases, observed_at=observed_at)

    def _apply_locked(self, leases: Sequence[ExecutionLease], *, observed_at: float) -> None:
        station_ids = [lease.station_id for lease in leases]
        if len(set(station_ids)) != len(station_ids):
            raise ValueError("execution leases must be unique per station")
        retained = set(station_ids)
        for row in self._connection.execute(
            "SELECT station_id FROM local_execution_lease"
        ).fetchall():
            station_id = row[0]
            if station_id not in retained:
                self._connection.execute(
                    "DELETE FROM local_execution_lease WHERE station_id = ?", (station_id,)
                )
        for lease in leases:
            self._connection.execute(
                """
                INSERT INTO local_execution_lease
                    (station_id, grant_id, holder_host_id, lease_expires_at, renewed_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (station_id) DO UPDATE SET
                    grant_id = excluded.grant_id,
                    holder_host_id = excluded.holder_host_id,
                    lease_expires_at = excluded.lease_expires_at,
                    renewed_at = excluded.renewed_at
                """,
                (
                    lease.station_id,
                    lease.grant_id,
                    lease.holder_host_id,
                    _epoch_seconds(lease.lease_expires_at),
                    observed_at,
                ),
            )

    def fact(self, station_id: str) -> ExecutionLeaseFact | None:
        """返回工位当前的持久租约事实; 无中心确认租约时返回 None。"""
        with self._lock:
            row = self._connection.execute(
                """
                SELECT station_id, grant_id, holder_host_id, lease_expires_at, renewed_at
                  FROM local_execution_lease
                 WHERE station_id = ?
                """,
                (station_id,),
            ).fetchone()
        if row is None:
            return None
        return ExecutionLeaseFact(
            station_id=row[0],
            grant_id=row[1],
            holder_host_id=row[2],
            lease_expires_at=row[3],
            renewed_at=row[4],
        )

    def status(self, station_id: str, *, now: float) -> ExecutionAuthority:
        """按本机墙钟返回工位当前的执行权状态; 到期判断发生在每次调用, 不缓存。"""
        fact = self.fact(station_id)
        if fact is None:
            return ExecutionAuthority(
                state=ExecutionLeaseState.MISSING,
                detail="本工位没有中心确认的物理执行权租约",
            )
        if now >= fact.lease_expires_at:
            return ExecutionAuthority(
                state=ExecutionLeaseState.EXPIRED,
                detail=f"物理执行权租约已于 {_isoformat(fact.lease_expires_at)} 到期",
                fact=fact,
            )
        return ExecutionAuthority(
            state=ExecutionLeaseState.ACTIVE,
            detail="",
            fact=fact,
        )


def _epoch_seconds(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.timestamp()


def _isoformat(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "ExecutionAuthority",
    "ExecutionLeaseFact",
    "ExecutionLeaseState",
    "LocalExecutionLeaseStore",
]
